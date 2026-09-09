# -*- coding: utf-8 -*-
"""独立 LVGL/主循环性能测试。

本文件不导入项目脚本，不使用外置字库和业务线程。显示驱动严格采用
QuecPython 官方示例方式，直接绑定 ``lcd.lcd_write``。

可测量：
    - UI 主循环频率和最大循环间隔；
    - lv.task_handler() 调用频率、平均/最大耗时；
    - 触摸 I2C 采样次数、平均/最大采样间隔；
    - 一次触摸会话持续时间。

限制：当前 LCD 无 TE/VSYNC 接口，且 flush_cb 直接绑定原生 lcd_write，
因此本文件不能得到 LCD 面板实际可见帧率，只能得到 CPU/LVGL 调度指标。
"""

import utime
import gc
import lvgl as lv
from machine import LCD, I2C

try:
    _ticks_us = utime.ticks_us
except AttributeError:
    def _ticks_us():
        return utime.ticks_ms() * 1000


LCD_WIDTH = 296
LCD_HEIGHT = 240
DISPLAY_BUFFER_LINES = 80

I2C_BUS = I2C.I2C1
I2C_MODE = I2C.FAST_MODE
TOUCH_ADDRESS = 0x15
TOUCH_SAMPLE_MS = 5
TOUCH_SAMPLE_US = TOUCH_SAMPLE_MS * 1000
# 用于定位触摸采样异常空洞的阈值。仅在测试结束时汇总打印，
# 触摸过程中不会输出日志，避免串口打印反过来影响采样节奏。
TOUCH_LONG_GAP_THRESHOLD_US = 10000
# 预留适量记录即可；超过容量时仍保留总次数并报告未保存数量。
# 多个统计器同时存在，过大的预分配会挤压 QuecPython 可用堆内存。
TOUCH_LONG_GAP_MAX_RECORDS = 128
TOUCH_REG_FINGER = 0x02
TOUCH_RAW_HEIGHT = 296

COLOR_BLACK = lv.color_hex(0x000000)
COLOR_PAGE_A = lv.color_hex(0x102040)
COLOR_PAGE_B = lv.color_hex(0x104020)
COLOR_WHITE = lv.color_hex(0xFFFFFF)
COLOR_RED = lv.color_hex(0xE02020)
COLOR_YELLOW = lv.color_hex(0xFFE000)

XSTART_H, XSTART_L, YSTART_H, YSTART_L = 0xF0, 0xF1, 0xF2, 0xF3
XEND_H, XEND_L, YEND_H, YEND_L = 0xE0, 0xE1, 0xE2, 0xE3

_INIT = (
    0, 0, 0x01, 2, 0, 120, 0, 0, 0x11, 2, 0, 120,
    0, 0, 0x21, 0, 0, 0x00,
    0, 1, 0x36, 1, 1, 0x60, 0, 1, 0x3A, 1, 1, 0x05,
    0, 1, 0x35, 1, 1, 0x00, 0, 1, 0xC7, 1, 1, 0x00,
    0, 1, 0xCC, 1, 1, 0x09,
    0, 5, 0xB2, 1, 1, 0x0C, 1, 1, 0x0C, 1, 1, 0x00,
    1, 1, 0x33, 1, 1, 0x33, 0, 1, 0xB7, 1, 1, 0x35,
    0, 1, 0xBB, 1, 1, 0x36, 0, 1, 0xC0, 1, 1, 0x2C,
    0, 1, 0xC2, 1, 1, 0x01, 0, 1, 0xC3, 1, 1, 0x0D,
    0, 1, 0xC4, 1, 1, 0x20, 0, 1, 0xC6, 1, 1, 0x0F,
    0, 2, 0xD0, 1, 1, 0xA4, 1, 1, 0xA1,
    0, 14, 0xE0, 1, 1, 0xD0, 1, 1, 0x17, 1, 1, 0x19,
    1, 1, 0x04, 1, 1, 0x03, 1, 1, 0x04, 1, 1, 0x32,
    1, 1, 0x41, 1, 1, 0x43, 1, 1, 0x09, 1, 1, 0x14,
    1, 1, 0x12, 1, 1, 0x33, 1, 1, 0x2C,
    0, 14, 0xE1, 1, 1, 0xD0, 1, 1, 0x18, 1, 1, 0x17,
    1, 1, 0x04, 1, 1, 0x03, 1, 1, 0x04, 1, 1, 0x31,
    1, 1, 0x46, 1, 1, 0x43, 1, 1, 0x09, 1, 1, 0x14,
    1, 1, 0x13, 1, 1, 0x31, 1, 1, 0x2D,
    0, 0, 0x29, 0, 0, 0x2C,
)

