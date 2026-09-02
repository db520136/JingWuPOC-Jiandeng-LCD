# -*- coding: utf-8 -*-
"""CST816D interrupt driven touch controller.

The controller stays in its low-power, interrupt-driven state while idle.
After IRQ, the worker disables the external interrupt and samples the I2C
registers at ``CST816D_DYNAMIC_SAMPLE_HZ`` until the finger is released.
"""

import _thread
import math
import utime

from machine import I2C, Pin, ExtInt


CST816D_I2C_ADDRESS = 0x15
CST816D_IRQ_GPIO = Pin.GPIO11
CST816D_RESET_GPIO = Pin.GPIO15
CST816D_I2C_BUS = I2C.I2C1
CST816D_I2C_MODE = I2C.STANDARD_MODE

# Change this single value to tune dynamic sampling. 100 Hz means 10 ms.
CST816D_DYNAMIC_SAMPLE_HZ = 100
CST816D_DYNAMIC_SAMPLE_INTERVAL_MS = 1000 // CST816D_DYNAMIC_SAMPLE_HZ
CST816D_MAX_TOUCH_MS = 3000
CST816D_RELEASE_GRACE_MS = 2000
CST816D_SWIPE_DISTANCE_PX = 30
CST816D_OUTLIER_DISTANCE_PX = 80
CST816D_EMA_ALPHA_NUM = 3
CST816D_EMA_ALPHA_DEN = 5

REG_GESTURE_ID = 0x01
REG_FINGER_NUM = 0x02
REG_XPOS_H = 0x03
REG_XPOS_L = 0x04
REG_YPOS_H = 0x05
REG_YPOS_L = 0x06
REG_MOTION_MASK = 0xEC
REG_IRQ_PULSE_WIDTH = 0xED
REG_NORMAL_SCAN_PERIOD = 0xEE
REG_IRQ_CONTROL = 0xFA
REG_CHIP_ID = 0xA7
REG_PROJECT_ID = 0xA8
REG_FW_VERSION = 0xA9

GESTURE_NAMES = {
    0x00: "无手势",
    0x01: "上滑",
    0x02: "下滑",
    0x03: "左滑",
    0x04: "右滑",
    0x05: "单击",
    0x0B: "双击",
    0x0C: "长按",
}


def _new_event_lock():
    event = _thread.allocate_lock()
    event.acquire()
    return event


