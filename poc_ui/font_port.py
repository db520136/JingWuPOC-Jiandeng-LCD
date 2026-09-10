# -*- coding: utf-8 -*-
"""外置 Flash 字库和 LVGL 文本样式适配。"""

import lvgl as lv
import uos


# QuecPython LVGL 的外置字库接口使用已烧录文件名；端口 0 表示外置
# Flash 字库资源。现场当前统一使用这一份完整中文字库。
BODY_FONT_PATH = "watch_Semibold_24.bin"
BODY_FONT_LINE_HEIGHT = 33
STATUS_FONT_PATH = "watch_Regular_16.bin"
STATUS_FONT_LINE_HEIGHT = 22
# 兼容仍引用旧常量的页面代码；正文统一使用 Semibold 24。
FONT_PATH = BODY_FONT_PATH
FONT_LINE_HEIGHT = BODY_FONT_LINE_HEIGHT
FONT_FLASH_PORT = 0
_styles = {}
_mounted = False


def mount_external_flash():
    """挂载已烧录字库的外置 NOR Flash。"""
    global _mounted
    if _mounted:
        return True
    try:
        device = uos.VfsLfs1(32, 32, 32, "ext_fs", 0, 3)
        uos.mount(device, "/ext")
        print("挂载外置 Flash 成功")
    except Exception:
        # 软重启时可能已经挂载；目录可读即可继续使用。
        uos.listdir("/ext")
    _mounted = True
    return True


def make_text_style(style_name, color, font_path=BODY_FONT_PATH,
                    line_height=BODY_FONT_LINE_HEIGHT):
    """返回共享文字样式；字体和颜色组合只创建一次。"""
    cached = _styles.get(style_name)
    if cached is not None:
        return cached
    style = lv.style_t()
    style.init()
    style.set_text_font_v2(font_path, line_height, FONT_FLASH_PORT)
    style.set_text_color(color)
    style.set_text_opa(255)
    style.set_bg_opa(0)
    style.set_border_width(0)
    style.set_pad_all(0)
    _styles[style_name] = style
    return style


def text_style_white(color):
    return make_text_style("white", color)


def text_style_green(color):
    return make_text_style("green", color)


def text_style_blue(color):
    return make_text_style("blue", color)


def text_style_red(color):
    return make_text_style("red", color)


def status_style_white(color):
    return make_text_style("status_white", color, STATUS_FONT_PATH,
                           STATUS_FONT_LINE_HEIGHT)


def status_style_green(color):
    return make_text_style("status_green", color, STATUS_FONT_PATH,
                           STATUS_FONT_LINE_HEIGHT)