_INVALID_P = bytearray((
    0, 4, 0x2A, 1, 1, XSTART_H, 1, 1, XSTART_L,
    1, 1, XEND_H, 1, 1, XEND_L, 0, 4, 0x2B, 1, 1,
    YSTART_H, 1, 1, YSTART_L, 1, 1, YEND_H, 1, 1, YEND_L,
    0, 0, 0x2C,
))


class Display:
    def __init__(self):
        self.lcd = None
        self.buffer = None
        self.draw_buf = None
        self.driver = None
        self.display = None

    def start(self):
        print("[PERFTEST] display start")
        self.lcd = LCD()
        from machine import Pin

        # 创建gpio对象
        self.gpio1 = Pin(Pin.GPIO34, Pin.IN, Pin.PULL_DISABLE, 0)
        
        print("GPIO1 level:", self.gpio1.read())
        
        self.lcd.lcd_init(
            bytearray(_INIT), LCD_WIDTH, LCD_HEIGHT, 52000, 1, 4, 0,
            _INVALID_P,
            bytearray((0, 0, 0x11, 2, 0, 120, 0, 0, 0x21, 0, 0, 0x29)),
            bytearray((0, 0, 0x28, 2, 0, 120, 0, 0, 0x10)), None,TESel=True,TEPin=34)
        print("TEpin=34")
        try:
            self.lcd.lcd_display_on()
        except Exception:
            pass
        lv.init()
        self.buffer = bytearray(LCD_WIDTH * DISPLAY_BUFFER_LINES * 2)
        self.draw_buf = lv.disp_draw_buf_t()
        self.draw_buf.init(self.buffer, None, len(self.buffer))
        self.driver = lv.disp_drv_t()
        self.driver.init()
        self.driver.draw_buf = self.draw_buf
        self.driver.flush_cb = self.lcd.lcd_write
        self.driver.hor_res = LCD_WIDTH
        self.driver.ver_res = LCD_HEIGHT
        self.display = self.driver.register()
        print("[PERFTEST] display ready registered={} buffer={} bytes "
              "flush=native lcd_write".format(
              self.display is not None, len(self.buffer)))


class ChangeStats:
    """记录一个数值/坐标发生变化时的时间间隔，不在采样过程中打印。"""

    def __init__(self):
        self.last_value = None
        self.last_change_us = None
        self.change_count = 0
        self.interval_total_us = 0
        self.interval_count = 0
        self.interval_max_us = 0
        self.long_gap_count = 0
        self.long_gap_record_count = 0
        self.long_gap_record_overflow = 0
        self.long_gap_times_us = [0] * TOUCH_LONG_GAP_MAX_RECORDS
        self.long_gap_intervals_us = [0] * TOUCH_LONG_GAP_MAX_RECORDS

    def observe(self, value, now_us):
        if self.last_value is None:
            self.last_value = value
            self.last_change_us = now_us
            return
        if value == self.last_value:
            return
        self.change_count += 1
        if self.last_change_us is not None:
            interval_us = utime.ticks_diff(now_us, self.last_change_us)
            self.interval_total_us += interval_us
            self.interval_count += 1
            self.interval_max_us = max(self.interval_max_us, interval_us)
            if interval_us >= TOUCH_LONG_GAP_THRESHOLD_US:
                self.long_gap_count += 1
                if self.long_gap_record_count < TOUCH_LONG_GAP_MAX_RECORDS:
                    index = self.long_gap_record_count
                    self.long_gap_times_us[index] = now_us
                    self.long_gap_intervals_us[index] = interval_us
                    self.long_gap_record_count += 1
                else:
                    self.long_gap_record_overflow += 1
        self.last_value = value
        self.last_change_us = now_us


