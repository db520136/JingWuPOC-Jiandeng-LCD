# -*- coding: utf-8 -*-
"""正式入口的统一通信业务服务。

本模块只保留一个通信业务线程。HTTP请求使用按需创建的一次性线程，结果
仍回到业务线程串行应用；TCP接收、UDP/RTP接收和统一发送由Runtime管理。
"""

import _thread
import utime

try:
	from machine import Timer
except ImportError:
	Timer = None

try:
	import checkNet
except ImportError:
	checkNet = None
try:
	import request
except ImportError:
	request = None
try:
	import usocket as socket
except ImportError:
	import socket

try:
	from usr.audio_port import AudioPort, play_forced_logout_tts
	from usr.comm_commands import (CMD_FORCE_LOGIN, CMD_JOIN_GROUP, CMD_LOGIN, CMD_LOGOUT,
									CMD_DISSOLVE_SINGLE_CALL,
									CMD_SINGLE_CALL, CMD_TCP_HEARTBEAT,
									RELEASE_RECOVERY_MAX_ATTEMPTS,
									SINGLE_CALL_FINAL_RESPONSE_TIMEOUT_MS,
									TCP_INITIAL_WAIT_MS, TCP_MAX_RETRIES,
									TCP_MAX_WAIT_MS, TCP_RETRY_STEP_MS)
	from usr.communication_queue import CommunicationCommandQueue
	from usr.communication_runtime import CommunicationRuntime
	from usr.communication_session import CommunicationSession
	from usr.poc_protocol import (MSG_FORCE_LOGIN, MSG_FORCE_LOGIN_ACK,
								  MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
								  MSG_LOGOUT, MSG_LOGOUT_ACK, MSG_INVITE_SINGLE,
								  MSG_INVITE_SINGLE_ACK,
								  MSG_SINGLE_INVITED, MSG_SINGLE_INVITED_ACK,
								  MSG_JOIN_GROUP, MSG_JOIN_GROUP_ACK, MSG_LOGIN,
								  MSG_LOGIN_ACK, build_force_login_payload, build_frame,
								  build_join_group_payload, build_login_payload,
								  build_logout_payload,
								  build_invite_single_payload,
								  build_single_invited_ack_payload,
								  build_heartbeat_payload,
								  group_id_to_uint32, parse_join_group_ack,
								  parse_force_login_ack, parse_login_ack, parse_logout_ack,
								  parse_invite_single_ack)
except ImportError:
	from audio_port import AudioPort, play_forced_logout_tts
	from comm_commands import (CMD_FORCE_LOGIN, CMD_JOIN_GROUP, CMD_LOGIN, CMD_LOGOUT,
								 CMD_DISSOLVE_SINGLE_CALL,
								 CMD_SINGLE_CALL, CMD_TCP_HEARTBEAT,
								 RELEASE_RECOVERY_MAX_ATTEMPTS,
								 SINGLE_CALL_FINAL_RESPONSE_TIMEOUT_MS,
								 TCP_INITIAL_WAIT_MS, TCP_MAX_RETRIES,
								 TCP_MAX_WAIT_MS, TCP_RETRY_STEP_MS)
	from communication_queue import CommunicationCommandQueue
	from communication_runtime import CommunicationRuntime
	from communication_session import CommunicationSession
	from poc_protocol import (MSG_FORCE_LOGIN, MSG_FORCE_LOGIN_ACK,
								 MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
								 MSG_LOGOUT, MSG_LOGOUT_ACK, MSG_INVITE_SINGLE,
								 MSG_INVITE_SINGLE_ACK,
								 MSG_SINGLE_INVITED, MSG_SINGLE_INVITED_ACK,
								 MSG_JOIN_GROUP, MSG_JOIN_GROUP_ACK, MSG_LOGIN,
								 MSG_LOGIN_ACK, build_force_login_payload, build_frame, build_join_group_payload,
								 build_logout_payload,
								 build_heartbeat_payload,
								 build_login_payload, group_id_to_uint32,
								 build_invite_single_payload,
								 build_single_invited_ack_payload,
								 parse_force_login_ack, parse_join_group_ack, parse_login_ack,
								 parse_logout_ack, parse_invite_single_ack)

try:
	from usr.poc_protocol import (MSG_DISSOLVE_SINGLE, MSG_DISSOLVE_SINGLE_ACK,
		MSG_FORCE_RELEASE_FLOOR, MSG_FLOOR_OCCUPIED, MSG_FLOOR_IDLE,
		build_dissolve_single_payload, build_dissolve_single_ack_payload,
		parse_dissolve_single_ack)
except ImportError:
	from poc_protocol import (MSG_DISSOLVE_SINGLE, MSG_DISSOLVE_SINGLE_ACK,
		MSG_FORCE_RELEASE_FLOOR, MSG_FLOOR_OCCUPIED, MSG_FLOOR_IDLE,
		build_dissolve_single_payload, build_dissolve_single_ack_payload,
		parse_dissolve_single_ack)

try:
	from usr.comm_commands import CMD_LEAVE_GROUP
	from usr.poc_protocol import (MSG_LEAVE_GROUP, MSG_LEAVE_GROUP_ACK,
		build_leave_group_payload, parse_leave_group_ack)
except ImportError:
	from comm_commands import CMD_LEAVE_GROUP
	from poc_protocol import (MSG_LEAVE_GROUP, MSG_LEAVE_GROUP_ACK,
		build_leave_group_payload, parse_leave_group_ack)

try:
	from usr.comm_commands import CMD_GNSS_UPLOAD
	from usr.poc_protocol import (MSG_GNSS_UPLOAD, MSG_GNSS_UPLOAD_ACK,
		build_gnss_upload_payload, parse_gnss_upload_ack)
except ImportError:
	from comm_commands import CMD_GNSS_UPLOAD
	from poc_protocol import (MSG_GNSS_UPLOAD, MSG_GNSS_UPLOAD_ACK,
		build_gnss_upload_payload, parse_gnss_upload_ack)

try:
	from usr.ui_messages import (MSG_ERROR, MSG_FLOOR_CHANGED, MSG_GROUP_CHANGED,
									 MSG_LOGIN_CHANGED, MSG_SINGLE_CALL)
	from usr.http_directory_worker import (HTTPDirectoryWorker,
		HTTP_REQUEST_TIMEOUT_S, fetch_directory)
	from usr.hardware_key_port import (FUNCTION_KEY_ACTIONS, KEY_VOLUME_DOWN,
		KEY_VOLUME_UP, KEY_PTT_PRESS, KEY_PTT_RELEASE)
except ImportError:
	from ui_messages import (MSG_ERROR, MSG_FLOOR_CHANGED, MSG_GROUP_CHANGED,
								 MSG_LOGIN_CHANGED, MSG_SINGLE_CALL)
	from http_directory_worker import (HTTPDirectoryWorker,
		HTTP_REQUEST_TIMEOUT_S, fetch_directory)
	from hardware_key_port import (FUNCTION_KEY_ACTIONS, KEY_VOLUME_DOWN,
		KEY_VOLUME_UP, KEY_PTT_PRESS, KEY_PTT_RELEASE)


STATE_LOGGED_IN = "logged_in"
STATE_JOINED = "joined"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_IDLE = "idle"
STATE_LOGGING_IN = "logging_in"
STATE_FORCE_LOGIN_REQUIRED = "force_login_required"
STATE_FORCE_LOGGING_IN = "force_logging_in"
LOGIN_REJECTED = object()

HEARTBEAT_INTERVAL_MS = 60000
RECONNECT_INTERVAL_MS = 3000


class TCPReconnectScheduled(RuntimeError):
	"""当前事务已终止，旧连接已交给统一重连状态机。"""


class LogoutRequested(RuntimeError):
	"""用户已确认切换账号，当前普通业务事务必须立即让出。"""


class ServerLogoutRequested(RuntimeError):
	"""服务器已接受强制退出，当前普通事务必须让出。"""


