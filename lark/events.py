"""Normalised stream events, their alert settings, and the local event log.

The log lives under ~/.config/lark/events/ as JSON lines. Nothing here touches the network.
"""
import collections
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("lark.events")

TYPES = ("follow", "subscribe", "gift", "resub", "cheer", "raid", "stream_online", "stream_offline", "channel_update")

LABELS = {"follow": "Follow", "subscribe": "Subscription", "gift": "Gifted subs", "resub": "Resub",
          "cheer": "Cheer", "raid": "Raid", "stream_online": "Stream online", "stream_offline": "Stream offline",
          "channel_update": "Channel update"}

TITLES = {"follow": "New follower", "subscribe": "New subscriber", "gift": "Gifted subs", "resub": "Resubscribed",
          "cheer": "Bits", "raid": "Incoming raid", "stream_online": "Stream is live",
          "stream_offline": "Stream ended", "channel_update": "Channel updated"}

DEFAULT_TEMPLATES = {
    "follow": "{user} just followed",
    "subscribe": "{user} subscribed",
    "gift": "{user} gifted {amount} subs",
    "resub": "{user} resubscribed for {amount} months {message}",
    "cheer": "{user} cheered {amount} bits {message}",
    "raid": "{user} is raiding with {amount} viewers",
    "stream_online": "The stream is live",
    "stream_offline": "The stream has ended",
    "channel_update": "Channel updated: {message}",
}

