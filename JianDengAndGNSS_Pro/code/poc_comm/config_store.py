# -*- coding: utf-8 -*-
import _thread
try:
	import ujson as json
except ImportError:
	import json
try:
	import uos as os
except ImportError:
	import os

CONFIG_PATH = "/usr/poc_config.json"
DEFAULT_CONFIG = {
	"version": 4,
	"device_id": None,
	"hardware_version": None,
	"identity_confirmed": False,
	"ota_request_id": 0,
	"ota_pending": None,
	"ota_completed_tasks": [],
}


def _validated(values):
	result = DEFAULT_CONFIG.copy()
	if not isinstance(values, dict):
		return result
	for key in ("device_id", "hardware_version"):
		value = values.get(key)
		if isinstance(value, str) and value.strip():
			result[key] = value.strip()
	result["identity_confirmed"] = (
		values.get("identity_confirmed") is True and
		bool(result["device_id"]) and bool(result["hardware_version"]))
	try:
		request_id = int(values.get("ota_request_id", 0))
		if 0 <= request_id <= 65535:
			result["ota_request_id"] = request_id
	except Exception:
		pass
	pending = values.get("ota_pending")
	if isinstance(pending, dict):
		try:
			task_id = str(pending.get("task_id") or "").strip()
			target = str(pending.get("target_version") or "").strip()
			if task_id and target:
				result["ota_pending"] = {
					"task_id": task_id,
					"request_id": int(pending.get("request_id") or 0) & 0xFFFF,
					"target_version": target,
					"stage": str(pending.get("stage") or "rebooting"),
					"start_time": int(pending.get("start_time") or 0),
					"pending_report": bool(pending.get("pending_report", True)),
				}
		except Exception:
			pass
	completed = values.get("ota_completed_tasks")
	if isinstance(completed, (list, tuple)):
		items = []
		for item in completed[-8:]:
			if not isinstance(item, dict):
				continue
			task_id = str(item.get("task_id") or "").strip()
			if task_id:
				items.append({"task_id": task_id,
					"status": str(item.get("status") or "unknown")})
		result["ota_completed_tasks"] = items
	return result


class PersistentConfig:
	def __init__(self, path=CONFIG_PATH):
		self.path = str(path)
		self.temp_path = self.path + ".tmp"
		self.backup_path = self.path + ".bak"
		self._lock = _thread.allocate_lock()
		self._values = self._load()
		self._identity_logger = None

	def set_identity_logger(self, logger):
		self._identity_logger = logger

	def _read_file(self, path):
		with open(path, "r") as source:
			return _validated(json.loads(source.read()))

	def _load(self):
		try:
			return self._read_file(self.path)
		except Exception:
			try:
				return self._read_file(self.backup_path)
			except Exception:
				return DEFAULT_CONFIG.copy()

	def snapshot(self):
		self._lock.acquire()
		try:
			return self._values.copy()
		finally:
			self._lock.release()

	def get(self, key, default=None):
		self._lock.acquire()
		try:
			return self._values.get(key, default)
		finally:
			self._lock.release()

	def _write_locked(self, values):
		self._remove(self.temp_path)
		with open(self.temp_path, "w") as output:
			output.write(json.dumps(values))
		self._remove(self.backup_path)
		try:
			os.rename(self.path, self.backup_path)
		except Exception:
			pass
		try:
			os.rename(self.temp_path, self.path)
		except Exception:
			try:
				os.rename(self.backup_path, self.path)
			except Exception:
				pass
			raise
		self._remove(self.backup_path)

	@staticmethod
	def _remove(path):
		try:
			os.remove(path)
		except Exception:
			pass

	def update(self, **changes):
		self._lock.acquire()
		try:
			candidate = self._values.copy()
			candidate.update(changes)
			candidate = _validated(candidate)
			if candidate != self._values:
				self._write_locked(candidate)
				self._values = candidate
			return True
		except Exception:
			return False
		finally:
			self._lock.release()

	def read_identity(self):
		self._lock.acquire()
		try:
			values = self._read_file(self.path)
			return (values["device_id"], values["hardware_version"],
				values["identity_confirmed"])
		finally:
			self._lock.release()

	def _write_identity(self, device_id, hardware_version, confirmed):
		self._lock.acquire()
		try:
			candidate = self._values.copy()
			candidate.update(device_id=device_id,
				hardware_version=hardware_version,
				identity_confirmed=confirmed)
			candidate = _validated(candidate)
			self._write_locked(candidate)
			self._values = candidate
			return True
		except Exception as error:
			if self._identity_logger is not None:
				self._identity_logger("身份保存失败：{}".format(error))
			return False
		finally:
			self._lock.release()

	def save_identity(self, device_id, hardware_version):
		if not (isinstance(device_id, str) and device_id.strip() and
				isinstance(hardware_version, str) and hardware_version.strip()):
			return False
		return self._write_identity(device_id.strip(), hardware_version.strip(), False)

	def confirm_identity(self, device_id, hardware_version):
		try:
			actual = self.read_identity()
			if actual[:2] != (device_id, hardware_version):
				return False
		except Exception:
			return False
		return self._write_identity(device_id, hardware_version, True)

	def clear_identity(self):
		return self._write_identity(None, None, False)

	def next_ota_request_id(self):
		value = int(self.get("ota_request_id", 0)) + 1
		if value > 65535:
			value = 1
		if not self.update(ota_request_id=value):
			raise RuntimeError("OTA request_id保存失败")
		return value

	def save_ota_pending(self, metadata):
		return self.update(ota_pending=dict(metadata))

	def clear_ota_pending(self):
		return self.update(ota_pending=None)

	def complete_ota_task(self, task_id, status):
		items = [item for item in self.get("ota_completed_tasks", [])
			if str(item.get("task_id")) != str(task_id)]
		items.append({"task_id": str(task_id), "status": str(status)})
		return self.update(ota_completed_tasks=items[-8:])

	def completed_ota_status(self, task_id):
		for item in self.get("ota_completed_tasks", []):
			if str(item.get("task_id")) == str(task_id):
				return item.get("status")
		return None
