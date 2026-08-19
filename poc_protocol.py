# -*- coding: utf-8 -*-
"""POC 私有协议 TCP 帧和字段工具。"""

MAGIC = b"HT"
VERSION = b"\x01\x00"
HEADER_SIZE = 9
MIN_FRAME_SIZE = 11
MAX_PACKET_LENGTH = 4096
MSG_LOGIN = 0x01
MSG_LOGIN_ACK = 0x81
MSG_HEARTBEAT = 0x02
MSG_HEARTBEAT_ACK = 0x82
MSG_JOIN_GROUP = 0x03
MSG_JOIN_GROUP_ACK = 0x83
MSG_INVITE_SINGLE = 0x04
MSG_INVITE_SINGLE_ACK = 0x84
MSG_SINGLE_INVITED = 0x05
MSG_SINGLE_INVITED_ACK = 0x85
MSG_DISSOLVE_SINGLE = 0x06
MSG_DISSOLVE_SINGLE_ACK = 0x86
MSG_REQUEST_FLOOR = 0x08
MSG_REQUEST_FLOOR_ACK = 0x88
MSG_RELEASE_FLOOR = 0x09
MSG_RELEASE_FLOOR_ACK = 0x89
MSG_FORCE_RELEASE_FLOOR = 0x0A
MSG_FORCE_RELEASE_FLOOR_ACK = 0x8A
MSG_FLOOR_OCCUPIED = 0x0B
MSG_FLOOR_OCCUPIED_ACK = 0x8B
MSG_FLOOR_IDLE = 0x0C
MSG_FLOOR_IDLE_ACK = 0x8C
MSG_GNSS_UPLOAD = 0x0D
MSG_GNSS_UPLOAD_ACK = 0x8D
MSG_FORCE_LOGIN = 0x0E
MSG_FORCE_LOGIN_ACK = 0x8E
MSG_LOGOUT = 0x0F
MSG_LOGOUT_ACK = 0x8F
MSG_LEAVE_GROUP = 0x10
MSG_LEAVE_GROUP_ACK = 0x90


def crc16_ccitt_false(data):
    """CRC-16/CCITT-FALSE：初值0xFFFF，多项式0x1021。"""
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


def _u32(value):
    value = int(value) & 0xFFFFFFFF
    return bytes(((value >> 24) & 0xFF, (value >> 16) & 0xFF,
                  (value >> 8) & 0xFF, value & 0xFF))


def _read_u16(data, offset=0):
    if len(data) < offset + 2:
        raise ValueError("UINT16字段长度不足")
    return (int(data[offset]) << 8) | int(data[offset + 1])


def _read_u32(data, offset=0):
    if len(data) < offset + 4:
        raise ValueError("UINT32字段长度不足")
    return ((int(data[offset]) << 24) | (int(data[offset + 1]) << 16) |
            (int(data[offset + 2]) << 8) | int(data[offset + 3]))


def _device_id_bytes(device_id):
    """校验并返回协议固定使用的20字节ASCII设备ID。"""
    value = str(device_id).encode("ascii")
    if len(value) != 20:
        raise ValueError("device_id必须是20字节ASCII")
    return value


def _police_no_bytes(police_no):
    """校验并返回协议固定使用的7字节警号（M/F加6位数字）。"""
    value = str(police_no).encode("ascii")
    if (len(value) != 7 or value[:1] not in (b"M", b"F") or
            not value[1:].isdigit()):
        raise ValueError("警员编号必须是 M/F 加 6 位数字")
    return value


def build_frame(msg_type, request_cnt, payload=b""):
    """构造完整 TCP 帧，长度字段包含末尾 CRC。"""
    if payload is None:
        payload = b""
    if not isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload)
    packet_length = HEADER_SIZE + len(payload) + 2
    if packet_length > 0xFFFF:
        raise ValueError("POC 数据包过长")
    body = (MAGIC + VERSION + bytes((int(msg_type) & 0xFF,)) +
            _u16(packet_length) + _u16(request_cnt) + bytes(payload))
    return body + _u16(crc16_ccitt_false(body))


def parse_frame(frame):
    """解析已经完整接收的 TCP 帧。"""
    if len(frame) < MIN_FRAME_SIZE or bytes(frame[0:2]) != MAGIC:
        raise ValueError("POC 帧头或长度错误")
    if bytes(frame[2:4]) != VERSION:
        raise ValueError("POC 协议版本不支持")
    packet_length = (int(frame[5]) << 8) | int(frame[6])
    if packet_length != len(frame):
        raise ValueError("POC 包长度字段错误")
    expected = (int(frame[-2]) << 8) | int(frame[-1])
    actual = crc16_ccitt_false(frame[:-2])
    if expected != actual:
        raise ValueError("POC CRC 错误")
    return {"msg_type": int(frame[4]), "packet_length": packet_length,
            "request_cnt": (int(frame[7]) << 8) | int(frame[8]),
            "payload": bytes(frame[9:-2]), "raw": bytes(frame)}


