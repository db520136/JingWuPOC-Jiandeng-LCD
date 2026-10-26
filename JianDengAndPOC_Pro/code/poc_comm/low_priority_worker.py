# -*- coding: utf-8 -*-
import _thread
import math
import utime
from machine import Timer
from usr.ui_commands import (CMD_ACCEL_DOUBLE_CLICK_IRQ,
							 CMD_CHANGE_VOLUME, CMD_COMMIT_LAMP_BRIGHTNESS,
							 CMD_COMMIT_SCREEN_BRIGHTNESS, CMD_DOUBLE_CLICK,
							 CMD_REFRESH_GNSS, CMD_REFRESH_NETWORK,
							 CMD_SET_DOUBLE_CLICK_ENABLED,
							 CMD_SET_LAMP_BRIGHTNESS, CMD_SET_LAMP_MODE,
							 CMD_SET_SCREEN_BRIGHTNESS, CMD_SET_SLEEP_TIMEOUT,
							 CMD_WAKE_SCREEN,
							 UICommandQueue)
from usr.ui_messages import (MSG_BATTERY_CHANGED,
							 MSG_DOUBLE_CLICK as MSG_DOUBLE_CLICK_EVENT,
							 MSG_ERROR, MSG_LAMP_CHANGED,
							 MSG_NETWORK_CHANGED, UIMessageBus)
from usr.ui_state import UIStateStore
from usr.hardware_key_port import (FUNCTION_KEY_ACTIONS, KEY_PTT_PRESS,
	KEY_LAMP_PRESS, KEY_LAMP_RELEASE, KEY_SCREEN_PRESS,
	KEY_SCREEN_RELEASE)
