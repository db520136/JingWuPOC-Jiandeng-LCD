# -*- coding: utf-8 -*-
"""EC800M SIM 卡、网络连接和信号状态监控。

本模块不直接操作 LVGL。网络就绪检查可能阻塞最长 30 秒，因此只在一个
后台线程中调用 ``checkNet.waitNetworkReady(30)``；主线程通过 ``tick()``
读取缓存并负责更新界面。SIM 卡号和信号值读取失败时不会抛出到 UI 主循环。
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
    import checkNet
except Exception:
    checkNet = None


# 信号值每30秒采集一次；进入网络状态页时可通过 refresh_now() 立即采集。
SIGNAL_REFRESH_INTERVAL_MS = 30000
NETWORK_RETRY_INTERVAL_MS = 5000
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
        self._network_worker_running = False
        self._network_worker_id = None
        self._lock = None

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

    def start_network_worker(self):
        """启动唯一网络检测线程；重复调用不会创建多个线程。"""
        if self._network_worker_running:
            return False
        if _thread is None or checkNet is None:
            self.last_error = RuntimeError("网络线程或 checkNet 库不可用")
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
                stage = None
                state = None
                connected = False
                try:
                    wait_ready = getattr(checkNet, "waitNetworkReady")
                    stage, state = wait_ready(30)
                    connected = stage == 3 and state == 1
                    self.last_error = None
                except Exception as error:
                    self.last_error = error
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
        """由 LVGL 主循环调用；开机立即读取，之后每30秒读取一次。

        只有缓存发生变化时才返回快照，否则返回 None。
        """
        if now_ms is None:
            now_ms = utime.ticks_ms()
        if _ticks_diff(now_ms, self._next_signal_ms) >= 0:
            self._update_signal_cache()
            self._next_signal_ms = _ticks_add(
                now_ms, self.signal_interval_ms)

        return self.get_snapshot_if_changed()

    def refresh_now(self, now_ms=None):
        """立即刷新 SIM/信号缓存并返回完整快照。

        网络状态页打开时调用此方法，避免等待下一个30秒周期；刷新后重新
        计算下一次周期，防止页面打开后连续采集两次。
        """
        if now_ms is None:
            now_ms = utime.ticks_ms()
        self._update_signal_cache()
        self._next_signal_ms = _ticks_add(now_ms, self.signal_interval_ms)
        # 标记本次刷新已被消费，避免页面打开后下一轮tick重复返回同一快照。
        snapshot = self.get_snapshot_if_changed()
        if snapshot is None:
            snapshot = self.get_snapshot()
        return snapshot

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
