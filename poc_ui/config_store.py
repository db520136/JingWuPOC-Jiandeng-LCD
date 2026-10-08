# -*- coding: utf-8 -*-

import _thread

try:
	import utime
except ImportError:
	import time as _time

	class _TimeCompat:
		@staticmethod
		def ticks_ms():
			return int(_time.monotonic() * 1000)

		@staticmethod
		def ticks_add(value, delta):
			return int(value) + int(delta)

		@staticmethod
		def ticks_diff(first, second):
			return int(first) - int(second)

	utime = _TimeCompat()

try:
	import ujson as json
except ImportError:
	import json

try:
	import uos as os
except ImportError:
	import os


CONFIG_PATH = "/ext/poc_config.json"
SETTING_SAVE_IDLE_MS = 60000
DEFERRED_SETTING_KEYS = (
	"audio_volume",
	"lamp_brightness",
	"screen_brightness",
	"double_click_enabled",
)
DEFAULT_CONFIG = {
	"version": 3,
	"police_no": None,
	"audio_volume": 11,
	"lamp_brightness": 42,
	"screen_brightness": 100,
	"double_click_enabled": True,
	"battery_percent": None,
	"battery_high_active": False,
	"battery_full_confirmed": False,
	"ota_request_id": 0,
	"ota_pending": None,
	"ota_completed_tasks": [],
}


def _valid_police_no(value):
	value = str(value or "")
	return (len(value) == 7 and value[0] in ("M", "F") and
		value[1:].isdigit())


def _validated(values):
	result = DEFAULT_CONFIG.copy()
	if not isinstance(values, dict):
		return result
	if _valid_police_no(values.get("police_no")):
		result["police_no"] = str(values["police_no"])
	try:
		volume = int(values.get("audio_volume"))
		if 0 <= volume <= 11:
			result["audio_volume"] = volume
	except Exception:
		pass
	for key in ("lamp_brightness", "screen_brightness"):
		try:
			value = int(values.get(key))
			if 5 <= value <= 100:
				result[key] = value
		except Exception:
			pass
	if isinstance(values.get("double_click_enabled"), bool):
		result["double_click_enabled"] = values["double_click_enabled"]
	try:
		percent = int(values.get("battery_percent"))
		if 1 <= percent <= 100:
			result["battery_percent"] = percent
	except Exception:
		pass
	for key in ("battery_high_active", "battery_full_confirmed"):
		if isinstance(values.get(key), bool):
			result[key] = values[key]
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
			target_version = str(pending.get("target_version") or "").strip()
			if task_id and target_version:
				result["ota_pending"] = {
					"task_id": task_id,
					"request_id": int(pending.get("request_id") or 0) & 0xFFFF,
					"target_version": target_version,
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
		self._pending_settings = {}
		self._settings_save_due_ms = None

	def _read_file(self, path):
		with open(path, "r") as source:
			return _validated(json.loads(source.read()))

	def _load(self):
		try:
			values = self._read_file(self.path)
			print("[配置] 已从外置Flash加载配置")
			return values
		except Exception:
			try:
				values = self._read_file(self.backup_path)
				print("[配置] 主配置不可用，已从备份恢复")
				return values
			except Exception:
				print("[配置] 未找到有效配置，使用首次开机默认值")
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

	@staticmethod
	def _remove(path):
		try:
			os.remove(path)
		except Exception:
			pass

	def _write_locked(self, values):
		self._remove(self.temp_path)
		with open(self.temp_path, "w") as output:
			output.write(json.dumps(values))
		# 保留上一份完整文件；若第二次改名时断电，下次启动可读备份。
		self._remove(self.backup_path)
		try:
			os.rename(self.path, self.backup_path)
		except Exception:
			pass
		try:
			os.rename(self.temp_path, self.path)
		except Exception:
			# 新文件接管失败时尽量恢复旧文件。
			try:
				os.rename(self.backup_path, self.path)
			except Exception:
				pass
			raise
		self._remove(self.backup_path)

	def update(self, **changes):
		self._lock.acquire()
		try:
			candidate = self._values.copy()
			candidate.update(changes)
			candidate = _validated(candidate)
			if candidate == self._values:
				return True
			self._write_locked(candidate)
			self._values = candidate
			return True
		except Exception as error:
			print("[配置] 保存失败：{}".format(error))
			return False
		finally:
			self._lock.release()

	def defer_settings_update(self, **changes):
		"""设置立即生效，最后一次变动满一分钟后再合并写入Flash。"""
		self._lock.acquire()
		try:
			accepted = {}
			for key, value in changes.items():
				if key in DEFERRED_SETTING_KEYS:
					accepted[key] = value
			if not accepted:
				return False
			candidate = self._values.copy()
			candidate.update(self._pending_settings)
			before = candidate.copy()
			candidate.update(accepted)
			validated = _validated(candidate)
			changed = False
			for key in accepted:
				value = validated[key]
				if before.get(key) != value:
					changed = True
					self._pending_settings[key] = value
			if not changed:
				return True
			self._settings_save_due_ms = utime.ticks_add(
				utime.ticks_ms(), SETTING_SAVE_IDLE_MS)
			return True
		finally:
			self._lock.release()

	def flush_deferred_settings(self, now_ms=None):
		"""由现有低优先级Worker周期调用；未到期时不访问Flash。"""
		self._lock.acquire()
		try:
			if not self._pending_settings or self._settings_save_due_ms is None:
				return None
			if now_ms is None:
				now_ms = utime.ticks_ms()
			if utime.ticks_diff(now_ms, self._settings_save_due_ms) < 0:
				return None
			candidate = self._values.copy()
			candidate.update(self._pending_settings)
			candidate = _validated(candidate)
			changed_keys = tuple(self._pending_settings.keys())
			if candidate != self._values:
				self._write_locked(candidate)
				self._values = candidate
			self._pending_settings = {}
			self._settings_save_due_ms = None
			print("[配置] 设置连续60秒无变动，已持久化：{}".format(
				",".join(changed_keys)))
			return True
		except Exception as error:
			self._settings_save_due_ms = utime.ticks_add(
				utime.ticks_ms(), SETTING_SAVE_IDLE_MS)
			print("[配置] 延迟保存失败，60秒后重试：{}".format(error))
			return False
		finally:
			self._lock.release()

	def next_ota_request_id(self):
		current = int(self.get("ota_request_id", 0))
		value = 1 if current >= 65535 else current + 1
		if not self.update(ota_request_id=value):
			raise RuntimeError("OTA request_id保存失败")
		return value

	def save_ota_pending(self, metadata):
		return self.update(ota_pending=dict(metadata))

	def clear_ota_pending(self):
		return self.update(ota_pending=None)

	def complete_ota_task(self, task_id, status):
		items = list(self.get("ota_completed_tasks", []))
		items = [item for item in items
			if str(item.get("task_id")) != str(task_id)]
		items.append({"task_id": str(task_id), "status": str(status)})
		return self.update(ota_completed_tasks=items[-8:])

	def completed_ota_status(self, task_id):
		for item in self.get("ota_completed_tasks", []):
			if str(item.get("task_id")) == str(task_id):
				return item.get("status")
		return None
