# -*- coding: utf-8 -*-
"""最小导航验证页面，不连接任何真实业务服务。"""

import lvgl as lv


BLACK = lv.color_hex(0x000000)
WHITE = lv.color_hex(0xFFFFFF)
CYAN = lv.color_hex(0x00D7FF)
GRAY = lv.color_hex(0x303840)
GREEN = lv.color_hex(0x168A45)


def _label(parent, text, x, y, w=296, h=34, color=WHITE):
    obj = lv.label(parent)
    obj.set_pos(x, y)
    obj.set_size(w, h)
    obj.set_text(text)
    obj.set_style_text_color(color, lv.PART.MAIN)
    obj.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN)
    return obj


def _button(parent, text, x, y, w=260, h=48, callback=None, host=None):
    btn = lv.btn(parent)
    try:
        # 按钮只负责点击，不允许自身参与页面级滚动。
        btn.clear_flag(lv.obj.FLAG.SCROLLABLE)
    except Exception:
        pass
    btn.set_pos(x, y)
    btn.set_size(w, h)
    btn.set_style_bg_color(GRAY, lv.PART.MAIN)
    btn.set_style_bg_opa(255, lv.PART.MAIN)
    _label(btn, text, 0, 7, w, 34, WHITE)
    if callback is not None:
        def guarded_callback(event):
            if host is not None and not host.can_click_current_touch():
                return
            callback(event)
        btn.add_event_cb(guarded_callback, lv.EVENT.CLICKED, None)
    return btn


class PageFactory:
    def __init__(self, app):
        self.app = app
        self._root_state_labels = {}

    def root(self, name, title, parent=None):
        page = lv.obj(parent if parent is not None else self.app.root_content)
        page.set_size(self.app.width, self.app.height)
        try:
            # 一级页面的横向滚动只能由外层 tileview 处理。
            page.clear_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass
        page.set_style_bg_color(BLACK, lv.PART.MAIN)
        page.set_style_bg_opa(255, lv.PART.MAIN)
        _label(page, title, 0, 22, self.app.width, 42, CYAN)
        _label(page, "一级页面：左右滑动", 0, 70, self.app.width, 30, WHITE)
        state_label = _label(page, "业务状态准备中", 0, 96,
                             self.app.width, 24, GREEN)
        self._root_state_labels[name] = state_label
        if name == "location":
            _button(page, "进入定位详情", 18, 125, 260, 50,
                    lambda e: self.app.open_child("location_detail"),
                    host=self.app)
        elif name == "shoulder":
            _button(page, "进入肩灯菜单", 18, 125, 260, 50,
                    lambda e: self.app.open_child("shoulder_menu"),
                    host=self.app)
        elif name == "intercom":
            _button(page, "进入对讲机菜单", 18, 125, 260, 50,
                    lambda e: self.app.open_child("intercom_menu"),
                    host=self.app)
        else:
            _button(page, "进入设置菜单", 18, 125, 260, 50,
                    lambda e: self.app.open_child("settings_menu"),
                    host=self.app)
        _label(page, "点击按钮进入二级页面", 0, 205, self.app.width, 26, WHITE)
        return page

    def refresh_state(self, snapshot):
        text = "网络:{} 电量:{}%".format(
            "在线" if snapshot["network_connected"] else "离线",
            "--" if snapshot["battery_percent"] is None else
            snapshot["battery_percent"])
        for label in self._root_state_labels.values():
            label.set_text(text)
            label.invalidate()

    def child(self, name, params=None, parent=None):
        page = lv.obj(parent if parent is not None else self.app.child_screen)
        page.set_size(self.app.width, self.app.height)
        try:
            # 子页面本身不可横向拖动；返回动画由 Router/LVGL screen
            # transition 统一执行。列表 viewport 单独保留垂直滚动。
            page.clear_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass
        page.set_style_bg_color(BLACK, lv.PART.MAIN)
        page.set_style_bg_opa(255, lv.PART.MAIN)
        _label(page, name, 0, 18, self.app.width, 38, CYAN)
        _label(page, "任意位置向右滑返回上一级", 0, 58, self.app.width, 28, WHITE)
        if name in ("intercom_menu", "settings_menu", "shoulder_menu"):
            _button(page, "进入三级页面", 18, 105, 260, 48,
                    lambda e: self.app.open_child(
                        "group_mode" if name == "intercom_menu" else
                        ("brightness" if name == "shoulder_menu" else "device_info")),
                    host=self.app)
            viewport = lv.obj(page)
            viewport.set_pos(8, 164)
            viewport.set_size(280, 68)
            viewport.set_scroll_dir(lv.DIR.VER)
            viewport.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
            viewport.add_flag(lv.obj.FLAG.SCROLLABLE)
            self._add_list(viewport, 0, 5)
        else:
            _label(page, "三级页面", 0, 105, self.app.width, 32, GREEN)
            _label(page, "再次右滑返回上一级", 0, 150, self.app.width, 30, WHITE)
        return page

    @staticmethod
    def _add_list(page, y, count):
        for index in range(count):
            row = lv.obj(page)
            row.set_pos(18, y + index * 42)
            row.set_size(260, 34)
            row.set_style_bg_color(GRAY, lv.PART.MAIN)
            row.set_style_bg_opa(255, lv.PART.MAIN)
            _label(row, "菜单列表项 {}（上下滑动区域）".format(index + 1),
                   0, 3, 260, 26, WHITE)
