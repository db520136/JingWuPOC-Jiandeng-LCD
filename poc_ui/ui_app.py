# -*- coding: utf-8 -*-
"""正式 UI 交互基线应用（不启动真实业务线程）。"""

import gc
import utime

import lvgl as lv

try:
	from usr.display_port import DisplayPort
	from usr.touch_port import TouchPort
	from usr.ui_pages import PageFactory
	from usr.ui_router import Router
	from usr.ui_commands import UICommandQueue
	from usr.ui_messages import UIMessageBus
	from usr.ui_state import get_default_state_store
except Exception:
	from display_port import DisplayPort
	from touch_port import TouchPort
	from ui_pages import PageFactory
	from ui_router import Router
	from ui_commands import UICommandQueue
	from ui_messages import UIMessageBus
	from ui_state import get_default_state_store


ROOT_NAMES = ("location", "shoulder", "intercom", "settings")
ROOT_TITLES = ("定位", "肩灯", "对讲机", "设置")
CLICK_MAX_DISTANCE_PX = 6
PAGE_SWITCH_DISTANCE_PX = 40
ROOT_SCROLL_THROW = 40
ROOT_SCROLL_LIMIT = 5
# 子页面原生切换动画期间禁止重复点击，避免重复创建 tile/JPG 对象。
CHILD_TRANSITION_LOCK_MS = 600


