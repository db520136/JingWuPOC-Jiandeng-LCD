# -*- coding: utf-8 -*-
"""第四阶段真实公网测试：三线程运行时承载完整 POC 流程。

与第三阶段入口的区别：本文件不在主线程直接调用 TCP ``recv``，所有 TCP
应答都由 TCPReceiveWorker 接收、解析后通过 CommunicationRuntime 回调；所有
TCP/RTP 发送都通过 CommunicationSendWorker 和 CommunicationCommandQueue。

本文件仍是独立测试入口，不启动 UI，不导入旧 poc_client/rtp_audio，也不修改
正式 main.py。
"""

import _thread
import sys
import utime

import checkNet

if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

try:
    import usocket as socket
except ImportError:
    import socket

try:
    import request
except ImportError:
    request = None

from comm_commands import (CMD_FORCE_LOGIN, CMD_LOGIN, CMD_JOIN_GROUP,
                           CMD_TCP_HEARTBEAT)
from audio_port import AudioPort
from communication_queue import CommunicationCommandQueue
from communication_session import CommunicationSession
from communication_runtime import CommunicationRuntime
from poc_protocol import (MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
                          MSG_JOIN_GROUP, MSG_JOIN_GROUP_ACK, MSG_LOGIN,
                          MSG_LOGIN_ACK, MSG_FORCE_LOGIN,
                          MSG_FORCE_LOGIN_ACK, build_frame,
                          build_heartbeat_payload, build_join_group_payload,
                          build_login_payload, group_id_to_uint32,
                          parse_force_login_ack, parse_join_group_ack,
                          parse_login_ack)
from rtp_protocol import RTP_PAYLOAD_SIZE, parse_rtp_packet


# ========================== 实机测试入口配置 ==========================

SERVER_MODE_PUBLIC = 0
SERVER_MODE_PRIVATE = 1
SERVER_MODE = SERVER_MODE_PUBLIC

DEVICE_ID = "33030002002000000590"
POLICE_NO = "M100001"
TCP_SERVER_PORT = 6060
RTP_LOCAL_PORT = 4001

PUBLIC_TCP_HOST = "125.124.233.231"
PUBLIC_HTTP_BASE = "http://125.124.233.231:18081"
PRIVATE_TCP_HOST = "68.95.0.31"
PRIVATE_HTTP_BASE = "http://68.95.0.31:38080"

TCP_CONNECT_TIMEOUT_SECONDS = 10
TCP_LOGIN_REQUEST_COUNT = 1
RECONNECT_WAIT_MS = 1000
PTT_TEST_TIMEOUT_SECONDS = 60
TEST_PROJECT_NAME = "JingWuPOC_Communication_Stage4"
TEST_PROJECT_VERSION = "1.0.0"


class EventMailbox:
    """保存 Worker 回调事件；主线程只轮询邮箱，不直接读取 Socket。"""

    def __init__(self):
        self._lock = _thread.allocate_lock()
        self._events = []

    def append(self, event):
        self._lock.acquire()
        try:
            self._events.append(event)
        finally:
            self._lock.release()

    def take_all(self):
        self._lock.acquire()
        try:
            events = self._events
            self._events = []
            return events
        finally:
            self._lock.release()


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _selected_server():
    if SERVER_MODE == SERVER_MODE_PUBLIC:
        return {
            "mode_name": "公网",
            "tcp_host": PUBLIC_TCP_HOST,
            "http_url": (PUBLIC_HTTP_BASE +
                          "/biz/deviceInfo/data?deviceId=" + DEVICE_ID),
        }
    if SERVER_MODE == SERVER_MODE_PRIVATE:
        return {
            "mode_name": "专网",
            "tcp_host": PRIVATE_TCP_HOST,
            "http_url": (PRIVATE_HTTP_BASE +
                          "/biz/deviceInfo/data?deviceId=" + DEVICE_ID),
        }
    raise ValueError("SERVER_MODE配置无效")


