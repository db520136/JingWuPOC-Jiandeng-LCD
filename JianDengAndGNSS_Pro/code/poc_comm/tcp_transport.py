# -*- coding: utf-8 -*-
"""精简产品需要的 TCP 传输层。

该模块只处理 POC 帧传输，不包含登录、组呼、单呼或 RTP 业务状态机。
"""
import _thread
import utime

from usr.comm_commands import (
	TCP_INITIAL_WAIT_MS, TCP_MAX_RETRIES, TCP_MAX_WAIT_MS,
	TCP_RETRY_STEP_MS)
from usr.poc_protocol import (
	MSG_GNSS_UPLOAD, MSG_GNSS_UPLOAD_ACK, MSG_HEARTBEAT,
	MSG_HEARTBEAT_ACK, FrameParser, build_frame,
	build_gnss_upload_payload, build_heartbeat_payload)

TCP_FRAME = "tcp_frame"
TCP_DISCONNECTED = "tcp_disconnected"
TCP_RECEIVE_ERROR = "tcp_receive_error"
HEARTBEAT_INTERVAL_MS = 60000


class TCPTransactionError(RuntimeError):
	"""发送成功但在规定重试次数内没有收到 ACK。"""


def _ticks_add(base, delta):
	return utime.ticks_add(base, delta)


def _ticks_diff(first, second):
	return utime.ticks_diff(first, second)


def retry_transaction(send_callback, wait_callback, connected_callback,
					  label="TCP事务", on_retry=None,
					  should_retry_error=None):
	"""执行统一的 TCP ACK 重试策略。

	`send_callback`负责投递当前帧，`wait_callback`接收本次等待毫秒数并
	返回 ACK 帧或 None。这样旧控制器和精简产品传输器使用同一套参数。
	"""
	last_error = None
	for attempt in range(TCP_MAX_RETRIES + 1):
		if not connected_callback():
			raise TCPTransactionError("TCP连接不可用")
		wait_ms = min(
			TCP_INITIAL_WAIT_MS + attempt * TCP_RETRY_STEP_MS,
			TCP_MAX_WAIT_MS)
		try:
			send_callback(attempt)
			frame = wait_callback(wait_ms)
			if frame is not None:
				return frame
			last_error = "{}等待{}ms无应答".format(label, wait_ms)
		except TCPTransactionError:
			raise
		except Exception as error:
			if (should_retry_error is not None and
					not should_retry_error(error)):
				raise
			last_error = error
		if attempt < TCP_MAX_RETRIES and on_retry is not None:
			on_retry(attempt + 1, wait_ms, last_error)
	raise TCPTransactionError(str(last_error))


class TCPTransport:
	"""带 ACK 等待和重发的最小 TCP 传输器。

	调用方负责建立 socket 和分配 request_cnt；本类不依赖 UI 或音频模块。
	"""
	def __init__(self, socket_obj, device_id=None, event_callback=None,
				receive_size=1024, parser=None):
		if socket_obj is None:
			raise ValueError("socket_obj不能为空")
		self.socket = socket_obj
		self.device_id = None if device_id is None else str(device_id)
		self.event_callback = event_callback
		self.receive_size = max(1, int(receive_size))
		self.parser = parser if parser is not None else FrameParser()
		self.running = False
		self.thread_id = None
		self._frames = []
		self._lock = _thread.allocate_lock()
		self._heartbeat_due_ms = _ticks_add(
			utime.ticks_ms(), HEARTBEAT_INTERVAL_MS)

	def _emit(self, event_type, payload=None):
		if self.event_callback is not None:
			self.event_callback({"type": event_type, "payload": payload})

	def start(self):
		if self.running:
			return False
		self.running = True
		try:
			self.thread_id = _thread.start_new_thread(self._receive_loop, ())
			return True
		except Exception:
			self.running = False
			self.thread_id = None
			raise

	def stop(self):
		self.running = False
		try:
			self.socket.close()
		except Exception:
			pass

	def _receive_loop(self):
		try:
			while self.running:
				try:
					data = self.socket.recv(self.receive_size)
				except Exception as error:
					if self.running:
						self._emit(TCP_RECEIVE_ERROR, error)
					break
				if not data:
					if self.running:
						self._emit(TCP_DISCONNECTED)
					break
				for frame in self.parser.feed(data):
					self._lock.acquire()
					try:
						if len(self._frames) >= 16:
							self._frames.pop(0)
						self._frames.append(frame)
					finally:
						self._lock.release()
					self._emit(TCP_FRAME, frame)
		finally:
			self.running = False
			self.thread_id = None

	def _send_all(self, packet):
		sent = 0
		data = bytes(packet)
		while sent < len(data):
			count = self.socket.send(data[sent:])
			if count is None:
				return
			count = int(count)
			if count <= 0:
				raise OSError("TCP发送返回0字节")
			sent += count

	def _take_ack(self, ack_type, request_count, timeout_ms):
		deadline = _ticks_add(utime.ticks_ms(), int(timeout_ms))
		expected_count = (int(request_count) + 1) & 0xFFFF
		while self.running and _ticks_diff(deadline, utime.ticks_ms()) > 0:
			self._lock.acquire()
			try:
				for index, frame in enumerate(self._frames):
					if (frame.get("msg_type") == int(ack_type) and
							frame.get("request_cnt") == expected_count):
						return self._frames.pop(index)
			finally:
				self._lock.release()
			utime.sleep_ms(5)
		return None

	def transact(self, packet, ack_type, request_count, label="TCP事务"):
		"""发送一帧并等待 ACK；首次发送后最多重发 3 次。"""
		def _send(_attempt):
			self._send_all(packet)

		def _wait(timeout_ms):
			return self._take_ack(ack_type, request_count, timeout_ms)

		def _retry(attempt, wait_ms, error):
			self._emit("tcp_retry", {
				"label": label, "attempt": attempt,
				"max_retries": TCP_MAX_RETRIES, "error": error})
		try:
			return retry_transaction(
				_send, _wait, lambda: self.running, label, _retry)
		except TCPTransactionError as error:
			self._emit("tcp_retry_exhausted", {
				"label": label, "max_retries": TCP_MAX_RETRIES,
				"error": error})
			raise

	def send_heartbeat(self, request_count):
		if self.device_id is None:
			raise ValueError("device_id不能为空")
		packet = build_frame(
			MSG_HEARTBEAT, request_count,
			build_heartbeat_payload(self.device_id))
		return self.transact(
			packet, MSG_HEARTBEAT_ACK, request_count, "0x02心跳")

	def send_heartbeat_if_due(self, request_count, now_ms=None):
		"""每 60 秒最多触发一次心跳；调用方仍负责递增 request_cnt。"""
		if now_ms is None:
			now_ms = utime.ticks_ms()
		if _ticks_diff(now_ms, self._heartbeat_due_ms) < 0:
			return False
		self._heartbeat_due_ms = _ticks_add(
			now_ms, HEARTBEAT_INTERVAL_MS)
		self.send_heartbeat(request_count)
		return True

	def upload_gnss(self, request_count, gnss_data=b""):
		if self.device_id is None:
			raise ValueError("device_id不能为空")
		packet = build_frame(
			MSG_GNSS_UPLOAD, request_count,
			build_gnss_upload_payload(self.device_id, gnss_data))
		return self.transact(
			packet, MSG_GNSS_UPLOAD_ACK, request_count, "0x0D定位上报")
