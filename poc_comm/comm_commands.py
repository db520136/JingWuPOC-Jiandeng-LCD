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


# ============================ P0 实时命令 ============================

CMD_PTT_DOWN = "ptt_down"
"""P0：物理 PTT 按下，开始请求麦权。"""

CMD_PTT_UP = "ptt_up"
"""P0：物理 PTT 松开，立即停止本地 RTP 音频发送并释放麦权。"""

CMD_RTP_ACTIVATE = "rtp_activate"
"""P0：发送一次 RTP 激活包，建立或刷新 UDP 映射。"""

CMD_RTP_AUDIO_START = "rtp_audio_start"
"""P0：开始实时采集并发送 RTP 音频流；不是单个音频包。"""

CMD_RTP_AUDIO_STOP = "rtp_audio_stop"
"""P0：停止实时采集和 RTP 音频发送。"""

CMD_RTP_AUDIO_FRAME = "rtp_audio_frame"
"""P0：单个 RTP 音频帧接口定义；不进入通用有界命令队列。"""

CMD_TCP_PRIORITY_RESPONSE = "tcp_priority_response"
"""P0：发送服务器协议要求立即回复的 TCP 应答帧。"""

CMD_STOP_ALL = "stop_all"
"""P0：停止通信发送业务和相关实时状态，拥有最高停止优先级。"""


# ============================ P1 控制事务 ============================

CMD_LOGIN = "login"
"""P1：发送登录请求并等待登录应答。"""

CMD_FORCE_LOGIN = "force_login"
"""P1：收到服务器要求后发送强制登录请求。"""

CMD_LOGOUT = "logout"
"""P1：执行退出登录清理并发送退出登录请求。"""

CMD_JOIN_GROUP = "join_group"
"""P1：加入指定群组。"""

CMD_LEAVE_GROUP = "leave_group"
"""P1：离开当前群组。"""

CMD_SINGLE_CALL = "single_call"
"""P1：发起指定目标的单呼。"""

CMD_ACCEPT_SINGLE_CALL = "accept_single_call"
"""P1：接受服务器发来的单呼。"""

CMD_REJECT_SINGLE_CALL = "reject_single_call"
"""P1：拒绝服务器发来的单呼。"""

CMD_DISSOLVE_SINGLE_CALL = "dissolve_single_call"
"""P1：解散当前单呼。"""

CMD_REQUEST_FLOOR = "request_floor"
"""P1：请求麦权；PTT 按下后由发送线程发起。"""

CMD_RELEASE_FLOOR = "release_floor"
"""P1：释放麦权；PTT 松开后由发送线程发起。"""

CMD_FORCE_RELEASE_FLOOR = "force_release_floor"
"""P1：发送强制释放麦权请求。"""


# ============================ P2 周期命令 ============================

CMD_TCP_HEARTBEAT = "tcp_heartbeat"
"""P2：TCP 空闲心跳；同一时刻只允许存在一个在途事务。"""

CMD_GNSS_UPLOAD = "gnss_upload"
"""P2：上传一次 GNSS 定位数据；同类待发送任务可以合并。"""

CMD_STATUS_SYNC = "status_sync"
"""P2：普通状态同步；允许延后和合并。"""


# ============================ P3 暂不接入命令 ============================

CMD_HTTP_REQUEST = "http_request"
"""P3：HTTP 请求；当前通信重构阶段不启动。"""

CMD_MAP_REQUEST = "map_request"
"""P3：地图请求；当前暂不接入。"""

CMD_MQTT_PUBLISH = "mqtt_publish"
"""P3：MQTT 发布；当前暂不接入。"""

CMD_OTA_REQUEST = "ota_request"
"""P3：OTA 请求；当前暂不接入。"""


# 普通有界命令队列允许投递的命令。RTP_AUDIO_FRAME 明确排除，避免把
# 高频音频包堆积在 Python 队列中造成延迟和内存增长。
BOUNDED_QUEUE_COMMANDS = (
    CMD_PTT_DOWN,
    CMD_PTT_UP,
    CMD_RTP_ACTIVATE,
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
            CMD_RTP_AUDIO_START, CMD_RTP_AUDIO_STOP,
            CMD_RTP_AUDIO_FRAME, CMD_TCP_PRIORITY_RESPONSE, CMD_STOP_ALL):
        return PRIORITY_P0
    if command_type in (
            CMD_LOGIN, CMD_FORCE_LOGIN, CMD_LOGOUT, CMD_JOIN_GROUP,
            CMD_LEAVE_GROUP, CMD_SINGLE_CALL, CMD_ACCEPT_SINGLE_CALL,
            CMD_REJECT_SINGLE_CALL, CMD_DISSOLVE_SINGLE_CALL,
            CMD_REQUEST_FLOOR, CMD_RELEASE_FLOOR,
            CMD_FORCE_RELEASE_FLOOR):
        return PRIORITY_P1
    if command_type in (CMD_TCP_HEARTBEAT, CMD_GNSS_UPLOAD, CMD_STATUS_SYNC):
        return PRIORITY_P2
    return PRIORITY_P3


def is_coalescible(command_type):
    """返回同类命令是否可以只保留一个待处理实例。"""
    return command_type in COALESCIBLE_COMMANDS


def is_non_droppable(command_type):
    """返回队列满时是否禁止静默丢弃该命令。"""
    return command_type in NON_DROPPABLE_COMMANDS
