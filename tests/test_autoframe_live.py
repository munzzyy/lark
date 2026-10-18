import os
import subprocess
import unittest

from lark.config import CAMERA_BY_ID as CAMERA


def cv2_or_none():
    try:
        import cv2
        return cv2
    except ImportError:
        return None


@unittest.skipUnless(os.environ.get("LARK_REAL_AUTOFRAME") == "1", "set LARK_REAL_AUTOFRAME=1 to open the real camera")
@unittest.skipUnless(cv2_or_none(), "needs python-opencv")
class LiveAutoFrame(unittest.TestCase):
    def test_haar_runs_on_a_real_frame(self):
        import numpy as np
        from lark.autoframe import HaarDetector, find_cascade
        path = find_cascade()
        self.assertIsNotNone(path)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080",
             "-i", CAMERA, "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"],
            capture_output=True, timeout=30, check=True).stdout
        gray = np.frombuffer(raw, dtype=np.uint8).reshape(1080, 1920)
        found = HaarDetector(path).detect(gray)
        self.assertIsInstance(found, list)
        for box in found:
            self.assertEqual(len(box), 4)
