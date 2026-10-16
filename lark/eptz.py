"""Pan, tilt and zoom done in OBS by moving the crop of the Camera scene item, plus the camera mode (Stream or Quality).

The camera has no motors, so framing is a window cut out of the full frame. Zoom 1.0x is the
facecam crop Lark found when it was built (two thirds of the frame width), 4.0x is a quarter of that.
Pan and tilt run from -1 to 1 across whatever room the window has left in the frame.
"""
import logging
import math
import time

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger("lark.eptz")

ZOOM_MIN, ZOOM_MAX = 1.0, 4.0
TICK_MS = 16
CONFIRM_S = 8.0
RES_1080, FPS_60 = 125830200, 65596
RES_4K, FPS_30 = 251660400, 65566
QUALITY_NOTE = ("Quality mode reads the camera at 3840x2160 and 30 fps, decoded from MJPG on the CPU. "
                "OBS downscales it to the facecam box, so it is sharper but costs a lot more CPU.")
MODE_STREAM, MODE_QUALITY = "Stream", "Quality"
MODE_DETAIL = {MODE_STREAM: "1080p60", MODE_QUALITY: "4K30"}

BUILTIN_PRESETS = {"Face": (2.0, 0.0, -0.35), "Wide": (1.0, 0.0, 0.0), "Desk": (1.5, 0.0, 1.0)}

# camera zoom_absolute -> magnification, from the fork's table
ZOOM_TABLE = ((0, 1.0), (165, 1.5), (170, 1.6), (260, 2.0), (320, 2.4), (400, 3.2))


