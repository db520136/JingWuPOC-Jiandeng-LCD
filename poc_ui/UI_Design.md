# 新 UI 最小导航验证设计

## 1. 目标

本设计用于替换旧 `lcd_touch.py` 中的 UI 组织方式，先建立一个只验证交互的最小 LVGL 程序。

第一阶段不接入 POC、GNSS、地图、RTP 音频、肩灯和传感器真实业务，只验证：

- 一级页面左右滑动；
- 二级、三级页面进入和逐级返回；
- 菜单上下滚动；
- 点击事件；
- 触摸坐标方向；
- 页面切换后的稳定性。

## 2. 新旧实现边界

新 UI 不导入 `lcd_touch.py`，不复制其中的导航和触摸状态机，也不采用：

- 动态 `set_parent()` 页面重挂载；
- 三槽 pager 旋转；
- Python 手工 `set_x()` 页面动画；
- 点击补发和坐标命中补偿；
- CST816D 手势回调驱动页面切换；
- 多套触摸状态标志互相兜底。

新 UI 只保留清晰的单向数据流：

```text
触摸硬件 -> touch_port -> LVGL indev -> LVGL 控件/手势
业务服务 -> ui_message_queue -> GUI 主循环 -> 页面刷新
页面操作 -> ui_command_adapter -> 业务服务
```

## 3. 建议文件划分

第一阶段建议新增以下文件，文件名可以在实现时微调，但职责不混合：

```text
ui_app.py              UI 应用启动和主循环
display_port.py        LCD 初始化、LVGL 显示驱动和 flush
touch_port.py          触摸读取、坐标变换和 LVGL 输入驱动
ui_router.py           二级/三级页面栈和生命周期
root_carousel.py       四个一级页面的固定横向容器
ui_gesture.py          历史手势模块（当前统一导航容器不再使用）
ui_messages.py         业务消息格式和 UI 消息队列
ui_commands.py         UI 到业务服务的命令适配
ui_pages.py            最小验证页面
ui_widgets.py          通用按钮、标题、列表行等控件
```

最小验证阶段可以先将 `ui_pages.py` 和 `ui_widgets.py` 合并，确认交互稳定后再拆分。

## 4. 显示层设计

### 4.1 `display_port.py`

只负责：

- ST7789/LCD 初始化；
- 逻辑分辨率配置；
- RGB565 显示缓冲；
- LVGL `disp_drv` 注册；
- flush 回调；
- 显示开关和必要的背光接口。

显示缓冲采用单缓冲：当前配置为 `296 x 40` 行 RGB565，约 23.7 KB。
EC800M 底层 `lcd_write()` 为同步发送，不实现双缓冲和 DMA 异步完成回调。
因此 LVGL 在 `flush_cb` 返回前不会复用该缓冲，显示适配层不再创建第二块
持久缓冲，也不在 UI 层复制或拼接整屏像素数据。

不负责：

- 页面创建；
- 触摸识别；
- 页面切换；
- 网络和业务状态。

### 4.2 分辨率和坐标

当前硬件逻辑显示区域按 `296x240` 设计。新实现必须在最小程序中确认：

- LVGL 逻辑坐标范围为 `x=0..295`、`y=0..239`；
- LCD flush 区域和逻辑坐标一致；
- 屏幕方向只在一个适配层转换；
- 不允许显示驱动和触摸驱动各自重复旋转。

## 5. 触摸层设计

### 5.1 `touch_port.py`

只负责：

- 初始化 CST816D；
- 读取原始坐标和触摸状态；
- 将原始坐标转换为统一的横屏逻辑坐标；
- 提供 LVGL pointer `read_cb`；
- 提供按下、移动、释放的基础状态。

不负责：

- 页面跳转；
- 页面栈；
- 点击回调；
- 网络暂停；
- POC 锁；
- 业务线程调度。

### 5.2 CST816D 使用原则

现有 `CST816D.py` 可以作为寄存器和硬件时序参考，但新 UI 不使用其旧的“手势回调驱动 UI”方式。

最小验证阶段优先采用：

