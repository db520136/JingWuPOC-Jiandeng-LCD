# -*- coding: utf-8 -*-
# 每台设备生产时单独配置；普通 OTA 升级包不包含本文件。
DEVICE_ID = "YNPOC110160930100018"
HARDWARE_VERSION = "1.0.1"
def get_identity():
	return DEVICE_ID, HARDWARE_VERSION
