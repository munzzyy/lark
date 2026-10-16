"""The camera watchdog against the fake OBS: bright, dark, fixed, and the Live page that reacts to it."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lark.app import MainWindow, build_context
from lark.config import Config
from lark.preflight import OK, SKIP, WARN, can_go_live
from tests.test_ui_smoke import FakeThread, close_window, pump


class FakeClock:
    t = 1000.0

    def __call__(self):
        return self.t


class WatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        cfg.set("obs_port", cls.ft.port)
        cfg.set("obs_password_source", "custom")
        cfg.set("obs_password", "pw")
        cfg.set("overlay_port", 1024 + (os.getpid() % 30000) + 7)
        cfg.set("camera_module_dir", "/nonexistent")
        cfg.set("twitch_channel", "")
        cls.ctx = build_context(cfg)
        cls.win = MainWindow(cls.ctx, "live")
        cls.ctx.bridge.start()
        cls.win.show()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and s.stream_key_set is not None)
        s.camwatch._timer.stop()
        assert pump(cls.app, lambda: not s.camwatch._busy)
        cls.clock = FakeClock()
        s.camwatch._clock = cls.clock
        s.camwatch.detector.reset()
        s.camwatch._publish()

    @classmethod
    def tearDownClass(cls):
        close_window(cls.app, cls.win)
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def step(self, luma):
        self.ft.fake.camera_luma = luma
        self.clock.t += 3
        got = []
        self.ctx.store.camwatch.sample(got.append)
        self.assertTrue(pump(self.app, lambda: bool(got)))
        self.app.processEvents()
        return got[0]

    def checks(self):
        return {c.key: c for c in self.win.pages["live"].checks()}

    def toasts(self):
        return [i["text"] for i in self.win.toast.items]

    def recorder(self, recovers):
        calls = []
        cam = self.ctx.camera

        def reassert(hold_stream=False, preset=""):
            calls.append(("reassert", hold_stream, preset))
            if recovers:
                self.ft.fake.camera_luma = 100

        cam.reassert_exposure = reassert
        cam.apply = lambda params, hold_stream=False: calls.append(("apply", params, hold_stream))
        return calls

    def run_fix(self):
        live = self.win.pages["live"]
        live.fixer._timer.setInterval(30)
        self.win.sticky.button.click()
        self.assertTrue(pump(self.app, lambda: not live.fixer.busy))

    def test_1_bright_then_dark_then_fixed(self):
        s, live, f = self.ctx.store, self.win.pages["live"], self.ft.fake
        for _ in range(5):
            self.assertAlmostEqual(self.step(100), 100, delta=1)
        self.assertFalse(s.cam_dark)
        self.assertEqual(self.checks()["light"].status, OK)
        header = [w for w in self.win.findChildren(type(live.headline)) if w.text().startswith("Light")]
        self.assertEqual([w.text() for w in header], ["Light ok"])
        shot = [d for t, d in f.requests if t == "GetSourceScreenshot"][-1]
        self.assertEqual(shot, {"sourceName": "Camera", "imageFormat": "png", "imageWidth": 160})

        s.start_stream()
        self.assertTrue(pump(self.app, lambda: s.stream["active"]))
        for _ in range(3):
            self.step(16)
        self.assertFalse(s.cam_dark)
        self.assertFalse(self.win.sticky.isVisible())
        self.step(16)
        self.assertTrue(s.cam_dark)
        self.assertEqual(self.checks()["light"].status, WARN)
        self.assertTrue(can_go_live(live.checks()))
        self.assertTrue(self.win.sticky.isVisible())
        self.assertEqual(self.win.sticky.text.text(), "Camera went dark")
        self.assertEqual(self.win.sticky.button.text(), "Fix")
        self.assertEqual([w.text() for w in self.win.findChildren(type(live.headline)) if w.text().startswith("Light")],
                         ["Light dim"])

        calls = self.recorder(recovers=False)
        self.run_fix()
        self.assertEqual(calls, [("reassert", False, "1")])
        self.assertTrue(s.cam_dark)
        self.assertIn("Camera is still dark, brightness 16", self.toasts())
        self.assertEqual(self.win.sticky.text.text(), "Camera is still dark")
        self.assertEqual(self.win.sticky.button.text(), "Try again")

        calls = self.recorder(recovers=True)
        self.run_fix()
        self.assertEqual(calls, [("reassert", False, "1")])
        self.assertFalse(s.cam_dark)
        self.assertIn("Camera is back to normal, brightness 100", self.toasts())
        self.assertFalse(self.win.sticky.isVisible())
        self.assertEqual(self.checks()["light"].status, OK)

        s.stop_stream()
        pump(self.app, lambda: not s.stream["active"])

    def test_2_fix_is_not_offered_when_not_live_but_the_row_still_warns(self):
        s, live = self.ctx.store, self.win.pages["live"]
        for _ in range(4):
            self.step(100)
        for _ in range(4):
            self.step(16)
        self.assertTrue(s.cam_dark)
        live.refresh()
        self.assertFalse(self.win.sticky.isVisible())
        self.assertTrue(live.rows[5].fix.isVisibleTo(live))
        self.assertEqual(live.rows[5].title.text(), "Camera image looks dark")
        calls = self.recorder(recovers=True)
        live.rows[5].fix.click()
        self.assertEqual(calls, [("reassert", True, "1")])
        self.assertTrue(pump(self.app, lambda: not live.fixer.busy))
        self.assertFalse(s.cam_dark)

    def test_3_settings_save_the_input_and_threshold(self):
        st, cfg = self.win.pages["settings"], self.ctx.config
        st.cam_input.setText("Cam2")
        st.dark.setValue(30)
        st.save()
        self.assertEqual((cfg.get("camera_input"), cfg.get("camera_dark_threshold")), ("Cam2", 30))
        st.cam_input.setText("   ")
        st.save()
        self.assertEqual(cfg.get("camera_input"), "Cam2")

    def test_4_missing_input_is_quiet(self):
        s = self.ctx.store
        self.assertEqual(self.ctx.config.get("camera_input"), "Cam2")
        s.camwatch.start()
        s.camwatch._timer.stop()
        self.assertTrue(pump(self.app, lambda: not s.camwatch._busy))
        self.app.processEvents()
        self.assertIsNone(s.cam_luma)
        self.assertFalse(s.cam_dark)
        self.assertEqual(self.checks()["light"].status, SKIP)
        self.assertFalse(any("Cam2" in t or "screenshot" in t.lower() for t in self.toasts()))
        self.assertEqual([w for w in self.win.findChildren(type(self.win.pages["live"].headline))
                          if w.text().startswith("Light") and w.isVisible()], [])


if __name__ == "__main__":
    unittest.main()
