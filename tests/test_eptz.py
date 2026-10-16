import itertools
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from lark import eptz
from lark.app import build_context
from lark.config import Config
from lark.pages.camera import CameraPage, show_value
from lark.pages.framing import CameraModeCard, FramePad
from tests.test_ui_smoke import FakeThread, pump

SIZES = [(1920, 1080), (3840, 2160), (1280, 720), (2560, 1440), (640, 480), (800, 450)]
GRID = [-1, -0.5, -0.01, 0, 0.3, 1, 7, -9, float("nan"), float("inf"), float("-inf"), None, "x"]


class MathTests(unittest.TestCase):
    def test_crop_never_leaves_the_frame_and_holds_aspect(self):
        for (w, h), z, p, t in itertools.product(SIZES, [0.2, 1, 1.37, 2, 3.99, 4, 9, float("nan")], GRID, GRID):
            c = eptz.frame_crop(w, h, z, p, t)
            self.assertTrue(all(isinstance(v, int) and v >= 0 for v in c.values()), (w, h, z, p, t, c))
            cw, ch = w - c["cropLeft"] - c["cropRight"], h - c["cropTop"] - c["cropBottom"]
            self.assertGreater(cw, 0)
            self.assertLessEqual(cw, w)
            self.assertLessEqual(ch, h)
            self.assertEqual(cw * 9, ch * 16, (w, h, z, p, t, c))

    def test_default_crop_matches_the_obs_facecam(self):
        self.assertEqual(eptz.frame_crop(1920, 1080, 1.0, 0, 0),
                         {"cropLeft": 320, "cropRight": 320, "cropTop": 180, "cropBottom": 180})
        self.assertEqual(eptz.frame_crop(3840, 2160, 1.0, 0, 0),
                         {"cropLeft": 640, "cropRight": 640, "cropTop": 360, "cropBottom": 360})

    def test_extremes_and_zoom_four(self):
        c = eptz.frame_crop(1920, 1080, 4.0, -1, 1)
        self.assertEqual(c, {"cropLeft": 0, "cropRight": 1600, "cropTop": 900, "cropBottom": 0})
        c = eptz.frame_crop(1920, 1080, 1.0, 1, -1)
        self.assertEqual((c["cropLeft"], c["cropRight"], c["cropTop"], c["cropBottom"]), (640, 0, 0, 360))

    def test_crop_scales_by_two_for_4k(self):
        a = eptz.frame_crop(1920, 1080, 2.0, 0.4, -0.2)
        b = eptz.frame_crop(3840, 2160, 2.0, 0.4, -0.2)
        for k in a:
            self.assertLessEqual(abs(b[k] - 2 * a[k]), 2)

    def test_infer_round_trips(self):
        for (w, h), z, p, t in itertools.product(SIZES[:2], [1.0, 1.5, 2.0, 3.2, 4.0], [-1, -0.4, 0, 0.7, 1], [-1, 0, 0.5]):
            got = eptz.infer_framing(w, h, eptz.frame_crop(w, h, z, p, t))
            self.assertAlmostEqual(got[0], z, delta=0.06)
            self.assertAlmostEqual(got[1], p, delta=0.03)
            self.assertAlmostEqual(got[2], t, delta=0.03)

    def test_infer_rejects_nonsense(self):
        for crop in ({}, {"cropLeft": 1}, {"cropLeft": -5, "cropRight": 0, "cropTop": 0, "cropBottom": 0},
                     {"cropLeft": 1000, "cropRight": 1000, "cropTop": 0, "cropBottom": 0}):
            self.assertIsNone(eptz.infer_framing(1920, 1080, crop))

    def test_zoom_table_matches_the_fork(self):
        for v, want in ((0, 1.0), (165, 1.5), (170, 1.6), (260, 2.0), (320, 2.4), (400, 3.2)):
            self.assertAlmostEqual(eptz.zoom_factor(v), want, places=6)
        self.assertGreater(eptz.zoom_factor(100), 1.0)
        self.assertLess(eptz.zoom_factor(100), 1.5)
        self.assertEqual(eptz.zoom_factor(-50), 1.0)
        self.assertEqual(eptz.zoom_factor(9999), 3.2)
        for bad in (None, "x", float("nan"), float("inf")):
            self.assertTrue(math.isfinite(eptz.zoom_factor(bad)))
        self.assertEqual(show_value("zoom_absolute", 260), "260  2.0x")
        self.assertEqual(show_value("brightness", 5), "5")

    def test_unpack_res(self):
        self.assertEqual(eptz.unpack_res(125830200), (1920, 1080))
        self.assertEqual(eptz.unpack_res(251660400), (3840, 2160))
        self.assertEqual(eptz.unpack_res("1920x1080"), (1920, 1080))
        for bad in (None, 0, -1, True, "auto", "ax1", 7):
            self.assertIsNone(eptz.unpack_res(bad))

    def test_ease_is_bounded_and_monotonic(self):
        xs = [i / 50 for i in range(-5, 60)]
        ys = [eptz.ease(x) for x in xs]
        self.assertEqual(ys, sorted(ys))
        self.assertEqual((eptz.ease(0), eptz.ease(1)), (0, 1))


class ObsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        cls.cfgpath = Path(tempfile.mkdtemp()) / "config.json"
        cfg = Config(cls.cfgpath)
        for k, v in (("obs_port", cls.ft.port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", ""),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 11)):
            cfg.set(k, v)
        cls.ctx = build_context(cfg)
        cls.ctx.bridge.start()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and s.scenes and s.current_scene)
        cls.e = cls.ctx.eptz
        cls.toasts = []
        s.toast.connect(lambda m, bad: cls.toasts.append((m, bad)))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def setUp(self):
        f = self.ft.fake
        f.transforms.clear()
        f.inputs["Camera"]["settings"].update(resolution=125830200, framerate=65596)
        f.current = "Game"
        self.ctx.store.stream["active"] = False
        self.ctx.store.record["active"] = False
        self.ctx.store.current_scene = "Game"
        self.toasts.clear()
        self.now = [100.0]
        self.e.clock = lambda: self.now[0]
        self.ctx.config.values["eptz_duration_ms"] = 400
        self.ctx.config.values["eptz_presets"] = {}
        self.e._confirm = None
        self.e.refresh()
        self.settle()
        self.assertTrue(self.e.ready, self.e.status)
        self.mark = len(f.requests)

    def settle(self, extra=0.15):
        pump(self.app, lambda: self.e.ready, 3)
        pump(self.app, lambda: False, extra)

    def sent(self, kind):
        return [d for t, d in self.ft.fake.requests[self.mark:] if t == kind]

    def crop(self):
        t = self.ft.fake.transforms[("Game", self.ft.fake.item_ids[("Game", "Camera")])]
        return {k: t[k] for k in ("cropLeft", "cropRight", "cropTop", "cropBottom")}


class FramingTests(ObsCase):
    def test_refresh_reads_the_obs_crop(self):
        self.assertEqual(self.e.src, (1920, 1080))
        self.assertAlmostEqual(self.e.zoom, 1.0, places=2)
        self.assertAlmostEqual(self.e.pan, 0.0, places=2)
        self.assertIs(self.e.fourk, False)

    def test_tween_runs_to_the_exact_end_state(self):
        self.assertTrue(self.e.set_framing(2.0, 0.5, -0.5))
        self.assertEqual(self.sent("SetSceneItemTransform"), [])
        seen = []
        for step in range(1, 11):
            self.now[0] = 100 + 0.04 * step
            self.e.tick(self.now[0])
            seen.append(self.e.zoom)
        self.assertEqual(seen, sorted(seen))
        self.assertTrue(0 < seen[2] < 2.0)
        self.assertIsNone(self.e._tween)
        pump(self.app, lambda: False, 0.3)
        self.assertEqual((self.e.zoom, self.e.pan, self.e.tilt), (2.0, 0.5, -0.5))
        self.assertEqual(self.crop(), eptz.frame_crop(1920, 1080, 2.0, 0.5, -0.5))
        bodies = self.sent("SetSceneItemTransform")
        self.assertTrue(bodies)
        for b in bodies:
            self.assertEqual(set(b["sceneItemTransform"]), {"cropLeft", "cropRight", "cropTop", "cropBottom"})
            self.assertEqual(b["sceneName"], "Game")
        self.assertEqual(self.sent("SetSceneItemTransform")[-1]["sceneItemTransform"], self.crop())

    def test_zero_duration_is_instant(self):
        self.ctx.config.values["eptz_duration_ms"] = 0
        self.e.set_framing(3.0, -1, 1)
        pump(self.app, lambda: bool(self.sent("SetSceneItemTransform")), 2)
        pump(self.app, lambda: False, 0.1)
        self.assertEqual(self.crop(), eptz.frame_crop(1920, 1080, 3.0, -1, 1))

    def test_requests_coalesce_while_one_is_in_flight(self):
        for i in range(30):
            self.e.set_framing(pan=-1 + i / 15, animate=False)
        pump(self.app, lambda: False, 0.4)
        self.assertLess(len(self.sent("SetSceneItemTransform")), 30)
        self.assertEqual(self.crop(), eptz.frame_crop(1920, 1080, 1.0, -1 + 29 / 15, 0.0))

    def test_garbage_input_is_clamped(self):
        self.e.set_framing(1.0, 0.25, 0.0, animate=False)
        self.e.set_framing(float("nan"), float("inf"), -50, animate=False)
        self.assertEqual((self.e.zoom, self.e.pan, self.e.tilt), (1.0, 0.25, -1.0))
        self.e.set_framing(99, animate=False)
        self.assertEqual(self.e.zoom, 4.0)

    def test_reset_returns_to_the_facecam_crop(self):
        self.e.set_framing(3.0, 1, 1, animate=False)
        self.e.reset()
        self.now[0] += 5
        self.e.tick(self.now[0])
        pump(self.app, lambda: False, 0.2)
        self.assertEqual(self.crop(), {"cropLeft": 320, "cropRight": 320, "cropTop": 180, "cropBottom": 180})

    def test_nudge_stacks_on_the_target(self):
        self.e.set_framing(1.0, 0, 0)
        self.e.nudge(dzoom=0.5)
        self.assertAlmostEqual(self.e.zoom, 1.5)

    def test_missing_camera_item_is_reported_not_crashed(self):
        self.ctx.config.values["eptz_item"] = "Nope"
        try:
            self.e.refresh()
            pump(self.app, lambda: "No Nope item" in self.e.status, 3)
            self.assertFalse(self.e.ready)
            self.assertFalse(self.e.set_framing(2.0))
            self.assertTrue(self.toasts[-1][1])
        finally:
            self.ctx.config.values["eptz_item"] = "Camera"

    def test_scene_without_the_item(self):
        self.ctx.store.current_scene = "BRB"
        self.e.refresh()
        pump(self.app, lambda: "No Camera item in BRB" in self.e.status, 3)
        self.assertFalse(self.e.ready)

    def test_presets_apply_and_persist_in_a_temp_config(self):
        self.assertEqual(set(self.e.presets()), {"Face", "Wide", "Desk"})
        self.e.set_framing(2.5, 0.25, -0.25, animate=False)
        self.assertTrue(self.e.save_preset("  Whiteboard "))
        saved = json.loads(self.cfgpath.read_text())["eptz_presets"]
        self.assertEqual(saved, {"Whiteboard": {"zoom": 2.5, "pan": 0.25, "tilt": -0.25}})
        reloaded = Config(self.cfgpath)
        self.assertEqual(reloaded.get("eptz_presets"), saved)
        self.e.reset()
        self.e.tick(self.now[0] + 9)
        self.assertTrue(self.e.apply_preset("Whiteboard"))
        self.e.tick(self.now[0] + 99)
        self.assertEqual((self.e.zoom, self.e.pan, self.e.tilt), (2.5, 0.25, -0.25))
        self.assertTrue(self.e.delete_preset("Whiteboard"))
        self.assertNotIn("Whiteboard", self.e.presets())
        self.assertFalse(self.e.delete_preset("Face"))

    def test_bad_preset_names_are_refused(self):
        for bad in ("", "   ", "x" * 25, "a\nb"):
            self.assertFalse(self.e.save_preset(bad), bad)
        self.assertEqual(self.ctx.config.get("eptz_presets"), {})

    def test_bad_presets_in_a_config_file_fall_back(self):
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps({"eptz_presets": {"a": {"zoom": 9, "pan": 0, "tilt": 0}}, "eptz_duration_ms": -3}))
        with self.assertLogs("lark.config", "WARNING"):
            c = Config(p)
        self.assertEqual(c.get("eptz_presets"), {})
        self.assertEqual(c.get("eptz_duration_ms"), 400)

    def test_deck_keys_and_runs(self):
        keys = self.e.deck_keys()
        self.assertIn("frame:preset:Face", keys)
        self.assertIn("frame:reset", keys)
        self.assertEqual(self.e.deck_label("frame:preset:Face"), "Face")
        self.e.deck_run("frame:preset:Face")
        self.e.tick(self.now[0] + 9)
        self.assertTrue(self.e.deck_lit("frame:preset:Face"))
        self.assertFalse(self.e.deck_lit("frame:preset:Wide"))
        self.e.deck_run("frame:left")
        self.assertLess(self.e.pan, 0)


