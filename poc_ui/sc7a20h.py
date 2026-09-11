# -*- coding: utf-8 -*-
"""SC7A20H 加速度计驱动与双击/跌倒业务服务。

SC7A20H 与 ET6312B 共用 EC800M 硬件 I2C0。底层驱动只负责寄存器和
采样数据；当前验证入口使用 ``SC7A20HDoubleClickAdapter``，不创建加速度
业务线程，双击寄存器读取由统一低优先级 Worker 执行。

SD0 接地时默认 7 位地址为 0x18，也可以通过构造参数改为 0x19。
"""

import _thread
import math
import utime

try:
	from usr.i2c_bus import get_i2c0
except Exception:
	from i2c_bus import get_i2c0


try:
	from machine import ExtInt
except Exception:
	ExtInt = None

try:
	from machine import Timer
except Exception:
	Timer = None


def _new_event_sem():
	"""创建首次 acquire 会阻塞的信号量或锁。"""
	if hasattr(_thread, "allocate_semphore"):
		sem = _thread.allocate_semphore(1)
		try:
			count = sem.getCnt()
			initial = count[1] if isinstance(count, (tuple, list)) else count
			for _ in range(int(initial or 0)):
				try:
					sem.acquire()
				except Exception:
					break
		except Exception:
			try:
				sem.acquire()
			except Exception:
				pass
		return sem
	sem = _thread.allocate_lock()
	sem.acquire()
	return sem


SC7A20H_ADDR = 0x19
SC7A20H_ADDR_SDO_GND = 0x18
SC7A20H_ID = 0x11
SC7A20H_AUTO_INCREMENT = 0x80
I2C_READ_DELAY = 0
M_G = 9.80665
RAD_TO_DEG = 180.0 / math.pi

# 寄存器地址。
REG_WHO_AM_I = 0x0F
REG_CTRL0 = 0x1F
REG_CTRL1 = 0x20
REG_CTRL2 = 0x21
REG_CTRL3 = 0x22
REG_CTRL4 = 0x23
REG_CTRL5 = 0x24
REG_CTRL6 = 0x25
REG_DRDY_STATUS = 0x27
REG_OUT_X_L = 0x28
REG_OUT_X_H = 0x29
REG_OUT_Y_L = 0x2A
REG_OUT_Y_H = 0x2B
REG_OUT_Z_L = 0x2C
REG_OUT_Z_H = 0x2D
REG_FIFO_CTRL = 0x2E
REG_AOI1_CFG = 0x30
REG_AOI1_SRC = 0x31
REG_AOI1_THS = 0x32
REG_AOI1_DURATION = 0x33
REG_AOI2_CFG = 0x34
REG_AOI2_SRC = 0x35
REG_AOI2_THS = 0x36
REG_AOI2_DURATION = 0x37
REG_CLICK_CTRL = 0x38
REG_CLICK_SRC = 0x39
REG_CLICK_COEFF1 = 0x3A
REG_CLICK_COEFF2 = 0x3B
REG_CLICK_COEFF3 = 0x3C
REG_CLICK_COEFF4 = 0x3D

# CTRL_REG1：200Hz、低功耗、三轴开启。
ODR_200HZ = 0x60
LOW_POWER_ENABLE = 0x08
ENABLE_ALL_AXES = 0x07

# CTRL_REG4：块数据更新 + 可配置量程；±4g 是跌倒冲击和自由落体的默认量程。
SCALE_2G = 0x00
SCALE_4G = 0x10
SCALE_8G = 0x20
SCALE_16G = 0x30
BDU_ENABLE = 0x80