def _test_http(config):
    if request is None:
        raise RuntimeError("QuecPython request模块不可用")
    response = None
    try:
        print("[通信测试] HTTP请求：{}".format(config["http_url"]))
        response = request.get(
            config["http_url"], headers={"Accept": "application/json"})
        status_code = int(response.status_code)
        if status_code != 200:
            raise RuntimeError("HTTP状态码={}".format(status_code))
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("HTTP响应顶层不是对象")
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError("HTTP响应data不是非空组列表")
        print("[通信测试] HTTP连通通过，状态码=200，组数量={}".format(
            len(data)))
        return data
    finally:
        if response is not None:
            response.close()


def _first_group(data):
    item = data[0]
    if not isinstance(item, dict):
        raise RuntimeError("HTTP第一组数据不是对象")
    raw_group_id = None
    for key in ("groupId", "groupID", "grouID", "grouId", "group_id", "id"):
        if item.get(key) is not None:
            raw_group_id = item.get(key)
            break
    if raw_group_id is None:
        raise RuntimeError("HTTP第一组缺少groupId/groupID字段")
    raw_group_id = str(raw_group_id).strip()
    if not raw_group_id:
        raise RuntimeError("HTTP第一组groupId为空")
    group_name = item.get("groupName")
    if group_name is None:
        group_name = item.get("group_name")
    if group_name is None:
        group_name = item.get("name")
    return raw_group_id, group_id_to_uint32(raw_group_id), str(
        group_name or raw_group_id)


def _socket_error(event):
    if event["source"] != "tcp":
        return None
    inner = event["event"]
    inner_type = inner["type"]
    if inner_type in ("tcp_disconnected", "tcp_receive_error"):
        return inner_type
    return None


def _wait_frame(mailbox, expected_type, request_count, label):
    expected_count = (int(request_count) + 1) & 0xFFFF
    deadline = utime.ticks_add(
        utime.ticks_ms(), TCP_CONNECT_TIMEOUT_SECONDS * 1000)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        for event in mailbox.take_all():
            error_type = _socket_error(event)
            if error_type is not None:
                raise OSError("TCP接收线程{}：{}".format(label, error_type))
            if event["source"] != "tcp":
                continue
            inner = event["event"]
            if inner["type"] != "tcp_frame":
                continue
            frame = inner["payload"]
            if frame["msg_type"] != expected_type:
                print("[通信测试] {}前收到其他TCP信令=0x{:02X}，继续等待".format(
                    label, frame["msg_type"]))
                continue
            if frame["request_cnt"] != expected_count:
                raise RuntimeError(
                    "{}应答request_cnt不匹配：期望={} 实际={}".format(
                        label, expected_count, frame["request_cnt"]))
            return frame
        utime.sleep_ms(5)
    raise TimeoutError("等待{}应答超时".format(label))


def _submit_frame(runtime, command_type, msg_type, request_count, payload, label):
    packet = build_frame(msg_type, request_count, payload)
    if not runtime.submit(command_type, packet):
        raise RuntimeError("{}命令入队失败".format(label))
    print("[通信测试] 发送{}：request_cnt={}".format(label, request_count))


def _login(runtime, mailbox, request_count, label):
    _submit_frame(runtime, CMD_LOGIN, MSG_LOGIN, request_count,
                  build_login_payload(DEVICE_ID, POLICE_NO),
                  label + "0x01登录")
    frame = _wait_frame(mailbox, MSG_LOGIN_ACK, request_count, "0x81登录")
    result = parse_login_ack(frame["payload"])["login_result"]
    print("[通信测试] {}收到0x81登录应答：结果码=0x{:02X}".format(
        label, result))
    if result == 0:
        return request_count + 1
    if result != 1:
        raise RuntimeError("服务器登录失败，结果码=0x{:02X}".format(result))

    force_count = request_count + 1
    _submit_frame(runtime, CMD_FORCE_LOGIN, MSG_FORCE_LOGIN, force_count,
                  build_login_payload(DEVICE_ID, POLICE_NO),
                  label + "0x0E强制登录")
    force_frame = _wait_frame(
        mailbox, MSG_FORCE_LOGIN_ACK, force_count, "0x8E强制登录")
    force_result = parse_force_login_ack(
        force_frame["payload"])["login_result"]
    print("[通信测试] {}收到0x8E强制登录应答：结果码=0x{:02X}".format(
        label, force_result))
    if force_result != 0:
        raise RuntimeError("强制登录失败，结果码=0x{:02X}".format(force_result))
    return force_count + 1


