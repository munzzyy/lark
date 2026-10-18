"""Deck actions, button styling and the Linux joystick reader. The page lives in pages/deck.py."""
import logging
import os
import select
import struct
import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from .preflight import can_go_live, run_checks

log = logging.getLogger("lark.deck")

JS_DEVICE = "/dev/input/js0"
JS_EVENT = struct.Struct("IhBB")
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

# Xbox 360 pad through the xpad driver
PAD_BUTTONS = {0: "A", 1: "B", 2: "X", 3: "Y", 4: "LB", 5: "RB", 6: "Back", 7: "Start", 8: "Guide", 9: "L3", 10: "R3"}
PAD_NAMES = list(PAD_BUTTONS.values()) + ["DUp", "DDown", "DLeft", "DRight"]
HAT_X, HAT_Y = 6, 7
HAT_THRESHOLD = 16000

STOP_CONFIRM_S = 3.0

DEFAULT_COLORS = {"scene": "#3b4a8c", "mic": "#8c3b4a", "desktop": "#8c3b4a", "replay": "#3b7a8c", "live": "#1f8f5f",
                  "record": "#8c5a3b", "alert": "#7c5cff", "countdown": "#6b4aa8",
                  "frame": "#2f6f6a"}
DEFAULT_LABELS = {"mic": "Mic mute", "desktop": "Desktop mute", "replay": "Save replay", "live": "Go live",
                  "record": "Record", "alert": "Alert test", "countdown": "Starting-soon countdown"}
FIXED_KEYS = ["mic", "desktop", "replay", "live", "record", "alert", "countdown"]


class PadParser:
    """Turns js_event bytes into pad button presses. Keeps a partial-event buffer between feeds."""

    def __init__(self):
        self._buf = b""
        self._hat = {HAT_X: 0, HAT_Y: 0}

    def feed(self, data):
        self._buf += data
        out = []
        while len(self._buf) >= JS_EVENT.size:
            chunk, self._buf = self._buf[:JS_EVENT.size], self._buf[JS_EVENT.size:]
            _, value, etype, number = JS_EVENT.unpack(chunk)
            if etype & JS_EVENT_INIT:
                if etype & JS_EVENT_AXIS and number in self._hat:
                    self._hat[number] = self._dir(value)
                continue
            if etype == JS_EVENT_BUTTON:
                if value == 1 and number in PAD_BUTTONS:
                    out.append(PAD_BUTTONS[number])
            elif etype == JS_EVENT_AXIS and number in self._hat:
                d = self._dir(value)
                if d != self._hat[number] and d != 0:
                    names = ("DLeft", "DRight") if number == HAT_X else ("DUp", "DDown")
                    out.append(names[0] if d < 0 else names[1])
                self._hat[number] = d
        return out

    @staticmethod
    def _dir(value):
        return -1 if value <= -HAT_THRESHOLD else 1 if value >= HAT_THRESHOLD else 0


class PadReader(QObject):
    """Reads the pad in a daemon thread. The device being absent is a normal state, not an error."""
    pressed = Signal(str)
    status = Signal(str)

    def __init__(self, path=JS_DEVICE, retry_s=2.0, parent=None):
        super().__init__(parent)
        self.path = path
        self.retry_s = retry_s
        self._stop = threading.Event()
        self._thread = None
        self.state = "no pad"

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="lark-pad", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def _set(self, state):
        if state != self.state:
            self.state = state
            self.status.emit(state)

    def _run(self):
        while not self._stop.is_set():
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                self._set("no pad")
                self._stop.wait(self.retry_s)
                continue
            self._set("pad connected")
            parser = PadParser()
            try:
                while not self._stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 0.3)
                    if not ready:
                        continue
                    data = os.read(fd, JS_EVENT.size * 16)
                    if not data:
                        break
                    for name in parser.feed(data):
                        self.pressed.emit(name)
            except OSError as e:
                log.info("pad read ended: %s", e)
            finally:
                os.close(fd)
            self._set("no pad")
            self._stop.wait(self.retry_s)


def contrast_text(color):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#10131a" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#f2f4f8"