class CST816D:
    """Direct CST816D driver with IRQ wake and dynamic coordinate sampling."""

    def __init__(self, address=CST816D_I2C_ADDRESS,
                 dynamic_sample_hz=CST816D_DYNAMIC_SAMPLE_HZ,
                 swipe_distance_px=CST816D_SWIPE_DISTANCE_PX):
        self.address = int(address)
        self.dynamic_sample_hz = int(dynamic_sample_hz)
        if self.dynamic_sample_hz <= 0:
            raise ValueError("dynamic_sample_hz must be positive")
        self.sample_interval_ms = max(1, 1000 // self.dynamic_sample_hz)
        self.swipe_distance_px = int(swipe_distance_px)
        self.i2c = I2C(CST816D_I2C_BUS, CST816D_I2C_MODE)
        print("i2c bus: {} mode: {}".format(self.i2c, CST816D_I2C_MODE))
        self.reset_pin = Pin(CST816D_RESET_GPIO, Pin.OUT,
                              Pin.PULL_DISABLE, 1)
        self.irq = None
        self._event = _new_event_lock()
        self._running = False
        self._worker_started = False
        self._irq_pending = False
        self._irq_count = 0
        self._sample_count = 0
        self._last_sample_ms = None
        self._callback = None

    def _write_reg(self, register, value):
        payload = bytearray([int(value) & 0xFF])
        result = self.i2c.write(self.address, bytes([register & 0xFF]), 1,
                                payload, 1)
        if result not in (None, 0):
            raise OSError("CST816D I2C write failed: {}".format(result))

    def _read_regs(self, register, length):
        data = bytearray(length)
        result = self.i2c.read(self.address, bytes([register & 0xFF]), 1,
                               data, length, 0)
        if result not in (None, 0):
            raise OSError("CST816D I2C read failed: {}".format(register,result))
        return bytes(data)

    def read_register(self, register):
        return self._read_regs(register, 1)[0]

    def read_identity(self):
        return (self.read_register(REG_CHIP_ID), self.read_register(REG_PROJECT_ID),
                self.read_register(REG_FW_VERSION))

    def _configure(self):
        # Disable double-click detection. Only touch-triggered low pulses are
        # needed by this test driver.
        self._write_reg(REG_MOTION_MASK, 0x00)
        # 1 ms IRQ pulse, 10 ms normal scan period, touch IRQ only.
        self._write_reg(REG_IRQ_PULSE_WIDTH, 1)
        self._write_reg(REG_NORMAL_SCAN_PERIOD, 1)
        self._write_reg(REG_IRQ_CONTROL, 0x40)

    def _irq_callback(self, *args):
        if not self._running:
            return
        self._irq_pending = True
        self._irq_count += 1
        try:
            self._event.release()
        except Exception:
            pass

    def _enable_irq(self):
        if self.irq is None:
            self.irq = ExtInt(CST816D_IRQ_GPIO, ExtInt.IRQ_FALLING,
                              ExtInt.PULL_PU, self._irq_callback)
        self.irq.enable()

    def _disable_irq(self):
        if self.irq is not None:
            self.irq.disable()

    def _reset(self):
        self.reset_pin.write(0)
        utime.sleep_ms(10)
        self.reset_pin.write(1)
        utime.sleep_ms(100)

    def start(self, callback=None):
        if self._running:
            return True
        self._callback = callback
        self._configure()
        self._running = True
        self._enable_irq()
        try:
            _thread.start_new_thread(self._worker, ())
            self._worker_started = True
        except Exception:
            self._running = False
            self._disable_irq()
            raise
        chip, project, version = self.read_identity()
        print("[CST816D] started: chip=0x{:02X} project=0x{:02X} fw=0x{:02X}, {}Hz".format(
            chip, project, version, self.dynamic_sample_hz))
        return True

    def stop(self):
        self._running = False
        self._disable_irq()
        try:
            self._event.release()
        except Exception:
            pass
        self.irq = None
        self._worker_started = False

    def _read_point(self):
        data = self._read_regs(REG_FINGER_NUM, 5)
        fingers = data[0]
        event_flag = (data[1] >> 6) & 0x03
        x = ((data[1] & 0x0F) << 8) | data[2]
        y = ((data[3] & 0x0F) << 8) | data[4]
        return fingers, event_flag, x, y

    def _filter_point(self, point, previous, ema):
        if previous is not None:
            if (abs(point[0] - previous[0]) > CST816D_OUTLIER_DISTANCE_PX or
                    abs(point[1] - previous[1]) > CST816D_OUTLIER_DISTANCE_PX):
                point = previous
        if ema is None:
            return point
        alpha_num = CST816D_EMA_ALPHA_NUM
        alpha_den = CST816D_EMA_ALPHA_DEN
        return ((ema[0] * (alpha_den - alpha_num) + point[0] * alpha_num) // alpha_den,
                (ema[1] * (alpha_den - alpha_num) + point[1] * alpha_num) // alpha_den)

    def _classify(self, start, end, gesture_id):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.sqrt(dx * dx + dy * dy)
        if distance < self.swipe_distance_px:
            return "单击", dx, dy, distance
        if abs(dx) >= abs(dy):
            return ("右滑" if dx > 0 else "左滑"), dx, dy, distance
        return ("下滑" if dy > 0 else "上滑"), dx, dy, distance

    def _sample_session(self):
        start = None
        previous = None
        filtered = None
        released_since_ms = None
        started_ms = utime.ticks_ms()
        session_samples = 0
        last_event_flag = 0
        last_gesture = 0
        while self._running:
            now = utime.ticks_ms()
            fingers, event_flag, x, y = self._read_point()
            self._sample_count += 1
            session_samples += 1
            last_event_flag = event_flag
            if fingers:
                point = self._filter_point((x, y), previous, filtered)
                if start is None:
                    start = point
                previous = point
                filtered = point
                released_since_ms = None
                gesture_id = self.read_register(REG_GESTURE_ID)
                if gesture_id in (1, 2, 3, 4, 5) and gesture_id != last_gesture:
                    print("[CST816D] 手势={} (动态采样)".format(
                        GESTURE_NAMES[gesture_id]))
                    last_gesture = gesture_id
            elif start is not None:
                if released_since_ms is None:
                    released_since_ms = now
                elif utime.ticks_diff(now, released_since_ms) >= CST816D_RELEASE_GRACE_MS:
                    break
            if utime.ticks_diff(now, started_ms) >= CST816D_MAX_TOUCH_MS:
                break
            elapsed = utime.ticks_diff(utime.ticks_ms(), now)
            wait_ms = max(1, self.sample_interval_ms - elapsed)
            utime.sleep_ms(wait_ms)
        if start is None or filtered is None:
            return
        gesture_id = self.read_register(REG_GESTURE_ID)
        name, dx, dy, distance = self._classify(start, filtered, gesture_id)
        elapsed_ms = max(1, utime.ticks_diff(utime.ticks_ms(), started_ms))
        frequency = session_samples * 1000.0 / elapsed_ms
        print("[CST816D] 手势={} raw=({}, {}) filtered=({}, {}) dx={} dy={} distance={:.1f}px irq={}".format(
            GESTURE_NAMES.get(gesture_id, name), start[0], start[1],
            filtered[0], filtered[1], dx, dy, distance, self._irq_count))
        print("[CST816D] 本次触摸结束：采样次数={} 时长={}ms 实际频率={:.1f}Hz 最后状态={}".format(
            session_samples, elapsed_ms, frequency, last_event_flag))
        if self._callback is not None:
            self._callback(name, start, filtered, gesture_id)

    def _worker(self):
        while self._running:
            self._event.acquire()
            if not self._running:
                break
            self._irq_pending = False
            self._disable_irq()
            try:
                self._sample_session()
            except Exception as error:
                print("[CST816D] dynamic sampling failed: {}".format(error))
            finally:
                if self._running:
                    self._enable_irq()

    def test(self, duration_ms=10000):
        """Run the IRQ/dynamic-sampling diagnostic loop."""
        print("[CST816D TEST] identity={} interval={}ms swipe={}px".format(
            self.read_identity(), self.sample_interval_ms,
            self.swipe_distance_px))
        self.start()
        end_ms = utime.ticks_add(utime.ticks_ms(), int(duration_ms))
        while utime.ticks_diff(end_ms, utime.ticks_ms()) > 0:
            utime.sleep_ms(100)
        print("[CST816D TEST] irq_count={} sample_count={}".format(
            self._irq_count, self._sample_count))
        self.stop()


def test_cst816d(duration_ms=10000):
    """Convenience test entry point for the QuecPython REPL."""
    driver = CST816D()
    driver.test(duration_ms)
    return driver
a = test_cst816d(30000)
while True:
    utime.sleep_ms(1000)
