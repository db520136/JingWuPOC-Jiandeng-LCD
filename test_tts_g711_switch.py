# -*- coding: utf-8 -*-
"""TTS 与 G.711/PCM 音频流交替播放测试。

测试目的：
* 使用与正式项目相同的 PCM 和 G.711 参数播放约 3 秒音频流；
* 播放 TTS“强制退出登录”；
* 重复交替播放，观察是否有抢占、卡顿、杂音或播放失败。

本脚本不启动 TCP/UDP，不加载正式通信控制器，只操作音频硬件。
设备执行前请确认没有其它程序正在使用音频输出。
"""

import utime

try:
	from machine import Pin
	import audio
	import G711
except ImportError as error:
	print("[测试] 缺少音频硬件模块：{}".format(error))
	raise


SPEAKER_ENABLE_GPIO = 27
PCM_SAMPLE_RATE = 8000
PCM_PERIOD_COUNT = 4
PCM_FRAME_SIZE = 160
PCM_FRAME_INTERVAL_MS = 20
STREAM_DURATION_MS = 3000
STREAM_FRAME_COUNT = STREAM_DURATION_MS // PCM_FRAME_INTERVAL_MS
TEST_ROUNDS = 3
VOLUME = 11
TTS_TEXT = "强制退出登录"


def encode_alaw(sample):
	"""将16位线性采样编码为一字节G.711 A-law。"""
	positive = sample >= 0
	value = sample if positive else -sample - 1
	value = min(value, 32767)
	segment = 0
	limit = 0xFF
	while value > limit and segment < 7:
		segment += 1
		limit <<= 1
	if segment == 0:
		code = (value >> 4) & 0x0F
	else:
		code = (segment << 4) | ((value >> (segment + 3)) & 0x0F)
	return code ^ (0xD5 if positive else 0x55)


# 8kHz采样下每8个点为一周期，产生约1kHz测试音；160字节对应20ms。
_TONE_SAMPLES = (0, 5657, 8000, 5657, 0, -5657, -8000, -5657)
G711_TEST_FRAME = bytes(encode_alaw(sample) for sample in _TONE_SAMPLES) * 20


def speaker_on(pin):
	"""按正式项目的GPIO27脉冲方式开启功放。"""

	pin.write(1)
	pin.write(0)
	pin.write(1)
	pin.write(0)
	pin.write(1)
	pin.write(0)
	pin.write(1)


def speaker_off(pin):
	if pin is not None:
		pin.write(0)


def play_g711_stream(g711, speaker_pin, round_no):
	"""按20ms节奏写入约3秒G.711数据。"""
	print("[测试] 第{}/{}轮：开始播放G.711音频流，约{}秒".format(
		round_no, TEST_ROUNDS, STREAM_DURATION_MS // 1000))
	speaker_on(speaker_pin)
	started = utime.ticks_ms()
	written = 0
	for index in range(STREAM_FRAME_COUNT):
		deadline = utime.ticks_add(
			started, (index + 1) * PCM_FRAME_INTERVAL_MS)
		try:
			g711.write(G711_TEST_FRAME, 0)
			written += 1
		except Exception as error:
			print("[测试] G.711写入失败：帧={} 错误={}".format(
				index + 1, error))
		remaining = utime.ticks_diff(deadline, utime.ticks_ms())
		if remaining > 0:
			utime.sleep_ms(remaining)
	# 等待PCM底层缓冲把最后几帧送到喇叭，避免马上切到TTS。
	utime.sleep_ms(150)
	#speaker_off(speaker_pin)
	print("[测试] G.711音频流结束：写入{}/{}帧".format(
		written, STREAM_FRAME_COUNT))


def play_tts(speaker_pin, round_no):
	"""播放一次TTS，并给异步TTS预留完成时间。"""
	print("[测试] 第{}/{}轮：开始播放TTS：{}".format(
		round_no, TEST_ROUNDS, TTS_TEXT))
	tts = None
	try:
		tts = audio.TTS(0)
		tts.setVolume(9)
		speaker_on(speaker_pin)
		result = tts.play(1, 0, 2, TTS_TEXT)
		print("[测试] TTS调用返回：{}".format(result))
		utime.sleep_ms(3500)
	except Exception as error:
		print("[测试] TTS播放失败：{}".format(error))
	finally:
		#speaker_off(speaker_pin)
		if tts is not None:
			try:
				tts.close()
			except Exception as error:
				print("[测试] TTS关闭失败：{}".format(error))


def main():
	print("[测试] TTS/G.711交替播放测试启动")
	print("[测试] 每轮：G.711约3秒 -> 间隔 -> TTS -> 间隔，共{}轮".format(
		TEST_ROUNDS))

	speaker_pin = None
	pcm = None
	try:
		speaker_pin = Pin(Pin.GPIO27, Pin.OUT, Pin.PULL_DISABLE, 0)
		speaker_off(speaker_pin)
		utime.sleep_ms(1)

		audio_device = audio.Audio(0)
		audio_device.setVolume(VOLUME)
		print("[测试] 音频初始化成功：PCM={}Hz periodcnt={} G.711帧={}字节".format(
			PCM_SAMPLE_RATE, PCM_PERIOD_COUNT, PCM_FRAME_SIZE))
		for round_no in range(1, TEST_ROUNDS + 1):
			try:
				pcm = audio.Audio.PCM(
					0, 1, PCM_SAMPLE_RATE, 2, 1, PCM_PERIOD_COUNT)
				g711 = G711(pcm)
				play_g711_stream(g711, speaker_pin, round_no)
			finally:
				#speaker_off(speaker_pin)
				if pcm is not None:
					pcm.close()
					pcm = None
			utime.sleep_ms(300)
			play_tts(speaker_pin, round_no)
			utime.sleep_ms(500)
		print("[测试] TTS/G.711交替播放测试完成")
	finally:
		#speaker_off(speaker_pin)
		if pcm is not None:
			try:
				pcm.close()
			except Exception as error:
				print("[测试] PCM关闭失败：{}".format(error))


if __name__ == "__main__":
	main()
