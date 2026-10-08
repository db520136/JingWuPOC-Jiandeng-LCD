# -*- coding: utf-8 -*-

import _thread

try:
	import ujson as json
except ImportError:
	import json
try:
	import utime
except ImportError:
	import time as _time

	class _UTime:
		@staticmethod
		def ticks_ms():
			return int(_time.monotonic() * 1000)

		@staticmethod
		def ticks_add(value, delta):
			return value + delta

		@staticmethod
		def ticks_diff(first, second):
			return first - second

		@staticmethod
		def sleep_ms(value):
			_time.sleep(value / 1000.0)

		@staticmethod
		def time():
			return int(_time.time())

	utime = _UTime()

try:
	from umqtt import MQTTClient
except ImportError:
	MQTTClient = None
try:
	import app_fota
except ImportError:
	app_fota = None
try:
	from misc import Power
except ImportError:
	Power = None


MQTT_RECONNECT_MAX_RETRIES = 3
MQTT_RECONNECT_INTERVAL_MS = 2000
OTA_QUERY_TIMEOUT_MS = 3000
OTA_QUERY_RECONNECT_ROUNDS = 1
OTA_DOWNLOAD_MAX_ATTEMPTS = 3
OTA_DOWNLOAD_RETRY_INTERVAL_MS = 2000
OTA_ERROR_POPUP_DURATION_MS = 2000
OTA_DOWNLOAD_COMPLETE_DURATION_MS = 2000
OTA_REBOOT_READY_DURATION_MS = 2000
OTA_PROGRESS_DOT_INTERVAL_MS = 1000
OTA_PROGRESS_DOT_MAX = 6
OTA_POST_BOOT_NETWORK_WAIT_MS = 90000


def _version_parts(value):
	text = str(value or "").strip()
	if text[:1] in ("v", "V"):
		text = text[1:]
	parts = text.split(".")
	if not parts or any(not item.isdigit() for item in parts):
		return None
	return tuple(int(item) for item in parts)


def _is_newer_version(target, current):
	target_parts = _version_parts(target)
	current_parts = _version_parts(current)
	if target_parts is None or current_parts is None:
		return str(target) != str(current)
	length = max(len(target_parts), len(current_parts))
	target_parts += (0,) * (length - len(target_parts))
	current_parts += (0,) * (length - len(current_parts))
	return target_parts > current_parts


def _version_matches(value, allowed):
	if isinstance(allowed, str):
		allowed = [allowed]
	if not isinstance(allowed, (list, tuple)):
		return False
	return str(value) in [str(item) for item in allowed]


def _current_time_value():
	callback = getattr(utime, "time", None)
	return int(callback()) if callback is not None else int(utime.ticks_ms())


