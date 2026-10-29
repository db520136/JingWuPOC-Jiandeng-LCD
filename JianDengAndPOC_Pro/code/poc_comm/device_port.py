# -*- coding: utf-8 -*-
import utime
try:
	from misc import PWM_V2
except ImportError:
	PWM_V2 = None
try:
	import quecgnss
except ImportError:
	quecgnss = None
try:
	import modem
except ImportError:
	modem = None
def _screen_pwm_duty(percent):
	# UI及持久化保留5~100%，背光占空比按80%上限等比例映射。
	return (percent * 80 + 50) // 100
def _coordinate(value, direction, latitude):
	digits = 2 if latitude else 3
	if len(value) <= digits:
		raise ValueError("GGA经纬度格式错误")
	degrees = int(value[:digits])
	minutes = float(value[digits:])
	limit = 90 if latitude else 180
	allowed = ("N", "S") if latitude else ("E", "W")
	if direction not in allowed or not 0 <= minutes < 60:
		raise ValueError("GGA经纬度格式错误")
	result = degrees + minutes / 60.0
	if not 0 <= result <= limit:
		raise ValueError("GGA经纬度超出范围")
	result = -result if direction in ("S", "W") else result
	minute_fraction_digits = 0
	if "." in value:
		minute_fraction_digits = len(value.rsplit(".", 1)[1])
	# 分钟转换为度需要额外约两位小数，按源GGA精度格式化，避免
	# 直接str(float)显示二进制浮点尾数。
	display = ("{:0." + str(minute_fraction_digits + 2) + "f}").format(result)
	return result, display
def parse_gga(line):
	body, checksum = line.strip().split("*")
	if not body.startswith("$") or len(checksum) != 2:
		raise ValueError("GGA校验字段错误")
	actual = 0
	for character in body[1:]:
		actual ^= ord(character)
	if actual != int(checksum, 16):
		raise ValueError("GGA校验和错误")
	fields = body.split(",")
	if len(fields) < 11 or fields[0][3:] != "GGA":
		raise ValueError("GGA字段不足")
	quality = int(fields[6])
	satellites = int(fields[7] or 0)
	if quality < 0 or quality > 8 or satellites < 0 or satellites > 99:
		raise ValueError("GGA定位质量或卫星数错误")
	result = {"fix": quality > 0, "latitude": None, "longitude": None,
		"latitude_text": None, "longitude_text": None,
		"altitude": None, "satellites": satellites,
		"raw_gga": line.strip().encode("ascii")}
	if quality > 0:
		result["latitude"], result["latitude_text"] = _coordinate(
			fields[2], fields[3], True)
		result["longitude"], result["longitude_text"] = _coordinate(
			fields[4], fields[5], False)
		altitude = float(fields[9])
		if fields[10] != "M" or not -12000 <= altitude <= 100000:
			raise ValueError("GGA海拔格式错误")
		result["altitude"] = altitude
	return result
