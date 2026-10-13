# OmniPicker 串口透传上位机

作者：**RUIO**。

本项目配合**专用透传设备**使用。独立桌面程序通过透传主机的数据串口控制远端 OmniPicker 夹爪，透传从机通过 CAN 连接夹爪。中间的 Wi-Fi、TCP 和从机 CAN 已配置时，选择串口并连接即可；本程序直接发送现有 `type:"omnipicker"` JSON 协议。

独立仓库：<https://github.com/ruiofang/omnipicker_serial>。配套透传设备固件见 [usbwifi-esp32](https://github.com/ruiofang/usbwifi-esp32)。

```bash
git clone https://github.com/ruiofang/omnipicker_serial.git
cd omnipicker_serial
```

## 启动

需要 Python 3.10 或更新版本、PyQt5 和 pyserial。在本仓库根目录运行：

```bash
python3 omnipicker_serial.py
```

Linux 推荐运行 `./run.sh`：启动前检查 Qt 和 pyserial，优先使用本工具的 `.venv`，其次使用当前 `python3`；当前环境不可用时尝试依赖正常的系统 Python。可通过 `OMNIPICKER_PYTHON=/完整路径/python ./run.sh` 明确指定环境。

初始窗口按当前屏幕可用区域居中适配。参数区保留完整高度；大字体或小屏幕放不下全部内容时，可滚动查看状态与日志，不会把参数滑块挤没。

