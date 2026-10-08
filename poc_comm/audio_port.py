# -*- coding: utf-8 -*-
"""EC800M 音频硬件端口。

本文件是新通信架构唯一直接操作音频硬件和 GPIO27 喇叭使能脚的模块：

* 麦克风：G.711 A-law 采集，整理为每包 160 字节；
* 远端语音：接收 160 字节 G.711 A-law 帧并写入 PCM；
* 按键提示音：使用同一 PCM/G.711 输出链路；
* GPIO27：初始化低电平保持1ms；开启使用硬件要求的脉冲序列，关闭拉低。

PTT GPIO29 不在这里处理，由 ``communication_session.py`` 负责。文件只使用
已确认的 QuecPython 固件接口，不进行动态属性探测。
"""

import _thread
import utime

try:
	from machine import Pin
except ImportError:
	Pin = None

try:
	import audio
except ImportError:
	audio = None

try:
	import G711
except ImportError:
	G711 = None


SPEAKER_ENABLE_GPIO = 27
PCM_SAMPLE_RATE = 8000
# 与旧版 POC_Old/main.py 的 PCM 配置保持一致：底层使用 4 个周期缓冲。
# 这样新旧架构在 EC800M 上使用相同的采集/播放缓冲深度，便于实机对比。
PCM_PERIOD_COUNT = 4
PCM_FRAME_SIZE = 160
KEY_BEEP_G711_DATA = bytes((0xD5, 0xD5, 0x2A, 0x2A)) * 40
KEY_BEEP_HOLD_MS = 30
KEY_BEEP_FRAME_COUNT = 8
FORCED_LOGOUT_TTS_TEXT = "账号强制下线"
FORCED_LOGOUT_TTS_WAIT_MS = 3500


def play_forced_logout_tts(volume):
	"""PCM已关闭后独占音频输出；返回前关闭TTS和功放。"""
	if Pin is None or audio is None:
		raise RuntimeError("machine.Pin或audio模块不可用")
	pin = Pin(Pin.GPIO27, Pin.OUT, Pin.PULL_DISABLE, 0)
	pin.write(0)
	utime.sleep_ms(1)
	tts = None
	try:
		# 独立测试中PCM关闭后留出短暂的驱动释放时间。
		utime.sleep_ms(300)
		device = audio.Audio(0)
		device.setVolume(max(0, min(11, int(volume))))
		tts = audio.TTS(0)
		tts.setVolume(max(0, min(9, int(volume))))
		for level in (1, 0, 1, 0, 1, 0, 1):
			pin.write(level)
		result = tts.play(1, 0, 2, FORCED_LOGOUT_TTS_TEXT)
		if isinstance(result, int) and result < 0:
			raise RuntimeError("TTS播放返回失败：{}".format(result))
		utime.sleep_ms(FORCED_LOGOUT_TTS_WAIT_MS)
	finally:
		pin.write(0)
		if tts is not None:
			tts.close()


