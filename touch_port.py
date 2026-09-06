# -*- coding: utf-8 -*-
"""独立的 CST816D 触摸输入适配层。

这里只输出统一的横屏逻辑坐标和 pressed/released 状态，不做页面导航、
点击补发或业务线程调度。该实现不使用旧 ``CST816D.py`` 的手势状态机。
"""

import utime

import lvgl as lv
from machine import I2C, Pin


I2C_BUS = I2C.I2C1
I2C_MODE = I2C.FAST_MODE
CST816D_ADDRESS = 0x15
RAW_WIDTH = 240
RAW_HEIGHT = 296
LOGICAL_WIDTH = 296
LOGICAL_HEIGHT = 240
REG_FINGER_NUM = 0x02
IRQ_GPIO = Pin.GPIO11
RESET_GPIO = Pin.GPIO15
TOUCH_SCROLL_THROW = 25
TOUCH_SCROLL_LIMIT = 10


class TouchPort:
    """主循环轮询的最小 CST816D -> LVGL pointer 适配。"""

    def __init__(self, address=CST816D_ADDRESS, sample_ms=5):
        self.address = int(address)
        self.sample_ms = max(5, int(sample_ms))
        self.i2c = I2C(I2C_BUS, I2C_MODE)
        self.point = (0, 0)
        self.pressed = False
        self.last_poll_ms = None
        self.started = False
        self._indev_driver = None
        self.indev = None
        self.poll_count = 0
        self._last_reported_pressed = None

    def configure_scroll(self, throw_value=TOUCH_SCROLL_THROW,
                         limit_value=TOUCH_SCROLL_LIMIT):
        """配置 LVGL 原生滚动惯性和启动门槛；不支持时保留固件默认值。"""
        throw_value = max(0, min(255, int(throw_value)))
        limit_value = max(0, min(255, int(limit_value)))
        throw_ok = False
        limit_ok = False
        targets = [self.indev]
        for target in targets:
            if target is None:
                continue
            setter = getattr(target, "set_scroll_throw", None)
            if setter is not None:
                try:
                    setter(throw_value)
                    throw_ok = True
                except Exception:
                    pass
            setter = getattr(target, "set_scroll_limit", None)
            if setter is not None:
                try:
                    setter(limit_value)
                    limit_ok = True
                except Exception:
                    pass
        # 部分 QuecPython 绑定导出为 lv.indev_set_* 全局函数。
        if not throw_ok:
            setter = getattr(lv, "indev_set_scroll_throw", None)
            if setter is not None and self.indev is not None:
                try:
                    setter(self.indev, throw_value)
                    throw_ok = True
                except Exception:
                    pass
        if not limit_ok:
            setter = getattr(lv, "indev_set_scroll_limit", None)
            if setter is not None and self.indev is not None:
                try:
                    setter(self.indev, limit_value)
                    limit_ok = True
                except Exception:
                    pass
        return throw_ok, limit_ok

    def _read_regs(self, register, length):
        data = bytearray(length)
        result = self.i2c.read(
            self.address, bytes([register]), 1, data, length, 0)
        if result not in (None, 0):
            raise OSError("CST816D I2C read failed: {}".format(result))
        return data

    @staticmethod
    def _rotate(x, y):
        # 触摸面板 portrait -> LCD landscape，整个系统只转换一次。
        return max(0, min(LOGICAL_WIDTH - 1, RAW_HEIGHT - 1 - int(y))), \
            max(0, min(LOGICAL_HEIGHT - 1, int(x)))

    def poll(self, force=False):
        if not self.started:
            return False
        now = utime.ticks_ms()
        if not force and self.last_poll_ms is not None:
            if utime.ticks_diff(now, self.last_poll_ms) < self.sample_ms:
                return False
        self.last_poll_ms = now
        try:
            data = self._read_regs(REG_FINGER_NUM, 5)
            fingers = int(data[0])
            event_flag = (int(data[1]) >> 6) & 0x03
            raw_x = ((int(data[1]) & 0x0F) << 8) | int(data[2])
            raw_y = ((int(data[3]) & 0x0F) << 8) | int(data[4])
            if fingers > 0 and event_flag in (0, 2):
                self.point = self._rotate(raw_x, raw_y)
                self.pressed = True
            else:
                self.pressed = False
            self.poll_count += 1
            self._last_reported_pressed = self.pressed
            return True
        except Exception as error:
            print("[TouchPort] 读取失败：{}".format(error))
            self.pressed = False
            return False

    def _read_cb(self, indev_drv, data):
        data.point.x = int(self.point[0])
        data.point.y = int(self.point[1])
        data.state = 1 if self.pressed else 0

    def start(self):
        self.started = True
        self._indev_driver = lv.indev_drv_t()
        self._indev_driver.init()
        self._indev_driver.type = lv.INDEV_TYPE.POINTER
        self._indev_driver.read_cb = self._read_cb
        self.indev = self._indev_driver.register()
        return self

    def stop(self):
        self.started = False
        self.pressed = False
