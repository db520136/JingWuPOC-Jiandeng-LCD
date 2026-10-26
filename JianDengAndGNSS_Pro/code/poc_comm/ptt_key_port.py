# -*- coding: utf-8 -*-
try:
	from machine import ExtInt, Pin
except ImportError:
	ExtInt = None
	Pin = None

PTT_ACTIVE_LEVEL = 0
PTT_DEBOUNCE_MS = 30
PTT_PRESS = "ptt_press"
PTT_RELEASE = "ptt_release"


class PTTKeyPort:
	def __init__(self, callback, gpio=29, debounce_ms=PTT_DEBOUNCE_MS):
		self.callback = callback
		self.gpio = int(gpio)
		self.debounce_ms = max(5, int(debounce_ms))
		self.pin = None
		self.irq = None
		self.running = False
		self._pressed = False

	def start(self):
		if self.running:
			return False
		if Pin is None or ExtInt is None:
			raise RuntimeError("machine.Pin或machine.ExtInt不可用")
		pin_id = getattr(Pin, "GPIO{}".format(self.gpio))
		ext_id = getattr(ExtInt, "GPIO{}".format(self.gpio))
		self.pin = Pin(pin_id, Pin.IN, Pin.PULL_PU, 1)
		self._pressed = int(self.pin.read()) == PTT_ACTIVE_LEVEL
		self.irq = ExtInt(ext_id, ExtInt.IRQ_RISING_FALLING,
			ExtInt.PULL_PU, self._on_irq)
		self.irq.enable()
		self.running = True
		return True

	def stop(self):
		if self.irq is not None:
			try:
				self.irq.disable()
			except Exception:
				pass
		self.irq = None
		self.pin = None
		self.running = False

	def _on_irq(self, *args):
		# 中断只通知轮询线程，避免在中断上下文执行 TCP 或打印。
		return

	def poll(self):
		if not self.running or self.pin is None:
			return None
		pressed = int(self.pin.read()) == PTT_ACTIVE_LEVEL
		if pressed == self._pressed:
			return None
		self._pressed = pressed
		action = PTT_PRESS if pressed else PTT_RELEASE
		if self.callback is not None:
			self.callback(action)
		return action
