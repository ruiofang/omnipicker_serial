# 串口 JSON 控制协议

本协议用于从主机数据串口控制 ESP32 从机连接的 CAN 设备，包含 OmniPicker 夹爪控制和原始 CAN 帧透传。主机保持 USB/UART 与 TCP 透明转发，由从机根据网页选择的控制模式解释 JSON。

| 从机网页控制模式 | 请求 `type` | 功能 |
| --- | --- | --- |
| OmniPicker CAN控制 | `"omnipicker"` | 按夹爪 ID 下发位置、速度、力矩、加减速度，返回设备状态 |
| CAN透传（JSON） | `"can"` | 按指定 CAN ID 发送原始帧，回传总线接收帧 |

两种模式分别使用下文对应的字段；普通串口透传模式只转发字节，不解释这些命令。GPIO、CAN 波特率和从机夹爪 ID 列表在网页配置，不属于每条 JSON 命令的字段。

## 发送前的配置

主机使用串口透传和 TCP Server；从机完成配对，或使用 TCP Client 连接主机。所有命令从主机的数据串口发送，回包从同一串口接收。

CAN TX 默认 GPIO6、RX 默认 GPIO7，网页可修改。需要外接 CAN 收发器，GPIO 不能直接连接 CAN_H/CAN_L。

OmniPicker 模式使用经典 CAN，固定 1 Mbps、80% 采样点。控制 ID1 和 ID2 时，在从机网页的“本从机夹爪 ID 列表”填写 `1,2` 并保存。该列表默认是 `1`，因此只配置 `1` 时，发给 ID2 的命令会被从机忽略。填写列表不会改变夹爪自身的 ID。

原始 CAN 模式不使用该夹爪 ID 列表过滤；总线速率按网页配置，可选 10、20、50、100、125、250、500、800 kbit/s 和 1 Mbit/s。两种模式均不支持 CAN-FD。

## 消息分帧与格式

每条请求占一行，包含一个完整 JSON 对象，末尾必须发送实际的 LF 或 CRLF：

| 结束方式 | 实际字节 | 说明 |
| --- | --- | --- |
| LF | `0A` | 推荐，对应程序字符串中的 `\n` |
| CRLF | `0D 0A` | 也支持，对应程序字符串中的 `\r\n` |

串口工具应勾选“发送后追加换行”。在输入框中直接打出反斜杠和字母 `n`，若工具不进行转义，会发送两个普通字符，不能结束命令。仅发送 CR 也不会提交命令。

- JSON 正文最多 **384 字节**，包含正文前后空白，不包含结尾 LF 或 CRLF。
- 字段名、字符串值区分大小写；数字使用十进制整数，不要写成字符串。布尔值使用 `true` / `false`。
- 使用 ASCII JSON，可直接按 UTF-8 编码这些 ASCII 字符；不要添加中文备注、BOM、注释或自定义字段。
- 支持串口/TCP 拆包和多行粘包；不需要一次底层写入恰好对应一条命令。
- 未知字段、重复字段、错误类型或越界参数会拒绝整条命令，不发送 CAN 帧。
- 空行会产生 `invalid_json`；超长行或包含 NUL、其他非法控制字符、非 ASCII 原始字节的行会整行丢弃，到下一次 LF 恢复，不保证返回错误通知。
- 回包统一以 LF 结束。字段顺序无固定要求，接收程序应按字段名解析。

下文 JSON 示例均需追加实际换行；代码块中的说明文字不属于串口数据。

## OmniPicker 请求字段

| 字段 | 类型与范围 | 规则 |
| --- | --- | --- |
| `type` | 字符串 | 必填，固定为 `"omnipicker"` |
| `id` | 整数，0～2047 | 单个目标夹爪 ID；与 `ids` 二选一 |
| `ids` | 整数数组 | 1～16 个不重复 ID，每个为 0～2047；与 `id` 二选一 |
| `cmd` | 字符串 | 必填，`"move"`、`"open"` 或 `"close"` |
| `position` | 整数，0～255 | `move` 必填；0 为全闭，255 为全开；`open` / `close` 不允许此字段 |
| `speed` | 整数，0～255 | 目标速度；`move` 必填，`open` / `close` 可省略，默认 255 |
| `force` | 整数，0～255 | 目标力矩；`move` 必填，`open` / `close` 可省略，默认 **32** |
| `acceleration` | 整数，0～255 | 目标加速度；`move` 必填，`open` / `close` 可省略，默认 255 |
| `deceleration` | 整数，0～255 | 目标减速度；`move` 必填，`open` / `close` 可省略，默认 255 |
| `seq` | 整数，0～4294967295 | 可选，请求标识；对应目标的状态/错误回包携带相同值 |

