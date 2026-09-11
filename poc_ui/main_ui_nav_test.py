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


def _build_preview_groups():
	groups = []
	for index in range(30):
		number = index + 1
		groups.append({
			"id": str(50000 + number),
			"name": "模拟小组{:02d}".format(number),
		})
	return tuple(groups)


def _build_preview_people():
	people = []
	for index in range(50):
		number = index + 1
		people.append({
			"key": "M{:06d}".format(100001 + number),
			"name": "模拟警员{:02d}".format(number),
			"online": True,
			"device_id": "3303000200200000{:04d}".format(591 + index),
			"is_self": False,
		})
	# 当前登录人员保留在源名单中，用于确认单呼页面会正确过滤本人；
	# 页面最终仍显示上面生成的50名可选成员。
	people.append({
		"key": "M100001",
		"name": "潘荣丰",
		"online": True,
		"device_id": "33030002002000000590",
		"is_self": True,
	})
	return tuple(people)


def start_ui_preview():
	mount_external_flash()
	command_queue = UICommandQueue()
	state_store = UIStateStore()
	message_bus = UIMessageBus()
	state_store.update_network(
		True, "ready", "online", "89860403102000000001", -71, "强")
	state_store.update_battery(99, None, False)
	state_store.update_location(
		"潘一", "120.7899", "120.7899", "80.1", "12", "10:44:59", "星期三")
	state_store.update_identity("潘荣丰", "M100001")
	state_store.update_intercom_directory(
		_build_preview_groups(), _build_preview_people())
	state_store.update_group_selection("50001")
	state_store.update_device_versions("1.0.0", "EC800M-P01", "1.0.0")
	state_store.update_upgrade_status("等待检查")
	return main(command_queue=command_queue, state_store=state_store,
				message_bus=message_bus)


if __name__ == "__main__":
	start_ui_preview()
