"""OmniPicker NDJSON helpers matching the bridge's existing protocol.

The desktop tool never sends raw CAN or an all-ID broadcast. One request has
either one ``id`` or up to 16 explicit ``ids``; replies are per target.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


MAX_LINE_BYTES = 384
MAX_TARGETS = 16
UINT32_MAX = 0xFFFFFFFF
PARAMETER_NAMES = ("position", "speed", "force", "acceleration", "deceleration")


@dataclass(frozen=True)
class MotionParameters:
    position: int = 128
    speed: int = 255
    force: int = 255
    acceleration: int = 255
    deceleration: int = 255


def _integer(value: Any, maximum: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} 必须是 0～{maximum} 的整数")
    return value


def _targets(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_TARGETS:
        raise ValueError("目标列表必须包含 1～16 个 ID")
    result = [_integer(value, 2047, "ID") for value in values]
    if len(result) != len(set(result)):
        raise ValueError("目标列表中不能有重复 ID")
    return result


_ID_TOKEN = r"(?:0[xX][0-9a-fA-F]+|[0-9]+)"
_ID_LIST = re.compile(rf"{_ID_TOKEN}(?:(?:\s*,\s*|\s+){_ID_TOKEN})*")


def parse_targets(text: str) -> list[int]:
    """Parse decimal/0x IDs separated by commas or whitespace, without dedupe."""
    if not isinstance(text, str):
        raise ValueError("请输入目标 ID")
    text = text.strip()
    if not text or _ID_LIST.fullmatch(text) is None:
        raise ValueError("ID 请用逗号或空格分隔，例如 1,2；也支持 0x01")
    tokens = re.split(r"[,\s]+", text)
    values = [int(token, 16 if token.lower().startswith("0x") else 10)
              for token in tokens]
    return _targets(values)


def build_command(cmd: str, targets: list[int], params: MotionParameters,
                  seq: int) -> dict[str, Any]:
    """Build a validated request. open/close deliberately omit position."""
    if cmd not in ("move", "open", "close"):
        raise ValueError("命令必须是 move、open 或 close")
    ids = _targets(targets)
    if not isinstance(params, MotionParameters):
        raise ValueError("运动参数类型不正确")
    command: dict[str, Any] = {"type": "omnipicker"}
    if len(ids) == 1:
        command["id"] = ids[0]
    else:
        command["ids"] = ids
    command["cmd"] = cmd
    for name in PARAMETER_NAMES:
        value = _integer(getattr(params, name), 255, name)
        if cmd == "move" or name != "position":
            command[name] = value
    command["seq"] = _integer(seq, UINT32_MAX, "seq")
    return command


def validate_command(command: Any) -> None:
    """Reject manual mutations that would make the bridge reject a request."""
    if not isinstance(command, dict):
        raise ValueError("命令必须是 JSON 对象")
    allowed = {"type", "id", "ids", "cmd", "seq", *PARAMETER_NAMES}
    if set(command) - allowed or command.get("type") != "omnipicker":
        raise ValueError("仅支持 OmniPicker 协议字段")
    if ("id" in command) == ("ids" in command):
        raise ValueError("id 和 ids 必须二选一")
    if "id" in command:
        _integer(command["id"], 2047, "id")
    else:
        # JSON arrays are lists. Tuples are only accepted by build_command.
        if not isinstance(command["ids"], list):
            raise ValueError("ids 必须是数组")
        _targets(command["ids"])
    cmd = command.get("cmd")
    if cmd not in ("move", "open", "close"):
        raise ValueError("命令必须是 move、open 或 close")
    if cmd != "move" and "position" in command:
        raise ValueError("open/close 不允许 position 字段")
    for name in PARAMETER_NAMES:
        if name in command:
            _integer(command[name], 255, name)
        elif cmd == "move":
            raise ValueError(f"move 缺少 {name}")
    if "seq" in command:
        _integer(command["seq"], UINT32_MAX, "seq")


def encode_command(command: dict[str, Any]) -> bytes:
    """Return compact ASCII JSON followed by an actual LF, never literal \\n."""
    validate_command(command)
    payload = json.dumps(command, ensure_ascii=True, separators=(",", ":"),
                         allow_nan=False).encode("ascii")
    if len(payload) > MAX_LINE_BYTES:
        raise ValueError("JSON 正文不能超过 384 字节")
    return payload + b"\n"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 重复字段")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"非法 JSON 常量 {value}")


def validate_reply(reply: Any) -> None:
    """Validate only the existing OmniPicker status/error reply schemas."""
    if not isinstance(reply, dict) or reply.get("type") != "omnipicker":
        raise ValueError("不是 OmniPicker 回包")
    event = reply.get("event")
    common = {"type", "id", "seq", "event"}
    if event == "status":
        required = {"id", "fault", "state", "position", "speed", "force"}
        if not required <= set(reply) or set(reply) - (common | required):
            raise ValueError("状态回包字段不完整或包含未知字段")
        for name in ("fault", "state", "position", "speed", "force"):
            _integer(reply[name], 255, name)
    elif event == "error":
        if set(reply) - (common | {"error", "rejected"}):
            raise ValueError("错误回包包含未知字段")
        error = reply.get("error")
        if not isinstance(error, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", error):
            raise ValueError("错误回包缺少有效 error")
        if "rejected" in reply:
            _integer(reply["rejected"], UINT32_MAX, "rejected")
    else:
        raise ValueError("不支持的回包 event")
    if "id" in reply:
        _integer(reply["id"], 2047, "id")
    if "seq" in reply:
        _integer(reply["seq"], UINT32_MAX, "seq")


class LineDecoder:
    """Bounded LF/CRLF decoder that resynchronizes after a bad/overlong line.

    ``invalid_lines`` includes overlong and malformed/unsupported replies.
    ``discarded_bytes`` measures invalid line bytes, excluding delimiters.
    Opening a new connection should create a new decoder, discarding half-lines.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._discard = False
        self.invalid_lines = 0
        self.overlong_lines = 0
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        replies: list[dict[str, Any]] = []
        for value in data:
            if value == 10:
                if not self._discard:
                    line = bytes(self._buffer)
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    try:
                        if len(line) > MAX_LINE_BYTES:
                            raise ValueError("回包过长")
                        reply = json.loads(line.decode("ascii"),
                                           object_pairs_hook=_unique_object,
                                           parse_constant=_reject_constant)
                        validate_reply(reply)
                    except (ValueError, TypeError, RecursionError, UnicodeError):
                        self.invalid_lines += 1
                        self.discarded_bytes += len(self._buffer)
                    else:
                        replies.append(reply)
                self._buffer.clear()
                self._discard = False
            elif self._discard:
                self.discarded_bytes += 1
            else:
                self._buffer.append(value)
                # Permit CR in CRLF in addition to the 384-byte JSON body.
                if len(self._buffer) > MAX_LINE_BYTES and not (
                        len(self._buffer) == MAX_LINE_BYTES + 1 and value == 13):
                    self.invalid_lines += 1
                    self.overlong_lines += 1
                    self.discarded_bytes += len(self._buffer)
                    self._buffer.clear()
                    self._discard = True
        return replies
