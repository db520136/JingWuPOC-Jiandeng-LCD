# -*- coding: utf-8 -*-
"""EC800M内置GNSS定位服务。

本模块只负责GNSS初始化和单次GGA读取，不创建测试循环，也不直接操作TCP。
"""

try:
    import quecgnss
except Exception:
    quecgnss = None


GNSS_READ_SIZE = 4096
GNSS_MAX_PAYLOAD_SIZE = 255


class CAT1_GNSS:
    """封装EC800M内置GNSS，向POC客户端返回一条有效GGA数据。"""

    def __init__(self, read_size=GNSS_READ_SIZE):
        self.read_size = max(128, int(read_size))
        self.initialized = False
        self.last_gga = b""
        self.last_success = False
        self.last_error = None

    def initialize(self):
        """按项目指定参数配置并初始化内置GNSS。"""
        if self.initialized:
            return True
        if quecgnss is None:
            self.last_error = "quecgnss模块不可用"
            print("[GNSS] 初始化失败：{}".format(self.last_error))
            return False
        try:
            quecgnss.configSet(0, 7)  # 定位星系：北斗
            quecgnss.configSet(1, 1)  # 只输出GGA信息
            quecgnss.configSet(2, 1)  # 打开AGPS
            quecgnss.configSet(4, 1)  # 打开备电
            result = quecgnss.init()
            if result != 0:
                raise RuntimeError("quecgnss.init返回{}".format(result))
            self.initialized = True
            self.last_error = None
            print("[GNSS] 初始化成功：北斗、GGA、AGPS、备电已启用")
            return True
        except Exception as error:
            self.initialized = False
            self.last_error = str(error)
            print("[GNSS] 初始化失败：{}".format(error))
            return False

    def get_state(self):
        """返回GNSS工作状态；读取失败时返回-1。"""
        if quecgnss is None:
            return -1
        try:
            return int(quecgnss.get_state())
        except Exception as error:
            self.last_error = str(error)
            return -1

    def enable(self, enabled=True):
        """按需开启或关闭GNSS硬件，正常对讲暂停期间不关闭。"""
        if quecgnss is None:
            return False
        try:
            result = quecgnss.gnssEnable(1 if enabled else 0)
            if not enabled:
                self.initialized = False
            return result == 0
        except Exception as error:
            self.last_error = str(error)
            return False

    @staticmethod
    def _read_bytes(result):
        """兼容quecgnss.read()返回元组或直接返回字节数据的固件。"""
        if isinstance(result, (tuple, list)):
            if len(result) < 2:
                return b""
            # 官方示例使用read()返回值的第二项；第一项在不同固件中
            # 可能是长度，也可能是状态码，不能据此丢弃第二项数据。
            result = result[1]
        if result is None:
            return b""
        if isinstance(result, str):
            return result.encode("ascii")
        try:
            return bytes(result)
        except Exception:
            return b""

    @staticmethod
    def _find_last_gga(text):
        """从读取缓冲中选择最新的一条GGA语句。"""
        selected = ""
        normalized = str(text).replace("\r", "\n")
        for line in normalized.split("\n"):
            line = line.strip().strip("\x00")
            if (len(line) >= 7 and line.startswith("$") and
                    line[3:6] == "GGA"):
                selected = line
        return selected

    @staticmethod
    def _gga_has_fix(gga):
        """GGA定位质量字段大于0，并且经纬度完整时才算定位成功。"""
        fields = str(gga).split(",")
        if len(fields) < 7:
            return False
        if (not fields[2] or fields[3] not in ("N", "S") or
                not fields[4] or fields[5] not in ("E", "W")):
            return False
        try:
            return int(fields[6]) > 0
        except Exception:
            return False

    def read_gga(self):
        """读取并打印定位结果；成功返回ASCII GGA，失败返回空字节。"""
        self.last_gga = b""
        self.last_success = False
        print("run111111111")
        if not self.initialize():
            print("[GNSS] 定位信息：无")
            print("[GNSS] 定位结果：定位失败")
            return b""

        state = self.get_state()
        if state != 2:
            self.last_error = "GNSS状态={}，当前不可读取定位数据".format(state)
            print("[GNSS] 定位信息：无")
            print("[GNSS] 定位结果：定位失败（{}）".format(self.last_error))
            return b""

        try:
            raw = self._read_bytes(quecgnss.read(self.read_size))
            text = raw.decode("ascii").strip("\x00") if raw else ""
            gga = self._find_last_gga(text)
            print("[GNSS] 定位信息：{}".format(gga or text.strip() or "无"))
            if not gga or not self._gga_has_fix(gga):
                self.last_error = "未取得有效GGA定位"
                print("[GNSS] 定位结果：定位失败")
                return b""
            data = gga.encode("ascii")
            if len(data) > GNSS_MAX_PAYLOAD_SIZE:
                self.last_error = "GGA长度超过UINT8范围"
                print("[GNSS] 定位结果：定位失败（{}）".format(self.last_error))
                return b""
            self.last_gga = data
            self.last_success = True
            self.last_error = None
            print("[GNSS] 定位结果：定位成功")
            return data
        except Exception as error:
            self.last_error = str(error)
            print("[GNSS] 定位信息：读取异常 {}".format(error))
            print("[GNSS] 定位结果：定位失败")
            return b""

    # 保留旧测试类曾使用的方法名，其他模块迁移时无需一次性改完。
    def gnss_get_state(self):
        return self.get_state()

    def gnss_enable(self, opt):
        return self.enable(int(opt) == 1)

    def gnss_get_location(self):
        return self.read_gga()

    def gnss_get_config(self, config_type=0):
        if quecgnss is None:
            return None
        try:
            return quecgnss.configGet(int(config_type))
        except Exception as error:
            self.last_error = str(error)
            return None
'''
aa = CAT1_GNSS()
aa.initialize()
import utime
while 1:

    aa.read_gga()
    utime.sleep(5)
'''