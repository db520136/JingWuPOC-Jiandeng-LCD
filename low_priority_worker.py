# -*- coding: utf-8 -*-
"""低优先级业务 Worker。

本 Worker 只处理非实时业务：电池、网络/信号、肩灯 I2C 和双击事件。
没有命令或周期任务时永久阻塞在 EC800M 信号量上；硬件对象由启动入口
显式注入，避免 Worker 内部探测固件接口或创建隐藏线程。
"""

import _thread
import utime

try:
    from usr.ui_commands import (CMD_CHANGE_VOLUME, CMD_DOUBLE_CLICK,
                                 CMD_REFRESH_GNSS, CMD_REFRESH_NETWORK,
                                 CMD_SET_LAMP_BRIGHTNESS, CMD_SET_LAMP_MODE,
                                 UICommandQueue)
    from usr.ui_messages import (MSG_BATTERY_CHANGED,
                                 MSG_DOUBLE_CLICK as MSG_DOUBLE_CLICK_EVENT,
                                 MSG_ERROR, MSG_LAMP_CHANGED,
                                 MSG_NETWORK_CHANGED, UIMessageBus)
    from usr.ui_state import UIStateStore
except ImportError:
    from ui_commands import (CMD_CHANGE_VOLUME, CMD_DOUBLE_CLICK,
                             CMD_REFRESH_GNSS, CMD_REFRESH_NETWORK,
                             CMD_SET_LAMP_BRIGHTNESS, CMD_SET_LAMP_MODE,
                             UICommandQueue)
    from ui_messages import (MSG_BATTERY_CHANGED,
                             MSG_DOUBLE_CLICK as MSG_DOUBLE_CLICK_EVENT,
                             MSG_ERROR, MSG_LAMP_CHANGED,
                             MSG_NETWORK_CHANGED, UIMessageBus)
    from ui_state import UIStateStore


BATTERY_INTERVAL_MS = 1000
NETWORK_INTERVAL_MS = 30000


