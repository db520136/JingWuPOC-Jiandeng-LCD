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
	KEY_PTT_RELEASE,
	KEY_VOLUME_DOWN, KEY_VOLUME_UP,
	KEY_LAMP_PRESS, KEY_LAMP_RELEASE, KEY_SCREEN_PRESS,
	KEY_SCREEN_RELEASE)
PERIODIC_INTERVAL_MS = 1000
NETWORK_REFRESH_PERIODS = 5
BATTERY_LOAD_RECOVERY_MS = 3000
SLEEP_IDLE_GRACE_MS = 3000
BATTERY_BOOT_SAMPLE_INTERVAL_MS = 20
BATTERY_BOOT_MAX_ATTEMPTS = 60
SCREEN_KEY_MAX_PRESS_MS = 1500
KEY_NOTICE_DURATION_MS = 2000
GNSS_FAST_SAMPLE_INTERVAL_MS = 5000
GNSS_FAST_SAMPLE_MAX_ATTEMPTS = 10
CHECKIN_SAMPLE_INTERVAL_MS = 10000
CHECKIN_MAX_ATTEMPTS = 4
LAMP_KEY_LONG_PRESS_MS = 1500
LAMP_KEY_CHECK_INTERVAL_MS = 100
KEY_RELEASE_DEBUG = False
KEY_SCHED_DIAGNOSTIC = False
class GnssWorker:
	def __init__(self, device, state, result_callback, error_callback,
				 interval_ms=30000, change_threshold_m=5.0):
		self.device = device
		self.state = state
		self.result_callback = result_callback
		self.error_callback = error_callback
		self.interval_ms = interval_ms
		self.change_threshold_m = change_threshold_m
		self.events = UICommandQueue()
		self.running = False
		self.thread_id = None
		self._next_due = utime.ticks_ms()
		self._init_due = self._next_due
		self._auto_enabled = False
		self._auto_generation = None
		self._auto_due = None
		self._auto_waiting = False
		self._auto_fast = False
		self._auto_attempts = 0
		self._auto_force = False
		self._last_valid_location = None
		self._manual = None
		self._manual_due = None
		self._manual_attempts = 0
		self._completed = None
		self._last_init_error = None
	def start(self):
		if self.running:
			return False
		self.running = True
		try:
			self.thread_id = _thread.start_new_thread(self._run, ())
		except Exception:
			self.running = False
			raise
		self.events.signal_periodic_isr()
		return True
	def stop(self):
		self.running = False
		self.events.signal_stop()
	def set_ui_busy(self, busy):
		self.events.set_ui_busy(busy)
	def request_auto(self, force=False, generation=None):
		return self.events.put_latest("auto_force" if force else "auto_check",
			generation, defer_if_busy=True)
	def request_manual(self, ticket):
		if not self.running:
			return False
		return self.events.put("manual", ticket, defer_if_busy=True)
	def resume_auto_if_idle(self):
		if self._auto_waiting and not LowPriorityWorker._intercom_busy(self.state.get_snapshot()[1]):
			self.request_auto(False, self._auto_generation)
	def notify_deadline(self):
		deadline = self._next_due
		if (self.running and deadline is not None and
				utime.ticks_diff(utime.ticks_ms(), deadline) >= 0):
			self.events.signal_periodic_isr()
	def _initialize(self):
		was_ready = bool(getattr(self.device, "gnss_initialized", False))
		try:
			ready = bool(self.device is not None and self.device.initialize_gnss())
			if ready:
				self._init_due = None
				self._last_init_error = None
				if not was_ready and self._auto_enabled:
					self._start_fast()
				return True
		except Exception as error:
			if str(error) != self._last_init_error:
				self.error_callback("gnss", error)
				self._last_init_error = str(error)
		self._init_due = utime.ticks_add(utime.ticks_ms(), 10000)
		return False
	def _start_fast(self):
		self._auto_fast = True
		self._auto_attempts = 0
		self._auto_due = utime.ticks_ms()
		self._auto_waiting = False
	def _read_once(self):
		try:
			if self._initialize():
				location = self.device.read_location()
				if not getattr(self.device, "gnss_initialized", False):
					self._init_due = utime.ticks_add(utime.ticks_ms(), 10000)
				return location
		except Exception as error:
			self.error_callback("gnss", error)
		return {"state": "unavailable", "fix": False, "latitude": None,
			"longitude": None, "altitude": None, "satellites": None,
			"raw_gga": b""}
	@staticmethod
	def _log_sample(kind, location):
		source = "手动打卡" if kind == "manual" else "自动定时采集"
		altitude = location.get("altitude")
		altitude = "{}m".format(altitude) if altitude is not None else "--"
		gga = location.get("raw_gga", b"")
		if isinstance(gga, bytes):
			gga = gga.decode("ascii")
		print("[定位采集][{}] 状态={} 有效定位={} 纬度={} 经度={} 海拔={} 卫星数={} GGA={}".format(
			source, location.get("state"), location.get("fix"),
			location.get("latitude_text", location.get("latitude")),
			location.get("longitude_text", location.get("longitude")),
			altitude, location.get("satellites"), gga))
	@staticmethod
	def _distance(first, second):
		lat1 = math.radians(first["latitude"])
		lat2 = math.radians(second["latitude"])
		dlat = lat2 - lat1
		dlon = math.radians(second["longitude"] - first["longitude"])
		value = min(1.0, max(0.0, math.sin(dlat / 2.0) ** 2 +
			math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2))
		return 6371000.0 * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))
	def _display(self, location, retained=None):
		display = retained or location
		self.state.update_gnss_location(location["state"], location["fix"],
			display.get("latitude"), display.get("longitude"),
			display.get("altitude"), display.get("satellites"),
			display.get("latitude_text"), display.get("longitude_text"))
	def _report(self, location, ticket=None):
		result = dict(location)
		if ticket is None:
			result["session_generation"] = self._auto_generation
		else:
			result["checkin_ticket"] = ticket
		return self.result_callback is None or bool(self.result_callback(result))
	def _finish_sample(self, kind, context, location):
		now = utime.ticks_ms()
		if kind == "manual":
			if context.get("cancelled") or self._manual is not context:
				return
			if location["fix"] or self._manual_attempts >= CHECKIN_MAX_ATTEMPTS:
				if self._report(location, context):
					self._manual = None
					self._manual_due = None
				else:
					self._completed = (kind, context, location)
					self._manual_due = utime.ticks_add(now, 1000)
			else:
				self._manual_due = utime.ticks_add(now, CHECKIN_SAMPLE_INTERVAL_MS)
			return
		if context != self._auto_generation:
			return
		self._auto_attempts += 1
		if location["fix"] or self._auto_attempts >= GNSS_FAST_SAMPLE_MAX_ATTEMPTS:
			self._auto_fast = False
		self._auto_due = utime.ticks_add(now,
			GNSS_FAST_SAMPLE_INTERVAL_MS if self._auto_fast else self.interval_ms)
		accepted = False
		if location["fix"]:
			previous = self._last_valid_location
			accepted = (previous is None or
				self._distance(previous, location) > self.change_threshold_m or
				abs(location["altitude"] - previous["altitude"]) > self.change_threshold_m)
			if accepted:
				self._last_valid_location = dict(location)
		self._display(location, self._last_valid_location)
		if accepted or self._auto_force:
			report = location if location["fix"] else self._last_valid_location or location
			self._auto_force = not self._report(report)
			if self._auto_force:
				self._auto_due = utime.ticks_add(now, 1000)
	def _service(self):
		# 先保留周期事件再等待触摸释放，避免忙碌切换时丢失唤醒。
		if self.events.is_ui_busy():
			self.events.signal_periodic_isr()
			return
		if self._completed is not None:
			completed = self._completed
			self._completed = None
			self._finish_sample(*completed)
			return
		now = utime.ticks_ms()
		if self._manual is not None and self._manual.get("cancelled"):
			self._manual = None
			self._manual_due = None
		manual_due = (self._manual is not None and self._manual_due is not None and
			utime.ticks_diff(now, self._manual_due) >= 0)
		auto_due = (self._auto_enabled and self._auto_due is not None and
			utime.ticks_diff(now, self._auto_due) >= 0)
		if auto_due and LowPriorityWorker._intercom_busy(self.state.get_snapshot()[1]):
			self._auto_waiting = True
			auto_due = False
		if manual_due:
			kind, context = "manual", self._manual
			self._manual_attempts += 1
		elif auto_due:
			if not self._initialize():
				self._auto_due = self._init_due
				return
			kind, context = "auto", self._auto_generation
		elif self._init_due is not None and utime.ticks_diff(now, self._init_due) >= 0:
			self._initialize()
			return
		else:
			return
		location = self._read_once()
		self._log_sample(kind, location)
		if self.events.is_ui_busy():
			self._completed = (kind, context, location)
			self.events.signal_periodic_isr()
		else:
			self._finish_sample(kind, context, location)
	def _plan(self):
		now = utime.ticks_ms()
		deadlines = [value for value in (self._init_due, self._manual_due,
			None if self._auto_waiting else self._auto_due) if value is not None]
		self._next_due = min(deadlines,
			key=lambda value: utime.ticks_diff(value, now)) if deadlines else None
	def _run(self):
		try:
			while self.running:
				self.events.wait()
				events = self.events.take_events()
				if not self.running or events[0]:
					break
				for item in events[6]:
					if item["type"] in ("auto_force", "auto_check"):
						generation = item["payload"]
						if (generation is not None and self._auto_generation is not None and
								generation < self._auto_generation):
							continue
						if generation != self._auto_generation:
							self._last_valid_location = None
							self._auto_generation = generation
							self._auto_force = True
						self._auto_enabled = True
						self._auto_waiting = False
						if item["type"] == "auto_force" or self._auto_due is None:
							self._start_fast()
							self._auto_force = True
					elif item["type"] == "manual" and not item["payload"].get("cancelled"):
						self._manual = item["payload"]
						self._manual_attempts = 0
						self._manual_due = utime.ticks_ms()
				self._service()
				self._plan()
		except Exception as error:
			self.error_callback("gnss", error)
			if self._manual is not None and not self._manual.get("cancelled"):
				self._report({"fix": False}, self._manual)
		finally:
			self.running = False
			self.thread_id = None
