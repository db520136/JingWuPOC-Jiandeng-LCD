# -*- coding: utf-8 -*-
# ST7789 + CST816 tp固件手势，修复回调参数数量报错
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
COLOR_BLACK = 0x0000
DISPLAY_INVERTED = True

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

FONT_NAME = "watch_Semibold_24.bin"
FONT_LINE_HEIGHT = 33
FONT_FLASH_PORT = 0

def panel_color(color):
    if DISPLAY_INVERTED:
        return color ^ 0xFFFF
    return color

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

def make_text_style(color):
    style = lv.style_t()
    style.init()
    style.set_text_color(lv.color_hex(lvgl_color(color)))
    style.set_text_font_v2(FONT_NAME, FONT_LINE_HEIGHT, FONT_FLASH_PORT)
    style.set_text_align(lv.TEXT_ALIGN.CENTER)
    style.set_text_opa(255)
    style.set_bg_opa(0)
    style.set_border_width(0)
    style.set_pad_all(0)
    return style

# ===================== 内置EventMesh 修复：回调统一接收topic,msg =====================
class EventMeshLocal:
    def __init__(self):
        self._subs = dict()
    def subscribe(self, topic, cb):
        if topic not in self._subs:
            self._subs[topic] = []
        self._subs[topic].append(cb)
    def publish(self, topic, msg):
        if topic not in self._subs:
            return
        for cb in self._subs[topic]:
            try:
                cb(topic, msg)
            except Exception as e:
                print("[EventMesh cb err]", e)
EventMesh = EventMeshLocal()

class Abstract:
    pass

# ===================== Screen 页面类 =====================
class Screen(Abstract):
    def __init__(self, meta_obj):
        self.meta = meta_obj
        self.prop = None
        self.last_screen_info = None
    def set_prop(self, prop):
        self.prop = prop

# ===================== UI控制器 =====================
class UI(Abstract):
    def __init__(self):
        self.screens = []
        self.current = None
        self.tileview_map = {
            "talk_screen":     [0,0, lv.ANIM.OFF],
            "light_screen":    [1,0, lv.ANIM.OFF],
            "setting_tile_screen": [2,0, lv.ANIM.OFF],
        }
        self.tileview_position_map = {
            (0,0): "talk_screen",
            (1,0): "light_screen",
            (2,0): "setting_tile_screen",
        }

    def add_screen(self, screen):
        self.screens.append(screen)

    def _cb_load_screen(self, topic, msg):
        for screen in self.screens:
            if screen.NAME == msg["screen"]:
                scr = screen
                if self.current != scr:
                    if self.current:
                        try:
                            self.current.deactivate()
                        except Exception:
                            pass
                        scr.last_screen_info = {"screen": self.current.NAME}
                    scr.set_prop(msg)
                    self.current = scr
                    if msg["screen"] in self.tileview_map:
                        lv.scr_load(tileview_screen)
                        tileview.set_tile_id(*self.tileview_map[msg['screen']])
                    else:
                        lv.scr_load(self.current.meta)

    def _cb_tileview(self, topic, msg):
        x,y = msg
        if (x,y) in self.tileview_position_map:
            sn = self.tileview_position_map[(x,y)]
            EventMesh.publish("load_screen", {"screen": sn})

    def post_processor_after_instantiation(self):
        # 修复：用lambda捕获self，避免实例方法直接订阅导致参数个数错误
        EventMesh.subscribe("load_screen", lambda t,m: self._cb_load_screen(t,m))
        EventMesh.subscribe("load_tileview", lambda t,m: self._cb_tileview(t,m))

    def start(self):
        self.post_processor_after_instantiation()

# ===================== 全局弱引用，main内部赋值 =====================
ui = None
tileview_screen = None
tileview = None
setting_list_screen = None
spilcd = None

def tp_cb(para):
    global ui
    print("[TP手势] {} -> {}".format(para, TP_GESTURE.get(para,"未知手势")))
    try:
        if ui is not None and ui.current is not None and ui.current.NAME == "setting_list_screen":
            if para == 0:
                EventMesh.publish("load_screen", {"screen":"setting_tile_screen"})
    except Exception as e:
        print("手势回调异常", e)

def lvgl_flush(color_buf, x_start, y_start, x_end, y_end):
    flush_buf = bytearray(color_buf)
    spilcd.lcd_write(flush_buf, x_start, y_start, x_end, y_end)

