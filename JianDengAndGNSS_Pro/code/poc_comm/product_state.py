# -*- coding: utf-8 -*-
import _thread


class ProductState:
	"""无显示产品使用的最小状态存储，兼容 OTAService 所需接口。"""
	def __init__(self, initial_settings=None):
		self._lock = _thread.allocate_lock()
		self._revision = 0
		self._state = {
			"network_connected": False,
			"tcp_socket_connected": False,
			"software_version": "--",
			"hardware_version": "--",
			"firmware_version": "--",
			"device_id": "--",
			"battery_voltage_mv": None,
			"battery_percent": None,
			"gnss_state": None,
			"gnss_fix": False,
			"latitude": None,
			"longitude": None,
			"altitude": None,
			"satellites": None,
			"latitude_text": None,
			"longitude_text": None,
			"ota_busy": False,
			"ota_thread_active": False,
			"ota_query_active": False,
			"ota_phase": "idle",
			"upgrade_status": "",
			"upgrade_status_tone": "normal",
			"ota_event_revision": 0,
			"ota_event_kind": None,
			"ota_event_text": None,
		}
		if isinstance(initial_settings, dict):
			self._state["device_id"] = str(
				initial_settings.get("device_id") or "--")

	def _update(self, values):
		self._lock.acquire()
		try:
			changed = False
			for key, value in values.items():
				if self._state.get(key) != value:
					self._state[key] = value
					changed = True
			if changed:
				self._revision += 1
			return changed
		finally:
			self._lock.release()

	def get_snapshot(self):
		self._lock.acquire()
		try:
			return self._revision, self._state.copy()
		finally:
			self._lock.release()

	def update_network(self, connected, stage=None, state=None,
					   iccid=None, signal_dbm=None, signal_level=None):
		return self._update({"network_connected": bool(connected)})

	def update_transport(self, network_ready=None, tcp_connected=None):
		values = {}
		if network_ready is not None:
			values["network_connected"] = bool(network_ready)
		if tcp_connected is not None:
			values["tcp_socket_connected"] = bool(tcp_connected)
		return self._update(values)

	def update_device_versions(self, software, hardware, firmware, device_id):
		return self._update({
			"software_version": str(software),
			"hardware_version": str(hardware),
			"firmware_version": str(firmware),
			"device_id": str(device_id),
		})

	def update_battery(self, percent, voltage_mv, charging=False,
					   full_confirmed=False):
		return self._update({
			"battery_percent": percent,
			"battery_voltage_mv": voltage_mv,
		})

	def update_gnss_location(self, state, fix, latitude, longitude, altitude,
							 satellites, location_time=None,
							 location_weekday=None, latitude_text=None,
							 longitude_text=None):
		return self._update({
			"gnss_state": state,
			"gnss_fix": bool(fix),
			"latitude": latitude,
			"longitude": longitude,
			"altitude": altitude,
			"satellites": satellites,
			"latitude_text": latitude_text,
			"longitude_text": longitude_text,
		})

	def update_upgrade_status(self, text, tone="normal"):
		return self._update({
			"upgrade_status": str(text),
			"upgrade_status_tone": str(tone),
		})

	def update_ota_runtime(self, **values):
		accepted = {}
		for key in ("busy", "thread_active", "query_active", "phase"):
			if key not in values:
				continue
			name = {
				"busy": "ota_busy",
				"thread_active": "ota_thread_active",
				"query_active": "ota_query_active",
				"phase": "ota_phase",
			}[key]
			accepted[name] = bool(values[key]) if key != "phase" else str(values[key])
		return self._update(accepted)

	def publish_ota_event(self, kind, text=None, duration_ms=None):
		self._lock.acquire()
		try:
			self._state["ota_event_revision"] += 1
			self._state["ota_event_kind"] = str(kind)
			self._state["ota_event_text"] = None if text is None else str(text)
			self._revision += 1
			return self._state["ota_event_revision"]
		finally:
			self._lock.release()
