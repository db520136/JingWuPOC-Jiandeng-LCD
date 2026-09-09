# -*- coding: utf-8 -*-
"""永久阻塞式 UDP/RTP 接收线程。

本阶段按确认方案使用永久阻塞 ``recvfrom()``。线程只接收并投递原始 RTP
数据包，不执行 UDP sendto、不执行激活包发送、不处理 UI。UDP 对端停止发送
不会产生可观察的“断连”事件，超时和可中断等待留到后续阶段。
"""

import _thread


RTP_RX_PACKET = "rtp_packet"
RTP_RX_ERROR = "rtp_receive_error"


class RTPReceiveWorker:
    """一个 UDP socket 对应一个永久阻塞接收线程。"""

    def __init__(self, socket_obj, packet_callback, receive_size=1024):
        if socket_obj is None:
            raise ValueError("socket_obj不能为空")
        if packet_callback is None:
            raise ValueError("packet_callback不能为空")
        self.socket = socket_obj
        self.packet_callback = packet_callback
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
        """永久 recvfrom 无超时；停止时关闭 socket，具体唤醒行为由固件决定。"""
        self.running = False
        try:
            self.socket.close()
        except Exception:
            pass

    def _run(self):
        try:
            while self.running:
                try:
                    packet, address = self.socket.recvfrom(self.receive_size)
                except Exception as error:
                    if self.running:
                        self.packet_callback({
                            "type": RTP_RX_ERROR,
                            "payload": error,
                        })
                    break
                if packet:
                    self.packet_callback({
                        "type": RTP_RX_PACKET,
                        "payload": packet,
                        "address": address,
                    })
        finally:
            self.running = False
            self.thread_id = None
