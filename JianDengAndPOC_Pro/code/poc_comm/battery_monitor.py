# -*- coding: utf-8 -*-
import utime
try:
	from misc import ADC
except Exception:
	ADC = None
try:
	from machine import Pin
except Exception:
	Pin = None
# 分压电阻参数，单位为 kΩ；电池端到 ADC 节点为 R52，节点到地为 R53。
DIVIDER_TOP_KOHM = 220
DIVIDER_BOTTOM_KOHM = 82
BOOT_SAMPLE_COUNT = 9
BOOT_SAMPLE_INTERVAL_MS = 20
RUN_SAMPLE_INTERVAL_MS = 1000
RUN_MEDIAN_WINDOW = 5
FULL_CONFIRM_SAMPLES = 3
CV_CONFIRM_SAMPLES = 3
CV_ENTER_MV = 4340
CV_STABLE_SPREAD_MV = 10
CV_STEP_SECONDS = 60
UNPLUG_HOLD_MS = 120000
UNPLUG_RECONCILE_TOTAL_MS = 1800000
UNPLUG_RECONCILE_MIN_STEP_MS = 60000
UNPLUG_RECONCILE_MAX_STEP_MS = 360000
DISCHARGE_STEP_MS = 120000
DISCHARGE_STABLE_SAMPLES = 3
DISCHARGE_STABLE_SPREAD_MV = 15
HIGH_BATTERY_RESTORE_CUTOFF_MV = 4074
LOW_VOLTAGE_CONFIRM_SAMPLES = 3
CV_EXIT_MV = 4325
CV_EXIT_CONFIRM_SAMPLES = 5
# EMA：新值占 1/4，旧值占 3/4，抑制 LED、PTT 等负载瞬态造成的电压跳变。
EMA_NEW_WEIGHT = 1
EMA_TOTAL_WEIGHT = 4
# 分段放电曲线，电压单位 mV，电量单位 %；电压必须按升序排列。
# 该表依据用户提供的 CC 放电曲线估算，最终应使用整机实测数据校准。
DISCHARGE_POINTS = (
	(3200, 1), (3396, 10), (3483, 20), (3551, 30),
	(3610, 40), (3667, 50), (3746, 60), (3844, 70),
	(3954, 80), (4025, 86), (4074, 90), (4136, 95),
	(4208, 99),
)
# 充电态曲线。4344mV为89%，4340mV以上连续稳定后进入恒压计时。
CHARGE_POINTS = (
	(3200, 1), (3655, 10), (3781, 20), (3910, 30),
	(3960, 40), (3999, 50), (4049, 60), (4128, 70),
	(4237, 80), (4299, 85), (4312, 86), (4324, 87),
	(4337, 88), (4344, 89),
)
VOLTAGE_EMPTY_MV = 3200
def _ticks_add(base_ms, delta_ms):
	try:
		return utime.ticks_add(base_ms, delta_ms)
	except Exception:
		return base_ms + delta_ms
def _ticks_diff(later_ms, earlier_ms):
	try:
		return utime.ticks_diff(later_ms, earlier_ms)
	except Exception:
		return later_ms - earlier_ms