class DevicePort:
	def __init__(self, software_version="1.0.0", hardware_version="1.0.0",
				 device_id="--"):
		self.software_version = str(software_version)
		self.hardware_version = str(hardware_version)
		self.device_id = str(device_id)
		self.pwm = None
		self.brightness = 100
		self.screen_sleeping = False
		self._screen_backlight_enabled = False
		self.gnss_initialized = False
		self._gnss_retry_at = None
		self._nmea_tail = ""
	def set_screen_brightness(self, percent):
		value = max(5, min(100, int(percent)))
		if PWM_V2 is None:
			raise RuntimeError("PWM_V2模块不可用")
		duty = 0 if self.screen_sleeping else _screen_pwm_duty(value)
		if self.pwm is None:
			pwm = PWM_V2(PWM_V2.PWM0, 1250.0, duty)
			pwm.open()
			self.pwm = pwm
		else:
			self.pwm.open(1250.0, duty)
		self.brightness = value
		self._screen_backlight_enabled = not self.screen_sleeping
		return value
	def prepare_screen_brightness(self, percent):
		value = max(5, min(100, int(percent)))
		if PWM_V2 is None:
			raise RuntimeError("PWM_V2模块不可用")
		if self.pwm is None:
			pwm = PWM_V2(PWM_V2.PWM0, 1250.0, 0)
			pwm.open()
			self.pwm = pwm
		else:
			self.pwm.open(1250.0, 0)
		self.brightness = value
		self.screen_sleeping = False
		self._screen_backlight_enabled = False
		return value
	def reveal_screen(self):
		if self.pwm is None:
			return self.set_screen_brightness(self.brightness)
		self.pwm.open(1250.0, 0 if self.screen_sleeping else _screen_pwm_duty(self.brightness))
		self._screen_backlight_enabled = not self.screen_sleeping
		return self.brightness
	def screen_off(self):
		if self.screen_sleeping:
			return False
		if self.pwm is None:
			self.set_screen_brightness(self.brightness)
		self.pwm.open(1250.0, 0)
		self.screen_sleeping = True
		self._screen_backlight_enabled = False
		return True
	def screen_on(self):
		if not self.screen_sleeping:
			return False
		if self.pwm is None:
			raise RuntimeError("背光PWM未初始化")
		self.pwm.open(1250.0, _screen_pwm_duty(self.brightness))
		self.screen_sleeping = False
		self._screen_backlight_enabled = True
		return True
	def read_versions(self):
		if modem is None:
			raise RuntimeError("modem模块不可用，无法读取固件版本")
		firmware = modem.getDevFwVersion()
		if isinstance(firmware, bytes):
			firmware = firmware.decode("utf-8")
		if not firmware or firmware == -1:
			raise RuntimeError("固件版本查询失败")
		return self.software_version, self.hardware_version, str(firmware)
	def initialize_gnss(self):
		if quecgnss is None:
			raise RuntimeError("quecgnss模块不可用")
		if self.gnss_initialized:
			return True
		now = utime.ticks_ms()
		if (self._gnss_retry_at is not None and
				utime.ticks_diff(now, self._gnss_retry_at) < 0):
			return False
		self._gnss_retry_at = utime.ticks_add(now, 10000)
		for setting in (0, 1, 2, 4):
			quecgnss.configSet(setting, 1)
		if quecgnss.init() != 0:
			raise RuntimeError("GNSS初始化失败")
		self.gnss_initialized = True
		self._gnss_retry_at = None
		return True
	def read_location(self):
		if not self.gnss_initialized:
			if not self.initialize_gnss():
				return {"state": "initializing", "fix": False, "latitude": None,
					"longitude": None, "latitude_text": None,
					"longitude_text": None, "altitude": None,
					"satellites": None, "raw_gga": b""}
		state = int(quecgnss.get_state())
		result = {"state": state, "fix": False, "latitude": None,
			"longitude": None, "latitude_text": None,
			"longitude_text": None, "altitude": None,
			"satellites": None, "raw_gga": b""}
		if state != 2:
			self._nmea_tail = ""
			if state < 0:
				self.gnss_initialized = False
			return result
		raw = quecgnss.read(4096)
		if raw == -1:
			return result
		if isinstance(raw, (tuple, list)):
			raw = raw[1] if len(raw) >= 2 else b""
		text = raw if isinstance(raw, str) else bytes(raw or b"").decode("ascii")
		lines = (self._nmea_tail + text.replace("\x00", "")).replace("\r", "\n").split("\n")
		self._nmea_tail = lines.pop()[-4096:]
		# 部分固件单次返回完整语句，但没有结尾CR/LF。
		if "*" in self._nmea_tail and len(self._nmea_tail.rsplit("*", 1)[1]) == 2:
			lines.append(self._nmea_tail)
			self._nmea_tail = ""
		for line in lines:
			if line.startswith("$") and line[3:6] == "GGA":
				try:
					parsed = parse_gga(line)
				except ValueError:
					continue
				# 无效GGA不能覆盖本次读取中已有的有效定位。
				if parsed["fix"] or not result["fix"]:
					result.update(parsed)
		return result
