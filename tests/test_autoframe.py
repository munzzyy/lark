import base64
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from lark import autoframe, eptz
from lark.autoframe import NEEDS_CV2, Tracker, gray_from_png, make_detector
from lark.config import Config
from tests import test_eptz
from tests.test_ui_smoke import pump

NAN, INF = float("nan"), float("inf")


def centred(fh=0.18, cx=0.5, cy=0.42):
    return (cx - fh * 9 / 16 / 2, cy - fh / 2, fh * 9 / 16, fh)



class TrackerTests(unittest.TestCase):
    def test_first_sample_zooms_to_the_face_share(self):
        t = Tracker()
        z, pan, tilt = t.feed(0, [centred(0.18)])
        self.assertAlmostEqual(z, (2 / 3) / (0.18 / 0.30), places=6)
        self.assertEqual((t.state, t.count), ("tracking", 1))
        self.assertAlmostEqual(pan, 0.0, places=6)

    def test_nothing_moves_afterwards_inside_the_dead_zone(self):
        t = Tracker()
        t.feed(0, [centred()])
        for i in range(1, 20):
            self.assertIsNone(t.feed(i * 0.4, [centred(cx=0.5 + 0.01 * (i % 2))]))

    def test_face_at_the_right_edge_pans_right_and_stays_inside(self):
        t = Tracker()
        z, pan, tilt = t.feed(0, [centred(cx=0.93)])
        self.assertGreater(pan, 0.9)
        self.assertLessEqual(pan, 1.0)
        self.assertTrue(-1 <= tilt <= 1)

    def test_clamped_edge_does_not_refire_every_sample(self):
        t = Tracker()
        t.feed(0, [centred(cx=0.95)])
        for i in range(1, 10):
            self.assertIsNone(t.feed(i * 0.4, [centred(cx=0.95)]))

    def test_two_faces_frame_the_pair_with_less_zoom(self):
        one = Tracker().feed(0, [centred(0.12, cx=0.35)])
        two = Tracker().feed(0, [centred(0.12, cx=0.35), centred(0.12, cx=0.55)])
        self.assertLess(two[0], one[0])
        fw = (2 / 3) / two[0]
        centre = ((0.35 - 0.12 * 9 / 32) + (0.55 + 0.12 * 9 / 32)) / 2
        self.assertAlmostEqual((1 - fw) * (1 + two[1]) / 2 + fw / 2, centre, places=3)
        union = 0.55 - 0.35 + 0.12 * 9 / 16
        self.assertLessEqual(union, 0.6 * fw + 1e-9)

    def test_tiny_faces_are_ignored(self):
        t = Tracker()
        self.assertIsNone(t.feed(0, [(0.5, 0.5, 0.02, 0.03)]))
        self.assertEqual((t.count, t.state), (0, "looking"))

    def test_garbage_is_safe(self):
        t = Tracker()
        for bad in (None, [], [None], [(NAN, 0, 0.1, 0.1)], [(0.5, 0.5, INF, 0.1)], [("a", 1, 2, 3)], [(1, 2)], 5):
            try:
                self.assertIsNone(t.feed(0, bad))
            except TypeError:
                self.fail(f"feed raised on {bad!r}")
        self.assertIsNone(t.feed(NAN, [centred()]))
        self.assertIsNone(t.feed(None, [centred()]))
        self.assertEqual(t.state, "looking")

    def test_lost_holds_then_goes_home_once(self):
        t = Tracker(lost_s=6, home="Wide", home_framing=(1.0, 0.0, 0.0))
        t.feed(0, [centred()])
        self.assertIsNone(t.feed(3, []))
        self.assertEqual(t.state, "holding")
        self.assertIsNone(t.feed(5.9, []))
        self.assertEqual(t.feed(6.1, []), (1.0, 0.0, 0.0))
        self.assertEqual(t.state, "home")
        self.assertIsNone(t.feed(9, []))
        self.assertIsNone(t.feed(60, []))
        self.assertEqual(t.state, "home")

    def test_never_seen_a_face_never_goes_home(self):
        t = Tracker(home_framing=(1.0, 0.0, 0.0))
        for i in range(30):
            self.assertIsNone(t.feed(i, []))
        self.assertEqual(t.state, "looking")

    def test_face_after_home_frames_again(self):
        t = Tracker(home_framing=(1.0, 0.0, 0.0))
        first = t.feed(0, [centred()])
        t.feed(10, [])
        again = t.feed(11, [centred()])
        self.assertEqual(again, first)
        self.assertEqual(t.state, "tracking")

    def test_dead_zone_boundaries(self):
        t = Tracker(smoothing=1.0, deadzone=0.06)
        t.feed(0, [centred(cx=0.5)])
        self.assertIsNone(t.feed(1, [centred(cx=0.55)]))
        self.assertIsNotNone(t.feed(2, [centred(cx=0.58)]))

    def test_zoom_change_over_twelve_percent_moves(self):
        t = Tracker(smoothing=1.0)
        t.feed(0, [centred(0.20)])
        self.assertIsNone(t.feed(1, [centred(0.21)]))
        self.assertIsNotNone(t.feed(2, [centred(0.16)]))

    def test_zoom_is_clamped(self):
        t = Tracker(zoom_max=2.5)
        self.assertAlmostEqual(t.feed(0, [centred(0.08)])[0], 2.5)
        t = Tracker()
        self.assertAlmostEqual(t.feed(0, [centred(0.9)])[0], 1.0)

    def test_smoothing_eases_toward_a_jump(self):
        t = Tracker(smoothing=0.35, deadzone=0.01)
        t.feed(0, [centred(cx=0.3)])
        move = t.feed(1, [centred(cx=0.7)])
        full = Tracker().feed(0, [centred(cx=0.7)])
        self.assertLess(move[1], full[1])

    def test_pan_formula_matches_eptz_crop(self):
        t = Tracker()
        z, pan, tilt = t.feed(0, [centred(0.18, cx=0.62, cy=0.4)])
        crop = eptz.frame_crop(1920, 1080, z, pan, tilt)
        left = crop["cropLeft"] / 1920
        w = (1920 - crop["cropLeft"] - crop["cropRight"]) / 1920
        self.assertAlmostEqual(left + w / 2, 0.62, delta=0.01)