def zoom_factor(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(v):
        return 1.0
    if v <= ZOOM_TABLE[0][0]:
        return ZOOM_TABLE[0][1]
    for (x0, y0), (x1, y1) in zip(ZOOM_TABLE, ZOOM_TABLE[1:]):
        if v <= x1:
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return ZOOM_TABLE[-1][1]


def clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


def window_size(src_w, src_h, zoom):
    zoom = clamp(zoom, ZOOM_MIN, ZOOM_MAX, 1.0)
    w = int((src_w * 2 // 3) / zoom) // 16 * 16
    w = min(w, src_w // 16 * 16, (src_h * 16 // 9) // 16 * 16)
    w = max(16, w)
    return w, w * 9 // 16


def frame_crop(src_w, src_h, zoom, pan, tilt):
    w, h = window_size(src_w, src_h, zoom)
    ax, ay = max(0, src_w - w), max(0, src_h - h)
    x = min(ax, max(0, round(ax * (1 + clamp(pan, -1, 1, 0.0)) / 2)))
    y = min(ay, max(0, round(ay * (1 + clamp(tilt, -1, 1, 0.0)) / 2)))
    return {"cropLeft": x, "cropRight": src_w - w - x, "cropTop": y, "cropBottom": src_h - h - y}


def infer_framing(src_w, src_h, crop):
    """Read zoom, pan and tilt back out of a crop. None when the crop makes no sense for this frame."""
    try:
        l, r, t, b = (int(crop[k]) for k in ("cropLeft", "cropRight", "cropTop", "cropBottom"))
    except (KeyError, TypeError, ValueError):
        return None
    w, h = src_w - l - r, src_h - t - b
    if min(l, r, t, b) < 0 or w <= 0 or h <= 0:
        return None
    zoom = clamp((src_w * 2 // 3) / w, ZOOM_MIN, ZOOM_MAX, 1.0)
    ax, ay = src_w - w, src_h - h
    pan = clamp(2 * l / ax - 1, -1, 1, 0.0) if ax else 0.0
    tilt = clamp(2 * t / ay - 1, -1, 1, 0.0) if ay else 0.0
    return zoom, pan, tilt


def unpack_res(v):
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        w, h = v >> 16, v & 0xFFFF
        return (w, h) if w and h else None
    if isinstance(v, str) and "x" in v:
        try:
            w, h = (int(p) for p in v.split("x", 1))
        except ValueError:
            return None
        return (w, h) if w > 0 and h > 0 else None
    return None


def ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


class Eptz(QObject):
    changed = Signal()
    mode_changed = Signal()
    confirm_needed = Signal(bool)

    def __init__(self, ctx, clock=time.monotonic):
        super().__init__()
        self.ctx = ctx
        self.clock = clock
        self.zoom, self.pan, self.tilt = 1.0, 0.0, 0.0
        self.src = None
        self.fourk = None
        self.scene = ""
        self.item_id = None
        self.status = "Waiting for OBS"
        self._gen = 0
        self._inflight = False
        self._dirty = False
        self._tween = None
        self._confirm = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(lambda: self.tick())
        s = ctx.store
        s.conn_changed.connect(lambda *_: self.refresh())
        s.scenes_changed.connect(self._scene_event)

    @property
    def bridge(self):
        return self.ctx.bridge

    @property
    def store(self):
        return self.ctx.store

    @property
    def cfg(self):
        return self.ctx.config

    @property
    def mode(self):
        """Stream, Quality, or None until OBS has been read."""
        return None if self.fourk is None else MODE_QUALITY if self.fourk else MODE_STREAM

    def mode_text(self):
        m = self.mode
        return "" if m is None else f"{m} ({MODE_DETAIL[m]})"

    @property
    def ready(self):
        return self.item_id is not None and self.src is not None

    def _toast(self, msg, bad=False):
        self.store.toast.emit(msg, bad)

    def _scene_event(self):
        if self.store.current_scene != self.scene:
            self.refresh()

    def _set_status(self, text):
        self.status = text
        self.changed.emit()

    def refresh(self):
        """Find the Camera item in the current scene and read its crop and the camera's size from OBS."""
        self._gen += 1
        gen = self._gen
        self._stop_tween()
        self.item_id = None
        if not self.store.connected:
            self.scene = ""
            return self._set_status("OBS is not connected")
        self.scene = scene = self.store.current_scene
        name = self.cfg.get("eptz_item")
        if not scene:
            return self._set_status("No current scene yet")

        def got_id(ok, p):
            if gen != self._gen:
                return
            if not ok:
                return self._set_status(f"No {name} item in {scene}")
            self.item_id = p.get("sceneItemId")
            self.bridge.request("GetInputSettings", {"inputName": name}, got_settings)

        def got_settings(ok, p):
            if gen != self._gen:
                return
            res = unpack_res((p.get("inputSettings") or {}).get("resolution")) if ok else None
            self.fourk = (res[0] >= 3840) if res else None
            self.bridge.request("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": self.item_id},
                                lambda ok2, p2: got_transform(ok2, p2, res))

        def got_transform(ok, p, res):
            if gen != self._gen:
                return
            if not ok:
                self.item_id = None
                return self._set_status(f"Could not read the {name} item: {p}")
            t = p.get("sceneItemTransform") or {}
            size = res or ((t.get("sourceWidth"), t.get("sourceHeight")) if t.get("sourceWidth") else None)
            if not size or not size[0] or not size[1]:
                self.item_id = None
                return self._set_status("Could not read the camera size from OBS")
            self.src = (int(size[0]), int(size[1]))
            inferred = infer_framing(*self.src, t)
            if inferred:
                self.zoom, self.pan, self.tilt = inferred
            self.mode_changed.emit()
            self._set_status("Ready")

        self.bridge.request("GetSceneItemId", {"sceneName": scene, "sourceName": name}, got_id)

    # presets

    def presets(self):
        out = dict(BUILTIN_PRESETS)
        for n, v in self.cfg.get("eptz_presets").items():
            out[n] = (v["zoom"], v["pan"], v["tilt"])
        return out

    def user_presets(self):
        return set(self.cfg.get("eptz_presets"))

    def save_preset(self, name):
        name = (name or "").strip()
        if not 1 <= len(name) <= 24 or not name.isprintable():
            self._toast("Preset names are 1 to 24 characters", True)
            return False
        mine = dict(self.cfg.get("eptz_presets"))
        mine[name] = {"zoom": round(self.zoom, 3), "pan": round(self.pan, 3), "tilt": round(self.tilt, 3)}
        return self._store_presets(mine, f"Saved framing {name}")

    def delete_preset(self, name):
        mine = dict(self.cfg.get("eptz_presets"))
        if name not in mine:
            return False
        del mine[name]
        return self._store_presets(mine, f"Removed framing {name}")

    def _store_presets(self, mine, msg):
        old = self.cfg.get("eptz_presets")
        try:
            self.cfg.set("eptz_presets", mine)
            self.cfg.save()
        except (ValueError, OSError) as e:
            self.cfg.values["eptz_presets"] = old
            self._toast(f"Could not save the framing: {e}", True)
            return False
        self._toast(msg)
        self.changed.emit()
        return True

    def apply_preset(self, name):
        p = self.presets().get(name)
        if p is None:
            self._toast(f"No framing called {name}", True)
            return False
        return self.set_framing(*p)

    def reset(self):
        return self.set_framing(1.0, 0.0, 0.0)

    # framing

    def duration_s(self):
        return self.cfg.get("eptz_duration_ms") / 1000.0

    def set_framing(self, zoom=None, pan=None, tilt=None, animate=True):
        if not self.ready:
            self._toast(self.status if self.status != "Ready" else "The camera is not ready yet", True)
            return False
        end = (clamp(self.zoom if zoom is None else zoom, ZOOM_MIN, ZOOM_MAX, self.zoom),
               clamp(self.pan if pan is None else pan, -1, 1, self.pan),
               clamp(self.tilt if tilt is None else tilt, -1, 1, self.tilt))
        dur = self.duration_s()
        if not animate or dur <= 0:
            self._stop_tween()
            self.zoom, self.pan, self.tilt = end
            self._push()
            self.changed.emit()
            return True
        self._tween = {"from": (self.zoom, self.pan, self.tilt), "to": end, "t0": self.clock(), "dur": dur}
        if not self._timer.isActive():
            self._timer.start()
        return True

    def nudge(self, dzoom=0.0, dpan=0.0, dtilt=0.0):
        end = self._tween["to"] if self._tween else (self.zoom, self.pan, self.tilt)
        return self.set_framing(end[0] + dzoom, end[1] + dpan, end[2] + dtilt, animate=False)

    def tick(self, now=None):
        tw = self._tween
        if tw is None:
            self._timer.stop()
            return
        now = self.clock() if now is None else now
        k = (now - tw["t0"]) / tw["dur"]
        if k >= 1:
            self.zoom, self.pan, self.tilt = tw["to"]
            self._stop_tween()
        else:
            e = ease(k)
            self.zoom, self.pan, self.tilt = (a + (b - a) * e for a, b in zip(tw["from"], tw["to"]))
        self._push()
        self.changed.emit()

    def _stop_tween(self):
        self._tween = None
        self._timer.stop()

    def window_fraction(self):
        sw, sh = self.src or (1920, 1080)
        w, h = window_size(sw, sh, self.zoom)
        return w / sw, h / sh

    def _push(self):
        if not self.ready:
            return
        if self._inflight:
            self._dirty = True
            return
        self._inflight = True
        crop = frame_crop(*self.src, self.zoom, self.pan, self.tilt)
        gen = self._gen

        def done(ok, p):
            self._inflight = False
            if not ok and gen == self._gen:
                self._dirty = False
                self._toast(f"Framing failed: {p}", True)
            elif self._dirty:
                self._dirty = False
                self._push()
        self.bridge.request("SetSceneItemTransform", {
            "sceneName": self.scene, "sceneItemId": self.item_id, "sceneItemTransform": crop}, done)

    # 4K

    def busy_output(self):
        s = self.store
        if s.stream["active"] and s.record["active"]:
            return "streaming and recording"
        return "streaming" if s.stream["active"] else "recording" if s.record["active"] else ""

    def set_4k(self, on, force=False):
        """Returns started, noop, offline or refused (a refusal arms a confirmation for CONFIRM_S)."""
        on = bool(on)
        if not self.store.connected:
            self._toast("OBS is not connected", True)
            return "offline"
        if self.fourk is on:
            self._toast("Already in Quality mode" if on else "Already in Stream mode")
            return "noop"
        busy = self.busy_output()
        armed = self._confirm is not None and self._confirm[0] == on and self.clock() <= self._confirm[1]
        if busy and not (force or armed):
            self._confirm = (on, self.clock() + CONFIRM_S)
            self._toast(f"Not switching while {busy}: OBS drops the camera for a moment. Confirm to switch anyway.", True)
            self.confirm_needed.emit(on)
            return "refused"
        self._confirm = None
        self._switch(on)
        return "started"

    def _switch(self, on):
        name = self.cfg.get("eptz_item")
        res, fps = (RES_4K, FPS_30) if on else (RES_1080, FPS_60)
        want = unpack_res(res)
        self._stop_tween()
        self._gen += 1

        def fail(msg):
            self._toast(msg, True)
            self.refresh()

        def got_old(ok, p):
            if not ok:
                return fail(f"Could not read the camera in OBS: {p}")
            old = unpack_res((p.get("inputSettings") or {}).get("resolution")) or self.src
            self.bridge.request("SetInputSettings", {"inputName": name, "inputSettings": {
                "resolution": res, "framerate": fps}, "overlay": True}, lambda ok2, p2: set_done(ok2, p2, old))

        def set_done(ok, p, old):
            if not ok:
                return fail(f"OBS refused the camera change: {p}")
            self.bridge.request("GetInputSettings", {"inputName": name}, lambda ok2, p2: confirmed(ok2, p2, old))

        def confirmed(ok, p, old):
            s = (p.get("inputSettings") or {}) if ok else {}
            if not ok or s.get("resolution") != res or s.get("framerate") != fps:
                return fail("OBS did not keep the new camera mode")
            if self.item_id is None or self.scene != self.store.current_scene:
                self._done(on)
                return self.refresh()
            self.bridge.request("GetSceneItemTransform", {"sceneName": self.scene, "sceneItemId": self.item_id},
                                lambda ok2, p2: got_crop(ok2, p2, old))

        def got_crop(ok, p, old):
            if not ok:
                return fail(f"Could not read the crop: {p}")
            t = p.get("sceneItemTransform") or {}
            k = want[0] / old[0] if old and old[0] else 1.0
            crop = {key: max(0, round(int(t.get(key, 0)) * k))
                    for key in ("cropLeft", "cropRight", "cropTop", "cropBottom")}
            self.bridge.request("SetSceneItemTransform", {"sceneName": self.scene, "sceneItemId": self.item_id,
                                                          "sceneItemTransform": crop},
                                lambda ok2, p2: scaled(ok2, p2, crop))

        def scaled(ok, p, crop):
            if not ok:
                return fail(f"Could not scale the crop: {p}")
            self.src = want
            inferred = infer_framing(*want, crop)
            if inferred:
                self.zoom, self.pan, self.tilt = inferred
            self._done(on)

        self.bridge.request("GetInputSettings", {"inputName": name}, got_old)

    def _done(self, on):
        self.fourk = on
        self._toast("Quality mode on, 3840x2160 at 30 fps" if on else "Stream mode on, 1920x1080 at 60 fps")
        self.mode_changed.emit()
        self.changed.emit()

    # deck

    QUALITY_KEY = "frame:quality"
    DECK_NUDGES = {"zoom_in": ("Zoom in", dict(dzoom=0.25)), "zoom_out": ("Zoom out", dict(dzoom=-0.25)),
                   "left": ("Pan left", dict(dpan=-0.15)), "right": ("Pan right", dict(dpan=0.15)),
                   "up": ("Tilt up", dict(dtilt=-0.15)), "down": ("Tilt down", dict(dtilt=0.15))}

    def deck_keys(self):
        return ([f"frame:preset:{n}" for n in self.presets()] + ["frame:reset"]
                + [f"frame:{k}" for k in self.DECK_NUDGES] + [self.QUALITY_KEY])

    def deck_label(self, key):
        k = key[6:]
        if key == self.QUALITY_KEY:
            return "Quality mode"
        if k.startswith("preset:"):
            return k[7:]
        return "Reset framing" if k == "reset" else self.DECK_NUDGES[k][0]

    def deck_lit(self, key):
        if key == self.QUALITY_KEY:
            return self.fourk is True
        k = key[6:]
        p = self.presets().get(k[7:]) if k.startswith("preset:") else None
        return bool(p) and self.ready and all(abs(a - b) < 0.02 for a, b in zip(p, (self.zoom, self.pan, self.tilt)))

    def deck_run(self, key):
        k = key[6:]
        if key == self.QUALITY_KEY:
            if self.fourk is None:
                self._toast(self.status if self.status != "Ready" else "The camera mode is not known yet", True)
                return False
            return self.set_4k(not self.fourk)
        if k == "reset":
            return self.reset()
        if k in self.DECK_NUDGES:
            return self.nudge(**self.DECK_NUDGES[k][1])
        if k.startswith("preset:"):
            return self.apply_preset(k[7:])
        self._toast(f"Unknown framing button {k}", True)
        return False
