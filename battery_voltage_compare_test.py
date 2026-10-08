# -*- coding: utf-8 -*-

import utime

from machine import Pin
from misc import ADC


SAMPLE_COUNT = 60
SAMPLE_INTERVAL_MS = 500

ADC_CHANNEL = ADC.ADC0
DIVIDER_TOP_KOHM = 220
DIVIDER_BOTTOM_KOHM = 82

NCHRG_GPIO = 14
NSTDBY_GPIO = 13


def adc_to_battery_mv(adc_mv):
	divider_total = DIVIDER_TOP_KOHM + DIVIDER_BOTTOM_KOHM
	return ((int(adc_mv) * divider_total) +
		DIVIDER_BOTTOM_KOHM // 2) // DIVIDER_BOTTOM_KOHM


def read_adc_mv(adc):
	value = adc.read(ADC_CHANNEL)
	if isinstance(value, (tuple, list)):
		if not value:
			raise ValueError("ADC返回空数据")
		value = value[0]
	value = int(value)
	if value < 0:
		raise ValueError("ADC返回负电压")
	return value


def read_power_state(nchrg_pin, nstdby_pin):
	nchrg = int(nchrg_pin.read())
	nstdby = int(nstdby_pin.read())
	if nchrg == 0 and nstdby == 1:
		return "charging", "充电态", nchrg, nstdby
	if nchrg == 1 and nstdby == 0:
		return "full", "充满态", nchrg, nstdby
	if nchrg == 1 and nstdby == 1:
		return "discharging", "非充电态", nchrg, nstdby
	return "invalid", "状态脚异常", nchrg, nstdby


def average(values):
	if not values:
		return None
	return (sum(values) + len(values) // 2) // len(values)


def print_group(title, records):
	print("\n========== {}：共{}次 ==========".format(title, len(records)))
	if not records:
		print("没有采集到{}数据".format(title))
		return
	values = []
	for record in records:
		index, elapsed_ms, battery_mv, adc_mv, nchrg, nstdby = record
		values.append(battery_mv)
		print("第{:02d}点  时间={:5d}ms  电池={}mV  ADC={}mV  "
			"NCHRG={} NSTDBY={}".format(
				index, elapsed_ms, battery_mv, adc_mv, nchrg, nstdby))
	print("{}电压列表：{}".format(
		title, ", ".join(["{}mV".format(value) for value in values])))
	print("{}统计：平均={}mV，最小={}mV，最大={}mV，波动={}mV".format(
		title, average(values), min(values), max(values),
		max(values) - min(values)))


def run_test():
	adc = ADC()
	nchrg_pin = Pin(NCHRG_GPIO, Pin.IN, Pin.PULL_PU, 1)
	nstdby_pin = Pin(NSTDBY_GPIO, Pin.IN, Pin.PULL_PU, 1)
	records = {
		"charging": [],
		"discharging": [],
		"full": [],
		"invalid": [],
	}
	open_result = adc.open()
	if open_result not in (None, 0):
		raise OSError("ADC打开失败，返回值：{}".format(open_result))

	print("[电池压差测试] 开始采集：总时长30秒，每500ms采集1次，共60点")
	print("[电池压差测试] 测试期间请插入或拔出充电器，结束后统一打印分类结果")
	start_ms = utime.ticks_ms()
	try:
		for index in range(1, SAMPLE_COUNT + 1):
			target_ms = utime.ticks_add(
				start_ms, index * SAMPLE_INTERVAL_MS)
			wait_ms = utime.ticks_diff(target_ms, utime.ticks_ms())
			if wait_ms > 0:
				utime.sleep_ms(wait_ms)

			elapsed_ms = utime.ticks_diff(utime.ticks_ms(), start_ms)
			adc_mv = read_adc_mv(adc)
			battery_mv = adc_to_battery_mv(adc_mv)
			state, _, nchrg, nstdby = read_power_state(
				nchrg_pin, nstdby_pin)
			records[state].append((
				index, elapsed_ms, battery_mv, adc_mv, nchrg, nstdby))
		print("[电池压差测试] 60个采样点已完成，开始整理结果")
	finally:
		adc.close()

	print_group("充电态", records["charging"])
	print_group("非充电态", records["discharging"])
	print_group("充满态", records["full"])
	print_group("状态脚异常", records["invalid"])

	charging_values = [record[2] for record in records["charging"]]
	discharging_values = [record[2] for record in records["discharging"]]
	charging_average = average(charging_values)
	discharging_average = average(discharging_values)
	print("\n========== 充电与非充电压差结论 ==========")
	if charging_average is None or discharging_average is None:
		print("无法计算压差：30秒内必须同时采集到充电态和非充电态数据")
	else:
		difference = charging_average - discharging_average
		print("充电态平均电压：{}mV".format(charging_average))
		print("非充电态平均电压：{}mV".format(discharging_average))
		print("平均压差（充电态－非充电态）：{}mV".format(difference))
	print("[电池压差测试] 测试结束")


if __name__ == "__main__":
	run_test()
