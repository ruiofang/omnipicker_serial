"""Session regressions without a GUI, a serial port, or physical motion."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_session import ControlSession, UINT32_MAX


def command(seq=1, *, device_id=1, ids=None):
    targets = {"id": device_id} if ids is None else {"ids": ids}
    return {"type": "omnipicker", **targets, "cmd": "move", "position": 0,
            "speed": 255, "force": 32, "acceleration": 255,
            "deceleration": 255, "seq": seq}


def status(device_id=1, seq=1, **overrides):
    reply = {"type": "omnipicker", "id": device_id, "seq": seq,
             "event": "status", "position": 0, "speed": 0, "force": 20,
             "state": 0, "fault": 0}
    reply.update(overrides)
    return reply


def error(device_id=1, seq=1, code="timeout"):
    return {"type": "omnipicker", "id": device_id, "seq": seq,
            "event": "error", "error": code}


class ControlSessionTests(unittest.TestCase):
    def test_queued_then_transmitted_refreshes_deadline(self):
        session = ControlSession(reply_timeout=2)
        request = command()
        session.register(request, 10)
        self.assertEqual(session.devices[1].result, "排队")
        self.assertEqual(session.pending[1].deadline, 12)
        self.assertIsNone(session.devices[1].updated_at)
        session.on_tx(request, 11)
        self.assertEqual(session.pending[1].deadline, 13)
        self.assertEqual(session.pending[1].transmitted_at, 11)
        self.assertEqual(session.devices[1].result, "等待反馈")
        self.assertEqual(session.expire(12), [])
        self.assertEqual(session.expire(13), [1])
        self.assertEqual(session.devices[1].error, "reply_timeout")

    def test_multi_id_out_of_order_and_partial_error(self):
        session = ControlSession()
        request = command(ids=[1, 2, 3])
        session.register(request, 1)
        session.on_tx(request, 1)
        self.assertEqual(session.handle_reply(status(2, position=255), 1.1), [2])
        self.assertEqual(list(session.pending), [1, 3])
        self.assertEqual(session.devices[2].result, "收到反馈")
        self.assertEqual(session.devices[2].position, 255)
        self.assertEqual(session.handle_reply(error(1, code="can_tx_failed"), 1.2), [1])
        self.assertEqual(list(session.pending), [3])
        self.assertEqual(session.devices[1].error, "can_tx_failed")
        session.handle_reply(status(3), 1.3)
        self.assertFalse(session.pending)
        self.assertEqual(list(session.devices), [1, 2, 3])

    def test_snapshot_does_not_claim_movement_completed(self):
        session = ControlSession()
        session.register(command(), 1)
        session.handle_reply(status(state=1, position=80, speed=100, force=190), 1.1)
        record = session.devices[1]
        self.assertEqual(record.result, "收到反馈")
        self.assertEqual((record.position, record.speed, record.force), (80, 100, 190))
        self.assertNotIn("完成", record.result)
        self.assertEqual(record.updated_at, 1.1)

    def test_unknown_state_and_fault_codes_are_preserved(self):
        session = ControlSession()
        session.register(command(), 1)
        session.handle_reply(status(state=200, fault=255), 1.1)
        self.assertEqual((session.devices[1].state, session.devices[1].fault), (200, 255))

    def test_busy_new_or_old_sequence_cannot_clear_waiting_request(self):
        session = ControlSession()
        session.register(command(20), 1)
        session.on_tx(command(20), 1)
        session.handle_reply(error(seq=19, code="busy"), 1.1)
        session.handle_reply(error(seq=21, code="busy"), 1.2)
        self.assertEqual(session.pending[1].seq, 20)
        self.assertEqual(session.devices[1].result, "等待反馈")
        self.assertIsNone(session.devices[1].error)
        session.handle_reply(error(seq=20, code="busy"), 1.3)
        self.assertFalse(session.pending)
        self.assertEqual(session.devices[1].error, "busy")

    def test_missing_or_unknown_seq_updates_snapshot_without_acknowledging(self):
        session = ControlSession()
        session.register(command(10), 1)
        session.on_tx(command(10), 1)
        uncorrelated = status(seq=9, position=80)
        session.handle_reply(uncorrelated, 1.1)
        uncorrelated.pop("seq")
        uncorrelated["position"] = 90
        session.handle_reply(uncorrelated, 1.2)
        self.assertEqual(session.pending[1].seq, 10)
        self.assertEqual(session.devices[1].result, "等待反馈")
        self.assertEqual(session.devices[1].position, 90)
        self.assertIsNone(session.devices[1].feedback_seq)
        self.assertEqual(session.devices[1].request_seq, 10)

    def test_unsolicited_reply_is_not_a_current_ack(self):
        session = ControlSession()
        session.handle_reply(status(seq=5), 1)
        self.assertEqual(session.devices[1].result, "未关联反馈")
        self.assertIsNone(session.devices[1].request_seq)
        session.handle_reply(error(2, 5, "busy"), 2)
        self.assertEqual(session.devices[2].result, "未关联错误：busy")

    def test_missing_seq_error_does_not_clear_pending(self):
        session = ControlSession()
        session.register(command(), 1)
        reply = error()
        del reply["seq"]
        session.handle_reply(reply, 1.1)
        self.assertIn(1, session.pending)
        self.assertEqual(session.devices[1].result, "排队")

    def test_global_error_never_clears_pending_or_creates_device(self):
        session = ControlSession()
        session.register(command(ids=[1, 2]), 1)
        self.assertEqual(session.handle_reply(
            {"type": "omnipicker", "event": "error", "error": "invalid_json"}, 1.1), [])
        self.assertEqual(session.handle_reply(
            {"type": "omnipicker", "event": "error", "error": "response_overflow",
             "rejected": 3}, 1.2), [])
        self.assertEqual(list(session.pending), [1, 2])
        self.assertEqual(list(session.devices), [1, 2])

    def test_atomic_rejection_when_any_target_is_busy(self):
        session = ControlSession()
        session.register(command(1), 1)
        with self.assertRaises(ValueError):
            session.register(command(2, ids=[2, 1]), 1.1)
        self.assertEqual(list(session.devices), [1])
        self.assertEqual(list(session.pending), [1])
        self.assertEqual(session.pending[1].seq, 1)

    def test_capacity_rejection_is_atomic_and_preserves_targets(self):
        session = ControlSession(max_devices=2)
        session.register(command(1), 1)
        existing = session.ensure_device(2)
        with self.assertRaises(ValueError):
            session.register(command(2, ids=[2, 3]), 1.1)
        self.assertEqual(list(session.devices), [1, 2])
        self.assertIs(session.devices[2], existing)
        self.assertEqual(existing.result, "暂无反馈")
        self.assertEqual(list(session.pending), [1])

    def test_oldest_nonpending_eviction_without_touching_waiting_device(self):
        session = ControlSession(max_devices=3)
        session.ensure_device(1)
        session.ensure_device(2)
        session.ensure_device(3)
        session.register(command(device_id=1), 1)
        session.ensure_device(4)
        self.assertEqual(list(session.devices), [1, 3, 4])
        self.assertIn(1, session.pending)
        session.register(command(2, ids=[3, 5]), 1.1)
        self.assertEqual(list(session.devices), [1, 3, 5])
        self.assertEqual(list(session.pending), [1, 3, 5])
        with self.assertRaises(ValueError):
            session.ensure_device(6)
        self.assertEqual(session.handle_reply(status(6), 1.2), [])
        self.assertEqual(list(session.devices), [1, 3, 5])

    def test_expire_only_overdue_targets_and_never_refreshes_feedback_age(self):
        session = ControlSession(reply_timeout=2)
        session.handle_reply(status(), 0)
        session.register(command(2), 1)
        session.register(command(3, device_id=2), 2)
        self.assertEqual(session.expire(2.99), [])
        self.assertEqual(session.expire(3), [1])
        self.assertEqual(list(session.pending), [2])
        self.assertEqual(session.devices[1].result, "未收到回执，可能已动作")
        self.assertEqual(session.devices[1].updated_at, 0)
        self.assertEqual(session.expire(3), [])

    def test_disconnect_and_reconnect_do_not_replay_or_accept_old_reply(self):
        session = ControlSession()
        session.register(command(5, ids=[1, 2]), 1)
        session.disconnect()
        self.assertFalse(session.pending)
        self.assertEqual(session.devices[1].result, "连接断开，结果未知")
        self.assertEqual(session.devices[2].error, "disconnected")
        session.register(command(6), 2)
        session.on_tx(command(6), 2)
        session.handle_reply(status(seq=5), 2.1)
        self.assertEqual(session.pending[1].seq, 6)
        self.assertEqual(session.devices[1].result, "等待反馈")
        session.handle_reply(status(seq=6), 2.2)
        self.assertFalse(session.pending)

    def test_cancel_only_matching_unsent_sequence(self):
        session = ControlSession()
        session.register(command(1, ids=[1, 2]), 1)
        session.register(command(2, device_id=3), 1)
        session.on_tx(command(1, ids=[1, 2]), 1.1)
        session.cancel_unsent(1)
        self.assertEqual(list(session.pending), [1, 2, 3])
        session.cancel_unsent(2)
        self.assertEqual(list(session.pending), [1, 2])
        self.assertEqual(session.devices[3].result, "发送队列满")
        self.assertEqual(session.devices[3].error, "send_queue_full")

    def test_cancel_multi_id_queue_rejection_as_a_group(self):
        session = ControlSession()
        session.register(command(1, ids=[1, 2]), 1)
        session.cancel_unsent(1)
        self.assertFalse(session.pending)
        self.assertEqual(session.devices[1].result, "发送队列满")
        self.assertEqual(session.devices[2].result, "发送队列满")

    def test_stale_tx_callback_does_not_refresh_new_request(self):
        session = ControlSession()
        session.register(command(5), 1)
        session.on_tx(command(4), 10)
        self.assertEqual(session.pending[1].deadline, 3)
        self.assertEqual(session.devices[1].result, "排队")

    def test_sequence_wrap_skips_all_active_request_sequences(self):
        session = ControlSession()
        session.register(command(UINT32_MAX), 1)
        session.register(command(0, device_id=2), 1)
        session._last_seq = UINT32_MAX - 1
        self.assertEqual(session.next_seq(), 1)
        self.assertEqual(session.next_seq(), 2)

    def test_request_snapshot_not_changed_by_caller_mutation(self):
        session = ControlSession()
        request = command(ids=[1, 2])
        session.register(request, 1)
        request["ids"].append(3)
        request["position"] = 255
        self.assertEqual(session.pending[1].command["ids"], [1, 2])
        self.assertEqual(session.pending[2].command["position"], 0)

    def test_invalid_envelope_rejected_without_mutation(self):
        for invalid in (
            command(ids=[]), command(ids=[1, 1]), command(ids=[True]),
            command(ids=list(range(17))), command(device_id=-1),
            command(device_id=2048), command(seq=True), command(seq=-1),
            command(seq=UINT32_MAX + 1), {**command(), "ids": [1]},
            {**command(), "type": "can"},
        ):
            with self.subTest(invalid=invalid):
                session = ControlSession()
                with self.assertRaises(ValueError):
                    session.register(invalid, 1)
                self.assertFalse(session.devices)
                self.assertFalse(session.pending)

    def test_invalid_reply_does_not_change_request_or_snapshot(self):
        session = ControlSession()
        session.register(command(), 1)
        for invalid in (
            status(position=True), status(speed=-1), status(force=256),
            status(seq=True), status(device_id=2048), status(event="tx_queued"),
            status(type="can"), {**error(), "error": 42},
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(session.handle_reply(invalid, 1.1), [])
        self.assertIn(1, session.pending)
        self.assertIsNone(session.devices[1].position)
        self.assertIsNone(session.devices[1].updated_at)

    def test_configuration_validation(self):
        for value in (0, -1, float("nan"), float("inf"), True, "2"):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                ControlSession(reply_timeout=value)
        for value in (0, -1, True, 1.5):
            with self.subTest(capacity=value), self.assertRaises(ValueError):
                ControlSession(max_devices=value)


if __name__ == "__main__":
    unittest.main()
