# -*- coding: utf-8 -*-
import _thread
from usr.comm_commands import (BOUNDED_QUEUE_COMMANDS,
							   CMD_RELEASE_FLOOR,
							   CMD_RTP_ACTIVATE,
							   CMD_RTP_AUDIO_STOP,
							   COALESCIBLE_COMMANDS,
							   NON_DROPPABLE_COMMANDS, command_priority)
class CommunicationCommandQueue:
	def __init__(self, max_length=32):
		self.max_length = max(1, int(max_length))
		self._lock = _thread.allocate_lock()
		self._wake = _thread.allocate_semphore(1)
		# QuecPython semaphore 初始计数通常为 1；清掉初始令牌，使 wait
		# 真正进入阻塞状态。若固件初始就是 0，第一次 acquire 会阻塞，
		# 因此采用 getCnt() 读取初始计数后只清除已有令牌。
		try:
			count = self._wake.getCnt()
			initial = count[1] if isinstance(count, (tuple, list)) else count
			for _ in range(int(initial or 0)):
				self._wake.acquire()
		except Exception:
			try:
				# 兼容没有 getCnt() 的固件：项目中使用的 semaphore 初始
				# 令牌为 1，此处清除一次；若本来就是 0，异常不会改变它。
				self._wake.acquire()
			except Exception:
				pass
		self._wake_pending = False
		self._stop_pending = False
		self._sequence = 0
		self._items = []
		# PTT松开必须同时保留“本地停音频”和“0x09释放麦权”。它们不占
		# 普通32项容量，因此普通队列满也不会拒绝松键事务。
		self._urgent_stop = None
		self._urgent_release = None
	def _append_item(self, command_type, payload, priority):
		item = {
			"type": command_type,
			"payload": payload,
			"priority": int(priority),
			"sequence": self._sequence,
		}
		self._sequence += 1
		return item
	def _take_urgent_locked(self):
		items = []
		if self._urgent_stop is not None:
			items.append(self._urgent_stop)
			self._urgent_stop = None
		if self._urgent_release is not None:
			items.append(self._urgent_release)
			self._urgent_release = None
		items.sort(key=lambda item: (item["priority"], item["sequence"]))
		return items
	def submit(self, command_type, payload=None, priority=None):
		if command_type not in BOUNDED_QUEUE_COMMANDS:
			return False
		if priority is None:
			priority = command_priority(command_type)
		priority = int(priority)
		should_wake = False
		accepted = False
		self._lock.acquire()
		try:
			if command_type == CMD_RTP_AUDIO_STOP:
				if self._urgent_stop is not None:
					return True
				self._urgent_stop = self._append_item(
					command_type, payload, priority)
				accepted = True
			elif command_type == CMD_RELEASE_FLOOR:
				if self._urgent_release is not None:
					# 单一麦权状态机同一时刻只允许一个释放事务。完全相同
					# 的重复投递视为已保留，不允许新事务覆盖旧释放帧。
					return self._urgent_release["payload"] == payload
				self._urgent_release = self._append_item(
					command_type, payload, priority)
				accepted = True
			if command_type in COALESCIBLE_COMMANDS:
				for item in self._items:
					if item["type"] == command_type:
						# 不同UDP目标的激活包是不同业务，不可合并掉其中一个。
						if command_type == CMD_RTP_ACTIVATE and item["payload"] != payload:
							continue
						return True
			if not accepted and len(self._items) >= self.max_length:
				if command_type in NON_DROPPABLE_COMMANDS:
					return False
				lowest_index = 0
				lowest_key = (self._items[0]["priority"],
							  self._items[0]["sequence"])
				for index in range(1, len(self._items)):
					item = self._items[index]
					key = (item["priority"], item["sequence"])
					if key > lowest_key:
						lowest_index = index
						lowest_key = key
				if lowest_key[0] < priority:
					return False
				self._items.pop(lowest_index)
			if not accepted:
				self._items.append(self._append_item(
					command_type, payload, priority))
				accepted = True
			if not self._wake_pending:
				self._wake_pending = True
				should_wake = True
		finally:
			self._lock.release()
		if should_wake:
			try:
				self._wake.release()
			except Exception:
				self._lock.acquire()
				try:
					self._wake_pending = False
				finally:
					self._lock.release()
				return False
		return accepted
	def signal_stop(self):
		should_wake = False
		self._lock.acquire()
		try:
			self._stop_pending = True
			if not self._wake_pending:
				self._wake_pending = True
				should_wake = True
		finally:
			self._lock.release()
		if should_wake:
			self._wake.release()
		return True
	def begin_logout(self):
		should_wake = False
		self._lock.acquire()
		try:
			self._items = []
			self._urgent_release = None
			if self._urgent_stop is None:
				self._urgent_stop = self._append_item(
					CMD_RTP_AUDIO_STOP, None, command_priority(CMD_RTP_AUDIO_STOP))
			if not self._wake_pending:
				self._wake_pending = True
				should_wake = True
		finally:
			self._lock.release()
		if should_wake:
			self._wake.release()
		return True
	def wait(self):
		self._wake.acquire()
	def take_batch(self):
		self._lock.acquire()
		try:
			stop_pending = self._stop_pending
			self._stop_pending = False
			items = self._take_urgent_locked() + self._items
			self._items = []
			self._wake_pending = False
			items.sort(key=lambda item: (item["priority"],
										 item["sequence"]))
			follow_up = bool(self._stop_pending or self._items)
			if follow_up:
				self._wake_pending = True
		finally:
			self._lock.release()
		if follow_up:
			self._wake.release()
		return stop_pending, items
	def take_urgent_batch(self):
		self._lock.acquire()
		try:
			return self._take_urgent_locked()
		finally:
			self._lock.release()
	def take_batch_nowait(self):
		self._lock.acquire()
		try:
			pending = self._wake_pending
		finally:
			self._lock.release()
		if not pending:
			return False, []
		self._wake.acquire()
		return self.take_batch()
	def pending_count(self):
		self._lock.acquire()
		try:
			return (len(self._items) +
					(1 if self._urgent_stop is not None else 0) +
					(1 if self._urgent_release is not None else 0) +
					(1 if self._stop_pending else 0))
		finally:
			self._lock.release()
