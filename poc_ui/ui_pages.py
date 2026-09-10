# -*- coding: utf-8 -*-
"""正式 UI 页面（纯 UI 基线，不访问任何真实业务硬件）。"""

import lvgl as lv

try:
    from usr.font_port import (text_style_blue, text_style_green,
                               text_style_red, text_style_white,
                               status_style_white)
except Exception:
    from font_port import (text_style_blue, text_style_green,
                           text_style_red, text_style_white,
                           status_style_white)

BLACK = lv.color_hex(0x000000)
WHITE = lv.color_hex(0xFFFFFF)
CYAN = lv.color_hex(0x00D7FF)
GRAY = lv.color_hex(0x303840)
GREEN = lv.color_hex(0x168A45)
RED = lv.color_hex(0xE00000)
BLUE = lv.color_hex(0x12618A)
# 登录输入框使用设计稿取样得到的深灰色（RGB 42,42,42）。
LOGIN_INPUT_BG = lv.color_hex(0x2A2A2A)
ASSET_ROOT = "U:/"


def _remove_frame(obj):
    obj.set_style_border_width(0, lv.PART.MAIN)
    obj.set_style_border_opa(0, lv.PART.MAIN)
    obj.set_style_outline_width(0, lv.PART.MAIN)
    obj.set_style_outline_opa(0, lv.PART.MAIN)
    try:
        obj.set_style_width(0, lv.PART.SCROLLBAR)
        obj.set_style_bg_opa(0, lv.PART.SCROLLBAR)
        obj.set_style_border_width(0, lv.PART.SCROLLBAR)
        obj.set_style_border_opa(0, lv.PART.SCROLLBAR)
    except Exception:
        pass


def _make_fixed(obj):
    """将页面或控件设为固定对象，不参与任何方向滚动。"""
    try:
        obj.clear_flag(lv.obj.FLAG.SCROLLABLE)
    except Exception:
        pass
    try:
        obj.set_scroll_dir(lv.DIR.NONE)
    except Exception:
        pass
    try:
        obj.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    except Exception:
        pass
    try:
        obj.set_scroll_chain(lv.DIR.NONE)
    except Exception:
        pass
    try:
        obj.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
    except Exception:
        pass
    try:
        obj.set_style_pad_all(0, lv.PART.MAIN)
    except Exception:
        pass


def _label(parent, text, x, y, w, h, color=WHITE,
           align=lv.TEXT_ALIGN.LEFT, status=False):
    obj = lv.label(parent)
    _remove_frame(obj)
    obj.set_pos(int(x), int(y))
    obj.set_size(int(w), int(h))
    obj.set_text(str(text))
    obj.set_style_text_align(align, lv.PART.MAIN)
    if status:
        style = status_style_white(color)
    elif color is GREEN:
        style = text_style_green(color)
    elif color is CYAN:
        style = text_style_blue(color)
    elif color is RED:
        style = text_style_red(color)
    else:
        style = text_style_white(color)
    obj.add_style(style, lv.PART.MAIN)
    return obj