def _wait_session_event(session_mailbox, event_type, timeout_seconds,
                        label):
    deadline = utime.ticks_add(
        utime.ticks_ms(), int(timeout_seconds * 1000))
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        for event in session_mailbox.take_all():
            if event["type"] == event_type:
                return event
        utime.sleep_ms(10)
    raise TimeoutError("等待{}超时".format(label))


def _run_flow(config, group_data):
    raw_group_id, group_id, group_name = _first_group(group_data)
    print("[通信测试] 选择第一组：名称={} 原始ID={} 数字ID={}".format(
        group_name, raw_group_id, group_id))

    mailbox = EventMailbox()
    session_mailbox = EventMailbox()
    audio_port = AudioPort()
    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    runtime = None
    session = None
    target = (config["tcp_host"], TCP_SERVER_PORT)
    try:
        tcp_socket.settimeout(TCP_CONNECT_TIMEOUT_SECONDS)
        udp_socket.bind(("0.0.0.0", RTP_LOCAL_PORT))
        print("[通信测试] UDP本地端口绑定通过：{}".format(RTP_LOCAL_PORT))
        print("[通信测试] TCP连接：{}:{}".format(target[0], target[1]))
        tcp_socket.connect(target)
        print("[通信测试] TCP连接通过")
        audio_port.start()
        print("[通信测试] 音频硬件已启动：GPIO27喇叭，G711/PCM已就绪")

        def on_runtime_event(event):
            # RTP接收线程只负责收包；这里把有效160字节PCMA帧交给统一
            # 音频端口播放，1字节激活包不会送入G711。
            if event["source"] == "rtp":
                inner = event["event"]
                if inner["type"] == "rtp_packet":
                    parsed = parse_rtp_packet(inner["payload"])
                    if (parsed is not None and
                            len(parsed["payload"]) == RTP_PAYLOAD_SIZE):
                        audio_port.write_playback_frame(parsed["payload"])
            mailbox.append(event)

        runtime = CommunicationRuntime(
            tcp_socket=tcp_socket,
            udp_socket=udp_socket,
            server_ip=config["tcp_host"],
            read_audio_frame=audio_port.read_capture_frame,
            event_callback=on_runtime_event,
            command_queue=CommunicationCommandQueue(max_length=32))
        session = CommunicationSession(
            submit_command=runtime.submit,
            device_id=DEVICE_ID,
            police_no=POLICE_NO,
            audio_port=audio_port,
            event_callback=session_mailbox.append)

        # 测试阶段保留 PTT 状态日志，确认 GPIO29 的按下/松开边沿以及
        # worker 是否在抢麦后仍保持运行；正式入口接入时不打印这些诊断信息。
        def on_session_event(event):
            session_mailbox.append(event)
            event_type = event["type"]
            if event_type in ("ptt_sample", "ptt_changed",
                              "floor_release_sent", "floor_released",
                              "ptt_worker_error"):
                print("[通信测试] 会话事件={} 内容={}".format(
                    event_type, event["payload"]))

        session.event_callback = on_session_event
        runtime.attach_session(session)
        _assert(runtime.start(), "三线程运行时启动失败")
        print("[通信测试] 四个事件组件已启动：TCP接收、UDP接收、统一发送、PTT会话")

        next_count = _login(runtime, mailbox, TCP_LOGIN_REQUEST_COUNT, "首次")
        print("[通信测试] POC登录成功")

        join_count = next_count
        _submit_frame(
            runtime, CMD_JOIN_GROUP, MSG_JOIN_GROUP, join_count,
            build_join_group_payload(DEVICE_ID, group_id, POLICE_NO),
            "0x03入组")
        join_frame = _wait_frame(
            mailbox, MSG_JOIN_GROUP_ACK, join_count, "0x83入组")
        join_ack = parse_join_group_ack(join_frame["payload"])
        print("[通信测试] 收到0x83入组应答：结果码=0x{:02X}".format(
            join_ack["join_result"]))
        if join_ack["join_result"] != 0:
            raise RuntimeError("入组失败，结果码=0x{:02X}".format(
                join_ack["join_result"]))
        print("[通信测试] 入组成功")
        session.configure_group(group_id, join_count + 1)
        print("[通信测试] 已入组，请按住GPIO29 PTT开始真实语音发送")
        print("[通信测试] GPIO29低电平=按下，高电平=松开，最长等待{}秒".format(
            PTT_TEST_TIMEOUT_SECONDS))
        _wait_session_event(
            session_mailbox, "floor_granted", PTT_TEST_TIMEOUT_SECONDS,
            "GPIO29按下后的抢麦成功")
        print("[通信测试] GPIO29按下→抢麦成功→RTP激活→真实麦克风发送")
        print("[通信测试] 请松开GPIO29 PTT结束语音")
        _wait_session_event(
            session_mailbox, "floor_released", PTT_TEST_TIMEOUT_SECONDS,
            "GPIO29松开后的释放麦权")
        print("[通信测试] GPIO29松开→RTP停止→释放麦权成功")

        heartbeat_count = session.next_request_count
        _submit_frame(
            runtime, CMD_TCP_HEARTBEAT, MSG_HEARTBEAT, heartbeat_count,
            build_heartbeat_payload(DEVICE_ID), "0x02 TCP心跳")
        heartbeat_frame = _wait_frame(
            mailbox, MSG_HEARTBEAT_ACK, heartbeat_count, "0x82 TCP心跳")
        session.next_request_count = (heartbeat_count + 1) & 0xFFFF
        print("[通信测试] 收到0x82 TCP心跳应答：request_cnt={}".format(
            heartbeat_frame["request_cnt"]))
        print("[通信测试] TCP心跳通过")

        print("[通信测试] 停止当前三线程，准备验证运行时重连")
        runtime.stop()
        utime.sleep_ms(RECONNECT_WAIT_MS)
        new_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        new_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        new_tcp.settimeout(TCP_CONNECT_TIMEOUT_SECONDS)
        new_udp.bind(("0.0.0.0", RTP_LOCAL_PORT))
        new_tcp.connect(target)
        tcp_socket = new_tcp
        udp_socket = new_udp
        mailbox = EventMailbox()
        runtime.event_callback = on_runtime_event
        runtime.reconnect(new_tcp, new_udp)
        print("[通信测试] TCP重连成功，三线程已重新启动")
        _login(runtime, mailbox, session.next_request_count, "重连后")
        print("[通信测试] 断线重连并重新登录通过")
        print("[通信测试] PTT真实音频、释放麦权和三线程重连流程通过")
    finally:
        if runtime is not None:
            runtime.stop()
        else:
            try:
                tcp_socket.close()
            except Exception:
                pass
            try:
                udp_socket.close()
            except Exception:
                pass
        audio_port.close()


def main():
    config = _selected_server()
    print("[通信测试] 第四阶段真实公网三线程测试开始")
    print("[通信测试] 不导入UI，不启动旧TCP/RTP")
    print("[通信测试] 当前环境={} 设备ID={} 警号={}".format(
        config["mode_name"], DEVICE_ID, POLICE_NO))
    print("[通信测试] 等待蜂窝网络注册和数据拨号完成")
    network = checkNet.CheckNetwork(TEST_PROJECT_NAME, TEST_PROJECT_VERSION)
    network.wait_network_connected()
    print("[通信测试] 蜂窝网络已就绪")
    group_data = _test_http(config)
    _run_flow(config, group_data)
    print("[通信测试] 第四阶段真实公网三线程测试全部通过")


if __name__ == "__main__":
    main()
