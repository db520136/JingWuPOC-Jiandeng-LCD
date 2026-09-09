from machine import Pin
a = Pin(Pin.GPIO38, Pin.OUT, Pin.PULL_DISABLE, 0)
# -*- coding: utf-8 -*-
import utime
from misc import Power
from usr import lcd_touch
from usr.poc_client import POCClient
from usr.rtp_audio import (RTPAudioController, HardwareKeyService,
                           DEFAULT_LOCAL_RTP_PORT)
from usr.network_monitor import get_default_monitor
import modem
import uos
import checkNet
from machine import WDT,Timer
FIRMWARE_VER = modem.getDevFwVersion()  #获取到固件版本
SOFTWARE_VER = "1.0.18"                  #软件版本
HARDWARE_VER = "1.0.0"                  #硬件版本
# POC服务器参数集中放在启动入口，后续更换测试服务器无需修改业务模块。
DEVICE_ID = "33030002002000000590"
#


POC_TCP_PORT = 6060
POC_RTP_LOCAL_PORT = DEFAULT_LOCAL_RTP_PORT
# GNSS定位信息上传周期，单位毫秒；后续可由设置页替换该参数。
POC_GNSS_UPLOAD_INTERVAL_MS = 60000
# TCP请求重发参数：首次发送后最多重发3次，等待1秒起步、每次增加500ms。
POC_TCP_MAX_RETRIES = 3
POC_TCP_RETRY_INITIAL_TIMEOUT_MS = 1000
POC_TCP_RETRY_TIMEOUT_STEP_MS = 500
# PCM使用4个底层周期缓冲；RTP接收先缓存5包（约100ms）再开始播放。
POC_PCM_PERIOD_COUNT = 4
POC_RTP_JITTER_PACKETS = 5
# A/B diagnosis switches. Keep all enabled for normal operation; set one to
# False for an isolated touch-frequency run without changing UI code.
AB_ENABLE_NETWORK_MONITOR = True
AB_ENABLE_RTP_AUDIO = True
AB_ENABLE_POC_TCP = True
# Independent A/B switch for the TCP outbound/control worker.  Keep the
# receive/control path enabled for normal operation; disable only for a short
# touch-performance experiment because TCP login/PTT/retry handling pauses.
AB_ENABLE_POC_TCP_TX = True
AB_ENABLE_HARDWARE_KEYS = True
# The watchdog callback runs periodically; keep its old marker available for
# board bring-up without adding unsolicited UART output in normal runs.
WATCHDOG_DEBUG_LOG_ENABLED = False
'''公网'''
'''
POC_TCP_HOST = "125.124.233.231"
POC_HTTP_URL = (
    "http://125.124.233.231:18081/biz/deviceInfo/data?deviceId=" + DEVICE_ID)
POC_LOCATION_MAP_URL = (
    "http://125.124.233.231:18081/biz/tile/crop?"
    "lat={lat}&lon={lon}&width=252&height=210")
POC_LOCATION_ADDRESS_URL = (
    "http://125.124.233.231:18081/map/poi/selection?"
    "longitude={lon}&latitude={lat}")
'''
'''专网'''
POC_TCP_HOST = "68.95.0.31"
POC_HTTP_URL = (
    "http://68.95.0.31:38080/biz/deviceInfo/data?deviceId=" + DEVICE_ID)
POC_LOCATION_MAP_URL = (
    "http://68.95.0.31:38080/biz/tile/crop?"
    "lat={lat}&lon={lon}&width=252&height=210")
POC_LOCATION_ADDRESS_URL = (
    "http://68.95.0.31:38080/map/poi/selection?"
    "longitude={lon}&latitude={lat}")

def cleanup_location_map_temp_files():
    """清理上次断电或脚本重启遗留的地图下载临时文件。"""
    removed = 0
    try:
        names = uos.listdir("/ext")
    except Exception as error:
        print("[Flash] 地图临时文件扫描失败：{}".format(error))
        return 0
    for name in names:
        name = str(name)
        if (not name.startswith("location_map_") or
                not name.endswith(".png.tmp")):
            continue
        path = "/ext/" + name
        try:
            uos.remove(path)
            removed += 1
            print("[Flash] 已清理地图临时文件：{}".format(path))
        except Exception as error:
            print("[Flash] 地图临时文件清理失败：{}，{}".format(
                path, error))
    print("[Flash] 地图临时文件清理完成：{}个".format(removed))
    return removed


def mount_external_flash():
    """在创建JingWuUI前挂载外置NOR Flash，供LVGL加载字体资源。"""
    try:
        device = uos.VfsLfs1(32, 32, 32, "ext_fs", 0, 2)
        uos.mount(device, "/ext")
        print("[Flash] 外置NOR Flash挂载成功：/ext")
        cleanup_location_map_temp_files()
        return True
    except Exception as error:
        # 软重跑main.py时可能已经挂载；此时目录可读即可安全复用。
        try:
            uos.listdir("/ext")
            print("[Flash] 外置NOR Flash已挂载：/ext")
            cleanup_location_map_temp_files()
            return True
        except Exception:
            print("[Flash] 外置NOR Flash挂载失败：{}".format(error))
            return False
        
