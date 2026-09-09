# -*- coding: utf-8 -*-
"""新 UI 与第一批低优先级业务的验证入口。

当前接入电池、网络、肩灯和双击；TCP/RTP 等通信业务暂不启动。
"""

try:
    from usr.ui_app import main
    from usr.battery_monitor import BatteryMonitor
    from usr.network_monitor import get_default_monitor
    from usr.low_priority_worker import LowPriorityWorker
    from usr.et6312b import ShoulderLampController
    from usr.sc7a20h import SC7A20H, SC7A20HDoubleClickAdapter
    from usr.font_port import mount_external_flash
    from usr.ui_commands import UICommandQueue
    from usr.ui_state import UIStateStore
    from usr.ui_messages import UIMessageBus
except ImportError:
    from ui_app import main
    from battery_monitor import BatteryMonitor
    from network_monitor import get_default_monitor
    from low_priority_worker import LowPriorityWorker
    from et6312b import ShoulderLampController
    from sc7a20h import SC7A20H, SC7A20HDoubleClickAdapter
    from font_port import mount_external_flash
    from ui_commands import UICommandQueue
    from ui_state import UIStateStore
    from ui_messages import UIMessageBus


def start_business_ui():
    """显式组装依赖，保持硬件访问集中在低优先级 Worker。"""
    mount_external_flash()
    command_queue = UICommandQueue()
    state_store = UIStateStore()
    message_bus = UIMessageBus()
    battery = BatteryMonitor()
    network = get_default_monitor(start_worker=False)
    lamp = ShoulderLampController()
    motion_sensor = SC7A20H()
    worker = LowPriorityWorker(
        command_queue=command_queue,
        state_store=state_store,
        message_bus=message_bus,
        battery=battery,
        network=network,
        lamp=lamp,
        motion_sensor=motion_sensor,
    )
    motion = SC7A20HDoubleClickAdapter(
        sensor=motion_sensor,
        on_interrupt=worker.notify_double_click,
    )
    return main(
        battery=battery,
        network=network,
        lamp=lamp,
        low_priority_worker=worker,
        sensor_service=motion,
        command_queue=command_queue,
        state_store=state_store,
        message_bus=message_bus,
    )


if __name__ == "__main__":
    start_business_ui()