class OTAService:

	def __init__(self, config, state_store, communication_controller,
				 persistent_config, mqtt_factory=None, fota_factory=None,
				 restart_callback=None):
		self.config = dict(config)
		self.state = state_store
		self.communication = communication_controller
		self.persistent = persistent_config
		self.mqtt_factory = mqtt_factory or MQTTClient
		self.fota_factory = fota_factory
		self.restart_callback = restart_callback
		self._lock = _thread.allocate_lock()
		self._thread_active = False
		self._cancel_requested = False
		self._decision = None
		self._request_id = None
		self._response = None
		self._mqtt = None
		self._phase = "idle"
		self._task = None
		self._saved_group_id = None

	def enter_page(self):
		if not self._thread_active:
			self.state.update_upgrade_status("", "normal")

	def leave_page(self):
		self._lock.acquire()
		try:
			cancellable = self._thread_active and self._phase in (
				"querying", "waiting_response", "waiting_confirm")
			if cancellable:
				self._cancel_requested = True
				self._decision = "page_cancel"
				self._request_id = None
			client = self._mqtt if cancellable else None
		finally:
			self._lock.release()
		if client is not None:
			print("[OTA] 离开升级页面，取消查询并关闭MQTT")
			self._close_client(client)
			self.state.publish_ota_event("hide")
		return cancellable

	def start_query(self):
		self._lock.acquire()
		try:
			if self._thread_active:
				print("[OTA] 上一次OTA临时线程尚未退出，本次查询已忽略")
				return False
			self._thread_active = True
			self._cancel_requested = False
			self._decision = None
			self._response = None
			self._task = None
			self._phase = "querying"
			self._request_id = self.persistent.next_ota_request_id()
			request_id = self._request_id
		finally:
			self._lock.release()
		self.state.update_upgrade_status("", "normal")
		self.state.update_ota_runtime(
			thread_active=True, query_active=True, phase="querying")
		self.state.publish_ota_event("progress", "查询中... ...")
		print("[OTA][查询] 启动临时线程：request_id={}".format(request_id))
		try:
			_thread.start_new_thread(self._query_worker, (request_id,))
			return True
		except Exception as error:
			self._finish_thread()
			self._show_error("OTA线程启动失败")
			print("[OTA] 临时线程启动失败：{}".format(error))
			return False

	def confirm_update(self):
		self._lock.acquire()
		try:
			if self._phase != "waiting_confirm" or self._task is None:
				return False
			self._decision = "accept"
			return True
		finally:
			self._lock.release()

	def reject_update(self):
		self._lock.acquire()
		try:
			if self._phase != "waiting_confirm" or self._task is None:
				return False
			self._decision = "reject"
			return True
		finally:
			self._lock.release()

	def start_post_boot_report(self):
		pending = self.persistent.get("ota_pending")
		if not isinstance(pending, dict) or not pending.get("pending_report"):
			return False
		self._lock.acquire()
		try:
			if self._thread_active:
				return False
			self._thread_active = True
			self._phase = "post_boot_report"
		finally:
			self._lock.release()
		self.state.update_ota_runtime(thread_active=True, phase="post_boot_report")
		print("[OTA][启动确认] 发现待确认任务：{}".format(pending))
		_thread.start_new_thread(self._post_boot_worker, (dict(pending),))
		return True

	def _cancelled(self):
		self._lock.acquire()
		try:
			return self._cancel_requested
		finally:
			self._lock.release()

	def _set_phase(self, phase, query_active=None, busy=None):
		self._lock.acquire()
		try:
			self._phase = str(phase)
		finally:
			self._lock.release()
		self.state.update_ota_runtime(
			query_active=query_active, busy=busy, phase=phase)

	def _finish_thread(self):
		self._lock.acquire()
		try:
			self._thread_active = False
			self._mqtt = None
			if self._phase not in ("downloading", "rebooting"):
				self._phase = "idle"
			phase = self._phase
		finally:
			self._lock.release()
		self.state.update_ota_runtime(
			thread_active=False, query_active=False, phase=phase)

	def _show_error(self, text):
		self.state.publish_ota_event(
			"error", str(text), OTA_ERROR_POPUP_DURATION_MS)

	def _query_worker(self, request_id):
		client = None
		try:
			client = self._connect_with_retries()
			if client is None:
				if not self._cancelled():
					self._show_error("MQTT连接失败")
				return
			response = None
			wait_status = None
			wait_error = None
			total_rounds = OTA_QUERY_RECONNECT_ROUNDS + 1
			for query_round in range(total_rounds):
				if self._cancelled():
					return
				if query_round:
					reason = ("等待应答超时" if wait_status == "timeout"
						else "等待应答期间连接异常")
					print("[OTA][查询] 第{}轮{}，重连MQTT后再次查询".format(
						query_round, reason))
					self._close_client(client)
					client = self._connect_with_retries()
					if client is None:
						self._show_error("MQTT连接失败")
						return
				self._response = None
				payload = self._build_query_payload(request_id)
				client, published = self._publish_query_with_reconnect(
					client, payload)
				if not published:
					self._show_error("MQTT连接失败")
					return
				self._set_phase("waiting_response", query_active=True)
				print("[OTA][查询] 第{}/{}轮等待平台应答：超时={}ms".format(
					query_round + 1, total_rounds, OTA_QUERY_TIMEOUT_MS))
				wait_status, response, wait_error = self._wait_response(
					client, OTA_QUERY_TIMEOUT_MS)
				if wait_status == "response":
					print("[OTA][查询] 第{}/{}轮收到有效应答".format(
						query_round + 1, total_rounds))
					break
				if wait_status == "cancelled":
					return
				if wait_status == "timeout":
					print("[OTA][查询] 第{}/{}轮等待超时：{}ms内无有效应答".format(
						query_round + 1, total_rounds, OTA_QUERY_TIMEOUT_MS))
				else:
					print("[OTA][查询] 第{}/{}轮等待期间连接异常：{}".format(
						query_round + 1, total_rounds, wait_error))
			if self._cancelled():
				return
			if response is None:
				if wait_status == "error":
					print("[OTA][查询] 两轮查询均因连接异常未完成")
					self._show_error("查询失败，连接异常")
				else:
					print("[OTA][查询] 两轮查询均未收到有效应答")
					self._show_error("查询失败，无应答")
				return
			self._handle_query_response(client, response)
		except Exception as error:
			if not self._cancelled():
				print("[OTA] 查询流程异常：{}".format(error))
				self._show_error("查询失败")
		finally:
			if client is not None:
				self._close_client(client)
			self._finish_thread()
			print("[OTA] 临时线程已退出，MQTT资源已释放")

	def _build_query_payload(self, request_id):
		snapshot = self.state.get_snapshot()[1]
		payload = {
			"schema_version": "1.1",
			"deviceId": self.config["device_id"],
			"Information_sta": "query",
			"query_type": "latest_software",
			"request_id": int(request_id),
			"Software_Ver": snapshot["software_version"],
			"Hardware_Ver": snapshot["hardware_version"],
			"Firmware_Ver": snapshot["firmware_version"],
		}
		print("[OTA][查询] 发布版本查询：{}".format(payload))
		return payload

	def _new_client(self):
		if self.mqtt_factory is None:
			raise RuntimeError("umqtt模块不可用")
		client_id = "POC_OTA_{}".format(self.config["device_id"])
		client = self.mqtt_factory(
			client_id, self.config["mqtt_host"], int(self.config["mqtt_port"]),
			None, None, keepalive=60, ssl=False, reconn=False)
		client.set_callback(self._mqtt_callback)
		if hasattr(client, "error_register_cb"):
			client.error_register_cb(self._mqtt_error_callback)
		return client

	def _connect_with_retries(self, max_retries=None):
		retries = (MQTT_RECONNECT_MAX_RETRIES if max_retries is None
			else max(0, int(max_retries)))
		attempts = retries + 1
		for index in range(attempts):
			if self._cancelled():
				return None
			client = None
			try:
				print("[OTA][MQTT] 第{}/{}次连接：{}:{}".format(
					index + 1, attempts, self.config["mqtt_host"],
					self.config["mqtt_port"]))
				client = self._new_client()
				client.connect()
				client.subscribe(self._topic(), 1)
				self._lock.acquire()
				try:
					self._mqtt = client
				finally:
					self._lock.release()
				print("[OTA][MQTT] 连接并订阅成功：topic={} qos=1".format(
					self._topic()))
				return client
			except Exception as error:
				print("[OTA][MQTT] 第{}/{}次连接失败：{}".format(
					index + 1, attempts, error))
				self._close_client(client)
				if index + 1 < attempts:
					self._sleep_cancelable(MQTT_RECONNECT_INTERVAL_MS)
		return None

	def _topic(self):
		return "/deviceStatusInfo/poc/Information/OTA_MSG/{}".format(
			self.config["device_id"])

	def _publish(self, client, payload, label):
		try:
			body = json.dumps(payload)
			client.publish(self._topic(), body, 1)
			print("[OTA][MQTT] {}发布成功：qos=1 内容={}".format(label, body))
			return True
		except Exception as error:
			print("[OTA][MQTT] {}发布失败：{}".format(label, error))
			return False

	def _publish_query_with_reconnect(self, client, payload):
		if self._publish(client, payload, "版本查询"):
			return client, True
		for retry in range(1, MQTT_RECONNECT_MAX_RETRIES + 1):
			if self._cancelled():
				return client, False
			print("[OTA][MQTT] 版本查询发布失败，{}ms后第{}/{}次重连".format(
				MQTT_RECONNECT_INTERVAL_MS, retry,
				MQTT_RECONNECT_MAX_RETRIES))
			self._close_client(client)
			self._sleep_cancelable(MQTT_RECONNECT_INTERVAL_MS)
			client = self._connect_with_retries(0)
			if client is not None and self._publish(client, payload, "版本查询"):
				return client, True
		return client, False

	def _mqtt_callback(self, topic, message=None):
		if message is None:
			return
		try:
			topic_text = topic.decode() if hasattr(topic, "decode") else str(topic)
			message_text = (message.decode() if hasattr(message, "decode")
				else str(message))
			print("[OTA][MQTT] 收到原始消息：topic={} 内容={}".format(
				topic_text, message_text))
			data = json.loads(message_text)
		except Exception as error:
			print("[OTA][MQTT] 消息解析失败，已过滤：{}".format(error))
			return
		if topic_text != self._topic():
			print("[OTA][MQTT] topic不匹配，已过滤")
			return
		try:
			response_request_id = int(data.get("request_id", -1))
		except Exception:
			response_request_id = -1
		checks = (
			(data.get("Information_sta") == "report", "Information_sta不是report"),
			(data.get("report_type") == "latest_software", "report_type不匹配"),
			(str(data.get("deviceId")) == str(self.config["device_id"]), "deviceId不匹配"),
			(self._request_id is not None and
			 response_request_id == int(self._request_id), "request_id不匹配"),
		)
		for valid, reason in checks:
			if not valid:
				print("[OTA][MQTT] {}，已过滤".format(reason))
				return
		self._response = data
		print("[OTA][MQTT] 收到有效版本查询应答：request_id={}".format(
			self._request_id))

	def _mqtt_error_callback(self, error):
		print("[OTA][MQTT] 底层异常回调：{}".format(error))

	@staticmethod
	def _socket_object(client):
		for name in ("sock", "socket", "_sock", "_socket"):
			value = getattr(client, name, None)
			if value is not None:
				return value
		return None

	@staticmethod
	def _is_receive_timeout(error):
		code = getattr(error, "errno", None)
		if code is None:
			args = getattr(error, "args", ())
			if args:
				code = args[0]
		try:
			if int(code) in (11, 110, 116):
				return True
		except Exception:
			pass
		text = str(error).upper()
		return ("EAGAIN" in text or "EWOULDBLOCK" in text or
			"ETIMEDOUT" in text or "TIMED OUT" in text)

	@staticmethod
	def _set_receive_timeout(sock, remaining_ms):
		seconds = max(0.001, float(remaining_ms) / 1000.0)
		try:
			sock.settimeout(seconds)
		except Exception:
			# 少数固件只接受整数秒；向上取整避免提前超时。
			sock.settimeout(max(1, (int(remaining_ms) + 999) // 1000))

	def _wait_response(self, client, timeout_ms):
		deadline = utime.ticks_add(utime.ticks_ms(), int(timeout_ms))
		sock = self._socket_object(client)
		waiter = getattr(client, "wait_msg", None)
		if (waiter is not None and sock is not None and
				hasattr(sock, "settimeout")):
			while True:
				if self._cancelled():
					return "cancelled", None, None
				remaining = utime.ticks_diff(deadline, utime.ticks_ms())
				if remaining <= 0:
					return "timeout", None, None
				try:
					self._set_receive_timeout(sock, remaining)
					waiter()
				except Exception as error:
					if self._cancelled():
						return "cancelled", None, None
					if self._is_receive_timeout(error):
						if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
							return "timeout", None, None
						# 某些固件即使设置了超时仍可能立即返回EAGAIN，
						# 短暂让出CPU后继续使用同一个总截止时间。
						utime.sleep_ms(10)
						continue
					return "error", None, error
				if self._response is not None:
					return "response", self._response, None
				# 收到非目标MQTT报文时，按总截止时间继续阻塞等待。
				utime.sleep_ms(1)

		# 兼容拿不到底层Socket的umqtt封装；仍以同一个3秒截止时间为准。
		checker = getattr(client, "check_msg", None)
		if checker is None:
			return "error", None, RuntimeError("MQTT客户端无可用接收接口")
		while True:
			if self._cancelled():
				return "cancelled", None, None
			remaining = utime.ticks_diff(deadline, utime.ticks_ms())
			if remaining <= 0:
				return "timeout", None, None
			try:
				checker()
			except Exception as error:
				if self._cancelled():
					return "cancelled", None, None
				if not self._is_receive_timeout(error):
					return "error", None, error
			if self._response is not None:
				return "response", self._response, None
			utime.sleep_ms(min(50, remaining))

	def _validate_task(self, response):
		task_id = str(response.get("task_id") or "").strip()
		target_version = str(response.get("Upgrad_Software_Ver") or "").strip()
		files = response.get("file_url")
		if not task_id or not target_version or not isinstance(files, list) or not files:
			raise ValueError("升级任务字段不完整")
		normalized_files = []
		for item in files:
			if not isinstance(item, dict):
				raise ValueError("file_url文件项不是对象")
			url = str(item.get("url") or "").strip()
			file_name = str(item.get("file_name") or "").strip()
			if (not (url.startswith("http://") or url.startswith("https://")) or
					not file_name.startswith("/") or ".." in file_name):
				raise ValueError("file_url文件项无效")
			normalized_files.append({"url": url, "file_name": file_name})
		try:
			total_size = int(response.get("Total_File_Size"))
		except Exception:
			raise ValueError("Total_File_Size无效")
		if total_size <= 0:
			raise ValueError("Total_File_Size无效")
		return {"task_id": task_id, "request_id": int(self._request_id),
			"target_version": target_version, "files": normalized_files,
			"total_size": total_size,
			"hardware_versions": response.get("Adapt_Hardware_Ver"),
			"firmware_versions": response.get("Adapt_Firmware_Ver")}

	def _handle_query_response(self, client, response):
		if int(response.get("has_update") or 0) == 0:
			print("[OTA][查询] 平台返回当前已是最新版本")
			self.state.publish_ota_event("hide")
			self.state.update_upgrade_status("当前已是最新版本", "success")
			self._set_phase("no_update", query_active=False)
			return
		try:
			task = self._validate_task(response)
		except Exception as error:
			print("[OTA][查询] 升级任务参数无效：{}".format(error))
			self._show_error("升级信息无效")
			return
		self._task = task
		snapshot = self.state.get_snapshot()[1]
		print("[OTA][适配] 当前软件={} 硬件={} 固件={} 目标软件={} 适配硬件={} 适配固件={}".format(
			snapshot["software_version"], snapshot["hardware_version"],
			snapshot["firmware_version"], task["target_version"],
			task["hardware_versions"], task["firmware_versions"]))
		completed = self.persistent.completed_ota_status(task["task_id"])
		pending = self.persistent.get("ota_pending")
		if completed is not None or (isinstance(pending, dict) and
				str(pending.get("task_id")) == task["task_id"]):
			status = completed or pending.get("stage") or "device_busy"
			print("[OTA][幂等] task_id={}已存在，状态={}，不重复执行".format(
				task["task_id"], status))
			self._report_status(client, status, task)
			self._show_error("升级任务已处理")
			return
		if not _is_newer_version(task["target_version"], snapshot["software_version"]):
			self.state.publish_ota_event("hide")
			self.state.update_upgrade_status("当前已是最新版本", "success")
			self._report_status(client, "no_update", task)
			return
		compatible = (_version_matches(snapshot["hardware_version"],
				task["hardware_versions"]) and
			_version_matches(snapshot["firmware_version"],
				task["firmware_versions"]))
		if not compatible:
			print("[OTA][适配] 硬件或固件版本不匹配，禁止下载")
			self._report_status(client, "incompatible", task)
			self._show_error("硬件/固件版本不匹配，无法下载")
			return
		self._report_status(client, "update_available", task)
		self._set_phase("waiting_confirm", query_active=True)
		self.state.publish_ota_event(
			"confirm", "新软件版本：{}\n是否升级？".format(task["target_version"]))
		print("[OTA][查询] 新版本信息有效，等待用户确认：task_id={}".format(
			task["task_id"]))
		while True:
			self._lock.acquire()
			try:
				decision = self._decision
			finally:
				self._lock.release()
			if decision is not None:
				break
			utime.sleep_ms(50)
		if decision == "reject":
			print("[OTA] 用户取消升级：task_id={}".format(task["task_id"]))
			self._report_status(client, "rejected", task)
			self._set_phase("rejected", query_active=False)
			return
		if decision != "accept":
			print("[OTA] 查询页面已离开，不执行升级")
			return
		self._execute_upgrade(client, task)

	def _report_status(self, client, status, task):
		payload = {"schema_version": "1.1",
			"deviceId": self.config["device_id"],
			"Information_sta": "upgrade_status",
			"task_id": task["task_id"],
			"request_id": int(task.get("request_id") or self._request_id or 0),
			"Updata_Sta": str(status),
			"Upgrad_Software_Ver": task["target_version"]}
		ok = self._publish(client, payload, "升级状态{}".format(status))
		if not ok:
			print("[OTA][状态] 状态上报失败，不中断本地升级：{}".format(status))
		return ok

	def _execute_upgrade(self, client, task):
		self._set_phase("preparing", query_active=False, busy=True)
		self.state.publish_ota_event("progress", "开始升级")
		self._report_status(client, "preparing", task)
		ok, saved_group_id, error = self.communication.prepare_for_ota()
		self._saved_group_id = saved_group_id
		if not ok:
			print("[OTA] 对讲退出失败：{}".format(error))
			self._report_status(client, "device_busy", task)
			self._show_error("设备忙，无法升级")
			self.communication.restore_after_ota_failure(saved_group_id)
			return
		self._set_phase("downloading", busy=True)
		self.state.publish_ota_event("progress", "下载中.")
		self._report_status(client, "downloading", task)
		download_finished = False
		try:
			fota = self._new_fota()
			remaining = list(task["files"])
			for attempt in range(1, OTA_DOWNLOAD_MAX_ATTEMPTS + 1):
				print("[OTA][下载] 第{}/{}次调用bulk_download".format(
					attempt, OTA_DOWNLOAD_MAX_ATTEMPTS))
				print("[OTA][下载] 本次下载文件={}".format(remaining))
				try:
					result = fota.bulk_download(info=remaining)
				except Exception as error:
					print("[OTA][下载] bulk_download抛出异常：{}".format(error))
					result = list(remaining)
				print("[OTA][下载] bulk_download原始返回值={}".format(result))
				if result is None or result == [] or result == ():
					remaining = []
					break
				remaining = self._failed_files(result, task["files"])
				if attempt < OTA_DOWNLOAD_MAX_ATTEMPTS:
					self._set_phase("download_retry", busy=True)
					self.state.publish_ota_event(
						"progress", "下载失败，重试下载")
					self._sleep_cancelable(OTA_DOWNLOAD_RETRY_INTERVAL_MS, False)
					self._set_phase("downloading", busy=True)
					self.state.publish_ota_event("progress", "下载中.")
			if remaining:
				raise RuntimeError("三次下载后仍有失败文件：{}".format(remaining))
			download_finished = True
			self._report_status(client, "download_complete", task)
			self._set_phase("download_complete", busy=True)
			self.state.publish_ota_event(
				"success", "下载完成", OTA_DOWNLOAD_COMPLETE_DURATION_MS)
			utime.sleep_ms(OTA_DOWNLOAD_COMPLETE_DURATION_MS)
			self._set_phase("rebooting", busy=True)
			self.state.publish_ota_event(
				"progress", "准备升级重启", OTA_REBOOT_READY_DURATION_MS)
			utime.sleep_ms(OTA_REBOOT_READY_DURATION_MS)
			metadata = {"task_id": task["task_id"],
				"request_id": task["request_id"],
				"target_version": task["target_version"],
				"stage": "rebooting", "start_time": _current_time_value(),
				"pending_report": True}
			if not self.persistent.save_ota_pending(metadata):
				raise RuntimeError("OTA重启确认信息保存失败")
			self._report_status(client, "rebooting", task)
			print("[OTA][升级] 调用set_update_flag(True)，启用重命名模式")
			# QuecPython部分固件的C扩展接口不接受关键字参数。
			flag_result = fota.set_update_flag(True)
			print("[OTA][升级] set_update_flag返回值={}".format(flag_result))
			self._close_client(client)
			self.communication.stop()
			print("[OTA][升级] 通信资源已关闭，执行设备重启")
			self._restart()
		except Exception as error:
			print("[OTA][升级] 最终失败：{}".format(error))
			reported = self._report_status(client, "upgrade_fail", task)
			if reported:
				self.persistent.clear_ota_pending()
			else:
				self._save_pending_report(task, "upgrade_fail")
			self.persistent.complete_ota_task(task["task_id"], "upgrade_fail")
			self._show_error("升级准备失败，结束任务" if download_finished
				else "最终下载失败，结束任务")
			utime.sleep_ms(OTA_ERROR_POPUP_DURATION_MS)
			self.communication.restore_after_ota_failure(self._saved_group_id)
			self._set_phase("upgrade_fail", busy=False)

	def _new_fota(self):
		if self.fota_factory is not None:
			return self.fota_factory()
		if app_fota is None:
			raise RuntimeError("app_fota模块不可用")
		return app_fota.new()

	def _save_pending_report(self, task, status):
		metadata = {"task_id": task["task_id"],
			"request_id": task.get("request_id", 0),
			"target_version": task["target_version"],
			"stage": str(status), "start_time": _current_time_value(),
			"pending_report": True}
		if not self.persistent.save_ota_pending(metadata):
			print("[OTA][状态] 待补报状态保存失败：{}".format(status))

	@staticmethod
	def _failed_files(result, original):
		items = list(result) if isinstance(result, (list, tuple)) else [result]
		failed = []
		for item in items:
			if isinstance(item, dict):
				url = str(item.get("url") or "")
				file_name = str(item.get("file_name") or "")
				matches = [value for value in original
					if (url and value["url"] == url) or
					(file_name and value["file_name"] == file_name)]
			else:
				text = str(item)
				matches = [value for value in original
					if value["url"] == text or value["file_name"] == text]
			if not matches:
				print("[OTA][下载] 无法识别失败文件返回项={}，安全终止任务".format(item))
				raise RuntimeError("bulk_download返回了未知失败文件")
			for value in matches:
				if value not in failed:
					failed.append(value)
		return failed

	def _restart(self):
		if self.restart_callback is not None:
			return self.restart_callback()
		if Power is None or not hasattr(Power, "powerRestart"):
			raise RuntimeError("设备重启接口不可用")
		return Power.powerRestart()

	def _post_boot_worker(self, pending):
		client = None
		try:
			deadline = utime.ticks_add(
				utime.ticks_ms(), OTA_POST_BOOT_NETWORK_WAIT_MS)
			while not self.state.get_snapshot()[1].get("network_connected"):
				if utime.ticks_diff(utime.ticks_ms(), deadline) >= 0:
					print("[OTA][启动确认] 等待网络超时，保留待上报信息")
					return
				utime.sleep_ms(1000)
			client = self._connect_with_retries()
			if client is None:
				print("[OTA][启动确认] MQTT连接失败，保留待上报信息")
				return
			task = {"task_id": pending["task_id"],
				"request_id": pending.get("request_id", 0),
				"target_version": pending["target_version"]}
			pending_stage = str(pending.get("stage") or "rebooting")
			if pending_stage in ("upgrade_ok", "upgrade_fail"):
				status = pending_stage
				current = self.state.get_snapshot()[1]["software_version"]
			else:
				self._report_status(client, "upgrading", task)
				current = self.state.get_snapshot()[1]["software_version"]
				status = ("upgrade_ok" if str(current) == str(task["target_version"])
					else "upgrade_fail")
			print("[OTA][启动确认] 当前软件={} 目标软件={} 结论={}".format(
				current, task["target_version"], status))
			if not self._report_status(client, status, task):
				print("[OTA][启动确认] 最终状态上报失败，保留待上报信息")
				return
			self.persistent.complete_ota_task(task["task_id"], status)
			self.persistent.clear_ota_pending()
			self.state.publish_ota_event(
				"success" if status == "upgrade_ok" else "error",
				"升级成功" if status == "upgrade_ok" else "升级失败",
				OTA_ERROR_POPUP_DURATION_MS)
		except Exception as error:
			print("[OTA][启动确认] 处理异常，保留待上报信息：{}".format(error))
		finally:
			self._close_client(client)
			self._finish_thread()

	def _sleep_cancelable(self, duration_ms, cancelable=True):
		deadline = utime.ticks_add(utime.ticks_ms(), int(duration_ms))
		while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
			if cancelable and self._cancelled():
				return False
			utime.sleep_ms(50)
		return True

	@staticmethod
	def _close_client(client):
		if client is None:
			return
		for name in ("disconnect", "close"):
			callback = getattr(client, name, None)
			if callback is None:
				continue
			try:
				callback()
			except Exception:
				pass
			return
