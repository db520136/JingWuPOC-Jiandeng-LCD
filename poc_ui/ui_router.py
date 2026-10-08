# -*- coding: utf-8 -*-
"""二级/三级页面路由器。"""


class Router:
	def __init__(self, host):
		self.host = host
		self.stack = []
		self.current = None
		self.busy = False

	def push(self, page_name, page_factory=None, params=None):
		if self.busy:
			return False
		self.busy = True
		try:
			if self.current is not None:
				self.stack.append(self.current)
			elif not self.stack:
				# 当前页面属于一级横向容器；用根哨兵保留返回目标，
				# 一级容器本身不纳入普通子页面栈。
				self.stack.append(("__root__", None, {}))
			page = page_factory(page_name, params) if page_factory else None
			self.current = (str(page_name), page, params or {})
			self.host.show_child_page(self.current, direction="forward")
			return True
		finally:
			self.busy = False

	def pop(self):
		if self.busy or not self.stack:
			return False
		self.busy = True
		try:
			leaving = self.current
			if leaving is not None and hasattr(self.host, "before_leave_page"):
				self.host.before_leave_page(leaving[0])
			self.current = self.stack.pop()
			if self.current[0] == "__root__":
				self.current = None
				self.host.show_root_page()
			else:
				self.host.show_child_page(self.current, direction="back")
			return True
		finally:
			self.busy = False

	def reset(self):
		if self.current is not None and hasattr(self.host, "before_leave_page"):
			self.host.before_leave_page(self.current[0])
		self.stack = []
		self.current = None