class NavigationTestApp:
	def __init__(self, battery=None, network=None, lamp=None,
				 low_priority_worker=None, low_priority_worker_factory=None,
				 sensor_service=None, command_queue=None,
				 state_store=None, message_bus=None):
		self.width = 296
		self.height = 240
		self.status_height = 30
		self.content_height = self.height - self.status_height
		self.content_width = self.width
		self.display = DisplayPort(self.width, self.height)
		self.command_queue = (command_queue if command_queue is not None
							  else UICommandQueue())
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
		self.root_pages = {}
		self.status_bar = None
		self.login_page = None
		self.login_prefix = "M"
		self.login_digits = ""
		self.login_m_button = None
		self.login_f_button = None
		self.login_display = None
		self._logged_in = False
		self._login_transition_pending = False
		self.last_ms = utime.ticks_ms()
		self._last_pressed = False
		self.touch_start_point = None
		self.touch_dx = 0
		self.touch_dy = 0
		self.touch_moved = False
		self.touch_horizontal_swipe = False
		self.touch_session_click_allowed = True
		self._root_session_index = 0
		self._ui_busy = False
		self._release_ui_after_task = False
		self._child_transition_busy = False
		self._child_transition_until = 0

	@staticmethod
	def _remove_frame(obj):
		"""移除LVGL默认边框和外轮廓，不改变对象布局或滚动行为。"""
		obj.set_style_border_width(0, lv.PART.MAIN)
		obj.set_style_border_opa(0, lv.PART.MAIN)
		obj.set_style_outline_width(0, lv.PART.MAIN)
		obj.set_style_outline_opa(0, lv.PART.MAIN)
		try:
			obj.set_style_radius(0, lv.PART.MAIN)
			obj.set_style_shadow_width(0, lv.PART.MAIN)
			obj.set_style_pad_all(0, lv.PART.MAIN)
		except Exception:
			pass
		# 某些固件即使关闭 scrollbar mode，仍会保留 scrollbar 部件样式；
		# 横向滚动条由高度控制，因此宽度和高度都清零。
		try:
			obj.set_style_width(0, lv.PART.SCROLLBAR)
			obj.set_style_height(0, lv.PART.SCROLLBAR)
			obj.set_style_bg_opa(0, lv.PART.SCROLLBAR)
			obj.set_style_border_width(0, lv.PART.SCROLLBAR)
			obj.set_style_border_opa(0, lv.PART.SCROLLBAR)
			obj.set_style_outline_width(0, lv.PART.SCROLLBAR)
			obj.set_style_outline_opa(0, lv.PART.SCROLLBAR)
			obj.set_style_shadow_width(0, lv.PART.SCROLLBAR)
			obj.set_style_pad_all(0, lv.PART.SCROLLBAR)
		except Exception:
			pass

	@staticmethod
	def _make_black_surface(obj):
		"""清除导航容器主题底色；只调整视觉样式，不改变滚动行为。"""
		obj.set_style_bg_color(lv.color_hex(0), lv.PART.MAIN)
		obj.set_style_bg_opa(255, lv.PART.MAIN)
		try:
			obj.set_style_radius(0, lv.PART.MAIN)
			obj.set_style_shadow_width(0, lv.PART.MAIN)
			obj.set_style_pad_all(0, lv.PART.MAIN)
		except Exception:
			pass

	@staticmethod
	def _make_fixed_container(obj):
		"""固定普通页面容器；不改变外层 tileview 的跟手处理。"""
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

	def _set_ui_busy(self, busy):
		busy = bool(busy)
		if busy == self._ui_busy:
			return
		self._ui_busy = busy
		self.command_queue.set_ui_busy(busy)

	def _make_root(self):
		self.root_screen = lv.obj()
		self.root_screen.set_size(self.width, self.height)
		self._remove_frame(self.root_screen)
		try:
			self.root_screen.clear_flag(lv.obj.FLAG.SCROLLABLE)
			self.root_screen.set_scroll_dir(lv.DIR.NONE)
			self.root_screen.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
			self.root_screen.set_scroll_chain(lv.DIR.NONE)
			self.root_screen.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
		except Exception:
			pass
		self.root_screen.set_style_bg_color(lv.color_hex(0), lv.PART.MAIN)
		self.root_screen.set_style_bg_opa(255, lv.PART.MAIN)
		self.nav_host = lv.tileview(self.root_screen)
		self.nav_host.set_pos(0, self.status_height)
		self.nav_host.set_size(self.content_width, self.content_height)
		self._remove_frame(self.nav_host)
		self._make_black_surface(self.nav_host)
		self.nav_host.set_scroll_dir(lv.DIR.HOR)
		self.nav_host.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
		try:
			self.nav_host.set_scroll_chain(lv.DIR.NONE)
		except Exception:
			pass
		# 禁止越界时的横向弹性回弹。有效方向仍由每个 tile 的 dir 控制。
		self._disable_horizontal_elastic(self.nav_host)
		# 外层根槽位不接受用户横向拖动；一级页面由内部 root_content
		# 独立处理，子页面槽位再通过 DIR.LEFT 提供右滑返回路径。
		root_tile = self.nav_host.add_tile(0, 0, lv.DIR.NONE)
		self._remove_frame(root_tile)
		self._make_black_surface(root_tile)
		self._make_fixed_container(root_tile)
		self.nav_tiles[0] = root_tile
		self.root_content = lv.tileview(root_tile)
		self.root_content.set_size(self.content_width, self.content_height)
		self._remove_frame(self.root_content)
		self._make_black_surface(self.root_content)
		self.root_content.set_scroll_dir(lv.DIR.HOR)
		self.root_content.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
		try:
			self.root_content.set_scroll_chain(lv.DIR.NONE)
		except Exception:
			pass
		self._disable_horizontal_elastic(self.root_content)
		for index, name in enumerate(ROOT_NAMES):
			# 当前 tile 必须保留双向许可：页面被拖出半页后，LVGL 仍需
			# 接受同一触摸会话的反向拖动以回到原页。边界禁止依靠相邻
			# tile 不存在和关闭 SCROLL_ELASTIC 实现，不引入手工位移。
			tile_dir = lv.DIR.LEFT | lv.DIR.RIGHT
			tile = self.root_content.add_tile(index, 0, tile_dir)
			self._remove_frame(tile)
			self._make_black_surface(tile)
			self.root_pages[name] = self.pages.root(
				name, ROOT_TITLES[index], parent=tile)
		self.pages.create_root_indicator(root_tile)
		self.root_content.add_event_cb(self._root_changed, lv.EVENT.VALUE_CHANGED, None)
		self.nav_host.add_event_cb(self._nav_changed, lv.EVENT.VALUE_CHANGED, None)
		self.root_view = 0
		self.status_bar = self.pages.create_status_bar(self.root_screen)
		self.login_page = self.pages.create_login_page(self.root_screen)
		self.pages.create_confirm_dialog(self.root_screen)
		self.nav_host.add_flag(lv.obj.FLAG.HIDDEN)
		self.status_bar.add_flag(lv.obj.FLAG.HIDDEN)
		lv.scr_load(self.root_screen)
		self.root_screen.invalidate()
		# 首帧在登录页创建完成后立即提交，避免等待触摸事件才触发重绘。
		self.display.present_initial_frame()

	def _finish_login_transition(self):
		if not self._login_transition_pending:
			return
		self._login_transition_pending = False
		self._logged_in = True
		self.login_page.add_flag(lv.obj.FLAG.HIDDEN)
		self.status_bar.clear_flag(lv.obj.FLAG.HIDDEN)
		self.nav_host.clear_flag(lv.obj.FLAG.HIDDEN)
		try:
			self.nav_host.set_tile_id(0, 0, lv.ANIM.OFF)
		except Exception:
			self.nav_host.set_tile_id(0, 0)
		self.root_screen.invalidate()

	def set_login_prefix(self, prefix):
		if prefix not in ("M", "F"):
			return
		self.login_prefix = prefix
		# 登录页的颜色属于页面样式，由 PageFactory 统一管理。应用层只
		# 传递状态，避免直接引用 ui_pages.py 内部的颜色常量。
		self.pages.update_login_prefix(prefix)

	def login_digit(self, digit):
		if len(self.login_digits) >= 6:
			return
		self.login_digits += str(digit)
		self.login_display.set_text(self.login_digits)

	def login_delete(self):
		if self.login_digits:
			self.login_digits = self.login_digits[:-1]
		self.login_display.set_text(
			self.login_digits if self.login_digits else "")

	def login_confirm(self):
		if len(self.login_digits) < 6:
			return
		self.state_store.update_identity(
			"潘荣丰", self.login_prefix + self.login_digits)
		self._login_transition_pending = True

	def logout_to_login(self):
		"""结束模拟登录并复位现有导航对象，不触发真实通信业务。"""
		self._logged_in = False
		self._login_transition_pending = False
		self.router.reset()
		try:
			self.nav_host.set_tile_id(0, 0, lv.ANIM.OFF)
		except Exception:
			self.nav_host.set_tile_id(0, 0)
		try:
			self.root_content.set_tile_id(0, 0, lv.ANIM.OFF)
		except Exception:
			self.root_content.set_tile_id(0, 0)
		self._remove_route_tiles_from(1)
		self.root_view = 0
		self._root_session_index = 0
		self.child_screen = self.nav_tiles.get(0)
		self.pages.update_root_indicator(0)
		self.pages.set_status_context("police")

		self.login_digits = ""
		if self.login_display is not None:
			self.login_display.set_text("")
			self.login_display.invalidate()
		self.set_login_prefix("M")

		self.nav_host.add_flag(lv.obj.FLAG.HIDDEN)
		self.status_bar.add_flag(lv.obj.FLAG.HIDDEN)
		self.login_page.clear_flag(lv.obj.FLAG.HIDDEN)
		try:
			self.login_page.move_foreground()
		except Exception:
			pass
		self.root_screen.invalidate()

	def select_lamp_mode(self, mode):
		if mode not in ("off", "alternate", "diagonal"):
			return False
		_revision, snapshot = self.state_store.get_snapshot()
		self.state_store.update_lamp(
			mode, snapshot["lamp_brightness"],
			snapshot["lamp_alarm_active"])
		return True

	def select_double_click(self, enabled):
		self.state_store.update_motion(bool(enabled))
		return True

	def select_group(self, group_id):
		self.state_store.update_group_selection(
			None if group_id == "__none_group__" else group_id)
		return True

	def select_person(self, person_key):
		self.state_store.update_single_selection(
			None if person_key == "__none_person__" else person_key)
		return True

	def set_screen_brightness(self, percent):
		value = max(5, min(100, int(percent)))
		self.state_store.update_screen_brightness(value)
		return value

	def set_lamp_brightness(self, percent):
		value = max(5, min(100, int(percent)))
		_revision, snapshot = self.state_store.get_snapshot()
		self.state_store.update_lamp(
			snapshot["lamp_mode"], value,
			snapshot["lamp_alarm_active"])
		return value

	def set_audio_volume(self, level):
		value = max(0, min(11, int(level)))
		_revision, snapshot = self.state_store.get_snapshot()
		self.state_store.update_audio(
			snapshot["audio_tx_active"], snapshot["audio_rx_active"], value)
		return value

	def select_sleep_timeout(self, seconds):
		if seconds not in (10, 15, 20, 30, 60, None):
			return False
		self.state_store.update_sleep_timeout(seconds)
		return True

	def select_fall_detection(self, enabled):
		self.state_store.update_fall_detection(bool(enabled))
		return True

	def preview_check_upgrade(self):
		status = "当前已是最新版本"
		self.state_store.update_upgrade_status(status)
		return status

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
		self._set_ui_busy(True)
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
			self.pages.update_root_indicator(self.root_view)
		except Exception:
			# LVGL 回调异常不能中断 UI 主循环。
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
		# 子页面保留双向许可，以便右滑返回过程中可以反向拖回当前页；
		# 左侧没有用户可到达的下一级 tile，因此左滑最终保持原页。下
		# 一级页面仍由点击事件通过 Router 以程序方式进入。
		tile = self.nav_host.add_tile(
			depth, 0, lv.DIR.LEFT | lv.DIR.RIGHT)
		self._remove_frame(tile)
		self._make_black_surface(tile)
		self._make_fixed_container(tile)
		self.nav_tiles[depth] = tile
		self.child_screen = tile
		self.pages.child(page_info[0], page_info[2], parent=tile)
		self._disable_horizontal_elastic(tile)
		return tile

	def _show_child(self, page_info, direction="forward"):
		depth = len(self.router.stack)
		gc.collect()
		if direction == "back" and depth in self.nav_tiles:
			self.child_screen = self.nav_tiles[depth]
		else:
			self._make_child_tile(depth, page_info)
		try:
			self.nav_host.set_tile_id(
				depth, 0, lv.ANIM.ON if direction == "forward" else lv.ANIM.OFF)
		except Exception:
			self.nav_host.set_tile_id(depth, 0)
		if direction == "back":
			# 返回手势确认完成后才清理更深层页面。这样拖动过程中仍能
			# 反向回到原页，返回完成后上一级右侧也不会残留旧页面。
			self._remove_route_tiles_from(depth + 1)
		self.nav_tiles[depth].invalidate()

	def show_root_page(self):
		self.pages.set_status_context("police")
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
		self.pages.set_status_context(
			"settings" if page_info[0] == "settings_menu" else "police")
		self._show_child(page_info, direction)

	def open_child(self, name):
		if self._child_transition_busy:
			return False
		if (self.router.current is not None and
				self.router.current[0] == name):
			return False
		self._child_transition_busy = True
		self._child_transition_until = utime.ticks_add(
			utime.ticks_ms(), CHILD_TRANSITION_LOCK_MS)
		try:
			result = self.router.push(name, params={})
		except Exception:
			self._child_transition_busy = False
			return False
		if not result:
			self._child_transition_busy = False
		return result

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
				self._set_ui_busy(False)
			if self._login_transition_pending and not self.touch.pressed:
				self._finish_login_transition()
			if (self._child_transition_busy and
					utime.ticks_diff(utime.ticks_ms(),
						self._child_transition_until) >= 0):
				self._child_transition_busy = False
			utime.sleep_ms(1)

	def _refresh_page_state(self):
		changed = self.state_store.get_snapshot_if_changed()
		if changed is None:
			return
		_revision, snapshot = changed
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