```text
硬件读取坐标
    -> touch_port 统一坐标
    -> LVGL indev
    -> LVGL 原生事件
```

如果现有 `CST816D.py` 的动态采样、释放确认或坐标接口影响验证，则单独建立干净的底层读取适配，不把旧状态机带入新 UI。

### 5.3 触摸采样原则

- 空闲时由 IRQ 或低频轮询唤醒；
- 按下期间按固定周期采样，建议 60～100Hz；
- 当前最小验证程序使用 5ms 周期（目标约 200Hz），以提高快速滑动时的中间
  坐标密度；实际频率仍受 EC800M I²C 事务和 LVGL 处理耗时限制；
- 不使用无间隔忙等；
- 释放后立即输出 released，不能等待秒级确认尾巴；
- 一次触摸只产生一条连续的 pressed/moving/released 序列。

## 6. 一级页面设计

一级页面固定为：

```text
定位 | 肩灯 | 对讲机 | 设置
```

使用一个固定的 LVGL 横向容器或 `lv.tileview`：

- 四个页面启动时创建一次；
- 每个页面固定一个 tile/index；
- 不动态改变父对象；
- 不创建三槽 pager；
- 不在 Python 中手工计算页面位移；
- 由 LVGL 负责拖动、吸附和边界；
- 仅监听页面变化事件，更新当前一级索引。

一级页面边界：

```text
第一项右滑：保持第一项
最后一项左滑：保持最后一项
一次手势最多切换一项
滑动完成后控件仍可点击
```

一级页面横向滚动允许从任意位置开始。触摸会话先保留点击候选：位移不超过
6 px 才允许点击；位移超过 6 px 立即取消点击资格；横向位移达到 40 px 且
横向占主导时交给 `tileview` 完成跟手滚动和页面吸附；7～39 px 的移动在
释放后视为无效触摸，不点击也不切页。页面对象和按钮本身不设置可滚动标志，
避免控件与页面容器争抢横向滚动。

一级页面原生滚动参数当前为 `ROOT_SCROLL_THROW=25` 和
`ROOT_SCROLL_LIMIT=10`。前者影响释放后的惯性强度，后者影响 LVGL 进入
滚动状态的门槛；程序启动时会通过 QuecPython LVGL 绑定进行兼容检测，接口
不存在时保留固件默认值，不改变页面滚动架构。

## 7. Router 页面栈设计

### 7.1 页面记录

Router 的栈项至少包含：

```text
页面名称
页面参数
页面实例
页面来源（可选）
```

示例：

```text
intercom -> group_mode -> person_select
settings -> shoulder_mode -> shoulder_brightness
location -> location_detail
```

### 7.2 Router 职责

- `push(page, params)`：进入子页面；
- `pop()`：返回上一级；
- `replace(page, params)`：替换当前页面；
- `reset_to_root()`：回到一级页面；
- 进入页面回调；
- 离开页面回调；
- 防止动画期间重复切换。

Router 不管理一级横向容器的 tile 位置。

### 7.3 页面显示策略

二级/三级页面采用独立的当前页面容器：

- 当前页面显示在主内容区域；
- 页面切换可以先使用无动画切换验证逻辑；
- 当前验证版本使用一个 LVGL 原生横向 `nav_host`，按导航深度建立槽位：
  槽位 0 是包含一级页面 `tileview` 的根页面，槽位 1/2/3 分别对应二级、
  三级、四级页面。前进和返回都由 `nav_host` 的原生滚动、跟手和吸附处理；
- 根槽位内部保留一级页面当前 tile，因此从 C 的子页面返回时只回到 C，不会
  让 A/B 一级页面参与或闪入转场；
- 子页面槽位只保留当前导航路径上的页面，返回后释放更深层槽位。
- 不为了动画重挂载页面对象。

## 8. 右滑返回设计

二级/三级/四级页面使用任意位置右滑返回：

```text
触摸起点 x <= 32 px
向右移动距离 >= 40 px
|dx| > |dy| * 1.2
当前页面允许返回
=> router.pop()
```

注意：页面级横向滚动与菜单纵向滚动按方向分流；返回只沿当前导航深度
向上一级移动，不改变根槽位内部当前的一级页面索引。

