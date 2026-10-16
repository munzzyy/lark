"""Writes one real camera control through the same path the Camera page uses, then restores it.

Skips cleanly if the camera is absent or busy.
"""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lark.camera_backend import CameraWorker
from lark.config import CAMERA_BY_ID, Config

LIB = os.path.expanduser("~/Projects/cameractrls-emeet/cameractrls.py")


def pump(app, cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@unittest.skipUnless(os.path.exists(CAMERA_BY_ID) and os.path.isfile(LIB), "camera or camera library not present")
class RealWrite(unittest.TestCase):
    def test_brightness_round_trip(self):
        app = QApplication.instance() or QApplication([])
        w = CameraWorker(Config())
        snaps, problems = [], []
        w.snapshot_ready.connect(snaps.append)
        w.unavailable.connect(problems.append)
        w.error.connect(problems.append)
        w.start()
        orig = None
        try:
            if not pump(app, lambda: snaps or problems):
                self.fail("no answer from the camera worker")
            if problems and not snaps:
                self.skipTest(f"camera unavailable: {problems[0]}")

            def brightness():
                for p in snaps[-1].pages:
                    for cat in p.categories:
                        for c in cat.ctrls:
                            if c.text_id == "brightness":
                                return c
                self.fail("no brightness control")

            c = brightness()
            orig = c.value
            target = orig + 10 if orig + 10 <= c.max else orig - 10
            n = len(snaps)
            w.apply({"brightness": str(target)})
            ok = pump(app, lambda: len(snaps) > n and brightness().value == target)
            if problems and any("busy" in m.lower() for m in problems):
                self.skipTest(f"camera busy: {problems[-1]}")
            self.assertTrue(ok, f"brightness did not read back as {target}: {problems}")
        finally:
            if orig is not None:
                n = len(snaps)
                w.apply({"brightness": str(orig)})
                restored = pump(app, lambda: len(snaps) > n and brightness().value == orig)
            else:
                restored = True
            w.stop()
        self.assertTrue(restored, "could not restore the original brightness")


if __name__ == "__main__":
    unittest.main()
