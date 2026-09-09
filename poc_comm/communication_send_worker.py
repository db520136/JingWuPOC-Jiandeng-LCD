# -*- coding: utf-8 -*-
"""隔离的 TCP/RTP 统一发送 Worker。

这是第三阶段的新发送层，目前不由主入口启动。该线程是新架构中唯一执行
TCP ``send()`` 和 UDP ``sendto()`` 的位置：

* TCP 控制命令的 payload 必须是已经按协议组好的 ``bytes``；
* ``CMD_TCP_PRIORITY_RESPONSE`` 的 payload 也是完整 TCP 帧；
* ``CMD_RTP_ACTIVATE`` 的 payload 是 UDP 目标端口整数，或包含
  ``server_port`` 的字典；
* RTP 音频帧由显式注入的 ``read_audio_frame()`` 提供，不进入命令队列。

协议状态机、重试计时和登录/麦权状态仍由上层后续迁移；本文件只负责发送
边界、P0 实时流和队列调度，不访问 UI，不进行动态属性探测。
"""

import _thread
import utime

try:
    from usr.comm_commands import (CMD_ACCEPT_SINGLE_CALL,
                                   CMD_DISSOLVE_SINGLE_CALL, CMD_FORCE_LOGIN,
                                   CMD_FORCE_RELEASE_FLOOR, CMD_GNSS_UPLOAD,
                                   CMD_JOIN_GROUP, CMD_LEAVE_GROUP,
                                   CMD_LOGIN, CMD_LOGOUT, CMD_PTT_DOWN,
                                   CMD_PTT_UP, CMD_RELEASE_FLOOR,
                                   CMD_REQUEST_FLOOR, CMD_REJECT_SINGLE_CALL,
                                   CMD_RTP_ACTIVATE, CMD_RTP_AUDIO_START,
                                   CMD_RTP_AUDIO_STOP, CMD_SINGLE_CALL,
                                   CMD_STATUS_SYNC, CMD_STOP_ALL,
                                   CMD_TCP_HEARTBEAT,
                                   CMD_TCP_PRIORITY_RESPONSE)
    from usr.communication_queue import CommunicationCommandQueue
    from usr.rtp_protocol import RTP_PAYLOAD_SIZE, build_rtp_packet
except ImportError:
    from comm_commands import (CMD_ACCEPT_SINGLE_CALL,
                               CMD_DISSOLVE_SINGLE_CALL, CMD_FORCE_LOGIN,
                               CMD_FORCE_RELEASE_FLOOR, CMD_GNSS_UPLOAD,
                               CMD_JOIN_GROUP, CMD_LEAVE_GROUP, CMD_LOGIN,
                               CMD_LOGOUT, CMD_PTT_DOWN, CMD_PTT_UP,
                               CMD_RELEASE_FLOOR, CMD_REQUEST_FLOOR,
                               CMD_REJECT_SINGLE_CALL, CMD_RTP_ACTIVATE,
                               CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP,
                               CMD_SINGLE_CALL, CMD_STATUS_SYNC, CMD_STOP_ALL,
                               CMD_TCP_HEARTBEAT, CMD_TCP_PRIORITY_RESPONSE)
    from communication_queue import CommunicationCommandQueue
    from rtp_protocol import RTP_PAYLOAD_SIZE, build_rtp_packet


RTP_ACTIVATE_PAYLOAD = b"\x00"

TCP_COMMANDS = (
    CMD_LOGIN, CMD_FORCE_LOGIN, CMD_LOGOUT, CMD_JOIN_GROUP,
    CMD_LEAVE_GROUP, CMD_SINGLE_CALL, CMD_ACCEPT_SINGLE_CALL,
    CMD_REJECT_SINGLE_CALL, CMD_DISSOLVE_SINGLE_CALL, CMD_REQUEST_FLOOR,
    CMD_RELEASE_FLOOR, CMD_FORCE_RELEASE_FLOOR, CMD_TCP_HEARTBEAT,
    CMD_GNSS_UPLOAD, CMD_STATUS_SYNC, CMD_TCP_PRIORITY_RESPONSE,
)


class CommunicationSendWorker:
    """统一发送线程；一个实例拥有 TCP/UDP 两个发送 socket。"""

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
        self.audio_active = False
        self.commands.signal_stop()

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

    def _send_tcp_packet(self, packet):
        if self.tcp_socket is None:
            self._emit("tcp_send_rejected", "tcp_socket_missing")
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
            self._emit("tcp_send_error", error)
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
        if self.udp_socket is None or self.udp_server_port is None:
            self._emit("rtp_send_rejected", "udp_target_missing")
            return False
        packet = build_rtp_packet(
            self.sequence, self.timestamp, self.ssrc, payload, marker)
        try:
            self.udp_socket.sendto(
                packet, (self.server_ip, self.udp_server_port))
        except Exception as error:
            self._emit("rtp_send_error", error)
            return False
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp = (self.timestamp + len(payload)) & 0xFFFFFFFF
        return True

    def _send_activate(self, payload):
        port = self._udp_port_from_payload(payload)
        if port is None:
            self._emit("rtp_activate_rejected", payload)
            return False
        self.udp_server_port = port
        return self._send_udp_rtp(RTP_ACTIVATE_PAYLOAD)

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
        return True

    def _stop_audio(self):
        self.audio_active = False
        self.first_audio_marker = 0

    def _send_audio_frame(self):
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
            self.first_audio_marker = 0
        return sent

    def _handle_command(self, item):
        command_type = item["type"]
        payload = item["payload"]
        if command_type == CMD_STOP_ALL:
            self._stop_audio()
            self.running = False
            return
        if command_type == CMD_RTP_ACTIVATE:
            self._send_activate(payload)
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
            packet = self._packet_from_payload(payload)
            if packet is None:
                self._emit("tcp_command_rejected", command_type)
                return
            self._send_tcp_packet(packet)

    def _run(self):
        try:
            while self.running:
                if not self.audio_active:
                    self.commands.wait()
                    stop_pending, items = self.commands.take_batch()
                    if stop_pending or not self.running:
                        break
                    for item in items:
                        self._handle_command(item)
                    continue

                # 音频发送是 P0 实时流；单个音频帧不经过普通命令队列。
                if not self._send_audio_frame():
                    utime.sleep_ms(1)
                stop_pending, items = self.commands.take_batch_nowait()
                if stop_pending or not self.running:
                    break
                for item in items:
                    self._handle_command(item)
        finally:
            self._stop_audio()
            self.running = False
            self.thread_id = None
