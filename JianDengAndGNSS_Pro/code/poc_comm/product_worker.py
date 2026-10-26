# -*- coding: utf-8 -*-
import _thread
import utime

GNSS_SAMPLE_INTERVAL_MS = 30000
WORKER_INTERVAL_MS = 20


class ProductWorker:
	"""肩灯、GNSS、ADC 和 PTT 的最小业务循环。"""
	def __init__(self, state_store, communication, battery, device,
				 lamp=None, ptt_keys=None,
				 gnss_interval_ms=GNSS_SAMPLE_INTERVAL_MS):
		self.state = state_store
		self.communication = communication
		self.battery = battery
		self.device = device
		self.lamp = lamp
		self.ptt_keys = ptt_keys
		self.gnss_interval_ms = max(1000, int(gnss_interval_ms))
		self.running = False
		self.thread_id = None
		self._next_gnss_ms = utime.ticks_ms()

	def _sample_battery(self):
		try:
			voltage = self.battery.tick()
			if voltage is not None:
				self.state.update_battery(None, voltage)
		except Exception as error:
			print("[电池] 采样失败：{}".format(error))

	def _sample_gnss(self):
		try:
			result = self.device.read_location()
			self.state.update_gnss_location(
				result.get("state"), result.get("fix"),
				result.get("latitude"), result.get("longitude"),
				result.get("altitude"), result.get("satellites"),
				latitude_text=result.get("latitude_text"),
				longitude_text=result.get("longitude_text"))
			self.communication.request_gnss_result(result)
		except Exception as error:
			print("[定位] 采样失败：{}".format(error))

	def _run(self):
		try:
			while self.running:
				self._sample_battery()
				if self.ptt_keys is not None:
					self.ptt_keys.poll()
				now = utime.ticks_ms()
				if utime.ticks_diff(now, self._next_gnss_ms) >= 0:
					self._sample_gnss()
					self._next_gnss_ms = utime.ticks_add(
						now, self.gnss_interval_ms)
				utime.sleep_ms(WORKER_INTERVAL_MS)
		finally:
			self.running = False
			self.thread_id = None

	def start(self):
		if self.running:
			return False
		self.running = True
		if self.ptt_keys is not None:
			self.ptt_keys.start()
		try:
			self.thread_id = _thread.start_new_thread(self._run, ())
			return True
		except Exception:
			self.running = False
			self.thread_id = None
			if self.ptt_keys is not None:
				self.ptt_keys.stop()
			raise

	def stop(self):
		self.running = False
		if self.ptt_keys is not None:
			self.ptt_keys.stop()