class FourKTests(ObsCase):
    def settings(self):
        s = self.ft.fake.inputs["Camera"]["settings"]
        return s["resolution"], s["framerate"]

    def test_switch_to_4k_request_bodies_and_crop_scaling(self):
        self.e.set_framing(2.0, 0.5, -0.5, animate=False)
        pump(self.app, lambda: False, 0.2)
        before = self.crop()
        self.mark = len(self.ft.fake.requests)
        self.assertEqual(self.e.set_4k(True), "started")
        pump(self.app, lambda: self.e.fourk is True and self.e.src == (3840, 2160), 3)
        pump(self.app, lambda: False, 0.1)
        self.assertEqual(self.settings(), (251660400, 65566))
        sets = self.sent("SetInputSettings")
        self.assertEqual(sets, [{"inputName": "Camera", "inputSettings": {"resolution": 251660400, "framerate": 65566},
                                 "overlay": True}])
        order = [t for t, _ in self.ft.fake.requests[self.mark:]]
        self.assertLess(order.index("SetInputSettings"), len(order) - 1 - order[::-1].index("GetInputSettings"))
        self.assertLess(order.index("SetInputSettings"), order.index("SetSceneItemTransform"))
        self.assertEqual(self.crop(), {k: 2 * v for k, v in before.items()})
        self.assertEqual(self.ft.fake.inputs["Camera"]["settings"]["pixelformat"], 1196444237)
        self.assertAlmostEqual(self.e.zoom, 2.0, delta=0.05)

    def test_switch_back_halves_the_crop(self):
        self.assertEqual(self.e.set_4k(True), "started")
        pump(self.app, lambda: self.e.fourk is True and self.e.src == (3840, 2160), 3)
        pump(self.app, lambda: False, 0.1)
        self.assertEqual(self.crop(), {"cropLeft": 640, "cropRight": 640, "cropTop": 360, "cropBottom": 360})
        self.assertEqual(self.e.set_4k(False), "started")
        pump(self.app, lambda: self.e.fourk is False and self.e.src == (1920, 1080), 3)
        pump(self.app, lambda: False, 0.1)
        self.assertEqual(self.settings(), (125830200, 65596))
        self.assertEqual(self.crop(), {"cropLeft": 320, "cropRight": 320, "cropTop": 180, "cropBottom": 180})

    def test_framing_after_switch_uses_the_new_frame_size(self):
        self.e.set_4k(True)
        pump(self.app, lambda: self.e.src == (3840, 2160), 3)
        pump(self.app, lambda: False, 0.1)
        self.e.set_framing(4.0, 1, 1, animate=False)
        pump(self.app, lambda: False, 0.2)
        self.assertEqual(self.crop(), eptz.frame_crop(3840, 2160, 4.0, 1, 1))

    def test_already_in_the_mode_is_a_noop(self):
        self.assertEqual(self.e.set_4k(False), "noop")
        self.assertEqual(self.sent("SetInputSettings"), [])

    def test_refused_while_recording_then_confirmed(self):
        self.ctx.store.record["active"] = True
        asked = []
        self.e.confirm_needed.connect(asked.append)
        self.assertEqual(self.e.set_4k(True), "refused")
        pump(self.app, lambda: False, 0.1)
        self.assertEqual(self.sent("SetInputSettings"), [])
        self.assertEqual(self.settings(), (125830200, 65596))
        self.assertEqual(asked, [True])
        self.assertTrue(self.toasts[-1][1])
        self.assertIn("recording", self.toasts[-1][0])
        self.assertEqual(self.e.set_4k(True, force=True), "started")
        pump(self.app, lambda: self.e.fourk is True, 3)
        self.assertEqual(self.settings(), (251660400, 65566))

    def test_refused_while_streaming_and_second_ask_inside_the_window_goes_through(self):
        self.ctx.store.stream["active"] = True
        self.assertEqual(self.e.set_4k(True), "refused")
        self.now[0] += 2
        self.assertEqual(self.e.set_4k(True), "started")
        pump(self.app, lambda: self.e.fourk is True, 3)

    def test_confirmation_expires(self):
        self.ctx.store.stream["active"] = True
        self.assertEqual(self.e.set_4k(True), "refused")
        self.now[0] += eptz.CONFIRM_S + 1
        self.assertEqual(self.e.set_4k(True), "refused")
        self.assertEqual(self.sent("SetInputSettings"), [])

    def test_confirm_for_one_direction_does_not_cover_the_other(self):
        self.ctx.store.stream["active"] = True
        self.e.fourk = True
        self.e._confirm = (True, self.now[0] + 5)
        self.assertEqual(self.e.set_4k(False), "refused")

    def test_obs_keeping_the_old_mode_is_reported(self):
        f = self.ft.fake
        orig = f.handle_request

        async def stubborn(t, d):
            if t == "SetInputSettings":
                return f.ok()
            return await orig(t, d)
        f.handle_request = stubborn
        try:
            self.e.set_4k(True)
            pump(self.app, lambda: any("did not keep" in m for m, _ in self.toasts), 3)
            self.assertIsNot(self.e.fourk, True)
            self.assertEqual(self.sent("SetSceneItemTransform"), [])
        finally:
            f.handle_request = orig

    def test_offline_is_refused_without_requests(self):
        self.ctx.store.conn = "disconnected"
        try:
            self.assertEqual(self.e.set_4k(True), "offline")
        finally:
            self.ctx.store.conn = "connected"
        self.assertEqual(self.sent("SetInputSettings"), [])


