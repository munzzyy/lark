"""Watches the camera image through OBS and flags the dark-exposure state the EMEET C60E drops into.

OBS holds the camera while streaming, so Lark cannot read frames itself. obs-websocket's GetSourceScreenshot of
the camera input works any time OBS runs, so that is the sample source.
"""
import base64
import binascii
import logging
import math
import statistics
import time

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QImage

log = logging.getLogger("lark.camwatch")

INTERVAL_MS = 3000
SHOT_WIDTH = 160
HISTORY_S = 60.0
DARK_HOLD_S = 9.0
BASELINE_MIN = 45.0
CLEAR_MARGIN = 13.0
DIM_BELOW = 45.0
BRIGHT_ABOVE = 185.0


def luma_of_png(data):
    """Mean luma (0-255) of the centre half of a PNG, from raw bytes or a base64 / data-URL string. None if undecodable."""
    if isinstance(data, str):
        if data.startswith("data:"):
            data = data.partition(",")[2]
        try:
            data = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
    img = QImage.fromData(data, "PNG")
    if img.isNull() or img.width() < 4 or img.height() < 4:
        return None
    w, h = img.width(), img.height()
    img = img.copy(w // 4, h // 4, w // 2, h // 2).convertToFormat(QImage.Format_Grayscale8)
    stride, width, rows = img.bytesPerLine(), img.width(), img.height()
    raw = bytes(img.constBits())
    total = sum(sum(raw[y * stride:y * stride + width]) for y in range(rows))
    return total / (width * rows)


def light_label(luma):
    if luma is None:
        return ""
    return "dim" if luma < DIM_BELOW else "bright" if luma > BRIGHT_ABOVE else "ok"


class DarkDetector:
    """Dark = luma under `dark` for DARK_HOLD_S straight while the 60 s before that had a median over BASELINE_MIN,
    so a room that was always dim is never flagged. Cleared by one sample over `dark` + CLEAR_MARGIN."""

    def __init__(self, dark=22.0):
        self.dark_below = float(dark)
        self.reset()

    def reset(self):
        self.history = []
        self.dark = False
        self.last = None
        self._since = None

    @property
    def clear_above(self):
        return self.dark_below + CLEAR_MARGIN

    def feed(self, t, luma):
        if luma is None or not math.isfinite(luma):
            return self.dark
        self.last = luma
        self.history.append((t, luma))
        self.history = [(ts, v) for ts, v in self.history if ts > t - HISTORY_S]
        if self.dark:
            if luma > self.clear_above:
                self.dark = False
                self._since = None
            return self.dark
        if luma >= self.dark_below:
            self._since = None
            return False
        if self._since is None:
            self._since = t
        if t - self._since >= DARK_HOLD_S:
            before = [v for ts, v in self.history if ts < self._since]
            if before and statistics.median(before) > BASELINE_MIN:
                self.dark = True
        return self.dark


class CamWatch(QObject):
    """Samples the camera input every few seconds while OBS is connected and writes the result to the store."""

    def __init__(self, store, config, interval_ms=INTERVAL_MS, clock=time.monotonic):
        super().__init__(store)
        self.store, self.config, self._clock = store, config, clock
        self.detector = DarkDetector(config.get("camera_dark_threshold"))
        self._busy = False
        self._waiters = []
        self._logged_missing = False
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.sample)

    def start(self):
        self.detector.reset()
        self._publish()
        self._timer.start()
        self.sample()

    def stop(self):
        self._timer.stop()
        self.detector.reset()
        self._busy = False
        self._publish()
        self._finish(None)

    def sample(self, done=None):
        """One screenshot now. done(luma_or_None) runs after the detector has seen it."""
        if not self.store.connected:
            if done:
                done(None)
            return
        if done:
            self._waiters.append(done)
        if self._busy:
            return
        self._busy = True
        req = {"sourceName": self.config.get("camera_input"), "imageFormat": "png", "imageWidth": SHOT_WIDTH}
        self.store.bridge.request("GetSourceScreenshot", req, self._got)

    def _finish(self, luma):
        waiters, self._waiters = self._waiters, []
        for w in waiters:
            w(luma)

    def _got(self, ok, payload):
        self._busy = False
        if not self.store.connected:
            self._finish(None)
            return
        luma = luma_of_png(payload.get("imageData", "")) if ok and isinstance(payload, dict) else None
        if luma is None:
            if not self._logged_missing:
                self._logged_missing = True
                log.info("no camera image from OBS input %r: %s", self.config.get("camera_input"),
                         payload if not ok else "undecodable image")
            self.detector.reset()
        else:
            self._logged_missing = False
            self.detector.dark_below = float(self.config.get("camera_dark_threshold"))
            self.detector.feed(self._clock(), luma)
        self._publish()
        self._finish(luma)

    def _publish(self):
        s, d = self.store, self.detector
        if (s.cam_luma, s.cam_dark) != (d.last, d.dark):
            s.cam_luma, s.cam_dark = d.last, d.dark
            s.cam_light_changed.emit()
