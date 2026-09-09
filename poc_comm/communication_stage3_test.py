# -*- coding: utf-8 -*-
"""第三阶段通信组件独立实机冒烟测试。

本文件不导入 UI、main.py、旧 poc_client.py 或旧 rtp_audio.py。先使用可控
假 socket 验证队列和收发 Worker 边界，再按入口中选择的公网/专网配置执行
真实 HTTP、TCP 和 UDP 本地端口连通性检查。
"""

import _thread
import utime
import checkNet
import sys

# QuecPython 使用 example.exec() 运行 /usr 下脚本时，部分固件不会自动
# 将脚本目录加入 sys.path。显式加入后，依赖文件必须与本入口同在 /usr。
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

# EC800M 的正式部署方式：所有依赖与本入口平铺在 /usr。不要把所有导入
# 放入一个“吞掉异常”的大 try，否则任一依赖缺失都会被误报为首个模块缺失。
from comm_commands import (CMD_LOGIN, CMD_RTP_ACTIVATE,
                           CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP,
                           CMD_STATUS_SYNC, CMD_STOP_ALL)
from communication_queue import CommunicationCommandQueue
from communication_send_worker import CommunicationSendWorker
from poc_protocol import (FrameParser, build_frame, build_floor_payload,
                          build_heartbeat_payload, build_join_group_payload,
                          build_login_payload,
                          group_id_to_uint32, parse_join_group_ack,
                          parse_login_ack, parse_release_floor_ack,
                          parse_request_floor_ack,
                          MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
                          MSG_JOIN_GROUP, MSG_JOIN_GROUP_ACK, MSG_LOGIN,
                          MSG_LOGIN_ACK, MSG_RELEASE_FLOOR,
                          MSG_RELEASE_FLOOR_ACK, MSG_REQUEST_FLOOR,
                          MSG_REQUEST_FLOOR_ACK, MSG_FORCE_LOGIN,
                          MSG_FORCE_LOGIN_ACK, parse_force_login_ack)
from rtp_protocol import (RTP_PAYLOAD_SIZE, build_rtp_packet,
                          parse_rtp_packet)
from rtp_receive_worker import RTPReceiveWorker, RTP_RX_PACKET
from tcp_receive_worker import TCPReceiveWorker, TCP_RX_FRAME


# ========================== 实机测试入口配置 ==========================

SERVER_MODE_PUBLIC = 0
SERVER_MODE_PRIVATE = 1

# 默认连接公网；需要验证专网时只修改这一项。
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
RTP_TEST_DURATION_SECONDS = 3
RTP_TEST_PACKET_INTERVAL_MS = 20
RTP_TEST_PAYLOAD_SIZE = 160
RTP_TEST_SSRC = 0x13572468
RECONNECT_WAIT_MS = 1000
TEST_PROJECT_NAME = "JingWuPOC_Communication_Test"
TEST_PROJECT_VERSION = "1.0.0"


def _selected_server():
    if SERVER_MODE == SERVER_MODE_PUBLIC:
        mode_name = "公网"
        tcp_host = PUBLIC_TCP_HOST
        http_base = PUBLIC_HTTP_BASE
    elif SERVER_MODE == SERVER_MODE_PRIVATE:
        mode_name = "专网"
        tcp_host = PRIVATE_TCP_HOST
        http_base = PRIVATE_HTTP_BASE
    else:
        raise ValueError("SERVER_MODE配置无效")
    return {
        "mode_name": mode_name,
        "device_id": DEVICE_ID,
        "police_no": POLICE_NO,
        "tcp_host": tcp_host,
        "tcp_port": TCP_SERVER_PORT,
        "rtp_local_port": RTP_LOCAL_PORT,
        "http_url": (http_base +
                     "/biz/deviceInfo/data?deviceId=" + DEVICE_ID),
    }


class CaptureTCPSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class CaptureUDPSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, address):
        self.sent.append((bytes(data), address))
        return len(data)

    def close(self):
        self.closed = True


