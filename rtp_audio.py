# -*- coding: utf-8 -*-
"""POC RTP 音频和物理 PTT 服务。

音频使用 G.711 A-law（PCMA），每个 RTP 包携带 160 字节、20 ms 语音。
接收端使用小型 RTP 抖动缓冲完成排序和匀速播放；PCM periodcnt 继续作为
底层播放/录音缓冲，两者用途不同。
"""

import utime

try:
    import usocket as _socket
except Exception:
    try:
        import socket as _socket
    except Exception:
        _socket = None

try:
    import _thread
except Exception:
    _thread = None

try:
    import audio
except Exception:
    audio = None

try:
    import G711
except Exception:
    G711 = None

try:
    from machine import Pin
except Exception:
    Pin = None

try:
    import dataCall
except Exception:
    dataCall = None


RTP_HEADER_SIZE = 12
RTP_PAYLOAD_TYPE_PCMA = 8
RTP_PAYLOAD_SIZE = 160
RTP_PACKET_INTERVAL_MS = 20
RTP_PROBE_INTERVAL_MS = 30000
RTP_STATS_INTERVAL_MS = 5000
RTP_RECEIVE_BURST = 6
RTP_JITTER_MAX_PACKETS = 24
RTP_RX_AUDIO_TIMEOUT_MS = 200
RTP_SOCKET_RECEIVE_BUFFER = 4096
RTP_TX_BACKLOG_LIMIT = 4
RTP_PCM_FREE_RESERVE = 1
# LCD提交弹框后再留10ms给SPI/DMA；发送与接收分别设置异常兜底时间。
RTP_UI_READY_SETTLE_MS = 10
RTP_TX_UI_READY_TIMEOUT_MS = 250
RTP_RX_UI_READY_SETTLE_MS = 80
# 息屏唤醒含ST7789的120ms等待和全屏刷新，接收端放宽到450ms；
# 仍小于24包RTP缓冲约480ms，避免等待弹框造成接收缓冲溢出。
RTP_RX_UI_READY_TIMEOUT_MS = 450
# 远端占麦后主动建立蜂窝UDP映射；收到首个有效语音包即停止后续重试。
RTP_RX_PROBE_DELAYS_MS = (0, 100, 300)
RTP_RX_FIRST_PACKET_WARN_MS = 500
DEFAULT_JITTER_PACKETS = 5
DEFAULT_LOCAL_RTP_PORT = 4001
DEFAULT_SSRC = 0x13572468
PCMA_SILENCE_FRAME = bytes((0xD5,)) * RTP_PAYLOAD_SIZE


def _ticks_ms():
    return utime.ticks_ms()


def _ticks_diff(now, then):
    try:
        return utime.ticks_diff(now, then)
    except Exception:
        return now - then


def _ticks_add(base, delta):
    try:
        return utime.ticks_add(base, delta)
    except Exception:
        return base + delta


def build_rtp_packet(sequence, timestamp, ssrc, payload=b"", marker=0,
                     payload_type=RTP_PAYLOAD_TYPE_PCMA):
    """构造 12 字节 RTP 固定头和负载。"""
    sequence = int(sequence) & 0xFFFF
    timestamp = int(timestamp) & 0xFFFFFFFF
    ssrc = int(ssrc) & 0xFFFFFFFF
    if not isinstance(payload, bytes):
        payload = bytes(payload)
    return (bytes((0x80, ((int(marker) & 1) << 7) |
                   (int(payload_type) & 0x7F))) +
            bytes(((sequence >> 8) & 0xFF, sequence & 0xFF)) +
            bytes(((timestamp >> 24) & 0xFF, (timestamp >> 16) & 0xFF,
                   (timestamp >> 8) & 0xFF, timestamp & 0xFF)) +
            bytes(((ssrc >> 24) & 0xFF, (ssrc >> 16) & 0xFF,
                   (ssrc >> 8) & 0xFF, ssrc & 0xFF)) + payload)


def parse_rtp_packet(packet):
    """按 RTP 位字段解析头部，兼容 CSRC、扩展头和 Padding。"""
    if packet is None or len(packet) < RTP_HEADER_SIZE:
        return None
    data = bytes(packet)
    first = data[0]
    second = data[1]
    offset = RTP_HEADER_SIZE + (first & 0x0F) * 4
    if len(data) < offset:
        return None
    if first & 0x10:
        if len(data) < offset + 4:
            return None
        ext_words = (data[offset + 2] << 8) | data[offset + 3]
        offset += 4 + ext_words * 4
        if len(data) < offset:
            return None
    end = len(data)
    if first & 0x20:
        pad = data[-1]
        if pad <= 0 or pad > end - offset:
            return None
        end -= pad
    if end < offset:
        return None
    return {
        "version": (first >> 6) & 0x03,
        "marker": (second >> 7) & 1,
        "payload_type": second & 0x7F,
        "sequence": (data[2] << 8) | data[3],
        "timestamp": ((data[4] << 24) | (data[5] << 16) |
                      (data[6] << 8) | data[7]),
        "ssrc": ((data[8] << 24) | (data[9] << 16) |
                 (data[10] << 8) | data[11]),
        "payload": data[offset:end],
    }


