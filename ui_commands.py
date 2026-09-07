# -*- coding: utf-8 -*-
"""UI -> 业务的非阻塞命令接口。

UI 只向本模块投递命令，不直接调用 socket、HTTP、I2C、RTP 或 GNSS。
业务 Worker 通过 ``wait_and_drain()`` 被事件唤醒并取走命令。
"""

import _thread


PRIORITY_P0 = 0
PRIORITY_P1 = 1
PRIORITY_P2 = 2
PRIORITY_P3 = 3

CMD_LOGIN = "login"
CMD_LOGOUT = "logout"
CMD_JOIN_GROUP = "join_group"
CMD_LEAVE_GROUP = "leave_group"
CMD_REQUEST_FLOOR = "request_floor"
CMD_RELEASE_FLOOR = "release_floor"
CMD_SINGLE_CALL = "single_call"
CMD_ACCEPT_SINGLE_CALL = "accept_single_call"
CMD_REJECT_SINGLE_CALL = "reject_single_call"
CMD_SET_VOLUME = "set_volume"
CMD_CHANGE_VOLUME = "change_volume"
CMD_SET_LAMP_MODE = "set_lamp_mode"
CMD_SET_LAMP_BRIGHTNESS = "set_lamp_brightness"
CMD_REFRESH_NETWORK = "refresh_network"
CMD_REFRESH_GNSS = "refresh_gnss"
CMD_DOUBLE_CLICK = "double_click"
CMD_ACCEL_DOUBLE_CLICK_IRQ = "accel_double_click_irq"


class UICommandQueue:
    """有界命令队列；多次唤醒请求合并为一次信号量 release。"""

    def __init__(self, max_length=24):
        self.max_length = max(1, int(max_length))
        self._lock = _thread.allocate_lock()
        self._wake = _thread.allocate_semphore(1)
        self._wake.acquire()
        self._wake_pending = False
        self._isr_event_pending = False
        self._isr_event = None
        self._sequence = 0
        self._items = []

    def put(self, command_type, payload=None, priority=PRIORITY_P2):
        item = {
            "type": str(command_type),
            "payload": payload,
            "priority": int(priority),
            "sequence": self._sequence,
        }
        self._lock.acquire()
        try:
            self._sequence += 1
            if len(self._items) >= self.max_length:
                return False
            self._items.append(item)
            should_wake = not self._wake_pending
            if should_wake:
                self._wake_pending = True
        finally:
            self._lock.release()
        if should_wake:
            self._wake.release()
        return True

    def put_coalesced(self, command_type, payload=None, priority=PRIORITY_P2):
        """投递可合并事件；队列中已有同类事件时不重复唤醒。"""
        self._lock.acquire()
        try:
            for item in self._items:
                if item["type"] == command_type:
                    return True
            if len(self._items) >= self.max_length:
                return False
            item = {"type": str(command_type), "payload": payload,
                    "priority": int(priority), "sequence": self._sequence}
            self._sequence += 1
            self._items.append(item)
            should_wake = not self._wake_pending
            if should_wake:
                self._wake_pending = True
        finally:
            self._lock.release()
        if should_wake:
            self._wake.release()
        return True

    def signal_isr(self, command_type, payload=None, priority=PRIORITY_P2):
        """ISR 安全的单类事件通知；只写标志并最多 release 一次。

        中断上下文不获取 Python 锁、不创建对象、不访问硬件。Worker 被
        唤醒后在普通线程上下文把该事件合并进有界队列。
        """
        if self._isr_event_pending:
            return True
        self._isr_event = (str(command_type), payload, int(priority))
        self._isr_event_pending = True
        if not self._wake_pending:
            self._wake_pending = True
            self._wake.release()
        return True

    def _merge_isr_event_locked(self):
        if not self._isr_event_pending:
            return
        if len(self._items) < self.max_length:
            command_type, payload, priority = self._isr_event
            self._items.append({
                "type": command_type,
                "payload": payload,
                "priority": priority,
                "sequence": self._sequence,
            })
            self._sequence += 1
        self._isr_event = None
        self._isr_event_pending = False

    def wait_and_drain(self, max_items=0):
        """阻塞等待命令并一次性取出当前批次。"""
        self._wake.acquire()
        self._lock.acquire()
        try:
            if max_items is None or int(max_items) <= 0:
                items = self._items
                self._items = []
            else:
                count = min(len(self._items), int(max_items))
                items = self._items[:count]
                self._items = self._items[count:]
            self._wake_pending = False
            items.sort(key=lambda item: (item["priority"],
                                         item["sequence"]))
            return items
        finally:
            self._lock.release()

    def drain_nowait(self, max_items=0):
        self._lock.acquire()
        try:
            self._merge_isr_event_locked()
            if max_items is None or int(max_items) <= 0:
                items = self._items
                self._items = []
            else:
                count = min(len(self._items), int(max_items))
                items = self._items[:count]
                self._items = self._items[count:]
            if not self._items:
                self._wake_pending = False
            items.sort(key=lambda item: (item["priority"],
                                         item["sequence"]))
            return items
        finally:
            self._lock.release()

    def pending_count(self):
        self._lock.acquire()
        try:
            return len(self._items)
        finally:
            self._lock.release()

    def wait(self):
        """仅等待一次唤醒；命令内容由 ``drain_nowait`` 读取。"""
        self._wake.acquire()
        self._lock.acquire()
        try:
            self._merge_isr_event_locked()
            self._wake_pending = False
        finally:
            self._lock.release()

    def wake(self):
        """唤醒等待 Worker；不添加命令，重复唤醒会合并。"""
        self._lock.acquire()
        try:
            if self._wake_pending:
                return False
            self._wake_pending = True
        finally:
            self._lock.release()
        self._wake.release()
        return True


