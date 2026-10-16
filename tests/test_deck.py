import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LARK_PAD_DEVICE"] = "/nonexistent/js0"

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lark import deck
from lark.app import build_context
from lark.config import Config
from lark.deck import DeckActions, PadParser, PadReader
from lark.pages.deck import ButtonEditor, DeckPage, PadMapDialog
from tests.test_ui_smoke import FakeThread, pump


def ev(value, etype, number, t=0):
    return struct.pack("IhBB", t, value, etype, number)


class PadParserTests(unittest.TestCase):
    def test_button_press_only_fires_on_press(self):
        p = PadParser()
        self.assertEqual(p.feed(ev(1, 1, 0) + ev(0, 1, 0)), ["A"])

    def test_recorded_sequence(self):
        data = (ev(1, 1, 2, 100) + ev(0, 1, 2, 180) + ev(1, 1, 7, 900) + ev(0, 1, 7, 950)
                + ev(1, 1, 4, 1200) + ev(1, 1, 5, 1300))
        self.assertEqual(PadParser().feed(data), ["X", "Start", "LB", "RB"])

    def test_init_events_are_ignored(self):
        data = ev(1, 0x81, 0) + ev(0, 0x81, 1) + ev(-32767, 0x82, 6)
        self.assertEqual(PadParser().feed(data), [])

    def test_event_split_across_reads(self):
        p = PadParser()
        raw = ev(1, 1, 1)
        self.assertEqual(p.feed(raw[:3]), [])
        self.assertEqual(p.feed(raw[3:]), ["B"])

    def test_unknown_button_and_stick_axes_do_nothing(self):
        self.assertEqual(PadParser().feed(ev(1, 1, 40) + ev(30000, 2, 0) + ev(-30000, 2, 3)), [])

    def test_dpad_directions_fire_once_per_push(self):
        p = PadParser()
        data = (ev(-32767, 2, 6) + ev(0, 2, 6) + ev(32767, 2, 6) + ev(0, 2, 6)
                + ev(-32767, 2, 7) + ev(-32767, 2, 7) + ev(0, 2, 7) + ev(32767, 2, 7))
        self.assertEqual(p.feed(data), ["DLeft", "DRight", "DUp", "DDown"])

    def test_dpad_held_through_init_does_not_fire(self):
        p = PadParser()
        self.assertEqual(p.feed(ev(-32767, 0x82, 6) + ev(-32767, 2, 6)), [])

    def test_garbage_length_keeps_the_tail(self):
        p = PadParser()
        self.assertEqual(p.feed(ev(1, 1, 3) + b"\x01\x02"), ["Y"])
        self.assertEqual(p.feed(b"\x00" * 6), [])


class PadReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_absent_device_reports_no_pad(self):
        r = PadReader("/nonexistent/js9", retry_s=0.05)
        r.start()
        try:
            QTest.qWait(150)
            self.assertEqual(r.state, "no pad")
        finally:
            r.stop()

    def test_reads_events_from_a_device_file(self):
        d = tempfile.mkdtemp()
        fifo = os.path.join(d, "js0")
        os.mkfifo(fifo)
        got, states = [], []
        r = PadReader(fifo, retry_s=0.05)
        r.pressed.connect(got.append)
        r.status.connect(states.append)
        r.start()
        try:
            self.assertTrue(pump(self.app, lambda: "pad connected" in states))
            w = os.open(fifo, os.O_WRONLY)
            os.write(w, ev(1, 1, 0) + ev(0, 1, 0) + ev(1, 1, 3))
            self.assertTrue(pump(self.app, lambda: got == ["A", "Y"]), got)
            os.close(w)
            self.assertTrue(pump(self.app, lambda: states[-1] == "no pad" or "pad connected" in states[1:]))
        finally:
            r.stop()


class DeckOverObsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        cls.cfgpath = Path(tempfile.mkdtemp()) / "config.json"
        cfg = Config(cls.cfgpath)
        for k, v in (("obs_port", cls.ft.port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", ""),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 6)):
            cfg.set(k, v)
        cls.ctx = build_context(cfg)
        cls.starts = []
        cls.ctx.services = SimpleNamespace(set_starting=lambda t, m: cls.starts.append((t, m)),
                                           twitch_state={"status": "off", "channel": None})
        cls.ctx.bridge.start()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and s.scenes and "Desktop Audio" in s.inputs
                    and "Mic (Fifine)" in s.inputs and s.stream_key_set is not None)
        cls.page = DeckPage(cls.ctx)

    @classmethod
    def tearDownClass(cls):
        cls.page.pad.stop()
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def setUp(self):
        self.mark = len(self.ft.fake.requests)
        self.starts.clear()

    def sent(self):
        return [t for t, _ in self.ft.fake.requests[self.mark:]]

    def test_keys_are_scenes_then_fixed_actions_without_alert(self):
        keys = self.page.actions.keys()
        self.assertEqual(keys[:4], ["scene:Game", "scene:Starting Soon", "scene:BRB", "scene:Camera full"])
        self.assertEqual(keys[4:10], ["mic", "desktop", "replay", "live", "record", "countdown"])
        self.assertTrue(all(k.startswith("frame:") for k in keys[10:]))
        self.assertEqual(list(self.page.buttons), keys)

    def test_clicking_a_scene_button_switches_scene(self):
        QTest.mouseClick(self.page.buttons["scene:BRB"], Qt.LeftButton)
        self.assertTrue(pump(self.app, lambda: self.ft.fake.current == "BRB"))
        self.assertIn("SetCurrentProgramScene", self.sent())
        self.ctx.store.switch_scene("Game")
        self.assertTrue(pump(self.app, lambda: self.ft.fake.current == "Game"))

    def test_mic_and_desktop_mute(self):
        f = self.ft.fake
        self.page.actions.run("mic")
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] is True))
        self.page.actions.run("mic")
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] is False))
        self.page.actions.run("desktop")
        self.assertTrue(pump(self.app, lambda: f.inputs["Desktop Audio"]["mute"] is True))
        self.page.actions.run("desktop")
        self.assertTrue(pump(self.app, lambda: f.inputs["Desktop Audio"]["mute"] is False))

    def test_replay_starts_the_buffer_then_saves(self):
        f = self.ft.fake
        self.page.actions.run("replay")
        self.assertTrue(pump(self.app, lambda: f.replay and self.ctx.store.replay))
        self.page.actions.run("replay")
        self.assertTrue(pump(self.app, lambda: "SaveReplayBuffer" in self.sent()))
        self.ctx.store.bridge.request("StopReplayBuffer", None)
        self.assertTrue(pump(self.app, lambda: not f.replay))

    def test_record_toggles(self):
        f = self.ft.fake
        self.page.actions.run("record")
        self.assertTrue(pump(self.app, lambda: f.recording and self.ctx.store.record["active"]))
        self.page.actions.run("record")
        self.assertTrue(pump(self.app, lambda: not f.recording))

    def test_go_live_obeys_preflight(self):
        f = self.ft.fake
        with mock.patch.object(deck, "can_go_live", lambda checks: False):
            self.page.actions.run("live")
        self.app.processEvents()
        self.assertNotIn("StartStream", self.sent())
        with mock.patch.object(deck, "can_go_live", lambda checks: True):
            self.page.actions.run("live")
        self.assertTrue(pump(self.app, lambda: f.streaming and self.ctx.store.stream["active"]))

    def test_stop_needs_two_presses_inside_the_window(self):
        f = self.ft.fake
        now = [100.0]
        actions = DeckActions(self.ctx, clock=lambda: now[0])
        if not f.streaming:
            self.ctx.store.start_stream()
        self.assertTrue(pump(self.app, lambda: f.streaming))
        self.assertTrue(pump(self.app, lambda: self.ctx.store.stream["active"]))
        actions.run("live")
        self.app.processEvents()
        self.assertTrue(f.streaming)
        now[0] += 10
        actions.run("live")
        self.app.processEvents()
        self.assertTrue(f.streaming, "a late second press must only re-arm")
        now[0] += 1
        actions.run("live")
        self.assertTrue(pump(self.app, lambda: not f.streaming))
        self.assertTrue(pump(self.app, lambda: not self.ctx.store.stream["active"]))

    def test_countdown_uses_title_and_minutes(self):
        self.ctx.config.set("deck_countdown_minutes", 7)
        self.page.actions.run("countdown")
        self.assertEqual(self.starts, [(self.ctx.config.get("starting_title"), 7)])
        self.ctx.config.set("deck_countdown_minutes", 5)

    def test_alert_button_appears_only_when_alerts_exist(self):
        calls = []
        self.ctx.alerts = SimpleNamespace(test_alert=lambda: calls.append(1))
        try:
            self.page.refresh()
            self.assertIn("alert", self.page.buttons)
            self.page.actions.run("alert")
            self.assertEqual(calls, [1])
        finally:
            del self.ctx.alerts
        self.page.refresh()
        self.assertNotIn("alert", self.page.buttons)

    def test_alert_button_uses_services_test_alert(self):
        calls = []
        self.ctx.services.test_alert = lambda kind: calls.append(kind)
        try:
            self.page.refresh()
            self.assertIn("alert", self.page.buttons)
            self.page.actions.run("alert")
            self.assertEqual(calls, ["follow"])
        finally:
            del self.ctx.services.test_alert
        self.page.refresh()
        self.assertNotIn("alert", self.page.buttons)

    def test_actions_refuse_when_obs_is_gone(self):
        toasts = []
        self.ctx.store.toast.connect(lambda m, bad: toasts.append((m, bad)))
        with mock.patch.object(type(self.ctx.store), "connected", new_callable=mock.PropertyMock, return_value=False):
            self.page.actions.run("record")
        self.assertEqual(toasts[-1], ("OBS is not connected", True))
        self.assertNotIn("StartRecord", self.sent())

    def test_label_and_colour_overrides_show_and_persist(self):
        self.ctx.config.set("deck_style", {"scene:BRB": {"label": "Back soon", "color": "#112233"}})
        self.page._repaint()
        b = self.page.buttons["scene:BRB"]
        self.assertTrue(b.text().startswith("Back soon"))
        self.assertIn("#112233", b.styleSheet())
        self.assertEqual(self.page.actions.look("scene:Game", self.ctx.config.get("deck_style"))[0], "Game")
        self.page._save("deck_style", dict(self.ctx.config.get("deck_style")))
        self.assertIn("Back soon", self.cfgpath.read_text())
        self.ctx.config.set("deck_style", {})
        self.page._repaint()

    def test_editor_result_is_minimal(self):
        ed = ButtonEditor(None, "mic", "Mic mute", "#8c3b4a", "Mic mute", "#8c3b4a")
        self.assertIsNone(ed.result_style())
        ed.name.setText("Mute me")
        self.assertEqual(ed.result_style(), {"label": "Mute me"})
        ed.color = "#000000"
        self.assertEqual(ed.result_style(), {"label": "Mute me", "color": "#000000"})
        ed._reset()
        self.assertIsNone(ed.result_style())

    def test_edit_mode_click_edits_instead_of_firing(self):
        self.page.edit.setChecked(True)
        seen = []
        with mock.patch.object(DeckPage, "edit_button", lambda self, k: seen.append(k)):
            QTest.mouseClick(self.page.buttons["scene:Game"], Qt.LeftButton)
        self.page.edit.setChecked(False)
        self.assertEqual(seen, ["scene:Game"])
        self.assertNotIn("SetCurrentProgramScene", self.sent())

    def test_pad_mapping_dialog_round_trip(self):
        choices = [(k, k) for k in self.page.actions.keys()]
        dlg = PadMapDialog(None, choices, {"A": "mic"}, False)
        self.assertEqual(dlg.combos["A"].currentData(), "mic")
        dlg.combos["B"].setCurrentIndex(dlg.combos["B"].findData("scene:BRB"))
        dlg.background.setChecked(True)
        self.assertEqual(dlg.mapping(), {"A": "mic", "B": "scene:BRB"})
        self.assertTrue(dlg.background.isChecked())

    def test_pad_ignored_when_lark_is_not_focused(self):
        f = self.ft.fake
        self.ctx.config.set("deck_pad_map", {"A": "scene:BRB"})
        self.ctx.config.set("deck_pad_background", False)
        self.page.focus_check = lambda: False
        self.page.on_pad("A")
        self.app.processEvents()
        QTest.qWait(150)
        self.assertNotEqual(f.current, "BRB")
        self.assertNotIn("SetCurrentProgramScene", self.sent())
        self.page.focus_check = lambda: True
        self.page.on_pad("A")
        self.assertTrue(pump(self.app, lambda: f.current == "BRB"))
        self.ctx.store.switch_scene("Game")
        self.assertTrue(pump(self.app, lambda: f.current == "Game"))
        self.page.focus_check = lambda: False
        self.ctx.config.set("deck_pad_background", True)
        self.page.on_pad("A")
        self.assertTrue(pump(self.app, lambda: f.current == "BRB"), "background mode must fire unfocused")
        self.ctx.store.switch_scene("Game")
        self.assertTrue(pump(self.app, lambda: f.current == "Game"))
        self.ctx.config.set("deck_pad_background", False)
        self.ctx.config.set("deck_pad_map", {})
        self.page.focus_check = lambda: True

    def test_pad_ignored_while_a_dialog_is_open_and_for_unmapped_or_stale_keys(self):
        self.ctx.config.set("deck_pad_map", {"A": "scene:BRB", "B": "scene:Gone"})
        self.page.focus_check = lambda: True
        self.page.dialog_open = True
        self.page.on_pad("A")
        self.page.dialog_open = False
        self.page.on_pad("B")
        self.page.on_pad("X")
        self.app.processEvents()
        QTest.qWait(150)
        self.assertNotIn("SetCurrentProgramScene", self.sent())
        self.ctx.config.set("deck_pad_map", {})

    def test_pad_pill_follows_status(self):
        self.page._pad_status("pad connected")
        self.assertEqual(self.page.pad_pill.text(), "pad connected")
        self.page._pad_status("no pad")
        self.assertEqual(self.page.pad_pill.text(), "no pad")


