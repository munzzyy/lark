"""Auto-framing: finds faces in the camera frames OBS already has and moves the OBS crop to follow them.

The camera cannot pan or tilt, so this only drives Eptz. OBS holds the camera, so frames come from
GetSourceScreenshot of the camera input, the same way the dark-camera watchdog gets them.
"""
import base64
import binascii
import logging
import math
import os
import threading
import time

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
from PySide6.QtGui import QImage

log = logging.getLogger("lark.autoframe")

SHOT_WIDTH = 384
MIN_FACE_WIDTH = 0.03
EYE_LINE = 0.42
ZOOM_CHANGE = 0.12
GROUP_WIDTH_SHARE = 0.6
NEEDS_CV2 = "Needs python-opencv (sudo pacman -S python-opencv)"
CASCADE_DIRS = ("/usr/share/opencv5/haarcascades", "/usr/share/opencv4/haarcascades",
                "/usr/share/OpenCV/haarcascades")
CASCADE_NAMES = ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
AUTO_KEY = "frame:auto"


def model_dirs():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return (os.path.join(data, "lark"), "/usr/share/lark")


def find_model(config, dirs=None):
    """The YuNet model: the configured path if it exists, else MODEL_NAME in the data dirs."""
    want = os.path.expanduser(config.get("autoframe_model") or "")
    if want and os.path.isfile(want):
        return want
    for d in (model_dirs() if dirs is None else dirs):
        path = os.path.join(d, MODEL_NAME)
        if os.path.isfile(path):
            return path
    return None


def find_cascade(dirs=None):
    if dirs is None:
        dirs = list(CASCADE_DIRS)
        try:
            import cv2
            dirs.append(cv2.data.haarcascades)
        except (ImportError, AttributeError):
            pass
    for name in CASCADE_NAMES:
        for d in dirs:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


class HaarDetector:
    def __init__(self, path):
        import cv2
        self.cv2 = cv2
        self.cascade = cv2.CascadeClassifier(path)
        if self.cascade.empty():
            raise ValueError(f"could not load the face cascade {path}")

    def detect(self, gray):
        h, w = gray.shape[:2]
        side = max(16, w // 14)
        found = self.cascade.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=3, minSize=(side, side))
        boxes = [(int(x), int(y), int(fw), int(fh)) for x, y, fw, fh in found] if len(found) else []
        if len(boxes) > 1:
            biggest = max(b[2] for b in boxes)
            boxes = [b for b in boxes if b[2] * 2 >= biggest]
        return boxes


