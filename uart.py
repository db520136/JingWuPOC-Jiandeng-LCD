import _thread
import utime
import log
from machine import UART

'''
 * 参数1：端口
        注：EC100YCN平台与EC600SCN平台，UARTn作用如下
        UART0 - DEBUG PORT
        UART1 – BT PORT
        UART2 – MAIN PORT
        UART3 – USB CDC PORT
 * 参数2：波特率
 * 参数3：data bits  （5~8）
 * 参数4：Parity  （0：NONE  1：EVEN  2：ODD）
 * 参数5：stop bits （1~2）
 * 参数6：flow control （0: FC_NONE  1：FC_HW）
'''


# 设置日志输出级别
log.basicConfig(level=log.INFO)
uart_log = log.getLogger("UART")



class Example_uart(object):
    def __init__(self, no=UART.UART3, bate=115200, data_bits=8, parity=0, stop_bits=1, flow_control=0):
        self.uart = UART(no, bate, data_bits, parity, stop_bits, flow_control)
        self.uart.set_callback(self.callback)


    def callback(self, para):
        if(0 == para[0]):
            self.uartRead(para[2])


    def uartWrite(self, msg):
        self.uart.write(msg)

    def uartRead(self, len):
        msg = self.uart.read(len)
        self.uartWrite(msg)
        

    def uartWrite_test(self):
        aa='hello'
        for i in range(2):
            self.uartWrite(aa)
            utime.sleep_ms(800)
        while 1:
            utime.sleep_ms(1000)
            # self.uartRead(len(write_msg))

if __name__ == "__main__":
    start = utime.ticks_ms()
    uart_test = Example_uart()
    uart_test.uartWrite_test()
