# -*- coding: utf-8 -*-
"""ET6312B 十二通道 LED 测试驱动。

适用硬件：EC800M + ET6312B，使用 QuecPython 的 I2C0 接口。

原理图中 ADDR 通过 R10 下拉到 GND，因此数据手册中的写/读地址字节
分别为 0x90/0x91；QuecPython 使用 7 位从机地址，所以填写 0x48。

对外提供三个测试接口：
    all_on()                  12 个灯全部点亮
    all_off()                 12 个灯全部熄灭
    set_led(number, state)    指定 D1~D12 点亮或熄灭

业务控制接口：
    apply_mask(mask)           一次性更新 D1~D12
    ShoulderLampController     肩灯关闭、交替闪和对角闪状态机
"""

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
REG_LED_MODE_BASE = 0x16
REG_LED_CURRENT_BASE = 0x1A
REG_EXTERNAL_PWM = 0x26

# CHIPCTR：关闭自主呼吸、使用内部时钟、退出软件关机、最大电流 24mA。
CHIP_CONTROL_NORMAL = 0x10

# LEDXMD_RGBn 中三个通道均设为 Always ON：001 001 01 = 0x25。
ALL_CHANNELS_ON_MODE = 0x25
ALL_CHANNELS_OFF_MODE = 0x00

# imax_sel=0 时，0x4F 对应 10mA；可在创建驱动时传入其他 0x00~0xBF 值。
DEFAULT_CURRENT_CODE = 0x4F

# 每组三个 LED 在 LEDXMD_RGBn 寄存器中的字段位置和掩码。
LED_MODE_SHIFTS = (0, 2, 5)
LED_MODE_MASKS = (0x03, 0x1C, 0xE0)
LED_MODE_ALWAYS_ON = 0x01

# D1~D12 在 12 位输出掩码中的对应位；D1 使用最低位。
ALL_LED_MASK = 0x0FFF


class ET6312B:
    """ET6312B 的 QuecPython I2C0 驱动。"""

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
        self.initialize()

    def _set_mode_registers(self, values):
        """更新本地模式缓存，兼容不支持 bytearray 切片赋值的固件。"""
        for index in range(4):
            self._mode_registers[index] = values[index]

    def _write_registers(self, start_register, values):
        """从指定寄存器开始连续写入一个或多个字节。"""
        register_buffer = bytearray((start_register,))
        data_buffer = values if isinstance(values, bytearray) else bytearray(values)
        result = self.i2c.write(
            self.address,
            register_buffer,
            len(register_buffer),
            data_buffer,
            len(data_buffer),
        )
        # QuecPython I2C.write 正常返回 0；部分固件可能不返回值。
        if result not in (0, None):
            raise OSError("ET6312B I2C 写入失败，返回值：{}".format(result))
        return result

    def _write_register(self, register, value):
        """写入一个 8 位寄存器。"""
        return self._write_registers(register, bytearray((value,)))

    def initialize(self):
        """初始化芯片，并确保上电后十二个通道均处于关闭状态。"""
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

    def all_on(self):
        """接口 1：点亮 D1~D12 全部十二个灯。"""
        return self.apply_mask(ALL_LED_MASK)

    def all_off(self):
        """接口 2：熄灭 D1~D12 全部十二个灯。"""
        return self.apply_mask(0)

    def apply_mask(self, mask):
        """一次性更新十二个通道，mask 的 bit0~bit11 对应 D1~D12。"""
        if not isinstance(mask, int) or not 0 <= mask <= ALL_LED_MASK:
            raise ValueError("LED 掩码必须是 0x000~0xFFF")

        # 每个 LEDXMD_RGBn 寄存器控制三个通道，组装后一次连续写入 0x16~0x19。
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

    def set_led(self, number, state):
        """接口 3：设置 D1~D12 中指定灯的亮灭状态。

        Args:
            number: 灯编号，取值 1~12，对应原理图中的 D1~D12。
            state: True/1 表示点亮，False/0 表示熄灭。
        """
        if not isinstance(number, int) or not 1 <= number <= 12:
            raise ValueError("灯编号必须是 1~12")
        if state not in (True, False, 1, 0):
            raise ValueError("state 必须是 True/False 或 1/0")

        led_index = number - 1
        group_index = led_index // 3
        channel_index = led_index % 3
        shift = LED_MODE_SHIFTS[channel_index]
        mask = LED_MODE_MASKS[channel_index]

        register_value = self._mode_registers[group_index] & (~mask & 0xFF)
        if bool(state):
            register_value |= LED_MODE_ALWAYS_ON << shift

        self._write_register(REG_LED_MODE_BASE + group_index, register_value)
        self._mode_registers[group_index] = register_value


# 模块级默认实例采用延迟创建，导入本文件时不会立刻占用 I2C0。
_default_driver = None


