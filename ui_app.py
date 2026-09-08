# -*- coding: utf-8 -*-
"""最小 LVGL 导航验证应用。"""

import utime

import lvgl as lv

try:
    from usr.display_port import DisplayPort
    from usr.touch_port import TouchPort
    from usr.ui_pages import PageFactory
    from usr.ui_router import Router
    from usr.ui_commands import UICommandClient, UICommandQueue
    from usr.ui_messages import UIMessageBus
    from usr.ui_state import get_default_state_store
except Exception:
    from display_port import DisplayPort
    from touch_port import TouchPort
    from ui_pages import PageFactory
    from ui_router import Router
    from ui_commands import UICommandClient, UICommandQueue
    from ui_messages import UIMessageBus
    from ui_state import get_default_state_store


ROOT_NAMES = ("location", "shoulder", "intercom", "settings")
ROOT_TITLES = ("定位", "肩灯", "对讲机", "设置")
CLICK_MAX_DISTANCE_PX = 6
PAGE_SWITCH_DISTANCE_PX = 40
ROOT_SCROLL_THROW = 40
ROOT_SCROLL_LIMIT = 5


class NavigationTestApp:
    def __init__(self, battery=None, network=None, lamp=None,
                 low_priority_worker=None, low_priority_worker_factory=None,
                 sensor_service=None, command_queue=None,
                 state_store=None, message_bus=None):
        self.width = 296
        self.height = 240
        self.display = DisplayPort(self.width, self.height)
        self.command_queue = (command_queue if command_queue is not None
                              else UICommandQueue())
        self.command_client = UICommandClient(self.command_queue)
        self.state_store = (state_store if state_store is not None
                            else get_default_state_store())
        self.message_bus = (message_bus if message_bus is not None
                            else UIMessageBus())
        # 业务对象由启动入口显式创建并注入；UI 不创建硬件对象，也不访问
        # ADC、网络或 I2C。未注入 Worker 时仍可作为纯导航验证程序运行。
        self.battery = battery
        self.network = network
        self.lamp = lamp
        self.low_priority_worker = low_priority_worker
        self.sensor_service = sensor_service
        if (self.low_priority_worker is None and
                low_priority_worker_factory is not None):
            self.low_priority_worker = low_priority_worker_factory(
                command_queue=self.command_queue,
                state_store=self.state_store,
                message_bus=self.message_bus,
                battery=battery,
                network=network,
                lamp=lamp,
            )
        # 延后 I2C/触摸对象创建，确保 LCD 与 LVGL 已先完成初始化。
        self.touch = None
        self.router = Router(self)
        self.pages = PageFactory(self)
        self.root_screen = None
        self.nav_host = None
        self.nav_tiles = {}
        self.root_content = None
        self.child_screen = None
        self.root_view = None
        self.child_view = None
        self.root_pages = {}
        self.last_ms = utime.ticks_ms()
        self._last_pressed = False
        self.touch_start_point = None
        self.touch_dx = 0
        self.touch_dy = 0
        self.touch_moved = False
        self.touch_horizontal_swipe = False
        self.touch_session_click_allowed = True
        self._root_session_index = 0
        self._ui_state_revision = -1
        self._ui_busy = False
        self._release_ui_after_task = False

    @staticmethod
    def _remove_frame(obj):
        """移除LVGL默认边框和外轮廓，不改变对象布局或滚动行为。"""
        obj.set_style_border_width(0, lv.PART.MAIN)
        obj.set_style_border_opa(0, lv.PART.MAIN)
        obj.set_style_outline_width(0, lv.PART.MAIN)
        obj.set_style_outline_opa(0, lv.PART.MAIN)

    def _set_ui_busy(self, busy, reason):
        busy = bool(busy)
        if busy == self._ui_busy:
            return
        self._ui_busy = busy
        released_periodic = self.command_queue.set_ui_busy(busy)

    def _make_root(self):
        self.root_screen = lv.obj()
        self.root_screen.set_size(self.width, self.height)
        self._remove_frame(self.root_screen)
        self.root_screen.set_style_bg_color(lv.color_hex(0), lv.PART.MAIN)
        self.root_screen.set_style_bg_opa(255, lv.PART.MAIN)
        self.nav_host = lv.tileview(self.root_screen)
        self.nav_host.set_size(self.width, self.height)
        self._remove_frame(self.nav_host)
        self.nav_host.set_scroll_dir(lv.DIR.HOR)
        self.nav_host.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        try:
            self.nav_host.set_scroll_chain(lv.DIR.NONE)
        except Exception:
            pass
        # 禁止越界时的横向弹性回弹。有效方向仍由每个 tile 的 dir 控制。
        self._disable_horizontal_elastic(self.nav_host)
        # 根槽位不接受用户横向拖动；一级页面由内部 root_content 独立处理。
        # 子槽位通过 DIR.LEFT 提供向右返回根页面的路径。
        root_tile = self.nav_host.add_tile(0, 0, lv.DIR.NONE)
        self._remove_frame(root_tile)
        self.nav_tiles[0] = root_tile
        self.root_content = lv.tileview(root_tile)
        self.root_content.set_size(self.width, self.height)
        self._remove_frame(self.root_content)
        self.root_content.set_scroll_dir(lv.DIR.HOR)
        self.root_content.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        try:
            self.root_content.set_scroll_chain(lv.DIR.NONE)
        except Exception:
            pass
        self._disable_horizontal_elastic(self.root_content)
        for index, name in enumerate(ROOT_NAMES):
            # 第一阶段仅放开 tileview 对当前触摸会话的双向处理；实际
            # 是否能切换仍由相邻 tile 拓扑决定，首尾页面没有相邻页时
            # 仍保持在边界。这样不引入自定义页面位移控制。
            tile_dir = lv.DIR.LEFT | lv.DIR.RIGHT
            tile = self.root_content.add_tile(index, 0, tile_dir)
            self._remove_frame(tile)
            self.root_pages[name] = self.pages.root(
                name, ROOT_TITLES[index], parent=tile)
        self.root_content.add_event_cb(self._root_changed, lv.EVENT.VALUE_CHANGED, None)
        self.nav_host.add_event_cb(self._nav_changed, lv.EVENT.VALUE_CHANGED, None)
        self.root_view = 0
        lv.scr_load(self.root_screen)
        self.root_screen.invalidate()

    @staticmethod
    def _disable_horizontal_elastic(obj):
        """关闭 tileview 边界横向弹性，避免无效方向出现晃动回弹。"""
        try:
            obj.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
        except Exception:
            # 某些 QuecPython LVGL 版本没有导出该 flag；tile 的方向限制
            # 仍然生效，因此不阻断启动。
            pass

    def _begin_touch_session(self):
        self._set_ui_busy(True, "触摸开始")
        self._release_ui_after_task = False
        self.touch_start_point = tuple(self.touch.point)
        self.touch_dx = 0
        self.touch_dy = 0
        self.touch_moved = False
        self.touch_horizontal_swipe = False
        self.touch_session_click_allowed = True
        self._root_session_index = self.root_view

    def _update_touch_session(self):
        if self.touch_start_point is None or not self.touch.pressed:
            return
        self.touch_dx = int(self.touch.point[0]) - int(self.touch_start_point[0])
        self.touch_dy = int(self.touch.point[1]) - int(self.touch_start_point[1])
        distance = max(abs(self.touch_dx), abs(self.touch_dy))
        if distance > CLICK_MAX_DISTANCE_PX:
            self.touch_moved = True
            self.touch_session_click_allowed = False
        if (abs(self.touch_dx) >= PAGE_SWITCH_DISTANCE_PX and
                abs(self.touch_dx) > abs(self.touch_dy) * 1.2):
            self.touch_horizontal_swipe = True

    def can_click_current_touch(self):
        """按钮 CLICKED 回调使用：只有全程近似原地才允许点击。"""
        return bool(self.touch_session_click_allowed and
                    not self.touch_moved)

    def _root_changed(self, event):
        try:
            tile = event.get_target().get_tile_act()
            index = int(tile.get_x() / self.width)
            self.root_view = index
            if (self._root_session_index is not None and
                    not self.touch_horizontal_swipe and self.touch_moved):
                try:
                    self.root_content.set_tile_id(
                        int(self._root_session_index), 0, lv.ANIM.OFF)
                except Exception:
                    try:
                        self.root_content.set_tile_id(
                            int(self._root_session_index), 0)
                    except Exception:
                        pass
                self.root_view = self._root_session_index
        except Exception as error:
            # LVGL event callbacks must not bring down the UI loop.
            self.root_view = None

    def _active_nav_depth(self):
        if self.router.current is None:
            return 0
        return len(self.router.stack)

    def _nav_changed(self, event):
        """处理子页面槽位的原生 LVGL 横向返回。"""
        try:
            tile = event.get_target().get_tile_act()
            index = int(tile.get_x() / self.width)
            expected = self._active_nav_depth()
            if index == expected:
                return
            # 只接受统一阈值定义的向右有效滑动；短滑动由 LVGL 回弹。
            if (index < expected and self.touch_horizontal_swipe and
                    self.touch_dx >= PAGE_SWITCH_DISTANCE_PX and
                    self.touch_dx > abs(self.touch_dy) * 1.2):
                while self._active_nav_depth() > index:
                    if not self.router.pop():
                        break
                return
            self.nav_host.set_tile_id(expected, 0, lv.ANIM.OFF)
        except Exception:
            try:
                self.nav_host.set_tile_id(
                    self._active_nav_depth(), 0, lv.ANIM.OFF)
            except Exception:
                pass

    def _remove_route_tiles_from(self, depth):
        for index in sorted(list(self.nav_tiles.keys()), reverse=True):
            if index < depth or index == 0:
                continue
            tile = self.nav_tiles.pop(index)
            try:
                tile.delete()
            except Exception:
                try:
                    lv.obj_del(tile)
                except Exception:
                    pass

    def _make_child_tile(self, depth, page_info):
        self._remove_route_tiles_from(depth)
        # 子页面同样允许 tileview 在当前触摸会话中双向处理；当前路径
        # 没有下一级 tile 时，向左仍没有可切换目标，页面保持不动。
        tile = self.nav_host.add_tile(
            depth, 0, lv.DIR.LEFT | lv.DIR.RIGHT)
        self._remove_frame(tile)
        self.nav_tiles[depth] = tile
        self.child_screen = tile
        self.pages.child(page_info[0], page_info[2], parent=tile)
        self._disable_horizontal_elastic(tile)
        return tile

    def _show_child(self, page_info, direction="forward"):
        depth = len(self.router.stack)
        if direction == "back" and depth in self.nav_tiles:
            self.child_screen = self.nav_tiles[depth]
        else:
            self._make_child_tile(depth, page_info)
        try:
            self.nav_host.set_tile_id(
                depth, 0, lv.ANIM.ON if direction == "forward" else lv.ANIM.OFF)
        except Exception:
            self.nav_host.set_tile_id(depth, 0)
        self.nav_tiles[depth].invalidate()

    def show_root_page(self):
        self.child_screen = self.nav_tiles.get(0)
        try:
            self.nav_host.set_tile_id(0, 0, lv.ANIM.OFF)
        except Exception:
            self.nav_host.set_tile_id(0, 0)
        # 确认活动槽位已回到根页面后，再释放当前路径中的子页面槽位；
        # 根槽位内部的 root_content 保持原来的一级页面索引，因此不会显示 A/B 过渡帧。
        self._remove_route_tiles_from(1)
        self.root_screen.invalidate()

    def show_child_page(self, page_info, direction="forward"):
        self._show_child(page_info, direction)

    def open_child(self, name):
        self.router.push(name, params={})

    def start(self):
        self.display.start()
        self.touch = TouchPort()
        self.touch.start()
        self.touch.configure_scroll(ROOT_SCROLL_THROW, ROOT_SCROLL_LIMIT)
        self._make_root()
        if self.low_priority_worker is not None:
            self.low_priority_worker.start()
        if self.sensor_service is not None:
            self.sensor_service.start()
        self.last_ms = utime.ticks_ms()
        while True:
            now = utime.ticks_ms()
            elapsed = utime.ticks_diff(now, self.last_ms)
            self.last_ms = now
            was_pressed = self.touch.pressed
            self.touch.poll()
            if self.touch.pressed and not was_pressed:
                self._begin_touch_session()
            if self.touch.pressed:
                self._update_touch_session()
            if (not self.touch.pressed) and was_pressed:
                # 保留本次会话结果到本轮 task_handler，按钮 CLICKED 回调
                # 才能据此区分点击和滑动。
                self._update_touch_session()
                self._release_ui_after_task = True
            self._refresh_page_state()
            self.display.tick(elapsed)
            self.display.task_handler()
            if self._release_ui_after_task:
                self._release_ui_after_task = False
                self._set_ui_busy(False, "触摸/点击本轮LVGL处理完成")
            utime.sleep_ms(1)

    def _refresh_page_state(self):
        changed = self.state_store.get_snapshot_if_changed()
        if changed is None:
            return
        revision, snapshot = changed
        self._ui_state_revision = revision
        self.pages.refresh_state(snapshot)


def main(battery=None, network=None, lamp=None,
         low_priority_worker=None, low_priority_worker_factory=None,
         sensor_service=None, command_queue=None, state_store=None,
         message_bus=None):
    NavigationTestApp(
        battery=battery,
        network=network,
        lamp=lamp,
        low_priority_worker=low_priority_worker,
        low_priority_worker_factory=low_priority_worker_factory,
        sensor_service=sensor_service,
        command_queue=command_queue,
        state_store=state_store,
        message_bus=message_bus,
    ).start()