class WidgetTests(ObsCase):
    def test_pad_drag_signals_clamp_to_the_edges(self):
        pad = FramePad(self.e)
        got = []
        pad.moved.connect(lambda x, y: got.append((x, y)))
        self.e.set_framing(2.0, 0, 0, animate=False)
        pad._from_point(QPointF(-50, 400))
        pad._from_point(QPointF(9999, -3))
        pad._from_point(QPointF(160, 90))
        self.assertEqual(got[0], (-1.0, 1.0))
        self.assertEqual(got[1], (1.0, -1.0))
        self.assertAlmostEqual(got[2][0], 0.0, places=2)

    def test_camera_page_shows_total_zoom_and_section(self):
        page = CameraPage(self.ctx)
        self.assertEqual(page.framing.total.text().split("(")[0].strip(), "Total zoom 1.0x")
        self.e.set_framing(2.0, animate=False)
        self.assertIn("Total zoom 2.0x", page.framing.total.text())
        page.framing.camera_zoom = lambda: 2.4
        page.framing.update_total()
        self.assertIn("Total zoom 4.8x", page.framing.total.text())
        self.assertIn("(camera 2.4x, framing 2.0x)", page.framing.total.text())

    def test_mode_card_asks_before_interrupting_a_recording(self):
        w = CameraModeCard(self.e, "#7c5cff")
        self.assertTrue(w.buttons["Stream"].isChecked())
        self.assertTrue(w.warn.isHidden())
        self.ctx.store.record["active"] = True
        w.buttons["Quality"].click()
        self.assertFalse(w.confirm.isHidden())
        self.assertFalse(w.buttons["Quality"].isChecked())
        self.assertEqual(self.sent("SetInputSettings"), [])
        w.confirm.click()
        pump(self.app, lambda: self.e.fourk is True, 3)
        self.assertTrue(w.buttons["Quality"].isChecked())
        self.assertFalse(w.buttons["Stream"].isChecked())
        self.assertTrue(w.confirm.isHidden())
        self.assertFalse(w.warn.isHidden())
        self.assertIn("CPU", w.warn.text())

    def test_framing_card_shows_the_mode_as_text(self):
        page = CameraPage(self.ctx)
        self.assertEqual(page.framing.mode.text(), "Camera mode: Stream (1080p60)")
        self.e.set_4k(True)
        pump(self.app, lambda: self.e.fourk is True, 3)
        self.assertEqual(page.framing.mode.text(), "Camera mode: Quality (4K30)")
        self.assertFalse(hasattr(page.framing, "fourk"))
        self.assertFalse(hasattr(page.looks_section, "mode_buttons"))

    def test_header_chip_shows_the_mode(self):
        from lark.shell import StatusHeader
        h = StatusHeader(self.ctx)
        self.assertIn("Stream (1080p60)", h.cam_label.text())
        self.e.set_4k(True)
        pump(self.app, lambda: self.e.fourk is True, 3)
        self.assertIn("Quality (4K30)", h.cam_label.text())
        self.assertNotIn("Stream", h.cam_label.text())

    def test_deck_quality_button_toggles_the_mode(self):
        key = "frame:quality"
        self.assertIn(key, self.e.deck_keys())
        self.assertEqual(self.e.deck_label(key), "Quality mode")
        self.assertFalse(self.e.deck_lit(key))
        self.e.deck_run(key)
        pump(self.app, lambda: self.e.fourk is True, 3)
        self.assertTrue(self.e.deck_lit(key))
        self.e.deck_run(key)
        pump(self.app, lambda: self.e.fourk is False, 3)
        self.assertFalse(self.e.deck_lit(key))

    def test_deck_quality_button_refuses_while_live(self):
        self.ctx.store.stream["active"] = True
        self.e.deck_run("frame:quality")
        self.assertEqual(self.sent("SetInputSettings"), [])
        self.assertIs(self.e.fourk, False)


if __name__ == "__main__":
    unittest.main()