缺少依赖时安装到独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python omnipicker_serial.py
```

Windows 可用 `python -m pip install -r requirements.txt` 安装依赖，再执行 `python omnipicker_serial.py`，选择主机对应的 COM 口。

## 使用

1. 选择连接主机桥接板的数据串口，点击“连接”。没有列出时点击“刷新串口”，也可手动输入设备名。
2. 默认控制单个 ID1。控制两只夹爪时选择“多 ID”，填写 `1,2`；支持逗号或空格分隔，最多 16 个不重复 ID。ID 是夹爪已有的地址，范围 0～2047。
3. 设置位置、速度、目标力矩、加速度和减速度。连接后拖动任意滑块，松开时发送一条包含当前全部参数的 `move` 指令，拖动过程中仅更新数值。也可编辑数值框后点击“全开”“全闭”或“移动到指定位置”。所有运动参数为 0～255；位置 0 全闭、255 全开。
4. 在状态表查看每个 ID 的请求结果、当前反馈、状态码、故障码、序号和反馈时间；在收发日志或“本次 JSON”查看发送内容。
5. 使用结束后点击“断开”或关闭窗口。串口后台停止并释放端口，重连时不会重放旧指令。

串口默认 115200、8N1、无流控。通常只需选串口；如果主机使用其他 UART 波特率，可展开“串口设置”修改。USB CDC 的波特率设置不决定 USB 传输速度。回包等待默认 2 秒，包含串口、网络和 CAN 的传输时间，可在同处调整。

从机的“本从机夹爪 ID 列表”仍须包含目标 ID。例如控制 `1,2`，对应从机列表也应包含 `1,2`。本程序不会修改中间链路配置。

## 参数与回包

目标力矩 `force` 默认 **255**，速度、加速度和减速度默认 **255**，指定位置默认 128。可通过滑块或数值框修改。`force` 用来调节目标力矩，数值不是牛顿值；回包中显示的是当前反馈力矩，可能与目标值不同。

按钮自动构造正确 JSON 并追加实际 LF 换行，避免手写指令遗漏逗号。单目标使用 `id`，多目标使用 `ids`，每次生成独立 `seq`。全开/全闭分别使用 `cmd:"move",position:255` 和 `cmd:"move",position:0`，同时更新界面位置值；三个按钮均保留用户设置的力矩、速度和加减速度。上位机始终明确发送 `force`，不使用固件 `open` / `close` 省略该字段时的默认 32。

例如默认参数下，多 ID 全闭发送以下内容（`seq` 每次变化，末尾追加实际换行）：

```json
{"type":"omnipicker","ids":[1,2],"cmd":"move","position":0,"speed":255,"force":255,"acceleration":255,"deceleration":255,"seq":1}
```

反馈 `state:2` 表示设备报告堵转，界面会突出显示并标明本次目标力矩；收到反馈不等于全闭已经完成。空载时若低力矩设置无法闭合，可调整力矩后重新测试，程序不会自动增加参数或重发。

多 ID 使用同一组参数逐帧下发，每个目标独立返回状态或错误，回包顺序可以不同。某个 ID 等待回包时，包含该 ID 的发送按钮会暂时禁用；可以切换到其他空闲 ID 操作。超时显示“未收到回执，可能已动作”，不会自动重发。

若松开滑块时上一条指令仍在等回包，只保留最新一次释放时的目标 ID 和完整参数；对应回包全部收到后发送一次，不积压中间值。错误、超时、断开或切换目标会取消尚未发送的滑块参数。未连接时只调整界面，连接后不会自动发送之前的改动。

“收到反馈”表示收到设备状态快照，不表示本次运动已经完成。旧序号、未知序号或无序号的回包不会解除当前请求的等待；仍可显示反馈快照并在日志中查看。全局错误不会被误认为某个夹爪执行成功。

断开只关闭本程序的串口，不能撤销设备已经收到的运动指令。现有协议没有独立停止或周期查询命令，因此界面提供的是三个已支持的运动操作。

完整字段、默认值和错误码见 [JSON 协议文档](JSON协议.md)。

## 日志和过载

串口收发运行在后台线程，界面定时批量刷新。显示事件队列最多 2048 条，日志最多保留 1000 行，状态表最多 128 个 ID；过载时显示丢弃计数，状态等待可能超时。保存日志导出的是当前保留内容，不是无限时长完整录制。

TX 计数表示整条 JSON 已成功写入串口，不表示夹爪收到或动作完成。部分写入失败会终止本次串口会话，并提示确认状态后再手动决定后续命令。

主机默认实时透传在过载时可能丢字节；多台从机同时回传还可能交错 JSON。优先采用一台 CAN 从机控制同一总线上的多个 ID，并控制发送频率，具体链路边界见协议文档。

## 串口打开失败

串口列表为空时，连接区会区分“未检测到串口”和“pyserial 依赖缺失 / 枚举失败”。依赖错误同时显示当前 Python 路径；Conda、系统 Python 和自行安装的 Python 的依赖互相独立，必须安装到启动程序使用的解释器。缺少 pyserial 时，可用该解释器执行 `/完整路径/python -m pip install pyserial==3.5`，或使用 `run.sh` 选择已有的可用环境。安装后须重启程序。刷新失败会保留手动输入的设备名，便于重试。

如果出现 `module 'serial' has no attribute 'Serial'`，通常是同一 Python 环境安装了用于序列化的同名 `serial` 包，覆盖了串口库 pyserial。`run.sh` 会在启动前识别并报告该冲突；连接时的检查也会保留界面，避免核心转储。

需要修复该环境时，使用它自身的 Python 执行以下命令，将 `/完整路径/python` 替换为实际解释器路径。卸载同名包后必须恢复 pyserial，因为它们可能共享并覆盖了 `serial/__init__.py`：

```bash
/完整路径/python -m pip uninstall serial
/完整路径/python -m pip install --force-reinstall --no-deps pyserial==3.5
```

重启程序后再连接。启动脚本只选择已有环境，不会自动卸载或安装全局包。

- **被占用**：关闭占用所选端口的串口调试器、ESP-IDF Monitor 或其他上位机，再连接。程序会保留所选端口。
- **无权限**：出现 `Permission denied` / `Errno 13` 时，按下方“Linux 串口权限设置”处理。
- **不存在或已拔出**：连接设备后刷新并选择实际端口。

打开、关闭和重连不会发送运动指令，也不会主动操作中间桥板的 BOOT/复位。程序在打开前将 DTR/RTS 请求值设为关闭；部分驱动在打开时仍可能产生控制线瞬变，行为见 [pyserial 官方文档](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)。

### Linux 串口权限设置

Ubuntu / Debian 通常通过 `dialout` 用户组授予串口访问权限。以下以 `/dev/ttyACM0` 为例，使用 `/dev/ttyUSB0` 等其他串口时替换设备路径。

先在运行上位机的普通用户终端中查看设备权限和当前会话的用户组：

```bash
ls -l /dev/ttyACM0
id -nG
```

#### 临时授权（立即生效）

需要立即调试、暂时不修改用户组时，可给当前普通用户添加所选串口的读写权限，无需注销登录。在运行上位机的普通用户终端中执行：

```bash
sudo setfacl -m "u:$USER:rw" /dev/ttyACM0
getfacl /dev/ttyACM0
```

授权后直接以普通用户启动上位机，或在已打开的窗口中重新点击“连接”。该方式只为当前用户添加访问权限；设备拔插、重新枚举或系统重启后，设备节点重建时通常需要重新授权。

如提示找不到 `setfacl`，Ubuntu / Debian 可先安装 `acl`：

```bash
sudo apt install acl
```

需要撤销上述用户 ACL 条目时执行：

```bash
sudo setfacl -x "u:$USER" /dev/ttyACM0
```

若当前用户同时通过 `dialout` 组或其他规则获得权限，删除该 ACL 条目不会移除那些权限。

#### 长期授权（dialout 用户组）

若设备权限类似 `crw-rw---- root dialout`，而 `id -nG` 中没有 `dialout`，将当前用户加入该组：

```bash
sudo usermod -aG dialout "$USER"
```

完成后**注销桌面会话并重新登录**（也可以重启系统），让新组权限生效。仅关闭并重开一个终端通常不足以更新桌面会话的组权限。重新登录后，确认 `id -nG` 包含 `dialout`，再从本仓库根目录启动上位机：

```bash
id -nG
./run.sh
```

如果暂时不注销，可以在加入用户组后执行以下命令，进入具有 `dialout` 权限的新 shell，再从**这个终端**启动上位机：

```bash
newgrp dialout
./run.sh
```

已有的上位机窗口、IDE 和其他终端不会因此自动获得新权限，需退出旧上位机后重新启动。

#### 串口占用排查

如果提示 `Could not exclusively lock port`（独占锁失败，常伴随 `Errno 11`），应检查串口占用：

```bash
fuser -v /dev/ttyACM0
```

若没有输出，也可用 `sudo fuser -v /dev/ttyACM0` 检查其他用户的进程。关闭列出的串口监视器或其他上位机后重试；ESP-IDF Monitor 可用 `Ctrl+]` 退出。修改串口权限不能解除其他程序持有的独占锁。

## 软件验证

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
```

测试使用模拟串口或本地 PTY，验证 JSON、分帧、请求关联、真实串口字节与 Qt 界面生命周期，不连接实际 USB 桥板或夹爪。真实设备仍需通过主机串口验证各 ID 的独立开合与反馈。
