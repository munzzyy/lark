"""Anonymous read-only Twitch chat over IRC (TLS), plus the parsing it needs."""
import asyncio
import logging
import random
import re
import ssl
import time

log = logging.getLogger("lark.irc")

HOST, PORT = "irc.chat.twitch.tv", 6697
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ESC = {":": ";", "s": " ", "r": "\r", "n": "\n", "\\": "\\"}


def unescape_tag(v):
    out, i = [], 0
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            out.append(_ESC.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_line(line):
    """Parse one IRC line into (tags, prefix, command, params). Returns None for blank lines."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    tags = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = unescape_tag(v)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    elif line.startswith(":"):
        line, trailing = "", line[1:]
    parts = line.split()
    if not parts:
        return None
    params = parts[1:] + ([trailing] if trailing is not None else [])
    return tags, prefix, parts[0], params


def _emote_count(raw):
    return sum(len(spans.split(",")) for entry in raw.split("/") if ":" in entry
               for spans in [entry.split(":", 1)[1]] if spans)


def to_message(parsed):
    """A chat message dict for PRIVMSG, a {'delete': ...} dict for CLEARMSG/CLEARCHAT, else None."""
    if not parsed:
        return None
    tags, prefix, cmd, params = parsed
    if cmd == "PRIVMSG" and len(params) >= 2:
        text = params[1]
        action = text.startswith("\x01ACTION ") and text.endswith("\x01")
        if action:
            text = text[8:-1]
        color = tags.get("color", "")
        nick = prefix.split("!", 1)[0]
        return {
            "id": tags.get("id", ""),
            "user": tags.get("display-name") or nick,
            "color": color if _COLOR.match(color) else "",
            "text": text[:500],
            "action": action,
            "ts": time.time(),
            "badges": [b.split("/")[0] for b in tags.get("badges", "").split(",") if b],
            "login": nick.lower(),
            "user_id": tags.get("user-id", ""),
            "emote_count": _emote_count(tags.get("emotes", "")),
        }
    if cmd == "CLEARMSG" and tags.get("target-msg-id"):
        return {"delete": {"id": tags["target-msg-id"]}}
    if cmd == "CLEARCHAT":
        return {"delete": {"user": params[1] if len(params) > 1 else None}}
    return None


class TwitchChat:
    """Keeps one anonymous connection to a channel's chat, reconnecting forever until stop()."""

    def __init__(self, channel_getter, on_message, on_state=None, host=HOST, port=PORT, use_tls=True,
                 backoff=(1, 2, 5, 10, 30), auth=None):
        self._channel = channel_getter
        self._on_message = on_message
        self._on_state = on_state or (lambda s, d: None)
        self._host, self._port, self._tls = host, port, use_tls
        self._backoff = backoff
        self._stop = False
        self._wake = None
        self._writer = None
        self._auth = auth
        self._login = None
        self._force_anon = False
        self._channel_now = ""

    async def run(self):
        self._wake = asyncio.Event()
        attempt = 0
        while not self._stop:
            self._wake.clear()
            channel = self._channel().lower().lstrip("#")
            try:
                if not channel:
                    self._on_state("idle", "No channel set")
                else:
                    self._on_state("connecting", channel)
                    await self._session(channel)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ssl.SSLError) as e:
                self._on_state("disconnected", f"{type(e).__name__}: {e}")
                attempt += 1
            else:
                if channel:
                    self._on_state("disconnected", "connection closed")
            if self._stop:
                break
            delay = self._backoff[min(attempt, len(self._backoff) - 1)] if channel else 3600
            delay *= 0.8 + random.random() * 0.4
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
        self._on_state("idle", "stopped")

    async def _session(self, channel):
        ctx = ssl.create_default_context() if self._tls else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port, ssl=ctx), 10)
        self._writer = writer
        try:
            creds = None if (self._auth is None or self._force_anon) else await self._auth()
            self._login = creds[0] if creds else None
            self._channel_now = channel
            if self._stop:
                return
            nick = creds[0] if creds else f"justinfan{random.randint(10000, 99999)}"
            password = creds[1] if creds else "SCHMOOZE"
            for l in ("CAP REQ :twitch.tv/tags twitch.tv/commands", f"PASS {password}", f"NICK {nick}",
                      f"JOIN #{channel}"):
                writer.write((l + "\r\n").encode())
            await writer.drain()
            while True:
                raw = await asyncio.wait_for(reader.readline(), 360)
                if not raw:
                    return
                parsed = parse_line(raw.decode("utf-8", "replace"))
                if not parsed:
                    continue
                cmd = parsed[2]
                if cmd == "PING":
                    writer.write(f"PONG :{parsed[3][0] if parsed[3] else 'tmi.twitch.tv'}\r\n".encode())
                    await writer.drain()
                elif cmd == "JOIN":
                    self._on_state("connected", channel)
                elif cmd == "RECONNECT":
                    return
                elif cmd == "NOTICE" and self._login is not None and any("authentication failed" in p.lower() for p in parsed[3]):
                    log.warning("Twitch rejected the chat login, reading anonymously")
                    self._force_anon = True
                    self._on_state("disconnected", "Twitch rejected the chat login")
                    return
                else:
                    m = to_message(parsed)
                    if m:
                        self._on_message(m)
        finally:
            self._writer = None
            self._login = None
            writer.close()

    @property
    def can_send(self):
        return self._writer is not None and self._login is not None

    async def send(self, text):
        """Send a chat line as the signed-in user. Returns False if we aren't connected with a login."""
        text = " ".join(text.split())[:500]
        w = self._writer
        if not text or w is None or self._login is None:
            return False
        w.write(f"PRIVMSG #{self._channel_now} :{text}\r\n".encode())
        await w.drain()
        # Twitch doesn't echo your own lines back, so show it ourselves.
        self._on_message({"id": "", "user": self._login, "color": "", "text": text, "action": False,
                          "ts": time.time(), "badges": []})
        return True

    def reconnect_now(self, retry_auth=False):
        if retry_auth:
            self._force_anon = False
        if self._wake is not None:
            self._wake.set()
        w = self._writer
        if w is not None:
            w.close()

    async def stop(self):
        self._stop = True
        self.reconnect_now()
