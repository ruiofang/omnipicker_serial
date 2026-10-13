#!/usr/bin/env python3
"""OmniPicker desktop controller for an already-configured serial bridge."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QCursor
from PyQt5.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStyle,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

from control_session import ControlSession
from protocol import MotionParameters, build_command, encode_command, parse_targets
from serial_worker import SerialWorker, require_serial_api


STATE_LABELS = {0: "已到位置", 1: "移动中", 2: "堵转", 3: "物体掉落"}
FAULT_LABELS = {0: "无故障", 1: "过温", 2: "超速", 3: "初始化异常", 4: "超限"}


def discover_serial_ports():
    """Report broken dependencies separately from an empty device list."""
    require_serial_api()
    if not callable(getattr(list_ports, "comports", None)):
        raise RuntimeError(
            f"当前 Python 无法加载 pyserial 串口枚举接口。Python：{sys.executable}。"
            "请使用本工具 run.sh，或在该 Python 环境安装 requirements.txt。")
    return list_ports.comports()


STYLE = """
QMainWindow { background: #f3f5fa; }
QLabel { color: #26364d; }
QGroupBox { background: white; border: 1px solid #dbe2ed; border-radius: 8px;
            margin-top: 12px; padding: 12px 10px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; }
QPushButton { padding: 7px 14px; border: 1px solid #cbd5e3; border-radius: 5px;
              background: white; color: #26364d; min-height: 20px; }
QPushButton:hover { background: #edf3ff; border-color: #88aaf0; }
QPushButton:disabled { color: #9aa6b5; background: #f5f7fa; border-color: #e1e6ef; }
QPushButton#connectButton, QPushButton#moveButton { background: #2563eb; color: white; border-color: #2563eb; }
QPushButton#closeButton { background: #eaf0ff; color: #1d4ed8; border-color: #b5c9f6; }
QPushButton#openButton { background: #e9f7f3; color: #087f65; border-color: #a9ded1; }
QPushButton#connectButton:disabled, QPushButton#moveButton:disabled,
QPushButton#closeButton:disabled, QPushButton#openButton:disabled { background: #eef1f6; color: #9aa6b5; border-color: #e1e6ef; }
QComboBox, QSpinBox, QDoubleSpinBox { padding: 5px; border: 1px solid #ccd6e5;
                                   border-radius: 4px; background: white; }
QTableWidget, QPlainTextEdit { background: white; border: 1px solid #dbe2ed; border-radius: 4px; }
QHeaderView::section { background: #edf2f9; color: #35455d; padding: 6px; border: none; }
QTableWidget { gridline-color: #e8edf5; }
"""


class ParameterControl(QWidget):
    def __init__(self, title: str, value: int, hint: str):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        label = QLabel(title)
        label.setStyleSheet("font-weight: 600;")
        layout.addWidget(label)
        row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 255)
        self.value = QSpinBox()
        self.value.setRange(0, 255)
        self.value.setValue(value)
        self.value.setMinimumWidth(max(80, self.value.fontMetrics().horizontalAdvance("255") + 48))
        self.value.setMinimumHeight(self.value.fontMetrics().height() + 17)
        self.slider.setValue(value)
        self.slider.valueChanged.connect(self.value.setValue)
        self.value.valueChanged.connect(self.slider.setValue)
        row.addWidget(self.slider, 1)
        row.addWidget(self.value)
        layout.addLayout(row)
        note = QLabel(hint)
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 11px; color: #64748b;")
        layout.addWidget(note)


class MainWindow(QMainWindow):
    def __init__(self, port_provider=None, worker_factory=SerialWorker):
        super().__init__()
        self.port_provider = port_provider if port_provider is not None else discover_serial_ports
        self.worker_factory = worker_factory
        self.worker = None
        self.connected = False
        self.closing = False
        self.last_serial_error = ""
        self.last_command = None
        self._pending_slider_request = None
        self.session = ControlSession()
        self.session.ensure_device(1)
        self.session.ensure_device(2)
        self.table_ids: list[int] = []
        self.setWindowTitle("OmniPicker 串口透传上位机")
        self.setMinimumSize(640, 480)
        self.setStyleSheet(STYLE)
        root = QWidget()
        self.content_widget = root
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.NoFrame)
        self.content_scroll.setWidget(root)
        self.setCentralWidget(self.content_scroll)
        layout = QVBoxLayout(root)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        title = QLabel("OmniPicker")
        title.setStyleSheet("font-size: 25px; font-weight: 700; color: #194fa6;")
        caption = QLabel("串口透传控制 · 选择主机串口即可连接")
        caption.setStyleSheet("color: #64748b;")
        header = QHBoxLayout()
        header.addWidget(title)
        header.addWidget(caption)
        header.addStretch()
        layout.addLayout(header)
        self._build_connection(layout)
        self._build_controls(layout)
        self._build_feedback(layout)
        self._build_log(layout)
        self.refresh_ports()
        self.render_devices()
        self.update_actions()
        self._fit_initial_geometry()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_events)
        self.timer.start(40)

    def _fit_initial_geometry(self, available_geometry=None):
        if available_geometry is None:
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
            if screen is None:
                self.resize(1180, 880)
                return
            available_geometry = screen.availableGeometry()
        self.ensurePolished()
        self.content_widget.layout().activate()
        content_hint = self.content_widget.sizeHint()
        title_height = self.style().pixelMetric(QStyle.PM_TitleBarHeight)
        width = min(max(1120, content_hint.width() + 24), max(1, available_geometry.width() - 32))
        height = min(max(820, content_hint.height()),
                     max(1, available_geometry.height() - title_height - 32))
        self.setMinimumSize(min(640, width), min(480, height))
        self.resize(width, height)
        self.move(available_geometry.x() + (available_geometry.width() - width) // 2,
                  available_geometry.y() + max(0, (available_geometry.height() - height - title_height) // 2))

    def _build_connection(self, layout):
        group = QGroupBox("串口连接")
        box = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("主机串口"))
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(290)
        self.port.setToolTip("选择连接主机桥接板的数据串口，例如 /dev/ttyACM0 或 COM3")
        self.refresh_button = QPushButton("刷新串口")
        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("connectButton")
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        self.settings_button = QPushButton("串口设置")
        self.settings_button.setCheckable(True)
        row.addWidget(self.port, 1)
        for button in (self.refresh_button, self.connect_button, self.disconnect_button, self.settings_button):
            row.addWidget(button)
        box.addLayout(row)
        self.settings_panel = QWidget()
        advanced = QHBoxLayout(self.settings_panel)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.addWidget(QLabel("波特率"))
        self.baudrate = QComboBox()
        self.baudrate.setEditable(True)
        self.baudrate.addItems(["115200", "230400", "460800", "921600", "1000000", "2000000", "3000000"])
        advanced.addWidget(self.baudrate)
        advanced.addWidget(QLabel("回包等待"))
        self.reply_timeout = QDoubleSpinBox()
        self.reply_timeout.setRange(0.6, 10.0)
        self.reply_timeout.setSingleStep(0.5)
        self.reply_timeout.setValue(2.0)
        self.reply_timeout.setSuffix(" s")
        advanced.addWidget(self.reply_timeout)
        note = QLabel("默认 115200 / 8N1；UART 须匹配主机，USB CDC 不受波特率限速")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748b;")
        advanced.addWidget(note, 1)
        self.settings_panel.hide()
        box.addWidget(self.settings_panel)
        self.connection_status = QLabel("未连接")
        self.connection_status.setWordWrap(True)
        box.addWidget(self.connection_status)
        layout.addWidget(group)
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.connect_serial)
        self.disconnect_button.clicked.connect(self.disconnect_serial)
        self.settings_button.toggled.connect(self.settings_panel.setVisible)

    def _build_controls(self, layout):
        group = QGroupBox("夹爪控制")
        box = QVBoxLayout(group)
        targets = QHBoxLayout()
        self.single_mode = QRadioButton("单 ID")
        self.multi_mode = QRadioButton("多 ID")
        self.single_mode.setChecked(True)
        self.target_modes = QButtonGroup(self)
        self.target_modes.addButton(self.single_mode)
        self.target_modes.addButton(self.multi_mode)
        self.target_id = QSpinBox()
        self.target_id.setRange(0, 2047)
        self.target_id.setValue(1)
        self.target_ids = QLineEdit("1,2")
        self.target_ids.setPlaceholderText("例如 1,2,16，最多 16 个不重复 ID")
        self.target_ids.setEnabled(False)
        targets.addWidget(self.single_mode)
        targets.addWidget(self.target_id)
        targets.addSpacing(16)
        targets.addWidget(self.multi_mode)
        targets.addWidget(self.target_ids, 1)
        self.target_hint = QLabel("每个 ID 独立接收回包")
        self.target_hint.setStyleSheet("color: #64748b;")
        targets.addWidget(self.target_hint)
        box.addLayout(targets)
        parameters = QGridLayout()
        self.parameters = {}
        for column, (name, label, initial, hint) in enumerate((
            ("position", "位置", 128, "0 全闭 · 255 全开"),
            ("speed", "速度", 255, "0～255 的目标速度"),
            ("force", "目标力矩", 255, "0～255，不是牛顿值"),
            ("acceleration", "加速度", 255, "0～255 的目标加速度"),
            ("deceleration", "减速度", 255, "0～255 的目标减速度"),
        )):
            control = ParameterControl(label, initial, hint)
            self.parameters[name] = control
            parameters.addWidget(control, 0, column)
            parameters.setColumnStretch(column, 1)
            control.slider.sliderReleased.connect(self.send_slider_motion)
        box.addLayout(parameters)
        actions = QHBoxLayout()
        self.open_button = QPushButton("全开")
        self.open_button.setObjectName("openButton")
        self.close_button = QPushButton("全闭")
        self.close_button.setObjectName("closeButton")
        self.move_button = QPushButton("移动到指定位置")
        self.move_button.setObjectName("moveButton")
        for button in (self.open_button, self.close_button, self.move_button):
            actions.addWidget(button, 1)
        box.addLayout(actions)
        self.command_status = QLabel("连接后点击按钮或拖动滑块后松开发送；拖动过程中仅更新数值")
        self.command_status.setWordWrap(True)
        self.command_status.setStyleSheet("color: #64748b;")
        box.addWidget(self.command_status)
        layout.addWidget(group)
        self.single_mode.toggled.connect(self.target_mode_changed)
        self.multi_mode.toggled.connect(self.target_mode_changed)
        self.target_id.valueChanged.connect(self.target_selection_changed)
        self.target_ids.textChanged.connect(self.target_selection_changed)
        self.open_button.clicked.connect(lambda: self.send_motion("open"))
        self.close_button.clicked.connect(lambda: self.send_motion("close"))
        self.move_button.clicked.connect(lambda: self.send_motion("move"))

    def _build_feedback(self, layout):
        group = QGroupBox("状态反馈")
        box = QVBoxLayout(group)
        note = QLabel("收到反馈代表收到状态快照；请结合反馈位置判断动作，目标力矩与当前力矩可以不同。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748b;")
        box.addWidget(note)
        self.table = QTableWidget(0, 9)
        headers = ["ID", "请求结果", "位置", "速度", "当前力矩", "状态", "故障", "请求 / 回包序号", "最后反馈"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        header_metrics = self.table.horizontalHeader().fontMetrics()
        for column, width in enumerate((48, 190, 60, 60, 85, 125, 130, 135, 125)):
            self.table.setColumnWidth(column, max(width, header_metrics.horizontalAdvance(headers[column]) + 24))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(160)
        box.addWidget(self.table)
        layout.addWidget(group, 2)

    def _build_log(self, layout):
        tabs = QTabWidget()
        log_page = QWidget()
        box = QVBoxLayout(log_page)
        box.setContentsMargins(0, 5, 0, 0)
        row = QHBoxLayout()
        self.count_label = QLabel("TX 0 / RX 0 / 无效回包 0 / 显示丢弃 0")
        row.addWidget(self.count_label, 1)
        clear = QPushButton("清空日志")
        save = QPushButton("保存当前日志")
        row.addWidget(clear)
        row.addWidget(save)
        box.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(1000)
        self.log_view.setMinimumHeight(100)
        box.addWidget(self.log_view)
        tabs.addTab(log_page, "收发日志")
        preview_page = QWidget()
        preview_box = QVBoxLayout(preview_page)
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setPlaceholderText("发送后显示程序生成的 JSON，串口输出自动追加 LF。")
        preview_box.addWidget(self.command_preview)
        copy = QPushButton("复制本次 JSON")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.command_preview.toPlainText()))
        preview_box.addWidget(copy)
        tabs.addTab(preview_page, "本次 JSON")
        layout.addWidget(tabs, 1)
        clear.clicked.connect(self.log_view.clear)
        save.clicked.connect(self.save_log)

    def log(self, text: str):
        self.log_view.appendPlainText(datetime.now().strftime("%H:%M:%S.%f")[:-3] + " " + text)

    def set_command_status(self, text: str, error=False):
        self.command_status.setText(text)
        self.command_status.setStyleSheet("color: #b42318;" if error else "color: #64748b;")

    def refresh_ports(self):
        selected = self.port.currentText().strip()
        try:
            ports = sorted(self.port_provider(), key=lambda item: item.device)
        except (OSError, RuntimeError) as error:
            message = f"串口枚举失败：{error}"
            if self.worker is None and not self.connected:
                self.connection_status.setText(message)
                self.connection_status.setStyleSheet("color: #b42318;")
            self.log(message)
            return
        self.port.blockSignals(True)
        try:
            self.port.clear()
            for port in ports:
                self.port.addItem(port.device)
                self.port.setItemData(self.port.count() - 1, port.description, Qt.ToolTipRole)
            if selected:
                self.port.setCurrentText(selected)
        finally:
            self.port.blockSignals(False)
        if self.worker is None and not self.connected:
            self.connection_status.setText(
                "未连接" if ports else "未检测到串口，请连接设备后刷新，也可手动输入设备名")
            self.connection_status.setStyleSheet("")

    def current_targets(self):
        return parse_targets(self.target_ids.text()) if self.multi_mode.isChecked() else [self.target_id.value()]

    def target_mode_changed(self, *_args):
        self._pending_slider_request = None
        self.target_id.setEnabled(self.single_mode.isChecked())
        self.target_ids.setEnabled(self.multi_mode.isChecked())
        self.update_actions()

    def target_selection_changed(self, *_args):
        self._pending_slider_request = None
        self.update_actions()

    def update_actions(self, *_args):
        enabled = self.connected and self.worker is not None
        try:
            targets = self.current_targets()
            waiting = [target for target in targets if target in self.session.pending]
            if waiting:
                enabled = False
                self.target_hint.setText("等待 ID " + ",".join(map(str, waiting)) + " 的回包")
            else:
                self.target_hint.setText("每个 ID 独立接收回包")
        except ValueError:
            enabled = False
            self.target_hint.setText("ID 列表格式不正确")
        for button in (self.open_button, self.close_button, self.move_button):
            button.setEnabled(enabled)

    def connect_serial(self):
        if self.worker is not None:
            return
        port = self.port.currentText().strip()
        try:
            if not port:
                raise ValueError("请选择主机串口，或输入 /dev/ttyACM0、COM3 等设备名")
            text = self.baudrate.currentText().strip()
            if not text.isascii() or not text.isdigit() or not 300 <= int(text) <= 3_000_000:
                raise ValueError("波特率须为 300～3000000 的整数")
            self.session.reply_timeout = self.reply_timeout.value()
            worker = self.worker_factory(port, baudrate=int(text))
        except (ValueError, OSError, RuntimeError) as error:
            self.connection_status.setText(str(error))
            self.connection_status.setStyleSheet("color: #b42318;")
            self.log(str(error))
            return
        self.last_serial_error = ""
        self.connected = False
        self.worker = worker
        worker.opened.connect(lambda text, w=worker: self.on_opened(w, text))
        worker.problem.connect(lambda text, w=worker: self.on_problem(w, text))
        worker.finished.connect(lambda w=worker: self.on_finished(w))
        for widget in (self.port, self.baudrate, self.reply_timeout, self.refresh_button, self.connect_button):
            widget.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.connection_status.setText(f"正在打开 {port}…")
        self.update_actions()
        worker.start()

    def on_opened(self, worker, text):
        if worker is not self.worker or worker.stop_event.is_set():
            return
        self.connected = True
        self.connection_status.setText(f"已连接：{text}")
        self.connection_status.setStyleSheet("color: #087f65;")
        self.log(text)
        self.update_actions()

    def on_problem(self, worker, text):
        if worker is not self.worker:
            return
        self.last_serial_error = text
        self._pending_slider_request = None
        self.connected = False
        self.connection_status.setText(text)
        self.connection_status.setStyleSheet("color: #b42318;")
        self.log(text)
        self.update_actions()

    def disconnect_serial(self):
        self._pending_slider_request = None
        if self.worker is None:
            return
        self.connected = False
        self.worker.request_stop()
        self.disconnect_button.setEnabled(False)
        self.connection_status.setText("正在关闭串口…")
        self.update_actions()

    def on_finished(self, worker):
        if worker is not self.worker:
            return
        self._pending_slider_request = None
        self.connected = False
        self.refresh_events(limit=worker.CAPACITY)
        had_pending = bool(self.session.pending)
        self.session.disconnect()
        if had_pending:
            self.set_command_status("串口已断开，尚未收到回包的请求结果未知", True)
        self.render_devices()
        self.connected = False
        self.worker = None
        worker.deleteLater()
        for widget in (self.port, self.baudrate, self.reply_timeout, self.refresh_button, self.connect_button):
            widget.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.connection_status.setText(self.last_serial_error or "已断开")
        self.connection_status.setStyleSheet("color: #b42318;" if self.last_serial_error else "color: #64748b;")
        self.update_actions()
        if self.closing:
            self.close()

    def send_motion(self, kind):
        self._pending_slider_request = None
        if not self.connected or self.worker is None:
            self.set_command_status("请先连接主机串口", True)
            return
        try:
            if kind in ("open", "close"):
                self.parameters["position"].value.setValue(255 if kind == "open" else 0)
            targets = self.current_targets()
            parameters = self.motion_parameters()
        except ValueError as error:
            self.set_command_status(str(error), True)
            return
        self._send_motion_parameters("move" if kind in ("open", "close") else kind, targets, parameters)

    def motion_parameters(self):
        return MotionParameters(**{name: widget.value.value() for name, widget in self.parameters.items()})

    def send_slider_motion(self):
        if not self.connected or self.worker is None:
            return
        try:
            targets = self.current_targets()
            parameters = self.motion_parameters()
        except ValueError as error:
            self._pending_slider_request = None
            self.set_command_status(str(error), True)
            return
        waiting = {device_id: self.session.pending[device_id].seq for device_id in targets
                   if device_id in self.session.pending}
        if waiting:
            self._pending_slider_request = (targets, parameters, waiting)
            self.set_command_status("已保留最新滑块参数，等待上一条指令回包后发送")
            return
        self._pending_slider_request = None
        self._send_motion_parameters("move", targets, parameters)

    def _send_motion_parameters(self, kind, targets, parameters):
        seq = self.session.next_seq()
        try:
            command = build_command(kind, targets, parameters, seq)
            self.session.register(command, time.time())
            accepted = self.worker.enqueue(command)
            if not accepted:
                self.session.cancel_unsent(seq)
                self.set_command_status("发送队列已满，本条命令未提交", True)
            else:
                self.last_command = command
                self.command_preview.setPlainText(encode_command(command).decode("ascii").rstrip("\n"))
                targets = command.get("ids", [command.get("id")])
                self.set_command_status(f"已排队：ID {','.join(map(str, targets))} · 序号 {seq} · 等待串口发送和设备回包")
        except (ValueError, OSError, RuntimeError) as error:
            self.session.cancel_unsent(seq)
            self.set_command_status(str(error), True)
            self.log(f"未发送：{error}")
        self.render_devices()
        self.update_actions()

    def _send_pending_slider_motion(self):
        request = self._pending_slider_request
        if request is None:
            return
        if not self.connected or self.worker is None:
            self._pending_slider_request = None
            return
        targets, parameters, waiting = request
        for device_id, seq in waiting.items():
            pending = self.session.pending.get(device_id)
            if pending is not None:
                if pending.seq != seq:
                    self._pending_slider_request = None
                    return
                continue
            record = self.session.devices.get(device_id)
            if record is None or record.result != "收到反馈" or record.feedback_seq != seq or record.error:
                self._pending_slider_request = None
                self.set_command_status("上一条指令失败或超时，本次滑块参数未发送；请确认后重新操作", True)
                return
        if any(device_id in self.session.pending for device_id in targets):
            return
        self._pending_slider_request = None
        self._send_motion_parameters("move", targets, parameters)

    def refresh_events(self, limit=200):
        changed = set()
        logs = []
        global_problem = False
        now = time.time()
        if self.worker is not None:
            events, counts = self.worker.drain(limit)
            self.count_label.setText(f"TX {counts['tx']} / RX {counts['rx']} / 无效回包 {counts['invalid']} / 显示丢弃 {counts['dropped']}")
            for event in events:
                kind = event.get("kind")
                command = event.get("command")
                if kind == "tx" and command:
                    self.session.on_tx(command, now)
                    changed.update(command.get("ids", [command.get("id")]))
                elif kind == "rx" and command:
                    changed.update(self.session.handle_reply(command, now))
                    if command.get("event") == "error":
                        if "id" not in command:
                            self._pending_slider_request = None
                        target = f"ID {command['id']}" if "id" in command else "桥接层"
                        self.set_command_status(f"{target} 返回错误：{command['error']}", True)
                        global_problem = global_problem or ("id" not in command or self.last_command is None
                                                            or command.get("seq") != self.last_command["seq"])
                text = event.get("text") or (json.dumps(command, ensure_ascii=True, separators=(",", ":")) if command else "")
                logs.append(datetime.now().strftime("%H:%M:%S.%f")[:-3] + f" [{kind.upper() if kind else '?'}] {text}")
        expired = self.session.expire(now)
        if expired:
            changed.update(expired)
            text = "ID " + ",".join(map(str, expired)) + " 未收到回包，设备可能已动作；请检查链路后再决定下一条指令"
            self.set_command_status(text, True)
            logs.append(datetime.now().strftime("%H:%M:%S ") + text)
        if logs:
            self.log_view.appendPlainText("\n".join(logs))
        if changed:
            self.render_devices(changed)
            if not global_problem:
                self.summarize_latest_command(changed)
        self._send_pending_slider_motion()
        self.update_actions()

    def summarize_latest_command(self, changed):
        if self.last_command is None:
            return
        ids = self.last_command.get("ids", [self.last_command.get("id")])
        if not set(ids).intersection(changed):
            return
        seq = self.last_command["seq"]
        waiting = [device_id for device_id in ids
                   if device_id in self.session.pending and self.session.pending[device_id].seq == seq]
        records = [self.session.devices[device_id] for device_id in ids
                   if device_id in self.session.devices and self.session.devices[device_id].request_seq == seq]
        received = [record.id for record in records
                    if record.result == "收到反馈" and record.feedback_seq == seq]
        failures = [f"ID {record.id} {record.result}" for record in records if record.error]
        stalled = [record.id for record in records
                   if record.id in received and record.state == 2]
        if waiting:
            text = f"序号 {seq}：已收到 {len(received)}/{len(ids)} 个反馈，等待 ID {','.join(map(str, waiting))}"
            if failures:
                text += "；" + "；".join(failures)
            if stalled:
                text += f"；ID {','.join(map(str, stalled))} 反馈堵转 · 目标力矩 {self.last_command['force']}"
            self.set_command_status(text, bool(failures or stalled))
        elif len(received) == len(ids):
            if stalled:
                self.set_command_status(
                    f"ID {','.join(map(str, stalled))} 反馈堵转 · 目标力矩 {self.last_command['force']}"
                    "；请检查阻挡或调整力矩后再试 · 状态为设备快照", True)
            else:
                self.set_command_status(f"已收到 ID {','.join(map(str, received))} 的反馈 · 序号 {seq} · 状态为设备快照")
        elif failures:
            self.set_command_status("；".join(failures), True)

    def render_devices(self, changed=None):
        ids = list(self.session.devices)
        rebuild = ids != self.table_ids
        if rebuild:
            self.table_ids = ids
            self.table.setRowCount(len(ids))
        for row, device_id in enumerate(ids):
            if not rebuild and changed is not None and device_id not in changed:
                continue
            device = self.session.devices[device_id]
            number = lambda value: "—" if value is None else str(value)
            state = "—" if device.state is None else f"{device.state} {STATE_LABELS.get(device.state, '未知状态')}"
            fault = "—" if device.fault is None else f"{device.fault} {FAULT_LABELS.get(device.fault, '未知故障')}"
            updated = "—" if device.updated_at is None else datetime.fromtimestamp(device.updated_at).strftime("%H:%M:%S.%f")[:-3]
            values = (str(device_id), device.result, number(device.position), number(device.speed), number(device.force),
                      state, fault, f"{number(device.request_seq)} / {number(device.feedback_seq)}", updated)
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(value)
                item.setToolTip(value)
                item.setTextAlignment(Qt.AlignVCenter | (Qt.AlignLeft if column in (1, 5, 6) else Qt.AlignHCenter))
                item.setForeground(QColor("#b42318" if (column == 6 and device.fault)
                                          or (column == 5 and device.state == 2)
                                          or (column == 1 and device.error) else "#26364d"))

    def save_log(self):
        filename, _ = QFileDialog.getSaveFileName(self, "保存当前日志（最多 1000 行）", "omnipicker_log.txt", "Text (*.txt)")
        if not filename:
            return
        try:
            Path(filename).write_text(self.log_view.toPlainText() + "\n", encoding="utf-8")
            self.log(f"日志已保存：{filename}")
        except OSError as error:
            self.set_command_status(f"保存日志失败：{error}", True)

    def closeEvent(self, event):
        if self.worker is not None:
            self.closing = True
            self.disconnect_serial()
            event.ignore()
            return
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="通过已配置好的主机串口透传控制 OmniPicker")
    parser.parse_args()
    application = QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()
    return application.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
