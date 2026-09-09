# -*- coding: utf-8 -*-
"""事件驱动的低优先级业务 Worker。

电池、网络、肩灯 I2C 和双击寄存器读取都在本线程执行。无事件时线程
阻塞在信号量上；5 秒 Timer 和 GPIO 中断只记录事件并通过统一闸门唤醒。
"""

import _thread
import utime
from machine import Timer

try:
    from usr.ui_commands import (CMD_ACCEL_DOUBLE_CLICK_IRQ,
                                 CMD_CHANGE_VOLUME, CMD_DOUBLE_CLICK,
                                 CMD_REFRESH_GNSS, CMD_REFRESH_NETWORK,
                                 CMD_SET_LAMP_BRIGHTNESS, CMD_SET_LAMP_MODE,
                                 UICommandQueue)
    from usr.ui_messages import (MSG_BATTERY_CHANGED,
                                 MSG_DOUBLE_CLICK as MSG_DOUBLE_CLICK_EVENT,
                                 MSG_ERROR, MSG_LAMP_CHANGED,
                                 MSG_NETWORK_CHANGED, UIMessageBus)
    from usr.ui_state import UIStateStore
except ImportError:
    from ui_commands import (CMD_ACCEL_DOUBLE_CLICK_IRQ,
                             CMD_CHANGE_VOLUME, CMD_DOUBLE_CLICK,
                             CMD_REFRESH_GNSS, CMD_REFRESH_NETWORK,
                             CMD_SET_LAMP_BRIGHTNESS, CMD_SET_LAMP_MODE,
                             UICommandQueue)
    from ui_messages import (MSG_BATTERY_CHANGED,
                             MSG_DOUBLE_CLICK as MSG_DOUBLE_CLICK_EVENT,
                             MSG_ERROR, MSG_LAMP_CHANGED,
                             MSG_NETWORK_CHANGED, UIMessageBus)
    from ui_state import UIStateStore


PERIODIC_INTERVAL_MS = 5000
BATTERY_BOOT_SAMPLE_INTERVAL_MS = 20
BATTERY_BOOT_MAX_ATTEMPTS = 60


