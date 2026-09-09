# -*- coding: utf-8 -*-
"""EC800M ADC0 电池电压采集、滤波和电量换算。

硬件分压：R52=220kΩ，R53=82kΩ，ADC 节点并联 100nF 电容。
QuecPython 的 ADC.read() 返回值单位为 mV，本模块将其还原为电池端电压。

本模块不创建线程，也不在采样时阻塞主循环。调用方应在主循环中周期调用
BatteryMonitor.tick()；开机阶段自动快速采样，稳定运行后每秒采样一次。
"""

import utime

try:
    from misc import ADC
except Exception:
    # 非 EC800M 环境允许通过构造参数注入 ADC 对象进行静态测试。
    ADC = None


# 分压电阻参数，单位为 kΩ；电池端到 ADC 节点为 R52，节点到地为 R53。
DIVIDER_TOP_KOHM = 220
DIVIDER_BOTTOM_KOHM = 82
DIVIDER_TOTAL_KOHM = DIVIDER_TOP_KOHM + DIVIDER_BOTTOM_KOHM

# 开机快速采样和运行期采样参数。
BOOT_SAMPLE_COUNT = 9
BOOT_SAMPLE_INTERVAL_MS = 20
RUN_SAMPLE_INTERVAL_MS = 1000
RUN_MEDIAN_WINDOW = 5

# EMA：新值占 1/4，旧值占 3/4，抑制 LED、PTT 等负载瞬态造成的电压跳变。
EMA_NEW_WEIGHT = 1
EMA_TOTAL_WEIGHT = 4

# 分段放电曲线，电压单位 mV，电量单位 %；电压必须按升序排列。
# 该表依据用户提供的 CC 放电曲线估算，最终应使用整机实测数据校准。
DISCHARGE_POINTS = (
    (3200, 0),
    (3420, 5),
    (3500, 6),
    (3570, 10),
    (3650, 20),
    (3690, 30),
    (3720, 40),
    (3760, 50),
    (3810, 60),
    (3880, 70),
    (3950, 80),
    (4000, 85),
    (4100, 95),
    (4200, 100),
)

# 恒流恒压充电曲线的估算点。当前充电状态脚未接入，暂不启用该表。
# 充电状态接入后，4.20V 仍按用户要求钳制为 100%。
CHARGE_POINTS = (
    (3200, 0),
    (3500, 5),
    (3700, 8),
    (3750, 10),
    (3850, 30),
    (3900, 45),
    (4000, 60),
    (4100, 80),
    (4180, 95),
    (4200, 100),
)

VOLTAGE_EMPTY_MV = 3200
VOLTAGE_FULL_MV = 4200


def _ticks_add(base_ms, delta_ms):
    """兼容不同固件的 ticks_add 接口。"""
    try:
        return utime.ticks_add(base_ms, delta_ms)
    except Exception:
        return base_ms + delta_ms


def _ticks_diff(later_ms, earlier_ms):
    """兼容不同固件的 ticks_diff 接口。"""
    try:
        return utime.ticks_diff(later_ms, earlier_ms)
    except Exception:
        return later_ms - earlier_ms


def _interpolate(points, voltage_mv):
    """在分段曲线中按电压线性插值，结果四舍五入到 1%。"""
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
    """ADC0 电池监测状态机。"""

    def __init__(self, adc=None, channel=None,
                 divider_top_kohm=DIVIDER_TOP_KOHM,
                 divider_bottom_kohm=DIVIDER_BOTTOM_KOHM):
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
            # 注入测试 ADC 时通常直接忽略 channel。
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
        self.charging_state = None
        self.last_error = None
        self._last_reported_percent = None

        self._open_adc()

    def _open_adc(self):
        """打开 ADC0；官方 ADC 对象通常使用 open() 无参数接口。"""
        opener = getattr(self.adc, "open", None)
        if opener is not None:
            result = opener()
            if result not in (None, 0):
                raise OSError("ADC打开失败，返回值：{}".format(result))
        self._adc_open = True

    def close(self):
        """关闭 ADC 资源。"""
        closer = getattr(self.adc, "close", None)
        if closer is not None and self._adc_open:
            closer()
        self._adc_open = False

    def _read_adc_mv(self):
        """通过官方 ADC0 接口读取节点电压，单位 mV。"""
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
        """按220k/82k分压比例，将ADC节点电压换算为电池电压。"""
        adc_mv = int(adc_mv)
        if adc_mv < 0:
            raise ValueError("ADC电压不能为负数")
        # 加半个除数实现整数四舍五入，避免浮点运算占用资源。
        return ((adc_mv * self.divider_total_kohm) +
                self.divider_bottom_kohm // 2) // self.divider_bottom_kohm

    def _trimmed_average(self, values):
        """去掉一个最大值和一个最小值后求平均。"""
        ordered = sorted(values)
        if len(ordered) <= 2:
            return sum(ordered) // len(ordered)
        middle = ordered[1:-1]
        return (sum(middle) + len(middle) // 2) // len(middle)

    def _median(self, values):
        """返回小窗口的中位数。"""
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    def _curve_points(self):
        """根据充电状态选择电压曲线；未知状态暂按放电曲线处理。"""
        if self.charging_state is True:
            return CHARGE_POINTS
        return DISCHARGE_POINTS

    def voltage_to_percent(self, voltage_mv):
        """将电池电压换算为整数电量百分比。"""
        voltage_mv = int(voltage_mv)
        if voltage_mv >= VOLTAGE_FULL_MV:
            return 100
        if voltage_mv <= VOLTAGE_EMPTY_MV:
            return 0
        return _interpolate(self._curve_points(), voltage_mv)

    def set_charging_state(self, state):
        """设置充电状态：True=充电，False=未充电，None=未知。"""
        if state not in (True, False, None):
            raise ValueError("充电状态只能是 True、False 或 None")
        self.charging_state = state
        if self.filtered_voltage_mv is None:
            return self.percent
        self.percent = self.voltage_to_percent(self.filtered_voltage_mv)
        self._last_reported_percent = None
        return self.percent

    def _update_filtered_voltage(self, voltage_mv, boot=False):
        """更新启动去极值平均或运行期中位数+EMA。"""
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
        self.percent = self.voltage_to_percent(self.filtered_voltage_mv)
        changed = self.percent != self._last_reported_percent
        self._last_reported_percent = self.percent
        return self.percent if changed else None

    def tick(self, now_ms=None):
        """由主循环调用；返回变化后的百分比，否则返回 None。"""
        if now_ms is None:
            now_ms = utime.ticks_ms()
        if _ticks_diff(now_ms, self._next_sample_ms) < 0:
            return None

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
            result = self._update_filtered_voltage(battery_mv, boot=True)
            self._last_sample_interval_ms = (
                BOOT_SAMPLE_INTERVAL_MS if self._booting
                else RUN_SAMPLE_INTERVAL_MS)
        else:
            result = self._update_filtered_voltage(battery_mv, boot=False)
            self._last_sample_interval_ms = RUN_SAMPLE_INTERVAL_MS

        self._next_sample_ms = _ticks_add(
            now_ms, self._last_sample_interval_ms)
        return result

    def get_voltage_mv(self):
        """返回当前EMA滤波后的电池电压，尚未就绪时返回 None。"""
        return self.filtered_voltage_mv

    def get_percent(self):
        """返回当前整数电量，尚未完成首次采样时返回 None。"""
        return self.percent

    def is_ready(self):
        """是否已经完成开机首次滤波。"""
        return not self._booting and self.percent is not None