class UICommandClient:
    """供页面调用的显式命令门面。所有方法均不执行实际业务。"""

    def __init__(self, queue):
        self.queue = queue

    def login(self, police_no):
        return self.queue.put(CMD_LOGIN, str(police_no), PRIORITY_P1)

    def logout(self):
        return self.queue.put(CMD_LOGOUT, None, PRIORITY_P1)

    def join_group(self, group_id):
        return self.queue.put(CMD_JOIN_GROUP, group_id, PRIORITY_P1)

    def leave_group(self):
        return self.queue.put(CMD_LEAVE_GROUP, None, PRIORITY_P1)

    def request_floor(self, call_type="group"):
        return self.queue.put(CMD_REQUEST_FLOOR, str(call_type), PRIORITY_P0)

    def release_floor(self):
        return self.queue.put(CMD_RELEASE_FLOOR, None, PRIORITY_P0)

    def single_call(self, person_key, target_device_id):
        return self.queue.put(
            CMD_SINGLE_CALL, (person_key, target_device_id), PRIORITY_P1)

    def accept_single_call(self):
        return self.queue.put(CMD_ACCEPT_SINGLE_CALL, None, PRIORITY_P1)

    def reject_single_call(self):
        return self.queue.put(CMD_REJECT_SINGLE_CALL, None, PRIORITY_P1)

    def set_volume(self, level):
        return self.queue.put(CMD_SET_VOLUME, int(level), PRIORITY_P1)

    def change_volume(self, delta):
        return self.queue.put(CMD_CHANGE_VOLUME, int(delta), PRIORITY_P1)

    def set_lamp_mode(self, mode):
        return self.queue.put(CMD_SET_LAMP_MODE, str(mode), PRIORITY_P2)

    def set_lamp_brightness(self, percent):
        return self.queue.put(
            CMD_SET_LAMP_BRIGHTNESS, int(percent), PRIORITY_P2)

    def refresh_network(self):
        return self.queue.put(CMD_REFRESH_NETWORK, None, PRIORITY_P2)

    def refresh_gnss(self):
        return self.queue.put(CMD_REFRESH_GNSS, None, PRIORITY_P2)

    def double_click(self):
        """供硬件事件适配器使用；UI 页面通常不调用此方法。"""
        return self.queue.put(CMD_DOUBLE_CLICK, None, PRIORITY_P2)
