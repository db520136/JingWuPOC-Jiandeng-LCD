# -*- coding: utf-8 -*-
"""可删除的 UI A/B 性能诊断层。

诊断只在一次触摸释放后的当前 LVGL 帧完成时输出一行，不在触摸过程中
输出串口日志，避免日志本身改变测试结果。
"""

import utime


def _diff(now_ms, old_ms):
    return utime.ticks_diff(now_ms, old_ms)


class UIPerfDiag:
    def __init__(self, mode_name="A", enabled=True):
        self.mode_name = str(mode_name)
        self.enabled = bool(enabled)
        self._active = False
        self._release_pending = False
        self._frame_ready = False
        self._down_ms = 0
        self._up_ms = 0
        self._last_poll_ms = None
        self._poll_count = 0
        self._poll_interval_total = 0
        self._poll_interval_max = 0
        self._task_count = 0
        self._task_total_ms = 0
        self._task_max_ms = 0
        self._task_start_ms = 0
        self._flush_count = 0
        self._flush_total_ms = 0
        self._flush_max_ms = 0
        self._flush_bytes = 0
        self._flush_start_ms = 0
        self._first_flush_end_ms = None
        self._release_frame_count = 0

    def _clear(self):
        self._poll_count = 0
        self._poll_interval_total = 0
        self._poll_interval_max = 0
        self._task_count = 0
        self._task_total_ms = 0
        self._task_max_ms = 0
        self._flush_count = 0
        self._flush_total_ms = 0
        self._flush_max_ms = 0
        self._flush_bytes = 0
        self._first_flush_end_ms = None
        self._release_frame_count = 0

    def touch_down(self, now_ms):
        if not self.enabled:
            return
        self._clear()
        self._active = True
        self._release_pending = False
        self._frame_ready = False
        self._down_ms = now_ms
        self._up_ms = 0
        self._last_poll_ms = now_ms

    def touch_poll(self, now_ms):
        if not self.enabled or not self._active:
            return
        if self._last_poll_ms is not None:
            interval = _diff(now_ms, self._last_poll_ms)
            self._poll_interval_total += interval
            self._poll_interval_max = max(self._poll_interval_max, interval)
        self._last_poll_ms = now_ms
        self._poll_count += 1

    def touch_up(self, now_ms):
        if not self.enabled or not self._active:
            return
        self._up_ms = now_ms
        self._release_pending = True
        self._frame_ready = False

    def task_start(self, now_ms):
        if self.enabled and self._active:
            self._task_start_ms = now_ms

    def task_end(self, now_ms):
        if not self.enabled or not self._active:
            return
        elapsed = max(0, _diff(now_ms, self._task_start_ms))
        self._task_count += 1
        self._task_total_ms += elapsed
        self._task_max_ms = max(self._task_max_ms, elapsed)

    def flush_start(self, now_ms):
        if self.enabled and self._active:
            self._flush_start_ms = now_ms

    def flush_end(self, now_ms, byte_count):
        if not self.enabled or not self._active:
            return
        elapsed = max(0, _diff(now_ms, self._flush_start_ms))
        self._flush_count += 1
        self._flush_total_ms += elapsed
        self._flush_max_ms = max(self._flush_max_ms, elapsed)
        self._flush_bytes += int(byte_count)
        if self._release_pending and self._first_flush_end_ms is None:
            self._first_flush_end_ms = now_ms

    def frame_complete(self, now_ms):
        if not self.enabled or not self._release_pending:
            return
        self._release_frame_count += 1
        # 正常情况下等到释放后的第一笔 flush；如果本次释放没有产生
        # 新脏区，也在第一帧结束后输出，避免诊断日志永久不触发。
        if (self._first_flush_end_ms is not None or
                self._release_frame_count >= 1):
            self._frame_ready = True

    def finish_if_ready(self, now_ms, page_index, switched, dx, dy):
        if not self.enabled or not self._active or not self._frame_ready:
            return
        touch_ms = max(0, _diff(self._up_ms, self._down_ms))
        first_flush_ms = 0
        if self._first_flush_end_ms is not None:
            first_flush_ms = max(0, _diff(self._first_flush_end_ms,
                                          self._up_ms))
        total_ms = max(0, _diff(now_ms, self._down_ms))
        task_hz = 0
        if touch_ms > 0:
            task_hz = self._task_count * 1000 // touch_ms
        task_avg = 0
        if self._task_count > 0:
            task_avg = self._task_total_ms // self._task_count
        poll_avg = 0
        if self._poll_count > 1:
            poll_avg = self._poll_interval_total // (self._poll_count - 1)
        flush_avg = 0
        if self._flush_count > 0:
            flush_avg = self._flush_total_ms // self._flush_count
        print("[诊断{}] 触摸={}ms 完成={}ms dx={} dy={} 页面={} 切换={} "
              "UI频率={}Hz task平均={}ms task最大={}ms "
              "采样平均={}ms 采样最大={}ms flush次数={} "
              "flush平均={}ms flush最大={}ms 刷新={}字节 松手后首刷={}ms".format(
                  self.mode_name, touch_ms, total_ms, int(dx), int(dy),
                  int(page_index) if page_index is not None else -1,
                  1 if switched else 0, task_hz, task_avg, self._task_max_ms,
                  poll_avg, self._poll_interval_max, self._flush_count,
                  flush_avg, self._flush_max_ms, self._flush_bytes,
                  first_flush_ms))
        self._active = False
        self._release_pending = False
        self._frame_ready = False
