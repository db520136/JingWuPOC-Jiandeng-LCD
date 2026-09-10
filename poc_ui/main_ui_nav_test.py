# -*- coding: utf-8 -*-
"""纯 UI 实机入口：只验证页面布局与交互，不启动真实业务线程。"""

try:
    from usr.ui_app import main
    from usr.font_port import mount_external_flash
    from usr.ui_commands import UICommandQueue
    from usr.ui_state import UIStateStore
    from usr.ui_messages import UIMessageBus
except ImportError:
    from ui_app import main
    from font_port import mount_external_flash
    from ui_commands import UICommandQueue
    from ui_state import UIStateStore
    from ui_messages import UIMessageBus


def start_ui_preview():
    mount_external_flash()
    command_queue = UICommandQueue()
    state_store = UIStateStore()
    message_bus = UIMessageBus()
    state_store.update_network(True, "ready", "online", None, None, 5)
    state_store.update_battery(99, None, False)
    state_store.update_location(
        "潘一", "120.7899", "120.7899", "80.1", "12", "10:44:59", "星期三")
    state_store.update_identity("潘荣丰", "M100001")
    return main(command_queue=command_queue, state_store=state_store,
                message_bus=message_bus)


if __name__ == "__main__":
    start_ui_preview()
