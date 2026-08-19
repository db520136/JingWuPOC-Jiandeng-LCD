# -*- coding: utf-8 -*-
# ST7789 (240x296) LCD + CST816 触摸 + 手势回调 集成脚本
#
# 手势回调 tp_cb 的触发链:
#   lv.task_handler() 后台线程 -> LVGL indev 读取定时器 -> tp.read(CST816_read)
#   -> 固件内部手势检测 -> 回调线程回调 tp_cb(para)
# 因此必须:1) 先注册 display  2) 再注册 indev  3) 持续调用 tick_inc + task_handler。

import utime
import lvgl as lv
from machine import LCD
from tp import cst816

# ---------------- ST7789 240x296 初始化数据 ----------------
XSTART_H = 0xf0
XSTART_L = 0xf1
YSTART_H = 0xf2
YSTART_L = 0xf3
XEND_H = 0xE0
XEND_L = 0xE1
YEND_H = 0xE2
YEND_L = 0xE3
XSTART = 0xD0
XEND = 0xD1
YSTART = 0xD2
YEND = 0xD3

init_st7789_240X296 = (
    0, 0, 0x11,
    2, 0, 120,
    0, 0, 0x20,
    0, 0, 0x00,
    0, 1, 0x36,
    1, 1, 0x00,
    0, 1, 0x3A,
    1, 1, 0x05,
    0, 1, 0x35,
    1, 1, 0x00,
    0, 1, 0xC7,
    1, 1, 0x00,
    0, 1, 0xCC,
    1, 1, 0x09,
    0, 5, 0xB2,
    1, 1, 0x0C,
    1, 1, 0x0C,
    1, 1, 0x00,
    1, 1, 0x33,
    1, 1, 0x33,
    0, 1, 0xB7,
    1, 1, 0x35,
    0, 1, 0xBB,
    1, 1, 0x36,
    0, 1, 0xC0,
    1, 1, 0x2C,
    0, 1, 0xC2,
    1, 1, 0x01,
    0, 1, 0xC3,
    1, 1, 0x0D,
    0, 1, 0xC4,
    1, 1, 0x20,
    0, 1, 0xC6,
    1, 1, 0x0F,
    0, 2, 0xD0,
    1, 1, 0xA4,
    1, 1, 0xA1,
    0, 14, 0xE0,
    1, 1, 0xD0,
    1, 1, 0x17,
    1, 1, 0x19,
    1, 1, 0x04,
    1, 1, 0x03,
    1, 1, 0x04,
    1, 1, 0x32,
    1, 1, 0x41,
    1, 1, 0x43,
    1, 1, 0x09,
    1, 1, 0x14,
    1, 1, 0x12,
    1, 1, 0x33,
    1, 1, 0x2C,
    0, 14, 0xE1,
    1, 1, 0xD0,
    1, 1, 0x18,
    1, 1, 0x17,
    1, 1, 0x04,
    1, 1, 0x03,
    1, 1, 0x04,
    1, 1, 0x31,
    1, 1, 0x46,
    1, 1, 0x43,
    1, 1, 0x09,
    1, 1, 0x14,
    1, 1, 0x13,
    1, 1, 0x31,
    1, 1, 0x2D,
    0, 0, 0x29,
    0, 1, 0x36,
    1, 1, 0x00,
    0, 0, 0x2c,
)
init_st7789_240X296_p = bytearray(init_st7789_240X296)

invalid_st7789_240X296 = (
    0, 4, 0x2a,
    1, 1, XSTART_H,
    1, 1, XSTART_L,
    1, 1, XEND_H,
    1, 1, XEND_L,
    0, 4, 0x2b,
    1, 1, YSTART_H,
    1, 1, YSTART_L,
    1, 1, YEND_H,
    1, 1, YEND_L,
    0, 0, 0x2c,
)
invalid_st7789_240X296_p = bytearray(invalid_st7789_240X296)

displayOFF_st7789_240X296 = (
    0, 0, 0x28,
    2, 0, 120,
    0, 0, 0x10,
)
displayOFF_st7789_240X296_p = bytearray(displayOFF_st7789_240X296)

displayON_st7789_240X296 = (
    0, 0, 0x11,
    2, 0, 20,
    0, 0, 0x20,
    0, 0, 0x29,
)
displayON_st7789_240X296_p = bytearray(displayON_st7789_240X296)

# ---------------- LCD 基本参数 ----------------
LCD_WIDTH = 240
LCD_HEIGHT = 296
COLOR_GREEN = 0x07E0
COLOR_RED = 0xF800
COLOR_BLACK = 0x0000
DISPLAY_INVERTED = True

# ---------------- CST816 手势编号(对应固件 TP_STATE_E) ----------------
TP_GESTURE = {
    0: "right_slide 右滑",
    1: "left_slide  左滑",
    2: "up_slide    上滑",
    3: "down_slide  下滑",
    4: "return      边缘返回",
    5: "click       单击",
    6: "return_button 返回键",
    7: "error       未识别",
}


def panel_color(color):
    if DISPLAY_INVERTED:
        return color ^ 0xFFFF
    return color


def clear_screen(color):
    spilcd.lcd_clear(panel_color(color))