class LowPriorityWorker:
	def __init__(self, command_queue=None, state_store=None,
				 message_bus=None, battery=None, network=None, lamp=None,
				 motion_sensor=None, device=None, hardware_keys=None,
				 key_action_callback=None, gnss_result_callback=None,
				 checkin_request_callback=None,
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
		self.checkin_request_callback = checkin_request_callback
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
		self.gnss = GnssWorker(device, self.state, gnss_result_callback,
			self._publish_error, self.gnss_interval_ms, self.gnss_change_threshold_m)
		self.gnss.set_ui_busy(self.commands.is_ui_busy())
		self.commands.ui_busy_callback = self._on_ui_busy
		self._lamp_pressed_at = None
		self._lamp_long_fired = False
		self._lamp_release_observed_at = None
		self._screen_key_pressed_at = None
		self._key_stable_pressed = {}
		self._hardware_irq_wake_count = 0
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
		self.gnss.stop()
		self._stop_sleep_timer()
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
	def _on_ui_busy(self, busy):
		self.gnss.set_ui_busy(busy)
		if not busy and self.hardware_keys is not None:
			self.commands.signal_hardware_key_isr()
	def _diagnostic_key_summary(self):
		# 只观察计数，不清除通知、不读GPIO；回调可能同时更新这些值。
		signalled = getattr(self.commands, "_hardware_key_signal_count", 0)
		taken = getattr(self.commands, "_hardware_key_taken_count", 0)
		irq_count = getattr(self.hardware_keys, "_irq_count", 0)
		first_ms = getattr(self.hardware_keys, "_irq_first_ms", None)
		age = (None if first_ms is None else
			utime.ticks_diff(utime.ticks_ms(), first_ms))
		return "通知累计={} 已取={} 未取={} 未汇总回调={} 首回调已等={}ms".format(
			signalled, taken, signalled - taken, irq_count, age)
	def _diagnostic_call(self, stage, callback, *args, **kwargs):
		if not KEY_SCHED_DIAGNOSTIC:
			return callback(*args, **kwargs)
		before = self._diagnostic_key_summary()
		verbose = stage != "电池刷新"
		if verbose:
			print("[按键调度诊断][任务] 开始={} t={}ms {}".format(
				stage, utime.ticks_ms(), before))
		started = utime.ticks_ms()
		succeeded = False
		try:
			result = callback(*args, **kwargs)
			succeeded = True
			return result
		finally:
			finished = utime.ticks_ms()
			elapsed = utime.ticks_diff(finished, started)
			after = self._diagnostic_key_summary()
			# 电池正常每秒刷新，只输出慢调用或有按键积压/回调变化的调用。
			if (verbose or elapsed >= 100 or not succeeded or before != after or
					getattr(self.commands, "_hardware_key_signal_count", 0) !=
					getattr(self.commands, "_hardware_key_taken_count", 0)):
				print("[按键调度诊断][任务] 结束={} 正常返回={} 耗时={}ms 起止={}/{}ms 之前=[{}] 之后=[{}]".format(
					stage, succeeded, elapsed, started, finished, before, after))
	@staticmethod
	def _debug_key_release(message):
		if KEY_RELEASE_DEBUG:
			print("[按键Dump排查] {}".format(message))
	def request_gnss_sample(self, force=True, generation=None):
		return self.gnss.request_auto(force, generation)
	def request_manual_gnss(self, ticket):
		return self.gnss.request_manual(ticket)
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
	def _handle_screen_key(self, action, event_ms=None):
		now = utime.ticks_ms() if event_ms is None else event_ms
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
			self._publish_error("lamp_key" if source == "GPIO28短按松开"
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
	def _check_lamp_key_long(self):
		if self._lamp_pressed_at is None or self.commands.is_ui_busy():
			return False
		try:
			is_pressed = getattr(self.hardware_keys, "is_key_pressed", None)
			pressed = (bool(is_pressed("lamp")) if is_pressed is not None else
				bool(self._key_stable_pressed.get("lamp")))
		except Exception as error:
			self._publish_error("hardware_key_read", error)
			return False
		if not pressed:
			has_pending = getattr(self.hardware_keys, "has_pending_events", None)
			if has_pending is not None and has_pending():
				return False
			now = utime.ticks_ms()
			if self._lamp_release_observed_at is None:
				self._lamp_release_observed_at = now
				return False
			if utime.ticks_diff(now, self._lamp_release_observed_at) < LAMP_KEY_CHECK_INTERVAL_MS:
				return False
			# 首次观察到松开时的时间是实际释放时刻上界；仅在该上界仍小于长按阈值时补发短按。
			release_ms = self._lamp_release_observed_at
			held_upper_bound = max(0, utime.ticks_diff(
				release_ms, self._lamp_pressed_at))
			if (not self._lamp_long_fired and
				held_upper_bound < LAMP_KEY_LONG_PRESS_MS):
				self._key_stable_pressed["lamp"] = False
				return self._handle_lamp_key(KEY_LAMP_RELEASE, release_ms)
			self._lamp_pressed_at = None
			self._lamp_long_fired = False
			self._lamp_release_observed_at = None
			self._key_stable_pressed["lamp"] = False
			return False
		if (self._lamp_long_fired or
				utime.ticks_diff(utime.ticks_ms(), self._lamp_pressed_at) <
				LAMP_KEY_LONG_PRESS_MS):
			return False
		return self._fire_lamp_key_long()
	def _fire_lamp_key_long(self):
		if self._lamp_pressed_at is None or self._lamp_long_fired:
			return False
		self._debug_key_release("肩灯键长按打卡业务开始")
		self._lamp_long_fired = True
		accepted = (self.checkin_request_callback is not None and
			self.checkin_request_callback())
		if KEY_SCHED_DIAGNOSTIC:
			print("[按键调度诊断] 肩灯键长按打卡：请求接受={}".format(
				bool(accepted)))
		self._debug_key_release("肩灯键长按打卡请求是否接受={}".format(bool(accepted)))
		if accepted:
			self._wake_screen("手动打卡")
		self._debug_key_release("肩灯键长按打卡业务完成")
		return True
	def _handle_lamp_key(self, action, event_ms=None):
		now = utime.ticks_ms() if event_ms is None else event_ms
		if action == KEY_LAMP_PRESS:
			if self._lamp_pressed_at is None:
				self._lamp_pressed_at = now
				self._lamp_long_fired = False
				self._lamp_release_observed_at = None
			return True
		if self._lamp_pressed_at is None:
			return False
		held = max(0, utime.ticks_diff(now, self._lamp_pressed_at))
		if not self._lamp_long_fired:
			if held >= LAMP_KEY_LONG_PRESS_MS:
				self._fire_lamp_key_long()
			else:
				if KEY_SCHED_DIAGNOSTIC:
					print("[按键调度诊断] 肩灯键短按派发：held={}ms".format(held))
				self._toggle_lamp("GPIO28短按松开")
		self._lamp_pressed_at = None
		self._lamp_long_fired = False
		self._lamp_release_observed_at = None
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
	def _handle_key_action(self, action, event_ms=None):
		if action in (KEY_SCREEN_PRESS, KEY_SCREEN_RELEASE):
			return self._handle_screen_key(action, event_ms)
		_revision, snapshot = self.state.get_snapshot()
		# 所有按键都清零息屏计时。黑屏时PTT按下先恢复亮屏，再照常
		# 转交通信会话；其他功能键维持原有过滤规则。
		self._mark_activity()
		if snapshot["screen_sleeping"]:
			if action in (KEY_LAMP_PRESS, KEY_LAMP_RELEASE):
				return self._handle_lamp_key(action, event_ms)
			if action != KEY_PTT_PRESS:
				return
			self._wake_screen("PTT按下")
		self._schedule_sleep()
		if action in (KEY_LAMP_PRESS, KEY_LAMP_RELEASE):
			return self._handle_lamp_key(action, event_ms)
		if self.key_action_callback is None:
			return
		if action in FUNCTION_KEY_ACTIONS:
			self.key_action_callback(action)
	def _handle_hardware_key_irq(self):
		if self.hardware_keys is None:
			return
		if self.commands.is_ui_busy():
			return
		try:
			events = self.hardware_keys.take_key_events()
			batch_through_ms = self.hardware_keys.batch_through_ms
			overflow_reader = getattr(self.hardware_keys, "take_event_overflow", None)
			overflow = 0 if overflow_reader is None else overflow_reader()
			take_diagnostics = getattr(
				self.hardware_keys, "take_irq_diagnostics", None)
			if take_diagnostics is None:
				irq_count, irq_first_ms, irq_last_ms = 0, None, None
			else:
				irq_count, irq_first_ms, irq_last_ms = take_diagnostics()
			if overflow:
				read_states = getattr(self.hardware_keys, "read_key_states", None)
				if read_states is not None:
					batch_through_ms = utime.ticks_ms()
					events += tuple((key, pressed, batch_through_ms)
						for key, pressed in read_states().items())
			self._hardware_irq_wake_count += 1
			if KEY_SCHED_DIAGNOSTIC:
				read_ms = utime.ticks_ms()
				first_delay = (None if irq_first_ms is None else
					utime.ticks_diff(read_ms, irq_first_ms))
				last_delay = (None if irq_last_ms is None else
					utime.ticks_diff(read_ms, irq_last_ms))
				print("[按键调度诊断] 唤醒={} 本批中断数={} 事件数={} 队列溢出={} 首/末中断至识别={}ms/{}ms".format(
					self._hardware_irq_wake_count, irq_count, len(events), overflow,
					first_delay, last_delay))
			# ISR时间戳保留真实顺序，调度积压不会改变短按持续时间。
			events = sorted(events, key=lambda event: utime.ticks_diff(
				event[2], batch_through_ms))
			for key_name, pressed, event_ms in events:
				if KEY_SCHED_DIAGNOSTIC:
					print("[按键调度诊断] 边沿入处理：key={} pressed={} event_ms={} process_ms={}".format(
						key_name, pressed, event_ms, utime.ticks_ms()))
				self._confirm_key_edge(key_name, pressed, event_ms)
			self._check_lamp_key_long()
		except Exception as error:
			self._publish_error("hardware_key_read", error)
	def _confirm_key_edge(self, key_name, pressed, event_ms):
		self._key_stable_pressed[key_name] = pressed
		if KEY_SCHED_DIAGNOSTIC:
			print("[按键调度诊断] 边沿处理：key={} pressed={} event_ms={} process_ms={}".format(
				key_name, pressed, event_ms, utime.ticks_ms()))
		action = None
		if key_name == "volume_up":
			if pressed:
				action = KEY_VOLUME_UP
		elif key_name == "volume_down":
			if pressed:
				action = KEY_VOLUME_DOWN
		elif key_name == "ptt":
			action = KEY_PTT_PRESS if pressed else KEY_PTT_RELEASE
		elif key_name == "screen":
			action = KEY_SCREEN_PRESS if pressed else KEY_SCREEN_RELEASE
		elif key_name == "lamp":
			action = KEY_LAMP_PRESS if pressed else KEY_LAMP_RELEASE
		if action is not None:
			self._handle_key_action(action, event_ms)
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
		busy = (snapshot.get("checkin_phase") in ("locating", "sending") or self.commands.is_ui_busy() or
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
		busy = (snapshot.get("checkin_phase") in ("locating", "sending") or self.commands.is_ui_busy() or
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
	def _refresh_location_clock(self):
		try:
			local = utime.localtime()
			time_text = "{:02d}:{:02d}:{:02d}".format(local[3], local[4], local[5])
			weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[local[6]]
			self.state.update_location_clock(time_text, weekday)
		except Exception:
			pass
	def notify_double_click(self):
		return self.commands.signal_double_click_isr()
	def _on_periodic_timer(self, timer):
		self.commands.signal_periodic_isr()
		self.gnss.notify_deadline()
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
				"lamp", "lamp_key",
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
			self._diagnostic_call("电池刷新", self._refresh_battery)
			if not self.battery.is_ready():
				utime.sleep_ms(BATTERY_BOOT_SAMPLE_INTERVAL_MS)
	def _refresh_network(self, force_signal=False, log_signal=False):
		if self.network is None:
			return
		try:
			snapshot = self._diagnostic_call("网络刷新", self.network.refresh_once,
				force_signal=force_signal)
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
				self.request_gnss_sample()
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
			if KEY_SCHED_DIAGNOSTIC:
				print("[按键调度诊断][启动] 低优先级线程进入 t={}ms".format(
					utime.ticks_ms()))
			if self.hardware_keys is not None:
				try:
					self._diagnostic_call("启动/按键中断初始化", self.hardware_keys.start)
					read_states = getattr(self.hardware_keys,
						"initial_key_states", None)
					if read_states is not None:
						self._key_stable_pressed.update(read_states())
				except Exception as error:
					self._publish_error("hardware_key_start", error)
			if self.device is not None:
				try:
					brightness = self.state.get_snapshot()[1]["screen_brightness"]
					if (self.device.brightness != brightness or
							not getattr(self.device,
									"_screen_backlight_enabled", False)):
						self._diagnostic_call("启动/屏幕亮度", self.device.set_screen_brightness,
							brightness)
				except Exception as error:
					self._publish_error("screen_brightness", error)
			if self.lamp is not None:
				try:
					value = self.state.get_snapshot()[1]["lamp_brightness"]
					if self.lamp.get_brightness_percent() != value:
						self._diagnostic_call("启动/肩灯亮度", self.lamp.set_brightness_percent,
							value)
				except Exception as error:
					self._publish_error("lamp_brightness", error)
			try:
				self._diagnostic_call("启动/GNSS线程", self.gnss.start)
			except Exception as error:
				self._publish_error("gnss", error)
			self._diagnostic_call("启动/版本读取", self._refresh_versions)
			self._diagnostic_call("启动/电池首次采样", self._bootstrap_battery)
			if not self.running:
				return
			self._refresh_network(force_signal=True)
			self._diagnostic_call("启动/肩灯状态", self._refresh_lamp)
			self._diagnostic_call("启动/周期定时器", self._start_periodic_timer)
			if KEY_SCHED_DIAGNOSTIC:
				print("[按键调度诊断][启动] 开始处理事件 t={}ms {}".format(
					utime.ticks_ms(), self._diagnostic_key_summary()))
			while self.running:
				poll_key = (self._lamp_pressed_at is not None and
					not self.commands.is_ui_busy())
				if poll_key:
					utime.sleep_ms(LAMP_KEY_CHECK_INTERVAL_MS)
					if self.commands.try_wait():
						events = self.commands.take_events()
					else:
						events = (False, False, False, False, 0, 0, (), False, False)
				else:
					self.commands.wait()
					events = self.commands.take_events()
				(stop_pending, double_click_pending, periodic_pending,
				 _, _, _,
				 items, screen_timeout_pending, hardware_key_pending,
				) = events
				if stop_pending or not self.running:
					break
				if double_click_pending:
					self._handle_accel_double_click_irq()
				pending_key_reader = (None if self.hardware_keys is None else
					getattr(self.hardware_keys, "has_pending_events", None))
				queued_key_pending = (periodic_pending and
					pending_key_reader is not None and pending_key_reader())
				if hardware_key_pending or poll_key or queued_key_pending:
					self._handle_hardware_key_irq()
				if periodic_pending:
					self._periodic_count += 1
					self.gnss.resume_auto_if_idle()
					self._flush_deferred_settings()
					if self.battery is not None:
						snapshot = self.state.get_snapshot()[1]
						self.battery.set_dynamic_load(
							self._battery_dynamic_load(snapshot))
					self._diagnostic_call("电池刷新", self._refresh_battery)
					if self._periodic_count >= NETWORK_REFRESH_PERIODS:
						self._periodic_count = 0
						self._refresh_network(force_signal=True)
					self._refresh_location_clock()
					self._service_sleep_deadline()
				for item in items:
					self._execute(item)
				if screen_timeout_pending:
					self._check_sleep_timeout()
		except Exception as error:
			self._publish_error("worker", error)
		finally:
			self.gnss.stop()
			if self.hardware_keys is not None:
				self.hardware_keys.stop()
			self._stop_sleep_timer()
			if self._timer is not None:
				try:
					self._timer.stop()
				except Exception:
					pass
				self._timer = None
			self.running = False
			self.thread_id = None
