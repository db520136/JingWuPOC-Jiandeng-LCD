# -*- coding: utf-8 -*-
"""EC800M 网络、SIM 和信号状态读取服务。

本模块只负责一次读取和状态缓存，不创建线程、不 sleep，也不访问 LVGL。
调度由 ``low_priority_worker.py`` 负责；这样网络监控不会再额外创建一个
与统一后台 Worker 重复的线程。``start_network_worker`` 保留为旧入口的
兼容空操作，绝不会创建线程。
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


SIGNAL_REFRESH_INTERVAL_MS = 30000
NETWORK_RETRY_INTERVAL_MS = 5000
NETWORK_DIRECT_STATUS = True
NETWORK_SIGNAL_DEBUG_ENABLED = False
_DEFAULT_MONITOR = None

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
    """无内部线程的网络状态缓存；由外部 Worker 显式调用刷新。"""

    def __init__(self, signal_interval_ms=SIGNAL_REFRESH_INTERVAL_MS,
                 start_worker=False):
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
        self._ui_interaction_active = False
        self._lock = None
        self._last_refresh_elapsed_ms = 0
        self._last_status_elapsed_ms = 0
        self._last_signal_elapsed_ms = 0
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

    def get_thread_perf_snapshot(self):
        """保留诊断接口；网络监控已无独立线程。"""
        return {
            "worker": (0, 0, 0, 0, 0, 0),
            "wait_ready": (self._last_status_elapsed_ms, 0, 0, 0, 0, 0),
            "signal": (self._last_signal_elapsed_ms, 0, 0, 0, 0, 0),
        }

    def set_ui_interaction_active(self, active):
        """兼容旧调用；新 Worker 在调用前自行决定是否推迟刷新。"""
        self._ui_interaction_active = bool(active)

    def start_network_worker(self):
        """兼容旧入口，不创建线程；实际调度由 LowPriorityWorker 完成。"""
        return True

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
            if NETWORK_SIGNAL_DEBUG_ENABLED:
                print("信号值=", value)
            if isinstance(value, (tuple, list)):
                if len(value) < 2 or not isinstance(value[1], (tuple, list)):
                    return None
                if len(value[1]) < 2:
                    return None
                value = value[1][1]
            value = int(value)
            if value in (99, 255) or value < -200 or value >= 0:
                return None
            return value
        except Exception:
            return None

    def _read_network_status(self):
        """读取网络就绪状态；只使用已确认的 EC800M 接口。"""
        started_ms = utime.ticks_ms()
        stage = None
        state = None
        connected = False
        error = None
        try:
            direct_known = False
            net_ready = None
            data_ready = None
            if NETWORK_DIRECT_STATUS and net is not None:
                net_state = net.getState()
                if isinstance(net_state, (tuple, list)):
                    if (len(net_state) >= 2 and
                            not isinstance(net_state[1], (tuple, list))):
                        stage = int(net_state[0])
                        state = int(net_state[1])
                        net_ready = (stage == 3 and state == 1)
                    elif len(net_state) >= 2:
                        nested = net_state[1]
                        net_ready = int(nested[0]) == 1
                    elif len(net_state) == 1:
                        net_ready = int(net_state[0]) == 1
                elif net_state is not None:
                    net_ready = int(net_state) == 1
            if NETWORK_DIRECT_STATUS and dataCall is not None:
                data_state = dataCall.getInfo(1, 0)
                if isinstance(data_state, (tuple, list)):
                    if (len(data_state) > 2 and
                            isinstance(data_state[2], (tuple, list))):
                        data_ready = int(data_state[2][0]) == 1
                    elif len(data_state) > 0:
                        data_ready = int(data_state[0]) == 1
            direct_available = (net is not None or dataCall is not None)
            direct_known = (direct_available and
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
            else:
                if checkNet is None:
                    raise RuntimeError("网络状态接口不可用")
                stage, state = checkNet.waitNetworkReady(1)
                connected = (stage == 3 and state == 1)
            self.last_error = None
        except Exception as exc:
            error = exc
            self.last_error = exc
        self._last_status_elapsed_ms = _ticks_diff(
            utime.ticks_ms(), started_ms)
        return connected, stage, state, error

    def refresh_once(self, now_ms=None, force_signal=True):
        """由外部 Worker 调用一次，刷新网络、SIM、信号并返回最新快照。"""
        if now_ms is None:
            now_ms = utime.ticks_ms()
        signal_due = (force_signal or self._signal_refresh_requested or
                      _ticks_diff(now_ms, self._next_signal_ms) >= 0)
        started_ms = utime.ticks_ms()
        if signal_due:
            signal_started_ms = utime.ticks_ms()
            iccid = self._read_iccid()
            signal_dbm = self._read_signal()
            self._last_signal_elapsed_ms = _ticks_diff(
                utime.ticks_ms(), signal_started_ms)
            self._signal_refresh_requested = False
            self._next_signal_ms = _ticks_add(
                utime.ticks_ms(), self.signal_interval_ms)
        else:
            iccid = self.iccid
            signal_dbm = self.signal_dbm
        signal_level = classify_signal(signal_dbm)
        connected, stage, state, error = self._read_network_status()
        self._lock_acquire()
        try:
            changed = (
                iccid != self.iccid or signal_dbm != self.signal_dbm or
                signal_level != self.signal_level or
                connected != self.network_connected or
                stage != self.network_stage or state != self.network_state)
            self.iccid = iccid
            self.signal_dbm = signal_dbm
            self.signal_level = signal_level
            self.network_connected = connected
            self.network_stage = stage
            self.network_state = state
            if changed:
                self._bump_revision()
            snapshot = self._snapshot_locked()
        finally:
            self._lock_release()
        self._last_refresh_elapsed_ms = _ticks_diff(
            utime.ticks_ms(), started_ms)
        return snapshot

    def tick(self, now_ms=None):
        """只读取缓存的变化快照，不访问硬件。"""
        return self.get_snapshot_if_changed()

    def refresh_now(self, now_ms=None):
        """同步刷新一次，供旧入口兼容；新 UI 应投递 Worker 命令。"""
        return self.refresh_once(now_ms=now_ms, force_signal=True)

    def _snapshot_locked(self):
        return {
            "iccid": self.iccid,
            "signal_dbm": self.signal_dbm,
            "signal_level": self.signal_level,
            "network_connected": self.network_connected,
            "network_stage": self.network_stage,
            "network_state": self.network_state,
        }

    def get_snapshot_if_changed(self):
        if self._revision == self._reported_revision:
            return None
        self._lock_acquire()
        try:
            if self._revision == self._reported_revision:
                return None
            snapshot = self._snapshot_locked()
            self._reported_revision = self._revision
            return snapshot
        finally:
            self._lock_release()

    def get_snapshot(self):
        self._lock_acquire()
        try:
            return self._snapshot_locked()
        finally:
            self._lock_release()

    def get_sim_iccid(self):
        """读取并缓存 ICCID；不启动线程。"""
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
        """读取并缓存信号信息；不启动线程。"""
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
        """返回缓存的网络状态，不访问硬件。"""
        self._lock_acquire()
        try:
            return (self.network_connected, self.network_stage,
                    self.network_state)
        finally:
            self._lock_release()

    def stop(self):
        """兼容旧入口；无线程可停止。"""
        return None


def get_default_monitor(start_worker=False):
    """返回共享监控实例；参数仅为兼容旧调用，永不创建线程。"""
    global _DEFAULT_MONITOR
    if _DEFAULT_MONITOR is None:
        _DEFAULT_MONITOR = NetworkMonitor(start_worker=False)
    return _DEFAULT_MONITOR


def get_sim_iccid():
    return get_default_monitor(False).get_sim_iccid()


def get_signal_info():
    return get_default_monitor(False).get_signal_info()


def get_network_status():
    return get_default_monitor(False).get_network_status()