def lvgl_color(color):
    red = (color >> 16) & 0xff
    green = (color >> 8) & 0xff
    blue = color & 0xff
    rgb565 = ((red & 0xf8) << 8) | ((green & 0xfc) << 3) | (blue >> 3)
    rgb565 = panel_color(rgb565)
    red = ((rgb565 >> 11) & 0x1f) * 255 // 31
    green = ((rgb565 >> 5) & 0x3f) * 255 // 63
    blue = (rgb565 & 0x1f) * 255 // 31
    return (red << 16) | (green << 8) | blue


# ---------------- LCD 硬件初始化 ----------------
spilcd = LCD()
spilcd.lcd_init(
    init_st7789_240X296_p,
    LCD_WIDTH,
    LCD_HEIGHT,
    52000,
    1,
    4,
    0,
    invalid_st7789_240X296_p,
    displayON_st7789_240X296_p,
    displayOFF_st7789_240X296_p,
    None,
)
clear_screen(COLOR_BLACK)

FONT_NAME = "watch_Semibold_24.bin"
FONT_LINE_HEIGHT = 33
FONT_FLASH_PORT = 0


def lvgl_flush(color_buf, x_start, y_start, x_end, y_end):
    flush_buf = bytearray(color_buf)
    spilcd.lcd_write(flush_buf, x_start, y_start, x_end, y_end)


def make_text_style(color):
    style = lv.style_t()
    style.init()
    style.set_text_color(lv.color_hex(lvgl_color(color)))
    style.set_text_font_v2(FONT_NAME, FONT_LINE_HEIGHT, FONT_FLASH_PORT)
    style.set_text_align(lv.TEXT_ALIGN.CENTER)
    style.set_text_opa(255)
    style.set_bg_opa(0)
    style.set_border_width(0)
    style.set_pad_top(0)
    style.set_pad_bottom(0)
    style.set_pad_left(0)
    style.set_pad_right(0)
    return style


def make_square(parent, x_pos, y_pos, background, text, foreground):
    square = lv.obj(parent)
    square.set_size(88, 88)
    square.set_pos(x_pos, y_pos)
    square.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    square.set_style_bg_color(lv.color_hex(lvgl_color(background)), lv.PART.MAIN | lv.STATE.DEFAULT)
    square.set_style_radius(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    square.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    label = lv.label(square)
    label.set_text(text)
    label.set_size(88, FONT_LINE_HEIGHT)
    label.set_pos(0, 27)
    label.add_style(make_text_style(foreground), lv.PART.MAIN | lv.STATE.DEFAULT)


# ---------------- LVGL 初始化 + 注册 display(触摸回调的前提) ----------------
lv.init()
draw_buffer = bytearray(LCD_WIDTH * 40 * 2)
lv_draw_buf = lv.disp_draw_buf_t()
lv_draw_buf.init(draw_buffer, None, len(draw_buffer))
lv_disp_drv = lv.disp_drv_t()
lv_disp_drv.init()
lv_disp_drv.draw_buf = lv_draw_buf
lv_disp_drv.flush_cb = lvgl_flush
lv_disp_drv.hor_res = LCD_WIDTH
lv_disp_drv.ver_res = LCD_HEIGHT
lv_disp_drv.register()

# ---------------- 创建演示界面 ----------------
main_page = lv.obj()
main_page.set_size(LCD_WIDTH, LCD_HEIGHT)
main_page.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
main_page.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
main_page.set_style_bg_color(lv.color_hex(lvgl_color(0x000000)), lv.PART.MAIN | lv.STATE.DEFAULT)
main_page.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)

title = lv.label(main_page)
title.set_text("对讲机")
title.set_size(LCD_WIDTH, FONT_LINE_HEIGHT)
title.set_pos(0, 8)
title.add_style(make_text_style(0xFFFFFF), lv.PART.MAIN | lv.STATE.DEFAULT)

make_square(main_page, 12, 70, 0xFFFFFF, "1穿", 0xFF0000)
make_square(main_page, 140, 70, 0x00FF00, "2戴", 0xFFFF00)

lv.scr_load(main_page)


# ---------------- 触摸手势回调 ----------------
# 注意:通过 set_callback 注册的是普通函数时,回调只会收到 1 个参数(手势编号),
# 所以签名是 tp_cb(para),不是 tp_cb(self, para)。
def tp_cb(para):
    print("[触摸手势] para =", para, "->", TP_GESTURE.get(para, "未知"))
    # 在这里按手势编号接入你的界面逻辑,例如:
    # if para == 0:   # 右滑
    #     ...
    # elif para == 5: # 单击
    #     ...


# ---------------- CST816 触摸初始化 + 注册输入设备 ----------------
# irq / reset 引脚号请按你的实际硬件修改(这里沿用你之前的 1 / 2)
tp = cst816(irq=11, reset=15)
print("tp=",tp)
tp.activate()
tp.init()
tp.set_callback(tp_cb)

indev_drv = lv.indev_drv_t()
indev_drv.init()
indev_drv.type = lv.INDEV_TYPE.POINTER
indev_drv.read_cb = tp.read           # 由 LVGL 周期性调用,内部完成坐标读取 + 手势检测
indev_drv.long_press_time = 80
indev_drv.register()

print("触摸屏就绪,请在屏幕上滑动或点击...")

# ---------------- 主循环:驱动 LVGL(不要再手动 read_xy,避免和 LVGL 抢触摸数据) ----------------
lv.tick_inc(5)
lv.task_handler()
while True:
    lv.tick_inc(5)
    lv.task_handler()
    utime.sleep_ms(5)
