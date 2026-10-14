# -*- coding: utf-8 -*-
from machine import Pin
Pin(Pin.GPIO38, Pin.OUT, Pin.PULL_PD , 1)
from usr.font_port import mount_external_flash
from usr.ui_app import main as start_ui
from usr.ui_commands import UICommandQueue
from usr.ui_messages import UIMessageBus
from usr.ui_state import UIStateStore
from usr.low_priority_worker import LowPriorityWorker
from usr.battery_monitor import BatteryMonitor
from usr.network_monitor import NetworkMonitor
from usr.et6312b import initialize_shoulder_lamp
from usr.sc7a20h import SC7A20H, SC7A20HDoubleClickAdapter, SCALE_4G
from usr.communication_controller import CommunicationController
from usr.device_port import DevicePort
from usr.hardware_key_port import HardwareKeyPort
from usr.config_store import PersistentConfig
from usr.ota_service import OTAService
from machine import WDT
SERVER_MODE_PUBLIC = 0
SERVER_MODE_PRIVATE = 1
SERVER_MODE = SERVER_MODE_PRIVATE
DEVICE_ID = "YNPOC110160930100012"
TCP_SERVER_PORT = 6060
RTP_LOCAL_PORT = 4001
PUBLIC_TCP_HOST = "125.124.233.231"
PUBLIC_HTTP_BASE = "http://125.124.233.231:18081"
PRIVATE_TCP_HOST = "68.95.0.31"
PRIVATE_HTTP_BASE = "http://68.95.0.31:38080"
PUBLIC_MQTT_HOST = "125.124.233.231"
PUBLIC_MQTT_PORT = 1883
PRIVATE_MQTT_HOST = "68.95.0.31"
PRIVATE_MQTT_PORT = 1883
PTT_GPIO = 29
SPEAKER_ENABLE_GPIO = 27
VOLUME_UP_GPIO = 30
VOLUME_DOWN_GPIO = 31
SCREEN_CONTROL_GPIO = 5
LAMP_CONTROL_GPIO = 28
BATTERY_CHARGE_GPIO = 14
BATTERY_FULL_GPIO = 13
SOFTWARE_VERSION = "1.5.10"
HARDWARE_VERSION = "1.0.1"
GNSS_SAMPLE_UPLOAD_INTERVAL_MS = 30000
GNSS_CHANGE_THRESHOLD_M = 5.0
WATCHDOG_TIMEOUT_S = 20
WATCHDOG_FEED_INTERVAL_MS = 5000
def selected_server():
	if SERVER_MODE == SERVER_MODE_PUBLIC:
		return {
			"mode_name": "公网",
			"tcp_host": PUBLIC_TCP_HOST,
			"http_base": PUBLIC_HTTP_BASE,
			"mqtt_host": PUBLIC_MQTT_HOST,
			"mqtt_port": PUBLIC_MQTT_PORT,
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
			"mqtt_host": PRIVATE_MQTT_HOST,
			"mqtt_port": PRIVATE_MQTT_PORT,
			"http_url": (PRIVATE_HTTP_BASE +
							 "/biz/deviceInfo/data?deviceId=" + DEVICE_ID),
			"device_id": DEVICE_ID,
			"ptt_gpio": PTT_GPIO,
			"speaker_enable_gpio": SPEAKER_ENABLE_GPIO,
			"tcp_port": TCP_SERVER_PORT,
			"udp_local_port": RTP_LOCAL_PORT,
		}
	raise ValueError("SERVER_MODE配置无效")
def _make_services(state_store, message_bus, command_queue, communication,
				   persistent_config):
	battery = BatteryMonitor(
		nchrg_gpio=BATTERY_CHARGE_GPIO,
		nstdby_gpio=BATTERY_FULL_GPIO,
		persistent_config=persistent_config)
	network = NetworkMonitor()
	lamp = initialize_shoulder_lamp()
	motion_sensor = SC7A20H(scale=SCALE_4G)
	worker = LowPriorityWorker(
		command_queue=command_queue,
		state_store=state_store,
		message_bus=message_bus,
		battery=battery,
		network=network,
		lamp=lamp,
		motion_sensor=motion_sensor,
		device=DevicePort(SOFTWARE_VERSION, HARDWARE_VERSION, DEVICE_ID),
		key_action_callback=communication.request_hardware_key,
		gnss_result_callback=communication.request_gnss_result,
		persistent_config=persistent_config,
		gnss_interval_ms=GNSS_SAMPLE_UPLOAD_INTERVAL_MS,
		gnss_change_threshold_m=GNSS_CHANGE_THRESHOLD_M)
	communication.set_gnss_sample_request(worker.request_gnss_sample)
	communication.set_screen_wake_request(worker.request_screen_wake)
	hardware_keys = HardwareKeyPort(
		worker.notify_hardware_key, VOLUME_UP_GPIO, VOLUME_DOWN_GPIO,
		PTT_GPIO, SCREEN_CONTROL_GPIO, LAMP_CONTROL_GPIO)
	worker.hardware_keys = hardware_keys
	sensor_service = SC7A20HDoubleClickAdapter(
		motion_sensor, worker.notify_double_click, int1_pin=25,
		double_click_enabled=state_store.get_snapshot()[1]["double_click_enabled"])
	return battery, network, lamp, motion_sensor, worker, sensor_service
def start():
	server = selected_server()
	# 字库必须先挂载，随后 UI 才会创建 LVGL 文字对象。
	mount_external_flash()
	persistent_config = PersistentConfig()
	settings = persistent_config.snapshot()
	state_store = UIStateStore(settings)
	state_store.update_device_versions(
		SOFTWARE_VERSION, HARDWARE_VERSION, "--", DEVICE_ID)
	message_bus = UIMessageBus()
	command_queue = UICommandQueue()
	communication = CommunicationController(
		server, state_store, message_bus,
		project_name="JingWuPOC", project_version=SOFTWARE_VERSION,
		persistent_config=persistent_config)
	ota_service = OTAService(
		server, state_store, communication, persistent_config)
	services = _make_services(
		state_store, message_bus, command_queue, communication,
		persistent_config)
	(battery, network, lamp, motion_sensor,
	 worker, sensor_service) = services
	watchdog = None
	try:
		watchdog = WDT(WATCHDOG_TIMEOUT_S)
	except Exception as error:
		print("[看门狗] 启动失败，程序继续运行：{}".format(error))
	return start_ui(
		battery=battery,
		network=network,
		lamp=lamp,
		low_priority_worker=worker,
		sensor_service=sensor_service,
		command_queue=command_queue,
		state_store=state_store,
		message_bus=message_bus,
		communication_controller=communication,
		ota_service=ota_service,
		initial_police_no=settings["police_no"],
		watchdog=watchdog,
		watchdog_feed_interval_ms=WATCHDOG_FEED_INTERVAL_MS)
import utime
if __name__ == "__main__":
	while 1:
		utime.sleep_ms(3000)
	start()
