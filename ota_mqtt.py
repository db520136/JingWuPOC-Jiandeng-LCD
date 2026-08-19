import _thread
import utime
import ujson
import net
import dataCall
import checkNet
from umqtt import MQTTClient


# 全局运行标志。调用 disconnect 后，监听线程、重连线程、业务线程都会退出。
TaskEnable = True


def _new_lock():
    """创建普通互斥锁，兼容 QuecPython 的 _thread 接口。"""
    return _thread.allocate_lock()


def _new_event_sem():
    """创建一个默认阻塞的信号量，用于唤醒业务处理线程。"""
    if hasattr(_thread, "allocate_semphore"):
        sem = _thread.allocate_semphore(1)
        try:
            # QuecPython 的信号量创建后可能自带计数，这里清空计数，保证业务线程先阻塞。
            cnt = sem.getCnt()
            for i in range(cnt[1]):
                try:
                    sem.acquire()
                except Exception:
                    break
        except Exception:
            try:
                sem.acquire()
            except Exception:
                pass
        return sem

    # 标准 MicroPython 没有信号量时，用已加锁的 lock 模拟事件。
    lock = _thread.allocate_lock()
    lock.acquire()
    return lock


class MqttClientManage(object):
    """EC800M/QuecPython MQTT 管理类。

    设计目标：
    1. 网络掉线、拨号断开、MQTT socket 异常、发布失败、订阅失败时都进入统一重连流程。
    2. MQTT 回调线程只解析和入队消息，具体业务逻辑放在本文件的业务线程中处理。
    3. 保留旧工程的 topic 和字段名，便于 poc_sip.py 逐步替换使用。
    """

    PROJECT_NAME = "QuecPython_MQTT_OTA"
    PROJECT_VERSION = "1.0.0"

    MSG_TYPE_OTA_QUERY = 1     # OTA 信息查询消息
    MSG_TYPE_OTA_UPGRADE = 2   # OTA 升级指令消息

    def __init__(self, client_id, server, port, deviceID, user=None, password=None,
                 keepalive=60, ssl=False, ssl_params=None, reconn=False,
                 business_obj=None):
        self.client_id = client_id
        self.server = server
        self.port = port
        self.user = user
        self.password = password
        self.keepalive = keepalive
        self.ssl = ssl
        self.ssl_params = ssl_params

        # 本文件使用外部统一重连逻辑，因此默认关闭 umqtt 内部自动重连，避免两套重连互相影响。
        self.reconn = reconn
        self.deviceId = deviceID
        self.business_obj = business_obj

        # 与旧工程保持一致的 topic 顺序。
        self.topic = [
            "/deviceStatusInfo/poc/Information/OTA_Msg/" + deviceID,
            "/deviceStatusInfo/poc/Information/OTA_GotoUpgrad/" + deviceID
        ]
        self.qos = 1
        self.client = None
        self.checknet = checkNet.CheckNetwork(self.PROJECT_NAME, self.PROJECT_VERSION)

        # 网络和 MQTT 状态标志。
        self.__nw_flag = False
        self.__mqtt_connected = False
        self.__reconnect_running = False

        # 互斥锁：保护 MQTT 客户端、重连流程和消息队列。
        self.mp_lock = _new_lock()
        self.queue_lock = _new_lock()

        # 业务线程唤醒信号量。为兼容旧工程，保留 mqtt_handler_lock 名称。
        self.mqtt_handler_lock = _new_event_sem()
        self.msg_queue = []
        self.max_queue_len = 10

        # 旧业务代码可能读取这些成员，这里继续保留。
        self.msg_type = None
        self.setTempType = None
        self.setTemp = None
        self.Clothes_Fan_OnOff = None
        self.Clothes_FanMode = None
        self.CoolingFan_Mode = None
        self.CoolingFan_PWM_Val = None

        self.listen_thread_id = None
        self.business_thread_id = None
        self.reconnect_thread_id = None

    def set_business_obj(self, business_obj):
        """绑定业务对象，通常传入 poc_sip.MyPOC 实例。"""
        self.business_obj = business_obj

    def start(self):
        """启动 MQTT：等待网络、连接服务器、订阅 topic，并启动监听和业务线程。"""
        global TaskEnable
        TaskEnable = True
        self.checknet.poweron_print_once()
        self.checknet.wait_network_connected()
        self._create_client()
        self._register_network_callback()
        self._connect_and_subscribe()

        self.listen_thread_id = _thread.start_new_thread(self.listen, ())
        self.business_thread_id = _thread.start_new_thread(self.business_handler, ())
        return True

    def _create_client(self):
        """创建 MQTTClient，并注册消息回调和异常回调。"""
        self.client = MQTTClient(self.client_id, self.server, self.port,
                                 self.user, self.password,
                                 keepalive=self.keepalive, ssl=self.ssl,
                                 ssl_params=self.ssl_params, reconn=self.reconn)
        self.client.set_callback(self.callback)
        self.client.error_register_cb(self.error_register_cb)

    def _register_network_callback(self):
        """注册拨号网络状态回调，网络变化时触发重连判断。"""
        flag = dataCall.setCallback(self.nw_cb)
        if flag != 0:
            raise Exception("Network callback registration failed")

    def _connect_and_subscribe(self):
        """连接 MQTT 并订阅所有 topic。失败时抛出异常，由外层进入重连。"""
        stage, state = checkNet.waitNetworkReady(30)
        if stage != 3 or state != 1:
            self.__nw_flag = False
            raise Exception("network not ready")

        self.client.connect()
        self.__nw_flag = True
        self.__mqtt_connected = True

        for topic in self.topic:
            self.client.subscribe(topic, self.qos)
        print("MQTT connected and subscribed")

    def listen(self):
        """MQTT 消息监听线程。wait_msg 阻塞接收，异常时统一触发重连。"""
        while TaskEnable:
            try:
                self.client.wait_msg()
            except Exception as e:
                print("MQTT listen error:", e)
                self.__mqtt_connected = False
                self._start_reconnect("listen error")
                utime.sleep(1)

    def callback(self, topic, sub_cb=None):
        """MQTT 消息回调：只做校验、分类、入队，不直接处理复杂业务。"""
        if sub_cb is None:
            print("MQTT callback message is empty")
            return

        try:
            topic_str = topic.decode() if hasattr(topic, "decode") else topic
            msg_str = sub_cb.decode() if hasattr(sub_cb, "decode") else sub_cb
            print("Subscribe Recv: Topic={}, Msg={}".format(topic_str, msg_str))
            dict_data = ujson.loads(msg_str)
        except Exception as e:
            print("MQTT message parse failed:", e)
            return

        if dict_data.get("deviceId") != self.deviceId:
            print("MQTT deviceId mismatch: recv={}, local={}".format(dict_data.get("deviceId"), self.deviceId))
            return

        msg_type = self._get_msg_type(topic_str, dict_data)
        if msg_type is None:
            print("MQTT message ignored, topic={}".format(topic_str))
            return

        self._save_last_message(msg_type, dict_data)
        self._push_message(msg_type, topic_str, dict_data)

    def _get_msg_type(self, topic_str, dict_data):
        """根据 topic 和字段判断消息类型。"""
        if topic_str == self.topic[0] and dict_data.get("Information_sta") == "query":
            return self.MSG_TYPE_OTA_QUERY
        if topic_str == self.topic[1]:
            return self.MSG_TYPE_OTA_UPGRADE
        return None


    def _push_message(self, msg_type, topic_str, dict_data):
        """消息入队，队列满时丢弃最旧消息，防止长期断网后内存持续增长。"""
        self.queue_lock.acquire()
        try:
            if len(self.msg_queue) >= self.max_queue_len:
                self.msg_queue.pop(0)
            self.msg_queue.append({"type": msg_type, "topic": topic_str, "data": dict_data})
        finally:
            self.queue_lock.release()

        self._release_handler_lock()

    def _pop_message(self):
        """从队列取出一条待处理业务消息。"""
        self.queue_lock.acquire()
        try:
            if len(self.msg_queue) == 0:
                return None
            return self.msg_queue.pop(0)
        finally:
            self.queue_lock.release()

    def _release_handler_lock(self):
        """唤醒业务线程。重复 release 失败时忽略，避免影响 MQTT 回调。"""
        try:
            self.mqtt_handler_lock.release()
        except Exception:
            pass

    def business_handler(self):
        """MQTT 业务处理线程。所有业务动作都在这里串行处理。"""
        while TaskEnable:
            try:
                self.mqtt_handler_lock.acquire()
                msg = self._pop_message()
                while msg is not None:
                    self._handle_business_message(msg)
                    msg = self._pop_message()
            except Exception as e:
                print("MQTT business handler error:", e)
                utime.sleep(1)

    def _handle_business_message(self, msg):
        """按消息类型处理业务。未绑定业务对象时，只记录消息，不报错。"""
        msg_type = msg["type"]
        data = msg["data"]
        obj = self.business_obj
        if msg_type == self.MSG_TYPE_OTA_QUERY:
            self._report_ota_status(obj)
        elif msg_type == self.MSG_TYPE_OTA_UPGRADE:
            self._handle_ota_upgrade(obj, data)

    def _handle_ota_upgrade(self, obj, data):
        """处理 OTA 升级指令。具体下载和升级动作可在业务对象中扩展。"""
        new_ver = data.get("Upgrad_Software_Ver")
        if new_ver == self.SOFTWARE_VER:
            print("OTA version is same, no need upgrade")
            return

        if obj is not None and hasattr(obj, "OTA_handler_lock"):
            try:
                obj.OTA_handler_lock.release()
            except Exception:
                pass
        print("OTA upgrade message received, version={}".format(new_ver))

    def _report_ota_status(self, obj):
        """上报 OTA 版本和存储状态。"""
        print("MQTT report OTA status")
        payload = {
            "deviceId": self.deviceId,
            "Information_sta": "report",
            "Software_Ver": self._get_attr(obj, "SOFTWARE_VER", self.SOFTWARE_VER),
            "Firmware_Ver": self._get_attr(obj, "Firmware_Ver", None),
            "Hardware_Ver": self._get_attr(obj, "HARDWARE_VER", None),
            "Updata_Sta": self._get_attr(obj, "updata_sta", "not start"),
        }
        self.publish_json(self.topic[2], payload, qos=1)

    def _get_attr(self, obj, name, default=None):
        """安全读取属性，避免业务对象未创建时抛异常。"""
        if obj is None:
            return default
        try:
            return getattr(obj, name)
        except Exception:
            return default

    def publish_json(self, topic, payload, qos=0):
        """发布 JSON 数据。"""
        return self.publish(topic, ujson.dumps(payload), qos=qos)

    def subscribe(self, topic, qos=0):
        """订阅 topic。订阅失败时触发重连。"""
        try:
            return self.client.subscribe(topic, qos)
        except Exception as e:
            print("MQTT subscribe failed:", e)
            self.__mqtt_connected = False
            self._start_reconnect("subscribe failed")
            return False

    def publish(self, topic, msg, qos=0):
        """发布消息。发布失败常见于断网或 socket 异常，失败后触发重连。"""
        try:
            if not self.__mqtt_connected:
                self._start_reconnect("publish while disconnected")
                return False
            return self.client.publish(topic, msg, qos)
        except Exception as e:
            print("MQTT publish failed:", e)
            self.__mqtt_connected = False
            self._start_reconnect("publish failed")
            return False

    def error_register_cb(self, error):
        """umqtt 内部线程异常回调。"""
        print("mqtt error =", error)
        self.__mqtt_connected = False
        self._start_reconnect("mqtt error")

    def _start_reconnect(self, reason):
        """启动重连线程。已经在重连时不重复启动。"""
        if not TaskEnable:
            return
        if self.__reconnect_running:
            return
        print("MQTT start reconnect, reason={}".format(reason))
        self.__reconnect_running = True
        self.reconnect_thread_id = _thread.start_new_thread(self.reconnect, ())

    def reconnect(self):
        """统一重连机制：等待网络恢复、关闭旧 socket、重建连接、重新订阅 topic。"""
        try:
            while TaskEnable:
                self.mp_lock.acquire()
                try:
                    self._close_client_socket()

                    if self._network_ready():
                        try:
                            self._create_client()
                            self._connect_and_subscribe()
                            print("MQTT reconnect success")
                            return True
                        except Exception as e:
                            self.__mqtt_connected = False
                            print("MQTT reconnect failed:", e)
                    else:
                        print("network not ready, wait reconnect")
                finally:
                    self.mp_lock.release()

                utime.sleep(5)

            return False
        finally:
            # 重连线程退出时复位标志，后续再次断网可以重新启动重连线程。
            self.__reconnect_running = False

    def _network_ready(self):
        """检查网络注册和拨号状态。"""
        try:
            net_sta = net.getState()
            if net_sta == -1 or net_sta[1][0] != 1:
                return False

            call_state = dataCall.getInfo(1, 0)
            if call_state == -1 or call_state[2][0] != 1:
                return False

            self.__nw_flag = True
            return True
        except Exception as e:
            print("network check failed:", e)
            return False

    def _close_client_socket(self):
        """关闭旧 MQTT socket，释放底层资源。"""
        if self.client is None:
            return
        try:
            self.client.close()
        except Exception:
            pass

    def nw_cb(self, args):
        """dataCall 网络回调。断网时立即标记离线，恢复时启动重连。"""
        try:
            nw_sta = args[1]
        except Exception:
            nw_sta = 0

        if nw_sta == 1:
            print("*** network connected! ***")
            self.__nw_flag = True
            if not self.__mqtt_connected:
                self._start_reconnect("network recovered")
        else:
            print("*** network not connected! ***")
            self.__nw_flag = False
            self.__mqtt_connected = False

    def disconnect(self):
        """停止 MQTT，关闭连接并退出相关线程。"""
        global TaskEnable
        TaskEnable = False
        self.__mqtt_connected = False
        self.__reconnect_running = False
        self._release_handler_lock()
        try:
            self.client.disconnect()
        except Exception:
            self._close_client_socket()