def feed(t):
    if WATCHDOG_DEBUG_LOG_ENABLED:
        print("11")
    wdt.feed()

# 设备启动入口：先打开外设 3.3V，再创建横屏界面并进入 LVGL 事件循环。
if __name__ == "__main__":
    print("网络监控线程={},RTP音频线程={},POC/TCP线程={},TCP发送控制线程={},物理按键线程={}".format(
        AB_ENABLE_NETWORK_MONITOR, AB_ENABLE_RTP_AUDIO, AB_ENABLE_POC_TCP,
        AB_ENABLE_POC_TCP_TX, AB_ENABLE_HARDWARE_KEYS))
    # LCD 供电恢复前先关闭背光，避免 ST7789 尚未刷新时显示历史 GRAM 内容。
    lcd_touch.prepare_backlight_off()
    #EN_3V3 = Pin(Pin.GPIO12, Pin.OUT, Pin.PULL_DISABLE, 1)
    #EN_3V3.write(1)
    # 给 LCD 电源和控制器留出稳定时间，不再固定黑屏等待 10 秒。
    #utime.sleep_ms(100)
    mount_external_flash()
    print("开机原因112221：",Power.powerOnReason())
    print("关机原因：",Power.powerDownReason())

    wdt = WDT(20)  # 启动看门狗，设置超时时间
    timer1 = Timer(Timer.Timer1)
    timer1.start(period=15000, mode=timer1.PERIODIC, callback=feed)  # 使用定时器喂狗

    # 先创建POC对象但不启动线程，避免TCP线程与LCD/LVGL的大块内存分配并发，
    # EC800M启动阶段内存碎片或原生驱动资源竞争可能导致无Python异常的重启。
    try:
        network_monitor = get_default_monitor(False)
    except TypeError:
        # 兼容设备中尚未同步支持start_worker参数的旧版network_monitor。
        network_monitor = get_default_monitor()
    poc_client = POCClient(
        device_id=DEVICE_ID,
        tcp_host=POC_TCP_HOST,
        tcp_port=POC_TCP_PORT,
        http_url=POC_HTTP_URL,
        location_map_url=POC_LOCATION_MAP_URL,
        location_address_url=POC_LOCATION_ADDRESS_URL,
        network_monitor=(network_monitor if AB_ENABLE_NETWORK_MONITOR else None),
        gnss_upload_interval_ms=POC_GNSS_UPLOAD_INTERVAL_MS,
        tcp_max_retries=POC_TCP_MAX_RETRIES,
        tcp_retry_initial_timeout_ms=POC_TCP_RETRY_INITIAL_TIMEOUT_MS,
        tcp_retry_timeout_step_ms=POC_TCP_RETRY_TIMEOUT_STEP_MS,
        enable_tx_worker=AB_ENABLE_POC_TCP_TX,
    )
    # RTP 与 TCP 使用同一服务器地址；服务器UDP端口由0x88/0x0B应答更新。  ptt 27
    rtp_audio = RTPAudioController(
        server_ip=POC_TCP_HOST,
        local_port=POC_RTP_LOCAL_PORT,
        speaker_gpio=27,
        pcm_periodcnt=POC_PCM_PERIOD_COUNT,
        jitter_packets=POC_RTP_JITTER_PACKETS,
    )
    poc_client.set_audio_controller(rtp_audio)
    ui = lcd_touch.JingWuUI(
        firmware_version=FIRMWARE_VER,
        software_version=SOFTWARE_VER,
        hardware_version=HARDWARE_VER,
        poc_client=poc_client,
    )
    # 此时登录页和LCD首帧已准备完成，再启动网络和POC线程；
    # 进入run()后网络连接、登录页面和LVGL主循环并行运行。
    if AB_ENABLE_NETWORK_MONITOR and not network_monitor.start_network_worker():
        print("[网络] 后台检测线程未能启动或已经运行")
    if AB_ENABLE_RTP_AUDIO and not rtp_audio.start():
        print("[RTP] 音频服务未能启动")
    if AB_ENABLE_POC_TCP and not poc_client.start(
            enable_tx_worker=AB_ENABLE_POC_TCP_TX):
        print("[POC] 后台线程未能启动")
    hardware_key_service = HardwareKeyService(
        rtp_audio,
        on_activity=ui.notify_activity,
        ptt_gpio=22,#28,
        volume_up_gpio=30,
        volume_down_gpio=31,
    )
    if AB_ENABLE_HARDWARE_KEYS and not hardware_key_service.start():
        print("[按键] 物理按键服务未能启动")
    ui.run()

