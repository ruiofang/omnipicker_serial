"""Mock and real local-PTY serial lifecycle checks, with no USB/gripper access."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import queue
import select
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtCore import QCoreApplication
import serial

from protocol import MotionParameters, build_command, encode_command
from serial_worker import EVENT_QUEUE_LIMIT, SerialWorker, TX_QUEUE_LIMIT, _serial_problem


def reply_line(target=1, seq=1, ending=b"\n"):
    return json.dumps({"type": "omnipicker", "id": target, "seq": seq,
                       "event": "status", "fault": 0, "state": 0,
                       "position": 255, "speed": 0, "force": 7},
                      separators=(",", ":")).encode("ascii") + ending


class FakeSerial:
    def __init__(self, **kwargs):
        self.settings = kwargs
        self.port = None
        self.dtr = None
        self.rts = None
        self.is_open = False
        self.closed = False
        self.bytes_written = bytearray()
        self.rx = queue.Queue()
        self.pending_rx = bytearray()
        self.thread_ids = [threading.get_ident()]
        self.open_gate = None
        self.read_gate = None
        self.write_actions = []
        self.open_error = None

    def _record_thread(self):
        self.thread_ids.append(threading.get_ident())

    def open(self):
        self._record_thread()
        if self.open_gate:
            self.open_gate()
        if self.open_error:
            raise self.open_error
        self.is_open = True

    @property
    def in_waiting(self):
        self._record_thread()
        while True:
            try:
                self.pending_rx.extend(self.rx.get_nowait())
            except queue.Empty:
                break
        return len(self.pending_rx)

    def read(self, size):
        self._record_thread()
        if self.read_gate:
            gate = self.read_gate
            self.read_gate = None
            gate()
        if not self.pending_rx:
            time.sleep(0.002)
        data = bytes(self.pending_rx[:size])
        del self.pending_rx[:size]
        return data

    def write(self, data):
        self._record_thread()
        action = self.write_actions.pop(0) if self.write_actions else len(data)
        if isinstance(action, Exception):
            raise action
        size = min(action, len(data))
        self.bytes_written.extend(data[:size])
        return size

    def close(self):
        self._record_thread()
        self.closed = True
        self.is_open = False


class WorkerTests(unittest.TestCase):
    def test_wrong_serial_package_reports_dependency_before_open(self):
        conflicting_module = SimpleNamespace(__file__="/conda/site-packages/serial/__init__.py")
        with patch("serial_worker.serial", conflicting_module):
            with self.assertRaisesRegex(RuntimeError, "pyserial") as raised:
                SerialWorker("/dev/ttyACM0")
        self.assertIn(conflicting_module.__file__, str(raised.exception))

    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication(["omnipicker-worker-test"])

    def setUp(self):
        self.workers = []
        self.gates = []
        self.messages = []
        self.opened = []
        self.fake = None

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for worker in self.workers:
            worker.request_stop()
        for worker in self.workers:
            self.assertTrue(worker.wait(2000), "worker failed to finish")
        self.app.processEvents()

    def pump_until(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()
        self.assertTrue(condition(), "worker did not reach expected state")

    def gate(self):
        entered, released = threading.Event(), threading.Event()
        self.gates.append(released)

        def wait():
            entered.set()
            if not released.wait(3):
                raise RuntimeError("test gate timeout")
        return entered, released, wait

    def make_worker(self, *, configure=None, port="test-port", factory=None):
        def fake_factory(**settings):
            self.fake = FakeSerial(**settings)
            if configure:
                configure(self.fake)
            return self.fake

        worker = SerialWorker(port, serial_factory=factory or fake_factory)
        worker.problem.connect(self.messages.append)
        worker.opened.connect(self.opened.append)
        self.workers.append(worker)
        return worker

    def start_worker(self, **kwargs):
        worker = self.make_worker(**kwargs)
        worker.start()
        self.pump_until(lambda: bool(self.opened))
        return worker

    def test_open_close_no_commands_and_worker_owned_io(self):
        gui_thread = threading.get_ident()
        worker = self.start_worker()
        self.assertEqual(bytes(self.fake.bytes_written), b"")
        self.assertFalse(self.fake.dtr)
        self.assertFalse(self.fake.rts)
        self.assertEqual(self.fake.settings["timeout"], 0.03)
        self.assertLessEqual(self.fake.settings["write_timeout"], 0.5)
        self.assertFalse(self.fake.settings["xonxoff"])
        if os.name == "posix":
            self.assertTrue(self.fake.settings["exclusive"])
        worker.request_stop()
        self.pump_until(worker.isFinished)
        self.assertTrue(self.fake.closed)
        self.assertEqual(bytes(self.fake.bytes_written), b"")
        self.assertTrue(all(thread != gui_thread for thread in self.fake.thread_ids))

    def test_partial_write_success_counts_one_tx(self):
        worker = self.start_worker(configure=lambda fake: fake.write_actions.extend([1, 4, 10]))
        command = build_command("close", [1, 2], MotionParameters(force=60), 7)
        self.assertTrue(worker.enqueue(command))
        command["ids"].append(3)
        self.pump_until(lambda: worker.drain(0)[1]["tx"] == 1)
        events, counts = worker.drain()
        self.assertEqual(counts["tx"], 1)
        tx = next(event for event in events if event["kind"] == "tx")
        self.assertEqual(tx["command"]["ids"], [1, 2])
        self.assertEqual(bytes(self.fake.bytes_written), encode_command(tx["command"]))

    def test_partial_write_failure_not_reported_as_tx_or_retried(self):
        error = serial.SerialTimeoutException("test timeout")
        worker = self.start_worker(configure=lambda fake: fake.write_actions.extend([4, error]))
        self.assertTrue(worker.enqueue(build_command("open", [1], MotionParameters(), 1)))
        self.assertTrue(worker.enqueue(build_command("close", [1], MotionParameters(), 2)))
        self.pump_until(worker.isFinished)
        events, counts = worker.drain()
        self.assertEqual(counts["tx"], 0)
        self.assertFalse(any(event["kind"] == "tx" for event in events))
        self.assertEqual(counts["cancelled"], 1)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(len(self.fake.bytes_written), 4)
        self.assertTrue(self.fake.closed)
        self.assertEqual(len(self.messages), 1)

    def test_zero_write_closes_no_busy_loop(self):
        worker = self.start_worker(configure=lambda fake: fake.write_actions.append(0))
        worker.enqueue(build_command("open", [1], MotionParameters(), 1))
        self.pump_until(worker.isFinished)
        self.assertEqual(worker.drain()[1]["tx"], 0)
        self.assertTrue(self.messages)

    def test_split_reply_and_bad_json_recovery(self):
        worker = self.start_worker()
        payload = reply_line()
        self.fake.rx.put(payload[:15])
        self.fake.rx.put(payload[15:] + b'not json\n' + reply_line(2, 1, b"\r\n"))
        self.pump_until(lambda: worker.drain(0)[1]["rx"] == 2)
        events, counts = worker.drain()
        self.assertEqual(counts["invalid"], 1)
        replies = [event["command"] for event in events if event["kind"] == "rx"]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertTrue(any(event["kind"] == "warning" for event in events))

    def test_pending_queue_bounded_and_stop_cancels_it(self):
        entered, released, wait = self.gate()
        worker = self.start_worker(configure=lambda fake: setattr(fake, "read_gate", wait))
        self.pump_until(entered.is_set)
        for seq in range(TX_QUEUE_LIMIT):
            self.assertTrue(worker.enqueue(build_command("open", [1], MotionParameters(), seq)))
        self.assertFalse(worker.enqueue(build_command("close", [1], MotionParameters(), 100)))
        self.assertEqual(worker.drain(0)[1]["pending"], TX_QUEUE_LIMIT)
        worker.request_stop()
        released.set()
        self.pump_until(worker.isFinished)
        counts = worker.drain()[1]
        self.assertEqual(counts["tx"], 0)
        self.assertEqual(counts["cancelled"], TX_QUEUE_LIMIT)
        self.assertEqual(bytes(self.fake.bytes_written), b"")

    def test_enqueue_before_open_or_after_stop_rejected(self):
        worker = self.make_worker()
        command = build_command("open", [1], MotionParameters(), 1)
        self.assertFalse(worker.enqueue(command))
        worker.request_stop()
        worker.start()
        self.pump_until(worker.isFinished)
        self.assertFalse(worker.enqueue(command))
        self.assertIsNone(self.fake)

    def test_open_failure_still_closed_and_classified(self):
        worker = self.make_worker(configure=lambda fake: setattr(
            fake, "open_error", OSError(errno.EBUSY, "busy")))
        worker.start()
        self.pump_until(worker.isFinished)
        self.assertTrue(self.fake.closed)
        self.assertEqual(self.opened, [])
        self.assertIn("占用", self.messages[0])

    def test_permission_missing_and_lock_error_messages(self):
        for number, text in ((errno.EACCES, "权限"), (errno.ENOENT, "不存在"),
                             (errno.ENODEV, "拔出"), (errno.EAGAIN, "占用")):
            self.assertIn(text, _serial_problem("test-port", OSError(number, "error")))
        self.assertIn("占用", _serial_problem("test-port", serial.SerialException(
            "Could not exclusively lock port")))

    def test_ui_queue_bounded_and_drops_visible(self):
        worker = self.start_worker()
        count = EVENT_QUEUE_LIMIT + 150
        self.fake.rx.put(reply_line() * count)
        self.pump_until(lambda: worker.drain(0)[1]["rx"] == count)
        events, counts = worker.drain(20)
        self.assertEqual(len(events), 20)
        self.assertEqual(counts["dropped"], 150)
        self.assertEqual(events[0]["kind"], "warning")
        self.assertIn("150", events[0]["text"])
        self.assertLessEqual(len(worker._events), EVENT_QUEUE_LIMIT)

    def test_drain_limits_and_invalid_enqueue(self):
        worker = self.make_worker()
        for limit in (-1, True, 1.0):
            with self.assertRaises(ValueError):
                worker.drain(limit)
        with self.assertRaises(ValueError):
            worker.enqueue({"type": "omnipicker", "id": 1, "cmd": "move"})

    @unittest.skipUnless(os.name == "posix", "local PTY requires POSIX")
    def test_real_pty_ndjson_round_trip_and_clean_reconnect(self):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        port = os.ttyname(slave)
        worker = self.start_worker(port=port, factory=serial.Serial)
        command = build_command("move", [1, 2], MotionParameters(position=255), 99)
        self.assertFalse(select.select([master], [], [], 0)[0], "opening sent bytes")
        self.assertTrue(worker.enqueue(command))
        self.pump_until(lambda: worker.drain(0)[1]["tx"] == 1)
        self.assertTrue(select.select([master], [], [], 1)[0])
        self.assertEqual(os.read(master, 1024), encode_command(command))
        os.write(master, reply_line(1, 99)[:20])
        time.sleep(0.03)
        os.write(master, reply_line(1, 99)[20:] + reply_line(2, 99, b"\r\n"))
        self.pump_until(lambda: worker.drain(0)[1]["rx"] == 2)
        events, counts = worker.drain()
        self.assertEqual(counts["rx"], 2)
        self.assertEqual([event["command"]["id"] for event in events if event["kind"] == "rx"], [1, 2])
        # A half-line left in the old decoder must not pollute a new worker.
        os.write(master, reply_line(1, 99)[:13])
        time.sleep(0.05)
        worker.request_stop()
        self.pump_until(worker.isFinished)
        self.opened.clear()
        second = self.start_worker(port=port, factory=serial.Serial)
        self.assertFalse(select.select([master], [], [], 0)[0], "reconnect sent bytes")
        os.write(master, reply_line(1, 100))
        self.pump_until(lambda: second.drain(0)[1]["rx"] == 1)
        self.assertEqual(second.drain()[1]["invalid"], 0)


if __name__ == "__main__":
    unittest.main()