DEFAULT_ENABLED = {"follow", "subscribe", "gift", "resub", "cheer", "raid"}
SOUND_EXTS = (".mp3", ".ogg", ".wav", ".opus", ".flac", ".m4a")
MAX_SOUND_BYTES = 10 * 1024 * 1024
_TOKEN = re.compile(r"\{(user|amount|message)\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def default_settings(kind):
    return {"enabled": kind in DEFAULT_ENABLED, "duration": 6, "sound": "", "template": DEFAULT_TEMPLATES[kind]}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def valid_alert_settings(v):
    """Config validator: {type: {enabled, duration, sound, template}}, every field optional."""
    if not isinstance(v, dict):
        return False
    for kind, s in v.items():
        if kind not in TYPES or not isinstance(s, dict) or set(s) - {"enabled", "duration", "sound", "template"}:
            return False
        if "enabled" in s and not isinstance(s["enabled"], bool):
            return False
        if "duration" in s and not (_is_num(s["duration"]) and 1 <= s["duration"] <= 60):
            return False
        if "sound" in s and not (isinstance(s["sound"], str) and len(s["sound"]) <= 512):
            return False
        if "template" in s and not (isinstance(s["template"], str) and len(s["template"]) <= 200):
            return False
    return True


def valid_volume(v):
    return _is_num(v) and 0 <= v <= 100


def alert_settings(values, kind):
    """Defaults for kind overlaid with whatever the config holds."""
    saved = values.get("alert_settings") or {}
    out = default_settings(kind)
    out.update(saved.get(kind, {}))
    return out


def sound_problem(path):
    """Why a sound path cannot be used, or None when it is fine."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.suffix.lower() not in SOUND_EXTS:
        return "Sounds must be " + ", ".join(SOUND_EXTS)
    try:
        if not p.is_file():
            return "That sound file does not exist"
        if p.stat().st_size > MAX_SOUND_BYTES:
            return "That sound file is over 10 MB"
    except OSError as e:
        return f"Cannot read that sound file ({e.strerror})"
    return None


def render(template, ev):
    """Fill {user} {amount} {message}. Anything else in braces stays as typed."""
    vals = {"user": ev["user"], "amount": "" if ev["amount"] is None else str(ev["amount"]), "message": ev["message"]}
    return " ".join(_TOKEN.sub(lambda m: vals[m.group(1)], template).split())


def clean(text, limit):
    return _CONTROL.sub("", text if isinstance(text, str) else "").strip()[:limit]


def make_event(kind, user="", amount=None, message="", ts=None, test=False, ident=None):
    if kind not in TYPES:
        raise ValueError(f"unknown event type {kind!r}")
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise ValueError("amount must be a whole number or None")
    return {"id": ident or uuid.uuid4().hex, "type": kind, "user": clean(user, 100), "amount": amount,
            "message": clean(message, 500), "ts": float(ts if ts is not None else time.time()), "test": bool(test)}


def _int(v):
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def parse_time(s, default):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return default


TIER = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3"}


def from_notification(sub_type, event, ts, ident=None):
    """Turn one EventSub notification payload into an event, or None when it should not be shown."""
    if not isinstance(event, dict):
        return None
    name = event.get("user_name") or event.get("user_login") or ""
    if sub_type == "channel.follow":
        return make_event("follow", name, ts=parse_time(event.get("followed_at"), ts), ident=ident)
    if sub_type == "channel.subscribe":
        if event.get("is_gift"):
            return None
        return make_event("subscribe", name, message=TIER.get(event.get("tier"), ""), ts=ts, ident=ident)
    if sub_type == "channel.subscription.gift":
        who = "Anonymous" if event.get("is_anonymous") else name
        return make_event("gift", who, _int(event.get("total")), TIER.get(event.get("tier"), ""), ts, ident=ident)
    if sub_type == "channel.subscription.message":
        msg = event.get("message")
        text = msg.get("text") if isinstance(msg, dict) else ""
        return make_event("resub", name, _int(event.get("cumulative_months")), text, ts, ident=ident)
    if sub_type == "channel.cheer":
        who = "Anonymous" if event.get("is_anonymous") else name
        return make_event("cheer", who, _int(event.get("bits")), event.get("message"), ts, ident=ident)
    if sub_type == "channel.raid":
        who = event.get("from_broadcaster_user_name") or event.get("from_broadcaster_user_login") or ""
        return make_event("raid", who, _int(event.get("viewers")), ts=ts, ident=ident)
    if sub_type == "stream.online":
        return make_event("stream_online", ts=parse_time(event.get("started_at"), ts), ident=ident)
    if sub_type == "stream.offline":
        return make_event("stream_offline", ts=ts, ident=ident)
    if sub_type == "channel.update":
        game = event.get("category_name") or ""
        title = event.get("title") or ""
        return make_event("channel_update", message=f"{title} ({game})" if game else title, ts=ts, ident=ident)
    return None


SAMPLES = {
    "follow": ("TestViewer", None, ""), "subscribe": ("TestViewer", None, "Tier 1"),
    "gift": ("TestViewer", 5, "Tier 1"), "resub": ("TestViewer", 12, "Twelve months already!"),
    "cheer": ("TestViewer", 500, "Cheer500 take my bits"), "raid": ("TestRaider", 42, ""),
    "stream_online": ("", None, ""), "stream_offline": ("", None, ""),
    "channel_update": ("", None, "Test title (Just Chatting)"),
}


def sample_event(kind):
    user, amount, message = SAMPLES[kind]
    return make_event(kind, user, amount, message, test=True)


class EventLog:
    """Thread-safe ring buffer of recent events plus a JSONL file per day. Test events stay out of the file."""

    def __init__(self, directory=None, keep=200):
        from .config import config_dir
        self.dir = Path(directory) if directory else config_dir() / "events"
        self.ring = collections.deque(maxlen=keep)
        self._lock = threading.Lock()
        self._warned = False

    def load(self):
        try:
            files = sorted(self.dir.glob("events-*.jsonl"))
        except OSError as e:
            log.warning("cannot list the event log: %s", e)
            return
        lines = []
        for f in reversed(files):
            try:
                lines = f.read_text().splitlines() + lines
            except (OSError, UnicodeDecodeError) as e:
                log.warning("cannot read %s: %s", f, e)
            if len(lines) >= self.ring.maxlen:
                break
        for line in lines[-self.ring.maxlen:]:
            try:
                d = json.loads(line)
                ev = make_event(d["type"], d.get("user", ""), d.get("amount"), d.get("message", ""),
                                d["ts"], ident=str(d["id"]))
            except (ValueError, KeyError, TypeError):
                log.warning("skipping a bad line in the event log")
                continue
            with self._lock:
                self.ring.append(ev)

    def add(self, ev):
        with self._lock:
            self.ring.append(ev)
        if not ev["test"]:
            self._write(ev)

    def recent(self):
        with self._lock:
            return list(self.ring)

    def _write(self, ev):
        path = self.dir / f"events-{time.strftime('%Y-%m-%d', time.localtime(ev['ts']))}.jsonl"
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps({k: ev[k] for k in ("id", "type", "user", "amount", "message", "ts")}) + "\n")
        except OSError as e:
            if not self._warned:
                log.error("cannot write the event log: %s", e)
                self._warned = True