class YuNetDetector:
    def __init__(self, path):
        import cv2
        self.cv2 = cv2
        quiet = getattr(getattr(cv2, "utils", None), "logging", None)
        if quiet is not None:
            quiet.setLogLevel(quiet.LOG_LEVEL_ERROR)
        self.net = cv2.FaceDetectorYN.create(path, "", (320, 320), score_threshold=0.7)

    def detect(self, gray):
        h, w = gray.shape[:2]
        self.net.setInputSize((w, h))
        bgr = self.cv2.cvtColor(gray, self.cv2.COLOR_GRAY2BGR)
        _, rows = self.net.detect(bgr)
        if rows is None:
            return []
        return [(int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in rows]


def make_detector(config, find=find_cascade, find_model=find_model):
    """(detector, "") when one loads, otherwise (None, why not). YuNet when its model is present, else the cascade."""
    try:
        import cv2
    except ImportError:
        return None, NEEDS_CV2
    model = find_model(config)
    if model and hasattr(cv2, "FaceDetectorYN"):
        try:
            return YuNetDetector(model), ""
        except (cv2.error, ValueError, OSError) as e:
            log.warning("could not load the face model %s, using the cascade: %s", model, e)
    path = find()
    if path is None:
        return None, "No face cascade found. Install opencv (the package with haarcascades)"
    try:
        return HaarDetector(path), ""
    except (cv2.error, ValueError, OSError) as e:
        return None, f"Could not load the face detector: {e}"


def gray_from_png(data):
    """(HxW uint8 array, width, height) from PNG bytes or a base64 / data-URL string, or None."""
    if isinstance(data, str):
        if data.startswith("data:"):
            data = data.partition(",")[2]
        try:
            data = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
    try:
        import numpy as np
    except ImportError:
        return None
    img = QImage.fromData(data, "PNG")
    if img.isNull() or img.width() < 16 or img.height() < 16:
        return None
    img = img.convertToFormat(QImage.Format_Grayscale8)
    w, h, stride = img.width(), img.height(), img.bytesPerLine()
    flat = np.frombuffer(bytes(img.constBits()), dtype=np.uint8)[:h * stride]
    return flat.reshape(h, stride)[:, :w].copy(), w, h


def _finite(*vals):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


class Tracker:
    """Turns face boxes (fractions of the frame) into zoom, pan and tilt, with smoothing and a dead zone.
    `ratio` is the window height fraction divided by its width fraction (1.0 for a 16:9 frame)."""

    def __init__(self, smoothing=0.35, face_share=0.30, zoom_max=2.5, deadzone=0.06, lost_s=6.0,
                 home="Wide", home_framing=None, ratio=1.0):
        self.smoothing, self.face_share, self.zoom_max = smoothing, face_share, zoom_max
        self.deadzone, self.lost_s = deadzone, lost_s
        self.home, self.home_framing, self.ratio = home, home_framing, ratio
        self.faces = []
        self.count = 0
        self.state = "looking"
        self.reset()

    def reset(self):
        self._smooth = None
        self._emitted = None
        self._last_seen = None
        self._homed = False
        self.state = "looking"
        self.faces, self.count = [], 0

    def forget_framing(self):
        self._emitted = None

    def feed(self, t, faces):
        if not _finite(t):
            return None
        good = []
        try:
            faces = list(faces or ())
        except TypeError:
            faces = []
        for f in faces:
            try:
                fx, fy, fw, fh = (float(v) for v in f)
            except (TypeError, ValueError):
                continue
            if _finite(fx, fy, fw, fh) and fw >= MIN_FACE_WIDTH and fh > 0:
                good.append((fx, fy, fw, fh))
        self.faces, self.count = good, len(good)
        if not good:
            return self._nobody(t)
        self._last_seen, self._homed = t, False
        self.state = "tracking"
        x0, y0 = min(f[0] for f in good), min(f[1] for f in good)
        x1, y1 = max(f[0] + f[2] for f in good), max(f[1] + f[3] for f in good)
        raw = ((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)
        if self._smooth is None:
            self._smooth = raw
        else:
            a = self.smoothing
            self._smooth = tuple(s + a * (r - s) for s, r in zip(self._smooth, raw))
        return self._decide(len(good) > 1)

    def _nobody(self, t):
        if self._smooth is None and not self._homed:
            self.state = "looking"
            return None
        if self._homed:
            self.state = "home"
            return None
        if t - self._last_seen < self.lost_s:
            self.state = "holding"
            return None
        self._homed = True
        self.state = "home"
        self._smooth = None
        self._emitted = None
        return tuple(self.home_framing) if self.home_framing else None

    def _wanted(self, group):
        cx, cy, uw, uh = self._smooth
        r = self.ratio
        fw = uh / r / self.face_share
        if group:
            fw = max(fw, uw / GROUP_WIDTH_SHARE)
        zoom = min(self.zoom_max, max(1.0, (2 / 3) / fw)) if fw > 0 else self.zoom_max
        fw = (2 / 3) / zoom
        wh = fw * r
        left = min(max(cx - fw / 2, 0.0), max(0.0, 1 - fw))
        top = min(max(cy - EYE_LINE * wh, 0.0), max(0.0, 1 - wh))
        pan = 2 * left / (1 - fw) - 1 if fw < 1 else 0.0
        tilt = 2 * top / (1 - wh) - 1 if wh < 1 else 0.0
        return zoom, max(-1.0, min(1.0, pan)), max(-1.0, min(1.0, tilt))

    def _decide(self, group):
        want = self._wanted(group)
        cx, cy = self._smooth[0], self._smooth[1]
        if self._emitted is not None:
            (ez, _, _), (ex, ey) = self._emitted
            moved = math.hypot(cx - ex, (cy - ey) * 9 / 16)
            if moved <= self.deadzone and abs(want[0] / ez - 1) <= ZOOM_CHANGE:
                return None
        self._emitted = (want, (cx, cy))
        return want


class _Result(QObject):
    done = Signal(int, object)

    def __init__(self, parent):
        super().__init__(parent)


def _detect(detector, gray):
    try:
        return detector.detect(gray)
    except Exception:
        log.exception("face detection failed")
        return None


class _Job(threading.Thread):
    def __init__(self, gen, gray, detector, result):
        super().__init__(name="lark-autoframe", daemon=True)
        self.gen, self.gray, self.detector, self.result = gen, gray, detector, result

    def run(self):
        self.result.done.emit(self.gen, _detect(self.detector, self.gray))


class AutoFrame(QObject):
    changed = Signal()

    def __init__(self, ctx, clock=time.monotonic):
        super().__init__()
        self.ctx, self.clock = ctx, clock
        self.detector, self.reason = make_detector(ctx.config)
        self.tracker = Tracker()
        self.faces = []
        self._busy = False
        self.sync = False
        self._job = None
        self._size = (0, 0)
        self._gen = 0
        self._paused_until = 0.0
        self._result = _Result(self)
        self._result.done.connect(self._detected)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        ctx.store.conn_changed.connect(lambda *_: self._sync())
        ctx.eptz.manual_framing.connect(self._manual)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._join)
        self._sync()

    @property
    def store(self):
        return self.ctx.store

    @property
    def cfg(self):
        return self.ctx.config

    @property
    def available(self):
        return self.detector is not None

    @property
    def on(self):
        return bool(self.cfg.get("autoframe_on")) and self.available

    def set_on(self, on):
        on = bool(on)
        if on and not self.available:
            self.store.toast.emit(f"Auto-frame is not available. {self.reason}", True)
            return False
        self.cfg.set("autoframe_on", on)
        try:
            self.cfg.save()
        except OSError as e:
            self.store.toast.emit(f"Could not write the config: {e.strerror}", True)
        self._sync()
        return True

    def toggle(self):
        return self.set_on(not self.on)

    def _active(self):
        return self.on and self.store.connected

    def _sync(self):
        if self._active():
            self._timer.setInterval(self.cfg.get("autoframe_interval_ms"))
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._join()
            self._gen += 1
            self._busy = False
            self.tracker.reset()
            self.faces = []
        self.changed.emit()

    def _join(self):
        job, self._job = self._job, None
        if job is not None and job.is_alive():
            job.join(2)

    def _manual(self):
        pause = self.cfg.get("autoframe_manual_pause_s")
        if self.on and pause > 0:
            self._paused_until = self.clock() + pause
            self.tracker.forget_framing()
            self.changed.emit()

    @property
    def paused(self):
        return self.clock() < self._paused_until

    @property
    def status(self):
        if not self.available:
            return self.reason
        if not self.on:
            return "Off"
        if not self.store.connected or not self.ctx.eptz.ready:
            return "Waiting for OBS"
        if self.paused:
            return "Paused after a manual move"
        t = self.tracker
        if t.state == "tracking":
            return f"Tracking {t.count} face" + ("" if t.count == 1 else "s")
        if t.state == "holding":
            return "Lost the face, holding"
        if t.state == "home":
            return f"Nobody in frame, back to {self.cfg.get('autoframe_home')}"
        return "Looking for a face"

    def _can_move(self):
        return self._active() and self.ctx.eptz.ready and not self.store.cam_dark and not self.paused

    def tick(self):
        interval = self.cfg.get("autoframe_interval_ms")
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        if self._busy or not self._can_move():
            return
        self._busy = True
        gen = self._gen
        req = {"sourceName": self.cfg.get("camera_input"), "imageFormat": "png", "imageWidth": SHOT_WIDTH}
        self.store.bridge.request("GetSourceScreenshot", req, lambda ok, p: self._shot(gen, ok, p))

    def _shot(self, gen, ok, payload):
        if gen != self._gen:
            return
        data = payload.get("imageData", "") if ok and isinstance(payload, dict) else ""
        decoded = gray_from_png(data) if data else None
        if decoded is None:
            self._busy = False
            return
        gray, w, h = decoded
        self._size = (w, h)
        if self.sync:
            return self._detected(gen, _detect(self.detector, gray))
        self._job = _Job(gen, gray, self.detector, self._result)
        self._job.start()

    def _detected(self, gen, boxes):
        if gen != self._gen:
            return
        self._busy = False
        w, h = self._size
        if boxes is None or not self._can_move():
            return
        fractions = [(x / w, y / h, bw / w, bh / h) for x, y, bw, bh in boxes]
        e, t, c = self.ctx.eptz, self.tracker, self.cfg
        t.smoothing, t.face_share = c.get("autoframe_smoothing"), c.get("autoframe_face_share")
        t.zoom_max, t.deadzone, t.lost_s = c.get("autoframe_zoom_max"), c.get("autoframe_deadzone"), c.get("autoframe_lost_s")
        t.home = c.get("autoframe_home")
        t.home_framing = e.presets().get(t.home)
        sw, sh = e.src
        t.ratio = (sw * 9) / (sh * 16)
        move = t.feed(self.clock(), fractions)
        self.faces = list(t.faces)
        if move is not None:
            e.set_framing(*move, animate=True, auto=True)
        self.changed.emit()