class CommunicationController:
	"""串行处理 UI 登录/入组请求，网络工作不进入 UI 线程。"""

	def __init__(self, config, state_store, message_bus,
				 project_name="JingWuPOC", project_version="1.0.0",
				 persistent_config=None):
		self.config = dict(config)
		self.state = state_store
		self.messages = message_bus
		self.project_name = str(project_name)
		self.project_version = str(project_version)
		self.persistent_config = persistent_config
		self.running = False
		self.thread_id = None
		self._lock = _thread.allocate_lock()
		self._wake = _thread.allocate_semphore(1)
		self._wake.acquire()
		self._wake_pending = False
		self._requests = []
		self._events = []
		self._event_lock = _thread.allocate_lock()
		self.runtime = None
		self.session = None
		self.audio = None
		self._volume_log_at = None
		self._runtime_generation = 0
		self.tcp_socket = None
		self.udp_socket = None
		self.police_no = None
		self.group_id = None
		self.single_group_id = None
		self.request_count = 1
		self.login_state = STATE_IDLE
		self.login_result = None
		self.force_login_stage = "idle"
		self._force_login_auto_join = True
		self._force_login_resume_group = None
		# 登录成功后保持自动恢复资格；业务事务失败进入 error 不能
		# 阻止 TCP 断线重连，只有主动退出登录才清除此资格。
		self._auto_reconnect_enabled = False
		self._heartbeat_timer = None
		self._heartbeat_due = False
		self._reconnect_pending = False
		self._reconnect_due = False
		self._reconnect_reason = None
		self._reconnect_attempt = 0
		self._incoming_single = None
		self._single_busy = False
		self._directory_refresh_busy = False
		self._http_worker = None
		self._http_active_context = None
		self._http_pending_context = None
		self._http_result = None
		self._http_request_id = 0
		self._directory_cache = None
		self._directory_cache_context = None
		self._directory_session_generation = 0
		self._group_action_generation = 0
		self._login_directory_context = None
		self._leave_group_busy = False
		self._group_explicitly_left = False
		self._server_logout_pending = False
		self._server_logout_ack_sent = False
		self._tts_lock = _thread.allocate_lock()
		self._tts_active = False
		self._logout_requested = False
		self._login_intent = None
		self._login_busy = False
		self._login_transaction_id = 0
		self._active_login_transaction_id = 0
		self._prelogin_connected = False
		self._network_ready = False
		self._tcp_ready = False
		self._connection_progress_seen = {"network": 0, "tcp": 0}
		self._response_history = []
		self._response_history_set = set()
		self._active_tcp_transaction = None
		self._release_recovery = None
		self._release_recovery_attempt = 0
		self._release_compensation_active = False
		self._gnss_sample_request = None
		self._screen_wake_request = None
		self._guest_mode = False
		self._guest_resume_police_no = None
		self._guest_auto_login_blocked = False
		self._guest_auto_login_attempted = False
		self._poc_enabled = None
		self._ota_busy = False
		self._ota_saved_group_id = None
		self._gnss_sampling_started = False
		self._gnss_empty_uploaded = False
		self._gnss_last_uploaded_fix = None
		self._gnss_deferred_result = None

	def start(self):
		if self.running:
			return False
		# UI首帧完成后、任何通信线程启动前初始化GPIO27并保持低电平1ms。
		self._ensure_audio()
		self.running = True
		self.thread_id = _thread.start_new_thread(self._run, ())
		print("[通信] 统一通信业务线程已启动")
		self._enqueue("prepare_connection", None)
		return True

	def stop(self):
		self.running = False
		self._signal()
		self._close_resources()

	def request_login(self, police_no):
		if self._logout_requested:
			return False
		value = str(police_no).strip()
		if not value:
			return False
		if self._guest_mode:
			self.exit_guest_mode()
		self._login_intent = value
		# 登录命令已经由统一业务线程取出后，队列中已看不到它。
		# 此时重复按确认只合并为当前登录事务，不能再排入第二次登录。
		if self._login_busy:
			print("[通信] 登录事务处理中，合并重复登录请求：警号={}".format(value))
			return True
		self._login_transaction_id += 1
		if self._login_transaction_id > 0x7FFFFFFF:
			self._login_transaction_id = 1
		transaction_id = self._login_transaction_id
		self._active_login_transaction_id = transaction_id
		self._directory_session_generation += 1
		self._login_directory_context = None
		if (self._http_active_context is not None and
				self._http_active_context.get("purpose") == "prelogin" and
				self._http_active_context.get("login_transaction_id") is None):
			self._http_active_context["login_transaction_id"] = transaction_id
			self._http_active_context["session_generation"] = (
				self._directory_session_generation)
		self._update_identity_from_cache(value)
		self.login_result = None
		self.force_login_stage = "idle"
		# 入队前先使旧logged_in/joined失效，UI不能把上一会话状态
		# 误认为本次登录成功。
		self._publish_login(
			STATE_LOGGING_IN, transaction_id=transaction_id)
		# 重连状态由Timer1统一恢复TCP并续接登录；此时只更新最新登录意图，
		# 不能再投递独立登录事务绕过3秒重连节奏。
		payload = {"police_no": value, "transaction_id": transaction_id}
		accepted = True if self._reconnect_pending else self._enqueue("login", payload)
		if accepted:
			print("[通信] 收到登录请求：警号={} 登录事务={}".format(
				value, transaction_id))
			self._publish_pending_connection_for_request(True)
		else:
			self._login_intent = None
			self._publish_login(
				STATE_ERROR, "登录请求入队失败", transaction_id)
		return accepted

	def request_force_login(self):
		"""用户确认后才允许把0x0E事务交给统一业务线程。"""
		if self.force_login_stage != "prompt" or self.police_no is None:
			return False
		accepted = self._enqueue("force_login", None)
		if accepted:
			self._publish_pending_connection_for_request(True)
		return accepted

	def cancel_force_login(self):
		if self.force_login_stage != "prompt":
			return False
		return self._enqueue("cancel_force_login", None)

	def request_join_group(self, group_id):
		if self._logout_requested:
			return False
		if self._ota_busy:
			print("[通信][OTA] 升级期间忽略入组请求")
			return False
		if self._guest_mode:
			print("[通信][游客] 忽略入组请求")
			return False
		self._group_action_generation += 1
		if self._poc_enabled is not True:
			print("[通信][POC权限] 功能未开通或权限尚未确认，忽略入组请求")
			return False
		try:
			value = group_id_to_uint32(group_id)
		except Exception:
			return False
		accepted = self._enqueue("join_group", value)
		if accepted:
			print("[通信] 收到入组请求：组ID={}".format(value))
			self._publish_pending_connection_for_request(True)
		return accepted

	def request_single_call(self, person_key):
		if self._logout_requested:
			return False
		if self._ota_busy:
			print("[通信][OTA] 升级期间忽略单呼请求")
			return False
		if self._guest_mode:
			print("[通信][游客] 忽略单呼请求")
			return False
		if self._poc_enabled is not True:
			print("[通信][POC权限] 功能未开通或权限尚未确认，忽略单呼请求")
			return False
		value = str(person_key).strip()
		if not value:
			return False
		accepted = self._enqueue("single_call", value)
		if accepted:
			print("[通信] 收到单呼请求：成员={}".format(value))
			self._publish_pending_connection_for_request(True)
		return accepted

	def request_leave_group(self):
		if self._logout_requested:
			return False
		if self._ota_busy:
			print("[通信][OTA] 升级期间忽略UI退组请求")
			return False
		if self._guest_mode:
			print("[通信][游客] 忽略退组请求")
			return False
		self._group_action_generation += 1
		accepted = self._enqueue("leave_group", None)
		print("[通信][组呼] UI退组请求：已入队或合并={}".format(accepted))
		if accepted:
			self._publish_pending_connection_for_request(True)
		return accepted

	def request_set_volume(self, level):
		value = max(0, min(11, int(level)))
		accepted = self._enqueue("set_volume", value)
		if not accepted:
			print("[设置] 音量请求入队失败：{}".format(value))
		return accepted

	def request_commit_volume(self):
		return self._enqueue("commit_volume", None)

	def request_hardware_key(self, action):
		"""现有低优先级Worker中的本地按键入口，不进入通信请求队列。"""
		if action not in FUNCTION_KEY_ACTIONS:
			return False
		try:
			return self._handle_hardware_key(str(action))
		except Exception as error:
			print("[按键] 功能键处理失败：{}".format(error))
			self._publish_error(error)
			return False

	def _apply_audio_volume(self, level):
		value = max(0, min(11, int(level)))
		if self.audio is not None:
			value = self.audio.set_volume(value)
		return value

	def _record_volume_change(self, value):
		_revision, snapshot = self.state.get_snapshot()
		self.state.update_audio(snapshot["audio_tx_active"], snapshot["audio_rx_active"], value)
		if (self.persistent_config is None or
				not self.persistent_config.defer_settings_update(
					audio_volume=value)):
			print("[设置] 音量已改变，但延迟保存登记失败")
		now = utime.ticks_ms()
		if self._volume_log_at is None or utime.ticks_diff(now, self._volume_log_at) >= 1000:
			self._volume_log_at = now
			print("[设置] 音量已执行：{} 音频硬件已启动={}，连续60秒无变动后保存".format(
				value, self.audio is not None))

	def _set_volume(self, level):
		value = self._apply_audio_volume(level)
		self._record_volume_change(value)
		return value

	def _commit_volume(self):
		# 页面释放动作仅保留接口兼容性；保存计时由实际值变动启动，
		# 不能把释放滑块误算成新的变动。
		return self.persistent_config is not None

	def _ensure_audio(self, wait_for_tts=False):
		"""按需启动共享音频端口，使按键提示音不依赖TCP登录状态。"""
		deadline = utime.ticks_add(utime.ticks_ms(), 6000)
		while True:
			self._tts_lock.acquire()
			try:
				if not self._tts_active:
					if self.audio is not None and self.audio.running:
						return self.audio
					audio_port = AudioPort(
						speaker_gpio=int(self.config["speaker_enable_gpio"]),
						volume=self.state.get_snapshot()[1]["audio_volume"])
					audio_port.start()
					self.audio = audio_port
					print("[通信] 音频端口已启动：喇叭GPIO{}".format(
						audio_port.speaker_gpio))
					return audio_port
			finally:
				self._tts_lock.release()
			if not wait_for_tts:
				return None
			if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
				raise RuntimeError("强退TTS尚未释放音频输出")
			utime.sleep_ms(20)

	def play_forced_logout_announcement(self):
		"""仅在强退清理完成且UI已返回登录页后调用一次。"""
		self._tts_lock.acquire()
		try:
			if self._tts_active or self.runtime is not None:
				return False
			if self.audio is not None:
				try:
					self.audio.close()
				except Exception as error:
					print("[通信][TTS] 按键音频释放失败：{}".format(error))
					return False
				self.audio = None
			self._tts_active = True
		finally:
			self._tts_lock.release()
		try:
			volume = self.state.get_snapshot()[1]["audio_volume"]
			_thread.start_new_thread(self._run_forced_logout_tts, (volume,))
			return True
		except Exception as error:
			self._tts_lock.acquire()
			try:
				self._tts_active = False
			finally:
				self._tts_lock.release()
			print("[通信][TTS] 强退播报启动失败：{}".format(error))
			return False

	def _run_forced_logout_tts(self, volume):
		try:
			print("[通信][TTS] 播报：账号强制下线")
			play_forced_logout_tts(volume)
		except Exception as error:
			print("[通信][TTS] 强退播报失败：{}".format(error))
		finally:
			self._tts_lock.acquire()
			try:
				self._tts_active = False
			finally:
				self._tts_lock.release()

	def _handle_hardware_key(self, action):
		if self._logout_requested:
			if action == KEY_PTT_PRESS:
				print("[通信][退出] PTT按下已过滤：正在退出登录")
			return True
		if action in (KEY_PTT_PRESS, KEY_PTT_RELEASE):
			pressed = action == KEY_PTT_PRESS
			if self._ota_busy:
				if pressed:
					print("[通信][OTA] PTT按下已过滤：设备正在升级")
				return True
			if self._guest_mode:
				if pressed:
					print("[通信][游客] PTT按下已过滤：游客身份无法对讲")
					self._publish_floor_event(
						"guest_ptt_blocked", "self", None, None)
				return True
			if self._poc_enabled is False:
				if pressed:
					print("[通信][POC权限] PTT按下已过滤：POC功能未开通")
				return True
			if self._poc_enabled is None:
				if pressed:
					print("[通信][POC权限] PTT按下已过滤：HTTP尚未确认POC权限")
					self._publish_pending_connection_for_request(
						True, user_visible=True)
				return True
			if (self._reconnect_pending or self.session is None or
					not self.session.running):
				if pressed:
					print("[通信] 重连期间PTT按下：不保存、不补发抢麦，提示当前连接状态")
					self._publish_pending_connection_for_request(
						True, user_visible=True)
				return True
			return self.session.signal_external_ptt(pressed)
		_revision, snapshot = self.state.get_snapshot()
		if self.audio is None or not self.audio.running:
			try:
				self._ensure_audio()
			except Exception as error:
				print("[按键] 提示音硬件启动失败：{}".format(error))
		value = int(snapshot["audio_volume"])
		if action in (KEY_VOLUME_UP, KEY_VOLUME_DOWN):
			delta = 1 if action == KEY_VOLUME_UP else -1
			value = max(0, min(11, value + delta))
			value = self._apply_audio_volume(value)
		beeped = False
		if (self.audio is not None and not snapshot["audio_tx_active"] and
				not snapshot["audio_rx_active"]):
			beeped = self.audio.play_key_beep()
		if action in (KEY_VOLUME_UP, KEY_VOLUME_DOWN):
			self._record_volume_change(value)
			self.state.publish_volume_key_event(value)
		if action == KEY_VOLUME_UP:
			name = "音量+"
		elif action == KEY_VOLUME_DOWN:
			name = "音量-"
		else:
			name = action
		print("[按键] {}：音量={} 提示音={}".format(name, value,
			"已播放" if beeped else "未播放"))
		return True

	def request_dissolve_single_call(self):
		if self._logout_requested:
			return False
		if self._ota_busy:
			print("[通信][OTA] 升级期间忽略UI解散单呼请求")
			return False
		if self._guest_mode:
			print("[通信][游客] 忽略解散单呼请求")
			return False
		accepted = self._enqueue("dissolve_single", None)
		print("[通信][单呼] UI解散请求入队={}".format(accepted))
		if accepted:
			self._publish_pending_connection_for_request(True)
		return accepted

	def request_logout(self):
		if self._logout_requested:
			return True
		self._begin_logout_isolation()
		if self._guest_mode:
			print("[通信][游客] 未建立POC登录，跳过退出登录包")
			self.exit_guest_mode()
			self._finish_logout_transaction("guest")
			return True
		accepted = self._enqueue("logout", None)
		if accepted:
			print("[通信] 收到退出登录请求")
		else:
			self._finish_logout_transaction("queue_failed")
		return accepted

	def _begin_logout_isolation(self):
		"""确认按钮调用路径：先隔离对讲，再等待业务线程执行退出。"""
		self._logout_requested = True
		self._auto_reconnect_enabled = False
		self._stop_heartbeat_timer()
		self.state.begin_logout()
		if self.runtime is not None:
			try:
				self.runtime.begin_logout()
			except Exception:
				pass
		if self.session is not None and self.runtime is None:
			try:
				self.session.begin_logout()
			except Exception as error:
				print("[通信][退出] 隔离对讲会话失败，继续退出：{}".format(error))
		elif self.audio is not None:
			try:
				self.audio.stop_playback()
			except Exception:
				pass
		_revision, snapshot = self.state.get_snapshot()
		self.state.update_audio(False, False, snapshot["audio_volume"])
		self._publish_floor_event("communication_ended", None, None, None)
		print("[通信][退出] 已立即屏蔽PTT、TCP对讲业务和UDP语音")

	def _finish_logout_transaction(self, result):
		self._logout_requested = False
		self.state.finish_logout(result)

	def request_refresh_directory(self):
		"""UI只提交名单刷新请求，实际HTTP由一次性线程执行。"""
		if self._guest_mode:
			print("[通信][游客] 跳过HTTP组呼组和成员刷新")
			return False
		if self.running and not self._network_ready:
			self._publish_pending_connection_for_request(False)
		if (not self.running or self.runtime is None or not self.runtime.running or
				self.login_state not in (STATE_LOGGED_IN, STATE_JOINED) or
				self._reconnect_pending):
			print("[通信][HTTP] 跳过名单刷新：当前未登录或正在重连")
			return False
		accepted = self._enqueue("refresh_directory", None)
		print("[通信][HTTP] 对讲机页面名单刷新请求：已入队或合并={}".format(accepted))
		return accepted

	def set_gnss_sample_request(self, callback):
		"""注册低优先级Worker采样入口；不创建线程。"""
		self._gnss_sample_request = callback

	def set_screen_wake_request(self, callback):
		"""注册现有低优先级Worker的亮屏入口；不创建线程。"""
		self._screen_wake_request = callback

	def prepare_for_ota(self, timeout_ms=30000):
		"""同步等待现有通信线程完成释放麦权、解散单呼和退组。"""
		if self._ota_busy:
			return False, self._ota_saved_group_id, "OTA事务已在执行"
		self._ota_busy = True
		self._ota_saved_group_id = self.group_id
		self.state.update_ota_runtime(busy=True)
		ticket = {"done": False, "ok": False, "error": None,
			"saved_group_id": self._ota_saved_group_id}
		if not self._enqueue("ota_prepare", ticket):
			self._ota_busy = False
			self.state.update_ota_runtime(busy=False)
			return False, ticket["saved_group_id"], "OTA通信准备命令入队失败"
		deadline = utime.ticks_add(utime.ticks_ms(), int(timeout_ms))
		while not ticket["done"]:
			if utime.ticks_diff(utime.ticks_ms(), deadline) >= 0:
				return False, ticket["saved_group_id"], "等待对讲退出超时"
			utime.sleep_ms(50)
		return ticket["ok"], ticket["saved_group_id"], ticket["error"]

	def restore_after_ota_failure(self, group_id, timeout_ms=30000):
		ticket = {"done": False, "ok": False, "error": None,
			"group_id": group_id}
		if not self._enqueue("ota_restore", ticket):
			self._ota_busy = False
			self.state.update_ota_runtime(busy=False)
			return False
		deadline = utime.ticks_add(utime.ticks_ms(), int(timeout_ms))
		while not ticket["done"]:
			if utime.ticks_diff(utime.ticks_ms(), deadline) >= 0:
				print("[通信][OTA] 等待失败恢复超时")
				self._ota_busy = False
				self._ota_saved_group_id = None
				self.state.update_ota_runtime(busy=False)
				return False
			utime.sleep_ms(50)
		return ticket["ok"]

	def _prepare_ota_business(self, ticket):
		try:
			print("[通信][OTA] 开始关闭对讲：原组ID={} 单呼组ID={}".format(
				ticket["saved_group_id"], self.single_group_id))
			if (self.session is not None and self.session.state in
					("requesting_floor", "transmitting", "releasing_floor")):
				self.session.signal_external_ptt(False)
				self._finish_floor_transaction()
			if self.single_group_id is not None:
				self._dissolve_single()
			if self.group_id is not None:
				self._leave_group()
			ticket["ok"] = True
			print("[通信][OTA] 对讲已关闭，可以开始下载")
		except Exception as error:
			ticket["error"] = str(error)
			print("[通信][OTA] 关闭对讲失败：{}".format(error))
		finally:
			ticket["done"] = True

	def _restore_ota_business(self, ticket):
		try:
			group_id = ticket.get("group_id")
			self._ota_busy = False
			self.state.update_ota_runtime(busy=False)
			if group_id is not None and self.group_id != group_id:
				print("[通信][OTA] 升级失败，开始恢复原组：组ID={}".format(group_id))
				self._join_group(group_id, force=True)
			ticket["ok"] = True
			print("[通信][OTA] PTT和息屏策略已恢复")
		except Exception as error:
			ticket["error"] = str(error)
			print("[通信][OTA] 原组恢复失败：{}".format(error))
		finally:
			self._ota_saved_group_id = None
			ticket["done"] = True

	def connection_ready(self):
		"""登录页只读查询：蜂窝网络和预连接TCP是否均已就绪。"""
		return bool(self._network_ready and self._tcp_ready and
			self.tcp_socket is not None)

	def connection_issue_text(self):
		"""返回无网登录确认框第一行，不触发任何连接动作。"""
		_revision, snapshot = self.state.get_snapshot()
		stage = snapshot.get("connection_stage")
		status = snapshot.get("connection_status")
		if not self._network_ready:
			return ("网络连接失败" if stage == "network" and
				status == "failure" else "网络连接中... ...")
		return ("TCP连接失败" if stage == "tcp" and
			status == "failure" else "TCP连接中... ...")

	def enter_guest_mode(self, police_no=None):
		"""取消真实登录意图，仅保留后台网络/TCP预连接。"""
		self._directory_session_generation += 1
		self._login_directory_context = None
		self._http_pending_context = None
		self._guest_mode = True
		candidate = str(police_no or self._login_intent or "").strip()
		self._guest_resume_police_no = candidate or None
		self._guest_auto_login_blocked = False
		self._guest_auto_login_attempted = False
		self._set_poc_access(None)
		self._login_intent = None
		self._auto_reconnect_enabled = False
		self.police_no = None
		self.group_id = None
		self.single_group_id = None
		self._lock.acquire()
		try:
			self._requests = [item for item in self._requests if item[0] not in (
				"login", "force_login", "join_group", "leave_group",
				"single_call", "dissolve_single", "refresh_directory",
				"gnss_result")]
		finally:
			self._lock.release()
		self.state.update_identity_mode("guest")
		self.state.update_intercom_directory((), ())
		self.state.update_group_selection(None)
		self.state.update_single_call("idle", None, None, None)
		self.state.update_poc(False, "guest", None, None,
			"idle", None, "idle", None)
		self.state.update_login_transaction("guest", None, None, "idle")
		self.state.clear_guest_relogin_prompt()
		self.messages.publish(MSG_GROUP_CHANGED, ())
		self._request_gnss_sample(True)
		print("[通信][游客] 已进入游客模式，仅保留后台网络和TCP预连接")
		return True

	def exit_guest_mode(self):
		"""退出游客显示状态；游客从未登录，因此不发送0x0F。"""
		was_guest = self._guest_mode
		self._guest_mode = False
		self._guest_resume_police_no = None
		self._guest_auto_login_blocked = False
		self._guest_auto_login_attempted = False
		self.state.clear_guest_relogin_prompt()
		self.state.update_identity_mode("normal")
		if self.login_state not in (STATE_LOGGED_IN, STATE_JOINED):
			self.state.update_poc(False, "idle", None, None,
				"idle", None, "idle", None)
			self.state.update_login_transaction("idle", None, None, "idle")
		if was_guest:
			print("[通信][游客] 已退出游客模式，未发送退出登录包")
		return was_guest

	def _request_screen_wake(self, reason):
		if self._screen_wake_request is None:
			return False
		try:
			return bool(self._screen_wake_request(reason))
		except Exception as error:
			# 亮屏属于显示侧效果，失败不能中断麦权和RTP处理。
			print("[通信] 请求亮屏失败，继续执行对讲流程：{}".format(error))
			return False

	def request_gnss_result(self, result):
		"""低优先级Worker只投递结果，TCP发送仍由本业务线程串行执行。"""
		if not isinstance(result, dict):
			return False
		return self._enqueue("gnss_result", dict(result))

	def _request_gnss_sample(self, force):
		if self._gnss_sample_request is None:
			return False
		try:
			return bool(self._gnss_sample_request(bool(force)))
		except Exception as error:
			print("[定位] 请求GNSS采样失败：{}".format(error))
			return False

	def _enqueue(self, kind, payload):
		self._lock.acquire()
		try:
			if self._logout_requested and kind != "logout":
				return False
			if kind in ("logout", "server_logout"):
				# 退出拥有最高业务优先级，旧会话的待处理命令不再执行。
				self._requests = []
				self._requests.insert(0, (kind, payload))
				inserted = True
			else:
				inserted = False
			if kind in ("login", "prepare_connection"):
				for index, request_item in enumerate(self._requests):
					if request_item[0] == kind:
						if kind == "login":
							self._requests[index] = (kind, payload)
						return True
			if kind == "set_volume":
				for index, request_item in enumerate(self._requests):
					if request_item[0] == kind:
						self._requests[index] = (kind, payload)
						return True
			if kind == "refresh_directory":
				if self._directory_refresh_busy:
					return True
				for request_item in self._requests:
					if request_item[0] == kind:
						return True
			if kind == "gnss_result":
				for index, request_item in enumerate(self._requests):
					if request_item[0] == kind:
						self._requests[index] = (kind, payload)
						return True
			# 入组与退组共用最新选择，待处理的旧组操作不能覆盖新选择。
			if kind in ("join_group", "leave_group"):
				pending = []
				for request_item in self._requests:
					if request_item[0] not in ("join_group", "leave_group"):
						pending.append(request_item)
				self._requests = pending
				if kind == "leave_group" and self._leave_group_busy:
					return True
			if kind == "ota_prepare":
				self._requests = [item for item in self._requests if item[0] not in (
					"join_group", "leave_group", "single_call", "dissolve_single")]
				self._requests.insert(0, (kind, payload))
				inserted = True
			elif kind not in ("logout", "server_logout"):
				inserted = False
			if inserted:
				pass
			elif len(self._requests) >= 8:
				return False
			elif kind in ("single_call", "dissolve_single"):
				if self._single_busy:
					return False
				for request_item in self._requests:
					if request_item[0] in ("single_call", "dissolve_single"):
						return False
			if not inserted:
				self._requests.append((kind, payload))
		finally:
			self._lock.release()
		self._signal()
		return True

	def _signal(self):
		self._lock.acquire()
		try:
			if self._wake_pending:
				return
			self._wake_pending = True
		finally:
			self._lock.release()
		try:
			self._wake.release()
		except Exception:
			self._lock.acquire()
			self._wake_pending = False
			self._lock.release()

	def _signal_timer_isr(self):
		"""Timer回调专用唤醒：不加锁，重复到期只保留一次令牌。"""
		if self._wake_pending:
			return False
		self._wake_pending = True
		try:
			self._wake.release()
			return True
		except Exception:
			self._wake_pending = False
			return False

	def _take_request(self):
		self._lock.acquire()
		try:
			item = self._requests.pop(0) if self._requests else None
			if item is not None and item[0] == "refresh_directory":
				self._directory_refresh_busy = True
			if item is not None and item[0] == "leave_group":
				self._leave_group_busy = True
			more_pending = bool(self._requests)
		finally:
			self._lock.release()
		if more_pending:
			self._signal()
		return item

	def _append_event(self, event):
		fatal = False
		self._event_lock.acquire()
		try:
			# 控制器只需暂存少量 TCP 应答与发送异常；PTT/RTP 的实时
			# 处理已由会话和音频端完成，故这里保持有界，防止异常网络
			# 下事件积压耗尽内存。
			if len(self._events) >= 32:
				self._events.pop(0)
			self._events.append(event)
			fatal = (event.get("source") == "send" and
				event.get("event", {}).get("type") in
				("tcp_send_error", "tcp_send_fatal"))
		finally:
			self._event_lock.release()
		if fatal:
			print("[通信] TCP发送异常，立即唤醒统一业务线程：{}".format(
				event.get("event", {}).get("payload")))
			self._signal()

	def _take_tcp_send_failure(self):
		"""取走发送线程上报的致命TCP发送失败事件。"""
		self._event_lock.acquire()
		try:
			for index, event in enumerate(self._events):
				if event["source"] != "send":
					continue
				inner = event["event"]
				if inner["type"] not in ("tcp_send_error", "tcp_send_fatal"):
					continue
				self._events.pop(index)
				return inner["payload"]
		finally:
			self._event_lock.release()
		return None

	def _remember_response(self, frame):
		signature = (int(frame["msg_type"]), int(frame["request_cnt"]),
			bytes(frame["payload"]))
		if signature in self._response_history_set:
			return False
		self._response_history.append(signature)
		self._response_history_set.add(signature)
		while len(self._response_history) > 128:
			old = self._response_history.pop(0)
			self._response_history_set.discard(old)
		return True

	def _take_frame(self, msg_type, request_count, timeout_ms,
					until_single_cleared=None):
		deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
		while self.running and utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
			transaction = self._active_tcp_transaction or {}
			if self._logout_requested and not transaction.get("logout"):
				raise LogoutRequested("用户已确认退出登录")
			# 正式入口的 PTT 状态机与本线程共用执行上下文；登录/入组
			# 等待期间也及时消费 GPIO 和已到达的运行时事件。
			if self.session is not None:
				self.session.poll_once()
			if self._server_logout_pending:
				raise ServerLogoutRequested("设备已被强制退出")
			if (until_single_cleared is not None and
					self.single_group_id != until_single_cleared):
				return None
			if self.runtime is None or not self.runtime.running:
				raise TCPReconnectScheduled("等待TCP应答期间连接已失效")
			send_failure = self._take_tcp_send_failure()
			if send_failure is not None:
				transaction = self._active_tcp_transaction or {}
				print("[通信][TCP] 发送失败：消息={} request_cnt={} 阶段={} 错误={}".format(
					transaction.get("label", "未知"),
					transaction.get("request_cnt"),
					transaction.get("attempt_text", "未知"), send_failure))
				if not transaction.get("logout"):
					self._schedule_reconnect("TCP发送失败：{}".format(send_failure))
				raise TCPReconnectScheduled("TCP发送失败")
			self._event_lock.acquire()
			try:
				index = 0
				while index < len(self._events):
					event = self._events[index]
					if event["source"] != "tcp":
						index += 1
						continue
					inner = event["event"]
					if inner["type"] != "tcp_frame":
						index += 1
						continue
					frame = inner["payload"]
					signature = (int(frame["msg_type"]), int(frame["request_cnt"]),
						bytes(frame["payload"]))
					if signature in self._response_history_set:
						self._events.pop(index)
						print("[通信][TCP] 过滤重复应答：0x{:02X} request_cnt={}".format(
							frame["msg_type"], frame["request_cnt"]))
						continue
					if (frame["msg_type"] == msg_type and
							frame["request_cnt"] == ((request_count + 1) & 0xFFFF)):
						self._events.pop(index)
						self._remember_response(frame)
						print("[通信][TCP] 应答匹配：0x{:02X} request_cnt={}".format(
							msg_type, frame["request_cnt"]))
						return frame
					index += 1
			finally:
				self._event_lock.release()
			utime.sleep_ms(5)
		return None

	def _send_tcp_transaction(self, command, packet, ack_type, request_count,
							  label, until_single_cleared=None, logout=False):
		"""同一Socket上用相同帧和request_cnt执行一次主动TCP事务。"""
		if self._logout_requested and not logout:
			raise LogoutRequested("用户已确认退出登录")
		for attempt in range(TCP_MAX_RETRIES + 1):
			if self._logout_requested and not logout:
				raise LogoutRequested("用户已确认退出登录")
			wait_ms = min(
				TCP_INITIAL_WAIT_MS + (attempt * TCP_RETRY_STEP_MS),
				TCP_MAX_WAIT_MS)
			attempt_text = ("首次发送" if attempt == 0 else
				"重发第{}/{}次".format(attempt, TCP_MAX_RETRIES))
			self._active_tcp_transaction = {
				"command": command, "label": label,
				"request_cnt": int(request_count),
				"attempt": attempt, "attempt_text": attempt_text,
				"logout": bool(logout),
			}
			print("[通信][TCP] {}：{} request_cnt={}，等待{}ms".format(
				attempt_text, label, request_count, wait_ms))
			if self.runtime is None or not self.runtime.running:
				if not logout:
					self._schedule_reconnect("{}发送前连接已失效".format(label))
				raise TCPReconnectScheduled("TCP连接已失效")
			if not self.runtime.submit(command, packet):
				if not logout:
					self._schedule_reconnect("{}命令入队失败".format(label))
				raise TCPReconnectScheduled("{}命令入队失败".format(label))
			frame = self._take_frame(
				ack_type, request_count, wait_ms, until_single_cleared)
			if frame is not None:
				self._active_tcp_transaction = None
				return frame
			if (until_single_cleared is not None and
					self.single_group_id != until_single_cleared):
				self._active_tcp_transaction = None
				return None
		if not logout:
			self._schedule_reconnect("{}重发{}次仍无应答".format(
				label, TCP_MAX_RETRIES))
		self._active_tcp_transaction = None
		print("[通信][TCP] {}重试耗尽：request_cnt={}，准备{}".format(
			label, request_count, "本地退出" if logout else "自动重连"))
		raise TCPReconnectScheduled("{}重试耗尽".format(label))

	def _publish_login(self, state, error=None, transaction_id=None):
		self.login_state = state
		if transaction_id is None:
			transaction_id = self._active_login_transaction_id
		if error is None:
			print("[通信] 登录状态={}".format(state))
		else:
			print("[通信] 登录状态={} 错误={}".format(state, error))
		_revision, snapshot = self.state.get_snapshot()
		self.state.update_poc(
			self.runtime is not None and self.runtime.running, state, error,
			(None if self.group_id is None else str(self.group_id)),
			snapshot["floor_state"], snapshot["floor_group_id"],
			snapshot["single_state"], snapshot["single_peer"])
		self.state.update_login_transaction(
			state, self.login_result, error, self.force_login_stage,
			transaction_id)
		self.messages.publish(MSG_LOGIN_CHANGED, {
			"state": state, "error": error, "result": self.login_result,
			"force_stage": self.force_login_stage,
			"transaction_id": transaction_id})

	def _publish_floor_event(self, event_type, direction, call_type, group_id,
							 speaker_name=None, audio_selected=False):
		revision = self.state.publish_floor_event(
			event_type, direction, call_type, group_id,
			speaker_name, audio_selected)
		self.messages.publish(MSG_FLOOR_CHANGED, {
			"revision": revision, "event": event_type,
			"direction": direction, "call_type": call_type,
			"group_id": group_id, "police_name": speaker_name,
			"selected": bool(audio_selected)}, 0)
		return revision

	def _publish_error(self, error):
		text = str(error)
		self.state.set_error("communication", text)
		self.messages.publish(MSG_ERROR, {"domain": "communication", "text": text}, 1)

	def _publish_connection_progress(self, stage, reconnect=False,
								 duration_ms=None, user_visible=False):
		seen = self._connection_progress_seen.get(stage, 0)
		if duration_ms is None:
			self._connection_progress_seen[stage] = seen + 1
			duration_ms = 3000 if seen == 0 else 1000
		if stage == "network":
			text = "网络重连中... ..." if reconnect else "网络连接中... ..."
		else:
			text = "TCPsocket连接中... ..."
		self.state.publish_connection_event(
			stage, "progress", text, duration_ms, user_visible)

	def _publish_connection_success(self, stage):
		text = "网络连接成功" if stage == "network" else "TCPsocket连接成功"
		self.state.publish_connection_event(stage, "success", text, 1000, False)

	def _publish_connection_failure(self, stage):
		text = "网络连接失败" if stage == "network" else "TCPsocket连接失败"
		self.state.publish_connection_event(stage, "failure", text, 2000, False)

	def _publish_pending_connection_for_request(self, tcp_required,
										 user_visible=False):
		"""按当前阶段发布提示；只有用户主动操作才允许业务页显示。"""
		if not self._network_ready:
			self._publish_connection_progress(
				"network", self._reconnect_pending, 2000, user_visible)
		elif tcp_required and not self._tcp_ready:
			self._publish_connection_progress(
				"tcp", self._reconnect_pending, 2000, user_visible)
		elif self._reconnect_pending:
			# TCP已恢复但重新登录/恢复入组尚未完成时，整体仍属于重连过程。
			self._publish_connection_progress(
				"network", True, 2000, user_visible)

	def _wait_network(self, reconnect=False):
		self._network_ready = False
		self._tcp_ready = False
		self.state.update_transport(False, False)
		self._publish_connection_progress("network", reconnect)
		print("[通信] 等待蜂窝网络注册和数据拨号")
		try:
			if checkNet is not None:
				checkNet.CheckNetwork(self.project_name,
								 self.project_version).wait_network_connected()
		except Exception:
			self._network_ready = False
			self._publish_connection_failure("network")
			raise
		self._network_ready = True
		self.state.update_transport(True, False)
		self._publish_connection_success("network")
		print("[通信] 蜂窝网络已就绪")

	def _connect_tcp_socket(self, reconnect=False):
		self._tcp_ready = False
		self.state.update_transport(None, False)
		self._publish_connection_progress("tcp", reconnect)
		print("[通信] 开始连接TCP：{}:{}".format(
			self.config["tcp_host"], self.config["tcp_port"]))
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			sock.connect((self.config["tcp_host"], int(self.config["tcp_port"])))
			try:
				sock.settimeout(None)
			except Exception:
				try:
					sock.setblocking(True)
				except Exception:
					pass
		except Exception:
			self._tcp_ready = False
			try:
				sock.close()
			except Exception:
				pass
			self._publish_connection_failure("tcp")
			raise
		self.tcp_socket = sock
		self._prelogin_connected = self.runtime is None
		self._tcp_ready = True
		self.state.update_transport(None, True)
		self._publish_connection_success("tcp")
		print("[通信] TCP连接成功：{}:{}".format(
			self.config["tcp_host"], self.config["tcp_port"]))
		return sock

	def _prepare_prelogin_connection(self, reconnect=False):
		"""登录页后台预连接蜂窝网络和TCP，不启动UDP、音频或三个Worker。"""
		if self.runtime is not None and self.runtime.running:
			return True
		if self.tcp_socket is not None and self._prelogin_connected:
			return True
		self._wait_network(reconnect)
		self._connect_tcp_socket(reconnect)
		print("[通信] 登录前TCP Socket已就绪，等待用户输入警号")
		if self.running:
			self._queue_http_request("prelogin")
		return True

	def _read_groups(self):
		return fetch_directory(
			self.config["http_url"], self.config["device_id"], self.police_no,
			HTTP_REQUEST_TIMEOUT_S, request)

	def _update_identity_from_cache(self, police_no):
		police_no = str(police_no or "").strip()
		name = "--"
		if (self._directory_cache is not None and
				self._directory_cache_valid_for_login()):
			for person in self._directory_cache[1]:
				code = str(person.get("police_code") or person.get("key") or "")
				if code.upper() == police_no.upper():
					name = str(person.get("name") or police_no)
					break
		self.state.update_identity(name, police_no)
		return name != "--"

	def _directory_cache_valid_for_login(self):
		context = self._directory_cache_context
		if context is None:
			return self._directory_cache is not None
		transaction_id = context.get("login_transaction_id")
		if context.get("purpose") == "prelogin" and transaction_id is None:
			generation = int(context.get("session_generation") or 0)
			current = int(self._directory_session_generation)
			return generation in (current, current - 1)
		return bool(
			context.get("session_generation") ==
				self._directory_session_generation and
			transaction_id == self._active_login_transaction_id)

	def _bind_directory_identity(self, result):
		groups, people, poc_enabled = self._unpack_directory_result(result)
		police_key = str(self.police_no or "").upper()
		bound_people = []
		identity_name = None
		for source in people:
			person = dict(source)
			code = str(person.get("police_code") or person.get("key") or "")
			person["is_self"] = bool(police_key and code.upper() == police_key)
			if person["is_self"]:
				identity_name = str(person.get("name") or self.police_no)
			bound_people.append(person)
		if self.police_no is not None:
			self.state.update_identity(identity_name or "--", self.police_no)
		return groups, bound_people, poc_enabled

	def _store_http_result(self, payload):
		self._lock.acquire()
		try:
			if not self.running:
				self._http_worker = None
				self._http_active_context = None
				return False
			self._http_result = payload
		finally:
			self._lock.release()
		self._signal()
		return True

	def _take_http_result(self):
		self._lock.acquire()
		try:
			payload = self._http_result
			self._http_result = None
			return payload
		finally:
			self._lock.release()

	def _queue_http_request(self, purpose, transaction_id=None):
		context = {
			"purpose": str(purpose),
			"session_generation": self._directory_session_generation,
			"login_transaction_id": transaction_id,
			"police_no": self.police_no,
		}
		if self._http_active_context is not None:
			active = self._http_active_context
			if (purpose == "prelogin" or
					(purpose == "login" and
					 active.get("login_transaction_id") == transaction_id)):
				return True
			if (self._http_pending_context is None or purpose == "login" or
					self._http_pending_context.get("purpose") != "login"):
				self._http_pending_context = context
			print("[通信][HTTP] 已有请求执行中，本次{}请求等待合并处理".format(purpose))
			return True
		self._http_request_id = (self._http_request_id + 1) & 0x7FFFFFFF
		if self._http_request_id == 0:
			self._http_request_id = 1
		context["request_id"] = self._http_request_id
		worker = HTTPDirectoryWorker(
			self.config["http_url"], self.config["device_id"],
			self._store_http_result, HTTP_REQUEST_TIMEOUT_S, request)
		self._http_active_context = context
		self._http_worker = worker
		print("[通信][HTTP] 启动临时线程：请求编号={} 用途={} 超时={}秒".format(
			context["request_id"], purpose, HTTP_REQUEST_TIMEOUT_S))
		try:
			worker.start(context["request_id"], context.get("police_no"))
			return True
		except Exception as error:
			self._http_worker = None
			self._http_active_context = None
			print("[通信][HTTP] 临时线程启动失败：{}".format(error))
			self._publish_error(error)
			return False

	def _apply_login_directory(self, result, context):
		groups, _people, poc_enabled = self._apply_directory_result(
			result, during_login=True)
		self._login_directory_context = None
		if (poc_enabled is not True or
				context["group_action_generation"] != self._group_action_generation):
			if poc_enabled is True:
				print("[通信][HTTP] 用户已主动选择或退出组，迟到名单不执行自动入组")
			return
		resume_group = context.get("resume_group")
		if resume_group is not None:
			self._join_group(resume_group, force=True)
		elif context.get("auto_join_first") and groups:
			first_group = groups[0]
			first_group_id = group_id_to_uint32(first_group["id"])
			print("[通信] 登录成功，自动进入第一组：组ID={} 名称={}".format(
				first_group_id, first_group.get("name", first_group_id)))
			self._join_group(first_group_id)

	def _prepare_login_directory(self, auto_join_first, resume_group):
		context = {
			"session_generation": self._directory_session_generation,
			"login_transaction_id": self._active_login_transaction_id,
			"group_action_generation": self._group_action_generation,
			"auto_join_first": bool(auto_join_first),
			"resume_group": resume_group,
		}
		self._login_directory_context = context
		if (self._directory_cache is not None and
				self._directory_cache_valid_for_login()):
			print("[通信][HTTP] 使用登录前缓存名单，不等待HTTP即可完成登录业务")
			self._apply_login_directory(self._directory_cache, context)
			return True
		if (self._http_active_context is not None and
				self._http_active_context.get("purpose") == "prelogin" and
				self._http_active_context.get("login_transaction_id") is None):
			self._http_active_context["login_transaction_id"] = (
				self._active_login_transaction_id)
			self._http_active_context["session_generation"] = (
				self._directory_session_generation)
		return self._queue_http_request(
			"login", self._active_login_transaction_id)

	def _start_pending_http_request(self):
		if self._http_active_context is not None:
			return False
		pending = self._http_pending_context
		self._http_pending_context = None
		if pending is None or not self.running:
			return False
		return self._queue_http_request(
			pending["purpose"], pending.get("login_transaction_id"))

	def _handle_http_result(self, payload):
		context = self._http_active_context
		if (context is None or int(payload.get("request_id") or 0) !=
				int(context.get("request_id") or 0)):
			print("[通信][HTTP] 丢弃无法匹配的迟到结果：请求编号={}".format(
				payload.get("request_id")))
			return
		self._http_worker = None
		self._http_active_context = None
		if payload.get("ok"):
			result = payload.get("result")
			self._directory_cache = result
			self._directory_cache_context = dict(context)
			groups, people, poc_enabled = self._unpack_directory_result(result)
			print("[通信][HTTP] 临时线程获取成功：请求编号={} {}个组，{}名成员，POC={}，耗时={}ms".format(
				payload["request_id"], len(groups), len(people),
				"ON" if poc_enabled is True else
					("OFF" if poc_enabled is False else "未确认"),
				payload.get("elapsed_ms")))
			login_context = self._login_directory_context
			if (login_context is not None and
					login_context["session_generation"] ==
						self._directory_session_generation and
					login_context["login_transaction_id"] ==
						self._active_login_transaction_id and
					context.get("session_generation") ==
						self._directory_session_generation and
					context.get("login_transaction_id") ==
						self._active_login_transaction_id and
					self.login_state in (STATE_LOGGED_IN, STATE_JOINED)):
				self._apply_login_directory(result, login_context)
			elif (context.get("purpose") == "refresh" and
					context.get("session_generation") ==
						self._directory_session_generation and
					context.get("login_transaction_id") ==
						self._active_login_transaction_id and
					self.login_state in (STATE_LOGGED_IN, STATE_JOINED) and
					not self._guest_mode):
				self._apply_directory_result(result)
			else:
				print("[通信][HTTP] 结果仅写入缓存，不覆盖当前会话状态")
		else:
			error = payload.get("error") or "HTTP名单获取失败"
			print("[通信][HTTP] 临时线程获取失败：请求编号={} 错误={}，耗时={}ms".format(
				payload.get("request_id"), error, payload.get("elapsed_ms")))
			if context.get("purpose") != "prelogin":
				self._publish_error(error)
		self._start_pending_http_request()

	def _unpack_directory_result(self, result):
		"""兼容阶段测试中的旧二元返回；正式HTTP始终返回权限字段。"""
		if len(result) == 3:
			return result[0], result[1], result[2]
		return result[0], result[1], True

	def _set_poc_access(self, enabled):
		self._poc_enabled = None if enabled is None else bool(enabled)
		self.state.update_poc_access(self._poc_enabled)
		if self.session is not None:
			self.session.set_intercom_enabled(self._poc_enabled is True)

	def _clear_pending_poc_requests(self):
		self._lock.acquire()
		try:
			self._requests = [item for item in self._requests if item[0] not in (
				"join_group", "leave_group", "single_call", "dissolve_single")]
		finally:
			self._lock.release()

	def _clear_poc_sessions_locally(self, reason):
		old_single = self.single_group_id
		old_group = self.group_id
		if self.session is not None:
			self.session.clear_single()
			self.session.clear_group()
		self.single_group_id = None
		self.group_id = None
		self._incoming_single = None
		self._group_explicitly_left = True
		self.state.update_single_call("idle", None, None, None)
		self.state.update_group_selection(None)
		_revision, snapshot = self.state.get_snapshot()
		self.state.update_audio(False, False, snapshot["audio_volume"])
		if old_single is not None:
			self._publish_floor_event(
				"call_ended", None, "single", old_single)
		if old_group is not None:
			self._publish_floor_event(
				"call_ended", None, "group", old_group)
		print("[通信][POC权限] 已清理本机组呼和单呼状态：{}".format(reason))

	def _disable_poc_access(self, send_protocol=True):
		self._set_poc_access(False)
		self._clear_pending_poc_requests()
		errors = []
		if send_protocol and self.single_group_id is not None:
			try:
				self._dissolve_single()
			except Exception as error:
				errors.append("解散单呼失败：{}".format(error))
		if send_protocol and self.group_id is not None:
			try:
				self._leave_group()
			except Exception as error:
				errors.append("退组失败：{}".format(error))
		self._clear_poc_sessions_locally("POC功能未开通")
		for error in errors:
			print("[通信][POC权限] {}，已完成本机兜底清理".format(error))
			self._publish_error(error)

	def _apply_directory_result(self, result, during_login=False):
		groups, people, poc_enabled = self._bind_directory_identity(result)
		if poc_enabled is None and self._poc_enabled is not None:
			print("[通信][POC权限] 本次HTTP未找到有效本机权限，保留上次结果")
			poc_enabled = self._poc_enabled
		if poc_enabled is True:
			self._set_poc_access(True)
			self.state.update_intercom_directory(groups, people)
			self.messages.publish(MSG_GROUP_CHANGED, groups)
			return groups, people, True
		self.state.update_intercom_directory((), ())
		self.messages.publish(MSG_GROUP_CHANGED, ())
		if poc_enabled is False:
			self._disable_poc_access(send_protocol=not during_login)
		else:
			self._set_poc_access(None)
			print("[通信][POC权限] HTTP未确认本机poc_enable，暂不开放对讲")
		return (), (), poc_enabled

	def _refresh_directory(self):
		if (self.runtime is None or not self.runtime.running or
				self.login_state not in (STATE_LOGGED_IN, STATE_JOINED) or
				self._reconnect_pending):
			print("[通信][HTTP] 跳过待处理名单刷新：连接或登录状态已变化")
			return
		print("[通信][HTTP] 提交对讲名单临时线程刷新：{}".format(
			self.config["http_url"]))
		self._queue_http_request(
			"refresh", self._active_login_transaction_id)

	def _ensure_runtime(self):
		if self.runtime is not None and self.runtime.running:
			print("[通信] 复用现有通信运行时：三个Worker仍在运行")
			return
		# 每次重建连接分配新代号；旧 Worker 即使在 socket.close 后稍晚
		# 才返回，也不能把旧事件或旧音频写入新会话。
		self._runtime_generation += 1
		runtime_generation = self._runtime_generation
		print("[通信] 开始创建正式UDP会话并接管已连接TCP Socket")
		if self.tcp_socket is None:
			self._wait_network(self._reconnect_pending)
			self._connect_tcp_socket(self._reconnect_pending)
		self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self.udp_socket.bind(("0.0.0.0", int(self.config["udp_local_port"])))
		print("[通信] UDP本地端口绑定：{}".format(self.config["udp_local_port"]))
		self._prelogin_connected = False
		print("[通信] TCP连接建立，准备启动音频和三个通信Worker")
		self._ensure_audio(wait_for_tts=True)

		def on_runtime_event(event):
			if runtime_generation != self._runtime_generation:
				return
			if event["source"] == "rtp":
				inner = event["event"]
				if inner["type"] == "rtp_receive_error":
					print("[通信][RTP] 接收异常：{}".format(inner["payload"]))
				elif inner["type"] == "rtp_first_packet":
					context = inner["payload"]
					if self.runtime is not None:
						self.runtime.cancel_rtp_activation(
							(context.get("call_type"), context.get("group_id")),
							context.get("server_port"))
				return
			if event["source"] == "tcp":
				inner = event["event"]
				if inner["type"] == "tcp_frame":
					frame = inner["payload"]
					if frame["msg_type"] == MSG_LOGOUT:
						self._server_logout_ack_sent = False
					print("[通信][TCP] 收帧：0x{:02X} request_cnt={} payload={}字节".format(
						frame["msg_type"], frame["request_cnt"], len(frame["payload"])))
					if frame["msg_type"] in (MSG_SINGLE_INVITED,
							MSG_DISSOLVE_SINGLE, MSG_FORCE_RELEASE_FLOOR,
							MSG_FLOOR_OCCUPIED, MSG_FLOOR_IDLE, MSG_LOGOUT,
							0x88, 0x89):
						# 主动控制和麦权应答只由会话处理，不堆积在事务应答缓存。
						return
			if event["source"] == "send":
				inner = event["event"]
				if inner["type"] == "rtp_activate_result":
					result = inner["payload"]
					print("[通信][RTP] 发送激活包：服务器UDP端口={}，结果={}".format(
						result.get("server_port"),
						"成功" if result.get("sent") else "失败"))
				elif inner["type"] == "rtp_activate_retry":
					info = inner["payload"]
					print("[通信][RTP] 首包未到，500ms后重发第{}次：服务器UDP端口={}".format(
						info.get("retry_count"), info.get("server_port")))
				elif inner["type"] == "rtp_activate_timeout":
					info = inner["payload"]
					print("[通信][RTP] 首包激活超时：连续500ms重试2次仍未收到有效语音，服务器UDP端口={}".format(
						info.get("server_port")))
				elif inner["type"] == "rtp_audio_first_frame_sent":
					print("[通信][RTP] 开始发送UDP语音包：服务器UDP端口={}，本次讲话仅打印一次".format(
						inner["payload"]))
				if (inner["type"] == "tcp_packet_result" and
						inner["payload"].get("msg_type") == MSG_LOGOUT_ACK):
					self._server_logout_ack_sent = bool(inner["payload"].get("sent"))
				if inner["type"] not in ("tcp_send_error", "tcp_send_fatal"):
					return
			self._append_event(event)

		self.runtime = CommunicationRuntime(
			self.tcp_socket, self.udp_socket, self.config["tcp_host"],
			read_audio_frame=self.audio.read_capture_frame,
			write_audio_frame=self.audio.write_playback_frame,
			event_callback=on_runtime_event,
			command_queue=CommunicationCommandQueue(max_length=32))
		self.session = CommunicationSession(
			submit_command=self.runtime.submit,
			device_id=self.config["device_id"], police_no=self.police_no,
			 audio_port=self.audio, ptt_gpio=int(self.config["ptt_gpio"]),
			 manage_ptt_gpio=False)
		self.session.set_intercom_enabled(self._poc_enabled is True)
		self.session.event_callback = self._on_session_event
		if self._logout_requested:
			self.session.begin_logout()
		else:
			self.session.control_busy = True
		self.runtime.attach_session(self.session)
		self.runtime.start(session_threaded=False,
						 session_wake_callback=self._signal)
		print("[通信] 三个通信Worker和统一业务线程已启动")

	def _on_heartbeat_timer(self, timer):
		"""Timer1只记录心跳/重连到期并唤醒业务线程。"""
		if not self.running:
			return
		# Timer回调可能运行在中断上下文，不能调用带锁的_signal()。
		# 单个待处理标志会合并重复到期，实际业务仍由_run执行。
		if self._reconnect_pending:
			if self._reconnect_due:
				return
			self._reconnect_due = True
		else:
			if self._heartbeat_due:
				return
			self._heartbeat_due = True
		if not self._heartbeat_due and not self._reconnect_due:
			return
		self._signal_timer_isr()

	def _start_heartbeat_timer(self):
		self._stop_heartbeat_timer()
		self._heartbeat_due = False
		if Timer is None:
			print("[通信] TCP心跳定时器不可用")
			return False
		try:
			timer = Timer(Timer.Timer1)
			mode = timer.PERIODIC
			timer.start(period=HEARTBEAT_INTERVAL_MS, mode=mode,
						  callback=self._on_heartbeat_timer)
			self._heartbeat_timer = timer
			print("[通信] TCP心跳定时器已启动：周期={}秒".format(
				HEARTBEAT_INTERVAL_MS // 1000))
			return True
		except Exception as error:
			self._heartbeat_timer = None
			print("[通信] TCP心跳定时器启动失败：{}".format(error))
			return False

	def _start_reconnect_timer(self):
		"""复用 Timer1：断线期间每3秒只唤醒统一业务线程重连一次。"""
		self._stop_heartbeat_timer()
		self._reconnect_due = False
		if Timer is None:
			print("[通信] 自动重连定时器不可用")
			return False
		try:
			timer = Timer(Timer.Timer1)
			timer.start(period=RECONNECT_INTERVAL_MS, mode=timer.PERIODIC,
						callback=self._on_heartbeat_timer)
			self._heartbeat_timer = timer
			print("[通信] 自动重连定时器已启动：间隔={}秒".format(
				RECONNECT_INTERVAL_MS // 1000))
			return True
		except Exception as error:
			self._heartbeat_timer = None
			print("[通信] 自动重连定时器启动失败：{}".format(error))
			return False

	def _stop_heartbeat_timer(self):
		timer = self._heartbeat_timer
		self._heartbeat_timer = None
		self._heartbeat_due = False
		self._reconnect_due = False
		if timer is not None:
			try:
				timer.stop()
			except Exception:
				pass

	def _send_heartbeat_if_due(self):
		"""在统一业务线程中发送一次到期心跳并等待0x82应答。"""
		if not self._heartbeat_due:
			return
		if self.login_state not in (STATE_LOGGED_IN, STATE_JOINED):
			return
		if (self.runtime is None or not self.runtime.running or
				self.session is None):
			return
		# PTT抢麦、通话或释放事务期间延后P2心跳，避免打断P0/P1流程。
		if self.session.state != "idle":
			return

		count = self.request_count
		packet = build_frame(
			MSG_HEARTBEAT, count,
			build_heartbeat_payload(self.config["device_id"]))
		self._heartbeat_due = False
		self.request_count = (count + 1) & 0xFFFF
		self.session.next_request_count = self.request_count
		print("[通信] 自动发送TCP心跳：request_cnt={}".format(count))
		try:
			self._send_tcp_transaction(
				CMD_TCP_HEARTBEAT, packet, MSG_HEARTBEAT_ACK, count,
				"0x02心跳")
			print("[通信] 收到TCP心跳应答：request_cnt={}".format(
				(count + 1) & 0xFFFF))
		except Exception as error:
			print("[通信] TCP心跳应答超时：{}".format(error))
			self._schedule_reconnect("TCP心跳应答超时")

	def _upload_gnss(self, result):
		"""在统一业务线程内完成一次0x0D/0x8D定位事务。"""
		if (self.login_state not in (STATE_LOGGED_IN, STATE_JOINED) or
				self.runtime is None or not self.runtime.running or
				self._reconnect_pending):
			print("[定位] 丢弃过期采样结果：当前未登录或正在重连")
			return False
		fix = bool(result.get("fix"))
		raw_gga = result.get("raw_gga") or b""
		if isinstance(raw_gga, str):
			raw_gga = raw_gga.encode("ascii")
		else:
			raw_gga = bytes(raw_gga)
		if fix and not raw_gga:
			raise RuntimeError("有效定位缺少原始GGA")
		if not fix:
			if self._gnss_empty_uploaded:
				print("[定位] 本次登录已确认过空GGA，过滤重复无定位上报")
				return False
			raw_gga = b""
		count = self.request_count
		packet = build_frame(MSG_GNSS_UPLOAD, count,
			build_gnss_upload_payload(self.config["device_id"], raw_gga))
		self.request_count = (count + 1) & 0xFFFF
		if self.session is not None:
			self.session.next_request_count = self.request_count
		print("[定位] 准备发送0x0D：request_cnt={} gnss_len={} 定位={}".format(
			count, len(raw_gga), "有效" if fix else "首次无定位"))
		frame = self._send_tcp_transaction(
			CMD_GNSS_UPLOAD, packet, MSG_GNSS_UPLOAD_ACK, count,
			"0x0D定位上报")
		parse_gnss_upload_ack(frame["payload"])
		if fix:
			self._gnss_last_uploaded_fix = {
				"latitude": result.get("latitude"),
				"longitude": result.get("longitude"),
				"altitude": result.get("altitude"),
				"raw_gga": raw_gga,
			}
		else:
			self._gnss_empty_uploaded = True
		print("[定位] 收到0x8D，定位上报成功：request_cnt={}".format(
			(count + 1) & 0xFFFF))
		return True

	def _gnss_send_busy(self):
		_revision, snapshot = self.state.get_snapshot()
		return bool(
			self.session is not None and self.session.state != "idle" or
			snapshot["audio_tx_active"] or snapshot["audio_rx_active"] or
			snapshot["floor_state"] != "idle" or
			snapshot["single_state"] != "idle")

	def _process_gnss_result(self, result):
		if self._gnss_send_busy():
			self._gnss_deferred_result = dict(result)
			print("[定位] 对讲事务进行中，定位上报已延期")
			return False
		return self._upload_gnss(result)

	def _flush_deferred_gnss(self):
		if (self._gnss_deferred_result is None or self._gnss_send_busy() or
				self._reconnect_pending or
				self.login_state not in (STATE_LOGGED_IN, STATE_JOINED)):
			return False
		result = self._gnss_deferred_result
		if not self._enqueue("gnss_result", result):
			return False
		self._gnss_deferred_result = None
		print("[定位] 对讲结束，延期定位上报已恢复")
		return True

	def _release_runtime(self, close_audio=False):
		"""停止通信Worker和Socket；重连时保留本地音频按键能力。"""
		self._gnss_deferred_result = None
		if self.runtime is not None or self.tcp_socket is not None:
			print("[通信] 开始断开当前TCP/UDP连接并停止三个Worker")
		# 先失效旧回调，再停止阻塞 Worker，避免停止过程中的尾部事件
		# 误进入下一次连接。
		self._runtime_generation += 1
		if self.runtime is not None:
			self._clear_single("连接资源释放，临时组不可恢复")
			self.runtime.stop()
		elif self.tcp_socket is not None:
			try:
				self.tcp_socket.close()
			except Exception:
				pass
		if self.runtime is None and self.udp_socket is not None:
			try:
				self.udp_socket.close()
			except Exception:
				pass
		self.runtime = None
		self.session = None
		self._incoming_single = None
		if close_audio and self.audio is not None:
			self.audio.close()
			self.audio = None
		self.tcp_socket = None
		self.udp_socket = None
		self._prelogin_connected = False
		self._tcp_ready = False
		self.state.update_transport(False, False)
		self._active_tcp_transaction = None
		self._response_history = []
		self._response_history_set = set()
		print("[通信] 当前连接资源已释放，等待自动重连")
		self.single_group_id = None
		self._publish_single("idle")
		_revision, snapshot = self.state.get_snapshot()
		self.state.update_audio(False, False, snapshot["audio_volume"])
		self.state.update_poc(False, self.login_state, snapshot["login_error"],
			None if self.group_id is None else str(self.group_id),
			"disconnected", None, "idle", None)
		# 旧连接上的TCP帧和发送失败事件不能参与新连接事务。
		self._event_lock.acquire()
		try:
			self._events = []
		finally:
			self._event_lock.release()

	def _schedule_reconnect(self, reason):
		"""通信异常后保留警号和入组目标，进入自动恢复状态。"""
		if not self.running:
			return False
		prelogin = self.police_no is None and self._login_intent is None
		if (not prelogin and not self._auto_reconnect_enabled and
				self._login_intent is None):
			return False
		if self._reconnect_pending:
			return True
		self._reconnect_pending = True
		self._reconnect_due = False
		self._reconnect_reason = str(reason)
		self._reconnect_attempt = 0
		self._network_ready = False
		self._tcp_ready = False
		self.state.update_transport(False, False)
		self._connection_progress_seen = {"network": 0, "tcp": 0}
		self._lock.acquire()
		try:
			self._requests = [item for item in self._requests if item[0] not in (
				"login", "force_login", "join_group", "leave_group", "single_call",
				"dissolve_single", "refresh_directory", "hardware_key", "gnss_result")]
		finally:
			self._lock.release()
		print("[通信] 通信异常：{}，准备自动重连".format(
			self._reconnect_reason))
		print("[通信] 已进入断线恢复状态：Timer1将每3秒唤醒业务线程")
		self._release_runtime()
		if not prelogin:
			self._publish_login(STATE_RECONNECTING, self._reconnect_reason)
		self._start_reconnect_timer()
		return True

	def _reconnect_if_due(self):
		"""统一业务线程执行一次重连、重新登录和恢复入组。"""
		if not self._reconnect_pending or not self._reconnect_due:
			return False
		self._reconnect_due = False
		resume_login = self.police_no or self._login_intent
		resume_group = self.group_id if self._auto_reconnect_enabled else None
		self._reconnect_attempt += 1
		print("[通信] 收到Timer1重连唤醒：第{}次尝试".format(
			self._reconnect_attempt))
		print("[通信] 开始自动重连：原因={}".format(
			self._reconnect_reason))
		try:
			self._wait_network(True)
			self._connect_tcp_socket(True)
			if resume_login is None:
				if self._guest_mode:
					candidate = self._guest_resume_police_no
					if (candidate and not self._guest_auto_login_blocked and
							not self._guest_auto_login_attempted):
						self._guest_auto_login_attempted = True
						print("[通信][游客] 网络和TCP已恢复，自动恢复登录：警号={}".format(
							candidate))
						completed = self._login(
							candidate, auto_join_first=True)
						if completed is True:
							print("[通信][游客] 自动恢复登录成功，已切换为正常身份")
							return True
						# 业务拒绝或需要强制登录确认：停止本轮自动尝试，
						# 由UI提示用户是否返回登录页处理。
						self._guest_auto_login_blocked = True
						self.police_no = None
						self._login_intent = None
						self._reconnect_pending = False
						self._reconnect_reason = None
						self.state.publish_guest_relogin_prompt()
						print("[通信][游客] 自动恢复登录被拒绝，等待用户确认是否重新登录")
						return False
					if not candidate:
						self._reconnect_pending = False
						self._reconnect_reason = None
						self.state.publish_guest_relogin_prompt()
						print("[通信][游客] 无可恢复警号，等待用户确认是否重新登录")
						return False
				self._reconnect_pending = False
				self._reconnect_reason = None
				print("[通信] 登录前网络和TCP Socket自动恢复成功")
				return True
			print("[通信] 重连网络和TCP Socket成功，开始重新登录")
			if resume_group is None:
				completed = self._login(
					resume_login,
					auto_join_first=(self._auto_reconnect_enabled and
						not self._group_explicitly_left))
			else:
				completed = self._login(
					resume_login, auto_join_first=False,
					resume_group=resume_group)
			if completed is False:
				self._reconnect_pending = False
				self._reconnect_reason = None
				print("[通信] 重连登录需要用户确认强制登录，暂停自动恢复")
				return False
			if completed is LOGIN_REJECTED:
				self._reconnect_pending = False
				self._reconnect_reason = None
				print("[通信] 重连后服务器明确拒绝登录，停止自动登录")
				return False
			print("[通信] 重新登录成功")
			restored_group = (resume_group is not None and
					self.group_id is not None and
					int(self.group_id) == int(resume_group))
			if restored_group:
				print("[通信] 断线前组恢复成功：组ID={}".format(resume_group))
			elif self._poc_enabled is False:
				print("[通信][POC权限] 重连后权限为OFF，不恢复断线前组")
			elif self._group_explicitly_left:
				print("[通信][组呼] 保持主动退组状态，重连后不自动入组")
			self._reconnect_pending = False
			self._reconnect_reason = None
			if self.session is not None:
				self.session.control_busy = False
			print("[通信] 自动重连、重新登录{}成功".format(
				"并恢复入组" if restored_group else ""))
			return True
		except Exception as error:
			print("[通信] 自动重连失败：{}，等待下次重试".format(error))
			if self._guest_mode:
				# 传输异常不视为警号业务拒绝，下一轮完整恢复后允许重试。
				self.police_no = None
				self._login_intent = None
				self._guest_auto_login_attempted = False
			self._release_runtime()
			self._publish_login(STATE_RECONNECTING, str(error))
			self._start_reconnect_timer()
			return False

	def _sync_rtp_receive_context(self):
		if (self.runtime is None or self.session is None or
				not isinstance(self.runtime, CommunicationRuntime)):
			return
		group_id = self.session.active_audio_group_id
		item = self.session.occupied_groups.get(group_id)
		if group_id is None or item is None or int(item.get("udp_port", 0)) <= 0:
			self.runtime.clear_rtp_receive_context()
			return
		self.runtime.set_rtp_receive_context(
			group_id, self.session._call_type(group_id), item["udp_port"])

	def _on_session_event(self, event):
		kind = event["type"]
		payload = event["payload"]
		if self._logout_requested:
			# begin_logout产生的本地停播结果和随后到达的旧会话通知
			# 都不能再刷新对讲状态或覆盖“正在退出”弹框。
			return
		if self._server_logout_pending:
			return
		if kind in ("floor_request_sent", "floor_release_sent"):
			# PTT会话拥有自己的连续计数器；同步到控制器，避免心跳
			# 在抢麦/释放麦权后复用旧request_cnt。
			count = int(payload["request_cnt"])
			self.request_count = (count + 1) & 0xFFFF
			if kind == "floor_release_sent":
				call_type = payload["call_type"]
				if (call_type not in ("group", "single") and
						self._release_recovery is not None):
					call_type = self._release_recovery["call_type"]
				self._release_recovery = {
					"group_id": int(payload["group_id"]),
					"call_type": call_type,
				}
			print("[通信][{}] {}首次发送：组ID={} request_cnt={}，等待{}ms".format(
				"单呼" if payload["call_type"] == "single" else "组呼",
				"0x08抢麦" if kind == "floor_request_sent" else "0x09释放",
				payload["group_id"], count, TCP_INITIAL_WAIT_MS))
			self._publish_floor_event(
				"request_started" if kind == "floor_request_sent" else "self_released",
				"self", payload["call_type"], payload["group_id"])
			self._refresh_call_state()
			return
		_revision, snapshot = self.state.get_snapshot()
		if kind in ("floor_occupied", "floor_idle", "floor_granted",
				"floor_force_released", "group_cleared",
				"single_group_cleared", "communication_disconnected",
				"server_logout_requested"):
			self._sync_rtp_receive_context()
		if kind == "ptt_changed":
			print("[通信] PTT状态={}".format("按下" if payload else "松开"))
			self._refresh_call_state()
		elif kind == "floor_granted":
			group_id = self.session.floor_group_id
			call_type = ("single" if group_id is not None and
				group_id == self.single_group_id else "group")
			print("[通信][{}] 本机抢麦成功：组ID={}，获得服务器UDP端口={}".format(
				self._floor_type_text(), self.session.floor_group_id, payload))
			self._publish_floor_event(
				"self_granted", "self", call_type, group_id)
			self._refresh_call_state()
		elif kind == "floor_released":
			self._release_recovery = None
			self._release_recovery_attempt = 0
			print("[通信][{}] 0x89释放应答：组ID={} 结果码=0x{:02X}".format(
				"单呼" if payload["call_type"] == "single" else "组呼",
				payload["group_id"], payload["ack"]["release_status"]))
			self._publish_floor_event(
				"self_released", "self", payload["call_type"],
				payload["group_id"])
			self._refresh_call_state()
		elif kind == "single_invited":
			self._handle_single_invited(payload)
		elif kind == "single_dissolve_requested":
			self._handle_single_dissolve(payload)
		elif kind == "server_logout_requested":
			self._server_logout_pending = True
			self._auto_reconnect_enabled = False
			self.login_result = None
			self.force_login_stage = "idle"
			# 保留已排队的0x8F应答，不调用runtime.begin_logout()清空发送队列。
			if self.runtime is not None:
				self.runtime.clear_rtp_receive_context()
			if self.session is not None:
				self.session.begin_logout()
			self._publish_floor_event(
				"communication_ended", None, None, None)
			self._enqueue("server_logout", None)
			print("[通信] 收到服务器强制退出，0x8F应答已入队")
		elif kind in ("floor_occupied", "floor_idle"):
			if kind == "floor_occupied":
				self._request_screen_wake("收到麦权占用通知")
			if kind == "floor_occupied":
				print("[通信][{}] 收到远端抢麦通知包：组ID={}，获得服务器UDP端口={}".format(
					"单呼" if payload["call_type"] == "single" else
					("组呼" if payload["call_type"] == "group" else "未知组"),
					payload["group_id"], payload.get("udp_port")))
				self._publish_floor_event(
					"remote_occupied", "remote", payload["call_type"],
					payload["group_id"], payload.get("police_name"),
					payload.get("selected", False))
			else:
				print("[通信][{}] 远端空闲0x0C：组ID={}".format(
					"单呼" if payload["call_type"] == "single" else "组呼",
					payload["group_id"]))
				self._publish_floor_event(
					"remote_idle", "remote", payload["call_type"],
					payload["group_id"])
				resumed_group = self.session.active_audio_group_id
				if (resumed_group is not None and
						resumed_group != payload["group_id"] and
						resumed_group in self.session.occupied_groups):
					resumed = self.session.occupied_groups[resumed_group]
					self._publish_floor_event(
						"remote_occupied", "remote",
						"single" if resumed_group == self.single_group_id else "group",
						resumed_group, resumed.get("police_name"), True)
			self._refresh_call_state()
		elif kind == "floor_retry":
			print("[通信][麦权] {}重发第{}/{}次：request_cnt={}，下次等待{}ms".format(
				"0x09释放" if payload.get("phase") == "releasing_floor" else "0x08抢麦",
				payload.get("retry"), payload.get("max_retries"),
				payload.get("request_cnt"), payload.get("wait_ms")))
		elif kind in ("floor_force_released", "floor_timeout", "floor_request_failed",
				"floor_release_failed"):
			print("[通信][麦权] 事件={} 详情={}".format(kind, payload))
			if kind == "floor_timeout":
				print("[通信][麦权] {}重试耗尽：已重发{}次".format(
					"0x09释放" if payload.get("phase") == "releasing_floor"
					else "0x08抢麦", TCP_MAX_RETRIES))
			group_id = payload.get("group_id") if isinstance(payload, dict) else None
			call_type = (payload.get("call_type") if isinstance(payload, dict)
						 else None)
			if call_type not in ("group", "single"):
				call_type = ("single" if group_id is not None and
					group_id == self.single_group_id else "group")
			if kind == "floor_request_failed":
				event_type = "request_failed"
			elif kind == "floor_timeout" and payload.get("phase") == "requesting_floor":
				event_type = "request_failed"
			elif kind == "floor_release_failed":
				event_type = "release_failed"
			else:
				event_type = "self_released"
			if kind == "floor_force_released":
				self._release_recovery = None
				self._release_recovery_attempt = 0
			elif kind == "floor_release_failed":
				self._release_recovery = None
				self._release_recovery_attempt = 0
			self._publish_floor_event(
				event_type, "self", call_type, group_id,
				None, False)
			self._refresh_call_state()
			if kind == "floor_timeout" and not self._release_compensation_active:
				self._schedule_reconnect("{}重发耗尽".format(
					"0x09释放麦权" if payload.get("phase") == "releasing_floor"
					else "0x08请求麦权"))
		elif kind in ("server_request_conflict", "floor_request_blocked",
				"floor_ack_ignored", "command_rejected", "ptt_worker_error",
				"intercom_event_ignored"):
			print("[通信][会话] 事件={} 详情={}".format(kind, payload))
		elif kind == "floor_release_deferred":
			group_id = self.session.floor_group_id
			self._publish_floor_event(
				"request_cancelled", "self",
				"single" if group_id == self.single_group_id else "group",
				group_id)
			print("[通信][会话] PTT已松开，等待迟到0x88后自动释放")
		elif kind == "communication_disconnected":
			inner_type = "未知"
			inner_error = None
			if isinstance(payload, dict):
				inner_type = payload.get("type", "未知")
				inner_error = payload.get("payload")
			print("[通信] TCP异常/断连事件：类型={} 错误={}".format(
				inner_type, inner_error))
			self._publish_floor_event(
				"communication_ended", None, None, None)
			self._schedule_reconnect("TCP接收断开：{}".format(inner_type))

	def _floor_type_text(self):
		return ("单呼" if self.session is not None and
			self.session.floor_group_id == self.single_group_id and
			self.single_group_id is not None else "组呼")

	def _refresh_call_state(self):
		if self.session is None:
			return
		_revision, snapshot = self.state.get_snapshot()
		self.state.update_poc(
			self.runtime is not None and self.runtime.running,
			self.login_state, snapshot["login_error"],
			None if self.group_id is None else str(self.group_id),
			self.session.state, self.session.floor_group_id,
			snapshot["single_state"], snapshot["single_peer"])
		self.state.update_audio(
			self.session.state == "transmitting",
			self.session.active_audio_group_id is not None,
			snapshot["audio_volume"])
		_revision, current = self.state.get_snapshot()
		if (self._gnss_sampling_started and
				current["floor_state"] == "idle" and
				current["single_state"] == "idle" and
				not current["audio_tx_active"] and
				not current["audio_rx_active"]):
			self._flush_deferred_gnss()
			# 若30秒截止点落在通话中，通话结束后立即唤醒低优先级
			# Worker；未到期时该Worker只检查截止时间，不读取GNSS。
			self._request_gnss_sample(False)

	def _publish_single(self, state, peer=None, error=None):
		self.state.update_single_call(state, peer, self.single_group_id, error)
		self.messages.publish(MSG_SINGLE_CALL, {
			"state": state, "person_key": peer,
			"group_id": self.single_group_id, "error": error}, 1)
		print("[通信][单呼] 状态={} 成员={} 临时组ID={} 错误={}".format(
			state, peer, self.single_group_id, error))

	def _clear_single(self, reason):
		old_group_id = self.single_group_id
		if self.session is not None:
			self.session.clear_single()
		self.single_group_id = None
		self._incoming_single = None
		self._publish_single("idle")
		if old_group_id is not None:
			self._publish_floor_event(
				"call_ended", None, "single", old_group_id)
		self._refresh_call_state()
		print("[通信][单呼] 已清理：临时组ID={} 原因={}".format(old_group_id, reason))

	def _handle_single_dissolve(self, request):
		group_id = int(request["group_id"])
		print("[通信][单呼] 收到服务器0x06解散：临时组ID={} request_cnt={}".format(
			group_id, request["request_cnt"]))
		valid = (request["device_id"] == self.config["device_id"].encode("ascii") and
			self.police_no is not None and
			request["police_no"] == self.police_no.encode("ascii") and
			self.single_group_id == group_id)
		if self.session is None:
			return
		# 解散发生时先停止对应单呼，不能继续向已经失效的临时组发音频。
		if valid:
			self._clear_single("服务器主动解散")
		accepted = self.session.submit_server_response(
			MSG_DISSOLVE_SINGLE, request["request_cnt"], MSG_DISSOLVE_SINGLE_ACK,
			build_dissolve_single_ack_payload(
				self.config["device_id"], 0 if valid else 1), retain_result=True)
		print("[通信][单呼] 0x86被动解散应答：校验={} 入队={}".format(valid, accepted))

	def _handle_single_invited(self, invited):
		"""处理服务器0x05邀请；空闲自动接受，忙碌时按协议拒绝。"""
		source_police_no = invited["src_police_no"].decode("ascii")
		group_id = int(invited["group_id"])
		request_count = int(invited["request_cnt"])
		if (len(source_police_no) != 7 or source_police_no[0] not in ("M", "F") or
				not source_police_no[1:].isdigit() or group_id == 0):
			raise ValueError("0x05来源警号或临时组ID无效")
		_revision, snapshot = self.state.get_snapshot()
		matched_key = source_police_no
		matched_name = None
		for person in snapshot["person_items"]:
			person_police_no = str(person.get("police_code") or
				person.get("key") or "").strip()
			if person_police_no == source_police_no:
				matched_key = str(person.get("key") or source_police_no)
				matched_name = str(person.get("name") or "")
				break
		print("[通信] 收到0x05单呼邀请：来源警号={} 临时组ID={}".format(
			source_police_no, group_id))
		if matched_name:
			print("[通信] 单呼邀请来源已匹配成员：{}（{}）".format(
				matched_name, matched_key))
		else:
			print("[通信] 单呼邀请来源不在当前名单：警号={}".format(
				source_police_no))
		if self._ota_busy:
			answer_status = 0x04
			print("[通信][OTA] 升级中，已拒绝单呼邀请")
		elif self._poc_enabled is not True:
			answer_status = 0x04
			print("[通信][POC权限] 拒绝单呼邀请：POC功能未开通或权限尚未确认")
		elif (self.session is not None and
				(self.group_id in self.session.occupied_groups or
				 self.session.state in (
					 "requesting_floor", "transmitting", "releasing_floor"))):
			answer_status = 0x02
		elif self._single_busy or self.single_group_id is not None:
			answer_status = 0x04
		elif self.session is not None and self.session.control_busy:
			answer_status = 0x04
		else:
			answer_status = 0x00
		if (not self.police_no or self.session is None or
				self.login_state not in (STATE_LOGGED_IN, STATE_JOINED)):
			print("[通信] 当前未登录，无法发送0x85单呼邀请应答")
			return
		if not self.session.submit_server_response(
				MSG_SINGLE_INVITED, request_count, MSG_SINGLE_INVITED_ACK,
				build_single_invited_ack_payload(
					self.config["device_id"], self.police_no, group_id, answer_status)):
			print("[通信] 0x85单呼邀请应答入队失败")
			return
		print("[通信] 0x85单呼邀请应答入队：状态=0x{:02X} request_cnt={}".format(
			answer_status, (request_count + 1) & 0xFFFF))
		if answer_status != 0x00:
			self.messages.publish(MSG_SINGLE_CALL, {
				"state": "rejected",
				"police_no": source_police_no,
				"group_id": group_id,
				"answer_status": answer_status,
			}, 1)
			return
		self.single_group_id = group_id
		self._incoming_single = {
			"request_cnt": request_count,
			"police_no": source_police_no,
			"group_id": group_id,
			"person_key": matched_key,
			"answer_status": answer_status,
		}
		self.session.configure_single(group_id, self.request_count)
		self._publish_single("active", matched_key)
		print("[通信] 已接受单呼邀请：成员={} 临时组ID={}".format(
			matched_name or source_police_no, group_id))

	def _recover_floor_release(self):
		"""登录恢复后优先补偿旧连接上未确认的0x09释放。"""
		if self._release_recovery is None:
			return True
		if self.session is None or self.runtime is None or not self.runtime.running:
			raise RuntimeError("释放麦权补偿时通信运行时不可用")
		info = dict(self._release_recovery)
		self._release_recovery_attempt += 1
		attempt = self._release_recovery_attempt
		print("[通信][麦权] 开始跨连接释放补偿：第{}/{}次 组ID={}".format(
			attempt, RELEASE_RECOVERY_MAX_ATTEMPTS, info["group_id"]))
		self._release_compensation_active = True
		try:
			if not self.session.start_release_compensation(
					info["group_id"], self.request_count):
				raise RuntimeError("无法启动释放麦权补偿事务")
			self._finish_floor_transaction()
		finally:
			self._release_compensation_active = False
		if self._release_recovery is None:
			if self.session is not None and self.session.floor_error is not None:
				print("[通信][麦权] 释放补偿被服务器拒绝：{}".format(
					self.session.floor_error))
				return False
			print("[通信][麦权] 跨连接释放补偿成功")
			return True
		if attempt < RELEASE_RECOVERY_MAX_ATTEMPTS:
			reason = "第{}/{}次释放麦权补偿未确认".format(
				attempt, RELEASE_RECOVERY_MAX_ATTEMPTS)
			self._schedule_reconnect(reason)
			raise TCPReconnectScheduled(reason)
		print("[通信][麦权] 释放补偿达到上限，停止跨连接重试")
		self._release_recovery = None
		self._release_recovery_attempt = 0
		self._publish_floor_event(
			"release_failed", "self", info["call_type"], info["group_id"])
		self._publish_error("释放麦权失败")
		return False

	def _complete_login(self, auto_join_first=True, resume_group=None):
		"""登录或强制登录成功后的唯一公共收尾路径。"""
		print("[通信] 登录应答成功")
		self._guest_mode = False
		self._guest_resume_police_no = None
		self._guest_auto_login_blocked = False
		self._guest_auto_login_attempted = False
		self.state.update_identity_mode("normal")
		if self.persistent_config is not None:
			self.persistent_config.update(police_no=self.police_no)
		self._auto_reconnect_enabled = True
		self.force_login_stage = "idle"
		self._update_identity_from_cache(self.police_no)
		self._publish_login(STATE_LOGGED_IN)
		self._login_intent = None
		self._reconnect_pending = False
		self._reconnect_due = False
		self._reconnect_reason = None
		self._gnss_sampling_started = False
		self._gnss_empty_uploaded = False
		self._gnss_last_uploaded_fix = None
		self._gnss_deferred_result = None
		self._recover_floor_release()
		self._start_heartbeat_timer()
		self._prepare_login_directory(auto_join_first, resume_group)
		self._gnss_sampling_started = True
		if self._request_gnss_sample(True):
			print("[定位] 登录成功，已请求首次GNSS采样")
		else:
			print("[定位] GNSS采样服务未就绪，首次采样请求未提交")

	def _login(self, police_no, auto_join_first=True, resume_group=None,
			   transaction_id=None):
		if transaction_id is None:
			transaction_id = self._active_login_transaction_id
			if transaction_id <= 0:
				self._login_transaction_id += 1
				transaction_id = self._login_transaction_id
		else:
			transaction_id = int(transaction_id)
		self._active_login_transaction_id = transaction_id
		if not self._reconnect_pending:
			self._group_explicitly_left = False
			self._set_poc_access(None)
		self.police_no = str(police_no)
		self._login_intent = self.police_no
		self.login_result = None
		self.force_login_stage = "idle"
		self._ensure_runtime()
		if self.session is not None:
			self.session.police_no = self.police_no
		# 只有TCP Socket和三个通信Worker均已就绪，才进入真正登录阶段。
		self._publish_login(STATE_LOGGING_IN, transaction_id=transaction_id)
		print("[通信] {}登录开始：警号={} request_cnt={}".format(
			"重新" if self._reconnect_pending else "首次",
			self.police_no, self.request_count))
		count = self.request_count
		packet = build_frame(MSG_LOGIN, count, build_login_payload(
			self.config["device_id"], self.police_no))
		print("[通信] 准备发送0x01登录并等待0x81应答")
		frame = self._send_tcp_transaction(
			CMD_LOGIN, packet, MSG_LOGIN_ACK, count, "0x01登录")
		result = parse_login_ack(frame["payload"])["login_result"]
		self.request_count = (count + 1) & 0xFFFF
		if self.session is not None:
			self.session.next_request_count = self.request_count
		self.login_result = result
		print("[通信] 收到登录应答：结果码=0x{:02X}".format(result))
		if result == 0x01:
			self.force_login_stage = "prompt"
			self._force_login_auto_join = bool(auto_join_first)
			self._force_login_resume_group = resume_group
			self._publish_login(STATE_FORCE_LOGIN_REQUIRED)
			print("[通信] 警号已登录，等待用户确认是否强制登录")
			return False
		if result != 0:
			if result == 0x03:
				error = "设备ID未注册"
			elif result == 0x04:
				error = "警号未注册"
			else:
				error = "登录失败"
			# 已收到匹配本次request_cnt的0x81，TCP事务正常闭环。
			# 明确业务拒绝只结束登录意图，保留现有TCP/UDP和Worker。
			self._login_intent = None
			self._auto_reconnect_enabled = False
			self.police_no = None
			self._set_poc_access(None)
			self._reconnect_pending = False
			self._reconnect_due = False
			self._reconnect_reason = None
			self._stop_heartbeat_timer()
			self._publish_login(STATE_ERROR, error, transaction_id)
			self._publish_error(error)
			print("[通信] 登录被服务器拒绝：{}，保留当前TCP/UDP连接，停止自动登录".format(
				error))
			return LOGIN_REJECTED
		self._complete_login(auto_join_first, resume_group)
		return True

	def _force_login(self):
		if (self.force_login_stage != "prompt" or self.police_no is None or
				self.runtime is None or not self.runtime.running):
			raise RuntimeError("当前状态不能强制登录")
		self.force_login_stage = "waiting"
		self.login_result = None
		self._publish_login(STATE_FORCE_LOGGING_IN)
		count = self.request_count
		packet = build_frame(MSG_FORCE_LOGIN, count, build_force_login_payload(
			self.config["device_id"], self.police_no))
		print("[通信] 准备发送0x0E强制登录并等待0x8E应答")
		try:
			frame = self._send_tcp_transaction(
				CMD_FORCE_LOGIN, packet, MSG_FORCE_LOGIN_ACK, count,
				"0x0E强制登录")
		except RuntimeError as error:
			if "超时" in str(error):
				raise RuntimeError("转机登录超时")
			raise
		result = parse_force_login_ack(frame["payload"])["login_result"]
		self.request_count = (count + 1) & 0xFFFF
		if self.session is not None:
			self.session.next_request_count = self.request_count
		self.login_result = result
		print("[通信] 收到0x8E强制登录应答：结果码=0x{:02X}".format(result))
		if result != 0:
			self.force_login_stage = "failed"
			if result == 0x01:
				raise RuntimeError("无法转机登录")
			raise RuntimeError("转机登录失败")
		self.force_login_stage = "success"
		self._complete_login(
			self._force_login_auto_join, self._force_login_resume_group)
		self._force_login_resume_group = None

	def _cancel_force_login(self):
		self.force_login_stage = "cancelled"
		self.login_result = 0x01
		self._force_login_resume_group = None
		self._publish_login(STATE_IDLE)
		print("[通信] 用户取消强制登录，保留当前输入警号")

	def _join_group(self, group_id, force=False):
		if self._poc_enabled is not True:
			raise RuntimeError("POC功能未开通或权限尚未确认，不能入组")
		if (self.runtime is None or not self.runtime.running or
				self.session is None or self.police_no is None):
			raise RuntimeError("通信未连接或尚未登录，不能入组")
		group_id = group_id_to_uint32(group_id)
		if (not force and self.group_id is not None and
				int(self.group_id) == int(group_id)):
			self.session.prefer_group()
			print("[通信] 已在目标组，忽略重复入组：组ID={}".format(group_id))
			return
		# 新连接的session没有旧组订阅；重连恢复不向新Socket重复退旧组。
		if self.session.group_id is not None and self.session.group_id != group_id:
			old_group_id = self.session.group_id
			print("[通信][组呼] 切组开始：旧组={} 新组={}，先确认退出旧组".format(
				old_group_id, group_id))
			self._leave_group()
			print("[通信][组呼] 旧组={}已退出，开始加入新组={}".format(old_group_id, group_id))
		if self.runtime is None or not self.runtime.running or self.session is None:
			raise RuntimeError("入组前连接已失效")
		count = self.request_count
		print("[通信] 入组开始：组ID={} request_cnt={}".format(
			group_id, count))
		packet = build_frame(MSG_JOIN_GROUP, count, build_join_group_payload(
			self.config["device_id"], group_id, self.police_no))
		self.request_count = (count + 1) & 0xFFFF
		self.session.next_request_count = self.request_count
		frame = self._send_tcp_transaction(
			CMD_JOIN_GROUP, packet, MSG_JOIN_GROUP_ACK, count, "0x03入组")
		ack = parse_join_group_ack(frame["payload"])
		print("[通信] 收到入组应答：组ID={} 结果码=0x{:02X}".format(
			group_id, ack["join_result"]))
		if (ack["device_id"] != self.config["device_id"].encode("ascii") or
				ack["police_no"] != self.police_no.encode("ascii") or
				ack["group_id"] != group_id or ack["join_result"] != 0):
			raise RuntimeError("入组应答拒绝或设备ID、警号、组ID不匹配，结果码=0x{:02X}".format(
				ack["join_result"]))
		self.group_id = int(group_id)
		self._group_explicitly_left = False
		self.session.configure_group(self.group_id, self.request_count)
		self._publish_login(STATE_JOINED)
		print("[通信][组呼] 入组成功：当前组ID={}，UI已更新".format(self.group_id))

	def _leave_group(self):
		group_id = self.group_id
		if group_id is None:
			print("[通信][组呼] 当前无普通组，忽略重复退组")
			return
		if (self.runtime is None or not self.runtime.running or self.session is None or
				self.police_no is None or
				self.login_state not in (STATE_LOGGED_IN, STATE_JOINED)):
			raise RuntimeError("当前连接或登录状态不可退组")
		session = self.session
		print("[通信][组呼] 退组开始：组ID={}".format(group_id))
		if session.request_stop_group():
			print("[通信][组呼] 先停止组呼音频并等待释放麦权")
			self._finish_floor_transaction()
			if session.floor_error is not None:
				raise RuntimeError("退组前麦权释放未确认：{}".format(session.floor_error))
		if self.runtime is None or not self.runtime.running or self.session is not session:
			raise RuntimeError("退组前连接已失效")
		count = self.request_count
		packet = build_frame(MSG_LEAVE_GROUP, count, build_leave_group_payload(
			self.config["device_id"], self.police_no, group_id))
		self.request_count = (count + 1) & 0xFFFF
		session.next_request_count = self.request_count
		print("[通信][组呼] 0x10退组命令入队：组ID={} request_cnt={}".format(group_id, count))
		frame = self._send_tcp_transaction(
			CMD_LEAVE_GROUP, packet, MSG_LEAVE_GROUP_ACK, count, "0x10退组")
		ack = parse_leave_group_ack(frame["payload"])
		print("[通信][组呼] 收到0x90退组应答：组ID={} 结果码=0x{:02X}".format(
			ack["group_id"], ack["join_result"]))
		if (ack["device_id"] != self.config["device_id"].encode("ascii") or
				ack["police_no"] != self.police_no.encode("ascii") or
				ack["group_id"] != group_id or ack["join_result"] != 0):
			raise RuntimeError("0x90退组应答拒绝或设备ID、警号、组ID不匹配")
		session.clear_group()
		self._publish_floor_event("call_ended", None, "group", group_id)
		resumed_group = session.active_audio_group_id
		if (resumed_group is not None and resumed_group in session.occupied_groups):
			resumed = session.occupied_groups[resumed_group]
			self._publish_floor_event(
				"remote_occupied", "remote",
				"single" if resumed_group == self.single_group_id else "group",
				resumed_group, resumed.get("police_name"), True)
		self.group_id = None
		self._group_explicitly_left = True
		self._refresh_call_state()
		self._publish_login(STATE_LOGGED_IN)
		self.messages.publish(MSG_GROUP_CHANGED, None)
		print("[通信][组呼] 退组成功：原组ID={}，已清理组状态和UI选择，单呼保持不变".format(group_id))

	def _single_call(self, person_key):
		if self._poc_enabled is not True:
			raise RuntimeError("POC功能未开通或权限尚未确认，不能发起单呼")
		if self.runtime is None or not self.runtime.running:
			raise RuntimeError("通信未连接，不能发起单呼")
		if self.police_no is None:
			raise RuntimeError("尚未登录，不能发起单呼")
		if self.login_state not in (STATE_LOGGED_IN, STATE_JOINED):
			raise RuntimeError("当前登录状态不可发起单呼")
		_revision, snapshot = self.state.get_snapshot()
		if self.single_group_id is not None and snapshot["single_peer"] == str(person_key):
			self.session.configure_single(self.single_group_id, self.request_count)
			print("[通信][单呼] 已是当前单呼成员，忽略重复邀请：{}".format(person_key))
			return
		person = None
		for item in snapshot["person_items"]:
			if str(item.get("key")) == str(person_key):
				person = item
				break
		if person is None:
			raise RuntimeError("找不到单呼成员：{}".format(person_key))
		if person.get("is_self"):
			raise RuntimeError("不能呼叫本人")
		if not person.get("online"):
			raise RuntimeError("单呼成员不在线：{}".format(person_key))
		target_device_id = str(person.get("device_id") or "").strip()
		target_police_no = str(person.get("police_code") or person_key).strip()
		if not target_device_id or not target_police_no:
			raise RuntimeError("单呼成员缺少设备ID或警号")
		if self.single_group_id is not None:
			print("[通信][单呼] 切换成员，先解散旧临时组={}".format(self.single_group_id))
			self._dissolve_single()
		if self.runtime is None or self.session is None:
			raise RuntimeError("单呼邀请前连接已经失效")
		count = self.request_count
		packet = build_frame(
			MSG_INVITE_SINGLE, count,
			build_invite_single_payload(
				self.config["device_id"], target_device_id,
				self.police_no, target_police_no))
		self.request_count = (count + 1) & 0xFFFF
		self.session.next_request_count = self.request_count
		self._publish_single("inviting")
		print("[通信] 0x04单呼邀请入队：成员={} request_cnt={}".format(
			person_key, count))
		frame = self._send_tcp_transaction(
			CMD_SINGLE_CALL, packet, MSG_INVITE_SINGLE_ACK, count,
			"0x04单呼邀请")
		ack = parse_invite_single_ack(frame["payload"])
		print("[通信] 收到0x84单呼第一阶段应答：成员={} ack_num={} 状态=0x{:02X}".format(
			person_key, ack["ack_num"], ack["invite_status"]))
		if ack["ack_num"] != 1 or ack["invite_status"] != 0x06:
			raise RuntimeError("单呼第一阶段应答无效：ack_num={} 状态=0x{:02X}".format(
				ack["ack_num"], ack["invite_status"]))
		self._publish_single("invite_accepted")
		print("[通信] 单呼邀请已受理，等待对端应答")
		deadline = utime.ticks_add(
			utime.ticks_ms(), SINGLE_CALL_FINAL_RESPONSE_TIMEOUT_MS)
		while True:
			remaining = utime.ticks_diff(deadline, utime.ticks_ms())
			if remaining <= 0:
				raise RuntimeError("单呼最终应答超时")
			frame = self._take_frame(MSG_INVITE_SINGLE_ACK, count, remaining)
			if frame is None:
				continue
			ack = parse_invite_single_ack(frame["payload"])
			if ack["ack_num"] == 1 and ack["invite_status"] == 0x06:
				print("[通信][单呼] 重复受理应答，仍等待最终结果（不重发邀请）")
				continue
			break
		print("[通信] 收到0x84单呼最终应答：成员={} ack_num={} 状态=0x{:02X} 临时组ID={}".format(
			person_key, ack["ack_num"], ack["invite_status"], ack["group_id"]))
		if (ack["ack_num"] != 2 or ack["invite_status"] != 0 or
				ack["group_id"] in (None, 0)):
			raise RuntimeError("单呼最终应答失败：ack_num={} 状态=0x{:02X}".format(
				ack["ack_num"], ack["invite_status"]))
		self.single_group_id = int(ack["group_id"])
		self.session.configure_single(self.single_group_id, self.request_count)
		self._publish_single("active", str(person_key))
		print("[通信] 单呼建立成功：成员={} 临时组ID={}".format(
			person_key, self.single_group_id))

	def _finish_floor_transaction(self):
		"""复用当前业务线程等待麦权应答；空闲仍永久阻塞，不新增定时线程。"""
		while (self.running and self.session is not None and
				self.session.state in ("requesting_floor", "releasing_floor")):
			self.session.poll_once()
			send_error = self._take_tcp_send_failure()
			if send_error is not None:
				phase = self.session.state
				group_id = self.session.floor_group_id
				self.session._finish_floor_locally()
				self.session.floor_error = "TCP发送失败：{}".format(send_error)
				print("[通信][麦权] TCP发送异常，立即结束{}等待：{}".format(
					phase, send_error))
				self._schedule_reconnect("麦权TCP发送失败：{}".format(send_error))
				if phase == "releasing_floor" and self._release_recovery is None:
					self._release_recovery = {
						"group_id": group_id,
						"call_type": "single" if group_id == self.single_group_id else "group",
					}
				break
			utime.sleep_ms(5)

	def _dissolve_single(self):
		group_id = self.single_group_id
		if group_id is None:
			print("[通信][单呼] 无已建立单呼，忽略解散")
			self._publish_single("idle")
			return
		if self.session is None or self.runtime is None:
			raise RuntimeError("通信未连接，无法解散单呼")
		_revision, snapshot = self.state.get_snapshot()
		self._publish_single("dissolving", snapshot["single_peer"])
		self.session.request_stop_single()
		self._finish_floor_transaction()
		if self.single_group_id != group_id:
			print("[通信][单呼] 等待释放期间服务器已解散，无需重复发0x06")
			return
		if self.runtime is None or not self.runtime.running:
			raise RuntimeError("解散前连接已失效")
		count = self.request_count
		packet = build_frame(MSG_DISSOLVE_SINGLE, count,
			build_dissolve_single_payload(
				self.config["device_id"], self.police_no, group_id))
		self.request_count = (count + 1) & 0xFFFF
		self.session.next_request_count = self.request_count
		print("[通信][单呼] 0x06解散命令入队：临时组ID={} request_cnt={}".format(
			group_id, count))
		frame = self._send_tcp_transaction(
			CMD_DISSOLVE_SINGLE_CALL, packet, MSG_DISSOLVE_SINGLE_ACK,
			count, "0x06解散单呼", until_single_cleared=group_id)
		if frame is None:
			print("[通信][单呼] 双方同时解散，已按服务器请求完成本机清理")
			return
		ack = parse_dissolve_single_ack(frame["payload"])
		print("[通信][单呼] 收到0x86主动解散应答：结果=0x{:02X}".format(ack["release_result"]))
		if (ack["device_id"] != self.config["device_id"].encode("ascii") or
				ack["release_result"] != 0):
			raise RuntimeError("0x86解散应答失败或设备ID不匹配")
		self._clear_single("主动解散成功")

	def _logout(self):
		print("[通信][退出] 开始执行退出事务")
		result = "no_connection"
		count = self.request_count
		packet = None
		try:
			self._directory_session_generation += 1
			self._login_directory_context = None
			self._http_pending_context = None
			if self.police_no is not None:
				packet = build_frame(MSG_LOGOUT, count,
					build_logout_payload(self.config["device_id"], self.police_no))
				try:
					ack_result = self._logout_packet_attempt(packet, count)
					result = "ack_{}".format(ack_result)
				except Exception as error:
					print("[通信][退出] 当前TCP退出重试失败：{}".format(error))
					self._release_runtime()
					try:
						self._wait_network(True)
						self._connect_tcp_socket(True)
						self._ensure_runtime()
						self.runtime.begin_logout()
						ack_result = self._logout_packet_attempt(packet, count)
						result = "reconnect_ack_{}".format(ack_result)
					except Exception as reconnect_error:
						print("[通信][退出] 专用重连或重连后退出失败：{}".format(
							reconnect_error))
						result = "failed"
			else:
				print("[通信][退出] 当前没有可用登录警号，跳过0x0F")
		finally:
			self._active_tcp_transaction = None
			self._close_resources()
			self.police_no = None
			self.group_id = None
			self._group_explicitly_left = False
			self.single_group_id = None
			self.request_count = 1
			self.login_state = STATE_IDLE
			self.login_result = None
			self.force_login_stage = "idle"
			self._reconnect_pending = False
			self._reconnect_due = False
			self._reconnect_reason = None
			self._incoming_single = None
			self._set_poc_access(None)
			self.state.update_poc(False, "idle", None, None, "idle", None,
				"idle", None)
			self.state.update_login_transaction("idle", None, None, "idle")
			print("[通信][退出] 退出事务结束：结果={}".format(result))
			self._finish_logout_transaction(result)
			if self.running:
				self._connection_progress_seen = {"network": 0, "tcp": 0}
				self._enqueue("prepare_connection", None)

	def _logout_packet_attempt(self, packet, count):
		self.request_count = (count + 1) & 0xFFFF
		frame = self._send_tcp_transaction(
			CMD_LOGOUT, packet, MSG_LOGOUT_ACK, count,
			"0x0F退出登录", logout=True)
		result = parse_logout_ack(frame["payload"])["logout_result"]
		print("[通信] 收到0x8F退出登录应答：结果码=0x{:02X}".format(result))
		if result != 0:
			print("[通信][退出] 服务器已明确应答失败，不再重发，继续本地退出")
		return result

	def _server_logout(self):
		"""等待0x8F实际发送完成，再做本机资源和选择状态清理。"""
		self._directory_session_generation += 1
		self._login_directory_context = None
		self._http_pending_context = None
		self._auto_reconnect_enabled = False
		if self.runtime is not None:
			deadline = utime.ticks_add(utime.ticks_ms(), 200)
			while (not self._server_logout_ack_sent and
					utime.ticks_diff(deadline, utime.ticks_ms()) > 0):
				utime.sleep_ms(5)
			if self._server_logout_ack_sent:
				print("[通信] 0x8F强制退出应答发送完成")
			else:
				print("[通信] 0x8F强制退出应答未在200ms内确认发送，继续本地清理")
		self._close_resources()
		self.police_no = None
		self.group_id = None
		self.single_group_id = None
		self.request_count = 1
		self._reconnect_pending = False
		self._reconnect_due = False
		self._reconnect_reason = None
		self._incoming_single = None
		self._set_poc_access(None)
		self._server_logout_pending = False
		self.login_state = STATE_ERROR
		self.login_result = None
		self.force_login_stage = "idle"
		self.state.update_poc(
			False, STATE_ERROR, "设备已被强制退出", None,
			"idle", None, "idle", None)
		self.state.update_login_transaction(
			STATE_ERROR, None, "设备已被强制退出", "idle")
		self._login_intent = None
		self._request_screen_wake("账号强制下线")
		self.state.publish_forced_logout()
		if self.running:
			self._connection_progress_seen = {"network": 0, "tcp": 0}
			self._enqueue("prepare_connection", None)

	def _close_resources(self):
		self._stop_heartbeat_timer()
		self._release_runtime(close_audio=True)

	def _run(self):
		try:
			while self.running:
				self._wake.acquire()
				self._lock.acquire()
				try:
					self._wake_pending = False
				finally:
					self._lock.release()
				if not self.running:
					break
				if self.session is not None:
					self.session.poll_once()
				if not self._server_logout_pending:
					self._finish_floor_transaction()
				send_error = self._take_tcp_send_failure()
				if send_error is not None:
					if self._logout_requested or self._server_logout_pending:
						print("[通信][退出] 忽略旧业务发送异常：{}".format(send_error))
					else:
						print("[通信] TCP发送线程上报致命异常：{}".format(send_error))
						self._schedule_reconnect("TCP发送失败：{}".format(send_error))
				if not self._logout_requested and not self._server_logout_pending:
					self._reconnect_if_due()
					self._send_heartbeat_if_due()
				http_result = self._take_http_result()
				if http_result is not None and not self._server_logout_pending:
					if self.session is not None:
						self.session.control_busy = True
					try:
						self._handle_http_result(http_result)
					except TCPReconnectScheduled as error:
						print("[通信][HTTP] 应用名单时连接已交由自动重连：{}".format(error))
					except Exception as error:
						print("[通信][HTTP] 应用名单失败：{}".format(error))
						self._publish_error(error)
					finally:
						if self.session is not None:
							self.session.control_busy = False
						self._start_pending_http_request()
				item = self._take_request()
				if item is None:
					continue
				is_single = item[0] in ("single_call", "dissolve_single")
				self._lock.acquire()
				try:
					self._login_busy = item[0] in ("login", "force_login")
					self._single_busy = is_single
					self._directory_refresh_busy = item[0] == "refresh_directory"
					self._leave_group_busy = item[0] == "leave_group"
				finally:
					self._lock.release()
				if self.session is not None:
					self.session.control_busy = True
				try:
					if item[0] == "prepare_connection":
						try:
							self._prepare_prelogin_connection(False)
						except Exception as error:
							print("[通信] 登录前预连接失败：{}".format(error))
							self._schedule_reconnect("登录前预连接失败：{}".format(error))
					elif item[0] == "login":
						login_payload = item[1]
						if isinstance(login_payload, dict):
							login_police_no = login_payload.get("police_no")
							login_transaction_id = login_payload.get("transaction_id")
						else:
							login_police_no = login_payload
							login_transaction_id = self._active_login_transaction_id
						if self._reconnect_pending:
							self._login_intent = str(login_police_no)
							print("[通信] 正在重连，登录意图已保留，等待TCP恢复后执行")
						else:
							self.login_result = None
							self.force_login_stage = "idle"
							self._login(
								login_police_no,
								transaction_id=login_transaction_id)
					elif item[0] == "force_login":
						self._force_login()
					elif item[0] == "cancel_force_login":
						self._cancel_force_login()
					elif item[0] == "join_group":
						self._join_group(item[1])
					elif item[0] == "leave_group":
						self._leave_group()
					elif item[0] == "single_call":
						self._single_call(item[1])
					elif item[0] == "dissolve_single":
						self._dissolve_single()
					elif item[0] == "logout":
						self._logout()
					elif item[0] == "server_logout":
						self._server_logout()
					elif item[0] == "refresh_directory":
						self._refresh_directory()
					elif item[0] == "set_volume":
						self._set_volume(item[1])
					elif item[0] == "commit_volume":
						self._commit_volume()
					elif item[0] == "gnss_result":
						self._process_gnss_result(item[1])
					elif item[0] == "ota_prepare":
						self._prepare_ota_business(item[1])
					elif item[0] == "ota_restore":
						self._restore_ota_business(item[1])
				except LogoutRequested as error:
					print("[通信][退出] 已中止旧业务事务：{}".format(error))
				except ServerLogoutRequested as error:
					print("[通信][强退] 已中止旧业务事务：{}".format(error))
				except TCPReconnectScheduled as error:
					print("[通信] 当前TCP事务已终止并交由自动重连：{}".format(error))
				except Exception as error:
					print("[通信] 业务处理异常：{}".format(error))
					if is_single:
						_revision, snapshot = self.state.get_snapshot()
						self._publish_single(
							"active" if self.single_group_id is not None else "idle",
							snapshot["single_peer"] if self.single_group_id is not None else None,
							str(error))
					elif item[0] in ("join_group", "leave_group"):
						print("[通信][组呼] {}失败，保留协议确认后的组状态和选中项：当前组ID={} 错误={}".format(
							"入组或切组" if item[0] == "join_group" else "退组",
							self.group_id, error))
					elif item[0] == "set_volume":
						print("[设置] 音量设置失败：{}".format(error))
						if self.audio is not None:
							_revision, snapshot = self.state.get_snapshot()
							self.state.update_audio(snapshot["audio_tx_active"],
								snapshot["audio_rx_active"], self.audio.volume)
					elif item[0] == "gnss_result":
						print("[定位] 定位上报失败：{}".format(error))
					else:
						if item[0] == "force_login":
							self.force_login_stage = "failed"
						self._publish_login(STATE_ERROR, str(error))
					self._publish_error(error)
				finally:
					self._lock.acquire()
					try:
						self._login_busy = False
						self._single_busy = False
						self._directory_refresh_busy = False
						self._leave_group_busy = False
					finally:
						self._lock.release()
					if self.session is not None and not self._logout_requested:
						self.session.control_busy = False
				if not self._logout_requested:
					self._send_heartbeat_if_due()
		finally:
			self._close_resources()
			self.running = False
			self.thread_id = None
