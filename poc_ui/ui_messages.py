# -*- coding: utf-8 -*-
import _thread
MSG_NETWORK_CHANGED = "network_changed"
MSG_BATTERY_CHANGED = "battery_changed"
MSG_LOGIN_CHANGED = "login_changed"
MSG_GROUP_CHANGED = "group_changed"
MSG_SINGLE_CALL = "single_call"
MSG_FLOOR_CHANGED = "floor_changed"
MSG_LAMP_CHANGED = "lamp_changed"
MSG_DOUBLE_CLICK = "double_click"
MSG_ERROR = "error"
class UIMessageBus:
	def __init__(self, max_length=32):
		self.max_length = max(1, int(max_length))
		self._lock = _thread.allocate_lock()
		self._sequence = 0
		self._items = []
	def publish(self, message_type, payload=None, priority=2):
		item = {
			"type": str(message_type),
			"payload": payload,
			"priority": int(priority),
			"sequence": self._sequence,
		}
		self._lock.acquire()
		try:
			self._sequence += 1
			if len(self._items) >= self.max_length:
				# UI 事件优先保留最新状态，丢弃最旧的一条。
				self._items.pop(0)
			self._items.append(item)
			return True
		finally:
			self._lock.release()
	def drain(self, max_items=0):
		self._lock.acquire()
		try:
			if max_items is None or int(max_items) <= 0:
				items = self._items
				self._items = []
			else:
				count = min(len(self._items), int(max_items))
				items = self._items[:count]
				self._items = self._items[count:]
			return items
		finally:
			self._lock.release()
	def pending_count(self):
		self._lock.acquire()
		try:
			return len(self._items)
		finally:
			self._lock.release()
