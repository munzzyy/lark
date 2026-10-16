"""Settings stored as JSON under ~/.config/lark.

The OBS WebSocket password is read straight from OBS's own plugin config at
connect time. It is never copied into Lark's file unless the user picks
"custom" and types one in.
"""
import glob
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from .events import valid_alert_settings, valid_volume
from .looks import valid_looks

log = logging.getLogger("lark.config")

OBS_WS_CONFIGS = (
    "~/.var/app/com.obsproject.Studio/config/obs-studio/plugin_config/obs-websocket/config.json",
    "~/.config/obs-studio/plugin_config/obs-websocket/config.json",
)

CAMERA_USB_ID = ("328f", "00e1")


def _sysfs_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def discover_camera():
    """The first capture node of the EMEET C60E (USB 328f:00e1), preferring its stable by-id name. "" if absent."""
    for d in sorted(glob.glob("/sys/class/video4linux/video*")):
        if _sysfs_text(d + "/index") != "0":
            continue
        if (_sysfs_text(d + "/device/../idVendor"), _sysfs_text(d + "/device/../idProduct")) != CAMERA_USB_ID:
            continue
        node = "/dev/" + os.path.basename(d)
        for link in glob.glob("/dev/v4l/by-id/*-video-index0"):
            if os.path.realpath(link) == node:
                return link
        return node
    return ""


CAMERA_BY_ID = discover_camera()

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_CHANNEL = re.compile(r"^[A-Za-z0-9_]{0,25}$")


def _port(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535


def _port_or_none(v):
    return v is None or _port(v)


def _str(v):
    return isinstance(v, str) and len(v) <= 512


def _host(v):
    return isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._:\-]{1,253}", v) is not None


def _client_id(v):
    return isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{0,64}", v) is not None