class Touch:
    def __init__(self):
        self.i2c = I2C(I2C_BUS, I2C_MODE)
        self.point = (0, 0)
        self.pressed = False
        self.last_poll_us = None
        self.driver = None
        self.indev = None
        self.poll_count = 0
        self.poll_interval_total = 0
        self.poll_interval_max = 0
        self.long_gap_count = 0
        self.long_gap_record_count = 0
        self.long_gap_record_overflow = 0
        # 预分配记录区，避免测试期间 list.append() 扩容干扰测量。
        self.long_gap_times_us = [0] * TOUCH_LONG_GAP_MAX_RECORDS
        self.long_gap_intervals_us = [0] * TOUCH_LONG_GAP_MAX_RECORDS
        self.raw_point = (0, 0)
        self.raw_update_count = 0
        self.raw_change_stats = ChangeStats()

    def _read_regs(self, register, length):
        data = bytearray(length)
        result = self.i2c.read(TOUCH_ADDRESS, bytes([register]), 1,
                                data, length, 0)
        if result not in (None, 0):
            raise OSError("touch I2C read failed: {}".format(result))
        return data

    @staticmethod
    def _rotate(x, y):
        return max(0, min(LCD_WIDTH - 1, TOUCH_RAW_HEIGHT - 1 - int(y))), \
            max(0, min(LCD_HEIGHT - 1, int(x)))

    def poll(self, now_us):
        if (self.last_poll_us is not None and
                utime.ticks_diff(now_us, self.last_poll_us) < TOUCH_SAMPLE_US):
            return False
        if self.last_poll_us is not None:
            interval = utime.ticks_diff(now_us, self.last_poll_us)
            self.poll_interval_total += interval
            self.poll_interval_max = max(self.poll_interval_max, interval)
            if interval >= TOUCH_LONG_GAP_THRESHOLD_US:
                self.long_gap_count += 1
                if self.long_gap_record_count < TOUCH_LONG_GAP_MAX_RECORDS:
                    index = self.long_gap_record_count
                    self.long_gap_times_us[index] = now_us
                    self.long_gap_intervals_us[index] = interval
                    self.long_gap_record_count += 1
                else:
                    self.long_gap_record_overflow += 1
        self.last_poll_us = now_us
        self.poll_count += 1
        try:
            data = self._read_regs(TOUCH_REG_FINGER, 5)
            fingers = int(data[0])
            event_flag = (int(data[1]) >> 6) & 0x03
            raw_x = ((int(data[1]) & 0x0F) << 8) | int(data[2])
            raw_y = ((int(data[3]) & 0x0F) << 8) | int(data[4])
            if fingers > 0 and event_flag in (0, 2):
                self.raw_point = (raw_x, raw_y)
                self.raw_update_count += 1
                self.raw_change_stats.observe(self.raw_point, now_us)
                self.point = self._rotate(raw_x, raw_y)
                self.pressed = True
            else:
                self.pressed = False
        except Exception:
            self.pressed = False
        return True

    def _read_cb(self, indev_drv, data):
        data.point.x = int(self.point[0])
        data.point.y = int(self.point[1])
        data.state = 1 if self.pressed else 0

    def start(self):
        self.driver = lv.indev_drv_t()
        self.driver.init()
        self.driver.type = lv.INDEV_TYPE.POINTER
        self.driver.read_cb = self._read_cb
        self.indev = self.driver.register()
        print("[PERFTEST] touch ready I2C=1 addr=0x15 sample={}ms".format(
            TOUCH_SAMPLE_MS))

    def reset_stats(self):
        self.poll_count = 0
        self.poll_interval_total = 0
        self.poll_interval_max = 0
        self.long_gap_count = 0
        self.long_gap_record_count = 0
        self.long_gap_record_overflow = 0
        self.raw_point = (0, 0)
        self.raw_update_count = 0
        self.raw_change_stats = ChangeStats()


