# -*- coding: utf-8 -*-
try:
	from machine import ExtInt, Pin
except ImportError:
	ExtInt = None
	Pin = None
KEY_VOLUME_UP = "volume_up"
KEY_VOLUME_DOWN = "volume_down"
KEY_AUX_1 = "aux_1"
KEY_AUX_2 = "aux_2"
KEY_PTT_PRESS = "ptt_press"
KEY_PTT_RELEASE = "ptt_release"
KEY_SCREEN_PRESS = "screen_press"
KEY_SCREEN_RELEASE = "screen_release"
KEY_LAMP_PRESS = "lamp_press"
KEY_LAMP_RELEASE = "lamp_release"
FUNCTION_KEY_ACTIONS = (
	KEY_VOLUME_UP, KEY_VOLUME_DOWN, KEY_AUX_1, KEY_AUX_2,
	KEY_PTT_PRESS, KEY_PTT_RELEASE)
KEY_ACTIVE_LEVEL = 0
KEY_DEBOUNCE_MS = 30
class HardwareKeyPort:
	def __init__(self, notify_callback, volume_up_gpio=30,
				 volume_down_gpio=31, ptt_gpio=29, screen_gpio=5,
				 lamp_gpio=28,
				 debounce_ms=KEY_DEBOUNCE_MS):
		self.notify_callback = notify_callback
		self.volume_up_gpio = int(volume_up_gpio)
		self.volume_down_gpio = int(volume_down_gpio)
		self.ptt_gpio = int(ptt_gpio)
		self.screen_gpio = int(screen_gpio)
		self.lamp_gpio = int(lamp_gpio)
		self.debounce_ms = max(5, int(debounce_ms))
		self.volume_up_pin = None
		self.volume_down_pin = None
		self.ptt_pin = None
		self.screen_pin = None
		self.lamp_pin = None
		self._volume_up_pressed = False
		self._volume_down_pressed = False
		self._ptt_pressed = False
		self._screen_pressed = False
		self._lamp_pressed = False
		self._irqs = []
		self.running = False
	def _pin_id(self, gpio):
		if gpio == 5:
			return Pin.GPIO5
		if gpio == 29:
			return Pin.GPIO29
		if gpio == 28:
			return Pin.GPIO28
		if gpio == 30:
			return Pin.GPIO30
		if gpio == 31:
			return Pin.GPIO31
		raise ValueError("当前固件按键GPIO未建档：{}".format(gpio))
	def _extint_id(self, gpio):
		if gpio == 5:
			return ExtInt.GPIO5
		if gpio == 29:
			return ExtInt.GPIO29
		if gpio == 28:
			return ExtInt.GPIO28
		if gpio == 30:
			return ExtInt.GPIO30
		if gpio == 31:
			return ExtInt.GPIO31
		raise ValueError("当前固件按键中断GPIO未建档：{}".format(gpio))
	def _make_input(self, gpio):
		return Pin(self._pin_id(gpio), Pin.IN, Pin.PULL_PU, 1)
	def _on_irq(self, *args):
		# 中断上下文不读GPIO、不消抖、不打印、不执行业务。
		self.notify_callback()
	def _make_irq(self, gpio):
		irq = ExtInt(self._extint_id(gpio), ExtInt.IRQ_RISING_FALLING,
					 ExtInt.PULL_PU, self._on_irq)
		irq.enable()
		return irq
	def start(self):
		if self.running:
			return False
		if Pin is None or ExtInt is None:
			raise RuntimeError("machine.Pin或machine.ExtInt不可用")
		try:
			self.volume_up_pin = self._make_input(self.volume_up_gpio)
			self.volume_down_pin = self._make_input(self.volume_down_gpio)
			self.ptt_pin = self._make_input(self.ptt_gpio)
			self.screen_pin = self._make_input(self.screen_gpio)
			try:
				self.lamp_pin = self._make_input(self.lamp_gpio)
			except Exception as error:
				self.lamp_pin = None
				print("[按键] GPIO{}肩灯键初始化失败，已单独禁用：{}".format(
					self.lamp_gpio, error))
			self._volume_up_pressed = (
				int(self.volume_up_pin.read()) == KEY_ACTIVE_LEVEL)
			self._volume_down_pressed = (
				int(self.volume_down_pin.read()) == KEY_ACTIVE_LEVEL)
			self._ptt_pressed = (
				int(self.ptt_pin.read()) == KEY_ACTIVE_LEVEL)
			self._screen_pressed = (
				int(self.screen_pin.read()) == KEY_ACTIVE_LEVEL)
			self._lamp_pressed = (self.lamp_pin is not None and
				int(self.lamp_pin.read()) == KEY_ACTIVE_LEVEL)
			self._irqs = []
			self._irqs.append(self._make_irq(self.volume_up_gpio))
			self._irqs.append(self._make_irq(self.volume_down_gpio))
			self._irqs.append(self._make_irq(self.ptt_gpio))
			self._irqs.append(self._make_irq(self.screen_gpio))
			if self.lamp_pin is not None:
				try:
					self._irqs.append(self._make_irq(self.lamp_gpio))
				except Exception as error:
					self.lamp_pin = None
					self._lamp_pressed = False
					print("[按键] GPIO{}肩灯键中断初始化失败，已单独禁用：{}".format(
						self.lamp_gpio, error))
			self.running = True
			return True
		except Exception:
			self.stop()
			raise
	def stop(self):
		for irq in self._irqs:
			try:
				irq.disable()
			except Exception:
				pass
		self._irqs = []
		self.volume_up_pin = None
		self.volume_down_pin = None
		self.ptt_pin = None
		self.screen_pin = None
		self.lamp_pin = None
		self.running = False
	def poll_pressed_edges(self):
		if not self.running:
			return ()
		up_pressed = int(self.volume_up_pin.read()) == KEY_ACTIVE_LEVEL
		down_pressed = int(self.volume_down_pin.read()) == KEY_ACTIVE_LEVEL
		ptt_pressed = self._ptt_pressed
		if self.ptt_pin is not None:
			ptt_pressed = int(self.ptt_pin.read()) == KEY_ACTIVE_LEVEL
		screen_pressed = self._screen_pressed
		if self.screen_pin is not None:
			screen_pressed = int(self.screen_pin.read()) == KEY_ACTIVE_LEVEL
		lamp_pressed = self._lamp_pressed
		if self.lamp_pin is not None:
			lamp_pressed = int(self.lamp_pin.read()) == KEY_ACTIVE_LEVEL
		actions = []
		if up_pressed and not self._volume_up_pressed:
			actions.append(KEY_VOLUME_UP)
		if down_pressed and not self._volume_down_pressed:
			actions.append(KEY_VOLUME_DOWN)
		if ptt_pressed != self._ptt_pressed:
			actions.append(KEY_PTT_PRESS if ptt_pressed else KEY_PTT_RELEASE)
		if screen_pressed != self._screen_pressed:
			actions.append(
				KEY_SCREEN_PRESS if screen_pressed else KEY_SCREEN_RELEASE)
		if lamp_pressed != self._lamp_pressed:
			actions.append(
				KEY_LAMP_PRESS if lamp_pressed else KEY_LAMP_RELEASE)
		self._volume_up_pressed = up_pressed
		self._volume_down_pressed = down_pressed
		self._ptt_pressed = ptt_pressed
		self._screen_pressed = screen_pressed
		self._lamp_pressed = lamp_pressed
		return tuple(actions)
	def lamp_pressed(self):
		return bool(self.lamp_pin is not None and
			int(self.lamp_pin.read()) == KEY_ACTIVE_LEVEL)