class LowPriorityWorker:
    """单一后台 Worker；所有低优先级硬件调用都在该线程执行。"""

    def __init__(self, command_queue=None, state_store=None,
                 message_bus=None, battery=None, network=None, lamp=None,
                 motion_sensor=None):
        self.commands = (command_queue if command_queue is not None else
                         UICommandQueue())
        self.state = (state_store if state_store is not None else
                      UIStateStore())
        self.messages = (message_bus if message_bus is not None else
                         UIMessageBus())
        self.battery = battery
        self.network = network
        self.lamp = lamp
        self.motion_sensor = motion_sensor
        self.running = False
        self.thread_id = None
        self._timer = None
        self._last_double_click_ms = None

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
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
        self.commands.signal_stop()

    def notify_double_click(self):
        """GPIO ISR入口：只记录事件并尝试释放统一唤醒闸门。"""
        return self.commands.signal_double_click_isr()

    def _on_periodic_timer(self, timer):
        """Timer回调不得打印、加锁或访问硬件。"""
        self.commands.signal_periodic_isr()

    def _start_periodic_timer(self):
        self._timer = Timer(Timer.Timer2)
        self._timer.start(period=PERIODIC_INTERVAL_MS,
                          mode=self._timer.PERIODIC,
                          callback=self._on_periodic_timer)

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
                changed = self.state.update_battery(
                    self.battery.get_percent(),
                    self.battery.get_voltage_mv(),
                    self.battery.charging_state)
                if changed:
                    self.messages.publish(MSG_BATTERY_CHANGED)
        except Exception as error:
            self._publish_error("battery", error)

    def _bootstrap_battery(self):
        if self.battery is None:
            return
        attempts = 0
        while (self.running and not self.battery.is_ready() and
               attempts < BATTERY_BOOT_MAX_ATTEMPTS):
            attempts += 1
            self._refresh_battery()
            if not self.battery.is_ready():
                utime.sleep_ms(BATTERY_BOOT_SAMPLE_INTERVAL_MS)
    def _refresh_network(self, force_signal=False):
        if self.network is None:
            return
        try:
            snapshot = self.network.refresh_once(force_signal=force_signal)
            changed = self.state.update_network(
                snapshot["network_connected"], snapshot["network_stage"],
                snapshot["network_state"], snapshot["iccid"],
                snapshot["signal_dbm"], snapshot["signal_level"])
            if changed:
                self.messages.publish(MSG_NETWORK_CHANGED)
        except Exception as error:
            self._publish_error("network", error)

    def _refresh_lamp(self):
        if self.lamp is None:
            return
        try:
            snapshot = self.lamp.get_status_snapshot()
            brightness = self.lamp.get_brightness_percent()
            changed = self.state.update_lamp(
                snapshot["mode"], brightness,
                snapshot["fall_alarm_active"])
            if changed:
                self.messages.publish(MSG_LAMP_CHANGED)
        except Exception as error:
            self._publish_error("lamp", error)

    def _execute(self, item):
        command_type = item["type"]
        payload = item["payload"]
        try:
            if command_type == CMD_SET_LAMP_MODE:
                if self.lamp is not None:
                    self.lamp.set_mode(payload)
                    self._refresh_lamp()
            elif command_type == CMD_SET_LAMP_BRIGHTNESS:
                if self.lamp is not None:
                    self.lamp.set_brightness_percent(payload)
                    self._refresh_lamp()
            elif command_type == CMD_CHANGE_VOLUME:
                pass
            elif command_type == CMD_REFRESH_NETWORK:
                self._refresh_network(force_signal=True)
            elif command_type == CMD_REFRESH_GNSS:
                pass
            elif command_type == CMD_DOUBLE_CLICK:
                if self.lamp is not None:
                    self.lamp.cycle_mode()
                    self._refresh_lamp()
                    self.messages.publish(MSG_DOUBLE_CLICK_EVENT)
            elif command_type == CMD_ACCEL_DOUBLE_CLICK_IRQ:
                self._handle_accel_double_click_irq()
        except Exception as error:
            self._publish_error(command_type, error)

    def _handle_accel_double_click_irq(self):
        if self.motion_sensor is None:
            return
        try:
            source = self.motion_sensor.read_click_source()
        except Exception as error:
            self._publish_error("double_click", error)
            return
        if (int(source) & 0x0F) != 2:
            return
        now_ms = utime.ticks_ms()
        if (self._last_double_click_ms is not None and
                utime.ticks_diff(now_ms, self._last_double_click_ms) < 250):
            return
        self._last_double_click_ms = now_ms
        if self.lamp is not None:
            self.lamp.cycle_mode()
            self._refresh_lamp()
        self.messages.publish(MSG_DOUBLE_CLICK_EVENT)

    def _run(self):
        try:
            self._bootstrap_battery()
            if not self.running:
                return
            self._refresh_network(force_signal=True)
            self._refresh_lamp()
            self._start_periodic_timer()
            while self.running:
                self.commands.wait()
                events = self.commands.take_events()
                (stop_pending, double_click_pending, periodic_pending,
                 periodic_deferred, double_click_count, periodic_count,
                 items) = events
                if stop_pending or not self.running:
                    break
                if double_click_pending:
                    self._handle_accel_double_click_irq()
                if periodic_pending:
                    self._refresh_battery()
                    self._refresh_network(force_signal=True)
                for item in items:
                    self._execute(item)
        except Exception as error:
            self._publish_error("worker", error)
        finally:
            if self._timer is not None:
                try:
                    self._timer.stop()
                except Exception:
                    pass
                self._timer = None
            self.running = False
            self.thread_id = None
