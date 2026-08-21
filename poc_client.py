# -*- coding: utf-8 -*-
"""POC 登录、TCP控制连接和HTTP名单查询后台客户端。"""

import utime
try:
    import ubinascii
except Exception:
    import binascii as ubinascii
try:
    import urandom
except Exception:
    try:
        import random as urandom
    except Exception:
        urandom = None

# 单呼邀请收到“已受理”后不能重复邀请，只等待服务器最终应答。
INVITE_FINAL_RESPONSE_TIMEOUT_MS = 10000
# TCP连接建立后按固定周期发送心跳。
HEARTBEAT_INTERVAL_MS = 60000
# 登录前名单获取失败后按固定间隔重试；断网恢复时不等待该间隔，立即刷新。
PRELOGIN_HTTP_RETRY_INTERVAL_MS = 5000
PRELOGIN_NETWORK_CHECK_INTERVAL_MS = 500
REQUEST_CNT_HISTORY_SIZE = 128
SERVER_REQUEST_CACHE_SIZE = 64
SERVER_REQUEST_CACHE_TTL_MS = 60000
SERVER_REQUEST_TYPES = (0x05, 0x06, 0x0A, 0x0B, 0x0C, 0x0F)
TCP_SIGNAL_NAMES = {
    0x01: "设备登录", 0x81: "设备登录应答", 0x02: "TCP心跳", 0x82: "TCP心跳应答",
    0x03: "进入组呼", 0x83: "进入组呼应答", 0x04: "邀请单呼", 0x84: "邀请单呼应答",
    0x05: "受邀单呼", 0x85: "受邀单呼应答", 0x06: "解散单呼", 0x86: "解散单呼应答",
    0x08: "抢麦", 0x88: "抢麦应答", 0x09: "释放麦", 0x89: "释放麦应答",
    0x0A: "强制释放麦", 0x8A: "强制释放麦应答", 0x0B: "麦权占用", 0x8B: "麦权占用应答",
    0x0C: "麦权空闲", 0x8C: "麦权空闲应答", 0x0D: "定位上传", 0x8D: "定位上传应答",
    0x0E: "强制转机登录", 0x8E: "强制转机登录应答", 0x0F: "退出登录", 0x8F: "退出登录应答",
    0x10: "退出组呼", 0x90: "退出组呼应答",
}
# GNSS初始化失败后延迟重试，避免主循环高频调用底层硬件接口。
GNSS_INIT_RETRY_INTERVAL_MS = 10000

try:
    import _thread
except Exception:
    _thread = None
try:
    import usocket as _socket
except Exception:
    try:
        import socket as _socket
    except Exception:
        _socket = None
try:
    import request
except Exception:
    request = None
try:
    import ujson as _json
except Exception:
    try:
        import json as _json
    except Exception:
        _json = None

try:
    from usr.poc_protocol import (FrameParser, build_frame,
                                  build_login_payload, parse_login_ack,
                                  build_force_login_payload, parse_force_login_ack,
                                  build_logout_payload, parse_logout_payload,
                                  build_logout_ack_payload, parse_logout_ack,
                                  build_heartbeat_payload,
                                  build_gnss_upload_payload,
                                  parse_gnss_upload_ack,
                                  build_join_group_payload, parse_join_group_ack,
                                  build_invite_single_payload,
                                  parse_invite_single_ack,
                                  parse_single_invited,
                                  build_single_invited_ack_payload,
                                  build_dissolve_single_payload,
                                  parse_dissolve_single,
                                  build_dissolve_single_ack_payload,
                                  parse_dissolve_single_ack,
                                  build_floor_payload,
                                  parse_request_floor_ack,
                                  parse_release_floor_ack,
                                  build_device_payload,
                                  parse_floor_occupied, parse_floor_idle,
                                  build_floor_idle_ack_payload,
                                  MSG_LOGIN, MSG_LOGIN_ACK,
                                  MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
                                  MSG_JOIN_GROUP,
                                  MSG_JOIN_GROUP_ACK,
                                  MSG_INVITE_SINGLE, MSG_INVITE_SINGLE_ACK,
                                  MSG_SINGLE_INVITED, MSG_SINGLE_INVITED_ACK,
                                  MSG_DISSOLVE_SINGLE,
                                  MSG_DISSOLVE_SINGLE_ACK,
                                  MSG_REQUEST_FLOOR, MSG_REQUEST_FLOOR_ACK,
                                  MSG_RELEASE_FLOOR, MSG_RELEASE_FLOOR_ACK,
                                  MSG_FORCE_RELEASE_FLOOR,
                                  MSG_FORCE_RELEASE_FLOOR_ACK,
                                  MSG_FLOOR_OCCUPIED, MSG_FLOOR_OCCUPIED_ACK,
                                  MSG_FLOOR_IDLE, MSG_FLOOR_IDLE_ACK,
                                  MSG_GNSS_UPLOAD, MSG_GNSS_UPLOAD_ACK,
                                  MSG_FORCE_LOGIN, MSG_FORCE_LOGIN_ACK,
                                  MSG_LOGOUT, MSG_LOGOUT_ACK,
                                  MSG_LEAVE_GROUP, MSG_LEAVE_GROUP_ACK,
                                  build_leave_group_payload, parse_leave_group_ack,
                                  group_id_to_uint32)
except Exception:
    from poc_protocol import (FrameParser, build_frame,
                              build_login_payload, parse_login_ack,
                              build_force_login_payload, parse_force_login_ack,
                              build_logout_payload, parse_logout_payload,
                              build_logout_ack_payload, parse_logout_ack,
                              build_heartbeat_payload,
                              build_gnss_upload_payload,
                              parse_gnss_upload_ack,
                              build_join_group_payload, parse_join_group_ack,
                              build_invite_single_payload,
                              parse_invite_single_ack,
                              parse_single_invited,
                              build_single_invited_ack_payload,
                              build_dissolve_single_payload,
                              parse_dissolve_single,
                              build_dissolve_single_ack_payload,
                              parse_dissolve_single_ack,
                              build_floor_payload,
                              parse_request_floor_ack,
                              parse_release_floor_ack,
                              build_device_payload,
                              parse_floor_occupied, parse_floor_idle,
                              build_floor_idle_ack_payload,
                              MSG_LOGIN, MSG_LOGIN_ACK,
                              MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
                              MSG_JOIN_GROUP,
                              MSG_JOIN_GROUP_ACK,
                              MSG_INVITE_SINGLE, MSG_INVITE_SINGLE_ACK,
                              MSG_SINGLE_INVITED, MSG_SINGLE_INVITED_ACK,
                              MSG_DISSOLVE_SINGLE,
                              MSG_DISSOLVE_SINGLE_ACK,
                              MSG_REQUEST_FLOOR, MSG_REQUEST_FLOOR_ACK,
                              MSG_RELEASE_FLOOR, MSG_RELEASE_FLOOR_ACK,
                              MSG_FORCE_RELEASE_FLOOR,
                              MSG_FORCE_RELEASE_FLOOR_ACK,
                              MSG_FLOOR_OCCUPIED, MSG_FLOOR_OCCUPIED_ACK,
                              MSG_FLOOR_IDLE, MSG_FLOOR_IDLE_ACK,
                              MSG_GNSS_UPLOAD, MSG_GNSS_UPLOAD_ACK,
                              MSG_FORCE_LOGIN, MSG_FORCE_LOGIN_ACK,
                              MSG_LOGOUT, MSG_LOGOUT_ACK,
                              MSG_LEAVE_GROUP, MSG_LEAVE_GROUP_ACK,
                              build_leave_group_payload, parse_leave_group_ack,
                              group_id_to_uint32)

try:
    from usr.network_monitor import get_default_monitor
except Exception:
    try:
        from network_monitor import get_default_monitor
    except Exception:
        get_default_monitor = None

try:
    from usr.cat1_gnss import CAT1_GNSS
except Exception:
    try:
        from cat1_gnss import CAT1_GNSS
    except Exception:
        CAT1_GNSS = None