def _interpolate(points, voltage_mv):
	if voltage_mv <= points[0][0]:
		return points[0][1]
	if voltage_mv >= points[-1][0]:
		return points[-1][1]
	for index in range(1, len(points)):
		upper_voltage, upper_percent = points[index]
		if voltage_mv <= upper_voltage:
			lower_voltage, lower_percent = points[index - 1]
			voltage_span = upper_voltage - lower_voltage
			percent_span = upper_percent - lower_percent
			offset = voltage_mv - lower_voltage
			value = (lower_percent * voltage_span +
					 offset * percent_span + voltage_span // 2)
			value //= voltage_span
			return max(0, min(100, int(value)))
	return 100
class BatteryMonitor:
	def __init__(self, adc=None, channel=None,
				 divider_top_kohm=DIVIDER_TOP_KOHM,
				 divider_bottom_kohm=DIVIDER_BOTTOM_KOHM,
				 nchrg_gpio=14, nstdby_gpio=13, persistent_config=None):
		self.adc = adc
		if self.adc is None:
			if ADC is None:
				raise RuntimeError("QuecPython ADC 库不可用")
			self.adc = ADC()
		if divider_top_kohm <= 0 or divider_bottom_kohm <= 0:
			raise ValueError("分压电阻必须大于 0")
		self.divider_top_kohm = int(divider_top_kohm)
		self.divider_bottom_kohm = int(divider_bottom_kohm)
		self.divider_total_kohm = (
			self.divider_top_kohm + self.divider_bottom_kohm)
		if channel is not None:
			self.channel = channel
		elif ADC is not None:
			self.channel = ADC.ADC0
		else:
			self.channel = 0
		self._adc_open = False
		self._booting = True
		self._boot_samples = []
		self._run_samples = []
		self._next_sample_ms = utime.ticks_ms()
		self._last_sample_interval_ms = BOOT_SAMPLE_INTERVAL_MS
		self.raw_adc_mv = None
		self.raw_voltage_mv = None
		self.filtered_voltage_mv = None
		self.percent = None
		self.estimated_percent = None
		self.charging_state = None
		self.last_error = None
		self._last_reported_percent = None
		self._nchrg_pin = self._make_status_pin(nchrg_gpio)
		self._nstdby_pin = self._make_status_pin(nstdby_gpio)
		self._charge_status_available = (self._nchrg_pin is not None and
			self._nstdby_pin is not None)
		self._full_low_count = 0
		self._full_signal = False
		self._cv_samples = []
		self._cv_active = False
		self._cv_started_ms = None
		self._cv_elapsed_ms = 0
		self._cv_anchor_percent = 90
		self._cv_last_ms = None
		self._cv_exit_count = 0
		self._unplug_started_ms = None
		self._last_drop_ms = None
		self._charge_anchor_percent = None
		self._discharge_samples = []
		self._low_voltage_count = 0
		self._dynamic_load = False
		self._last_status_bits = None
		self.persistent_config = persistent_config
		self._full_confirmed = bool(persistent_config and
			persistent_config.get("battery_full_confirmed", False))
		self._full_signal = self._full_confirmed
		self._auto_recharge = False
		self._timed_100 = False
		self._reconcile_target = None
		self._reconcile_step_ms = None
		self._reconcile_complete = False
		self._last_drop_ms = None
		self._restored_high = False
		self._restore_check_samples = []
		if persistent_config is not None:
			saved_percent = persistent_config.get("battery_percent")
			if (persistent_config.get("battery_high_active", False) and
				saved_percent is not None and int(saved_percent) >= 90):
				self.percent = int(saved_percent)
				self._charge_anchor_percent = self.percent
				self._restored_high = True
				self._timed_100 = (self.percent == 100 and
					not self._full_confirmed)
				if 90 <= self.percent < 100:
					self._cv_active = True
					self._cv_anchor_percent = self.percent
					self._cv_elapsed_ms = 0
		self._open_adc()
	def _make_status_pin(self, gpio):
		if Pin is None or gpio is None:
			return None
		try:
			pin_id = getattr(Pin, "GPIO{}".format(int(gpio)), int(gpio))
			return Pin(pin_id, Pin.IN, Pin.PULL_PU, 1)
		except Exception as error:
			print("[电池] GPIO{}充电状态脚不可用：{}".format(gpio, error))
			return None
	def _read_charge_status(self):
		if not self._charge_status_available:
			return None, False
		nchrg = int(self._nchrg_pin.read())
		nstdby = int(self._nstdby_pin.read())
		bits = (nchrg, nstdby)
		changed = bits != self._last_status_bits
		self._last_status_bits = bits
		if nchrg == 0 and nstdby == 1:
			return True, False
		if nchrg == 1 and nstdby == 0:
			return False, True
		if nchrg == 1 and nstdby == 1:
			return False, False
		if changed:
			print("[电池] SLM6335状态脚异常：NCHRG={} NSTDBY={}，保持原状态".format(
				nchrg, nstdby))
		return None, False
	def _update_charge_status(self, now_ms, confirm_full=True):
		charging, full = self._read_charge_status()
		if charging is None:
			return
		previous = self.charging_state
		previous_full = self._full_confirmed
		previous_signal = self._full_signal
		self.charging_state = charging
		if charging and self._restored_high:
			self._restored_high = False
		if full and confirm_full:
			self._full_low_count += 1
		elif confirm_full:
			self._full_low_count = 0
		if self._full_low_count >= FULL_CONFIRM_SAMPLES:
			self._full_signal = True
			self._full_confirmed = True
			self._auto_recharge = False
			self._timed_100 = False
			self.percent = 100
			self._cv_active = False
			self._cv_started_ms = None
			self._cv_elapsed_ms = 0
			self._cv_last_ms = None
		elif self._full_signal and charging:
			self._full_signal = False
			self._full_confirmed = False
			self._auto_recharge = True
			self.percent = 100
			self._cv_active = False
			self._cv_last_ms = None
		elif self._full_signal and not full and not charging:
			self._full_signal = False
			self._full_confirmed = False
			self._charge_anchor_percent = self.percent or 100
			self._unplug_started_ms = now_ms
			self._last_drop_ms = now_ms
		if previous != charging:
			self._charge_anchor_percent = self.percent
			self._unplug_started_ms = (None if charging else now_ms)
			if charging:
				self._last_drop_ms = None
				self._unplug_started_ms = None
				self._reconcile_target = None
				self._reconcile_step_ms = None
				self._reconcile_complete = False
			else:
				self._auto_recharge = False
				if self.percent is not None:
					self._charge_anchor_percent = self.percent
				self._unplug_started_ms = now_ms
				self._last_drop_ms = now_ms
				self._reconcile_target = None
				self._reconcile_step_ms = None
				self._reconcile_complete = False
				self._cv_samples = []
				self._cv_active = False
				self._cv_last_ms = None
				self._discharge_samples = []
		if (previous_full != self._full_confirmed or
			previous_signal != self._full_signal):
			self._persist_display_state()
	def set_charging_state(self, state):
		if self._charge_status_available:
			return self.percent
		self.charging_state = None if state is None else bool(state)
		return self.percent
	def set_dynamic_load(self, active):
		self._dynamic_load = bool(active)
	def _persist_display_state(self):
		if self.persistent_config is None:
			return
		percent = self.percent
		if percent is not None and percent >= 90:
			self.persistent_config.update(
				battery_percent=int(percent), battery_high_active=True,
				battery_full_confirmed=bool(self._full_confirmed))
		elif percent is not None:
			self.persistent_config.update(
				battery_percent=None, battery_high_active=False,
				battery_full_confirmed=bool(self._full_confirmed))
	def _clear_restored_high_state(self):
		self._restored_high = False
		self._timed_100 = False
		self._full_signal = False
		self._full_confirmed = False
		self._auto_recharge = False
		self._unplug_started_ms = None
		self._charge_anchor_percent = None
		self._reconcile_target = None
		self._reconcile_step_ms = None
		self._reconcile_complete = True
		self._last_drop_ms = None
		if self.persistent_config is not None:
			self.persistent_config.update(battery_percent=None,
				battery_high_active=False, battery_full_confirmed=False)
	def is_full_confirmed(self):
		return bool(self._full_signal)
	def _open_adc(self):
		opener = getattr(self.adc, "open", None)
		if opener is not None:
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
		# 兼容少数包装层返回单元素列表/元组的情况。
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
		# 加半个除数实现整数四舍五入，避免浮点运算占用资源。
		return ((adc_mv * self.divider_total_kohm) +
				self.divider_bottom_kohm // 2) // self.divider_bottom_kohm
	def _trimmed_average(self, values):
		ordered = sorted(values)
		if len(ordered) <= 2:
			return sum(ordered) // len(ordered)
		middle = ordered[1:-1]
		return (sum(middle) + len(middle) // 2) // len(middle)
	def _median(self, values):
		ordered = sorted(values)
		return ordered[len(ordered) // 2]
	def _curve_points(self):
		if self.charging_state is True:
			return CHARGE_POINTS
		return DISCHARGE_POINTS
	def voltage_to_percent(self, voltage_mv):
		voltage_mv = int(voltage_mv)
		return max(1, min(99, _interpolate(self._curve_points(), voltage_mv)))
	def _apply_display_rules(self, mapped_percent, now_ms, sample_mv=None):
		if self.filtered_voltage_mv <= VOLTAGE_EMPTY_MV:
			self._low_voltage_count += 1
		else:
			self._low_voltage_count = 0
		if self._low_voltage_count >= LOW_VOLTAGE_CONFIRM_SAMPLES:
			return 1
		if self._full_signal:
			return 100
		if self._dynamic_load:
			if self._cv_active:
				self._cv_last_ms = now_ms
			return self.percent if self.percent is not None else mapped_percent
		if self.charging_state is True:
			if self._auto_recharge:
				return 100
			if self._timed_100 and self.percent == 100:
				return 100
			cv_voltage = (self.filtered_voltage_mv if sample_mv is None else
				int(sample_mv))
			if (not self._booting and not self._cv_active and
				cv_voltage >= CV_ENTER_MV):
				self._cv_samples.append(cv_voltage)
				if len(self._cv_samples) > CV_CONFIRM_SAMPLES:
					self._cv_samples.pop(0)
				if (len(self._cv_samples) == CV_CONFIRM_SAMPLES and
					max(self._cv_samples) - min(self._cv_samples) <=
					CV_STABLE_SPREAD_MV):
					self._cv_active = True
					self._cv_started_ms = now_ms
					self._cv_elapsed_ms = 0
					self._cv_anchor_percent = 90
					self._cv_last_ms = now_ms
					self._timed_100 = False
			elif not self._cv_active:
				self._cv_samples = []
			if self._cv_active:
				if self._cv_last_ms is None:
					self._cv_last_ms = now_ms
				if cv_voltage < CV_EXIT_MV:
					self._cv_exit_count += 1
				else:
					self._cv_exit_count = 0
				if self._cv_exit_count >= CV_EXIT_CONFIRM_SAMPLES:
					self._cv_active = False
					self._cv_samples = []
					self._cv_last_ms = None
					self._cv_elapsed_ms = 0
				else:
					if self._cv_last_ms is not None:
						self._cv_elapsed_ms += max(0, _ticks_diff(
							now_ms, self._cv_last_ms))
					self._cv_last_ms = now_ms
					value = min(100, self._cv_anchor_percent +
						self._cv_elapsed_ms //
						(CV_STEP_SECONDS * 1000))
					self._timed_100 = value >= 100
					return max(self.percent or 1, value)
			value = min(89, mapped_percent)
			return max(self.percent or 1, value)
		if self._restored_high:
			self._restore_check_samples.append(self.filtered_voltage_mv)
			if len(self._restore_check_samples) > DISCHARGE_STABLE_SAMPLES:
				self._restore_check_samples.pop(0)
			if (len(self._restore_check_samples) == DISCHARGE_STABLE_SAMPLES and
				max(self._restore_check_samples) -
				min(self._restore_check_samples) <=
				DISCHARGE_STABLE_SPREAD_MV and
				self.filtered_voltage_mv <= HIGH_BATTERY_RESTORE_CUTOFF_MV):
				self.percent = max(1, mapped_percent)
				self._clear_restored_high_state()
				return self.percent
		if self._unplug_started_ms is not None:
			hold_ms = _ticks_diff(now_ms, self._unplug_started_ms)
			if hold_ms < UNPLUG_HOLD_MS:
				return self._charge_anchor_percent or mapped_percent
		self._discharge_samples.append(self.filtered_voltage_mv)
		if len(self._discharge_samples) > DISCHARGE_STABLE_SAMPLES:
			self._discharge_samples.pop(0)
		if (len(self._discharge_samples) < DISCHARGE_STABLE_SAMPLES or
			max(self._discharge_samples) - min(self._discharge_samples) >
			DISCHARGE_STABLE_SPREAD_MV):
			return self.percent if self.percent is not None else mapped_percent
		if (not self._reconcile_complete and self.percent is not None and
			mapped_percent < self.percent):
			if self._reconcile_target is None:
				gap = self.percent - mapped_percent
				self._reconcile_target = mapped_percent
				if gap > 0:
					self._reconcile_step_ms = max(
						UNPLUG_RECONCILE_MIN_STEP_MS,
						min(UNPLUG_RECONCILE_MAX_STEP_MS,
							UNPLUG_RECONCILE_TOTAL_MS // gap))
					self._last_drop_ms = now_ms
			if self.percent > self._reconcile_target:
				if _ticks_diff(now_ms, self._last_drop_ms) >= self._reconcile_step_ms:
					self._last_drop_ms = now_ms
					value = max(self._reconcile_target, self.percent - 1)
					if value <= self._reconcile_target:
						self._reconcile_complete = True
					return value
				return self.percent
			self._reconcile_target = None
			self._reconcile_step_ms = None
			self._reconcile_complete = True
			self._last_drop_ms = now_ms
			return self.percent
		if self.percent is not None and mapped_percent < self.percent:
			if self._last_drop_ms is None or _ticks_diff(
				now_ms, self._last_drop_ms) >= DISCHARGE_STEP_MS:
				self._last_drop_ms = now_ms
				return max(1, self.percent - 1)
			return self.percent
		return self.percent if self.percent is not None else max(1, mapped_percent)
	def _update_filtered_voltage(self, voltage_mv, boot=False, now_ms=None):
		if now_ms is None:
			now_ms = utime.ticks_ms()
		self.raw_voltage_mv = voltage_mv
		if boot:
			self._boot_samples.append(voltage_mv)
			if len(self._boot_samples) < BOOT_SAMPLE_COUNT:
				return None
			filtered = self._trimmed_average(self._boot_samples)
			self._booting = False
			# 用首次稳定值填充运行窗口，避免启动后连续5秒没有稳定输出。
			self._run_samples = [filtered] * RUN_MEDIAN_WINDOW
		else:
			self._run_samples.append(voltage_mv)
			if len(self._run_samples) > RUN_MEDIAN_WINDOW:
				self._run_samples.pop(0)
			filtered = self._median(self._run_samples)
			if self.filtered_voltage_mv is not None:
				filtered = ((self.filtered_voltage_mv *
							 (EMA_TOTAL_WEIGHT - EMA_NEW_WEIGHT)) +
							filtered * EMA_NEW_WEIGHT +
							EMA_TOTAL_WEIGHT // 2) // EMA_TOTAL_WEIGHT
		self.filtered_voltage_mv = int(filtered)
		self.estimated_percent = self.voltage_to_percent(
			self.filtered_voltage_mv)
		self.percent = self._apply_display_rules(
			self.estimated_percent, now_ms,
			filtered)
		if self._full_signal:
			self._full_confirmed = True
		if self.percent != self._last_reported_percent:
			self._persist_display_state()
		changed = self.percent != self._last_reported_percent
		self._last_reported_percent = self.percent
		return self.percent if changed else None
	def tick(self, now_ms=None):
		if now_ms is None:
			now_ms = utime.ticks_ms()
		if _ticks_diff(now_ms, self._next_sample_ms) < 0:
			return None
		self._update_charge_status(now_ms, confirm_full=not self._booting)
		try:
			adc_mv = self._read_adc_mv()
			self.raw_adc_mv = adc_mv
			battery_mv = self.adc_to_battery_mv(adc_mv)
			self.last_error = None
		except Exception as error:
			self.last_error = error
			self._next_sample_ms = _ticks_add(
				now_ms, self._last_sample_interval_ms)
			return None
		if self._booting:
			result = self._update_filtered_voltage(
				battery_mv, boot=True, now_ms=now_ms)
			self._last_sample_interval_ms = (
				BOOT_SAMPLE_INTERVAL_MS if self._booting
				else RUN_SAMPLE_INTERVAL_MS)
		else:
			result = self._update_filtered_voltage(
				battery_mv, boot=False, now_ms=now_ms)
			self._last_sample_interval_ms = RUN_SAMPLE_INTERVAL_MS
		self._next_sample_ms = _ticks_add(
			now_ms, self._last_sample_interval_ms)
		return result
	def get_voltage_mv(self):
		return self.filtered_voltage_mv
	def get_percent(self):
		return self.percent
	def get_estimated_percent(self):
		return self.estimated_percent
	def is_ready(self):
		return not self._booting and self.percent is not None
