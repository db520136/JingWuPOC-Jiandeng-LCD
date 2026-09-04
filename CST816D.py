# -*- coding: utf-8 -*-
"""主线程轮询版 CST816D 触摸驱动（I2C1/GPIO11/GPIO15）。"""

import math
import utime
from machine import I2C, Pin, ExtInt

CST816D_I2C_ADDRESS = 0x15
CST816D_IRQ_GPIO = Pin.GPIO11
CST816D_RESET_GPIO = Pin.GPIO15
CST816D_I2C_BUS = I2C.I2C1
CST816D_I2C_MODE = I2C.FAST_MODE
CST816D_DYNAMIC_SAMPLE_HZ = 100
CST816D_DYNAMIC_SAMPLE_INTERVAL_MS = 1000 // CST816D_DYNAMIC_SAMPLE_HZ
CST816D_RELEASE_CONFIRM_MS = 2000
# Time-based release handling is independent of the variable main-loop rate.
# The first 200ms suppresses a noisy transition; after 1s without a touch the
# LCD fast path may resume deferred work, while the 2s confirmation below still
# controls IRQ re-enable and dynamic-session completion.
CST816D_RELEASE_DEBOUNCE_MS = 200
CST816D_FAST_PATH_RELEASE_MS = 1000
CST816D_SWIPE_DISTANCE_PX = 30
CST816D_FILTER_DEADZONE_PX = 3
# Keep gesture classification visible while allowing the remaining CST816D
# performance/session diagnostics to be silenced during UI tests.
CST816D_DIAGNOSTIC_LOG_ENABLED = False
# CST816D reports the touch panel in portrait coordinates (240x296), while
# ST7789 is configured for 296x240 landscape.  The panel must be rotated
# clockwise to line up with the LCD.
CST816D_RAW_HEIGHT = 296

REG_GESTURE_ID = 0x01
REG_FINGER_NUM = 0x02
REG_MOTION_MASK = 0xEC
REG_IRQ_PULSE_WIDTH = 0xED
REG_NORMAL_SCAN_PERIOD = 0xEE
REG_CHIP_ID = 0xA7
REG_PROJECT_ID = 0xA8
REG_FW_VERSION = 0xA9
REG_IRQ_CONTROL = 0xFA
REG_DIS_AUTO_SLEEP = 0xFE

GESTURE_NAMES = {
    0x00: "无手势", 0x01: "上滑", 0x02: "下滑", 0x03: "左滑",
    0x04: "右滑", 0x05: "单击", 0x0C: "长按",
}