## 9. 菜单滚动设计

设置、组呼、人员选择等列表页面使用独立的垂直滚动容器：

- `scroll_dir = VER`；
- 关闭水平滚动和水平链式滚动；
- 由 LVGL 处理垂直拖动和惯性；
- 页面级返回允许任意位置起始的明显水平右滑；
- 不在滚动过程中重建列表；
- 列表项点击由 LVGL 控件事件直接处理。

## 10. UI 消息与业务命令

### 10.1 业务到 UI

业务线程只产生数据消息或更新线程安全快照，例如：

```text
network_changed
battery_changed
location_changed
login_result
group_list_changed
single_call_changed
floor_changed
fall_alarm_changed
ota_progress
```

GUI 主循环取出消息后，调用当前页面的刷新方法。

### 10.2 UI 到业务

页面操作通过命令适配层调用业务服务，例如：

```text
login(police_no)
logout()
join_group(group_id)
leave_group(group_id)
request_floor(group_id)
release_floor(group_id)
refresh_location()
set_shoulder_mode(mode)
set_volume(level)
set_fall_detection(enabled)
```

UI 不直接操作 socket、I2C、ADC、音频缓冲或传感器寄存器。

## 11. GUI 主循环设计

主循环的职责保持简单：

```text
读取触摸输入
处理 UI 消息
处理 Router 请求
执行 lv.tick_inc()
执行 lv.task_handler()
周期性让出 CPU
```

要求：

- 只有该循环调用 LVGL API；
- 不在 LVGL 回调中执行网络、HTTP、地图下载或耗时 I2C；
- 不在触摸采样路径中暂停所有业务线程；
- 不以大量全局布尔变量协调页面和业务；
- 不用异常吞掉导航失败。

刷新边界固定为：页面和业务代码只操作 LVGL 对象；`set_text`、`set_value`
等状态变更由 LVGL 自动标记脏区，确有需要时才调用 `obj.invalidate()`。
主循环统一调用 `lv.tick_inc()` 和 `lv.task_handler()`，由 LVGL 收集、合并
并绘制脏矩形。`lcd_write()` 仅允许出现在 `display_port.py` 的 `flush_cb`
中，用于把 LVGL 已渲染的当前区域同步发送到 LCD。

## 12. 最小验证页面

建议先做以下页面：

```text
RootLocation
RootShoulder
RootIntercom
RootSettings

IntercomMenu
GroupMode
PersonSelect

SettingsMenu
ShoulderMode
Brightness
```

页面内容先使用静态标题、按钮和占位列表，不接真实服务。

## 13. 验证顺序

1. 显示方向和触摸坐标；
2. 单页面点击；
3. 四个一级页面左右滑动；
4. 一级页面首尾边界；
5. 一级页面点击进入二级页面；
6. 二级页面点击进入三级页面；
7. 三级右滑返回二级；
8. 二级右滑返回一级；
9. 菜单上下滚动；
10. 滚动与返回手势冲突测试；
11. 连续快速点击/滑动；
12. 长时间空载运行。

## 14. 进入业务迁移的门槛

只有以下项目全部通过，才开始接入真实业务：

- 一级滑动连续运行稳定；
- 二级/三级页面栈没有跳级或重复返回；
- 菜单滚动不触发误返回；
- 点击不丢失、不重复；
- 坐标方向和显示区域一致；
- LVGL 主循环无明显卡顿；
- 触摸过程中无秒级释放延迟；
- 连续运行无黑屏、白屏和对象状态错乱。

## 15. 当前验证代码

最小验证程序入口为 `main_ui_nav_test.py`，对应文件如下：

```text
display_port.py       LCD/LVGL 显示适配
touch_port.py         CST816D 简化坐标输入
ui_router.py          二级/三级页面栈
ui_gesture.py         历史手势模块（当前入口不依赖）
ui_pages.py           静态验证页面
ui_app.py             LVGL 主循环和应用编排
main_ui_nav_test.py   独立启动入口
```

该入口不启动 `main.py` 中的 POC、网络、音频和传感器服务。