class DeckActions:
    """What each deck key does and how it looks right now. Keys: scene:<name> plus FIXED_KEYS."""

    def __init__(self, ctx, clock=time.monotonic):
        self.ctx = ctx
        self.clock = clock
        self._stop_armed_at = None

    @property
    def store(self):
        return self.ctx.store

    def alerts(self):
        a = getattr(self.ctx, "alerts", None)
        if a is not None and callable(getattr(a, "test_alert", None)):
            return a
        sv = getattr(self.ctx, "services", None)
        if sv is not None and callable(getattr(sv, "test_alert", None)):
            return SimpleNamespace(test_alert=lambda: sv.test_alert("follow"))
        return None

    def _framing(self):
        f = getattr(self.ctx, "eptz", None)
        return f if f is not None else SimpleNamespace(deck_keys=lambda: [])

    def keys(self):
        ks = [f"scene:{n}" for n in self.store.scenes] + FIXED_KEYS + self._framing().deck_keys()
        return [k for k in ks if k != "alert" or self.alerts()]

    def default_label(self, key):
        if key.startswith("frame:"):
            return self.ctx.eptz.deck_label(key)
        return key[6:] if key.startswith("scene:") else DEFAULT_LABELS[key]

    def default_color(self, key):
        return DEFAULT_COLORS["scene" if key.startswith("scene:") else "frame" if key.startswith("frame:") else key]

    def look(self, key, style):
        st = style.get(key, {})
        label = st.get("label") or self.default_label(key)
        color = st.get("color") or self.default_color(key)
        return label, color

    def state(self, key):
        """(lit, small text under the label)"""
        s = self.store
        cfg = self.ctx.config
        if key.startswith("scene:"):
            live = s.current_scene == key[6:]
            return live, "on air" if live else ""
        if key.startswith("frame:"):
            lit = self.ctx.eptz.deck_lit(key)
            return lit, ("on" if key in ("frame:quality", "frame:auto") else "framed") if lit else ""
        if key == "mic":
            muted = s.inputs.get(cfg.get("mic_input"), {}).get("muted", False)
            return muted, "muted" if muted else ""
        if key == "desktop":
            muted = s.inputs.get(cfg.get("desktop_input"), {}).get("muted", False)
            return muted, "muted" if muted else ""
        if key == "replay":
            return s.replay, "buffer on" if s.replay else "starts buffer"
        if key == "live":
            if s.stream["active"]:
                return True, "press twice to stop"
            return False, ""
        if key == "record":
            return s.record["active"], "recording" if s.record["active"] else ""
        return False, ""

    def _toast(self, msg, bad=False):
        self.store.toast.emit(msg, bad)

    def run(self, key):
        s = self.store
        if not s.connected and key != "countdown":
            return self._toast("OBS is not connected", True)
        if key.startswith("scene:"):
            return s.switch_scene(key[6:])
        if key.startswith("frame:"):
            return self.ctx.eptz.deck_run(key)
        if key in ("mic", "desktop"):
            name = self.ctx.config.get("mic_input" if key == "mic" else "desktop_input")
            if name not in s.inputs:
                return self._toast(f"No input called {name}", True)
            return s.set_mute(name, not s.inputs[name].get("muted", False))
        if key == "replay":
            return s.save_replay()
        if key == "record":
            return s.toggle_record()
        if key == "live":
            return self._live()
        if key == "alert":
            a = self.alerts()
            return a.test_alert() if a else self._toast("Alerts are not set up", True)
        if key == "countdown":
            cfg = self.ctx.config
            minutes = cfg.get("deck_countdown_minutes")
            self.ctx.services.set_starting(cfg.get("starting_title"), minutes)
            return self._toast(f"Countdown started, {minutes} min")
        self._toast(f"Unknown deck button {key}", True)

    def _live(self):
        s = self.store
        if s.stream["active"]:
            now = self.clock()
            if self._stop_armed_at is not None and now - self._stop_armed_at <= STOP_CONFIRM_S:
                self._stop_armed_at = None
                return s.stop_stream()
            self._stop_armed_at = now
            return self._toast("Press again to stop the stream")
        self._stop_armed_at = None
        cfg, tw = self.ctx.config, self.ctx.services.twitch_state
        mic = cfg.get("mic_input")
        checks = run_checks(connected=s.connected, scene=s.current_scene, mic_name=mic, mic=s.inputs.get(mic),
                            mic_signal=s.has_signal(mic), camera_present=s.camera_present(),
                            stream_key_set=s.stream_key_set, twitch_connected=tw["status"] == "connected",
                            twitch_title=(tw["channel"] or {}).get("title", ""))
        if not can_go_live(checks):
            return self._toast("Pre-flight is blocking Go Live", True)
        s.start_stream()