def _button(parent, text, x, y, w, h, callback, host=None,
            color=GRAY, text_color=WHITE):
    btn = lv.btn(parent)
    _remove_frame(btn)
    _make_fixed(btn)
    btn.set_pos(int(x), int(y))
    btn.set_size(int(w), int(h))
    btn.set_style_bg_color(color, lv.PART.MAIN)
    btn.set_style_bg_opa(255, lv.PART.MAIN)
    try:
        btn.set_style_radius(6, lv.PART.MAIN)
        btn.set_style_shadow_width(0, lv.PART.MAIN)
    except Exception:
        pass
    try:
        btn.clear_flag(lv.obj.FLAG.SCROLLABLE)
    except Exception:
        pass
    _label(btn, text, 0, max(0, (int(h) - 33) // 2), int(w), 33,
           text_color, lv.TEXT_ALIGN.CENTER)
    if callback is not None:
        def guarded_callback(event):
            if host is None or host.can_click_current_touch():
                callback(event)
        btn.add_event_cb(guarded_callback, lv.EVENT.CLICKED, None)
    return btn


def _image(parent, path, x, y, w, h, callback=None, host=None):
    image = lv.img(parent)
    _remove_frame(image)
    image.set_pos(int(x), int(y))
    image.set_size(int(w), int(h))
    try:
        image.set_src(ASSET_ROOT + path)
    except Exception:
        pass
    if callback is not None:
        try:
            image.add_flag(lv.obj.FLAG.CLICKABLE)
        except Exception:
            pass
        def guarded_image(event):
            if host is None or host.can_click_current_touch():
                callback(event)
        image.add_event_cb(guarded_image, lv.EVENT.CLICKED, None)
    return image


class PageFactory:
    def __init__(self, app):
        self.app = app
        self._status_labels = {}
        self._location_labels = {}

    def create_status_bar(self, parent):
        bar = lv.obj(parent)
        _remove_frame(bar)
        _make_fixed(bar)
        bar.set_pos(0, 0)
        bar.set_size(self.app.width, self.app.status_height)
        bar.set_style_bg_color(BLACK, lv.PART.MAIN)
        bar.set_style_bg_opa(255, lv.PART.MAIN)
        self._status_labels["police"] = _label(
            bar, "M100001", 8, 3, 105, 24, WHITE, lv.TEXT_ALIGN.LEFT, True)
        self._status_network_icon = _image(bar, "wifi_5.png", 173, 3, 24, 24)
        self._status_labels["battery"] = _label(
            bar, "电量：99%", 220, 3, 74, 24, WHITE, lv.TEXT_ALIGN.RIGHT, True)
        return bar

    def refresh_status(self, snapshot):
        labels = self._status_labels
        if not labels:
            return
        police_no = snapshot.get("police_no") or "M100001"
        connected = bool(snapshot.get("network_connected"))
        battery = snapshot.get("battery_percent")
        labels["police"].set_text(str(police_no))
        try:
            self._status_network_icon.set_src(
                ASSET_ROOT + ("wifi_5.png" if connected else "wifi_0.png"))
        except Exception:
            pass
        labels["battery"].set_text("电量：{}".format(
            "--" if battery is None else "{}%".format(battery)))

    def create_login_page(self, parent):
        page = lv.obj(parent)
        _remove_frame(page)
        _make_fixed(page)
        page.set_pos(0, 0)
        page.set_size(self.app.width, self.app.height)
        page.set_style_bg_color(BLACK, lv.PART.MAIN)
        page.set_style_bg_opa(255, lv.PART.MAIN)
        self.app.login_prefix = "M"
        # M/F 按钮本身就是前缀选择指示，不额外创建隐藏控件。
        self.app.login_prefix_label = None
        self.app.login_m_button = _button(
            page, "M", 5, 4, 44, 38,
            lambda e: self.app.set_login_prefix("M"), self.app, BLUE)
        self.app.login_f_button = _button(
            page, "F", 53, 4, 44, 38,
            lambda e: self.app.set_login_prefix("F"), self.app, GRAY)
        input_box = lv.obj(page)
        _remove_frame(input_box)
        _make_fixed(input_box)
        input_box.set_pos(101, 4)
        input_box.set_size(190, 38)
        input_box.set_style_bg_color(LOGIN_INPUT_BG, lv.PART.MAIN)
        input_box.set_style_bg_opa(255, lv.PART.MAIN)
        input_box.set_style_radius(6, lv.PART.MAIN)
        self.app.login_display = _label(
            input_box, "", 8, 2, 174, 34, WHITE, lv.TEXT_ALIGN.LEFT)
        keys = (("1", "2", "3"), ("4", "5", "6"),
                ("7", "8", "9"), ("删除", "0", "确认"))
        key_w, key_h, gap_x, gap_y = 93, 44, 4, 4
        for row_index, row in enumerate(keys):
            for col_index, value in enumerate(row):
                x = 5 + col_index * (key_w + gap_x)
                y = 46 + row_index * (key_h + gap_y)
                if value == "删除":
                    callback = lambda e: self.app.login_delete()
                elif value == "确认":
                    callback = lambda e: self.app.login_confirm()
                else:
                    callback = lambda e, digit=value: self.app.login_digit(digit)
                if value == "确认":
                    button_color = GREEN
                elif value == "删除":
                    button_color = RED
                else:
                    button_color = GRAY
                _button(page, value, x, y, key_w, key_h, callback, self.app,
                        button_color)
        # 本页面没有提示语、标题或空白滚动区域；最后一行控件结束于234px，
        # 保留6px安全边距，避免圆角边缘贴屏造成裁切。
        self.app.login_message = None
        return page

    def update_login_prefix(self, prefix):
        """更新登录页 M/F 前缀按钮的选中状态。"""
        selected = str(prefix)
        if self.app.login_m_button is not None:
            self.app.login_m_button.set_style_bg_color(
                BLUE if selected == "M" else GRAY, lv.PART.MAIN)
            self.app.login_m_button.invalidate()
        if self.app.login_f_button is not None:
            self.app.login_f_button.set_style_bg_color(
                BLUE if selected == "F" else GRAY, lv.PART.MAIN)
            self.app.login_f_button.invalidate()

    def root(self, name, title, parent=None):
        page = lv.obj(parent if parent is not None else self.app.root_content)
        _remove_frame(page)
        _make_fixed(page)
        page.set_size(self.app.content_width, self.app.content_height)
        page.set_style_bg_color(BLACK, lv.PART.MAIN)
        page.set_style_bg_opa(255, lv.PART.MAIN)
        if name == "location":
            self._make_location(page)
        else:
            # 92x92 SJPG 是设备端已验证的图片格式；PNG 资源仍可用于列表图标。
            icons = {"shoulder": "shoulder_lamp.jpg", "intercom": "intercom.jpg",
                     "settings": "settings.jpg"}
            targets = {"shoulder": "shoulder_menu", "intercom": "intercom_menu",
                       "settings": "settings_menu"}
            _image(page, icons[name], 102, 26, 92, 92,
                   lambda e, n=name: self.app.open_child(targets[n]), self.app)
            _label(page, title, 0, 126, self.app.content_width, 40,
                   WHITE, lv.TEXT_ALIGN.CENTER)
            _label(page, "点击图标进入", 0, 174, self.app.content_width, 30,
                   CYAN, lv.TEXT_ALIGN.CENTER)
        return page

    def _make_location(self, page):
        specs = (("name", "潘一"), ("longitude", "经度：120.7899°"),
                 ("latitude", "纬度：120.7899°"), ("altitude", "海拔：80.1"),
                 ("satellites", "卫星数量：12"))
        ys = (3, 39, 75, 111, 147)
        for index, item in enumerate(specs):
            self._location_labels[item[0]] = _label(
                page, item[1], 8, ys[index], 184, 33, WHITE,
                lv.TEXT_ALIGN.LEFT)
        self._location_labels["time"] = _label(
            page, "10:44:59", 190, 48, 100, 33, WHITE,
            lv.TEXT_ALIGN.CENTER)
        self._location_labels["weekday"] = _label(
            page, "星期三", 190, 84, 100, 33, WHITE,
            lv.TEXT_ALIGN.CENTER)
        _image(page, "location_icon_92.sjpg", 196, 112, 92, 92,
               lambda e: self.app.open_child("location_detail"), self.app)

    def refresh_state(self, snapshot):
        self.refresh_status(snapshot)
        values = (("name", snapshot.get("location_name") or "潘一"),
                  ("longitude", "经度：{}°".format(snapshot.get("longitude") or "120.7899")),
                  ("latitude", "纬度：{}°".format(snapshot.get("latitude") or "120.7899")),
                  ("altitude", "海拔：{}".format(snapshot.get("altitude") or "80.1")),
                  ("satellites", "卫星数量：{}".format(snapshot.get("satellites") or "12")),
                  ("time", snapshot.get("location_time") or "10:44:59"),
                  ("weekday", snapshot.get("location_weekday") or "星期三"))
        for key, value in values:
            label = self._location_labels.get(key)
            if label is not None:
                label.set_text(str(value))
                label.invalidate()

    def child(self, name, params=None, parent=None):
        page = lv.obj(parent if parent is not None else self.app.child_screen)
        _remove_frame(page)
        _make_fixed(page)
        page.set_size(self.app.content_width, self.app.content_height)
        page.set_style_bg_color(BLACK, lv.PART.MAIN)
        page.set_style_bg_opa(255, lv.PART.MAIN)
        if name == "location_detail":
            self._make_location_detail(page)
        elif name == "shoulder_menu":
            self._make_choice_page(page, "肩灯", (("肩灯模式", "shoulder_mode"),
                                                  ("双击模式", "double_click_mode")))
        elif name == "intercom_menu":
            self._make_choice_page(page, "对讲机", (("组呼模式", "group_mode"),
                                                   ("单呼模式", "single_mode")))
        elif name == "settings_menu":
            self._make_settings(page)
        else:
            self._make_placeholder(page, name)
        return page

    def _make_location_detail(self, page):
        _revision, snapshot = self.app.state_store.get_snapshot()
        police_name = snapshot.get("police_name") or "潘荣丰"
        police_no = snapshot.get("police_no") or "M100001"
        _label(page, "用户", 0, 18, 296, 33, WHITE, lv.TEXT_ALIGN.CENTER)
        _label(page, police_name, 0, 70, 296, 33, WHITE, lv.TEXT_ALIGN.CENTER)
        _label(page, "警号：{}".format(police_no), 0, 122, 296, 33, WHITE,
               lv.TEXT_ALIGN.CENTER)
        _button(page, "切换用户", 4, 170, 288, 40,
                lambda e: self.app.show_message("当前为模拟页面"), self.app, RED)

    def _make_choice_page(self, page, title, choices):
        _label(page, title, 0, 8, 296, 33, CYAN, lv.TEXT_ALIGN.CENTER)
        for index, choice in enumerate(choices):
            _button(page, choice[0], 18, 56 + index * 60, 260, 48,
                    lambda e, target=choice[1]: self.app.open_child(target),
                    self.app, GRAY)

    def _make_settings(self, page):
        _label(page, "设置", 0, 4, 296, 33, CYAN, lv.TEXT_ALIGN.CENTER)
        viewport = lv.obj(page)
        _remove_frame(viewport)
        viewport.set_pos(8, 40)
        viewport.set_size(280, 166)
        viewport.set_scroll_dir(lv.DIR.VER)
        viewport.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        try:
            viewport.set_scroll_chain(lv.DIR.NONE)
        except Exception:
            pass
        try:
            viewport.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
        except Exception:
            pass
        viewport.add_flag(lv.obj.FLAG.SCROLLABLE)
        rows = (("settings_device.png", "设备信息", "device_info"),
                ("settings_brightness.png", "屏幕亮度", "brightness"),
                ("settings_shoulder_brightness.png", "肩灯亮度", "lamp_brightness"),
                ("settings_volume.png", "音量", "volume"),
                ("settings_sleep.png", "息屏时间", "sleep"),
                ("settings_network.png", "网络状态", "network"),
                ("settings_fall_detection.png", "跌倒检测", "fall_detection"),
                (None, "升级", "upgrade"))
        for index, row_data in enumerate(rows):
            row = lv.btn(viewport)
            _remove_frame(row)
            row.set_pos(0, index * 46)
            row.set_size(280, 42)
            row.set_style_bg_color(GRAY, lv.PART.MAIN)
            row.set_style_bg_opa(255, lv.PART.MAIN)
            if row_data[0] is not None:
                _image(row, row_data[0], 6, 5, 32, 32)
            _label(row, row_data[1], 46, 4, 220, 33, WHITE,
                   lv.TEXT_ALIGN.LEFT)
            def guarded_row(event, target=row_data[2]):
                if self.app.can_click_current_touch():
                    self.app.open_child(target)
            row.add_event_cb(guarded_row, lv.EVENT.CLICKED, None)

    def _make_placeholder(self, page, name):
        titles = {"shoulder_mode": "肩灯模式", "double_click_mode": "双击模式",
                  "group_mode": "组呼模式", "single_mode": "单呼模式",
                  "device_info": "设备信息", "brightness": "屏幕亮度",
                  "lamp_brightness": "肩灯亮度", "volume": "音量",
                  "sleep": "息屏时间", "network": "网络状态",
                  "fall_detection": "跌倒检测", "upgrade": "升级"}
        _label(page, titles.get(name, name), 0, 55, 296, 40,
               WHITE, lv.TEXT_ALIGN.CENTER)
        _label(page, "当前为 UI 交互预览", 0, 110, 296, 33,
               CYAN, lv.TEXT_ALIGN.CENTER)

    def show_message(self, text):
        message = self.app.login_message
        if message is not None:
            message.set_text(str(text))
