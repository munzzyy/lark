"""Toast queue, status header state, Go Live helpers, remembered layout and the shell around the pages."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from lark.app import VIEWS, MainWindow, build_context
from lark.config import Config
from lark.golive import EDIT_TITLE, OPEN, PICK_SCENE, UNMUTE, Countdown, fix_action
from lark.onboarding import pending_steps
from lark.preflight import FAIL, OK, SKIP, WARN, Check
from lark.shell import (RAIL_COLLAPSE_BELOW, header_state, rail_is_collapsed, restore_view, save_state,
                        window_title)
from lark.viewers import fetch_viewers
from lark.widgets import EmptyState, Toast
from tests.test_ui_smoke import FakeThread, pump


def app():
    return QApplication.instance() or QApplication([])


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ToastTests(unittest.TestCase):
    def setUp(self):
        app()
        self.clock = FakeClock()
        self.parent = QWidget()
        self.parent.resize(800, 600)
        self.toast = Toast(self.parent, clock=self.clock)

    def texts(self):
        return [i["text"] for i in self.toast.items]

    def test_shows_and_expires(self):
        self.toast.show_message("Saved")
        self.assertEqual(self.texts(), ["Saved"])
        self.clock.t += 2.9
        self.toast.tick()
        self.assertEqual(self.texts(), ["Saved"])
        self.clock.t += 0.2
        self.toast.tick()
        self.assertEqual(self.texts(), [])
        self.assertTrue(self.toast.isHidden())

    def test_errors_stay_longer(self):
        self.toast.show_message("Nope", True)
        self.clock.t += 4.0
        self.toast.tick()
        self.assertEqual(self.texts(), ["Nope"])
        self.clock.t += 1.1
        self.toast.tick()
        self.assertEqual(self.texts(), [])

    def test_queue_caps_visible_and_drains_in_order(self):
        for n in range(5):
            self.toast.show_message(f"m{n}")
        self.assertEqual(self.texts(), ["m0", "m1", "m2"])
        self.assertEqual([t for t, _ in self.toast.pending], ["m3", "m4"])
        self.clock.t += 3.1
        self.toast.tick()
        self.assertEqual(self.texts(), ["m3", "m4"])
        self.assertEqual(len(self.toast.pending), 0)

    def test_repeat_message_extends_instead_of_stacking(self):
        self.toast.show_message("Replay saved")
        self.clock.t += 2.0
        self.toast.show_message("Replay saved")
        self.assertEqual(self.texts(), ["Replay saved"])
        self.clock.t += 2.0
        self.toast.tick()
        self.assertEqual(self.texts(), ["Replay saved"])

    def test_repeat_while_pending_is_dropped(self):
        for n in range(3):
            self.toast.show_message(f"m{n}")
        self.toast.show_message("x")
        self.toast.show_message("x")
        self.assertEqual(len(self.toast.pending), 1)

    def test_never_takes_focus_or_clicks(self):
        self.toast.show_message("hi")
        self.assertEqual(self.toast.focusPolicy(), Qt.NoFocus)
        self.assertTrue(self.toast.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertTrue(self.toast.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_sits_at_the_bottom_centre_and_follows_resize(self):
        self.parent.show()
        self.toast.show_message("hi")
        self.assertEqual(self.toast.geometry().bottom() + 24 + 1, self.parent.height())
        self.parent.resize(500, 400)
        app().processEvents()
        self.assertEqual(self.toast.geometry().bottom() + 24 + 1, 400)
        self.parent.close()


class EmptyStateTests(unittest.TestCase):
    def test_action_fires_and_text_hides_when_empty(self):
        app()
        hits = []
        e = EmptyState("Nothing", "Do a thing", "Do it", lambda: hits.append(1))
        e.button.click()
        self.assertEqual(hits, [1])
        self.assertIn("Do a thing", e.text())
        e.set_content("Plain")
        self.assertTrue(e.button.isHidden())
        self.assertTrue(e.text_label.isHidden())


class HeaderStateTests(unittest.TestCase):
    BASE = dict(conn="connected", scene="Game", stream={"active": False, "reconnecting": False, "duration_ms": 0},
                inputs={"Mic": {"muted": False}}, mic_name="Mic", twitch_connected=False, viewers=None)

    def st(self, **kw):
        return header_state(**{**self.BASE, **kw})

    def test_camera_mode_is_carried_for_the_header(self):
        self.assertEqual(self.st()["camera_mode"], "")
        self.assertEqual(self.st(mode="Quality (4K30)")["camera_mode"], "Quality (4K30)")

    def test_connected_idle(self):
        s = self.st()
        self.assertEqual((s["dot"], s["conn"], s["scene"], s["live"]), ("good", "OBS connected", "Game", False))
        self.assertEqual(s["clock"], "")
        self.assertEqual(s["mic"], ("good", "Mic on"))
        self.assertIsNone(s["viewers"])

    def test_disconnected_hides_scene_and_mic(self):
        for conn, dot in (("disconnected", "bad"), ("connecting", "warn"), ("auth_failed", "bad")):
            s = self.st(conn=conn)
            self.assertEqual(s["dot"], dot)
            self.assertEqual(s["scene"], "")
            self.assertEqual(s["mic"][1], "Mic --")
            self.assertFalse(s["mic_togglable"])

    def test_live_timer_and_title(self):
        s = self.st(stream={"active": True, "reconnecting": False, "duration_ms": 3_725_000})
        self.assertEqual(s["clock"], "1:02:05")
        self.assertEqual(window_title(s), "Lark - LIVE 1:02:05")
        r = self.st(stream={"active": True, "reconnecting": True, "duration_ms": 5000})
        self.assertIn("RECONNECTING", window_title(r))
        self.assertEqual(window_title(self.st()), "Lark")

    def test_stale_stream_flag_while_disconnected_is_not_live(self):
        s = self.st(conn="disconnected", stream={"active": True, "reconnecting": False, "duration_ms": 9000})
        self.assertFalse(s["live"])
        self.assertEqual(window_title(s), "Lark")

    def test_viewers_only_when_signed_in(self):
        self.assertIsNone(self.st(viewers=12)["viewers"])
        s = self.st(viewers=12, twitch_connected=True)
        self.assertEqual((s["viewers"], s["twitch"]), (12, True))
        self.assertIsNone(self.st(viewers=None, twitch_connected=True)["viewers"])

    def test_mic_states(self):
        self.assertEqual(self.st(inputs={"Mic": {"muted": True}})["mic"], ("bad", "Mic muted"))
        s = self.st(inputs={})
        self.assertEqual(s["mic"], ("warn", "No mic"))
        self.assertFalse(s["mic_togglable"])


class FetchViewersTests(unittest.TestCase):
    class Client:
        state = {"user_id": "42"}

        def __init__(self, body):
            self.body, self.calls = body, []

        async def _helix(self, method, path, **kw):
            self.calls.append((method, path, kw))
            return self.body

    def run_it(self, body):
        c = self.Client(body)
        return asyncio.run(fetch_viewers(c)), c

    def test_live_offline_and_garbage(self):
        n, c = self.run_it({"data": [{"viewer_count": 17}]})
        self.assertEqual(n, 17)
        self.assertEqual(c.calls[0][:2], ("GET", "/streams"))
        self.assertEqual(c.calls[0][2]["params"]["user_id"], "42")
        self.assertIsNone(self.run_it({"data": []})[0])
        self.assertIsNone(self.run_it({})[0])
        self.assertIsNone(self.run_it({"data": [{"viewer_count": -3}]})[0])
        self.assertIsNone(self.run_it({"data": [{"viewer_count": True}]})[0])
        self.assertIsNone(self.run_it({"data": [{"viewer_count": "9"}]})[0])


class GoLiveTests(unittest.TestCase):
    def test_fix_actions(self):
        c = lambda k, st: Check(k, k, st)
        self.assertIsNone(fix_action(c("obs", OK)))
        self.assertEqual(fix_action(c("obs", FAIL))[1:], (OPEN, "settings"))
        self.assertEqual(fix_action(c("mic", WARN), mic_muted=True)[1], UNMUTE)
        self.assertEqual(fix_action(c("mic", WARN), mic_muted=False)[1:], (OPEN, "audio"))
        self.assertEqual(fix_action(c("scene", FAIL), scenes=["A"])[1], PICK_SCENE)
        self.assertEqual(fix_action(c("scene", FAIL), scenes=[])[1:], (OPEN, "scenes"))
        self.assertIsNone(fix_action(c("scene", SKIP)))
        self.assertIsNone(fix_action(c("mic", SKIP)))
        self.assertEqual(fix_action(c("title", WARN))[1], EDIT_TITLE)
        self.assertEqual(fix_action(c("title", SKIP))[1:], (OPEN, "settings"))
        self.assertIsNone(fix_action(c("key", FAIL)))
        self.assertIsNone(fix_action(c("camera", SKIP)))
        self.assertEqual(fix_action(c("camera", WARN))[1:], (OPEN, "camera"))

    def test_countdown_runs_then_fires_once(self):
        app()
        cd = Countdown(3)
        seen, done = [], []
        cd.ticked.connect(seen.append)
        cd.finished.connect(lambda: done.append(1))
        cd.start()
        cd.start()
        for _ in range(5):
            cd.tick()
        self.assertEqual(seen, [3, 2, 1])
        self.assertEqual(done, [1])
        self.assertFalse(cd.running)

    def test_countdown_cancel_never_fires(self):
        app()
        cd = Countdown(3)
        done, cancelled = [], []
        cd.finished.connect(lambda: done.append(1))
        cd.cancelled.connect(lambda: cancelled.append(1))
        cd.cancel()
        self.assertEqual(cancelled, [])
        cd.start()
        cd.tick()
        cd.cancel()
        cd.tick()
        cd.tick()
        self.assertEqual((done, cancelled, cd.running), ([], [1], False))
        cd.start()
        for _ in range(3):
            cd.tick()
        self.assertEqual(done, [1])

    def test_onboarding_order(self):
        self.assertEqual(pending_steps(connected=False, mic_ok=False, camera_ok=False, twitch_ok=False),
                         ["obs", "mic", "camera", "twitch"])
        self.assertEqual(pending_steps(connected=True, mic_ok=True, camera_ok=True, twitch_ok=False), ["twitch"])
        self.assertEqual(pending_steps(connected=True, mic_ok=True, camera_ok=True, twitch_ok=True), [])


class LayoutPersistenceTests(unittest.TestCase):
    def cfg(self):
        return Config(Path(tempfile.mkdtemp()) / "config.json")

    def test_size_and_page_round_trip(self):
        c = self.cfg()
        self.assertTrue(save_state(c, page="audio", size=(1366, 768)))
        c2 = Config(c.path)
        self.assertEqual((c2.get("last_page"), c2.get("window_size")), ("audio", [1366, 768]))
        self.assertFalse(save_state(c2, page="audio", size=(1366, 768)))

    def test_bad_values_fall_back_to_defaults(self):
        c = self.cfg()
        c.path.write_text('{"window_size": [10, 99999], "last_page": "../etc", "onboarding_done": "yes"}')
        with self.assertLogs("lark.config", "WARNING"):
            c.load()
        self.assertEqual((c.get("window_size"), c.get("last_page"), c.get("onboarding_done")), (None, "", False))
        c.path.write_text('{"window_size": [true, 800]}')
        with self.assertLogs("lark.config", "WARNING"):
            c.load()
        self.assertIsNone(c.get("window_size"))

    def test_absurd_size_is_not_saved(self):
        c = self.cfg()
        with self.assertLogs("lark.shell", "WARNING"):
            self.assertFalse(save_state(c, size=(50, 50)))
        self.assertIsNone(c.get("window_size"))
        self.assertFalse(c.path.exists())

    def test_unwritable_config_logs_instead_of_raising(self):
        c = self.cfg()
        c.path = Path("/proc/lark-nope/config.json")
        with self.assertLogs("lark.shell", "ERROR"):
            save_state(c, page="live")

    def test_restore_view_ignores_unknown_pages(self):
        c = self.cfg()
        self.assertEqual(restore_view(c, VIEWS), "live")
        c.set("last_page", "audio")
        self.assertEqual(restore_view(c, VIEWS), "audio")
        c.set("last_page", "gone")
        self.assertEqual(restore_view(c, VIEWS), "live")

    def test_rail_threshold(self):
        self.assertTrue(rail_is_collapsed(RAIL_COLLAPSE_BELOW - 1))
        self.assertFalse(rail_is_collapsed(RAIL_COLLAPSE_BELOW))
        self.assertFalse(rail_is_collapsed(1600))


class ShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = app()
        cls.ft = FakeThread()
        cls.dir = Path(tempfile.mkdtemp())
        cfg = Config(cls.dir / "config.json")
        for k, v in (("obs_port", cls.ft.port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", ""),
                     ("camera_device", "/nonexistent"),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 5)):
            cfg.set(k, v)
        cls.cfg = cfg
        cls.ctx = build_context(cfg)
        cls.win = MainWindow(cls.ctx, "live")
        cls.win.resize(1280, 720)
        cls.win.show()
        cls.ctx.bridge.start()
        cls.ctx.services.start()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and "Mic (Fifine)" in s.inputs and s.scenes)

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.ctx.services.stop()
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def test_1_header_follows_the_store_and_is_on_every_page(self):
        h = self.win.status_header
        h.refresh()
        self.assertEqual(h.conn.text(), "OBS connected")
        self.assertEqual(h.scene.text(), f"Scene: {self.ctx.store.current_scene}")
        self.assertTrue(h.live.isHidden())
        self.assertTrue(h.view_label.isHidden())
        for v in VIEWS:
            self.win.show_view(v)
            self.assertTrue(h.isVisible(), v)
        self.win.show_view("live")

    def test_2_mic_chip_toggles_mute(self):
        h, f = self.win.status_header, self.ft.fake
        h.mic.click()
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] is True))
        self.assertTrue(pump(self.app, lambda: h.mic.text() == "Mic muted"))
        live = self.win.pages["live"]
        live.refresh()
        mic_row = live.rows[2]
        self.assertEqual(mic_row.fix.text(), "Unmute")
        self.assertFalse(mic_row.fix.isHidden())
        mic_row.fix.click()
        self.assertTrue(pump(self.app, lambda: f.inputs["Mic (Fifine)"]["mute"] is False))
        self.assertTrue(pump(self.app, lambda: h.mic.text() == "Mic on"))

    def test_3_rail_collapses_under_the_threshold(self):
        rail = self.win.findChild(QWidget, "rail")
        self.win.resize(1280, 720)
        self.app.processEvents()
        self.assertEqual(rail.width(), 208)
        self.assertEqual(self.win.buttons["live"].text(), "Live")
        self.win.resize(1000, 700)
        self.app.processEvents()
        self.assertEqual(rail.width(), 68)
        self.assertEqual(self.win.buttons["live"].text(), "")
        self.assertIn("Live", self.win.buttons["live"].toolTip())
        self.win.resize(1280, 720)
        self.app.processEvents()
        self.assertEqual(self.win.buttons["live"].text(), "Live")

    def test_4_page_and_size_are_remembered(self):
        self.win.show_view("audio")
        self.assertEqual(Config(self.cfg.path).get("last_page"), "audio")
        self.win.resize(1111, 777)
        self.win.watcher.remember_size()
        self.assertEqual(Config(self.cfg.path).get("window_size"), [1111, 777])
        self.win.show_view("live")
        self.win.resize(1280, 720)

    def test_5_go_live_countdown_cancel_and_confirmation(self):
        live, s = self.win.pages["live"], self.ctx.store
        self.assertTrue(pump(self.app, lambda: live.go.isEnabled()))
        live.go.click()
        self.assertTrue(live.countdown.running)
        self.assertIn("Going live in 3", live.go.text())
        self.assertEqual(live.hero.property("stream"), "starting")
        live.go.click()
        self.assertFalse(live.countdown.running)
        self.assertEqual(live.go.text(), "Go Live")
        self.assertFalse(s.stream["active"])
        live.go.click()
        for _ in range(live.countdown.seconds):
            live.countdown.tick()
        self.assertTrue(pump(self.app, lambda: s.stream["active"]))
        self.assertTrue(pump(self.app, lambda: live.hero.property("stream") == "live"))
        self.assertTrue(pump(self.app, lambda: self.win.status_header.state["live"]))
        self.assertTrue(pump(self.app, lambda: self.win.windowTitle().startswith("Lark - LIVE")))
        self.assertTrue(any(i["text"] == "You are live" for i in self.win.toast.items))
        live.stop.click()
        live.stop.click()
        self.assertTrue(pump(self.app, lambda: not s.stream["active"]))
        self.assertTrue(pump(self.app, lambda: self.win.windowTitle() == "Lark"))

    def test_6_countdown_cancels_itself_when_preflight_breaks(self):
        live, s = self.win.pages["live"], self.ctx.store
        self.assertTrue(pump(self.app, lambda: live.go.isEnabled()))
        live.go.click()
        self.assertTrue(live.countdown.running)
        s.scenes = []
        s.current_scene = ""
        live.refresh()
        self.assertFalse(live.countdown.running)
        s.refresh_scenes()
        self.assertTrue(pump(self.app, lambda: s.current_scene != ""))

    def test_7_onboarding_card_until_done(self):
        live = self.win.pages["live"]
        ob = live.onboarding
        ob.refresh()
        self.assertFalse(ob.isHidden())
        self.assertIn("Not found", ob.rows["camera"].detail.text())
        ob.hide_btn.click()
        self.assertTrue(ob.isHidden())
        self.assertTrue(Config(self.cfg.path).get("onboarding_done"))

    def test_8_empty_states_say_what_to_do(self):
        sc, au = self.win.pages["scenes"], self.win.pages["audio"]
        self.ctx.store.scenes = []
        sc.rebuild()
        self.assertIn("No scenes", sc.empty.text())
        self.assertFalse(sc.empty.isHidden())
        self.assertEqual(sc.empty.button.text(), "Refresh")
        self.ctx.store.conn = "disconnected"
        sc.rebuild()
        self.assertIn("not connected", sc.empty.text())
        self.assertEqual(sc.empty.button.text(), "Open Settings")
        sc.empty.button.click()
        self.assertIs(self.win.stack.currentWidget(), self.win.pages["settings"])
        au.rebuild()
        self.assertIn("not connected", au.empty.text())
        self.ctx.store.conn = "connected"
        self.ctx.store.refresh_scenes()
        self.win.show_view("live")


if __name__ == "__main__":
    unittest.main()