`open` 自动使用 `position=255`，`close` 自动使用 `position=0`。速度、力矩、加速度和减速度的 255 对应各自最大设定；本桥接协议不做物理单位换算。

`ids` 中的目标使用同一组运动参数。不同 ID 需要不同参数时，发送多条单 ID 命令。每台从机只执行自己配置列表中的 ID，列表外目标静默忽略，不返回“设备不存在”错误。

### force 的作用

`force` 是**目标力矩参数**，用于调整夹持力度，取值 0～255；255 表示最大力矩。张合位置由 `position` 决定，运动速度由 `speed` 决定。该参数随运动命令一起下发。依据：[智元 OmniPicker 官方手册第 6.3 节](https://agibot.com/DOCS/APC2026)。

目标值应按实际夹持效果调整。协议中的数值没有直接的 N 或 N·m 换算，`force:255` 不表示 255 N，也不能据此承诺某个实际夹持力。`force:0` 也不是本协议定义的停止命令。

请求中的 `force` 是目标值，状态回包中的 `force` 是设备反馈的当前力矩值。例如发送 255、收到 190 或 192，表示收到的当前值与目标设定不同，不是固件把命令中的 255 改成了 190/192。

省略 `force` 只适用于 `open` / `close`，本 JSON 协议会填入 **32**。本项目的[独立串口上位机](README.md)默认明确发送 `force:255`，可手动调节；全开 / 全闭均使用 `move` 和目标位置，因此不采用固件的省略默认值。`move` 必须明确给出五个运动参数。

### 单 ID 示例

ID1 全闭，所有运动参数设为 255：

```json
{"type":"omnipicker","id":1,"cmd":"move","position":0,"speed":255,"force":255,"acceleration":255,"deceleration":255,"seq":1}
```

ID2 全开，所有运动参数设为 255：

```json
{"type":"omnipicker","id":2,"cmd":"move","position":255,"speed":255,"force":255,"acceleration":255,"deceleration":255,"seq":2}
```

ID1 全闭，速度 100、目标力矩 60，省略的加减速度自动使用 255：

```json
{"type":"omnipicker","id":1,"cmd":"close","speed":100,"force":60,"seq":3}
```

ID1 全开简写，使用速度 255、目标力矩 32、加减速度 255：

```json
{"type":"omnipicker","id":1,"cmd":"open","seq":4}
```

ID2 移动到中间位置，并显式设置全部运动参数：

```json
{"type":"omnipicker","id":2,"cmd":"move","position":128,"speed":100,"force":60,"acceleration":100,"deceleration":100,"seq":5}
```

### 多 ID 示例

ID1 和 ID2 使用相同参数全闭：

```json
{"type":"omnipicker","ids":[1,2],"cmd":"close","speed":100,"force":60,"seq":6}
```

ID1 和 ID2 全开，使用简写默认参数：

```json
{"type":"omnipicker","ids":[1,2],"cmd":"open","seq":7}
```

一条 `ids` 请求会逐个发送独立 CAN 帧，不是硬件同步触发，也不是整批原子操作。某个 ID 返回 `busy` 或错误时，其他符合条件的 ID 仍可执行。回包逐 ID 返回，用 `(id, seq)` 区分；回包顺序不保证与 `ids` 顺序一致。

### 下发 CAN 帧

每个目标使用经典 CAN 标准帧，CAN ID 等于夹爪 ID，DLC 固定为 8：

| 字节 | D0 | D1 | D2 | D3 | D4 | D5 | D6 | D7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 内容 | `00` | position | speed | force | acceleration | deceleration | `00` | `00` |

所有运动参数原值写入单个字节；`seq` 仅由桥接层保存，不写入 CAN 帧。

## OmniPicker 状态和错误回包

### 状态回包

```json
{"type":"omnipicker","id":1,"seq":1,"event":"status","fault":0,"state":1,"position":120,"speed":100,"force":190}
```

| 字段 | 含义 |
| --- | --- |
| `type` | 固定为 `"omnipicker"` |
| `id` | 返回状态的夹爪 ID |
| `seq` | 请求带有 `seq` 时返回；未带时省略 |
| `event` | 固定为 `"status"` |
| `fault` | 设备故障码，原始 0～255 数值 |
| `state` | 设备状态码，原始 0～255 数值 |
| `position` | 当前反馈位置，0～255 |
| `speed` | 当前反馈速度，0～255 |
| `force` | 当前反馈力矩，0～255，不要求等于请求目标值 |

设备手册定义的常用状态码如下；固件保留未知数值，不自行翻译或改写。夹爪在收到控制命令后立即上报状态快照，**`event:"status"` 不等于本次运动已经完成**。即使快照中的 `state` 为 0，也应结合反馈位置和实际动作判断。依据：[官方手册第 6.3.2 节](https://agibot.com/DOCS/APC2026)。

| 数值 | `state` | `fault` |
| --- | --- | --- |
| 0 | 到达目标位置 | 无故障 |
| 1 | 移动中 | 过温 |
| 2 | 堵转 | 超速 |
| 3 | 物体掉落 | 初始化异常 |
| 4 | — | 超限 |

本模式只在某 ID 有待回执命令时接收其状态，匹配成功后返回一次 `status`；没有周期订阅或独立 `query` 命令。

### 错误回包

```json
{"type":"omnipicker","id":2,"seq":2,"event":"error","error":"timeout"}
{"type":"omnipicker","event":"error","error":"invalid_json"}
{"type":"omnipicker","event":"error","error":"response_overflow","rejected":3}
```

| `error` | 含义 |
| --- | --- |
| `invalid_json` | JSON 或字段不合规；不携带 `id` / `seq` |
| `busy` | 该 ID 已有一条等待回执的命令，或待处理目标槽已满 |
| `timeout` | 发送后 500 ms 内未收到匹配的有效状态帧 |
| `can_tx_failed` | CAN 发送被拒绝或驱动报告发送失败 |
| `bus_off` | CAN 总线进入 bus-off，控制停止 |
| `can_not_running` | CAN 驱动未正常运行 |
| `can_rx_congested` | 旧接收帧持续积压，拒绝新运动请求 |
| `can_rx_overflow` | CAN 驱动接收队列或 FIFO 溢出，尚未匹配的命令失败 |
| `can_cancelled` | 超时清理共享发送队列时，其他未完成目标被取消 |
| `response_backpressure` | 回传缓存没有足够空间，拒绝新的运动请求 |
| `response_overflow` | 错误回包未能进入缓存；`rejected` 为累计未入队回包数 |

能关联目标时携带 `id`，请求包含序号时也携带 `seq`；全局错误或解析失败可能不包含它们。持续过载时错误通知可能合并计数，不能保证每个请求均有独立回包。

## 原始 CAN 请求字段

| 字段 | 类型与范围 | 规则 |
| --- | --- | --- |
| `type` | 字符串 | 必填，固定为 `"can"` |
| `id` | 整数 | 必填；标准帧 0～2047，扩展帧 0～536870911 |
| `extended` | 布尔值 | 可选，默认 `false`；`true` 使用 29 位扩展帧 |
| `rtr` | 布尔值 | 可选，默认 `false`；`true` 使用远程帧 |
| `data` | 十六进制字符串 | 数据帧必填；0～16 个连续 HEX 字符，字符数必须为偶数，表示 0～8 字节；大小写均可，不带空格、分隔符或 `0x` |
| `dlc` | 整数，0～8 | 数据帧可省略，自动按 `data` 长度计算；提供时须与数据字节数一致。远程帧必填，且 `data` 必须省略或为空 |
| `seq` | 整数，0～4294967295 | 可选，仅用于发送入队确认或可关联的即时错误，不写入 CAN 帧 |

该模式每条 JSON 表示一帧。不同 ID 可连续发送多行；运动参数包含在 `data` 中，由接收设备解释，不使用 OmniPicker 的 `cmd` / `ids` 等字段。

### 原始 CAN 示例

标准数据帧：

```json
{"type":"can","id":291,"data":"0102030405060708","seq":10}
```

29 位扩展数据帧：

```json
{"type":"can","id":536870911,"extended":true,"data":"AABB","seq":11}
```

远程帧，请求长度 8：

```json
{"type":"can","id":291,"rtr":true,"dlc":8,"seq":12}
```

零字节数据帧：

```json
{"type":"can","id":291,"data":"","seq":13}
```

OmniPicker ID1 全闭和 ID2 全开，速度、力矩、加减速度均为 255；从机与夹爪总线需使用 1 Mbps：

```json
{"type":"can","id":1,"data":"0000FFFFFFFF0000","seq":14}
{"type":"can","id":2,"data":"00FFFFFFFFFF0000","seq":15}
```

## 原始 CAN 回包

### 发送入队确认

```json
{"type":"can","event":"tx_queued","id":291,"extended":false,"rtr":false,"dlc":8,"data":"0102030405060708","seq":10}
```

`tx_queued` 只表示 CAN 驱动接受了帧，不保证总线 ACK、目标设备收到或执行。输入有 `seq` 时确认携带相同值，未提供则省略。

### CAN 接收帧

```json
{"type":"can","event":"rx","id":291,"extended":false,"rtr":false,"dlc":3,"data":"102030"}
{"type":"can","event":"rx","id":291,"extended":false,"rtr":true,"dlc":8,"data":""}
```

`rx` 保留总线接收帧的 ID、扩展标志、远程标志、DLC 和载荷，输出 HEX 使用大写。总线主动上报的合法帧也会回传。接收帧不携带请求 `seq`，因为原始 CAN 帧没有本 JSON 的请求序号；应用应按设备自身协议关联回复。

`event` 仅用于回包，不能把带有 `event` 的 JSON 原样作为请求发送。

### 原始 CAN 错误

```json
{"type":"can","id":291,"seq":10,"event":"error","error":"can_tx_failed"}
{"type":"can","event":"error","error":"rx_overflow","dropped":3}
{"type":"can","event":"error","error":"can_rx_overflow","occurrences":1}
```

| `error` | 含义 |
| --- | --- |
| `invalid_json` | JSON 或字段不合规，不携带 `id` / `seq` |
| `can_not_running` | CAN 驱动未正常运行 |
| `bus_off` | CAN 总线进入 bus-off，停止发送 |
| `can_tx_failed` | 即时发送拒绝或异步硬件发送失败 |
| `invalid_can_frame` | 收到超出经典 CAN 帧限制的帧 |
| `can_rx_overflow` | 驱动接收队列或 FIFO 溢出；`occurrences` 为观察到告警的次数，不是精确丢帧数 |
| `rx_overflow` | 回传缓存空间不足，新接收帧按整帧丢弃；`dropped` 为累计丢帧数 |
| `response_backpressure` | 无空间存储发送确认，拒绝新请求；累计提示中的 `rejected` 为未能入队的错误回包数 |

即时发送错误能携带请求 ID/seq；发送入队后驱动报告的异步失败可能不携带，因为驱动无法确认具体失败帧。入队确认后仍可能收到异步错误。

## 发送节奏和连接边界

OmniPicker 模式每个 ID 最多保留一条待回执命令，等待窗口为 500 ms。相同 ID 的下一条命令宜在收到对应 `status` 或 `error` 后发送；不同 ID 可使用 `ids` 一起发送。原始 CAN 模式不等待设备回复，可连续下发多帧，但应用需要自行控制总线和回传负载。

两种模式均采用单次 CAN 发送，不自动重发。超时或发送失败不能证明设备未动作；应用应检查当前状态再决定下一条命令。bus-off 后需排除总线故障，再在网页重新保存并应用控制模式；TCP 重连不会自动解除 bus-off。

连接切换会清除半条 JSON、排队回包和尚未发送的 CAN 帧，不自动重放旧会话命令。OmniPicker 的 `seq` 由桥接层关联，设备本身没有该序号，迟到的旧状态仍可能被关联到后续请求。

主机仍保留原透传策略：实时模式过载时会丢弃旧字节。需要完整解析 JSON 时，可在主机 menuconfig 关闭 `CONFIG_BRIDGE_REALTIME` 并控制发送频率。

一台 CAN 从机连接一条总线并控制多个 ID，可避免多个 TCP 来源同时回传的问题。使用多台从机时，每个夹爪 ID 应只归属一台从机；主机合并多个来源的字节，同时回包可能交错。跨从机逐条发送、收到完整换行回包后再发下一条可减少重叠，但异步错误仍可能同时到达。跨从机使用 `ids` 能下发命令，不保证同时回包的行完整。

## 程序追加换行示例

Python 中已打开串口对象 `ser` 后，可以这样发送：

```python
import json

command = {
    "type": "omnipicker",
    "ids": [1, 2],
    "cmd": "close",
    "speed": 100,
    "force": 60,
    "seq": 20,
}
payload = json.dumps(command, separators=(",", ":")).encode("ascii") + b"\n"
ser.write(payload)  # CRLF 时将 b"\n" 改为 b"\r\n"
```

接收侧应缓存字节并按 LF 分行，去除行尾可选 CR 后解析 JSON，不要假定一次串口 `read()` 就得到一条完整回包。

## 实现与参考

- [OmniPicker 请求解析与分帧](https://github.com/ruiofang/usbwifi-esp32/blob/main/main/omnipicker_protocol.c)
- [OmniPicker CAN 控制与回包](https://github.com/ruiofang/usbwifi-esp32/blob/main/main/omnipicker_can.c)
- [原始 CAN 请求与回包编码](https://github.com/ruiofang/usbwifi-esp32/blob/main/main/can_frame_protocol.c)
- [原始 CAN 收发与错误处理](https://github.com/ruiofang/usbwifi-esp32/blob/main/main/can_passthrough.c)
- [配套固件的 OmniPicker 资料](https://github.com/ruiofang/usbwifi-esp32/blob/main/doc/OmniPicker/README.md)
- [智元 OmniPicker 官方手册](https://agibot.com/DOCS/APC2026)，设备参数、CAN 字节布局和状态码见第 6.3 节。
