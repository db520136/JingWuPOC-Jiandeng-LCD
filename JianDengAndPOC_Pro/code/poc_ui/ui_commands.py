# -*- coding: utf-8 -*-
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
CMD_CHANGE_VOLUME = "change_volume"
CMD_SET_LAMP_MODE = "set_lamp_mode"
CMD_SET_LAMP_BRIGHTNESS = "set_lamp_brightness"
CMD_COMMIT_LAMP_BRIGHTNESS = "commit_lamp_brightness"
CMD_SET_DOUBLE_CLICK_ENABLED = "set_double_click_enabled"
CMD_SET_SCREEN_BRIGHTNESS = "set_screen_brightness"
CMD_COMMIT_SCREEN_BRIGHTNESS = "commit_screen_brightness"
CMD_SET_SLEEP_TIMEOUT = "set_sleep_timeout"  # 设置自动息屏时间，None表示禁用。
CMD_WAKE_SCREEN = "wake_screen"  # 对讲事件恢复亮屏；LCD操作仍由低优先级Worker执行。
CMD_REFRESH_NETWORK = "refresh_network"
CMD_REFRESH_GNSS = "refresh_gnss"
CMD_DOUBLE_CLICK = "double_click"
CMD_ACCEL_DOUBLE_CLICK_IRQ = "accel_double_click_irq"
class UICommandQueue:
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
		self._screen_timeout_pending = False
		self._hardware_key_signal_count = 0
		self._hardware_key_taken_count = 0
		self._lamp_key_long_pending = False
		self._ui_busy = False
		self._sequence = 0
		self._items = []
	def _release_once_isr(self):
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
	def put_latest(self, command_type, payload=None, priority=PRIORITY_P2):
		self._lock.acquire()
		try:
			for item in self._items:
				if item["type"] == command_type:
					item["payload"] = payload
					return True
			if len(self._items) >= self.max_length:
				return False
			self._items.append({"type": str(command_type), "payload": payload,
				"priority": int(priority), "sequence": self._sequence})
			self._sequence += 1
			should_wake = not self._wake_pending
			if should_wake:
				self._wake_pending = True
		finally:
			self._lock.release()
		if should_wake:
			self._wake.release()
		return True
	def signal_screen_timeout_isr(self):
		self._screen_timeout_pending = True
		self._release_once_isr()
	def signal_hardware_key_isr(self):
		self._hardware_key_signal_count += 1
		self._release_once_isr()
		return True
	def signal_lamp_key_long_isr(self):
		self._lamp_key_long_pending = True
		self._release_once_isr()
		return True
	def is_ui_busy(self):
		return self._ui_busy
	def signal_double_click_isr(self):
		self._double_click_signal_count += 1
		self._release_once_isr()
		return True
	def signal_periodic_isr(self):
		self._periodic_signal_count += 1
		if self._ui_busy:
			self._periodic_deferred = True
			return True
		self._release_once_isr()
		return True
	def signal_stop(self):
		self._stop_pending = True
		self._release_once_isr()
		return True
	def set_ui_busy(self, busy):
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
		self._lock.acquire()
		try:
			stop_pending = self._stop_pending
			screen_timeout_pending = self._screen_timeout_pending
			self._screen_timeout_pending = False
			double_click_signal_count = self._double_click_signal_count
			double_click_count = (double_click_signal_count -
								  self._double_click_taken_count)
			double_click_pending = double_click_count > 0
			periodic_signal_count = self._periodic_signal_count
			periodic_count = (periodic_signal_count -
							  self._periodic_taken_count)
			periodic_pending = periodic_count > 0 and not self._ui_busy
			periodic_deferred = self._periodic_deferred
			hardware_key_signal_count = self._hardware_key_signal_count
			hardware_key_pending = (hardware_key_signal_count !=
									self._hardware_key_taken_count)
			lamp_key_long_pending = self._lamp_key_long_pending
			self._lamp_key_long_pending = False
			self._stop_pending = False
			if double_click_pending:
				self._double_click_taken_count = double_click_signal_count
			if periodic_pending:
				self._periodic_taken_count = periodic_signal_count
				self._periodic_deferred = False
			if hardware_key_pending:
				self._hardware_key_taken_count = hardware_key_signal_count
			items = self._items
			self._items = []
			self._wake_pending = False
			items.sort(key=lambda item: (item["priority"],
										 item["sequence"]))
			result = (stop_pending, double_click_pending,
					periodic_pending, periodic_deferred,
					double_click_count, periodic_count, items,
					screen_timeout_pending, hardware_key_pending,
					lamp_key_long_pending)
			# 取批次期间可能又有 ISR/Timer 事件，以计数器为准补一次唤醒。
			follow_up = (self._stop_pending or self._screen_timeout_pending or
						 self._lamp_key_long_pending or
						 self._double_click_signal_count !=
						 self._double_click_taken_count or
						 self._hardware_key_signal_count !=
						 self._hardware_key_taken_count or
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
	def pending_count(self):
		self._lock.acquire()
		try:
			return (len(self._items) +
					(1 if self._screen_timeout_pending else 0) +
					(1 if self._lamp_key_long_pending else 0) +
					(1 if self._hardware_key_signal_count !=
					 self._hardware_key_taken_count else 0) +
					(1 if self._double_click_signal_count !=
					 self._double_click_taken_count else 0) +
					(1 if self._periodic_signal_count !=
					 self._periodic_taken_count else 0) +
					(1 if self._stop_pending else 0))
		finally:
			self._lock.release()
	def wait(self):
		self._wake.acquire()
	def wake(self):
		self._lock.acquire()
		try:
			if self._wake_pending:
				return False
			self._wake_pending = True
		finally:
			self._lock.release()
		self._wake.release()
		return True