class RTPAudioController:
    """RTP 收发、PCM/G711 和喇叭使能控制器。"""

    def __init__(self, server_ip, local_port=DEFAULT_LOCAL_RTP_PORT,
                 speaker_gpio=27, pcm_periodcnt=3, ssrc=DEFAULT_SSRC,
                 jitter_packets=DEFAULT_JITTER_PACKETS):
        self.server_ip = str(server_ip)
        self.local_port = int(local_port)
        self.server_port = None
        self.speaker_gpio = int(speaker_gpio)
        self.pcm_periodcnt = max(2, min(25, int(pcm_periodcnt)))
        self.jitter_packets = max(2, min(8, int(jitter_packets)))
        self.ssrc = int(ssrc) & 0xFFFFFFFF
        self.socket = None
        self.pcm = None
        self.g711 = None
        self.audio_device = None
        self.speaker_pin = None
        self._speaker_enabled = False
        self._running = False
        self._ptt_pressed = False
        self._tx_active = False
        self._start_tx_pending = False
        self._tx_group_id = None
        self._rx_group_id = None
        self._last_rx_ms = None
        self._last_probe_ms = None
        self._next_tx_ms = None
        self._sequence = _ticks_ms() & 0xFFFF
        self._timestamp = (_ticks_ms() * 8) & 0xFFFFFFFF
        self._first_tx_marker = 0
        self._tx_cache = bytearray()
        self._tx_burst_count = 0
        self._last_tx_packet_ms = None
        self._rx_buffer = {}
        self._rx_expected_sequence = None
        self._rx_highest_sequence = None
        self._rx_playout_started = False
        self._rx_announced_ms = None
        self._rx_last_arrival_ms = None
        self._next_playout_ms = None
        self._rx_underrun_active = False
        self._ui_ready_revision = None
        self._ui_ready_ms = None
        self._tx_ui_revision = None
        self._tx_ui_wait_started_ms = None
        self._tx_ui_timeout_reported = False
        self._rx_ui_revision = None
        self._rx_ui_wait_started_ms = None
        self._rx_ui_timeout_reported = False
        self._rx_probe_group_id = None
        self._rx_probe_attempt = 0
        self._rx_probe_next_ms = None
        self._rx_no_packet_warned = False
        self._stats_started_ms = _ticks_ms()
        self._stats = self._new_stats()
        self.volume = 11
        self.volume_revision = 0
        self.on_request_floor = None
        self.on_release_floor = None

    def _set_speaker(self, enabled):
        enabled = bool(enabled)
        if enabled == self._speaker_enabled:
            return
        if self.speaker_pin is None:
            return
        try:
            self.speaker_pin.write(1 if enabled else 0)
            self._speaker_enabled = enabled
        except Exception:
            pass

    @staticmethod
    def _new_stats():
        return {
            "tx_packets": 0,
            "tx_errors": 0,
            "tx_interval_count": 0,
            "tx_interval_sum": 0,
            "tx_interval_max": 0,
            "tx_schedule_late": 0,
            "capture_count": 0,
            "capture_bytes": 0,
            "capture_time_sum": 0,
            "capture_time_max": 0,
            "rx_packets": 0,
            "rx_invalid": 0,
            "rx_duplicate": 0,
            "rx_reordered": 0,
            "rx_late": 0,
            "rx_missing": 0,
            "rx_played": 0,
            "rx_silence": 0,
            "rx_write_errors": 0,
            "rx_pcm_underrun": 0,
            "rx_overflow_drop": 0,
            "rx_resync": 0,
            "rx_interval_count": 0,
            "rx_interval_sum": 0,
            "rx_interval_max": 0,
            "rx_buffer_max": 0,
            "pcm_count_min": None,
            "pcm_count_max": None,
        }

    def _stats_have_data(self):
        stats = self._stats
        return bool(stats["tx_packets"] or stats["tx_errors"] or
                    stats["rx_packets"] or stats["rx_invalid"] or
                    stats["rx_write_errors"])

    def _report_stats(self, now_ms=None, force=False):
        """只在音频结束后打印，避免串口日志阻塞实时收发线程。"""
        if now_ms is None:
            now_ms = _ticks_ms()
        if not force and self.is_audio_priority_active():
            return
        if (not force and
                _ticks_diff(now_ms, self._stats_started_ms) <
                RTP_STATS_INTERVAL_MS):
            return
        if self._stats_have_data():
            stats = self._stats
            tx_avg = (stats["tx_interval_sum"] //
                      stats["tx_interval_count"]
                      if stats["tx_interval_count"] else 0)
            capture_avg = (stats["capture_time_sum"] //
                           stats["capture_count"]
                           if stats["capture_count"] else 0)
            capture_size_avg = (stats["capture_bytes"] //
                                stats["capture_count"]
                                if stats["capture_count"] else 0)
            rx_avg = (stats["rx_interval_sum"] //
                      stats["rx_interval_count"]
                      if stats["rx_interval_count"] else 0)
            pcm_range = "--"
            if stats["pcm_count_min"] is not None:
                pcm_range = "{}~{}".format(
                    stats["pcm_count_min"], stats["pcm_count_max"])
            print(
                "[RTP统计] TX 包={} 间隔={}/{}ms 采集={}/{}ms/{}B "
                "错误={} 超时={}；RX 包={} 到达={}/{}ms 播放={} "
                "缺包={} 补静音={} 乱序={} 重复={} 迟到={} "
                "PCM欠载={} 溢出丢包={} 重同步={} 缓冲峰值={} PCM帧={}".format(
                    stats["tx_packets"], tx_avg,
                    stats["tx_interval_max"], capture_avg,
                    stats["capture_time_max"], capture_size_avg,
                    stats["tx_errors"],
                    stats["tx_schedule_late"], stats["rx_packets"],
                    rx_avg, stats["rx_interval_max"],
                    stats["rx_played"], stats["rx_missing"],
                    stats["rx_silence"], stats["rx_reordered"],
                    stats["rx_duplicate"], stats["rx_late"],
                    stats["rx_pcm_underrun"],
                    stats["rx_overflow_drop"], stats["rx_resync"],
                    stats["rx_buffer_max"], pcm_range))
        self._stats = self._new_stats()
        self._stats_started_ms = now_ms

    def _reset_receive_state(self):
        self._rx_buffer = {}
        self._rx_expected_sequence = None
        self._rx_highest_sequence = None
        self._rx_playout_started = False
        self._rx_last_arrival_ms = None
        self._last_rx_ms = None
        self._next_playout_ms = None
        self._rx_underrun_active = False
        self._set_speaker(False)

    def is_audio_priority_active(self):
        """供LCD主循环判断是否应延后非关键任务。"""
        if self._tx_active or self._start_tx_pending:
            return True
        if self._rx_group_id is None:
            return False
        if self._rx_playout_started:
            return True
        now = _ticks_ms()
        reference = self._last_rx_ms
        if reference is None:
            reference = self._rx_announced_ms
        return (reference is not None and
                _ticks_diff(now, reference) < RTP_RX_AUDIO_TIMEOUT_MS)

    def _init_hardware(self):
        if audio is None or G711 is None:
            raise RuntimeError("audio/G711 模块不可用")
        if self.speaker_pin is None and Pin is not None:
            pin_id = getattr(
                Pin, "GPIO{}".format(self.speaker_gpio), self.speaker_gpio)
            self.speaker_pin = Pin(
                pin_id, Pin.OUT, getattr(Pin, "PULL_DISABLE", 0), 0)
        self._set_speaker(False)
        if self.audio_device is None:
            self.audio_device = audio.Audio(0)
            try:
                self.audio_device.setVolume(self.volume)
            except Exception:
                pass
        if self.pcm is None:
            self.pcm = audio.Audio.PCM(
                0, 1, 8000, 2, 1, self.pcm_periodcnt)
            # QuecPython 的 G711 在部分固件中是可调用模块，部分是模块内类。
            factory = getattr(G711, "G711", G711)
            self.g711 = factory(self.pcm)

    def _init_socket(self):
        if _socket is None:
            raise RuntimeError("socket 模块不可用")
        if self.socket is not None:
            return
        protocol = getattr(_socket, "IPPROTO_UDP", 17)
        try:
            self.socket = _socket.socket(
                _socket.AF_INET, _socket.SOCK_DGRAM, protocol)
            try:
                self.socket.setsockopt(
                    _socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            except Exception:
                pass
            try:
                receive_buffer_option = getattr(_socket, "SO_RCVBUF")
                self.socket.setsockopt(
                    _socket.SOL_SOCKET, receive_buffer_option,
                    RTP_SOCKET_RECEIVE_BUFFER)
            except Exception:
                # 部分QuecPython固件未开放SO_RCVBUF，不影响基本收发。
                pass
            try:
                # 空地址由蜂窝网络栈选择当前 PDP 上下文的本机地址。
                self.socket.bind(("0.0.0.0", self.local_port))
            except Exception:
                if dataCall is None:
                    raise
                local_ip = dataCall.getInfo(1, 0)[2][2]
                self.socket.bind((local_ip, self.local_port))
            try:
                self.socket.setblocking(False)
            except Exception:
                # 极旧固件没有setblocking时，用短超时保证发送线程不会卡住。
                self.socket.settimeout(0.02)
        except Exception:
            sock = self.socket
            self.socket = None
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise

    def start(self):
        if self._running:
            return True
        if _thread is None:
            print("[RTP] QuecPython 缺少 _thread")
            return False
        try:
            self._init_hardware()
            self._running = True
            _thread.start_new_thread(self._worker, ())
            print("[RTP] 音频线程已启动：本地端口={}，PCM periodcnt={}，"
                  "抖动缓冲={}包".format(
                      self.local_port, self.pcm_periodcnt,
                      self.jitter_packets))
            return True
        except Exception as error:
            self._running = False
            print("[RTP] 服务启动失败：{}".format(error))
            return False

    def stop(self):
        self._running = False
        self._tx_active = False
        self._rx_group_id = None
        self._rx_announced_ms = None
        self._reset_receive_state()
        self._report_stats(force=True)
        self._set_speaker(False)
        sock = self.socket
        self.socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if self.pcm is not None:
            try:
                self.pcm.close()
            except Exception:
                pass
            self.pcm = None
            self.g711 = None

    def set_server(self, server_ip=None, server_port=None):
        if server_ip is not None:
            self.server_ip = str(server_ip)
        if server_port is not None:
            new_port = int(server_port)
            if new_port != self.server_port:
                # 新端口尚未建立UDP映射，不能伪装成刚发送过心跳。
                self._last_probe_ms = None
            self.server_port = new_port

    def on_floor_ui_ready(self, event_revision):
        """记录LCD已经提交的麦权弹框首帧，供RTP线程解除音频门控。"""
        try:
            revision = int(event_revision)
        except Exception:
            return
        self._ui_ready_revision = revision
        self._ui_ready_ms = _ticks_ms()

    def _ui_gate_state(self, event_revision, started_ms, now_ms,
                       timeout_ms, settle_ms=RTP_UI_READY_SETTLE_MS):
        """返回(可启动, 是否因LCD超时而放行)。"""
        if event_revision is None:
            return True, False
        if (self._ui_ready_revision == event_revision and
                self._ui_ready_ms is not None and
                _ticks_diff(now_ms, self._ui_ready_ms) >=
                int(settle_ms)):
            return True, False
        if (started_ms is not None and
                _ticks_diff(now_ms, started_ms) >=
                int(timeout_ms)):
            return True, True
        return False, False

    def _clear_receive_probe(self):
        self._rx_probe_group_id = None
        self._rx_probe_attempt = 0
        self._rx_probe_next_ms = None

    def _schedule_receive_probe(self, group_id, now_ms):
        self._rx_probe_group_id = int(group_id) & 0xFFFFFFFF
        self._rx_probe_attempt = 0
        self._rx_probe_next_ms = now_ms
        self._rx_no_packet_warned = False

    def _process_receive_probe(self, now_ms):
        """在RTP线程中发送接收首包，避免TCP线程并发操作UDP socket。"""
        if (self._rx_probe_next_ms is None or
                self._rx_group_id is None or
                self._rx_probe_group_id != self._rx_group_id):
            return
        if _ticks_diff(now_ms, self._rx_probe_next_ms) < 0:
            return
        attempt = self._rx_probe_attempt
        total = len(RTP_RX_PROBE_DELAYS_MS)
        self._send_probe("接收首包{}/{}".format(attempt + 1, total))
        attempt += 1
        self._rx_probe_attempt = attempt
        if attempt >= total:
            self._rx_probe_next_ms = None
            return
        self._rx_probe_next_ms = _ticks_add(
            self._rx_announced_ms, RTP_RX_PROBE_DELAYS_MS[attempt])

    def _check_receive_start_timeout(self, now_ms):
        if (self._rx_group_id is None or self._last_rx_ms is not None or
                self._rx_announced_ms is None or
                self._rx_no_packet_warned or
                _ticks_diff(now_ms, self._rx_announced_ms) <
                RTP_RX_FIRST_PACKET_WARN_MS):
            return
        self._rx_no_packet_warned = True
        print("[RTP] 远端占麦500ms仍未收到语音包：group_id={}，目标={}:{}".format(
            self._rx_group_id, self.server_ip, self.server_port))

    def set_volume(self, level):
        """设置 0~11 音量等级，供 LCD 页面和物理音量键共用。"""
        level = max(0, min(11, int(level)))
        if level == self.volume:
            return level
        self.volume = level
        self.volume_revision += 1
        if self.audio_device is not None:
            try:
                self.audio_device.setVolume(level)
            except Exception:
                pass
        if self.pcm is not None:
            try:
                self.pcm.setVolume(level)
            except Exception:
                pass
        return level

    def change_volume(self, delta):
        return self.set_volume(self.volume + int(delta))

    def get_volume_snapshot(self):
        return {"value": self.volume, "revision": self.volume_revision}

    def set_ptt_pressed(self, pressed):
        """PTT 按下请求麦权，松开立即停发语音并请求释放麦权。"""
        pressed = bool(pressed)
        if pressed == self._ptt_pressed:
            return
        self._ptt_pressed = pressed
        if pressed:
            if self.on_request_floor is not None:
                if not self.on_request_floor():
                    self._ptt_pressed = False
            return
        self._stop_tx()
        if self.on_release_floor is not None:
            self.on_release_floor()

    def on_floor_granted(self, group_id, udp_port, event_revision=None):
        self.server_port = int(udp_port)
        self._last_probe_ms = None
        self._tx_group_id = int(group_id) & 0xFFFFFFFF
        self._tx_ui_revision = (None if event_revision is None
                                else int(event_revision))
        self._tx_ui_wait_started_ms = _ticks_ms()
        self._tx_ui_timeout_reported = False
        self._rx_group_id = None
        self._rx_announced_ms = None
        self._rx_ui_revision = None
        self._rx_ui_wait_started_ms = None
        self._clear_receive_probe()
        self._reset_receive_state()
        if self._ptt_pressed:
            # TCP线程只发布状态，首包和语音统一由RTP线程发送。
            self._start_tx_pending = True

    def on_floor_released(self, group_id=None):
        self._stop_tx()
        if group_id is None or self._rx_group_id == int(group_id):
            self._report_stats(force=True)
            self._rx_group_id = None
            self._rx_announced_ms = None
            self._rx_ui_revision = None
            self._rx_ui_wait_started_ms = None
            self._clear_receive_probe()
            self._reset_receive_state()

    def stop_transmit(self):
        """立即停止本地PCM采集和RTP发送，不等待TCP释放麦应答。"""
        self._stop_tx()

    def on_floor_occupied(self, group_id, udp_port, event_revision=None):
        """服务器通知远端讲话，后续 RTP 音频归属于该组。"""
        self.server_port = int(udp_port)
        self._last_probe_ms = None
        self._stop_tx()
        self._reset_receive_state()
        self._rx_group_id = int(group_id) & 0xFFFFFFFF
        self._rx_announced_ms = _ticks_ms()
        self._rx_ui_revision = (None if event_revision is None
                                else int(event_revision))
        self._rx_ui_wait_started_ms = self._rx_announced_ms
        self._rx_ui_timeout_reported = False
        self._schedule_receive_probe(self._rx_group_id,
                                     self._rx_announced_ms)
        print("[RTP] 远端讲话准备接收：group_id={}，本地端口={}，目标={}:{}".format(
            self._rx_group_id, self.local_port,
            self.server_ip, self.server_port))

    def on_floor_idle(self, group_id):
        if self._rx_group_id == int(group_id):
            self._report_stats(force=True)
            self._rx_group_id = None
            self._rx_announced_ms = None
            self._rx_ui_revision = None
            self._rx_ui_wait_started_ms = None
            self._clear_receive_probe()
            self._reset_receive_state()

    def _stop_tx(self):
        was_active = self._tx_active
        self._tx_active = False
        self._start_tx_pending = False
        self._tx_cache = bytearray()
        self._tx_burst_count = 0
        self._tx_group_id = None
        self._next_tx_ms = None
        self._tx_ui_revision = None
        self._tx_ui_wait_started_ms = None
        self._tx_ui_timeout_reported = False
        if was_active:
            self._report_stats(force=True)

    def _begin_tx(self):
        if self.socket is None or self.server_port is None or self.g711 is None:
            return False
        # 每次正式发语音前先发首包，刷新服务器和 NAT 的 UDP 映射。
        if not self._send_probe("首包"):
            return False
        self._tx_active = True
        self._first_tx_marker = 1
        self._tx_cache = bytearray()
        self._tx_burst_count = 0
        self._last_tx_packet_ms = None
        self._next_tx_ms = None
        print("[RTP] 开始发送语音：group_id={}".format(self._tx_group_id))
        return True

    def _send_probe(self, reason="心跳"):
        """发送 12 字节 RTP 头加 1 字节 0x00 的 UDP 探测包。"""
        if self.socket is None or self.server_port is None:
            return False
        # 记录发送尝试时间，失败时也避免主循环每1ms重复发送普通心跳。
        self._last_probe_ms = _ticks_ms()
        packet = build_rtp_packet(
            self._sequence, self._timestamp, self.ssrc, b"\x00")
        try:
            self.socket.sendto(packet, (self.server_ip, self.server_port))
            self._sequence = (self._sequence + 1) & 0xFFFF
            print("[RTP] 已发送UDP{}：{}:{}".format(
                reason, self.server_ip, self.server_port))
            return True
        except Exception as error:
            print("[RTP] UDP{}发送失败：{}".format(reason, error))
            return False

    def _send_audio_packet(self, payload):
        if len(payload) != RTP_PAYLOAD_SIZE:
            return False
        packet = build_rtp_packet(
            self._sequence, self._timestamp, self.ssrc,
            payload, self._first_tx_marker)
        self._first_tx_marker = 0
        try:
            self.socket.sendto(packet, (self.server_ip, self.server_port))
            now = _ticks_ms()
            if self._last_tx_packet_ms is not None:
                interval = _ticks_diff(now, self._last_tx_packet_ms)
                if 0 < interval < 1000:
                    self._stats["tx_interval_count"] += 1
                    self._stats["tx_interval_sum"] += interval
                    if interval > self._stats["tx_interval_max"]:
                        self._stats["tx_interval_max"] = interval
                    if interval >= RTP_PACKET_INTERVAL_MS * 2:
                        self._stats["tx_schedule_late"] += 1
            self._last_tx_packet_ms = now
            self._stats["tx_packets"] += 1
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._timestamp = (
                self._timestamp + RTP_PAYLOAD_SIZE) & 0xFFFFFFFF
            return True
        except Exception as error:
            self._stats["tx_errors"] += 1
            print("[RTP] UDP语音发送失败：{}".format(error))
            return False

    def _capture_audio(self):
        # 缓存中已有完整帧时先发送一包，剩余数据留给下一个20ms时隙。
        if len(self._tx_cache) >= RTP_PAYLOAD_SIZE:
            payload = bytes(self._tx_cache[:RTP_PAYLOAD_SIZE])
            self._tx_cache = bytearray(self._tx_cache[RTP_PAYLOAD_SIZE:])
            return self._send_audio_packet(payload)
        read_started = _ticks_ms()
        try:
            data = self.g711.read(0)
        except Exception as error:
            print("[RTP] G711采集失败：{}".format(error))
            return False
        read_elapsed = max(0, _ticks_diff(_ticks_ms(), read_started))
        self._stats["capture_count"] += 1
        self._stats["capture_time_sum"] += read_elapsed
        if read_elapsed > self._stats["capture_time_max"]:
            self._stats["capture_time_max"] = read_elapsed
        if not data:
            return False
        if not isinstance(data, (bytes, bytearray)):
            try:
                data = bytes(data)
            except Exception:
                return False
        self._stats["capture_bytes"] += len(data)
        self._tx_cache.extend(data)
        if len(self._tx_cache) >= RTP_PAYLOAD_SIZE:
            payload = bytes(self._tx_cache[:RTP_PAYLOAD_SIZE])
            # QuecPython bytearray 不支持切片删除，重建剩余缓存。
            self._tx_cache = bytearray(self._tx_cache[RTP_PAYLOAD_SIZE:])
            return self._send_audio_packet(payload)
        return False

    def _process_transmit(self):
        """由PCM采集数据驱动发送，不再依赖Python的20ms软件定时。"""
        return self._capture_audio()

    def _queue_received_packet(self, packet):
        parsed = parse_rtp_packet(packet)
        if parsed is None or parsed["payload_type"] != RTP_PAYLOAD_TYPE_PCMA:
            self._stats["rx_invalid"] += 1
            return
        payload = parsed["payload"]
        # 1字节的首包/心跳只维持链路，不能送入 G711 解码器。
        if len(payload) != RTP_PAYLOAD_SIZE:
            return
        if self._rx_group_id is None:
            return

        now = _ticks_ms()
        first_audio_packet = self._last_rx_ms is None
        self._stats["rx_packets"] += 1
        if self._rx_last_arrival_ms is not None:
            interval = _ticks_diff(now, self._rx_last_arrival_ms)
            if 0 <= interval < 1000:
                self._stats["rx_interval_count"] += 1
                self._stats["rx_interval_sum"] += interval
                if interval > self._stats["rx_interval_max"]:
                    self._stats["rx_interval_max"] = interval
        self._rx_last_arrival_ms = now
        self._last_rx_ms = now
        if first_audio_packet:
            self._rx_probe_next_ms = None
            delay_ms = (0 if self._rx_announced_ms is None else
                        max(0, _ticks_diff(now, self._rx_announced_ms)))
            print("[RTP] 已收到首个语音包：group_id={}，耗时={}ms".format(
                self._rx_group_id, delay_ms))

        sequence = int(parsed["sequence"]) & 0xFFFF
        if sequence in self._rx_buffer:
            self._stats["rx_duplicate"] += 1
            return

        if self._rx_expected_sequence is None:
            self._rx_expected_sequence = sequence
        else:
            forward = (sequence - self._rx_expected_sequence) & 0xFFFF
            if self._rx_playout_started and forward > 0x8000:
                self._stats["rx_late"] += 1
                return
            if not self._rx_playout_started and forward > 0x8000:
                backward = ((self._rx_expected_sequence - sequence) &
                            0xFFFF)
                if backward <= RTP_JITTER_MAX_PACKETS:
                    self._rx_expected_sequence = sequence
                else:
                    self._stats["rx_late"] += 1
                    return

        if self._rx_highest_sequence is None:
            self._rx_highest_sequence = sequence
        else:
            high_delta = (sequence - self._rx_highest_sequence) & 0xFFFF
            if high_delta == 0:
                self._stats["rx_duplicate"] += 1
                return
            if high_delta > 0x8000:
                self._stats["rx_reordered"] += 1
            else:
                self._rx_highest_sequence = sequence

        if len(self._rx_buffer) >= RTP_JITTER_MAX_PACKETS:
            # 保留已经排好序的连续音频，只丢弃本次新到包；不能清空整个缓冲。
            self._stats["rx_overflow_drop"] += 1
            return
        self._rx_buffer[sequence] = bytes(payload)
        buffered = len(self._rx_buffer)
        if buffered > self._stats["rx_buffer_max"]:
            self._stats["rx_buffer_max"] = buffered

    def _receive_available(self, max_packets=None):
        received = 0
        limit = (RTP_RECEIVE_BURST if max_packets is None else
                 max(1, int(max_packets)))
        while received < limit:
            try:
                packet, _address = self.socket.recvfrom(1024)
            except Exception:
                break
            received += 1
            self._queue_received_packet(packet)

    def _get_pcm_free_count(self):
        getter = getattr(self.pcm, "getWriteFrameCount", None)
        if getter is None:
            return None
        try:
            count = int(getter())
        except Exception:
            return None
        count = max(0, min(self.pcm_periodcnt, count))
        current_min = self._stats["pcm_count_min"]
        current_max = self._stats["pcm_count_max"]
        if current_min is None or count < current_min:
            self._stats["pcm_count_min"] = count
        if current_max is None or count > current_max:
            self._stats["pcm_count_max"] = count
        return count

    def _write_playout_frame(self, payload, silence=False):
        try:
            self._set_speaker(True)
            self.g711.write(payload, 0)
            self._stats["rx_played"] += 1
            if silence:
                self._stats["rx_silence"] += 1
            return True
        except Exception as error:
            self._stats["rx_write_errors"] += 1
            self._set_speaker(False)
            print("[RTP] G711播放失败：{}".format(error))
            return False

    def _nearest_buffered_sequence(self):
        """查找相对待播放序号最近的未来数据包。"""
        expected = self._rx_expected_sequence
        if expected is None or not self._rx_buffer:
            return None, None
        nearest = None
        nearest_delta = 0x10000
        for sequence in self._rx_buffer.keys():
            delta = (sequence - expected) & 0xFFFF
            if delta < 0x8000 and delta < nearest_delta:
                nearest = sequence
                nearest_delta = delta
        return nearest, nearest_delta

    def _take_next_playout_payload(self):
        """按序取出下一帧；小缺口补静音，大缺口只移动序号但保留有效包。"""
        expected = self._rx_expected_sequence
        if expected is None:
            return None, False
        payload = self._rx_buffer.pop(expected, None)
        if payload is not None:
            self._rx_expected_sequence = (expected + 1) & 0xFFFF
            return payload, False

        nearest, delta = self._nearest_buffered_sequence()
        if nearest is None:
            return None, False
        if delta >= RTP_JITTER_MAX_PACKETS:
            self._stats["rx_missing"] += delta
            self._stats["rx_resync"] += 1
            self._rx_expected_sequence = nearest
            payload = self._rx_buffer.pop(nearest)
            self._rx_expected_sequence = (nearest + 1) & 0xFFFF
            return payload, False

        self._stats["rx_missing"] += 1
        self._rx_expected_sequence = (expected + 1) & 0xFFFF
        return PCMA_SILENCE_FRAME, True

    def _start_playout(self, now_ms):
        reset_buffer = getattr(self.pcm, "resetWriteBuffer", None)
        if reset_buffer is not None:
            try:
                reset_buffer()
            except Exception:
                pass
        self._rx_playout_started = True
        self._rx_underrun_active = False
        # 无getWriteFrameCount的旧固件才使用该20ms兼容时钟。
        self._next_playout_ms = now_ms

    def _finish_playout(self):
        self._rx_playout_started = False
        self._rx_expected_sequence = None
        self._rx_highest_sequence = None
        self._next_playout_ms = None
        self._rx_underrun_active = False
        self._set_speaker(False)
        self._report_stats(force=True)

    def _process_playout_timed_fallback(self, now_ms):
        """兼容没有PCM空闲帧接口的旧固件。"""
        if _ticks_diff(now_ms, self._next_playout_ms) < 0:
            return
        payload, silence = self._take_next_playout_payload()
        if payload is None:
            recently_received = (
                self._last_rx_ms is not None and
                _ticks_diff(now_ms, self._last_rx_ms) <
                RTP_RX_AUDIO_TIMEOUT_MS)
            if not recently_received:
                self._finish_playout()
            return
        self._write_playout_frame(payload, silence)
        next_ms = _ticks_add(
            self._next_playout_ms, RTP_PACKET_INTERVAL_MS)
        finished_ms = _ticks_ms()
        if _ticks_diff(finished_ms, next_ms) >= RTP_PACKET_INTERVAL_MS:
            if not self._rx_underrun_active:
                self._stats["rx_pcm_underrun"] += 1
                self._rx_underrun_active = True
            next_ms = _ticks_add(finished_ms, RTP_PACKET_INTERVAL_MS)
        else:
            self._rx_underrun_active = False
        self._next_playout_ms = next_ms

    def _process_playout(self, now_ms):
        if self._rx_group_id is None:
            return
        ui_ready, ui_timed_out = self._ui_gate_state(
            self._rx_ui_revision, self._rx_ui_wait_started_ms, now_ms,
            RTP_RX_UI_READY_TIMEOUT_MS, RTP_RX_UI_READY_SETTLE_MS)
        if not ui_ready:
            return
        if ui_timed_out and not self._rx_ui_timeout_reported:
            self._rx_ui_timeout_reported = True
            print("[RTP] LCD弹框确认超时，继续播放远端语音")
        started_now = False
        if not self._rx_playout_started:
            if len(self._rx_buffer) < self.jitter_packets:
                return
            self._start_playout(now_ms)
            started_now = True

        free_count = self._get_pcm_free_count()
        if free_count is None:
            self._process_playout_timed_fallback(now_ms)
            return

        recently_received = (
            self._last_rx_ms is not None and
            _ticks_diff(now_ms, self._last_rx_ms) <
            RTP_RX_AUDIO_TIMEOUT_MS)
        if (free_count >= self.pcm_periodcnt and
                not self._rx_buffer and not recently_received):
            self._finish_playout()
            return
        if (not started_now and free_count >= self.pcm_periodcnt and
                not self._rx_underrun_active):
            # 起播后的PCM缓冲再次完全变空，说明Python或网络供帧不及时。
            self._stats["rx_pcm_underrun"] += 1
            self._rx_underrun_active = True

        writes = 0
        while (free_count > RTP_PCM_FREE_RESERVE and
               writes < self.pcm_periodcnt):
            payload, silence = self._take_next_playout_payload()
            if payload is None:
                if not recently_received:
                    break
                # RTP暂时断流但麦权仍有效时，用静音保持PCM连续工作。
                payload = PCMA_SILENCE_FRAME
                silence = True
                self._stats["rx_missing"] += 1
                self._rx_expected_sequence = (
                    (self._rx_expected_sequence + 1) & 0xFFFF)
            if not self._write_playout_frame(payload, silence):
                break
            if not silence:
                self._rx_underrun_active = False
            free_count -= 1
            writes += 1

    def _worker(self):
        while self._running:
            if self.socket is None:
                try:
                    self._init_socket()
                    print("[RTP] UDP端口绑定成功：{}".format(self.local_port))
                except Exception as error:
                    print("[RTP] UDP端口绑定失败，稍后重试：{}".format(error))
                    utime.sleep_ms(1000)
                    continue
            now = _ticks_ms()
            # 弹框首帧确认前限制接收突发，避免RTP线程一次处理多个包抢占
            # LCD主线程；套接字仍保持少量读取，防止接收缓存立即溢出。
            rx_gate_open = True
            if (self._rx_group_id is not None and
                    self._rx_ui_revision is not None):
                rx_gate_open, _rx_gate_timeout = self._ui_gate_state(
                    self._rx_ui_revision, self._rx_ui_wait_started_ms, now,
                    RTP_RX_UI_READY_TIMEOUT_MS, RTP_RX_UI_READY_SETTLE_MS)
            if rx_gate_open:
                self._receive_available()
            else:
                self._receive_available(1)
            now = _ticks_ms()
            self._process_receive_probe(now)
            self._check_receive_start_timeout(now)
            if self._start_tx_pending and self._ptt_pressed:
                ui_ready, ui_timed_out = self._ui_gate_state(
                    self._tx_ui_revision,
                    self._tx_ui_wait_started_ms, now,
                    RTP_TX_UI_READY_TIMEOUT_MS)
                if ui_ready:
                    if ui_timed_out and not self._tx_ui_timeout_reported:
                        self._tx_ui_timeout_reported = True
                        print("[RTP] LCD弹框确认超时，继续发送本机语音")
                    self._start_tx_pending = False
                    self._begin_tx()
            tx_sent = False
            if self._tx_active and self._ptt_pressed:
                tx_sent = self._process_transmit()
                if tx_sent:
                    self._tx_burst_count += 1
                else:
                    self._tx_burst_count = 0
            elif self._tx_active:
                self._stop_tx()
            self._process_playout(now)
            if (not self._start_tx_pending and
                    self.server_port is not None and
                    (self._last_probe_ms is None or
                     _ticks_diff(now, self._last_probe_ms) >=
                     RTP_PROBE_INTERVAL_MS)):
                self._send_probe("心跳")
            # 功放在PCM已播放完全部预填帧后由_finish_playout()关闭，
            # 不能仅按最后收包时间关闭，否则会截断仍在硬件缓冲中的尾音。
            self._report_stats(now)
            try:
                if self._tx_active:
                    # g711.read()由PCM采集时钟阻塞/供帧；积压时每4包让出一次CPU。
                    if (not tx_sent or
                            self._tx_burst_count >= RTP_TX_BACKLOG_LIMIT):
                        self._tx_burst_count = 0
                        utime.sleep_ms(1)
                elif self.is_audio_priority_active():
                    utime.sleep_ms(1)
                else:
                    utime.sleep_ms(5)
            except Exception:
                pass


class HardwareKeyService:
    """单线程处理PTT及音量键，减少EC800M线程栈内存占用。"""

    def __init__(self, controller, on_activity=None, ptt_gpio=29,
                 volume_up_gpio=30, volume_down_gpio=31, poll_ms=10,
                 debounce_ms=30):
        self.controller = controller
        self.on_activity = on_activity
        self.ptt_gpio = int(ptt_gpio)
        self.volume_up_gpio = int(volume_up_gpio)
        self.volume_down_gpio = int(volume_down_gpio)
        self.poll_ms = max(5, int(poll_ms))
        self.debounce_ms = max(self.poll_ms, int(debounce_ms))
        self.ptt_pin = None
        self.up_pin = None
        self.down_pin = None
        self._running = False
        self._ptt_pressed = False
        self._up_pressed = False
        self._down_pressed = False
        self._candidate = None
        self._candidate_since_ms = None

    def _make_input(self, gpio):
        pin_id = getattr(Pin, "GPIO{}".format(gpio), gpio)
        pull = getattr(Pin, "PULL_PU", getattr(Pin, "PULL_UP", 0))
        try:
            return Pin(pin_id, Pin.IN, pull, 1)
        except TypeError:
            return Pin(pin_id, Pin.IN, pull)

    def start(self):
        if Pin is None or _thread is None:
            print("[按键] machine.Pin或_thread不可用")
            return False
        try:
            self.ptt_pin = self._make_input(self.ptt_gpio)
            self.up_pin = self._make_input(self.volume_up_gpio)
            self.down_pin = self._make_input(self.volume_down_gpio)
            self._ptt_pressed = (int(self.ptt_pin.read()) == 0)
            self._up_pressed = (int(self.up_pin.read()) == 0)
            self._down_pressed = (int(self.down_pin.read()) == 0)
            self._candidate = (self._ptt_pressed, self._up_pressed,
                               self._down_pressed)
            self._candidate_since_ms = _ticks_ms()
            self._running = True
            _thread.start_new_thread(self._worker, ())
            print("[按键] 已启动：GPIO29 PTT，GPIO30音量+，GPIO31音量-")
            return True
        except Exception as error:
            print("[按键] 初始化失败：{}".format(error))
            return False

    def stop(self):
        self._running = False

    def _released(self, delta):
        value = self.controller.change_volume(delta)
        if self.on_activity is not None:
            self.on_activity()
        print("[音量键] 当前音量={}".format(value))

    def _worker(self):
        while self._running:
            try:
                ptt = (int(self.ptt_pin.read()) == 0)
                up = (int(self.up_pin.read()) == 0)
                down = (int(self.down_pin.read()) == 0)
                raw = (ptt, up, down)
                now = _ticks_ms()
                if raw != self._candidate:
                    self._candidate = raw
                    self._candidate_since_ms = now
                    utime.sleep_ms(self.poll_ms)
                    continue
                if (_ticks_diff(now, self._candidate_since_ms) <
                        self.debounce_ms):
                    utime.sleep_ms(self.poll_ms)
                    continue
                if ptt != self._ptt_pressed:
                    self._ptt_pressed = ptt
                    if self.on_activity is not None:
                        self.on_activity()
                    self.controller.set_ptt_pressed(ptt)
                if self._up_pressed and not up:
                    self._released(1)
                if self._down_pressed and not down:
                    self._released(-1)
                self._up_pressed = up
                self._down_pressed = down
            except Exception as error:
                print("[按键] 读取失败：{}".format(error))
            utime.sleep_ms(self.poll_ms)
