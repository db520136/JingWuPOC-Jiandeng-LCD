# -*- coding: utf-8 -*-
"""警务机横屏 LCD 与触摸界面。

硬件目标：ST7789V（296x240 横屏）+ CST816D。
本文件包含显示初始化、触摸初始化、0~3 级页面和页面返回逻辑，
main.py 只负责创建 JingWuUI 对象并启动事件循环。
"""

import gc
import utime

from machine import LCD
import lvgl as lv
from tp import cst816
from misc import PWM_V2

try:
    # 肩灯驱动与 UI 同在 usr 目录，导入时不会自动执行硬件测试。
    from usr.et6312b import ShoulderLampController
except Exception:
    try:
        # 兼容部分固件将 usr 目录直接加入模块搜索路径的情况。
        from et6312b import ShoulderLampController
    except Exception:
        # 非 EC800M 环境没有 machine.I2C 时，仍允许静态检查和 UI 调试启动。
        ShoulderLampController = None

try:
    # 加速度服务线程不接触 LVGL，只通过肩灯控制器传递业务事件。
    from usr.sc7a20h import SC7A20HService
except Exception:
    try:
        from sc7a20h import SC7A20HService
    except Exception:
        SC7A20HService = None

try:
    # 电池监控模块独立于界面，便于后续接入充电状态脚或其他电源业务。
    from usr.battery_monitor import BatteryMonitor
except Exception:
    try:
        # 兼容 usr 目录已加入模块搜索路径的固件环境。
        from battery_monitor import BatteryMonitor
    except Exception:
        # 非 EC800M 环境允许界面继续启动，电量保持为未知状态。
        BatteryMonitor = None

try:
    # 网络等待在独立模块的后台线程执行，LVGL 主线程只读取缓存。
    from usr.network_monitor import get_default_monitor
except Exception:
    try:
        from network_monitor import get_default_monitor
    except Exception:
        get_default_monitor = None

# 横屏逻辑分辨率，坐标原点在屏幕左上角。
LCD_WIDTH = 296
LCD_HEIGHT = 240
STATUS_HEIGHT = 30
PAGE_HEIGHT = LCD_HEIGHT - STATUS_HEIGHT
# 绘制缓冲提高到60行，减少整页平移时的分块刷新次数。
DISPLAY_BUFFER_LINES = 60
# EC800M 的 lcd_write() 在部分固件中会异步持有刷新缓冲区。页面平移期间
# 如果同时启用LVGL双缓冲，下一帧可能覆盖上一帧仍在发送的数据，导致花屏
# 甚至异常重启。保留60行单缓冲，待底层驱动确认同步发送后再开启双缓冲。
DISPLAY_ENABLE_DOUBLE_BUFFER = False
# 显式开启双缓冲时仍需为页面、字体、图片和业务对象保留的堆空间。
DISPLAY_DOUBLE_BUFFER_RESERVE = 160 * 1024
# 启动清屏同样按20行分块，避免一次申请整屏约142KB的连续内存。
DISPLAY_BOOT_CLEAR_LINES = 20
# MADCTL：MV 交换行列，MX 翻转 X 轴，将 240x296 面板旋转为 296x240 横屏。
# 若实机横屏方向与预期相反，可将 0x60 改为 0xA0 旋转到另一侧。
MADCTL_LANDSCAPE_RGB = 0x60

# CST816D 固件仍按竖屏安装方向上报手势编号，而 LCD 已顺时针旋转 90 度。
# 键为触控芯片原始编号，值为横屏 UI 中对应的手势编号。
LANDSCAPE_GESTURE_MAP = {
    0: 3,  # 原始右滑 -> 横屏下滑
    1: 2,  # 原始左滑 -> 横屏上滑
    2: 0,  # 原始上滑 -> 横屏右滑
    3: 1,  # 原始下滑 -> 横屏左滑
    4: 4,  # 边缘返回不改变
    5: 5,  # 点击不改变
    6: 6,  # 返回键不改变
}
# 手指移动超过此距离即判定为滑动，不再触发点击。
CLICK_MOVE_LIMIT = 12
# 右滑开始跟手的最小位移。独立于点击阈值，快速滑动时更容易及时接管。
BACK_DRAG_START_DISTANCE = 8
# 右滑达到屏幕宽度约四分之一后完成返回，否则松手时回弹。
BACK_COMMIT_DISTANCE = LCD_WIDTH // 4
# 快速甩动使用更短的距离阈值；超过时间后仍按普通位移阈值判断。
BACK_FLICK_DISTANCE = LCD_WIDTH // 8
BACK_FLICK_MAX_MS = 240
# 横向位移至少达到纵向位移的 2/3 才视为右滑，允许快速滑动中的少量斜向抖动。
BACK_DIRECTION_NUMERATOR = 2
BACK_DIRECTION_DENOMINATOR = 3
# 触摸移动只记录最新位置，主循环每20ms最多提交一帧。
BACK_FRAME_INTERVAL_MS = 20
# 未达到返回条件时使用20~70ms纯线性回弹。
BACK_REBOUND_MIN_MS = 20
BACK_REBOUND_MAX_MS = 70
# 返回成功后用60~90ms纯线性动画补完剩余行程。
BACK_COMPLETE_MIN_MS = 60
BACK_COMPLETE_MAX_MS = 90
# CST816D 仍按 240x296 竖屏方向上报坐标；LCD 已顺时针旋转为横屏。
TOUCH_POINT_NEEDS_ROTATION = True

# 由 ST7789 使用 0x21 命令完成硬件反色；发送端始终输出正常 RGB565。
# 这样刷新时不需要由 MicroPython 遍历并取反每一个像素。
DISPLAY_INVERSION_COMMAND = 0x21
# QuecPython的lcd_write()由原生驱动实现，不能直接持有LVGL回调传入的
# 临时缓冲对象。使用长期存活的bytearray承接数据，避免底层访问失效内存。
FLUSH_BUFFER = None
FLUSH_TAIL_BUFFER = None

FONT_MAIN = ("watch_Semibold_24.bin", 33)
FONT_POPUP = ("watch_Semibold_32.bin", 43)
FONT_SMALL = ("watch_Regular_16.bin", 22)
# 登录页M/F使用32号字库，其余登录文字统一使用24号字库。
FONT_LOGIN_PREFIX = FONT_POPUP
FONT_LOGIN_TEXT = FONT_MAIN
FONT_LOGIN_UNIFORM = ("watch_Semibold_24.bin", 33)
FONT_FLASH_PORT = 0

# 全局POC状态弹框：除登录页外覆盖在所有页面上方。
POC_POPUP_WIDTH = 270
POC_POPUP_HEIGHT = 110
POC_POPUP_GREEN = 0x168A45
POC_POPUP_RED = 0xC93434
POC_FAILURE_DURATION_MS = 2000
POC_REMOTE_MAX_DURATION_MS = 120000
POC_POPUP_PROCESS_INTERVAL_MS = 50
# 紧急弹框至少跨过一个20ms显示周期后才确认首帧已经提交。
POC_POPUP_FRAME_SUBMIT_MS = 20
# 接收端允许等待弹框450ms。这里必须略长于RTP门控时间，避免息屏唤醒时
# LCD先在250ms清掉待确认序号，导致RTP只能超时放行且弹框延迟出现。
POC_POPUP_FRAME_MAX_WAIT_MS = 600
# 音频收发期间静态界面最多每20ms运行一次LVGL任务，减少与RTP线程争用CPU。
AUDIO_LVGL_TASK_INTERVAL_MS = 20

# 图片资源按移远 LVGL 官方文件接口放在 usr 目录。
ICON_DIR = 'U:'  # 图片路径格式：U:/xxx.png；首页调用处使用完整路径。
# 电量控件占据屏幕右半区，文字右边缘仍保留 20px。
BATTERY_LABEL_WIDTH = LCD_WIDTH // 2
BATTERY_RIGHT_MARGIN = 20
# 网络信号图标使用24x24源图，右边缘与电量控件左边缘保持10px间距。
NETWORK_ICON_SIZE = 24
NETWORK_ICON_GAP = 10
NETWORK_ICON_X = LCD_WIDTH // 2 - NETWORK_ICON_SIZE - NETWORK_ICON_GAP + 50
NETWORK_ICON_Y = 0
NETWORK_ICON_PATHS = {
    None: "U:/wifi_0.png",
    "极弱": "U:/wifi_1.png",
    "弱": "U:/wifi_2.png",
    "一般": "U:/wifi_3.png",
    "强": "U:/wifi_4.png",
    "极好": "U:/wifi_5.png",
}
# 设置页按钮与对讲机模式按钮保持相同高度；内容超出后纵向滚动。
SETTINGS_ROW_HEIGHT = 58
SETTINGS_ROW_GAP = 8
SETTINGS_ICON_SIZE = 40
# 图标使用独立的透明安全槽位，避免图片贴近菜单行边界时被裁剪。
SETTINGS_ICON_BOX_SIZE = 48
SETTINGS_ICON_BOX_X = 6
SETTINGS_ICON_BOX_Y = (SETTINGS_ROW_HEIGHT - SETTINGS_ICON_BOX_SIZE) // 2
SETTINGS_LABEL_X = SETTINGS_ICON_BOX_X + SETTINGS_ICON_BOX_SIZE + 4
# 设置页释放后的惯性衰减参数；从 25 调到 12，滑行距离约增强为当前的 2 倍。
SETTINGS_SCROLL_THROW = 12
# 设置页跟手滚动倍率；原生 1 倍位移再补 2 倍，合计约 3 倍。
SETTINGS_SCROLL_MULTIPLIER = 3
# 设置页点击判定使用更小的位移阈值，优先把上下拖动识别为滑动。
SETTINGS_CLICK_MOVE_LIMIT = 8
SETTINGS_ROW_BG = 0x2D2D2D
# 滚动过程中不切换菜单行底色，避免按压态反复出现造成水波纹闪动。
SETTINGS_ROW_PRESSED_BG = SETTINGS_ROW_BG
SETTINGS_TITLE_COLOR = 0x00D7FF

# LCD 背光使用 PWM0 调光，亮度百分比与 PWM 占空比直接对应。
BRIGHTNESS_PWM_CHANNEL = PWM_V2.PWM0
BRIGHTNESS_PWM_FREQUENCY = 1250.0
BRIGHTNESS_MIN = 5
BRIGHTNESS_MAX = 100
BRIGHTNESS_DEFAULT = 100
# 启动阶段先关闭背光，等 LCD 首帧完整刷新后再恢复默认亮度，避免显示残留。
BRIGHTNESS_BOOT_OFF = 0
BRIGHTNESS_TRACK_BG = 0x26343C
BRIGHTNESS_TRACK_COLOR = 0xE8FFFF
BRIGHTNESS_ACCENT_COLOR = 0x00D7FF
BRIGHTNESS_DIM_COLOR = 0x66858F

# PCM音量使用0~11等级；开机默认最高音量。
VOLUME_MIN = 0
VOLUME_MAX = 11
VOLUME_DEFAULT = 11

# 入口文件可在打开 LCD 供电前调用该方法，避免供电恢复时背光先显示旧画面。
_BOOT_BACKLIGHT = None
_ACTIVE_BACKLIGHT = None


def prepare_backlight_off():
    """启动阶段关闭 LCD 背光；JingWuUI 会复用已创建的 PWM 对象。"""
    global _BOOT_BACKLIGHT, _ACTIVE_BACKLIGHT
    # 软重跑脚本时优先复用上一个 UI 的 PWM0，避免同一通道重复创建失败。
    if _ACTIVE_BACKLIGHT is not None:
        try:
            _ACTIVE_BACKLIGHT.open(
                BRIGHTNESS_PWM_FREQUENCY, BRIGHTNESS_BOOT_OFF)
            _BOOT_BACKLIGHT = _ACTIVE_BACKLIGHT
            return True
        except Exception:
            _ACTIVE_BACKLIGHT = None
    if _BOOT_BACKLIGHT is not None:
        return True
    try:
        _BOOT_BACKLIGHT = PWM_V2(
            BRIGHTNESS_PWM_CHANNEL,
            BRIGHTNESS_PWM_FREQUENCY,
            BRIGHTNESS_BOOT_OFF)
        _BOOT_BACKLIGHT.open()
        _ACTIVE_BACKLIGHT = _BOOT_BACKLIGHT
        return True
    except Exception as error:
        _BOOT_BACKLIGHT = None
        print("[亮度] 启动阶段关闭背光失败：{}".format(error))
        return False

# 连续无操作 3 秒后才开始所选息屏时长的倒计时。
SLEEP_COUNTDOWN_DELAY_MS = 3000
# ST7789 唤醒命令失败后的重试间隔，避免主循环每 5ms 连续阻塞重试。
DISPLAY_WAKE_RETRY_MS = 300
# LVGL首帧提交后给SPI/DMA留出完成时间，再打开背光，避免历史画面短暂可见。
DISPLAY_FIRST_FRAME_SETTLE_MS = 100 #60
SLEEP_DEFAULT_SECONDS = 30
SLEEP_OPTIONS = (
    ("10秒", 10),
    ("15秒", 15),
    ("20秒", 20),
    ("30秒", 30),
    ("1分钟", 60),
    ("永不息屏", None),
)

# 登录页固定布局参数；内容区位于全局状态栏下方。
LOGIN_PREFIX_DEFAULT = "M"
LOGIN_DIGIT_COUNT = 6
LOGIN_MESSAGE_DURATION_MS = 2000
LOGIN_ROW_BG = 0x252D32
LOGIN_KEY_BG = 0x30383D
LOGIN_SELECTED_BG = 0x006F7A
LOGIN_CONFIRM_BG = 0x007A42

# 肩灯每个亮灭阶段的默认持续时间，单位毫秒；运行时可通过公开方法调整。
SHOULDER_FLASH_INTERVAL_MS = 500

BLACK = 0x000000
WHITE = 0xFFFFFF
GREEN = 0x00FF00
RED = 0xFF0000
GRAY = 0x303030
# 对讲机模式选择行使用低对比度深色背景，选中状态使用深青色高亮。
MODE_ROW_BG = 0x202A32
MODE_ROW_SELECTED_BG = 0x0A3D47
MODE_DOT_OFF = 0x8FA7B3
MODE_DOT_ON = 0x00FFFF
MODE_TEXT_ON = 0x00FFFF
MODE_TEXT_OFF = 0x606060

# 网络状态页面颜色；信号等级颜色差异保持明显，便于快速识别。
NETWORK_SIGNAL_VERY_GOOD_COLOR = 0x008A45
NETWORK_SIGNAL_STRONG_COLOR = 0x62E682
NETWORK_SIGNAL_NORMAL_COLOR = WHITE
NETWORK_SIGNAL_WEAK_COLOR = 0xFFD400
NETWORK_SIGNAL_VERY_WEAK_COLOR = RED
NETWORK_CONNECTED_COLOR = 0x00D060
NETWORK_DISCONNECTED_COLOR = RED


def _lv_color(color):
    """返回 lv.color_hex() 使用的颜色值，RGB565转换由LVGL原生完成。"""
    return int(color) & 0xFFFFFF


def _flush(buf, x1, y1, x2, y2):
    """复制到持久RGB565缓冲后写屏；反色由ST7789硬件完成。"""
    global FLUSH_BUFFER, FLUSH_TAIL_BUFFER
    size = len(buf)
    if FLUSH_BUFFER is not None and len(FLUSH_BUFFER) == size:
        out = FLUSH_BUFFER
        out[:] = buf
    elif FLUSH_TAIL_BUFFER is not None and len(FLUSH_TAIL_BUFFER) == size:
        out = FLUSH_TAIL_BUFFER
        out[:] = buf
    else:
        # 局部刷新尺寸不固定时仍转换成lcd_write支持的bytearray。
        out = bytearray(buf)
    LCD_DEV.lcd_write(out, x1, y1, x2, y2)


# QuecPython ST7789 命令表；窗口坐标由驱动使用 0xF0~0xF3 等占位符填充。
XSTART_H, XSTART_L, YSTART_H, YSTART_L = 0xF0, 0xF1, 0xF2, 0xF3
XEND_H, XEND_L, YEND_H, YEND_L = 0xE0, 0xE1, 0xE2, 0xE3

_INIT = (
    # 软件复位可清除软重启前残留的 Sleep In 状态；复位后至少等待 120ms。
    0, 0, 0x01, 2, 0, 120,
    0, 0, 0x11, 2, 0, 120,
    0, 0, DISPLAY_INVERSION_COMMAND,
    0, 0, 0x00,
    0, 1, 0x36, 1, 1, MADCTL_LANDSCAPE_RGB,
    0, 1, 0x3A, 1, 1, 0x05,
    0, 1, 0x35, 1, 1, 0x00,
    0, 1, 0xC7, 1, 1, 0x00,
    0, 1, 0xCC, 1, 1, 0x09,
    0, 5, 0xB2, 1, 1, 0x0C, 1, 1, 0x0C, 1, 1, 0x00,
    1, 1, 0x33, 1, 1, 0x33,
    0, 1, 0xB7, 1, 1, 0x35, 0, 1, 0xBB, 1, 1, 0x36,
    0, 1, 0xC0, 1, 1, 0x2C, 0, 1, 0xC2, 1, 1, 0x01,
    0, 1, 0xC3, 1, 1, 0x0D, 0, 1, 0xC4, 1, 1, 0x20,
    0, 1, 0xC6, 1, 1, 0x0F,
    0, 2, 0xD0, 1, 1, 0xA4, 1, 1, 0xA1,
    0, 14, 0xE0, 1, 1, 0xD0, 1, 1, 0x17, 1, 1, 0x19,
    1, 1, 0x04, 1, 1, 0x03, 1, 1, 0x04, 1, 1, 0x32,
    1, 1, 0x41, 1, 1, 0x43, 1, 1, 0x09, 1, 1, 0x14,
    1, 1, 0x12, 1, 1, 0x33, 1, 1, 0x2C,
    0, 14, 0xE1, 1, 1, 0xD0, 1, 1, 0x18, 1, 1, 0x17,
    1, 1, 0x04, 1, 1, 0x03, 1, 1, 0x04, 1, 1, 0x31,
    1, 1, 0x46, 1, 1, 0x43, 1, 1, 0x09, 1, 1, 0x14,
    1, 1, 0x13, 1, 1, 0x31, 1, 1, 0x2D,
    0, 0, 0x29, 0, 0, 0x2C,
)
_INIT_P = bytearray(_INIT)
_INVALID_P = bytearray((
    0, 4, 0x2A, 1, 1, XSTART_H, 1, 1, XSTART_L, 1, 1, XEND_H, 1, 1, XEND_L,
    0, 4, 0x2B, 1, 1, YSTART_H, 1, 1, YSTART_L, 1, 1, YEND_H, 1, 1, YEND_L,
    0, 0, 0x2C,
))
# 深度睡眠唤醒后重新发送硬件反色命令，防止控制器状态丢失。
_ON_P = bytearray((
    0, 0, 0x11, 2, 0, 120,
    0, 0, DISPLAY_INVERSION_COMMAND,
    0, 0, 0x29,
))
_OFF_P = bytearray((0, 0, 0x28, 2, 0, 120, 0, 0, 0x10))


def _style(font, color, align=lv.TEXT_ALIGN.CENTER):
    """创建统一的中文字体样式。"""
    style = lv.style_t()
    style.init()
    style.set_text_font_v2(font[0], font[1], FONT_FLASH_PORT)
    style.set_text_color(lv.color_hex(_lv_color(color)))
    style.set_text_align(align)
    style.set_text_opa(255)
    style.set_bg_opa(0)
    style.set_border_width(0)
    try:
        style.set_pad_all(0)
    except Exception:
        style.set_pad_top(0)
        style.set_pad_bottom(0)
        style.set_pad_left(0)
        style.set_pad_right(0)
    return style


def _set_bg(obj, color):
    """设置对象背景及边框。"""
    obj.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    obj.set_style_bg_color(lv.color_hex(_lv_color(color)), lv.PART.MAIN | lv.STATE.DEFAULT)
    obj.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)


def _fix_position(obj):
    """关闭 LVGL 默认滚动行为，使对象及其子控件保持固定位置。"""
    obj.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    # 不同 QuecPython 固件包含的 LVGL 滚动标志可能不同，逐项兼容处理。
    for flag_name in (
            "SCROLLABLE", "SCROLL_ELASTIC", "SCROLL_MOMENTUM", "SCROLL_ONE",
            "SCROLL_CHAIN_HOR", "SCROLL_CHAIN_VER", "SCROLL_ON_FOCUS",
            "SCROLL_WITH_ARROW", "SNAPPABLE"):
        try:
            obj.clear_flag(getattr(lv.obj.FLAG, flag_name))
        except Exception:
            pass
    try:
        obj.set_scroll_dir(lv.DIR.NONE)
    except Exception:
        pass


