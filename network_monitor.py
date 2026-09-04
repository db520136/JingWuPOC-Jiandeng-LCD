# -*- coding: utf-8 -*-
"""EC800M SIM 卡、网络连接和信号状态监控。

本模块不直接操作 LVGL。网络状态检查只在后台线程执行，并在同一线程采集
SIM/信号；触摸快速路径期间暂停低优先级检查，避免抢占 LCD 主循环。
主线程通过 ``tick()`` 只读取缓存。硬件访问不会再阻塞 LCD/LVGL 循环。
"""

import utime

try:
    import _thread
except Exception:
    _thread = None

try:
    import sim
except Exception:
    sim = None

try:
    import net
except Exception:
    net = None

try:
    import dataCall
except Exception:
    dataCall = None

try:
    import checkNet
except Exception:
    checkNet = None



# 信号值每30秒采集一次；进入网络状态页时可请求后台提前采集。
SIGNAL_REFRESH_INTERVAL_MS = 30000
NETWORK_RETRY_INTERVAL_MS = 5000
# Prefer the non-blocking modem status APIs.  ``checkNet.waitNetworkReady``
# is retained only as a compatibility fallback when a firmware does not
# expose a recognizable status shape.
NETWORK_DIRECT_STATUS = True
# During an active touch/scroll, signal and other proactive network work may
# be deferred for a bounded period.  The bound prevents a continuously held
# finger from starving network state refresh forever.
UI_INTERACTION_DEFER_MAX_MS = 30000
_DEFAULT_MONITOR = None

# 返回给界面的固定等级名称，颜色由 lcd_touch.py 根据等级决定。
SIGNAL_VERY_WEAK = "极弱"
SIGNAL_WEAK = "弱"
SIGNAL_GENERAL = "一般"
SIGNAL_STRONG = "强"
SIGNAL_VERY_GOOD = "极好"


def classify_signal(signal):
    """把 dBm 整数转换为信号等级；无法转换时返回 None。"""
    try:
        value = int(signal)
    except Exception:
        return None

    # dBm 分级采用不重叠的区间：-114 是“弱”档的下边界。
    if value < -114:
        return SIGNAL_VERY_WEAK
    if value <= -105:
        return SIGNAL_WEAK
    if value <= -95:
        return SIGNAL_GENERAL
    if value <= -85:
        return SIGNAL_STRONG
    return SIGNAL_VERY_GOOD


def _ticks_add(base_ms, delta_ms):
    try:
        return utime.ticks_add(base_ms, delta_ms)
    except Exception:
        return base_ms + delta_ms


def _ticks_diff(later_ms, earlier_ms):
    try:
        return utime.ticks_diff(later_ms, earlier_ms)
    except Exception:
        return later_ms - earlier_ms


