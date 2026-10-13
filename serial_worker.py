"""Single-owner serial I/O thread for the standalone OmniPicker desktop tool.

No motion command is generated on open, close, or reconnect. A TX event means
all JSON bytes (including LF) reached pyserial; it is not device completion.
"""
from __future__ import annotations

from collections import deque
import errno
import json
import os
import sys
import threading
import time
from typing import Any, Callable

from PyQt5.QtCore import QThread, pyqtSignal
try:
    import serial
except ImportError:
    serial = None

try:
    from .protocol import LineDecoder, encode_command
except ImportError:
    from protocol import LineDecoder, encode_command


TX_QUEUE_LIMIT = 64
EVENT_QUEUE_LIMIT = 2048
READ_LIMIT = 4096
WRITE_DEADLINE_SECONDS = 0.5


def require_serial_api() -> None:
    required = ("Serial", "SerialException", "SerialTimeoutException",
                "EIGHTBITS", "PARITY_NONE", "STOPBITS_ONE")
    missing = [name for name in required if not hasattr(serial, name)]
    if missing or not callable(getattr(serial, "Serial", None)):
        source = getattr(serial, "__file__", None) or "未安装"
        reason = ("当前 Python 未安装 pyserial 串口模块" if serial is None else
                  "当前 Python 的 serial 不是完整的 pyserial 串口模块，可能被同名 serial 包覆盖")
        raise RuntimeError(
            f"{reason}。Python：{sys.executable}。模块路径：{source}。"
            "请使用本工具 run.sh 选择正确环境，"
            "或在独立虚拟环境安装 requirements.txt；需要的是 pyserial，不是 serial。")


def _serial_problem(port: str, exc: Exception) -> str:
    """Describe common port failures without confusing ownership/permissions."""
    error_number = getattr(exc, "errno", None)
    if error_number is None and exc.__cause__ is not None:
        error_number = getattr(exc.__cause__, "errno", None)
    reason = str(exc)
    lower = reason.lower()
    if error_number in (errno.EBUSY, errno.EAGAIN) or any(
            token in lower for token in ("exclusively lock", "resource busy", "port is already open")):
        hint = "串口被其他程序占用，请关闭串口监视器或其他上位机后重试"
    elif error_number in (errno.EACCES, errno.EPERM) or "permission denied" in lower or "access is denied" in lower:
        hint = "没有串口访问权限，请检查串口权限或其他程序是否占用"
    elif error_number in (errno.ENOENT, errno.ENODEV) or any(
            token in lower for token in ("no such file", "could not find", "device not configured")):
        hint = "串口不存在或设备已拔出，请刷新串口并重新选择"
    else:
        hint = "串口打开或收发失败，请检查设备连接"
    return f"{port}：{hint}（{reason}）"