class SC7A20H:
	"""SC7A20H 寄存器驱动。"""

	def __init__(self, address=SC7A20H_ADDR_SDO_GND, i2c=None,
				 scale=SCALE_4G):
		if not 0 <= address <= 0x7F:
			raise ValueError("I2C 地址必须是 0x00~0x7F 的 7 位地址")
		if scale not in (SCALE_2G, SCALE_4G, SCALE_8G, SCALE_16G):
			raise ValueError("量程必须是 SCALE_2G/4G/8G/16G")
		self.address = address
		self.scale = scale
		self.i2c = i2c if i2c is not None else get_i2c0()
		self.scale_factor = self._scale_factor(scale)
		self.initialized = False

	@staticmethod
	def _scale_factor(scale):
		"""返回 12 位原始值到 g 的换算因子。"""
		# 12 位有符号值满量程约 2048；±4g 时每 LSB 为 4/2048g。
		return {
			SCALE_2G: 2.0 / 2048.0,
			SCALE_4G: 4.0 / 2048.0,
			SCALE_8G: 8.0 / 2048.0,
			SCALE_16G: 16.0 / 2048.0,
		}[scale]

	def _i2c_write(self, payload):
		"""通过共享 I2C0 写寄存器。"""
		data = bytearray(payload)
		if len(data) < 2:
			raise ValueError("I2C 写入内容必须包含寄存器地址和数据")
		register_buffer = bytearray((data[0] & 0x7F,))
		value_buffer = bytearray(len(data) - 1)
		for index in range(1, len(data)):
			value_buffer[index - 1] = data[index]
		result = self.i2c.write(
			self.address, register_buffer, 1,
			value_buffer, len(value_buffer))
		if result not in (0, None):
			raise OSError("SC7A20H I2C 写入失败，返回值：{}".format(result))
		return result

	def _i2c_read(self, register, length):
		"""按 QuecPython 六参数接口执行重复起始读。"""
		if length <= 0:
			raise ValueError("读取长度必须大于 0")
		register_address = register & 0x7F
		if length > 1:
			register_address |= SC7A20H_AUTO_INCREMENT
		data = bytearray(length)
		result = self.i2c.read(
			self.address, bytearray((register_address,)), 1,
			data, length, I2C_READ_DELAY)
		if result not in (0, None):
			raise OSError("SC7A20H I2C 读取失败，返回值：{}".format(result))
		return data

	def _write_register(self, register, value):
		return self._i2c_write((register, value))

	def read_register(self, register):
		return self._i2c_read(register, 1)[0]

	def write_register(self, register, value):
		return self._write_register(register, value)

	def read_registers(self, register, length):
		return self._i2c_read(register, length)

	def who_am_i(self):
		return self.read_register(REG_WHO_AM_I)

	def initialize(self):
		"""检查芯片 ID 并配置 200Hz 低功耗、双击和两路 AOI 中断。"""
		device_id = self.who_am_i()
		if device_id != SC7A20H_ID:
			raise OSError(
				"SC7A20H ID 不匹配：期望 0x{:02X}，实际 0x{:02X}".format(
					SC7A20H_ID, device_id))

		self._write_register(REG_CTRL1,
							 ODR_200HZ | LOW_POWER_ENABLE | ENABLE_ALL_AXES)
		self._write_register(REG_CTRL4, self.scale | BDU_ENABLE)
		self._write_register(REG_CTRL0, 0x00)
		self._write_register(REG_CTRL2, 0x00)
		self._write_register(REG_FIFO_CTRL, 0x00)

		# CLICK 映射 INT1，AOI1/AOI2 映射 INT2，并锁存中断直到读 SRC。
		self._write_register(REG_CTRL3, 0x80)
		self._write_register(REG_CTRL5, 0x0A)
		self._write_register(REG_CTRL6, 0x60)
		self.config_freefall(400, 20)
		self.config_impact(1800, 2)
		self.config_double_click()
		# 读取一次 SRC，清除上电前可能残留的锁存状态。
		self.read_register(REG_CLICK_SRC)
		self.read_register(REG_AOI1_SRC)
		self.read_register(REG_AOI2_SRC)
		self.initialized = True
		return True

	def set_fall_interrupts_enabled(self, enabled):
		"""Map/unmap AOI1 and AOI2 on INT2 without affecting CLICK/INT1."""
		self._write_register(REG_CTRL6, 0x60 if enabled else 0x00)
		if not enabled:
			self.read_register(REG_AOI1_SRC)
			self.read_register(REG_AOI2_SRC)

	def set_double_click_interrupt_enabled(self, enabled):
		"""Map/unmap CLICK on INT1 while preserving its register setup."""
		self._write_register(REG_CTRL3, 0x80 if enabled else 0x00)
		if not enabled:
			self.read_register(REG_CLICK_SRC)

	def _read_raw_value(self, low, high):
		data = self._i2c_read(low, 2)
		raw = ((data[1] << 8) | data[0]) >> 4
		if raw & 0x0800:
			raw -= 0x1000
		return raw

	def read_raw(self):
		"""读取三轴左对齐 12 位有符号原始值。"""
		data = self._i2c_read(REG_OUT_X_L, 6)
		values = []
		for offset in (0, 2, 4):
			raw = ((data[offset + 1] << 8) | data[offset]) >> 4
			if raw & 0x0800:
				raw -= 0x1000
			values.append(raw)
		return tuple(values)

	def read_acceleration_g(self):
		"""读取经过项目坐标变换的三轴加速度，单位 g。"""
		raw_x, raw_y, raw_z = self.read_raw()
		sensor_x = raw_x * self.scale_factor
		sensor_y = raw_y * self.scale_factor
		sensor_z = raw_z * self.scale_factor
		return sensor_y, -sensor_x, -sensor_z

	def read_acceleration(self):
		"""读取三轴加速度，单位 m/s²。"""
		values = self.read_acceleration_g()
		return values[0] * M_G, values[1] * M_G, values[2] * M_G

	def read_data(self):
		"""读取三轴加速度、合加速度、静态 pitch/roll。

		加速度计没有陀螺仪或磁力计，不能提供可靠的 yaw；pitch/roll 仅在
		静态或准静态时有意义。
		"""
		acc_x, acc_y, acc_z = self.read_acceleration()
		acc_g = math.sqrt(acc_x * acc_x + acc_y * acc_y + acc_z * acc_z)
		if acc_g == 0.0:
			pitch = 0.0
			roll = 0.0
		else:
			pitch = math.atan2(acc_y, acc_z) * RAD_TO_DEG
			roll = math.atan2(acc_x, acc_z) * RAD_TO_DEG
		return {
			"acc_x": acc_x,
			"acc_y": acc_y,
			"acc_z": acc_z,
			"acc_g": acc_g,
			"pitch": pitch,
			"roll": roll,
		}

	def data_ready(self):
		return self.read_register(REG_DRDY_STATUS)

	def _threshold_code(self, threshold_mg):
		mg_per_lsb = {
			SCALE_2G: 16,
			SCALE_4G: 32,
			SCALE_8G: 64,
			SCALE_16G: 128,
		}[self.scale]
		# THS[6:0] 有 7 位，保留 127 作为通用配置上限。
		return max(0, min(127, int(float(threshold_mg) / mg_per_lsb)))

	def config_freefall(self, threshold_mg=400, duration=20):
		"""配置 AOI1 自由落体：三轴低于阈值并持续 duration 个 ODR 周期。"""
		if not 0 <= duration <= 127:
			raise ValueError("自由落体 duration 必须是 0~127")
		self._write_register(REG_AOI1_THS, self._threshold_code(threshold_mg))
		self._write_register(REG_AOI1_DURATION, duration)
		# AOI=1、X/Y/Z 低事件同时成立；适合自由落体检测。
		self._write_register(REG_AOI1_CFG, 0x95)

	def config_impact(self, threshold_mg=1800, duration=2):
		"""配置 AOI2 冲击：检测六个轴向的绝对值超过阈值。"""
		if not 0 <= duration <= 127:
			raise ValueError("冲击 duration 必须是 0~127")
		self._write_register(REG_AOI2_THS, self._threshold_code(threshold_mg))
		self._write_register(REG_AOI2_DURATION, duration)
		# AOI=0 为或逻辑，六个高/低方向都启用，避免漏掉反向冲击。
		self._write_register(REG_AOI2_CFG, 0x3F)

	def config_double_click(self):
		"""配置 XYZ 精确双击并把事件锁存到 CLICK_SRC。

		COEFF4 的高四位 SCMT 决定芯片等待完整单击序列的最长时间。
		原来的 0x52（SCMT=5）在 200Hz 下会等待约 1.7 秒，造成明显延迟；
		这里改为 SCMT=1，最长约 420ms，仍覆盖正常人手双击间隔。
		低四位 MCNTH=2 保持双击计数。
		"""
		self._write_register(REG_CLICK_CTRL, 0x1F)
		self._write_register(REG_CLICK_COEFF1, 0x12)
		self._write_register(REG_CLICK_COEFF2, 0xA2)
		self._write_register(REG_CLICK_COEFF3, 0x04)
		self._write_register(REG_CLICK_COEFF4, 0x12)

	def read_click_source(self):
		return self.read_register(REG_CLICK_SRC)

	def read_aoi1_source(self):
		return self.read_register(REG_AOI1_SRC)

	def read_aoi2_source(self):
		return self.read_register(REG_AOI2_SRC)


