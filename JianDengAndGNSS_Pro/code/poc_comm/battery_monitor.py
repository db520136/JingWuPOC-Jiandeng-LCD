# -*- coding: utf-8 -*-
import utime

try:
	from misc import ADC
except Exception:
	ADC = None


# Battery voltage is measured through R52/R53 on the ADC input.
DIVIDER_TOP_KOHM = 220
DIVIDER_BOTTOM_KOHM = 82
SAMPLE_INTERVAL_MS = 1000


def _ticks_add(value, delta):
	try:
		return utime.ticks_add(value, delta)
	except Exception:
		return int(value) + int(delta)


def _ticks_diff(first, second):
	try:
		return utime.ticks_diff(first, second)
	except Exception:
		return int(first) - int(second)


class BatteryMonitor:
	"""Minimal ADC voltage sampler kept compatible with the old worker API."""

	def __init__(self, adc=None, channel=None,
				 divider_top_kohm=DIVIDER_TOP_KOHM,
				 divider_bottom_kohm=DIVIDER_BOTTOM_KOHM,
				 nchrg_gpio=14, nstdby_gpio=13, persistent_config=None):
		# Charging pins and persistent_config remain accepted for compatibility,
		# but are intentionally unused until the battery policy is defined.
		if divider_top_kohm <= 0 or divider_bottom_kohm <= 0:
			raise ValueError("分压电阻必须大于 0")
		if adc is None:
			if ADC is None:
				raise RuntimeError("QuecPython ADC 库不可用")
			adc = ADC()
		self.adc = adc
		if channel is not None:
			self.channel = channel
		elif ADC is not None:
			self.channel = ADC.ADC0
		else:
			self.channel = 0
		self.divider_top_kohm = int(divider_top_kohm)
		self.divider_bottom_kohm = int(divider_bottom_kohm)
		self.divider_total_kohm = (
			self.divider_top_kohm + self.divider_bottom_kohm)
		self._adc_open = False
		self._next_sample_ms = utime.ticks_ms()
		self.raw_adc_mv = None
		self.raw_voltage_mv = None
		self.filtered_voltage_mv = None
		self.percent = None
		self.estimated_percent = None
		self.charging_state = None
		self.last_error = None
		self._ready = False
		self._open_adc()

	def _open_adc(self):
		opener = getattr(self.adc, "open", None)
		if opener is not None and not self._adc_open:
			result = opener()
			if result not in (None, 0):
				raise OSError("ADC打开失败，返回值：{}".format(result))
		self._adc_open = True

	def close(self):
		closer = getattr(self.adc, "close", None)
		if closer is not None and self._adc_open:
			closer()
		self._adc_open = False

	def _read_adc_mv(self):
		if not self._adc_open:
			self._open_adc()
		value = self.adc.read(self.channel)
		if isinstance(value, (tuple, list)):
			if not value:
				raise ValueError("ADC返回空数据")
			value = value[0]
		value = int(value)
		if value < 0:
			raise ValueError("ADC返回负电压")
		return value

	def adc_to_battery_mv(self, adc_mv):
		adc_mv = int(adc_mv)
		if adc_mv < 0:
			raise ValueError("ADC电压不能为负数")
		return ((adc_mv * self.divider_total_kohm) +
				self.divider_bottom_kohm // 2) // self.divider_bottom_kohm

	def tick(self, now_ms=None):
		if now_ms is None:
			now_ms = utime.ticks_ms()
		if _ticks_diff(now_ms, self._next_sample_ms) < 0:
			return None
		self._next_sample_ms = _ticks_add(now_ms, SAMPLE_INTERVAL_MS)
		try:
			adc_mv = self._read_adc_mv()
			battery_mv = self.adc_to_battery_mv(adc_mv)
			self.raw_adc_mv = adc_mv
			self.raw_voltage_mv = battery_mv
			self.filtered_voltage_mv = battery_mv
			self.last_error = None
			self._ready = True
			print("[电池] ADC={}mV 电池电压={}mV".format(adc_mv, battery_mv))
			return battery_mv
		except Exception as error:
			self.last_error = error
			print("[电池] ADC采样失败：{}".format(error))
			return None

	def get_voltage_mv(self):
		return self.filtered_voltage_mv

	def get_percent(self):
		return self.percent

	def get_estimated_percent(self):
		return self.estimated_percent

	def is_ready(self):
		return self._ready

	def is_full_confirmed(self):
		return False

	def set_dynamic_load(self, active):
		# Retained as a no-op for compatibility with the old worker.
		return None

	def set_charging_state(self, state):
		self.charging_state = None if state is None else bool(state)
		return self.percent
