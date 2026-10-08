# -*- coding: utf-8 -*-
"""一次性HTTP名单获取线程。"""

import _thread
import utime

try:
	import request
except ImportError:
	request = None


HTTP_REQUEST_TIMEOUT_S = 5


def _group_id(value):
	number = int(value)
	if number < 0 or number > 0xFFFFFFFF:
		raise ValueError("组ID超出uint32范围")
	return number


def _first_value(item, keys):
	for key in keys:
		value = item.get(key)
		if value is not None:
			return value
	return None


def parse_directory_payload(payload, device_id, police_no=None):
	data = payload.get("data") if isinstance(payload, dict) else None
	if not isinstance(data, list):
		raise RuntimeError("HTTP响应缺少data列表")
	groups = []
	people_by_code = {}
	people_order = []
	poc_access_values = []
	local_device_id = str(device_id).strip()
	local_police_no = str(police_no or "").strip().upper()
	for item in data:
		if not isinstance(item, dict):
			continue
		raw_id = _first_value(item, (
			"groupId", "groupID", "grouID", "grouId", "group_id", "id"))
		if raw_id is None:
			continue
		group_id = _group_id(raw_id)
		name = _first_value(item, ("groupName", "group_name", "name"))
		groups.append({"id": str(group_id), "name": str(name or group_id)})
		users = _first_value(item, (
			"groupUsers", "users", "members", "groupMembers"))
		if not isinstance(users, list):
			users = []
		for user in users:
			if not isinstance(user, dict):
				continue
			code = _first_value(user, ("policeCode", "policeNo", "police_no"))
			device = _first_value(user, ("deviceId", "deviceID", "device_id"))
			device = "" if device is None else str(device).strip()
			if device == local_device_id:
				access_text = str(user.get("poc_enable") or "").strip().upper()
				if access_text == "ON":
					poc_access_values.append(True)
				elif access_text == "OFF":
					poc_access_values.append(False)
			if code is None or not device:
				continue
			code = str(code).strip()
			if not code:
				continue
			key = code.upper()
			person = {
				"key": code,
				"name": str(user.get("policeName") or user.get("name") or code),
				"police_code": code,
				"device_id": device,
				"online": bool(user.get("online") or user.get("isOnline")),
				"is_self": bool(local_police_no and key == local_police_no),
			}
			if key not in people_by_code:
				people_order.append(key)
				people_by_code[key] = person
			elif not people_by_code[key]["online"] and person["online"]:
				people_by_code[key] = person
	if not groups:
		raise RuntimeError("HTTP没有可用组")
	if False in poc_access_values:
		poc_enabled = False
	elif True in poc_access_values:
		poc_enabled = True
	else:
		poc_enabled = None
	return groups, [people_by_code[key] for key in people_order], poc_enabled


def fetch_directory(url, device_id, police_no=None, timeout_s=HTTP_REQUEST_TIMEOUT_S,
					request_module=None):
	http = request if request_module is None else request_module
	if http is None:
		raise RuntimeError("request模块不可用")
	response = None
	try:
		response = http.get(
			url, headers={"Accept": "application/json"}, timeout=int(timeout_s))
		if int(response.status_code) != 200:
			raise RuntimeError("HTTP状态码={}".format(response.status_code))
		payload = response.json()
		print("[通信][HTTP诊断] 成员信息完整数据：{}".format(payload))
		return parse_directory_payload(payload, device_id, police_no)
	finally:
		if response is not None:
			try:
				response.close()
			except Exception:
				pass


class HTTPDirectoryWorker:
	"""每个实例只运行一次，请求结束后线程自然退出。"""

	def __init__(self, url, device_id, callback,
				 timeout_s=HTTP_REQUEST_TIMEOUT_S, request_module=None):
		self.url = str(url)
		self.device_id = str(device_id)
		self.callback = callback
		self.timeout_s = int(timeout_s)
		self.request_module = request_module
		self.thread_id = None
		self.running = False

	def start(self, request_id, police_no=None):
		if self.running:
			return False
		self.running = True
		try:
			self.thread_id = _thread.start_new_thread(
				self._run, (int(request_id), police_no))
			return True
		except Exception:
			self.running = False
			self.thread_id = None
			raise

	def _run(self, request_id, police_no):
		started = utime.ticks_ms()
		result = None
		error = None
		try:
			result = fetch_directory(
				self.url, self.device_id, police_no, self.timeout_s,
				self.request_module)
		except Exception as caught:
			error = str(caught)
		finally:
			self.running = False
			self.thread_id = None
			elapsed = utime.ticks_diff(utime.ticks_ms(), started)
			try:
				self.callback({
					"request_id": int(request_id),
					"ok": error is None,
					"result": result,
					"error": error,
					"elapsed_ms": elapsed,
				})
			except Exception as callback_error:
				print("[通信][HTTP] 结果回传失败：{}".format(callback_error))
