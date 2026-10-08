# -*- coding: utf-8 -*-

import _thread
import utime

try:
	from usr.i2c_bus import get_i2c0
except Exception:
	from i2c_bus import get_i2c0


# ET6312B 的 7 位 I2C 地址：ADDR 接 GND 时为 0x48。
ET6312B_I2C_ADDRESS = 0x48

# 数据手册寄存器地址。
REG_CHIP_CONTROL = 0x00
REG_TIMING_BASE = 0x01
REG_RF_SCALE = 0x15
REG_LED_MODE_BASE = 0x16
REG_LED_CURRENT_BASE = 0x1A
REG_EXTERNAL_PWM = 0x26

# CHIPCTR：关闭自主呼吸、使用内部时钟、退出软件关机、最大电流 24mA。
CHIP_CONTROL_NORMAL = 0x10
CHIP_CONTROL_AUTONOMOUS_MODE2 = CHIP_CONTROL_NORMAL | 0x80 | 0x01

ALL_CHANNELS_OFF_MODE = 0x00

# imax_sel=0 时，0x4F 对应 10mA；可在创建驱动时传入其他 0x00~0xBF 值。
DEFAULT_CURRENT_CODE = 0x4F
CURRENT_LEVEL_COUNT = 192
MIN_BRIGHTNESS_PERCENT = 5
MAX_BRIGHTNESS_PERCENT = 100
SHOULDER_LAMP_INIT_ATTEMPTS = 5
SHOULDER_LAMP_INIT_RETRY_MS = 100
# 单次寄存器事务失败时的有限重试；外层初始化仍保留5次完整初始化重试。
I2C_WRITE_RETRY_ATTEMPTS = 3
I2C_WRITE_RETRY_DELAY_MS = 10
ET6312B_WAKEUP_SETTLE_MS = 10

# 每组三个 LED 在 LEDXMD_RGBn 寄存器中的字段位置。
LED_MODE_SHIFTS = (0, 2, 5)
LED_MODE_ALWAYS_ON = 0x01
# ET6312B 的每个 RGB 组共用一个呼吸周期。三路全部分配到
# Thread1/Thread2 时，LEDXMD_RGBn 分别编码为 0x4A/0x6F。
ALL_CHANNELS_THREAD1_MODE = 0x4A
ALL_CHANNELS_THREAD2_MODE = 0x6F

AUTONOMOUS_PERIOD_CODE = 0x07
AUTONOMOUS_TON1 = 0x7F
AUTONOMOUS_TON2 = 0x7F
AUTONOMOUS_RAMP_RATE = 0x00
AUTONOMOUS_RF_SCALE = 0x00

# D1~D12 在 12 位输出掩码中的对应位；D1 使用最低位。
ALL_LED_MASK = 0x0FFF


