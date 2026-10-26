# -*- coding: utf-8 -*-
import _thread
import utime
from usr.comm_commands import (CMD_ACCEPT_SINGLE_CALL,
							   CMD_DISSOLVE_SINGLE_CALL, CMD_FORCE_LOGIN,
							   CMD_FORCE_RELEASE_FLOOR, CMD_GNSS_UPLOAD,
							   CMD_JOIN_GROUP, CMD_LEAVE_GROUP,
							   CMD_LOGIN, CMD_LOGOUT, CMD_PTT_DOWN,
							   CMD_PTT_UP, CMD_RELEASE_FLOOR,
							   CMD_REQUEST_FLOOR, CMD_REJECT_SINGLE_CALL,
							   CMD_RTP_ACTIVATE, CMD_RTP_ACTIVATE_CANCEL,
							   CMD_RTP_AUDIO_START,
							   CMD_RTP_AUDIO_STOP, CMD_SINGLE_CALL,
							   CMD_STATUS_SYNC, CMD_STOP_ALL,
							   CMD_TCP_HEARTBEAT,
							   CMD_TCP_PRIORITY_RESPONSE)
from usr.communication_queue import CommunicationCommandQueue
from usr.rtp_protocol import RTP_PAYLOAD_SIZE, build_rtp_packet
RTP_ACTIVATE_PAYLOAD = b"\x00"
TCP_COMMANDS = (
	CMD_LOGIN, CMD_FORCE_LOGIN, CMD_LOGOUT, CMD_JOIN_GROUP,
	CMD_LEAVE_GROUP, CMD_SINGLE_CALL, CMD_ACCEPT_SINGLE_CALL,
	CMD_REJECT_SINGLE_CALL, CMD_DISSOLVE_SINGLE_CALL, CMD_REQUEST_FLOOR,
	CMD_RELEASE_FLOOR, CMD_FORCE_RELEASE_FLOOR, CMD_TCP_HEARTBEAT,
	CMD_GNSS_UPLOAD, CMD_STATUS_SYNC, CMD_TCP_PRIORITY_RESPONSE,
)
class CommunicationSendWorker:
	def __init__(self, tcp_socket=None, udp_socket=None,
				 read_audio_frame=None, event_callback=None,
				 command_queue=None, server_ip="", udp_server_port=None,
				 ssrc=0x13572468, initial_sequence=0,
				 initial_timestamp=0):
		if tcp_socket is None and udp_socket is None:
			raise ValueError("tcp_socket和udp_socket不能同时为空")
		if udp_socket is not None and read_audio_frame is None:
			raise ValueError("使用UDP发送时必须提供read_audio_frame")
		self.tcp_socket = tcp_socket
		self.udp_socket = udp_socket
		self.read_audio_frame = read_audio_frame
		self.event_callback = event_callback
		self.commands = (command_queue if command_queue is not None else
						 CommunicationCommandQueue())
		self.server_ip = str(server_ip)
		self.udp_server_port = (None if udp_server_port is None else
								int(udp_server_port))
		self.ssrc = int(ssrc) & 0xFFFFFFFF
		self.sequence = int(initial_sequence) & 0xFFFF
		self.timestamp = int(initial_timestamp) & 0xFFFFFFFF
		self.running = False
		self.audio_active = False
		self.first_audio_marker = 0
		self.thread_id = None
		self.audio_frames_sent = 0
		self.audio_failures = 0
		self.tcp_failed = False
		self._tcp_attempts = {}
		self._activate_retries = {}
		self.logout_pending = False
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
		self.commands.signal_stop()
	def begin_logout(self):
		self.logout_pending = True
		self.audio_active = False
		self.first_audio_marker = 0
		self._activate_retries = {}
		return self.commands.begin_logout()
	def _emit(self, event_type, payload=None):
		if self.event_callback is not None:
			self.event_callback({"type": event_type, "payload": payload})
	@staticmethod
	def _packet_from_payload(payload):
		if not isinstance(payload, (bytes, bytearray)):
			return None
		if len(payload) <= 0:
			return None
		return bytes(payload)
	def _send_tcp_packet(self, command_type, packet, attempt):
		if self.logout_pending and command_type != CMD_LOGOUT:
			return False
		if self.tcp_socket is None:
			self.tcp_failed = True
			self._emit("tcp_send_fatal", {
				"command": command_type,
				"msg_type": packet[4] if len(packet) >= 9 else None,
				"request_cnt": ((packet[7] << 8) | packet[8]) if len(packet) >= 9 else None,
				"attempt": int(attempt),
				"error": "tcp_socket_missing",
			})
			return False
		if self.tcp_failed:
			return False
		sent = 0
		data = bytes(packet)
		try:
			while sent < len(data):
				count = self.tcp_socket.send(data[sent:])
				if count is None:
					return True
				count = int(count)
				if count <= 0:
					raise OSError("TCP发送返回0字节")
				sent += count
			return True
		except Exception as error:
			self.tcp_failed = True
			self._stop_audio()
			self._emit("tcp_send_fatal", {
				"command": command_type,
				"msg_type": packet[4] if len(packet) >= 9 else None,
				"request_cnt": ((packet[7] << 8) | packet[8]) if len(packet) >= 9 else None,
				"attempt": int(attempt),
				"error": error,
			})
			return False
	def _udp_port_from_payload(self, payload):
		if isinstance(payload, dict):
			if "server_port" not in payload:
				return None
			payload = payload["server_port"]
		elif isinstance(payload, (tuple, list)):
			if not payload:
				return None
			payload = payload[0]
		try:
			port = int(payload)
		except Exception:
			return None
		if port <= 0 or port > 65535:
			return None
		return port
	def _send_udp_rtp(self, payload, marker=0):
		if self.logout_pending:
			return False
		if self.udp_socket is None or self.udp_server_port is None:
			self._emit("rtp_send_rejected", "udp_target_missing")
			return False
		packet = build_rtp_packet(
			self.sequence, self.timestamp, self.ssrc, payload, marker)
		try:
			self.udp_socket.sendto(
				packet, (self.server_ip, self.udp_server_port))
		except Exception as error:
			if len(payload) != RTP_PAYLOAD_SIZE or self.audio_failures == 0:
				self._emit("rtp_send_error", error)
			if len(payload) == RTP_PAYLOAD_SIZE:
				self.audio_failures += 1
			return False
		self.sequence = (self.sequence + 1) & 0xFFFF
		self.timestamp = (self.timestamp + len(payload)) & 0xFFFFFFFF
		return True
	def _send_activate(self, payload):
		port = self._udp_port_from_payload(payload)
		if port is None:
			self._emit("rtp_activate_rejected", payload)
			return False
		previous_port = self.udp_server_port
		self.udp_server_port = port
		try:
			sent = self._send_udp_rtp(RTP_ACTIVATE_PAYLOAD)
			self._emit("rtp_activate_result", {
				"server_port": port, "sent": sent,
				"probe": isinstance(payload, dict) and bool(payload.get("probe"))})
			return sent
		finally:
			# 远端占麦打洞不能改写本机正在发送音频的目标端口。
			if isinstance(payload, dict) and payload.get("probe"):
				self.udp_server_port = previous_port
	@staticmethod
	def _activate_retry_key(payload, port=None):
		if isinstance(payload, dict) and payload.get("retry_key") is not None:
			key = payload.get("retry_key")
			return tuple(key) if isinstance(key, list) else key
		return ("udp", int(port)) if port is not None else None
	def _cancel_activate_retry(self, payload):
		port = self._udp_port_from_payload(payload)
		key = self._activate_retry_key(payload, port)
		if key in self._activate_retries:
			del self._activate_retries[key]
			self._emit("rtp_activate_retry_cancelled", {"retry_key": key})
	def _start_activate_retry(self, payload):
		if not isinstance(payload, dict) or not payload.get("probe"):
			return
		port = self._udp_port_from_payload(payload)
		key = self._activate_retry_key(payload, port)
		if key is None:
			return
		self._activate_retries[key] = {
			"payload": dict(payload), "port": port, "retry_count": 0,
			"deadline": utime.ticks_add(utime.ticks_ms(), 500)}
	def _process_activate_retries(self):
		now = utime.ticks_ms()
		for key, state in list(self._activate_retries.items()):
			if utime.ticks_diff(now, state["deadline"]) < 0:
				continue
			if state["retry_count"] < 2:
				state["retry_count"] += 1
				self._send_activate(state["payload"])
				state["deadline"] = utime.ticks_add(now, 500)
				self._emit("rtp_activate_retry", {
					"retry_key": key, "retry_count": state["retry_count"],
					"server_port": state["port"]})
			else:
				del self._activate_retries[key]
				self._emit("rtp_activate_timeout", {
					"retry_key": key, "server_port": state["port"]})
	def _start_audio(self, payload):
		if payload is not None:
			port = self._udp_port_from_payload(payload)
			if port is None:
				self._emit("rtp_audio_start_rejected", payload)
				return False
			self.udp_server_port = port
		if self.udp_server_port is None:
			self._emit("rtp_audio_start_rejected", "udp_target_missing")
			return False
		self.audio_active = True
		self.first_audio_marker = 1
		self.audio_frames_sent = 0
		self.audio_failures = 0
		self._emit("rtp_audio_started", self.udp_server_port)
		return True
	def _stop_audio(self):
		if self.audio_active:
			self._emit("rtp_audio_stopped", {
				"server_port": self.udp_server_port,
				"frames_sent": self.audio_frames_sent,
				"failed_frames": self.audio_failures})
		self.audio_active = False
		self.first_audio_marker = 0
	def _send_audio_frame(self):
		if self.logout_pending:
			return False
		frame = self.read_audio_frame()
		if frame is None:
			return False
		if not isinstance(frame, (bytes, bytearray)):
			frame = bytes(frame)
		if len(frame) != RTP_PAYLOAD_SIZE:
			self._emit("rtp_audio_frame_rejected", len(frame))
			return False
		sent = self._send_udp_rtp(frame, self.first_audio_marker)
		if sent:
			self.audio_frames_sent += 1
			if self.audio_frames_sent == 1:
				self._emit("rtp_audio_first_frame_sent", self.udp_server_port)
			self.first_audio_marker = 0
		return sent
	def _handle_command(self, item):
		command_type = item["type"]
		payload = item["payload"]
		if (self.logout_pending and command_type not in
				(CMD_RTP_AUDIO_STOP, CMD_LOGOUT, CMD_STOP_ALL)):
			return
		if command_type == CMD_STOP_ALL:
			self._stop_audio()
			self.running = False
			return
		if command_type == CMD_RTP_ACTIVATE:
			self._send_activate(payload)
			self._start_activate_retry(payload)
			return
		if command_type == CMD_RTP_ACTIVATE_CANCEL:
			self._cancel_activate_retry(payload)
			return
		if command_type == CMD_RTP_AUDIO_START:
			self._start_audio(payload)
			return
		if command_type in (CMD_RTP_AUDIO_STOP, CMD_PTT_UP):
			self._stop_audio()
			return
		if command_type == CMD_PTT_DOWN:
			# PTT按下只作为实时状态通知；只有上层确认麦权后才投递
			# RTP_AUDIO_START，防止未获麦权时提前发语音。
			return
		if command_type in TCP_COMMANDS:
			if self.tcp_failed:
				return
			packet = self._packet_from_payload(payload)
			if packet is None:
				self._emit("tcp_command_rejected", command_type)
				return
			key = (packet[4] if len(packet) >= 9 else None,
				   ((packet[7] << 8) | packet[8]) if len(packet) >= 9 else None)
			attempt = self._tcp_attempts.get(key, 0)
			self._tcp_attempts[key] = attempt + 1
			sent = self._send_tcp_packet(command_type, packet, attempt)
			self._emit("tcp_packet_result", {
				"command": command_type, "sent": sent,
				"msg_type": packet[4] if len(packet) >= 9 else None,
				"request_cnt": ((packet[7] << 8) | packet[8]) if len(packet) >= 9 else None,
				"attempt": attempt,
				"bytes": len(packet)})
	def _handle_batch(self, items):
		pending = list(items)
		while pending and self.running:
			self._handle_command(pending.pop(0))
			urgent = self.commands.take_urgent_batch()
			if urgent:
				pending = urgent + pending
	def _run(self):
		try:
			while self.running:
				if not self.audio_active:
					if self._activate_retries:
						stop_pending, items = self.commands.take_batch_nowait()
						if stop_pending or not self.running:
							break
						if items:
							self._handle_batch(items)
						self._process_activate_retries()
						utime.sleep_ms(10)
					else:
						self.commands.wait()
						stop_pending, items = self.commands.take_batch()
						if stop_pending or not self.running:
							break
						self._handle_batch(items)
					continue
				# 音频发送是 P0 实时流；单个音频帧不经过普通命令队列。
				if not self._send_audio_frame():
					utime.sleep_ms(1)
				stop_pending, items = self.commands.take_batch_nowait()
				if stop_pending or not self.running:
					break
				self._handle_batch(items)
		finally:
			self._stop_audio()
			self.running = False
			self.thread_id = None
