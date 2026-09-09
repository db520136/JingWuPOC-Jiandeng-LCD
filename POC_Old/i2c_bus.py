# -*- coding: utf-8 -*-
"""EC800M 硬件 I2C0 共享访问管理器。

ET6312B 和 SC7A20H 挂在同一条 I2C0 总线上。整个程序只创建一个硬件
I2C0 对象，并用线程锁保证一次读写事务不会被另一个线程打断。
"""

import _thread

from machine import I2C


class SharedI2CBus:
    """为 QuecPython I2C 对象增加跨线程互斥保护。"""

    def __init__(self, i2c=None):
        self._i2c = i2c if i2c is not None else I2C(
            I2C.I2C0, I2C.STANDARD_MODE)
        self._lock = _thread.allocate_lock()

    def write(self, slave_address, register_buffer, register_length,
              data_buffer, data_length):
        """执行一次完整写事务；参数与 QuecPython I2C.write 一致。"""
        self._lock.acquire()
        try:
            return self._i2c.write(
                slave_address,
                register_buffer,
                register_length,
                data_buffer,
                data_length,
            )
        finally:
            self._lock.release()

    def read(self, slave_address, register_buffer, register_length,
             data_buffer, data_length, delay):
        """执行一次完整读事务；保留 QuecPython 要求的 delay 参数。"""
        self._lock.acquire()
        try:
            return self._i2c.read(
                slave_address,
                register_buffer,
                register_length,
                data_buffer,
                data_length,
                delay,
            )
        finally:
            self._lock.release()


_default_bus = None
_default_bus_lock = _thread.allocate_lock()


def get_i2c0():
    """返回全局唯一的、带线程锁的硬件 I2C0 代理。"""
    global _default_bus
    _default_bus_lock.acquire()
    try:
        if _default_bus is None:
            _default_bus = SharedI2CBus()
        return _default_bus
    finally:
        _default_bus_lock.release()