class AudioPort:
	"""统一管理麦克风采集、远端播放和 GPIO27。"""

	def __init__(self, speaker_gpio=SPEAKER_ENABLE_GPIO,
				 pcm_periodcnt=PCM_PERIOD_COUNT, volume=11):
		self.speaker_gpio = int(speaker_gpio)
		self.pcm_periodcnt = max(2, min(25, int(pcm_periodcnt)))
		self.volume = int(volume)
		self.speaker_pin = None
		self.audio_device = None
		self.pcm = None
		self.g711 = None
		self.running = False
		self.speaker_enabled = False
		self.playback_active = False
		self.capture_cache = bytearray()
		self._playback_error_logged = False
		self._lock = _thread.allocate_lock()

	def start(self):
		if self.running:
			return True
		if Pin is None or audio is None or G711 is None:
			raise RuntimeError("machine.Pin、audio或G711模块不可用")
		self.speaker_pin = Pin(
			Pin.GPIO27, Pin.OUT, Pin.PULL_DISABLE, 0)
		self.speaker_pin.write(0)
		self.speaker_enabled = False
		# 上电初始化阶段必须连续保持低电平至少1ms；此时尚未启动通信线程。
		utime.sleep_ms(1)
		self.audio_device = audio.Audio(0)
		self.audio_device.setVolume(self.volume)
		self.pcm = audio.Audio.PCM(
			0, 1, PCM_SAMPLE_RATE, 2, 1, self.pcm_periodcnt)
		# 当前 EC800M 固件中 ``import G711`` 返回的就是可调用构造器，
		# 不是包含 G711 子类的模块，因此必须直接实例化；不做动态
		# 接口探测，避免掩盖固件接口问题。
		self.g711 = G711(self.pcm)
		self.capture_cache = bytearray()
		self.playback_active = False
		self.running = True
		return True

	def set_volume(self, volume):
		value = max(0, min(11, int(volume)))
		self._lock.acquire()
		try:
			if self.audio_device is not None:
				self.audio_device.setVolume(value)
			self.volume = value
			return value
		finally:
			self._lock.release()

	def speaker_on(self):
		if self.speaker_pin is None or self.speaker_enabled:
			return
		self.speaker_pin.write(1)
		self.speaker_pin.write(0)
		self.speaker_pin.write(1)
		self.speaker_pin.write(0)
		self.speaker_pin.write(1)
		self.speaker_pin.write(0)
		self.speaker_pin.write(1)
		self.speaker_enabled = True

	def speaker_off(self):
		if self.speaker_pin is None:
			return
		self.speaker_pin.write(0)
		self.speaker_enabled = False

	def start_playback(self):
		self._lock.acquire()
		try:
			if not self.running:
				return False
			self.playback_active = True
			self._playback_error_logged = False
			self.speaker_on()
			return True
		finally:
			self._lock.release()

	def write_playback_frame(self, frame, require_active=False):
		if frame is None or len(frame) != PCM_FRAME_SIZE:
			return False
		self._lock.acquire()
		try:
			if not self.running or self.g711 is None:
				return False
			if require_active and not self.playback_active:
				return False
			self.playback_active = True
			self.speaker_on()
			self.g711.write(bytes(frame), 0)
			return True
		except Exception as error:
			if not self._playback_error_logged:
				self._playback_error_logged = True
			self.playback_active = False
			self.speaker_off()
			return False
		finally:
			self._lock.release()

	def stop_playback(self):
		self._lock.acquire()
		try:
			self.playback_active = False
			self._playback_error_logged = False
			self.speaker_off()
		finally:
			self._lock.release()

	def play_key_beep(self):
		"""播放一次按键提示音；远端语音播放期间不抢占喇叭。"""
		self._lock.acquire()
		try:
			if (not self.running or self.g711 is None or
					self.playback_active):
				return False
			self.speaker_on()
			for _ in range(max(KEY_BEEP_FRAME_COUNT, self.pcm_periodcnt)):
				self.g711.write(KEY_BEEP_G711_DATA, 0)
		except Exception:
			self.speaker_off()
			return False
		finally:
			self._lock.release()
		utime.sleep_ms(max(KEY_BEEP_HOLD_MS, 170))
		self._lock.acquire()
		try:
			if not self.playback_active:
				self.speaker_off()
		finally:
			self._lock.release()
		return True

	def read_capture_frame(self):
		"""读取并整理一帧 160 字节 G.711 A-law 数据。"""
		self._lock.acquire()
		try:
			if not self.running or self.g711 is None:
				return None
			if len(self.capture_cache) < PCM_FRAME_SIZE:
				data = self.g711.read(0)
				if data:
					self.capture_cache.extend(bytes(data))
			if len(self.capture_cache) < PCM_FRAME_SIZE:
				return None
			frame = bytes(self.capture_cache[:PCM_FRAME_SIZE])
			self.capture_cache = bytearray(
				self.capture_cache[PCM_FRAME_SIZE:])
			return frame
		except Exception:
			return None
		finally:
			self._lock.release()

	def close(self):
		self._lock.acquire()
		try:
			self.running = False
			self.playback_active = False
			self.speaker_off()
			if self.pcm is not None:
				self.pcm.close()
			self.pcm = None
			self.g711 = None
			self.audio_device = None
			self.speaker_pin = None
			self.capture_cache = bytearray()
		finally:
			self._lock.release()