class ET6312B:

	def __init__(self, address=ET6312B_I2C_ADDRESS,
				 current_code=DEFAULT_CURRENT_CODE, i2c=None):
		if not 0 <= address <= 0x7F:
			raise ValueError("I2C 地址必须是 0x00~0x7F 的 7 位地址")
		if not 0 <= current_code <= 0xBF:
			raise ValueError("电流代码必须在 0x00~0xBF 范围内")

		self.address = address
		self.current_code = current_code
		# 允许外部传入 I2C 对象，便于总线共享；默认使用 EC800M 的 I2C0。
		# 默认从共享管理器取得 I2C0；外部传入对象时用于测试或特殊总线。
		self.i2c = i2c if i2c is not None else get_i2c0()

		# 保存四个 LEDXMD_RGBn 寄存器的状态，单灯操作时不会影响其他灯。
		self._mode_registers = bytearray(4)
		self._autonomous_enabled = False
		self.initialize()

	def _set_mode_registers(self, values):
		for index in range(4):
			self._mode_registers[index] = values[index]

	def _write_registers(self, start_register, values):
		register_buffer = bytearray((start_register,))
		data_buffer = values if isinstance(values, bytearray) else bytearray(values)
		last_error = None
		attempts = max(1, int(I2C_WRITE_RETRY_ATTEMPTS))
		for attempt in range(1, attempts + 1):
			try:
				result = self.i2c.write(
					self.address,
					register_buffer,
					len(register_buffer),
					data_buffer,
					len(data_buffer),
				)
				# QuecPython I2C.write 正常返回0；部分固件不返回值。
				if result in (0, None):
					if attempt > 1:
						print("[肩灯I2C] 写入恢复：寄存器=0x{:02X} 长度={} 第{}/{}次成功".format(
							start_register, len(data_buffer), attempt, attempts))
					return result
				last_error = OSError(
					"ET6312B I2C写入返回值异常：{}".format(result))
			except Exception as error:
				last_error = error
			print("[肩灯I2C] 写入失败：寄存器=0x{:02X} 长度={} 第{}/{}次 错误={}".format(
				start_register, len(data_buffer), attempt, attempts, last_error))
			if attempt < attempts and I2C_WRITE_RETRY_DELAY_MS > 0:
				utime.sleep_ms(I2C_WRITE_RETRY_DELAY_MS)
		raise OSError(
			"ET6312B I2C写入失败：寄存器=0x{:02X} 长度={} 共尝试{}次，最后错误={}".format(
				start_register, len(data_buffer), attempts, last_error))

	def _write_register(self, register, value):
		return self._write_registers(register, bytearray((value,)))

	def set_brightness_percent(self, brightness_percent):
		try:
			brightness_percent = int(brightness_percent)
		except Exception:
			raise ValueError("肩灯亮度必须是整数百分比")
		if not MIN_BRIGHTNESS_PERCENT <= brightness_percent <= MAX_BRIGHTNESS_PERCENT:
			raise ValueError("肩灯亮度必须在 5%~100% 范围内")

		# LEDx_CURT 的 0x00~0xBF 对应 192 档。先向下取整再减一，
		# 可保证 42% 仍为原来的 0x4F，100% 则为 0xBF。
		current_code = (
			brightness_percent * CURRENT_LEVEL_COUNT // 100) - 1
		self._write_registers(
			REG_LED_CURRENT_BASE,
			bytearray((current_code,) * 12),
		)
		# 只有 I2C 写入成功后才更新缓存，失败时继续保留原亮度。
		self.current_code = current_code
		return brightness_percent

	def get_brightness_percent(self):
		brightness_percent = (
			((self.current_code + 1) * 100 + CURRENT_LEVEL_COUNT // 2)
			// CURRENT_LEVEL_COUNT
		)
		if brightness_percent < MIN_BRIGHTNESS_PERCENT:
			return MIN_BRIGHTNESS_PERCENT
		if brightness_percent > MAX_BRIGHTNESS_PERCENT:
			return MAX_BRIGHTNESS_PERCENT
		return brightness_percent

	def disable_autonomous(self):
		if self._autonomous_enabled:
			self._write_register(REG_CHIP_CONTROL, CHIP_CONTROL_NORMAL)
			self._autonomous_enabled = False
		return True

	def configure_autonomous(self, mode_registers):
		if len(mode_registers) != 4:
			raise ValueError("自主模式必须提供 4 个 RGB 模式寄存器")

		self._write_register(REG_CHIP_CONTROL, CHIP_CONTROL_NORMAL)
		self._autonomous_enabled = False

		# QuecPython 的 bytearray.extend() 要求传入 buffer 对象，不能直接
		# 传入 tuple；预分配并逐项写入可兼容设备固件的实现。
		timing = bytearray(20)
		for group_index in range(4):
			offset = group_index * 5
			timing[offset] = AUTONOMOUS_PERIOD_CODE
			timing[offset + 1] = AUTONOMOUS_TON1
			timing[offset + 2] = AUTONOMOUS_TON2
			timing[offset + 3] = AUTONOMOUS_RAMP_RATE
			timing[offset + 4] = 0x00
		self._write_registers(REG_TIMING_BASE, timing)
		self._write_register(REG_RF_SCALE, AUTONOMOUS_RF_SCALE)
		values = bytearray(mode_registers)
		self._write_registers(REG_LED_MODE_BASE, values)
		self._set_mode_registers(values)
		self._write_register(REG_CHIP_CONTROL, CHIP_CONTROL_AUTONOMOUS_MODE2)
		self._autonomous_enabled = True
		return values

	def initialize(self):
		# 芯片上电默认处于软件关机状态；先唤醒，再配置其他寄存器。
		# 该步骤放在完整初始化内，因此每次初始化重试都会重新执行。
		self._write_register(REG_CHIP_CONTROL, CHIP_CONTROL_NORMAL)
		if ET6312B_WAKEUP_SETTLE_MS > 0:
			utime.sleep_ms(ET6312B_WAKEUP_SETTLE_MS)

		# 先关闭全部输出，避免初始化过程中 LED 短暂闪烁。
		self._write_registers(
			REG_LED_MODE_BASE,
			bytearray((ALL_CHANNELS_OFF_MODE,) * 4),
		)
		self._set_mode_registers(bytearray((ALL_CHANNELS_OFF_MODE,) * 4))

		# 关闭外部 PWM，设置十二路恒流值，再退出软件关机状态。
		self._write_register(REG_EXTERNAL_PWM, 0x00)
		self._write_registers(
			REG_LED_CURRENT_BASE,
			bytearray((self.current_code,) * 12),
		)
		self._write_register(REG_CHIP_CONTROL, CHIP_CONTROL_NORMAL)

	def apply_mask(self, mask):
		if not isinstance(mask, int) or not 0 <= mask <= ALL_LED_MASK:
			raise ValueError("LED 掩码必须是 0x000~0xFFF")

		# 每个 LEDXMD_RGBn 寄存器控制三个通道，组装后一次连续写入 0x16~0x19。
		self.disable_autonomous()
		values = bytearray(4)
		for led_index in range(12):
			if mask & (1 << led_index):
				group_index = led_index // 3
				channel_index = led_index % 3
				values[group_index] |= (
					LED_MODE_ALWAYS_ON << LED_MODE_SHIFTS[channel_index])

		self._write_registers(REG_LED_MODE_BASE, values)
		self._set_mode_registers(values)
		return mask

# 模块级默认实例采用延迟创建，导入本文件时不会立刻占用 I2C0。
_default_driver = None


def _get_default_driver():
	global _default_driver
	if _default_driver is None:
		_default_driver = ET6312B()
	return _default_driver


class ShoulderLampController:

	MODE_OFF = "off"
	MODE_ALTERNATE = "alternate"
	MODE_DIAGONAL = "diagonal"
	VALID_MODES = (MODE_OFF, MODE_ALTERNATE, MODE_DIAGONAL)
	DEFAULT_FLASH_INTERVAL_MS = 500

	# RGB0~RGB3 依次对应 D1~D3、D4~D6、D7~D9、D10~D12。
	AUTONOMOUS_MODES = {
		MODE_ALTERNATE: (
			ALL_CHANNELS_THREAD2_MODE,
			ALL_CHANNELS_THREAD2_MODE,
			ALL_CHANNELS_THREAD1_MODE,
			ALL_CHANNELS_THREAD1_MODE,
		),
		MODE_DIAGONAL: (
			ALL_CHANNELS_THREAD1_MODE,
			ALL_CHANNELS_THREAD2_MODE,
			ALL_CHANNELS_THREAD1_MODE,
			ALL_CHANNELS_THREAD2_MODE,
		),
	}

	def __init__(self, driver=None, interval_ms=500):
		self.driver = driver if driver is not None else _get_default_driver()
		self._lock = _thread.allocate_lock()
		self.mode = self.MODE_OFF
		self.flash_interval_ms = self.DEFAULT_FLASH_INTERVAL_MS
		self._revision = 0
		self.set_flash_interval(interval_ms)
		# 驱动初始化后再次明确关闭，避免接管已有对象时残留输出。
		self.driver.apply_mask(0)

	def _configure_autonomous_mode(self, mode):
		values = self.AUTONOMOUS_MODES.get(mode)
		if values is None:
			raise ValueError("自主模式不支持：{}".format(mode))
		self.driver.configure_autonomous(values)

	def set_flash_interval(self, interval_ms):
		try:
			interval_ms = int(interval_ms)
		except Exception:
			raise ValueError("肩灯延迟时间必须是正整数毫秒")
		if interval_ms <= 0:
			raise ValueError("肩灯延迟时间必须大于 0")
		self._lock.acquire()
		try:
			self.flash_interval_ms = interval_ms
		finally:
			self._lock.release()
		return interval_ms

	def set_brightness_percent(self, brightness_percent):
		self._lock.acquire()
		try:
			return self.driver.set_brightness_percent(brightness_percent)
		finally:
			self._lock.release()

	def get_brightness_percent(self):
		self._lock.acquire()
		try:
			return self.driver.get_brightness_percent()
		finally:
			self._lock.release()

	def set_mode(self, mode):
		if mode not in self.VALID_MODES:
			raise ValueError("不支持的肩灯模式：{}".format(mode))
		self._lock.acquire()
		try:
			was_changed = mode != self.mode
			if mode == self.MODE_OFF:
				self.driver.apply_mask(0)
			else:
				self._configure_autonomous_mode(mode)
			self.mode = mode
			if was_changed:
				self._revision += 1
			return was_changed
		finally:
			self._lock.release()

	def cycle_mode(self):
		self._lock.acquire()
		try:
			next_mode = (self.MODE_ALTERNATE
						 if self.mode == self.MODE_OFF else self.MODE_OFF)
		finally:
			self._lock.release()
		return next_mode if self.set_mode(next_mode) else next_mode

	def get_mode(self):
		self._lock.acquire()
		try:
			return self.mode
		finally:
			self._lock.release()

	def get_status_snapshot(self):
		self._lock.acquire()
		try:
			return {
				"revision": self._revision,
				"mode": self.mode,
				"fall_alarm_active": False,
			}
		finally:
			self._lock.release()

	def stop(self):
		return self.set_mode(self.MODE_OFF)


def initialize_shoulder_lamp(attempts=SHOULDER_LAMP_INIT_ATTEMPTS,
							 retry_delay_ms=SHOULDER_LAMP_INIT_RETRY_MS):
	attempts = max(1, int(attempts))
	retry_delay_ms = max(0, int(retry_delay_ms))
	for attempt in range(1, attempts + 1):
		try:
			controller = ShoulderLampController()
			print("[肩灯] 第{}/{}次初始化成功".format(attempt, attempts))
			return controller
		except Exception as error:
			print("[肩灯] 第{}/{}次初始化失败：{}".format(
				attempt, attempts, error))
			if attempt < attempts and retry_delay_ms:
				utime.sleep_ms(retry_delay_ms)
	print("[肩灯] 连续{}次初始化失败，肩灯功能已跳过，其余服务继续启动".format(
		attempts))
	return None
