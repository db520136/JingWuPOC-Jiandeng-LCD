# -*- coding: utf-8 -*-
from usr.i2c_bus import get_i2c0
try:
	from machine import ExtInt
except Exception:
	ExtInt = None
SC7A20H_ADDR_SDO_GND = 0x18
SC7A20H_ID = 0x11
SC7A20H_AUTO_INCREMENT = 0x80
I2C_READ_DELAY = 0
REG_WHO_AM_I = 0x0F
REG_CTRL0 = 0x1F
REG_CTRL1 = 0x20
REG_CTRL2 = 0x21
REG_CTRL3 = 0x22
REG_CTRL4 = 0x23
REG_CTRL5 = 0x24
REG_CTRL6 = 0x25
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
	def __init__(self, address=SC7A20H_ADDR_SDO_GND, i2c=None,
				 scale=SCALE_4G):
		if not 0 <= address <= 0x7F:
			raise ValueError("I2C 地址必须是 0x00~0x7F 的 7 位地址")
		if scale not in (SCALE_2G, SCALE_4G, SCALE_8G, SCALE_16G):
			raise ValueError("量程必须是 SCALE_2G/4G/8G/16G")
		self.address = address
		self.scale = scale
		self.i2c = i2c if i2c is not None else get_i2c0()
		self.initialized = False
	def _i2c_write(self, payload):
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
	def who_am_i(self):
		return self.read_register(REG_WHO_AM_I)
	def initialize(self):
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
		self._write_register(REG_CTRL6, 0x60 if enabled else 0x00)
		if not enabled:
			self.read_register(REG_AOI1_SRC)
			self.read_register(REG_AOI2_SRC)
	def set_double_click_interrupt_enabled(self, enabled):
		# 先屏蔽输出并停止检测，清除旧锁存后再恢复，避免 INT1 持续为高。
		self._write_register(REG_CTRL3, 0x00)
		self._write_register(REG_CLICK_CTRL, 0x00)
		self.read_register(REG_CLICK_SRC)
		if enabled:
			self._write_register(REG_CLICK_CTRL, 0x1F)
			self.read_register(REG_CLICK_SRC)
			self._write_register(REG_CTRL3, 0x80)
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
		if not 0 <= duration <= 127:
			raise ValueError("自由落体 duration 必须是 0~127")
		self._write_register(REG_AOI1_THS, self._threshold_code(threshold_mg))
		self._write_register(REG_AOI1_DURATION, duration)
		# AOI=1、X/Y/Z 低事件同时成立；适合自由落体检测。
		self._write_register(REG_AOI1_CFG, 0x95)
	def config_impact(self, threshold_mg=1800, duration=2):
		if not 0 <= duration <= 127:
			raise ValueError("冲击 duration 必须是 0~127")
		self._write_register(REG_AOI2_THS, self._threshold_code(threshold_mg))
		self._write_register(REG_AOI2_DURATION, duration)
		# AOI=0 为或逻辑，六个高/低方向都启用，避免漏掉反向冲击。
		self._write_register(REG_AOI2_CFG, 0x3F)
	def config_double_click(self):
		self._write_register(REG_CLICK_CTRL, 0x1F)
		self._write_register(REG_CLICK_COEFF1, 0x12)
		self._write_register(REG_CLICK_COEFF2, 0xA2)
		self._write_register(REG_CLICK_COEFF3, 0x04)
		self._write_register(REG_CLICK_COEFF4, 0x12)
	def read_click_source(self):
		return self.read_register(REG_CLICK_SRC)
class SC7A20HDoubleClickAdapter:
	def __init__(self, sensor, on_interrupt, int1_pin=25,
				 double_click_enabled=True):
		self.sensor = sensor
		self.on_interrupt = on_interrupt
		self.int1_pin = int1_pin
		self.irq = None
		self.started = False
		self.double_click_enabled = bool(double_click_enabled)
	def _on_int1(self, *args):
		self.on_interrupt()
	def start(self):
		if self.started:
			return True
		self.sensor.initialize()
		self.sensor.set_fall_interrupts_enabled(False)
		self.sensor.set_double_click_interrupt_enabled(
			self.double_click_enabled)
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
