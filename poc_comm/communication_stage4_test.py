# -*- coding: utf-8 -*-
"""第四阶段独立测试：三线程运行时，不启动 UI 和旧通信代码。"""
from machine import Pin
import utime
gpio1 = Pin(Pin.GPIO29, Pin.IN, Pin.PULL_PU , 0)
while 1:
    print("io=",gpio1.read())
    utime.sleep_ms(1000)
try:
    from comm_commands import (CMD_LOGIN, CMD_RTP_ACTIVATE,
                               CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP)
    from communication_queue import CommunicationCommandQueue
    from communication_runtime import CommunicationRuntime
    from poc_protocol import MSG_LOGIN, build_frame
    from rtp_protocol import RTP_PAYLOAD_SIZE
except ImportError:
    from usr.comm_commands import (CMD_LOGIN, CMD_RTP_ACTIVATE,
                                   CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP)
    from usr.communication_queue import CommunicationCommandQueue
    from usr.communication_runtime import CommunicationRuntime
    from usr.poc_protocol import MSG_LOGIN, build_frame
    from usr.rtp_protocol import RTP_PAYLOAD_SIZE


class CaptureSocket:
    """同时支持发送、TCP接收和UDP接收的可控假 Socket。"""

    def __init__(self, receive_data, udp=False):
        self.receive_data = list(receive_data)
        self.udp = bool(udp)
        self.sent = []
        self.closed = False

    def send(self, data):
        if self.closed:
            raise OSError("socket closed")
        self.sent.append(bytes(data))
        return len(data)

    def sendto(self, data, address):
        if self.closed:
            raise OSError("socket closed")
        self.sent.append((bytes(data), address))
        return len(data)

    def recv(self, size):
        while not self.receive_data and not self.closed:
            utime.sleep_ms(2)
        if self.closed:
            raise OSError("socket closed")
        return self.receive_data.pop(0)

    def recvfrom(self, size):
        while not self.receive_data and not self.closed:
            utime.sleep_ms(2)
        if self.closed:
            raise OSError("socket closed")
        return self.receive_data.pop(0), ("127.0.0.1", 37114)

    def close(self):
        self.closed = True


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("[通信测试] 第四阶段三线程运行时测试开始")
    print("[通信测试] 不导入UI，不启动旧TCP/RTP")
    tcp_frame = build_frame(MSG_LOGIN, 1, b"")
    tcp_socket = CaptureSocket([tcp_frame])
    udp_socket = CaptureSocket([b"rtp-receive-test"], udp=True)
    events = []
    frame = bytes((0xD5,)) * RTP_PAYLOAD_SIZE
    runtime = CommunicationRuntime(
        tcp_socket=tcp_socket,
        udp_socket=udp_socket,
        server_ip="127.0.0.1",
        udp_server_port=37114,
        read_audio_frame=lambda: frame,
        event_callback=lambda event: events.append(event),
        command_queue=CommunicationCommandQueue(max_length=16))
    _assert(runtime.start(), "三线程运行时启动失败")
    utime.sleep_ms(30)
    status = runtime.worker_status()
    _assert(status["runtime"], "运行时状态未置为运行")
    _assert(status["tcp_receive"], "TCP接收线程未运行")
    _assert(status["rtp_receive"], "RTP接收线程未运行")
    _assert(status["send"], "统一发送线程未运行")
    print("[通信测试] 三线程已启动：TCP接收、UDP接收、TCP/RTP发送")

    login_frame = build_frame(MSG_LOGIN, 2, b"")
    _assert(runtime.submit(CMD_LOGIN, login_frame), "TCP命令入队失败")
    _assert(runtime.submit(CMD_RTP_ACTIVATE, 37114), "RTP激活入队失败")
    _assert(runtime.submit(CMD_RTP_AUDIO_START, 37114),
            "RTP音频开始入队失败")
    utime.sleep_ms(80)
    _assert(len(tcp_socket.sent) >= 1, "统一线程未发送TCP命令")
    _assert(len(udp_socket.sent) >= 2, "统一线程未发送RTP激活/音频")
    _assert(any(event["source"] == "tcp" for event in events),
            "TCP接收事件未回调")
    _assert(any(event["source"] == "rtp" for event in events),
            "RTP接收事件未回调")
    print("[通信测试] TCP命令、RTP激活、RTP音频发送通过")
    print("[通信测试] TCP/UDP接收回调通过")

    _assert(runtime.submit(CMD_RTP_AUDIO_STOP), "RTP音频停止入队失败")
    utime.sleep_ms(10)
    runtime.stop()
    utime.sleep_ms(30)
    status = runtime.worker_status()
    _assert(not status["runtime"], "运行时停止状态错误")
    _assert(not status["send"], "统一发送线程未停止")
    print("[通信测试] 音频停止、三线程停止、Socket关闭通过")
    print("[通信测试] 第四阶段三线程运行时测试全部通过")


if __name__ == "__main__":
    main()