def _get_default_driver():
    """首次调用公开接口时创建默认 I2C0 驱动实例。"""
    global _default_driver
    if _default_driver is None:
        _default_driver = ET6312B()
    return _default_driver


def all_on():
    """点亮 D1~D12 全部十二个灯。"""
    return _get_default_driver().all_on()


def all_off():
    """熄灭 D1~D12 全部十二个灯。"""
    return _get_default_driver().all_off()


def set_led(number, state):
    """设置指定编号的灯；number 为 1~12，state 为 True/False。"""
    return _get_default_driver().set_led(number, state)


def apply_mask(mask):
    """一次性更新十二个灯；bit0~bit11 分别对应 D1~D12。"""
    return _get_default_driver().apply_mask(mask)


class ShoulderLampController:
    """肩灯业务控制器；普通闪烁由主循环 tick() 非阻塞驱动。

    传感器服务线程也会调用本类，因此模式、报警状态和每次 I2C 写入都
    使用同一把锁保护。跌倒报警优先于普通模式，只有选择 ``off`` 才会取消。
    """

    MODE_OFF = "off"
    MODE_ALTERNATE = "alternate"
    MODE_DIAGONAL = "diagonal"
    VALID_MODES = (MODE_OFF, MODE_ALTERNATE, MODE_DIAGONAL)
    DEFAULT_FLASH_INTERVAL_MS = 500
    FALL_ALARM_INTERVAL_MS = 200

    # D1~D6 为蓝灯，D7~D12 为红灯。
    ALTERNATE_MASKS = (0xFC0, 0x03F)
    DIAGONAL_MASKS = (0x1C7, 0xE38)

    def __init__(self, driver=None, interval_ms=500):
        self.driver = driver if driver is not None else _get_default_driver()
        self._lock = _thread.allocate_lock()
        self.mode = self.MODE_OFF
        self.phase = 0
        self.flash_interval_ms = self.DEFAULT_FLASH_INTERVAL_MS
        self._next_toggle_ms = None
        self._fall_alarm_active = False
        self._fall_alarm_phase = 0
        self._next_alarm_toggle_ms = None
        # 每次明确选择 off 都递增，供跌倒服务区分“普通模式本来就是关闭”
        # 和“用户刚刚执行了取消当前跌倒事件”。
        self._fall_cancel_version = 0
        self.set_flash_interval(interval_ms)
        # 驱动初始化后再次明确关闭，避免接管已有对象时残留输出。
        self.driver.apply_mask(0)

    def _now_ms(self):
        return utime.ticks_ms()

    def _ticks_add(self, base_ms, delta_ms):
        try:
            return utime.ticks_add(base_ms, delta_ms)
        except Exception:
            return base_ms + delta_ms

    def _ticks_diff(self, later_ms, earlier_ms):
        try:
            return utime.ticks_diff(later_ms, earlier_ms)
        except Exception:
            return later_ms - earlier_ms

    def _phase_mask(self):
        if self.mode == self.MODE_ALTERNATE:
            return self.ALTERNATE_MASKS[self.phase]
        if self.mode == self.MODE_DIAGONAL:
            return self.DIAGONAL_MASKS[self.phase]
        return 0

    def _apply_phase(self):
        # 每个阶段只做一次连续寄存器写入，避免逐通道更新造成中间状态。
        self.driver.apply_mask(self._phase_mask())

    def _apply_alarm_phase(self):
        # 报警使用全亮/全灭两种完整掩码，避免逐通道产生中间状态。
        self.driver.apply_mask(ALL_LED_MASK if self._fall_alarm_phase else 0)

    def set_flash_interval(self, interval_ms):
        """设置普通模式闪烁半周期，单位毫秒。"""
        try:
            interval_ms = int(interval_ms)
        except Exception:
            raise ValueError("肩灯延迟时间必须是正整数毫秒")
        if interval_ms <= 0:
            raise ValueError("肩灯延迟时间必须大于 0")
        self._lock.acquire()
        try:
            self.flash_interval_ms = interval_ms
            if self.mode != self.MODE_OFF and not self._fall_alarm_active:
                self._next_toggle_ms = self._ticks_add(
                    self._now_ms(), self.flash_interval_ms)
        finally:
            self._lock.release()
        return interval_ms

    def set_mode(self, mode):
        """切换模式；即使已经是 off，也会清除当前跌倒报警。"""
        if mode not in self.VALID_MODES:
            raise ValueError("不支持的肩灯模式：{}".format(mode))
        self._lock.acquire()
        try:
            was_changed = mode != self.mode or self._fall_alarm_active
            if mode == self.MODE_OFF:
                # off 是业务上的明确取消动作，报警和普通闪烁一并停止。
                self._fall_cancel_version += 1
                self._fall_alarm_active = False
                self._next_alarm_toggle_ms = None
                self._next_toggle_ms = None
                self.phase = 0
                self.driver.apply_mask(0)
                self.mode = mode
                return was_changed

            self.mode = mode
            self.phase = 0
            if self._fall_alarm_active:
                # 报警期间只记录用户选择，报警灯效保持优先。
                self._next_toggle_ms = None
                return was_changed

            self._apply_phase()
            self._next_toggle_ms = self._ticks_add(
                self._now_ms(), self.flash_interval_ms)
            return was_changed
        finally:
            self._lock.release()

    def cycle_mode(self):
        """按 off -> alternate -> diagonal -> off 循环切换。"""
        self._lock.acquire()
        try:
            index = self.VALID_MODES.index(self.mode)
            next_mode = self.VALID_MODES[(index + 1) % len(self.VALID_MODES)]
        finally:
            self._lock.release()
        return next_mode if self.set_mode(next_mode) else next_mode

    def start_fall_alarm(self):
        """启动跌倒报警；全灯以 200ms 半周期同步快闪。"""
        self._lock.acquire()
        try:
            if self._fall_alarm_active:
                return False
            self._fall_alarm_active = True
            self._fall_alarm_phase = 1
            self._next_alarm_toggle_ms = self._ticks_add(
                self._now_ms(), self.FALL_ALARM_INTERVAL_MS)
            self._next_toggle_ms = None
            self._apply_alarm_phase()
            return True
        finally:
            self._lock.release()

    def clear_fall_alarm(self):
        """清除跌倒报警并恢复当前普通模式。"""
        self._lock.acquire()
        try:
            if not self._fall_alarm_active:
                return False
            self._fall_alarm_active = False
            self._next_alarm_toggle_ms = None
            self.phase = 0
            if self.mode == self.MODE_OFF:
                self._next_toggle_ms = None
                self.driver.apply_mask(0)
            else:
                self._apply_phase()
                self._next_toggle_ms = self._ticks_add(
                    self._now_ms(), self.flash_interval_ms)
            return True
        finally:
            self._lock.release()

    def is_fall_alarm_active(self):
        self._lock.acquire()
        try:
            return self._fall_alarm_active
        finally:
            self._lock.release()

    def get_mode(self):
        """线程安全返回当前用户选择的普通肩灯模式。"""
        self._lock.acquire()
        try:
            return self.mode
        finally:
            self._lock.release()

    def get_fall_cancel_version(self):
        """返回用户明确选择 off 的递增序号。"""
        self._lock.acquire()
        try:
            return self._fall_cancel_version
        finally:
            self._lock.release()

    def tick(self, now_ms=None):
        """由 LCD 主循环周期调用，到时切换一次 LED 阶段。"""
        self._lock.acquire()
        try:
            if now_ms is None:
                now_ms = self._now_ms()
            if self._fall_alarm_active:
                if (self._next_alarm_toggle_ms is None or
                        self._ticks_diff(now_ms, self._next_alarm_toggle_ms) < 0):
                    return False
                self._fall_alarm_phase = 1 - self._fall_alarm_phase
                self._apply_alarm_phase()
                self._next_alarm_toggle_ms = self._ticks_add(
                    now_ms, self.FALL_ALARM_INTERVAL_MS)
                return True

            if self.mode == self.MODE_OFF or self._next_toggle_ms is None:
                return False
            if self._ticks_diff(now_ms, self._next_toggle_ms) < 0:
                return False
            self.phase = 1 - self.phase
            self._apply_phase()
            # 使用当前时间重算，避免主循环偶发延迟时连续补写多次 I2C。
            self._next_toggle_ms = self._ticks_add(
                now_ms, self.flash_interval_ms)
            return True
        finally:
            self._lock.release()

    def stop(self):
        """停止闪烁、清除报警并关闭全部 LED。"""
        return self.set_mode(self.MODE_OFF)


def run_test(interval_ms=1000, single_led_ms=300):
    """运行全亮、全灭及 D1~D12 逐个点亮的硬件测试。"""
    driver = _get_default_driver()

    print("ET6312B：12 个灯全亮")
    driver.all_on()
    utime.sleep_ms(interval_ms)

    print("ET6312B：12 个灯全灭")
    driver.all_off()
    utime.sleep_ms(interval_ms)

    for number in range(1, 13):
        print("ET6312B：测试 D{}".format(number))
        driver.set_led(number, True)
        utime.sleep_ms(single_led_ms)
        driver.set_led(number, False)
        print("第{}号灯亮灭".format(number))
        utime.sleep(1)
    print("ET6312B：测试完成")
    
#et6312B_obj = ET6312B()
# 测试入口已屏蔽，防止 LCD 导入该模块时自动执行全灯测试。
# if __name__ == "__main__":
#     run_test()
