"""Real Qt widgets + QThread + local PTY bridge, without USB/CAN devices.

Run alone or in unittest discovery with QT_QPA_PLATFORM=offscreen. The optional
OMNIPICKER_GUI_SCREENSHOT path captures local simulated feedback for visual QA.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLineEdit, QStyle, QStyleOptionSlider

from omnipicker_serial import MainWindow
from serial_worker import SerialWorker


def status(device_id, seq, *, position=255, speed=0, force=7,
           state=0, fault=0):
    return {"type": "omnipicker", "id": device_id, "seq": seq,
            "event": "status", "position": position, "speed": speed,
            "force": force, "state": state, "fault": fault}


def error(device_id, seq, text="can_tx_failed"):
    return {"type": "omnipicker", "id": device_id, "seq": seq,
            "event": "error", "error": text}


def wire(reply, ending=b"\n"):
    return json.dumps(reply, separators=(",", ":")).encode("ascii") + ending


class PortDiscoveryGuiTests(unittest.TestCase):
    """Exercise discovery failures without opening any serial device."""

    @classmethod
    def setUpClass(cls):
        existing = QApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise RuntimeError("GUI tests need QApplication, not QCoreApplication")
        cls.app = existing or QApplication(["omnipicker-port-discovery-test"])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.windows = []
        self.worker_factory = Mock(side_effect=AssertionError(
            "port discovery must not connect or create a serial worker"))

    def create_window(self, **kwargs):
        window = MainWindow(worker_factory=self.worker_factory, **kwargs)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def tearDown(self):
        for window in self.windows:
            self.assertIsNone(window.worker)
            window.timer.stop()
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.worker_factory.assert_not_called()

    def test_default_provider_reports_missing_pyserial_at_startup(self):
        # The default provider must check the same serial API used by the I/O
        # thread, rather than silently presenting a healthy empty port list.
        enumerator = Mock(side_effect=AssertionError(
            "missing serial API must be reported before enumerating devices"))
        with patch("serial_worker.serial", None), patch(
                "omnipicker_serial.list_ports", SimpleNamespace(comports=enumerator)):
            window = self.create_window()
            status_text = window.connection_status.text()
            log_text = window.log_view.toPlainText()
            for text in (status_text, log_text):
                self.assertIn("pyserial", text)
                self.assertIn(sys.executable, text)
                self.assertIn("run.sh", text)
            self.assertTrue(window.isVisible())
            self.assertFalse(window.connected)
            self.assertEqual(window.port.count(), 0)
            enumerator.assert_not_called()
            window.close()
            self.app.processEvents()
            self.assertFalse(window.isVisible())

    def test_failed_enumeration_preserves_manual_port_and_retry_recovers(self):
        for error in (OSError("enumerator device scan failed"),
                      RuntimeError("enumerator backend unavailable")):
            with self.subTest(error=type(error).__name__):
                recovered_port = SimpleNamespace(
                    device="/dev/ttyACM0", description="recovered USB CDC device")
                provider = Mock(side_effect=[[], error, [recovered_port]])
                window = self.create_window(port_provider=provider)
                window.port.setCurrentText("/dev/ttyACM0")
                window.refresh_button.click()
                self.app.processEvents()
                self.assertEqual(window.port.currentText(), "/dev/ttyACM0")
                self.assertIn("枚举失败", window.connection_status.text())
                self.assertIn(str(error), window.connection_status.text())
                self.assertIn(str(error), window.log_view.toPlainText())
                self.assertFalse(window.connected)
                self.assertTrue(window.connect_button.isEnabled())

                window.refresh_button.click()
                self.app.processEvents()
                self.assertGreaterEqual(window.port.findText("/dev/ttyACM0"), 0)
                self.assertEqual(window.port.currentText(), "/dev/ttyACM0")
                self.assertNotIn("枚举失败", window.connection_status.text())
                self.assertNotIn(str(error), window.connection_status.text())
                self.assertFalse(window.connected)

    def test_empty_healthy_provider_reports_no_ports_and_allows_manual_input(self):
        window = self.create_window(port_provider=lambda: [])
        self.assertIn("未检测到串口", window.connection_status.text())
        self.assertNotIn("pyserial", window.connection_status.text())
        self.assertNotIn("枚举失败", window.connection_status.text())
        self.assertTrue(window.port.isEditable())
        window.port.setCurrentText("/dev/ttyACM0")
        window.refresh_button.click()
        self.app.processEvents()
        self.assertEqual(window.port.currentText(), "/dev/ttyACM0")
        self.assertFalse(window.connected)
        self.assertFalse(window.move_button.isEnabled())


@unittest.skipUnless(os.name == "posix", "local PTY integration needs POSIX")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        existing = QApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise RuntimeError("GUI tests need QApplication, not QCoreApplication")
        cls.app = existing or QApplication(["omnipicker-gui-test"])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        import pty
        self.master, self.slave = pty.openpty()
        self.serial_port = os.ttyname(self.slave)
        os.set_blocking(self.master, False)
        self.received = bytearray()
        self.window = MainWindow(
            port_provider=lambda: [SimpleNamespace(
                device=self.serial_port, description="本地 PTY 模拟桥接板")],
            worker_factory=SerialWorker,
        )
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        deadline = time.monotonic() + 3
        while self.window.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertIsNone(self.window.worker, "GUI left a serial QThread running")
        self.window.timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        os.close(self.master)
        os.close(self.slave)

    def pump(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()
        self.assertTrue(predicate(), "GUI did not reach expected state")

    def idle(self, duration=0.1):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)

    def connect(self, timeout=None):
        if timeout is not None:
            self.window.reply_timeout.setValue(timeout)
        self.assertEqual(self.window.port.currentText(), self.serial_port)
        self.window.connect_button.click()
        self.pump(lambda: self.window.connected)
        self.assertFalse(self.window.port.isEnabled())
        self.assertTrue(self.window.disconnect_button.isEnabled())

    def read_available(self):
        while select.select([self.master], [], [], 0)[0]:
            self.received.extend(os.read(self.master, 4096))
        return bytes(self.received)

    def read_command(self):
        self.pump(lambda: b"\n" in self.read_available())
        newline = self.received.index(10)
        payload = bytes(self.received[:newline + 1])
        del self.received[:newline + 1]
        self.assertTrue(payload.endswith(b"\n"))
        self.assertFalse(payload.endswith(b"\\n"))
        self.assertNotIn(b"\r", payload)
        command = json.loads(payload)
        self.assertEqual(command["type"], "omnipicker")
        self.assertIs(type(command["seq"]), int)
        self.assertGreater(command["seq"], 0)
        self.assertEqual(self.window.command_preview.toPlainText(),
                         payload[:-1].decode("ascii"))
        return command

    def write_bytes(self, payload):
        offset = 0
        deadline = time.monotonic() + 5
        while offset < len(payload):
            self.app.processEvents()
            self.assertLess(time.monotonic(), deadline, "PTY writer stalled")
            if not select.select([], [self.master], [], 0.02)[1]:
                continue
            try:
                offset += os.write(self.master, payload[offset:offset + 4096])
            except BlockingIOError:
                pass

    def reply(self, reply, ending=b"\n"):
        self.write_bytes(wire(reply, ending))

    def acknowledge(self, command, position=255):
        targets = command.get("ids", [command.get("id")])
        for device_id in targets:
            self.reply(status(device_id, command["seq"], position=position))
        self.pump(lambda: all(device_id not in self.window.session.pending
                              for device_id in targets))

    def row(self, device_id):
        row_index = self.window.table_ids.index(device_id)
        return [self.window.table.item(row_index, column).text()
                for column in range(self.window.table.columnCount())]

    def assert_no_serial_output(self, duration=0.1):
        self.idle(duration)
        self.assertEqual(self.read_available(), b"", "unexpected automatic command")

    def release_slider(self, name, value):
        """Use Qt's real sliderReleased signal, without emitting it by hand."""
        slider = self.window.parameters[name].slider
        slider.setSliderDown(True)
        slider.setValue(value)
        self.app.processEvents()
        self.assertEqual(self.window.parameters[name].value.value(), value)
        slider.setSliderDown(False)
        self.app.processEvents()

    def test_default_connect_only_selects_port_and_sends_nothing(self):
        self.assertFalse(self.window.connected)
        self.assertTrue(self.window.single_mode.isChecked())
        self.assertEqual(self.window.target_id.value(), 1)
        self.assertEqual(self.window.parameters["force"].value.value(), 255)
        self.assertFalse(self.window.settings_panel.isVisible())
        self.assertFalse(self.window.open_button.isEnabled())
        self.assertFalse(self.window.close_button.isEnabled())
        self.assertFalse(self.window.move_button.isEnabled())
        self.connect()
        self.assertTrue(self.window.move_button.isEnabled())
        self.assertEqual(self.window.session.pending, {})
        self.assert_no_serial_output()

    def test_parameter_numbers_fit_at_default_and_minimum_window_size(self):
        for width, height in ((1180, 880), (920, 700)):
            self.window.resize(width, height)
            self.idle(0.02)
            for name, control in self.window.parameters.items():
                editor = control.value.findChild(QLineEdit)
                self.assertGreaterEqual(editor.height(), editor.fontMetrics().height(),
                                        f"{name} digits vertically clipped at {width}x{height}")
                control.value.setValue(255)
                self.app.processEvents()
                margins = editor.textMargins()
                available_width = editor.contentsRect().width() - margins.left() - margins.right()
                self.assertGreaterEqual(available_width, editor.fontMetrics().horizontalAdvance("255"),
                                        f"{name} digits horizontally clipped at {width}x{height}")

    def test_mouse_drag_sends_once_after_release_with_current_parameters(self):
        self.connect()
        parameters = {"position": 128, "speed": 85, "force": 46,
                      "acceleration": 102, "deceleration": 156}
        for name, value in parameters.items():
            self.window.parameters[name].value.setValue(value)
        slider = self.window.parameters["position"].slider
        option = QStyleOptionSlider()
        slider.initStyleOption(option)
        handle = slider.style().subControlRect(
            QStyle.CC_Slider, option, QStyle.SC_SliderHandle, slider)
        start = handle.center()
        destination = QPoint(slider.width() - handle.width() // 2 - 3,
                             start.y())
        QTest.mousePress(slider, Qt.LeftButton, Qt.NoModifier, start)
        self.assertTrue(slider.isSliderDown())
        QTest.mouseMove(slider, destination, delay=10)
        # Offscreen platforms cannot propagate QCursor motion with the held
        # button; deliver the same Qt mouse-move event with its button state.
        self.app.sendEvent(slider, QMouseEvent(
            QEvent.MouseMove, QPointF(destination), Qt.NoButton,
            Qt.LeftButton, Qt.NoModifier))
        self.idle(0.04)
        self.assert_no_serial_output(duration=0.04)
        QTest.mouseRelease(slider, Qt.LeftButton, Qt.NoModifier, destination)
        self.assertFalse(slider.isSliderDown())
        command = self.read_command()
        self.assertEqual(command["cmd"], "move")
        self.assertEqual(command["id"], 1)
        self.assertGreater(command["position"], parameters["position"])
        self.assertEqual(command["position"], slider.value())
        for name, value in parameters.items():
            if name != "position":
                self.assertEqual(command[name], value)
        self.assert_no_serial_output()
        self.acknowledge(command, command["position"])
        self.assert_no_serial_output()

    def test_every_parameter_slider_release_sends_complete_single_and_multi_json(self):
        self.connect()
        values = {"position": 74, "speed": 83, "force": 109,
                  "acceleration": 137, "deceleration": 196}
        for multiple in (False, True):
            if multiple:
                self.window.multi_mode.click()
                self.window.target_ids.setText("1,2")
            for name, value in values.items():
                with self.subTest(multiple=multiple, parameter=name):
                    slider = self.window.parameters[name].slider
                    slider.setSliderDown(True)
                    slider.setValue(value - 5)
                    slider.setValue(value)
                    self.assert_no_serial_output(duration=0.03)
                    slider.setSliderDown(False)
                    command = self.read_command()
                    self.assertEqual(command["cmd"], "move")
                    if multiple:
                        self.assertEqual(command["ids"], [1, 2])
                        self.assertNotIn("id", command)
                    else:
                        self.assertEqual(command["id"], 1)
                        self.assertNotIn("ids", command)
                    for parameter, control in self.window.parameters.items():
                        self.assertEqual(command[parameter], control.value.value())
                    self.assertEqual(command[name], value)
                    self.acknowledge(command, command["position"])
                    self.assert_no_serial_output(duration=0.03)

    def test_programmatic_values_and_disconnected_release_never_send_or_replay(self):
        for name, value in (("position", 97), ("force", 83)):
            self.window.parameters[name].slider.setValue(value)
            self.window.parameters[name].value.setValue(value + 1)
            self.release_slider(name, value + 2)
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.session.pending, {})
        self.connect()
        self.assert_no_serial_output()
        for name, value in (("position", 17), ("force", 221), ("speed", 114)):
            self.window.parameters[name].slider.setValue(value)
            self.window.parameters[name].value.setValue(value + 1)
        self.assert_no_serial_output()
        self.window.close_button.click()
        command = self.read_command()
        self.assertEqual(command["position"], 0)
        self.assertEqual(self.window.parameters["position"].slider.value(), 0)
        self.acknowledge(command, 0)
        self.assert_no_serial_output()

    def test_pending_slider_releases_send_only_latest_snapshot_after_current_status(self):
        self.connect()
        self.window.move_button.click()
        first = self.read_command()
        self.release_slider("position", 64)
        self.release_slider("force", 123)
        self.release_slider("position", 211)
        self.assert_no_serial_output()
        # Ordinary editing after release must not rewrite the queued request.
        self.window.parameters["position"].value.setValue(17)
        self.window.parameters["force"].value.setValue(202)
        self.window.parameters["speed"].value.setValue(41)
        self.assert_no_serial_output(duration=0.03)
        self.reply(status(1, first["seq"], position=first["position"]))
        next_command = self.read_command()
        self.assertGreater(next_command["seq"], first["seq"])
        self.assertEqual(next_command["id"], 1)
        self.assertEqual(next_command["cmd"], "move")
        self.assertEqual(next_command["position"], 211)
        self.assertEqual(next_command["force"], 123)
        self.assertEqual(next_command["speed"], 255)
        self.assertEqual(next_command["acceleration"], 255)
        self.assertEqual(next_command["deceleration"], 255)
        self.assert_no_serial_output()
        self.acknowledge(next_command, 211)
        self.assert_no_serial_output()

    def test_old_or_missing_seq_cannot_release_deferred_slider_command(self):
        self.connect()
        self.window.close_button.click()
        previous = self.read_command()
        self.acknowledge(previous, 0)
        self.window.open_button.click()
        current = self.read_command()
        self.release_slider("position", 91)
        self.reply(status(1, previous["seq"], position=0))
        missing_sequence = status(1, current["seq"], position=44)
        del missing_sequence["seq"]
        self.reply(missing_sequence)
        self.assert_no_serial_output(duration=0.15)
        self.assertEqual(self.window.session.pending[1].seq, current["seq"])
        self.reply(status(1, current["seq"], position=255))
        deferred = self.read_command()
        self.assertEqual(deferred["position"], 91)
        self.acknowledge(deferred, 91)
        self.assert_no_serial_output()

    def test_multi_id_release_waits_for_all_matching_status_replies(self):
        self.connect()
        self.window.multi_mode.click()
        self.window.target_ids.setText("1,2")
        self.window.open_button.click()
        first = self.read_command()
        self.release_slider("force", 174)
        self.release_slider("position", 68)
        self.reply(status(2, first["seq"], position=255))
        self.pump(lambda: 2 not in self.window.session.pending)
        self.assertIn(1, self.window.session.pending)
        self.assert_no_serial_output()
        self.reply(status(1, first["seq"], position=255))
        deferred = self.read_command()
        self.assertEqual(deferred["ids"], [1, 2])
        self.assertEqual(deferred["position"], 68)
        self.assertEqual(deferred["force"], 174)
        self.acknowledge(deferred, 68)
        self.assert_no_serial_output()

    def test_error_discards_deferred_slider_request_without_automatic_retry(self):
        self.connect()
        self.window.move_button.click()
        first = self.read_command()
        self.release_slider("position", 52)
        self.reply(error(1, first["seq"]))
        self.pump(lambda: not self.window.session.pending)
        self.assert_no_serial_output(duration=0.15)
        self.window.move_button.click()
        recovery = self.read_command()
        self.assertEqual(recovery["position"], 52)
        self.acknowledge(recovery, 52)
        self.assert_no_serial_output()

    def test_partial_multi_error_discards_deferred_request_after_remaining_status(self):
        self.connect()
        self.window.multi_mode.click()
        self.window.target_ids.setText("1,2")
        self.window.move_button.click()
        first = self.read_command()
        self.release_slider("position", 57)
        self.reply(error(2, first["seq"]))
        self.pump(lambda: 2 not in self.window.session.pending)
        self.assertIn(1, self.window.session.pending)
        self.assert_no_serial_output()
        self.reply(status(1, first["seq"], position=128))
        self.pump(lambda: not self.window.session.pending)
        self.assert_no_serial_output(duration=0.15)

    def test_global_error_discards_deferred_request_without_clearing_current_pending(self):
        self.connect()
        self.window.move_button.click()
        first = self.read_command()
        self.release_slider("position", 58)
        self.reply({"type": "omnipicker", "event": "error", "error": "invalid_json"})
        self.pump(lambda: "invalid_json" in self.window.command_status.text())
        self.assertEqual(self.window.session.pending[1].seq, first["seq"])
        self.assert_no_serial_output()
        self.reply(status(1, first["seq"], position=128))
        self.pump(lambda: not self.window.session.pending)
        self.assert_no_serial_output(duration=0.15)
        # A new user release after the error is a fresh explicit request.
        self.release_slider("position", 59)
        recovery = self.read_command()
        self.assertEqual(recovery["position"], 59)
        self.assertGreater(recovery["seq"], first["seq"])
        self.acknowledge(recovery, 59)
        self.assert_no_serial_output()

    def test_timeout_discards_deferred_slider_request_even_if_status_arrives_late(self):
        self.connect(timeout=0.6)
        self.window.move_button.click()
        first = self.read_command()
        self.release_slider("position", 53)
        self.pump(lambda: not self.window.session.pending, timeout=2)
        self.assert_no_serial_output()
        self.reply(status(1, first["seq"], position=128))
        self.assert_no_serial_output(duration=0.15)
        self.window.move_button.click()
        recovery = self.read_command()
        self.assertEqual(recovery["position"], 53)
        self.acknowledge(recovery, 53)
        self.assert_no_serial_output()

    def test_disconnect_discards_deferred_slider_request_without_reconnect_replay(self):
        self.connect()
        self.window.move_button.click()
        first = self.read_command()
        self.release_slider("position", 54)
        self.window.disconnect_button.click()
        self.pump(lambda: self.window.worker is None)
        self.assert_no_serial_output()
        self.connect()
        self.assert_no_serial_output()
        self.reply(status(1, first["seq"], position=128))
        self.assert_no_serial_output(duration=0.15)
        self.window.move_button.click()
        recovery = self.read_command()
        self.assertEqual(recovery["position"], 54)
        self.acknowledge(recovery, 54)
        self.assert_no_serial_output()

    def test_changing_target_id_or_mode_discards_deferred_slider_request(self):
        self.connect()
        for index, change in enumerate((lambda: self.window.target_id.setValue(2),
                                        self.window.multi_mode.click,
                                        lambda: self.window.target_ids.setText("2,3"))):
            with self.subTest(change=change):
                self.window.move_button.click()
                first = self.read_command()
                self.release_slider("position", 55 + index)
                change()
                for device_id in first.get("ids", [first.get("id")]):
                    self.reply(status(device_id, first["seq"], position=128))
                self.pump(lambda: not self.window.session.pending)
                self.assert_no_serial_output(duration=0.15)

    def test_close_open_move_encode_lf_single_id_and_all_parameters(self):
        self.connect()
        params = {"position": 73, "speed": 85, "force": 46,
                  "acceleration": 102, "deceleration": 156}
        for name, value in params.items():
            self.window.parameters[name].value.setValue(value)
            self.assertEqual(self.window.parameters[name].slider.value(), value)
        self.window.parameters["force"].slider.setValue(61)
        self.assertEqual(self.window.parameters["force"].value.value(), 61)
        self.window.parameters["force"].value.setValue(params["force"])
        sequences = []
        for cmd, button, position in (
                ("close", self.window.close_button, 0),
                ("open", self.window.open_button, 255),
                ("move", self.window.move_button, params["position"])):
            if cmd == "move":
                self.window.parameters["position"].value.setValue(position)
            self.assertTrue(button.isEnabled())
            button.click()
            command = self.read_command()
            sequences.append(command["seq"])
            self.assertEqual(command["id"], 1)
            self.assertNotIn("ids", command)
            self.assertEqual(command["cmd"], "move")
            for name, value in params.items():
                self.assertEqual(command[name], position if name == "position" else value)
            self.assertEqual(self.window.parameters["position"].value.value(), position)
            self.assertEqual(self.window.parameters["position"].slider.value(), position)
            self.assertFalse(self.window.open_button.isEnabled())
            self.assertFalse(self.window.close_button.isEnabled())
            self.assertFalse(self.window.move_button.isEnabled())
            self.acknowledge(command, position)
            self.assertEqual(self.row(1)[1], "收到反馈")
            self.assertEqual(self.row(1)[2], str(position))
            self.assertIn("已收到 ID 1", self.window.command_status.text())
            self.assertNotIn("等待", self.window.command_status.text())
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assert_no_serial_output()

    def test_default_single_and_multi_buttons_match_working_reference_json(self):
        # This is the user's known-working command, read from actual PTY bytes.
        reference = json.loads(
            '{"type":"omnipicker","ids":[1,2],"cmd":"move","position":0,'
            '"speed":255,"force":255,"acceleration":255,"deceleration":255}')
        self.connect()
        for multiple in (False, True):
            if multiple:
                self.window.multi_mode.click()
                self.window.target_ids.setText("1,2")
            for position, button in ((0, self.window.close_button),
                                     (255, self.window.open_button)):
                with self.subTest(multiple=multiple, position=position):
                    expected = dict(reference, position=position)
                    if not multiple:
                        del expected["ids"]
                        expected["id"] = 1
                    button.click()
                    command = self.read_command()
                    self.assertEqual({key: value for key, value in command.items() if key != "seq"},
                                     expected)
                    self.assertEqual(self.window.parameters["position"].value.value(), position)
                    self.assertEqual(self.window.parameters["position"].slider.value(), position)
                    self.acknowledge(command, position=position)
        self.assert_no_serial_output()

    def test_low_force_stall_is_visible_without_retry_then_manual_recovery(self):
        self.connect()
        self.window.parameters["force"].value.setValue(32)
        self.window.close_button.click()
        stalled_request = self.read_command()
        self.assertEqual(stalled_request["cmd"], "move")
        self.assertEqual(stalled_request["position"], 0)
        self.assertEqual(stalled_request["force"], 32)
        self.reply(status(1, stalled_request["seq"], position=255,
                          speed=0, force=7, state=2, fault=0))
        self.pump(lambda: not self.window.session.pending)
        self.assertIn("堵转", self.window.command_status.text())
        self.assertIn("目标力矩 32", self.window.command_status.text())
        self.assertIn("#b42318", self.window.command_status.styleSheet())
        self.assertEqual(self.row(1)[1], "收到反馈")
        self.assertEqual(self.row(1)[2], "255")
        self.assertTrue(self.row(1)[5].startswith("2 "))
        self.assertTrue(self.window.close_button.isEnabled())
        self.assert_no_serial_output(duration=0.25)

        self.window.parameters["force"].value.setValue(255)
        self.window.close_button.click()
        recovered_request = self.read_command()
        self.assertGreater(recovered_request["seq"], stalled_request["seq"])
        self.assertEqual(recovered_request["position"], 0)
        self.assertEqual(recovered_request["force"], 255)
        self.acknowledge(recovered_request, position=0)
        self.assertNotIn("堵转", self.window.command_status.text())
        self.assertNotIn("#b42318", self.window.command_status.styleSheet())
        self.window.open_button.click()
        open_request = self.read_command()
        self.assertEqual(open_request["cmd"], "move")
        self.assertEqual(open_request["position"], 255)
        self.assertEqual(open_request["force"], 255)
        self.assertEqual(self.window.parameters["position"].slider.value(), 255)
        self.acknowledge(open_request, position=255)
        self.assert_no_serial_output()

    def test_multi_mode_updates_widgets_and_out_of_order_partial_error(self):
        self.connect()
        self.window.multi_mode.click()
        self.assertTrue(self.window.target_ids.isEnabled())
        self.assertFalse(self.window.target_id.isEnabled())
        self.window.target_ids.setText("1,2")
        self.assertTrue(self.window.close_button.isEnabled())
        self.window.close_button.click()
        command = self.read_command()
        self.assertEqual(command["ids"], [1, 2])
        self.assertNotIn("id", command)
        self.assertEqual(set(self.window.session.pending), {1, 2})
        self.reply(status(2, command["seq"], position=0, force=183,
                          state=2, fault=3), b"\r\n")
        self.pump(lambda: 2 not in self.window.session.pending)
        self.assertIn(1, self.window.session.pending)
        self.assertFalse(self.window.close_button.isEnabled())
        self.assertIn("等待 ID 1", self.window.command_status.text())
        second = self.row(2)
        self.assertEqual(second[1:5], ["收到反馈", "0", "0", "183"])
        self.assertTrue(second[5].startswith("2 "))
        self.assertTrue(second[6].startswith("3 "))
        self.reply(error(1, command["seq"]))
        self.pump(lambda: not self.window.session.pending)
        self.assertEqual(self.row(1)[1], "错误：can_tx_failed")
        self.assertEqual(self.row(1)[2], "—")
        self.assertTrue(self.window.open_button.isEnabled())
        self.assertIn("ID 1", self.window.command_status.text())
        self.window.single_mode.click()
        self.assertTrue(self.window.target_id.isEnabled())
        self.assertFalse(self.window.target_ids.isEnabled())

    def test_old_seq_keeps_new_pending_and_different_id_can_still_send(self):
        self.connect()
        self.window.close_button.click()
        old = self.read_command()
        self.acknowledge(old, 0)
        self.window.open_button.click()
        current = self.read_command()
        self.reply(status(1, old["seq"], position=0, force=173, state=2))
        self.pump(lambda: self.window.session.devices[1].feedback_seq == old["seq"])
        self.assertEqual(self.window.session.pending[1].seq, current["seq"])
        self.assertFalse(self.window.open_button.isEnabled())
        self.assertIn(self.row(1)[1], ("排队", "等待反馈"))
        self.assertNotIn("堵转", self.window.command_status.text())
        self.reply({"type": "omnipicker", "event": "error", "error": "invalid_json"})
        self.pump(lambda: "invalid_json" in self.window.command_status.text())
        self.assertIn(1, self.window.session.pending)
        self.window.target_id.setValue(2)
        self.assertTrue(self.window.move_button.isEnabled())
        self.window.move_button.click()
        second = self.read_command()
        self.assertEqual(second["id"], 2)
        self.assertEqual(set(self.window.session.pending), {1, 2})
        self.reply(status(1, current["seq"], position=255))
        self.reply(status(2, second["seq"], position=128))
        self.pump(lambda: not self.window.session.pending)
        self.assertEqual(self.row(1)[2], "255")
        self.assertEqual(self.row(2)[2], "128")

    def test_reply_timeout_reports_uncertain_result_without_auto_retry(self):
        self.connect(timeout=0.6)
        self.window.close_button.click()
        self.read_command()
        self.pump(lambda: not self.window.session.pending, timeout=2)
        self.assertEqual(self.row(1)[1], "未收到回执，可能已动作")
        self.assertEqual(self.row(1)[2], "—")
        self.assertEqual(self.row(1)[8], "—")
        self.assertIn("可能已动作", self.window.command_status.text())
        self.assertTrue(self.window.close_button.isEnabled())
        self.assert_no_serial_output(duration=0.7)

    def test_disconnect_reconnect_and_window_close_stop_qthread_cleanly(self):
        self.connect()
        self.window.close_button.click()
        old = self.read_command()
        self.write_bytes(wire(status(1, old["seq"]))[:14])
        self.idle(0.05)
        finished = []
        self.window.worker.finished.connect(lambda: finished.append(True))
        self.window.disconnect_button.click()
        self.pump(lambda: self.window.worker is None)
        self.assertEqual(finished, [True])
        self.assertFalse(self.window.connected)
        self.assertEqual(self.window.session.pending, {})
        self.assertEqual(self.row(1)[1], "连接断开，结果未知")
        self.assertTrue(self.window.port.isEnabled())
        self.assertFalse(self.window.move_button.isEnabled())
        self.connect()
        self.assert_no_serial_output()
        self.reply(status(1, old["seq"], position=0))
        self.pump(lambda: self.row(1)[2] == "0")
        self.assertIn("无效回包 0", self.window.count_label.text())
        self.window.open_button.click()
        self.read_command()
        finished.clear()
        self.window.worker.finished.connect(lambda: finished.append(True))
        self.window.close()
        self.pump(lambda: self.window.worker is None and not self.window.isVisible())
        self.assertEqual(finished, [True])
        self.assertFalse(self.window.connected)
        self.assertEqual(self.window.session.pending, {})
        self.assert_no_serial_output()

    def test_malformed_and_fragmented_replies_recover_and_log_is_bounded(self):
        self.connect()
        valid = wire(status(2, 8, position=217, force=18), b"\r\n")
        self.write_bytes(b'not-json\n{"type":"can"}\n' + b"x" * 450 + b"\n" + valid[:19])
        self.idle(0.06)
        self.assertIsNone(self.window.session.devices[2].position)
        self.write_bytes(valid[19:])
        self.pump(lambda: self.row(2)[2] == "217")
        self.assertIn("无效回包 3", self.window.count_label.text())
        self.assertEqual(self.row(2)[1], "未关联反馈")
        self.assertIn("忽略", self.window.log_view.toPlainText())
        count = 1100
        self.write_bytes(wire(status(2, 9, position=218, force=19)) * count)
        self.pump(lambda: f"RX {count + 1} /" in self.window.count_label.text(), timeout=5)
        self.pump(lambda: self.window.worker.drain(0)[1]["rx"] == count + 1)
        self.idle(0.1)
        self.assertLessEqual(self.window.log_view.document().blockCount(), 1000)
        self.assertEqual(self.row(2)[2], "218")
        self.assertEqual(self.row(2)[4], "19")
        self.assert_no_serial_output()

    def test_invalid_multi_ids_disable_commands_until_corrected(self):
        self.connect()
        self.window.multi_mode.click()
        for invalid in ("", "1,", "1,1", "-1,2", "2048", ",".join(map(str, range(17)))):
            self.window.target_ids.setText(invalid)
            self.assertFalse(self.window.open_button.isEnabled(), invalid)
            self.assertIn("格式不正确", self.window.target_hint.text())
        self.assert_no_serial_output()
        self.window.target_ids.setText("0x01,2")
        self.assertTrue(self.window.move_button.isEnabled())
        self.window.move_button.click()
        self.assertEqual(self.read_command()["ids"], [1, 2])

    def test_failed_port_keeps_selection_and_refresh_preserves_it(self):
        missing = f"/tmp/omnipicker-absent-port-{os.getpid()}"
        self.window.port.setCurrentText(missing)
        self.window.connect_button.click()
        self.pump(lambda: self.window.worker is None and bool(self.window.last_serial_error))
        self.assertFalse(self.window.connected)
        self.assertEqual(self.window.port.currentText(), missing)
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assertTrue(self.window.port.isEnabled())
        self.assertFalse(self.window.close_button.isEnabled())
        self.assertIn("不存在", self.window.connection_status.text())
        self.window.refresh_button.click()
        self.assertEqual(self.window.port.currentText(), missing)
        self.assertEqual(self.window.session.pending, {})

    def test_serial_package_conflict_does_not_crash_connect_slot(self):
        conflicting = SimpleNamespace(__file__="/conda/site-packages/serial/__init__.py")
        with patch("serial_worker.serial", conflicting):
            self.window.connect_button.click()
            self.app.processEvents()
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.window.connected)
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assertEqual(self.window.port.currentText(), self.serial_port)
        self.assertIn("pyserial", self.window.connection_status.text())
        self.assertIn(conflicting.__file__, self.window.connection_status.text())
        self.assert_no_serial_output()
        self.connect()
        self.assert_no_serial_output()

    def test_render_simulated_multi_device_feedback(self):
        self.connect()
        self.window.multi_mode.click()
        self.window.target_ids.setText("1,2")
        self.window.close_button.click()
        command = self.read_command()
        self.reply(status(2, command["seq"], position=0, force=176))
        self.reply(status(1, command["seq"], position=0, force=181))
        self.pump(lambda: not self.window.session.pending)
        self.assertIn("已收到 ID 1,2", self.window.command_status.text())
        self.assertNotIn("等待", self.window.command_status.text())
        self.window.connection_status.setText("本地 PTY 模拟桥接板已连接 · 反馈由测试模拟")
        screenshot = os.environ.get("OMNIPICKER_GUI_SCREENSHOT")
        if screenshot:
            self.idle(0.1)
            self.assertTrue(self.window.grab().save(screenshot))


if __name__ == "__main__":
    unittest.main()