class SC7A20HDoubleClickAdapter:
	"""无业务线程的双击中断适配器。"""

	def __init__(self, sensor, on_interrupt, int1_pin=25):
		self.sensor = sensor
		self.on_interrupt = on_interrupt
		self.int1_pin = int1_pin
		self.irq = None
		self.started = False

	def _on_int1(self, *args):
		self.on_interrupt()

	def start(self):
		if self.started:
			return True
		self.sensor.initialize()
		self.sensor.set_fall_interrupts_enabled(False)
		self.sensor.set_double_click_interrupt_enabled(True)
		self.irq = ExtInt(
			self.int1_pin, ExtInt.IRQ_RISING, ExtInt.PULL_DISABLE,
			self._on_int1)
		self.irq.enable()
		self.started = True
		return True

	def stop(self):
		if self.irq is not None:
			self.irq.disable()
		self.irq = None
		self.started = False


_default_sensor = None


def get_sensor(address=SC7A20H_ADDR_SDO_GND, scale=SCALE_4G, i2c=None):
	"""获取默认传感器实例并完成初始化；地址仍可显式配置。"""
	global _default_sensor
	if _default_sensor is None:
		_default_sensor = SC7A20H(address=address, scale=scale, i2c=i2c)
		_default_sensor.initialize()
	return _default_sensor


