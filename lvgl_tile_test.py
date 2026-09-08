# -*- coding: utf-8 -*-
"""独立 LVGL tileview 跟手移动测试。

本文件不导入项目中的任何脚本，不使用外置字库、业务线程或 Router。
只验证：LCD -> LVGL flush_cb -> CST816D 触摸 -> 两个纯色 tileview 页面。

运行后在两个页面之间慢速、快速、反向拖动，观察两页交界线是否抖动、
折线或上下错位。每秒输出一次 LVGL flush 汇总，避免每次刷新打印影响结果。
"""

import utime
import lvgl as lv
from machine import LCD, I2C, Pin


LCD_WIDTH = 296
LCD_HEIGHT = 240
DISPLAY_BUFFER_LINES = 40

I2C_BUS = I2C.I2C1
I2C_MODE = I2C.FAST_MODE
TOUCH_ADDRESS = 0x15
TOUCH_SAMPLE_MS = 5
TOUCH_REG_FINGER = 0x02
TOUCH_RAW_HEIGHT = 296

COLOR_BLACK = lv.color_hex(0x000000)
COLOR_PAGE_A = lv.color_hex(0x102040)
COLOR_PAGE_B = lv.color_hex(0x104020)
COLOR_WHITE = lv.color_hex(0xFFFFFF)
COLOR_YELLOW = lv.color_hex(0xFFE000)
COLOR_RED = lv.color_hex(0xE02020)

XSTART_H, XSTART_L, YSTART_H, YSTART_L = 0xF0, 0xF1, 0xF2, 0xF3
XEND_H, XEND_L, YEND_H, YEND_L = 0xE0, 0xE1, 0xE2, 0xE3

_INIT = (
    0, 0, 0x01, 2, 0, 120, 0, 0, 0x11, 2, 0, 120,
    0, 0, 0x21, 0, 0, 0x00,
    0, 1, 0x36, 1, 1, 0x60, 0, 1, 0x3A, 1, 1, 0x05,
    0, 1, 0x35, 1, 1, 0x00, 0, 1, 0xC7, 1, 1, 0x00,
    0, 1, 0xCC, 1, 1, 0x09,
    0, 5, 0xB2, 1, 1, 0x0C, 1, 1, 0x0C, 1, 1, 0x00,
    1, 1, 0x33, 1, 1, 0x33, 0, 1, 0xB7, 1, 1, 0x35,
    0, 1, 0xBB, 1, 1, 0x36, 0, 1, 0xC0, 1, 1, 0x2C,
    0, 1, 0xC2, 1, 1, 0x01, 0, 1, 0xC3, 1, 1, 0x0D,
    0, 1, 0xC4, 1, 1, 0x20, 0, 1, 0xC6, 1, 1, 0x0F,
    0, 2, 0xD0, 1, 1, 0xA4, 1, 1, 0xA1,
    0, 14, 0xE0, 1, 1, 0xD0, 1, 1, 0x17, 1, 1, 0x19,
    1, 1, 0x04, 1, 1, 0x03, 1, 1, 0x04, 1, 1, 0x32,
    1, 1, 0x41, 1, 1, 0x43, 1, 1, 0x09, 1, 1, 0x14,
    1, 1, 0x12, 1, 1, 0x33, 1, 1, 0x2C,
    0, 14, 0xE1, 1, 1, 0xD0, 1, 1, 0x18, 1, 1, 0x17,
    1, 1, 0x04, 1, 1, 0x03, 1, 1, 0x04, 1, 1, 0x31,
    1, 1, 0x46, 1, 1, 0x43, 1, 1, 0x09, 1, 1, 0x14,
    1, 1, 0x13, 1, 1, 0x31, 1, 1, 0x2D,
    0, 0, 0x29, 0, 0, 0x2C,
)

_INVALID_P = bytearray((
    0, 4, 0x2A, 1, 1, XSTART_H, 1, 1, XSTART_L,
    1, 1, XEND_H, 1, 1, XEND_L, 0, 4, 0x2B, 1, 1,
    YSTART_H, 1, 1, YSTART_L, 1, 1, YEND_H, 1, 1, YEND_L,
    0, 0, 0x2C,
))


