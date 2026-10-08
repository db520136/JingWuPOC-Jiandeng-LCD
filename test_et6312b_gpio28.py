# -*- coding: utf-8 -*-

import utime

from machine import Pin

try:
	from usr.et6312b import ShoulderLampController
	from usr.i2c_bus import get_i2c0
except Exception:
	from poc_ui.et6312b import ShoulderLampController
	from poc_ui.i2c_bus import get_i2c0


GPIO = Pin.GPIO28
ACTIVE_LEVEL = 0
POLL_MS = 20
DEBOUNCE_MS = 60
SHORT_PRESS_MAX_MS = 2000
ET6312B_I2C_ADDRESS = 0x48
CHIP_CONTROL_REGISTER = 0x00
CHIP_CONTROL_NORMAL = 0x10
PRE_WAKEUP_ATTEMPTS = 3
PRE_WAKEUP_RETRY_MS = 50
PRE_WAKEUP_SETTLE_MS = 10


def pre_wakeup_et6312b():
	"""测试用：先退出ET6312B软件关机，再进入驱动初始化。"""
	i2c = get_i2c0()
	register = bytearray((CHIP_CONTROL_REGISTER,))
	value = bytearray((CHIP_CONTROL_NORMAL,))
	last_error = None

	for attempt in range(1, PRE_WAKEUP_ATTEMPTS + 1):
		try:
			result = i2c.write(
				ET6312B_I2C_ADDRESS,
				register,
				len(register),
				value,
				len(value),
			)
			if result in (0, None):
				print("[测试] ET6312B预唤醒成功：写入0x00=0x10，第{}/{}次".format(
					attempt, PRE_WAKEUP_ATTEMPTS))
				utime.sleep_ms(PRE_WAKEUP_SETTLE_MS)
				return True
			last_error = "I2C返回值={}".format(result)
		except Exception as error:
			last_error = error
		print("[测试] ET6312B预唤醒失败：第{}/{}次 错误={}".format(
			attempt, PRE_WAKEUP_ATTEMPTS, last_error))
		if attempt < PRE_WAKEUP_ATTEMPTS:
			utime.sleep_ms(PRE_WAKEUP_RETRY_MS)

	raise OSError("ET6312B预唤醒失败：{}".format(last_error))


def main():
	key = Pin(GPIO, Pin.IN, Pin.PULL_PU, 1)
	lamp = None
	pressed = False
	pressed_at = None
	last_edge_at = utime.ticks_ms()
	success_count = 0
	fail_count = 0

	print("[测试] ET6312B GPIO28独立测试启动")
	print("[测试] 仅使用GPIO28和I2C0，短按切换肩灯，长按忽略")
	#utime.sleep_ms(1000)
	try:
		pre_wakeup_et6312b()
		lamp = ShoulderLampController()
		print("[测试] ET6312B初始化成功，当前模式={}".format(
			lamp.get_mode()))
	except Exception as error:
		print("[测试] ET6312B初始化失败：{}".format(error))
		return

	try:
		while True:
			now = utime.ticks_ms()
			is_pressed = int(key.read()) == ACTIVE_LEVEL

			if is_pressed != pressed:
				if utime.ticks_diff(now, last_edge_at) < DEBOUNCE_MS:
					utime.sleep_ms(POLL_MS)
					continue
				last_edge_at = now
				pressed = is_pressed

				if pressed:
					pressed_at = now
					print("[测试] GPIO28按下")
				elif pressed_at is not None:
					held_ms = max(0, utime.ticks_diff(now, pressed_at))
					pressed_at = None
					if held_ms > SHORT_PRESS_MAX_MS:
						print("[测试] 长按{}ms，忽略".format(held_ms))
					else:
						before = lamp.get_mode()
						start = utime.ticks_ms()
						try:
							after = lamp.cycle_mode()
							duration = utime.ticks_diff(utime.ticks_ms(), start)
							success_count += 1
							print("[测试] 短按{}ms切换成功：{} -> {}，耗时={}ms，成功={}失败={}".format(
								held_ms, before, after, duration,
								success_count, fail_count))
						except Exception as error:
							fail_count += 1
							print("[测试] 短按{}ms切换失败：{}，成功={}失败={}".format(
								held_ms, error, success_count, fail_count))

			utime.sleep_ms(POLL_MS)
	except KeyboardInterrupt:
		print("[测试] 收到退出信号")
	finally:
		if lamp is not None:
			try:
				lamp.set_mode(ShoulderLampController.MODE_OFF)
				print("[测试] 退出前已关闭肩灯")
			except Exception as error:
				print("[测试] 退出时关闭肩灯失败：{}".format(error))
		print("[测试] 最终统计：成功={}失败={}".format(
			success_count, fail_count))


if __name__ == "__main__":
	main()
