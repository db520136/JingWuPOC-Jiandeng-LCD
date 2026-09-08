# -*- coding: utf-8 -*-
"""EC800M LCD 坐标、窗口和局部刷新黑盒测试。

本文件完全独立运行：不导入项目中的任何脚本，不使用 LVGL。
本轮用于检查刷新区域坐标、buffer 字节数、静态边界和连续局部刷新。
当前硬件不支持 TE，因此本文件不测试 TE。
"""

import utime
from machine import LCD


LCD_WIDTH = 296
LCD_HEIGHT = 240
BLOCK_LINES = 40
STATIC_HOLD_MS = 2000
MOVING_FRAMES = 30
MOVING_STEP_PX = 8

COLOR_BLACK = 0x0000
COLOR_RED = 0xF800
COLOR_GREEN = 0x07E0
COLOR_BLUE = 0x001F
COLOR_WHITE = 0xFFFF
COLOR_YELLOW = 0xFFE0
COLOR_CYAN = 0x07FF
COLOR_MAGENTA = 0xF81F

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


def _fill_color(buffer, color):
    high = (int(color) >> 8) & 0xFF
    low = int(color) & 0xFF
    index = 0
    while index < len(buffer):
        buffer[index] = high
        buffer[index + 1] = low
        index += 2


def _color_name(color):
    names = {COLOR_BLACK: "黑", COLOR_RED: "红", COLOR_GREEN: "绿",
             COLOR_BLUE: "蓝", COLOR_WHITE: "白", COLOR_YELLOW: "黄",
             COLOR_CYAN: "青", COLOR_MAGENTA: "紫"}
    return names.get(int(color), "0x{:04X}".format(int(color)))


def _region_info(x1, y1, x2, y2):
    if x1 < 0 or y1 < 0 or x2 >= LCD_WIDTH or y2 >= LCD_HEIGHT:
        raise ValueError("region out of bounds")
    if x1 > x2 or y1 > y2:
        raise ValueError("invalid region")
    width = x2 - x1 + 1
    height = y2 - y1 + 1
    return width, height, width * height * 2


def _write_rect(lcd, x1, y1, x2, y2, color, label):
    width, height, expected_bytes = _region_info(x1, y1, x2, y2)
    buffer = bytearray(expected_bytes)
    _fill_color(buffer, color)
    begin = utime.ticks_ms()
    lcd.lcd_write(buffer, x1, y1, x2, y2)
    elapsed = utime.ticks_diff(utime.ticks_ms(), begin)
    print("[LCDTEST] {} region=({},{}..{},{}), size={}x{} pixels={} "
          "bytes={} expected={} write={}ms".format(
              label, x1, y1, x2, y2, width, height, width * height,
              len(buffer), expected_bytes, elapsed))
    if len(buffer) != expected_bytes:
        print("[LCDTEST][错误] {} buffer字节数不匹配".format(label))
    return elapsed


def _clear_screen(lcd):
    buffer = bytearray(LCD_WIDTH * BLOCK_LINES * 2)
    _fill_color(buffer, COLOR_BLACK)
    y = 0
    total = 0
    blocks = 0
    while y < LCD_HEIGHT:
        lines = min(BLOCK_LINES, LCD_HEIGHT - y)
        begin = utime.ticks_ms()
        lcd.lcd_write(buffer, 0, y, LCD_WIDTH - 1, y + lines - 1)
        total += utime.ticks_diff(utime.ticks_ms(), begin)
        blocks += 1
        y += lines
    print("[LCDTEST] 清屏 blocks={} lines={} total={}ms".format(
        blocks, BLOCK_LINES, total))