class LowPriorityWorker:
    """单一后台 Worker；所有硬件调用都在该线程执行。"""

    def __init__(self, command_queue=None, state_store=None,
                 message_bus=None, battery=None, network=None, lamp=None):
        self.commands = (command_queue if command_queue is not None else
                         UICommandQueue())
        self.state = (state_store if state_store is not None else
                      UIStateStore())
        self.messages = (message_bus if message_bus is not None else
                         UIMessageBus())
        self.battery = battery
        self.network = network
        self.lamp = lamp
        self.running = False
        self.thread_id = None
        self._periodic_lock = _thread.allocate_lock()
        self._periodic_wake_pending = False
        self._battery_due = True
        self._network_due = True
        self._gnss_due = False
        self._next_battery_ms = utime.ticks_ms()
        self._next_network_ms = utime.ticks_ms()

    def start(self):
        if self.running:
            return False
        self.running = True
        try:
            self.thread_id = _thread.start_new_thread(self._run, ())
            return True
        except Exception:
            self.running = False
            self.thread_id = None
            raise

    def stop(self):
        self.running = False
        self._wake()

    def request_periodic(self, now_ms=None):
        """由 UI 主循环低频调用，仅设置到期标志并唤醒 Worker。"""
        if now_ms is None:
            now_ms = utime.ticks_ms()
        should_wake = False
        self._periodic_lock.acquire()
        try:
            if utime.ticks_diff(now_ms, self._next_battery_ms) >= 0:
                self._battery_due = True
                should_wake = True
                self._next_battery_ms = utime.ticks_add(
                    now_ms, BATTERY_INTERVAL_MS)
            if utime.ticks_diff(now_ms, self._next_network_ms) >= 0:
                self._network_due = True
                should_wake = True
                self._next_network_ms = utime.ticks_add(
                    now_ms, NETWORK_INTERVAL_MS)
        finally:
            self._periodic_lock.release()
        if should_wake:
            self._wake()

    def notify_double_click(self):
        """供 SC7A20H 中断适配器调用，不在中断中访问 I2C。"""
        return self.commands.put(CMD_DOUBLE_CLICK, None)

    def _wake(self):
        self._periodic_lock.acquire()
        try:
            if self._periodic_wake_pending:
                return
            self._periodic_wake_pending = True
        finally:
            self._periodic_lock.release()
        self.commands.wake()

    def _take_due(self):
        self._periodic_lock.acquire()
        try:
            battery_due = self._battery_due
            network_due = self._network_due
            gnss_due = self._gnss_due
            self._battery_due = False
            self._network_due = False
            self._gnss_due = False
            self._periodic_wake_pending = False
            return battery_due, network_due, gnss_due
        finally:
            self._periodic_lock.release()

    def _publish_error(self, domain, error):
        text = str(error)
        self.state.set_error(domain, text)
        self.messages.publish(MSG_ERROR, {"domain": domain, "text": text}, 1)

    def _refresh_battery(self):
        if self.battery is None:
            return
        try:
            percent = self.battery.tick()
            if percent is not None:
                self.state.update_battery(
                    self.battery.get_percent(),
                    self.battery.get_voltage_mv(),
                    self.battery.charging_state)
                self.messages.publish(MSG_BATTERY_CHANGED)
        except Exception as error:
            self._publish_error("battery", error)

    def _refresh_network(self):
        if self.network is None:
            return
        try:
            signal_dbm, signal_level = self.network.get_signal_info()
            connected, stage, state = self.network.get_network_status()
            iccid = self.network.get_sim_iccid()
            self.state.update_network(
                connected, stage, state, iccid, signal_dbm, signal_level)
            self.messages.publish(MSG_NETWORK_CHANGED)
        except Exception as error:
            self._publish_error("network", error)

    def _refresh_lamp(self):
        if self.lamp is None:
            return
        try:
            snapshot = self.lamp.get_status_snapshot()
            brightness = self.lamp.get_brightness_percent()
            self.state.update_lamp(
                snapshot["mode"], brightness,
                snapshot["fall_alarm_active"])
            self.messages.publish(MSG_LAMP_CHANGED)
        except Exception as error:
            self._publish_error("lamp", error)

    def _execute(self, item):
        command_type = item["type"]
        payload = item["payload"]
        try:
            if command_type == CMD_SET_LAMP_MODE:
                if self.lamp is None:
                    return
                self.lamp.set_mode(payload)
                self._refresh_lamp()
            elif command_type == CMD_SET_LAMP_BRIGHTNESS:
                if self.lamp is None:
                    return
                self.lamp.set_brightness_percent(payload)
                self._refresh_lamp()
            elif command_type == CMD_CHANGE_VOLUME:
                # 音量属于实时通信服务；低优先级 Worker 不执行该命令。
                return
            elif command_type == CMD_REFRESH_NETWORK:
                self._refresh_network()
            elif command_type == CMD_REFRESH_GNSS:
                self._gnss_due = True
            elif command_type == CMD_DOUBLE_CLICK:
                if self.lamp is None:
                    return
                self.lamp.cycle_mode()
                self._refresh_lamp()
                self.messages.publish(MSG_DOUBLE_CLICK_EVENT)
        except Exception as error:
            self._publish_error(command_type, error)

    def _run(self):
        while self.running:
            battery_due, network_due, gnss_due = self._take_due()
            if battery_due:
                self._refresh_battery()
            if network_due:
                self._refresh_network()
            if gnss_due:
                # GNSS 读取/上传接口待 POC 后台任务接入，此处只保留唤醒位。
                pass
            items = self.commands.drain_nowait()
            for item in items:
                self._execute(item)
            if not self.running:
                break
            self.commands.wait()
            self._periodic_lock.acquire()
            try:
                self._periodic_wake_pending = False
            finally:
                self._periodic_lock.release()
            items = self.commands.drain_nowait()
            for item in items:
                self._execute(item)
        self.thread_id = None
