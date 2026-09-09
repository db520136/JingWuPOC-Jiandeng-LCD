# -*- coding: utf-8 -*-
"""PTT GPIO29 和 POC 麦权会话状态机。

本模块不直接访问 Socket；所有 TCP/RTP 发送都通过注入的 ``submit_command``
进入 CommunicationRuntime 的有界队列。GPIO29 中断只负责唤醒本模块的事件
线程，消抖、协议组包、状态改变和命令投递全部在线程上下文完成。
"""

import _thread
import utime

try:
    from machine import ExtInt, Pin
except ImportError:
    ExtInt = None
    Pin = None

try:
    from usr.comm_commands import (CMD_RELEASE_FLOOR, CMD_RTP_ACTIVATE,
                                   CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP,
                                   CMD_REQUEST_FLOOR)
    from usr.poc_protocol import (MSG_FLOOR_IDLE, MSG_FLOOR_OCCUPIED,
                                  MSG_RELEASE_FLOOR,
                                  MSG_RELEASE_FLOOR_ACK, MSG_REQUEST_FLOOR,
                                  MSG_REQUEST_FLOOR_ACK, build_floor_payload,
                                  build_frame, parse_floor_idle,
                                  parse_floor_occupied,
                                  parse_release_floor_ack,
                                  parse_request_floor_ack)
except ImportError:
    from comm_commands import (CMD_RELEASE_FLOOR, CMD_RTP_ACTIVATE,
                               CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP,
                               CMD_REQUEST_FLOOR)
    from poc_protocol import (MSG_FLOOR_IDLE, MSG_FLOOR_OCCUPIED,
                              MSG_RELEASE_FLOOR,
                              MSG_RELEASE_FLOOR_ACK, MSG_REQUEST_FLOOR,
                              MSG_REQUEST_FLOOR_ACK, build_floor_payload,
                              build_frame, parse_floor_idle,
                              parse_floor_occupied,
                              parse_release_floor_ack,
                              parse_request_floor_ack)


PTT_GPIO = 29
PTT_ACTIVE_LEVEL = 0
PTT_DEBOUNCE_MS = 30

STATE_IDLE = "idle"
STATE_REQUESTING_FLOOR = "requesting_floor"
STATE_TRANSMITTING = "transmitting"
STATE_RELEASING_FLOOR = "releasing_floor"
STATE_DISCONNECTED = "disconnected"