class ReceiveSocket:
    """可控 recv/recvfrom 假 socket，数据耗尽后阻塞，close 后退出。"""

    def __init__(self, data, udp=False):
        self.data = list(data)
        self.udp = bool(udp)
        self.closed = False

    def recv(self, size):
        while not self.data and not self.closed:
            utime.sleep_ms(5)
        if self.closed:
            raise OSError("socket closed")
        return self.data.pop(0)

    def recvfrom(self, size):
        while not self.data and not self.closed:
            utime.sleep_ms(5)
        if self.closed:
            raise OSError("socket closed")
        return self.data.pop(0), ("127.0.0.1", 4001)

    def close(self):
        self.closed = True


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _test_queue():
    queue = CommunicationCommandQueue(max_length=8)
    _assert(queue.submit(CMD_STATUS_SYNC, b"status"),
            "P2状态同步入队失败")
    _assert(queue.submit(CMD_LOGIN, b"login"), "P1登录命令入队失败")
    _assert(queue.submit(CMD_RTP_ACTIVATE, 4001),
            "P0激活命令入队失败")
    _assert(queue.submit(CMD_STATUS_SYNC, b"status-duplicate"),
            "P2同类命令合并失败")
    stop_pending, items = queue.take_batch_nowait()
    _assert(not stop_pending, "未触发停止却出现停止事件")
    _assert(len(items) == 3, "命令合并后数量错误：{}".format(len(items)))
    _assert(items[0]["type"] == CMD_RTP_ACTIVATE, "P0未优先取出")
    _assert(items[1]["type"] == CMD_LOGIN, "P1未排在P0之后")
    _assert(items[2]["type"] == CMD_STATUS_SYNC, "P2未排在最后")
    queue.signal_stop()
    stop_pending, items = queue.take_batch_nowait()
    _assert(stop_pending and not items, "停止事件未独立保留")
    print("[通信测试] 队列：优先级、合并、停止事件通过")


def _test_send_worker(config):
    tcp = CaptureTCPSocket()
    udp = CaptureUDPSocket()
    queue = CommunicationCommandQueue(max_length=8)
    frame = build_frame(MSG_LOGIN, 1, b"")
    worker = CommunicationSendWorker(
        tcp_socket=tcp,
        udp_socket=udp,
        read_audio_frame=lambda: bytes((0xD5,)) * RTP_PAYLOAD_SIZE,
        command_queue=queue,
        server_ip=config["tcp_host"],
        udp_server_port=4001,
    )
    _assert(worker._send_tcp_packet(frame), "TCP完整帧发送失败")
    _assert(worker._send_activate(4001), "RTP激活包发送失败")
    _assert(worker._start_audio(4001), "RTP音频状态启动失败")
    _assert(worker._send_audio_frame(), "RTP音频帧发送失败")
    _assert(len(tcp.sent) == 1, "TCP发送数量错误")
    _assert(len(udp.sent) == 2, "UDP激活/音频发送数量错误")
    activate = parse_rtp_packet(udp.sent[0][0])
    audio = parse_rtp_packet(udp.sent[1][0])
    _assert(activate is not None and len(activate["payload"]) == 1,
            "激活包格式错误")
    _assert(audio is not None and len(audio["payload"]) == RTP_PAYLOAD_SIZE,
            "音频包格式错误")
    worker._stop_audio()
    queue.signal_stop()
    print("[通信测试] 统一发送：TCP、RTP激活、RTP音频、停止状态通过")


def _test_live_http(config):
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
        print("[通信测试] HTTP公网/专网连通通过，状态码=200，组数量={}".format(
            len(data)))
        return data
    finally:
        if response is not None:
            response.close()


def _send_tcp_all(tcp_socket, data):
    sent = 0
    packet = bytes(data)
    while sent < len(packet):
        count = tcp_socket.send(packet[sent:])
        if count is None:
            return
        count = int(count)
        if count <= 0:
            raise OSError("TCP send返回0字节")
        sent += count