def _static_geometry_test(lcd):
    print("[LCDTEST] 测试1：静态矩形和四边界坐标")
    _clear_screen(lcd)
    _write_rect(lcd, 0, 0, 79, 59, COLOR_RED, "左上红色")
    _write_rect(lcd, 216, 0, 295, 59, COLOR_GREEN, "右上绿色")
    _write_rect(lcd, 0, 180, 79, 239, COLOR_BLUE, "左下蓝色")
    _write_rect(lcd, 216, 180, 295, 239, COLOR_WHITE, "右下白色")
    _write_rect(lcd, 0, 60, 1, 179, COLOR_MAGENTA, "左边界")
    _write_rect(lcd, 294, 60, 295, 179, COLOR_MAGENTA, "右边界")
    _write_rect(lcd, 80, 0, 215, 1, COLOR_CYAN, "上边界")
    _write_rect(lcd, 80, 238, 215, 239, COLOR_CYAN, "下边界")
    _write_rect(lcd, 147, 0, 148, 239, COLOR_YELLOW, "中心竖线")
    _write_rect(lcd, 80, 119, 215, 120, COLOR_YELLOW, "中心横线")
    print("[LCDTEST] 测试1完成，保持画面{}ms；预期边界线连续、中心十字笔直".format(
        STATIC_HOLD_MS))
    utime.sleep_ms(STATIC_HOLD_MS)


def _partial_refresh_test(lcd):
    print("[LCDTEST] 测试2：小区域坐标和字节数")
    _clear_screen(lcd)
    tests = (
        (0, 0, 0, 0, COLOR_RED, "左上单像素区域"),
        (295, 0, 295, 0, COLOR_GREEN, "右上单像素区域"),
        (0, 239, 0, 239, COLOR_BLUE, "左下单像素区域"),
        (295, 239, 295, 239, COLOR_WHITE, "右下单像素区域"),
        (147, 110, 148, 129, COLOR_YELLOW, "中心窄竖块"),
        (130, 119, 165, 120, COLOR_CYAN, "中心窄横块"),
    )
    for x1, y1, x2, y2, color, label in tests:
        _write_rect(lcd, x1, y1, x2, y2, color, label)
    print("[LCDTEST] 测试2完成，保持画面{}ms；预期色块位于指定坐标".format(
        STATIC_HOLD_MS))
    utime.sleep_ms(STATIC_HOLD_MS)


def _moving_line_test(lcd):
    print("[LCDTEST] 测试3：连续局部刷新移动竖线")
    _clear_screen(lcd)
    line_width = 2
    old_x = 0
    for frame in range(MOVING_FRAMES):
        new_x = (frame * MOVING_STEP_PX) % (LCD_WIDTH - line_width + 1)
        _write_rect(lcd, old_x, 0, old_x + line_width - 1,
                    LCD_HEIGHT - 1, COLOR_BLACK, "移动帧{}擦除".format(frame))
        _write_rect(lcd, new_x, 0, new_x + line_width - 1,
                    LCD_HEIGHT - 1, COLOR_WHITE, "移动帧{}写入".format(frame))
        old_x = new_x
        utime.sleep_ms(20)
    print("[LCDTEST] 测试3完成；预期白色竖线上下保持同一x坐标，无折线/错位")
    utime.sleep_ms(STATIC_HOLD_MS)


def main():
    print("[LCDTEST] 独立 LCD 坐标/窗口/局部刷新测试开始")
    print("[LCDTEST] 分辨率={}x{} block_lines={}，不使用LVGL/项目脚本".format(
        LCD_WIDTH, LCD_HEIGHT, BLOCK_LINES))
    print("[LCDTEST] TE测试关闭：当前硬件和LCD接口不支持TE引脚")
    lcd = LCD()
    lcd.lcd_init(
        bytearray(_INIT), LCD_WIDTH, LCD_HEIGHT, 52000, 1, 4, 0,
        _INVALID_P,
        bytearray((0, 0, 0x11, 2, 0, 120, 0, 0, 0x21, 0, 0, 0x29)),
        bytearray((0, 0, 0x28, 2, 0, 120, 0, 0, 0x10)), None)
    try:
        lcd.lcd_display_on()
        print("[LCDTEST] LCD初始化完成，display_on成功")
    except Exception as error:
        print("[LCDTEST] LCD初始化完成，display_on不可用：{}".format(error))
    _static_geometry_test(lcd)
    _partial_refresh_test(lcd)
    _moving_line_test(lcd)
    print("[LCDTEST] 全部测试完成")
    print("[LCDTEST] 若测试1/2静态线条正确，而测试3移动竖线错位，"
          "问题更接近LCD扫描与连续局部刷新时序")


main()
