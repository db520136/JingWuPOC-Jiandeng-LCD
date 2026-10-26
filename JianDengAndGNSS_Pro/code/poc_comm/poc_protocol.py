# -*- coding: utf-8 -*-
MAGIC = b"HT"
VERSION = b"\x01\x00"
HEADER_SIZE = 9
MIN_FRAME_SIZE = 11
MAX_PACKET_LENGTH = 4096
MSG_HEARTBEAT = 0x02
MSG_HEARTBEAT_ACK = 0x82
MSG_GNSS_UPLOAD = 0x0D
MSG_GNSS_UPLOAD_ACK = 0x8D


def crc16_ccitt_false(data):
	crc = 0xFFFF
	for value in data:
		crc ^= (int(value) & 0xFF) << 8
		for _ in range(8):
			crc = (((crc << 1) ^ 0x1021) if crc & 0x8000 else
				   (crc << 1)) & 0xFFFF
	return crc


def _u16(value):
	value = int(value) & 0xFFFF
	return bytes(((value >> 8) & 0xFF, value & 0xFF))


def _device_id_bytes(device_id):
	value = str(device_id).encode("ascii")
	if len(value) != 20:
		raise ValueError("device_id必须是20字节ASCII")
	return value


def build_frame(msg_type, request_cnt, payload=b""):
	if payload is None:
		payload = b""
	if not isinstance(payload, (bytes, bytearray)):
		payload = bytes(payload)
	packet_length = HEADER_SIZE + len(payload) + 2
	if packet_length > 0xFFFF:
		raise ValueError("POC数据包过长")
	body = (MAGIC + VERSION + bytes((int(msg_type) & 0xFF,)) +
			_u16(packet_length) + _u16(request_cnt) + bytes(payload))
	return body + _u16(crc16_ccitt_false(body))


def parse_frame(frame):
	if len(frame) < MIN_FRAME_SIZE or bytes(frame[0:2]) != MAGIC:
		raise ValueError("POC帧头或长度错误")
	if bytes(frame[2:4]) != VERSION:
		raise ValueError("POC协议版本不支持")
	packet_length = (int(frame[5]) << 8) | int(frame[6])
	if packet_length != len(frame):
		raise ValueError("POC包长度字段错误")
	expected = (int(frame[-2]) << 8) | int(frame[-1])
	actual = crc16_ccitt_false(frame[:-2])
	if expected != actual:
		raise ValueError("POC CRC错误")
	return {
		"msg_type": int(frame[4]),
		"packet_length": packet_length,
		"request_cnt": (int(frame[7]) << 8) | int(frame[8]),
		"payload": bytes(frame[9:-2]),
		"raw": bytes(frame),
	}


class FrameParser:
	def __init__(self, max_packet_length=MAX_PACKET_LENGTH):
		self.buffer = bytearray()
		self.max_packet_length = int(max_packet_length)

	def _discard(self, count):
		count = max(0, int(count))
		if count >= len(self.buffer):
			self.buffer = bytearray()
		elif count:
			self.buffer = bytearray(self.buffer[count:])

	def feed(self, data):
		if data:
			self.buffer.extend(data)
		result = []
		while True:
			if len(self.buffer) < 2:
				break
			pos = bytes(self.buffer).find(MAGIC)
			if pos < 0:
				keep = 1 if self.buffer[-1] == MAGIC[0] else 0
				self.buffer = (bytearray(self.buffer[-keep:])
						   if keep else bytearray())
				break
			if pos:
				self._discard(pos)
			if len(self.buffer) < HEADER_SIZE:
				break
			length = (int(self.buffer[5]) << 8) | int(self.buffer[6])
			if length < MIN_FRAME_SIZE or length > self.max_packet_length:
				self._discard(2)
				continue
			if len(self.buffer) < length:
				break
			candidate = bytes(self.buffer[:length])
			try:
				frame = parse_frame(candidate)
			except Exception:
				self._discard(2)
				continue
			self._discard(length)
			result.append(frame)
		return result


def build_heartbeat_payload(device_id):
	return _device_id_bytes(device_id)


def build_gnss_upload_payload(device_id, gnss_data=b""):
	if gnss_data is None:
		gnss_data = b""
	if isinstance(gnss_data, str):
		gnss_data = gnss_data.encode("ascii")
	elif not isinstance(gnss_data, bytes):
		gnss_data = bytes(gnss_data)
	if len(gnss_data) > 0xFF:
		raise ValueError("GNSS定位数据超过UINT8长度范围")
	return (_device_id_bytes(device_id) + bytes((len(gnss_data),)) +
			gnss_data)


def parse_gnss_upload_ack(payload):
	return True
