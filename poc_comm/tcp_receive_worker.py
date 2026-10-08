# -*- coding: utf-8 -*-
"""永久阻塞式 TCP 接收线程。

该文件是第三阶段的新隔离实现，不由当前 main.py 自动启动。socket、帧解析器
和事件回调全部显式注入；本线程只 recv/解析/投递，不执行 send、不修改 UI，
也不调用任何模糊属性探测接口。
"""

import _thread

try:
	from usr.poc_protocol import FrameParser
except ImportError:
	from poc_protocol import FrameParser


TCP_RX_FRAME = "tcp_frame"
TCP_RX_DISCONNECTED = "tcp_disconnected"
TCP_RX_ERROR = "tcp_receive_error"


class TCPReceiveWorker:
	"""一个 socket 对应一个接收线程；连接替换时由上层创建新实例。"""

	def __init__(self, socket_obj, event_callback, parser=None,
				 receive_size=1024):
		if socket_obj is None:
			raise ValueError("socket_obj不能为空")
		if event_callback is None:
			raise ValueError("event_callback不能为空")
		self.socket = socket_obj
		self.event_callback = event_callback
		self.parser = parser if parser is not None else FrameParser()
		self.receive_size = max(1, int(receive_size))
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
		"""停止标志加关闭 socket；EC800M 可用关闭动作唤醒 recv。"""
		self.running = False
		try:
			self.socket.close()
		except Exception:
			pass

	def _emit(self, event_type, payload=None):
		self.event_callback({"type": event_type, "payload": payload})

	def _run(self):
		try:
			while self.running:
				try:
					data = self.socket.recv(self.receive_size)
				except Exception as error:
					if self.running:
						self._emit(TCP_RX_ERROR, error)
					break
				if not data:
					if self.running:
						self._emit(TCP_RX_DISCONNECTED, None)
					break
				frames = self.parser.feed(data)
				for frame in frames:
					self._emit(TCP_RX_FRAME, frame)
		finally:
			self.running = False
			self.thread_id = None