class POCClient:
    """后台线程只处理网络和数据，绝不操作LVGL。"""

    def __init__(self, device_id="33030002002000000590",
                 tcp_host="125.124.233.231", tcp_port=6060,
                 http_url=None, network_monitor=None,
                 audio_controller=None, gnss_service=None,
                 gnss_upload_interval_ms=60000, tcp_max_retries=3,
                 tcp_retry_initial_timeout_ms=1000,
                 tcp_retry_timeout_step_ms=500):
        self.device_id = str(device_id)
        self.tcp_host = str(tcp_host)
        print("tcp host=",self.tcp_host)
        self.tcp_port = int(tcp_port)
        self.http_url = (http_url or
                         "http://125.124.233.231:18081/biz/deviceInfo/data?"
                         "deviceId=" + self.device_id)
        self.network_monitor = network_monitor
        # max_retries不包含首次发送。默认总共最多发送4次：首次+3次重发。
        self.tcp_max_retries = max(0, int(tcp_max_retries))
        self.tcp_retry_initial_timeout_ms = max(
            100, int(tcp_retry_initial_timeout_ms))
        self.tcp_retry_timeout_step_ms = max(
            0, int(tcp_retry_timeout_step_ms))
        self.audio_controller = None
        self.tcp_connected = False
        self.login_state = "idle"
        self.login_result = None
        self.login_error = None
        self.http_state = "idle"
        self.groups = []
        self.people = []
        # HTTP使用独立临时线程，避免同步request.get()阻塞TCP控制收包。
        self._http_refresh_pending = False
        self._http_refresh_auto_join = False
        self._http_refresh_reason = None
        self._http_worker_running = False
        self._prelogin_network_ready = False
        self._prelogin_http_retry_at_ms = None
        self._prelogin_network_check_at_ms = None
        self.last_error = None
        self._revision = 0
        self._reported_revision = -1
        self._running = False
        self._socket = None
        self._parser = FrameParser()
        self._police_no = None
        # Keep the login-page value separate from transient call state so an
        # abnormal TCP reconnect can authenticate with the same identity.
        self._login_police_no = None
        self._login_session_active = False
        self._pending_login = False
        # 使用启动时钟生成本次连接的16位起点，避免每次开机都从固定值开始。
        self._request_cnt = int(utime.ticks_ms()) & 0xFFFE
        self._request_cnt_history = []
        self._request_cnt_history_set = {}
        self._server_request_cache = {}
        self._server_request_order = []
        self._login_request_cnt = None
        self._login_sent_ms = None
        self._login_packet = None
        self._login_retry_count = 0
        self._force_login_pending = False
        self._force_login_request_cnt = None
        self._force_login_packet = None
        self._force_login_sent_ms = None
        self._force_login_retry_count = 0
        self.logout_state = "idle"
        self.logout_error = None
        self._logout_revision = 0
        self._logout_request_cnt = None
        self._logout_packet = None
        self._logout_sent_ms = None
        self._logout_retry_count = 0
        self._server_logout_pending = False
        self._server_logout_request_cnt = None
        self._leave_group_request_cnt = None
        self._leave_group_packet = None
        self._leave_group_sent_ms = None
        self._leave_group_retry_count = 0
        self._leave_group_pending = False
        self._leave_group_request_group_id = None
        self._tcp_last_activity_ms = None
        self._heartbeat_last_sent_ms = None
        self._heartbeat_request_cnt = None
        self._heartbeat_sent_ms = None
        self._heartbeat_packet = None
        self._heartbeat_retry_count = 0
        self.gnss_service = gnss_service
        if self.gnss_service is None and CAT1_GNSS is not None:
            try:
                self.gnss_service = CAT1_GNSS()
            except Exception as error:
                print("[GNSS] 服务创建失败：{}".format(error))
        self.gnss_upload_interval_ms = max(
            1000, int(gnss_upload_interval_ms))
        self._gnss_init_last_attempt_ms = None
        self._gnss_first_upload_pending = False
        self._gnss_next_upload_ms = None
        self._gnss_request_cnt = None
        self._gnss_sent_ms = None
        self._gnss_packet = None
        self._gnss_retry_count = 0
        self.join_state = "idle"
        self.join_error = None
        # 每次入组状态变化都递增，防止连续两次success/failed被UI漏判。
        self._join_revision = 0
        self.confirmed_group_id = None
        self.confirmed_group_raw_id = None
        self._pending_join_id = None
        self._pending_join_raw_id = None
        self._join_request_cnt = None
        self._join_request_group_id = None
        self._join_request_raw_id = None
        self._join_sent_ms = None
        self._join_packet = None
        self._join_retry_count = 0
        # 普通组与单呼临时组可以同时存在；单呼状态单独发布给LCD。
        self.single_state = "none"
        self.single_error = None
        self._single_revision = 0
        self.single_call_group_id = None
        self.single_call_person_key = None
        self.single_call_person_device_id = None
        self._single_target_key = None
        self._single_target_device_id = None
        self._single_request_cnt = None
        self._single_sent_ms = None
        self._single_packet = None
        self._single_retry_count = 0
        self._single_invite_ack1_seen = False
        self._single_action = None
        self._single_next_key = None
        self._single_next_device_id = None
        self._single_target_police_no = None
        self._single_next_police_no = None
        # 抢麦状态必须带组ID，防止组呼、单呼事件互相覆盖。
        self.floor_state = "idle"
        self.floor_error = None
        self._floor_revision = 0
        self._floor_event_revision = 0
        self._floor_event = None
        self.pending_floor_group_id = None
        self.held_floor_group_id = None
        self.active_audio_group_id = None
        self.floor_udp_port = None
        # MicroPython并非所有版本都有完整set实现，用字典保存占用组集合。
        self.occupied_group_ids = {}
        self._floor_request_cnt = None
        self._floor_request_group_id = None
        self._floor_request_action = None
        self._floor_sent_ms = None
        self._floor_packet = None
        self._floor_retry_count = 0
        self._next_floor_group_id = None
        # PTT默认操作组呼；用户选择单呼人员后切到单呼，重新选择组时切回组呼。
        self.preferred_call_type = "group"
        self._ptt_pressed = False
        self._release_after_floor_grant = False
        self._lock = None
        if _thread is not None:
            try:
                self._lock = _thread.allocate_lock()
            except Exception:
                self._lock = None
        self.set_audio_controller(audio_controller)

    def _lock_acquire(self):
        if self._lock is not None:
            self._lock.acquire()

    def _lock_release(self):
        if self._lock is not None:
            self._lock.release()

    def _changed(self):
        self._revision += 1

    def _join_changed(self):
        self._join_revision += 1
        self._changed()

    def _single_changed(self):
        self._single_revision += 1
        self._changed()

    def _logout_changed(self):
        self._logout_revision += 1
        self._changed()

    def _floor_changed(self):
        self._floor_revision += 1
        self._changed()

    def _floor_event_changed(self, event, group_id=None, police_name=None):
        """发布麦权UI事件；后台线程不直接操作LVGL。"""
        self._floor_event_revision += 1
        self._floor_event = {
            "revision": self._floor_event_revision,
            "event": event,
            "group_id": group_id,
            "police_name": police_name,
        }
        self._changed()
        return self._floor_event_revision

    def set_audio_controller(self, controller):
        """绑定RTP音频模块；协议模块只通过公开方法通知音频状态。"""
        self.audio_controller = controller
        if controller is not None:
            controller.on_request_floor = self.request_ptt_floor
            controller.on_release_floor = self._request_ptt_release

    def _audio_notify(self, method, *args):
        controller = self.audio_controller
        if controller is None:
            return
        callback = getattr(controller, method, None)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as error:
            print("[POC] RTP音频状态同步失败：{}".format(error))

    def notify_floor_ui_ready(self, event_revision):
        """由LCD主线程确认麦权弹框首帧已提交，再放行音频业务。"""
        self._audio_notify("on_floor_ui_ready", event_revision)

    def start(self):
        if self._running:
            return True
        if _thread is None:
            self.last_error = RuntimeError("QuecPython缺少_thread")
            return False
        self._running = True
        try:
            _thread.start_new_thread(self._worker, ())
            return True
        except Exception as error:
            self._running = False
            self.last_error = error
            print("[POC] 后台线程启动失败：{}".format(error))
            return False

    def submit_login(self, police_no):
        """用户确认登录后提交编号，TCP未就绪时等待连接。"""
        value = str(police_no).strip().upper()
        try:
            build_login_payload(self.device_id, value)
        except Exception as error:
            self._set_login_state("failed", error, None)
            return False
        self._lock_acquire()
        try:
            if (self.login_state in ("waiting_tcp", "logging_in") or
                    self._pending_login or
                    self._login_request_cnt is not None or
                    self._force_login_pending or
                    self._force_login_request_cnt is not None):
                # 重复点击登录不再创建第二个请求；对UI保持“登录中”语义。
                return True
            self._police_no = value
            self._login_police_no = value
            self._login_session_active = True
            self._pending_login = True
            self.logout_state = "idle"
            self.logout_error = None
            self._logout_revision += 1
            self.login_error = None
            self.login_result = None
            self.login_state = ("logging_in" if self.tcp_connected
                                else "waiting_tcp")
            self._changed()
        finally:
            self._lock_release()
        print("[POC] 已提交登录：{}".format(value))
        return True

    def confirm_force_login(self):
        """确认0x81=0x01后的强制转机登录。"""
        police_no = self._login_police_no or self._police_no
        if self.login_state != "failed" or not police_no or self._socket is None:
            return False
        self._police_no = police_no
        self._login_session_active = True
        self._force_login_pending = True
        self.login_state = "logging_in"
        self.login_error = None
        self._changed()
        return True

    def request_logout(self):
        """设备主动退出接口；发送0x0F前先清理已有单呼和组呼。"""
        if (not self.tcp_connected or self._socket is None or
                self._logout_request_cnt is not None or
                self.logout_state in ("cleaning", "waiting")):
            print("[POC] 主动退出未发送：TCP未连接或退出请求已在处理中")
            return False
        self._server_logout_pending = False
        self._leave_group_pending = False
        self.logout_error = None
        self.logout_state = "cleaning"
        self._logout_changed()
        self._prepare_single_for_logout()
        self._continue_logout_cleanup()
        print("[POC] 已启动主动退出登录清理流程")
        return True

    def _send_force_login(self):
        if not self._force_login_pending or self._socket is None or self._force_login_request_cnt is not None:
            return
        police_no = self._login_police_no or self._police_no
        if not police_no:
            return
        request_cnt = self._next_request_cnt()
        packet = build_frame(MSG_FORCE_LOGIN, request_cnt,
                             build_force_login_payload(self.device_id, police_no))
        self._send_all(packet)
        self._force_login_pending = False
        self._force_login_request_cnt = request_cnt
        self._force_login_packet = packet
        self._force_login_sent_ms = utime.ticks_ms()
        self._force_login_retry_count = 0

    def _send_logout(self):
        police_no = self._police_no or self._login_police_no
        if (self._socket is None or self._logout_request_cnt is not None or
                not police_no):
            return
        request_cnt = self._next_request_cnt()
        packet = build_frame(
            MSG_LOGOUT, request_cnt,
            build_logout_payload(self.device_id, police_no))
        self._send_all(packet)
        self._logout_request_cnt = request_cnt
        self._logout_packet = packet
        self._logout_sent_ms = utime.ticks_ms()
        self._logout_retry_count = 0
        self.logout_state = "waiting"
        self._logout_changed()

    def _send_leave_group(self):
        if (not self._leave_group_pending or self._socket is None or
                self._leave_group_request_cnt is not None or
                self.confirmed_group_id is None or not self._police_no):
            return
        request_cnt = self._next_request_cnt()
        packet = build_frame(MSG_LEAVE_GROUP, request_cnt,
                             build_leave_group_payload(self.device_id,
                                                       self._police_no,
                                                       self.confirmed_group_id))
        self._send_all(packet)
        self._leave_group_pending = False
        self._leave_group_request_cnt = request_cnt
        self._leave_group_request_group_id = int(
            self.confirmed_group_id) & 0xFFFFFFFF
        self._leave_group_packet = packet
        self._leave_group_sent_ms = utime.ticks_ms()
        self._leave_group_retry_count = 0

    def _audio_priority_active(self):
        controller = self.audio_controller
        if controller is None:
            return False
        checker = getattr(controller, "is_audio_priority_active", None)
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _gnss_pause_active(self):
        """对讲建立、发送或接收期间暂停GNSS读取、发送和重发。"""
        return bool(
            self._audio_priority_active() or
            self.held_floor_group_id is not None or
            self.active_audio_group_id is not None or
            self.pending_floor_group_id is not None or
            self._floor_request_action is not None)

    @staticmethod
    def _time_reached(deadline_ms, now_ms=None):
        if deadline_ms is None:
            return False
        if now_ms is None:
            now_ms = utime.ticks_ms()
        try:
            return utime.ticks_diff(now_ms, deadline_ms) >= 0
        except Exception:
            return now_ms >= deadline_ms

    @staticmethod
    def _add_ms(base_ms, interval_ms):
        try:
            return utime.ticks_add(base_ms, int(interval_ms))
        except Exception:
            return base_ms + int(interval_ms)

    def _ensure_gnss_initialized(self, now_ms=None):
        """在非对讲阶段初始化GNSS；失败后每10秒最多重试一次。"""
        service = self.gnss_service
        if service is None or self._gnss_pause_active():
            return False
        if bool(getattr(service, "initialized", False)):
            return True
        if now_ms is None:
            now_ms = utime.ticks_ms()
        if (self._gnss_init_last_attempt_ms is not None and
                self._elapsed_ms(
                    self._gnss_init_last_attempt_ms, now_ms) <
                GNSS_INIT_RETRY_INTERVAL_MS):
            return False
        self._gnss_init_last_attempt_ms = now_ms
        initializer = getattr(service, "initialize", None)
        if initializer is None:
            return False
        try:
            return bool(initializer())
        except Exception as error:
            print("[GNSS] 初始化调用异常：{}".format(error))
            return False

    def _gnss_startup_ready(self):
        """首次定位包等待HTTP刷新和默认入组流程结束。"""
        if self.login_state != "success":
            return False
        if (self._http_worker_running or self._http_refresh_pending or
                self.http_state not in ("success", "failed")):
            return False
        if (self._pending_join_id is not None or
                self._join_request_cnt is not None or
                self.join_state in ("waiting", "requesting")):
            return False
        return True

    def _clear_gnss_request(self):
        self._gnss_request_cnt = None
        self._gnss_sent_ms = None
        self._gnss_packet = None
        self._gnss_retry_count = 0

    def _send_gnss_if_due(self):
        """登录后首次及周期上报定位；过期期间只保留一次待发送。"""
        if (self._socket is None or self.login_state != "success" or
                self._gnss_request_cnt is not None or
                self._gnss_pause_active()):
            return
        now_ms = utime.ticks_ms()
        if self._gnss_first_upload_pending:
            if not self._gnss_startup_ready():
                return
        elif (self._gnss_next_upload_ms is None or
              not self._time_reached(self._gnss_next_upload_ms, now_ms)):
            return

        gnss_data = b""
        service = self.gnss_service
        if service is None:
            print("[GNSS] 定位信息：无")
            print("[GNSS] 定位结果：定位失败（GNSS服务不可用）")
        elif self._ensure_gnss_initialized(now_ms):
            reader = getattr(service, "read_gga", None)
            if reader is None:
                print("[GNSS] 定位信息：无")
                print("[GNSS] 定位结果：定位失败（缺少read_gga接口）")
            else:
                try:
                    gnss_data = reader() or b""
                except Exception as error:
                    print("[GNSS] 定位信息：读取异常 {}".format(error))
                    print("[GNSS] 定位结果：定位失败")
                    gnss_data = b""
        else:
            print("[GNSS] 定位信息：无")
            print("[GNSS] 定位结果：定位失败（GNSS尚未初始化）")

        request_cnt = self._next_request_cnt()
        packet = build_frame(
            MSG_GNSS_UPLOAD, request_cnt,
            build_gnss_upload_payload(self.device_id, gnss_data))
        self._send_all(packet)
        self._gnss_request_cnt = request_cnt
        self._gnss_packet = packet
        self._gnss_retry_count = 0
        self._gnss_sent_ms = utime.ticks_ms()
        self._gnss_first_upload_pending = False
        self._gnss_next_upload_ms = self._add_ms(
            self._gnss_sent_ms, self.gnss_upload_interval_ms)
        print("[GNSS] 已发送定位信息上报：gnss_len={}，frame={}".format(
            len(gnss_data), self._hex(packet)))

    def request_http_refresh(self, reason="页面", auto_join_first=False,
                             allow_before_login=False):
        """请求后台刷新HTTP名单；并发请求合并，绝不阻塞调用线程。"""
        if (_thread is None or not self._running or
                (self.login_state != "success" and
                 not allow_before_login)):
            return False
        if self.login_state != "success":
            # 登录前只获取身份名单，绝不能触发登录后的默认入组动作。
            auto_join_first = False
        start_worker = False
        self._lock_acquire()
        try:
            self._http_refresh_pending = True
            self._http_refresh_auto_join = (
                self._http_refresh_auto_join or bool(auto_join_first))
            self._http_refresh_reason = str(reason or "页面")
            if not self._http_worker_running:
                self._http_worker_running = True
                start_worker = True
        finally:
            self._lock_release()
        if not start_worker:
            return True
        try:
            _thread.start_new_thread(self._http_refresh_worker, ())
            return True
        except Exception as error:
            self._lock_acquire()
            try:
                self._http_worker_running = False
                self._http_refresh_pending = False
                self._http_refresh_auto_join = False
            finally:
                self._lock_release()
            print("[POC] HTTP后台线程启动失败：{}".format(error))
            return False

    def _http_refresh_worker(self):
        """串行执行HTTP刷新；音频活跃时等待，优先保证RTP稳定。"""
        try:
            while self._running:
                self._lock_acquire()
                try:
                    if not self._http_refresh_pending:
                        self._http_worker_running = False
                        return
                    auto_join_first = self._http_refresh_auto_join
                    reason = self._http_refresh_reason
                    self._http_refresh_pending = False
                    self._http_refresh_auto_join = False
                    self._http_refresh_reason = None
                finally:
                    self._lock_release()

                while self._running and self._audio_priority_active():
                    utime.sleep_ms(100)
                if not self._running:
                    break
                self._fetch_http_info(auto_join_first, reason)
        except Exception as error:
            print("[POC] HTTP后台任务异常：{}".format(error))
            self._set_http_failed()
        self._lock_acquire()
        try:
            self._http_worker_running = False
        finally:
            self._lock_release()

    def _set_login_state(self, state, error=None, result=None):
        self._lock_acquire()
        try:
            self.login_state = state
            self.login_error = None if error is None else str(error)
            self.login_result = result
            self._changed()
        finally:
            self._lock_release()

    def _reset_request_tracking(self):
        """Reset request history and inbound deduplication for a new TCP link."""
        self._lock_acquire()
        try:
            self._request_cnt_history = []
            self._request_cnt_history_set = {}
            self._server_request_cache = {}
            self._server_request_order = []
        finally:
            self._lock_release()

    def _next_request_cnt(self):
        """Allocate a request counter not used recently on this TCP link."""
        self._lock_acquire()
        try:
            active = {}
            for request_cnt in (
                    self._login_request_cnt,
                    self._force_login_request_cnt,
                    self._logout_request_cnt,
                    self._leave_group_request_cnt,
                    self._heartbeat_request_cnt,
                    self._gnss_request_cnt,
                    self._join_request_cnt,
                    self._single_request_cnt,
                    self._floor_request_cnt):
                if request_cnt is not None:
                    active[int(request_cnt) & 0xFFFF] = True
            value = None
            for _ in range(8):
                candidate = None
                if urandom is not None:
                    try:
                        candidate = int(urandom.getrandbits(16))
                    except Exception:
                        try:
                            candidate = int(urandom.randint(0, 0xFFFE))
                        except Exception:
                            candidate = None
                if candidate is None:
                    candidate = ((self._request_cnt * 1103515245 +
                                  int(utime.ticks_ms()) + 12345) >> 8) & 0xFFFF
                candidate &= 0xFFFF
                if (candidate != 0xFFFF and
                        candidate not in self._request_cnt_history_set and
                        candidate not in active):
                    value = candidate
                    break

            if value is None:
                candidate = (self._request_cnt + 1) & 0xFFFF
                for _ in range(0xFFFF):
                    if (candidate != 0xFFFF and
                            candidate not in self._request_cnt_history_set and
                            candidate not in active):
                        value = candidate
                        break
                    candidate = (candidate + 1) & 0xFFFF
            if value is None:
                raise RuntimeError("no request_cnt is available")

            self._request_cnt = value
            self._request_cnt_history.append(value)
            self._request_cnt_history_set[value] = True
            while len(self._request_cnt_history) > REQUEST_CNT_HISTORY_SIZE:
                old_value = self._request_cnt_history.pop(0)
                if old_value in self._request_cnt_history_set:
                    del self._request_cnt_history_set[old_value]
            return value
        finally:
            self._lock_release()

    def _prune_server_request_cache(self, now_ms=None):
        if now_ms is None:
            now_ms = utime.ticks_ms()
        while self._server_request_order:
            request_cnt = self._server_request_order[0]
            entry = self._server_request_cache.get(request_cnt)
            expired = (entry is None or
                       self._elapsed_ms(entry.get("first_seen_ms"), now_ms) >=
                       SERVER_REQUEST_CACHE_TTL_MS)
            oversized = (len(self._server_request_order) >
                         SERVER_REQUEST_CACHE_SIZE)
            if not expired and not oversized:
                break
            self._server_request_order.pop(0)
            if entry is not None:
                del self._server_request_cache[request_cnt]

    def _validate_server_request(self, frame):
        msg_type = frame["msg_type"]
        payload = frame["payload"]
        parsed = None
        if msg_type == MSG_SINGLE_INVITED:
            parsed = parse_single_invited(payload)
        elif msg_type == MSG_DISSOLVE_SINGLE:
            parsed = parse_dissolve_single(payload)
        elif msg_type == MSG_FORCE_RELEASE_FLOOR:
            if payload:
                raise ValueError("0x0A payload must be empty")
        elif msg_type == MSG_FLOOR_OCCUPIED:
            parsed = parse_floor_occupied(payload)
        elif msg_type == MSG_FLOOR_IDLE:
            parsed = parse_floor_idle(payload)
        elif msg_type == MSG_LOGOUT:
            parsed = parse_logout_payload(payload)

        expected_police = self._police_no or self._login_police_no
        if msg_type == MSG_SINGLE_INVITED and not expected_police:
            raise ValueError("single-call invitation received before login")
        if msg_type in (MSG_DISSOLVE_SINGLE, MSG_LOGOUT):
            if parsed["device_id"] != self.device_id.encode("ascii"):
                raise ValueError("server request device_id does not match")
            if (not expected_police or
                    parsed["police_no"] != expected_police.encode("ascii")):
                raise ValueError("server request police_no does not match")
        return parsed

    def _begin_server_request(self, frame):
        """Return True only for a new, valid server-initiated request."""
        try:
            self._validate_server_request(frame)
        except Exception as error:
            print("[POC] 服务器主动请求格式错误，已忽略：{}".format(error))
            return False

        now_ms = utime.ticks_ms()
        self._prune_server_request_cache(now_ms)
        request_cnt = int(frame["request_cnt"]) & 0xFFFF
        msg_type = int(frame["msg_type"]) & 0xFF
        payload = bytes(frame["payload"])
        entry = self._server_request_cache.get(request_cnt)
        if entry is not None:
            if (entry.get("msg_type") != msg_type or
                    entry.get("payload") != payload):
                print("[POC] 服务器request_cnt冲突，已忽略：cnt={}, old=0x{:02X}, new=0x{:02X}".format(
                    request_cnt, entry.get("msg_type", 0), msg_type))
                return False
            if entry.get("state") == "done" and entry.get("ack_packet"):
                self._send_all(entry["ack_packet"])
                print("[POC] 重复服务器请求，重发缓存应答：cnt={}, type=0x{:02X}".format(
                    request_cnt, msg_type))
            else:
                print("[POC] 重复服务器请求仍在处理中，忽略副作用：cnt={}, type=0x{:02X}".format(
                    request_cnt, msg_type))
            return False

        self._server_request_cache[request_cnt] = {
            "msg_type": msg_type,
            "payload": payload,
            "state": "processing",
            "ack_packet": None,
            "first_seen_ms": now_ms,
        }
        self._server_request_order.append(request_cnt)
        self._prune_server_request_cache(now_ms)
        return True

    def _cache_server_response(self, request_cnt, packet):
        request_cnt = int(request_cnt) & 0xFFFF
        entry = self._server_request_cache.get(request_cnt)
        if entry is None:
            return
        entry["ack_packet"] = bytes(packet)
        entry["state"] = "done"

    def _network_ready(self):
        if self.network_monitor is None and get_default_monitor is not None:
            try:
                self.network_monitor = get_default_monitor()
            except Exception:
                self.network_monitor = None
        if self.network_monitor is None:
            return True
        try:
            return self.network_monitor.get_network_status()[0] is True
        except Exception:
            return False

    def _connect(self):
        if _socket is None:
            raise RuntimeError("socket模块不可用")
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            # 蜂窝网络首次建链可能超过1秒，连接阶段放宽到10秒。
            sock.settimeout(10)
        except Exception:
            pass
        #print("tcp connect...")
        sock.connect((self.tcp_host, self.tcp_port))
        #print("tcp connect over")
        try:
            # 非阻塞接收配合20ms主循环，使1秒起步的重发计时保持准确。
            sock.setblocking(False)
        except Exception:
            #print("tcp connect err")
            try:
                sock.settimeout(0)
            except Exception:
                # 极少数旧固件只接受整秒超时，保留1秒作为最后兼容方案。
                try:
                    sock.settimeout(1)
                except Exception:
                    pass
        self._socket = sock
        self._parser = FrameParser()
        self._reset_request_tracking()
        self._lock_acquire()
        try:
            self.tcp_connected = True
            self._heartbeat_last_sent_ms = utime.ticks_ms()
            if self._pending_login:
                self.login_state = "logging_in"
            self._changed()
        finally:
            self._lock_release()
        print("[POC] TCP连接服务器成功：{}:{}".format(self.tcp_host, self.tcp_port))

    def _close_socket(self):
        forced_logout_interrupted = (
            self._server_logout_pending or
            (self.logout_state == "cleaning" and
             self.logout_error == "设备已被强制退出"))
        active_logout_interrupted = (
            not forced_logout_interrupted and
            self.logout_state in ("cleaning", "waiting"))
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        self._lock_acquire()
        try:
            # TCP控制链路断开后麦权已经不可确认，立即停止所有RTP收发。
            self._audio_notify("on_floor_released", None)
            self.held_floor_group_id = None
            self.active_audio_group_id = None
            self.floor_udp_port = None
            self.occupied_group_ids = {}
            self._ptt_pressed = False
            self._release_after_floor_grant = False
            if self.floor_state == "held":
                self.floor_state = "idle"
                self._floor_changed()
            self._tcp_last_activity_ms = None
            self._heartbeat_last_sent_ms = None
            self._heartbeat_request_cnt = None
            self._heartbeat_sent_ms = None
            self._heartbeat_packet = None
            self._heartbeat_retry_count = 0
            self._login_request_cnt = None
            self._login_sent_ms = None
            self._login_packet = None
            self._login_retry_count = 0
            self._force_login_pending = False
            self._force_login_request_cnt = None
            self._force_login_packet = None
            self._force_login_sent_ms = None
            self._force_login_retry_count = 0
            self._logout_request_cnt = None
            self._logout_packet = None
            self._logout_sent_ms = None
            self._logout_retry_count = 0
            self._leave_group_pending = False
            self._leave_group_request_cnt = None
            self._leave_group_request_group_id = None
            self._leave_group_packet = None
            self._leave_group_sent_ms = None
            self._leave_group_retry_count = 0
            self._clear_gnss_request()
            self._gnss_first_upload_pending = False
            self._gnss_next_upload_ms = None
            self._request_cnt_history = []
            self._request_cnt_history_set = {}
            self._server_request_cache = {}
            self._server_request_order = []
            if forced_logout_interrupted:
                # 服务器已要求本机退出，清理或0x8F发送期间即使链路断开，
                # 本机仍必须完成退出，不能永久停在cleaning状态。
                self._server_logout_pending = False
                self._server_logout_request_cnt = None
                self._clear_forced_logout()
                self._login_session_active = False
                self._pending_login = False
                self._police_no = None
                self.login_state = "idle"
                self.login_result = 0
                self.logout_state = "success"
                self.logout_error = "设备已被强制退出"
                self._logout_changed()
            elif active_logout_interrupted:
                # 主动退出期间链路中断按退出失败处理；后续仍按异常断线规则
                # 使用原警号重连并重新登录。
                self._clear_forced_logout()
                self.logout_state = "failed"
                self.logout_error = "退出失败"
                self._logout_changed()
            if self.tcp_connected:
                self.tcp_connected = False
                self._changed()
            # Only an abnormal disconnect keeps the login session active.
            if (self._running and self._login_session_active and
                    self._login_police_no):
                self._police_no = self._login_police_no
                self._pending_login = True
                self.login_state = "waiting_tcp"
            elif not self._login_session_active:
                self._pending_login = False
            # TCP断开时，当前入组请求视为失败；已确认的小组保留不变，
            # 由UI回滚到断线前的已确认状态。
            if self.join_state in ("waiting", "requesting"):
                self._pending_join_id = None
                self._join_sent_ms = None
                self._join_request_cnt = None
                self._join_request_group_id = None
                self._join_request_raw_id = None
                self._join_packet = None
                self._join_retry_count = 0
                self.join_state = "failed"
                self.join_error = "TCP连接断开"
                self._join_changed()
            if self._single_action is not None:
                # 断线时不能确认邀请或解散是否完成，保留已确认会话并回滚UI。
                self._single_request_cnt = None
                self._single_sent_ms = None
                self._single_packet = None
                self._single_retry_count = 0
                self._single_action = None
                self._single_target_key = None
                self._single_target_device_id = None
                self._single_target_police_no = None
                self._single_next_key = None
                self._single_next_device_id = None
                self._single_next_police_no = None
                self._single_invite_ack1_seen = False
                self.single_state = "failed"
                self.single_error = "TCP连接断开"
                self._single_changed()
            if self._floor_request_action is not None:
                self.pending_floor_group_id = None
                self._floor_request_cnt = None
                self._floor_request_group_id = None
                self._floor_request_action = None
                self._floor_sent_ms = None
                self._floor_packet = None
                self._floor_retry_count = 0
                self._next_floor_group_id = None
                self.floor_state = "failed"
                self.floor_error = "TCP连接断开"
                self._floor_changed()
            self._leave_group_request_group_id = None
        finally:
            self._lock_release()

    def _hex(self, data):
        try:
            return ubinascii.hexlify(data).decode("ascii").upper()
        except Exception:
            return str(data)

    def _send_all(self, data):
        try:
            msg_type = data[4]
            payload = data[9:-2] if len(data) >= 11 else b""
            print("[TCP发送] 信令=0x{:02X} {}，消息体={}".format(
                msg_type, TCP_SIGNAL_NAMES.get(msg_type, "未知信令"),
                self._hex(payload)))
        except Exception:
            pass
        """兼容没有sendall()的QuecPython socket，确保完整发送一帧。"""
        sender = getattr(self._socket, "sendall", None)
        if sender is not None:
            sender(data)
            self._mark_tcp_activity()
            return
        sent = 0
        while sent < len(data):
            count = self._socket.send(data[sent:])
            if count is None:
                # 部分QuecPython固件以None表示本次数据已由底层接收。
                self._mark_tcp_activity()
                return
            count = int(count)
            if count <= 0:
                raise OSError("TCP发送返回0字节")
            sent += count
        self._mark_tcp_activity()

    def _elapsed_ms(self, started_ms, now_ms=None):
        if started_ms is None:
            return 0
        if now_ms is None:
            now_ms = utime.ticks_ms()
        try:
            return utime.ticks_diff(now_ms, started_ms)
        except Exception:
            return now_ms - started_ms

    def _retry_timeout_ms(self, retry_count):
        """首次等待1秒，此后每重发一次把下一次等待增加500ms。"""
        return (self.tcp_retry_initial_timeout_ms +
                int(retry_count) * self.tcp_retry_timeout_step_ms)

    def _retry_packet_if_due(self, packet, sent_ms, retry_count, label):
        """复用同一完整帧重发；返回(发送时间, 重发次数, 是否最终超时)。"""
        if packet is None or sent_ms is None:
            return sent_ms, retry_count, False
        if self._elapsed_ms(sent_ms) < self._retry_timeout_ms(retry_count):
            return sent_ms, retry_count, False
        if retry_count >= self.tcp_max_retries:
            return sent_ms, retry_count, True
        self._send_all(packet)
        retry_count += 1
        sent_ms = utime.ticks_ms()
        print("[POC] {}未收到应答，重发{}/{}：{}".format(
            label, retry_count, self.tcp_max_retries, self._hex(packet)))
        return sent_ms, retry_count, False

    def _mark_tcp_activity(self, now_ms=None):
        """登录后记录最近一次TCP收发时间，重新开始60秒心跳计时。"""
        if self.login_state != "success":
            return
        self._tcp_last_activity_ms = (
            utime.ticks_ms() if now_ms is None else int(now_ms))

    def _heartbeat_due(self, now_ms=None):
        """返回TCP连接是否已经连续空闲60秒。"""
        if self._socket is None or self._heartbeat_last_sent_ms is None:
            return False
        if now_ms is None:
            now_ms = utime.ticks_ms()
        try:
            elapsed = utime.ticks_diff(now_ms, self._heartbeat_last_sent_ms)
        except Exception:
            elapsed = now_ms - self._heartbeat_last_sent_ms
        return elapsed >= HEARTBEAT_INTERVAL_MS

    def _send_heartbeat_if_due(self):
        """TCP空闲满一分钟时发送0x02心跳请求。"""
        if self._heartbeat_request_cnt is not None or not self._heartbeat_due():
            return
        request_cnt = self._next_request_cnt()
        packet = build_frame(
            MSG_HEARTBEAT, request_cnt,
            build_heartbeat_payload(self.device_id))
        self._send_all(packet)
        self._heartbeat_request_cnt = request_cnt
        self._heartbeat_packet = packet
        self._heartbeat_retry_count = 0
        self._heartbeat_sent_ms = utime.ticks_ms()
        self._heartbeat_last_sent_ms = self._heartbeat_sent_ms
        print("[POC] 已发送TCP心跳包：{}".format(self._hex(packet)))

    def _send_login(self):
        self._lock_acquire()
        try:
            police_no = self._login_police_no or self._police_no
            pending = self._pending_login
        finally:
            self._lock_release()
        if (not pending or not police_no or self._socket is None or
                self._login_request_cnt is not None):
            return
        request_cnt = self._next_request_cnt()
        packet = build_frame(MSG_LOGIN, request_cnt,
                             build_login_payload(self.device_id, police_no))
        self._send_all(packet)
        print("[POC] 已发送登录包：{}".format(self._hex(packet)))
        self._lock_acquire()
        try:
            self._police_no = police_no
            self._pending_login = False
            self._login_request_cnt = request_cnt
            self._login_packet = packet
            self._login_retry_count = 0
            self._login_sent_ms = utime.ticks_ms()
            self.login_state = "logging_in"
            self._changed()
        finally:
            self._lock_release()

    def request_join_group(self, raw_group_id, numeric_group_id=None):
        """请求进入小组；只有0x83成功应答后才更新confirmed_group。"""
        try:
            numeric = (group_id_to_uint32(raw_group_id)
                       if numeric_group_id is None else int(numeric_group_id))
        except Exception as error:
            self.join_state = "failed"
            self.join_error = str(error)
            self._join_changed()
            return False
        if self.login_state != "success":
            return False
        self.preferred_call_type = "group"
        self._lock_acquire()
        try:
            self._pending_join_id = numeric & 0xFFFFFFFF
            self._pending_join_raw_id = str(raw_group_id)
            # 新请求替换旧请求，旧请求的应答不再用于更新界面。
            self._join_request_cnt = None
            self._join_request_group_id = None
            self._join_request_raw_id = None
            self._join_sent_ms = None
            self._join_packet = None
            self._join_retry_count = 0
            self.join_state = "waiting"
            self.join_error = None
            self._join_changed()
        finally:
            self._lock_release()
        print("[POC] 请求进入组：{} (UINT32={})".format(raw_group_id, numeric))
        return True

    def request_leave_group(self):
        if self.login_state != "success" or self.confirmed_group_id is None:
            return False
        self._leave_group_pending = True
        self.join_state = "waiting"
        self.join_error = None
        self._join_changed()
        return True

    def request_select_single(self, person_key, target_device_id,
                              target_police_no=None):
        """选择单呼对象；已有单呼时先解散，成功后再自动邀请新人。"""
        key = None if person_key in (None, "") else str(person_key)
        target = None if target_device_id in (None, "") else str(target_device_id)
        target_police = (None if target_police_no in (None, "") else
                         str(target_police_no).strip().upper())
        if key is not None and target is None:
            self.single_error = "单呼对象缺少device_id"
            self.single_state = "failed"
            self._single_changed()
            return False
        if target is not None and target_police is None:
            for person in self.people:
                person_device = str(person.get("device_id", ""))
                person_key_value = str(
                    person.get("device_id") or person.get("police_code") or
                    person.get("name") or "")
                if person_device == target or person_key_value == key:
                    value = str(person.get("police_code", "")).strip().upper()
                    target_police = value or None
                    break
        if target is not None:
            try:
                build_invite_single_payload(
                    self.device_id, target,
                    self._police_no or self._login_police_no,
                    target_police)
            except Exception as error:
                self.single_error = str(error)
                self.single_state = "failed"
                self._single_changed()
                return False
        if self.login_state != "success":
            return False
        if key is not None:
            self.preferred_call_type = "single"
        self._lock_acquire()
        try:
            if self._single_action is not None:
                return False
            if (self.single_call_group_id is not None and key is not None and
                    key == self.single_call_person_key):
                return True
            self.single_error = None
            self._single_next_key = key
            self._single_next_device_id = target
            self._single_next_police_no = target_police
            if self.single_call_group_id is not None:
                # 旧临时组仍有效，只有0x86确认成功后才清除并进入邀请阶段。
                self._single_action = "dissolve"
                self.single_state = "dissolve_waiting"
            elif key is not None:
                self._single_target_key = key
                self._single_target_device_id = target
                self._single_target_police_no = target_police
                self._single_action = "invite"
                self.single_state = "invite_waiting"
            else:
                self.single_state = "none"
            self._single_changed()
        finally:
            self._lock_release()
        return True

    def set_preferred_call_type(self, call_type):
        """只切换PTT目标，不重复发送入组、邀请或解散信令。"""
        if call_type == "group" and self.confirmed_group_id is not None:
            self.preferred_call_type = "group"
            return True
        if call_type == "single" and self.single_call_group_id is not None:
            self.preferred_call_type = "single"
            return True
        return False

    def request_floor(self, call_type="group"):
        """公开抢麦接口；组呼优先于单呼，任一时刻只请求一个组的麦权。"""
        group_id = (self.confirmed_group_id if call_type == "group" else
                    self.single_call_group_id)
        if group_id is None or self.login_state != "success":
            return False
        group_id = int(group_id) & 0xFFFFFFFF
        if (call_type != "group" and
                self._group_audio_is_active()):
            return False
        if group_id in self.occupied_group_ids:
            return False
        self._lock_acquire()
        try:
            if self._floor_request_action is not None:
                # 单呼抢麦已发出时无法撤包，记录组呼目标；收到应答后立即
                # 释放可能取得的单呼麦权，再继续抢组呼麦权。
                if (call_type == "group" and
                        self.pending_floor_group_id ==
                        self.single_call_group_id):
                    self._next_floor_group_id = group_id
                    self._floor_changed()
                    return True
                return False
            if self.held_floor_group_id == group_id:
                if self.floor_udp_port is not None:
                    self._audio_notify("on_floor_granted", group_id,
                                       self.floor_udp_port)
                self.floor_state = "held"
                self.floor_error = None
                self._floor_changed()
                return True
            if self.held_floor_group_id is not None:
                # 组呼抢占正在讲话的单呼：先释放单呼麦权，再自动抢普通组麦权。
                if (call_type == "group" and
                        self.held_floor_group_id == self.single_call_group_id):
                    self._next_floor_group_id = group_id
                    self._floor_request_action = "release"
                    self._floor_request_group_id = self.held_floor_group_id
                    self.floor_state = "release_waiting"
                    self._floor_changed()
                    return True
                return False
            self.pending_floor_group_id = group_id
            self._floor_request_action = "request"
            self.floor_state = "request_waiting"
            self.floor_error = None
            self._floor_changed()
        finally:
            self._lock_release()
        return True

    def request_ptt_floor(self):
        """PTT按下入口：使用LCD最近选择的通话类型，组呼事件仍可抢占。"""
        if (self.preferred_call_type == "single" and
                self.single_call_group_id is not None):
            call_type = "single"
        elif self.confirmed_group_id is not None:
            call_type = "group"
        elif self.single_call_group_id is not None:
            call_type = "single"
        else:
            print("[POC] PTT抢麦忽略：没有可用的组呼或单呼组")
            return False
        self._ptt_pressed = True
        self.preferred_call_type = call_type
        result = self.request_floor(call_type)
        if not result:
            self._ptt_pressed = False
        return result

    def _request_ptt_release(self):
        """PTT松开入口；未拿到麦权时记为延迟释放。"""
        self._ptt_pressed = False
        # 先在本机立即停止PCM采集和RTP发送，0x09应答及重发不能阻塞停话。
        self._audio_notify("stop_transmit")
        if self.held_floor_group_id is not None:
            self._floor_event_changed(
                "self_released", self.held_floor_group_id)
        if self.held_floor_group_id is None:
            if (self._floor_request_action == "request" and
                    self._floor_request_cnt is None):
                # 0x08尚未发出时直接取消，避免一次极短点按仍占麦再释放。
                self.pending_floor_group_id = None
                self._floor_request_action = None
                self.floor_state = "idle"
                self._floor_changed()
                return True
            self._release_after_floor_grant = (
                self._floor_request_action == "request")
            return False
        self._release_after_floor_grant = False
        return self.release_floor()

    def release_floor(self):
        """公开释放麦权接口；释放包始终携带实际持有的组ID。"""
        group_id = self.held_floor_group_id
        if group_id is None or self.login_state != "success":
            return False
        self._lock_acquire()
        try:
            if self._floor_request_action is not None:
                return False
            self._floor_request_action = "release"
            self._floor_request_group_id = int(group_id) & 0xFFFFFFFF
            self.floor_state = "release_waiting"
            self._floor_changed()
        finally:
            self._lock_release()
        return True

    def _group_audio_is_active(self):
        """判断当前普通组是否正在本机讲话或被其他成员占用。"""
        group_id = self.confirmed_group_id
        return (group_id is not None and
                (self.held_floor_group_id == group_id or
                 self.active_audio_group_id == group_id or
                 int(group_id) in self.occupied_group_ids))

    def _person_key_by_device(self, device_id):
        for person in self.people:
            if str(person.get("device_id", "")) == str(device_id):
                return str(person.get("device_id") or
                           person.get("police_code") or
                           person.get("name") or "")
        return str(device_id)

    def _person_by_police_no(self, police_no):
        police_no = str(police_no)
        for person in self.people:
            if str(person.get("police_code", "")) == police_no:
                key = str(person.get("device_id") or
                          person.get("police_code") or
                          person.get("name") or police_no)
                return key, str(person.get("device_id", "")) or None
        return police_no, None

    def _call_type_text(self, group_id):
        """根据组编号判断本次麦权属于普通组呼还是单呼临时组。"""
        if group_id is None:
            return "未知通话"
        numeric = int(group_id) & 0xFFFFFFFF
        if (self.single_call_group_id is not None and
                numeric == int(self.single_call_group_id)):
            return "单呼"
        if (self.confirmed_group_id is not None and
                numeric == int(self.confirmed_group_id)):
            return "组呼"
        # 0x0B可能来自已加入但当前没有显示在界面上的普通组，继续从HTTP组表判断。
        for group in self.groups:
            try:
                if numeric == int(group.get("id")):
                    return "组呼"
            except Exception:
                pass
        return "未知通话"

    def _print_floor_state(self, group_id, state, police_name=None,
                           self_speaking=False):
        """统一输出组类型、组编号和讲话/空闲/释放状态。"""
        call_type = self._call_type_text(group_id)
        if self_speaking:
            print("[POC] 抢麦成功：{} 组编号={} 我在讲话中".format(
                call_type, group_id))
        elif police_name is not None:
            name = str(police_name) if police_name else "未知警员"
            print("[POC] {} 组编号={} {}讲话中".format(
                call_type, group_id, name))
        else:
            print("[POC] {} 组编号={} {}".format(
                call_type, group_id, state))

    def _send_join_group(self):
        self._lock_acquire()
        try:
            group_id = self._pending_join_id
            raw_id = self._pending_join_raw_id
        finally:
            self._lock_release()
        if group_id is None or self._socket is None:
            return
        request_cnt = self._next_request_cnt()
        packet = build_frame(
            MSG_JOIN_GROUP, request_cnt,
            build_join_group_payload(self.device_id, group_id, self._police_no))
        self._send_all(packet)
        self._lock_acquire()
        try:
            self._pending_join_id = None
            self._join_request_cnt = request_cnt
            self._join_request_group_id = group_id
            self._join_request_raw_id = raw_id
            self._join_packet = packet
            self._join_retry_count = 0
            self._join_sent_ms = utime.ticks_ms()
            self.join_state = "requesting"
            self._join_changed()
        finally:
            self._lock_release()
        print("[POC] 已发送入组包：{}，raw_group_id={}".format(
            self._hex(packet), raw_id))

    def _send_single_control(self):
        """发送单呼状态机当前步骤；解散成功前绝不会提前邀请新人。"""
        if self._socket is None or self._single_request_cnt is not None:
            return
        action = self._single_action
        if action == "invite":
            target = self._single_target_device_id
            target_police = self._single_target_police_no
            police_no = self._police_no or self._login_police_no
            if not target or not target_police or not police_no:
                return
            request_cnt = self._next_request_cnt()
            packet = build_frame(
                MSG_INVITE_SINGLE, request_cnt,
                build_invite_single_payload(
                    self.device_id, target, police_no, target_police))
            self._send_all(packet)
            self._single_request_cnt = request_cnt
            self._single_packet = packet
            self._single_retry_count = 0
            self._single_invite_ack1_seen = False
            self._single_sent_ms = utime.ticks_ms()
            self.single_state = "inviting"
            self._single_changed()
            print("[POC] 已发送单呼邀请：target={}，frame={}".format(
                target, self._hex(packet)))
        elif action == "dissolve":
            group_id = self.single_call_group_id
            if group_id is None:
                self._after_dissolve_success()
                return
            request_cnt = self._next_request_cnt()
            packet = build_frame(
                MSG_DISSOLVE_SINGLE, request_cnt,
                build_dissolve_single_payload(
                    self.device_id,
                    self._police_no or self._login_police_no,
                    group_id))
            self._send_all(packet)
            self._single_request_cnt = request_cnt
            self._single_packet = packet
            self._single_retry_count = 0
            self._single_sent_ms = utime.ticks_ms()
            self.single_state = "dissolving"
            self._single_changed()
            print("[POC] 已发送解散单呼：group_id={}，frame={}".format(
                group_id, self._hex(packet)))

    def _finish_logout(self, success=True, error=None):
        self._logout_request_cnt = None
        self._logout_packet = None
        self._logout_sent_ms = None
        self._logout_retry_count = 0
        self.logout_state = "success" if success else "failed"
        self.logout_error = None if error is None else str(error)
        if success:
            self._login_session_active = False
            self._pending_login = False
            self.login_state = "idle"
            self.login_result = 0
            self.confirmed_group_id = None
            self.confirmed_group_raw_id = None
            self.join_state = "idle"
            self.single_call_group_id = None
            self.single_call_person_key = None
            self.single_call_person_device_id = None
            self.single_state = "none"
            self._police_no = None
        if self._server_logout_pending:
            self.logout_state = "success"
        self._logout_changed()

    def _prepare_single_for_logout(self):
        """取消排队邀请，只保留当前临时组所需的解散步骤。"""
        self._single_next_key = None
        self._single_next_device_id = None
        self._single_next_police_no = None
        if (self.single_call_group_id is not None and
                self._single_action == "dissolve"):
            return

        self._single_request_cnt = None
        self._single_packet = None
        self._single_sent_ms = None
        self._single_retry_count = 0
        self._single_invite_ack1_seen = False
        self._single_target_key = None
        self._single_target_device_id = None
        self._single_target_police_no = None
        if self.single_call_group_id is not None:
            self._single_action = "dissolve"
            self.single_state = "dissolve_waiting"
        else:
            self._single_action = None
            self.single_state = "none"
        self.single_error = None
        self._single_changed()

    def _clear_forced_logout(self):
        self._single_action = None
        self._single_request_cnt = None
        self._single_packet = None
        self._single_sent_ms = None
        self._single_retry_count = 0
        self._single_target_key = None
        self._single_target_device_id = None
        self._single_target_police_no = None
        self._single_next_key = None
        self._single_next_device_id = None
        self._single_next_police_no = None
        self._single_invite_ack1_seen = False
        self._leave_group_pending = False
        self._leave_group_request_cnt = None
        self._leave_group_request_group_id = None
        self._leave_group_packet = None
        self._leave_group_sent_ms = None
        self._leave_group_retry_count = 0
        self.confirmed_group_id = None
        self.confirmed_group_raw_id = None
        self.single_call_group_id = None
        self.single_call_person_key = None
        self.single_call_person_device_id = None
        self.single_state = "none"
        self.join_state = "idle"
        self.join_error = None
        self._audio_notify("on_floor_released", None)
        self.held_floor_group_id = None
        self.active_audio_group_id = None
        self.floor_udp_port = None
        self.occupied_group_ids = {}
        self.pending_floor_group_id = None
        self._floor_request_cnt = None
        self._floor_request_group_id = None
        self._floor_request_action = None
        self._floor_sent_ms = None
        self._floor_packet = None
        self._floor_retry_count = 0
        self._next_floor_group_id = None
        self.floor_state = "idle"
        self._floor_changed()
        self._join_changed()
        self._single_changed()

    def _continue_logout_cleanup(self):
        if self.single_call_group_id is not None and self._single_action is None:
            self._single_action = "dissolve"
            return
        if self._single_action is not None or self._single_request_cnt is not None:
            return
        if self.confirmed_group_id is not None:
            self._leave_group_pending = True
            return
        self._clear_forced_logout()
        if self._server_logout_pending:
            request_cnt = self._server_logout_request_cnt
            if request_cnt is not None:
                self._send_server_ack(MSG_LOGOUT_ACK, request_cnt,
                                      build_logout_ack_payload(0))
            self._server_logout_pending = False
            self._server_logout_request_cnt = None
            self._login_session_active = False
            self._pending_login = False
            self._police_no = None
            self.login_state = "idle"
            self.login_result = 0
            self.logout_state = "success"
            self.logout_error = "设备已被强制退出"
            self._logout_changed()
            self._close_socket()
        else:
            self._send_logout()

    def _after_dissolve_success(self):
        """清除旧临时组，并按用户最后选择决定是否继续邀请新人。"""
        old_group_id = self.single_call_group_id
        self.single_call_group_id = None
        self.single_call_person_key = None
        self.single_call_person_device_id = None
        if self.held_floor_group_id == old_group_id:
            self.held_floor_group_id = None
        if self.active_audio_group_id == old_group_id:
            self.active_audio_group_id = None
        if old_group_id is not None:
            self._audio_notify("on_floor_released", old_group_id)
        if old_group_id is not None:
            old_group_id = int(old_group_id)
            if old_group_id in self.occupied_group_ids:
                del self.occupied_group_ids[old_group_id]
        self._single_request_cnt = None
        self._single_sent_ms = None
        self._single_packet = None
        self._single_retry_count = 0
        self._single_invite_ack1_seen = False
        if self.logout_state == "cleaning":
            next_key = None
            next_device = None
            next_police = None
        else:
            next_key = self._single_next_key
            next_device = self._single_next_device_id
            next_police = self._single_next_police_no
        self._single_next_key = None
        self._single_next_device_id = None
        self._single_next_police_no = None
        if next_key is not None and next_device and next_police:
            self._single_target_key = next_key
            self._single_target_device_id = next_device
            self._single_target_police_no = next_police
            self._single_action = "invite"
            self.single_state = "invite_waiting"
        else:
            self._single_target_key = None
            self._single_target_device_id = None
            self._single_target_police_no = None
            self._single_action = None
            self.single_state = "none"
        self.single_error = None
        self._single_changed()
        if self.logout_state == "cleaning":
            self._continue_logout_cleanup()

    def _send_floor_control(self):
        if (self._socket is None or self._floor_request_cnt is not None or
                not self._police_no):
            return
        action = self._floor_request_action
        if action not in ("request", "release"):
            return
        group_id = (self.pending_floor_group_id if action == "request" else
                    self._floor_request_group_id)
        if group_id is None:
            return
        request_cnt = self._next_request_cnt()
        msg_type = (MSG_REQUEST_FLOOR if action == "request" else
                    MSG_RELEASE_FLOOR)
        packet = build_frame(
            msg_type, request_cnt,
            build_floor_payload(self.device_id, self._police_no, group_id))
        self._send_all(packet)
        self._floor_request_cnt = request_cnt
        self._floor_packet = packet
        self._floor_retry_count = 0
        self._floor_request_group_id = int(group_id) & 0xFFFFFFFF
        self._floor_sent_ms = utime.ticks_ms()
        self.floor_state = ("requesting" if action == "request" else
                            "releasing")
        self._floor_changed()
        print("[POC] 已发送{}麦：group_id={}，frame={}".format(
            "抢" if action == "request" else "释放",
            group_id, self._hex(packet)))

    def _send_server_ack(self, msg_type, request_cnt, payload):
        """服务器主动请求的应答计数固定为请求计数+1，按UINT16回绕。"""
        packet = build_frame(msg_type, (int(request_cnt) + 1) & 0xFFFF,
                             payload)
        # Mark done before sending so a retry can reuse the exact same frame.
        self._cache_server_response(request_cnt, packet)
        self._send_all(packet)
        print("[POC] 已发送应答0x{:02X}：{}".format(
            msg_type, self._hex(packet)))

    def _recv_once(self):
        if self._socket is None:
            return
        try:
            data = self._socket.recv(1024)
        except Exception as error:
            name = str(error).lower()
            code = None
            try:
                code = int(error.args[0])
            except Exception:
                pass
            # 不同QuecPython固件可能以EAGAIN、ETIMEDOUT或116表示轮询超时。
            if (code in (11, 110, 116) or "timed out" in name or
                    "timeout" in name or "eagain" in name):
                return
            raise
        if not data:
            raise OSError("TCP服务器主动断开")
        # 收到TCP数据即认为链路有业务活动；完整帧随后仍由解析器处理。
        self._mark_tcp_activity()
        for frame in self._parser.feed(data):
            self._handle_frame(frame)

    def _finish_single_failure(self, message):
        """结束当前单呼控制请求；真实临时组状态不因失败而伪造变化。"""
        self._single_request_cnt = None
        self._single_sent_ms = None
        self._single_packet = None
        self._single_retry_count = 0
        self._single_invite_ack1_seen = False
        self._single_action = None
        self._single_target_key = None
        self._single_target_device_id = None
        self._single_target_police_no = None
        self._single_next_key = None
        self._single_next_device_id = None
        self._single_next_police_no = None
        self.single_state = "failed"
        self.single_error = str(message)
        self._single_changed()
        print("[POC] 单呼操作失败：{}".format(message))
        if self.logout_state == "cleaning":
            self.single_call_group_id = None
            self.single_call_person_key = None
            self.single_call_person_device_id = None
            self._continue_logout_cleanup()

    def _handle_invite_ack(self, frame):
        if (self._single_action != "invite" or
                self._single_request_cnt is None or
                frame["request_cnt"] !=
                ((self._single_request_cnt + 1) & 0xFFFF)):
            print("[POC] 单呼邀请应答request_cnt不匹配，已忽略")
            return
        try:
            ack = parse_invite_single_ack(frame["payload"])
        except Exception as error:
            self._finish_single_failure(error)
            return
        ack_num = ack["ack_num"]
        status = ack["invite_status"]
        if ack_num == 1:
            if status != 0x06:
                self._finish_single_failure(
                    "邀请首次应答失败码=0x{:02X}".format(status))
                return
            if self._single_invite_ack1_seen:
                print("[POC] 重复收到单呼处理中应答，继续等待最终结果")
                return
            # 已受理后停止重发原邀请，继续等待相同request_cnt的最终应答。
            self._single_invite_ack1_seen = True
            self.single_state = "invite_accepted"
            self._single_sent_ms = utime.ticks_ms()
            self._single_packet = None
            self._single_retry_count = 0
            self._single_changed()
            print("[POC] 单呼邀请已受理，等待对方应答")
            return
        if not self._single_invite_ack1_seen:
            self._finish_single_failure("邀请最终应答缺少处理中应答")
            return
        if status != 0x00:
            self._finish_single_failure(
                "邀请失败码=0x{:02X}".format(status))
            return
        # 成功必须由ack_num=2返回有效的UINT32临时组ID。
        group_id = ack.get("group_id")
        if group_id in (None, 0):
            self._finish_single_failure("邀请成功应答缺少有效临时组ID")
            return
        self.single_call_group_id = int(group_id) & 0xFFFFFFFF
        self.single_call_person_key = self._single_target_key
        self.single_call_person_device_id = self._single_target_device_id
        self._single_request_cnt = None
        self._single_sent_ms = None
        self._single_packet = None
        self._single_retry_count = 0
        self._single_invite_ack1_seen = False
        self._single_action = None
        self._single_target_key = None
        self._single_target_device_id = None
        self._single_target_police_no = None
        self._single_next_key = None
        self._single_next_device_id = None
        self._single_next_police_no = None
        self.single_state = "active"
        self.single_error = None
        self._single_changed()
        print("[POC] 单呼建立成功：group_id={}".format(group_id))

    def _handle_single_invited(self, frame):
        try:
            request = parse_single_invited(frame["payload"])
            caller_police = request["src_police_no"].decode("ascii")
            group_id = request["group_id"]
        except Exception as error:
            print("[POC] 受邀单呼解析失败：{}".format(error))
            return
        # 普通组正在讲话时组呼优先；已有单呼临时组时也拒绝新邀请。
        if self._group_audio_is_active():
            answer_status = 0x02
        elif (self.single_call_group_id is not None or
              self._single_action is not None):
            answer_status = 0x04
        else:
            answer_status = 0x00
        local_police = self._police_no or self._login_police_no
        if not local_police:
            print("[POC] 未登录，无法应答受邀单呼")
            return
        self._send_server_ack(
            MSG_SINGLE_INVITED_ACK, frame["request_cnt"],
            build_single_invited_ack_payload(
                self.device_id,
                local_police,
                group_id, answer_status))
        if answer_status == 0x00:
            caller_key, caller_device = self._person_by_police_no(caller_police)
            self.single_call_group_id = group_id
            self.single_call_person_device_id = caller_device
            self.single_call_person_key = caller_key
            self.preferred_call_type = "single"
            self.single_state = "active"
            self.single_error = None
            self._single_changed()
            print("[POC] 已接受单呼邀请：src_police_no={}，group_id={}".format(
                caller_police, group_id))
        else:
            print("[POC] 已拒绝单呼邀请：status=0x{:02X}".format(
                answer_status))

    def _handle_dissolve_request(self, frame):
        try:
            request = parse_dissolve_single(frame["payload"])
            group_id = request["group_id"]
        except Exception as error:
            print("[POC] 解散单呼请求解析失败：{}".format(error))
            return
        expected_police = self._police_no or self._login_police_no
        success = (
            request["device_id"] == self.device_id.encode("ascii") and
            expected_police is not None and
            request["police_no"] == expected_police.encode("ascii") and
            self.single_call_group_id == group_id)
        self._send_server_ack(
            MSG_DISSOLVE_SINGLE_ACK, frame["request_cnt"],
            build_dissolve_single_ack_payload(
                self.device_id, 0x00 if success else 0x01))
        if success:
            self._after_dissolve_success()
            print("[POC] 服务器请求解散单呼成功：group_id={}".format(group_id))

    def _handle_dissolve_ack(self, frame):
        if (self._single_action != "dissolve" or
                self._single_request_cnt is None or
                frame["request_cnt"] !=
                ((self._single_request_cnt + 1) & 0xFFFF)):
            print("[POC] 解散单呼应答request_cnt不匹配，已忽略")
            return
        try:
            ack = parse_dissolve_single_ack(frame["payload"])
        except Exception as error:
            self._finish_single_failure(error)
            return
        if ack["device_id"] != self.device_id.encode("ascii"):
            print("[POC] 解散单呼应答device_id不匹配，已忽略")
            return
        if ack["release_result"] == 0:
            self._after_dissolve_success()
            print("[POC] 旧单呼组解散成功")
        else:
            # 解散失败时旧会话仍真实存在，UI恢复旧人员。
            self._finish_single_failure(
                "解散失败码=0x{:02X}".format(ack["release_result"]))

    def _handle_server_logout(self, frame):
        """服务器强制退出：应答0x8F前清理现有通话，超时也继续退出。"""
        try:
            request = parse_logout_payload(frame["payload"])
        except Exception as error:
            print("[POC] 服务器退出登录请求解析失败：{}".format(error))
            return
        expected_police = self._police_no or self._login_police_no
        if (request["device_id"] != self.device_id.encode("ascii") or
                not expected_police or
                request["police_no"] != expected_police.encode("ascii")):
            print("[POC] 服务器退出登录请求身份不匹配，已忽略")
            return
        self._server_logout_pending = True
        self._server_logout_request_cnt = frame["request_cnt"]
        self._login_session_active = False
        self._pending_login = False
        self.logout_state = "cleaning"
        self.logout_error = "设备已被强制退出"
        self._logout_changed()
        self._audio_notify("on_floor_released", None)
        self._ptt_pressed = False
        self._release_after_floor_grant = False
        self.pending_floor_group_id = None
        self.held_floor_group_id = None
        self.active_audio_group_id = None
        self.floor_udp_port = None
        self._floor_request_cnt = None
        self._floor_request_group_id = None
        self._floor_request_action = None
        self._floor_sent_ms = None
        self._floor_packet = None
        self._floor_retry_count = 0
        self._next_floor_group_id = None
        self.floor_state = "idle"
        self._floor_changed()
        # Cleanup is serialized: dissolve the single call first, then leave
        # the normal group, and only then acknowledge logout.
        self._leave_group_pending = False
        self._prepare_single_for_logout()
        self._continue_logout_cleanup()

    def _handle_logout_ack(self, frame):
        if (self._logout_request_cnt is None or
                frame["request_cnt"] != ((self._logout_request_cnt + 1) & 0xFFFF)):
            return
        try:
            ack = parse_logout_ack(frame["payload"])
        except Exception as error:
            self._finish_logout(False, error)
            return
        self._logout_request_cnt = None
        self._logout_packet = None
        self._logout_sent_ms = None
        self._logout_retry_count = 0
        if ack["logout_result"] == 0:
            self._finish_logout(True)
        else:
            self._finish_logout(False, "退出失败码=0x{:02X}".format(ack["logout_result"]))

    def _handle_leave_group_ack(self, frame):
        if (self._leave_group_request_cnt is None or
                frame["request_cnt"] != ((self._leave_group_request_cnt + 1) & 0xFFFF)):
            return
        try:
            ack = parse_leave_group_ack(frame["payload"])
        except Exception as error:
            print("[POC] 退出组呼应答解析失败，等待重试或超时：{}".format(error))
            return
        expected_police = self._police_no or self._login_police_no
        expected_group = self._leave_group_request_group_id
        if (ack["device_id"] != self.device_id.encode("ascii") or
                not expected_police or
                ack["police_no"] != expected_police.encode("ascii") or
                ack["group_id"] != expected_group):
            print("[POC] 退出组呼应答字段不匹配，已忽略")
            return
        self._leave_group_request_cnt = None
        self._leave_group_request_group_id = None
        self._leave_group_packet = None
        self._leave_group_sent_ms = None
        self._leave_group_retry_count = 0
        self._leave_group_pending = False
        if ack["join_result"] == 0x00:
            self.confirmed_group_id = None
            self.confirmed_group_raw_id = None
            self.join_state = "idle"
            self.join_error = None
            print("[POC] 退出组呼成功：group_id={}".format(expected_group))
        else:
            print("[POC] 退出组呼失败：join_result=0x{:02X}".format(
                ack["join_result"]))
            if self.logout_state == "cleaning":
                # Forced logout continues even when call cleanup is rejected.
                self.confirmed_group_id = None
                self.confirmed_group_raw_id = None
                self.join_state = "idle"
                self.join_error = None
            else:
                self.join_state = "failed"
                self.join_error = "退出组呼失败码=0x{:02X}".format(
                    ack["join_result"])
        self._join_changed()
        if self.logout_state == "cleaning":
            self._continue_logout_cleanup()

    def _handle_floor_request_ack(self, frame):
        if (self._floor_request_action != "request" or
                self._floor_request_cnt is None or
                frame["request_cnt"] !=
                ((self._floor_request_cnt + 1) & 0xFFFF)):
            print("[POC] 抢麦应答request_cnt不匹配，已忽略")
            return
        try:
            ack = parse_request_floor_ack(frame["payload"])
        except Exception as error:
            self.floor_error = str(error)
            ack = {"floor_status": 0xFF, "udp_port": 0}
        group_id = self._floor_request_group_id
        release_after_ack = False
        if ack["floor_status"] == 0:
            preempt_single = (
                group_id == self.single_call_group_id and
                (self._group_audio_is_active() or
                 self._next_floor_group_id is not None))
            self.held_floor_group_id = group_id
            self.active_audio_group_id = group_id
            self.floor_udp_port = ack["udp_port"]
            self.floor_state = "held"
            self.floor_error = None
            self._print_floor_state(
                group_id, "讲话中", self_speaking=True)
            release_after_ack = (
                preempt_single or self._release_after_floor_grant or
                not self._ptt_pressed)
            if not release_after_ack:
                event_revision = self._floor_event_changed(
                    "self_granted", group_id)
                self._audio_notify("on_floor_granted", group_id,
                                   ack["udp_port"], event_revision)
            if release_after_ack:
                self._release_after_floor_grant = False
        else:
            self.floor_state = "failed"
            self.floor_error = "抢麦失败码=0x{:02X}".format(
                ack["floor_status"])
            self._release_after_floor_grant = False
            self._floor_event_changed("request_failed", group_id)
        self.pending_floor_group_id = None
        self._floor_request_cnt = None
        self._floor_request_group_id = None
        self._floor_sent_ms = None
        self._floor_packet = None
        self._floor_retry_count = 0
        if release_after_ack:
            self._floor_request_action = "release"
            self._floor_request_group_id = group_id
            self.floor_state = "release_waiting"
        elif ack["floor_status"] != 0 and self._next_floor_group_id is not None:
            self.pending_floor_group_id = self._next_floor_group_id
            self._next_floor_group_id = None
            self._floor_request_action = "request"
            self.floor_state = "request_waiting"
        else:
            self._floor_request_action = None
        self._floor_changed()

    def _handle_floor_release_ack(self, frame):
        if (self._floor_request_action != "release" or
                self._floor_request_cnt is None or
                frame["request_cnt"] !=
                ((self._floor_request_cnt + 1) & 0xFFFF)):
            print("[POC] 释放麦应答request_cnt不匹配，已忽略")
            return
        try:
            ack = parse_release_floor_ack(frame["payload"])
        except Exception as error:
            self.floor_error = str(error)
            ack = {"device_id": b"", "group_id": None,
                   "release_status": 0xFF}
        expected_group = self._floor_request_group_id
        success = (ack["device_id"] == self.device_id.encode("ascii") and
                   ack["group_id"] == expected_group and
                   ack["release_status"] == 0)
        next_group = self._next_floor_group_id
        self._next_floor_group_id = None
        # PTT已经松开，匹配request_cnt的0x89无论业务状态码如何，
        # 本机都结束持麦，避免服务器异常使采集状态永久卡住。
        if self.held_floor_group_id == expected_group:
            self.held_floor_group_id = None
        if self.active_audio_group_id == expected_group:
            self.active_audio_group_id = None
            self.floor_udp_port = None
        self._audio_notify("on_floor_released", expected_group)
        if success:
            # 在清除组编号状态前打印，保证能正确区分组呼和单呼。
            self._print_floor_state(expected_group, "释放")
            self.floor_state = "idle"
            self.floor_error = None
            print("[POC] 释放成功")
            self._floor_event_changed("self_released", expected_group)
        else:
            self.floor_state = "idle"
            self.floor_error = "释放应答校验或状态失败，本机已完成释放"
            print("[POC] 释放失败：{}".format(self.floor_error))
            self._floor_event_changed("self_released", expected_group)
        self._floor_request_cnt = None
        self._floor_request_group_id = None
        self._floor_request_action = None
        self._floor_sent_ms = None
        self._floor_packet = None
        self._floor_retry_count = 0
        if next_group is not None:
            self.pending_floor_group_id = next_group
            self._floor_request_action = "request"
            self.floor_state = "request_waiting"
        self._floor_changed()

    def _finish_floor_timeout(self):
        """麦权请求最终超时；释放超时时本机仍必须完成资源清理。"""
        action = self._floor_request_action
        group_id = self._floor_request_group_id
        next_group = self._next_floor_group_id
        self._next_floor_group_id = None
        self.pending_floor_group_id = None
        self._floor_request_cnt = None
        self._floor_request_group_id = None
        self._floor_request_action = None
        self._floor_sent_ms = None
        self._floor_packet = None
        self._floor_retry_count = 0
        if action == "release":
            # 即使服务器没有应答，本机也不继续保持虚假的持麦和采集状态。
            if self.held_floor_group_id == group_id:
                self.held_floor_group_id = None
            if self.active_audio_group_id == group_id:
                self.active_audio_group_id = None
                self.floor_udp_port = None
            self._audio_notify("on_floor_released", group_id)
            self.floor_state = "idle"
            self.floor_error = "释放麦应答超时"
            print("[POC] 释放超时失败")
            self._floor_event_changed("self_released", group_id)
            if next_group is not None:
                self.pending_floor_group_id = next_group
                self._floor_request_action = "request"
                self.floor_state = "request_waiting"
        else:
            self._release_after_floor_grant = False
            self.floor_state = "failed"
            self.floor_error = "抢麦应答超时"
            print("[POC] 抢麦失败：应答超时")
            self._floor_event_changed("request_failed", group_id)
        self._floor_changed()

    def _process_tcp_request_timeouts(self):
        """轮询全部设备主动TCP请求，并按递增等待时间重发原始帧。"""
        if self._login_sent_ms is not None:
            (self._login_sent_ms, self._login_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._login_packet, self._login_sent_ms,
                self._login_retry_count, "登录包")
            if timed_out:
                self._login_sent_ms = None
                self._login_request_cnt = None
                self._login_packet = None
                self._login_retry_count = 0
                self._set_login_state("failed", "登录应答超时", None)
                print("[POC] 登录失败：应答超时")

        if self._force_login_sent_ms is not None:
            (self._force_login_sent_ms, self._force_login_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._force_login_packet, self._force_login_sent_ms,
                self._force_login_retry_count, "转机登录包")
            if timed_out:
                self._force_login_sent_ms = None
                self._force_login_request_cnt = None
                self._force_login_packet = None
                self._force_login_retry_count = 0
                self._set_login_state("failed", "转机登录应答超时", None)

        if self._logout_sent_ms is not None:
            (self._logout_sent_ms, self._logout_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._logout_packet, self._logout_sent_ms,
                self._logout_retry_count, "退出登录包")
            if timed_out:
                self._finish_logout(False, "退出登录应答超时")

        if self._leave_group_sent_ms is not None:
            (self._leave_group_sent_ms, self._leave_group_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._leave_group_packet, self._leave_group_sent_ms,
                self._leave_group_retry_count, "退出组包")
            if timed_out:
                self._leave_group_request_cnt = None
                self._leave_group_request_group_id = None
                self._leave_group_packet = None
                self._leave_group_sent_ms = None
                self._leave_group_retry_count = 0
                if self.logout_state == "cleaning":
                    self.confirmed_group_id = None
                    self.confirmed_group_raw_id = None
                    self.join_state = "idle"
                    self.join_error = None
                    self._continue_logout_cleanup()
                else:
                    self.join_state = "failed"
                    self.join_error = "退出组呼应答超时"
                    self._join_changed()

        if self._heartbeat_sent_ms is not None:
            (self._heartbeat_sent_ms, self._heartbeat_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._heartbeat_packet, self._heartbeat_sent_ms,
                self._heartbeat_retry_count, "TCP心跳包")
            if timed_out:
                self._heartbeat_request_cnt = None
                self._heartbeat_sent_ms = None
                self._heartbeat_packet = None
                self._heartbeat_retry_count = 0
                # 心跳无应答不伪造链路断开；重新按空闲周期计时。
                self._tcp_last_activity_ms = utime.ticks_ms()
                print("[POC] TCP心跳应答超时")

        if (self._gnss_sent_ms is not None and
                not self._gnss_pause_active()):
            (self._gnss_sent_ms, self._gnss_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._gnss_packet, self._gnss_sent_ms,
                self._gnss_retry_count, "定位信息上报包")
            if timed_out:
                self._clear_gnss_request()
                print("[GNSS] 定位信息上报失败：0x8D应答超时")

        if self._join_sent_ms is not None:
            (self._join_sent_ms, self._join_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._join_packet, self._join_sent_ms,
                self._join_retry_count, "入组包")
            if timed_out:
                self._join_sent_ms = None
                self._join_request_cnt = None
                self._join_request_group_id = None
                self._join_request_raw_id = None
                self._join_packet = None
                self._join_retry_count = 0
                self.join_state = "failed"
                self.join_error = "入组应答超时"
                self._join_changed()
                print("[POC] 入组失败：应答超时")

        if self._single_sent_ms is not None:
            # 0x84受理中之后原包已清空，只给最终应答保留10秒等待时间。
            if self._single_packet is None:
                if (self._elapsed_ms(self._single_sent_ms) >=
                        INVITE_FINAL_RESPONSE_TIMEOUT_MS):
                    self._finish_single_failure("单呼最终应答超时")
            else:
                (self._single_sent_ms, self._single_retry_count,
                 timed_out) = self._retry_packet_if_due(
                    self._single_packet, self._single_sent_ms,
                    self._single_retry_count, "单呼控制包")
                if timed_out:
                    self._finish_single_failure("单呼应答超时")

        if self._floor_sent_ms is not None:
            (self._floor_sent_ms, self._floor_retry_count,
             timed_out) = self._retry_packet_if_due(
                self._floor_packet, self._floor_sent_ms,
                self._floor_retry_count,
                "释放麦包" if self._floor_request_action == "release"
                else "抢麦包")
            if timed_out:
                self._finish_floor_timeout()

    def _preempt_single_floor(self):
        """组呼事件抢占单呼语音；只结束讲话，不解散单呼临时组。"""
        if (self.single_call_group_id is None or
                self.held_floor_group_id != self.single_call_group_id):
            return
        if self._floor_request_action is None:
            self._floor_request_action = "release"
            self._floor_request_group_id = self.held_floor_group_id
            self.floor_state = "release_waiting"
            self._floor_changed()

    def _handle_floor_occupied(self, frame):
        try:
            notice = parse_floor_occupied(frame["payload"])
        except Exception as error:
            print("[POC] 麦权占用通知解析失败：{}".format(error))
            return
        group_id = notice["group_id"]
        self.occupied_group_ids[group_id] = {
            "police_name": notice["police_name"],
            "udp_port": notice["udp_port"]}
        self._print_floor_state(
            group_id, "讲话中", police_name=notice["police_name"])
        event_revision = self._floor_event_changed(
            "remote_occupied", group_id, notice["police_name"])
        if group_id == self.confirmed_group_id:
            self._preempt_single_floor()
        if group_id in (self.confirmed_group_id, self.single_call_group_id):
            current_is_group = (
                self.active_audio_group_id == self.confirmed_group_id)
            incoming_is_group = (group_id == self.confirmed_group_id)
            # 组呼正在播放时忽略单呼音频；组呼到来则立即抢占单呼播放。
            if incoming_is_group or not current_is_group:
                self.active_audio_group_id = group_id
                self.floor_udp_port = notice["udp_port"]
                self._audio_notify("on_floor_occupied", group_id,
                                   notice["udp_port"], event_revision)
        self._send_server_ack(
            MSG_FLOOR_OCCUPIED_ACK, frame["request_cnt"],
            build_device_payload(self.device_id))
        self._floor_changed()

    def _handle_floor_idle(self, frame):
        try:
            group_id = parse_floor_idle(frame["payload"])["group_id"]
        except Exception as error:
            print("[POC] 麦权空闲通知解析失败：{}".format(error))
            return
        # 只清除完全匹配的组，其他组的占用状态保持不变。
        if group_id in self.occupied_group_ids:
            del self.occupied_group_ids[group_id]
        self._print_floor_state(group_id, "空闲")
        self._floor_event_changed("remote_idle", group_id)
        if (self.active_audio_group_id == group_id and
                self.held_floor_group_id != group_id):
            self.active_audio_group_id = None
            self._audio_notify("on_floor_idle", group_id)
            # 组呼结束后，如单呼组仍处于占麦状态，恢复单呼音频。
            single_id = self.single_call_group_id
            if (single_id is not None and
                    single_id in self.occupied_group_ids):
                item = self.occupied_group_ids[single_id]
                self.active_audio_group_id = single_id
                self.floor_udp_port = item["udp_port"]
                self._audio_notify("on_floor_occupied", single_id,
                                   item["udp_port"])
        self._send_server_ack(
            MSG_FLOOR_IDLE_ACK, frame["request_cnt"],
            build_floor_idle_ack_payload(self.device_id, group_id))
        self._floor_changed()

    def _handle_control_frame(self, frame):
        """处理0x04以后单呼和麦权信令；返回是否已消费。"""
        msg_type = frame["msg_type"]
        if msg_type in SERVER_REQUEST_TYPES:
            if not self._begin_server_request(frame):
                return True
        if msg_type == MSG_LOGOUT:
            self._handle_server_logout(frame)
        elif msg_type == MSG_LOGOUT_ACK:
            self._handle_logout_ack(frame)
        elif msg_type == MSG_LEAVE_GROUP_ACK:
            self._handle_leave_group_ack(frame)
        elif msg_type == MSG_INVITE_SINGLE_ACK:
            self._handle_invite_ack(frame)
        elif msg_type == MSG_SINGLE_INVITED:
            self._handle_single_invited(frame)
        elif msg_type == MSG_DISSOLVE_SINGLE:
            self._handle_dissolve_request(frame)
        elif msg_type == MSG_DISSOLVE_SINGLE_ACK:
            self._handle_dissolve_ack(frame)
        elif msg_type == MSG_REQUEST_FLOOR_ACK:
            self._handle_floor_request_ack(frame)
        elif msg_type == MSG_RELEASE_FLOOR_ACK:
            self._handle_floor_release_ack(frame)
        elif msg_type == MSG_FORCE_RELEASE_FLOOR:
            # 0x0A不带组ID，按协议强制结束设备当前持有的麦权。
            released_group_id = self.held_floor_group_id
            if released_group_id is not None:
                self._print_floor_state(released_group_id, "释放")
            self.held_floor_group_id = None
            self.pending_floor_group_id = None
            if self.active_audio_group_id == released_group_id:
                self.active_audio_group_id = None
                self.floor_udp_port = None
            self._audio_notify("on_floor_released", released_group_id)
            self._floor_event_changed("self_released", released_group_id)
            self._floor_request_cnt = None
            self._floor_request_group_id = None
            self._floor_request_action = None
            self._floor_sent_ms = None
            self._floor_packet = None
            self._floor_retry_count = 0
            self._next_floor_group_id = None
            self.floor_state = "idle"
            self._send_server_ack(
                MSG_FORCE_RELEASE_FLOOR_ACK, frame["request_cnt"],
                build_device_payload(self.device_id))
            self._floor_changed()
        elif msg_type == MSG_FLOOR_OCCUPIED:
            self._handle_floor_occupied(frame)
        elif msg_type == MSG_FLOOR_IDLE:
            self._handle_floor_idle(frame)
        else:
            return False
        return True

    def _handle_frame(self, frame):
        try:
            print("[TCP接收] 信令=0x{:02X} {}，消息体={}".format(
                frame["msg_type"],
                TCP_SIGNAL_NAMES.get(frame["msg_type"], "未知信令"),
                self._hex(frame["payload"])))
        except Exception:
            pass
        print("[POC] 收到TCP完整帧：{}".format(self._hex(frame["raw"])))
        print("[POC] 字段解析：type=0x{:02X}, length={}, request_cnt={}, payload={}".format(
            frame["msg_type"], frame["packet_length"],
            frame["request_cnt"], self._hex(frame["payload"])))
        if self._handle_control_frame(frame):
            return
        if frame["msg_type"] == MSG_GNSS_UPLOAD_ACK:
            if (self._gnss_request_cnt is None or
                    frame["request_cnt"] !=
                    ((self._gnss_request_cnt + 1) & 0xFFFF)):
                print("[GNSS] 0x8D应答request_cnt不匹配，已忽略")
                return
            try:
                parse_gnss_upload_ack(frame["payload"])
            except Exception as error:
                print("[GNSS] 0x8D应答格式错误：{}".format(error))
                return
            self._clear_gnss_request()
            print("[GNSS] 定位信息上报成功")
            return
        if frame["msg_type"] == MSG_HEARTBEAT_ACK:
            if (self._heartbeat_request_cnt is not None and
                    frame["request_cnt"] ==
                    ((self._heartbeat_request_cnt + 1) & 0xFFFF)):
                self._heartbeat_request_cnt = None
                self._heartbeat_sent_ms = None
                self._heartbeat_packet = None
                self._heartbeat_retry_count = 0
                print("[POC] 收到TCP心跳应答")
            else:
                print("[POC] TCP心跳应答request_cnt不匹配，已忽略")
            return
        if frame["msg_type"] == MSG_FORCE_LOGIN_ACK:
            if (self._force_login_request_cnt is None or
                    frame["request_cnt"] != ((self._force_login_request_cnt + 1) & 0xFFFF)):
                return
            try:
                result = parse_force_login_ack(frame["payload"])["login_result"]
            except Exception as error:
                result = None
                self.login_error = str(error)
            self._force_login_request_cnt = None
            self._force_login_packet = None
            self._force_login_sent_ms = None
            self._force_login_retry_count = 0
            if result == 0:
                self._login_session_active = True
                self._police_no = self._login_police_no or self._police_no
                self._set_login_state("success", None, result)
                self._mark_tcp_activity()
                self.request_http_refresh("登录初始化", auto_join_first=True)
            else:
                self._login_session_active = False
                if result == 0x01:
                    error = "无法转机登录"
                elif result == 0x02:
                    error = "转机登录失败"
                else:
                    error = "转机登录失败码=0x{:02X}".format(
                        result if result is not None else 0xFF)
                self._set_login_state("failed", error, result)
            return
        if frame["msg_type"] != MSG_LOGIN_ACK:
            if frame["msg_type"] != MSG_JOIN_GROUP_ACK:
                return
            if (self._join_request_cnt is None or
                    frame["request_cnt"] !=
                    ((self._join_request_cnt + 1) & 0xFFFF)):
                print("[POC] 入组应答request_cnt不匹配，已忽略")
                return
            try:
                ack = parse_join_group_ack(frame["payload"])
            except Exception as error:
                self._join_sent_ms = None
                self._join_request_cnt = None
                self._join_request_group_id = None
                self._join_request_raw_id = None
                self._join_packet = None
                self._join_retry_count = 0
                self.join_state = "failed"
                self.join_error = str(error)
                self._join_changed()
                return
            if (ack["device_id"] != self.device_id.encode("ascii") or
                    not self._police_no or
                    ack["police_no"] != self._police_no.encode("ascii") or
                    ack["group_id"] != self._join_request_group_id):
                print("[POC] 入组应答设备ID、警号或组ID不匹配，已忽略")
                return
            if ack["join_result"] == 0:
                self.confirmed_group_id = ack["group_id"]
                self.confirmed_group_raw_id = self._join_request_raw_id
                self.join_state = "success"
                self.join_error = None
                self._join_sent_ms = None
                print("[POC] 入组成功：{}".format(self.confirmed_group_raw_id))
            else:
                self.join_state = "failed"
                self.join_error = "入组失败码=0x{:02X}".format(ack["join_result"])
                self._join_sent_ms = None
                print("[POC] 入组失败：{}".format(self.join_error))
            # 请求已经结束，迟到或重复的同一应答必须忽略。
            self._join_request_cnt = None
            self._join_request_group_id = None
            self._join_request_raw_id = None
            self._join_packet = None
            self._join_retry_count = 0
            self._join_changed()
            return
        if (self._login_request_cnt is None or
                frame["request_cnt"] !=
                ((self._login_request_cnt + 1) & 0xFFFF)):
            print("[POC] 登录应答request_cnt不匹配，已忽略")
            return
        try:
            result = parse_login_ack(frame["payload"])["login_result"]
        except Exception as error:
            self._login_sent_ms = None
            self._login_request_cnt = None
            self._login_packet = None
            self._login_retry_count = 0
            self._set_login_state("failed", error, None)
            return
        self._login_sent_ms = None
        self._login_request_cnt = None
        self._login_packet = None
        self._login_retry_count = 0
        if result == 0:
            self._login_session_active = True
            self._police_no = self._login_police_no or self._police_no
            self._set_login_state("success", None, result)
            # 登录应答本身不计入下一周期；从确认登录成功这一刻开始计时。
            self._mark_tcp_activity()
            print("[POC] 登录成功")
            if not self.request_http_refresh(
                    "登录初始化", auto_join_first=True):
                self._set_http_failed()
            # 首包等待HTTP名单和默认入组流程结束，定位失败也会上报gnss_len=0。
            self._clear_gnss_request()
            self._gnss_first_upload_pending = True
            self._gnss_next_upload_ms = None
        elif result == 1:
            self._login_session_active = False
            self._set_login_state("failed", "警号已登录，等待确认强制登录", result)
        else:
            self._login_session_active = False
            error = "服务器登录失败码=0x{:02X}".format(result)
            self._set_login_state("failed", error, result)
            print("[POC] 登录失败，结果码=0x{:02X}".format(result))

    def _json_value(self, value, *keys):
        if not isinstance(value, dict):
            return None
        for key in keys:
            if key in value:
                return value[key]
        return None

    def _find_group_list(self, value):
        """兼容接口返回 data/list/groups 等不同JSON外层。"""
        if isinstance(value, list):
            if any(isinstance(item, dict) and self._json_value(
                    item, "groupId", "groupID", "grouID", "grouId",
                    "group_id") is not None
                   for item in value):
                return value
            for item in value:
                found = self._find_group_list(item)
                if found is not None:
                    return found
        elif isinstance(value, dict):
            for key in ("groups", "groupList", "groupInfos", "data", "result"):
                if key in value:
                    found = self._find_group_list(value[key])
                    if found is not None:
                        return found
            # 服务端使用其他外层字段名时，也递归检查所有值。
            for child in value.values():
                found = self._find_group_list(child)
                if found is not None:
                    return found
        return None

    def _normalize_groups(self, value):
        raw_groups = self._find_group_list(value) or []
        groups = []
        seen_raw = set()
        seen_numeric = set()
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            raw_id = self._json_value(
                item, "groupId", "groupID", "grouID", "grouId",
                "group_id", "id")
            if raw_id is None:
                continue
            raw_text = str(raw_id).strip()
            if not raw_text or raw_text in seen_raw:
                continue
            try:
                numeric_id = group_id_to_uint32(raw_text)
            except Exception:
                continue
            if numeric_id in seen_numeric:
                continue
            seen_raw.add(raw_text)
            seen_numeric.add(numeric_id)
            name = str(self._json_value(
                item, "groupName", "group_name", "name") or raw_text)
            users = self._json_value(
                item, "groupUsers", "users", "members", "groupMembers") or []
            groups.append({"id": numeric_id, "raw_id": raw_text,
                           "name": name, "users": users})
        return groups

    def _normalize_people(self, groups):
        """跨组按警号去重，优先保留同警号的首条在线记录。"""
        selected_by_code = {}
        code_order = []
        for group in groups:
            for user in group.get("users", []):
                if not isinstance(user, dict):
                    continue
                device = str(self._json_value(
                    user, "deviceId", "deviceID", "device_id") or "").strip()
                code = str(self._json_value(
                    user, "policeCode", "policeNo", "police_no") or "").strip()
                # 警号和设备号是单呼目标的必要字段，任一缺失就丢弃整条记录。
                if not device or not code:
                    continue
                name = str(self._json_value(
                    user, "policeName", "name", "userName", "username") or "").strip()
                online = (self._json_value(user, "online", "isOnline") is True)
                record = {"name": name or code or device,
                          "police_code": code, "device_id": device,
                          "online": online}
                code_key = code.upper()
                current = selected_by_code.get(code_key)
                if current is None:
                    selected_by_code[code_key] = record
                    code_order.append(code_key)
                elif current["online"] is not True and online:
                    # 之前保留的是离线记录，遇到在线设备时替换；
                    # 已经保留在线记录后，后续重复项不再覆盖它。
                    selected_by_code[code_key] = record
        people = [selected_by_code[key] for key in code_order]
        # 在线成员优先，离线成员保持HTTP原始相对顺序并置于末尾。
        online = [item for item in people if item.get("online") is True]
        offline = [item for item in people if item.get("online") is not True]
        return online + offline

    def _set_http(self, groups, people, state):
        self._lock_acquire()
        try:
            self.groups = groups
            self.people = people
            self.http_state = state
            self._changed()
        finally:
            self._lock_release()

    def _set_http_failed(self):
        """刷新失败时进入主界面，但本次名单视为空。"""
        self._lock_acquire()
        try:
            self.http_state = "failed"
            self.groups = []
            self.people = []
            self._changed()
        finally:
            self._lock_release()

    def _fetch_http_info(self, auto_join_first=False, reason=None):
        if request is None:
            self._set_http_failed()
            print("[POC] HTTP模块不可用，未获取组员名单")
            return
        self._lock_acquire()
        try:
            self.http_state = "loading"
            self._changed()
        finally:
            self._lock_release()
        response = None
        try:
            print("[POC] HTTP获取设备信息({})：{}".format(
                str(reason or "刷新"), self.http_url))
            response = request.get(
                self.http_url, headers={"Accept": "application/json"})
            status = int(getattr(response, "status_code", 0))
            if status != 200:
                raise RuntimeError("HTTP状态码={}".format(status))

            # QuecPython官方request响应对象提供json()，优先让底层按实际
            # 响应编码解析；不要再把response.text转成字符串交给ujson。
            json_reader = getattr(response, "json", None)
            
            if json_reader is not None:
                payload = json_reader()
            else:
                # 仅兼容少数没有response.json()的旧固件。
                if _json is None:
                    raise RuntimeError("HTTP响应不支持json()且无ujson模块")
                body = getattr(response, "text", "")
                if isinstance(body, bytes):
                    body = body.decode("utf-8")
                payload = _json.loads(body)
            print("http payload={}".format(payload))
            # 服务端固定返回 {'data': [组信息...]}，真正的组列表是data键值。
            if not isinstance(payload, dict):
                raise ValueError("HTTP JSON顶层不是对象")
            if "data" not in payload:
                raise ValueError("HTTP JSON缺少data字段")
            group_data = payload.get("data")
            if not isinstance(group_data, list):
                raise ValueError("HTTP JSON的data字段不是列表")

            groups = self._normalize_groups(group_data)
            people = self._normalize_people(groups)
            self._set_http(groups, people, "success")
            if (auto_join_first and groups and
                    self.confirmed_group_id is None):
                first = groups[0]
                self.request_join_group(first["raw_id"], first["id"])
            print("[POC] HTTP名单获取成功：{}个组，{}名人员".format(
                len(groups), len(people)))
            for person in people:
                print("{} -> {} -> {}-->{}".format(
                    person.get("name", ""),
                    person.get("device_id", ""),
                    person.get("police_code", ""),
                    "在线" if person.get("online") is True else "离线"))
        except Exception as error:
            self.last_error = error
            self._set_http_failed()
            print("[POC] HTTP获取设备信息失败：{}".format(error))
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _process_prelogin_http_refresh(self):
        """网络首次就绪、失败重试或断网恢复时刷新登录前身份名单。"""
        if self._login_session_active:
            self._prelogin_network_ready = False
            self._prelogin_http_retry_at_ms = None
            return
        now_ms = utime.ticks_ms()
        if (self._prelogin_network_check_at_ms is not None and
                not self._time_reached(
                    self._prelogin_network_check_at_ms, now_ms)):
            return
        self._prelogin_network_check_at_ms = self._add_ms(
            now_ms, PRELOGIN_NETWORK_CHECK_INTERVAL_MS)
        network_ready = self._network_ready()
        if not network_ready:
            self._prelogin_network_ready = False
            self._prelogin_http_retry_at_ms = None
            return

        should_refresh = not self._prelogin_network_ready
        self._prelogin_network_ready = True
        if (should_refresh and
                (self._http_worker_running or self._http_refresh_pending)):
            # 已有请求覆盖本次网络就绪事件；等待其结果后再决定是否重试。
            self._prelogin_http_retry_at_ms = self._add_ms(
                now_ms, PRELOGIN_HTTP_RETRY_INTERVAL_MS)
            return
        if (not should_refresh and self.http_state != "success" and
                self._time_reached(
                    self._prelogin_http_retry_at_ms, now_ms)):
            should_refresh = True
        if not should_refresh:
            return
        if self._http_worker_running or self._http_refresh_pending:
            return
        started = self.request_http_refresh(
            "登录前名单", auto_join_first=False,
            allow_before_login=True)
        self._prelogin_http_retry_at_ms = self._add_ms(
            now_ms, PRELOGIN_HTTP_RETRY_INTERVAL_MS)
        if not started:
            self._set_http_failed()

    def _worker(self):
        """网络就绪后连接TCP，处理登录包和服务器应答。"""
        retry_ms = 1000
        try:
            while self._running:
                self._process_prelogin_http_refresh()
                if self._socket is None:
                    if not self._network_ready():
                        utime.sleep_ms(500)
                        continue
                    # 数据网络就绪后尽早启动GNSS搜星，不等待登录流程完成。
                    self._ensure_gnss_initialized()
                    try:
                        self._connect()
                        retry_ms = 1000
                    except Exception as error:
                        self.last_error = error
                        print("[POC] TCP连接服务器失败：{}".format(error))
                        self._close_socket()
                        utime.sleep_ms(retry_ms)
                        retry_ms = min(10000, retry_ms * 2)
                        continue
                try:
                    self._send_login()
                    self._send_force_login()
                    self._send_join_group()
                    self._send_single_control()
                    # 服务器主动退出时，单呼清理完成后发送统一0x10退出组呼请求。
                    self._send_leave_group()
                    self._send_floor_control()
                    self._recv_once()
                    self._send_gnss_if_due()
                    self._send_heartbeat_if_due()
                    self._process_tcp_request_timeouts()
                except Exception as error:
                    self.last_error = error
                    print("[POC] TCP连接异常，将重连：{}".format(error))
                    self._close_socket()
                    utime.sleep_ms(retry_ms)
                    retry_ms = min(10000, retry_ms * 2)
                else:
                    utime.sleep_ms(20)
        finally:
            self._close_socket()
            self._running = False

    def get_snapshot(self):
        self._lock_acquire()
        try:
            return {"tcp_connected": self.tcp_connected,
                    "login_state": self.login_state,
                    "login_police_no": (
                        self._login_police_no
                        if self._login_session_active else None),
                    "login_result": self.login_result,
                    "login_error": self.login_error,
                    "logout_revision": self._logout_revision,
                    "logout_state": self.logout_state,
                    "logout_error": self.logout_error,
                    "http_state": self.http_state,
                    "join_revision": self._join_revision,
                    "join_state": self.join_state,
                    "join_error": self.join_error,
                    "confirmed_group_id": self.confirmed_group_id,
                    "confirmed_group_raw_id": self.confirmed_group_raw_id,
                    "single_revision": self._single_revision,
                    "single_state": self.single_state,
                    "single_error": self.single_error,
                    "single_call_group_id": self.single_call_group_id,
                    "single_call_person_key": self.single_call_person_key,
                    "single_target_key": (self._single_next_key or
                                          self._single_target_key),
                    "floor_revision": self._floor_revision,
                    "floor_event_revision": self._floor_event_revision,
                    "floor_event": (dict(self._floor_event)
                                    if self._floor_event is not None else None),
                    "floor_state": self.floor_state,
                    "pending_floor_group_id": self.pending_floor_group_id,
                    "held_floor_group_id": self.held_floor_group_id,
                    "active_audio_group_id": self.active_audio_group_id,
                    "floor_udp_port": self.floor_udp_port,
                    "occupied_group_ids": list(self.occupied_group_ids.keys()),
                    "occupied_groups": [
                        {"group_id": group_id,
                         "police_name": item.get("police_name", "")}
                        for group_id, item in self.occupied_group_ids.items()],
                    "groups": list(self.groups), "people": list(self.people)}
        finally:
            self._lock_release()

    def get_snapshot_if_changed(self):
        if self._revision == self._reported_revision:
            return None
        self._lock_acquire()
        try:
            if self._revision == self._reported_revision:
                return None
            snapshot = {"tcp_connected": self.tcp_connected,
                        "login_state": self.login_state,
                        "login_police_no": (
                            self._login_police_no
                            if self._login_session_active else None),
                        "login_result": self.login_result,
                        "login_error": self.login_error,
                        "logout_revision": self._logout_revision,
                        "logout_state": self.logout_state,
                        "logout_error": self.logout_error,
                        "http_state": self.http_state,
                        "join_revision": self._join_revision,
                        "join_state": self.join_state,
                        "join_error": self.join_error,
                        "confirmed_group_id": self.confirmed_group_id,
                        "confirmed_group_raw_id": self.confirmed_group_raw_id,
                        "single_revision": self._single_revision,
                        "single_state": self.single_state,
                        "single_error": self.single_error,
                        "single_call_group_id": self.single_call_group_id,
                        "single_call_person_key": self.single_call_person_key,
                        "single_target_key": (self._single_next_key or
                                              self._single_target_key),
                        "floor_revision": self._floor_revision,
                        "floor_event_revision": self._floor_event_revision,
                        "floor_event": (dict(self._floor_event)
                                        if self._floor_event is not None
                                        else None),
                        "floor_state": self.floor_state,
                        "pending_floor_group_id": self.pending_floor_group_id,
                        "held_floor_group_id": self.held_floor_group_id,
                        "active_audio_group_id": self.active_audio_group_id,
                        "floor_udp_port": self.floor_udp_port,
                        "occupied_group_ids": list(
                            self.occupied_group_ids.keys()),
                        "occupied_groups": [
                            {"group_id": group_id,
                             "police_name": item.get("police_name", "")}
                            for group_id, item in
                            self.occupied_group_ids.items()],
                        "groups": list(self.groups), "people": list(self.people)}
            self._reported_revision = self._revision
            return snapshot
        finally:
            self._lock_release()

    def stop(self):
        self._running = False
        self._close_socket()
        if self.audio_controller is not None:
            try:
                self.audio_controller.stop()
            except Exception:
                pass