class FrameParser:
    """处理 TCP 粘包、拆包和魔数重新同步。"""

    def __init__(self, max_packet_length=MAX_PACKET_LENGTH):
        self.buffer = bytearray()
        self.max_packet_length = int(max_packet_length)

    def _discard(self, count):
        """丢弃前 count 字节。

        部分 EC800M 固件不支持 bytearray 的切片删除（del buf[:n]），
        因此通过重新构造剩余缓冲区保持兼容。
        """
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


def build_login_payload(device_id, police_no):
    """登录消息体：device_id[20] + police_no[7]，不填本地UDP端口。"""
    device = str(device_id).encode("ascii")
    if len(device) != 20:
        raise ValueError("device_id 必须是20字节 ASCII")
    police = _police_no_bytes(police_no)
    return device + police


def build_force_login_payload(device_id, police_no):
    """0x0E：确认警号强制转机登录，device_id[20]+police_no[7]。"""
    return build_login_payload(device_id, police_no)


def parse_force_login_ack(payload):
    if len(payload) < 1:
        raise ValueError("转机登录应答消息体为空")
    return {"login_result": int(payload[0]), "extra": bytes(payload[1:])}


def build_logout_payload(device_id, police_no):
    """0x0F: device_id[20] + police_no[7]."""
    return _device_id_bytes(device_id) + _police_no_bytes(police_no)


def parse_logout_payload(payload):
    """Parse a server-initiated 0x0F logout request."""
    if len(payload) != 27:
        raise ValueError("0x0F payload must be exactly 27 bytes")
    return {"device_id": bytes(payload[:20]),
            "police_no": bytes(payload[20:27]),
            "extra": b""}


def build_logout_ack_payload(result=0):
    """0x8F: result[1]."""
    return bytes((int(result) & 0xFF,))


def parse_logout_ack(payload):
    """Parse the one-byte 0x8F logout result."""
    if len(payload) != 1:
        raise ValueError("0x8F payload must be exactly 1 byte")
    return {"logout_result": int(payload[0])}


def build_heartbeat_payload(device_id):
    """构造TCP心跳请求消息体：固定20字节device_id。"""
    return _device_id_bytes(device_id)


def build_gnss_upload_payload(device_id, gnss_data=b""):
    """构造0x0D：device_id[20] + gnss_len[1] + GGA ASCII。"""
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
    """0x8D应答消息体必须为空。"""
    #if payload:
    #    raise ValueError("定位信息上报应答消息体应为0字节")
    return True


def build_join_group_payload(device_id, group_id, police_no=None):
    """构造入组请求：device_id[20] + police_no[7] + group_id UINT32。"""
    if police_no is None:
        raise ValueError("入组请求缺少police_no")
    return (_device_id_bytes(device_id) + _police_no_bytes(police_no) +
            _u32(group_id))


def parse_join_group_ack(payload):
    """解析入组应答：device_id[20]+police_no[7]+group_id[4]+result[1]。"""
    if len(payload) != 32:
        raise ValueError("0x83/0x90 payload must be exactly 32 bytes")
    group_id = _read_u32(payload, 27)
    return {"device_id": bytes(payload[:20]), "group_id": group_id,
            "police_no": bytes(payload[20:27]),
            "join_result": int(payload[31]),
            "extra": bytes(payload[32:])}


def build_leave_group_payload(device_id, police_no, group_id):
    return (build_join_group_payload(device_id, group_id, police_no))


def parse_leave_group_ack(payload):
    return parse_join_group_ack(payload)


def parse_login_ack(payload):
    if not payload:
        raise ValueError("登录应答消息体为空")
    return {"login_result": int(payload[0]), "extra": bytes(payload[1:])}


def build_invite_single_payload(device_id, target_device_id, police_no,
                                target_police_no):
    """0x04: two device IDs followed by both seven-byte police numbers."""
    return (_device_id_bytes(device_id) +
            _device_id_bytes(target_device_id) +
            _police_no_bytes(police_no) +
            _police_no_bytes(target_police_no))


def parse_invite_single_ack(payload):
    """Parse 0x84: ack 1 is 2 bytes and ack 2 is always 6 bytes."""
    if not payload:
        raise ValueError("0x84 payload is empty")
    ack_num = int(payload[0])
    if ack_num == 1:
        if len(payload) != 2:
            raise ValueError("0x84 ack_num=1 payload must be exactly 2 bytes")
        invite_status = int(payload[1])
        group_id = None
    elif ack_num == 2:
        if len(payload) != 6:
            raise ValueError("0x84 ack_num=2 payload must be exactly 6 bytes")
        invite_status = int(payload[1])
        group_id = _read_u32(payload, 2)
    else:
        raise ValueError("unsupported 0x84 ack_num: {}".format(ack_num))
    return {"ack_num": ack_num, "invite_status": invite_status,
            "group_id": group_id, "extra": b""}