class JingWuUI:
    """296x240 横屏页面控制器。"""

    def __init__(self, firmware_version="--", software_version="--",
                 hardware_version="--", poc_client=None):
        global LCD_DEV, _BOOT_BACKLIGHT, _ACTIVE_BACKLIGHT
        startup_wake_error = None
        # 先关背光再操作 LCD，防止电源重新打开时直接看到旧 GRAM 内容。
        self.brightness = BRIGHTNESS_DEFAULT
        self._brightness_pwm = _BOOT_BACKLIGHT
        _BOOT_BACKLIGHT = None
        if self._brightness_pwm is None:
            self._init_brightness_pwm(boot=True)
        _ACTIVE_BACKLIGHT = self._brightness_pwm
        LCD_DEV = LCD()
        LCD_DEV.lcd_init(_INIT_P, LCD_WIDTH, LCD_HEIGHT, 52000, 1, 4, 0,
                         _INVALID_P, _ON_P, _OFF_P, None)
        # QuecPython 在同一解释器中再次运行脚本时，底层 LCD 驱动可能仍保留
        # 上一次的 Sleep In 状态，并且 lcd_init() 不一定会重新发送初始化表。
        # 因此每次创建 UI 都无条件发送一次 Sleep Out + Display On，使 ST7789
        # 的真实状态与新 UI 对象的软件状态保持一致。
        try:
            LCD_DEV.lcd_display_on()
            print("[LCD] 启动时已强制唤醒屏幕")
        except Exception as error:
            startup_wake_error = error
            print("[LCD] 启动强制唤醒失败，将在主循环中重试：{}".format(
                error))
        if startup_wake_error is None:
            try:
                self._clear_display_memory()
            except Exception as error:
                # 清黑失败不等于LCD仍在休眠，后续仍由LVGL首帧覆盖画面。
                print("[LCD] 启动显存清黑失败：{}".format(error))
        # 清屏临时块已经完成SPI发送，立即回收后再分配LVGL显示缓冲，
        # 避免EC800M启动阶段因连续大块分配产生堆碎片。
        gc.collect()
        lv.init()

        # 页面、样式和驱动都由本对象长期持有，防止 MicroPython GC 回收后
        # LVGL 仍继续使用已经失效的底层内存。
        self.stack = []
        self.current = None
        # 普通组和单呼临时组可以同时存在，两类选择分别保存。
        # 当前未进入任何组时，组呼页第一项“无/退出”必须保持选中。
        self.selected_group = "__none_group__"
        self.selected_person = None
        self._intercom_groups = []
        self._intercom_people = []
        # 组呼选择只保存TCP 0x83确认成功的组；待确认选择单独保存。
        self._pending_group = None
        self._join_state_seen = None
        self._join_revision_seen = None
        self._join_previous_group = None
        # 单呼页面第一项固定为“无/解散”，待确认选择单独保存。
        self._single_none_key = "__none__"
        self._single_group_id = None
        self._pending_person = None
        self._single_state_seen = None
        self._single_revision_seen = None
        # 版本信息由启动入口传入，避免 lcd_touch 反向导入 main 形成循环依赖。
        self.device_versions = {
            "software": ("--" if software_version in (None, "")
                         else str(software_version)),
            "hardware": ("--" if hardware_version in (None, "")
                         else str(hardware_version)),
            "firmware": ("--" if firmware_version in (None, "")
                         else str(firmware_version)),
        }
        self.status = {"battery": "--"}
        # 息屏期间只更新内存中的电量，唤醒后再统一刷新 LVGL 标签。
        self._status_refresh_pending = False
        self._battery_monitor = None
        self._battery_error_reported = False
        self._network_monitor = None
        self._network_snapshot = None
        self._network_icon = None
        self._network_icon_path = None
        self._network_labels = {}
        self._network_refresh_pending = False
        self._network_error_reported = False
        # POC后台线程只发布快照，所有LVGL对象仍只在本主线程操作。
        self._poc_client = poc_client
        self._poc_snapshot = None
        self._poc_error_reported = False
        self._poc_http_data_applied = False
        # HTTP结果可能与对讲音频同时到达；先缓存，等弹框首帧和音频结束后
        # 再在LVGL主线程更新名单控件，避免名单重排抢占语音关键时序。
        self._pending_intercom_data = None
        self._poc_floor_event_seen = None
        self._poc_popup = None
        self._poc_popup_line1 = None
        self._poc_popup_line2 = None
        self._poc_touch_blocker = None
        self._poc_popup_visible = False
        self._poc_popup_mode = None
        self._poc_popup_signature = None
        self._poc_popup_last_process_ms = None
        # 紧急麦权弹框提交首帧后才通知RTP线程，避免音频先占用调度。
        self._poc_popup_frame_pending_revision = None
        self._poc_popup_frame_pending_ms = None
        self._poc_call_active = False
        self._poc_touch_locked = False
        self._poc_touch_guard = False
        self._poc_touch_released = True
        self._poc_self_hidden_group = None
        self._poc_remote_started = {}
        self._poc_remote_expired = {}
        self._poc_failure_until_ms = None
        self._poc_notice_until_ms = None
        self._poc_notice_text = None
        self._logout_revision_seen = None
        self.volume = VOLUME_DEFAULT
        self._volume_slider = None
        self._volume_value_label = None
        self._volume_revision_seen = None
        self._login_prefix = LOGIN_PREFIX_DEFAULT
        self._login_digits = ""
        self._login_prefix_buttons = {}
        self._login_number_label = None
        self._login_status_label = None
        self._force_login_box = None
        self._force_login_cancel_button = None
        self._force_login_confirm_button = None
        self._login_message_until_ms = None
        self._force_login_prompt = False
        self._login_home_loaded = False
        self._pending_back = False
        self._touch_sequence_swipe = False
        self._pointer_point = None
        self._pending_gc = False
        self._back_drag_tracking = False
        self._back_drag_active = False
        self._back_drag_start = None
        self._back_drag_started_ms = None
        self._back_drag_offset = 0
        self._back_drag_target_offset = 0
        self._back_drag_frame_pending = False
        self._back_drag_last_frame_ms = 0
        self._back_drag_current = None
        self._back_drag_previous = None
        self._back_drag_hardware_confirmed = False
        self._back_animating = False
        self._back_anim_complete = False
        self._back_anim_start_value = 0
        self._back_anim_end_value = 0
        self._back_anim_started_ms = 0
        self._back_anim_duration_ms = 0
        self._back_anim_last_frame_ms = 0
        self._back_anim_finish_pending = False
        self._touch_point_cache = None
        self._back_drag_ready_reported = False
        self._touch_point_warning_reported = False
        # LVGL 输入层使用 90 度旋转把 CST816D 的 240x296 原始坐标
        # 转成界面使用的 296x240 坐标。这样命中判断和画面坐标完全一致。
        self._lvgl_input_rotated = False
        self.pages = {}
        self._styles = {}
        # 页面对象长期复用，分别保存组呼和单呼页面的单选控件。
        self._group_rows = {}
        self._single_rows = {}
        self._group_content = None
        self._single_content = None
        self._group_rows_built = False
        self._single_rows_built = False
        self.shoulder_mode = "off"
        self._shoulder_rows = {}
        self._shoulder_controller = None
        self._shoulder_error_reported = False
        self._motion_service = None
        self._shoulder_refresh_pending = False
        # 息屏时间开机默认 30 秒；None 表示“永不息屏”。
        self.sleep_timeout_seconds = SLEEP_DEFAULT_SECONDS
        self._sleep_rows = {}
        self.fall_detection_enabled = False
        self._fall_rows = {}
        self._sc7a20h_service = None
        self._last_activity_ms = utime.ticks_ms()
        self._sleep_countdown_active = False
        # 启动强制唤醒失败时按“仍在息屏”处理，主循环会按固定间隔重试，
        # 避免软件误认为屏幕已亮而吞掉后续唤醒机会。
        self._screen_sleeping = startup_wake_error is not None
        self._pending_display_wake = startup_wake_error is not None
        # 息屏唤醒期间若有对讲弹框，先提交弹框首帧，再补刷完整页面。
        self._display_wake_full_refresh_pending = False
        self._display_wake_last_attempt_ms = None
        self._display_wake_error_reported = startup_wake_error is not None
        # 唤醒触摸必须整段吞掉，防止同一次触摸继续产生点击或滑动。
        self._wake_touch_guard = False
        self._wake_touch_released = False
        self._click_targets = []
        self._click_area = None
        self._click_dispatched = False
        # 当前是否仍处于按住状态；设置页只在按住拖动阶段放大位移。
        self._touch_pressed = False
        self._event_callbacks = []
        self._scroll_containers = []
        self._sleep_scroll_positions = []
        self.brightness = BRIGHTNESS_DEFAULT
        self._sleep_brightness = BRIGHTNESS_DEFAULT
        self._brightness_slider = None
        self._brightness_value_label = None
        self._restoring_brightness = False

        self._init_display()
        self._init_shoulder_lamp()
        self._init_battery_monitor()
        self._init_network_monitor()
        self._init_root_screen()
        self._init_touch()
        self.show_login()
        # 先让 LVGL 完成登录页首帧，再打开背光。
        self._present_initial_frame()
        self.set_brightness(BRIGHTNESS_DEFAULT)
        self.notify_activity()
        # 加速度服务会创建后台线程；等显示缓冲、页面、触摸和首帧全部完成后
        # 再启动，避免与LVGL启动阶段的大量内存分配并发。
        self._init_motion_service()

    def _clear_display_memory(self):
        """直接用纯黑RGB565数据覆盖ST7789完整GRAM，清除历史残留画面。"""
        # 发送端始终使用正常RGB565；硬件反色模式由ST7789内部处理。
        # 黑色数据为0x0000，分块缓冲区全为0即可直接发送。
        clear_block = bytearray(
            LCD_WIDTH * DISPLAY_BOOT_CLEAR_LINES * 2)
        y = 0
        while y < LCD_HEIGHT:
            lines = min(DISPLAY_BOOT_CLEAR_LINES, LCD_HEIGHT - y)
            if lines == DISPLAY_BOOT_CLEAR_LINES:
                data = clear_block
            else:
                data = bytearray(LCD_WIDTH * lines * 2)
            LCD_DEV.lcd_write(
                data, 0, y, LCD_WIDTH - 1, y + lines - 1)
            y += lines
        # 给最后一块SPI/DMA传输留出完成时间，缓冲区在此期间保持有效。
        utime.sleep_ms(DISPLAY_FIRST_FRAME_SETTLE_MS)
        print("[LCD] 启动时已清除历史显存")

    def _present_initial_frame(self):
        """启动时主动提交首帧，避免背光打开前后出现花屏或历史残留。"""
        # 不同 QuecPython 固件的 LVGL 绑定差异较大，部分版本没有
        # lv.obj_invalidate()，但对象自身的 invalidate() 可用。
        try:
            self._root_screen.invalidate()
        except Exception:
            pass
        try:
            # 即使无效化接口不可用，也必须继续执行 task_handler()，不能让
            # 异常提前结束首帧刷新流程。
            # LVGL默认显示刷新周期通常约30ms；只推进5ms时，task_handler()
            # 可能尚未真正执行显示刷新。这里一次推进60ms，保证首帧任务到期。
            lv.tick_inc(DISPLAY_FIRST_FRAME_SETTLE_MS)
            lv.task_handler()
            # 部分QuecPython固件的lcd_write()由底层异步发送；task_handler()
            # 返回时最后一批像素可能仍在传输，因此背光恢复前稍作等待。
            utime.sleep_ms(DISPLAY_FIRST_FRAME_SETTLE_MS)
        except Exception as error:
            # 首帧刷新失败不阻断程序；主循环仍会继续刷新 LVGL。
            print("[LCD] 启动首帧刷新失败：{}".format(error))

    def _init_display(self):
        # 第一块60行绘制缓冲和持久发送缓冲是基础配置。
        self._display_buffer = bytearray(
            LCD_WIDTH * DISPLAY_BUFFER_LINES * 2)
        global FLUSH_BUFFER, FLUSH_TAIL_BUFFER
        FLUSH_BUFFER = bytearray(len(self._display_buffer))
        # 页面内容区为210行，使用60行缓冲时最后一块是30行；长期复用该
        # 尾块，避免每一帧动画都临时分配约17.8KB。
        tail_lines = max(
            LCD_HEIGHT % DISPLAY_BUFFER_LINES,
            PAGE_HEIGHT % DISPLAY_BUFFER_LINES)
        FLUSH_TAIL_BUFFER = (bytearray(LCD_WIDTH * tail_lines * 2)
                             if tail_lines else None)
        self._display_buffer_secondary = None
        self._display_double_buffered = False
        # QuecPython当前LVGL绑定和官方示例均使用缓冲区字节长度。
        # 这里保持固件接口约定，不能直接套用标准C接口的像素数量定义。
        buffer_size = len(self._display_buffer)
        if DISPLAY_ENABLE_DOUBLE_BUFFER:
            self._draw_buffer = lv.disp_draw_buf_t()
            try:
            # 内存充足时由LVGL交替绘制两块缓冲，LCD传输期间可准备下一帧。
                try:
                    free_before = int(gc.mem_free())
                except Exception:
                    free_before = 0
                required = (len(self._display_buffer) +
                            DISPLAY_DOUBLE_BUFFER_RESERVE)
                if free_before < required:
                    raise MemoryError(
                        "free={} required={}".format(free_before, required))
                secondary = bytearray(len(self._display_buffer))
                self._draw_buffer.init(
                    self._display_buffer, secondary, buffer_size)
                self._display_buffer_secondary = secondary
                self._display_double_buffered = True
                print("[LCD] 显示缓冲：60行双缓冲")
            except Exception as error:
                # 分配失败时释放第二块缓冲，再创建安全单缓冲。
                self._display_buffer_secondary = None
                self._draw_buffer = lv.disp_draw_buf_t()
                self._draw_buffer.init(
                    self._display_buffer, None, buffer_size)
                gc.collect()
                print("[LCD] 显示缓冲：60行单缓冲，双缓冲失败：{}".format(
                    error))
        else:
            # 当前EC800M配置固定使用安全单缓冲，不通过异常流程重复创建对象。
            self._draw_buffer = lv.disp_draw_buf_t()
            self._draw_buffer.init(
                self._display_buffer, None, buffer_size)
            gc.collect()
            print("[LCD] 显示缓冲：60行安全单缓冲")
        self._display_driver = lv.disp_drv_t()
        self._display_driver.init()
        self._display_driver.draw_buf = self._draw_buffer
        self._display_driver.flush_cb = _flush
        # CST816D 的原始坐标轴仍是竖屏 240x296。把 LVGL 驱动的基础
        # 分辨率设为竖屏，再启用硬件旋转标志：LVGL 会自动把输入点
        # 转成 x=295-y、y=x，同时逻辑分辨率仍为 296x240。关闭
        # sw_rotate，避免重复旋转已经由 LCD MADCTL 完成的画面。
        self._display_driver.hor_res = LCD_HEIGHT
        self._display_driver.ver_res = LCD_WIDTH
        try:
            # LVGL C 枚举值：LV_DISP_ROT_NONE=0、LV_DISP_ROT_90=1。
            self._display_driver.rotated = 1
            self._lvgl_input_rotated = True
        except Exception:
            # 极旧固件若没有 rotated 字段，回退到原来的逻辑分辨率；
            # 页面级坐标分发仍可工作，但圆圈命中可能受固件限制。
            self._display_driver.hor_res = LCD_WIDTH
            self._display_driver.ver_res = LCD_HEIGHT
        try:
            # 画面旋转已经由 ST7789 的 MADCTL 完成，LVGL 不再做软件旋转。
            self._display_driver.sw_rotate = 0
        except Exception:
            pass
        self._display = self._display_driver.register()

    def _init_root_screen(self):
        """创建根屏幕和全局共享状态标签。"""
        self._root_screen = lv.obj()
        self._root_screen.set_size(LCD_WIDTH, LCD_HEIGHT)
        _fix_position(self._root_screen)
        _set_bg(self._root_screen, BLACK)
        lv.scr_load(self._root_screen)

        # 状态栏控件在根屏幕中只创建一次，所有页面共享。
        self._network_icon = lv.img(self._root_screen)
        self._network_icon.set_size(NETWORK_ICON_SIZE, NETWORK_ICON_SIZE)
        self._network_icon.set_pos(NETWORK_ICON_X, NETWORK_ICON_Y)
        _fix_position(self._network_icon)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            try:
                self._network_icon.set_style_bg_opa(
                    0, lv.PART.MAIN | state)
                self._network_icon.set_style_border_width(
                    0, lv.PART.MAIN | state)
                self._network_icon.set_style_outline_width(
                    0, lv.PART.MAIN | state)
                self._network_icon.set_style_shadow_width(
                    0, lv.PART.MAIN | state)
                self._network_icon.set_style_pad_all(
                    0, lv.PART.MAIN | state)
            except Exception:
                pass

        self._battery_label = lv.label(self._root_screen)
        self._battery_label.set_size(BATTERY_LABEL_WIDTH, 24)
        # 电量文字右对齐，实际右边缘距离屏幕右侧 20px；纵坐标保持 3px。
        self._root_pad_left = 0
        try:
            self._root_pad_left = int(
                self._root_screen.get_style_pad_left(lv.PART.MAIN))
        except Exception:
            pass
        self._battery_label.set_pos(
            max(0, LCD_WIDTH // 2 - self._root_pad_left), 3)
        self._battery_label.add_style(self._get_style(
            FONT_SMALL, WHITE, lv.TEXT_ALIGN.RIGHT), lv.PART.MAIN)
        try:
            self._battery_label.set_style_pad_right(
                BATTERY_RIGHT_MARGIN, lv.PART.MAIN | lv.STATE.DEFAULT)
        except Exception:
            pass
        self._refresh_status()
        self._init_poc_popup()

    def _init_poc_popup(self):
        """创建一次全局对讲弹框，后续只更新文本、颜色和隐藏状态。"""
        blocker = lv.obj(self._root_screen)
        blocker.set_size(LCD_WIDTH, LCD_HEIGHT)
        blocker.set_pos(0, 0)
        _fix_position(blocker)
        try:
            blocker.set_style_bg_opa(
                0, lv.PART.MAIN | lv.STATE.DEFAULT)
            blocker.set_style_border_opa(
                0, lv.PART.MAIN | lv.STATE.DEFAULT)
        except Exception:
            pass
        try:
            blocker.add_flag(lv.obj.FLAG.CLICKABLE)
        except Exception:
            pass

        def block_touch(*args):
            event = args[-1]
            try:
                code = event.get_code()
            except Exception:
                code = event
            self._consume_touch_event_for_poc(code)

        self._event_callbacks.append(block_touch)
        try:
            blocker.add_event_cb(block_touch, lv.EVENT.ALL, None)
        except Exception:
            wrapper = lambda target, event: block_touch(event)
            self._event_callbacks.append(wrapper)
            blocker.set_event_cb(wrapper, lv.EVENT.ALL, None)
        blocker.add_flag(lv.obj.FLAG.HIDDEN)
        self._poc_touch_blocker = blocker

        popup = lv.obj(self._root_screen)
        popup.set_size(POC_POPUP_WIDTH, POC_POPUP_HEIGHT)
        popup.set_pos((LCD_WIDTH - POC_POPUP_WIDTH) // 2,
                      (LCD_HEIGHT - POC_POPUP_HEIGHT) // 2)
        _fix_position(popup)
        try:
            # 弹框只负责显示，触摸继续落到其下方的全屏拦截层。
            popup.clear_flag(lv.obj.FLAG.CLICKABLE)
        except Exception:
            pass
        _set_bg(popup, POC_POPUP_GREEN)
        try:
            popup.set_style_radius(6, lv.PART.MAIN | lv.STATE.DEFAULT)
            popup.set_style_pad_all(0, lv.PART.MAIN | lv.STATE.DEFAULT)
        except Exception:
            pass

        line_width = POC_POPUP_WIDTH - 28
        self._poc_popup_line1 = lv.label(popup)
        self._poc_popup_line1.set_size(line_width, FONT_MAIN[1])
        self._poc_popup_line1.set_pos(14, 17)
        self._poc_popup_line1.add_style(self._get_style(
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.LEFT), lv.PART.MAIN)

        self._poc_popup_line2 = lv.label(popup)
        self._poc_popup_line2.set_size(line_width, FONT_MAIN[1])
        self._poc_popup_line2.set_pos(14, 58)
        self._poc_popup_line2.add_style(self._get_style(
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.LEFT), lv.PART.MAIN)

        popup.add_flag(lv.obj.FLAG.HIDDEN)
        self._poc_popup = popup

    def _init_touch(self):
        # 使用固件提供的 CST816D 原生输入回调，避免主动调用 CST816_read 对象。
        #self.tp = cst816(irq=44, reset=2)
        self.tp = cst816(irq=11, reset=15)
        self.tp.activate()
        self.tp.init()
        self.tp.set_callback(self._gesture)
        self._touch_driver = lv.indev_drv_t()
        self._touch_driver.init()
        self._touch_driver.type = lv.INDEV_TYPE.POINTER
        # QuecPython 官方用法：直接把驱动提供的 read 回调交给 LVGL。
        # self.tp.read 是 CST816_read 对象，不是可由 Python 主动调用的普通函数；
        # LVGL 会在轮询输入设备时按固件约定自动调用它。
        self._touch_driver.read_cb = self.tp.read
        self._touch_driver.long_press_time = 80
        # 设置菜单启用释放惯性，让快速上滑/下滑时松手后继续滑行。
        try:
            self._touch_driver.scroll_throw = SETTINGS_SCROLL_THROW
        except Exception:
            pass
        try:
            # 让 LVGL 手势阈值与点击位移阈值一致，作为坐标判断的双重保护。
            self._touch_driver.gesture_limit = CLICK_MOVE_LIMIT
        except Exception:
            pass
        self._touch_input = self._touch_driver.register()

    def _init_brightness_pwm(self, boot=False):
        """初始化 LCD 背光 PWM；启动阶段使用0%避免旧画面短暂可见。"""
        global _ACTIVE_BACKLIGHT
        if self._brightness_pwm is not None:
            return True
        try:
            duty = BRIGHTNESS_BOOT_OFF if boot else BRIGHTNESS_DEFAULT
            self._brightness_pwm = PWM_V2(
                BRIGHTNESS_PWM_CHANNEL,
                BRIGHTNESS_PWM_FREQUENCY,
                duty)
            self._brightness_pwm.open()
            _ACTIVE_BACKLIGHT = self._brightness_pwm
            return True
        except Exception as error:
            self._brightness_pwm = None
            print("[亮度] PWM初始化失败：{}".format(error))
            return False

    def _init_shoulder_lamp(self):
        """初始化肩灯控制器；I2C异常时保留 UI，避免阻塞屏幕启动。"""
        if ShoulderLampController is None:
            print("[肩灯] 未找到 ET6312B 控制器")
            return
        try:
            self._shoulder_controller = ShoulderLampController(
                interval_ms=SHOULDER_FLASH_INTERVAL_MS)
        except Exception as error:
            self._shoulder_controller = None
            print("[肩灯] ET6312B 初始化失败：{}".format(error))

    def _init_motion_service(self):
        """启动独立加速度服务；失败时不阻断 LCD 主界面。"""
        if SC7A20HService is None or self._shoulder_controller is None:
            if SC7A20HService is None:
                print("[加速度] 未找到 SC7A20HService，双击/跌倒功能不可用")
            return
        try:
            self._motion_service = SC7A20HService(
                self._shoulder_controller,
                int1_pin=17,
                int2_pin=18)
            self._motion_service.start()
            self._sc7a20h_service = self._motion_service
        except Exception as error:
            self._motion_service = None
            print("[加速度] 服务启动失败：{}".format(error))

    def _init_battery_monitor(self):
        """初始化 EC800M ADC0 电池监控；失败时不阻断 LCD 启动。"""
        if BatteryMonitor is None:
            print("[电池] 未找到 battery_monitor 模块，电量暂不可用")
            return
        try:
            self._battery_monitor = BatteryMonitor()
            self._battery_error_reported = False
        except Exception as error:
            self._battery_monitor = None
            print("[电池] ADC0 初始化失败：{}".format(error))

    def _init_network_monitor(self):
        """初始化网络监控，并立即读取一次 SIM 卡号和信号值。"""
        # 优先复用入口传给POC客户端的实例。main.py会在UI首帧完成后再启动
        # 它的后台线程，从而避免网络线程与LVGL初始化并发。
        monitor = None
        if self._poc_client is not None:
            monitor = getattr(self._poc_client, "network_monitor", None)
        if monitor is None and get_default_monitor is None:
            print("[网络] 未找到 network_monitor 模块")
            return
        try:
            if monitor is None:
                try:
                    monitor = get_default_monitor(False)
                except TypeError:
                    # 兼容未更新的旧版network_monitor模块。
                    monitor = get_default_monitor()
            self._network_monitor = monitor
            snapshot = self._network_monitor.tick()
            if snapshot is not None:
                self._network_snapshot = snapshot
            self._network_error_reported = False
        except Exception as error:
            self._network_monitor = None
            print("[网络] 监控初始化失败：{}".format(error))

    def set_brightness(self, value):
        """设置 5%~100% LCD 亮度；亮度值直接对应 PWM 占空比。"""
        global _ACTIVE_BACKLIGHT
        try:
            value = int(value)
        except Exception:
            value = BRIGHTNESS_DEFAULT
        value = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, value))
        self.brightness = value

        if self._brightness_pwm is None:
            # 首次初始化失败时允许调用者再次尝试，方便外部直接调用本方法恢复。
            try:
                self._brightness_pwm = PWM_V2(
                    BRIGHTNESS_PWM_CHANNEL,
                    BRIGHTNESS_PWM_FREQUENCY,
                    value)
                self._brightness_pwm.open()
                _ACTIVE_BACKLIGHT = self._brightness_pwm
            except Exception as error:
                self._brightness_pwm = None
                print("[亮度] PWM设置失败：{}".format(error))
        else:
            try:
                self._brightness_pwm.open(
                    BRIGHTNESS_PWM_FREQUENCY, value)
            except Exception as error:
                print("[亮度] PWM设置失败：{}".format(error))

        if self._brightness_value_label is not None:
            self._brightness_value_label.set_text("{}%".format(value))
        return value

    def get_brightness(self):
        """返回当前 LCD 背光亮度百分比。"""
        return self.brightness

    def notify_activity(self):
        """通知 UI 发生了操作，供触摸、PTT 和物理按键统一重置息屏计时。"""
        self._last_activity_ms = utime.ticks_ms()
        self._sleep_countdown_active = False
        if self._screen_sleeping:
            # LCD 开关命令留给主循环执行，避免外部按键或触摸回调阻塞。
            if not self._pending_display_wake:
                self._display_wake_last_attempt_ms = None
            self._pending_display_wake = True
        return True

    def _ticks_since(self, start_ms):
        """兼容不同 QuecPython 固件，安全计算带回绕的毫秒差值。"""
        now = utime.ticks_ms()
        try:
            return max(0, utime.ticks_diff(now, start_ms))
        except Exception:
            return max(0, now - start_ms)

    def _reset_touch_for_wake(self):
        """清除本次触摸可能产生的点击、滑动及返回状态。"""
        self._touch_pressed = False
        self._touch_sequence_swipe = True
        self._click_dispatched = True
        self._pending_back = False
        self._clear_back_drag_state()

    def _remember_sleep_ui_state(self):
        """保存可能被唤醒触摸改变的滑动位置和亮度值。"""
        self._sleep_brightness = self.brightness
        positions = []
        for content in self._scroll_containers:
            try:
                y = int(content.get_scroll_y())
            except Exception:
                try:
                    y = int(lv.obj_get_scroll_y(content))
                except Exception:
                    continue
            positions.append((content, y))
        self._sleep_scroll_positions = positions

    def _restore_sleep_ui_state(self):
        """唤醒时恢复息屏前的滑动位置和亮度，消除首次触摸副作用。"""
        for content, y in self._sleep_scroll_positions:
            try:
                content.scroll_to_y(y, lv.ANIM.OFF)
            except Exception:
                try:
                    lv.obj_scroll_to_y(content, y, lv.ANIM.OFF)
                except Exception:
                    pass
        self._sleep_scroll_positions = []

        # 息屏时只把硬件 PWM 置为 0，不改变逻辑亮度；唤醒后必须
        # 无条件重写息屏前占空比，否则 brightness 数值相同会跳过恢复。
        self.set_brightness(self._sleep_brightness)
        if self._brightness_slider is not None:
            try:
                self._brightness_slider.set_value(
                    self._sleep_brightness, lv.ANIM.OFF)
            except Exception:
                pass

    def _consume_touch_event_for_wake(self, code):
        """息屏后的第一段 LVGL 触摸只唤醒屏幕，不执行界面操作。"""
        if self._screen_sleeping or self._pending_display_wake:
            if not self._wake_touch_guard:
                self._wake_touch_guard = True
                self._wake_touch_released = False
            if code in (lv.EVENT.RELEASED, lv.EVENT.CLICKED):
                self._wake_touch_released = True
            self._reset_touch_for_wake()
            self.notify_activity()
            return True

        if self._wake_touch_guard:
            # 第一段触摸结束后，下一次按下才解除拦截，避免 RELEASED 后到达的
            # CLICKED 或 CST816D 手势继续操作当前页面。
            if code == lv.EVENT.PRESSED and self._wake_touch_released:
                self._wake_touch_guard = False
                self._wake_touch_released = False
                self._touch_sequence_swipe = False
                self._click_dispatched = False
                self.notify_activity()
                return False
            if code in (lv.EVENT.RELEASED, lv.EVENT.CLICKED):
                self._wake_touch_released = True
            self._reset_touch_for_wake()
            return True

        if code == lv.EVENT.PRESSED:
            self.notify_activity()
        return False

    def _set_poc_touch_locked(self, locked):
        """对讲期间过滤触摸；登录页始终保留登录操作。"""
        if self.current is not None and self.current[0] == "login":
            locked = False
        locked = bool(locked)
        if locked == self._poc_touch_locked:
            return

        was_pressed = self._touch_pressed
        self._poc_touch_locked = locked
        if locked:
            # 若锁定发生在右滑过程中，按当前位移正常收尾，避免页面停在半屏。
            if self._back_drag_tracking and not self._back_animating:
                self._back_drag_release(self._touch_point_cache)
            self._pending_back = False
            self._touch_pressed = False
            self._touch_sequence_swipe = True
            self._click_dispatched = True
            self._poc_touch_guard = True
            self._poc_touch_released = not was_pressed
            if self._poc_touch_blocker is not None:
                self._poc_touch_blocker.clear_flag(lv.obj.FLAG.HIDDEN)
                try:
                    self._poc_touch_blocker.move_foreground()
                except Exception:
                    pass
            if self._poc_popup_visible and self._poc_popup is not None:
                try:
                    self._poc_popup.move_foreground()
                except Exception:
                    pass
        elif self._poc_touch_blocker is not None:
            self._poc_touch_blocker.add_flag(lv.obj.FLAG.HIDDEN)

    def _consume_touch_event_for_poc(self, code):
        """吞掉对讲期间及解锁前残留的同一段触摸事件。"""
        if self._poc_touch_locked:
            if code == lv.EVENT.PRESSED:
                self._poc_touch_released = False
            elif code in (lv.EVENT.RELEASED, lv.EVENT.CLICKED):
                self._poc_touch_released = True
            self._touch_pressed = False
            self._touch_sequence_swipe = True
            self._click_dispatched = True
            self._pending_back = False
            return True

        if self._poc_touch_guard:
            # 锁定期间已经松手时，解锁后的下一次新按下可直接使用。
            if code == lv.EVENT.PRESSED and self._poc_touch_released:
                self._poc_touch_guard = False
                self._poc_touch_released = False
                return False
            if code in (lv.EVENT.RELEASED, lv.EVENT.CLICKED):
                self._poc_touch_released = True
            self._touch_pressed = False
            self._touch_sequence_swipe = True
            self._click_dispatched = True
            return True
        return False

    def _consume_touch_event(self, code):
        """统一处理息屏唤醒和对讲触摸锁定。"""
        if self._poc_touch_locked:
            return self._consume_touch_event_for_poc(code)
        if self._consume_touch_event_for_wake(code):
            return True
        return self._consume_touch_event_for_poc(code)

    def _consume_gesture_for_wake(self):
        """处理只有硬件手势、没有连续坐标事件时的息屏唤醒。"""
        if self._screen_sleeping or self._pending_display_wake:
            self._wake_touch_guard = True
            # 硬件手势通常在松手后上报，可直接标记本次触摸已经结束。
            self._wake_touch_released = True
            self._reset_touch_for_wake()
            self.notify_activity()
            return True
        if self._wake_touch_guard:
            self._reset_touch_for_wake()
            return True
        self.notify_activity()
        return False

    def _consume_gesture_for_poc(self):
        """硬件手势在松手后上报，对讲期间直接标记为已释放并丢弃。"""
        if not self._poc_touch_locked and not self._poc_touch_guard:
            return False
        self._poc_touch_released = True
        self._touch_pressed = False
        self._touch_sequence_swipe = True
        self._click_dispatched = True
        self._pending_back = False
        return True

    def _enter_display_sleep(self):
        """通过 ST7789 的 Display Off + Sleep In 命令进入低功耗息屏。"""
        if self._screen_sleeping or self._pending_display_wake:
            return False
        self._remember_sleep_ui_state()
        try:
            LCD_DEV.lcd_display_off()
        except Exception as error:
            print("[息屏] ST7789息屏失败：{}".format(error))
            self.notify_activity()
            return False
        if self._brightness_pwm is not None:
            try:
                self._brightness_pwm.open(
                    BRIGHTNESS_PWM_FREQUENCY, BRIGHTNESS_BOOT_OFF)
            except Exception as error:
                print("[息屏] 背光PWM关闭失败：{}".format(error))
        self._screen_sleeping = True
        self._display_wake_last_attempt_ms = None
        self._display_wake_error_reported = False
        self._sleep_countdown_active = False
        print("[息屏] 已进入低功耗模式")
        return True

    def _process_display_wake(self):
        """在主循环中执行 ST7789 深度睡眠唤醒并恢复当前 LVGL 画面。"""
        if not self._pending_display_wake:
            return
        if not self._screen_sleeping:
            self._pending_display_wake = False
            self._display_wake_last_attempt_ms = None
            return

        if (self._display_wake_last_attempt_ms is not None and
                self._ticks_since(self._display_wake_last_attempt_ms) <
                DISPLAY_WAKE_RETRY_MS):
            return
        self._display_wake_last_attempt_ms = utime.ticks_ms()
        try:
            LCD_DEV.lcd_display_on()
        except Exception as error:
            # 保留 pending 标志，主循环将在退避时间后自动重试。
            if not self._display_wake_error_reported:
                print("[息屏] ST7789唤醒失败，将自动重试：{}".format(error))
                self._display_wake_error_reported = True
            return

        self._pending_display_wake = False
        self._display_wake_last_attempt_ms = None
        self._display_wake_error_reported = False
        self._restore_sleep_ui_state()
        self._screen_sleeping = False
        self._last_activity_ms = utime.ticks_ms()
        self._sleep_countdown_active = False
        # 息屏期间电池仍持续采集，但不操作 LVGL；唤醒时只刷新一次最新值。
        if self._status_refresh_pending:
            self._refresh_status()
            self._status_refresh_pending = False
        # 网络页面息屏期间只更新缓存；唤醒时将最新快照刷新到标签。
        if (self.current is not None and
                self.current[0] == "network" and
                self._network_monitor is not None):
            try:
                self._network_snapshot = (
                    self._network_monitor.get_snapshot())
                self._refresh_network_page()
                self._network_refresh_pending = False
            except Exception as error:
                if not self._network_error_reported:
                    print("[网络] 唤醒刷新失败：{}".format(error))
                    self._network_error_reported = True
        # 双击可能在息屏期间切换了肩灯模式；唤醒后才更新单选控件。
        self._sync_shoulder_mode_ui()
        # 对讲弹框优先：不要让整屏分块刷新挡住弹框首帧；弹框确认后
        # 再补刷根屏幕，避免唤醒时先听到声音、后看到弹框。
        if self._poc_popup_frame_pending_revision is not None:
            self._display_wake_full_refresh_pending = True
        else:
            self._invalidate_root_screen()
        print("[息屏] 屏幕已唤醒")

    def _invalidate_root_screen(self):
        """请求根屏幕完整刷新，兼容不同QuecPython LVGL绑定。"""
        try:
            self._root_screen.invalidate()
        except Exception:
            try:
                lv.obj_invalidate(self._root_screen)
            except Exception:
                pass

    def _process_auto_sleep(self):
        """处理“等待 3 秒 + 所选时长”的非阻塞自动息屏计时。"""
        if (self._screen_sleeping or self._pending_display_wake or
                self.sleep_timeout_seconds is None or
                self._poc_call_active):
            self._sleep_countdown_active = False
            return

        idle_ms = self._ticks_since(self._last_activity_ms)
        if idle_ms < SLEEP_COUNTDOWN_DELAY_MS:
            self._sleep_countdown_active = False
            return

        self._sleep_countdown_active = True
        sleep_after_ms = (SLEEP_COUNTDOWN_DELAY_MS +
                          self.sleep_timeout_seconds * 1000)
        if idle_ms >= sleep_after_ms:
            self._enter_display_sleep()

    def _is_audio_priority_active(self):
        """对讲音频收发期间延后非关键采样、重绘和GC。"""
        if self._poc_client is None:
            return False
        controller = getattr(self._poc_client, "audio_controller", None)
        if controller is None:
            return False
        checker = getattr(controller, "is_audio_priority_active", None)
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _process_shoulder_lamp(self):
        """驱动肩灯状态机；LCD息屏时仍继续运行。"""
        if self._shoulder_controller is None:
            return
        try:
            self._shoulder_controller.tick()
            actual_mode = self._shoulder_controller.get_mode()
            if actual_mode != self.shoulder_mode:
                self.shoulder_mode = actual_mode
                self._shoulder_refresh_pending = True
            if (self._shoulder_refresh_pending and
                    not self._is_audio_priority_active() and
                    not self._screen_sleeping and
                    not self._pending_display_wake and
                    self.current is not None and
                    self.current[0] == "shoulder"):
                self._sync_shoulder_mode_ui()
            self._shoulder_error_reported = False
        except Exception as error:
            # I2C异常不应中断 LVGL 主循环；同一故障只打印一次，避免刷屏。
            if not self._shoulder_error_reported:
                print("[肩灯] ET6312B 更新失败：{}".format(error))
                self._shoulder_error_reported = True

    def _process_battery_monitor(self):
        """非阻塞更新电量；tick() 内部负责启动快采样和 1 秒节流。"""
        if (self._battery_monitor is None or
                self._is_audio_priority_active()):
            return
        try:
            percent = self._battery_monitor.tick()
            sample_error = self._battery_monitor.last_error
            if sample_error is not None:
                if not self._battery_error_reported:
                    print("[电池] ADC0 采样失败：{}".format(sample_error))
                    self._battery_error_reported = True
                return
            if percent is not None:
                # 仅在整数百分比变化时刷新状态栏，减少 LVGL 重绘。
                self.update_status(battery=percent)
            self._battery_error_reported = False
        except Exception as error:
            # ADC 短暂异常不能中断显示、触摸和肩灯状态机。
            if not self._battery_error_reported:
                print("[电池] ADC0 采样失败：{}".format(error))
                self._battery_error_reported = True

    def _process_network_monitor(self):
        """轮询网络缓存；信号图标和网络页分别按可见状态刷新。"""
        if (self._network_monitor is None or
                self._poc_call_active or self._is_audio_priority_active()):
            return
        try:
            snapshot = self._network_monitor.tick()
            if snapshot is None:
                return
            self._network_snapshot = snapshot

            if (self._screen_sleeping or self._pending_display_wake or
                    self._back_transition_active()):
                # 息屏/页面动画期间只缓存，亮屏或动画结束后统一刷新。
                self._status_refresh_pending = True
                self._network_refresh_pending = True
                return

            # 状态栏图标在所有亮屏页面都可见；网络页标签只在网络页刷新。
            self._refresh_status()
            self._status_refresh_pending = False
            if (self.current is None or self.current[0] != "network"):
                return
            self._refresh_network_page()
            self._network_refresh_pending = False
            self._network_error_reported = False
        except Exception as error:
            if not self._network_error_reported:
                print("[网络] 状态更新失败：{}".format(error))
                self._network_error_reported = True

    def _process_login_message(self):
        """非阻塞隐藏登录提示，失败提示固定显示2秒。"""
        if self._login_message_until_ms is None:
            return
        now = utime.ticks_ms()
        try:
            expired = utime.ticks_diff(
                now, self._login_message_until_ms) >= 0
        except Exception:
            expired = now >= self._login_message_until_ms
        if expired:
            self._hide_login_message()

    def _poc_call_type(self, group_id, snapshot=None):
        """根据当前普通组和单呼临时组编号返回弹框通话类型。"""
        snapshot = snapshot or self._poc_snapshot or {}
        try:
            numeric = int(group_id) & 0xFFFFFFFF
        except Exception:
            return "组呼"
        single_id = snapshot.get("single_call_group_id")
        if single_id is not None:
            try:
                if numeric == (int(single_id) & 0xFFFFFFFF):
                    return "单呼"
            except Exception:
                pass
        group_id_now = snapshot.get("confirmed_group_id")
        if group_id_now is not None:
            try:
                if numeric == (int(group_id_now) & 0xFFFFFFFF):
                    return "组呼"
            except Exception:
                pass
        for item in snapshot.get("groups") or []:
            try:
                if numeric == (int(item.get("id")) & 0xFFFFFFFF):
                    return "组呼"
            except Exception:
                pass
        return "组呼"

    def _poc_event_group_key(self, group_id):
        try:
            return int(group_id) & 0xFFFFFFFF
        except Exception:
            return group_id

    def _poc_has_visible_remote(self, snapshot):
        for item in snapshot.get("occupied_groups") or []:
            group_id = item.get("group_id")
            if not self._poc_is_known_call_group(group_id, snapshot):
                continue
            key = self._poc_event_group_key(group_id)
            if key not in self._poc_remote_expired:
                return True
        return False

    def _poc_is_known_call_group(self, group_id, snapshot):
        """只接受当前已加入组呼或当前单呼临时组的麦权事件。"""
        try:
            numeric = int(group_id) & 0xFFFFFFFF
        except Exception:
            return False
        for key in ("confirmed_group_id", "single_call_group_id"):
            value = snapshot.get(key)
            if value is not None:
                try:
                    if numeric == (int(value) & 0xFFFFFFFF):
                        return True
                except Exception:
                    pass
        return False

    def _consume_poc_floor_event(self, snapshot):
        """消费一次性麦权事件，并保存弹框计时所需的最小状态。"""
        revision = snapshot.get("floor_event_revision")
        if revision is None or revision == self._poc_floor_event_seen:
            return
        self._poc_floor_event_seen = revision
        event = snapshot.get("floor_event") or {}
        event_type = event.get("event")
        group_key = self._poc_event_group_key(event.get("group_id"))
        now = utime.ticks_ms()

        if event_type == "remote_occupied":
            self._poc_remote_started[group_key] = now
            self._poc_remote_expired.pop(group_key, None)
            self.notify_activity()
        elif event_type == "remote_idle":
            self._poc_remote_started.pop(group_key, None)
            self._poc_remote_expired.pop(group_key, None)
        elif event_type == "self_granted":
            if self._poc_self_hidden_group == group_key:
                self._poc_self_hidden_group = None
            self.notify_activity()
        elif event_type == "self_released":
            self._poc_self_hidden_group = group_key
        elif event_type == "request_failed":
            # 远端占麦弹框优先，显示期间不再叠加抢麦失败提示。
            if not self._poc_has_visible_remote(snapshot):
                try:
                    self._poc_failure_until_ms = utime.ticks_add(
                        now, POC_FAILURE_DURATION_MS)
                except Exception:
                    self._poc_failure_until_ms = now + POC_FAILURE_DURATION_MS
                self.notify_activity()

        if (event_type in ("remote_occupied", "remote_idle",
                           "self_granted", "self_released",
                           "request_failed") and
                self.current is not None and self.current[0] != "login"):
            # 下一轮task_handler必须提交该事件对应的显示或隐藏状态。
            self._poc_popup_frame_pending_revision = revision
            self._poc_popup_frame_pending_ms = now

    def _poc_time_reached(self, deadline_ms):
        if deadline_ms is None:
            return False
        now = utime.ticks_ms()
        try:
            return utime.ticks_diff(now, deadline_ms) >= 0
        except Exception:
            return now >= deadline_ms

    def _show_poc_notice(self, text, duration_ms=2000):
        """在当前非登录页面显示一次定时业务提示。"""
        self._poc_notice_text = str(text)
        try:
            self._poc_notice_until_ms = utime.ticks_add(
                utime.ticks_ms(), int(duration_ms))
        except Exception:
            self._poc_notice_until_ms = utime.ticks_ms() + int(duration_ms)
        self.notify_activity()

    def _show_poc_popup(self, mode, line1, line2, color):
        if self._poc_popup is None:
            return
        signature = (mode, str(line1), str(line2), int(color))
        if (signature == self._poc_popup_signature and
                self._poc_popup_visible and self.current is not None and
                self.current[0] != "login"):
            # 内容没有变化时只保持层级，不重复失效整个弹框区域。
            # 弹框高于60行显示缓冲，反复失效会造成上下分块重绘。
            return
        # 先隐藏再批量更新，避免两个标签和背景分别触发可见的局部刷新。
        if self._poc_popup_visible:
            try:
                self._poc_popup.add_flag(lv.obj.FLAG.HIDDEN)
            except Exception:
                pass
        self._poc_popup_line1.set_text(str(line1))
        self._poc_popup_line2.set_text(str(line2))
        self._poc_popup_line1.set_pos(14, 38 if not line2 else 17)
        self._poc_popup_line2.set_pos(14, 58)
        self._poc_popup.set_style_bg_color(
            lv.color_hex(_lv_color(color)),
            lv.PART.MAIN | lv.STATE.DEFAULT)
        self._poc_popup_mode = mode
        self._poc_popup_signature = signature
        # 登录页禁止显示，但保留真实对讲状态，进入首页后可立即恢复。
        if self.current is not None and self.current[0] == "login":
            self._poc_popup.add_flag(lv.obj.FLAG.HIDDEN)
            self._poc_popup_visible = False
            return
        self._poc_popup.clear_flag(lv.obj.FLAG.HIDDEN)
        self._poc_popup_visible = True
        try:
            self._poc_popup.move_foreground()
        except Exception:
            pass
        try:
            self._poc_popup.invalidate()
        except Exception:
            pass

    def _hide_poc_popup(self):
        if not self._poc_popup_visible and self._poc_popup_mode is None:
            return
        if self._poc_popup is not None:
            self._poc_popup.add_flag(lv.obj.FLAG.HIDDEN)
        self._poc_popup_visible = False
        self._poc_popup_mode = None
        self._poc_popup_signature = None

    def _confirm_poc_popup_frame(self):
        """LVGL提交紧急弹框首帧后，向RTP线程返回对应事件序号。"""
        revision = self._poc_popup_frame_pending_revision
        if revision is None:
            return
        pending_elapsed = (0 if self._poc_popup_frame_pending_ms is None else
                           self._ticks_since(
                               self._poc_popup_frame_pending_ms))
        if self._screen_sleeping or self._pending_display_wake:
            if pending_elapsed >= POC_POPUP_FRAME_MAX_WAIT_MS:
                self._poc_popup_frame_pending_revision = None
                self._poc_popup_frame_pending_ms = None
            return
        if pending_elapsed < POC_POPUP_FRAME_SUBMIT_MS:
            return
        self._poc_popup_frame_pending_revision = None
        self._poc_popup_frame_pending_ms = None
        full_refresh_after_popup = self._display_wake_full_refresh_pending
        self._display_wake_full_refresh_pending = False
        if self._poc_client is None:
            if full_refresh_after_popup:
                self._invalidate_root_screen()
            return
        callback = getattr(self._poc_client, "notify_floor_ui_ready", None)
        if callback is None:
            if full_refresh_after_popup:
                self._invalidate_root_screen()
            return
        try:
            callback(revision)
        except Exception as error:
            print("[POC] 弹框首帧确认失败：{}".format(error))
        if full_refresh_after_popup:
            self._invalidate_root_screen()

    def _process_poc_popup(self, force=False):
        """按组呼优先级显示全局讲话弹框，并处理失败及远端超时。"""
        snapshot = self._poc_snapshot or {}
        now = utime.ticks_ms()
        if (not force and self._poc_popup_last_process_ms is not None and
                self._ticks_since(self._poc_popup_last_process_ms) <
                POC_POPUP_PROCESS_INTERVAL_MS):
            return
        self._poc_popup_last_process_ms = now
        occupied = snapshot.get("occupied_groups") or []
        remote_candidates = []
        active_remote_keys = {}
        for item in occupied:
            group_id = item.get("group_id")
            if not self._poc_is_known_call_group(group_id, snapshot):
                continue
            key = self._poc_event_group_key(group_id)
            active_remote_keys[key] = True
            started = self._poc_remote_started.get(key)
            if started is None:
                started = now
                self._poc_remote_started[key] = started
            if (key not in self._poc_remote_expired and
                    self._ticks_since(started) >= POC_REMOTE_MAX_DURATION_MS):
                self._poc_remote_expired[key] = True
            if key in self._poc_remote_expired:
                continue
            call_type = self._poc_call_type(group_id, snapshot)
            remote_candidates.append((
                2 if call_type == "组呼" else 1,
                call_type, group_id,
                str(item.get("police_name") or "未知警员")))
        # 空闲包到达后从快照消失，同时回收本地超时记录。
        for key in list(self._poc_remote_started.keys()):
            if key not in active_remote_keys:
                self._poc_remote_started.pop(key, None)
                self._poc_remote_expired.pop(key, None)

        self_candidate = None
        held_group = snapshot.get("held_floor_group_id")
        held_key = self._poc_event_group_key(held_group)
        if (held_group is not None and
                held_key != self._poc_self_hidden_group):
            call_type = self._poc_call_type(held_group, snapshot)
            self_candidate = (
                2 if call_type == "组呼" else 1,
                call_type, held_group)
        elif held_group is None:
            self._poc_self_hidden_group = None

        # 组呼优先于单呼；同一类型下远端占麦优先显示。
        best_remote = None
        for candidate in remote_candidates:
            if best_remote is None or candidate[0] > best_remote[0]:
                best_remote = candidate
        request_pending = snapshot.get("floor_state") in (
            "request_waiting", "requesting")
        # 远端弹框超过2分钟后会隐藏；此时即使协议层尚未收到空闲/超时包，
        # 也允许息屏计时重新开始，避免异常占麦状态长期阻塞息屏。
        call_active = bool(best_remote is not None or
                           self_candidate is not None or request_pending)
        if call_active and not self._poc_call_active:
            self.notify_activity()
        elif not call_active and self._poc_call_active:
            # 对讲结束后从当前时刻重新开始“等待3秒+息屏时间”。
            self._last_activity_ms = now
            self._sleep_countdown_active = False
        self._poc_call_active = call_active

        failure_active = False
        if self._poc_failure_until_ms is not None:
            if self._poc_time_reached(self._poc_failure_until_ms):
                self._poc_failure_until_ms = None
            else:
                failure_active = True
        notice_active = False
        if self._poc_notice_until_ms is not None:
            if self._poc_time_reached(self._poc_notice_until_ms):
                self._poc_notice_until_ms = None
                self._poc_notice_text = None
            else:
                notice_active = True
        self._set_poc_touch_locked(
            call_active or failure_active or notice_active)

        if notice_active:
            self._show_poc_popup(
                "notice", self._poc_notice_text or "退出失败", "",
                POC_POPUP_RED)
            return

        if (best_remote is not None and
                (self_candidate is None or
                 best_remote[0] >= self_candidate[0])):
            self._show_poc_popup(
                "remote", best_remote[1],
                "{}：正在讲话中... ...".format(best_remote[3]),
                POC_POPUP_RED)
            return
        if self_candidate is not None:
            self._show_poc_popup(
                "self", self_candidate[1], "正在讲话中... ...",
                POC_POPUP_GREEN)
            return

        if failure_active:
            self._show_poc_popup(
                "failure", "抢麦失败!!!", "", POC_POPUP_RED)
            return
        self._hide_poc_popup()

    def _apply_intercom_data(self, groups, people):
        """保存HTTP名单；默认组由TCP入组成功后再确认。"""
        self._intercom_groups = list(groups or [])
        self._intercom_people = list(people or [])
        valid_group_keys = [str(item.get("raw_id", ""))
                            for item in self._intercom_groups]
        if (self.selected_group != "__none_group__" and
                self.selected_group not in valid_group_keys):
            self.selected_group = "__none_group__"
        if self._pending_group not in valid_group_keys:
            self._pending_group = None
        if not valid_group_keys:
            self.selected_group = "__none_group__"

        selected_person_available = False
        if self.selected_person is not None:
            for item in self._intercom_people:
                if self._person_key(item) == self.selected_person:
                    selected_person_available = item.get("online") is True
                    break
            refresh_succeeded = (
                (self._poc_snapshot or {}).get("http_state") == "success")
            if not selected_person_available and refresh_succeeded:
                # 成功刷新后当前对象离线（或已不在名单）时，已有临时组必须解散。
                if (self._single_group_id is not None and
                        self._pending_person is None and
                        self._poc_client is not None):
                    try:
                        if self._poc_client.request_select_single(None, None):
                            self._pending_person = self._single_none_key
                            print("[对讲机] 当前单呼对象已离线，自动解散单呼")
                    except Exception as error:
                        print("[对讲机] 离线单呼自动解散提交失败：{}".format(
                            error))
                elif self._single_group_id is None:
                    self.selected_person = None
                    self._pending_person = None
        self._populate_group_rows()
        self._populate_single_rows()
        self._sync_intercom_selection_ui()

    def _queue_intercom_data(self, groups, people):
        """保存最新HTTP结果；相同结果不重复分配或重建控件。"""
        groups = list(groups or [])
        people = list(people or [])
        if (groups == self._intercom_groups and
                people == self._intercom_people):
            self._pending_intercom_data = None
            self._poc_http_data_applied = True
            return
        pending = self._pending_intercom_data
        if (pending is not None and pending[0] == groups and
                pending[1] == people):
            return
        # 只保留最后一次HTTP结果；连续进入页面时旧结果不会覆盖新结果。
        self._pending_intercom_data = (groups, people)

    def _process_pending_intercom_data(self):
        """在非对讲阶段应用名单，保证RTP和紧急弹框优先。"""
        pending = self._pending_intercom_data
        if pending is None:
            return
        if (self._poc_call_active or self._is_audio_priority_active() or
                self._poc_popup_frame_pending_revision is not None):
            return
        self._pending_intercom_data = None
        self._apply_intercom_data(pending[0], pending[1])
        self._poc_http_data_applied = True

    def _process_poc_client(self):
        """在LVGL主线程中消费POC快照并更新登录页或名单页面。"""
        if self._poc_client is None:
            self._process_poc_popup()
            self._process_pending_intercom_data()
            return
        try:
            snapshot = self._poc_client.get_snapshot_if_changed()
            if snapshot is None:
                self._process_poc_popup()
                self._process_pending_intercom_data()
                return
            self._poc_snapshot = snapshot
            logout_revision = snapshot.get("logout_revision")
            if logout_revision is None:
                logout_revision = (snapshot.get("logout_state"),
                                   snapshot.get("logout_error"))
            if logout_revision != self._logout_revision_seen:
                self._logout_revision_seen = logout_revision
                logout_state = snapshot.get("logout_state")
                logout_error = snapshot.get("logout_error")
                if logout_state == "success":
                    self._force_login_prompt = False
                    self._login_home_loaded = False
                    if (self.current is not None and
                            self.current[0] != "login"):
                        self.show_login()
                    if logout_error == "设备已被强制退出":
                        self.notify_activity()
                        self._show_login_message(
                            "设备已被强制退出", RED,
                            LOGIN_MESSAGE_DURATION_MS)
                elif logout_state == "failed":
                    # 主动退出失败不切页面，只在当前页面提示2秒。
                    self._show_poc_notice(
                        "退出失败", LOGIN_MESSAGE_DURATION_MS)
            self._consume_poc_floor_event(snapshot)
            # 麦权弹框是音频起播的前置条件，必须先于HTTP名单控件更新。
            self._process_poc_popup(True)
            state = snapshot.get("login_state")
            if state in ("waiting_tcp", "logging_in"):
                if self.current is not None and self.current[0] == "login":
                    text = ("等待网络" if state == "waiting_tcp"
                            else "登录中")
                    self._show_login_message(
                        text, LOGIN_SELECTED_BG, None)
            elif state == "failed":
                if self.current is not None and self.current[0] == "login":
                    result = snapshot.get("login_result")
                    login_error = str(snapshot.get("login_error") or "")
                    if (result == 1 and
                            login_error == "警号已登录，等待确认强制登录"):
                        self._force_login_prompt = True
                        self._show_login_message(
                            "警号已登录:强制登录?", RED, None)
                    elif login_error == "无法转机登录":
                        self._force_login_prompt = False
                        self._show_login_message(
                            "无法转机登录", RED,
                            LOGIN_MESSAGE_DURATION_MS)
                    elif login_error.startswith("转机登录失败"):
                        self._force_login_prompt = False
                        self._show_login_message(
                            "转机登录失败", RED,
                            LOGIN_MESSAGE_DURATION_MS)
                    elif result == 3:
                        self._force_login_prompt = False
                        self._show_login_message("设备ID未注册", RED,
                                                 LOGIN_MESSAGE_DURATION_MS)
                    elif result == 4:
                        self._force_login_prompt = False
                        self._show_login_message("警号未注册", RED,
                                                 LOGIN_MESSAGE_DURATION_MS)
                    elif result == 5:
                        self._force_login_prompt = False
                        self._show_login_message("登录失败", RED,
                                                 LOGIN_MESSAGE_DURATION_MS)
                    else:
                        self._force_login_prompt = False
                        self._show_login_message("登录失败", RED,
                                                 LOGIN_MESSAGE_DURATION_MS)
            elif state == "success" and not self._login_home_loaded:
                self._hide_login_message()
                self._login_home_loaded = True
                self.show_home()

            if snapshot.get("http_state") in ("success", "failed"):
                groups = snapshot.get("groups") or []
                people = snapshot.get("people") or []
                self._queue_intercom_data(groups, people)
            self._apply_join_snapshot(snapshot)
            self._apply_single_snapshot(snapshot)
            self._process_pending_intercom_data()
            self._poc_error_reported = False
        except Exception as error:
            if not self._poc_error_reported:
                print("[POC] UI状态同步失败：{}".format(error))
            self._poc_error_reported = True
            self._process_poc_popup()

    def _process_audio_volume(self):
        """只在LVGL主线程中把物理音量键结果同步到音量页。"""
        if self._poc_client is None:
            return
        controller = getattr(self._poc_client, "audio_controller", None)
        if controller is None:
            return
        try:
            snapshot = controller.get_volume_snapshot()
            revision = snapshot.get("revision")
            if revision == self._volume_revision_seen:
                return
            self._volume_revision_seen = revision
            self.volume = int(snapshot.get("value", self.volume))
            if self._volume_slider is not None:
                self._volume_slider.set_value(self.volume, lv.ANIM.OFF)
            if self._volume_value_label is not None:
                self._volume_value_label.set_text(str(self.volume))
        except Exception as error:
            print("[音量] UI同步失败：{}".format(error))

    def set_volume(self, value):
        """公开设置0~11音量，LCD和物理按键使用同一音频对象。"""
        value = max(VOLUME_MIN, min(VOLUME_MAX, int(value)))
        controller = (getattr(self._poc_client, "audio_controller", None)
                      if self._poc_client is not None else None)
        if controller is not None:
            value = controller.set_volume(value)
        self.volume = value
        if self._volume_value_label is not None:
            self._volume_value_label.set_text(str(value))
        return value

    def _apply_join_snapshot(self, snapshot):
        """把后台入组状态同步到UI；失败时恢复上一个已确认组。"""
        state = snapshot.get("join_state")
        revision = snapshot.get("join_revision")
        # 用独立版本号识别每次入组结果；状态字符串可能连续两次都是success。
        if state is None or revision == self._join_revision_seen:
            return
        self._join_revision_seen = revision
        self._join_state_seen = state
        if state == "success":
            confirmed = snapshot.get("confirmed_group_raw_id")
            if confirmed is not None:
                # 入组成功可能先于HTTP名单控件应用；先保存确认的原始组号，
                # 待名单到达后再由行控件同步选中状态。
                confirmed = str(confirmed)
                self.selected_group = confirmed
                # 后续切换失败时，以最近一次TCP确认成功的小组作为回滚基线。
                self._join_previous_group = confirmed
            self._pending_group = None
            self._sync_intercom_selection_ui()
        elif state == "failed":
            # 待确认行回滚到原先确认状态；首次失败则全部不选中。
            self._pending_group = None
            self.selected_group = (self._join_previous_group or
                                   "__none_group__")
            self._sync_intercom_selection_ui()
        elif (state == "idle" and
              snapshot.get("confirmed_group_id") is None):
            self._pending_group = None
            self._join_previous_group = None
            self.selected_group = "__none_group__"
            self._sync_intercom_selection_ui()

    def _apply_single_snapshot(self, snapshot):
        """根据0x84/0x86最终结果同步单呼选择，失败时显示真实会话状态。"""
        state = snapshot.get("single_state")
        revision = snapshot.get("single_revision")
        if state is None or revision == self._single_revision_seen:
            return
        self._single_revision_seen = revision
        self._single_state_seen = state
        confirmed = snapshot.get("single_call_person_key")
        self._single_group_id = snapshot.get("single_call_group_id")
        valid = [self._person_key(item) for item in self._intercom_people]
        self.selected_person = confirmed if confirmed in valid else None
        if state in ("invite_waiting", "inviting", "invite_accepted",
                     "dissolve_waiting", "dissolving"):
            target = snapshot.get("single_target_key")
            self._pending_person = (target if target in valid else
                                    self._single_none_key)
        else:
            # 成功、失败或空闲都以客户端保存的真实临时组状态为准。
            self._pending_person = None
        self._sync_intercom_selection_ui()

    def _get_style(self, font, color, align=lv.TEXT_ALIGN.CENTER):
        """创建并缓存共享字体样式，避免切换页面时重复分配样式内存。"""
        key = (font[0], font[1], color, align)
        style = self._styles.get(key)
        if style is None:
            style = _style(font, color, align)
            self._styles[key] = style
        return style

    def _map_touch_point(self, x, y):
        """把 CST816D 的竖屏原始坐标转换为 296x240 横屏坐标。"""
        if TOUCH_POINT_NEEDS_ROTATION:
            x, y = LCD_WIDTH - 1 - y, x
        x = max(0, min(LCD_WIDTH - 1, int(x)))
        y = max(0, min(LCD_HEIGHT - 1, int(y)))
        return x, y

    def _read_indev_point(self, indev):
        """兼容成员函数和全局函数两种 LVGL 触点读取形式。"""
        if indev is None:
            return None
        if self._pointer_point is None:
            try:
                self._pointer_point = lv.point_t()
            except Exception:
                return None
        try:
            try:
                indev.get_point(self._pointer_point)
            except Exception:
                lv.indev_get_point(indev, self._pointer_point)
            if self._lvgl_input_rotated:
                # get_point() 返回的是 LVGL 已完成旋转后的逻辑坐标，
                # 再旋转一次会把左侧触摸点映射到屏幕外。
                point = (
                    max(0, min(LCD_WIDTH - 1, int(self._pointer_point.x))),
                    max(0, min(LCD_HEIGHT - 1, int(self._pointer_point.y))))
            else:
                point = self._map_touch_point(
                    self._pointer_point.x, self._pointer_point.y)
            self._touch_point_cache = point
            return point
        except Exception:
            return None

    def _read_tp_xy(self):
        """使用 CST816D 官方公开 read_xy 接口读取当前触点坐标。

        tp.read 是传给 LVGL 的 CST816_read 对象，不能主动调用；read_xy
        则是官方示例用于调试/读取坐标的普通接口。这里仅在 LVGL 坐标
        查询不可用时调用，避免每次 LVGL 轮询都额外访问 I2C。
        """
        try:
            reader = getattr(self.tp, "read_xy", None)
            if reader is None:
                return None
            result = reader()
            x = y = None
            if isinstance(result, dict):
                x = result.get("x")
                y = result.get("y")
            elif isinstance(result, (tuple, list)):
                if (len(result) >= 2 and
                        not isinstance(result[0], (tuple, list, dict))):
                    x, y = result[0], result[1]
                elif len(result) == 1 and isinstance(result[0], (tuple, list)):
                    value = result[0]
                    if len(value) >= 2:
                        x, y = value[0], value[1]
            if x is None or y is None:
                return None
            point = self._map_touch_point(x, y)
            self._touch_point_cache = point
            return point
        except Exception:
            return None

    def _get_pointer_point(self, event=None):
        """读取当前触点；必要时使用 CST816D 的公开 read_xy 接口。"""
        # 事件中携带的输入设备最可靠，不依赖全局“当前输入设备”接口。
        if event is not None:
            try:
                point = self._read_indev_point(event.get_indev())
                if point is not None:
                    return point
            except Exception:
                try:
                    point = self._read_indev_point(
                        lv.event_get_indev(event))
                    if point is not None:
                        return point
                except Exception:
                    pass

        # 兼容导出全局接口的 LVGL 固件。
        try:
            point = self._read_indev_point(lv.indev_get_act())
            if point is not None:
                return point
        except Exception:
            pass

        # 最后尝试初始化时保存的输入设备对象。
        try:
            point = self._read_indev_point(self._touch_input)
            if point is not None:
                return point
        except Exception:
            pass

        # EC800M 某些固件不向 MicroPython 导出 LVGL 的坐标读取接口，
        # 但官方 cst816 示例仍可通过 read_xy() 获取连续坐标。
        point = self._read_tp_xy()
        return point if point is not None else self._touch_point_cache

    def _click_move_limit(self):
        """返回当前页面的点击/滑动分界，设置页优先识别为滑动。"""
        if (self.current is not None and
                self.current[0] in (
                    "settings", "sleep", "group_mode", "single_mode")):
            return SETTINGS_CLICK_MOVE_LIMIT
        return CLICK_MOVE_LIMIT

    def _is_root_page(self):
        """首页和登录页都没有上一级，禁止右滑返回。"""
        return (self.current is None or
                self.current[0] in ("home", "login"))

    def _can_drag_back(self):
        """仅允许有上一级页面的非首页执行右滑返回。"""
        return (not self._back_animating and self.current is not None and
                not self._is_root_page() and bool(self.stack))

    def _clear_back_drag_state(self):
        """清除一次右滑跟踪状态，不改变任何页面的显示位置。"""
        self._back_drag_tracking = False
        self._back_drag_active = False
        self._back_drag_start = None
        self._back_drag_started_ms = None
        self._back_drag_offset = 0
        self._back_drag_target_offset = 0
        self._back_drag_frame_pending = False
        self._back_drag_last_frame_ms = 0
        self._back_drag_current = None
        self._back_drag_previous = None
        self._back_drag_hardware_confirmed = False

    def _back_drag_press(self, point):
        """记录按下点，为可能发生的右滑返回做准备。"""
        if self._back_animating:
            return
        # 页面和菜单行都可能收到同一次 PRESSED 事件；重复收到时保留
        # 第一次按下点，避免快速滑动被重复初始化而丢失位移。
        if (self._back_drag_tracking and
                self._back_drag_current is self.current):
            return
        self._clear_back_drag_state()
        self._pending_back = False
        if point is None or not self._can_drag_back():
            return
        self._back_drag_tracking = True
        self._back_drag_start = point
        self._back_drag_started_ms = utime.ticks_ms()
        self._back_drag_current = self.current
        self._back_drag_previous = self.stack[-1]

    def _activate_back_drag(self):
        """显示上一级页面，并建立当前页在上、上一级页面在下的层级。"""
        if self._back_drag_active:
            return True
        if (self._back_drag_current is None or
                self._back_drag_previous is None):
            return False
        current_page = self._back_drag_current[1]
        previous_page = self._back_drag_previous[1]
        self._set_page_visible(previous_page, True)
        # 上一级页面固定在原位，只移动当前页面，减少一半位置更新。
        previous_page.set_x(0)
        current_page.set_x(0)
        try:
            # 页面可能被重复使用，显式放到最前面可以保持正确遮挡关系。
            current_page.move_foreground()
        except Exception:
            pass
        if (self._poc_popup_visible and self._poc_popup is not None and
                self.current is not None and self.current[0] != "login"):
            try:
                self._poc_popup.move_foreground()
            except Exception:
                pass
        self._back_drag_active = True
        self._back_drag_offset = 0
        self._back_drag_target_offset = 0
        self._back_drag_frame_pending = False
        self._back_drag_last_frame_ms = utime.ticks_ms()
        self._touch_sequence_swipe = True
        if not self._back_drag_ready_reported:
            print("[触摸] 跟手右滑已启用")
            self._back_drag_ready_reported = True
        return True

    def _set_back_drag_offset(self, offset):
        """只移动当前页面；上一级页面始终固定在屏幕原位。"""
        if self._back_drag_current is None:
            return
        offset = max(0, min(LCD_WIDTH, int(offset)))
        if offset == self._back_drag_offset:
            return
        self._back_drag_offset = offset
        self._back_drag_current[1].set_x(offset)

    def _queue_back_drag_offset(self, offset):
        """只保存最新触摸位移，等待主循环按固定帧间隔提交。"""
        offset = max(0, min(LCD_WIDTH, int(offset)))
        self._back_drag_target_offset = offset
        self._back_drag_frame_pending = (
            offset != self._back_drag_offset)

    def _process_back_drag_frame(self, force=False):
        """每20ms最多应用一次最新触摸位置，合并过密的PRESSING事件。"""
        if (not self._back_drag_active or self._back_animating or
                not self._back_drag_frame_pending):
            return False
        now = utime.ticks_ms()
        if (not force and self._back_drag_last_frame_ms and
                self._ticks_since(self._back_drag_last_frame_ms) <
                BACK_FRAME_INTERVAL_MS):
            return False
        target = self._back_drag_target_offset
        self._back_drag_frame_pending = False
        self._back_drag_last_frame_ms = now
        self._set_back_drag_offset(target)
        return True

    def _back_drag_move(self, point):
        """处理按住移动；确认横向右滑后才开始移动整个页面。"""
        if (not self._back_drag_tracking or point is None or
                self._back_drag_start is None):
            return False
        if self.current is not self._back_drag_current:
            self._clear_back_drag_state()
            return False

        delta_x = point[0] - self._back_drag_start[0]
        delta_y = point[1] - self._back_drag_start[1]
        if not self._back_drag_active:
            # 明显的纵向手势不参与返回，避免上下滑时页面发生横向抖动。
            if (abs(delta_y) > CLICK_MOVE_LIMIT and
                    (delta_x <= 0 or
                     delta_x * BACK_DIRECTION_DENOMINATOR <
                     abs(delta_y) * BACK_DIRECTION_NUMERATOR)):
                self._clear_back_drag_state()
                return False
            if delta_x <= BACK_DRAG_START_DISTANCE:
                return False
            if (delta_x * BACK_DIRECTION_DENOMINATOR <
                    abs(delta_y) * BACK_DIRECTION_NUMERATOR):
                return False
            if not self._activate_back_drag():
                return False

        self._queue_back_drag_offset(delta_x)
        return True

    def _start_back_linear_animation(self, end, complete,
                                     minimum_ms, maximum_ms):
        """从当前显示位置启动纯线性动画，不使用缓入或缓出曲线。"""
        if not self._back_drag_active:
            return False
        start = self._back_drag_offset
        end = max(0, min(LCD_WIDTH, int(end)))
        distance = abs(end - start)
        self._back_drag_target_offset = end
        self._back_drag_frame_pending = False
        self._back_animating = True
        self._back_anim_complete = bool(complete)
        self._back_anim_finish_pending = False

        if distance == 0:
            self._finish_back_animation()
            return True

        self._back_anim_start_value = start
        self._back_anim_end_value = end
        self._back_anim_duration_ms = (
            int(minimum_ms) +
            (int(maximum_ms) - int(minimum_ms)) * distance // LCD_WIDTH)
        self._back_anim_started_ms = utime.ticks_ms()
        self._back_anim_last_frame_ms = self._back_anim_started_ms
        return True

    def _start_back_rebound(self):
        """松手距离不足时，使用20~70ms线性动画回到原位。"""
        return self._start_back_linear_animation(
            0, False, BACK_REBOUND_MIN_MS, BACK_REBOUND_MAX_MS)

    def _start_back_complete(self):
        """返回成功后，使用60~90ms线性动画补完剩余距离。"""
        return self._start_back_linear_animation(
            LCD_WIDTH, True,
            BACK_COMPLETE_MIN_MS, BACK_COMPLETE_MAX_MS)

    def _process_back_animation(self):
        """在主循环中执行非阻塞线性回弹，避免依赖固件动画绑定。"""
        if not self._back_animating:
            return
        if self._back_anim_finish_pending:
            # 终点位置已经交给上一轮task_handler绘制，本轮再隐藏页面，
            # 确保松手动画不会从中间位置直接跳到上一级页面。
            self._back_anim_finish_pending = False
            self._finish_back_animation()
            return
        now = utime.ticks_ms()
        try:
            elapsed = utime.ticks_diff(now, self._back_anim_started_ms)
            frame_elapsed = utime.ticks_diff(
                now, self._back_anim_last_frame_ms)
        except Exception:
            elapsed = now - self._back_anim_started_ms
            frame_elapsed = now - self._back_anim_last_frame_ms
        elapsed = max(0, elapsed)
        duration = self._back_anim_duration_ms
        if elapsed >= duration:
            self._set_back_drag_offset(self._back_anim_end_value)
            self._back_anim_finish_pending = True
            return

        # 动画和跟手移动使用同一帧率限制，避免生成LCD来不及刷新的无效帧。
        if frame_elapsed < BACK_FRAME_INTERVAL_MS:
            return

        # 纯线性插值，不使用缓入或缓出曲线。
        value = (self._back_anim_start_value +
                 (self._back_anim_end_value -
                  self._back_anim_start_value) * elapsed // duration)
        self._back_anim_last_frame_ms = now
        self._set_back_drag_offset(value)

    def _back_drag_release(self, point):
        """根据松手位移决定完成返回还是回弹。"""
        if not self._back_drag_tracking:
            return False
        # 硬件手势可能在 RELEASED 之后才回调。若此前已经启动回弹，
        # 右滑确认到达时把动画升级为完成返回，避免快速滑动被判失败。
        if self._back_animating:
            if (self._back_drag_hardware_confirmed and
                    not self._back_anim_complete):
                self._start_back_complete()
            return True
        self._back_drag_move(point)
        if (not self._back_drag_active and
                self._back_drag_hardware_confirmed):
            self._activate_back_drag()
        if not self._back_drag_active:
            self._clear_back_drag_state()
            return False

        # 松手时提交本次触摸的最后坐标；阈值和动画起点都以该位置为准。
        self._process_back_drag_frame(force=True)

        fast_flick = False
        if self._back_drag_started_ms is not None:
            try:
                elapsed = utime.ticks_diff(
                    utime.ticks_ms(), self._back_drag_started_ms)
            except Exception:
                elapsed = utime.ticks_ms() - self._back_drag_started_ms
            fast_flick = (0 <= elapsed <= BACK_FLICK_MAX_MS and
                          self._back_drag_target_offset >=
                          BACK_FLICK_DISTANCE)
        complete = (self._back_drag_hardware_confirmed or
                    self._back_drag_target_offset >=
                    BACK_COMMIT_DISTANCE or
                    fast_flick)
        self._touch_sequence_swipe = True
        if complete:
            self._start_back_complete()
        else:
            self._start_back_rebound()
        return True

    def _finish_back_animation(self):
        """动画结束后只切换可见状态，不重新创建或删除页面对象。"""
        complete = self._back_anim_complete
        outgoing = self._back_drag_current
        previous = self._back_drag_previous

        if complete and outgoing is not None and previous is not None:
            # 先隐藏移出的当前页，再复位其坐标，避免复位过程被绘制出来。
            self._set_page_visible(outgoing[1], False)
            self._set_page_visible(previous[1], True)
            outgoing[1].set_x(0)
            previous[1].set_x(0)
            try:
                # 页面长期复用时显式恢复上一级页面的绘制层级。
                previous[1].move_foreground()
            except Exception:
                pass
            if (self._poc_popup_visible and self._poc_popup is not None and
                    previous[0] != "login"):
                try:
                    self._poc_popup.move_foreground()
                except Exception:
                    pass
            if self.stack and self.stack[-1][1] is previous[1]:
                self.stack.pop()
            self.current = previous
            # 页面对象是长期复用的，返回过程没有新分配对象；不在收尾帧
            # 立即执行 gc.collect()，避免回收停顿造成偶发卡顿。
        else:
            if outgoing is not None:
                outgoing[1].set_x(0)
            if previous is not None:
                previous[1].set_x(0)
                self._set_page_visible(previous[1], False)

        # 某些固件只重绘位置变化形成的局部区域，HIDDEN切换后旧页面控件
        # 仍可能在ST7789 GRAM中停留。收尾时强制安排一次完整页面刷新。
        self._invalidate_page_transition(
            outgoing[1] if outgoing is not None else None,
            previous[1] if previous is not None else None)

        self._pending_back = False
        self._back_animating = False
        self._back_anim_complete = False
        self._back_anim_start_value = 0
        self._back_anim_end_value = 0
        self._back_anim_started_ms = 0
        self._back_anim_duration_ms = 0
        self._back_anim_last_frame_ms = 0
        self._back_anim_finish_pending = False
        self._clear_back_drag_state()

    def _start_programmatic_back(self):
        """坐标接口不可用时，收到硬件右滑后播放完整线性返回动画。"""
        if not self._can_drag_back():
            return False
        self._clear_back_drag_state()
        self._back_drag_current = self.current
        self._back_drag_previous = self.stack[-1]
        if not self._activate_back_drag():
            return False
        return self._start_back_complete()

    def _bind_page_swipe(self, page):
        """页面统一接收点击和滑动，避免横屏旋转后的点击落空。"""
        press_state = [None, False]

        def update_movement(event=None):
            point = self._get_pointer_point(event)
            start = press_state[0]
            if point is None or start is None:
                return point
            move_limit = self._click_move_limit()
            if (abs(point[0] - start[0]) > move_limit or
                    abs(point[1] - start[1]) > move_limit):
                press_state[1] = True
            return point

        def page_event(*args):
            event = args[-1]
            try:
                code = event.get_code()
            except Exception:
                code = event

            if self._consume_touch_event(code):
                press_state[0] = None
                press_state[1] = True
                return

            if code == lv.EVENT.PRESSED:
                self._touch_pressed = True
                self._touch_sequence_swipe = False
                self._click_dispatched = False
                # 新触摸序列不得复用上一次点击留下的坐标。
                self._touch_point_cache = None
                point = self._get_pointer_point(event)
                press_state[0] = point
                press_state[1] = False
                self._back_drag_press(point)
            elif code == lv.EVENT.PRESSING:
                self._back_drag_move(update_movement(event))
            elif code == lv.EVENT.GESTURE:
                press_state[1] = True
                self._touch_sequence_swipe = True
            elif code == lv.EVENT.RELEASED:
                self._touch_pressed = False
                if self._back_drag_release(update_movement(event)):
                    press_state[1] = True
            elif code == lv.EVENT.CLICKED:
                self._touch_pressed = False
                point = update_movement(event)
                if (not press_state[1] and
                        not self._touch_sequence_swipe and
                        not self._back_animating and
                        not self._click_dispatched):
                    # 即使 LVGL 按竖屏原始坐标把事件交给了页面空白区，
                    # 仍按转换后的横屏坐标命中实际菜单行。
                    self._click_dispatched = self._dispatch_click(point)
                press_state[0] = None
                press_state[1] = False

        self._event_callbacks.append(page_event)
        try:
            page.add_event_cb(page_event, lv.EVENT.ALL, None)
        except Exception:
            wrapper = lambda target, event: page_event(event)
            self._event_callbacks.append(wrapper)
            page.set_event_cb(wrapper, lv.EVENT.ALL, None)

    def _bind_click(self, page, obj, callback):
        """根据位移区分点击和滑动，并按横屏坐标统一命中。"""
        # 保存控件所属页面，CLICKED 到来时统一按横屏真实坐标重新命中。
        self._click_targets.append((page, obj, callback))
        # 每个可点击对象保存自己的按下起点和移动状态。
        press_state = [None, False]

        def update_movement(event=None):
            point = self._get_pointer_point(event)
            start = press_state[0]
            if point is None or start is None:
                return point
            move_limit = self._click_move_limit()
            if (abs(point[0] - start[0]) > move_limit or
                    abs(point[1] - start[1]) > move_limit):
                press_state[1] = True
            return point

        def pointer_event(*args):
            event = args[-1]
            try:
                code = event.get_code()
            except Exception:
                code = event

            if self._consume_touch_event(code):
                press_state[0] = None
                press_state[1] = True
                return

            if code == lv.EVENT.PRESSED:
                self._touch_pressed = True
                # 新触摸序列先清空旧坐标，CLICKED 只能回退到本次序列缓存。
                self._touch_point_cache = None
                self._click_dispatched = False
                point = self._get_pointer_point(event)
                press_state[0] = point
                press_state[1] = False
                self._touch_sequence_swipe = False
                self._back_drag_press(point)
            elif code == lv.EVENT.PRESSING:
                self._back_drag_move(update_movement(event))
            elif code == lv.EVENT.RELEASED:
                self._touch_pressed = False
                if self._back_drag_release(update_movement(event)):
                    press_state[1] = True
            elif code == lv.EVENT.GESTURE:
                press_state[1] = True
                self._touch_sequence_swipe = True
            elif code == lv.EVENT.CLICKED:
                self._touch_pressed = False
                point = update_movement(event)
                if (not press_state[1] and
                        not self._touch_sequence_swipe and
                        not self._back_animating and
                        not self._click_dispatched):
                    self._click_dispatched = self._dispatch_click(point, obj)
                press_state[0] = None
                press_state[1] = False

        self._event_callbacks.append(pointer_event)
        try:
            obj.add_event_cb(pointer_event, lv.EVENT.ALL, None)
        except Exception:
            # 兼容旧版 QuecPython LVGL 的事件注册形式。
            wrapper = lambda target, event: pointer_event(event)
            self._event_callbacks.append(wrapper)
            obj.set_event_cb(wrapper, lv.EVENT.ALL, None)

    def _gesture(self, *args):
        """将 CST816D 竖屏手势转换为横屏方向后打印并处理。"""
        if not args:
            return
        raw_gesture = args[-1]
        try:
            raw_gesture = int(raw_gesture)
        except Exception:
            pass
        gesture = LANDSCAPE_GESTURE_MAP.get(raw_gesture, raw_gesture)
        names = {
            0: "右滑",
            1: "左滑",
            2: "上滑",
            3: "下滑",
            4: "边缘返回",
            5: "点击",
            6: "返回键",
        }
        name = names.get(gesture, "未知")
        print(name)
        if self._poc_touch_locked:
            self._consume_gesture_for_poc()
            return
        if self._consume_gesture_for_wake():
            return
        if self._consume_gesture_for_poc():
            return
        if gesture in (0, 1, 2, 3, 4, 6):
            # 硬件手势作为坐标位移判断之外的第二道滑动确认。
            self._touch_sequence_swipe = True
        if (gesture in (0, 4, 6) and self.current and
                not self._is_root_page()):
            # 先锁存硬件确认，再由主循环安全地处理页面动画。这样即使
            # 快速滑动没有产生完整的 PRESSING/RELEASED 事件，也不会丢失返回。
            self._back_drag_hardware_confirmed = True
            self._pending_back = True
            if self._back_drag_tracking:
                # 已有坐标跟踪时只做确认，由 RELEASED 决定动画起点。
                pass
            elif not self._back_animating:
                # 个别固件无坐标事件时，在主循环中播放完整返回动画。
                if (gesture == 0 and
                        not self._touch_point_warning_reported):
                    print("[触摸] 未取得连续坐标，仅执行松手返回")
                    self._touch_point_warning_reported = True
                self._pending_back = True

    def _process_pending_back(self):
        """在主循环中执行右滑返回，避免在触摸回调内直接切屏。"""
        if self._pending_back:
            self._pending_back = False
            if (self._back_drag_tracking and self._back_drag_active and
                    self._back_drag_hardware_confirmed):
                # 快速滑动可能没有 RELEASED 事件，使用当前位移直接完成。
                self._back_drag_release(self._get_pointer_point())
            elif (self._back_drag_tracking and
                  self._back_drag_hardware_confirmed and
                  not self._back_animating):
                # 已确认但尚未激活跟手页面时，补齐激活并启动完成动画。
                self._activate_back_drag()
                self._back_drag_release(self._get_pointer_point())
            elif (not self._back_drag_tracking and
                  not self._back_animating and self.current and
                  not self._is_root_page()):
                self._start_programmatic_back()

    def _set_page_visible(self, page, visible):
        """切换页面容器可见状态，兼容 LVGL MicroPython 的对象标志接口。"""
        if visible:
            page.clear_flag(lv.obj.FLAG.HIDDEN)
        else:
            page.add_flag(lv.obj.FLAG.HIDDEN)

    def _invalidate_page_transition(self, *pages):
        """页面切换收尾时请求一次完整重绘，清掉旧页面残留像素。"""
        # 部分固件没有 lv.obj_invalidate()，但对象实例提供 invalidate()。
        # 同时让根屏幕失效，确保隐藏三级页面后旧GRAM区域被新页面覆盖。
        targets = []
        for page in pages:
            if page is not None and page not in targets:
                targets.append(page)
        if (getattr(self, "_root_screen", None) is not None and
                self._root_screen not in targets):
            targets.append(self._root_screen)
        for page in targets:
            try:
                page.invalidate()
            except Exception:
                # 旧版绑定不支持实例invalidate时，位置和隐藏标志仍会生效。
                pass

    def _switch_page(self, key, page, push_history):
        """在同一根屏幕内切换页面，避免反复加载屏幕产生资源碎片。"""
        previous = self.current
        if previous is not None and previous[0] == key:
            return
        if push_history and previous is not None:
            self.stack.append(previous)
        if previous is not None:
            previous[1].set_x(0)
            self._set_page_visible(previous[1], False)
        page.set_x(0)
        self._set_page_visible(page, True)
        self.current = (key, page)
        # 页面容器切到前景后，重新把全局弹框放到最上层；登录页保持隐藏。
        if getattr(self, "_poc_popup", None) is not None:
            if key == "login":
                self._poc_popup.add_flag(lv.obj.FLAG.HIDDEN)
                self._poc_popup_visible = False
            elif self._poc_popup_mode is not None:
                self._poc_popup.clear_flag(lv.obj.FLAG.HIDDEN)
                self._poc_popup_visible = True
                try:
                    self._poc_popup.move_foreground()
                except Exception:
                    pass
        self._pending_gc = True

    def _screen(self, key, title):
        """按页面名称创建一次屏幕；后续进入同一页面时直接复用。"""
        page = self.pages.get(key)
        if page is not None:
            return page, False

        page = lv.obj(self._root_screen)
        # 页面只占状态栏下方区域，避免覆盖根屏幕上的共享状态标签。
        page.set_size(LCD_WIDTH, PAGE_HEIGHT)
        page.set_pos(0, STATUS_HEIGHT)
        _fix_position(page)
        _set_bg(page, BLACK)
        try:
            page.add_flag(lv.obj.FLAG.CLICKABLE)
        except Exception:
            pass
        self._bind_page_swipe(page)
        self._set_page_visible(page, False)
        self.pages[key] = page
        self._page_title(page, title)
        return page, True

    def _battery_color(self):
        """根据电量值返回状态栏颜色；未知或非法数据按白色显示。"""
        try:
            return RED if int(self.status["battery"]) <= 20 else WHITE
        except Exception:
            return WHITE

    def _refresh_network_icon(self):
        """根据最新缓存切换状态栏信号图标；没有SIM或信号失败显示wifi_0。"""
        if self._network_icon is None:
            return
        snapshot = self._network_snapshot or {}
        # ICCID 不存在时，即使底层偶尔返回 dBm，也按无SIM处理。
        if not snapshot.get("iccid"):
            path = NETWORK_ICON_PATHS[None]
        else:
            path = NETWORK_ICON_PATHS.get(snapshot.get("signal_level"),
                                          NETWORK_ICON_PATHS[None])
        if path == self._network_icon_path:
            return
        try:
            self._network_icon.set_src(path)
            self._network_icon_path = path
        except Exception as error:
            if not self._network_error_reported:
                print("[网络] 状态栏图标刷新失败：{}".format(error))
                self._network_error_reported = True

    def _refresh_status(self):
        """原地刷新根屏幕上的网络图标和电量信息。"""
        self._refresh_network_icon()
        self._battery_label.set_text(
            "电量：{}%".format(self.status["battery"]))
        self._battery_label.set_style_text_color(
            lv.color_hex(_lv_color(self._battery_color())),
            lv.PART.MAIN | lv.STATE.DEFAULT)

    def _page_title(self, page, title):
        """在页面内容区顶部创建可选标题。"""
        if title:
            label = lv.label(page)
            label.set_text(title)
            label.set_size(180, 30)
            label.set_pos((LCD_WIDTH - 180) // 2, 4)
            label.add_style(self._get_style(FONT_MAIN, WHITE), lv.PART.MAIN)

    def _settings_title(self, page):
        """创建设置页自己的标题，并在视觉上与状态栏水平对齐。"""
        label = lv.label(page)
        label.set_text("设置")
        label.set_size(120, 24)
        # 设置页使用全屏透明容器，标题直接放在 y=3，和电量标签水平对齐。
        label.set_pos(42, 3)
        label.add_style(self._get_style(
            FONT_SMALL, SETTINGS_TITLE_COLOR, lv.TEXT_ALIGN.LEFT),
            lv.PART.MAIN)
        return label

    def _bind_vertical_scroll_speed(self, content):
        """将纵向菜单的跟手位移放大到约 3 倍，并保持边界限制。"""
        state = [None, False]

        def read_scroll_y():
            try:
                return int(content.get_scroll_y())
            except Exception:
                try:
                    return int(lv.obj_get_scroll_y(content))
                except Exception:
                    return None

        def scroll_by_bounded(delta_y):
            """优先使用 LVGL 有边界版本，避免放大位移越过首尾。"""
            delta_y = int(delta_y)
            if delta_y == 0:
                return False
            try:
                content.scroll_by_bounded(0, delta_y, lv.ANIM.OFF)
                return True
            except Exception:
                try:
                    lv.obj_scroll_by_bounded(
                        content, 0, delta_y, lv.ANIM.OFF)
                    return True
                except Exception:
                    return False

        def on_scroll(*args):
            if state[1]:
                return
            current = read_scroll_y()
            if current is None:
                return
            previous = state[0]
            state[0] = current
            if previous is None:
                return

            # 原生滚动已经移动 1 倍，这里只补充剩余 2 倍，合计约 3 倍。
            delta_y = current - previous
            if delta_y == 0:
                return
            # 设置容器一旦发生真实滚动，本次触摸就不能再派发点击。
            self._touch_sequence_swipe = True
            # 松手后的惯性只由 LVGL 原生路径处理，避免再次放大造成波纹抖动。
            if not self._touch_pressed:
                return
            extra = delta_y * (SETTINGS_SCROLL_MULTIPLIER - 1)
            state[1] = True
            try:
                scroll_by_bounded(extra)
            finally:
                state[1] = False
            latest = read_scroll_y()
            state[0] = current if latest is None else latest

        # 保存回调引用，避免 MicroPython 垃圾回收后 LVGL 调用失效对象。
        self._event_callbacks.append(on_scroll)
        try:
            content.add_event_cb(on_scroll, lv.EVENT.SCROLL, None)
        except Exception:
            # 旧版绑定若不支持新增事件回调，则保持原生 1 倍滚动，避免覆盖已有回调。
            pass

    def _vertical_scroll_area(self, page, y, height):
        """创建快速、无回弹、隐藏滚动条的纵向菜单容器。"""
        content = lv.obj(page)
        content.set_size(LCD_WIDTH, height)
        content.set_pos(0, y)
        # 先清除主题默认的滚动惯性和弹性，再单独开启纵向滚动。
        # 这样拖到顶部/底部后继续拖动会直接停在边界，不会回弹或抖动。
        _fix_position(content)
        try:
            content.add_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass
        try:
            content.set_scroll_dir(lv.DIR.VER)
        except Exception:
            pass
        # 不使用吸附动画，避免松手后出现一帧轻微的上下回弹。
        try:
            content.set_scroll_snap_y(lv.SCROLL_SNAP.NONE)
        except Exception:
            pass
        # 关闭弹性回弹、水平滚动链和吸附；惯性单独保留，用于松手后的自然滑行。
        for flag_name in ("SCROLL_ELASTIC", "SCROLL_CHAIN_HOR",
                          "SCROLL_CHAIN_VER", "SNAPPABLE"):
            try:
                content.clear_flag(getattr(lv.obj.FLAG, flag_name))
            except Exception:
                pass
        try:
            content.add_flag(lv.obj.FLAG.SCROLL_MOMENTUM)
        except Exception:
            pass
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            content.set_style_bg_opa(0, lv.PART.MAIN | state)
            content.set_style_border_width(0, lv.PART.MAIN | state)
            content.set_style_outline_width(0, lv.PART.MAIN | state)
            content.set_style_shadow_width(0, lv.PART.MAIN | state)
            content.set_style_radius(0, lv.PART.MAIN | state)
            try:
                content.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                content.set_style_pad_top(0, lv.PART.MAIN | state)
                content.set_style_pad_bottom(0, lv.PART.MAIN | state)
                content.set_style_pad_left(0, lv.PART.MAIN | state)
                content.set_style_pad_right(0, lv.PART.MAIN | state)
        self._bind_page_swipe(content)
        self._bind_vertical_scroll_speed(content)
        self._scroll_containers.append(content)
        return content

    def _tile(self, page, x, y, width, height, text, callback, selected=False):
        """创建可点击的圆角按钮占位；后续可在按钮内替换为图标图片。"""
        tile = lv.btn(page)
        tile.set_size(width, height)
        tile.set_pos(x, y)
        _fix_position(tile)
        _set_bg(tile, GREEN if selected else GRAY)
        label = lv.label(tile)
        label.set_text(text)
        label.set_size(width, height)
        label.set_pos(0, 0)
        label.add_style(self._get_style(FONT_MAIN, WHITE), lv.PART.MAIN)
        self._bind_click(page, tile, callback)
        return tile

    def _settings_row(self, page, parent, y, icon_path, text, callback):
        """创建设置页纵向菜单行：左侧 30px 图标，右侧大号中文文字。"""
        row = lv.obj(parent)
        try:
            row.add_flag(lv.obj.FLAG.CLICKABLE)
        except Exception:
            pass
        row.set_size(LCD_WIDTH - 8, SETTINGS_ROW_HEIGHT)
        row.set_pos(4, y)
        _fix_position(row)

        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            row.set_style_bg_opa(255, lv.PART.MAIN | state)
            row.set_style_bg_color(
                lv.color_hex(_lv_color(SETTINGS_ROW_BG)),
                lv.PART.MAIN | state)
            row.set_style_border_width(0, lv.PART.MAIN | state)
            row.set_style_outline_width(0, lv.PART.MAIN | state)
            row.set_style_shadow_width(0, lv.PART.MAIN | state)
            row.set_style_radius(8, lv.PART.MAIN | state)
            try:
                row.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                pass
        row.set_style_bg_color(
            lv.color_hex(_lv_color(SETTINGS_ROW_PRESSED_BG)),
            lv.PART.MAIN | lv.STATE.PRESSED)

        # 图片控件放在 48x48 透明槽位内，四周各预留 4px，避免解码边缘被父行裁掉。
        icon_box = lv.obj(row)
        icon_box.set_size(SETTINGS_ICON_BOX_SIZE, SETTINGS_ICON_BOX_SIZE)
        icon_box.set_pos(SETTINGS_ICON_BOX_X, SETTINGS_ICON_BOX_Y)
        _fix_position(icon_box)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            icon_box.set_style_bg_opa(0, lv.PART.MAIN | state)
            icon_box.set_style_border_width(0, lv.PART.MAIN | state)
            icon_box.set_style_outline_width(0, lv.PART.MAIN | state)
            icon_box.set_style_shadow_width(0, lv.PART.MAIN | state)
            try:
                icon_box.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                pass

        icon = lv.img(icon_box)
        icon.set_src(icon_path)
        # 源文件保持 40x40，不把图片控件放大，否则 LVGL 可能平铺图片。
        icon.set_size(SETTINGS_ICON_SIZE, SETTINGS_ICON_SIZE)
        icon.set_pos((SETTINGS_ICON_BOX_SIZE - SETTINGS_ICON_SIZE) // 2,
                     (SETTINGS_ICON_BOX_SIZE - SETTINGS_ICON_SIZE) // 2)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            icon.set_style_bg_opa(0, lv.PART.MAIN | state)
            icon.set_style_border_width(0, lv.PART.MAIN | state)
            icon.set_style_outline_width(0, lv.PART.MAIN | state)
            icon.set_style_shadow_width(0, lv.PART.MAIN | state)
            try:
                icon.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                pass

        label = lv.label(row)
        label.set_text(text)
        label.set_size(LCD_WIDTH - SETTINGS_LABEL_X - 8, FONT_MAIN[1])
        label.set_pos(SETTINGS_LABEL_X, (SETTINGS_ROW_HEIGHT - FONT_MAIN[1]) // 2)
        label.add_style(self._get_style(
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.LEFT), lv.PART.MAIN)
        self._bind_click(page, row, callback)
        return row

    def _radio_row(self, parent, y, text, key, selected, callback, rows,
                   owner_page=None):
        """用 LVGL 原生 checkbox 创建圆形单选按钮。"""
        # 滚动页中 parent 是滚动容器，而点击目标必须归属实际页面。
        if owner_page is None:
            owner_page = parent
        # 清除页面主题默认内边距，避免左侧点击区域被父容器裁剪。
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            try:
                parent.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                parent.set_style_pad_top(0, lv.PART.MAIN | state)
                parent.set_style_pad_bottom(0, lv.PART.MAIN | state)
                parent.set_style_pad_left(0, lv.PART.MAIN | state)
                parent.set_style_pad_right(0, lv.PART.MAIN | state)

        radio = lv.checkbox(parent)
        radio.set_text(text)
        radio.set_size(LCD_WIDTH, 58)
        radio.set_pos(0, y)
        _fix_position(radio)
        try:
            # 互斥状态由业务代码统一维护，禁止 checkbox 自行反选。
            radio.clear_flag(lv.obj.FLAG.CHECKABLE)
        except Exception:
            pass

        # 主体样式：保持原来的深色圆角菜单行和中文字体。
        radio.add_style(self._get_style(
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.LEFT), lv.PART.MAIN)
        radio.set_style_bg_opa(255, lv.PART.MAIN)
        radio.set_style_bg_color(
            lv.color_hex(_lv_color(MODE_ROW_BG)), lv.PART.MAIN)
        radio.set_style_bg_color(
            lv.color_hex(_lv_color(MODE_ROW_SELECTED_BG)),
            lv.PART.MAIN | lv.STATE.CHECKED)
        radio.set_style_text_color(
            lv.color_hex(_lv_color(WHITE)), lv.PART.MAIN)
        radio.set_style_text_color(
            lv.color_hex(_lv_color(MODE_TEXT_ON)),
            lv.PART.MAIN | lv.STATE.CHECKED)
        radio.set_style_border_width(0, lv.PART.MAIN)
        radio.set_style_outline_width(0, lv.PART.MAIN)
        radio.set_style_shadow_width(0, lv.PART.MAIN)
        radio.set_style_radius(8, lv.PART.MAIN)
        radio.set_style_pad_left(14, lv.PART.MAIN)
        radio.set_style_pad_right(0, lv.PART.MAIN)
        radio.set_style_pad_top(12, lv.PART.MAIN)
        radio.set_style_pad_bottom(0, lv.PART.MAIN)
        try:
            radio.set_style_pad_column(8, lv.PART.MAIN)
        except Exception:
            # 旧版绑定没有列间距接口时，仍保持可用的默认间距。
            pass

        # checkbox 的 INDICATOR 就是原生选择区域；改成圆形并隐藏勾号。
        radio.set_style_bg_opa(255, lv.PART.INDICATOR)
        radio.set_style_bg_color(
            lv.color_hex(_lv_color(MODE_DOT_OFF)), lv.PART.INDICATOR)
        radio.set_style_bg_color(
            lv.color_hex(_lv_color(MODE_DOT_ON)),
            lv.PART.INDICATOR | lv.STATE.CHECKED)
        radio.set_style_border_width(0, lv.PART.INDICATOR)
        radio.set_style_radius(20, lv.PART.INDICATOR)
        try:
            # 33px 字体对应的默认指示器缩到约 21px，保持原视觉尺寸。
            radio.set_style_transform_width(-6, lv.PART.INDICATOR)
            radio.set_style_transform_height(-6, lv.PART.INDICATOR)
        except Exception:
            pass
        try:
            radio.set_style_bg_img_opa(
                0, lv.PART.INDICATOR | lv.STATE.CHECKED)
        except Exception:
            pass

        rows[key] = radio
        self._set_radio_row_selected(radio, selected)
        def on_select(obj):
            callback(key)
        self._bind_click(owner_page, radio, on_select)
        return radio

    def _set_radio_row_selected(self, radio, selected):
        """更新原生单选按钮的 CHECKED 状态。"""
        if radio is None:
            return
        if selected:
            radio.add_state(lv.STATE.CHECKED)
        else:
            radio.clear_state(lv.STATE.CHECKED)

    def _set_mode_row_selected(self, parts, selected):
        """兼容旧调用名称，实际使用通用单选行状态更新。"""
        self._set_radio_row_selected(parts, selected)

    def _sync_shoulder_mode_ui(self):
        """把控制器的模式缓存同步到肩灯页面单选按钮。"""
        if self._shoulder_controller is not None:
            try:
                self.shoulder_mode = self._shoulder_controller.get_mode()
            except Exception:
                pass
        for key, radio in self._shoulder_rows.items():
            self._set_radio_row_selected(radio, key == self.shoulder_mode)
        self._shoulder_refresh_pending = False

    def _load(self, key, page):
        """加载页面并保存上一级页面，用于右滑返回。"""
        self._switch_page(key, page, True)

    def go_back(self):
        """返回上一级；首页没有上一级。"""
        if not self.stack or self._back_animating:
            return
        self._clear_back_drag_state()
        previous = self.stack.pop()
        self._switch_page(previous[0], previous[1], False)

    def _get_obj_area(self, obj):
        """读取控件的绝对屏幕区域，兼容两种 LVGL Python 绑定形式。"""
        try:
            if self._click_area is None:
                self._click_area = lv.area_t()
            area = self._click_area
            try:
                obj.get_coords(area)
            except Exception:
                lv.obj_get_coords(obj, area)
            return area
        except Exception:
            return None

    def _dispatch_click(self, point, preferred_obj=None):
        """按当前事件对象优先、横屏坐标兜底查找点击目标。"""
        if self.current is None:
            return False
        current_page = self.current[1]

        # 子控件（例如左侧圆点）收到事件时优先使用事件对象，避免
        # 坐标命中顺序先找到外层整行而丢失圆点自身的点击。
        if preferred_obj is not None:
            for page, obj, callback in self._click_targets:
                if page is current_page and obj is preferred_obj:
                    # LVGL 已经把该对象确定为事件目标，事件对象本身
                    # 比 Python 侧再次读取的坐标更可靠。尤其是圆形
                    # indicator，不能因一次坐标读取延迟而丢失点击。
                    callback(None)
                    return True

        if point is None:
            return False
        x, y = point
        for page, obj, callback in self._click_targets:
            if page is not current_page:
                continue
            area = self._get_obj_area(obj)
            if area is None:
                continue
            if (area.x1 <= x <= area.x2 and
                    area.y1 <= y <= area.y2):
                callback(None)
                return True
        return False

    def _icon_row(self, page, y, icon_name, text, callback):
        """创建手表式首页菜单行：左侧图标、右侧文字，整行可点击。"""
        row = lv.obj(page)
        # 普通对象默认不可点击，显式开启点击标志以保留菜单跳转。
        try:
            row.add_flag(lv.obj.FLAG.CLICKABLE)
        except Exception:
            pass
        # 菜单行高度增至 64 像素，为 52x52 图标及解码器边缘留出裁剪余量。
        row.set_size(LCD_WIDTH - 20, 64)
        row.set_pos(10, y)
        _fix_position(row)
        # lv.obj 的默认主题带有内边距。先记录原内边距用于保持横向位置，
        # 随后显式清零，避免子控件被向下推并在菜单行底部发生裁剪。
        try:
            row_pad_top = int(row.get_style_pad_top(lv.PART.MAIN))
        except Exception:
            row_pad_top = 16
        try:
            row_pad_left = int(row.get_style_pad_left(lv.PART.MAIN))
        except Exception:
            row_pad_left = 16
        row.set_style_bg_opa(0, lv.PART.MAIN | lv.STATE.DEFAULT)
        row.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
        row.set_style_outline_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
        row.set_style_shadow_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
        row.set_style_radius(0, lv.PART.MAIN | lv.STATE.DEFAULT)
        # 关闭所有 LVGL 状态的边框、轮廓、阴影和滚动条，避免残留线条。
        for _state in (lv.STATE.DEFAULT, lv.STATE.PRESSED, lv.STATE.FOCUSED, lv.STATE.CHECKED):
            row.set_style_bg_opa(0, lv.PART.MAIN | _state)
            row.set_style_border_width(0, lv.PART.MAIN | _state)
            row.set_style_outline_width(0, lv.PART.MAIN | _state)
            row.set_style_shadow_width(0, lv.PART.MAIN | _state)
            try:
                row.set_style_pad_all(0, lv.PART.MAIN | _state)
            except Exception:
                row.set_style_pad_top(0, lv.PART.MAIN | _state)
                row.set_style_pad_bottom(0, lv.PART.MAIN | _state)
                row.set_style_pad_left(0, lv.PART.MAIN | _state)
                row.set_style_pad_right(0, lv.PART.MAIN | _state)
        # 按官方接口从 usr 目录读取 PNG 图标。
        icon = lv.img(row)
        # 官方接口：lv.img.set_src 直接接收文件系统路径。
        # 直接使用传入的文件系统路径，不再自动拼接扩展名。
        icon_path = icon_name
        icon.set_src(icon_path)
        # 图像控件必须与 52x52 源图一致；控件大于源图时 LVGL 会平铺，
        # 从而在右侧或底部画出一小段重复图像。
        icon.set_size(52, 52)
        icon_y = max(0, min(12, row_pad_top + 1 - 12))
        icon.set_pos(row_pad_left + 4, icon_y)

        label = lv.label(row)
        label.set_text(text)
        label.set_size(LCD_WIDTH - 88, 33)
        # 文字基线放在菜单行中部，而不是贴近底部。
        # 横向补偿被清除的主题内边距，纵向相对原位置上移 12 像素。
        label.set_pos(row_pad_left + 68, max(0, row_pad_top))
        label.add_style(self._get_style(
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.LEFT), lv.PART.MAIN)
        # LVGL 仍负责产生点击事件，最终入口由横屏真实坐标统一决定。
        self._bind_click(page, row, callback)
        return row

    def _login_button(self, page, x, y, width, height, text, callback,
                      color=LOGIN_KEY_BG, font=FONT_LOGIN_TEXT,
                      label_clickable=False, click_page=None):
        """创建登录页固定按钮；登录页本身不使用滚动容器。"""
        button = lv.btn(page)
        button.set_size(width, height)
        button.set_pos(x, y)
        _fix_position(button)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            button.set_style_bg_opa(255, lv.PART.MAIN | state)
            button.set_style_bg_color(
                lv.color_hex(_lv_color(color)), lv.PART.MAIN | state)
            button.set_style_border_width(0, lv.PART.MAIN | state)
            button.set_style_outline_width(0, lv.PART.MAIN | state)
            button.set_style_shadow_width(0, lv.PART.MAIN | state)
            button.set_style_radius(6, lv.PART.MAIN | state)
            try:
                button.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                pass
        label = lv.label(button)
        # 标签使用字体真实行高，并在按钮内水平、垂直居中。
        label.set_size(width, font[1])
        label.set_pos(0, (height - font[1]) // 2)
        label.set_text(text)
        label.add_style(self._get_style(font, WHITE), lv.PART.MAIN)
        # 某些LVGL固件会把子标签作为触摸目标；标签和按钮都绑定同一动作，
        # 保证点在M/F字形区域也能稳定切换。
        if label_clickable:
            try:
                label.add_flag(lv.obj.FLAG.CLICKABLE)
            except Exception:
                pass
        # page 既是控件父对象也是默认点击页面；弹框按钮的父对象是弹框，
        # 但点击分发仍必须登记到当前登录页面，否则坐标分发会忽略按钮。
        self._bind_click(click_page if click_page is not None else page,
                         button, callback)
        if label_clickable:
            self._bind_click(click_page if click_page is not None else page,
                             label, callback)
        return button

    def _set_login_prefix(self, prefix):
        """选择M或F，二者始终保持单选。"""
        if prefix not in ("M", "F"):
            return
        self._login_prefix = prefix
        for key, button in self._login_prefix_buttons.items():
            color = LOGIN_SELECTED_BG if key == prefix else LOGIN_ROW_BG
            # 对所有状态使用当前单选颜色，避免FOCUSED/PRESSED覆盖选中效果。
            for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                          lv.STATE.FOCUSED, lv.STATE.CHECKED):
                button.set_style_bg_color(
                    lv.color_hex(_lv_color(color)),
                    lv.PART.MAIN | state)

    def _refresh_login_number(self):
        if self._login_number_label is None:
            return
        # 未输入时保持空白；输入后只显示已经输入的数字。
        self._login_number_label.set_text(self._login_digits)

    def _login_digit(self, digit):
        """追加数字，最多保存6位。"""
        if self._force_login_prompt:
            return
        if len(self._login_digits) >= LOGIN_DIGIT_COUNT:
            return
        self._login_digits += str(digit)
        self._refresh_login_number()

    def _login_delete(self):
        if self._force_login_prompt:
            self._force_login_prompt = False
            self._hide_login_message()
            return
        if self._login_digits:
            self._login_digits = self._login_digits[:-1]
            self._refresh_login_number()

    def _show_login_message(self, text, color=RED, duration_ms=None):
        if self._login_status_label is None:
            return
        self._login_status_label.set_text(text)
        if self._force_login_box is not None:
            self._force_login_box.set_style_bg_color(
                lv.color_hex(_lv_color(color)),
                lv.PART.MAIN | lv.STATE.DEFAULT)
            self._force_login_box.clear_flag(lv.obj.FLAG.HIDDEN)
        self._login_status_label.set_style_bg_color(
            lv.color_hex(_lv_color(color)),
            lv.PART.MAIN | lv.STATE.DEFAULT)
        self._login_status_label.set_style_bg_opa(
            0, lv.PART.MAIN | lv.STATE.DEFAULT)
        self._login_status_label.clear_flag(lv.obj.FLAG.HIDDEN)
        if self._force_login_prompt:
            for button in (self._force_login_cancel_button,
                           self._force_login_confirm_button):
                if button is not None:
                    button.clear_flag(lv.obj.FLAG.HIDDEN)
        else:
            for button in (self._force_login_cancel_button,
                           self._force_login_confirm_button):
                if button is not None:
                    button.add_flag(lv.obj.FLAG.HIDDEN)
        if duration_ms is None:
            self._login_message_until_ms = None
        else:
            try:
                self._login_message_until_ms = utime.ticks_add(
                    utime.ticks_ms(), int(duration_ms))
            except Exception:
                self._login_message_until_ms = (
                    utime.ticks_ms() + int(duration_ms))

    def _hide_login_message(self):
        if self._force_login_box is not None:
            self._force_login_box.add_flag(lv.obj.FLAG.HIDDEN)
        elif self._login_status_label is not None:
            self._login_status_label.add_flag(lv.obj.FLAG.HIDDEN)
        self._login_message_until_ms = None
        for button in (self._force_login_cancel_button,
                       self._force_login_confirm_button):
            if button is not None:
                button.add_flag(lv.obj.FLAG.HIDDEN)

    def _cancel_force_login_prompt(self):
        """取消强制转机登录，只关闭确认弹框，保留警号输入。"""
        if not self._force_login_prompt:
            return
        self._force_login_prompt = False
        self._hide_login_message()

    def _confirm_force_login_prompt(self):
        """确认强制转机登录，发送0x0E。"""
        if not self._force_login_prompt:
            return
        if self._poc_client is not None and self._poc_client.confirm_force_login():
            self._force_login_prompt = False
            self._show_login_message("转机登录中", LOGIN_SELECTED_BG, None)

    def _confirm_login(self):
        """组合M/F和6位数字，并提交给后台POC客户端。"""
        if self._force_login_prompt:
            return
        if self._login_message_until_ms is not None:
            if not self._poc_time_reached(self._login_message_until_ms):
                return
            self._hide_login_message()
        if len(self._login_digits) != LOGIN_DIGIT_COUNT:
            self._show_login_message(
                "请输入6位编号", RED, LOGIN_MESSAGE_DURATION_MS)
            return
        if self._poc_client is None:
            self._show_login_message(
                "登录服务不可用", RED, LOGIN_MESSAGE_DURATION_MS)
            return
        police_no = self._login_prefix + self._login_digits
        if self._poc_client.submit_login(police_no):
            self._show_login_message("登录中", LOGIN_SELECTED_BG, None)
        else:
            self._show_login_message(
                "登录失败", RED, LOGIN_MESSAGE_DURATION_MS)

    def _create_login_page(self, page):
        """创建编号栏和3x4数字键盘。"""
        # 清除主题默认内边距，使视觉坐标、控件坐标和触摸命中完全一致。
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            try:
                page.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                page.set_style_pad_top(0, lv.PART.MAIN | state)
                page.set_style_pad_bottom(0, lv.PART.MAIN | state)
                page.set_style_pad_left(0, lv.PART.MAIN | state)
                page.set_style_pad_right(0, lv.PART.MAIN | state)

        # 登录页面整体上移10px，但保留非负的内部坐标，避免顶部控件被裁剪。
        page.set_size(LCD_WIDTH, PAGE_HEIGHT + 10)
        page.set_pos(0, STATUS_HEIGHT - 10)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            page.set_style_bg_opa(0, lv.PART.MAIN | state)

        # 编号栏左右各留15px，M/F选择区和数字显示区紧邻排列。
        prefix_width = 40
        prefix_gap = 4
        # 43px高度完整容纳32号M/F字库。
        row_y = 6
        row_height = 35
        for index, prefix in enumerate(("M", "F")):
            button = self._login_button(
                page, 15 + index * (prefix_width + prefix_gap), row_y,
                prefix_width, row_height, prefix,
                lambda obj, value=prefix: self._set_login_prefix(value),
                LOGIN_ROW_BG, FONT_LOGIN_UNIFORM, True)
            self._login_prefix_buttons[prefix] = button

        number_x = 15 + 2 * (prefix_width + prefix_gap) + 2
        number_width = LCD_WIDTH - number_x - 15
        number_box = lv.obj(page)
        number_box.set_size(number_width, row_height)
        number_box.set_pos(number_x, row_y)
        _fix_position(number_box)
        _set_bg(number_box, LOGIN_ROW_BG)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            try:
                number_box.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                pass
        self._login_number_label = lv.label(number_box)
        self._login_number_label.set_size(number_width, FONT_LOGIN_UNIFORM[1])
        self._login_number_label.set_pos(
            0, (row_height - FONT_LOGIN_UNIFORM[1]) // 2)
        self._login_number_label.add_style(self._get_style(
            FONT_LOGIN_UNIFORM, WHITE), lv.PART.MAIN)

        key_width = 84
        key_height = 35
        key_gap_x = 7
        key_gap_y = 5
        key_x = 15
        key_y = 51
        keys = (("1", "2", "3"), ("4", "5", "6"),
                ("7", "8", "9"), ("删除", "0", "确认"))
        for row_index, row in enumerate(keys):
            for column_index, text in enumerate(row):
                x = key_x + column_index * (key_width + key_gap_x)
                y = key_y + row_index * (key_height + key_gap_y)
                if text == "删除":
                    callback = lambda obj: self._login_delete()
                    color = LOGIN_ROW_BG
                elif text == "确认":
                    callback = lambda obj: self._confirm_login()
                    color = LOGIN_CONFIRM_BG
                else:
                    callback = lambda obj, value=text: self._login_digit(value)
                    color = LOGIN_KEY_BG
                self._login_button(
                    page, x, y, key_width, key_height,
                    text, callback, color, FONT_LOGIN_UNIFORM)

        # 提示层位于键盘中部，仅登录中或失败时显示。
        self._force_login_box = lv.obj(page)
        # 使用接近屏幕宽度的弹框，给长提示文字和按钮分别预留独立区域。
        self._force_login_box.set_size(286, 110)
        self._force_login_box.set_pos((LCD_WIDTH - 286) // 2, 68)
        self._force_login_box.set_style_bg_color(lv.color_hex(_lv_color(RED)), lv.PART.MAIN | lv.STATE.DEFAULT)
        self._force_login_box.set_style_radius(6, lv.PART.MAIN | lv.STATE.DEFAULT)
        # 弹框为固定静态区域，禁止滚动、拖拽和弹性移动。
        try:
            self._force_login_box.set_scroll_dir(lv.DIR.NONE)
            self._force_login_box.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        except Exception:
            pass
        try:
            self._force_login_box.clear_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass
        self._login_status_label = lv.label(self._force_login_box)
        self._login_status_label.set_size(286, 52)
        self._login_status_label.set_pos(0, 0)
        self._login_status_label.add_style(
            self._get_style(FONT_LOGIN_UNIFORM, WHITE), lv.PART.MAIN)
        # 提示文字在46px高提示框内垂直居中。
        try:
            self._login_status_label.set_style_pad_top(
                (52 - FONT_LOGIN_UNIFORM[1]) // 2,
                lv.PART.MAIN | lv.STATE.DEFAULT)
            self._login_status_label.set_style_pad_bottom(
                52 - FONT_LOGIN_UNIFORM[1] -
                (52 - FONT_LOGIN_UNIFORM[1]) // 2,
                lv.PART.MAIN | lv.STATE.DEFAULT)
        except Exception:
            pass
        self._login_status_label.set_style_bg_opa(
            0, lv.PART.MAIN | lv.STATE.DEFAULT)
        self._login_status_label.set_style_radius(
            6, lv.PART.MAIN | lv.STATE.DEFAULT)
        self._force_login_box.add_flag(lv.obj.FLAG.HIDDEN)

        # 强制登录确认弹框第二行按钮：左取消、右确认。
        self._force_login_cancel_button = self._login_button(
            self._force_login_box, 4, 58, 94, 35, "取消",
            lambda obj: self._cancel_force_login_prompt(),
            GRAY, FONT_LOGIN_TEXT, click_page=page)
        self._force_login_confirm_button = self._login_button(
            self._force_login_box, 102, 58, 94, 35, "确认",
            lambda obj: self._confirm_force_login_prompt(),
            LOGIN_CONFIRM_BG, FONT_LOGIN_TEXT, click_page=page)
        # 除统一坐标分发外，再绑定按钮自身的 CLICKED 事件，兼容部分
        # QuecPython/LVGL 固件中子按钮事件不向外层坐标分发的情况。
        def force_cancel_clicked(event=None):
            self._click_dispatched = True
            self._cancel_force_login_prompt()

        def force_confirm_clicked(event=None):
            self._click_dispatched = True
            self._confirm_force_login_prompt()

        try:
            self._force_login_cancel_button.add_event_cb(
                force_cancel_clicked, lv.EVENT.CLICKED, None)
            self._force_login_confirm_button.add_event_cb(
                force_confirm_clicked, lv.EVENT.CLICKED, None)
        except Exception:
            try:
                self._force_login_cancel_button.set_event_cb(
                    force_cancel_clicked, lv.EVENT.CLICKED, None)
                self._force_login_confirm_button.set_event_cb(
                    force_confirm_clicked, lv.EVENT.CLICKED, None)
            except Exception:
                pass
        self._force_login_cancel_button.add_flag(lv.obj.FLAG.HIDDEN)
        self._force_login_confirm_button.add_flag(lv.obj.FLAG.HIDDEN)
        self._set_login_prefix(self._login_prefix)
        self._refresh_login_number()

    def show_login(self):
        """启动登录页：不压入历史栈，也不允许滑动返回。"""
        page, created = self._screen("login", "")
        if created:
            self._create_login_page(page)
        # 登录页不显示顶部状态栏，登录控件使用完整屏幕宽度。
        page.set_pos(0, 0)
        page.set_size(LCD_WIDTH, LCD_HEIGHT)
        for widget in (self._network_icon, self._battery_label):
            if widget is not None:
                widget.add_flag(lv.obj.FLAG.HIDDEN)
        self.stack = []
        self._switch_page("login", page, False)

    def show_home(self):
        """一级页面：首页，三个入口在横屏内纵向排列。"""
        page, created = self._screen("home", "")
        if created:
            # 页面从状态栏下方 y=30 开始；首行紧贴内容区顶部，三行等距排列。
            self._icon_row(page, 0, "U:/intercom.png", "对讲机", lambda obj: self.show_intercom())
            self._icon_row(page, 64, "U:/shoulder_lamp.png", "肩灯", lambda obj: self.show_shoulder())
            self._icon_row(page, 128, "U:/settings.png", "设置", lambda obj: self.show_settings())
        for widget in (self._network_icon, self._battery_label):
            if widget is not None:
                widget.clear_flag(lv.obj.FLAG.HIDDEN)
        self.stack = []
        self._switch_page("home", page, False)

    def show_intercom(self):
        """二级页面：按设置页风格进入组呼或单呼对象列表。"""
        page, created = self._screen("intercom", "")
        if created:
            # 清除主题内边距，使菜单行左右边缘与设置页一致。
            for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                          lv.STATE.FOCUSED, lv.STATE.CHECKED):
                try:
                    page.set_style_pad_all(0, lv.PART.MAIN | state)
                except Exception:
                    page.set_style_pad_top(0, lv.PART.MAIN | state)
                    page.set_style_pad_bottom(0, lv.PART.MAIN | state)
                    page.set_style_pad_left(0, lv.PART.MAIN | state)
                    page.set_style_pad_right(0, lv.PART.MAIN | state)
            self._settings_row(
                page, page, 6, "U:/group_call.png", "组呼模式",
                lambda obj: self.show_group_mode())
            self._settings_row(
                page, page, 72, "U:/single_call.png", "单呼模式",
                lambda obj: self.show_single_mode())
        self._load("intercom", page)
        if self._poc_client is not None:
            try:
                self._poc_client.request_http_refresh("对讲机页面")
            except Exception as error:
                print("[对讲机] 名单刷新请求失败：{}".format(error))

    def _sync_intercom_selection_ui(self):
        """分别同步普通组和单呼对象；两类会话允许同时选中。"""
        for key, radio in self._group_rows.items():
            visible_group = (self._pending_group
                             if self._pending_group is not None
                             else self.selected_group)
            selected = visible_group == key
            self._set_radio_row_selected(radio, selected)
        for key, radio in self._single_rows.items():
            visible_person = (self._pending_person
                              if self._pending_person is not None else
                              (self.selected_person if
                               self._single_group_id is not None else
                               self._single_none_key))
            selected = visible_person == key
            self._set_radio_row_selected(radio, selected)

    def _person_key(self, person):
        """返回稳定的人员选择键，保证同名不同编号仍是不同对象。"""
        if not isinstance(person, dict):
            return str(person)
        return str(person.get("device_id") or
                   person.get("police_code") or person.get("name") or "")

    def _is_logged_in_person(self, person):
        """判断HTTP人员是否为当前登录警号对应的人员。"""
        if not isinstance(person, dict) or self._poc_client is None:
            return False
        login_police = getattr(self._poc_client, "_login_police_no", None)
        if not login_police:
            login_police = getattr(self._poc_client, "_police_no", None)
        person_police = person.get("police_code")
        if not login_police or not person_police:
            return False
        return (str(person_police).strip().upper() ==
                str(login_police).strip().upper())

    def _group_name(self, key):
        for group in self._intercom_groups:
            if str(group.get("raw_id", "")) == key:
                return str(group.get("name") or key)
        return key

    def _person_name(self, key):
        for person in self._intercom_people:
            if self._person_key(person) == key:
                return str(person.get("name") or
                           person.get("police_code") or key)
        return key

    def _populate_group_rows(self):
        """按最新HTTP名单新增、更新或隐藏组呼行，控件对象始终复用。"""
        if self._group_content is None:
            return
        active_keys = {"__none_group__": True}
        none_radio = self._group_rows.get("__none_group__")
        if none_radio is None:
            none_radio = self._radio_row(
                self._group_content, 6, "无/退出", "__none_group__",
                self.selected_group == "__none_group__", self.select_group,
                self._group_rows, owner_page=self.pages.get("group_mode"))
        else:
            none_radio.set_text("无/退出")
            none_radio.set_pos(0, 6)
            none_radio.clear_flag(lv.obj.FLAG.HIDDEN)
        for index, group in enumerate(self._intercom_groups):
            key = str(group.get("raw_id", ""))
            if not key:
                continue
            active_keys[key] = True
            y = 6 + (index + 1) * (SETTINGS_ROW_HEIGHT + SETTINGS_ROW_GAP)
            text = str(group.get("name") or key)
            radio = self._group_rows.get(key)
            if radio is None:
                radio = self._radio_row(
                    self._group_content, y, text, key,
                    self.selected_group == key,
                    self.select_group, self._group_rows,
                    owner_page=self.pages.get("group_mode"))
            else:
                radio.set_text(text)
                radio.set_pos(0, y)
                radio.clear_flag(lv.obj.FLAG.HIDDEN)
        for key, radio in self._group_rows.items():
            if key not in active_keys:
                radio.add_flag(lv.obj.FLAG.HIDDEN)
                # 旧固件对隐藏对象仍可能返回旧坐标，移出内容区作为双保险。
                radio.set_pos(0, -1000)
        self._group_rows_built = bool(active_keys)

    def _populate_single_rows(self):
        """按最新HTTP名单新增、更新或隐藏单呼行，保留“无/解散”。"""
        if self._single_content is None:
            return
        # 第一项始终存在，即使HTTP没有返回任何人员也能反映“无单呼”状态。
        if self._single_none_key not in self._single_rows:
            self._radio_row(
                self._single_content, 6, "无/解散", self._single_none_key,
                self.selected_person is None, self.select_single,
                self._single_rows, owner_page=self.pages.get("single_mode"))
        active_keys = {self._single_none_key: True}
        none_radio = self._single_rows.get(self._single_none_key)
        none_radio.set_text("无/解散")
        none_radio.set_pos(0, 6)
        none_radio.clear_flag(lv.obj.FLAG.HIDDEN)
        index = 1
        for person in self._intercom_people:
            if self._is_logged_in_person(person):
                continue
            key = self._person_key(person)
            if not key:
                continue
            active_keys[key] = True
            y = 6 + index * (SETTINGS_ROW_HEIGHT + SETTINGS_ROW_GAP)
            index += 1
            text = str(person.get("name") or
                       person.get("police_code") or key)
            online = person.get("online") is True
            radio = self._single_rows.get(key)
            if radio is None:
                radio = self._radio_row(
                    self._single_content, y, text, key,
                    self.selected_person == key,
                    self.select_single, self._single_rows,
                    owner_page=self.pages.get("single_mode"))
            else:
                radio.set_text(text)
                radio.set_pos(0, y)
                radio.clear_flag(lv.obj.FLAG.HIDDEN)
            try:
                color = WHITE if online else BLACK
                radio.set_style_text_color(lv.color_hex(_lv_color(color)),
                                           lv.PART.MAIN)
                radio.set_style_text_color(lv.color_hex(_lv_color(color)),
                                           lv.PART.MAIN | lv.STATE.CHECKED)
            except Exception:
                pass
        for key, radio in self._single_rows.items():
            if key not in active_keys:
                radio.add_flag(lv.obj.FLAG.HIDDEN)
                radio.set_pos(0, -1000)
        self._single_rows_built = True

    def select_group(self, group):
        """请求切换组呼；UI先显示待选组，最终以TCP 0x83应答为准。"""
        valid = [str(item.get("raw_id", ""))
                 for item in self._intercom_groups]
        if group == "__none_group__":
            if self._poc_client is not None:
                self._poc_client.request_leave_group()
            self.selected_group = group
            self._pending_group = None
            self._sync_intercom_selection_ui()
            return
        if group not in valid:
            return
        if (self._pending_group or self.selected_group) == group:
            # 过滤重复选择，同时纠正 checkbox 被固件自行反选的情况。
            if self._poc_client is not None:
                try:
                    self._poc_client.set_preferred_call_type("group")
                except Exception:
                    pass
            self._set_radio_row_selected(self._group_rows.get(group), True)
            return
        # QuecPython部分固件的next()不支持CPython的default参数，
        # 使用普通循环查找，避免“function takes 1 positional arguments”异常。
        numeric_id = None
        for item in self._intercom_groups:
            if str(item.get("raw_id", "")) == group:
                numeric_id = item.get("id")
                break
        self._join_previous_group = self.selected_group
        join_ok = False
        if self._poc_client is not None:
            try:
                # 新版客户端同时传递原始组ID和协议用数值组ID。
                join_ok = bool(self._poc_client.request_join_group(
                    group, numeric_id))
            except TypeError as error:
                # 兼容设备中尚未同步的旧版客户端（只接受一个参数）。
                message = str(error).lower()
                if ("positional" not in message and
                        "argument" not in message and
                        "arguments" not in message):
                    raise
                join_ok = bool(self._poc_client.request_join_group(group))
        if not join_ok:
            # 请求未提交时保持当前已确认组，不显示失败选择。
            self._sync_intercom_selection_ui()
            return
        self._pending_group = group
        self._sync_intercom_selection_ui()
        print("[对讲机] 当前组呼：{}，group_id={}".format(
            self._group_name(group),
            numeric_id))

    def select_single(self, person):
        """选择或解散单呼；切换人员由客户端先解散旧组再邀请新人。"""
        valid = [self._person_key(item) for item in self._intercom_people]
        if person != self._single_none_key and person not in valid:
            return
        if person != self._single_none_key:
            for item in self._intercom_people:
                if self._is_logged_in_person(item) and \
                        self._person_key(item) == person:
                    self._sync_intercom_selection_ui()
                    return
                if self._person_key(item) == person and item.get("online") is not True:
                    self._sync_intercom_selection_ui()
                    return
        if self._pending_person is not None:
            # 一个单呼控制流程尚未结束时不再叠加新请求。
            self._sync_intercom_selection_ui()
            return
        if ((person == self._single_none_key and
             self._single_group_id is None) or
                person == self.selected_person):
            # 过滤重复选择，同时纠正 checkbox 被固件自行反选的情况。
            if (person != self._single_none_key and
                    self._poc_client is not None):
                try:
                    self._poc_client.set_preferred_call_type("single")
                except Exception:
                    pass
            self._set_radio_row_selected(
                self._single_rows.get(person), True)
            return
        person_key = None if person == self._single_none_key else person
        device_id = None
        if person_key is not None:
            for item in self._intercom_people:
                if self._person_key(item) == person_key:
                    device_id = item.get("device_id")
                    break
        request_ok = False
        if self._poc_client is not None:
            try:
                request_ok = bool(self._poc_client.request_select_single(
                    person_key, device_id))
            except Exception as error:
                print("[对讲机] 单呼请求提交失败：{}".format(error))
        if not request_ok:
            self._sync_intercom_selection_ui()
            return
        self._pending_person = person
        self._sync_intercom_selection_ui()
        print("[对讲机] 请求单呼：{}".format(
            "无/解散" if person_key is None else
            self._person_name(person_key)))

    def show_group_mode(self):
        """三级页面：纵向单选当前组呼小组。"""
        if self._poc_client is not None:
            try:
                self._poc_client.set_preferred_call_type("group")
            except Exception:
                pass
        page, created = self._screen("group_mode", "")
        if created:
            self._group_content = self._vertical_scroll_area(
                page, 0, PAGE_HEIGHT)
        self._populate_group_rows()
        self._sync_intercom_selection_ui()
        self._load("group_mode", page)
        if self._poc_client is not None:
            try:
                self._poc_client.request_http_refresh("组呼页面")
            except Exception as error:
                print("[POC] 组呼名单刷新请求失败：{}".format(error))

    def show_single_mode(self):
        """三级页面：纵向单选当前单呼对象。"""
        if self._poc_client is not None:
            try:
                self._poc_client.set_preferred_call_type("single")
            except Exception:
                pass
        page, created = self._screen("single_mode", "")
        if created:
            self._single_content = self._vertical_scroll_area(
                page, 0, PAGE_HEIGHT)
        self._populate_single_rows()
        self._sync_intercom_selection_ui()
        self._load("single_mode", page)
        if self._poc_client is not None:
            try:
                self._poc_client.request_http_refresh("单呼页面")
            except Exception as error:
                print("[POC] 单呼名单刷新请求失败：{}".format(error))

    def select_shoulder_mode(self, mode):
        """切换肩灯工作模式；重复点击当前模式时直接过滤。"""
        if mode not in ("off", "alternate", "diagonal"):
            return
        if self._shoulder_controller is not None:
            try:
                # 即使重复点击 off，也必须调用控制器清除跌倒报警。
                self._shoulder_controller.set_mode(mode)
                self._shoulder_error_reported = False
            except Exception as error:
                print("[肩灯] 模式切换失败：{}".format(error))
                self._sync_shoulder_mode_ui()
                return
        self.shoulder_mode = mode
        self._sync_shoulder_mode_ui()

    def set_shoulder_flash_interval(self, interval_ms):
        """公开设置肩灯闪烁半周期，单位毫秒。"""
        if self._shoulder_controller is None:
            raise RuntimeError("ET6312B 肩灯控制器未初始化")
        value = self._shoulder_controller.set_flash_interval(interval_ms)
        self._shoulder_error_reported = False
        return value

    def show_shoulder(self):
        """二级页面：肩灯三种工作模式。"""
        # 与对讲机页面一致，去掉标题后直接显示三项单选模式。
        if self._shoulder_controller is not None:
            try:
                self.shoulder_mode = self._shoulder_controller.get_mode()
            except Exception:
                pass
        page, created = self._screen("shoulder", "")
        if created:
            self._radio_row(page, 6, "关闭", "off",
                            self.shoulder_mode == "off",
                            self.select_shoulder_mode, self._shoulder_rows)
            self._radio_row(page, 72, "红蓝交替闪", "alternate",
                            self.shoulder_mode == "alternate",
                            self.select_shoulder_mode, self._shoulder_rows)
            self._radio_row(page, 138, "红蓝对角闪", "diagonal",
                            self.shoulder_mode == "diagonal",
                            self.select_shoulder_mode, self._shoulder_rows)
        else:
            self._sync_shoulder_mode_ui()
        self._load("shoulder", page)

    def show_settings(self):
        """二级页面：按手表样式显示设置纵向菜单。"""
        # 标题属于设置页自身，切回上一级时页面整体隐藏，不会残留。
        page, created = self._screen("settings", "")
        # 设置页覆盖整块屏幕，但保持透明，让根屏幕的电量标签继续可见。
        page.set_size(LCD_WIDTH, LCD_HEIGHT)
        page.set_pos(0, 0)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            page.set_style_bg_opa(0, lv.PART.MAIN | state)
        if created:
            # 清除主题默认内边距，保证标题和菜单行使用真实屏幕坐标。
            for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                          lv.STATE.FOCUSED, lv.STATE.CHECKED):
                try:
                    page.set_style_pad_all(0, lv.PART.MAIN | state)
                except Exception:
                    page.set_style_pad_top(0, lv.PART.MAIN | state)
                    page.set_style_pad_bottom(0, lv.PART.MAIN | state)
                    page.set_style_pad_left(0, lv.PART.MAIN | state)
                    page.set_style_pad_right(0, lv.PART.MAIN | state)
            self._settings_title(page)
            content = self._vertical_scroll_area(
                page, STATUS_HEIGHT, PAGE_HEIGHT)
            items = (
                ("U:/settings_device.png", "设备信息", self.show_device),
                ("U:/settings_brightness.png", "亮度", self.show_brightness),
                ("U:/settings_volume.png", "音量", self.show_volume),
                ("U:/settings_sleep.png", "息屏时间", self.show_sleep),
                ("U:/settings_network.png", "网络状态", self.show_network),
                ("U:/settings_fall_detection.png", "跌倒检测", self.show_fall_detection),
            )
            for index, item in enumerate(items):
                y = index * (SETTINGS_ROW_HEIGHT + SETTINGS_ROW_GAP)
                self._settings_row(
                    page, content, y, item[0], item[1],
                    lambda obj, cb=item[2]: cb())
        self._load("settings", page)

    def _simple_list(self, key, title, labels):
        """三级页面通用列表布局：横屏两列固定区域。"""
        page, created = self._screen(key, title)
        if created:
            for index, text in enumerate(labels):
                x = 12 + (index % 2) * 142
                y = 38 + (index // 2) * 48
                self._tile(page, x, y, 130, 40, text, lambda obj: None)
        self._load(key, page)

    def _create_device_page(self, page):
        """创建与网络状态页一致的设备版本纯标签布局。"""
        self._network_label(
            page, 0, 4, LCD_WIDTH, "设备信息",
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.CENTER)

        # 三行版本信息的间距由 50px 缩小为 25px，便于集中查看。
        row_y = (56, 81, 106)
        caption_x = 20
        caption_width = 90
        value_x = caption_x + caption_width
        value_width = LCD_WIDTH - value_x - 20
        items = (
            ("软件版本：", self.device_versions["software"]),
            ("硬件版本：", self.device_versions["hardware"]),
            ("固件版本：", self.device_versions["firmware"]),
        )
        for index, item in enumerate(items):
            self._network_label(
                page, caption_x, row_y[index], caption_width, item[0])
            self._network_label(
                page, value_x, row_y[index], value_width, item[1])

    def show_device(self):
        """三级页面：显示软件、硬件和固件版本。"""
        page, created = self._screen("device", "")
        if created:
            self._create_device_page(page)
        self._load("device", page)

    def _brightness_symbol(self, page, x, y, size, color):
        """绘制简单太阳亮度符号，避免额外图片资源和文件系统依赖。"""
        holder = lv.obj(page)
        holder.set_size(size, size)
        holder.set_pos(x, y)
        _fix_position(holder)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            holder.set_style_bg_opa(0, lv.PART.MAIN | state)
            holder.set_style_border_width(0, lv.PART.MAIN | state)
            holder.set_style_outline_width(0, lv.PART.MAIN | state)
            holder.set_style_shadow_width(0, lv.PART.MAIN | state)
            try:
                holder.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                pass

        def solid_part(part, part_x, part_y, part_w, part_h, radius=0):
            part.set_size(part_w, part_h)
            part.set_pos(part_x, part_y)
            _fix_position(part)
            for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                          lv.STATE.FOCUSED, lv.STATE.CHECKED):
                part.set_style_bg_opa(255, lv.PART.MAIN | state)
                part.set_style_bg_color(
                    lv.color_hex(_lv_color(color)), lv.PART.MAIN | state)
                part.set_style_border_width(0, lv.PART.MAIN | state)
                part.set_style_outline_width(0, lv.PART.MAIN | state)
                part.set_style_shadow_width(0, lv.PART.MAIN | state)
                part.set_style_radius(radius, lv.PART.MAIN | state)

        ray = max(3, size // 8)
        ray_len = max(5, size // 4)
        center = max(8, size // 2)
        center_pos = (size - center) // 2
        core = lv.obj(holder)
        solid_part(core, center_pos, center_pos, center, center, center // 2)

        rays = (
            (center_pos + center // 2 - ray // 2, 0, ray, ray_len),
            (center_pos + center // 2 - ray // 2,
             size - ray_len, ray, ray_len),
            (0, center_pos + center // 2 - ray // 2, ray_len, ray),
            (size - ray_len, center_pos + center // 2 - ray // 2,
             ray_len, ray),
            (2, 2, ray, ray),
            (size - ray - 2, 2, ray, ray),
            (2, size - ray - 2, ray, ray),
            (size - ray - 2, size - ray - 2, ray, ray),
        )
        for part_x, part_y, part_w, part_h in rays:
            solid_part(lv.obj(holder), part_x, part_y,
                       part_w, part_h, ray // 2)
        return holder

    def _brightness_slider_changed(self, *args):
        """处理亮度滑块触摸，并在数值变化时同步 PWM 占空比。"""
        event = args[-1] if args else None
        slider = self._brightness_slider
        try:
            slider = event.get_target()
        except Exception:
            pass
        if slider is None:
            return
        try:
            code = event.get_code()
        except Exception:
            code = event

        if self._consume_touch_event(code):
            if code == lv.EVENT.VALUE_CHANGED:
                # 原生 slider 可能先更新值，再回调 Python；立刻恢复息屏前值。
                if not self._restoring_brightness:
                    self._restoring_brightness = True
                    try:
                        slider.set_value(self._sleep_brightness, lv.ANIM.OFF)
                    except Exception:
                        pass
                    finally:
                        self._restoring_brightness = False
            return
        if code != lv.EVENT.VALUE_CHANGED:
            return
        try:
            self.set_brightness(slider.get_value())
        except Exception:
            pass

    def _create_brightness_page(self, page):
        """创建参考图片风格的中央竖向亮度滑块页面。"""
        # 亮度页保持为状态栏下方的标准不透明页面。此前使用全屏透明容器，
        # 右滑时只有滑块和百分比等不透明子控件参与局部刷新，容易在设置页
        # 上留下短暂残影；黑色实底会在页面移动过程中同步覆盖旧像素。
        page.set_size(LCD_WIDTH, PAGE_HEIGHT)
        page.set_pos(0, STATUS_HEIGHT)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            page.set_style_bg_opa(255, lv.PART.MAIN | state)
            page.set_style_bg_color(
                lv.color_hex(_lv_color(BLACK)), lv.PART.MAIN | state)
            page.set_style_border_width(0, lv.PART.MAIN | state)
            page.set_style_outline_width(0, lv.PART.MAIN | state)
            page.set_style_shadow_width(0, lv.PART.MAIN | state)
            try:
                page.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                page.set_style_pad_top(0, lv.PART.MAIN | state)
                page.set_style_pad_bottom(0, lv.PART.MAIN | state)
                page.set_style_pad_left(0, lv.PART.MAIN | state)
                page.set_style_pad_right(0, lv.PART.MAIN | state)

        slider_width = 68
        slider_height = 140
        slider_x = (LCD_WIDTH - slider_width) // 2
        # 页面原点由屏幕y=0改为状态栏下方y=30，因此内部坐标减去30px，
        # 保持滑块、百分比和底部图标在屏幕上的实际位置基本不变。
        slider_y = 16
        slider_center_x = slider_x + slider_width // 2

        # 所有横向位置都以滑块中心为基准，避免主题内边距造成视觉偏移。
        top_symbol_size = 22
        bottom_symbol_size = 24
        self._brightness_symbol(
            page, slider_center_x - top_symbol_size // 2,
            0,
            top_symbol_size, BRIGHTNESS_TRACK_COLOR)
        self._brightness_symbol(
            page, slider_center_x - bottom_symbol_size // 2,
            160,
            bottom_symbol_size, BRIGHTNESS_DIM_COLOR)

        slider = lv.slider(page)
        slider.set_size(slider_width, slider_height)
        slider.set_pos(slider_x, slider_y)
        _fix_position(slider)
        slider.set_range(BRIGHTNESS_MIN, BRIGHTNESS_MAX)
        slider.set_value(self.brightness, lv.ANIM.OFF)

        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            slider.set_style_bg_opa(255, lv.PART.MAIN | state)
            slider.set_style_bg_color(
                lv.color_hex(_lv_color(BRIGHTNESS_TRACK_BG)),
                lv.PART.MAIN | state)
            slider.set_style_border_width(2, lv.PART.MAIN | state)
            slider.set_style_border_color(
                lv.color_hex(_lv_color(BRIGHTNESS_ACCENT_COLOR)),
                lv.PART.MAIN | state)
            slider.set_style_outline_width(0, lv.PART.MAIN | state)
            slider.set_style_shadow_width(0, lv.PART.MAIN | state)
            slider.set_style_radius(34, lv.PART.MAIN | state)
            slider.set_style_bg_opa(255, lv.PART.INDICATOR | state)
            slider.set_style_bg_color(
                lv.color_hex(_lv_color(BRIGHTNESS_TRACK_COLOR)),
                lv.PART.INDICATOR | state)
            slider.set_style_radius(34, lv.PART.INDICATOR | state)
            # 参考图中没有明显圆形旋钮，使用填充高度表达当前亮度。
            slider.set_style_bg_opa(0, lv.PART.KNOB | state)
            slider.set_style_border_width(0, lv.PART.KNOB | state)
            slider.set_style_shadow_width(0, lv.PART.KNOB | state)

        value_label = lv.label(page)
        value_label.set_size(82, FONT_MAIN[1])
        value_label.set_pos(
            slider_x + slider_width + 16,
            slider_y + (slider_height - FONT_MAIN[1]) // 2)
        value_label.add_style(self._get_style(
            FONT_MAIN, BRIGHTNESS_TRACK_COLOR, lv.TEXT_ALIGN.LEFT),
            lv.PART.MAIN)
        value_label.set_text("{}%".format(self.brightness))

        self._brightness_slider = slider
        self._brightness_value_label = value_label
        callback = self._brightness_slider_changed
        self._event_callbacks.append(callback)
        try:
            # 监听全部触摸事件，保证息屏时按在滑块上也只执行唤醒。
            slider.add_event_cb(callback, lv.EVENT.ALL, None)
        except Exception:
            try:
                wrapper = lambda target, event: callback(event)
                self._event_callbacks.append(wrapper)
                slider.set_event_cb(wrapper, lv.EVENT.ALL, None)
            except Exception:
                # 极旧固件不支持事件绑定时，仍可通过 set_brightness() 调整 PWM。
                pass

    def show_brightness(self):
        """三级页面：按参考手表样式调整 LCD 背光亮度。"""
        page, created = self._screen("brightness", "")
        if created:
            self._create_brightness_page(page)
        else:
            # 页面对象复用时，只同步当前值，不重复创建控件。
            if self._brightness_slider is not None:
                self._brightness_slider.set_value(
                    self.brightness, lv.ANIM.OFF)
            if self._brightness_value_label is not None:
                self._brightness_value_label.set_text(
                    "{}%".format(self.brightness))
        self._load("brightness", page)

    def show_volume(self):
        """三级页面：复用亮度页竖向滑块布局，显示0~11音量等级。"""
        page, created = self._screen("volume", "")
        if created:
            self._create_volume_page(page)
        else:
            if self._volume_slider is not None:
                self._volume_slider.set_value(self.volume, lv.ANIM.OFF)
            if self._volume_value_label is not None:
                self._volume_value_label.set_text(str(self.volume))
        self._load("volume", page)

    def _volume_slider_changed(self, *args):
        event = args[-1] if args else None
        slider = self._volume_slider
        try:
            slider = event.get_target()
        except Exception:
            pass
        if slider is None:
            return
        try:
            code = event.get_code()
        except Exception:
            code = event
        if self._consume_touch_event(code):
            if code == lv.EVENT.VALUE_CHANGED:
                try:
                    slider.set_value(self.volume, lv.ANIM.OFF)
                except Exception:
                    pass
            return
        if code == lv.EVENT.VALUE_CHANGED:
            self.set_volume(slider.get_value())

    def _create_volume_page(self, page):
        page.set_size(LCD_WIDTH, PAGE_HEIGHT)
        page.set_pos(0, STATUS_HEIGHT)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            page.set_style_bg_opa(255, lv.PART.MAIN | state)
            page.set_style_bg_color(
                lv.color_hex(_lv_color(BLACK)), lv.PART.MAIN | state)
            page.set_style_border_width(0, lv.PART.MAIN | state)
            try:
                page.set_style_pad_all(0, lv.PART.MAIN | state)
            except Exception:
                page.set_style_pad_top(0, lv.PART.MAIN | state)
                page.set_style_pad_bottom(0, lv.PART.MAIN | state)
                page.set_style_pad_left(0, lv.PART.MAIN | state)
                page.set_style_pad_right(0, lv.PART.MAIN | state)

        slider_width = 68
        slider_height = 140
        slider_x = (LCD_WIDTH - slider_width) // 2
        slider_y = 16
        slider = lv.slider(page)
        slider.set_size(slider_width, slider_height)
        slider.set_pos(slider_x, slider_y)
        _fix_position(slider)
        slider.set_range(VOLUME_MIN, VOLUME_MAX)
        slider.set_value(self.volume, lv.ANIM.OFF)
        for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                      lv.STATE.FOCUSED, lv.STATE.CHECKED):
            slider.set_style_bg_opa(255, lv.PART.MAIN | state)
            slider.set_style_bg_color(
                lv.color_hex(_lv_color(BRIGHTNESS_TRACK_BG)),
                lv.PART.MAIN | state)
            slider.set_style_border_width(2, lv.PART.MAIN | state)
            slider.set_style_border_color(
                lv.color_hex(_lv_color(BRIGHTNESS_ACCENT_COLOR)),
                lv.PART.MAIN | state)
            slider.set_style_radius(34, lv.PART.MAIN | state)
            slider.set_style_bg_opa(255, lv.PART.INDICATOR | state)
            slider.set_style_bg_color(
                lv.color_hex(_lv_color(BRIGHTNESS_TRACK_COLOR)),
                lv.PART.INDICATOR | state)
            slider.set_style_radius(34, lv.PART.INDICATOR | state)
            slider.set_style_bg_opa(0, lv.PART.KNOB | state)
            slider.set_style_border_width(0, lv.PART.KNOB | state)

        value_label = lv.label(page)
        value_label.set_size(60, FONT_MAIN[1])
        value_label.set_pos(
            slider_x + slider_width + 16,
            slider_y + (slider_height - FONT_MAIN[1]) // 2)
        value_label.add_style(self._get_style(
            FONT_MAIN, BRIGHTNESS_TRACK_COLOR, lv.TEXT_ALIGN.LEFT),
            lv.PART.MAIN)
        value_label.set_text(str(self.volume))

        # 使用现有扬声器图片，不显示亮度页的太阳图标和百分号。
        icon = lv.img(page)
        icon.set_src("U:/settings_volume.png")
        icon.set_size(40, 40)
        icon.set_pos(slider_x + (slider_width - 40) // 2, 164)
        _fix_position(icon)

        self._volume_slider = slider
        self._volume_value_label = value_label
        callback = self._volume_slider_changed
        self._event_callbacks.append(callback)
        try:
            slider.add_event_cb(callback, lv.EVENT.ALL, None)
        except Exception:
            try:
                wrapper = lambda target, event: callback(event)
                self._event_callbacks.append(wrapper)
                slider.set_event_cb(wrapper, lv.EVENT.ALL, None)
            except Exception:
                pass

    def select_sleep_timeout(self, seconds):
        """切换息屏时间；重复选择当前值时不做状态切换。"""
        valid = False
        for _text, value in SLEEP_OPTIONS:
            if seconds == value:
                valid = True
                break
        if not valid:
            return

        if seconds == self.sleep_timeout_seconds:
            self._set_radio_row_selected(
                self._sleep_rows.get(seconds), True)
            return

        self.sleep_timeout_seconds = seconds
        for key, radio in self._sleep_rows.items():
            self._set_radio_row_selected(radio, key == seconds)
        # 程序或外部按键切换息屏时间时，也从本次操作重新计算空闲时间。
        self.notify_activity()

    def show_sleep(self):
        """三级页面：与肩灯相同样式的息屏时间纵向单选菜单。"""
        page, created = self._screen("sleep", "")
        if created:
            # 页面没有标题，滚动区域直接占满状态栏下方的 296x210 内容区。
            content = self._vertical_scroll_area(page, 0, PAGE_HEIGHT)
            for index, item in enumerate(SLEEP_OPTIONS):
                self._radio_row(
                    content, 6 + index * 66,
                    item[0], item[1],
                    self.sleep_timeout_seconds == item[1],
                    self.select_sleep_timeout, self._sleep_rows,
                    owner_page=page)
        else:
            # 页面对象长期复用，重新进入时同步业务层保存的选中状态。
            for key, radio in self._sleep_rows.items():
                self._set_radio_row_selected(
                    radio, key == self.sleep_timeout_seconds)
        self._load("sleep", page)

    def select_fall_detection(self, enabled):
        enabled = bool(enabled)
        self.fall_detection_enabled = enabled
        for key, radio in self._fall_rows.items():
            self._set_radio_row_selected(radio, key == enabled)
        service = getattr(self, "_sc7a20h_service", None)
        if service is not None:
            method = getattr(service, "set_fall_detection_enabled", None)
            if method is not None:
                try:
                    method(enabled)
                except Exception as error:
                    print("[跌倒检测] 设置失败：{}".format(error))
        self.notify_activity()

    def show_fall_detection(self):
        page, created = self._screen("fall_detection", "")
        if created:
            content = lv.obj(page)
            content.set_size(LCD_WIDTH, PAGE_HEIGHT)
            content.set_pos(0, 0)
            for state in (lv.STATE.DEFAULT, lv.STATE.PRESSED,
                          lv.STATE.FOCUSED, lv.STATE.CHECKED):
                try:
                    content.set_style_pad_all(0, lv.PART.MAIN | state)
                    content.set_style_bg_opa(255, lv.PART.MAIN | state)
                    content.set_style_bg_color(
                        lv.color_hex(_lv_color(BLACK)), lv.PART.MAIN | state)
                except Exception:
                    pass
            for index, item in enumerate(((True, "开启"), (False, "关闭"))):
                self._radio_row(content, 6 + index * 66, item[1], item[0],
                                self.fall_detection_enabled == item[0],
                                self.select_fall_detection, self._fall_rows,
                                owner_page=page)
        else:
            for key, radio in self._fall_rows.items():
                self._set_radio_row_selected(
                    radio, key == self.fall_detection_enabled)
        self._load("fall_detection", page)

    def _network_label(self, page, x, y, width, text="",
                       font=FONT_SMALL, color=WHITE,
                       align=lv.TEXT_ALIGN.LEFT):
        """创建信息页固定文字标签，不添加按钮或点击行为。"""
        label = lv.label(page)
        label.set_size(width, font[1] + 15)
        label.set_pos(x, y)
        label.add_style(
            self._get_style(font, color, align), lv.PART.MAIN)
        label.set_text(text)
        return label

    def _set_network_value(self, label, text, color):
        """原地修改网络信息文字和颜色，避免重复创建 LVGL 对象。"""
        if label is None:
            return
        label.set_text(text)
        label.set_style_text_color(
            lv.color_hex(_lv_color(color)),
            lv.PART.MAIN | lv.STATE.DEFAULT)

    def _create_network_page(self, page):
        """创建网络状态三级页面的纯标签布局。"""
        title = self._network_label(
            page, 0, 4, LCD_WIDTH, "网络状态",
            FONT_MAIN, WHITE, lv.TEXT_ALIGN.CENTER)

        # 标题下方保留约 15px 间距；三行标题统一从屏幕左侧 20px 开始。
        row_y = (56, 106, 156)
        caption_x = 20
        caption_width = 82
        value_x = caption_x + caption_width
        value_width = LCD_WIDTH - value_x - 20

        # ICCID 通常为 19~20 位，单独压缩标题区并保留 192px 数值宽度。
        sim_caption_width = 74
        sim_value_x = caption_x + sim_caption_width
        sim_value_width = LCD_WIDTH - sim_value_x - 10

        self._network_label(
            page, caption_x, row_y[0], sim_caption_width, "SIM卡号：")
        self._network_label(
            page, caption_x, row_y[1], caption_width, "信号值：")
        self._network_label(
            page, caption_x, row_y[2], caption_width, "网络状态：")

        self._network_labels = {
            "title": title,
            "iccid": self._network_label(
                page, sim_value_x, row_y[0], sim_value_width),
            "signal": self._network_label(
                page, value_x, row_y[1], value_width),
            "connection": self._network_label(
                page, value_x, row_y[2], value_width),
        }

    def _refresh_network_page(self):
        """使用缓存快照刷新网络页；本方法只能由 LVGL 主线程调用。"""
        if not self._network_labels:
            return
        snapshot = self._network_snapshot or {}

        iccid = snapshot.get("iccid")
        if iccid:
            self._set_network_value(
                self._network_labels.get("iccid"), str(iccid), WHITE)
        else:
            self._set_network_value(
                self._network_labels.get("iccid"), "未识别到", RED)

        # 没有识别到SIM卡时，信号值统一按“无”处理，不使用残留dBm等级。
        signal_level = (snapshot.get("signal_level")
                        if snapshot.get("iccid") else None)
        signal_colors = {
            "极好": NETWORK_SIGNAL_VERY_GOOD_COLOR,
            "强": NETWORK_SIGNAL_STRONG_COLOR,
            "一般": NETWORK_SIGNAL_NORMAL_COLOR,
            "弱": NETWORK_SIGNAL_WEAK_COLOR,
            "极弱": NETWORK_SIGNAL_VERY_WEAK_COLOR,
        }
        if signal_level in signal_colors:
            self._set_network_value(
                self._network_labels.get("signal"), signal_level,
                signal_colors[signal_level])
        else:
            # 信号读取失败或没有SIM时明确显示“无”。
            self._set_network_value(
                self._network_labels.get("signal"), "无", WHITE)

        connected = snapshot.get("network_connected")
        if connected is True:
            self._set_network_value(
                self._network_labels.get("connection"),
                "成功连接", NETWORK_CONNECTED_COLOR)
        elif connected is False:
            self._set_network_value(
                self._network_labels.get("connection"),
                "连接失败", NETWORK_DISCONNECTED_COLOR)
        else:
            # 后台 waitNetworkReady() 尚未返回时先留空，避免误报失败。
            self._set_network_value(
                self._network_labels.get("connection"), "", WHITE)

    def show_network(self):
        """三级页面：显示 SIM 卡号、信号等级和网络连接状态。"""
        page, created = self._screen("network", "")
        if created:
            self._create_network_page(page)
        self._load("network", page)

        # 页面打开立即刷新一次信号；对讲期间只读取缓存，避免抢占音频资源。
        if self._network_monitor is not None:
            try:
                if (self._poc_call_active or
                        self._is_audio_priority_active()):
                    self._network_snapshot = (
                        self._network_monitor.get_snapshot())
                else:
                    refresh_now = getattr(
                        self._network_monitor, "refresh_now", None)
                    if refresh_now is not None:
                        self._network_snapshot = refresh_now()
                    else:
                        # 兼容尚未同步 refresh_now() 的旧版监测模块。
                        self._network_snapshot = (
                            self._network_monitor.get_snapshot())
            except Exception as error:
                if not self._network_error_reported:
                    print("[网络] 页面读取缓存失败：{}".format(error))
                    self._network_error_reported = True
        # 页面打开时状态栏图标也立即使用本次最新缓存。
        if not self._screen_sleeping and not self._pending_display_wake:
            self._refresh_status()
        self._refresh_network_page()
        self._network_refresh_pending = False

    def set_battery_charging_state(self, state):
        """设置充电状态：True=充电中，False=未充电，None=未知。"""
        if self._battery_monitor is None:
            return None
        percent = self._battery_monitor.set_charging_state(state)
        if percent is not None:
            self.update_status(battery=percent)
        return percent

    def get_battery_voltage_mv(self):
        """返回滤波后的电池端电压（mV）；尚未完成启动采样时返回 None。"""
        if self._battery_monitor is None:
            return None
        return self._battery_monitor.get_voltage_mv()

    def get_battery_percent(self):
        """返回当前整数电量百分比；尚未完成启动采样时返回 None。"""
        if self._battery_monitor is None:
            return None
        return self._battery_monitor.get_percent()

    def update_status(self, battery=None):
        """更新电量；息屏或页面平移期间只保存数值，不触发重绘。"""
        changed = False
        if battery is not None:
            value = str(battery)
            changed = value != self.status["battery"]
            self.status["battery"] = value

        if (self._screen_sleeping or self._pending_display_wake or
                self._is_audio_priority_active() or
                self._back_transition_active()):
            if changed:
                self._status_refresh_pending = True
            return

        self._refresh_status()
        self._status_refresh_pending = False

    def _back_transition_active(self):
        """返回跟手或收尾动画正在改变页面位置。"""
        return self._back_drag_active or self._back_animating

    def _process_deferred_status_refresh(self):
        """页面平移结束后一次性应用最新电量和网络标签。"""
        if (self._screen_sleeping or self._pending_display_wake or
                self._is_audio_priority_active() or
                self._back_transition_active()):
            return
        if self._status_refresh_pending:
            self._refresh_status()
            self._status_refresh_pending = False
        if (self._network_refresh_pending and self.current is not None and
                self.current[0] == "network"):
            self._refresh_network_page()
            self._network_refresh_pending = False

    def _process_pending_gc(self):
        """页面切换完成后回收 Python 临时对象，并打印剩余堆内存。"""
        if (not self._pending_gc or self._back_transition_active() or
                self._is_audio_priority_active()):
            return
        self._pending_gc = False
        gc.collect()
        try:
            print("[内存] free={} pages={} stack={}".format(
                gc.mem_free(), len(self.pages), len(self.stack)))
        except Exception:
            pass

    def run(self):
        """LVGL 主循环，统一处理显示、触摸和页面返回。"""
        last_tick_ms = utime.ticks_ms()
        last_lvgl_task_ms = last_tick_ms
        while True:
            now = utime.ticks_ms()
            try:
                tick_elapsed = utime.ticks_diff(now, last_tick_ms)
            except Exception:
                tick_elapsed = now - last_tick_ms
            last_tick_ms = now
            if tick_elapsed > 0:
                # 使用真实经过时间，显示刷新或外设操作不会再造成LVGL时钟变慢。
                lv.tick_inc(int(tick_elapsed))

            # 麦权事件优先于本轮LVGL刷新处理，保证弹框能在音频启动前提交。
            self._process_poc_client()
            # 对讲事件会主动请求唤醒；先恢复ST7789，再提交弹框首帧。
            self._process_display_wake()
            force_poc_frame = (
                self._poc_popup_frame_pending_revision is not None)
            run_lvgl_task = True
            if self._is_audio_priority_active() and not force_poc_frame:
                try:
                    run_lvgl_task = (
                        utime.ticks_diff(now, last_lvgl_task_ms) >=
                        AUDIO_LVGL_TASK_INTERVAL_MS)
                except Exception:
                    run_lvgl_task = (
                        now - last_lvgl_task_ms >=
                        AUDIO_LVGL_TASK_INTERVAL_MS)
            if run_lvgl_task:
                # 弹框首帧确认期间只提交紧急弹框，避免返回动画、状态栏
                # 或其他页面脏区排在弹框前面，导致声音先于弹框出现。
                if not force_poc_frame:
                    # 音频期间降低静态页面调度频率；PTT由独立物理按键线程处理。
                    self._process_back_drag_frame()
                    self._process_back_animation()
                    self._process_deferred_status_refresh()
                lv.task_handler()
                last_lvgl_task_ms = utime.ticks_ms()
                self._confirm_poc_popup_frame()
            # 肩灯状态机独立于 LCD 页面和 LCD 息屏状态持续运行。
            self._process_shoulder_lamp()
            # 电池监控同样独立于当前页面和 LCD 息屏状态持续采样。
            self._process_battery_monitor()
            # 网络后台线程只写缓存，所有标签刷新都在 LVGL 主循环执行。
            self._process_network_monitor()
            self._process_audio_volume()
            self._process_login_message()
            self._process_pending_back()
            self._process_pending_gc()
            self._process_auto_sleep()
            utime.sleep_ms(5)