class NetworkMonitor:
    """缓存网络信息的状态机；所有硬件调用都集中在本模块。"""

    def __init__(self, signal_interval_ms=SIGNAL_REFRESH_INTERVAL_MS,
                 start_worker=True):
        self.signal_interval_ms = max(1000, int(signal_interval_ms))
        self.iccid = None
        self.signal_dbm = None
        self.signal_level = None
        self.network_connected = None
        self.network_stage = None
        self.network_state = None
        self.last_error = None
        self._revision = 0
        self._reported_revision = -1
        self._next_signal_ms = utime.ticks_ms()
        self._signal_refresh_requested = True
        self._network_worker_running = False
        self._network_worker_id = None
        self._lock = None
        self._ui_interaction_active = False
        self._ui_interaction_started_ms = None
        self._perf_worker = [0, 0, 0, 0, 0, 0]
        self._perf_wait_ready = [0, 0, 0, 0, 0, 0]
        self._perf_signal = [0, 0, 0, 0, 0, 0]

        if _thread is not None:
            try:
                self._lock = _thread.allocate_lock()
            except Exception:
                self._lock = None

        if start_worker:
            self.start_network_worker()

    def _lock_acquire(self):
        if self._lock is not None:
            self._lock.acquire()

    def _lock_release(self):
        if self._lock is not None:
            self._lock.release()

    def _bump_revision(self):
        self._revision += 1

    @staticmethod
    def _perf_add(item, elapsed):
        item[0] += 1
        item[1] += elapsed
        item[2] = max(item[2], elapsed)
        if elapsed >= 30: item[3] += 1
        if elapsed >= 50: item[4] += 1
        if elapsed >= 100: item[5] += 1

    def get_thread_perf_snapshot(self):
        result = {}
        for name, item in (("worker", self._perf_worker),
                           ("wait_ready", self._perf_wait_ready),
                           ("signal", self._perf_signal)):
            result[name] = tuple(item)
            for i in range(6): item[i] = 0
        return result

    def set_ui_interaction_active(self, active):
        """Gate low-priority signal polling while the LCD is being touched.

        This only changes a flag; it never acquires the monitor lock and never
        interrupts an in-progress modem operation.  The worker checks the flag
        before starting the next signal read and automatically resumes after
        ``UI_INTERACTION_DEFER_MAX_MS``.
        """
        active = bool(active)
        if active == self._ui_interaction_active:
            return
        self._ui_interaction_active = active
        self._ui_interaction_started_ms = (
            utime.ticks_ms() if active else None)

    def _ui_interaction_deferred(self, now_ms=None):
        if not self._ui_interaction_active:
            return False
        started = self._ui_interaction_started_ms
        if started is None:
            return False
        if now_ms is None:
            now_ms = utime.ticks_ms()
        return _ticks_diff(now_ms, started) < UI_INTERACTION_DEFER_MAX_MS

    def start_network_worker(self):
        """启动唯一网络检测线程；重复调用不会创建多个线程。"""
        if self._network_worker_running:
            return False
        if (_thread is None or
                (not NETWORK_DIRECT_STATUS and checkNet is None) or
                (NETWORK_DIRECT_STATUS and net is None and
                 dataCall is None and checkNet is None)):
            self.last_error = RuntimeError("网络线程或网络状态接口不可用")
            return False
        self._network_worker_running = True
        try:
            self._network_worker_id = _thread.start_new_thread(
                self._network_worker, ())
            return True
        except Exception as error:
            self._network_worker_running = False
            self._network_worker_id = None
            self.last_error = error
            return False

    def _network_worker(self):
        """后台执行可能阻塞的网络就绪检测，不接触任何 LVGL 对象。"""
        try:
            while self._network_worker_running:
                worker_started_ms = utime.ticks_ms()
                now_ms = utime.ticks_ms()

                # ``checkNet.waitNetworkReady`` is a low-priority
                # readiness probe.  Some QuecPython builds implement this
                # call with a long Python-side polling loop; allowing it to
                # start while the LCD is in its touch fast path can therefore
                # steal the interpreter for tens/hundreds of milliseconds.
                # Defer the *whole* probe (not just signal sampling) until
                # the interaction ends.  The cached state remains valid for
                # the bounded 30-second interaction window.
                if self._ui_interaction_deferred(now_ms):
                    # Use a coarse one-second checkpoint rather than a
                    # 100-ms polling loop.  This preserves the 30-second
                    # starvation bound while keeping scheduler wakeups rare.
                    utime.sleep_ms(1000)
                    self._perf_add(self._perf_worker,
                                   _ticks_diff(utime.ticks_ms(),
                                               worker_started_ms))
                    continue

                signal_due = (self._signal_refresh_requested or
                              _ticks_diff(now_ms, self._next_signal_ms) >= 0)
                if (signal_due and
                        not self._ui_interaction_deferred(now_ms)):
                    # Clear before the hardware call so a concurrent page
                    # refresh request remains pending for the next iteration.
                    self._signal_refresh_requested = False
                    signal_started_ms = utime.ticks_ms()
                    self._update_signal_cache()
                    self._perf_add(self._perf_signal,
                                   _ticks_diff(utime.ticks_ms(), signal_started_ms))
                    self._next_signal_ms = _ticks_add(
                        utime.ticks_ms(), self.signal_interval_ms)
                # Prefer direct, non-blocking modem status reads.  This avoids
                # an in-flight 30-second waitNetworkReady() call straddling a
                # touch edge and stealing the interpreter from the LCD loop.
                stage = None
                state = None
                connected = False
                wait_started_ms = utime.ticks_ms()
                try:
                    direct_known = False
                    if NETWORK_DIRECT_STATUS and (net is not None or
                                                   dataCall is not None):
                        net_ready = None
                        if net is not None:
                            try:
                                net_state = net.getState()
                                if isinstance(net_state, (tuple, list)):
                                    if (len(net_state) >= 2 and
                                            not isinstance(net_state[1],
                                                         (tuple, list))):
                                        stage = int(net_state[0])
                                        state = int(net_state[1])
                                        net_ready = (stage == 3 and
                                                     state == 1)
                                    elif len(net_state) >= 2:
                                        nested = net_state[1]
                                        net_ready = (int(nested[0]) == 1)
                                    elif len(net_state) == 1:
                                        net_ready = int(net_state[0]) == 1
                                elif net_state is not None:
                                    net_ready = int(net_state) == 1
                            except Exception:
                                net_ready = None
                        data_ready = None
                        if dataCall is not None:
                            try:
                                data_state = dataCall.getInfo(1, 0)
                                if isinstance(data_state, (tuple, list)):
                                    # QuecPython EC800M: index 2 contains a
                                    # status tuple whose first field is 1
                                    # when the PDP context is active.
                                    if (len(data_state) > 2 and
                                            isinstance(data_state[2],
                                                      (tuple, list))):
                                        data_ready = (int(data_state[2][0]) == 1)
                                    elif len(data_state) > 0:
                                        data_ready = int(data_state[0]) == 1
                            except Exception:
                                data_ready = None
                        # Use the direct result only when every available
                        # interface returned a recognizable value.  If one
                        # modem API failed or changed shape, fall back to the
                        # compatibility probe instead of reporting a false
                        # "connected" state.
                        direct_known = (
                            (net is None or net_ready is not None) and
                            (dataCall is None or data_ready is not None))
                        if direct_known:
                            if net_ready is None:
                                net_ready = True
                            if data_ready is None:
                                data_ready = True
                            connected = bool(net_ready and data_ready)
                            if stage is None:
                                stage = 3 if connected else 0
                            if state is None:
                                state = 1 if connected else 0
                    if not direct_known:
                        if checkNet is None:
                            raise RuntimeError("网络状态接口不可用")
                        stage, state = checkNet.waitNetworkReady(1)
                        connected = (stage == 3 and state == 1)
                    self.last_error = None
                except Exception as error:
                    self.last_error = error
                self._perf_add(self._perf_wait_ready,
                               _ticks_diff(utime.ticks_ms(), wait_started_ms))
                self._lock_acquire()
                try:
                    changed = (
                        connected != self.network_connected or
                        stage != self.network_stage or
                        state != self.network_state)
                    self.network_connected = connected
                    self.network_stage = stage
                    self.network_state = state
                    if changed:
                        self._bump_revision()
                finally:
                    self._lock_release()

                # 失败或断网时也不要立即紧循环，避免网络异常造成 CPU 占用。
                utime.sleep_ms(NETWORK_RETRY_INTERVAL_MS)
                self._perf_add(self._perf_worker,
                               _ticks_diff(utime.ticks_ms(), worker_started_ms))
        except Exception as error:
            self.last_error = error
        finally:
            self._network_worker_running = False
            self._network_worker_id = None

    def _read_iccid(self):
        if sim is None:
            return None
        try:
            value = sim.getIccid()
            if value is None:
                return None
            if isinstance(value, bytes):
                value = value.decode()
            value = str(value).strip()
            if not value or value in ("-1", "0"):
                return None
            return value
        except Exception:
            return None

    def _read_signal(self):
        if net is None:
            return None
        try:
            value = net.getSignal()
            print("信号值=", value)
            # EC800M返回：
            # ([rssi, bitErrorRate, rscp, ecno],
            #  [rssi, rsrp, rsrq, cqi, sinr])。
            # LTE信号强度使用第二组中的RSRP，也就是索引1。
            if isinstance(value, (tuple, list)):
                if len(value) < 2:
                    return None
                lte_signal = value[1]
                if not isinstance(lte_signal, (tuple, list)):
                    return None
                if len(lte_signal) < 2:
                    return None
                value = lte_signal[1]
            value = int(value)
            # 99、255等非负占位值表示本次没有取得有效RSRP。
            if value in (99, 255) or value < -200 or value >= 0:
                return None
            return value
        except Exception:
            return None

    def _update_signal_cache(self):
        """刷新 SIM 卡号和信号缓存；函数不执行网络等待。"""
        iccid = self._read_iccid()
        signal_dbm = self._read_signal()
        signal_level = classify_signal(signal_dbm)
        self._lock_acquire()
        try:
            changed = (
                iccid != self.iccid or
                signal_dbm != self.signal_dbm or
                signal_level != self.signal_level)
            self.iccid = iccid
            self.signal_dbm = signal_dbm
            self.signal_level = signal_level
            if changed:
                self._bump_revision()
        finally:
            self._lock_release()

    def tick(self, now_ms=None):
        """Return a changed cached snapshot without any hardware access."""
        return self.get_snapshot_if_changed()

    def refresh_now(self, now_ms=None):
        """Request an asynchronous signal refresh and return current cache."""
        self._signal_refresh_requested = True
        return self.get_snapshot()

    def get_snapshot_if_changed(self):
        """返回一次性变化快照，避免主循环重复刷新标签。"""
        # 整数读写在 MicroPython 中是原子的；先无锁判断，避免 LVGL 主循环
        # 每 5ms 都争用锁。真正复制多字段快照时仍然使用锁保证一致性。
        if self._revision == self._reported_revision:
            return None
        self._lock_acquire()
        try:
            if self._revision == self._reported_revision:
                return None
            snapshot = {
                "iccid": self.iccid,
                "signal_dbm": self.signal_dbm,
                "signal_level": self.signal_level,
                "network_connected": self.network_connected,
                "network_stage": self.network_stage,
                "network_state": self.network_state,
            }
            self._reported_revision = self._revision
            return snapshot
        finally:
            self._lock_release()

    def get_snapshot(self):
        """读取当前缓存快照；不触发硬件访问。"""
        self._lock_acquire()
        try:
            return {
                "iccid": self.iccid,
                "signal_dbm": self.signal_dbm,
                "signal_level": self.signal_level,
                "network_connected": self.network_connected,
                "network_stage": self.network_stage,
                "network_state": self.network_state,
            }
        finally:
            self._lock_release()

    def get_sim_iccid(self):
        """立即读取一次 SIM 卡 ICCID；失败返回 None。"""
        value = self._read_iccid()
        self._lock_acquire()
        try:
            if value != self.iccid:
                self.iccid = value
                self._bump_revision()
        finally:
            self._lock_release()
        return value

    def get_signal_info(self):
        """立即读取一次信号，返回 (dBm, 等级)；失败返回 (None, None)。"""
        value = self._read_signal()
        
        level = classify_signal(value)
        self._lock_acquire()
        try:
            if value != self.signal_dbm or level != self.signal_level:
                self.signal_dbm = value
                self.signal_level = level
                self._bump_revision()
        finally:
            self._lock_release()
        return value, level

    def get_network_status(self):
        """返回缓存的 (是否连接, stage, state)，本方法不会阻塞。"""
        self._lock_acquire()
        try:
            return (self.network_connected,
                    self.network_stage, self.network_state)
        finally:
            self._lock_release()

    def stop(self):
        """请求后台线程停止；通常设备全程运行时无需调用。"""
        self._network_worker_running = False


def get_default_monitor(start_worker=True):
    """返回共享监控实例；启动阶段可暂缓创建后台线程。"""
    global _DEFAULT_MONITOR
    if _DEFAULT_MONITOR is None:
        _DEFAULT_MONITOR = NetworkMonitor(start_worker=start_worker)
    elif start_worker:
        _DEFAULT_MONITOR.start_network_worker()
    return _DEFAULT_MONITOR


def get_sim_iccid():
    """公共接口：立即读取 SIM 卡 ICCID，失败返回 None。"""
    return get_default_monitor().get_sim_iccid()


def get_signal_info():
    """公共接口：立即返回 (信号 dBm, 信号等级)。"""
    return get_default_monitor().get_signal_info()


def get_network_status():
    """公共接口：返回缓存的 (是否连接, stage, state)，不会阻塞。"""
    return get_default_monitor().get_network_status()