class Display:
    def __init__(self):
        self.lcd = None
        self.buffer = None
        self.draw_buf = None
        self.driver = None
        self.display = None

    def start(self):
        print("[TILETEST] display start")
        self.lcd = LCD()
        self.lcd.lcd_init(
            bytearray(_INIT), LCD_WIDTH, LCD_HEIGHT, 52000, 1, 4, 0,
            _INVALID_P,
            bytearray((0, 0, 0x11, 2, 0, 120, 0, 0, 0x21, 0, 0, 0x29)),
            bytearray((0, 0, 0x28, 2, 0, 120, 0, 0, 0x10)), None)
        try:
            self.lcd.lcd_display_on()
        except Exception:
            pass
        lv.init()
        self.buffer = bytearray(LCD_WIDTH * DISPLAY_BUFFER_LINES * 2)
        self.draw_buf = lv.disp_draw_buf_t()
        # QuecPython 官方例程按 buffer 字节长度传入第三个参数；本测试
        # 专门采用官方写法，不再混入项目 display_port 的 Python 适配逻辑。
        self.draw_buf.init(self.buffer, None, len(self.buffer))
        self.driver = lv.disp_drv_t()
        self.driver.init()
        self.driver.draw_buf = self.draw_buf
        # 直接绑定 QuecPython 原生 LCD 回调。该路径由固件负责解析 LVGL
        # 刷新参数并完成 flush，不经过自定义 Python _flush 包装。
        self.driver.flush_cb = self.lcd.lcd_write
        self.driver.hor_res = LCD_WIDTH
        self.driver.ver_res = LCD_HEIGHT
        # 保存 register() 返回的 display 对象，避免 LVGL display 实例在
        # 独立测试中没有 Python 引用，导致回调生命周期和统计结果不可靠。
        self.display = self.driver.register()
        print("[TILETEST] display ready registered={} buffer_lines={} "
              "buffer={} bytes".format(
                  self.display is not None, DISPLAY_BUFFER_LINES,
                  len(self.buffer)))

    def force_refresh(self):
        """请求当前 display 立即重绘。"""
        try:
            lv.refr_now(self.display)
            print("[TILETEST] lv.refr_now() 已调用（原生lcd_write直绑定）")
        except Exception as error:
            print("[TILETEST] lv.refr_now() 不可用：{}".format(error))


class Touch:
    def __init__(self):
        self.i2c = I2C(I2C_BUS, I2C_MODE)
        self.point = (0, 0)
        self.pressed = False
        self.last_poll = None
        self.driver = None
        self.indev = None

    def _read_regs(self, register, length):
        data = bytearray(length)
        result = self.i2c.read(TOUCH_ADDRESS, bytes([register]), 1,
                                data, length, 0)
        if result not in (None, 0):
            raise OSError("touch I2C read failed: {}".format(result))
        return data

    @staticmethod
    def _rotate(x, y):
        return max(0, min(LCD_WIDTH - 1, TOUCH_RAW_HEIGHT - 1 - int(y))), \
            max(0, min(LCD_HEIGHT - 1, int(x)))

    def poll(self):
        now = utime.ticks_ms()
        if self.last_poll is not None and utime.ticks_diff(
                now, self.last_poll) < TOUCH_SAMPLE_MS:
            return False
        self.last_poll = now
        try:
            data = self._read_regs(TOUCH_REG_FINGER, 5)
            fingers = int(data[0])
            event_flag = (int(data[1]) >> 6) & 0x03
            raw_x = ((int(data[1]) & 0x0F) << 8) | int(data[2])
            raw_y = ((int(data[3]) & 0x0F) << 8) | int(data[4])
            if fingers > 0 and event_flag in (0, 2):
                self.point = self._rotate(raw_x, raw_y)
                self.pressed = True
            else:
                self.pressed = False
        except Exception:
            self.pressed = False
        return True

    def _read_cb(self, indev_drv, data):
        data.point.x = int(self.point[0])
        data.point.y = int(self.point[1])
        data.state = 1 if self.pressed else 0

    def start(self):
        self.driver = lv.indev_drv_t()
        self.driver.init()
        self.driver.type = lv.INDEV_TYPE.POINTER
        self.driver.read_cb = self._read_cb
        self.indev = self.driver.register()
        print("[TILETEST] touch ready I2C=1 addr=0x15 sample={}ms".format(
            TOUCH_SAMPLE_MS))


