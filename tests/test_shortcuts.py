import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from lark.app import MainWindow, build_context
from lark.config import Config
from lark.shortcuts import is_text_input
from tests.test_ui_smoke import FakeThread, pump


class ShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        for k, v in (("obs_port", cls.ft.port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", ""), ("highlights_dir", tempfile.mkdtemp()),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 2)):
            cfg.set(k, v)
        cls.ctx = build_context(cfg)
        cls.win = MainWindow(cls.ctx, "live")
        cls.win.show()
        cls.ctx.bridge.start()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and s.scenes and "Mic (Fifine)" in s.inputs and s.stream_key_set is not None)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def key(self, key, mods=Qt.ControlModifier):
        self.win.activateWindow()
        QTest.keyClick(self.win, key, mods)

    def test_ctrl_n_switches_scene_in_obs_order(self):
        s, f = self.ctx.store, self.ft.fake
        self.key(Qt.Key_3)
        self.assertTrue(pump(self.app, lambda: f.current == "BRB"), f.current)
        self.key(Qt.Key_1)
        self.assertTrue(pump(self.app, lambda: f.current == "Game"))
        self.key(Qt.Key_9)  # no 9th scene: nothing happens
        self.app.processEvents()
        self.assertEqual(f.current, "Game")

    def test_ctrl_m_toggles_mic_mute(self):
        f = self.ft.fake
        before = f.inputs["Mic (Fifine)"]["mute"]
        self.key(Qt.Key_M)
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] == (not before)))
        self.key(Qt.Key_M)
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] == before))

    def test_ctrl_shift_r_and_ctrl_comma(self):
        f = self.ft.fake
        self.key(Qt.Key_R, Qt.ControlModifier | Qt.ShiftModifier)  # starts the buffer first
        self.assertTrue(pump(self.app, lambda: f.replay and self.ctx.store.replay))
        self.key(Qt.Key_R, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertTrue(pump(self.app, lambda: any(t == "SaveReplayBuffer" for t, _ in f.requests)))
        self.key(Qt.Key_Comma)
        self.assertIs(self.win.stack.currentWidget(), self.win.pages["settings"])
        self.win.show_view("live")

    def test_ctrl_g_goes_live_and_needs_two_presses_to_stop(self):
        s, f = self.ctx.store, self.ft.fake
        self.key(Qt.Key_G)
        self.assertTrue(pump(self.app, lambda: s.stream["active"]))
        self.key(Qt.Key_G)
        pump(self.app, lambda: False, 0.3)
        self.assertTrue(f.streaming, "first press only arms the stop")
        self.key(Qt.Key_G)
        self.assertTrue(pump(self.app, lambda: not f.streaming))

    def test_nothing_fires_while_a_text_field_has_focus(self):
        f = self.ft.fake
        self.win.show_view("settings")
        field = self.win.pages["settings"].channel
        field.setFocus()
        self.win.activateWindow()
        self.assertTrue(pump(self.app, lambda: self.app.focusWidget() is field), "focus did not land")
        self.assertTrue(is_text_input(field))
        before = (f.current, f.inputs["Mic (Fifine)"]["mute"])
        self.key(Qt.Key_2)
        self.key(Qt.Key_M)
        pump(self.app, lambda: False, 0.4)
        self.assertEqual((f.current, f.inputs["Mic (Fifine)"]["mute"]), before)
        field.clearFocus()
        self.win.show_view("live")

    def test_is_text_input(self):
        self.assertTrue(is_text_input(QLineEdit()))
        c = QComboBox()
        self.assertFalse(is_text_input(c))
        c.setEditable(True)
        self.assertTrue(is_text_input(c))
        self.assertFalse(is_text_input(None))


if __name__ == "__main__":
    unittest.main()
