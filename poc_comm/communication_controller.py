# -*- coding: utf-8 -*-
"""正式入口的统一通信业务服务。

本模块只启动一个业务线程。登录、HTTP、入组和 PTT 麦权状态机都在该
线程中串行推进；TCP 接收、UDP/RTP 接收和统一发送仍由 Runtime 管理的
三个 Worker 负责。
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
	from usr.audio_port import AudioPort
	from usr.comm_commands import (CMD_JOIN_GROUP, CMD_LOGIN, CMD_LOGOUT,
									CMD_TCP_HEARTBEAT)
	from usr.communication_queue import CommunicationCommandQueue
	from usr.communication_runtime import CommunicationRuntime
	from usr.communication_session import CommunicationSession
	from usr.poc_protocol import (MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
								  MSG_LOGOUT, MSG_LOGOUT_ACK,
								  MSG_JOIN_GROUP, MSG_JOIN_GROUP_ACK, MSG_LOGIN,
								  MSG_LOGIN_ACK, build_frame,
								  build_join_group_payload, build_login_payload,
								  build_logout_payload,
								  build_heartbeat_payload,
								  group_id_to_uint32, parse_join_group_ack,
								  parse_login_ack, parse_logout_ack)
	from usr.rtp_protocol import RTP_PAYLOAD_SIZE, parse_rtp_packet
except ImportError:
	from audio_port import AudioPort
	from comm_commands import CMD_JOIN_GROUP, CMD_LOGIN, CMD_LOGOUT, CMD_TCP_HEARTBEAT
	from communication_queue import CommunicationCommandQueue
	from communication_runtime import CommunicationRuntime
	from communication_session import CommunicationSession
	from poc_protocol import (MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
								 MSG_LOGOUT, MSG_LOGOUT_ACK,
								 MSG_JOIN_GROUP, MSG_JOIN_GROUP_ACK, MSG_LOGIN,
								 MSG_LOGIN_ACK, build_frame, build_join_group_payload,
								 build_logout_payload,
								 build_heartbeat_payload,
								 build_login_payload, group_id_to_uint32,
								 parse_join_group_ack, parse_login_ack,
								 parse_logout_ack)
	from rtp_protocol import RTP_PAYLOAD_SIZE, parse_rtp_packet

try:
	from usr.ui_messages import (MSG_ERROR, MSG_GROUP_CHANGED,
									 MSG_LOGIN_CHANGED)
except ImportError:
	from ui_messages import MSG_ERROR, MSG_GROUP_CHANGED, MSG_LOGIN_CHANGED


STATE_CONNECTING = "connecting"
STATE_LOGGED_IN = "logged_in"
STATE_JOINED = "joined"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_IDLE = "idle"

HEARTBEAT_INTERVAL_MS = 60000
HEARTBEAT_ACK_TIMEOUT_MS = 5000
LOGOUT_ACK_TIMEOUT_MS = 5000
RECONNECT_INTERVAL_MS = 3000


class CommunicationController:
	"""串行处理 UI 登录/入组请求，网络工作不进入 UI 线程。"""

	def __init__(self, config, state_store, message_bus,
				 project_name="JingWuPOC", project_version="1.0.0"):
		self.config = dict(config)
		self.state = state_store
		self.messages = message_bus
		self.project_name = str(project_name)
		self.project_version = str(project_version)
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
		self._runtime_generation = 0
		self.tcp_socket = None
		self.udp_socket = None
		self.police_no = None
		self.group_id = None
		self.request_count = 1
		self.login_state = STATE_IDLE
		# 登录成功后保持自动恢复资格；业务事务失败进入 error 不能
		# 阻止 TCP 断线重连，只有主动退出登录才清除此资格。
		self._auto_reconnect_enabled = False
		self._heartbeat_timer = None
		self._heartbeat_due = False
		self._reconnect_pending = False
		self._reconnect_due = False
		self._reconnect_reason = None
		self._reconnect_attempt = 0

	def start(self):
		if self.running:
			return False
		self.running = True
		self.thread_id = _thread.start_new_thread(self._run, ())
		print("[通信] 统一通信业务线程已启动")
		return True

	def stop(self):
		self.running = False
		self._signal()
		self._close_resources()

	def request_login(self, police_no):
		value = str(police_no).strip()
		if not value:
			return False
		accepted = self._enqueue("login", value)
		if accepted:
			print("[通信] 收到登录请求：警号={}".format(value))
		return accepted

	def request_join_group(self, group_id):
		try:
			value = group_id_to_uint32(group_id)
		except Exception:
			return False
		accepted = self._enqueue("join_group", value)
		if accepted:
			print("[通信] 收到入组请求：组ID={}".format(value))
		return accepted

	def request_logout(self):
		accepted = self._enqueue("logout", None)
		if accepted:
			print("[通信] 收到退出登录请求")
		return accepted

	def _enqueue(self, kind, payload):
		self._lock.acquire()
		try:
			# 同一时刻只保留最后一次入组选择，避免快速滑动/点击造成
			# 多个串行入组事务互相等待，旧请求不会覆盖最新选择。
			if kind == "join_group":
				pending = []
				for request_item in self._requests:
					if request_item[0] != "join_group":
						pending.append(request_item)
				self._requests = pending
			if len(self._requests) >= 8:
				return False
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
			self._wake_pending = False
			if self._requests:
				return self._requests.pop(0)
			return None
		finally:
			self._lock.release()

	def _append_event(self, event):
		self._event_lock.acquire()
		try:
			# 控制器只需暂存少量 TCP 应答与发送异常；PTT/RTP 的实时
			# 处理已由会话和音频端完成，故这里保持有界，防止异常网络
			# 下事件积压耗尽内存。
			if len(self._events) >= 32:
				self._events.pop(0)
			self._events.append(event)
			if (event.get("source") == "send" and
					event.get("event", {}).get("type") == "tcp_send_error"):
				print("[通信] TCP发送异常已记录，等待当前事务结束后处理：{}".format(
					event.get("event", {}).get("payload")))
		finally:
			self._event_lock.release()

	def _take_tcp_send_failure(self):
		"""事务结束后取走发送线程上报的 TCP 发送失败事件。"""
		self._event_lock.acquire()
		try:
			for index, event in enumerate(self._events):
				if event["source"] != "send":
					continue
				inner = event["event"]
				if inner["type"] != "tcp_send_error":
					continue
				self._events.pop(index)
				print("[通信] 当前事务已结束，开始处理延迟TCP发送异常：{}".format(
					inner["payload"]))
				return inner["payload"]
		finally:
			self._event_lock.release()
		return None

	def _take_frame(self, msg_type, request_count, timeout_ms=10000):
		deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
		while self.running and utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
			# 正式入口的 PTT 状态机与本线程共用执行上下文；登录/入组
			# 等待期间也及时消费 GPIO 和已到达的运行时事件。
			if self.session is not None:
				self.session.poll_once()
			self._event_lock.acquire()
			try:
				for index, event in enumerate(self._events):
					if event["source"] != "tcp":
						continue
					inner = event["event"]
					if inner["type"] != "tcp_frame":
						continue
					frame = inner["payload"]
					if (frame["msg_type"] == msg_type and
							frame["request_cnt"] == ((request_count + 1) & 0xFFFF)):
						self._events.pop(index)
						return frame
			finally:
				self._event_lock.release()
			utime.sleep_ms(5)
		print("[通信] 等待TCP应答超时：期望=0x{:02X} request_cnt={} timeout={}ms".format(
			int(msg_type), int((request_count + 1) & 0xFFFF), int(timeout_ms)))
		raise RuntimeError("等待TCP应答超时")

	def _publish_login(self, state, error=None):
		self.login_state = state
		if error is None:
			print("[通信] 登录状态={}".format(state))
		else:
			print("[通信] 登录状态={} 错误={}".format(state, error))
		self.state.update_poc(
			self.runtime is not None and self.runtime.running, state, error,
			(None if self.group_id is None else str(self.group_id)),
			"idle", None, "idle", None)
		self.messages.publish(MSG_LOGIN_CHANGED, {"state": state, "error": error})

	def _publish_error(self, error):
		text = str(error)
		self.state.set_error("communication", text)
		self.messages.publish(MSG_ERROR, {"domain": "communication", "text": text}, 1)

	def _wait_network(self):
		print("[通信] 等待蜂窝网络注册和数据拨号")
		if checkNet is not None:
			checkNet.CheckNetwork(self.project_name,
							 self.project_version).wait_network_connected()
		print("[通信] 蜂窝网络已就绪")

	def _read_groups(self):
		if request is None:
			raise RuntimeError("request模块不可用")
		response = None
		try:
			response = request.get(self.config["http_url"],
							  headers={"Accept": "application/json"})
			if int(response.status_code) != 200:
				raise RuntimeError("HTTP状态码={}".format(response.status_code))
			payload = response.json()
			data = payload.get("data") if isinstance(payload, dict) else None
			if not isinstance(data, list):
				raise RuntimeError("HTTP响应缺少data列表")
			groups = []
			for item in data:
				if not isinstance(item, dict):
					continue
				raw_id = None
				for key in ("groupId", "groupID", "grouID", "grouId", "group_id", "id"):
					if item.get(key) is not None:
						raw_id = item.get(key)
						break
				if raw_id is None:
					continue
				group_id = group_id_to_uint32(raw_id)
				name = item.get("groupName")
				if name is None:
					name = item.get("group_name")
				if name is None:
					name = item.get("name")
				groups.append({"id": str(group_id),
							   "name": str(name or group_id)})
			if not groups:
				raise RuntimeError("HTTP没有可用组")
			print("[通信] HTTP获取组列表成功：{}个组".format(len(groups)))
			return groups
		finally:
			if response is not None:
				response.close()

	def _ensure_runtime(self):
		if self.runtime is not None and self.runtime.running:
			print("[通信] 复用现有通信运行时：三个Worker仍在运行")
			return
		# 每次重建连接分配新代号；旧 Worker 即使在 socket.close 后稍晚
		# 才返回，也不能把旧事件或旧音频写入新会话。
		self._runtime_generation += 1
		runtime_generation = self._runtime_generation
		print("[通信] 开始创建TCP/UDP Socket")
		self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self.udp_socket.bind(("0.0.0.0", int(self.config["udp_local_port"])))
		print("[通信] UDP本地端口绑定：{}".format(self.config["udp_local_port"]))
		print("[通信] 开始连接TCP：{}:{}".format(
			self.config["tcp_host"], self.config["tcp_port"]))
		self.tcp_socket.connect((self.config["tcp_host"], int(self.config["tcp_port"])))
		# TCPReceiveWorker 设计为永久阻塞 recv；连接建立阶段可以有
		# 超时，但交给三个 Worker 前必须恢复阻塞模式。
		try:
			self.tcp_socket.settimeout(None)
		except Exception:
			try:
				self.tcp_socket.setblocking(True)
			except Exception:
				pass
		print("[通信] TCP连接成功：{}:{}".format(
			self.config["tcp_host"], self.config["tcp_port"]))
		print("[通信] TCP连接建立，准备启动音频和三个通信Worker")
		self.audio = AudioPort()
		self.audio.start()
		print("[通信] 音频端口已启动：喇叭GPIO{}".format(
			self.config["speaker_enable_gpio"]))

		def on_runtime_event(event):
			if runtime_generation != self._runtime_generation:
				return
			if event["source"] == "rtp":
				inner = event["event"]
				if inner["type"] == "rtp_packet":
					parsed = parse_rtp_packet(inner["payload"])
					if parsed is not None and len(parsed["payload"]) == RTP_PAYLOAD_SIZE:
						self.audio.write_playback_frame(parsed["payload"])
				# RTP数据已经直接写入播放缓冲；不进入控制器事件列表，
				# 避免持续收音时高频音频包堆积在Python内存中。
				return
			self._append_event(event)

		self.runtime = CommunicationRuntime(
			self.tcp_socket, self.udp_socket, self.config["tcp_host"],
			read_audio_frame=self.audio.read_capture_frame,
			event_callback=on_runtime_event,
			command_queue=CommunicationCommandQueue(max_length=32))
		self.session = CommunicationSession(
			submit_command=self.runtime.submit,
			device_id=self.config["device_id"], police_no=self.police_no,
			 audio_port=self.audio, ptt_gpio=int(self.config["ptt_gpio"]))
		self.session.event_callback = self._on_session_event
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
		if not self.runtime.submit(CMD_TCP_HEARTBEAT, packet):
			print("[通信] TCP心跳入队失败，将在下一周期重试")
			return
		self._heartbeat_due = False
		self.request_count = (count + 1) & 0xFFFF
		self.session.next_request_count = self.request_count
		print("[通信] 自动发送TCP心跳：request_cnt={}".format(count))
		try:
			self._take_frame(MSG_HEARTBEAT_ACK, count,
							 HEARTBEAT_ACK_TIMEOUT_MS)
			print("[通信] 收到TCP心跳应答：request_cnt={}".format(
				(count + 1) & 0xFFFF))
		except Exception as error:
			print("[通信] TCP心跳应答超时：{}".format(error))
			self._schedule_reconnect("TCP心跳应答超时")

	def _release_runtime(self):
		"""停止三个通信 Worker 并释放音频/Socket，不修改登录资料。"""
		if self.runtime is not None or self.tcp_socket is not None:
			print("[通信] 开始断开当前TCP/UDP连接并停止三个Worker")
		# 先失效旧回调，再停止阻塞 Worker，避免停止过程中的尾部事件
		# 误进入下一次连接。
		self._runtime_generation += 1
		if self.runtime is not None:
			self.runtime.stop()
		self.runtime = None
		self.session = None
		if self.audio is not None:
			self.audio.close()
		self.audio = None
		self.tcp_socket = None
		self.udp_socket = None
		print("[通信] 当前连接资源已释放，等待自动重连")
		# 旧连接上的TCP帧和发送失败事件不能参与新连接事务。
		self._event_lock.acquire()
		try:
			self._events = []
		finally:
			self._event_lock.release()

	def _schedule_reconnect(self, reason):
		"""通信异常后保留警号和入组目标，进入自动恢复状态。"""
		if (not self.running or self.police_no is None or
				not self._auto_reconnect_enabled):
			return False
		if self._reconnect_pending:
			return True
		self._reconnect_pending = True
		self._reconnect_due = False
		self._reconnect_reason = str(reason)
		self._reconnect_attempt = 0
		print("[通信] 通信异常：{}，准备自动重连".format(
			self._reconnect_reason))
		print("[通信] 已进入断线恢复状态：Timer1将每3秒唤醒业务线程")
		self._release_runtime()
		self._publish_login(STATE_RECONNECTING, self._reconnect_reason)
		self._start_reconnect_timer()
		return True

	def _reconnect_if_due(self):
		"""统一业务线程执行一次重连、重新登录和恢复入组。"""
		if not self._reconnect_pending or not self._reconnect_due:
			return False
		self._reconnect_due = False
		if self.police_no is None:
			self._reconnect_pending = False
			return False
		resume_group = self.group_id
		self._reconnect_attempt += 1
		print("[通信] 收到Timer1重连唤醒：第{}次尝试".format(
			self._reconnect_attempt))
		print("[通信] 开始自动重连：原因={}".format(
			self._reconnect_reason))
		try:
			self._wait_network()
			print("[通信] 重连网络检查通过，开始重新登录")
			self._login(self.police_no, auto_join_first=(resume_group is None))
			print("[通信] 重新登录成功")
			if resume_group is not None:
				print("[通信] 开始恢复断线前组：组ID={}".format(resume_group))
				self._join_group(resume_group, force=True)
				print("[通信] 断线前组恢复成功：组ID={}".format(resume_group))
			self._reconnect_pending = False
			self._reconnect_reason = None
			print("[通信] 自动重连、重新登录{}成功".format(
				"并恢复入组" if resume_group is not None else ""))
			return True
		except Exception as error:
			print("[通信] 自动重连失败：{}，等待下次重试".format(error))
			self._release_runtime()
			self._publish_login(STATE_RECONNECTING, str(error))
			self._start_reconnect_timer()
			return False

	def _on_session_event(self, event):
		kind = event["type"]
		payload = event["payload"]
		if kind in ("floor_request_sent", "floor_release_sent"):
			# PTT会话拥有自己的连续计数器；同步到控制器，避免心跳
			# 在抢麦/释放麦权后复用旧request_cnt。
			self.request_count = (int(payload) + 1) & 0xFFFF
			return
		_revision, snapshot = self.state.get_snapshot()
		if kind == "ptt_changed":
			print("[通信] PTT状态={}".format("按下" if payload else "松开"))
			self.state.update_audio(bool(payload), snapshot["audio_rx_active"],
								snapshot["audio_volume"])
		elif kind == "floor_granted":
			print("[通信] 抢麦成功，开始RTP音频发送")
			self.state.update_poc(
				True, STATE_JOINED, None,
				(None if self.group_id is None else str(self.group_id)),
							"transmitting", self.group_id, STATE_IDLE, None)
		elif kind == "floor_released":
			print("[通信] 麦权已释放，RTP音频发送停止")
			self.state.update_poc(
				True, STATE_JOINED, None,
				(None if self.group_id is None else str(self.group_id)),
							"idle", None, STATE_IDLE, None)
		elif kind == "communication_disconnected":
			inner_type = "未知"
			inner_error = None
			if isinstance(payload, dict):
				inner_type = payload.get("type", "未知")
				inner_error = payload.get("payload")
			print("[通信] TCP异常/断连事件：类型={} 错误={}".format(
				inner_type, inner_error))
			self._schedule_reconnect("TCP接收断开：{}".format(inner_type))

	def _login(self, police_no, auto_join_first=True):
		self.police_no = str(police_no)
		print("[通信] {}登录开始：警号={} request_cnt={}".format(
			"重新" if self._reconnect_pending else "首次",
			self.police_no, self.request_count))
		self._ensure_runtime()
		count = self.request_count
		packet = build_frame(MSG_LOGIN, count, build_login_payload(
			self.config["device_id"], self.police_no))
		if not self.runtime.submit(CMD_LOGIN, packet):
			raise RuntimeError("登录命令入队失败")
		print("[通信] 0x01登录命令已入队，等待0x81应答")
		frame = self._take_frame(MSG_LOGIN_ACK, count)
		result = parse_login_ack(frame["payload"])["login_result"]
		print("[通信] 收到登录应答：结果码=0x{:02X}".format(result))
		if result != 0:
			raise RuntimeError("登录失败，结果码=0x{:02X}".format(result))
		print("[通信] 0x81登录应答成功，开始获取HTTP组列表")
		self._auto_reconnect_enabled = True
		self.request_count = (count + 1) & 0xFFFF
		groups = self._read_groups()
		self.state.update_intercom_directory(groups, ())
		self.messages.publish(MSG_GROUP_CHANGED, groups)
		self._publish_login(STATE_LOGGED_IN)
		self._start_heartbeat_timer()
		if auto_join_first and groups:
			first_group = groups[0]
			first_group_id = group_id_to_uint32(first_group["id"])
			print("[通信] 登录成功，自动进入第一组：组ID={} 名称={}".format(
				first_group_id, first_group.get("name", first_group_id)))
			self._join_group(first_group_id)

	def _join_group(self, group_id, force=False):
		if self.runtime is None or self.police_no is None:
			return
		group_id = group_id_to_uint32(group_id)
		if (not force and self.group_id is not None and
				int(self.group_id) == int(group_id)):
			print("[通信] 已在目标组，忽略重复入组：组ID={}".format(group_id))
			return
		count = self.request_count
		print("[通信] 入组开始：组ID={} request_cnt={}".format(
			group_id, count))
		packet = build_frame(MSG_JOIN_GROUP, count, build_join_group_payload(
			self.config["device_id"], group_id, self.police_no))
		if not self.runtime.submit(CMD_JOIN_GROUP, packet):
			raise RuntimeError("入组命令入队失败")
		frame = self._take_frame(MSG_JOIN_GROUP_ACK, count)
		ack = parse_join_group_ack(frame["payload"])
		print("[通信] 收到入组应答：组ID={} 结果码=0x{:02X}".format(
			group_id, ack["join_result"]))
		if ack["join_result"] != 0:
			raise RuntimeError("入组失败，结果码=0x{:02X}".format(ack["join_result"]))
		self.group_id = int(group_id)
		self.request_count = (count + 1) & 0xFFFF
		self.session.configure_group(self.group_id, self.request_count)
		self._publish_login(STATE_JOINED)

	def _logout(self):
		print("[通信] 正在释放通信、音频和Socket资源")
		# 主动退出时先关闭自动恢复资格，避免退出包发送失败触发重连。
		self._auto_reconnect_enabled = False
		if (self.runtime is not None and self.runtime.running and
				self.police_no is not None):
			count = self.request_count
			packet = build_frame(
				MSG_LOGOUT, count,
				build_logout_payload(self.config["device_id"], self.police_no))
			if self.runtime.submit(CMD_LOGOUT, packet):
				self.request_count = (count + 1) & 0xFFFF
				print("[通信] 发送0x0F退出登录：request_cnt={}".format(count))
				try:
					frame = self._take_frame(
						MSG_LOGOUT_ACK, count, LOGOUT_ACK_TIMEOUT_MS)
					result = parse_logout_ack(frame["payload"])["logout_result"]
					print("[通信] 收到0x8F退出登录应答：结果码=0x{:02X}".format(
						result))
					if result != 0:
						print("[通信] 服务器退出登录未成功，继续清理本地资源")
				except Exception as error:
					print("[通信] 退出登录应答失败：{}，继续清理本地资源".format(
						error))
			else:
				print("[通信] 退出登录命令入队失败，继续清理本地资源")
		else:
			print("[通信] 当前没有可用TCP连接，跳过0x0F并清理本地资源")
		self._close_resources()
		self.police_no = None
		self.group_id = None
		self.request_count = 1
		self.login_state = STATE_IDLE
		self._reconnect_pending = False
		self._reconnect_due = False
		self._reconnect_reason = None
		self.state.update_poc(False, "idle", None, None, "idle", None,
							 "idle", None)

	def _close_resources(self):
		self._stop_heartbeat_timer()
		self._release_runtime()

	def _run(self):
		try:
			while self.running:
				self._wake.acquire()
				if not self.running:
					break
				if self.session is not None:
					self.session.poll_once()
				send_error = self._take_tcp_send_failure()
				if send_error is not None:
					print("[通信] TCP发送线程上报异常：{}".format(send_error))
					self._schedule_reconnect("TCP发送失败：{}".format(send_error))
				self._reconnect_if_due()
				self._send_heartbeat_if_due()
				item = self._take_request()
				if item is None:
					continue
				try:
					if item[0] == "login":
						self._publish_login(STATE_CONNECTING)
						self._wait_network()
						self._login(item[1])
					elif item[0] == "join_group":
						self._join_group(item[1])
					elif item[0] == "logout":
						self._logout()
				except Exception as error:
					print("[通信] 业务处理异常：{}".format(error))
					self._publish_login(STATE_ERROR, str(error))
					self._publish_error(error)
				self._send_heartbeat_if_due()
		finally:
			self._close_resources()
			self.running = False
			self.thread_id = None
