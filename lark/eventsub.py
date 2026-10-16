"""Twitch EventSub over WebSocket. Idle until the Twitch client holds a token.

Pure asyncio, no Qt. The only network calls are the user's own Twitch ones: the EventSub socket and the Helix
subscription requests made with the signed-in token.
"""
import asyncio
import collections
import json
import logging
import time

import aiohttp

from .events import from_notification, parse_time
from .twitch_api import TwitchError

log = logging.getLogger("lark.eventsub")

WS_URL = "wss://eventsub.wss.twitch.tv/ws"
MAX_AGE = 600

# (type, version, condition builder, scope needed or None, what it gives)
SUBSCRIPTIONS = [
    ("channel.follow", "2", lambda u: {"broadcaster_user_id": u, "moderator_user_id": u}, "moderator:read:followers", "follows"),
    ("channel.subscribe", "1", lambda u: {"broadcaster_user_id": u}, "channel:read:subscriptions", "subscriptions"),
    ("channel.subscription.gift", "1", lambda u: {"broadcaster_user_id": u}, "channel:read:subscriptions", "gifted subs"),
    ("channel.subscription.message", "1", lambda u: {"broadcaster_user_id": u}, "channel:read:subscriptions", "resubs"),
    ("channel.cheer", "1", lambda u: {"broadcaster_user_id": u}, "bits:read", "bits"),
    ("channel.raid", "1", lambda u: {"to_broadcaster_user_id": u}, None, "raids"),
    ("stream.online", "1", lambda u: {"broadcaster_user_id": u}, None, "stream start"),
    ("stream.offline", "1", lambda u: {"broadcaster_user_id": u}, None, "stream end"),
    ("channel.update", "2", lambda u: {"broadcaster_user_id": u}, None, "title changes"),
]
EVENT_SCOPES = sorted({s for _, _, _, s, _ in SUBSCRIPTIONS if s})


class EventSubError(Exception):
    pass