class CommunicationSession:
    """单实例 PTT/麦权会话；空闲时线程永久阻塞在信号量上。"""

    def __init__(self, submit_command, device_id, police_no,
                 audio_port=None, ptt_gpio=PTT_GPIO,
                 event_callback=None):
        if submit_command is None:
            raise ValueError("submit_command不能为空")
        self.submit_command = submit_command
        self.device_id = str(device_id)
        self.police_no = str(police_no)
        self.audio_port = audio_port
        self.ptt_gpio = int(ptt_gpio)
        self.event_callback = event_callback
        self.group_id = None
        self.next_request_count = 1
        self.state = STATE_DISCONNECTED
        self.ptt_pressed = False
        self.release_pending = False
        self.pending_request_count = None
        self.pending_release_count = None
        self.floor_udp_port = None
        self.ptt_pin = None
        self.ptt_irq = None
        self.running = False
        self.thread_id = None
        self._lock = _thread.allocate_lock()
        self._event_sem = self._new_event_sem()
        self._wake_pending = False
        self._ptt_irq_pending = False
        self._events = []

    @staticmethod
    def _new_event_sem():
        sem = _thread.allocate_semphore(1)
        try:
            count = sem.getCnt()
            initial = count[1] if isinstance(count, (tuple, list)) else count
            for _ in range(int(initial or 0)):
                sem.acquire()
        except Exception:
            try:
                sem.acquire()
            except Exception:
                pass
        return sem

    def _emit(self, event_type, payload=None):
        if self.event_callback is not None:
            self.event_callback({"type": event_type, "payload": payload})

    def _signal(self):
        self._lock.acquire()
        try:
            if self._wake_pending:
                return
            self._wake_pending = True
        finally:
            self._lock.release()
        try:
            self._event_sem.release()
        except Exception:
            self._lock.acquire()
            try:
                self._wake_pending = False
            finally:
                self._lock.release()

    def _on_ptt_irq(self, *args):
        # ISR 只记录一次 GPIO 状态可能已变化的事实并唤醒 worker；
        # 不在 ISR 中读取 GPIO 或执行协议业务。
        self._ptt_irq_pending = True
        self._signal()

    def _queue_event(self, event):
        self._lock.acquire()
        try:
            if len(self._events) < 32:
                self._events.append(event)
        finally:
            self._lock.release()
        self._signal()

    def on_runtime_event(self, event):
        """由 CommunicationRuntime 回调投递 TCP/RTP 事件。"""
        # RTP 接收帧由测试入口/上层音频端口直接处理；PTT 状态机不需要
        # 为每个 RTP 包排队，否则音频流会与 GPIO 松开事件竞争 worker。
        if event["source"] == "rtp":
            return
        self._queue_event({"kind": "runtime", "event": event})

    def configure_group(self, group_id, next_request_count=1):
        self.group_id = int(group_id) & 0xFFFFFFFF
        self.next_request_count = int(next_request_count) & 0xFFFF
        self.state = STATE_IDLE
        self._emit("group_configured", self.group_id)

    def _make_ptt_input(self):
        if Pin is None:
            raise RuntimeError("machine.Pin不可用")
        # 硬件已确认 PTT 使用 GPIO29。
        return Pin(Pin.GPIO29, Pin.IN, Pin.PULL_PU, 1)

    def _make_ptt_irq(self):
        if ExtInt is None:
            raise RuntimeError("machine.ExtInt不可用")
        return ExtInt(
            # PTT GPIO29，使用双边沿中断，线程上下文再读取电平并消抖。
            ExtInt.GPIO29, ExtInt.IRQ_RISING_FALLING,
            ExtInt.PULL_PU, self._on_ptt_irq)

    def _wait_thread_exit(self, timeout_ms=300):
        """等待 PTT worker 退出；_thread 没有 join，这里用 thread_id 完成回收。"""
        deadline = utime.ticks_add(utime.ticks_ms(), int(timeout_ms))
        while self.thread_id is not None:
            if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
                break
            utime.sleep_ms(1)
        return self.thread_id is None

    def start(self):
        if self.running:
            return False
        # 重启/重连前，确保上一次 worker 已经离开 _run；否则同一对象
        # 可能同时存在两个线程，共用 GPIO 和信号量。
        if self.thread_id is not None:
            if not self._wait_thread_exit():
                raise RuntimeError("PTT worker未退出，不能重复启动")
        self._lock.acquire()
        try:
            self._events = []
            self._wake_pending = False
            self._ptt_irq_pending = False
        finally:
            self._lock.release()
        self.ptt_pin = self._make_ptt_input()
        self.ptt_pressed = (int(self.ptt_pin.read()) == PTT_ACTIVE_LEVEL)
        self.ptt_irq = self._make_ptt_irq()
        self.ptt_irq.enable()
        self.running = True
        self.state = STATE_IDLE if self.group_id is not None else STATE_DISCONNECTED
        try:
            self.thread_id = _thread.start_new_thread(self._run, ())
        except Exception:
            self.running = False
            self.ptt_irq.disable()
            self.ptt_irq = None
            raise
        self._emit("ptt_service_started", self.ptt_gpio)
        return True

    def stop(self):
        self.running = False
        if self.ptt_irq is not None:
            self.ptt_irq.disable()
        self._signal()
        if self.audio_port is not None:
            self.audio_port.stop_playback()
        self.state = STATE_DISCONNECTED
        self.release_pending = False
        self._wait_thread_exit()
        self.ptt_irq = None
        self.ptt_pin = None
        self._lock.acquire()
        try:
            self._events = []
            self._wake_pending = False
            self._ptt_irq_pending = False
        finally:
            self._lock.release()

    def _take_events(self):
        self._lock.acquire()
        try:
            events = self._events
            self._events = []
            self._wake_pending = False
            return events
        finally:
            self._lock.release()

    def _submit(self, command_type, payload=None):
        accepted = self.submit_command(command_type, payload)
        if not accepted:
            self._emit("command_rejected", command_type)
        return accepted

    def _next_count(self):
        value = self.next_request_count
        self.next_request_count = (value + 1) & 0xFFFF
        return value

    def _send_floor_request(self):
        if self.group_id is None or self.state != STATE_IDLE:
            return False
        count = self._next_count()
        payload = build_floor_payload(
            self.device_id, self.police_no, self.group_id)
        if not self._submit(
                CMD_REQUEST_FLOOR,
                build_frame(MSG_REQUEST_FLOOR, count, payload)):
            return False
        self.pending_request_count = count
        self.state = STATE_REQUESTING_FLOOR
        self._emit("floor_request_sent", count)
        return True

    def _send_release(self):
        if self.group_id is None:
            self.state = STATE_IDLE
            return False
        if self.state == STATE_RELEASING_FLOOR:
            return True
        count = self._next_count()
        payload = build_floor_payload(
            self.device_id, self.police_no, self.group_id)
        if not self._submit(
                CMD_RELEASE_FLOOR,
                build_frame(MSG_RELEASE_FLOOR, count, payload)):
            return False
        self.pending_release_count = count
        self.state = STATE_RELEASING_FLOOR
        self.release_pending = False
        self._emit("floor_release_sent", count)
        return True

    def _on_ptt_changed(self, pressed):
        self.ptt_pressed = bool(pressed)
        self._emit("ptt_changed", self.ptt_pressed)
        if self.ptt_pressed:
            if self.state == STATE_IDLE:
                self._send_floor_request()
            return
        self.release_pending = True
        if self.audio_port is not None:
            self.audio_port.stop_playback()
        self._submit(CMD_RTP_AUDIO_STOP)
        if self.state == STATE_TRANSMITTING:
            self._send_release()
        elif self.state == STATE_IDLE:
            self.release_pending = False
        elif self.state == STATE_REQUESTING_FLOOR:
            # 等待0x88结果；若服务器已经授予麦权，收到应答后立即释放。
            self._emit("floor_release_deferred", None)

    def _handle_tcp_frame(self, frame):
        msg_type = frame["msg_type"]
        request_count = frame["request_cnt"]
        if msg_type == MSG_FLOOR_OCCUPIED:
            occupied = parse_floor_occupied(frame["payload"])
            if (self.group_id is not None and
                    occupied["group_id"] == self.group_id):
                self.floor_udp_port = occupied["udp_port"]
                if self.floor_udp_port > 0:
                    self._submit(CMD_RTP_ACTIVATE, self.floor_udp_port)
                self._emit("floor_occupied", occupied)
            return
        if msg_type == MSG_FLOOR_IDLE:
            idle = parse_floor_idle(frame["payload"])
            if (self.group_id is not None and
                    idle["group_id"] == self.group_id):
                if self.audio_port is not None:
                    self.audio_port.stop_playback()
                self._emit("floor_idle", idle)
            return
        if msg_type == MSG_REQUEST_FLOOR_ACK:
            if self.pending_request_count is None or request_count != (
                    self.pending_request_count + 1) & 0xFFFF:
                return
            ack = parse_request_floor_ack(frame["payload"])
            self.pending_request_count = None
            self.floor_udp_port = ack["udp_port"]
            if ack["floor_status"] != 0 or self.floor_udp_port <= 0:
                self.state = STATE_IDLE
                self._emit("floor_request_failed", ack)
                return
            if not self.ptt_pressed:
                self._send_release()
                return
            self._submit(CMD_RTP_ACTIVATE, self.floor_udp_port)
            self._submit(CMD_RTP_AUDIO_START, self.floor_udp_port)
            self.state = STATE_TRANSMITTING
            self._emit("floor_granted", self.floor_udp_port)
            return
        if msg_type == MSG_RELEASE_FLOOR_ACK:
            if self.pending_release_count is None or request_count != (
                    self.pending_release_count + 1) & 0xFFFF:
                return
            ack = parse_release_floor_ack(frame["payload"])
            self.pending_release_count = None
            self.state = STATE_IDLE
            self.release_pending = False
            self._emit("floor_released", ack)

    def _handle_runtime_event(self, event):
        if event["source"] == "tcp":
            inner = event["event"]
            if inner["type"] == "tcp_frame":
                self._handle_tcp_frame(inner["payload"])
            elif inner["type"] in ("tcp_disconnected", "tcp_receive_error"):
                self.state = STATE_DISCONNECTED
                self.pending_request_count = None
                self.pending_release_count = None
                if self.audio_port is not None:
                    self.audio_port.stop_playback()
                self._submit(CMD_RTP_AUDIO_STOP)
                self._emit("communication_disconnected", inner)
        elif event["source"] == "rtp":
            self._emit("rtp_event", event["event"])

    def _run(self):
        try:
            while self.running:
                self._event_sem.acquire()
                if not self.running:
                    break
                try:
                    events = self._take_events()
                    self._lock.acquire()
                    try:
                        ptt_irq_pending = self._ptt_irq_pending
                        self._ptt_irq_pending = False
                    finally:
                        self._lock.release()
                    if ptt_irq_pending and self.ptt_pin is not None:
                        # GPIO 中断只负责唤醒；在线程中消抖并读取最终电平。
                        utime.sleep_ms(PTT_DEBOUNCE_MS)
                        current = (int(self.ptt_pin.read()) ==
                                   PTT_ACTIVE_LEVEL)
                        # ExtInt 在 start() 中启用一次即可；与旧版按键服务
                        # 保持一致，不在 worker 中重复 enable，避免部分固件
                        # 在回调期间重配边沿触发器。
                        self._emit("ptt_sample", {
                            "gpio": self.ptt_gpio,
                            "pressed": current,
                            "previous": self.ptt_pressed,
                        })
                        if current != self.ptt_pressed:
                            self._on_ptt_changed(current)
                    for item in events:
                        if item["kind"] == "runtime":
                            self._handle_runtime_event(item["event"])
                except Exception as error:
                    # 不能让一次协议/驱动异常静默杀死 PTT worker，否则后续
                    # 松开边沿将永远无人处理。
                    self._emit("ptt_worker_error", error)
        finally:
            self._lock.acquire()
            try:
                self._events = []
                self._wake_pending = False
            finally:
                self._lock.release()
            self.running = False
            self.thread_id = None
