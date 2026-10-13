"""Check real Qt layout at large fonts on a small desktop, without devices."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QGroupBox, QLabel, QStyle

from omnipicker_serial import MainWindow


class ResponsiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["omnipicker-layout-test"])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.original_font = QFont(self.app.font())
        self.windows = []
        self.worker_factory = Mock(side_effect=AssertionError(
            "layout checks must never open a serial device"))

    def tearDown(self):
        for window in self.windows:
            self.assertIsNone(window.worker)
            window.timer.stop()
            window.close()
            window.deleteLater()
        self.app.setFont(self.original_font)
        self.app.processEvents()
        self.worker_factory.assert_not_called()

    def create_window(self, font_size, desktop):
        self.app.setFont(QFont("sans", font_size))
        window = MainWindow(port_provider=lambda: [],
                            worker_factory=self.worker_factory)
        self.windows.append(window)
        window._fit_initial_geometry(available_geometry=desktop)
        window.show()
        self.app.processEvents()
        return window

    def assert_parameters_complete(self, window):
        for name, control in window.parameters.items():
            with self.subTest(parameter=name):
                self.assertGreaterEqual(control.height(),
                                        control.minimumSizeHint().height())
                title = control.findChildren(QLabel)[0]
                for child in (title, control.slider, control.value):
                    self.assertTrue(control.rect().contains(child.geometry()),
                                    f"{name}: child clipped outside parameter control")
                self.assertGreaterEqual(title.height(), title.fontMetrics().height())
                self.assertGreaterEqual(control.slider.height(),
                                        control.slider.sizeHint().height())
                self.assertGreaterEqual(control.value.height(),
                                        control.value.minimumHeight())
                self.assertFalse(control.slider.geometry().intersects(
                    control.value.geometry()), f"{name}: slider overlaps number input")
                self.assertLess(title.geometry().bottom(),
                                min(control.slider.y(), control.value.y()),
                                f"{name}: title overlaps slider row")
                self.assertLess(control.geometry().bottom(),
                                window.move_button.geometry().top(),
                                f"{name}: motion button row overlaps parameters")
        for widget in (window.target_id, window.open_button,
                       window.close_button, window.move_button):
            self.assertTrue(widget.parentWidget().rect().contains(widget.geometry()),
                            "target or motion controls are clipped")
            self.assertGreaterEqual(widget.height(), widget.minimumSizeHint().height())

    def test_default_size_and_parameters_fit_small_desktop_at_large_fonts(self):
        desktop = QRect(0, 0, 1280, 800)
        for font_size in (14, 18):
            with self.subTest(font_size=font_size):
                window = self.create_window(font_size, desktop)
                self.assertTrue(desktop.contains(window.frameGeometry()),
                                "default window extends outside available desktop")
                self.assert_parameters_complete(window)
                groups = window.content_widget.findChildren(QGroupBox)
                for first, second in zip(groups, groups[1:]):
                    self.assertFalse(first.geometry().intersects(second.geometry()),
                                     "main sections overlap")
                scroll = window.content_scroll
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0,
                                   "small desktop should scroll instead of crushing controls")
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                log_bottom = window.log_view.mapTo(
                    scroll.viewport(), window.log_view.rect().bottomRight()).y()
                self.assertLess(log_bottom, scroll.viewport().height(),
                                "scrollbar cannot reach the bottom of the log")
                self.assertGreater(log_bottom, 0)

    def test_settings_expansion_and_narrow_resize_preserve_parameter_geometry(self):
        window = self.create_window(18, QRect(0, 0, 1280, 800))
        window.settings_button.click()
        window.resize(700, 600)
        self.app.processEvents()
        self.assertTrue(window.settings_panel.isVisible())
        self.assert_parameters_complete(window)
        self.assertGreater(window.content_scroll.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(window.content_widget.width(),
                                window.content_widget.minimumSizeHint().width())
        scroll = window.content_scroll
        scroll.ensureWidgetVisible(window.move_button)
        self.app.processEvents()
        button_top = window.move_button.mapTo(scroll.viewport(), QPoint(0, 0)).y()
        button_bottom = window.move_button.mapTo(
            scroll.viewport(), window.move_button.rect().bottomRight()).y()
        self.assertGreaterEqual(button_top, 0)
        self.assertLess(button_bottom, scroll.viewport().height(),
                        "motion buttons are unreachable after resizing")

    def test_window_centers_on_the_given_monitor(self):
        desktop = QRect(1280, 50, 1280, 800)
        window = self.create_window(14, desktop)
        self.assertTrue(desktop.contains(window.frameGeometry()))
        distance = window.frameGeometry().center() - desktop.center()
        self.assertLessEqual(abs(distance.x()), 4)
        # Offscreen does not draw a native title bar, while the initial size
        # reserves space for the real desktop's title bar and window frame.
        title_height = window.style().pixelMetric(QStyle.PM_TitleBarHeight)
        self.assertLessEqual(abs(distance.y()), title_height // 2 + 4)


if __name__ == "__main__":
    unittest.main()