class EventSubClient:
    """on_event(event) gets each normalised event; on_state(status, detail) gets idle/connecting/connected/error."""

    def __init__(self, twitch, on_event, on_state, url=WS_URL, clock=time.time, backoff=(1, 60), grace=5, hello_wait=10):
        self.twitch = twitch
        self._on_event, self._on_state = on_event, on_state
        self.url, self._clock = url, clock
        self._backoff, self._grace, self._hello_wait = backoff, grace, hello_wait
        self.status, self.detail = "idle", ""
        self._wake = asyncio.Event()
        self._conn = None
        self._stopping = False
        self._up = False
        self._seen = collections.deque(maxlen=500)
        self.subscribed = []
        self.skipped = {}

    def _set(self, status, detail=""):
        if (status, detail) != (self.status, self.detail):
            self.status, self.detail = status, detail
            self._on_state(status, detail)

    def kick(self):
        """Call when the Twitch sign-in changed."""
        self._wake.set()
        if self._conn and not self._ready():
            self._conn.cancel()

    def _ready(self):
        return self.twitch.connected and bool(self.twitch.state.get("user_id"))

    async def stop(self):
        self._stopping = True
        self._wake.set()
        if self._conn:
            self._conn.cancel()

    async def run(self):
        failures = 0
        while not self._stopping:
            if not self._ready():
                self._set("idle", "Sign in to Twitch in Settings to get alerts")
                await self._wait(None)
                continue
            self._up = False
            self._conn = asyncio.ensure_future(self._once())
            try:
                await asyncio.wait({self._conn})
            finally:
                self._conn.cancel()
            task, self._conn = self._conn, None
            if task.cancelled():
                failures = 0
                continue
            err = task.exception()
            if err is None:
                failures = 0
                continue
            failures = 1 if self._up else failures + 1
            self.subscribed = []
            msg = str(err) if isinstance(err, (EventSubError, TwitchError)) else f"{type(err).__name__}: {err}"
            if not isinstance(err, (EventSubError, TwitchError, aiohttp.ClientError, OSError, asyncio.TimeoutError)):
                log.error("eventsub crashed", exc_info=err)
            else:
                log.warning("eventsub: %s", msg)
            delay = min(self._backoff[0] * 2 ** (failures - 1), self._backoff[1])
            self._set("error", f"{msg}. Retrying in {delay:g}s")
            await self._wait(delay)

    async def _wait(self, delay):
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _once(self):
        self._set("connecting", "Connecting to Twitch events")
        async with aiohttp.ClientSession() as http:
            ws, sid, kt = await self._open(http, self.url)
            try:
                await self._subscribe(sid)
                self._up = True
                self._announce()
                while True:
                    msg = await self._next(ws, kt + self._grace)
                    meta = msg.get("metadata") or {}
                    kind = meta.get("message_type")
                    if kind == "notification":
                        self._notification(meta, msg.get("payload") or {})
                    elif kind == "revocation":
                        sub = (msg.get("payload") or {}).get("subscription") or {}
                        log.warning("twitch revoked %s: %s", sub.get("type"), sub.get("status"))
                        self.skipped[sub.get("type", "?")] = f"revoked ({sub.get('status')})"
                        self.subscribed = [t for t in self.subscribed if t != sub.get("type")]
                        self._announce()
                    elif kind == "session_reconnect":
                        new = ((msg.get("payload") or {}).get("session") or {}).get("reconnect_url")
                        if not isinstance(new, str) or not new.startswith(("wss://", "ws://")):
                            raise EventSubError("Twitch sent a bad reconnect address")
                        nws, sid, kt = await self._open(http, new)
                        await ws.close()
                        ws = nws
            finally:
                await ws.close()

    async def _next(self, ws, timeout):
        try:
            m = await ws.receive(timeout=timeout)
        except asyncio.TimeoutError:
            raise EventSubError("No keepalive from Twitch") from None
        if m.type == aiohttp.WSMsgType.TEXT:
            try:
                d = json.loads(m.data)
            except ValueError:
                raise EventSubError("Twitch sent something unreadable") from None
            if not isinstance(d, dict):
                raise EventSubError("Twitch sent something unreadable")
            return d
        if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            raise EventSubError(f"Twitch closed the connection ({ws.close_code})")
        raise EventSubError("Connection to Twitch events failed")

    async def _open(self, http, url):
        ws = await http.ws_connect(url, heartbeat=None, max_msg_size=1 << 20)
        try:
            msg = await self._next(ws, self._hello_wait)
            if (msg.get("metadata") or {}).get("message_type") != "session_welcome":
                raise EventSubError("Twitch did not say hello")
            session = (msg.get("payload") or {}).get("session") or {}
            sid = session.get("id")
            if not isinstance(sid, str) or not sid:
                raise EventSubError("Twitch sent no session id")
            kt = session.get("keepalive_timeout_seconds")
            return ws, sid, kt if isinstance(kt, (int, float)) and kt > 0 else 10
        except BaseException:
            await ws.close()
            raise

    async def _subscribe(self, sid):
        uid = self.twitch.state["user_id"]
        have = set(self.twitch.state.get("scopes") or [])
        self.subscribed, self.skipped = [], {}
        for typ, ver, cond, scope, _ in SUBSCRIPTIONS:
            if scope and scope not in have:
                self.skipped[typ] = f"needs {scope}"
                continue
            body = {"type": typ, "version": ver, "condition": cond(uid),
                    "transport": {"method": "websocket", "session_id": sid}}
            try:
                await self.twitch._helix("POST", "/eventsub/subscriptions", json=body)
                self.subscribed.append(typ)
            except TwitchError as e:
                if e.status in (0, 401) or e.status >= 500:
                    raise
                self.skipped[typ] = str(e)
        if not self.subscribed:
            raise EventSubError("Twitch accepted none of the event subscriptions")

    def _announce(self):
        names = {t: label for t, _, _, _, label in SUBSCRIPTIONS}
        detail = f"Listening for {len(self.subscribed)} of {len(SUBSCRIPTIONS)} event types"
        if self.skipped:
            detail += ". Not available: " + ", ".join(names.get(t, t) for t in self.skipped)
            if any("needs " in v for v in self.skipped.values()):
                detail += ". Disconnect and connect Twitch again to allow them"
        self._set("connected", detail)

    def _notification(self, meta, payload):
        mid = meta.get("message_id")
        if mid in self._seen:
            return
        self._seen.append(mid)
        now = self._clock()
        ts = now
        raw = meta.get("message_timestamp")
        if isinstance(raw, str):
            ts = parse_time(raw, now)
        if now - ts > MAX_AGE:
            return
        sub = payload.get("subscription") or {}
        ev = from_notification(sub.get("type"), payload.get("event"), ts, ident=mid if isinstance(mid, str) else None)
        if ev is not None:
            self._on_event(ev)
