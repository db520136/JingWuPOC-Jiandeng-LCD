# -*- coding: utf-8 -*-
"""正式 UI 页面（纯 UI 基线，不访问任何真实业务硬件）。"""

import lvgl as lv
import utime

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
LIST_ROW_STEP = 52
LIST_ROW_HEIGHT = 48
LIST_VISIBLE_ROWS = 4
LIST_SETTLE_POLL_MS = 30
LIST_SETTLE_STABLE_MS = 120
OPTION_SELECTED_BG = lv.color_hex(0x0A3D47)
OPTION_DOT_OFF = lv.color_hex(0x8FA7B3)
DISABLED_TEXT = lv.color_hex(0x68747A)


def _remove_frame(obj):
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
		# 单张资源缺失时保留页面结构，不阻断 UI 启动。
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
		self._root_indicator_dots = []
		self._status_context = "police"
		self._status_police_no = "M100001"
		self._list_scroll_states = []
		self._list_scroll_timer = None
		self._confirm_overlay = None
		self._confirm_label = None
		self._pending_confirm = None

	def create_confirm_dialog(self, parent):
		"""创建唯一一套确认弹窗及全屏输入遮罩。"""
		overlay = lv.obj(parent)
		_remove_frame(overlay)
		_make_fixed(overlay)
		overlay.set_pos(0, 0)
		overlay.set_size(self.app.width, self.app.height)
		overlay.set_style_bg_opa(0, lv.PART.MAIN)
		try:
			overlay.add_flag(lv.obj.FLAG.CLICKABLE)
		except Exception:
			pass

		dialog = lv.obj(overlay)
		_remove_frame(dialog)
		_make_fixed(dialog)
		dialog.set_pos(16, 35)
		dialog.set_size(264, 164)
		dialog.set_style_bg_color(BLUE, lv.PART.MAIN)
		dialog.set_style_bg_opa(255, lv.PART.MAIN)
		try:
			dialog.set_style_radius(6, lv.PART.MAIN)
		except Exception:
			pass

		self._confirm_label = _label(
			dialog, "确认修改？", 0, 40, 264, 40,
			WHITE, lv.TEXT_ALIGN.CENTER)
		_button(dialog, "取消", 25, 104, 95, 45,
				lambda event: self.cancel_confirm_dialog(), self.app, RED)
		_button(dialog, "确认", 144, 104, 95, 45,
				lambda event: self.accept_confirm_dialog(), self.app, GREEN)

		self._confirm_overlay = overlay
		overlay.add_flag(lv.obj.FLAG.HIDDEN)
		return overlay

	def show_confirm_dialog(self, text, on_confirm):
		"""显示公共弹窗；同一时刻只保存一个待确认动作。"""
		if self._confirm_overlay is None or on_confirm is None:
			return False
		if self._pending_confirm is not None:
			return False
		self._pending_confirm = on_confirm
		self._confirm_label.set_text(str(text))
		self._confirm_label.invalidate()
		self._confirm_overlay.clear_flag(lv.obj.FLAG.HIDDEN)
		try:
			self._confirm_overlay.move_foreground()
		except Exception:
			pass
		self._confirm_overlay.invalidate()
		return True

	def cancel_confirm_dialog(self):
		"""取消待确认动作，不修改页面状态。"""
		self._pending_confirm = None
		if self._confirm_overlay is not None:
			self._confirm_overlay.add_flag(lv.obj.FLAG.HIDDEN)

	def accept_confirm_dialog(self):
		"""先关闭弹窗，再执行一次待确认动作。"""
		callback = self._pending_confirm
		self._pending_confirm = None
		if self._confirm_overlay is not None:
			self._confirm_overlay.add_flag(lv.obj.FLAG.HIDDEN)
		if callback is not None:
			callback()

	def create_status_bar(self, parent):
		bar = lv.obj(parent)
		_remove_frame(bar)
		_make_fixed(bar)
		bar.set_pos(0, 0)
		bar.set_size(self.app.width, self.app.status_height)
		bar.set_style_bg_color(BLACK, lv.PART.MAIN)
		bar.set_style_bg_opa(255, lv.PART.MAIN)
		self._status_labels["police"] = _label(
			bar, "M100001", 23, 3, 105, 24, WHITE, lv.TEXT_ALIGN.LEFT, True)
		self._status_network_icon = _image(bar, "wifi_5.jpg", 143, 3, 24, 24)
		self._status_labels["battery"] = _label(
			bar, "电量：99%", 170, 3, 110, 24, WHITE, lv.TEXT_ALIGN.RIGHT, True)
		return bar

	def create_root_indicator(self, parent):
		"""创建覆盖在一级页面之上的唯一一组四页指示圆点。"""
		holder = lv.obj(parent)
		_remove_frame(holder)
		_make_fixed(holder)
		holder.set_pos(116, self.app.content_height - 16)
		holder.set_size(64, 12)
		holder.set_style_bg_opa(0, lv.PART.MAIN)
		try:
			holder.clear_flag(lv.obj.FLAG.CLICKABLE)
		except Exception:
			pass
		centers = (11, 25, 39, 53)
		for center_x in centers:
			dot = lv.obj(holder)
			_remove_frame(dot)
			_make_fixed(dot)
			try:
				dot.clear_flag(lv.obj.FLAG.CLICKABLE)
			except Exception:
				pass
			self._root_indicator_dots.append((dot, center_x))
		self.update_root_indicator(0)
		return holder

	def update_root_indicator(self, active_index):
		"""按已吸附的一级页面索引更新圆点，不参与手势判定。"""
		try:
			selected = int(active_index)
		except Exception:
			selected = 0
		for index, item in enumerate(self._root_indicator_dots):
			dot, center_x = item
			size = 8 if index == selected else 6
			dot.set_size(size, size)
			dot.set_pos(center_x - size // 2, 6 - size // 2)
			dot.set_style_radius(size // 2, lv.PART.MAIN)
			dot.set_style_bg_color(CYAN if index == selected else GRAY,
								   lv.PART.MAIN)
			dot.set_style_bg_opa(255, lv.PART.MAIN)
			dot.invalidate()

	def set_status_context(self, context):
		"""同步固定状态栏左侧上下文，不改变状态栏位置。"""
		self._status_context = ("settings" if context == "settings"
								else "police")
		labels = self._status_labels
		label = labels.get("police")
		if label is None:
			return
		if self._status_context == "settings":
			label.set_text("设置")
			label.set_style_text_color(CYAN, lv.PART.MAIN)
		else:
			label.set_text(self._status_police_no)
			label.set_style_text_color(WHITE, lv.PART.MAIN)
		label.invalidate()

	def refresh_status(self, snapshot):
		labels = self._status_labels
		if not labels:
			return
		police_no = snapshot.get("police_no") or "M100001"
		self._status_police_no = str(police_no)
		connected = bool(snapshot.get("network_connected"))
		battery = snapshot.get("battery_percent")
		if self._status_context == "settings":
			labels["police"].set_text("设置")
			labels["police"].set_style_text_color(CYAN, lv.PART.MAIN)
		else:
			labels["police"].set_text(str(police_no))
			labels["police"].set_style_text_color(WHITE, lv.PART.MAIN)
		try:
			self._status_network_icon.set_src(
				ASSET_ROOT + ("wifi_5.jpg" if connected else "wifi_0.jpg"))
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
			icons = {"shoulder": "shoulder_lamp.jpg", "intercom": "intercom.jpg",
					 "settings": "settings.jpg"}
			targets = {"shoulder": "shoulder_menu", "intercom": "intercom_menu",
					   "settings": "settings_menu"}
			_image(page, icons[name], 102, 26, 92, 92,
				   lambda e, n=name: self.app.open_child(targets[n]), self.app)
			_label(page, title, 0, 126, self.app.content_width, 40,
				   WHITE, lv.TEXT_ALIGN.CENTER)
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
		_image(page, "location_icon_92.jpg", 196, 112, 92, 92,
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
		elif name == "shoulder_mode":
			self._make_shoulder_mode(page)
		elif name == "double_click_mode":
			self._make_double_click_mode(page)
		elif name == "group_mode":
			self._make_group_mode(page)
		elif name == "single_mode":
			self._make_single_mode(page)
		elif name == "device_info":
			self._make_device_info(page)
		elif name == "brightness":
			self._make_brightness(page)
		elif name == "lamp_brightness":
			self._make_lamp_brightness(page)
		elif name == "volume":
			self._make_volume(page)
		elif name == "sleep":
			self._make_sleep(page)
		elif name == "network":
			self._make_network(page)
		elif name == "fall_detection":
			self._make_fall_detection(page)
		elif name == "upgrade":
			self._make_upgrade(page)
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
				lambda e: self.show_confirm_dialog(
					"确认退出登录？", self.app.logout_to_login),
				self.app, RED)

	def _make_choice_page(self, page, title, choices):
		_label(page, title, 0, 8, 296, 33, CYAN, lv.TEXT_ALIGN.CENTER)
		for index, choice in enumerate(choices):
			_button(page, choice[0], 18, 56 + index * 60, 260, 48,
					lambda e, target=choice[1]: self.app.open_child(target),
					self.app, GRAY)

	def _snapshot(self):
		_revision, snapshot = self.app.state_store.get_snapshot()
		return snapshot

	@staticmethod
	def _set_radio_visual(row_info, selected):
		row, dot, label, enabled = row_info
		row.set_style_bg_color(
			OPTION_SELECTED_BG if selected else GRAY, lv.PART.MAIN)
		dot.set_style_bg_color(
			CYAN if selected else OPTION_DOT_OFF, lv.PART.MAIN)
		label.set_style_text_color(
			CYAN if selected else (WHITE if enabled else DISABLED_TEXT),
			lv.PART.MAIN)
		row.invalidate()

	def _radio_row(self, parent, y, text, value, selected, enabled,
				   on_select, rows, selection, require_confirm):
		row = lv.btn(parent)
		_remove_frame(row)
		row.set_pos(4, int(y))
		row.set_size(self.app.content_width - 8, LIST_ROW_HEIGHT)
		try:
			row.clear_flag(lv.obj.FLAG.SCROLLABLE)
			row.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
		except Exception:
			pass
		dot = lv.obj(row)
		_remove_frame(dot)
		_make_fixed(dot)
		dot.set_pos(12, 16)
		dot.set_size(16, 16)
		dot.set_style_radius(8, lv.PART.MAIN)
		label = _label(
			row, text, 42, 7, self.app.content_width - 58, 33,
			WHITE if enabled else DISABLED_TEXT, lv.TEXT_ALIGN.LEFT)
		info = (row, dot, label, bool(enabled))
		rows[value] = info
		self._set_radio_visual(info, selected)

		def choose(event, key=value, allowed=bool(enabled)):
			if not allowed or not self.app.can_click_current_touch():
				return
			if key == selection["value"]:
				return

			def commit_selection():
				if not on_select(key):
					return
				selection["value"] = key
				for item_key in rows:
					self._set_radio_visual(rows[item_key], item_key == key)

			if require_confirm:
				self.show_confirm_dialog("确认修改？", commit_selection)
			else:
				commit_selection()
		row.add_event_cb(choose, lv.EVENT.CLICKED, None)
		return row

	def _make_radio_page(self, page, title, options, selected, on_select,
						 list_mode=False, require_confirm=False):
		rows = {}
		selection = {"value": selected}
		if list_mode:
			parent = self._new_list_viewport(page)
			start_y = 0
			step = LIST_ROW_STEP
		else:
			parent = page
			_label(page, title, 0, 4, self.app.content_width, 33,
				   CYAN, lv.TEXT_ALIGN.CENTER)
			start_y = 44
			step = LIST_ROW_STEP
		for index, option in enumerate(options):
			self._radio_row(
				parent, start_y + index * step, option[1], option[0],
				option[0] == selected, option[2], on_select, rows,
				selection, require_confirm)
		if list_mode:
			self._finish_list_viewport(parent, len(options))

	def _make_shoulder_mode(self, page):
		snapshot = self._snapshot()
		options = (("off", "关闭", True),
				   ("alternate", "红蓝交替闪", True),
				   ("diagonal", "红蓝对角闪", True))
		self._make_radio_page(
			page, "肩灯模式", options, snapshot["lamp_mode"],
			self.app.select_lamp_mode, require_confirm=True)

	def _make_double_click_mode(self, page):
		snapshot = self._snapshot()
		options = ((True, "开启", True), (False, "关闭", True))
		self._make_radio_page(
			page, "双击模式", options, snapshot["double_click_enabled"],
			self.app.select_double_click, require_confirm=True)

	def _make_group_mode(self, page):
		snapshot = self._snapshot()
		selected = snapshot["confirmed_group_id"]
		selected = "__none_group__" if selected is None else selected
		options = [("__none_group__", "无/退出", True)]
		for group in snapshot["group_items"]:
			options.append((group["id"], group["name"], True))
		self._make_radio_page(
			page, "", tuple(options), selected, self.app.select_group,
			list_mode=True, require_confirm=True)

	def _make_single_mode(self, page):
		snapshot = self._snapshot()
		selected = snapshot["single_peer"]
		selected = "__none_person__" if selected is None else selected
		options = [("__none_person__", "无/解散", True)]
		for person in snapshot["person_items"]:
			if person["is_self"]:
				continue
			online = bool(person["online"])
			text = person["name"] if online else person["name"] + "（离线）"
			options.append((person["key"], text, online))
		self._make_radio_page(
			page, "", tuple(options), selected, self.app.select_person,
			list_mode=True, require_confirm=True)

	def _make_device_info(self, page):
		snapshot = self._snapshot()
		_label(page, "设备信息", 0, 4, self.app.content_width, 33,
			   CYAN, lv.TEXT_ALIGN.CENTER)
		values = (("软件版本：", snapshot["software_version"]),
				  ("硬件版本：", snapshot["hardware_version"]),
				  ("固件版本：", snapshot["firmware_version"]))
		for index, item in enumerate(values):
			y = 48 + index * 44
			_label(page, item[0], 16, y, 120, 33, WHITE, lv.TEXT_ALIGN.LEFT)
			_label(page, item[1], 136, y, 144, 33, CYAN, lv.TEXT_ALIGN.LEFT)

	def _make_brightness_symbol(self, page, x, y, size, level):
		"""绘制同款大小太阳，以图标尺寸表达高低亮度。"""
		holder = lv.obj(page)
		_remove_frame(holder)
		_make_fixed(holder)
		holder.set_pos(int(x), int(y))
		holder.set_size(int(size), int(size))
		holder.set_style_bg_opa(0, lv.PART.MAIN)
		draw_size = int(size) if level == "high" else int(size) * 3 // 4
		draw_pos = (int(size) - draw_size) // 2
		rays = max(3, draw_size // 8)
		ray_length = max(5, draw_size // 4)
		core_size = max(8, draw_size // 2)
		core_pos = draw_pos + (draw_size - core_size) // 2

		def part(px, py, pw, ph, radius=0):
			item = lv.obj(holder)
			_remove_frame(item)
			_make_fixed(item)
			item.set_pos(int(px), int(py))
			item.set_size(int(pw), int(ph))
			item.set_style_bg_color(WHITE, lv.PART.MAIN)
			item.set_style_bg_opa(255, lv.PART.MAIN)
			try:
				item.set_style_radius(int(radius), lv.PART.MAIN)
			except Exception:
				pass

		part(core_pos, core_pos, core_size, core_size, core_size // 2)
		center = core_pos + core_size // 2 - rays // 2
		edge = draw_pos
		far_edge = draw_pos + draw_size
		ray_items = (
			(center, edge, rays, ray_length),
			(center, far_edge - ray_length, rays, ray_length),
			(edge, center, ray_length, rays),
			(far_edge - ray_length, center, ray_length, rays),
			(edge + 2, edge + 2, rays, rays),
			(far_edge - rays - 2, edge + 2, rays, rays),
			(edge + 2, far_edge - rays - 2, rays, rays),
			(far_edge - rays - 2, far_edge - rays - 2, rays, rays),
		)
		for item in ray_items:
			part(item[0], item[1], item[2], item[3], rays // 2)
		return holder

	def _make_vertical_slider(self, page, title, minimum, maximum, value,
							  on_change, suffix="", mode="brightness"):
		content_height = self.app.content_height
		slider_x = 130
		slider_y = 10
		slider_width = 36
		slider_height = max(1, content_height - 20)
		if mode == "brightness":
			_label(page, title, 178, 2, 110, 28, CYAN, lv.TEXT_ALIGN.RIGHT)
			self._make_brightness_symbol(
				page, 82, slider_y, 32, "high")
			self._make_brightness_symbol(
				page, 82, content_height - 10 - 32, 32, "low")
		else:
			_image(page, "settings_volume.jpg", 80,
				   (content_height - 40) // 2, 40, 40)
		slider = lv.slider(page)
		_remove_frame(slider)
		slider.set_pos(slider_x, slider_y)
		slider.set_size(slider_width, slider_height)
		slider.set_range(int(minimum), int(maximum))
		slider.set_value(int(value), lv.ANIM.OFF)
		slider.set_style_bg_color(lv.color_hex(0x26343C), lv.PART.MAIN)
		slider.set_style_bg_opa(255, lv.PART.MAIN)
		slider.set_style_radius(slider_width // 2, lv.PART.MAIN)
		slider.set_style_bg_color(CYAN, lv.PART.INDICATOR)
		slider.set_style_bg_opa(255, lv.PART.INDICATOR)
		slider.set_style_radius(slider_width // 2, lv.PART.INDICATOR)
		slider.set_style_bg_opa(0, lv.PART.KNOB)
		value_label = _label(
			page, "{}{}".format(value, suffix), slider_x + slider_width + 10,
			86, 110, 33, CYAN, lv.TEXT_ALIGN.LEFT)

		def value_changed(event):
			target = event.get_target()
			new_value = on_change(int(target.get_value()))
			value_label.set_text("{}{}".format(new_value, suffix))
			value_label.invalidate()
		slider.add_event_cb(value_changed, lv.EVENT.VALUE_CHANGED, None)

	def _make_brightness(self, page):
		snapshot = self._snapshot()
		self._make_vertical_slider(
			page, "屏幕亮度", 5, 100, snapshot["screen_brightness"],
			self.app.set_screen_brightness, "%", "brightness")

	def _make_lamp_brightness(self, page):
		snapshot = self._snapshot()
		self._make_vertical_slider(
			page, "肩灯亮度", 5, 100, snapshot["lamp_brightness"],
			self.app.set_lamp_brightness, "%", "brightness")

	def _make_volume(self, page):
		snapshot = self._snapshot()
		self._make_vertical_slider(
			page, "", 0, 11, snapshot["audio_volume"],
			self.app.set_audio_volume, "", "volume")

	def _make_sleep(self, page):
		snapshot = self._snapshot()
		options = ((10, "10秒", True), (15, "15秒", True),
				   (20, "20秒", True), (30, "30秒", True),
				   (60, "1分钟", True), (None, "永不息屏", True))
		self._make_radio_page(
			page, "", options, snapshot["sleep_timeout_seconds"],
			self.app.select_sleep_timeout,
			list_mode=True, require_confirm=True)

	def _make_network(self, page):
		snapshot = self._snapshot()
		_label(page, "网络状态", 0, 4, self.app.content_width, 33,
			   CYAN, lv.TEXT_ALIGN.CENTER)
		connected = bool(snapshot["network_connected"])
		items = (("SIM卡号：", snapshot["iccid"] or "未识别到",
				  WHITE if snapshot["iccid"] else RED),
				 ("信号值：", snapshot["signal_level"] or "无",
				  GREEN if snapshot["signal_level"] in ("极好", "强") else WHITE),
				 ("网络状态：", "成功连接" if connected else "连接失败",
				  GREEN if connected else RED))
		for index, item in enumerate(items):
			y = 50 + index * 50
			_label(page, item[0], 16, y, 114, 33, WHITE, lv.TEXT_ALIGN.LEFT)
			_label(page, item[1], 130, y, 154, 33, item[2],
				   lv.TEXT_ALIGN.LEFT)

	def _make_fall_detection(self, page):
		snapshot = self._snapshot()
		options = ((True, "开启", True), (False, "关闭", True))
		self._make_radio_page(
			page, "跌倒检测", options, snapshot["fall_detection_enabled"],
			self.app.select_fall_detection)

	def _make_upgrade(self, page):
		snapshot = self._snapshot()
		_label(page, "升级", 0, 4, self.app.content_width, 33,
			   CYAN, lv.TEXT_ALIGN.CENTER)
		_label(page, "当前版本：", 20, 54, 130, 33, WHITE, lv.TEXT_ALIGN.LEFT)
		_label(page, snapshot["firmware_version"], 150, 54, 126, 33,
			   CYAN, lv.TEXT_ALIGN.LEFT)
		status = _label(page, snapshot["upgrade_status"], 8, 100,
						self.app.content_width - 16, 33, WHITE,
						lv.TEXT_ALIGN.CENTER)

		def check_upgrade(event):
			status.set_text(self.app.preview_check_upgrade())
			status.set_style_text_color(GREEN, lv.PART.MAIN)
			status.invalidate()
		_button(page, "检查更新", 48, 152, 200, 48,
				check_upgrade, self.app, BLUE)

	def _make_settings(self, page):
		viewport = self._new_list_viewport(page)
		rows = (("settings_device.jpg", "设备信息", "device_info"),
				("settings_brightness.jpg", "屏幕亮度", "brightness"),
				("settings_shoulder_brightness.jpg", "肩灯亮度", "lamp_brightness"),
				("settings_volume.jpg", "音量", "volume"),
				("settings_sleep.jpg", "息屏时间", "sleep"),
				("settings_network.jpg", "网络状态", "network"),
				("settings_fall_detection.jpg", "跌倒检测", "fall_detection"),
				(None, "升级", "upgrade"))
		for index, row_data in enumerate(rows):
			row = lv.btn(viewport)
			_remove_frame(row)
			row.set_pos(4, index * LIST_ROW_STEP)
			row.set_size(self.app.content_width - 8, LIST_ROW_HEIGHT)
			try:
				row.clear_flag(lv.obj.FLAG.SCROLLABLE)
				row.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
			except Exception:
				pass
			row.set_style_bg_color(GRAY, lv.PART.MAIN)
			row.set_style_bg_opa(255, lv.PART.MAIN)
			if row_data[0] is not None:
				_image(row, row_data[0], 8, 8, 32, 32)
			_label(row, row_data[1], 48, 7, 228, 33, WHITE,
				   lv.TEXT_ALIGN.LEFT)
			def guarded_row(event, target=row_data[2]):
				if self.app.can_click_current_touch():
					self.app.open_child(target)
			row.add_event_cb(guarded_row, lv.EVENT.CLICKED, None)
		self._finish_list_viewport(viewport, len(rows))

	def _new_list_viewport(self, page):
		viewport = lv.obj(page)
		_remove_frame(viewport)
		viewport.set_pos(0, 0)
		viewport.set_size(self.app.content_width, self.app.content_height)
		viewport.set_style_bg_color(BLACK, lv.PART.MAIN)
		viewport.set_style_bg_opa(255, lv.PART.MAIN)
		# 210px内容区配合48px行高和4px行距，一屏完整显示四项。
		viewport.set_style_pad_bottom(6, lv.PART.MAIN)
		viewport.set_scroll_dir(lv.DIR.VER)
		viewport.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
		try:
			viewport.set_scroll_chain(lv.DIR.NONE)
		except Exception:
			pass
		viewport.add_flag(lv.obj.FLAG.SCROLLABLE)
		try:
			viewport.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
		except Exception:
			pass
		return viewport

	def _finish_list_viewport(self, viewport, item_count):
		"""完成内容尺寸计算，并绑定不截断惯性的整行收尾。"""
		try:
			viewport.update_layout()
		except Exception:
			try:
				lv.obj_update_layout(viewport)
			except Exception:
				pass
		try:
			viewport.scroll_to_y(0, lv.ANIM.OFF)
		except Exception:
			try:
				lv.obj_scroll_to_y(viewport, 0, lv.ANIM.OFF)
			except Exception:
				pass
		try:
			viewport.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
		except Exception:
			pass
		last_row_start = max(
			0, (int(item_count) - LIST_VISIBLE_ROWS) * LIST_ROW_STEP)
		self._bind_list_scroll_settle(viewport, last_row_start)

	def _bind_list_scroll_settle(self, viewport, last_row_start):
		state = {
			"viewport": viewport,
			"last_row_start": int(last_row_start),
			"tracking": False,
			"last_y": None,
			"stable_since": None,
			"target": None,
			"settling": False,
		}
		self._list_scroll_states.append(state)
		def scroll_activity(event, target=viewport):
			self._list_scroll_activity(state)
		def scroll_deleted(event, target=viewport):
			self._list_scroll_deleted(state)
		viewport.add_event_cb(
			scroll_activity, lv.EVENT.SCROLL_BEGIN, None)
		viewport.add_event_cb(
			scroll_activity, lv.EVENT.SCROLL, None)
		viewport.add_event_cb(
			scroll_deleted, lv.EVENT.DELETE, None)
		if self._list_scroll_timer is None:
			try:
				self._list_scroll_timer = lv.timer_create(
					self._list_scroll_poll, LIST_SETTLE_POLL_MS, None)
				self._pause_list_scroll_timer()
			except Exception:
				self._list_scroll_timer = None

	@staticmethod
	def _read_list_scroll_y(viewport):
		try:
			return int(viewport.get_scroll_y())
		except Exception:
			try:
				return int(lv.obj_get_scroll_y(viewport))
			except Exception:
				return None

	def _pause_list_scroll_timer(self):
		timer = self._list_scroll_timer
		if timer is None:
			return
		try:
			timer.pause()
		except Exception:
			pass

	def _resume_list_scroll_timer(self):
		timer = self._list_scroll_timer
		if timer is None:
			return
		try:
			timer.reset()
			timer.resume()
		except Exception:
			pass

	def _list_scroll_activity(self, state):
		"""只记录原生滚动活动，不修改跟手或惯性过程。"""
		if state["settling"] or state not in self._list_scroll_states:
			return
		now = utime.ticks_ms()
		was_tracking = state["tracking"]
		if self.app.touch.pressed:
			state["target"] = None
		state["tracking"] = True
		state["last_y"] = self._read_list_scroll_y(state["viewport"])
		state["stable_since"] = now
		if not was_tracking:
			self._resume_list_scroll_timer()

	def _list_scroll_deleted(self, state):
		"""列表销毁后解除引用，避免定时器持有失效的LVGL对象。"""
		self._list_scroll_states = [
			item for item in self._list_scroll_states if item is not state]
		if not any(item["tracking"] for item in self._list_scroll_states):
			self._pause_list_scroll_timer()

	def _list_scroll_poll(self, timer):
		"""原生惯性真正静止后，再将活动列表补齐到最近的完整行。"""
		now = utime.ticks_ms()
		for state in tuple(self._list_scroll_states):
			if state["tracking"]:
				self._poll_one_list_scroll(state, now)
		if not any(item["tracking"] for item in self._list_scroll_states):
			self._pause_list_scroll_timer()

	def _poll_one_list_scroll(self, state, now):
		viewport = state["viewport"]
		scroll_y = self._read_list_scroll_y(viewport)
		if scroll_y is None:
			state["tracking"] = False
			state["target"] = None
			return
		if self.app.touch.pressed:
			state["target"] = None
			state["last_y"] = scroll_y
			state["stable_since"] = now
			return
		if scroll_y != state["last_y"]:
			state["last_y"] = scroll_y
			state["stable_since"] = now
			return
		stable_since = state["stable_since"]
		if (stable_since is None or
				utime.ticks_diff(now, stable_since) < LIST_SETTLE_STABLE_MS):
			return
		if state["target"] is not None:
			state["target"] = None
			state["tracking"] = False
			return
		target = ((scroll_y + LIST_ROW_STEP // 2) //
				  LIST_ROW_STEP) * LIST_ROW_STEP
		target = max(0, min(state["last_row_start"], target))
		if target == scroll_y:
			state["tracking"] = False
			return
		state["target"] = target
		state["settling"] = True
		aligned = True
		try:
			try:
				viewport.scroll_to_y(target, lv.ANIM.ON)
			except Exception:
				try:
					lv.obj_scroll_to_y(viewport, target, lv.ANIM.ON)
				except Exception:
					aligned = False
		finally:
			state["settling"] = False
		if not aligned:
			state["target"] = None
			state["tracking"] = False
			return
		state["last_y"] = None
		state["stable_since"] = now

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