# ---------------- 主入口：全部lv对象在这里创建！！----------------
def main():
    global ui, tileview_screen, tileview, setting_list_screen, spilcd
    # 1.LCD硬件初始化，不要碰LVGL
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
    spilcd.lcd_clear(panel_color(COLOR_BLACK))

    # 2.【重点】必须先lv.init()，之后才允许调用lv.obj / lv.tileview
    lv.init()

    # 3.创建LVGL buffer + display驱动
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

    # ========== lv.init之后，才可以创建所有LVGL控件 ==========
    style_text_white = make_text_style(0xFFFFFF)
    style_list_bg = lv.style_t()
    style_list_bg.init()
    style_list_bg.set_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    style_list_bg.set_bg_color(lv.color_hex(lvgl_color(0x222222)), lv.PART.MAIN | lv.STATE.DEFAULT)

    # tileview根容器
    tileview_screen = lv.obj()
    tileview_screen.set_size(LCD_WIDTH, LCD_HEIGHT)
    tileview_screen.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    tileview_screen.set_style_bg_color(lv.color_hex(lvgl_color(0x000000)), lv.PART.MAIN | lv.STATE.DEFAULT)
    tileview = lv.tileview(tileview_screen)

    def tileview_event_handler(e):
        code = e.get_code()
        if code == lv.EVENT.VALUE_CHANGED:
            t = e.get_target().get_tile_act()
            EventMesh.publish("load_tileview", (t.get_x(), t.get_y()))
    tileview.add_event_cb(tileview_event_handler, lv.EVENT.ALL, None)
    tileview.set_scroll_dir(lv.DIR.HOR)

    # tile0 对讲机
    tile0 = tileview.add_tile(0,0, lv.DIR.RIGHT | lv.DIR.LEFT)
    page_talk_screen = lv.obj(parent=tile0, size=(LCD_WIDTH, LCD_HEIGHT))
    page_talk_screen.set_style_bg_opa(255,lv.PART.MAIN|lv.STATE.DEFAULT)
    page_talk_screen.set_style_bg_color(lv.color_hex(lvgl_color(0x000000)),lv.PART.MAIN|lv.STATE.DEFAULT)
    label_talk = lv.label(page_talk_screen)
    label_talk.set_size(LCD_WIDTH, FONT_LINE_HEIGHT)
    label_talk.set_pos(0, (LCD_HEIGHT-FONT_LINE_HEIGHT)//2)
    label_talk.add_style(style_text_white, lv.PART.MAIN | lv.STATE.DEFAULT)
    label_talk.set_text("对讲机")

    # tile1 肩灯
    tile1 = tileview.add_tile(1,0, lv.DIR.RIGHT | lv.DIR.LEFT)
    page_light_screen = lv.obj(parent=tile1, size=(LCD_WIDTH, LCD_HEIGHT))
    page_light_screen.set_style_bg_opa(255,lv.PART.MAIN|lv.STATE.DEFAULT)
    page_light_screen.set_style_bg_color(lv.color_hex(lvgl_color(0x000000)),lv.PART.MAIN|lv.STATE.DEFAULT)
    label_light = lv.label(page_light_screen)
    label_light.set_size(LCD_WIDTH, FONT_LINE_HEIGHT)
    label_light.set_pos(0, (LCD_HEIGHT-FONT_LINE_HEIGHT)//2)
    label_light.add_style(style_text_white, lv.PART.MAIN | lv.STATE.DEFAULT)
    label_light.set_text("肩灯")

    # tile2 设置瓦片页
    tile2 = tileview.add_tile(2,0, lv.DIR.RIGHT | lv.DIR.LEFT)
    page_setting_tile_screen = lv.obj(parent=tile2, size=(LCD_WIDTH, LCD_HEIGHT))
    page_setting_tile_screen.set_style_bg_opa(255,lv.PART.MAIN|lv.STATE.DEFAULT)
    page_setting_tile_screen.set_style_bg_color(lv.color_hex(lvgl_color(0x000000)),lv.PART.MAIN|lv.STATE.DEFAULT)
    page_setting_tile_screen.add_flag(lv.obj.FLAG.CLICKABLE)
    label_setting_tile = lv.label(page_setting_tile_screen)
    label_setting_tile.set_size(LCD_WIDTH, FONT_LINE_HEIGHT)
    label_setting_tile.set_pos(0, (LCD_HEIGHT-FONT_LINE_HEIGHT)//2)
    label_setting_tile.add_style(style_text_white, lv.PART.MAIN | lv.STATE.DEFAULT)
    label_setting_tile.set_text("设置")

    def cb_click_setting_tile(event):
        EventMesh.publish("load_screen", {"screen":"setting_list_screen"})
    page_setting_tile_screen.add_event_cb(cb_click_setting_tile, lv.EVENT.CLICKED, None)

    # 设置列表独立页面
    setting_list_screen = lv.obj(size=(LCD_WIDTH, LCD_HEIGHT))
    setting_list_screen.set_style_bg_opa(255,lv.PART.MAIN|lv.STATE.DEFAULT)
    setting_list_screen.set_style_bg_color(lv.color_hex(lvgl_color(0x000000)),lv.PART.MAIN|lv.STATE.DEFAULT)
    setting_list = lv.list(setting_list_screen)
    setting_list.set_pos(0, 10)
    setting_list.set_size(LCD_WIDTH, LCD_HEIGHT-20)
    menu_items = ["设置", "设备信息", "肩灯亮度", "屏幕亮度", "声音", "跌倒检查", "升级", "组呼"]
    for txt in menu_items:
        item = setting_list.add_btn(txt)
        item.add_style(style_list_bg, lv.PART.MAIN | lv.STATE.DEFAULT)

    # 实例Screen 内部类
    class TalkScreen(Screen):
        NAME = "talk_screen"
    class LightScreen(Screen):
        NAME = "light_screen"
    class SettingTileScreen(Screen):
        NAME = "setting_tile_screen"
    class SettingListScreen(Screen):
        NAME = "setting_list_screen"

    sc_talk = TalkScreen(page_talk_screen)
    sc_light = LightScreen(page_light_screen)
    sc_set_tile = SettingTileScreen(page_setting_tile_screen)
    sc_set_list = SettingListScreen(setting_list_screen)

    ui = UI()
    ui.add_screen(sc_talk)
    ui.add_screen(sc_light)
    ui.add_screen(sc_set_tile)
    ui.add_screen(sc_set_list)
    ui.start()
    EventMesh.publish("load_screen", {"screen":"talk_screen"})

    # 4.触摸CST816：display注册完成之后，再初始化indev
    tp = cst816(irq=10, reset=15)
    tp.activate()
    tp.init()
    tp.set_callback(tp_cb)

    indev_drv = lv.indev_drv_t()
    indev_drv.init()
    indev_drv.type = lv.INDEV_TYPE.POINTER
    indev_drv.read_cb = tp.read
    indev_drv.long_press_time = 80
    indev_drv.register()

    print("====系统就绪====")
    while True:
        lv.tick_inc(5)
        lv.task_handler()
        utime.sleep_ms(5)

if __name__ == "__main__":
    main()