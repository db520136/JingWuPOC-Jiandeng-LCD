# -*- coding: utf-8 -*-
from usr.communication_queue import CommunicationCommandQueue
from usr.communication_send_worker import CommunicationSendWorker
from usr.rtp_receive_worker import RTPReceiveWorker
from usr.tcp_receive_worker import TCPReceiveWorker
class CommunicationRuntime:
	def __init__(self, tcp_socket, udp_socket, server_ip,
				 udp_server_port=None, read_audio_frame=None,
				 write_audio_frame=None, event_callback=None,
				 command_queue=None):
		if tcp_socket is None:
			raise ValueError("tcp_socket不能为空")
		if udp_socket is None:
			raise ValueError("udp_socket不能为空")
		if read_audio_frame is None:
			raise ValueError("read_audio_frame不能为空")
		self.tcp_socket = tcp_socket
		self.udp_socket = udp_socket
		self.server_ip = str(server_ip)
		self.udp_server_port = (None if udp_server_port is None else
								int(udp_server_port))
		self.read_audio_frame = read_audio_frame
		self.write_audio_frame = write_audio_frame
		self.event_callback = event_callback
		self.commands = (command_queue if command_queue is not None else
						 CommunicationCommandQueue())
		self.tcp_receive = None
		self.rtp_receive = None
		self.send = None
		self.session = None
		self.running = False
	def attach_session(self, session):
		if self.running:
			raise RuntimeError("运行时运行中不能更换通信会话")
		if session is None:
			raise ValueError("session不能为空")
		self.session = session
		return True
	def _on_tcp_event(self, event):
		self._emit("tcp", event)
	def _on_rtp_event(self, event):
		self._emit("rtp", event)
	def _on_send_event(self, event):
		self._emit("send", event)
	def _emit(self, source, event):
		wrapped = {"source": source, "event": event}
		if self.session is not None:
			self.session.on_runtime_event(wrapped)
		if self.event_callback is not None:
			self.event_callback(wrapped)
	def start(self, session_threaded=True, session_wake_callback=None):
		if self.running:
			return False
		self.tcp_receive = TCPReceiveWorker(
			self.tcp_socket, self._on_tcp_event)
		self.rtp_receive = RTPReceiveWorker(
			self.udp_socket, self._on_rtp_event,
			write_audio_frame=self.write_audio_frame)
		self.send = CommunicationSendWorker(
			tcp_socket=self.tcp_socket,
			udp_socket=self.udp_socket,
			read_audio_frame=self.read_audio_frame,
			event_callback=self._on_send_event,
			command_queue=self.commands,
			server_ip=self.server_ip,
			udp_server_port=self.udp_server_port)
		started_tcp = False
		started_rtp = False
		started_send = False
		try:
			started_tcp = self.tcp_receive.start()
			started_rtp = self.rtp_receive.start()
			started_send = self.send.start()
			if not started_send:
				raise RuntimeError("统一发送线程未启动")
			self.running = True
			if self.session is not None:
				self.session.start(session_threaded, session_wake_callback)
			return True
		except Exception:
			if started_tcp:
				self.tcp_receive.stop()
			if started_rtp:
				self.rtp_receive.stop()
			if started_send or self.send is not None:
				self.send.stop()
			if self.session is not None:
				self.session.stop()
			self._close_sockets()
			self.tcp_receive = None
			self.rtp_receive = None
			self.send = None
			raise
	def submit(self, command_type, payload=None, priority=None):
		return self.commands.submit(command_type, payload, priority)
	def cancel_rtp_activation(self, retry_key=None, server_port=None):
		from usr.comm_commands import CMD_RTP_ACTIVATE_CANCEL
		payload = {"retry_key": retry_key}
		if server_port is not None:
			payload["server_port"] = server_port
		return self.submit(CMD_RTP_ACTIVATE_CANCEL, payload)
	def set_rtp_receive_context(self, group_id, call_type, server_port):
		if self.rtp_receive is None:
			return False
		self.rtp_receive.set_receive_context(
			group_id, call_type, server_port)
		return True
	def clear_rtp_receive_context(self):
		if self.rtp_receive is None:
			return False
		self.rtp_receive.clear_receive_context()
		return True
	def begin_logout(self):
		if self.rtp_receive is not None:
			self.rtp_receive.clear_receive_context()
		if self.send is not None:
			self.send.begin_logout()
		if self.session is not None:
			self.session.begin_logout()
		return True
	def stop(self):
		self.running = False
		if self.session is not None:
			self.session.stop()
		if self.send is not None:
			self.send.stop()
		if self.tcp_receive is not None:
			self.tcp_receive.stop()
		if self.rtp_receive is not None:
			self.rtp_receive.stop()
		self._close_sockets()
	def _close_sockets(self):
		try:
			self.tcp_socket.close()
		except Exception:
			pass
		try:
			self.udp_socket.close()
		except Exception:
			pass
	def reconnect(self, tcp_socket, udp_socket, udp_server_port=None):
		if tcp_socket is None or udp_socket is None:
			raise ValueError("重连Socket不能为None")
		# 调用方可能已经先 stop() 释放旧 Socket（例如需要先重新 bind
		# UDP 本地端口）；只有仍在运行时才在这里执行停止和关闭。
		if self.running:
			self.stop()
		self.tcp_socket = tcp_socket
		self.udp_socket = udp_socket
		self.udp_server_port = (None if udp_server_port is None else
								int(udp_server_port))
		# stop() 会在旧队列中记录终止事件；重连必须使用全新的队列，
		# 否则新发送线程启动后会立即消费旧停止标志并退出。
		self.commands = CommunicationCommandQueue(
			max_length=self.commands.max_length)
		return self.start()