def png_bytes(w=64, h=36, gray=120):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(gray, gray, gray))
    img.setPixelColor(0, 0, QColor(255, 255, 255))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class DecodeAndDetectorTests(unittest.TestCase):
    def test_gray_from_png(self):
        data = png_bytes(63, 36)
        for source in (data, "data:image/png;base64," + base64.b64encode(data).decode(),
                       base64.b64encode(data).decode()):
            arr, w, h = gray_from_png(source)
            self.assertEqual((w, h, arr.shape, str(arr.dtype)), (63, 36, (36, 63), "uint8"))
            self.assertEqual(int(arr[0, 0]), 255)
            self.assertAlmostEqual(int(arr[10, 10]), 120, delta=1)

    def test_gray_from_png_rejects_junk(self):
        for bad in ("", "not base64!!", b"", b"\x89PNG", base64.b64encode(b"nope").decode(), png_bytes(8, 8)):
            self.assertIsNone(gray_from_png(bad))

    def test_make_detector_says_what_is_missing_without_cv2(self):
        with mock.patch.dict(sys.modules, {"cv2": None}):
            det, why = make_detector(Config(os.path.join(os.environ["LARK_CONFIG_DIR"], "x.json")))
        self.assertIsNone(det)
        self.assertEqual(why, NEEDS_CV2)

    def test_find_model_takes_the_config_path_then_the_data_dir(self):
        d = tempfile.mkdtemp()
        cfg = Config(os.path.join(os.environ["LARK_CONFIG_DIR"], "m.json"))
        self.assertIsNone(autoframe.find_model(cfg, [d]))
        data_model = os.path.join(d, autoframe.MODEL_NAME)
        open(data_model, "w").close()
        self.assertEqual(autoframe.find_model(cfg, [d]), data_model)
        own = os.path.join(d, "mine.onnx")
        open(own, "w").close()
        cfg.set("autoframe_model", own)
        self.assertEqual(autoframe.find_model(cfg, [d]), own)
        cfg.set("autoframe_model", os.path.join(d, "gone.onnx"))
        self.assertEqual(autoframe.find_model(cfg, [d]), data_model)

    def test_find_cascade_prefers_the_default_cascade(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.assertIsNone(autoframe.find_cascade([d]))
        open(os.path.join(d, "haarcascade_frontalface_alt2.xml"), "w").close()
        self.assertTrue(autoframe.find_cascade([d]).endswith("alt2.xml"))
        open(os.path.join(d, "haarcascade_frontalface_default.xml"), "w").close()
        self.assertTrue(autoframe.find_cascade([d]).endswith("default.xml"))


class FakeDetector:
    def __init__(self):
        self.boxes = []
        self.calls = 0

    def detect(self, gray):
        self.calls += 1
        return list(self.boxes)


FACE_PX = (170, 70, 44, 39)


class AutoFrameTests(test_eptz.ObsCase):
    def setUp(self):
        super().setUp()
        self.a = self.ctx.autoframe
        self.det = FakeDetector()
        self.det.boxes = [FACE_PX]
        self.a.detector, self.a.reason = self.det, ""
        self.ctx.config.values["eptz_duration_ms"] = 0
        for k in ("autoframe_manual_pause_s", "autoframe_lost_s"):
            self.ctx.config.values[k] = {"autoframe_manual_pause_s": 8, "autoframe_lost_s": 6}[k]
        self.ctx.config.values["autoframe_on"] = False
        self.ctx.store.cam_dark = False
        self.now = [1000.0]
        self.a.clock = lambda: self.now[0]
        self.a._paused_until = 0.0
        self.a.tracker.reset()
        self.a.sync = True
        self.a.set_on(True)
        self.a._timer.stop()
        self.mark = len(self.ft.fake.requests)

    def tearDown(self):
        self.a.set_on(False) if self.a.detector else None
        self.a.detector, self.a.reason = None, NEEDS_CV2
        self.ctx.config.values["autoframe_on"] = False
        self.ctx.store.cam_dark = False

    def shots(self):
        return [r for r in self.ft.fake.requests[self.mark:] if r[0] == "GetSourceScreenshot"]

    def sets(self):
        return [r for r in self.ft.fake.requests[self.mark:] if r[0] == "SetSceneItemTransform"]

    def sample(self):
        self.a.tick()
        self.assertTrue(pump(self.app, lambda: not self.a._busy, 3))
        pump(self.app, lambda: False, 0.12)

    def expected_crop(self):
        cx, cy, uh = (170 + 22) / 384, (70 + 19.5) / 216, 39 / 216
        fw = (2 / 3) / min(2.5, max(1.0, (2 / 3) / (uh / 0.3)))
        left, top = min(max(cx - fw / 2, 0), 1 - fw), min(max(cy - 0.42 * fw, 0), 1 - fw)
        z = (2 / 3) / fw
        return eptz.frame_crop(1920, 1080, z, 2 * left / (1 - fw) - 1, 2 * top / (1 - fw) - 1)

    def test_a_face_moves_the_crop_once(self):
        self.sample()
        sent = self.sets()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["sceneItemTransform"], self.expected_crop())
        self.assertEqual(self.a.status, "Tracking 1 face")
        self.assertEqual(len(self.a.faces), 1)
        self.sample()
        self.assertEqual(len(self.sets()), 1)
        self.assertEqual(self.det.calls, 2)

    def test_threaded_path_moves_the_crop_too(self):
        self.a.sync = False
        self.sample()
        self.a._job.join(3)
        self.assertEqual(len(self.sets()), 1)
        self.assertEqual(self.sets()[0][1]["sceneItemTransform"], self.expected_crop())
        self.a._join()

    def test_screenshot_is_the_camera_input_at_384(self):
        self.sample()
        self.assertEqual(self.shots()[0][1], {"sourceName": "Camera", "imageFormat": "png", "imageWidth": 384})

    def test_manual_move_pauses_then_resumes(self):
        self.sample()
        self.e.set_framing(zoom=2.0, animate=False)
        self.assertEqual(self.a.status, "Paused after a manual move")
        before = len(self.shots())
        self.sample()
        self.assertEqual(len(self.shots()), before)
        self.now[0] += 9
        self.sample()
        self.assertEqual(len(self.shots()), before + 1)
        self.assertEqual(self.sets()[-1][1]["sceneItemTransform"], self.expected_crop())

    def test_its_own_moves_do_not_pause_it(self):
        self.sample()
        self.assertFalse(self.a.paused)

    def test_dark_camera_holds(self):
        self.ctx.store.cam_dark = True
        self.sample()
        self.assertEqual((len(self.shots()), len(self.sets())), (0, 0))

    def test_disconnect_stops_the_timer(self):
        self.a._timer.start()
        with mock.patch.object(type(self.ctx.store), "connected", new_callable=mock.PropertyMock, return_value=False):
            self.a._sync()
            self.assertFalse(self.a._timer.isActive())
            self.assertEqual(self.a.status, "Waiting for OBS")
        self.a._sync()
        self.assertTrue(self.a._timer.isActive())

    def test_no_face_holds_then_goes_back_to_wide(self):
        self.sample()
        self.det.boxes = []
        self.now[0] += 2
        self.sample()
        self.assertEqual(self.a.status, "Lost the face, holding")
        self.now[0] += 5
        self.sample()
        self.assertEqual(self.a.status, "Nobody in frame, back to Wide")
        self.assertEqual(self.sets()[-1][1]["sceneItemTransform"], eptz.frame_crop(1920, 1080, 1.0, 0, 0))
        n = len(self.sets())
        self.now[0] += 30
        self.sample()
        self.assertEqual(len(self.sets()), n)

    def test_off_and_unavailable_status(self):
        self.a.set_on(False)
        self.assertEqual(self.a.status, "Off")
        self.assertFalse(self.a._timer.isActive())
        self.a.detector, self.a.reason = None, NEEDS_CV2
        self.assertEqual(self.a.status, NEEDS_CV2)
        self.toasts.clear()
        self.assertFalse(self.a.set_on(True))
        self.assertFalse(self.ctx.config.get("autoframe_on"))
        self.assertEqual(len(self.toasts), 1)

    def test_two_faces_status(self):
        self.det.boxes = [(60, 70, 44, 39), (280, 70, 44, 39)]
        self.sample()
        self.assertEqual(self.a.status, "Tracking 2 faces")

    def test_detector_crash_does_not_wedge_it(self):
        def boom(gray):
            raise RuntimeError("no")
        self.det.detect = boom
        with self.assertLogs("lark.autoframe", "ERROR"):
            self.sample()
        self.det.detect = FakeDetector().detect
        self.sample()
        self.assertFalse(self.a._busy)

    def test_deck_button(self):
        self.assertIn("frame:auto", self.e.deck_keys())
        self.assertEqual(self.e.deck_label("frame:auto"), "Auto-frame")
        self.assertTrue(self.e.deck_lit("frame:auto"))
        self.e.deck_run("frame:auto")
        self.assertFalse(self.e.deck_lit("frame:auto"))
        self.e.deck_run("frame:auto")
        self.assertTrue(self.e.deck_lit("frame:auto"))

    def test_camera_page_has_the_toggle_and_status(self):
        from lark.pages.framing import FramingCard
        a = self.ctx.autoframe
        a.detector, a.reason = None, NEEDS_CV2
        f = FramingCard(self.ctx)
        self.assertFalse(f.auto.isEnabled())
        self.assertEqual(f.auto.toolTip(), NEEDS_CV2)
        self.assertEqual(f.auto_status.text(), NEEDS_CV2)
        a.detector, a.reason = FakeDetector(), ""
        a.set_on(False)
        self.assertTrue(f.auto.isEnabled())
        self.assertEqual(f.auto_status.text(), "Off")
        f.auto.click()
        self.assertTrue(a.on)
        self.assertTrue(f.auto.isChecked())
        a.faces = [(0.4, 0.3, 0.1, 0.2)]
        f.pad.grab()
        f.auto.click()
        self.assertFalse(a.on)
        a._timer.stop()
        a.detector, a.reason = None, NEEDS_CV2

    def test_settings_card_writes_the_config(self):
        from lark.pages.framing import autoframe_card
        card = autoframe_card(self.ctx)
        from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox
        zoom = card.findChild(QDoubleSpinBox)
        zoom.setValue(3.0)
        self.assertEqual(self.ctx.config.get("autoframe_zoom_max"), 3.0)
        spins = card.findChildren(QSpinBox)
        spins[0].setValue(40)
        self.assertEqual(self.ctx.config.get("autoframe_face_share"), 0.4)
        spins[-1].setValue(800)
        self.assertEqual(self.ctx.config.get("autoframe_interval_ms"), 800)
        self.ctx.config.set("autoframe_zoom_max", 2.5)
        self.ctx.config.set("autoframe_face_share", 0.3)
        self.ctx.config.set("autoframe_interval_ms", 400)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        c = Config(os.path.join(os.environ["LARK_CONFIG_DIR"], "af.json"))
        self.assertEqual((c.get("autoframe_on"), c.get("autoframe_interval_ms"), c.get("autoframe_home")), (False, 400, "Wide"))
        for k, bad in (("autoframe_interval_ms", 100), ("autoframe_zoom_max", 5), ("autoframe_deadzone", 0),
                       ("autoframe_on", 1), ("autoframe_smoothing", 0), ("autoframe_face_share", 0.9)):
            with self.assertRaises(ValueError):
                c.set(k, bad)


if __name__ == "__main__":
    unittest.main()
