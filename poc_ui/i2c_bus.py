# -*- coding: utf-8 -*-

import _thread

from machine import I2C


class SharedI2CBus:

	def __init__(self, i2c=None):
		self._i2c = i2c if i2c is not None else I2C(
			I2C.I2C0, I2C.STANDARD_MODE)
		self._lock = _thread.allocate_lock()

	def write(self, slave_address, register_buffer, register_length,
			  data_buffer, data_length):
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
	global _default_bus
	_default_bus_lock.acquire()
	try:
		if _default_bus is None:
			_default_bus = SharedI2CBus()
		return _default_bus
	finally:
		_default_bus_lock.release()