def _make_page(parent, color, marker_color):
    page = lv.obj(parent)
    page.set_size(LCD_WIDTH, LCD_HEIGHT)
    page.set_style_bg_color(color, lv.PART.MAIN)
    page.set_style_bg_opa(255, lv.PART.MAIN)
    marker = lv.obj(page)
    marker.set_pos(146, 0)
    marker.set_size(4, LCD_HEIGHT)
    marker.set_style_bg_color(marker_color, lv.PART.MAIN)
    marker.set_style_bg_opa(255, lv.PART.MAIN)
    top = lv.obj(page)
    top.set_pos(0, 0)
    top.set_size(LCD_WIDTH, 2)
    top.set_style_bg_color(COLOR_WHITE, lv.PART.MAIN)
    top.set_style_bg_opa(255, lv.PART.MAIN)
    return page


def _make_ui():
    screen = lv.obj()
    screen.set_size(LCD_WIDTH, LCD_HEIGHT)
    screen.set_style_bg_color(COLOR_BLACK, lv.PART.MAIN)
    screen.set_style_bg_opa(255, lv.PART.MAIN)
    tileview = lv.tileview(screen)
    tileview.set_size(LCD_WIDTH, LCD_HEIGHT)
    tileview.set_scroll_dir(lv.DIR.HOR)
    tileview.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    try:
        tileview.clear_flag(lv.obj.FLAG.SCROLL_ELASTIC)
        tileview.set_scroll_chain(lv.DIR.NONE)
    except Exception:
        pass
    tile_a = tileview.add_tile(0, 0, lv.DIR.RIGHT)
    tile_b = tileview.add_tile(1, 0, lv.DIR.LEFT)
    _make_page(tile_a, COLOR_PAGE_A, COLOR_RED)
    _make_page(tile_b, COLOR_PAGE_B, COLOR_YELLOW)
    lv.scr_load(screen)
    screen.invalidate()
    print("[PERFTEST] tileview ready: A<->B")
    return screen, tileview


