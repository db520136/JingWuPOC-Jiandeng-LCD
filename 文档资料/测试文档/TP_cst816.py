import utime
from tp import cst816
from machine import Pin, I2C
# tp_cst816 = cst816(i2c_no=I2C.I2C2, i2c_mode=I2C.STANDARD_MODE, irq=44, reset=31)
tp_cst816 = cst816(irq=11, reset=15)
print("act=",tp_cst816.activate())
print("init=",tp_cst816.init())
#gpio = Pin(Pin.GPIO44, Pin.OUT, Pin.PULL_PD, 0)
tp_cst816.read_xy()

def tp_cb(self, para):
    print("中断回调")
    if (para == 0):
        self.screen.done_left_to_right()
        print("done_left_to_right")
    elif (para == 1):
        self.screen.done_right_to_left()
        print("done_right_to_left")
    elif (para == 2):
        self.screen.done_bottom_to_top()
        print("done_bottom_to_top")
    elif (para == 3):
        self.screen.done_top_to_bottom()
        print("V")
    elif (para == 4):
        self.screen.done_return()
        print("return")
    elif (para == 5):
        self.screen.done_click()
        print("CLICK")
    elif (para == 6):
        self.screen.done_error()
        print("error")
    else:
        print("error")

tp_cst816.set_callback(tp_cb)
print(111)
while True:
     utime.sleep_ms(500)
     print(tp_cst816.read_xy())
