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
	from usr.ui_commands import (UICommandQueue, CMD_SET_LAMP_MODE,
		CMD_COMMIT_LAMP_BRIGHTNESS, CMD_COMMIT_SCREEN_BRIGHTNESS,
		CMD_SET_DOUBLE_CLICK_ENABLED, CMD_SET_LAMP_BRIGHTNESS,
		CMD_SET_SCREEN_BRIGHTNESS, CMD_SET_SLEEP_TIMEOUT,
		CMD_REFRESH_NETWORK)
	from usr.ui_messages import UIMessageBus
	from usr.ui_state import get_default_state_store
	from usr.ota_service import (OTA_PROGRESS_DOT_INTERVAL_MS,
		OTA_PROGRESS_DOT_MAX)
except Exception:
	from display_port import DisplayPort
	from touch_port import TouchPort
	from ui_pages import PageFactory
	from ui_router import Router
	from ui_commands import (UICommandQueue, CMD_SET_LAMP_MODE,
		CMD_COMMIT_LAMP_BRIGHTNESS, CMD_COMMIT_SCREEN_BRIGHTNESS,
		CMD_SET_DOUBLE_CLICK_ENABLED, CMD_SET_LAMP_BRIGHTNESS,
		CMD_SET_SCREEN_BRIGHTNESS, CMD_SET_SLEEP_TIMEOUT,
		CMD_REFRESH_NETWORK)
	from ui_messages import UIMessageBus
	from ui_state import get_default_state_store
	from ota_service import OTA_PROGRESS_DOT_INTERVAL_MS, OTA_PROGRESS_DOT_MAX


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
				 state_store=None, message_bus=None,
				 communication_controller=None, ota_service=None,
				 initial_police_no=None, watchdog=None,
				 watchdog_feed_interval_ms=5000):
		print("[UI入口] 创建UI应用，11111内存={}KB".format(gc.mem_free() // 1024))
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
		self.communication_controller = communication_controller
		self.ota_service = ota_service
		self.watchdog = watchdog
		self.watchdog_feed_interval_ms = max(1, int(watchdog_feed_interval_ms))
		self._watchdog_next_feed_ms = utime.ticks_add(
			utime.ticks_ms(), self.watchdog_feed_interval_ms)
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
		police_no = str(initial_police_no or "")
		if (len(police_no) == 7 and police_no[0] in ("M", "F") and
				police_no[1:].isdigit()):
			self.login_prefix = police_no[0]
			self.login_digits = police_no[1:]
		else:
			self.login_prefix = "M"
			self.login_digits = ""
		self.login_m_button = None
		self.login_f_button = None
		self.login_display = None
		self._logged_in = False
		# 主动切换用户时，通信线程清理资源是异步的；在清理完成前，
		# 禁止旧的 logged_in/joined 状态再次触发自动进入业务页。
		self._returning_to_login = False
		self._login_transition_pending = False
		self._pending_login_transaction_id = None
		self._logout_event_seen = 0
		self._forced_logout_seen = 0
		self._last_login_popup_key = None
		self._connection_event_seen = 0
		self._pending_connection_events = []
		self._connection_prompt_seen = {"network": False, "tcp": False}
		self._main_connection_recovery_active = False
		self._guest_relogin_prompt_shown = False
		self._guest_relogin_prompt_seen = 0
		self._floor_event_seen = 0
		self._active_call_direction = None
		self._active_call_group_id = None
		self._active_remote_group_id = None
		self._active_remote_name = None
		self._active_remote_call_type = None
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
		self._display_sleep_state = None
		self._ota_event_seen = 0
		self._volume_key_event_seen = 0
		self._key_notice_seen = 0
		self._pending_key_notice = None
		self._ota_dot_count = 1
		self._ota_dot_at = None

	def _sync_display_sleep(self):
		"""由UI线程串行执行LCD电源命令并切换触摸输入。"""
		_sleeping = bool(self.state_store.get_snapshot()[1]["screen_sleeping"])
		if self._display_sleep_state == _sleeping:
			return
		if _sleeping:
			if self.touch is not None:
				self.touch.set_enabled(False)
			if not self.display.enter_sleep():
				return
		else:
			if not self.display.leave_sleep():
				if self.touch is not None:
					self.touch.set_enabled(False)
				return
			if self.touch is not None:
				self.touch.set_enabled(True)
		self._display_sleep_state = _sleeping

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
		self.pages.create_volume_overlay(self.root_screen)
		self.nav_host.add_flag(lv.obj.FLAG.HIDDEN)
		self.status_bar.add_flag(lv.obj.FLAG.HIDDEN)
		lv.scr_load(self.root_screen)
		self.root_screen.invalidate()
		# 首帧在登录页创建完成后立即提交，避免等待触摸事件才触发重绘。
		self.display.present_initial_frame()

	def _finish_login_transition(self):
		if not self._login_transition_pending:
			return
		# 登录前连接提示属于登录页生命周期；进入业务页时丢弃其显示积压。
		# 已读序号推进到当前快照，之后真正发生的断线重连仍会产生新序号。
		_revision, snapshot = self.state_store.get_snapshot()
		self._connection_event_seen = max(
			self._connection_event_seen,
			int(snapshot.get("connection_event_revision") or 0))
		self._pending_connection_events = []
		self._main_connection_recovery_active = False
		self.pages.hide_connection_popup()
		# 登录进度弹框只属于登录页；切入业务页时无条件清理，
		# 避免状态刷新与页面切换错开一帧造成“登录中”残留。
		self.pages.hide_login_progress_popup()
		self._login_transition_pending = False
		self._pending_login_transaction_id = None
		self._returning_to_login = False
		self._logged_in = True
		self.state_store.update_business_page_active(True)
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
			self.pages.show_transient_popup("请输入6位编号")
			return
		police_no = self.login_prefix + self.login_digits
		self._returning_to_login = False
		if self.communication_controller is not None:
			if not self.communication_controller.connection_ready():
				self.pages.show_confirm_dialog(
					"{}\n无网强制登录？".format(
						self.communication_controller.connection_issue_text()),
					self._confirm_guest_login, self._cancel_guest_login)
				return
			if not self.communication_controller.request_login(police_no):
				self.pages.show_transient_popup("登录服务不可用")
				return
			self._pending_login_transaction_id = int(
				self.state_store.get_snapshot()[1].get(
					"login_transaction_id") or 0)
		self.state_store.update_identity(
			self.state_store.get_snapshot()[1]["police_name"], police_no)
		self._login_transition_pending = True

	def _confirm_guest_login(self):
		"""确认无网登录；若确认前连接已恢复，则转为普通登录。"""
		if self.communication_controller is None:
			return
		self._returning_to_login = False
		police_no = self.login_prefix + self.login_digits
		if self.communication_controller.connection_ready():
			if self.communication_controller.request_login(police_no):
				self._pending_login_transaction_id = int(
					self.state_store.get_snapshot()[1].get(
						"login_transaction_id") or 0)
				self.state_store.update_identity(
					self.state_store.get_snapshot()[1]["police_name"], police_no)
				self._login_transition_pending = True
			return
		if not self.communication_controller.enter_guest_mode(police_no):
			self.pages.show_transient_popup("游客登录失败")
			return
		self._guest_relogin_prompt_shown = False
		self._pending_login_transaction_id = None
		self._login_transition_pending = True

	def _cancel_guest_login(self):
		# 取消只关闭弹框，后台连接继续运行，用户仍可再次确认登录。
		self._login_transition_pending = False
		self._pending_login_transaction_id = None

	def _confirm_force_login(self):
		if (self.communication_controller is None or
				not self.communication_controller.request_force_login()):
			self._login_transition_pending = False
			self._pending_login_transaction_id = None
			self.pages.show_transient_popup("登录服务不可用")

	def _cancel_force_login(self):
		if self.communication_controller is not None:
			self.communication_controller.cancel_force_login()
		self._login_transition_pending = False
		self._pending_login_transaction_id = None

	def _process_login_popup(self, snapshot):
		key = (snapshot.get("login_state"), snapshot.get("login_result"),
			   snapshot.get("force_login_stage"), snapshot.get("login_error"),
			   snapshot.get("login_transaction_id"))
		if snapshot.get("identity_mode") == "guest":
			self._last_login_popup_key = key
			self.pages.hide_login_progress_popup()
			return
		if key == self._last_login_popup_key:
			return
		state = key[0]
		stage = key[2]
		# 业务页不显示普通登录进度。自动重连期间的网络/TCP提示由
		# _process_connection_popup()负责，不能被“登录中”覆盖。
		if (self._logged_in and
				state in ("waiting_network", "connecting", "logging_in")):
			self._last_login_popup_key = key
			self.pages.hide_login_progress_popup()
			return
		# 连接阶段结果必须可见；登录进度不覆盖正在显示的连接提示。
		if (state in ("waiting_network", "connecting", "logging_in") and
				self.pages.connection_popup_state() is not None):
			return
		self._last_login_popup_key = key
		if stage == "prompt" or state == "force_login_required":
			self.pages.hide_communication_popup()
			self.pages.show_confirm_dialog(
				"警号已登录\n是否强制登录?",
				self._confirm_force_login, self._cancel_force_login)
			return
		if state == "waiting_network":
			self._cancel_current_touch_for_popup()
			self.pages.show_progress_popup(
				"等待网络", signature=("login", state))
		elif state in ("connecting", "logging_in"):
			self._cancel_current_touch_for_popup()
			self.pages.show_progress_popup(
				"登录中", signature=("login", state))
		elif state == "force_logging_in" or stage == "waiting":
			self._cancel_current_touch_for_popup()
			self.pages.show_progress_popup(
				"转机登录中", signature=("login", state))
		elif state in ("logged_in", "joined"):
			self.pages.hide_login_progress_popup()
		elif state == "guest":
			self.pages.hide_login_progress_popup()
		elif state == "error":
			transaction_id = int(snapshot.get("login_transaction_id") or 0)
			if (self._login_transition_pending and
					self._pending_login_transaction_id is not None and
					transaction_id != self._pending_login_transaction_id):
				return
			self._login_transition_pending = False
			self._pending_login_transaction_id = None
			text = snapshot.get("login_error") or "登录失败"
			if text == "设备已被强制退出":
				# 强退提示和播报由清理完成事件统一触发。
				return
			if "等待TCP应答超时" in text and stage == "failed":
				text = "转机登录超时"
			self.pages.show_transient_popup(text)
		elif state == "idle":
			self.pages.hide_login_progress_popup()

	def _process_connection_popup(self, snapshot=None):
		current = snapshot or {}
		state_store = getattr(self, "state_store", None)
		if snapshot is None and state_store is not None:
			getter = getattr(state_store, "get_snapshot", None)
			if getter is not None:
				current = getter()[1]
		# 游客模式的恢复登录结果由独立修订号触发，不能再依赖TCP成功弹框。
		guest_revision = int(
			current.get("guest_relogin_prompt_revision") or 0)
		if (current.get("identity_mode") == "guest" and
				guest_revision > self._guest_relogin_prompt_seen):
			if self.pages.connection_popup_blocked():
				return
			self._guest_relogin_prompt_seen = guest_revision
			self._pending_connection_events = []
			self._guest_relogin_prompt_shown = True
			self.pages.show_confirm_dialog(
				"网络正常\n是否重新登录？",
				self._confirm_guest_relogin,
				self._cancel_guest_relogin)
			return

		if snapshot is not None:
			events = snapshot.get("connection_events") or ()
			if events:
				for item in events:
					revision = int(item[0])
					if revision <= self._connection_event_seen:
						continue
					self._pending_connection_events.append({
						"revision": revision, "stage": item[1],
						"status": item[2], "text": item[3],
						"duration_ms": item[4],
						"user_visible": bool(item[5]) if len(item) > 5 else False})
					self._connection_event_seen = revision
			else:
				revision = int(snapshot.get("connection_event_revision") or 0)
				if revision > self._connection_event_seen:
					self._connection_event_seen = revision
					self._pending_connection_events.append({
						"revision": revision,
						"stage": snapshot.get("connection_stage"),
						"status": snapshot.get("connection_status"),
						"text": snapshot.get("connection_text"),
						"duration_ms": snapshot.get("connection_duration_ms"),
						"user_visible": False})
		while self._pending_connection_events:
			event = self._pending_connection_events[0]
			status = event["status"]
			# 连接成功/失败事件以及后台进度事件均静默消费。
			if status != "progress":
				self._pending_connection_events.pop(0)
				continue
			if current.get("identity_mode") == "guest":
				self._pending_connection_events.pop(0)
				continue
			if self._logged_in:
				if not event["user_visible"]:
					self._pending_connection_events.pop(0)
					continue
				if (current.get("network_ready") and
						current.get("tcp_socket_connected") and
						current.get("network_connected")):
					# PTT提示在排队期间连接已经恢复，丢弃过期提示。
					self._pending_connection_events.pop(0)
					continue
			else:
				stage = event["stage"]
				if event["user_visible"] or self._connection_prompt_seen.get(stage):
					self._pending_connection_events.pop(0)
					continue
			if self.pages.connection_popup_blocked():
				return
			if not self._logged_in:
				self._connection_prompt_seen[event["stage"]] = True
			self._pending_connection_events.pop(0)
			self._cancel_current_touch_for_popup()
			self.pages.show_progress_popup(
				event["text"], locked=True,
				signature=("connection", event["stage"], "progress",
					event["revision"]),
				duration_ms=event["duration_ms"] or 3000)

	@staticmethod
	def _call_type_text(call_type):
		return "单呼" if call_type == "single" else "组呼"

	def _cancel_current_touch_for_popup(self):
		"""终止当前点击资格；不改变触摸采集和跟手状态机结构。"""
		self.touch_session_click_allowed = False
		if self.touch is not None and self.touch.pressed:
			self.touch_moved = True

	def _process_floor_event(self, snapshot):
		revision = int(snapshot.get("floor_event_revision") or 0)
		if revision == self._floor_event_seen:
			return
		self._floor_event_seen = revision
		event_type = snapshot.get("floor_event_type")
		group_id = snapshot.get("floor_event_group_id")
		call_type = snapshot.get("floor_call_type")
		if event_type == "guest_ptt_blocked":
			self.pages.show_transient_popup("游客身份，无法对讲")
			return
		if event_type == "request_started":
			if self._active_remote_group_id is None:
				self._cancel_current_touch_for_popup()
				self.pages.show_progress_popup(
					self._call_type_text(call_type), "正在申请麦权... ...",
					True, ("request", call_type, group_id, revision))
		elif event_type == "request_cancelled":
			if self._active_remote_group_id is None:
				self.pages.hide_communication_popup()
		elif event_type == "request_failed":
			if self._active_remote_group_id is None:
				self.pages.show_transient_popup(
					self._call_type_text(call_type), "抢麦失败")
		elif event_type == "self_granted":
			self._active_call_direction = "self"
			self._active_call_group_id = group_id
			if self._active_remote_group_id == group_id:
				self._active_remote_group_id = None
				self._active_remote_name = None
				self._active_remote_call_type = None
			self._cancel_current_touch_for_popup()
			self.pages.show_call_popup(
				self._call_type_text(call_type), "正在讲话中... ...", False,
				("self", call_type, group_id, revision))
		elif event_type == "self_released":
			if self._active_call_direction == "self" or (
					self._active_remote_group_id is None):
				self.pages.hide_communication_popup()
			self._active_call_direction = None
			self._active_call_group_id = None
		elif event_type == "release_failed":
			self._active_call_direction = None
			self._active_call_group_id = None
			self.pages.hide_communication_popup()
			self.pages.show_transient_popup("释放麦权失败")
		elif event_type == "remote_occupied":
			if not snapshot.get("floor_audio_selected"):
				return
			name = snapshot.get("floor_speaker_name") or "未知警员"
			same_visible_session = (
				self.pages.call_popup_visible() and
				self._active_call_direction == "remote" and
				self._active_remote_group_id == group_id and
				self._active_remote_name == name and
				self._active_remote_call_type == call_type)
			self._active_remote_group_id = group_id
			self._active_remote_name = name
			self._active_remote_call_type = call_type
			self._active_call_direction = "remote"
			self._active_call_group_id = group_id
			if same_visible_session:
				return
			self._cancel_current_touch_for_popup()
			self.pages.show_call_popup(
				self._call_type_text(call_type),
				"{}：正在讲话中... ...".format(name), True,
				("remote", call_type, group_id, name, revision))
		elif event_type == "remote_idle":
			if self._active_remote_group_id == group_id:
				self._active_remote_group_id = None
				self._active_remote_name = None
				self._active_remote_call_type = None
				self._active_call_direction = None
				self._active_call_group_id = None
				if snapshot.get("floor_state") == "requesting_floor":
					request_group_id = snapshot.get("floor_group_id")
					request_call_type = (
						"single" if snapshot.get("single_group_id") is not None and
						request_group_id == snapshot.get("single_group_id") else "group")
					self._cancel_current_touch_for_popup()
					self.pages.show_progress_popup(
						self._call_type_text(request_call_type),
						"正在申请麦权... ...", True,
						("request", request_call_type, request_group_id, revision))
				else:
					self.pages.hide_communication_popup()
		elif event_type in ("call_ended", "communication_ended"):
			if (event_type == "call_ended" and group_id is not None and
					self._active_call_group_id != group_id):
				return
			self._active_remote_group_id = None
			self._active_remote_name = None
			self._active_remote_call_type = None
			self._active_call_direction = None
			self._active_call_group_id = None
			self.pages.hide_communication_popup()

	def _return_to_login(self, notify_controller):
		"""复位导航；服务器强退时不再反向发送主动退出请求。"""
		self._returning_to_login = True
		if notify_controller and self.communication_controller is not None:
			self.communication_controller.request_logout()
		self._logged_in = False
		self.state_store.update_business_page_active(False)
		self._login_transition_pending = False
		self._pending_login_transaction_id = None
		# 开启新的登录页生命周期；不能沿用上一次业务页的状态去重键。
		self._last_login_popup_key = None
		_revision, snapshot = self.state_store.get_snapshot()
		self._connection_event_seen = max(
			self._connection_event_seen,
			int(snapshot.get("connection_event_revision") or 0))
		self._pending_connection_events = []
		self._connection_prompt_seen = {"network": False, "tcp": False}
		self._main_connection_recovery_active = False
		self._guest_relogin_prompt_shown = False
		self._guest_relogin_prompt_seen = int(
			snapshot.get("guest_relogin_prompt_revision") or 0)
		self._active_remote_group_id = None
		self._active_remote_name = None
		self._active_remote_call_type = None
		self._active_call_direction = None
		self._active_call_group_id = None
		self.pages.hide_communication_popup()
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

		if self.login_display is not None:
			self.login_display.set_text(self.login_digits)
			self.login_display.invalidate()
		self.set_login_prefix(self.login_prefix)

		self.nav_host.add_flag(lv.obj.FLAG.HIDDEN)
		self.status_bar.add_flag(lv.obj.FLAG.HIDDEN)
		self.login_page.clear_flag(lv.obj.FLAG.HIDDEN)
		try:
			self.login_page.move_foreground()
		except Exception:
			pass
		self.root_screen.invalidate()

	def logout_to_login(self):
		if self._returning_to_login:
			return
		self._returning_to_login = True
		self._login_transition_pending = False
		self._pending_login_transaction_id = None
		self.pages.hide_communication_popup()
		accepted = (self.communication_controller is not None and
			self.communication_controller.request_logout())
		if not accepted:
			self._return_to_login(False)
			return
		self.pages.show_progress_popup(
			"正在退出... ...", locked=True,
			signature=("logout", "progress"))

	def _confirm_guest_relogin(self):
		if self.communication_controller is None:
			return
		self.communication_controller.exit_guest_mode()
		self._return_to_login(False)

	def _cancel_guest_relogin(self):
		# 保持游客身份；下一次网络断开并重新恢复时重新允许询问。
		return

	def select_lamp_mode(self, mode):
		if mode not in ("off", "alternate", "diagonal"):
			return False
		if self.low_priority_worker is not None:
			if self.lamp is None:
				print("[肩灯] 服务不可用，模式设置已忽略")
				return False
			if not self.command_queue.put_latest(CMD_SET_LAMP_MODE, mode):
				return False
		_revision, snapshot = self.state_store.get_snapshot()
		self.state_store.update_lamp(
			mode, snapshot["lamp_brightness"],
			snapshot["lamp_alarm_active"])
		return True

	def select_double_click(self, enabled):
		enabled = bool(enabled)
		if self.low_priority_worker is not None:
			return self.command_queue.put_latest(
				CMD_SET_DOUBLE_CLICK_ENABLED, enabled)
		self.state_store.update_motion(enabled)
		return True

	def select_group(self, group_id):
		getter = getattr(self.state_store, "get_snapshot", None)
		if getter is not None:
			snapshot = getter()[1]
			if (snapshot.get("ota_busy") or
					snapshot.get("identity_mode") == "guest" or
					snapshot.get("poc_enabled") is not True):
				return False
		if self.communication_controller is not None:
			if group_id == "__none_group__":
				return self.communication_controller.request_leave_group()
			return self.communication_controller.request_join_group(group_id)
		self.state_store.update_group_selection(
			None if group_id == "__none_group__" else group_id)
		return True

	def select_person(self, person_key):
		getter = getattr(self.state_store, "get_snapshot", None)
		if getter is not None:
			snapshot = getter()[1]
			if (snapshot.get("ota_busy") or
					snapshot.get("identity_mode") == "guest" or
					snapshot.get("poc_enabled") is not True):
				return False
		if self.communication_controller is not None:
			if person_key == "__none_person__":
				return self.communication_controller.request_dissolve_single_call()
			return self.communication_controller.request_single_call(person_key)
		self.state_store.update_single_selection(
			None if person_key == "__none_person__" else person_key)
		return True

	def set_screen_brightness(self, percent):
		value = max(5, min(100, int(percent)))
		if self.low_priority_worker is not None:
			if not self.command_queue.put_latest(CMD_SET_SCREEN_BRIGHTNESS, value):
				return self.state_store.get_snapshot()[1]["screen_brightness"]
		self.state_store.update_screen_brightness(value)
		return value

	def set_lamp_brightness(self, percent):
		value = max(5, min(100, int(percent)))
		if self.low_priority_worker is not None:
			if self.lamp is None:
				print("[肩灯] 服务不可用，亮度设置已忽略")
				return self.state_store.get_snapshot()[1]["lamp_brightness"]
			if not self.command_queue.put_latest(CMD_SET_LAMP_BRIGHTNESS, value):
				return self.state_store.get_snapshot()[1]["lamp_brightness"]
		_revision, snapshot = self.state_store.get_snapshot()
		self.state_store.update_lamp(
			snapshot["lamp_mode"], value,
			snapshot["lamp_alarm_active"])
		return value

	def set_audio_volume(self, level):
		value = max(0, min(11, int(level)))
		if self.communication_controller is not None:
			if not self.communication_controller.request_set_volume(value):
				return self.state_store.get_snapshot()[1]["audio_volume"]
		_revision, snapshot = self.state_store.get_snapshot()
		self.state_store.update_audio(
			snapshot["audio_tx_active"], snapshot["audio_rx_active"], value)
		return value

	def commit_slider_setting(self, setting):
		if setting == "screen_brightness":
			return self.command_queue.put(
				CMD_COMMIT_SCREEN_BRIGHTNESS, None)
		if setting == "lamp_brightness":
			return self.command_queue.put(
				CMD_COMMIT_LAMP_BRIGHTNESS, None)
		if setting == "audio_volume" and self.communication_controller is not None:
			return self.communication_controller.request_commit_volume()
		return False

	def select_sleep_timeout(self, seconds):
		if seconds not in (10, 15, 20, 30, 60, None):
			return False
		if self.low_priority_worker is not None:
			if not self.command_queue.put_latest(CMD_SET_SLEEP_TIMEOUT, seconds):
				return False
		self.state_store.update_sleep_timeout(seconds)
		return True

	def refresh_network(self, event=None):
		"""请求低优先级Worker立即重新读取网络和信号值。"""
		if self.low_priority_worker is None:
			return False
		return self.command_queue.put_coalesced(
			CMD_REFRESH_NETWORK, None)

	def select_fall_detection(self, enabled):
		self.state_store.update_fall_detection(bool(enabled))
		return True

	def check_upgrade(self):
		if self.ota_service is None:
			self.pages.show_transient_popup("升级服务不可用")
			return False
		return self.ota_service.start_query()

	def before_leave_page(self, name):
		if name == "upgrade" and self.ota_service is not None:
			if self.ota_service.leave_page():
				self.pages.hide_ota_popup()

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
		if page_info[0] == "volume":
			self.pages.hide_volume_overlay()
		self.pages.set_status_context(
			"settings" if page_info[0] == "settings_menu" else "police")
		self._show_child(page_info, direction)
		if page_info[0] == "intercom_menu" and self.communication_controller is not None:
			self.communication_controller.request_refresh_directory()

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
			if name == "upgrade" and self.ota_service is not None:
				self.ota_service.enter_page()
			result = self.router.push(name, params={})
		except Exception:
			self._child_transition_busy = False
			return False
		if not result:
			self._child_transition_busy = False
		return result

	def _login_transition_ready(self, snapshot):
		if snapshot["login_state"] == "guest":
			return True
		return (
			self._pending_login_transaction_id is not None and
			int(snapshot.get("login_transaction_id") or 0) ==
				self._pending_login_transaction_id and
			snapshot.get("login_result") == 0 and
			snapshot["login_state"] in ("logged_in", "joined"))

	def start(self):
		device = getattr(self.low_priority_worker, "device", None)
		initial_brightness = self.state_store.get_snapshot()[1][
			"screen_brightness"]
		backlight_prepared = False
		if device is not None:
			try:
				device.prepare_screen_brightness(initial_brightness)
				backlight_prepared = True
			except Exception as error:
				print("[显示] 启动前准备背光失败，首帧后再设置：{}".format(error))
		self.display.start()
		self.touch = TouchPort()
		self.touch.start()
		self.touch.configure_scroll(ROOT_SCROLL_THROW, ROOT_SCROLL_LIMIT)
		self._make_root()
		# 先把登录页实际刷新到LCD，再启动后台蜂窝网络和TCP预连接。
		self.display.tick(1)
		self.display.task_handler()
		if device is not None:
			try:
				if backlight_prepared:
					device.reveal_screen()
				else:
					device.set_screen_brightness(initial_brightness)
			except Exception as error:
				print("[显示] 首帧背光设置失败：{}".format(error))
		if self.communication_controller is not None:
			self.communication_controller.start()
		if self.low_priority_worker is not None:
			self.low_priority_worker.start()
		if self.sensor_service is not None:
			self.sensor_service.start()
		if self.ota_service is not None:
			self.ota_service.start_post_boot_report()
		self.last_ms = utime.ticks_ms()
		while True:
			now = utime.ticks_ms()
			elapsed = utime.ticks_diff(now, self.last_ms)
			self.last_ms = now
			self._sync_display_sleep()
			was_pressed = self.touch.pressed
			self.touch.poll()
			if self.touch.pressed and self.low_priority_worker is not None:
				self.low_priority_worker.note_ui_activity(now)
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
			self._process_connection_popup()
			self.pages.process_popup()
			self._process_pending_key_notice()
			self.pages.process_volume_overlay()
			self._animate_ota_popup(now)
			self.display.tick(elapsed)
			self.display.task_handler()
			if self._release_ui_after_task:
				self._release_ui_after_task = False
				self._set_ui_busy(False)
			if self._login_transition_pending and not self.touch.pressed:
				_login_ready = self.communication_controller is None
				if self.communication_controller is not None:
					_login_snapshot = self.state_store.get_snapshot()[1]
					_login_ready = self._login_transition_ready(_login_snapshot)
				if _login_ready:
					self._finish_login_transition()
			if (self._child_transition_busy and
					utime.ticks_diff(utime.ticks_ms(),
						self._child_transition_until) >= 0):
				self._child_transition_busy = False
			self._feed_watchdog_if_due(utime.ticks_ms())
			utime.sleep_ms(1)

	def _feed_watchdog_if_due(self, now):
		if (self.watchdog is None or
				utime.ticks_diff(now, self._watchdog_next_feed_ms) < 0):
			return
		try:
			self.watchdog.feed()
		except Exception as error:
			print("[看门狗] 喂狗失败，停止继续喂狗：{}".format(error))
			self.watchdog = None
			return
		self._watchdog_next_feed_ms = utime.ticks_add(
			now, self.watchdog_feed_interval_ms)

	def _refresh_page_state(self):
		changed = self.state_store.get_snapshot_if_changed()
		if changed is None:
			return
		_revision, snapshot = changed
		forced_revision = int(snapshot.get("forced_logout_revision") or 0)
		if forced_revision > self._forced_logout_seen:
			self._forced_logout_seen = forced_revision
			self._return_to_login(False)
			self._connection_prompt_seen = {"network": True, "tcp": True}
			self._last_login_popup_key = (
				snapshot.get("login_state"), snapshot.get("login_result"),
				snapshot.get("force_login_stage"), snapshot.get("login_error"),
				snapshot.get("login_transaction_id"))
			self.pages.show_transient_popup(
				"设备已被强制退出", duration_ms=2000)
			if self.communication_controller is not None:
				self.communication_controller.play_forced_logout_announcement()
			self.pages.refresh_state(snapshot)
			return
		if self._process_logout_state(snapshot):
			self.pages.refresh_state(snapshot)
			return
		self._process_volume_key_event(snapshot)
		self._queue_key_notice(snapshot)
		self._process_connection_popup(snapshot)
		self._process_login_popup(snapshot)
		if (self._returning_to_login and
				snapshot.get("login_state") == "idle"):
			self._returning_to_login = False
		# 游客自动恢复登录成功后没有用户触发的登录按钮事务，
		# 由状态变化直接完成页面切换。
		if (not self._logged_in and
				not self._returning_to_login and
				snapshot.get("identity_mode") == "normal" and
				snapshot.get("login_state") in ("logged_in", "joined") and
				not self._login_transition_pending):
			self._login_transition_pending = True
			self._finish_login_transition()
		if snapshot.get("ota_busy"):
			self._floor_event_seen = max(
				self._floor_event_seen,
				int(snapshot.get("floor_event_revision") or 0))
		elif self._logged_in or snapshot.get("identity_mode") == "guest":
			self._process_floor_event(snapshot)
		self._process_ota_event(snapshot)
		self.pages.refresh_state(snapshot)

	def _process_logout_state(self, snapshot):
		revision = int(snapshot.get("logout_event_revision") or 0)
		if snapshot.get("logout_active"):
			self._returning_to_login = True
			self.pages.show_progress_popup(
				"正在退出... ...", locked=True,
				signature=("logout", "progress"))
			return True
		if revision <= self._logout_event_seen:
			return False
		self._logout_event_seen = revision
		self.pages.hide_logout_popup()
		self._return_to_login(False)
		return True

	def _process_volume_key_event(self, snapshot):
		revision = int(snapshot.get("volume_key_event_revision") or 0)
		if revision <= self._volume_key_event_seen:
			return
		self._volume_key_event_seen = revision
		current = None if self.router.current is None else self.router.current[0]
		if current == "volume":
			self.pages.hide_volume_overlay()
			return
		self.pages.show_volume_overlay(
			snapshot.get("volume_key_event_value", snapshot["audio_volume"]))

	def _queue_key_notice(self, snapshot):
		revision = int(snapshot.get("key_notice_revision") or 0)
		if revision <= self._key_notice_seen:
			return
		self._key_notice_seen = revision
		self._pending_key_notice = {
			"revision": revision,
			"text": snapshot.get("key_notice_text") or "",
			"success": bool(snapshot.get("key_notice_success", True)),
			"duration_ms": int(
				snapshot.get("key_notice_duration_ms") or 2000),
		}

	def _process_pending_key_notice(self):
		notice = self._pending_key_notice
		if notice is None or not self.pages.can_show_low_priority_popup():
			return False
		shown = self.pages.show_key_notice(
			notice["text"], notice["success"], notice["duration_ms"],
			("key_notice", notice["revision"]))
		if shown:
			self._pending_key_notice = None
		return shown

	def _process_ota_event(self, snapshot):
		revision = int(snapshot.get("ota_event_revision") or 0)
		if revision <= self._ota_event_seen:
			return
		kind = snapshot.get("ota_event_kind")
		text = snapshot.get("ota_event_text") or ""
		duration = snapshot.get("ota_event_duration_ms")
		shown = True
		if kind == "hide":
			self.pages.hide_ota_popup()
		elif kind == "progress":
			self.pages.show_ota_progress(
				text, duration, ("ota", "progress", revision),
				locked=snapshot.get("ota_phase") not in
				("querying", "waiting_response"))
			if snapshot.get("ota_phase") == "downloading":
				self._ota_dot_count = 1
				self._ota_dot_at = utime.ticks_ms()
		elif kind == "success":
			self.pages.show_ota_result(
				text, True, duration or 2000, ("ota", "success", revision))
		elif kind == "error":
			self.pages.show_ota_result(
				text, False, duration or 2000, ("ota", "error", revision))
		elif kind == "confirm":
			shown = self.pages.show_confirm_dialog(
				text, self.ota_service.confirm_update,
				self.ota_service.reject_update,
				signature=("ota", "confirm", revision))
		if shown:
			self._ota_event_seen = revision

	def _animate_ota_popup(self, now):
		if self.ota_service is None:
			return
		snapshot = self.state_store.get_snapshot()[1]
		if snapshot.get("ota_phase") != "downloading":
			self._ota_dot_at = None
			return
		if self._ota_dot_at is None:
			self._ota_dot_at = now
			return
		if utime.ticks_diff(now, self._ota_dot_at) < OTA_PROGRESS_DOT_INTERVAL_MS:
			return
		self._ota_dot_at = now
		self._ota_dot_count = (1 if self._ota_dot_count >= OTA_PROGRESS_DOT_MAX
			else self._ota_dot_count + 1)
		self.pages.show_ota_progress(
			"下载中" + "." * self._ota_dot_count,
			signature=("ota", "download", self._ota_dot_count))


def main(battery=None, network=None, lamp=None,
		 low_priority_worker=None, low_priority_worker_factory=None,
		 sensor_service=None, command_queue=None, state_store=None,
		 message_bus=None, communication_controller=None, ota_service=None,
		 initial_police_no=None, watchdog=None,
		 watchdog_feed_interval_ms=5000):
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
		communication_controller=communication_controller,
		ota_service=ota_service,
		initial_police_no=initial_police_no,
		watchdog=watchdog,
		watchdog_feed_interval_ms=watchdog_feed_interval_ms,
	).start()
