# -*- coding: UTF-8 -*-
import usocket
import log
import utime
import checkNet
PROJECT_NAME = "QuecPython_Socket_example"
PROJECT_VERSION = "1.0.0"

checknet = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
stagecode, subcode = checknet.wait_network_connected(30)
if stagecode == 3 and subcode == 1:
    print('Network connection successful!')
    while True:
        sock = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM, usocket.IPPROTO_UDP)
        print('[client] socket object created.')
        data = b'1234567890'
        sock.sendto(data, (address, port))
        print('[client] send data:', data)
        
    while True:
        sock = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        sock.setblocking(True)
        sock.connect(("125.124.233.231", 6060))
        print("Connected to server")
        try:
            sock.recv(1024)
        except Exception as e:
            print(e)
            utime.sleep(1)
print("err")
