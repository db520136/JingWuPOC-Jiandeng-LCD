# -*- coding: utf-8 -*-
import _thread
import utime
try:
	import usocket as socket
except ImportError:
	import socket
try:
	import checkNet
except ImportError:
	checkNet = None

from usr.tcp_transport import TCPTransport

RECONNECT_INTERVAL_MS = 3000
GNSS_QUEUE_WAIT_MS = 100


class ProductCommunication:
	"""只保留 GNSS 上报、60 秒心跳和 TCP 重连的通信服务。"""
	def __init__(self, config, state_store, project_name="JingWu",
				 project_version="1.0.0"):
		self.config = dict(config)
		self.state = state_store
		self.project_name = str(project_name)
		self.project_version = str(project_version)
		self.running = False
		self.thread_id = None
		self.transport = None
		self.socket = None
		self.request_count = 1
		self._lock = _thread.allocate_lock()
		self._gnss_result = None

	def _network_ready(self):
		if checkNet is None:
			return True
		try:
			checkNet.CheckNetwork(
				self.project_name, self.project_version).wait_network_connected()
			return True
		except Exception as error:
			print("[通信] 网络等待失败：{}".format(error))
			return False

	def _close_socket(self):
		if self.transport is not None:
			self.transport.stop()
		self.transport = None
		self.socket = None
		self.state.update_transport(None, False)

	def _connect(self):
		if not self._network_ready():
			return False
		self.state.update_transport(True, False)
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			sock.connect((self.config["tcp_host"],
				int(self.config["tcp_port"])))
			try:
				sock.settimeout(None)
			except Exception:
				pass
			transport = TCPTransport(
				sock, self.config["device_id"], self._on_transport_event)
			transport.start()
			self.socket = sock
			self.transport = transport
			self.state.update_transport(True, True)
			print("[通信] TCP连接成功：{}:{}".format(
				self.config["tcp_host"], self.config["tcp_port"]))
			return True
		except Exception as error:
			try:
				sock.close()
			except Exception:
				pass
			self.state.update_transport(True, False)
			print("[通信] TCP连接失败：{}".format(error))
			return False

	def _on_transport_event(self, event):
		if event.get("type") in ("tcp_disconnected", "tcp_receive_error"):
			self.state.update_transport(None, False)

	def _take_gnss(self):
		self._lock.acquire()
		try:
			result = self._gnss_result
			self._gnss_result = None
			return result
		finally:
			self._lock.release()

	def request_gnss_result(self, result):
		if not isinstance(result, dict):
			return False
		self._lock.acquire()
		try:
			self._gnss_result = dict(result)
		finally:
			self._lock.release()
		return True

	def set_gnss_sample_request(self, callback):
		return True

	def set_screen_wake_request(self, callback):
		return True

	def request_hardware_key(self, action):
		print("[按键] PTT事件：{}".format(action))
		return True

	def _send_gnss_if_pending(self):
		result = self._take_gnss()
		if result is None or self.transport is None:
			return True
		raw_gga = result.get("raw_gga") or b""
		if isinstance(raw_gga, str):
			raw_gga = raw_gga.encode("ascii")
		try:
			self.transport.upload_gnss(self.request_count, raw_gga)
			print("[定位] TCP定位上报成功：request_cnt={}".format(
				self.request_count))
			self.request_count = (self.request_count + 1) & 0xFFFF
			return True
		except Exception as error:
			print("[定位] TCP定位上报失败：{}".format(error))
			return False

	def _run(self):
		try:
			while self.running:
				if self.transport is None or not self.transport.running:
					self._close_socket()
					if not self._connect():
						utime.sleep_ms(RECONNECT_INTERVAL_MS)
						continue
				if not self._send_gnss_if_pending():
					self._close_socket()
					continue
				try:
					if self.transport.send_heartbeat_if_due(self.request_count):
						print("[通信] TCP心跳发送：request_cnt={}".format(
							self.request_count))
						self.request_count = (self.request_count + 1) & 0xFFFF
				except Exception as error:
					print("[通信] TCP心跳失败：{}".format(error))
					self._close_socket()
				utime.sleep_ms(GNSS_QUEUE_WAIT_MS)
		finally:
			self._close_socket()
			self.running = False
			self.thread_id = None

	def start(self):
		if self.running:
			return False
		self.running = True
		try:
			self.thread_id = _thread.start_new_thread(self._run, ())
			return True
		except Exception:
			self.running = False
			self.thread_id = None
			raise

	def stop(self):
		self.running = False
		self._close_socket()

	def prepare_for_ota(self, timeout_ms=30000):
		self.stop()
		return True, None, None

	def restore_after_ota_failure(self, group_id=None, timeout_ms=30000):
		if not self.running:
			return self.start()
		return True