def _receive_expected_frame(tcp_socket, parser, expected_type,
                            request_count, label):
    expected_count = (int(request_count) + 1) & 0xFFFF
    deadline = utime.ticks_add(
        utime.ticks_ms(), TCP_CONNECT_TIMEOUT_SECONDS * 1000)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        data = tcp_socket.recv(1024)
        if not data:
            raise OSError("TCP服务器在{}应答前断开".format(label))
        for frame in parser.feed(data):
            if frame["msg_type"] != expected_type:
                print("[通信测试] {}前收到其他TCP信令=0x{:02X}，继续等待".format(
                    label,
                    frame["msg_type"]))
                continue
            if frame["request_cnt"] != expected_count:
                raise RuntimeError(
                    "{}应答request_cnt不匹配：期望={} 实际={}".format(
                        label, expected_count, frame["request_cnt"]))
            return frame
    raise TimeoutError("等待{}应答超时".format(label))


def _first_group_from_http(data):
    item = data[0]
    if not isinstance(item, dict):
        raise RuntimeError("HTTP第一组数据不是对象")
    raw_group_id = None
    for key in ("groupId", "groupID", "grouID", "grouId",
                "group_id", "id"):
        if item.get(key) is not None:
            raw_group_id = item.get(key)
            break
    if raw_group_id is None:
        raise RuntimeError("HTTP第一组缺少groupId/groupID字段")
    raw_group_id = str(raw_group_id).strip()
    if not raw_group_id:
        raise RuntimeError("HTTP第一组groupId为空")
    numeric_group_id = group_id_to_uint32(raw_group_id)
    group_name = item.get("groupName")
    if group_name is None:
        group_name = item.get("group_name")
    if group_name is None:
        group_name = item.get("name")
    return raw_group_id, numeric_group_id, str(group_name or raw_group_id)


def _login_socket(tcp_socket, config, request_count, label):
    parser = FrameParser()
    login_packet = build_frame(
        MSG_LOGIN, request_count,
        build_login_payload(config["device_id"], config["police_no"]))
    print("[通信测试] {}发送0x01登录：警号={} request_cnt={}".format(
        label, config["police_no"], request_count))
    _send_tcp_all(tcp_socket, login_packet)
    login_frame = _receive_expected_frame(
        tcp_socket, parser, MSG_LOGIN_ACK, request_count, "0x81登录")
    result = parse_login_ack(login_frame["payload"])["login_result"]
    print("[通信测试] {}收到0x81登录应答：结果码=0x{:02X}".format(
        label, result))
    if result == 0:
        return request_count + 1
    if result != 1:
        raise RuntimeError("服务器登录失败，结果码=0x{:02X}".format(result))

    # 重复运行测试时，服务器可能仍认为上一次连接在线；按协议发送
    # 0x0E 强制转机登录，让本次测试可以在上次异常中断后继续。
    force_count = request_count + 1
    force_packet = build_frame(
        MSG_FORCE_LOGIN, force_count,
        build_login_payload(config["device_id"], config["police_no"]))
    print("[通信测试] {}发送0x0E强制登录：request_cnt={}".format(
        label, force_count))
    _send_tcp_all(tcp_socket, force_packet)
    force_frame = _receive_expected_frame(
        tcp_socket, parser, MSG_FORCE_LOGIN_ACK, force_count, "0x8E强制登录")
    force_result = parse_force_login_ack(
        force_frame["payload"])["login_result"]
    print("[通信测试] {}收到0x8E强制登录应答：结果码=0x{:02X}".format(
        label, force_result))
    if force_result != 0:
        raise RuntimeError("强制登录失败，结果码=0x{:02X}".format(force_result))
    return force_count + 1


def _send_and_receive(tcp_socket, parser, msg_type, request_count,
                      payload, ack_type, label):
    packet = build_frame(msg_type, request_count, payload)
    print("[通信测试] 发送{}：request_cnt={}".format(label, request_count))
    _send_tcp_all(tcp_socket, packet)
    return _receive_expected_frame(
        tcp_socket, parser, ack_type, request_count, label)