def read_acceleration():
	"""快捷接口：读取三轴加速度（m/s²）。"""
	return get_sensor().read_acceleration()


def read_data():
	"""快捷接口：读取三轴加速度及 pitch/roll。"""
	return get_sensor().read_data()


class SC7A20HService:
	"""独立的双击切换与人员跌倒识别服务。

GPIO25（物理 Pin17）接 INT1，GPIO26（物理 Pin18）接 INT2。中断回调
	只设置标志，实际寄存器读取和业务判断均在线程上下文完成。
	"""

	SAMPLE_INTERVAL_MS = 20       # 50Hz 业务采样
	FREEFALL_THRESHOLD_G = 0.40
	FREEFALL_MIN_MS = 100
	IMPACT_THRESHOLD_G = 1.80
	FAST_FALL_WINDOW_MS = 2000
	POSTURE_CHANGE_DEG = 45.0
	STATIC_MIN_G = 0.75
	STATIC_MAX_G = 1.25
	STATIC_AXIS_DELTA_G = 0.08
	STATIC_CONFIRM_MS = 5000
	CANDIDATE_TIMEOUT_MS = 15000
	DOUBLE_CLICK_GUARD_MS = 250

	def __init__(self, controller, sensor=None,
				 address=SC7A20H_ADDR_SDO_GND,
				 int1_pin=17, int2_pin=18,
				 sample_interval_ms=SAMPLE_INTERVAL_MS,
				 extint_factory=None, on_double_click=None):
		self.controller = controller
		self.sensor = sensor if sensor is not None else SC7A20H(
			address=address, scale=SCALE_4G)
		self.int1_pin_number = int1_pin
		self.int2_pin_number = int2_pin
		self.sample_interval_ms = max(5, int(sample_interval_ms))
		self.extint_factory = extint_factory if extint_factory is not None else ExtInt
		self.on_double_click = on_double_click
		self._running = False
		self._int1_pending = False
		self._int2_pending = False
		self._sample_pending = False
		self._fall_config_pending = False
		self._double_click_config_pending = False
		self._wake_pending = False
		self._event_sem = _new_event_sem()
		self._fall_timer = None
		self._fall_timer_started = False
		self._irq_objects = []
		self._last_double_click_ms = None
		self.double_click_enabled = True
		self._freefall_started_ms = None
		self._fast_fall_until_ms = None
		self._hardware_impact = False
		self._baseline = None
		self._baseline_stable_since_ms = None
		self._previous_vector = None
		self._candidate_started_ms = None
		self._candidate_reference = None
		self._static_started_ms = None
		self._fall_confirmed = False
		self.fall_detection_enabled = False
		try:
			self._fall_cancel_version = controller.get_fall_cancel_version()
		except Exception:
			self._fall_cancel_version = 0

	@staticmethod
	def _ticks_diff(later_ms, earlier_ms):
		try:
			return utime.ticks_diff(later_ms, earlier_ms)
		except Exception:
			return later_ms - earlier_ms

	@staticmethod
	def _ticks_add(base_ms, delta_ms):
		try:
			return utime.ticks_add(base_ms, delta_ms)
		except Exception:
			return base_ms + delta_ms

	@staticmethod
	def _normalize(vector):
		length = math.sqrt(
			vector[0] * vector[0] + vector[1] * vector[1] +
			vector[2] * vector[2])
		if length <= 0.0001:
			return None
		return (vector[0] / length, vector[1] / length, vector[2] / length)

	@classmethod
	def _angle_between(cls, first, second):
		if first is None or second is None:
			return 0.0
		dot = (first[0] * second[0] + first[1] * second[1] +
			   first[2] * second[2])
		dot = max(-1.0, min(1.0, dot))
		return math.acos(dot) * RAD_TO_DEG

	def _pin_value(self, name, fallback):
		if self.extint_factory is None:
			return fallback
		return getattr(self.extint_factory, name, fallback)

	def _setup_one_irq(self, pin_name, fallback_pin, callback):
		if self.extint_factory is None:
			return False
		pin = self._pin_value(pin_name, fallback_pin)
		mode = self._pin_value("IRQ_RISING", 1)
		pull = self._pin_value("PULL_DISABLE", 0)
		try:
			irq = self.extint_factory(pin, mode, pull, callback)
		except TypeError:
			# 个别固件构造函数没有 pull 参数，保留兼容回退。
			irq = self.extint_factory(pin, mode, callback)
		try:
			irq.enable()
		except Exception:
			pass
		self._irq_objects.append(irq)
		return True

	def _setup_irqs(self):
		try:
			int1_ok = self._setup_one_irq("GPIO25", 25, self._on_int1)
			int2_ok = self._setup_one_irq("GPIO26", 26, self._on_int2)
			return int1_ok and int2_ok
		except Exception:
			self._irq_objects = []
			return False

	def _wake_worker(self):
		# Worker 已经可运行时合并重复通知，避免信号量无界累积。
		if self._wake_pending:
			return
		self._wake_pending = True
		try:
			self._event_sem.release()
		except Exception:
			self._wake_pending = False

	def _on_fall_sample_timer(self, *args):
		self._sample_pending = True
		self._wake_worker()

	def _start_fall_sample_timer(self):
		if self._fall_timer_started or not self._running:
			return
		if Timer is None or not hasattr(Timer, "Timer2"):
			raise RuntimeError("当前固件不支持 Timer2，无法启动跌倒采样")
		timer = Timer(Timer.Timer2)
		mode = getattr(timer, "PERIODIC", getattr(Timer, "PERIODIC", 1))
		timer.start(period=self.sample_interval_ms, mode=mode,
					callback=self._on_fall_sample_timer)
		self._fall_timer = timer
		self._fall_timer_started = True

	def _stop_fall_sample_timer(self):
		timer = self._fall_timer
		self._fall_timer = None
		self._fall_timer_started = False
		self._sample_pending = False
		if timer is not None:
			try:
				timer.stop()
			except Exception:
				pass

	def _on_int1(self, *args):
		# IRQ 中禁止 I2C 和业务调用，只记录事件并唤醒服务线程。
		self._int1_pending = True
		self._wake_worker()

	def _on_int2(self, *args):
		self._int2_pending = True
		self._wake_worker()

	def start(self):
		"""初始化传感器、注册中断并启动服务线程。"""
		if self._running:
			return True
		self.sensor.initialize()
		# 跌倒检测默认关闭，只保留双击中断。
		self.sensor.set_fall_interrupts_enabled(
			self.fall_detection_enabled)
		self._setup_irqs()
		self._running = True
		try:
			_thread.start_new_thread(self._run, ())
		except Exception:
			self._running = False
			raise
		if self.fall_detection_enabled:
			try:
				self._start_fall_sample_timer()
			except Exception:
				self.stop()
				raise
		return True

	def set_fall_detection_enabled(self, enabled):
		"""切换跌倒检测；关闭时清空当前检测状态。"""
		enabled = bool(enabled)
		if enabled == self.fall_detection_enabled:
			return enabled
		self.fall_detection_enabled = enabled
		self._fall_config_pending = True
		if enabled:
			try:
				self._start_fall_sample_timer()
			except Exception:
				self.fall_detection_enabled = False
				raise
		else:
			self._stop_fall_sample_timer()
			self._reset_fall_state()
		self._wake_worker()
		return enabled

	def set_double_click_enabled(self, enabled):
		"""启用或关闭硬件双击处理。"""
		enabled = bool(enabled)
		if enabled == self.double_click_enabled:
			return enabled
		self.double_click_enabled = enabled
		self._double_click_config_pending = True
		if not self.double_click_enabled:
			self._last_double_click_ms = None
		self._wake_worker()
		return enabled

	def stop(self):
		"""停止线程并禁用已注册的 GPIO 中断。"""
		self._running = False
		self._stop_fall_sample_timer()
		# 停止时强制唤醒，确保线程不会再次阻塞。
		self._wake_pending = False
		self._wake_worker()
		for irq in self._irq_objects:
			try:
				irq.disable()
			except Exception:
				pass
		self._irq_objects = []

	def _consume_sources(self, now_ms):
		"""在线程上下文读取锁存源寄存器，处理中断事件。"""
		int1 = self._int1_pending
		int2 = self._int2_pending
		self._int1_pending = False
		self._int2_pending = False

		try:
			if int1:
				click_source = self.sensor.read_click_source()
				if self.double_click_enabled and (click_source & 0x0F) == 2:
					if (self._last_double_click_ms is None or
							self._ticks_diff(now_ms, self._last_double_click_ms) >=
							self.DOUBLE_CLICK_GUARD_MS):
						self._last_double_click_ms = now_ms
						if self.on_double_click is not None:
							self.on_double_click()
						else:
							self.controller.cycle_mode()
			if int2:
				aoi1_source = self.sensor.read_aoi1_source()
				aoi2_source = self.sensor.read_aoi2_source()
				if aoi1_source & 0x40:
					self._fast_fall_until_ms = self._ticks_add(
						now_ms, self.FAST_FALL_WINDOW_MS)
				if aoi2_source & 0x40:
					self._hardware_impact = True
		except Exception:
			pass

	def _reset_fall_state(self, keep_baseline=False):
		self._freefall_started_ms = None
		self._fast_fall_until_ms = None
		self._hardware_impact = False
		self._candidate_started_ms = None
		self._candidate_reference = None
		self._static_started_ms = None
		self._fall_confirmed = False
		self._previous_vector = None
		if not keep_baseline:
			self._baseline = None
			self._baseline_stable_since_ms = None

	def _confirm_fall(self):
		if self._fall_confirmed:
			return
		self._fall_confirmed = True
		try:
			self.controller.start_fall_alarm()
		except Exception:
			pass

	def _process_sample(self, now_ms, values):
		vector = self._normalize(values)
		if vector is None:
			return
		magnitude = math.sqrt(
			values[0] * values[0] + values[1] * values[1] +
			values[2] * values[2])
		stable = (self.STATIC_MIN_G <= magnitude <= self.STATIC_MAX_G and
				  (self._previous_vector is None or max(
					  abs(values[0] - self._previous_vector[0]),
					  abs(values[1] - self._previous_vector[1]),
					  abs(values[2] - self._previous_vector[2])) <=
				   self.STATIC_AXIS_DELTA_G))
		self._previous_vector = values

		if self._baseline is None:
			if stable:
				if self._baseline_stable_since_ms is None:
					self._baseline_stable_since_ms = now_ms
				elif self._ticks_diff(now_ms,
									  self._baseline_stable_since_ms) >= 1000:
					self._baseline = vector
			else:
				self._baseline_stable_since_ms = None
			return

		# 正常且稳定时缓慢更新姿态基准；候选状态下冻结基准。
		if self._candidate_started_ms is None and stable:
			self._baseline = self._normalize((
				self._baseline[0] * 0.98 + vector[0] * 0.02,
				self._baseline[1] * 0.98 + vector[1] * 0.02,
				self._baseline[2] * 0.98 + vector[2] * 0.02))

		if magnitude < self.FREEFALL_THRESHOLD_G:
			if self._freefall_started_ms is None:
				self._freefall_started_ms = now_ms
		elif self._freefall_started_ms is not None:
			if self._ticks_diff(now_ms, self._freefall_started_ms) >= self.FREEFALL_MIN_MS:
				self._fast_fall_until_ms = self._ticks_add(
					now_ms, self.FAST_FALL_WINDOW_MS)
			self._freefall_started_ms = None

		if (self._fast_fall_until_ms is not None and
				self._ticks_diff(now_ms, self._fast_fall_until_ms) > 0):
			self._fast_fall_until_ms = None

		impact = magnitude >= self.IMPACT_THRESHOLD_G or self._hardware_impact
		fast_sequence = (impact and self._fast_fall_until_ms is not None)
		posture_change = (self._angle_between(self._baseline, vector) >=
						  self.POSTURE_CHANGE_DEG)

		if self._candidate_started_ms is None:
			if fast_sequence:
				self._candidate_started_ms = now_ms
				self._candidate_reference = self._baseline
			elif posture_change:
				# 没有明显自由落体的缓慢跌倒路径。
				self._candidate_started_ms = now_ms
				self._candidate_reference = self._baseline
		self._hardware_impact = False

		if self._candidate_started_ms is None:
			return
		if self._ticks_diff(now_ms, self._candidate_started_ms) > self.CANDIDATE_TIMEOUT_MS:
			self._reset_fall_state(keep_baseline=True)
			return

		posture_change = (self._angle_between(self._candidate_reference, vector) >=
						  self.POSTURE_CHANGE_DEG)
		if posture_change and stable:
			if self._static_started_ms is None:
				self._static_started_ms = now_ms
			elif self._ticks_diff(now_ms, self._static_started_ms) >= self.STATIC_CONFIRM_MS:
				self._confirm_fall()
		elif not stable:
			self._static_started_ms = None

	def _run(self):
		while True:
			self._event_sem.acquire()
			self._wake_pending = False
			if not self._running:
				break
			now_ms = utime.ticks_ms()
			sample_pending = self._sample_pending
			self._sample_pending = False
			try:
				if self._fall_config_pending:
					enabled = self.fall_detection_enabled
					self.sensor.set_fall_interrupts_enabled(enabled)
					if enabled == self.fall_detection_enabled:
						self._fall_config_pending = False
				if self._double_click_config_pending:
					enabled = self.double_click_enabled
					self.sensor.set_double_click_interrupt_enabled(enabled)
					if enabled == self.double_click_enabled:
						self._double_click_config_pending = False
				self._consume_sources(now_ms)
				try:
					cancel_version = self.controller.get_fall_cancel_version()
				except Exception:
					cancel_version = self._fall_cancel_version
				if cancel_version != self._fall_cancel_version:
					# 只在用户明确选择 off 时取消当前事件；普通模式保持 off
					# 并不会关闭检测，否则开机默认 off 时将无法识别跌倒。
					self._fall_cancel_version = cancel_version
					self._reset_fall_state()
				if self.fall_detection_enabled and sample_pending:
					if self._fall_confirmed:
						if not self.controller.is_fall_alarm_active():
							self._reset_fall_state(keep_baseline=True)
						else:
							self.controller.tick(now_ms)
					else:
						values = self.sensor.read_acceleration_g()
						self._process_sample(now_ms, values)
						if self._fall_confirmed:
							self.controller.tick(now_ms)
			except Exception:
				pass
			# 若读取期间又产生中断或定时事件，重新唤醒下一轮处理。
			if (self._running and
					(self._int1_pending or self._int2_pending or
					 self._sample_pending or self._fall_config_pending or
					 self._double_click_config_pending)):
				self._wake_worker()
