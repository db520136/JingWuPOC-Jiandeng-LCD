# -*- coding: utf-8 -*-
"""RTP 基础组包工具，供新的 RTP 收发线程复用。"""


RTP_HEADER_SIZE = 12
RTP_PAYLOAD_TYPE_PCMA = 8
RTP_PAYLOAD_SIZE = 160


def build_rtp_packet(sequence, timestamp, ssrc, payload=b"", marker=0,
					 payload_type=RTP_PAYLOAD_TYPE_PCMA):
	"""构造 RTP 固定头和负载，不访问 socket 或硬件。"""
	sequence = int(sequence) & 0xFFFF
	timestamp = int(timestamp) & 0xFFFFFFFF
	ssrc = int(ssrc) & 0xFFFFFFFF
	if not isinstance(payload, bytes):
		payload = bytes(payload)
	return (bytes((0x80, ((int(marker) & 1) << 7) |
				   (int(payload_type) & 0x7F))) +
			bytes(((sequence >> 8) & 0xFF, sequence & 0xFF)) +
			bytes(((timestamp >> 24) & 0xFF, (timestamp >> 16) & 0xFF,
				   (timestamp >> 8) & 0xFF, timestamp & 0xFF)) +
			bytes(((ssrc >> 24) & 0xFF, (ssrc >> 16) & 0xFF,
				   (ssrc >> 8) & 0xFF, ssrc & 0xFF)) + payload)


def parse_rtp_packet(packet):
	"""解析本项目使用的 RTP 包，返回头字段和负载。"""
	if packet is None or len(packet) < RTP_HEADER_SIZE:
		return None
	data = bytes(packet)
	first = data[0]
	second = data[1]
	offset = RTP_HEADER_SIZE + (first & 0x0F) * 4
	if len(data) < offset:
		return None
	if first & 0x10:
		if len(data) < offset + 4:
			return None
		ext_words = (data[offset + 2] << 8) | data[offset + 3]
		offset += 4 + ext_words * 4
		if len(data) < offset:
			return None
	end = len(data)
	if first & 0x20:
		padding = data[-1]
		if padding <= 0 or padding > end - offset:
			return None
		end -= padding
	if end < offset:
		return None
	return {
		"version": (first >> 6) & 0x03,
		"marker": (second >> 7) & 1,
		"payload_type": second & 0x7F,
		"sequence": (data[2] << 8) | data[3],
		"timestamp": ((data[4] << 24) | (data[5] << 16) |
					  (data[6] << 8) | data[7]),
		"ssrc": ((data[8] << 24) | (data[9] << 16) |
				 (data[10] << 8) | data[11]),
		"payload": data[offset:end],
	}
