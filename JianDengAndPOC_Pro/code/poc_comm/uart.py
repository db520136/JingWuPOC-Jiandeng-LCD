# -*- coding: utf-8 -*-
import utime
from machine import UART, Pin
from misc import Power
BLINK_INTERVAL_MS = 500
REBOOT_DELAY_MS = 500
RAW_OK_IDLE_MS = 50
MAX_COMMAND_BYTES = 2048
PRODUCTION_UART_DEBUG = True
class ProductionConfigUART:
    def __init__(self, persistent_config, identity_callback, indicator=None):
        self.config = persistent_config
        self.identity_callback = identity_callback
        self.indicator = indicator
        if self.indicator is None:
            self.indicator = Pin(Pin.GPIO38, Pin.OUT, Pin.PULL_PD, 1)
        # 仅生产模式构造 UART3；正常启动保留默认 REPL。
        self.uart = UART(UART.UART3, 115200, 8, 0, 1, 0)
        self._buffer = b""
        self._discard_line = False
        self._last_rx_ms = None
        self._identity = None
        self._clear_pending = False
        self._clear_due_ms = None
        self._reboot_due_ms = None
        self._restart_requested = False
        self._led_value = 1
        self.indicator.write(self._led_value)
        self._blink_due_ms = utime.ticks_add(utime.ticks_ms(), BLINK_INTERVAL_MS)
        # 注册后先读取一次，接管注册回调前已经到达的数据。
        self._rx_pending = True
        self._callback_info = None
        self.config.set_identity_logger(self._log)
        self.uart.set_callback(self._receive_callback)
    def _log(self, message):
        try:
            self.uart.write((message + "\r\n").encode("utf-8"))
        except Exception:
            # 串口异常时日志可能无法发出，仍须继续身份清理和重试。
            pass
    def _receive_callback(self, para):
        if PRODUCTION_UART_DEBUG:
            self._callback_info = para
        if para and para[0] == 0:
            self._rx_pending = True
    def _debug(self, message):
        if PRODUCTION_UART_DEBUG:
            self._log("[生产配置诊断] " + message)
    def _read_available(self, discard=False):
        if not self._rx_pending:
            return
        # 先清通知再读取；读取期间的新回调只置位，不会被后续清除。
        self._rx_pending = False
        try:
            available = self.uart.any()
            if PRODUCTION_UART_DEBUG:
                self._debug("待读取：{}字节".format(available))
            if available:
                data = self.uart.read(min(available, MAX_COMMAND_BYTES))
                if PRODUCTION_UART_DEBUG:
                    self._debug("实际读取：{}字节，HEX={}".format(
                        len(data or b""), " ".join(
                            "{:02X}".format(value) for value in (data or b""))))
                if available > len(data or b""):
                    self._rx_pending = True
                if data and not discard:
                    self._buffer += data
                    self._last_rx_ms = utime.ticks_ms()
        except Exception:
            self._rx_pending = True
            raise
    def _reject(self):
        self._identity = None
        self.identity_callback(None, None)
        self._clear_pending = True
        self._clear_due_ms = utime.ticks_ms()
        self._try_clear()
    def _try_clear(self):
        if self.config.clear_identity():
            self._clear_pending = False
            self._clear_due_ms = None
        else:
            self._clear_due_ms = utime.ticks_add(utime.ticks_ms(), 500)
    def _handle_line(self, data):
        if not data.strip():
            return
        if self._identity is not None:
            if data.strip().lower() != b"ok":
                self._reject()
                return
            device_id, hardware_version = self._identity
            if not self.config.confirm_identity(device_id, hardware_version):
                self._reject()
                return
            self._reboot_due_ms = utime.ticks_add(utime.ticks_ms(), REBOOT_DELAY_MS)
            return
        try:
            fields = data.decode("utf-8").split(";")
            if len(fields) != 2:
                raise ValueError("配置命令必须包含 SN 和 Hard_V")
            values = {}
            for field in fields:
                key, value = field.split("=", 1)
                key, value = key.strip(), value.strip()
                if key not in ("SN", "Hard_V") or key in values or not value:
                    raise ValueError("配置字段缺失或为空")
                values[key] = value
            device_id, hardware_version = values["SN"], values["Hard_V"]
            self._debug("完整配置命令已识别，开始保存")
            if not self.config.save_identity(device_id, hardware_version):
                raise RuntimeError("设备身份保存失败")
            actual = self.config.read_identity()
            if actual != (device_id, hardware_version, False):
                raise RuntimeError("设备身份回读失败")
            self._debug("保存及回读成功，准备发送应答")
            self._identity = actual[:2]
            self.identity_callback(*self._identity)
            reply = "SN={};Hard_V={}\r\n".format(*self._identity).encode("utf-8")
            written = self.uart.write(reply)
            if written != len(reply):
                raise RuntimeError("配置回发不完整：{}".format(written))
            if PRODUCTION_UART_DEBUG:
                self._debug("应答write返回：{}，等待OK".format(written))
        except Exception as error:
            self._log("[生产配置] 配置失败：{}".format(error))
            self._reject()
    def _consume(self):
        while not self._clear_pending and self._reboot_due_ms is None:
            end = self._buffer.find(b"\r\n")
            if end < 0:
                if len(self._buffer) > MAX_COMMAND_BYTES:
                    self._buffer = b""
                    self._discard_line = True
                    self._reject()
                return
            line, self._buffer = self._buffer[:end], self._buffer[end + 2:]
            if self._discard_line:
                self._discard_line = False
                continue
            if len(line) > MAX_COMMAND_BYTES:
                self._reject()
            else:
                self._handle_line(line)
    def step(self):
        if PRODUCTION_UART_DEBUG:
            callback_info = self._callback_info
            self._callback_info = None
            if callback_info is not None:
                self._debug("收到回调：{}".format(callback_info))
        now_ms = utime.ticks_ms()
        if utime.ticks_diff(now_ms, self._blink_due_ms) >= 0:
            self._led_value = 1 - self._led_value
            self.indicator.write(self._led_value)
            self._blink_due_ms = utime.ticks_add(now_ms, BLINK_INTERVAL_MS)
        if self._restart_requested:
            return
        if self._reboot_due_ms is not None:
            if utime.ticks_diff(now_ms, self._reboot_due_ms) >= 0:
                Power.powerRestart()
                self._restart_requested = True
            return
        if self._clear_pending:
            if utime.ticks_diff(now_ms, self._clear_due_ms) >= 0:
                self._try_clear()
            if self._clear_pending:
                self._buffer = b""
                self._read_available(discard=True)
                return
        self._read_available()
        self._consume()
        # 裸 OK 必须等到接收短暂空闲，不能只匹配分包中的前两个字节。
        if (self._identity is not None and self._reboot_due_ms is None and
                self._buffer.strip().lower() == b"ok" and
                self._last_rx_ms is not None and
                utime.ticks_diff(utime.ticks_ms(), self._last_rx_ms) >= RAW_OK_IDLE_MS and
                not self._rx_pending and not self.uart.any()):
            data, self._buffer = self._buffer, b""
            self._handle_line(data)
    def run(self):
        self._debug("已进入生产配置，等待SN和Hard_V命令")
        while True:
            try:
                self.step()
            except Exception as error:
                self._log("[生产配置] 串口或复位异常：{}".format(error))
                utime.sleep_ms(500)
            utime.sleep_ms(10)