class SerialWorker(QThread):
    opened = pyqtSignal(str)
    problem = pyqtSignal(str)
    CAPACITY = EVENT_QUEUE_LIMIT

    def __init__(self, port: str, baudrate: int = 115200,
                 serial_factory: Callable[..., Any] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        if not isinstance(port, str) or not port.strip():
            raise ValueError("请选择串口")
        if type(baudrate) is not int or not 1 <= baudrate <= 4_000_000:
            raise ValueError("波特率必须是 1～4000000 的整数")
        require_serial_api()
        self.port = port
        self.baudrate = baudrate
        self._factory = serial_factory or serial.Serial
        self._stop_requested = threading.Event()
        self.stop_event = self._stop_requested
        self._lock = threading.Lock()
        self._commands: deque[tuple[dict[str, Any], bytes]] = deque()
        self._events: deque[dict[str, Any]] = deque()
        self._ready = False
        self._counts = {"tx": 0, "rx": 0, "invalid": 0, "dropped": 0, "cancelled": 0}
        self._reported_dropped = 0

    def _publish_locked(self, event: dict[str, Any]) -> None:
        if len(self._events) >= EVENT_QUEUE_LIMIT:
            self._events.popleft()
            self._counts["dropped"] += 1
        self._events.append(event)

    def _warning_locked(self, text: str) -> None:
        self._publish_locked({"kind": "warning", "text": text})

    def enqueue(self, command: dict[str, Any]) -> bool:
        """Validate and copy a request, returning False when disconnected/full.

        Invalid protocol input raises ValueError in the caller. No serial API is
        used here, so this method is safe for the GUI thread.
        """
        payload = encode_command(command)
        # Snapshot mutable target lists as well as the outer dictionary.
        snapshot = json.loads(payload)
        with self._lock:
            if not self._ready or self._stop_requested.is_set():
                self._warning_locked("串口尚未连接或正在断开，命令未发送")
                return False
            if len(self._commands) >= TX_QUEUE_LIMIT:
                self._warning_locked("发送队列已满，命令未入队；请等待回包后再发送")
                return False
            self._commands.append((snapshot, payload))
        return True

    def request_stop(self) -> None:
        """Request stop and cancel unsent commands without touching serial I/O."""
        self._stop_requested.set()
        with self._lock:
            self._ready = False
            self._cancel_commands_locked()

    def _cancel_commands_locked(self) -> None:
        count = len(self._commands)
        self._commands.clear()
        if count:
            self._counts["cancelled"] += count
            self._warning_locked(f"连接结束，已取消 {count} 条尚未发送的命令")

    def drain(self, limit: int = 200) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Drain bounded GUI events and snapshot cumulative session counters."""
        if type(limit) is not int or limit < 0:
            raise ValueError("drain limit 必须是非负整数")
        with self._lock:
            events: list[dict[str, Any]] = []
            if limit and self._counts["dropped"] != self._reported_dropped:
                self._reported_dropped = self._counts["dropped"]
                events.append({"kind": "warning", "text": (
                    f"显示队列溢出，累计丢弃 {self._reported_dropped} 条显示事件；"
                    "请以最新回包为准")})
            for _ in range(min(limit - len(events), len(self._events))):
                events.append(self._events.popleft())
            counts = dict(self._counts)
            counts["pending"] = len(self._commands)
        return events, counts

    def _write_complete(self, connection, payload: bytes) -> bool:
        offset = 0
        deadline = time.monotonic() + WRITE_DEADLINE_SECONDS
        while offset < len(payload):
            if self._stop_requested.is_set():
                if offset == 0:
                    # Stop won the race before any bytes were sent. This is an
                    # ordinary disconnect, not a serial failure.
                    with self._lock:
                        self._counts["cancelled"] += 1
                    return False
                raise serial.SerialException(
                    f"发送期间断开，JSON 未完整写入（{offset}/{len(payload)} 字节）；"
                    "请确认设备状态后手动重发")
            if time.monotonic() >= deadline:
                raise serial.SerialTimeoutException(
                    f"发送超时，JSON 未完整写入（{offset}/{len(payload)} 字节）；"
                    "请确认设备状态后手动重发")
            try:
                written = connection.write(payload[offset:])
            except Exception as exc:
                raise serial.SerialException(
                    f"发送未完整确认（{offset}/{len(payload)} 字节已确认）；"
                    f"请确认设备状态后手动重发（{exc}）") from exc
            if type(written) is not int or not 0 < written <= len(payload) - offset:
                raise serial.SerialTimeoutException(
                    f"发送未完成（{offset}/{len(payload)} 字节）；"
                    "请确认设备状态后手动重发")
            offset += written
        return True

    def run(self) -> None:
        connection = None
        decoder = LineDecoder()
        try:
            if self._stop_requested.is_set():
                return
            settings = {
                "port": None, "baudrate": self.baudrate,
                "bytesize": serial.EIGHTBITS, "parity": serial.PARITY_NONE,
                "stopbits": serial.STOPBITS_ONE, "timeout": 0.03,
                "write_timeout": 0.3, "xonxoff": False,
                "rtscts": False, "dsrdtr": False,
            }
            if os.name == "posix":
                settings["exclusive"] = True
            connection = self._factory(**settings)
            connection.port = self.port
            connection.dtr = False
            connection.rts = False
            if self._stop_requested.is_set():
                return
            connection.open()
            with self._lock:
                if self._stop_requested.is_set():
                    return
                self._ready = True
            self.opened.emit(self.port)

            while not self._stop_requested.is_set():
                with self._lock:
                    item = self._commands.popleft() if self._commands else None
                if item is not None:
                    command, payload = item
                    if not self._write_complete(connection, payload):
                        break
                    with self._lock:
                        self._counts["tx"] += 1
                        self._publish_locked({"kind": "tx", "command": command,
                                              "text": payload[:-1].decode("ascii")})
                if self._stop_requested.is_set():
                    break
                waiting = connection.in_waiting
                chunk = connection.read(min(READ_LIMIT, max(1, waiting)))
                if chunk:
                    replies = decoder.feed(chunk)
                    with self._lock:
                        invalid_added = decoder.invalid_lines - self._counts["invalid"]
                        self._counts["invalid"] = decoder.invalid_lines
                        if invalid_added:
                            self._warning_locked(
                                f"已忽略 {invalid_added} 行非法、超长或非 OmniPicker 回包"
                                f"（累计 {decoder.invalid_lines} 行）")
                        for reply in replies:
                            self._counts["rx"] += 1
                            self._publish_locked({"kind": "rx", "command": reply,
                                                  "text": json.dumps(reply, ensure_ascii=True,
                                                                     separators=(",", ":"))})
        except Exception as exc:
            # A write/read failure always ends this session. There is no retry
            # and queued movements cannot migrate into the next connection.
            self.problem.emit(_serial_problem(self.port, exc))
        finally:
            with self._lock:
                self._ready = False
                self._cancel_commands_locked()
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    self.problem.emit(f"{self.port}：串口关闭失败（{exc}）")