def _print_change_report(label, stats, run_start_us):
    interval_avg_us = (stats.interval_total_us // stats.interval_count
                       if stats.interval_count else 0)
    print("[性能测试] {}变化={}次 平均变化间隔={}us ({}ms) "
          "最大变化间隔={}us ({}ms)".format(
              label, stats.change_count, interval_avg_us,
              interval_avg_us // 1000, stats.interval_max_us,
              stats.interval_max_us // 1000))
    print("[性能测试] {}变化间隔>={}ms 次数={}".format(
        label, TOUCH_LONG_GAP_THRESHOLD_US // 1000,
        stats.long_gap_count))
    if stats.long_gap_record_count:
        print("[性能测试] {}大间隔发生时刻（相对测试开始，毫秒）:".format(label))
        previous_event_us = None
        for index in range(stats.long_gap_record_count):
            try:
                # 先让出一次执行权，再进行下一行串口输出，避免日志汇总阶段
                # 连续输出触发底层串口阻塞或看门狗。
                utime.sleep_ms(5)
                event_us = stats.long_gap_times_us[index]
                interval_us = stats.long_gap_intervals_us[index]
                elapsed_us = utime.ticks_diff(event_us, run_start_us)
                if previous_event_us is None:
                    period_text = "首次"
                else:
                    period_us = utime.ticks_diff(event_us, previous_event_us)
                    period_text = "距上次={}us ({}ms)".format(
                        period_us, period_us // 1000)
                print("[性能测试]   第{}次：时刻={}ms 间隔={}us ({}ms) {}".format(
                    index + 1, elapsed_us // 1000, interval_us,
                    interval_us // 1000, period_text))
                previous_event_us = event_us
            except Exception as error:
                print("[性能测试] {}第{}次记录输出异常：{}".format(
                    label, index + 1, error))
            if (index & 7) == 7:
                try:
                    gc.collect()
                except Exception:
                    pass
        if stats.long_gap_record_overflow:
            print("[性能测试] {}大间隔记录区已满，未逐条保存={}次".format(
                label, stats.long_gap_record_overflow))


def _print_report(run_us, loop_count, task_count, task_total_us,
                  task_max_us, loop_gap_max_us, touch, touch_count,
                  touch_total_us, touch_max_us, tick_total_us, tick_max_us,
                  sleep_total_us, sleep_max_us, run_start_us,
                  scroll_stats):
    """测试结束后一次性打印，避免串口输出污染循环间隔。"""
    run_us = max(1, int(run_us))
    task_avg_us = task_total_us // task_count if task_count else 0
    poll_avg_us = (touch.poll_interval_total // (touch.poll_count - 1)
                   if touch.poll_count > 1 else 0)
    poll_call_avg_us = touch_total_us // touch.poll_count \
        if touch.poll_count else 0
    tick_avg_us = tick_total_us // loop_count if loop_count else 0
    sleep_avg_us = sleep_total_us // loop_count if loop_count else 0
    # 释放测试期间可能产生的临时对象，避免汇总格式化时堆内存不足。
    gc.collect()
    print("[性能测试] 运行时长={}ms 循环={}次 任务={}次".format(
        run_us // 1000, loop_count, task_count))
    print("[性能测试] 主循环频率={}Hz 最大循环间隔={}us ({}ms)".format(
        loop_count * 1000000 // run_us,
        loop_gap_max_us, loop_gap_max_us // 1000))
    print("[性能测试] LVGL任务频率={}Hz 平均耗时={}us ({}ms) "
          "最大耗时={}us ({}ms)".format(
              task_count * 1000000 // run_us,
              task_avg_us, task_avg_us // 1000,
              task_max_us, task_max_us // 1000))
    print("[性能测试] 触摸采样={}次 平均间隔={}us ({}ms) "
          "最大间隔={}us ({}ms)".format(
              touch.poll_count, poll_avg_us, poll_avg_us // 1000,
              touch.poll_interval_max, touch.poll_interval_max // 1000))
    print("[性能测试] 触摸采样间隔>={}ms 次数={}".format(
        TOUCH_LONG_GAP_THRESHOLD_US // 1000, touch.long_gap_count))
    if touch.long_gap_record_count:
        print("[性能测试] 大间隔发生时刻（相对测试开始，毫秒）:")
        previous_event_us = None
        for index in range(touch.long_gap_record_count):
            try:
                utime.sleep_ms(5)
                event_us = touch.long_gap_times_us[index]
                interval_us = touch.long_gap_intervals_us[index]
                elapsed_us = utime.ticks_diff(event_us, run_start_us)
                if previous_event_us is None:
                    period_text = "首次"
                else:
                    period_us = utime.ticks_diff(event_us, previous_event_us)
                    period_text = "距上次={}us ({}ms)".format(
                        period_us, period_us // 1000)
                print("[性能测试]   第{}次：时刻={}ms 间隔={}us ({}ms) {}".format(
                    index + 1, elapsed_us // 1000, interval_us,
                    interval_us // 1000, period_text))
                previous_event_us = event_us
            except Exception as error:
                print("[性能测试] 第{}次大间隔记录输出异常：{}".format(
                    index + 1, error))
            if (index & 7) == 7:
                try:
                    gc.collect()
                except Exception:
                    pass
        if touch.long_gap_record_overflow:
            print("[性能测试] 大间隔记录区已满，未逐条保存={}次".format(
                touch.long_gap_record_overflow))
    else:
        print("[性能测试] 测试期间没有超过阈值的大间隔")
    print("[性能测试] 触摸原始坐标有效读取={}次（仅手指按下且数据有效时计数）".format(
        touch.raw_update_count))
    _print_change_report("触摸原始坐标", touch.raw_change_stats, run_start_us)
    _print_change_report("tileview scroll_x", scroll_stats, run_start_us)
    print("[性能测试] 触摸I2C调用 平均耗时={}us ({}ms) "
          "最大耗时={}us ({}ms)".format(
              poll_call_avg_us, poll_call_avg_us // 1000,
              touch_max_us, touch_max_us // 1000))
    print("[性能测试] lv.tick_inc 平均耗时={}us ({}ms) "
          "最大耗时={}us ({}ms)".format(
              tick_avg_us, tick_avg_us // 1000,
              tick_max_us, tick_max_us // 1000))
    print("[性能测试] 循环sleep累计平均={}us ({}ms) 最大={}us ({}ms) "
          "触摸状态事件={}".format(
              sleep_avg_us, sleep_avg_us // 1000,
              sleep_max_us, sleep_max_us // 1000, touch_count))
    print("[性能测试] 说明：当前无TE，以上数据不是LCD面板实际可见帧率")


def main():
    print("[PERFTEST] 独立 LVGL 主循环/task 性能测试开始")
    print("[PERFTEST] 不导入项目脚本，不使用外置字库，不启动业务线程")
    print("[PERFTEST] LCD无TE：日志不代表面板实际可见帧率")
    display = Display()
    display.start()
    touch = Touch()
    touch.start()
    screen, tileview = _make_ui()
    screen.invalidate()
    try:
        lv.refr_now(display.display)
    except Exception:
        pass
    scroll_stats = ChangeStats()

    last_us = _ticks_us()
    run_start_us = last_us
    last_loop_us = last_us
    last_pressed = False
    loop_count = 0
    task_count = 0
    task_total_us = 0
    task_max_us = 0
    loop_gap_max_us = 0
    touch_total_us = 0
    touch_max_us = 0
    tick_total_us = 0
    tick_max_us = 0
    sleep_total_us = 0
    sleep_max_us = 0
    touch_event_count = 0
    while True:
        now_us = _ticks_us()
        elapsed_us = utime.ticks_diff(now_us, last_us)
        loop_gap_us = utime.ticks_diff(now_us, last_loop_us)
        loop_gap_max_us = max(loop_gap_max_us, loop_gap_us)
        last_loop_us = now_us
        last_us = now_us
        touch_now_us = _ticks_us()
        touch.poll(touch_now_us)
        touch_elapsed_us = utime.ticks_diff(_ticks_us(), touch_now_us)
        touch_total_us += touch_elapsed_us
        touch_max_us = max(touch_max_us, touch_elapsed_us)
        if touch.pressed != last_pressed:
            touch_event_count += 1
            if touch.pressed:
                pass
            else:
                pass
            last_pressed = touch.pressed

        task_begin_us = _ticks_us()
        tick_begin_us = _ticks_us()
        lv.tick_inc(max(0, int(elapsed_us // 1000)))
        tick_elapsed_us = utime.ticks_diff(_ticks_us(), tick_begin_us)
        tick_total_us += tick_elapsed_us
        tick_max_us = max(tick_max_us, tick_elapsed_us)
        lv.task_handler()
        task_elapsed_us = utime.ticks_diff(_ticks_us(), task_begin_us)
        scroll_stats.observe(int(tileview.get_scroll_x()), _ticks_us())
        task_count += 1
        task_total_us += task_elapsed_us
        task_max_us = max(task_max_us, task_elapsed_us)
        loop_count += 1
        sleep_begin_us = _ticks_us()
        utime.sleep_ms(1)
        sleep_elapsed_us = utime.ticks_diff(_ticks_us(), sleep_begin_us)
        sleep_total_us += sleep_elapsed_us
        sleep_max_us = max(sleep_max_us, sleep_elapsed_us)
        #if utime.ticks_diff(_ticks_us(), run_start_us) >= 1000:#40000000:
        #    print("GPIO1111 level:", display.gpio1.read())
       # while 1:
        #    utime.sleep(10)
            #break

    run_end_us = _ticks_us()
    print("[性能测试] 采集结束，开始输出汇总")
    _print_report(utime.ticks_diff(run_end_us, run_start_us),
                  loop_count, task_count, task_total_us, task_max_us,
                  loop_gap_max_us, touch, touch_event_count,
                  touch_total_us, touch_max_us, tick_total_us, tick_max_us,
                  sleep_total_us, sleep_max_us, run_start_us, scroll_stats)


main()