def _fade(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 3600


def _window_size(v):
    return v is None or (isinstance(v, list) and len(v) == 2 and all(
        isinstance(n, int) and not isinstance(n, bool) and 400 <= n <= 10000 for n in v))


def _page_key(v):
    return isinstance(v, str) and re.fullmatch(r"[a-z0-9_]{0,32}", v) is not None


def _num(lo, hi):
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def _deck_style(v):
    if not isinstance(v, dict) or len(v) > 200:
        return False
    for k, st in v.items():
        if not isinstance(k, str) or len(k) > 200 or not isinstance(st, dict):
            return False
        if set(st) - {"label", "color"}:
            return False
        if "label" in st and not (isinstance(st["label"], str) and len(st["label"]) <= 40):
            return False
        if "color" in st and not (isinstance(st["color"], str) and _HEX.match(st["color"])):
            return False
    return True


def _pad_map(v):
    return (isinstance(v, dict) and len(v) <= 40
            and all(isinstance(k, str) and isinstance(a, str) and len(k) <= 20 and len(a) <= 200 for k, a in v.items()))


def _eptz_presets(v):
    if not isinstance(v, dict) or len(v) > 20:
        return False
    z, u = _num(1.0, 4.0), _num(-1.0, 1.0)
    for k, p in v.items():
        if not (isinstance(k, str) and 1 <= len(k) <= 24 and k.isprintable()):
            return False
        if not (isinstance(p, dict) and set(p) == {"zoom", "pan", "tilt"}
                and z(p["zoom"]) and u(p["pan"]) and u(p["tilt"])):
            return False
    return True


def _flag(v):
    return isinstance(v, bool)


def _minutes(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 180


# key -> (default, validator)
SCHEMA = {
    "obs_host": ("127.0.0.1", _host),
    "obs_port": (None, _port_or_none),
    "obs_password_source": ("obs_file", lambda v: v in ("obs_file", "custom")),
    "obs_password": ("", _str),
    "overlay_port": (4777, _port),
    "twitch_channel": ("", lambda v: isinstance(v, str) and bool(_CHANNEL.match(v))),
    "twitch_client_id": ("", _client_id),
    "accent": ("#7c5cff", lambda v: isinstance(v, str) and bool(_HEX.match(v))),
    "mic_input": ("Mic (Fifine)", _str),
    "camera_device": (CAMERA_BY_ID, _str),
    "camera_module_dir": ("~/Projects/cameractrls-emeet", _str),
    "chat_fade_seconds": (30, _fade),
    "starting_title": ("Starting soon", _str),
    "window_size": (None, _window_size),
    "last_page": ("", _page_key),
    "onboarding_done": (False, lambda v: isinstance(v, bool)),
    "alert_settings": ({}, valid_alert_settings),
    "alert_volume": (80, valid_volume),
    "highlights_enabled": (True, lambda v: isinstance(v, bool)),
    "highlights_multiplier": (3.0, _num(1.5, 20)),
    "highlights_cooldown": (60, _num(5, 3600)),
    "highlights_min_messages": (8, _num(2, 500)),
    "highlights_dir": ("~/Videos/OBS", _str),
    "desktop_input": ("Desktop Audio", _str),
    "deck_style": ({}, _deck_style),
    "deck_pad_map": ({}, _pad_map),
    "deck_pad_background": (False, _flag),
    "deck_countdown_minutes": (5, _minutes),
    "wizard_done": (False, _flag),
    "eptz_item": ("Camera", lambda v: isinstance(v, str) and 1 <= len(v) <= 128),
    "eptz_duration_ms": (400, lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3000),
    "eptz_presets": ({}, _eptz_presets),
    "camera_input": ("Camera", lambda v: _str(v) and bool(v.strip())),
    "camera_dark_threshold": (22, _num(5, 60)),
    "camera_poll_ms": (1000, lambda v: isinstance(v, int) and not isinstance(v, bool) and 250 <= v <= 10000),
    "camera_preflight_preset": ("1", lambda v: v in ("", "1", "2", "3", "4")),
    "looks": ({}, valid_looks),
    "look_active": ("Neutral", _str),
    "look_other": ("", _str),
}


def config_dir():
    env = os.environ.get("LARK_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "lark"


class Config:
    def __init__(self, path=None, obs_config_paths=None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self.obs_config_paths = [Path(p).expanduser() for p in (obs_config_paths or OBS_WS_CONFIGS)]
        self.values = {k: d for k, (d, _) in SCHEMA.items()}
        self.load()

    def load(self):
        self.values = {k: d for k, (d, _) in SCHEMA.items()}
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            bad = self.path.with_suffix(".json.bad")
            log.error("config unreadable (%s), moving it to %s and using defaults", e, bad)
            try:
                os.replace(self.path, bad)
            except OSError as e2:
                log.error("could not move bad config aside: %s", e2)
            return
        if not isinstance(raw, dict):
            log.error("config is not an object, ignoring it")
            return
        for k, v in raw.items():
            if k not in SCHEMA:
                continue
            if SCHEMA[k][1](v):
                self.values[k] = v
            else:
                log.warning("config key %s has an invalid value, using the default", k)

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        if key not in SCHEMA:
            raise KeyError(key)
        if not SCHEMA[key][1](value):
            raise ValueError(f"invalid value for {key}")
        self.values[key] = value

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.values, f, indent=2, sort_keys=True)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read_obs_file(self):
        """Return the parsed OBS websocket config dict, or None."""
        for p in self.obs_config_paths:
            try:
                data = json.loads(p.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                log.warning("cannot read %s: %s", p, e)
                continue
            if isinstance(data, dict):
                return data
        return None

    def obs_connection(self):
        """(host, port, password) for the websocket, read fresh each call."""
        obs = self.read_obs_file() or {}
        port = self.values["obs_port"]
        if port is None:
            p = obs.get("server_port")
            port = p if _port(p) else 4455
        if self.values["obs_password_source"] == "custom":
            pw = self.values["obs_password"]
        else:
            pw = obs.get("server_password", "") if obs.get("auth_required", True) else ""
            if not isinstance(pw, str):
                pw = ""
        return self.values["obs_host"], port, pw

    def obs_file_status(self):
        obs = self.read_obs_file()
        if obs is None:
            return "OBS websocket config not found"
        if not obs.get("server_enabled", False):
            return "OBS websocket server is turned off in OBS"
        return "OBS websocket config found"
