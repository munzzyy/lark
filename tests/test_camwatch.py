import base64
import unittest

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from lark.camwatch import DarkDetector, light_label, luma_of_png


def png(centre, border=255, w=40, h=24):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(border, border, border))
    for y in range(h // 4, h - h // 4):
        for x in range(w // 4, w - w // 4):
            img.setPixelColor(x, y, QColor(centre, centre, centre))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def run(det, samples, step=3.0, t0=0.0):
    t = t0
    for v in samples:
        det.feed(t, v)
        t += step
    return t


class LumaTests(unittest.TestCase):
    def test_centre_only(self):
        self.assertAlmostEqual(luma_of_png(png(100)), 100, delta=1)
        self.assertAlmostEqual(luma_of_png(png(16)), 16, delta=1)

    def test_data_url_and_base64(self):
        b = base64.b64encode(png(80)).decode()
        self.assertAlmostEqual(luma_of_png("data:image/png;base64," + b), 80, delta=1)
        self.assertAlmostEqual(luma_of_png(b), 80, delta=1)

    def test_garbage_is_none(self):
        for bad in ("", "not base64!!", base64.b64encode(b"nope").decode(), b"", b"\x89PNG"):
            self.assertIsNone(luma_of_png(bad), bad)

    def test_light_label(self):
        self.assertEqual([light_label(x) for x in (None, 16, 100, 230)], ["", "dim", "ok", "bright"])


class DetectorTests(unittest.TestCase):
    def test_bright_then_dark_flags_after_nine_seconds(self):
        d = DarkDetector()
        t = run(d, [100] * 5)
        for i, expect in enumerate([False, False, False, True]):
            d.feed(t + 3 * i, 16)
            self.assertEqual(d.dark, expect, i)

    def test_always_dark_room_is_not_flagged(self):
        d = DarkDetector()
        run(d, [16] * 30)
        self.assertFalse(d.dark)
        self.assertEqual(d.last, 16)

    def test_dim_baseline_is_not_flagged(self):
        d = DarkDetector()
        t = run(d, [40] * 5)
        run(d, [16] * 6, t0=t)
        self.assertFalse(d.dark)

    def test_brief_dip_is_not_flagged(self):
        d = DarkDetector()
        t = run(d, [100] * 5)
        t = run(d, [16, 16], t0=t)
        run(d, [100] * 3, t0=t)
        self.assertFalse(d.dark)

    def test_between_thresholds_keeps_flag_and_over_35_clears(self):
        d = DarkDetector()
        t = run(d, [100] * 5 + [16] * 5)
        self.assertTrue(d.dark)
        t = run(d, [30, 35], t0=t)
        self.assertTrue(d.dark)
        d.feed(t, 36)
        self.assertFalse(d.dark)

    def test_flag_survives_longer_than_the_history_window(self):
        d = DarkDetector()
        t = run(d, [100] * 5 + [16] * 40)
        self.assertTrue(d.dark)
        self.assertLessEqual(len(d.history), 21)

    def test_nan_and_inf_ignored(self):
        d = DarkDetector()
        run(d, [100] * 3)
        for bad in (float("nan"), float("inf"), None):
            d.feed(50.0, bad)
        self.assertEqual(d.last, 100)
        self.assertFalse(d.dark)

    def test_threshold_moves_the_clear_point(self):
        d = DarkDetector(dark=30)
        self.assertEqual(d.clear_above, 43)

    def test_reset(self):
        d = DarkDetector()
        run(d, [100] * 5 + [16] * 5)
        d.reset()
        self.assertFalse(d.dark)
        self.assertIsNone(d.last)
        self.assertEqual(d.history, [])


if __name__ == "__main__":
    unittest.main()
