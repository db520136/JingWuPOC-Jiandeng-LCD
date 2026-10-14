# -*- coding: utf-8 -*-
import _thread
import utime
from usr.rtp_protocol import RTP_PAYLOAD_SIZE, parse_rtp_packet
RTP_RX_FIRST_PACKET = "rtp_first_packet"
RTP_RX_ERROR = "rtp_receive_error"
RTP_ACTIVE_TIMEOUT_S = 0.020
RTP_PACKET_INTERVAL_MS = 20
RTP_IDLE_AFTER_MS = 5000
RTP_START_PACKETS = 5
RTP_MAX_PACKETS = 24
RTP_SILENCE_FRAME = bytes((0xD5,)) * RTP_PAYLOAD_SIZE
def _seq_distance(sequence, expected):
	return (int(sequence) - int(expected)) & 0xFFFF
class RTPReceiveWorker:
	def __init__(self, socket_obj, packet_callback, receive_size=1024,
				 write_audio_frame=None):
		if socket_obj is None:
			raise ValueError("socket_obj不能为空")
		if packet_callback is None:
			raise ValueError("packet_callback不能为空")
		self.socket = socket_obj
		self.packet_callback = packet_callback
		self.write_audio_frame = write_audio_frame
		self.receive_size = max(1, int(receive_size))
		self.running = False
		self.thread_id = None
		self._lock = _thread.allocate_lock()
		self._requested_context = None
		self._generation = 0
		self._worker_mode = "idle"
		self._current_context = None
		self._buffer = {}
		self._expected_sequence = None
		self._started = False
		self._next_play_ms = None
		self._last_rtp_ms = None
	def start(self):
		if self.running:
			return False
		self.running = True
		self._set_idle_mode()
		try:
			self.thread_id = _thread.start_new_thread(self._run, ())
			return True
		except Exception:
			self.running = False
			self.thread_id = None
			raise
	def stop(self):
		self.running = False
		self.clear_receive_context()
		try:
			self.socket.close()
		except Exception:
			pass
	def set_receive_context(self, group_id, call_type, server_port):
		self._lock.acquire()
		try:
			if (self._requested_context is not None and
					self._requested_context.get("group_id") == int(group_id) and
					self._requested_context.get("call_type") == str(call_type) and
					self._requested_context.get("server_port") == int(server_port)):
				return dict(self._requested_context)
			self._generation += 1
			self._requested_context = {
				"group_id": int(group_id), "call_type": str(call_type),
				"server_port": int(server_port),
				"generation": self._generation}
			return dict(self._requested_context)
		finally:
			self._lock.release()
	def clear_receive_context(self):
		self._lock.acquire()
		try:
			self._generation += 1
			self._requested_context = None
		finally:
			self._lock.release()
	def _read_context(self):
		self._lock.acquire()
		try:
			return (None if self._requested_context is None else
					dict(self._requested_context))
		finally:
			self._lock.release()
	def _clear_jitter(self):
		self._buffer = {}
		self._expected_sequence = None
		self._started = False
		self._next_play_ms = None
		self._last_rtp_ms = None
	def _resync_jitter(self, sequence):
		self._buffer = {}
		self._expected_sequence = int(sequence) & 0xFFFF
		self._started = False
		self._next_play_ms = None
	def _set_idle_mode(self):
		self._clear_jitter()
		self._worker_mode = "idle"
		self._current_context = None
		self.socket.settimeout(None)
	def _set_active_mode(self, context, parsed, now):
		self._clear_jitter()
		self._worker_mode = "active"
		self._current_context = dict(context)
		self._expected_sequence = parsed["sequence"]
		self._last_rtp_ms = now
		self.socket.settimeout(RTP_ACTIVE_TIMEOUT_S)
		self._push_packet(parsed)
		self.packet_callback({
			"type": RTP_RX_FIRST_PACKET,
			"payload": dict(context)})
	@staticmethod
	def _valid_packet(packet):
		parsed = parse_rtp_packet(packet)
		if (parsed is None or parsed["version"] != 2 or
				parsed["payload_type"] != 8 or
				len(parsed["payload"]) != RTP_PAYLOAD_SIZE):
			return None
		return parsed
	@staticmethod
	def _source_matches(address, context):
		if not isinstance(address, (tuple, list)) or len(address) < 2:
			return True
		try:
			return int(address[1]) == int(context["server_port"])
		except Exception:
			return False
	def _push_packet(self, parsed):
		sequence = parsed["sequence"]
		if sequence in self._buffer:
			return False
		if self._expected_sequence is None:
			self._expected_sequence = sequence
		distance = _seq_distance(sequence, self._expected_sequence)
		if distance >= 0x8000:
			return False
		if distance >= RTP_MAX_PACKETS:
			self._resync_jitter(sequence)
		self._buffer[sequence] = parsed["payload"]
		if len(self._buffer) > RTP_MAX_PACKETS:
			self._resync_jitter(sequence)
			self._buffer[sequence] = parsed["payload"]
		return True
	def _play_one(self):
		if self._expected_sequence is None:
			return False
		sequence = self._expected_sequence
		frame = self._buffer.pop(sequence, None)
		if frame is None:
			if not self._buffer:
				return False
			frame = RTP_SILENCE_FRAME
		self._expected_sequence = (sequence + 1) & 0xFFFF
		if self.write_audio_frame is not None:
			self.write_audio_frame(frame, require_active=True)
		return True
	def _drive_playback(self, now):
		if not self._started:
			if len(self._buffer) < RTP_START_PACKETS:
				return
			self._started = True
			self._next_play_ms = now
		while (self._next_play_ms is not None and
				utime.ticks_diff(now, self._next_play_ms) >= 0):
			if not self._play_one():
				break
			self._next_play_ms = utime.ticks_add(
				self._next_play_ms, RTP_PACKET_INTERVAL_MS)
	@staticmethod
	def _is_timeout(error):
		errno = getattr(error, "errno", None)
		if errno in (11, 110, 116, 10060):
			return True
		text = str(error).lower()
		return ("eagain" in text or "timed out" in text or
				"timeout" in text)
	def _active_tick(self, now):
		context = self._read_context()
		if context is None or context != self._current_context:
			self._set_idle_mode()
			return
		if (self._last_rtp_ms is not None and
				utime.ticks_diff(now, self._last_rtp_ms) >= RTP_IDLE_AFTER_MS):
			# 只让接收线程休眠，保留业务层远端讲话、喇叭和弹框状态。
			self._set_idle_mode()
			return
		self._drive_playback(now)
	def _run(self):
		try:
			while self.running:
				try:
					packet, address = self.socket.recvfrom(self.receive_size)
				except Exception as error:
					if (self.running and self._worker_mode == "active" and
							self._is_timeout(error)):
						self._active_tick(utime.ticks_ms())
						continue
					if self.running:
						self.packet_callback({"type": RTP_RX_ERROR, "payload": error})
					break
				if not packet:
					continue
				now = utime.ticks_ms()
				context = self._read_context()
				parsed = self._valid_packet(packet)
				if (context is None or parsed is None or
						not self._source_matches(address, context)):
					if self._worker_mode == "active":
						self._active_tick(now)
					continue
				if (self._worker_mode != "active" or
						self._current_context != context):
					self._set_active_mode(context, parsed, now)
				else:
					self._last_rtp_ms = now
					self._push_packet(parsed)
				self._drive_playback(now)
		finally:
			self._clear_jitter()
			self.running = False
			self.thread_id = None