def _send_rtp_test_stream(udp_socket, server_ip, server_port):
    sequence = 0
    timestamp = 0
    activate = build_rtp_packet(
        sequence, timestamp, RTP_TEST_SSRC, b"\x00")
    udp_socket.sendto(activate, (server_ip, server_port))
    print("[通信测试] RTP激活包已发送：目标={}:{} 长度={}".format(
        server_ip, server_port, len(activate)))
    sequence = (sequence + 1) & 0xFFFF
    payload = bytes((0xD5,)) * RTP_TEST_PAYLOAD_SIZE
    packet_count = int((RTP_TEST_DURATION_SECONDS * 1000) /
                       RTP_TEST_PACKET_INTERVAL_MS)
    print("[通信测试] 开始持续RTP测试：时长={}秒，预计{}帧".format(
        RTP_TEST_DURATION_SECONDS, packet_count))
    for index in range(packet_count):
        audio_packet = build_rtp_packet(
            sequence, timestamp, RTP_TEST_SSRC, payload,
            1 if index == 0 else 0)
        udp_socket.sendto(audio_packet, (server_ip, server_port))
        sequence = (sequence + 1) & 0xFFFF
        timestamp = (timestamp + RTP_TEST_PAYLOAD_SIZE) & 0xFFFFFFFF
        utime.sleep_ms(RTP_TEST_PACKET_INTERVAL_MS)
    print("[通信测试] 持续RTP测试完成：实际发送{}帧".format(packet_count))


def _test_live_poc_flow(config, http_data):
    raw_group_id, group_id, group_name = _first_group_from_http(http_data)
    print("[通信测试] 选择第一组：名称={} 原始ID={} 数字ID={}".format(
        group_name, raw_group_id, group_id))

    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    parser = FrameParser()
    try:
        tcp_socket.settimeout(TCP_CONNECT_TIMEOUT_SECONDS)
        udp_socket.bind(("0.0.0.0", config["rtp_local_port"]))
        print("[通信测试] UDP本地端口绑定通过：{}".format(
            config["rtp_local_port"]))
        target = (config["tcp_host"], config["tcp_port"])
        print("[通信测试] TCP连接：{}:{}".format(target[0], target[1]))
        tcp_socket.connect(target)
        print("[通信测试] TCP连接通过")

        next_count = _login_socket(
            tcp_socket, config, TCP_LOGIN_REQUEST_COUNT, "首次")
        print("[通信测试] POC登录成功")

        join_request_count = next_count
        join_frame = _send_and_receive(
            tcp_socket, parser, MSG_JOIN_GROUP, join_request_count,
            build_join_group_payload(
                config["device_id"], group_id, config["police_no"]),
            MSG_JOIN_GROUP_ACK, "0x03入组")
        join_ack = parse_join_group_ack(join_frame["payload"])
        print("[通信测试] 收到0x83入组应答：结果码=0x{:02X}".format(
            join_ack["join_result"]))
        if join_ack["join_result"] != 0:
            raise RuntimeError("入组失败，结果码=0x{:02X}".format(
                join_ack["join_result"]))
        if (join_ack["device_id"] != config["device_id"].encode("ascii") or
                join_ack["police_no"] != config["police_no"].encode("ascii") or
                join_ack["group_id"] != group_id):
            raise RuntimeError("0x83入组应答设备ID、警号或组ID不匹配")
        print("[通信测试] 入组成功")

        floor_request_count = join_request_count + 1
        floor_frame = _send_and_receive(
            tcp_socket, parser, MSG_REQUEST_FLOOR, floor_request_count,
            build_floor_payload(
                config["device_id"], config["police_no"], group_id),
            MSG_REQUEST_FLOOR_ACK, "0x08抢麦")
        floor_ack = parse_request_floor_ack(floor_frame["payload"])
        print("[通信测试] 收到0x88抢麦应答：状态=0x{:02X} UDP端口={}".format(
            floor_ack["floor_status"], floor_ack["udp_port"]))
        if floor_ack["floor_status"] != 0:
            raise RuntimeError("抢麦失败，状态码=0x{:02X}".format(
                floor_ack["floor_status"]))
        udp_port = floor_ack["udp_port"]
        if udp_port <= 0 or udp_port > 65535:
            raise RuntimeError("0x88返回UDP端口无效：{}".format(udp_port))
        _send_rtp_test_stream(
            udp_socket, config["tcp_host"], udp_port)

        release_request_count = floor_request_count + 1
        release_frame = _send_and_receive(
            tcp_socket, parser, MSG_RELEASE_FLOOR, release_request_count,
            build_floor_payload(
                config["device_id"], config["police_no"], group_id),
            MSG_RELEASE_FLOOR_ACK, "0x09释放麦权")
        release_ack = parse_release_floor_ack(release_frame["payload"])
        print("[通信测试] 收到0x89释放麦权应答：结果码=0x{:02X}".format(
            release_ack["release_status"]))
        if (release_ack["release_status"] != 0 or
                release_ack["device_id"] != config["device_id"].encode("ascii") or
                release_ack["group_id"] != group_id):
            raise RuntimeError("释放麦权失败或应答字段不匹配")
        print("[通信测试] 松开PTT→释放麦权完成")

        heartbeat_request_count = release_request_count + 1
        heartbeat_frame = _send_and_receive(
            tcp_socket, parser, MSG_HEARTBEAT, heartbeat_request_count,
            build_heartbeat_payload(config["device_id"]),
            MSG_HEARTBEAT_ACK, "0x02 TCP心跳")
        print("[通信测试] 收到0x82 TCP心跳应答：request_cnt={}".format(
            heartbeat_frame["request_cnt"]))
        print("[通信测试] TCP心跳通过")

        print("[通信测试] 开始断开TCP，验证断线重连")
        tcp_socket.close()
        utime.sleep_ms(RECONNECT_WAIT_MS)
        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_socket.settimeout(TCP_CONNECT_TIMEOUT_SECONDS)
        tcp_socket.connect(target)
        print("[通信测试] TCP重连成功")
        _login_socket(
            tcp_socket, config, heartbeat_request_count + 1, "重连后")
        print("[通信测试] 断线重连并重新登录通过")
        print("[通信测试] 登录→入组→抢麦→RTP持续发送→释放→心跳→重连流程通过")
    finally:
        try:
            udp_socket.close()
        except Exception:
            pass
        try:
            tcp_socket.close()
        except Exception:
            pass