class CST816D:
    """IRQ 只唤醒主循环；坐标 I2C 读取全部由主循环完成。"""

    def __init__(self, address=CST816D_I2C_ADDRESS,
                 swipe_distance_px=CST816D_SWIPE_DISTANCE_PX,
                 filter_deadzone_px=CST816D_FILTER_DEADZONE_PX):
        self.address = int(address)
        self.swipe_distance_px = max(1, int(swipe_distance_px))
        self.filter_deadzone_px = max(0, int(filter_deadzone_px))
        self.i2c = I2C(CST816D_I2C_BUS, CST816D_I2C_MODE)
        #self.reset_pin = Pin(CST816D_RESET_GPIO, Pin.OUT,
        #                      Pin.PULL_DISABLE, 1)
        #utime.sleep_ms(500)
        self.irq = None
        self._running = False
        self._irq_pending = False
        self._dynamic = False
        self._pressed = False
        # Coordinates are kept in both controller (portrait/raw) space and
        # LCD (landscape) space.  LVGL receives raw coordinates and applies
        # the display rotation; the UI's custom gesture code uses landscape.
        self._input_point = (0, 0)
        self._point = (0, 0)
        self._last_point = None
        self._start_point = None
        self._release_since_ms = None
        self._session_started_ms = None
        self._session_samples = 0
        # Strict fast-path statistics.  Release-confirmation polls (including
        # the controller's 2 s tail) are intentionally excluded so the
        # reported rate describes only samples obtained while a finger was
        # actually down in the LCD fast path.
        self._fast_session_started_ms = None
        self._fast_session_last_touch_ms = None
        self._fast_session_samples = 0
        self._press_generation = 0
        self._irq_count = 0
        self._last_i2c_read_us = 0
        self._last_event_flag = 0
        self._gesture_reported = False
        self._deferred_release = False
        self._gesture_callback = None
    def _write_reg(self, register, value):
        result = self.i2c.write(
            self.address, bytes([int(register) & 0xFF]), 1,
            bytearray([int(value) & 0xFF]), 1)
        if result not in (None, 0):
            raise OSError("CST816D I2C write failed: {}".format(result))

    def _read_regs(self, register, length):
        data = bytearray(int(length))
        result = self.i2c.read(
            self.address, bytes([int(register) & 0xFF]), 1,
            data, int(length), 0)
        if result not in (None, 0):
            raise OSError("CST816D I2C read failed: {}".format(result))
        return bytes(data)

    def read_register(self, register):
        return self._read_regs(register, 1)[0]

    def read_identity(self):
        return (self.read_register(REG_CHIP_ID),
                self.read_register(REG_PROJECT_ID),
                self.read_register(REG_FW_VERSION))

    def _configure(self):
        # 关闭双击，仅触摸时输出周期性低脉冲；禁止自动低功耗。
        self._write_reg(REG_MOTION_MASK, 0x00)
        self._write_reg(REG_IRQ_PULSE_WIDTH, 1)
        self._write_reg(REG_NORMAL_SCAN_PERIOD, 1)
        self._write_reg(REG_IRQ_CONTROL, 0x40)
        self._write_reg(REG_DIS_AUTO_SLEEP, 0x01)

    def _reset(self):
        self.reset_pin.write(0)
        utime.sleep_ms(10)
        self.reset_pin.write(1)
        utime.sleep_ms(100)

    def _irq_callback(self, *args):
        if self._running:
            self._irq_pending = True
            self._irq_count += 1

    def _enable_irq(self):
        if self.irq is None:
            self.irq = ExtInt(CST816D_IRQ_GPIO, ExtInt.IRQ_FALLING,
                              ExtInt.PULL_PU, self._irq_callback)
        self.irq.enable()

    def _disable_irq(self):
        if self.irq is not None:
            self.irq.disable()

    def set_gesture_callback(self, callback):
        self._gesture_callback = callback

    def start(self, callback=None):
        if self._running:
            return True
        if callback is not None:
            self._gesture_callback = callback
        chip, project, version = self.read_identity()
        if CST816D_DIAGNOSTIC_LOG_ENABLED:
            print("[CST816D] started I2C1 IRQ=GPIO11 RST=GPIO15 "
                          "chip=0x{:02X} project=0x{:02X} fw=0x{:02X} target={}Hz".format(
                              chip, project, version, CST816D_DYNAMIC_SAMPLE_HZ))
        self._configure()
        self._running = True
        self._enable_irq()
        return True

    def stop(self):
        self._running = False
        self._dynamic = False
        self._pressed = False
        self._disable_irq()
        self.irq = None

    def is_dynamic_active(self):
        return bool(self._dynamic)

    def is_fast_path_active(self):
        """Whether the LCD should keep the touch fast path enabled.

        Dynamic sampling remains active through the controller's 2s release
        confirmation, but expensive UI work is allowed again after 1s of
        continuous no-touch samples.
        """
        if not self._dynamic:
            return False
        if self._release_since_ms is None:
            return True
        try:
            return (utime.ticks_diff(utime.ticks_ms(),
                                     self._release_since_ms) <
                    CST816D_FAST_PATH_RELEASE_MS)
        except Exception:
            return True

    def is_pressed(self):
        return bool(self._pressed)

    def get_raw_point(self):
        """Return the cached landscape LCD coordinate for UI gesture logic."""
        return self._point

    def get_input_point(self):
        """Return the cached portrait/raw coordinate for LVGL input."""
        return self._input_point

    def get_press_generation(self):
        """Monotonic counter incremented for each CST816D down edge."""
        return self._press_generation

    def _read_point(self):
        try:
            started_us = utime.ticks_us()
            use_us = True
        except Exception:
            started_us = utime.ticks_ms()
            use_us = False
        try:
            data = self._read_regs(REG_FINGER_NUM, 5)
        finally:
            try:
                if use_us:
                    self._last_i2c_read_us = utime.ticks_diff(
                        utime.ticks_us(), started_us)
                else:
                    self._last_i2c_read_us = 1000 * utime.ticks_diff(
                        utime.ticks_ms(), started_us)
            except Exception:
                self._last_i2c_read_us = 0
        fingers = data[0]
        event_flag = (data[1] >> 6) & 0x03
        x = ((data[1] & 0x0F) << 8) | data[2]
        y = ((data[3] & 0x0F) << 8) | data[4]
        return fingers, event_flag, x, y

    def get_last_i2c_read_us(self):
        """Return the most recent 5-byte coordinate I2C transaction time."""
        return int(self._last_i2c_read_us)

    def _filter_point(self, point):
        if self._last_point is not None:
            if (abs(point[0] - self._last_point[0]) <= self.filter_deadzone_px and
                    abs(point[1] - self._last_point[1]) <= self.filter_deadzone_px):
                return self._last_point
        return point

    @staticmethod
    def _landscape_point(point):
        # Clockwise 90-degree rotation: (x, y) -> (H-1-y, x).
        return CST816D_RAW_HEIGHT - 1 - int(point[1]), int(point[0])

    def _classify(self):
        start = self._landscape_point(self._start_point)
        end = self._landscape_point(self._last_point)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.sqrt(dx * dx + dy * dy)
        if distance < self.swipe_distance_px:
            return "单击", dx, dy, distance
        if abs(dx) >= abs(dy):
            return ("右滑" if dx > 0 else "左滑"), dx, dy, distance
        return ("下滑" if dy > 0 else "上滑"), dx, dy, distance

    def _report_gesture_once(self):
        if self._gesture_reported or self._start_point is None:
            return
        self._gesture_reported = True
        name, dx, dy, distance = self._classify()
        print("[CST816D] 手势={} dx={} dy={} distance={:.1f}px".format(
            name, dx, dy, distance))
        if self._gesture_callback is not None:
            self._gesture_callback(name)

    def _finish_session(self):
        now = utime.ticks_ms()
        stats_started = (self._fast_session_started_ms
                         if self._fast_session_started_ms is not None
                         else self._session_started_ms)
        stats_ended = (self._fast_session_last_touch_ms
                       if self._fast_session_last_touch_ms is not None
                       else now)
        elapsed_ms = max(1, utime.ticks_diff(stats_ended, stats_started))
        sample_count = self._fast_session_samples
        frequency = ((sample_count - 1) * 1000.0 / elapsed_ms
                     if sample_count > 1 else 0.0)
        if CST816D_DIAGNOSTIC_LOG_ENABLED:
            print("[CST816D] 触摸结束：快速采样次数={} 快速时长={}ms 快速实际频率={:.1f}Hz "
                  "最后状态={} IRQ次数={}".format(
                      sample_count, elapsed_ms, frequency,
                      self._last_event_flag, self._irq_count))
        self._dynamic = False
        self._pressed = False
        self._input_point = (0, 0)
        self._point = (0, 0)
        self._start_point = None
        self._last_point = None
        self._release_since_ms = None
        self._session_started_ms = None
        self._session_samples = 0
        self._fast_session_started_ms = None
        self._fast_session_last_touch_ms = None
        self._fast_session_samples = 0
        self._gesture_reported = False
        self._deferred_release = False
        self._irq_pending = False
        if self._running:
            self._enable_irq()

    def poll_once(self):
        """主循环调用；空闲且无 IRQ 时不产生 I2C 读取。"""
        if not self._running:
            return False
        if not self._dynamic:
            if not self._irq_pending:
                return False
            self._irq_pending = False
            self._disable_irq()
            self._dynamic = True
            self._session_started_ms = utime.ticks_ms()
            self._session_samples = 0
            self._fast_session_started_ms = self._session_started_ms
            self._fast_session_last_touch_ms = None
            self._fast_session_samples = 0
            self._start_point = None
            self._last_point = None
            self._release_since_ms = None
            self._gesture_reported = False
        try:
            now = utime.ticks_ms()
            fingers, event_flag, x, y = self._read_point()
            self._session_samples += 1
            previous_event_flag = self._last_event_flag
            self._last_event_flag = event_flag
            raw_point = (int(x), int(y))
            # Event flag 00=down and 10=hold/move.  01 is release; the
            # reserved 11 value is treated as non-touch for safety.
            if fingers and event_flag in (0, 2):
                new_press = (event_flag == 0 and
                             (self._start_point is None or
                              previous_event_flag != 0))
                if new_press:
                    self._press_generation += 1
                # A new press may arrive during the 2-second release
                # confirmation window while the IRQ remains disabled.  Start
                # a fresh logical session so consecutive keypad taps do not
                # share the first tap's gesture/click state.
                if (self._release_since_ms is not None or
                        (new_press and self._start_point is not None)):
                    self._session_started_ms = now
                    self._session_samples = 1
                    self._fast_session_started_ms = now
                    self._fast_session_last_touch_ms = None
                    self._fast_session_samples = 0
                    self._start_point = None
                    self._last_point = None
                    self._gesture_reported = False
                # Keep the last valid coordinate for both the LVGL release
                # event and custom gesture handling.  Release frames from
                # CST816D may contain zero/undefined coordinates.
                self._input_point = raw_point
                point = self._filter_point((x, y))
                self._point = self._landscape_point(point)
                if self._start_point is None:
                    self._start_point = point
                self._last_point = point
                self._pressed = True
                self._release_since_ms = None
                self._fast_session_samples += 1
                self._fast_session_last_touch_ms = now
            else:
                if self._release_since_ms is None:
                    self._release_since_ms = now
                release_elapsed = utime.ticks_diff(
                    now, self._release_since_ms)
                if (self._pressed and
                        release_elapsed < CST816D_RELEASE_DEBOUNCE_MS):
                    # Keep publishing the last coordinate while the release
                    # transition is debounced by time, not frame count.
                    return True
                if self._pressed:
                    self._pressed = False
                if self._start_point is not None:
                    self._report_gesture_once()
                elif (self._session_samples == 1 and
                      (raw_point[0] != 0 or raw_point[1] != 0)):
                    # A very short tap can be sampled only after the panel
                    # has already sent its release frame.  Publish one
                    # synthetic pressed frame at that coordinate so LVGL
                    # still observes PRESSED -> RELEASED -> CLICKED.
                    self._start_point = raw_point
                    self._last_point = raw_point
                    self._input_point = raw_point
                    self._point = self._landscape_point(raw_point)
                    self._pressed = True
                    self._deferred_release = True
                    # Count the synthetic first point as a fast-path sample;
                    # otherwise a very short tap would report zero samples
                    # even though its coordinate was delivered to LVGL.
                    self._fast_session_samples += 1
                    self._fast_session_last_touch_ms = now
                    self._release_since_ms = None
                    return True
                if release_elapsed >= CST816D_RELEASE_CONFIRM_MS:
                    self._finish_session()
            return True
        except Exception as error:
            print("[CST816D] 主线程采样失败：{}".format(error))
            return False

    def read(self, indev_drv, data):
        """LVGL 回调只读缓存，不访问 I2C。"""
        # The display driver is configured as rotated landscape.  Return the
        # panel's portrait coordinates here and let LVGL perform the single
        # configured rotation.  Returning _point would rotate twice and make
        # button hit testing (notably the login keypad) incorrect.
        data.point.x = int(self._input_point[0])
        data.point.y = int(self._input_point[1])
        data.state = 1 if self._pressed else 0


def test_cst816d(duration_ms=30000):
    driver = CST816D()
    driver.start()
    end_ms = utime.ticks_add(utime.ticks_ms(), int(duration_ms))
    while utime.ticks_diff(end_ms, utime.ticks_ms()) > 0:
        driver.poll_once()
        utime.sleep_ms(1 if not driver.is_dynamic_active() else 0)
    driver.stop()
    return driver