class DeckConfigTests(unittest.TestCase):
    def cfg(self):
        return Config(Path(tempfile.mkdtemp()) / "config.json")

    def test_defaults(self):
        c = self.cfg()
        self.assertEqual(c.get("deck_style"), {})
        self.assertFalse(c.get("deck_pad_background"))
        self.assertEqual(c.get("desktop_input"), "Desktop Audio")

    def test_bad_values_are_rejected(self):
        c = self.cfg()
        for key, bad in (("deck_style", {"mic": {"color": "red"}}), ("deck_style", {"mic": {"label": "x" * 41}}),
                         ("deck_style", {"mic": {"evil": 1}}), ("deck_style", [1]),
                         ("deck_pad_map", {"A": 5}), ("deck_pad_background", "yes"),
                         ("deck_countdown_minutes", 0), ("deck_countdown_minutes", True)):
            with self.assertRaises(ValueError, msg=f"{key}={bad}"):
                c.set(key, bad)

    def test_round_trip_through_the_file(self):
        c = self.cfg()
        c.set("deck_style", {"mic": {"label": "Mute", "color": "#abcdef"}})
        c.set("deck_pad_map", {"A": "scene:Game"})
        c.set("deck_pad_background", True)
        c.save()
        d = Config(c.path)
        self.assertEqual(d.get("deck_style"), {"mic": {"label": "Mute", "color": "#abcdef"}})
        self.assertEqual(d.get("deck_pad_map"), {"A": "scene:Game"})
        self.assertTrue(d.get("deck_pad_background"))


if __name__ == "__main__":
    unittest.main()
