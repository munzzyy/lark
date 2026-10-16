"""Local clip highlights: spot chat spikes, save the OBS replay buffer, keep a log the Shorts pipeline reads."""
import json
import logging
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger("lark.highlights")

WINDOW_S = 10
HISTORY_WINDOWS = 12
WARMUP_WINDOWS = 3
CONTEXT_LINES = 20
FILE_NAME = "highlights.json"
TRIGGERS = ("chat_spike", "manual", "hotkey", "command")
COMMAND = "!clip"
COMMAND_BADGES = ("broadcaster", "moderator")
POLL_TRIES = 8
POLL_EVERY_MS = 600
THUMB_TIMEOUT_S = 20


class SpikeDetector:
    """Fires when messages in the last 10 s pass multiplier times the rolling median of recent 10 s windows."""

    def __init__(self, params):
        """params() -> (multiplier, cooldown_s, min_messages), read on every message so settings apply live."""
        self.params = params
        self.stamps = deque()
        self.buckets = {}
        self.first_idx = None
        self.last_fire = None
        self.peak = 0

    def baseline(self, idx):
        prev = [self.buckets.get(i, 0) for i in range(idx - HISTORY_WINDOWS, idx)]
        return statistics.median(prev)

    @property
    def rate(self):
        return len(self.stamps)

    def add(self, t):
        """Record a message at time t. Returns True when this message tips chat into a spike."""
        mult, cooldown, floor = self.params()
        idx = int(t // WINDOW_S)
        if self.first_idx is None:
            self.first_idx = idx
        self.buckets[idx] = self.buckets.get(idx, 0) + 1
        for old in [i for i in self.buckets if i < idx - HISTORY_WINDOWS - 1]:
            del self.buckets[old]
        self.stamps.append(t)
        while self.stamps and self.stamps[0] <= t - WINDOW_S:
            self.stamps.popleft()
        self.peak = max(self.peak, self.rate)
        if idx - self.first_idx < WARMUP_WINDOWS:
            return False
        if self.last_fire is not None and t - self.last_fire < cooldown:
            return False
        if self.rate >= max(floor, mult * self.baseline(idx)):
            self.last_fire = t
            return True
        return False


def make_thumbnail(ffmpeg, src, dst, timeout=THUMB_TIMEOUT_S):
    """One frame near the end of src as a small jpeg. False when src is missing or ffmpeg fails."""
    if not ffmpeg or not src or not os.path.isabs(src) or not os.path.isfile(src):
        return False
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    base = [ffmpeg, "-nostdin", "-v", "error", "-y"]
    tail = ["-i", src, "-an", "-frames:v", "1", "-vf", "scale=320:-2", str(dst)]
    for seek in (["-sseof", "-3"], []):
        try:
            subprocess.run(base + seek + tail, capture_output=True, timeout=timeout, check=True)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("no thumbnail for %s (%s)", src, getattr(e, "returncode", type(e).__name__))
        else:
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return True
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
    return False


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hl-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    """Parsed json, None when missing. An unreadable file is moved aside so it is not overwritten."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        bad = Path(str(path) + ".bad")
        log.error("%s unreadable (%s), moving it to %s", path, e, bad)
        try:
            os.replace(path, bad)
        except OSError as e2:
            log.error("could not move it aside: %s", e2)
        return None


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


class HighlightManager(QObject):
    changed = Signal()
    _thumb_done = Signal(str, object)

    def __init__(self, ctx, ffmpeg=None):
        super().__init__()
        self.ctx = ctx
        self.config = ctx.config
        self.store = ctx.store
        self.ffmpeg = ffmpeg if ffmpeg is not None else (shutil.which("ffmpeg") or "/usr/bin/ffmpeg")
        self.detector = SpikeDetector(self._params)
        self.context = deque(maxlen=CONTEXT_LINES)
        self.entries = []
        self.session = None
        self._pending = []
        self._known_last = None
        self._thumbing = set()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save)
        self._thumb_done.connect(self._on_thumb)
        self.load()
        ctx.services.chat_message.connect(self.on_chat)
        ctx.bridge.event.connect(self.on_event)
        self.store.conn_changed.connect(self._conn)

    def _params(self):
        c = self.config
        return c.get("highlights_multiplier"), c.get("highlights_cooldown"), c.get("highlights_min_messages")

    @property
    def folder(self):
        return Path(self.config.get("highlights_dir")).expanduser()

    @property
    def path(self):
        return self.folder / FILE_NAME

    @property
    def thumb_dir(self):
        return self.folder / "highlight-thumbs"

    def load(self):
        data = read_json(self.path)
        items = data.get("highlights") if isinstance(data, dict) else None
        self.entries = [e for e in items or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]

    def save(self):
        self._save_timer.stop()
        try:
            write_json(self.path, {"version": 1, "highlights": self.entries})
        except OSError as e:
            log.error("could not write %s: %s", self.path, e)
            self.store.toast.emit(f"Could not save highlights: {e.strerror or e}", True)

    def find(self, entry_id):
        return next((e for e in self.entries if e["id"] == entry_id), None)

    def update(self, entry_id, keep_for_shorts=None, notes=None):
        e = self.find(entry_id)
        if e is None:
            return
        if keep_for_shorts is not None:
            e["keep_for_shorts"] = bool(keep_for_shorts)
        if notes is not None:
            e["notes"] = str(notes)[:2000]
        self._save_timer.start()

    def delete(self, entry_id):
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        self._pending = [p for p in self._pending if p != entry_id]
        self.save()
        self.changed.emit()

    def mark(self, trigger="manual"):
        if trigger not in TRIGGERS:
            raise ValueError(trigger)
        s = self.store
        now = time.time()
        entry = {"id": f"{int(now * 1000):x}{os.urandom(2).hex()}", "time": iso(now), "ts": round(now, 3),
                 "trigger": trigger, "chat": list(self.context),
                 "replay": None, "thumbnail": None, "keep_for_shorts": False, "notes": ""}
        self.entries.append(entry)
        if self.session is not None:
            self.session["highlights"] += 1
        self.save()
        self.changed.emit()
        if s.connected and s.replay:
            self._save_replay(entry["id"])
        elif trigger == "hotkey" and s.connected:
            s.save_replay()
        elif trigger != "chat_spike":
            s.toast.emit("Marked. Start the replay buffer to get a clip next time", False)
        return entry

    def _save_replay(self, entry_id):
        self._pending.append(entry_id)

        def cb(ok, payload):
            if not ok:
                self._pending = [p for p in self._pending if p != entry_id]
                self.store.toast.emit(f"Save replay: {payload}", True)
                return
            self._poll_last(entry_id, 0)
        self.store.bridge.request("SaveReplayBuffer", None, cb)

    def _poll_last(self, entry_id, attempt):
        if entry_id not in self._pending:
            return

        def cb(ok, payload):
            if entry_id not in self._pending:
                return
            path = payload.get("savedReplayPath") if ok and isinstance(payload, dict) else None
            if path and path != self._known_last:
                self._resolve(entry_id, path)
            elif attempt + 1 < POLL_TRIES:
                QTimer.singleShot(POLL_EVERY_MS, lambda: self._poll_last(entry_id, attempt + 1))
            else:
                self._pending = [p for p in self._pending if p != entry_id]
                log.warning("OBS never reported the saved replay file for highlight %s", entry_id)
        self.store.bridge.request("GetLastReplayBufferReplay", None, cb)

    def _resolve(self, entry_id, path):
        self._pending = [p for p in self._pending if p != entry_id]
        self._known_last = path
        e = self.find(entry_id)
        if e is None:
            return
        e["replay"] = path
        self.save()
        self.changed.emit()
        self.make_thumb(entry_id)

    def make_thumb(self, entry_id):
        e = self.find(entry_id)
        if e is None or not e.get("replay") or entry_id in self._thumbing or not os.path.isfile(e["replay"]):
            return
        self._thumbing.add(entry_id)
        dst = str(self.thumb_dir / f"{entry_id}.jpg")
        src = e["replay"]

        def work():
            ok = make_thumbnail(self.ffmpeg, src, dst)
            self._thumb_done.emit(entry_id, dst if ok else None)
        threading.Thread(target=work, daemon=True).start()

    def _on_thumb(self, entry_id, dst):
        self._thumbing.discard(entry_id)
        e = self.find(entry_id)
        if e is not None and dst:
            e["thumbnail"] = dst
            self.save()
            self.changed.emit()

    def _seed_last(self):
        def cb(ok, p):
            if ok and isinstance(p, dict) and not self._pending:
                self._known_last = p.get("savedReplayPath") or self._known_last
        self.store.bridge.request("GetLastReplayBufferReplay", None, cb)

    def _conn(self, state, _detail):
        if state == "connected":
            self._seed_last()

    def on_chat(self, msg):
        if not isinstance(msg, dict) or "text" not in msg:
            return
        t = float(msg.get("ts") or time.time())
        self.context.append({"user": str(msg.get("user", "")), "text": str(msg["text"])})
        spike = self.detector.add(t)
        if self.session is not None:
            self.session["chat_messages"] += 1
            self.session["peak"] = max(self.session["peak"], self.detector.rate)
        words = str(msg["text"]).strip().lower().split()
        if words[:1] == [COMMAND] and set(msg.get("badges") or []) & set(COMMAND_BADGES):
            self.mark("command")
        elif spike and self.config.get("highlights_enabled"):
            self.mark("chat_spike")

    def on_event(self, etype, data):
        data = data if isinstance(data, dict) else {}
        if etype == "ReplayBufferSaved":
            path = data.get("savedReplayPath")
            if path:
                if self._pending:
                    self._resolve(self._pending[0], path)
                else:
                    self._known_last = path
        elif etype == "ReplayBufferStateChanged" and data.get("outputActive"):
            self._seed_last()
        elif etype == "StreamStateChanged":
            if data.get("outputActive"):
                self.begin_session(time.time())
            else:
                self.end_session(time.time())
        elif etype == "CurrentProgramSceneChanged" and self.session is not None:
            self.session["scenes"].append({"time": iso(time.time()), "scene": data.get("sceneName", "")})

    def begin_session(self, start):
        self.session = {"start": start, "highlights": 0, "chat_messages": 0, "peak": 0, "scenes": []}

    def end_session(self, end):
        s = self.session
        self.session = None
        if s is None:
            ms = self.store.stream.get("duration_ms", 0)
            if not ms:
                return
            s = {"start": end - ms / 1000, "highlights": 0, "chat_messages": 0, "peak": 0, "scenes": []}
        record = {"start": iso(s["start"]), "end": iso(end), "duration_s": round(end - s["start"]),
                  "peak_chat_per_10s": s["peak"], "chat_messages": s["chat_messages"],
                  "highlight_count": s["highlights"], "scene_change_count": len(s["scenes"]),
                  "scene_changes": s["scenes"]}
        date = time.strftime("%Y-%m-%d", time.localtime(s["start"]))
        path = self.folder / "sessions" / f"{date}.json"
        try:
            data = read_json(path)
            streams = data.get("streams", []) if isinstance(data, dict) else []
            write_json(path, {"version": 1, "date": date, "streams": streams + [record]})
        except OSError as e:
            log.error("could not write %s: %s", path, e)
            self.store.toast.emit(f"Could not save the stream summary: {e.strerror or e}", True)
            return
        self.store.toast.emit("Stream summary saved", False)


def manager_for(ctx):
    mgr = getattr(ctx, "highlights", None)
    if mgr is None:
        mgr = ctx.highlights = HighlightManager(ctx)
    return mgr
