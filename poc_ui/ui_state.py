# -*- coding: utf-8 -*-
"""UI 可读取的业务状态快照。

业务线程通过显式的 update_* 方法写入状态，UI 主线程只读取快照；本模块
不依赖 LVGL，也不访问任何硬件。状态写入使用一把短临界区锁，快照读取
返回独立字典，避免 UI 持有业务线程内部对象。
"""

import _thread


class UIStateStore:
	"""线程安全的 UI 状态存储，采用版本号通知变化。"""

	def __init__(self):
		self._lock = _thread.allocate_lock()
		self._revision = 0
		self._reported_revision = -1
		self._state = {
			# 网络和 SIM
			"network_connected": False,
			"network_stage": None,
			"network_state": None,
			"iccid": None,
			"signal_dbm": None,
			"signal_level": None,
			# 电池
			"battery_percent": None,
			"battery_voltage_mv": None,
			"battery_charging": None,
			# GNSS
			"gnss_state": None,
			"gnss_fix": False,
			"latitude": None,
			"longitude": None,
			"location_name": None,
			"altitude": None,
			"satellites": None,
			"location_time": None,
			"location_weekday": None,
			# POC/TCP 会话
			"tcp_connected": False,
			"login_state": "idle",
			"login_error": None,
			"confirmed_group_id": None,
			"floor_state": "idle",
			"floor_group_id": None,
			"single_state": "idle",
			"single_peer": None,
			# RTP 和按键
			"audio_tx_active": False,
			"audio_rx_active": False,
			"audio_volume": 11,
			# 本机显示与交互设置
			"screen_brightness": 100,
			"sleep_timeout_seconds": 30,
			"fall_detection_enabled": False,
			# 肩灯和运动
			"lamp_mode": "off",
			"lamp_brightness": 42,
			"lamp_alarm_active": False,
			"double_click_enabled": True,
			# 对讲名单由业务层归一化后整体替换，页面不解析原始接口数据。
			"group_items": (),
			"person_items": (),
			# 设备与升级信息
			"software_version": "--",
			"hardware_version": "--",
			"firmware_version": "--",
			"upgrade_status": "等待检查",
			# 当前纯 UI 基线使用的身份快照；后续由真实接口更新。
			"police_name": "潘荣丰",
			"police_no": "M100001",
			# 统一错误字段，供 UI 显示最近一次可见错误
			"last_error_domain": None,
			"last_error_text": None,
		}

	def _changed(self):
		self._revision += 1

	def _update(self, values):
		self._lock.acquire()
		try:
			changed = False
			for key, value in values.items():
				if self._state[key] != value:
					self._state[key] = value
					changed = True
			if changed:
				self._changed()
			return changed
		finally:
			self._lock.release()

	def update_network(self, connected, stage, state, iccid,
					   signal_dbm, signal_level):
		return self._update({
			"network_connected": bool(connected),
			"network_stage": stage,
			"network_state": state,
			"iccid": iccid,
			"signal_dbm": signal_dbm,
			"signal_level": signal_level,
		})

	def update_battery(self, percent, voltage_mv, charging):
		return self._update({
			"battery_percent": percent,
			"battery_voltage_mv": voltage_mv,
			"battery_charging": charging,
		})

	def update_gnss(self, state, fix, latitude, longitude, location_name):
		return self._update({
			"gnss_state": state,
			"gnss_fix": bool(fix),
			"latitude": latitude,
			"longitude": longitude,
			"location_name": location_name,
		})

	def update_location(self, location_name, latitude, longitude, altitude,
						satellites, location_time, location_weekday):
		return self._update({
			"location_name": location_name,
			"latitude": latitude,
			"longitude": longitude,
			"altitude": altitude,
			"satellites": satellites,
			"location_time": location_time,
			"location_weekday": location_weekday,
		})

	def update_identity(self, police_name, police_no):
		return self._update({
			"police_name": police_name,
			"police_no": police_no,
		})

	def update_poc(self, tcp_connected, login_state, login_error,
				   confirmed_group_id, floor_state, floor_group_id,
				   single_state, single_peer):
		return self._update({
			"tcp_connected": bool(tcp_connected),
			"login_state": login_state,
			"login_error": login_error,
			"confirmed_group_id": confirmed_group_id,
			"floor_state": floor_state,
			"floor_group_id": floor_group_id,
			"single_state": single_state,
			"single_peer": single_peer,
		})

	def update_audio(self, tx_active, rx_active, volume):
		return self._update({
			"audio_tx_active": bool(tx_active),
			"audio_rx_active": bool(rx_active),
			"audio_volume": int(volume),
		})

	def update_lamp(self, mode, brightness, alarm_active):
		return self._update({
			"lamp_mode": mode,
			"lamp_brightness": int(brightness),
			"lamp_alarm_active": bool(alarm_active),
		})

	def update_motion(self, double_click_enabled):
		return self._update({
			"double_click_enabled": bool(double_click_enabled),
		})

	def update_screen_brightness(self, percent):
		return self._update({
			"screen_brightness": int(percent),
		})

	def update_sleep_timeout(self, seconds):
		return self._update({
			"sleep_timeout_seconds": seconds,
		})

	def update_fall_detection(self, enabled):
		return self._update({
			"fall_detection_enabled": bool(enabled),
		})

	def update_intercom_directory(self, groups, people):
		return self._update({
			"group_items": tuple(groups),
			"person_items": tuple(people),
		})

	def update_group_selection(self, group_id):
		return self._update({
			"confirmed_group_id": group_id,
		})

	def update_single_selection(self, person_key):
		return self._update({
			"single_peer": person_key,
		})

	def update_device_versions(self, software, hardware, firmware):
		return self._update({
			"software_version": str(software),
			"hardware_version": str(hardware),
			"firmware_version": str(firmware),
		})

	def update_upgrade_status(self, status):
		return self._update({
			"upgrade_status": str(status),
		})

	def set_error(self, domain, text):
		return self._update({
			"last_error_domain": domain,
			"last_error_text": text,
		})

	def get_snapshot(self):
		self._lock.acquire()
		try:
			return self._revision, self._state.copy()
		finally:
			self._lock.release()

	def get_snapshot_if_changed(self):
		self._lock.acquire()
		try:
			if self._revision == self._reported_revision:
				return None
			self._reported_revision = self._revision
			return self._revision, self._state.copy()
		finally:
			self._lock.release()


_default_store = None
_default_store_lock = _thread.allocate_lock()


def get_default_state_store():
	"""返回进程内唯一状态存储实例。"""
	global _default_store
	_default_store_lock.acquire()
	try:
		if _default_store is None:
			_default_store = UIStateStore()
		return _default_store
	finally:
		_default_store_lock.release()
