# -*- coding: utf-8 -*-
import _thread
class UIStateStore:
	def __init__(self, initial_settings=None):
		self._lock = _thread.allocate_lock()
		self._revision = 0
		self._reported_revision = -1
		self._state = {
			"network_connected": False,
			# 传输层连接状态：与POC登录/运行态分开，供状态栏判断。
			"network_ready": False,
			"tcp_socket_connected": False,
			"network_stage": None,
			"network_state": None,
			"iccid": None,
			"signal_dbm": None,
			"signal_level": None,
			"connection_event_revision": 0,
			"connection_stage": None,
			"connection_status": None,
			"connection_text": None,
			"connection_duration_ms": None,
			"connection_events": (),
			"guest_relogin_prompt_revision": 0,
			"battery_percent": None,
			"battery_voltage_mv": None,
			"battery_charging": None,
			"battery_full_confirmed": False,
			"gnss_state": None,
			"gnss_fix": False,
			"checkin_id": 0,
			"checkin_phase": "idle",
			"latitude": None,
			"longitude": None,
			"latitude_text": None,
			"longitude_text": None,
			"location_name": None,
			"altitude": None,
			"satellites": None,
			"location_time": None,
			"location_weekday": None,
			"tcp_connected": False,
			"login_state": "idle",
			"login_result": None,
			"login_error": None,
			"login_transaction_id": 0,
			"force_login_stage": "idle",
			"logout_active": False,
			"logout_event_revision": 0,
			"logout_result": None,
			"forced_logout_revision": 0,
			"confirmed_group_id": None,
			"floor_state": "idle",
			"floor_group_id": None,
			"floor_event_revision": 0,
			"floor_event_type": None,
			"floor_direction": None,
			"floor_call_type": None,
			"floor_event_group_id": None,
			"floor_speaker_name": None,
			"floor_audio_selected": False,
			"single_state": "idle",
			"single_peer": None,
			"single_group_id": None,
			"single_error": None,
			# None表示HTTP尚未确认本机权限；True/False对应ON/OFF。
			"poc_enabled": None,
			"audio_tx_active": False,
			"audio_rx_active": False,
			"audio_volume": 11,
			"volume_key_event_revision": 0,
			"volume_key_event_value": 11,
			"screen_brightness": 100,
			"screen_sleeping": False,
			"sleep_timeout_seconds": 30,
			# 已进入游客或正常业务页面；网络/TCP状态不得改变该资格。
			"business_page_active": False,
			"fall_detection_enabled": False,
			"lamp_mode": "off",
			"lamp_brightness": 42,
			"lamp_alarm_active": False,
			"double_click_enabled": False,
			"key_notice_revision": 0,
			"key_notice_text": None,
			"key_notice_success": True,
			"key_notice_duration_ms": None,
			"group_items": (),
			"person_items": (),
			"software_version": "--",
			"hardware_version": "--",
			"firmware_version": "--",
			"device_id": "--",
			"upgrade_status": "",
			"upgrade_status_tone": "normal",
			"ota_busy": False,
			"ota_thread_active": False,
			"ota_query_active": False,
			"ota_phase": "idle",
			"ota_event_revision": 0,
			"ota_event_kind": None,
			"ota_event_text": None,
			"ota_event_duration_ms": None,
			"identity_mode": "normal",
			"police_name": "--",
			"police_no": "M100001",
			"last_error_domain": None,
			"last_error_text": None,
		}
		if isinstance(initial_settings, dict):
			for key in ("audio_volume", "lamp_brightness",
						"screen_brightness"):
				if key in initial_settings:
					self._state[key] = int(initial_settings[key])
			if isinstance(initial_settings.get("double_click_enabled"), bool):
				self._state["double_click_enabled"] = initial_settings[
					"double_click_enabled"]
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
	def update_transport(self, network_ready=None, tcp_connected=None):
		values = {}
		if network_ready is not None:
			values["network_ready"] = bool(network_ready)
		if tcp_connected is not None:
			values["tcp_socket_connected"] = bool(tcp_connected)
		return self._update(values) if values else False
	def publish_connection_event(self, stage, status, text, duration_ms=None,
								 user_visible=False):
		self._lock.acquire()
		try:
			self._state["connection_event_revision"] += 1
			revision = self._state["connection_event_revision"]
			stage = str(stage)
			status = str(status)
			text = str(text)
			self._state["connection_stage"] = stage
			self._state["connection_status"] = status
			self._state["connection_text"] = text
			self._state["connection_duration_ms"] = duration_ms
			events = self._state["connection_events"] + (
				(revision, stage, status, text, duration_ms, bool(user_visible)),)
			# 连接阶段可能连续变化；保留最近事件，避免UI轮询只看到最后一个。
			self._state["connection_events"] = events[-8:]
			self._changed()
			return revision
		finally:
			self._lock.release()
	def publish_guest_relogin_prompt(self):
		self._lock.acquire()
		try:
			self._state["guest_relogin_prompt_revision"] += 1
			self._changed()
			return self._state["guest_relogin_prompt_revision"]
		finally:
			self._lock.release()
	def publish_forced_logout(self):
		self._lock.acquire()
		try:
			self._state["forced_logout_revision"] += 1
			self._changed()
			return self._state["forced_logout_revision"]
		finally:
			self._lock.release()
	def clear_guest_relogin_prompt(self):
		# 修订号保持单调，UI只需记录已消费的修订号即可。
		return False
	def update_battery(self, percent, voltage_mv, charging,
					full_confirmed=False):
		return self._update({
			"battery_percent": percent,
			"battery_voltage_mv": voltage_mv,
			"battery_charging": charging,
			"battery_full_confirmed": bool(full_confirmed),
		})
	def update_location_clock(self, time_text, weekday):
		return self._update({"location_time": time_text,
			"location_weekday": weekday})
	def update_checkin(self, request_id, phase):
		return self._update({"checkin_id": request_id, "checkin_phase": phase})
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
	def update_identity_mode(self, mode):
		value = "guest" if str(mode) == "guest" else "normal"
		return self._update({"identity_mode": value})
	def update_poc_access(self, enabled):
		value = None if enabled is None else bool(enabled)
		return self._update({"poc_enabled": value})
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
	def update_login_transaction(self, login_state, result=None, error=None,
							 force_stage="idle", transaction_id=None):
		values = {
			"login_state": str(login_state),
			"login_result": result,
			"login_error": error,
			"force_login_stage": str(force_stage),
		}
		if transaction_id is not None:
			values["login_transaction_id"] = int(transaction_id)
		return self._update(values)
	def begin_logout(self):
		return self._update({
			"logout_active": True,
			"logout_result": None,
		})
	def finish_logout(self, result):
		self._lock.acquire()
		try:
			self._state["logout_active"] = False
			self._state["logout_result"] = str(result)
			self._state["logout_event_revision"] += 1
			self._changed()
			return self._state["logout_event_revision"]
		finally:
			self._lock.release()
	def publish_floor_event(self, event_type, direction, call_type, group_id,
							speaker_name=None, audio_selected=False):
		self._lock.acquire()
		try:
			revision = int(self._state["floor_event_revision"]) + 1
			self._state["floor_event_revision"] = revision
			self._state["floor_event_type"] = event_type
			self._state["floor_direction"] = direction
			self._state["floor_call_type"] = call_type
			self._state["floor_event_group_id"] = group_id
			self._state["floor_speaker_name"] = speaker_name
			self._state["floor_audio_selected"] = bool(audio_selected)
			self._changed()
			return revision
		finally:
			self._lock.release()
	def update_audio(self, tx_active, rx_active, volume):
		return self._update({
			"audio_tx_active": bool(tx_active),
			"audio_rx_active": bool(rx_active),
			"audio_volume": int(volume),
		})
	def publish_volume_key_event(self, volume):
		self._lock.acquire()
		try:
			self._state["volume_key_event_revision"] += 1
			self._state["volume_key_event_value"] = int(volume)
			self._changed()
			return self._state["volume_key_event_revision"]
		finally:
			self._lock.release()
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
	def publish_key_notice(self, text, success=True, duration_ms=2000):
		self._lock.acquire()
		try:
			self._state["key_notice_revision"] += 1
			self._state["key_notice_text"] = str(text)
			self._state["key_notice_success"] = bool(success)
			self._state["key_notice_duration_ms"] = int(duration_ms)
			self._changed()
			return self._state["key_notice_revision"]
		finally:
			self._lock.release()
	def update_screen_brightness(self, percent):
		return self._update({
			"screen_brightness": int(percent),
		})
	def update_screen_sleeping(self, sleeping):
		return self._update({"screen_sleeping": bool(sleeping)})
	def update_gnss_location(self, state, fix, latitude, longitude, altitude,
							 satellites,
							 latitude_text=None, longitude_text=None):
		return self._update({"gnss_state": state, "gnss_fix": bool(fix),
			"latitude": latitude, "longitude": longitude, "altitude": altitude,
			"satellites": satellites,
			"latitude_text": latitude_text,
			"longitude_text": longitude_text})
	def update_sleep_timeout(self, seconds):
		return self._update({
			"sleep_timeout_seconds": seconds,
		})
	def update_business_page_active(self, active):
		return self._update({
			"business_page_active": bool(active),
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
	def update_single_call(self, single_state, single_peer,
						   single_group_id=None, error=None):
		return self._update({
			"single_state": str(single_state),
			"single_peer": single_peer,
			"single_group_id": single_group_id,
			"single_error": error,
		})
	def update_device_versions(self, software, hardware, firmware,
							 device_id=None):
		values = {
			"software_version": str(software),
			"hardware_version": str(hardware),
			"firmware_version": str(firmware),
		}
		if device_id is not None:
			values["device_id"] = str(device_id)
		return self._update(values)
	def update_upgrade_status(self, status, tone="normal"):
		return self._update({"upgrade_status": str(status),
			"upgrade_status_tone": str(tone)})
	def update_ota_runtime(self, busy=None, thread_active=None,
						 query_active=None, phase=None):
		values = {}
		if busy is not None:
			values["ota_busy"] = bool(busy)
		if thread_active is not None:
			values["ota_thread_active"] = bool(thread_active)
		if query_active is not None:
			values["ota_query_active"] = bool(query_active)
		if phase is not None:
			values["ota_phase"] = str(phase)
		return self._update(values) if values else False
	def publish_ota_event(self, kind, text=None, duration_ms=None):
		self._lock.acquire()
		try:
			self._state["ota_event_revision"] += 1
			self._state["ota_event_kind"] = kind
			self._state["ota_event_text"] = text
			self._state["ota_event_duration_ms"] = duration_ms
			self._changed()
			return self._state["ota_event_revision"]
		finally:
			self._lock.release()
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
	global _default_store
	_default_store_lock.acquire()
	try:
		if _default_store is None:
			_default_store = UIStateStore()
		return _default_store
	finally:
		_default_store_lock.release()
