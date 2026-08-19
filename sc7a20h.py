# -*- coding: utf-8 -*-
"""SC7A20H 加速度计驱动与双击/跌倒业务服务。

SC7A20H 与 ET6312B 共用 EC800M 硬件 I2C0。底层驱动只负责寄存器和
采样数据，``SC7A20HService`` 在独立线程中处理 GPIO 中断、双击切换和
跌倒状态机，避免把传感器 I2C 操作塞进 LCD/LVGL 主循环。

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
ODR_4434HZ = 0xB0
LOW_POWER_ENABLE = 0x08
ENABLE_ALL_AXES = 0x07

# CTRL_REG0：HR=1 使能高性能/增强模式。
HIGH_PERFORMANCE_ENABLE = 0x01

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
        self._write_register(REG_CLICK_COEFF2, 0x9A)
        self._write_register(REG_CLICK_COEFF3, 0x04)
        self._write_register(REG_CLICK_COEFF4, 0x12)

    def read_click_source(self):
        return self.read_register(REG_CLICK_SRC)

    def read_aoi1_source(self):
        return self.read_register(REG_AOI1_SRC)

    def read_aoi2_source(self):
        return self.read_register(REG_AOI2_SRC)


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
                 extint_factory=None):
        self.controller = controller
        self.sensor = sensor if sensor is not None else SC7A20H(
            address=address, scale=SCALE_4G)
        self.int1_pin_number = int1_pin
        self.int2_pin_number = int2_pin
        self.sample_interval_ms = max(5, int(sample_interval_ms))
        self.extint_factory = extint_factory if extint_factory is not None else ExtInt
        self._running = False
        self._thread_started = False
        self._int1_pending = False
        self._int2_pending = False
        self._irq_objects = []
        self._sensor_error_reported = False
        self._last_double_click_ms = None
        self._freefall_started_ms = None
        self._fast_fall_until_ms = None
        self._hardware_impact = False
        self._baseline = None
        self._baseline_stable_since_ms = None
        self._previous_vector = None
        self._candidate_started_ms = None
        self._candidate_reference = None
        self._candidate_requires_posture = True
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
        except Exception as error:
            print("[加速度] GPIO 中断初始化失败，将使用轮询：{}".format(error))
            self._irq_objects = []
            return False

    def _on_int1(self, *args):
        # IRQ 中禁止 I2C 和业务调用，只记录待处理事件。
        self._int1_pending = True

    def _on_int2(self, *args):
        self._int2_pending = True

    def start(self):
        """初始化传感器、注册中断并启动服务线程。"""
        if self._running:
            return True
        self.sensor.initialize()
        self._setup_irqs()
        self._running = True
        try:
            _thread.start_new_thread(self._run, ())
            self._thread_started = True
        except Exception:
            self._running = False
            self._thread_started = False
            raise
        print("[加速度] SC7A20H 服务已启动，INT1=GPIO25/Pin17，INT2=GPIO26/Pin18")
        return True

    def set_fall_detection_enabled(self, enabled):
        """切换跌倒检测；关闭时清空当前检测状态。"""
        self.fall_detection_enabled = bool(enabled)
        if not self.fall_detection_enabled:
            self._reset_fall_state()

    def stop(self):
        """停止线程并禁用已注册的 GPIO 中断。"""
        self._running = False
        for irq in self._irq_objects:
            try:
                irq.disable()
            except Exception:
                pass
        self._irq_objects = []

    def _consume_sources(self, now_ms):
        """在线程上下文读取锁存源寄存器，处理中断事件。"""
        poll_all = not self._irq_objects
        int1 = self._int1_pending
        int2 = self._int2_pending
        self._int1_pending = False
        self._int2_pending = False

        try:
            if int1 or poll_all:
                click_source = self.sensor.read_click_source()
                if (click_source & 0x0F) == 2:
                    if (self._last_double_click_ms is None or
                            self._ticks_diff(now_ms, self._last_double_click_ms) >=
                            self.DOUBLE_CLICK_GUARD_MS):
                        self._last_double_click_ms = now_ms
                        mode = self.controller.cycle_mode()
                        print("[加速度] 双击，肩灯模式切换为 {}".format(mode))
            if int2 or poll_all:
                aoi1_source = self.sensor.read_aoi1_source()
                aoi2_source = self.sensor.read_aoi2_source()
                if aoi1_source & 0x40:
                    self._fast_fall_until_ms = self._ticks_add(
                        now_ms, self.FAST_FALL_WINDOW_MS)
                if aoi2_source & 0x40:
                    self._hardware_impact = True
        except Exception as error:
            if not self._sensor_error_reported:
                print("[加速度] 中断源读取失败：{}".format(error))
                self._sensor_error_reported = True

    def _reset_fall_state(self, keep_baseline=False):
        self._freefall_started_ms = None
        self._fast_fall_until_ms = None
        self._hardware_impact = False
        self._candidate_started_ms = None
        self._candidate_reference = None
        self._candidate_requires_posture = True
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
        print("[跌倒] 已确认，肩灯进入全灯快闪")
        try:
            self.controller.start_fall_alarm()
        except Exception as error:
            print("[跌倒] 肩灯报警启动失败：{}".format(error))

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
                self._candidate_requires_posture = True
            elif posture_change:
                # 没有明显自由落体的缓慢跌倒路径。
                self._candidate_started_ms = now_ms
                self._candidate_reference = self._baseline
                self._candidate_requires_posture = True
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
        next_sample_ms = utime.ticks_ms()
        while self._running:
            now_ms = utime.ticks_ms()
            if self._ticks_diff(now_ms, next_sample_ms) >= 0:
                next_sample_ms = self._ticks_add(
                    now_ms, self.sample_interval_ms)
                try:
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
                    if not self.fall_detection_enabled:
                        self._reset_fall_state(keep_baseline=True)
                    elif self._fall_confirmed:
                        if not self.controller.is_fall_alarm_active():
                            self._reset_fall_state(keep_baseline=True)
                    else:
                        values = self.sensor.read_acceleration_g()
                        self._process_sample(now_ms, values)
                    self._sensor_error_reported = False
                except Exception as error:
                    if not self._sensor_error_reported:
                        print("[加速度] 采样服务异常：{}".format(error))
                        self._sensor_error_reported = True
            utime.sleep_ms(5)


def run_4270hz_sampling_test(interval_ms=100, sample_count=None,
                             address=SC7A20H_ADDR_SDO_GND,
                             scale=SCALE_4G, i2c=None):
    """以芯片最高速档采集三轴数据，并按 TSV 格式输出 G 值。

    SC7A20H 说明书把 ODR=1011 标为 4.434kHz，本方法使用该档位作为
    4.27kHz 高速采集测试。默认每 100ms 读取最新一帧，sample_count=None
    时持续运行，按 Ctrl+C 停止。测试结束后恢复调用前的相关寄存器。

    该方法输出芯片物理 X/Y/Z 轴，不执行项目业务使用的 Y/-X/-Z 坐标变换。
    测试时不要同时运行 SC7A20HService，以免临时配置影响中断和跌倒检测。
    """
    interval_ms = int(interval_ms)
    if interval_ms <= 0:
        raise ValueError("interval_ms 必须大于 0")
    if sample_count is not None:
        sample_count = int(sample_count)
        if sample_count <= 0:
            raise ValueError("sample_count 必须大于 0 或为 None")

    sensor = SC7A20H(address=address, scale=scale, i2c=i2c)
    device_id = sensor.who_am_i()
    if device_id != SC7A20H_ID:
        raise OSError(
            "SC7A20H ID 不匹配：期望 0x{:02X}，实际 0x{:02X}".format(
                SC7A20H_ID, device_id))

    saved_ctrl0 = sensor.read_register(REG_CTRL0)
    saved_ctrl1 = sensor.read_register(REG_CTRL1)
    saved_ctrl4 = sensor.read_register(REG_CTRL4)

    try:
        # 先关断输出，再切换到无降频的高性能 4.434kHz 档位。
        sensor.write_register(REG_CTRL1, 0x00)
        sensor.write_register(REG_CTRL0, HIGH_PERFORMANCE_ENABLE)
        sensor.write_register(REG_CTRL4, BDU_ENABLE | scale)
        sensor.write_register(REG_CTRL1, ODR_4434HZ | ENABLE_ALL_AXES)
        utime.sleep_ms(10)

        print("time_ms\tx_g\ty_g\tz_g")
        started_ms = utime.ticks_ms()
        next_sample_ms = started_ms
        samples_read = 0

        while sample_count is None or samples_read < sample_count:
            now_ms = utime.ticks_ms()
            try:
                wait_ms = utime.ticks_diff(next_sample_ms, now_ms)
            except Exception:
                wait_ms = next_sample_ms - now_ms
            if wait_ms > 0:
                utime.sleep_ms(wait_ms)

            sample_time_ms = utime.ticks_ms()
            raw_x, raw_y, raw_z = sensor.read_raw()
            x_g = raw_x * sensor.scale_factor
            y_g = raw_y * sensor.scale_factor
            z_g = raw_z * sensor.scale_factor
            try:
                elapsed_ms = utime.ticks_diff(sample_time_ms, started_ms)
            except Exception:
                elapsed_ms = sample_time_ms - started_ms
            print("{}\t{:.6f}\t{:.6f}\t{:.6f}".format(
                elapsed_ms, x_g, y_g, z_g))

            samples_read += 1
            try:
                next_sample_ms = utime.ticks_add(
                    next_sample_ms, interval_ms)
            except Exception:
                next_sample_ms += interval_ms
    finally:
        # 尽量恢复全部寄存器；单个恢复失败不能阻止后续寄存器恢复。
        restore_error = None
        for register, value in (
                (REG_CTRL1, 0x00),
                (REG_CTRL0, saved_ctrl0),
                (REG_CTRL4, saved_ctrl4),
                (REG_CTRL1, saved_ctrl1)):
            try:
                sensor.write_register(register, value)
            except Exception as error:
                if restore_error is None:
                    restore_error = error
        if restore_error is not None:
            raise restore_error


def run_test(interval_ms=100):
    """手动测试入口；导入本模块时不会自动执行。"""
    sensor = get_sensor()
    print("SC7A20H 初始化成功，I2C0，地址 0x{:02X}".format(sensor.address))
    while True:
        print(sensor.read_data())
        utime.sleep_ms(interval_ms)

#run_test()
# 测试入口保持屏蔽；业务由 main.py 创建 SC7A20HService 后自动运行。
# if __name__ == "__main__":
#     run_test()
