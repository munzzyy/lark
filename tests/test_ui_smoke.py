"""Whole app, offscreen, against the fake OBS server: bridge, store and pages together."""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from lark.app import MainWindow, build_context, VIEWS
from lark.config import Config
from tests.fake_obs import FakeObs


class FakeThread:
    def __init__(self):
        self.fake = FakeObs(password="pw", meters=True)
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()
        self.port = asyncio.run_coroutine_threadsafe(self.fake.start(), self.loop).result(5)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.fake.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)
        self.loop.close()


def pump(app, cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def close_window(app, win):
    for _ in range(3):
        app.processEvents()
    for name in ("store", "bridge", "services", "camera"):
        getattr(win.ctx, name).blockSignals(True)
    win.close()
    win.deleteLater()
    end = time.time() + 5
    while shiboken6.isValid(win) and time.time() < end:
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    assert not shiboken6.isValid(win), "MainWindow still alive"


class SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        d = Path(os.environ["LARK_CONFIG_DIR"])
        cfg = Config(d / "config.json")
        cfg.set("obs_port", cls.ft.port)
        cfg.set("obs_password_source", "custom")
        cfg.set("obs_password", "pw")
        cfg.set("overlay_port", 1024 + (os.getpid() % 30000))
        cfg.set("camera_module_dir", "/nonexistent")
        cfg.set("twitch_channel", "")
        cls.ctx = build_context(cfg)
        cls.win = MainWindow(cls.ctx, "live")
        cls.ctx.bridge.start()
        cls.ctx.camera.start()
        cls.ctx.services.start()
        cls.win.show()

    @classmethod
    def tearDownClass(cls):
        close_window(cls.app, cls.win)
        cls.ctx.camera.stop()
        cls.ctx.services.stop()
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def test_1_everything_loads(self):
        s = self.ctx.store
        self.assertTrue(pump(self.app, lambda: s.connected and s.scenes and "Mic (Fifine)" in s.inputs
                             and s.stream_key_set is not None and s.has_signal("Mic (Fifine)") is not None))
        self.assertEqual(s.scenes, ["Game", "Starting Soon", "BRB", "Camera full"])
        self.assertNotIn("Camera", s.inputs)
        self.assertTrue(pump(self.app, lambda: "Game" in s.items))

    def test_2_every_page_shows(self):
        for v in VIEWS:
            self.win.show_view(v)
            self.app.processEvents()
        cam = self.win.pages["camera"]
        self.assertTrue(pump(self.app, lambda: "Camera module not found" in cam.status.text()))

    def test_3_go_live_and_stop_round_trip(self):
        live = self.win.pages["live"]
        s = self.ctx.store
        self.assertTrue(live.go.isEnabled())
        live.go.click()
        self.assertTrue(live.countdown.running)
        live.go.click()
        self.assertFalse(live.countdown.running)
        self.assertFalse(s.stream["active"])
        live.go.click()
        for _ in range(live.countdown.seconds):
            live.countdown.tick()
        self.assertTrue(pump(self.app, lambda: s.stream["active"]))
        self.assertTrue(pump(self.app, lambda: live.stop.isVisibleTo(live)))
        live.stop.click()
        self.assertTrue(live.stop.text().startswith("Click again"))
        live.stop.click()
        self.assertTrue(pump(self.app, lambda: not s.stream["active"]))

    def test_4_scene_switch_toggle_mute_volume(self):
        s, f = self.ctx.store, self.ft.fake
        s.switch_scene("BRB")
        self.assertTrue(pump(self.app, lambda: s.current_scene == "BRB"))
        item = s.items["BRB"][0]
        s.set_item_enabled("BRB", item["id"], False)
        self.assertTrue(pump(self.app, lambda: not s.items["BRB"][0]["enabled"]))
        self.assertFalse(f.scenes["BRB"][0][1])
        row = self.win.pages["audio"].rows["Mic (Fifine)"]
        row.mute.click()
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] is True))
        row.slider.setValue(-300)
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["db"] == -30.0))
        s.switch_scene("Game")
        pump(self.app, lambda: s.current_scene == "Game")

    def test_5_meters_move_the_bar(self):
        row = self.win.pages["audio"].rows["Desktop Audio"]
        self.assertTrue(pump(self.app, lambda: row.meter.level > -60))

    def test_6_add_browser_source(self):
        f = self.ft.fake
        ov = self.win.pages["overlays"]
        self.assertTrue(pump(self.app, lambda: self.ctx.services.port is not None))
        ov._refresh_urls()
        ov.chat_card[0].add.click()
        self.assertTrue(pump(self.app, lambda: any(t == "CreateInput" for t, _ in f.requests)))
        req = [d for t, d in f.requests if t == "CreateInput"][-1]
        self.assertEqual(req["inputKind"], "browser_source")
        self.assertEqual(req["inputSettings"]["url"], f"http://127.0.0.1:{self.ctx.services.port}/chat")

    def test_6b_virtual_camera_button_exists_when_supported(self):
        live = self.win.pages["live"]
        live.refresh()
        self.assertIsNotNone(live.vcam)

    def test_7_settings_reject_bad_values_without_changing_config(self):
        st = self.win.pages["settings"]
        before = dict(self.ctx.config.values)
        st.channel.setText("bad channel!")
        st.save()
        self.assertEqual(self.ctx.config.values, before)
        st.channel.setText("")


class NoVcamTests(unittest.TestCase):
    def test_button_does_not_exist_when_obs_says_604(self):
        app = QApplication.instance() or QApplication([])
        ft = FakeThread()
        ft.fake.vcam_supported = False
        d = tempfile.mkdtemp()
        cfg = Config(Path(d) / "config.json")
        cfg.set("obs_port", ft.port)
        cfg.set("obs_password_source", "custom")
        cfg.set("obs_password", "pw")
        cfg.set("camera_module_dir", "/nonexistent")
        cfg.set("overlay_port", 1024 + (os.getpid() % 30000) + 1)
        cfg.set("twitch_channel", "")
        ctx = build_context(cfg)
        win = MainWindow(ctx, "live")
        ctx.bridge.start()
        try:
            s = ctx.store
            self.assertTrue(pump(app, lambda: s.connected and s.vcam_available is not None))
            win.pages["live"].refresh()
            self.assertIsNone(win.pages["live"].vcam)
            self.assertFalse(any(b.text().endswith("irtual camera") for b in win.pages["live"].findChildren(type(win.pages["live"].rec))))
        finally:
            close_window(app, win)
            ctx.bridge.stop()
            ft.stop()


if __name__ == "__main__":
    unittest.main()
