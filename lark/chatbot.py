"""The chat bot: commands, timers and moderation on top of the IRC client and the Twitch API.

No Qt in here. The bot talks to the outside through an `io` object (see botruntime.LiveIO), which is what the
tests replace. Nothing is said or enforced unless io says the account is signed in with the right scopes.
"""
import copy
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections import deque

from . import chatmod
from .config import config_dir
from .twitch_api import TwitchError

log = logging.getLogger("lark.bot")

LEVELS = ("everyone", "subscriber", "mod", "broadcaster")
ACTIONS = ("delete", "timeout", "warn")
NAME = re.compile(r"^[a-z0-9_]{1,25}$")
LOGIN = re.compile(r"^[A-Za-z0-9_]{1,25}$")
MAX_COMMANDS, MAX_TIMERS, MAX_LIST = 200, 50, 500
MAX_REPLY = 450
SAY_LIMIT, SAY_WINDOW = 15, 30.0

BUILTINS = {
    "uptime": {"permission": "everyone", "cooldown": 5, "user_cooldown": 15},
    "title": {"permission": "everyone", "cooldown": 5, "user_cooldown": 15},
    "game": {"permission": "everyone", "cooldown": 5, "user_cooldown": 15},
    "commands": {"permission": "everyone", "cooldown": 10, "user_cooldown": 30},
    "lurk": {"permission": "everyone", "cooldown": 0, "user_cooldown": 60,
             "response": "{user} is lurking. Thanks for hanging out!"},
    "so": {"permission": "mod", "cooldown": 5, "user_cooldown": 0,
           "response": "Go check out {touser} at {link}"},
    "clip": {"permission": "mod", "cooldown": 30, "user_cooldown": 0},
    "permit": {"permission": "mod", "cooldown": 0, "user_cooldown": 0},
}

DEFAULTS = {
    "enabled": False,
    "prefix": "!",
    "commands": [],
    "timers": [],
    "builtins": {k: {"enabled": True, **v} for k, v in BUILTINS.items()},
    "counts": {},
    "mod": {
        "enabled": False,
        "exempt_subs": False,
        "links": {"on": True, "action": "timeout", "seconds": 10, "allow": [], "permit_seconds": 60},
        "caps": {"on": True, "action": "delete", "seconds": 10, "min_letters": 12, "percent": 70},
        "emotes": {"on": True, "action": "delete", "seconds": 10, "max": 15},
        "terms": {"on": True, "action": "timeout", "seconds": 600, "blocked": [], "personal": []},
        "contacts": {"on": True, "action": "delete", "seconds": 10},
    },
}


