# -*- coding: utf-8 -*-
"""通信线程的统一命令定义。

本文件只定义接口，不创建线程、不创建 socket，也不执行任何硬件操作。
发送线程后续只接收这里定义的命令；RTP 音频帧属于实时流，不逐帧进入
通用命令队列，使用 RTP 发送状态和音频缓冲处理。
"""


# 命令优先级：数值越小，优先级越高。
PRIORITY_P0 = 0  # 实时业务：必须尽快执行，不允许普通队列静默丢弃。
PRIORITY_P1 = 1  # 重要控制事务：需要应答和超时重发。
PRIORITY_P2 = 2  # 低频周期业务：允许延后或合并。
PRIORITY_P3 = 3  # 非实时扩展业务：当前不接入。


# TCP主动事务重试参数。首次发送后等待500ms；随后最多重发3次，
# 每次等待增加500ms，超过2000ms后保持2000ms不再增长。
TCP_MAX_RETRIES = 3
TCP_INITIAL_WAIT_MS = 500
TCP_RETRY_STEP_MS = 500
TCP_MAX_WAIT_MS = 2000

# 0x04第一阶段被服务器受理后不再重发原请求，只等待对端最终结果。
SINGLE_CALL_FINAL_RESPONSE_TIMEOUT_MS = 10000

# 0x09跨连接补偿最多执行3轮；每轮内部仍采用上面的同Socket重试规则。
RELEASE_RECOVERY_MAX_ATTEMPTS = 3


# ============================ P0 实时命令 ============================

CMD_PTT_DOWN = "ptt_down"

CMD_PTT_UP = "ptt_up"

CMD_RTP_ACTIVATE = "rtp_activate"

CMD_RTP_ACTIVATE_CANCEL = "rtp_activate_cancel"

CMD_RTP_AUDIO_START = "rtp_audio_start"

CMD_RTP_AUDIO_STOP = "rtp_audio_stop"

CMD_RELEASE_FLOOR = "release_floor"

CMD_RTP_AUDIO_FRAME = "rtp_audio_frame"

CMD_TCP_PRIORITY_RESPONSE = "tcp_priority_response"

CMD_STOP_ALL = "stop_all"


# ============================ P1 控制事务 ============================

CMD_LOGIN = "login"

CMD_FORCE_LOGIN = "force_login"

CMD_LOGOUT = "logout"

CMD_JOIN_GROUP = "join_group"

CMD_LEAVE_GROUP = "leave_group"

CMD_SINGLE_CALL = "single_call"

CMD_ACCEPT_SINGLE_CALL = "accept_single_call"

CMD_REJECT_SINGLE_CALL = "reject_single_call"

CMD_DISSOLVE_SINGLE_CALL = "dissolve_single_call"

CMD_REQUEST_FLOOR = "request_floor"

CMD_FORCE_RELEASE_FLOOR = "force_release_floor"


# ============================ P2 周期命令 ============================

CMD_TCP_HEARTBEAT = "tcp_heartbeat"

CMD_GNSS_UPLOAD = "gnss_upload"

CMD_STATUS_SYNC = "status_sync"


# 普通有界命令队列允许投递的命令。RTP_AUDIO_FRAME 明确排除，避免把
# 高频音频包堆积在 Python 队列中造成延迟和内存增长。
BOUNDED_QUEUE_COMMANDS = (
	CMD_PTT_DOWN,
	CMD_PTT_UP,
	CMD_RTP_ACTIVATE,
	CMD_RTP_ACTIVATE_CANCEL,
	CMD_RTP_AUDIO_START,
	CMD_RTP_AUDIO_STOP,
	CMD_TCP_PRIORITY_RESPONSE,
	CMD_STOP_ALL,
	CMD_LOGIN,
	CMD_FORCE_LOGIN,
	CMD_LOGOUT,
	CMD_JOIN_GROUP,
	CMD_LEAVE_GROUP,
	CMD_SINGLE_CALL,
	CMD_ACCEPT_SINGLE_CALL,
	CMD_REJECT_SINGLE_CALL,
	CMD_DISSOLVE_SINGLE_CALL,
	CMD_REQUEST_FLOOR,
	CMD_RELEASE_FLOOR,
	CMD_FORCE_RELEASE_FLOOR,
	CMD_TCP_HEARTBEAT,
	CMD_GNSS_UPLOAD,
	CMD_STATUS_SYNC,
)


# 同类命令允许合并；P0/P1 事务本身仍由独立状态保存，不能靠合并丢失。
COALESCIBLE_COMMANDS = (
	CMD_RTP_ACTIVATE,
	CMD_TCP_HEARTBEAT,
	CMD_GNSS_UPLOAD,
	CMD_STATUS_SYNC,
)


# 不允许普通队列静默丢弃的命令。队列满时应返回失败，或由独立状态槽保留。
NON_DROPPABLE_COMMANDS = (
	CMD_PTT_DOWN,
	CMD_PTT_UP,
	CMD_RTP_ACTIVATE,
	CMD_RTP_ACTIVATE_CANCEL,
	CMD_RTP_AUDIO_START,
	CMD_RTP_AUDIO_STOP,
	CMD_TCP_PRIORITY_RESPONSE,
	CMD_STOP_ALL,
	CMD_LOGIN,
	CMD_FORCE_LOGIN,
	CMD_LOGOUT,
	CMD_JOIN_GROUP,
	CMD_LEAVE_GROUP,
	CMD_SINGLE_CALL,
	CMD_ACCEPT_SINGLE_CALL,
	CMD_REJECT_SINGLE_CALL,
	CMD_DISSOLVE_SINGLE_CALL,
	CMD_REQUEST_FLOOR,
	CMD_RELEASE_FLOOR,
	CMD_FORCE_RELEASE_FLOOR,
)


def command_priority(command_type):
	"""返回命令优先级；未知命令默认按最低优先级处理。"""
	if command_type in (
			CMD_PTT_DOWN, CMD_PTT_UP, CMD_RTP_ACTIVATE,
			CMD_RTP_ACTIVATE_CANCEL,
			CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP,
			CMD_RTP_AUDIO_FRAME, CMD_RELEASE_FLOOR,
			CMD_TCP_PRIORITY_RESPONSE, CMD_STOP_ALL):
		return PRIORITY_P0
	if command_type in (
			CMD_LOGIN, CMD_FORCE_LOGIN, CMD_LOGOUT, CMD_JOIN_GROUP,
			CMD_LEAVE_GROUP, CMD_SINGLE_CALL, CMD_ACCEPT_SINGLE_CALL,
			CMD_REJECT_SINGLE_CALL, CMD_DISSOLVE_SINGLE_CALL,
			CMD_REQUEST_FLOOR, CMD_FORCE_RELEASE_FLOOR):
		return PRIORITY_P1
	if command_type in (CMD_TCP_HEARTBEAT, CMD_GNSS_UPLOAD, CMD_STATUS_SYNC):
		return PRIORITY_P2
	return PRIORITY_P3
