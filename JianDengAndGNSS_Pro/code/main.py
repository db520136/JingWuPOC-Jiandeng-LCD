# -*- coding: utf-8 -*-
import utime
try:
	from machine import Pin, WDT
except ImportError:
	Pin = None
	WDT = None

from usr.battery_monitor import BatteryMonitor
from usr.config_store import PersistentConfig
from usr.device_port import DevicePort
from usr.et6312b import initialize_shoulder_lamp
from usr.ota_service import OTAService
from usr.product_communication import ProductCommunication
from usr.product_state import ProductState
from usr.product_worker import ProductWorker
from usr.ptt_key_port import PTTKeyPort
from usr.uart import ProductionConfigUART

SERVER_MODE_PUBLIC = 0
SERVER_MODE_PRIVATE = 1
SERVER_MODE = SERVER_MODE_PRIVATE
TCP_SERVER_PORT = 6060
PUBLIC_TCP_HOST = "125.124.233.231"
PUBLIC_MQTT_HOST = "125.124.233.231"
PRIVATE_TCP_HOST = "68.95.0.31"
PRIVATE_MQTT_HOST = "68.95.0.31"
MQTT_PORT = 1883
PTT_GPIO = 29
SOFTWARE_VERSION = "1.5.12"
GNSS_SAMPLE_UPLOAD_INTERVAL_MS = 30000
WATCHDOG_TIMEOUT_S = 20
WATCHDOG_FEED_INTERVAL_MS = 5000


def _set_identity(device_id, hardware_version):
	return device_id, hardware_version


def selected_server(device_id):
	if SERVER_MODE == SERVER_MODE_PUBLIC:
		host, mqtt_host = PUBLIC_TCP_HOST, PUBLIC_MQTT_HOST
	elif SERVER_MODE == SERVER_MODE_PRIVATE:
		host, mqtt_host = PRIVATE_TCP_HOST, PRIVATE_MQTT_HOST
	else:
		raise ValueError("SERVER_MODE配置无效")
	return {
		"tcp_host": host,
		"tcp_port": TCP_SERVER_PORT,
		"mqtt_host": mqtt_host,
		"mqtt_port": MQTT_PORT,
		"device_id": device_id,
	}


def _run_production_config(config):
	if Pin is None:
		indicator = None
	else:
		indicator = Pin(Pin.GPIO38, Pin.OUT, Pin.PULL_PD, 1)
	ProductionConfigUART(config, _set_identity, indicator).run()


def start():
	config = PersistentConfig()
	settings = config.snapshot()
	device_id = settings.get("device_id")
	hardware_version = settings.get("hardware_version")
	if not settings.get("identity_confirmed") or not device_id or not hardware_version:
		_run_production_config(config)
		return None

	server = selected_server(device_id)
	state = ProductState(settings)
	device = DevicePort(SOFTWARE_VERSION, hardware_version, device_id)
	firmware_version = "--"
	try:
		firmware_version = device.read_versions()[2]
	except Exception as error:
		print("[设备] 固件版本读取失败：{}".format(error))
	state.update_device_versions(
		SOFTWARE_VERSION, hardware_version, firmware_version, device_id)

	battery = BatteryMonitor()
	lamp = initialize_shoulder_lamp()
	communication = ProductCommunication(
		server, state, project_name="JingWu", project_version=SOFTWARE_VERSION)
	ptt_keys = PTTKeyPort(communication.request_hardware_key, PTT_GPIO)
	worker = ProductWorker(
		state, communication, battery, device, lamp, ptt_keys,
		GNSS_SAMPLE_UPLOAD_INTERVAL_MS)
	ota_service = OTAService(server, state, communication, config)

	communication.start()
	worker.start()
	if not ota_service.start_post_boot_report():
		ota_service.start_query()

	watchdog = None
	if WDT is not None:
		try:
			watchdog = WDT(WATCHDOG_TIMEOUT_S)
		except Exception as error:
			print("[看门狗] 启动失败，程序继续运行：{}".format(error))
	last_feed_ms = utime.ticks_ms()
	try:
		while True:
			now_ms = utime.ticks_ms()
			if watchdog is not None and utime.ticks_diff(
					now_ms, last_feed_ms) >= WATCHDOG_FEED_INTERVAL_MS:
				watchdog.feed()
				last_feed_ms = now_ms
			utime.sleep_ms(1000)
	finally:
		worker.stop()
		communication.stop()


if __name__ == "__main__":
	start()