def _num(v, lo, hi, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return int(min(max(v, lo), hi))


def _bool(v, default):
    return v if isinstance(v, bool) else default


def _text(v, limit, default=""):
    if not isinstance(v, str):
        return default
    return " ".join(v.split())[:limit]


def _choice(v, options, default):
    return v if v in options else default


def _strings(v, limit, norm=lambda s: s):
    out = []
    for item in v if isinstance(v, list) else []:
        s = norm(_text(item, limit))
        if s and s not in out:
            out.append(s)
        if len(out) >= MAX_LIST:
            break
    return out


def _domain(s):
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", s.lower()).split("/")[0]


def _rule(raw, default, extras):
    raw = raw if isinstance(raw, dict) else {}
    out = {"on": _bool(raw.get("on"), default["on"]), "action": _choice(raw.get("action"), ACTIONS, default["action"]),
           "seconds": _num(raw.get("seconds"), 1, 1209600, default["seconds"])}
    for k, fn in extras.items():
        out[k] = fn(raw.get(k), default[k])
    return out


def sanitize(raw):
    """Turn whatever was in bot.json (or the editor) into a complete, valid settings dict."""
    raw = raw if isinstance(raw, dict) else {}
    d = copy.deepcopy(DEFAULTS)
    d["enabled"] = _bool(raw.get("enabled"), False)
    prefix = raw.get("prefix")
    if isinstance(prefix, str) and len(prefix) == 1 and prefix.isascii() and prefix.isprintable() \
            and not prefix.isalnum() and prefix not in " /.":
        d["prefix"] = prefix

    rb = raw.get("builtins") if isinstance(raw.get("builtins"), dict) else {}
    for name, base in d["builtins"].items():
        r = rb.get(name) if isinstance(rb.get(name), dict) else {}
        base["enabled"] = _bool(r.get("enabled"), True)
        base["permission"] = _choice(r.get("permission"), LEVELS, base["permission"])
        base["cooldown"] = _num(r.get("cooldown"), 0, 86400, base["cooldown"])
        base["user_cooldown"] = _num(r.get("user_cooldown"), 0, 86400, base["user_cooldown"])
        if "response" in base:
            base["response"] = _text(r.get("response"), 400, base["response"]) or base["response"]

    taken = set(BUILTINS)
    cmds = []
    for r in raw.get("commands") if isinstance(raw.get("commands"), list) else []:
        if not isinstance(r, dict) or len(cmds) >= MAX_COMMANDS:
            continue
        name = _text(r.get("name"), 26).lower().lstrip(d["prefix"])
        resp = _text(r.get("response"), 400)
        if not NAME.match(name) or name in taken or not resp:
            continue
        taken.add(name)
        aliases = []
        for a in _strings(r.get("aliases"), 26, lambda s: s.lower().lstrip(d["prefix"])):
            if NAME.match(a) and a not in taken and len(aliases) < 5:
                taken.add(a)
                aliases.append(a)
        cmds.append({"name": name, "response": resp, "permission": _choice(r.get("permission"), LEVELS, "everyone"),
                     "cooldown": _num(r.get("cooldown"), 0, 86400, 5),
                     "user_cooldown": _num(r.get("user_cooldown"), 0, 86400, 15),
                     "enabled": _bool(r.get("enabled"), True), "aliases": aliases})
    d["commands"] = cmds

    seen = set()
    for r in raw.get("timers") if isinstance(raw.get("timers"), list) else []:
        if not isinstance(r, dict) or len(d["timers"]) >= MAX_TIMERS:
            continue
        name, msg = _text(r.get("name"), 40), _text(r.get("message"), 400)
        if not name or not msg or name in seen:
            continue
        seen.add(name)
        d["timers"].append({"name": name, "message": msg, "interval": _num(r.get("interval"), 1, 86400, 900),
                            "min_lines": _num(r.get("min_lines"), 0, 10000, 5), "enabled": _bool(r.get("enabled"), True)})

    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    d["counts"] = {k: _num(v, 0, 10**9, 0) for k, v in counts.items() if isinstance(k, str) and NAME.match(k)}

    m = raw.get("mod") if isinstance(raw.get("mod"), dict) else {}
    dm = d["mod"]
    dm["enabled"] = _bool(m.get("enabled"), False)
    dm["exempt_subs"] = _bool(m.get("exempt_subs"), False)
    dm["links"] = _rule(m.get("links"), dm["links"], {
        "allow": lambda v, _: _strings(v, 100, _domain),
        "permit_seconds": lambda v, dflt: _num(v, 5, 3600, dflt)})
    dm["caps"] = _rule(m.get("caps"), dm["caps"], {
        "min_letters": lambda v, dflt: _num(v, 1, 500, dflt), "percent": lambda v, dflt: _num(v, 10, 100, dflt)})
    dm["emotes"] = _rule(m.get("emotes"), dm["emotes"], {"max": lambda v, dflt: _num(v, 1, 100, dflt)})
    dm["terms"] = _rule(m.get("terms"), dm["terms"], {"blocked": lambda v, _: _strings(v, 100),
                                                      "personal": lambda v, _: _strings(v, 100)})
    dm["contacts"] = _rule(m.get("contacts"), dm["contacts"], {})
    return d


class BotStore:
    """bot.json next to config.json. Written atomically, readable only by you."""

    def __init__(self, path=None):
        self.path = path or config_dir() / "bot.json"
        self._lock = threading.Lock()

    def load(self):
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return sanitize({})
        except (OSError, ValueError) as e:
            bad = self.path.with_suffix(".json.bad")
            log.error("bot settings unreadable (%s), moving them to %s", e, bad)
            try:
                os.replace(self.path, bad)
            except OSError as e2:
                log.error("could not move bad bot settings aside: %s", e2)
            return sanitize({})
        return sanitize(raw)

    def save(self, data):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".bot-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                    f.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


def level_of(msg, channel=""):
    badges = msg.get("badges") or []
    if "broadcaster" in badges or (channel and msg.get("login", "").lower() == channel.lower()):
        return 3
    if "moderator" in badges:
        return 2
    if "subscriber" in badges or "founder" in badges:
        return 1
    return 0


_VAR = re.compile(r"\{(user|touser|count|channel|link)\}")


def render(template, **vars_):
    return _VAR.sub(lambda m: str(vars_.get(m.group(1), m.group(0))), template)


def clean_out(text):
    """One line, within Twitch's limit, and never a slash command other than /me."""
    text = " ".join(str(text).split())[:MAX_REPLY]
    if text.startswith("/") and not text.startswith("/me "):
        return ""
    return text


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


class Bot:
    def __init__(self, data, io, clock=time.monotonic, on_log=None, on_change=None):
        self._data = sanitize(data)
        self.io = io
        self.clock = clock
        self._on_log = on_log or (lambda e: None)
        self._on_change = on_change or (lambda d: None)
        self.history = deque(maxlen=300)
        self._lock = threading.Lock()
        self._lines = 0
        self._cd_global, self._cd_user = {}, {}
        self._timer_state = {}
        self._permits = {}
        self._sent = deque()
        self._notes = {}
        self._ticking = False

    @property
    def data(self):
        return self._data

    def apply(self, data):
        """Swap in edited settings. Use counts stay with the bot, not the editor."""
        new = sanitize(data)
        with self._lock:
            new["counts"] = {k: v for k, v in self._data["counts"].items()
                             if k in {c["name"] for c in new["commands"]}}
            self._data = new
            if not new["enabled"]:
                self._timer_state.clear()
        return new

    def log(self, kind, text):
        entry = {"ts": time.time(), "kind": kind, "text": text}
        self.history.append(entry)
        log.info("%s: %s", kind, text)
        self._on_log(entry)

    def _note(self, key, text):
        """Log a standing condition once, again only when it changes."""
        if self._notes.get(key) != text:
            self._notes[key] = text
            self.log("note", text)

    def _clear_note(self, key):
        self._notes.pop(key, None)

    async def _say(self, text):
        text = clean_out(text)
        if not text:
            return False
        now = self.clock()
        while self._sent and now - self._sent[0] > SAY_WINDOW:
            self._sent.popleft()
        if len(self._sent) >= SAY_LIMIT:
            self.log("note", "Held back a reply, too many messages in the last 30 seconds")
            return False
        try:
            ok = await self.io.say(text)
        except TwitchError as e:
            self.log("error", f"Could not send: {e}")
            return False
        if ok:
            self._sent.append(now)
            self.log("say", text)
        else:
            self.log("note", "Not sent, chat is not connected with a login")
        return ok

    # moderation

    async def _moderate(self, d, msg, user):
        mod = d["mod"]
        level = level_of(msg, self.io.channel())
        if level >= 2 or (mod["exempt_subs"] and level >= 1):
            return False
        now = self.clock()
        self._permits = {u: t for u, t in self._permits.items() if t > now}
        rule = chatmod.check(msg, mod, permitted=user in self._permits)
        if not rule:
            return False
        ok, why = self.io.can_moderate()
        if not ok:
            self._note("mod", f"Moderation is on but skipped: {why}")
            return False
        self._clear_note("mod")
        cfg = mod[rule]
        reason = chatmod.REASONS[rule]
        who = msg.get("user") or user
        try:
            if cfg["action"] == "delete":
                if not msg.get("id"):
                    self.log("error", f"Could not delete a message from {who}, it had no id")
                    return True
                await self.io.delete(msg["id"])
                self.log("mod", f"Deleted a message from {who} ({rule})")
            elif not msg.get("user_id"):
                self.log("error", f"Could not act on {who}, no user id on the message")
                return True
            elif cfg["action"] == "timeout":
                await self.io.timeout(msg["user_id"], cfg["seconds"], reason)
                self.log("mod", f"Timed out {who} for {cfg['seconds']}s ({rule})")
            else:
                await self.io.warn(msg["user_id"], reason)
                self.log("mod", f"Warned {who} ({rule})")
        except TwitchError as e:
            self.log("error", f"Moderation on {who} failed: {e}")
        return True

    # commands

    def _find_command(self, d, name):
        if name in d["builtins"]:
            return name, d["builtins"][name], True
        for c in d["commands"]:
            if name == c["name"] or name in c["aliases"]:
                return c["name"], c, False
        return None

    def _cooling(self, key, user, spec):
        now = self.clock()
        if now - self._cd_global.get(key, -1e18) < spec["cooldown"]:
            return True
        return now - self._cd_user.get((key, user), -1e18) < spec["user_cooldown"]

    def _stamp(self, key, user):
        now = self.clock()
        self._cd_global[key] = now
        self._cd_user[(key, user)] = now

    async def _command(self, d, msg, user):
        text = msg["text"].strip()
        if not text.startswith(d["prefix"]) or len(text) < 2:
            return
        head, _, rest = text[len(d["prefix"]):].partition(" ")
        found = self._find_command(d, head.lower())
        if not found:
            return
        name, spec, builtin = found
        if not spec["enabled"] or level_of(msg, self.io.channel()) < LEVELS.index(spec["permission"]):
            return
        if self._cooling(name, user, spec):
            return
        ok, why = self.io.can_act()
        if not ok:
            self._note("act", f"The bot can't answer: {why}")
            return
        self._clear_note("act")
        args = rest.split()
        arg = args[0].lstrip("@") if args else ""
        arg = arg if LOGIN.match(arg) else ""
        who = msg.get("user") or user
        self._stamp(name, user)
        if builtin:
            reply = await self._builtin(d, name, spec, msg, who, arg)
        else:
            with self._lock:
                n = d["counts"][name] = d["counts"].get(name, 0) + 1
            reply = render(spec["response"], user=who, touser=arg or who, count=n, channel=self.io.channel())
            self._on_change(d)
        if reply:
            await self._say(reply)

    async def _builtin(self, d, name, spec, msg, who, arg):
        channel = self.io.channel()
        try:
            if name == "uptime":
                secs = await self.io.uptime_seconds()
                return f"{channel} has been live for {fmt_duration(secs)}" if secs is not None \
                    else f"{channel} is offline right now"
            if name in ("title", "game"):
                info = await self.io.channel_info()
                value = info.get("title" if name == "title" else "game_name") or ""
                return (f"{'Title' if name == 'title' else 'Game'}: {value}" if value else f"No {name} set")
            if name == "commands":
                level = level_of(msg, channel)
                names = [n for n, s in d["builtins"].items()
                         if s["enabled"] and n != "permit" and level >= LEVELS.index(s["permission"])]
                names += [c["name"] for c in d["commands"] if c["enabled"] and level >= LEVELS.index(c["permission"])]
                out = "Commands: " + ", ".join(d["prefix"] + n for n in names)
                return out if names else "No commands available"
            if name == "lurk":
                return render(spec["response"], user=who, touser=who, channel=channel)
            if name == "so":
                if not arg:
                    return f"Usage: {d['prefix']}so <user>"
                return render(spec["response"], user=who, touser=arg, channel=channel,
                              link=f"https://twitch.tv/{arg.lower()}")
            if name == "clip":
                ok, why = await self.io.save_replay()
                self.log("clip", "Saved the replay buffer" if ok else f"Clip failed: {why}")
                return "Clip saved" if ok else "Couldn't save a clip, the replay buffer isn't running in OBS"
            if name == "permit":
                if not arg:
                    return f"Usage: {d['prefix']}permit <user>"
                secs = d["mod"]["links"]["permit_seconds"]
                self._permits[arg.lower()] = self.clock() + secs
                self.log("mod", f"{who} permitted {arg} to post links for {secs}s")
                return f"{arg}, you can post links for the next {secs} seconds"
        except TwitchError as e:
            self.log("error", f"!{name} failed: {e}")
        return ""

    async def handle(self, msg):
        d = self._data
        if not d["enabled"] or "text" not in msg:
            return
        user = (msg.get("login") or msg.get("user") or "").lower()
        if not user or user == (self.io.login() or "").lower():
            return
        self._lines += 1
        if d["mod"]["enabled"] and await self._moderate(d, msg, user):
            return
        await self._command(d, msg, user)

    # timers

    async def tick(self):
        d = self._data
        if self._ticking:
            return
        if not d["enabled"]:
            self._timer_state.clear()
            return
        self._ticking = True
        try:
            now = self.clock()
            for t in d["timers"]:
                st = self._timer_state.setdefault(t["name"], [now, self._lines])
                if not t["enabled"] or now - st[0] < t["interval"] or self._lines - st[1] < t["min_lines"]:
                    continue
                ok, why = self.io.can_act()
                if not ok:
                    self._note("act", f"The bot can't answer: {why}")
                    return
                self._clear_note("act")
                if await self._say(render(t["message"], channel=self.io.channel())):
                    st[0], st[1] = now, self._lines
        finally:
            self._ticking = False