def parse_single_invited(payload):
    """Parse server 0x05: src_police_no[7] + group_id[4]."""
    if len(payload) != 11:
        raise ValueError("0x05 payload must be exactly 11 bytes")
    return {"src_police_no": bytes(payload[:7]),
            "group_id": _read_u32(payload, 7),
            "extra": b""}


def build_single_invited_ack_payload(device_id, police_no, group_id,
                                     answer_status):
    """0x85: device_id[20] + police_no[7] + group_id[4] + status[1]."""
    return (_device_id_bytes(device_id) + _police_no_bytes(police_no) +
            _u32(group_id) +
            bytes((int(answer_status) & 0xFF,)))


def build_dissolve_single_payload(device_id, police_no, group_id):
    """0x06: device_id[20] + police_no[7] + group_id[4]."""
    return (_device_id_bytes(device_id) + _police_no_bytes(police_no) +
            _u32(group_id))


def parse_dissolve_single(payload):
    """Parse 0x06: device_id[20] + police_no[7] + group_id[4]."""
    if len(payload) != 31:
        raise ValueError("0x06 payload must be exactly 31 bytes")
    return {"device_id": bytes(payload[:20]),
            "police_no": bytes(payload[20:27]),
            "group_id": _read_u32(payload, 27),
            "extra": b""}


def build_dissolve_single_ack_payload(device_id, release_result):
    """0x86保持原格式：设备ID + 解散结果。"""
    return (_device_id_bytes(device_id) +
            bytes((int(release_result) & 0xFF,)))


def parse_dissolve_single_ack(payload):
    if len(payload) != 21:
        raise ValueError("0x86 payload must be exactly 21 bytes")
    return {"device_id": bytes(payload[:20]),
            "release_result": int(payload[20]),
            "extra": bytes(payload[21:])}


def build_floor_payload(device_id, police_no, group_id):
    """0x08和0x09共用：设备ID + 警号 + 当前通话对应的组ID。"""
    return (_device_id_bytes(device_id) + _police_no_bytes(police_no) +
            _u32(group_id))


def parse_request_floor_ack(payload):
    if len(payload) < 3:
        raise ValueError("抢麦应答消息体长度不足")
    return {"floor_status": int(payload[0]),
            "udp_port": _read_u16(payload, 1),
            "extra": bytes(payload[3:])}


def parse_release_floor_ack(payload):
    if len(payload) < 25:
        raise ValueError("释放麦应答消息体长度不足")
    return {"device_id": bytes(payload[:20]),
            "group_id": _read_u32(payload, 20),
            "release_status": int(payload[24]),
            "extra": bytes(payload[25:])}


def build_device_payload(device_id):
    """0x8A和0x8B保持原格式，消息体仅包含本机设备ID。"""
    return _device_id_bytes(device_id)


def parse_floor_occupied(payload):
    """解析0x0B可变长度姓名以及端口、组ID。"""
    if not payload:
        raise ValueError("麦权占用通知消息体为空")
    name_len = int(payload[0])
    end = 1 + name_len
    if len(payload) < end + 6:
        raise ValueError("麦权占用通知消息体长度不足")
    raw_name = bytes(payload[1:end])
    try:
        police_name = raw_name.decode("utf-8")
    except Exception:
        police_name = ""
    return {"name_len": name_len, "police_name": police_name,
            "police_name_raw": raw_name,
            "udp_port": _read_u16(payload, end),
            "group_id": _read_u32(payload, end + 2),
            "extra": bytes(payload[end + 6:])}


def parse_floor_idle(payload):
    if len(payload) != 4:
        raise ValueError("0x0C payload must be exactly 4 bytes")
    return {"group_id": _read_u32(payload, 0),
            "extra": b""}


def build_floor_idle_ack_payload(device_id, group_id):
    """0x8C：本机设备ID + 已处理的空闲组ID。"""
    return _device_id_bytes(device_id) + _u32(group_id)


def group_id_to_uint32(raw_id):
    """数字ID直接转换；非数字ID用稳定的FNV-1a映射到UINT32。"""
    text = str(raw_id).strip()
    if not text:
        raise ValueError("groupID为空")
    try:
        return int(text, 0 if text.lower().startswith("0x") else 10) & 0xFFFFFFFF
    except Exception:
        value = 2166136261
        for char in text:
            for byte in char.encode("utf-8"):
                value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
        return value


def uint32_bytes(value):
    return _u32(value)
