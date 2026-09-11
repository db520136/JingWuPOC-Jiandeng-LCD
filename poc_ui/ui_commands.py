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
	"""有界命令队列和独立事件槽；所有事件共用一次唤醒闸门。"""

	def __init__(self, max_length=24):
		self.max_length = max(1, int(max_length))
		self._lock = _thread.allocate_lock()
		self._wake = _thread.allocate_semphore(1)
		self._wake.acquire()
		self._wake_pending = False
		self._double_click_signal_count = 0
		self._double_click_taken_count = 0
		self._periodic_signal_count = 0
		self._periodic_taken_count = 0
		self._periodic_deferred = False
		self._stop_pending = False
		self._ui_busy = False
		self._sequence = 0
		self._items = []

	def _release_once_isr(self):
		"""中断/Timer回调使用；不分配对象，最多释放一次信号量。"""
		if self._wake_pending:
			return False
		self._wake_pending = True
		self._wake.release()
		return True

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
		"""兼容旧调用；当前ISR只允许投递加速度双击事件。"""
		if command_type != CMD_ACCEL_DOUBLE_CLICK_IRQ:
			return False
		return self.signal_double_click_isr()

	def signal_double_click_isr(self):
		"""GPIO中断投递双击；同类事件未处理前自动合并。"""
		self._double_click_signal_count += 1
		self._release_once_isr()
		return True

	def signal_periodic_isr(self):
		"""5秒Timer投递周期任务；UI忙时保留事件但不唤醒Worker。"""
		self._periodic_signal_count += 1
		if self._ui_busy:
			self._periodic_deferred = True
			return True
		self._release_once_isr()
		return True

	def signal_stop(self):
		"""保留独立停止事件，并确保阻塞中的Worker能够退出。"""
		self._stop_pending = True
		self._release_once_isr()
		return True

	def set_ui_busy(self, busy):
		"""更新UI忙闲状态；转为空闲时只在有延后任务时唤醒。"""
		busy = bool(busy)
		should_wake = False
		self._lock.acquire()
		try:
			self._ui_busy = busy
			if (not busy and
					self._periodic_signal_count != self._periodic_taken_count and
					not self._wake_pending):
				self._wake_pending = True
				should_wake = True
		finally:
			self._lock.release()
		if should_wake:
			self._wake.release()
		return should_wake

	def take_events(self):
		"""Worker醒来后原子取得不同事件和普通有界队列。"""
		self._lock.acquire()
		try:
			stop_pending = self._stop_pending
			double_click_signal_count = self._double_click_signal_count
			double_click_count = (double_click_signal_count -
								  self._double_click_taken_count)
			double_click_pending = double_click_count > 0
			periodic_signal_count = self._periodic_signal_count
			periodic_count = (periodic_signal_count -
							  self._periodic_taken_count)
			periodic_pending = periodic_count > 0 and not self._ui_busy
			periodic_deferred = self._periodic_deferred
			self._stop_pending = False
			if double_click_pending:
				self._double_click_taken_count = double_click_signal_count
			if periodic_pending:
				self._periodic_taken_count = periodic_signal_count
				self._periodic_deferred = False
			items = self._items
			self._items = []
			self._wake_pending = False
			items.sort(key=lambda item: (item["priority"],
										 item["sequence"]))
			result = (stop_pending, double_click_pending,
					periodic_pending, periodic_deferred,
					double_click_count, periodic_count, items)
			# 取批次期间可能又有 ISR/Timer 事件，以计数器为准补一次唤醒。
			follow_up = (self._stop_pending or
						 self._double_click_signal_count !=
						 self._double_click_taken_count or
						 (self._periodic_signal_count !=
						  self._periodic_taken_count and not self._ui_busy) or
						 bool(self._items))
			if follow_up:
				self._wake_pending = True
		finally:
			self._lock.release()
		if follow_up:
			self._wake.release()
		return result

	def wait_and_drain(self, max_items=0):
		"""阻塞等待命令并一次性取出当前批次。"""
		self._wake.acquire()
		events = self.take_events()
		return events[6]

	def drain_nowait(self, max_items=0):
		self._lock.acquire()
		try:
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
			return (len(self._items) +
					(1 if self._double_click_signal_count !=
					 self._double_click_taken_count else 0) +
					(1 if self._periodic_signal_count !=
					 self._periodic_taken_count else 0) +
					(1 if self._stop_pending else 0))
		finally:
			self._lock.release()

	def wait(self):
		"""仅阻塞等待一次统一唤醒。"""
		self._wake.acquire()

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