PERIODIC_INTERVAL_MS = 1000
NETWORK_REFRESH_PERIODS = 5
BATTERY_LOAD_RECOVERY_MS = 3000
SLEEP_IDLE_GRACE_MS = 3000
BATTERY_BOOT_SAMPLE_INTERVAL_MS = 20
BATTERY_BOOT_MAX_ATTEMPTS = 60
SCREEN_KEY_MAX_PRESS_MS = 1500
LAMP_KEY_LONG_PRESS_MS = 2000
KEY_NOTICE_DURATION_MS = 2000
GNSS_FAST_SAMPLE_INTERVAL_MS = 5000
GNSS_FAST_SAMPLE_MAX_ATTEMPTS = 10
class LowPriorityWorker:
	def __init__(self, command_queue=None, state_store=None,
				 message_bus=None, battery=None, network=None, lamp=None,
				 motion_sensor=None, device=None, hardware_keys=None,
				 key_action_callback=None, gnss_result_callback=None,
				 persistent_config=None,
				 gnss_interval_ms=30000, gnss_change_threshold_m=5.0):
		self.commands = (command_queue if command_queue is not None else
						 UICommandQueue())
		self.state = (state_store if state_store is not None else
					  UIStateStore())
		self.messages = (message_bus if message_bus is not None else
						 UIMessageBus())
		self.battery = battery
		self.network = network
		self.lamp = lamp
		self.motion_sensor = motion_sensor
		self.device = device
		self.hardware_keys = hardware_keys
		self.key_action_callback = key_action_callback
		self.gnss_result_callback = gnss_result_callback
		self.persistent_config = persistent_config
		self.gnss_interval_ms = max(1000, int(gnss_interval_ms))
		self.gnss_change_threshold_m = max(0.0, float(gnss_change_threshold_m))
		self.running = False
		self.thread_id = None
		self._timer = None
		self._last_double_click_ms = None
		self._sleep_timer = None
		self._sleep_timer_armed = False
		self._sleep_eligible = False
		self._last_activity_ms = utime.ticks_ms()
		self._idle_gate_until_ms = utime.ticks_add(
			self._last_activity_ms, SLEEP_IDLE_GRACE_MS)
		self._last_busy_state = False
		self._last_gnss_status = None
		self._last_gnss_init_error = None
		self._last_valid_location = None
		self._gnss_sampling_enabled = False
		# 定位初始化成功后先短周期搜星，成功或达到上限后恢复正常周期。
		self._gnss_phase = "idle"
		self._gnss_fast_attempts = 0
		self._gnss_next_sample_at = None
		self._gnss_overdue = False
		self._gnss_force_report = False
		self._screen_key_pressed_at = None
		self._lamp_key_pressed_at = None
		self._lamp_key_long_fired = False
		self._lamp_key_timer = None
		self._pending_persistent_settings = {}
		self._periodic_count = 0
		self._battery_load_signature = None
		self._battery_recovery_until_ms = None
		self._device_identity_logged = False
	def start(self):
		if self.running:
			return False
		self.running = True
		try:
			self.thread_id = _thread.start_new_thread(self._run, ())
			return True
		except Exception:
			self.running = False
			self.thread_id = None
			raise
	def stop(self):
		self.running = False
		if self._timer is not None:
			try:
				self._timer.stop()
			except Exception:
				pass
			self._timer = None
		self.commands.signal_stop()
		self._stop_sleep_timer()
		self._stop_lamp_key_timer()
	def note_ui_activity(self, now_ms):
		self._mark_activity(now_ms)
	def _mark_activity(self, now_ms=None):
		if now_ms is None:
			now_ms = utime.ticks_ms()
		self._last_activity_ms = now_ms
		self._idle_gate_until_ms = utime.ticks_add(
			now_ms, SLEEP_IDLE_GRACE_MS)
	def notify_hardware_key(self):
		return self.commands.signal_hardware_key_isr()
	def request_gnss_sample(self, force=True):
		self._gnss_sampling_enabled = True
		if force:
			self._start_gnss_fast_sampling("强制采样请求")
			self._gnss_force_report = True
			self._gnss_overdue = True
		return self.commands.put_coalesced(CMD_REFRESH_GNSS)
	def _start_gnss_fast_sampling(self, reason):
		self._gnss_phase = "fast"
		self._gnss_fast_attempts = 0
		self._gnss_next_sample_at = None
		self._gnss_overdue = True
	def _finish_gnss_fast_sampling(self, reason):
		self._gnss_phase = "normal"
		self._gnss_next_sample_at = utime.ticks_add(
			utime.ticks_ms(), self.gnss_interval_ms)
		self._gnss_overdue = False
	def request_screen_wake(self, reason="intercom"):
		return self.commands.put_coalesced(
			CMD_WAKE_SCREEN, str(reason), priority=0)
	def _wake_screen(self, reason="GPIO5短按"):
		self._mark_activity()
		if self.device is None:
			return False
		if not self.device.screen_sleeping:
			self._schedule_sleep()
			return False
		try:
			self.device.screen_on()
			self.state.update_screen_sleeping(False)
			self._schedule_sleep()
			return True
		except Exception as error:
			self._publish_error("screen_wake", error)
			return False
	def _sleep_screen(self, automatic=False, timeout_seconds=None):
		if self.device is None or self.device.screen_sleeping:
			return False
		self._stop_sleep_timer()
		try:
			self.device.screen_off()
			self.state.update_screen_sleeping(True)
			return True
		except Exception as error:
			self._publish_error("screen_sleep", error)
			self._mark_activity()
			self._schedule_sleep()
			return False
	def _handle_screen_key(self, action):
		now = utime.ticks_ms()
		self._mark_activity(now)
		if action == KEY_SCREEN_PRESS:
			self._screen_key_pressed_at = now
			if not self.state.get_snapshot()[1]["screen_sleeping"]:
				self._schedule_sleep()
			return True
		pressed_at = self._screen_key_pressed_at
		self._screen_key_pressed_at = None
		if pressed_at is None:
			return False
		held_ms = max(0, utime.ticks_diff(now, pressed_at))
		if held_ms > SCREEN_KEY_MAX_PRESS_MS:
			if not self.state.get_snapshot()[1]["screen_sleeping"]:
				self._schedule_sleep()
			return False
		if self.state.get_snapshot()[1]["screen_sleeping"]:
			return self._wake_screen()
		return self._sleep_screen(False)
	def _stop_lamp_key_timer(self):
		if self._lamp_key_timer is None:
			return
		try:
			self._lamp_key_timer.stop()
		except Exception as error:
			self._publish_error("lamp_key_timer_stop", error)
	def _on_lamp_key_timer(self, timer):
		self.commands.signal_lamp_key_long_isr()
	def _start_lamp_key_timer(self):
		self._stop_lamp_key_timer()
		try:
			if self._lamp_key_timer is None:
				self._lamp_key_timer = Timer(Timer.Timer0)
			self._lamp_key_timer.start(
				period=LAMP_KEY_LONG_PRESS_MS,
				mode=self._lamp_key_timer.ONE_SHOT,
				callback=self._on_lamp_key_timer)
			return True
		except Exception as error:
			self._publish_error("lamp_key_timer_start", error)
			return False
	def _toggle_lamp(self, source):
		if self.lamp is None:
			return False
		before = "unknown"
		try:
			before = self.lamp.get_mode()
			after = self.lamp.cycle_mode()
			self._refresh_lamp()
			self.messages.publish(MSG_DOUBLE_CLICK_EVENT)
			return True
		except Exception as error:
			self._publish_error("lamp_key" if source == "GPIO28短按"
				else "double_click_lamp", error)
			return False
	def _set_double_click_enabled(self, enabled, notice=False):
		if self.motion_sensor is None:
			raise RuntimeError("加速度计服务未接入")
		enabled = bool(enabled)
		self.motion_sensor.set_double_click_interrupt_enabled(enabled)
		self._last_double_click_ms = None
		self.state.update_motion(enabled)
		saved = (self.persistent_config is not None and
			self.persistent_config.defer_settings_update(
				double_click_enabled=enabled))
		if not saved:
			if notice:
				self.state.publish_key_notice(
					"双击模式保存失败", False, KEY_NOTICE_DURATION_MS)
		else:
			if notice:
				self.state.publish_key_notice(
					"双击模式开启" if enabled else "双击模式关闭",
					True, KEY_NOTICE_DURATION_MS)
		return enabled
	def _fire_lamp_key_long(self):
		if self._lamp_key_pressed_at is None or self._lamp_key_long_fired:
			return False
		self._lamp_key_long_fired = True
		self._stop_lamp_key_timer()
		try:
			if self.state.get_snapshot()[1]["screen_sleeping"]:
				self._wake_screen("GPIO28长按")
			current = self.state.get_snapshot()[1]["double_click_enabled"]
			self._set_double_click_enabled(not current, notice=True)
			return True
		except Exception as error:
			self.state.publish_key_notice(
				"双击模式切换失败", False, KEY_NOTICE_DURATION_MS)
			self._publish_error("lamp_key_double_click", error)
			return False
	def _handle_lamp_key_long(self):
		if self._lamp_key_pressed_at is None or self._lamp_key_long_fired:
			return False
		if self.hardware_keys is None or not self.hardware_keys.lamp_pressed():
			return False
		return self._fire_lamp_key_long()
	def _handle_lamp_key(self, action):
		now = utime.ticks_ms()
		if action == KEY_LAMP_PRESS:
			if self._lamp_key_pressed_at is not None:
				return False
			self._lamp_key_pressed_at = now
			self._lamp_key_long_fired = False
			self._start_lamp_key_timer()
			return True
		pressed_at = self._lamp_key_pressed_at
		if pressed_at is None:
			return False
		held_ms = max(0, utime.ticks_diff(now, pressed_at))
		self._stop_lamp_key_timer()
		if not self._lamp_key_long_fired:
			if held_ms >= LAMP_KEY_LONG_PRESS_MS:
				self._fire_lamp_key_long()
			else:
				self._toggle_lamp("GPIO28短按")
		self._lamp_key_pressed_at = None
		self._lamp_key_long_fired = False
		return True
	@staticmethod
	def _intercom_busy(snapshot):
		# disconnected只是通信链路状态，不是麦权或对讲业务。这里明确
		# 枚举真正会占用对讲流程的状态，避免新增的非业务状态误阻止息屏。
		floor_busy = snapshot.get("floor_state") in (
			"requesting_floor", "transmitting", "releasing_floor")
		single_busy = snapshot.get("single_state") in (
			"inviting", "invite_accepted", "active", "dissolving")
		return bool(snapshot.get("ota_busy") or
			snapshot["audio_tx_active"] or
			snapshot["audio_rx_active"] or
			floor_busy or single_busy)
	def _battery_dynamic_load(self, snapshot):
		now_ms = utime.ticks_ms()
		signature = (snapshot.get("lamp_mode"),
			bool(snapshot.get("screen_sleeping")))
		if (self._battery_load_signature is not None and
			signature != self._battery_load_signature):
			self._battery_recovery_until_ms = utime.ticks_add(
				now_ms, BATTERY_LOAD_RECOVERY_MS)
		self._battery_load_signature = signature
		audio_active = bool(snapshot.get("audio_tx_active") or
			snapshot.get("audio_rx_active"))
		if audio_active:
			self._battery_recovery_until_ms = utime.ticks_add(
				now_ms, BATTERY_LOAD_RECOVERY_MS)
		return bool(audio_active or
			(self._battery_recovery_until_ms is not None and
			 utime.ticks_diff(self._battery_recovery_until_ms, now_ms) > 0))
	def _handle_key_action(self, action):
		if action in (KEY_SCREEN_PRESS, KEY_SCREEN_RELEASE):
			return self._handle_screen_key(action)
		_revision, snapshot = self.state.get_snapshot()
		# 所有按键都清零息屏计时。黑屏时PTT按下先恢复亮屏，再照常
		# 转交通信会话；其他功能键维持原有过滤规则。
		self._mark_activity()
		if snapshot["screen_sleeping"]:
			if action in (KEY_LAMP_PRESS, KEY_LAMP_RELEASE):
				return self._handle_lamp_key(action)
			if action != KEY_PTT_PRESS:
				return
			self._wake_screen("PTT按下")
		self._schedule_sleep()
		if action in (KEY_LAMP_PRESS, KEY_LAMP_RELEASE):
			return self._handle_lamp_key(action)
		if self.key_action_callback is None:
			return
		if action in FUNCTION_KEY_ACTIONS:
			self.key_action_callback(action)
	def _handle_hardware_key_irq(self):
		if self.hardware_keys is None:
			return
		try:
			utime.sleep_ms(self.hardware_keys.debounce_ms)
			for action in self.hardware_keys.poll_pressed_edges():
				self._handle_key_action(action)
		except Exception as error:
			self._publish_error("hardware_key_read", error)
	def _on_sleep_timer(self, timer):
		self.commands.signal_screen_timeout_isr()
	def _stop_sleep_timer(self):
		if self._sleep_timer is not None:
			try:
				self._sleep_timer.stop()
			except Exception as error:
				self._publish_error("screen_timer_stop", error)
		self._sleep_timer_armed = False
	def _schedule_sleep(self):
		if self.device is None:
			return
		_revision, snapshot = self.state.get_snapshot()
		seconds = snapshot["sleep_timeout_seconds"]
		eligible = bool(snapshot.get("business_page_active"))
		if not eligible:
			self._sleep_eligible = False
		if seconds is None or not eligible or snapshot["screen_sleeping"]:
			self._stop_sleep_timer()
			return
		if not self._sleep_eligible:
			self._sleep_eligible = True
			now = utime.ticks_ms()
			self._last_activity_ms = now
			self._idle_gate_until_ms = utime.ticks_add(now, SLEEP_IDLE_GRACE_MS)
		now = utime.ticks_ms()
		gate = self._idle_gate_until_ms
		if gate is None:
			gate = utime.ticks_add(self._last_activity_ms, SLEEP_IDLE_GRACE_MS)
		remaining = (utime.ticks_diff(gate, now) +
			int(seconds) * 1000)
		try:
			if self._sleep_timer is None:
				self._sleep_timer = Timer(Timer.Timer3)
			self._sleep_timer.start(period=max(1, remaining), mode=self._sleep_timer.ONE_SHOT,
				callback=self._on_sleep_timer)
			self._sleep_timer_armed = True
		except Exception as error:
			self._sleep_timer_armed = False
			self._publish_error("screen_timer_start", error)
	def _check_sleep_timeout(self):
		self._sleep_timer_armed = False
		_revision, snapshot = self.state.get_snapshot()
		seconds = snapshot["sleep_timeout_seconds"]
		if (self.device is None or seconds is None or snapshot["screen_sleeping"] or
				not snapshot.get("business_page_active")):
			self._schedule_sleep()
			return
		now = utime.ticks_ms()
		busy = (self.commands.is_ui_busy() or
			self._intercom_busy(snapshot))
		if busy:
			self._last_activity_ms = now
			self._idle_gate_until_ms = utime.ticks_add(now, SLEEP_IDLE_GRACE_MS)
			self._last_busy_state = True
		elif self._last_busy_state:
			self._last_busy_state = False
			self._last_activity_ms = now
			self._idle_gate_until_ms = utime.ticks_add(now, SLEEP_IDLE_GRACE_MS)
		gate = self._idle_gate_until_ms
		if (gate is None or
				utime.ticks_diff(now, gate) < int(seconds) * 1000):
			self._schedule_sleep()
			return
		self._sleep_screen(True, seconds)
	def _service_sleep_deadline(self):
		_revision, snapshot = self.state.get_snapshot()
		seconds = snapshot["sleep_timeout_seconds"]
		if (self.device is None or seconds is None or
				snapshot["screen_sleeping"] or
				not snapshot.get("business_page_active")):
			if self._sleep_timer_armed:
				self._schedule_sleep()
			return
		if not self._sleep_eligible:
			self._schedule_sleep()
			return
		now = utime.ticks_ms()
		busy = (self.commands.is_ui_busy() or
			self._intercom_busy(snapshot))
		if busy:
			self._last_activity_ms = now
			self._idle_gate_until_ms = utime.ticks_add(now, SLEEP_IDLE_GRACE_MS)
			self._last_busy_state = True
			self._schedule_sleep()
			return
		if self._last_busy_state:
			self._last_busy_state = False
			self._last_activity_ms = now
			self._idle_gate_until_ms = utime.ticks_add(now, SLEEP_IDLE_GRACE_MS)
		gate = self._idle_gate_until_ms
		if gate is not None and utime.ticks_diff(now, gate) >= 0:
			idle_ms = utime.ticks_diff(now, gate)
		else:
			idle_ms = -1
		if idle_ms >= int(seconds) * 1000:
			self._check_sleep_timeout()
		elif not self._sleep_timer_armed:
			self._schedule_sleep()
	def _refresh_versions(self):
		if self.device is None:
			return
		try:
			software, hardware, firmware = self.device.read_versions()
			self.state.update_device_versions(
				software, hardware, firmware, self.device.device_id)
		except Exception as error:
			self._publish_error("device_info", error)
	def _initialize_gnss(self):
		if self.device is None or not hasattr(self.device, "initialize_gnss"):
			return False
		was_initialized = bool(getattr(self.device, "gnss_initialized", False))
		try:
			initialized = bool(self.device.initialize_gnss())
			if initialized:
				self._last_gnss_init_error = None
				if (self._gnss_sampling_enabled and not was_initialized and
						self._gnss_phase != "fast"):
					self._start_gnss_fast_sampling("GNSS初始化成功")
			return initialized
		except Exception as error:
			text = str(error)
			if text != self._last_gnss_init_error:
				print("[定位] GNSS初始化失败，10秒后重试：{}".format(text))
				self._last_gnss_init_error = text
			return False
	@staticmethod
	def _location_distance_m(first, second):
		radius_m = 6371000.0
		lat1 = math.radians(first["latitude"])
		lon1 = math.radians(first["longitude"])
		lat2 = math.radians(second["latitude"])
		lon2 = math.radians(second["longitude"])
		dlat = lat2 - lat1
		dlon = lon2 - lon1
		value = (math.sin(dlat / 2.0) ** 2 +
			math.cos(lat1) * math.cos(lat2) *
			math.sin(dlon / 2.0) ** 2)
		value = min(1.0, max(0.0, value))
		return radius_m * 2.0 * math.atan2(
			math.sqrt(value), math.sqrt(1.0 - value))
	def _gnss_busy(self, snapshot):
		return bool(self.commands.is_ui_busy() or self._intercom_busy(snapshot))
	def _notify_gnss_result(self, result):
		if self.gnss_result_callback is None:
			return True
		try:
			return bool(self.gnss_result_callback(dict(result)))
		except Exception as error:
			self._publish_error("gnss_result", error)
			return False
	def _report_gnss_result(self, result):
		if self._notify_gnss_result(result):
			return True
		# 通信业务队列暂满时不丢失上报资格；保留到期标记，在下次
		# Worker唤醒后重新采样并投递最新结果。
		self._gnss_force_report = True
		self._gnss_overdue = True
		return False
	def _refresh_location_clock(self):
		_revision, snapshot = self.state.get_snapshot()
		try:
			local = utime.localtime()
			time_text = "{:02d}:{:02d}:{:02d}".format(local[3], local[4], local[5])
			weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[local[6]]
			self.state.update_location(snapshot["location_name"], snapshot["latitude"],
				snapshot["longitude"], snapshot["altitude"], snapshot["satellites"],
				time_text, weekday)
		except Exception:
			pass
	def _service_gnss_deadline(self):
		if not self._initialize_gnss():
			# 初始化未完成时只等待下次初始化，不把这次等待算入快速采集次数。
			self._refresh_location_clock()
			return False
		self._refresh_location_clock()
		if not self._gnss_sampling_enabled:
			return False
		now = utime.ticks_ms()
		due = (self._gnss_overdue or self._gnss_next_sample_at is None or
			utime.ticks_diff(now, self._gnss_next_sample_at) >= 0)
		if not due:
			return False
		_revision, snapshot = self.state.get_snapshot()
		if self._gnss_busy(snapshot):
			self._gnss_overdue = True
			return False
		return self._refresh_gnss()
	def _refresh_gnss(self):
		if self.device is None:
			return False
		_revision, snapshot = self.state.get_snapshot()
		time_text = snapshot["location_time"]
		weekday = snapshot["location_weekday"]
		try:
			local = utime.localtime()
			time_text = "{:02d}:{:02d}:{:02d}".format(local[3], local[4], local[5])
			weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[local[6]]
			if self._gnss_busy(snapshot):
				self._gnss_overdue = True
				self.state.update_location(snapshot["location_name"], snapshot["latitude"],
					snapshot["longitude"], snapshot["altitude"], snapshot["satellites"], time_text, weekday)
				return False
			now = utime.ticks_ms()
			self._gnss_overdue = False
			if self._gnss_phase == "fast":
				self._gnss_fast_attempts += 1
				self._gnss_next_sample_at = utime.ticks_add(
					now, GNSS_FAST_SAMPLE_INTERVAL_MS)
			else:
				self._gnss_next_sample_at = utime.ticks_add(
					now, self.gnss_interval_ms)
			location = self.device.read_location()
			force_report = self._gnss_force_report
			self._gnss_force_report = False
			accepted = False
			if location["fix"]:
				if self._gnss_phase == "fast":
					self._finish_gnss_fast_sampling("已获得有效定位")
				previous = self._last_valid_location
				if previous is None:
					accepted = True
				else:
					distance = self._location_distance_m(previous, location)
					altitude_delta = abs(float(location["altitude"]) -
						float(previous["altitude"]))
					accepted = (distance > self.gnss_change_threshold_m or
						altitude_delta > self.gnss_change_threshold_m)
				if accepted:
					self._last_valid_location = dict(location)
				display = self._last_valid_location or location
				self.state.update_gnss_location(location["state"], True,
					display["latitude"], display["longitude"], display["altitude"],
					display["satellites"], time_text, weekday,
					display.get("latitude_text"), display.get("longitude_text"))
				if accepted or force_report:
					self._report_gnss_result(location)
			else:
				if (self._gnss_phase == "fast" and
						self._gnss_fast_attempts >= GNSS_FAST_SAMPLE_MAX_ATTEMPTS):
					self._finish_gnss_fast_sampling("达到{}次仍未获得有效定位".format(
						GNSS_FAST_SAMPLE_MAX_ATTEMPTS))
				retained = self._last_valid_location
				self.state.update_gnss_location(location["state"], False,
					None if retained is None else retained["latitude"],
					None if retained is None else retained["longitude"],
					None if retained is None else retained["altitude"],
					(location.get("satellites") if retained is None else
					 retained["satellites"]), time_text, weekday,
					None if retained is None else retained.get("latitude_text"),
					None if retained is None else retained.get("longitude_text"))
				if force_report:
					if retained is None:
						self._report_gnss_result(location)
					else:
						self._report_gnss_result(retained)
			status = (location["state"], location["fix"], location["latitude"],
				location["longitude"], location["altitude"], location["satellites"])
			if status != self._last_gnss_status:
				self._last_gnss_status = status
			return True
		except Exception as error:
			now = utime.ticks_ms()
			self._gnss_overdue = False
			if self._gnss_phase == "fast":
				self._gnss_fast_attempts += 1
				if self._gnss_fast_attempts >= GNSS_FAST_SAMPLE_MAX_ATTEMPTS:
					self._finish_gnss_fast_sampling("采集异常达到快速阶段上限")
				else:
					self._gnss_next_sample_at = utime.ticks_add(
						now, GNSS_FAST_SAMPLE_INTERVAL_MS)
			else:
				self._gnss_next_sample_at = utime.ticks_add(now, self.gnss_interval_ms)
			force_report = self._gnss_force_report
			self._gnss_force_report = False
			retained = self._last_valid_location
			self.state.update_gnss_location("error", False,
				None if retained is None else retained["latitude"],
				None if retained is None else retained["longitude"],
				None if retained is None else retained["altitude"],
				None if retained is None else retained["satellites"],
				time_text, weekday,
				None if retained is None else retained.get("latitude_text"),
				None if retained is None else retained.get("longitude_text"))
			if force_report:
				if retained is None:
					self._report_gnss_result({"state": "error", "fix": False,
						"latitude": None, "longitude": None, "altitude": None,
						"satellites": None, "raw_gga": b""})
				else:
					print("[定位] GNSS读取异常，首次上报沿用最后有效GGA")
					self._report_gnss_result(retained)
			if self._last_gnss_status != "error":
				self._publish_error("gnss", error)
				self._last_gnss_status = "error"
			return True
	def notify_double_click(self):
		return self.commands.signal_double_click_isr()
	def _on_periodic_timer(self, timer):
		self.commands.signal_periodic_isr()
	def _start_periodic_timer(self):
		self._timer = Timer(Timer.Timer2)
		self._timer.start(period=PERIODIC_INTERVAL_MS,
						  mode=self._timer.PERIODIC,
						  callback=self._on_periodic_timer)
	def _publish_error(self, domain, error):
		text = str(error)
		self.state.set_error(domain, text)
		self.messages.publish(MSG_ERROR, {"domain": domain, "text": text}, 1)
		if domain in (
				"screen_wake", "screen_sleep", "screen_brightness",
				"lamp", "lamp_key", "lamp_key_timer_stop",
				"lamp_key_timer_start", "lamp_key_double_click",
				"lamp_brightness", "double_click", "hardware_key_read",
				"hardware_key_start", "battery", "gnss", "gnss_result"):
			print("[硬件] {}失败：{}".format(domain, text))
	def _refresh_battery(self):
		if self.battery is None:
			return
		try:
			self.battery.tick()
			if self.battery.get_percent() is not None:
				changed = self.state.update_battery(
					self.battery.get_percent(),
					self.battery.get_voltage_mv(),
					self.battery.charging_state,
					self.battery.is_full_confirmed())
				if changed:
					self.messages.publish(MSG_BATTERY_CHANGED)
		except Exception as error:
			self._publish_error("battery", error)
	def _bootstrap_battery(self):
		if self.battery is None:
			return
		attempts = 0
		while (self.running and not self.battery.is_ready() and
			   attempts < BATTERY_BOOT_MAX_ATTEMPTS):
			attempts += 1
			self._refresh_battery()
			if not self.battery.is_ready():
				utime.sleep_ms(BATTERY_BOOT_SAMPLE_INTERVAL_MS)
	def _refresh_network(self, force_signal=False, log_signal=False):
		if self.network is None:
			return
		try:
			snapshot = self.network.refresh_once(force_signal=force_signal)
			sn = getattr(self.device, "device_id", "--")
			iccid = snapshot.get("iccid")
			if not self._device_identity_logged:
				print("[设备统计] SN={}, ICCID={}".format(
					sn, iccid or "未识别到"))
				self._device_identity_logged = True
			changed = self.state.update_network(
				snapshot["network_connected"], snapshot["network_stage"],
				snapshot["network_state"], snapshot["iccid"],
				snapshot["signal_dbm"], snapshot["signal_level"])
			if changed:
				self.messages.publish(MSG_NETWORK_CHANGED)
		except Exception as error:
			self._publish_error("network", error)
	def _refresh_lamp(self):
		if self.lamp is None:
			return
		try:
			snapshot = self.lamp.get_status_snapshot()
			brightness = self.lamp.get_brightness_percent()
			changed = self.state.update_lamp(
				snapshot["mode"], brightness,
				snapshot["fall_alarm_active"])
			if changed:
				self.messages.publish(MSG_LAMP_CHANGED)
		except Exception as error:
			self._publish_error("lamp", error)
	def _defer_setting(self, key, value):
		self._pending_persistent_settings[key] = value
		if self.persistent_config is None:
			return False
		return self.persistent_config.defer_settings_update(**{key: value})
	def _flush_deferred_settings(self):
		if self.persistent_config is None:
			return
		result = self.persistent_config.flush_deferred_settings()
		if result is True:
			self._pending_persistent_settings = {}
	def _execute(self, item):
		command_type = item["type"]
		payload = item["payload"]
		try:
			if command_type == CMD_SET_LAMP_MODE:
				if self.lamp is None:
					return
				self.lamp.set_mode(payload)
				self._refresh_lamp()
			elif command_type == CMD_SET_LAMP_BRIGHTNESS:
				if self.lamp is None:
					return
				self.lamp.set_brightness_percent(payload)
				self._refresh_lamp()
				self._defer_setting("lamp_brightness", int(payload))
			elif command_type == CMD_COMMIT_LAMP_BRIGHTNESS:
				pass
			elif command_type == CMD_SET_DOUBLE_CLICK_ENABLED:
				self._set_double_click_enabled(payload)
			elif command_type == CMD_SET_SCREEN_BRIGHTNESS:
				if self.device is None:
					raise RuntimeError("背光服务未接入")
				value = self.device.set_screen_brightness(payload)
				self.state.update_screen_brightness(value)
				self._defer_setting("screen_brightness", value)
			elif command_type == CMD_COMMIT_SCREEN_BRIGHTNESS:
				pass
			elif command_type == CMD_SET_SLEEP_TIMEOUT:
				self.state.update_sleep_timeout(payload)
				self._last_activity_ms = utime.ticks_ms()
				self._schedule_sleep()
			elif command_type == CMD_WAKE_SCREEN:
				self._wake_screen(payload or "对讲事件")
			elif command_type == CMD_CHANGE_VOLUME:
				pass
			elif command_type == CMD_REFRESH_NETWORK:
				self._refresh_network(force_signal=True, log_signal=True)
			elif command_type == CMD_REFRESH_GNSS:
				if not self._gnss_sampling_enabled:
					self._gnss_sampling_enabled = True
					self._gnss_force_report = True
					self._gnss_overdue = True
				self._service_gnss_deadline()
			elif command_type == CMD_DOUBLE_CLICK:
				self._toggle_lamp("双击命令")
			elif command_type == CMD_ACCEL_DOUBLE_CLICK_IRQ:
				self._handle_accel_double_click_irq()
		except Exception as error:
			self._publish_error(command_type, error)
			if command_type in (CMD_SET_LAMP_MODE, CMD_SET_LAMP_BRIGHTNESS):
				if command_type == CMD_SET_LAMP_BRIGHTNESS:
					self._pending_persistent_settings.pop("lamp_brightness", None)
				self._refresh_lamp()
			elif command_type == CMD_SET_SCREEN_BRIGHTNESS and self.device is not None:
				self._pending_persistent_settings.pop("screen_brightness", None)
				self.state.update_screen_brightness(self.device.brightness)
	def _handle_accel_double_click_irq(self):
		print("[加速度] 双击中断")
		if self.motion_sensor is None:
			return
		try:
			source = self.motion_sensor.read_click_source()
		except Exception as error:
			self._publish_error("double_click", error)
			return
		# 即使已关闭，也先读取 SRC 释放切换期间可能到达的锁存中断。
		if not self.state.get_snapshot()[1]["double_click_enabled"]:
			return
		if (int(source) & 0x0F) != 2:
			return
		now_ms = utime.ticks_ms()
		if (self._last_double_click_ms is not None and
				utime.ticks_diff(now_ms, self._last_double_click_ms) < 250):
			return
		self._last_double_click_ms = now_ms
		if self.lamp is None:
			return
		self._toggle_lamp("加速度双击")
	def _run(self):
		try:
			if self.hardware_keys is not None:
				try:
					self.hardware_keys.start()
				except Exception as error:
					self._publish_error("hardware_key_start", error)
			if self.device is not None:
				try:
					brightness = self.state.get_snapshot()[1]["screen_brightness"]
					if (self.device.brightness != brightness or
							not getattr(self.device,
									"_screen_backlight_enabled", False)):
						self.device.set_screen_brightness(brightness)
				except Exception as error:
					self._publish_error("screen_brightness", error)
			if self.lamp is not None:
				try:
					value = self.state.get_snapshot()[1]["lamp_brightness"]
					if self.lamp.get_brightness_percent() != value:
						self.lamp.set_brightness_percent(value)
				except Exception as error:
					self._publish_error("lamp_brightness", error)
			self._initialize_gnss()
			self._refresh_versions()
			self._bootstrap_battery()
			if not self.running:
				return
			self._refresh_network(force_signal=True)
			self._refresh_lamp()
			self._start_periodic_timer()
			while self.running:
				self.commands.wait()
				events = self.commands.take_events()
				(stop_pending, double_click_pending, periodic_pending,
				 _, _, _,
				 items, screen_timeout_pending, hardware_key_pending,
				 lamp_key_long_pending) = events
				if stop_pending or not self.running:
					break
				if double_click_pending:
					self._handle_accel_double_click_irq()
				if hardware_key_pending:
					self._handle_hardware_key_irq()
				if lamp_key_long_pending:
					self._handle_lamp_key_long()
				if periodic_pending:
					self._periodic_count += 1
					self._flush_deferred_settings()
					if self.battery is not None:
						snapshot = self.state.get_snapshot()[1]
						self.battery.set_dynamic_load(
							self._battery_dynamic_load(snapshot))
					self._refresh_battery()
					if self._periodic_count >= NETWORK_REFRESH_PERIODS:
						self._periodic_count = 0
						self._refresh_network(force_signal=True)
					self._service_gnss_deadline()
					self._service_sleep_deadline()
				for item in items:
					self._execute(item)
				if screen_timeout_pending:
					self._check_sleep_timeout()
		except Exception as error:
			self._publish_error("worker", error)
		finally:
			if self.hardware_keys is not None:
				self.hardware_keys.stop()
			self._stop_sleep_timer()
			self._stop_lamp_key_timer()
			if self._timer is not None:
				try:
					self._timer.stop()
				except Exception:
					pass
				self._timer = None
			self.running = False
			self.thread_id = None