def _make_page(parent, color, title, line_color, color_name):
    page = lv.obj(parent)
    page.set_size(LCD_WIDTH, LCD_HEIGHT)
    page.set_style_bg_color(color, lv.PART.MAIN)
    page.set_style_bg_opa(255, lv.PART.MAIN)
    # 每页都放置相同的边界标记，便于观察两页拼接时是否变成折线。
    marker = lv.obj(page)
    marker.set_pos(146, 0)
    marker.set_size(4, LCD_HEIGHT)
    marker.set_style_bg_color(line_color, lv.PART.MAIN)
    marker.set_style_bg_opa(255, lv.PART.MAIN)
    top = lv.obj(page)
    top.set_pos(0, 0)
    top.set_size(LCD_WIDTH, 2)
    top.set_style_bg_color(COLOR_WHITE, lv.PART.MAIN)
    top.set_style_bg_opa(255, lv.PART.MAIN)
    bottom = lv.obj(page)
    bottom.set_pos(0, LCD_HEIGHT - 2)
    bottom.set_size(LCD_WIDTH, 2)
    bottom.set_style_bg_color(COLOR_WHITE, lv.PART.MAIN)
    bottom.set_style_bg_opa(255, lv.PART.MAIN)
    print("[TILETEST] page {} created color={}".format(title, color_name))
    return page


def _make_ui():
    screen = lv.obj()
    screen.set_size(LCD_WIDTH, LCD_HEIGHT)
    screen.set_style_bg_color(COLOR_BLACK, lv.PART.MAIN)
    screen.set_style_bg_opa(255, lv.PART.MAIN)
    tileview = lv.tileview(screen)
    tileview.set_size(LCD_WIDTH, LCD_HEIGHT)
    tileview.set_scroll_dir(lv.DIR.HOR)
    tileview.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    try:
        tileview.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
        tileview.set_scroll_chain(lv.DIR.NONE)
    except Exception:
        pass
    tile_a = tileview.add_tile(0, 0, lv.DIR.RIGHT)
    tile_b = tileview.add_tile(1, 0, lv.DIR.LEFT)
    _make_page(tile_a, COLOR_PAGE_A, "A", COLOR_RED, "深蓝")
    _make_page(tile_b, COLOR_PAGE_B, "B", COLOR_YELLOW, "深绿")
    lv.scr_load(screen)
    screen.invalidate()
    print("[TILETEST] tileview ready: A<->B, pure LVGL native drag")
    return screen, tileview


def main():
    print("[TILETEST] 独立 LVGL tileview 跟手测试开始")
    print("[TILETEST] 不导入项目脚本，不使用外置字库，不启动业务线程")
    display = Display()
    display.start()
    touch = Touch()
    touch.start()
    screen, tileview = _make_ui()
    screen.invalidate()
    display.force_refresh()
    for _ in range(5):
        lv.tick_inc(5)
        lv.task_handler()
    print("[TILETEST] 首帧已执行：原生 lcd_write 直绑定，Python层不统计flush")
    last_ms = utime.ticks_ms()
    last_report_ms = last_ms
    last_pressed = False
    loop_count = 0
    touch_count = 0
    while True:
        now = utime.ticks_ms()
        elapsed = utime.ticks_diff(now, last_ms)
        last_ms = now
        touch.poll()
        if touch.pressed != last_pressed:
            touch_count += 1
            print("[TILETEST] touch {} point=({}, {})".format(
                "DOWN" if touch.pressed else "UP",
                touch.point[0], touch.point[1]))
            last_pressed = touch.pressed
        lv.tick_inc(max(0, int(elapsed)))
        lv.task_handler()
        loop_count += 1
        if utime.ticks_diff(now, last_report_ms) >= 1000:
            screen.invalidate()
        if utime.ticks_diff(now, last_report_ms) >= 1000:
            print("[TILETEST] 1s loop={} touch_event={} "
                  "flush=原生lcd_write直绑定，Python不统计".format(
                      loop_count, touch_count))
            loop_count = 0
            touch_count = 0
            last_report_ms = now
        utime.sleep_ms(1)


main()