def _test_live_server(config):
    print("[通信测试] 真实服务器测试开始：{}".format(config["mode_name"]))
    print("[通信测试] 等待蜂窝网络注册和数据拨号完成")
    network = checkNet.CheckNetwork(TEST_PROJECT_NAME, TEST_PROJECT_VERSION)
    network.wait_network_connected()
    print("[通信测试] 蜂窝网络已就绪")
    http_data = _test_live_http(config)
    _test_live_poc_flow(config, http_data)
    print("[通信测试] 真实服务器基础及P0抢麦链路通过")


def _test_receive_workers():
    tcp_frame = build_frame(MSG_LOGIN, 2, b"")
    tcp_socket = ReceiveSocket([tcp_frame])
    tcp_events = []
    tcp_worker = TCPReceiveWorker(
        tcp_socket, lambda event: tcp_events.append(event))
    _assert(tcp_worker.start(), "TCP接收线程启动失败")
    utime.sleep_ms(30)
    tcp_worker.stop()
    utime.sleep_ms(10)
    _assert(tcp_events and tcp_events[0]["type"] == TCP_RX_FRAME,
            "TCP接收帧回调失败")

    udp_socket = ReceiveSocket([b"rtp-test"], udp=True)
    udp_events = []
    udp_worker = RTPReceiveWorker(
        udp_socket, lambda event: udp_events.append(event))
    _assert(udp_worker.start(), "UDP接收线程启动失败")
    utime.sleep_ms(30)
    udp_worker.stop()
    utime.sleep_ms(10)
    _assert(udp_events and udp_events[0]["type"] == RTP_RX_PACKET,
            "UDP接收回调失败")
    print("[通信测试] TCP/UDP永久阻塞接收回调通过")


def main():
    config = _selected_server()
    print("[通信测试] 第三阶段独立测试开始")
    print("[通信测试] 不导入UI，不启动旧TCP/RTP")
    print("[通信测试] 当前环境={} 设备ID={} 警号={}".format(
        config["mode_name"], config["device_id"], config["police_no"]))
    print("[通信测试] TCP={}:{} UDP本地端口={}".format(
        config["tcp_host"], config["tcp_port"], config["rtp_local_port"]))
    _test_queue()
    _test_send_worker(config)
    _test_receive_workers()
    _test_live_server(config)
    print("[通信测试] 全部通过")


if __name__ == "__main__":
    main()
