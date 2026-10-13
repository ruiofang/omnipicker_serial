"""Bounded, transport-independent tracking of OmniPicker JSON requests.

The bridge replies with a status snapshot, not a movement-completed event.
Request sequence numbers live in the bridge and must not be used as proof that
the physical CAN device can distinguish a delayed response from an old command.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any


UINT32_MAX = (1 << 32) - 1
MAX_COMMAND_IDS = 16


@dataclass
class DeviceState:
    id: int
    result: str = "暂无反馈"
    position: int | None = None
    speed: int | None = None
    force: int | None = None
    state: int | None = None
    fault: int | None = None
    request_seq: int | None = None
    feedback_seq: int | None = None
    # Timestamp of a received device reply, never a fabricated completion time.
    updated_at: float | None = None
    error: str | None = None


@dataclass
class PendingRequest:
    id: int
    seq: int
    deadline: float
    command: dict[str, Any]
    queued_at: float
    transmitted_at: float | None = None


def _integer(value: Any, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _targets(command: dict[str, Any]) -> list[int]:
    """Validate the target/sequence envelope without rewriting the command."""
    if not isinstance(command, dict) or command.get("type") != "omnipicker":
        raise ValueError("请求类型必须为 omnipicker")
    if ("id" in command) == ("ids" in command):
        raise ValueError("请求必须且只能指定 id 或 ids")
    ids = [command["id"]] if "id" in command else command["ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_COMMAND_IDS:
        raise ValueError("目标 ID 数量必须为 1～16 个")
    if any(not _integer(device_id, 2047) for device_id in ids):
        raise ValueError("夹爪 ID 必须是 0～2047 的整数")
    if len(set(ids)) != len(ids):
        raise ValueError("目标 ID 不可重复")
    if not _integer(command.get("seq"), UINT32_MAX):
        raise ValueError("请求 seq 必须是 uint32 整数")
    return list(ids)


class ControlSession:
    """Track one in-flight command per ID; never retry or issue CAN commands.

    ``now`` arguments use the caller's single clock (the UI passes time.time()).
    ``updated_at`` records received replies only, so a timeout or a new command
    does not make an older feedback snapshot look fresh. Device insertion order
    determines eviction; a device with an outstanding request cannot be evicted.
    """

    def __init__(self, reply_timeout: float = 2.0, max_devices: int = 128):
        if isinstance(reply_timeout, bool) or not isinstance(reply_timeout, (int, float)):
            raise ValueError("回执等待时间必须为正数")
        if not math.isfinite(reply_timeout) or reply_timeout <= 0:
            raise ValueError("回执等待时间必须为有限正数")
        if type(max_devices) is not int or max_devices < 1:
            raise ValueError("设备表容量必须为正整数")
        self.reply_timeout = float(reply_timeout)
        self.max_devices = max_devices
        self.devices: dict[int, DeviceState] = {}
        self.pending: dict[int, PendingRequest] = {}
        self._last_seq = 0

    def ensure_device(self, device_id: int) -> DeviceState:
        if not _integer(device_id, 2047):
            raise ValueError("夹爪 ID 必须是 0～2047 的整数")
        if device_id in self.devices:
            return self.devices[device_id]
        if len(self.devices) >= self.max_devices:
            victim = next((value for value in self.devices if value not in self.pending), None)
            if victim is None:
                raise ValueError("设备表已满，现有设备正在等待回执")
            del self.devices[victim]
        record = DeviceState(id=device_id)
        self.devices[device_id] = record
        return record

    def next_seq(self) -> int:
        occupied = {request.seq for request in self.pending.values()}
        while True:
            self._last_seq = (self._last_seq + 1) & UINT32_MAX
            if self._last_seq not in occupied:
                return self._last_seq

    def register(self, command: dict[str, Any], now: float) -> None:
        ids = _targets(command)
        busy_ids = [device_id for device_id in ids if device_id in self.pending]
        if busy_ids:
            raise ValueError("夹爪正在等待回执：" + ",".join(map(str, busy_ids)))

        # Plan every eviction before mutating anything: a multi-ID request is
        # either registered as a whole or rejected as a whole.
        missing = [device_id for device_id in ids if device_id not in self.devices]
        excess = max(0, len(self.devices) + len(missing) - self.max_devices)
        candidates = [device_id for device_id in self.devices
                      if device_id not in self.pending and device_id not in ids]
        if excess > len(candidates):
            raise ValueError("设备表容量不足，无法登记整条请求")
        snapshot = deepcopy(command)
        for victim in candidates[:excess]:
            del self.devices[victim]
        for device_id in ids:
            record = self.ensure_device(device_id)
            record.request_seq = command["seq"]
            record.result = "排队"
            record.error = None
            self.pending[device_id] = PendingRequest(
                id=device_id, seq=command["seq"],
                deadline=now + self.reply_timeout, command=deepcopy(snapshot),
                queued_at=now,
            )

    def on_tx(self, command: dict[str, Any], now: float) -> None:
        ids = _targets(command)
        for device_id in ids:
            request = self.pending.get(device_id)
            if request is None or request.seq != command["seq"]:
                continue
            request.transmitted_at = now
            request.deadline = now + self.reply_timeout
            self.devices[device_id].result = "等待反馈"

    def cancel_unsent(self, seq: int) -> None:
        """Cancel queue rejection without disturbing a frame already sent."""
        for device_id, request in list(self.pending.items()):
            if request.seq != seq or request.transmitted_at is not None:
                continue
            del self.pending[device_id]
            record = self.devices[device_id]
            record.result = "发送队列满"
            record.error = "send_queue_full"

    def handle_reply(self, reply: dict[str, Any], now: float) -> list[int]:
        if not isinstance(reply, dict) or reply.get("type") != "omnipicker":
            return []
        event = reply.get("event")
        if event not in ("status", "error") or not _integer(reply.get("id"), 2047):
            # A global error must not clear unrelated outstanding requests.
            return []
        device_id = reply["id"]
        seq = reply.get("seq")
        if "seq" in reply and not _integer(seq, UINT32_MAX):
            return []
        if event == "status":
            if any(not _integer(reply.get(field), 255)
                   for field in ("position", "speed", "force", "state", "fault")):
                return []
        elif not isinstance(reply.get("error"), str) or not reply["error"]:
            return []
        try:
            record = self.ensure_device(device_id)
        except ValueError:
            return []  # Unknown-device chatter may not evict a pending device.
        request = self.pending.get(device_id)
        matched = request is not None and seq is not None and request.seq == seq

        record.updated_at = now
        record.feedback_seq = seq
        if event == "status":
            for field in ("position", "speed", "force", "state", "fault"):
                setattr(record, field, reply[field])
            if matched:
                del self.pending[device_id]
                record.result = "收到反馈"
                record.error = None
            elif request is None:
                record.result = "未关联反馈"
                record.error = None
        elif matched:
            del self.pending[device_id]
            record.error = reply["error"]
            record.result = "错误：" + reply["error"]
        elif request is None:
            record.error = reply["error"]
            record.result = "未关联错误：" + reply["error"]
        # With a pending new request, an old/missing sequence may refresh the
        # displayed snapshot but cannot mark this new request as acknowledged.
        return [device_id]

    def expire(self, now: float) -> list[int]:
        expired = [device_id for device_id, request in self.pending.items()
                   if now >= request.deadline]
        for device_id in expired:
            del self.pending[device_id]
            record = self.devices[device_id]
            record.result = "未收到回执，可能已动作"
            record.error = "reply_timeout"
        return expired

    def disconnect(self) -> None:
        for device_id in self.pending:
            record = self.devices[device_id]
            record.result = "连接断开，结果未知"
            record.error = "disconnected"
        self.pending.clear()
