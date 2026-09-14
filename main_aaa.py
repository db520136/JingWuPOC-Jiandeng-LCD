# -*- coding: utf-8 -*-
"""警务 POC 正式启动入口。

本入口只负责装配硬件服务、共享状态和 UI；网络协议状态机在通信整合阶段
由独立控制器接入。警号由平台设备信息下发，设备端不保存或配置警号。
"""

import utime

try:
	from usr.font_port import mount_external_flash
	from usr.ui_app import main as start_ui
	from usr.ui_commands import UICommandQueue
	from usr.ui_messages import UIMessageBus
	from usr.ui_state import UIStateStore
	from usr.low_priority_worker import LowPriorityWorker
	from usr.battery_monitor import BatteryMonitor
	from usr.network_monitor import NetworkMonitor
	from usr.et6312b import ShoulderLampController
	from usr.sc7a20h import (SC7A20H, SC7A20HDoubleClickAdapter,
							  SCALE_4G)
	from usr.communication_controller import CommunicationController
except ImportError:
	from poc_ui.font_port import mount_external_flash
	from poc_ui.ui_app import main as start_ui
	from poc_ui.ui_commands import UICommandQueue
	from poc_ui.ui_messages import UIMessageBus
	from poc_ui.ui_state import UIStateStore
	from poc_ui.low_priority_worker import LowPriorityWorker
	from poc_ui.battery_monitor import BatteryMonitor
	from poc_ui.network_monitor import NetworkMonitor
	from poc_ui.et6312b import ShoulderLampController
	from poc_ui.sc7a20h import (SC7A20H, SC7A20HDoubleClickAdapter,
							  SCALE_4G)
	from poc_comm.communication_controller import CommunicationController


# ============================ 设备端配置 ============================

SERVER_MODE_PUBLIC = 0
SERVER_MODE_PRIVATE = 1
SERVER_MODE = SERVER_MODE_PUBLIC

DEVICE_ID = "33030002002000000590"
TCP_SERVER_PORT = 6060
RTP_LOCAL_PORT = 4001

PUBLIC_TCP_HOST = "125.124.233.231"
PUBLIC_HTTP_BASE = "http://125.124.233.231:18081"
PRIVATE_TCP_HOST = "68.95.0.31"
PRIVATE_HTTP_BASE = "http://68.95.0.31:38080"

PTT_GPIO = 29
SPEAKER_ENABLE_GPIO = 27


def selected_server():
	"""返回当前服务器配置；不包含平台下发的警号。"""
	if SERVER_MODE == SERVER_MODE_PUBLIC:
		return {
			"mode_name": "公网",
			"tcp_host": PUBLIC_TCP_HOST,
			"http_base": PUBLIC_HTTP_BASE,
			"http_url": (PUBLIC_HTTP_BASE +
							 "/biz/deviceInfo/data?deviceId=" + DEVICE_ID),
			"device_id": DEVICE_ID,
			"ptt_gpio": PTT_GPIO,
			"speaker_enable_gpio": SPEAKER_ENABLE_GPIO,
			"tcp_port": TCP_SERVER_PORT,
			"udp_local_port": RTP_LOCAL_PORT,
		}
	if SERVER_MODE == SERVER_MODE_PRIVATE:
		return {
			"mode_name": "专网",
			"tcp_host": PRIVATE_TCP_HOST,
			"http_base": PRIVATE_HTTP_BASE,
			"http_url": (PRIVATE_HTTP_BASE +
							 "/biz/deviceInfo/data?deviceId=" + DEVICE_ID),
			"device_id": DEVICE_ID,
			"ptt_gpio": PTT_GPIO,
			"speaker_enable_gpio": SPEAKER_ENABLE_GPIO,
			"tcp_port": TCP_SERVER_PORT,
			"udp_local_port": RTP_LOCAL_PORT,
		}
	raise ValueError("SERVER_MODE配置无效")


def _make_services(state_store, message_bus, command_queue):
	"""创建低优先级硬件服务；此处不启动线程。"""
	battery = BatteryMonitor()
	network = NetworkMonitor()
	lamp = ShoulderLampController()
	motion_sensor = SC7A20H(scale=SCALE_4G)
	worker = LowPriorityWorker(
		command_queue=command_queue,
		state_store=state_store,
		message_bus=message_bus,
		battery=battery,
		network=network,
		lamp=lamp,
		motion_sensor=motion_sensor)
	sensor_service = SC7A20HDoubleClickAdapter(
		motion_sensor, worker.notify_double_click, int1_pin=25)
	return battery, network, lamp, motion_sensor, worker, sensor_service


def start():
	"""装配并启动正式 UI；启动线程由 UI 应用按固定顺序完成。"""
	server = selected_server()
	print("[正式入口] 启动，环境={} 设备ID={} TCP={}:{} UDP本地端口={}".format(
		server["mode_name"], DEVICE_ID, server["tcp_host"],
		server["tcp_port"], server["udp_local_port"]))
	# 字库必须先挂载，随后 UI 才会创建 LVGL 文字对象。
	mount_external_flash()
	state_store = UIStateStore()
	message_bus = UIMessageBus()
	command_queue = UICommandQueue()
	services = _make_services(state_store, message_bus, command_queue)
	(battery, network, lamp, motion_sensor,
	 worker, sensor_service) = services
	print("[正式入口] UI硬件服务已创建：电池、网络、肩灯、加速度、低优先级Worker")
	communication = CommunicationController(
		server, state_store, message_bus,
		project_name="JingWuPOC", project_version="1.0.0")
	communication.start()
	print("[正式入口] 统一通信业务线程已启动，等待UI登录请求")
	return start_ui(
		battery=battery,
		network=network,
		lamp=lamp,
		low_priority_worker=worker,
		sensor_service=sensor_service,
		command_queue=command_queue,
		state_store=state_store,
		message_bus=message_bus,
		communication_controller=communication)


if __name__ == "__main__":
	start()
