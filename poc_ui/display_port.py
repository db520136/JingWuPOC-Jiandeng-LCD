# -*- coding: utf-8 -*-
import lvgl as lv
from machine import LCD
LCD_WIDTH = 296
LCD_HEIGHT = 240
DISPLAY_BUFFER_LINES = 100
# 上电时先清除 LCD GRAM，避免上一次运行留下的页面残影。
DISPLAY_BOOT_CLEAR_LINES = 20
# EC800M 图片缓存容量（缓存项数量，不是字节数）。页面图片统一使用
# 标准 JPG，缓存初始化放在显示层，避免页面或业务线程重复清空缓存。
IMAGE_CACHE_SIZE = 12
XSTART_H, XSTART_L, YSTART_H, YSTART_L = 0xF0, 0xF1, 0xF2, 0xF3
XEND_H, XEND_L, YEND_H, YEND_L = 0xE0, 0xE1, 0xE2, 0xE3
_INIT = (
	0, 0, 0x01, 2, 0, 120,
	0, 0, 0x11, 2, 0, 120,
	0, 0, 0x21,
	0, 0, 0x00,
	0, 1, 0x36, 1, 1, 0x60,
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
	0, 4, 0x2A, 1, 1, XSTART_H, 1, 1, XSTART_L,
	1, 1, XEND_H, 1, 1, XEND_L,
	0, 4, 0x2B, 1, 1, YSTART_H, 1, 1, YSTART_L,
	1, 1, YEND_H, 1, 1, YEND_L,
	0, 0, 0x2C,
))
class DisplayPort:
	def __init__(self, width=LCD_WIDTH, height=LCD_HEIGHT,
				 buffer_lines=DISPLAY_BUFFER_LINES):
		self.width = int(width)
		self.height = int(height)
		self.buffer_lines = max(1, int(buffer_lines))
		self.lcd = None
		self.draw_buffer = None
		self.draw_buffer_bytes = None
		self.display_driver = None
		self.display = None
	def start(self):
		self.lcd = LCD()
		self.lcd.lcd_init(
			_INIT_P, self.width, self.height, 52000, 1, 4, 0,
			_INVALID_P, bytearray((0, 0, 0x11, 2, 0, 120, 0, 0, 0x21,
								   0, 0, 0x29)),
			bytearray((0, 0, 0x28, 2, 0, 120, 0, 0, 0x10)), None)
		# _INIT_P 已包含 0x29（显示开启），不再重复调用 lcd_display_on。
		try:
			self._clear_display_memory()
		except Exception:
			# 个别固件若不支持启动清屏，不阻断 LVGL 页面启动。
			pass
		lv.init()
		try:
			lv.img.cache_invalidate_src(None)
			lv.img.cache_set_size(IMAGE_CACHE_SIZE)
		except (AttributeError, TypeError):
			# 兼容未导出图片缓存 API 的固件；图片仍可按普通方式加载。
			pass
		# LVGL 的 draw_buf.init 第三个参数是“像素数量”，不是字节数。
		# RGB565 每像素 2 字节，但这里必须传 width * lines；传入
		# bytearray 的 len 会让 LVGL 误判容量为实际的两倍，连续局部
		# 刷新或滑动时可能越界破坏内存。
		size_pixels = self.width * self.buffer_lines
		size_bytes = size_pixels * 2
		# EC800M 采用单缓冲；保留 Python 引用，避免 LVGL 注册后底层缓冲
		# 被垃圾回收。同步 lcd_write 返回前不会复用这块缓冲，因此无需第二块。
		self.draw_buffer_bytes = bytearray(size_bytes)
		self.draw_buffer = lv.disp_draw_buf_t()
		self.draw_buffer.init(self.draw_buffer_bytes, None, size_pixels)
		self.display_driver = lv.disp_drv_t()
		self.display_driver.init()
		self.display_driver.draw_buf = self.draw_buffer
		self.display_driver.flush_cb = self.lcd.lcd_write
		self.display_driver.hor_res = self.width
		self.display_driver.ver_res = self.height
		self.display = self.display_driver.register()
		return self
	def enter_sleep(self):
		if self.lcd is None:
			return False
		try:
			self.lcd.lcd_display_off()
		except Exception:
			return False
		return True
	def leave_sleep(self):
		if self.lcd is None:
			return False
		try:
			self.lcd.lcd_display_on()
		except Exception:
			return False
		return True
	def _clear_display_memory(self):
		block_lines = min(DISPLAY_BOOT_CLEAR_LINES, self.height)
		clear_block = bytearray(self.width * block_lines * 2)
		y = 0
		while y < self.height:
			lines = min(block_lines, self.height - y)
			data = (clear_block if lines == block_lines else
					bytearray(self.width * lines * 2))
			self.lcd.lcd_write(data, 0, y, self.width - 1, y + lines - 1)
			y += lines
	def present_initial_frame(self):
		try:
			lv.refr_now(self.display)
		except Exception:
			# 部分固件未导出 refr_now；task_handler 仍会处理整屏失效区域。
			try:
				lv.task_handler()
			except Exception:
				pass
	def tick(self, elapsed_ms):
		lv.tick_inc(max(0, int(elapsed_ms)))
	def task_handler(self):
		lv.task_handler()
