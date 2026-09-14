# 通信重构第二阶段接口定义

本阶段只定义命令、优先级和线程边界，不接入现有 `main.py`，不改变当前
已经验证的 UI、LowPriorityWorker、TCP/RTP 运行路径。

第三阶段已新增三个隔离基础模块：

```text
communication_queue.py  通信发送命令有界优先级队列
tcp_receive_worker.py   永久阻塞 TCP recv() 接收线程
rtp_receive_worker.py   永久阻塞 UDP recvfrom() 接收线程
```

它们目前不由 `main.py` 导入或启动，只用于独立静态检查和后续逐步迁移。

第三阶段继续新增 RTP P0 发送基础模块：

```text
rtp_protocol.py     RTP 固定头组包
```

RTP 激活包和音频实时流统一由 `communication_send_worker.py` 发送。音频通过
显式注入的 `read_audio_frame()` 逐帧发送，不进入普通命令队列，避免另起一个
重复的 RTP 发送线程。

独立实机冒烟入口：

```text
communication_stage3_test.py
```

该入口先使用假 socket 验证队列、TCP/RTP 发送和永久阻塞接收回调，再按入口
顶部的公网/专网配置执行真实 HTTP、TCP 和 POC 流程。当前真实流程为：HTTP
取得第一组、TCP 登录 0x01/0x81、入组 0x03/0x83、抢麦 0x08/0x88、按
动态端口持续发送测试 RTP 音频、释放麦权 0x09/0x89、TCP 心跳 0x02/0x82，
以及断开后重连并重新登录。默认使用公网，且不启动 UI 或旧通信线程。持续
RTP 测试使用入口内固定的 160 字节测试帧，不调用麦克风硬件；服务器 UDP
端口仍必须等待 TCP 的 0x88/0x0B 消息动态下发，不会把本地 4001 端口误作
远端端口。

统一发送层已增加：

```text
communication_send_worker.py
```

它把 TCP 和 RTP 的发送边界集中到一个线程：TCP 命令 payload 必须是完整协议
帧，RTP 激活命令携带目标 UDP 端口，RTP 音频帧由显式采集回调提供。该模块
仍未接入旧主流程。

第四阶段新增运行时管理层：

```text
communication_runtime.py
communication_stage4_test.py
audio_port.py
communication_session.py
```

`CommunicationRuntime` 固定管理三个线程：TCP 接收、UDP/RTP 接收和 TCP/RTP
统一发送。它负责 Worker 启停、Socket 关闭和 Socket 替换重连，但不自动执行
登录、入组等协议状态机。第四阶段测试入口只使用可控假 Socket 验证三个线程
同时运行，不修改正式 `main.py`。`audio_port.py` 统一管理 G.711/PCM 和
GPIO27；`communication_session.py` 管理 GPIO29 PTT 消抖、抢麦和释放麦权
状态机。正式 UI 入口由统一通信业务线程调用 `poll_once()`，不再额外创建
PTT 线程；独立测试入口仍可使用默认的内部线程模式。`audio_port.py` 本身不
创建线程。

第四阶段真实公网测试入口为：

```text
communication_stage4_live_test.py
```

该入口将 HTTP 组查询、登录、入组、抢麦、RTP 激活与持续音频、释放麦权、TCP
心跳和重连流程迁移到 `CommunicationRuntime` 上。当前入口在入组后等待 GPIO29
PTT，使用 `audio_port.py` 的真实 G.711 麦克风采集；主线程不直接调用 TCP
`recv()`，TCP 应答全部来自 `TCPReceiveWorker` 回调，所有发送均通过统一发送
线程完成。该入口仍不修改正式 `main.py`。

当前真实 PTT 测试入口需要将以下文件平铺烧录到设备 `/usr`：

```text
communication_stage4_live_test.py
audio_port.py
communication_session.py
communication_runtime.py
communication_queue.py
communication_send_worker.py
comm_commands.py
tcp_receive_worker.py
rtp_receive_worker.py
poc_protocol.py
rtp_protocol.py
```

## 文件迁移策略

现有 `poc_client.py` 和 `rtp_audio.py` 已经包含可复用的协议解析、协议组包、
音频编解码和硬件适配代码，但同时混合了连接管理、发送、接收、播放、按键、
HTTP/地图和历史调试逻辑。本次不在旧文件上继续叠加分支。

后续采用“新文件承载新架构，旧文件逐步回退”的方式：

1. 从旧文件只迁移已经实机验证的协议常量、组包/解析函数和硬件调用细节。
2. 新文件只保留一个明确职责，不复制 HTTP/地图/OTA/MQTT 等暂不接入业务。
3. 新线程通过明确接口通信，不直接调用另一个线程的内部方法。
4. 新线程完成独立验证后，再由主入口切换；切换前不删除旧代码。
5. 新代码不进行动态属性探测；固件接口直接按已确认的官方 API 调用。

## 线程边界

```text
TCP接收线程
    永久阻塞 socket.recv()
    只接收、拆包、解析、投递 TCP 接收事件

RTP接收线程
    暂定永久阻塞 socket.recvfrom()
    只接收 RTP、排序、抖动缓冲、播放

TCP/RTP发送线程
    唯一执行 TCP send/sendall 和 UDP sendto
    执行 P0/P1/P2 发送命令、事务重试和实时音频发送
```

## RTP 发送规则

UDP 发送只有两类业务：

```text
RTP 激活包：P0 事件，发生即发送一次
RTP 音频包：P0 实时流，由发送线程从音频采集缓冲取得后立即发送
```

单个 RTP 音频包不进入普通有界命令队列，避免队列延迟导致语音断续。

## 有界命令队列规则

队列中存放 `comm_commands.py` 定义的命令，不存放每个 RTP 音频包。

```text
P0：PTT、RTP激活、音频开始/停止、TCP立即应答、停止
P1：登录、群组、单呼、麦权等需要应答和重试的事务
P2：TCP心跳、GNSS、状态同步，可延后或合并
P3：HTTP、地图、MQTT、OTA，当前不接入
```

同类 P2 命令可以合并；P0/P1 命令不能静默丢弃；停止事件和 PTT 当前状态
使用独立状态保存，不依赖队列中是否还存在旧命令。

## UDP 永久阻塞边界

当前阶段 RTP 接收线程使用永久阻塞 `recvfrom()`。UDP 对端停止发送时，
`recvfrom()` 不会返回“断连”，因此 UDP 超时、主动重建 socket 和可中断停止
留到后续阶段单独处理。
