"""Connects the bot to the real services: Twitch chat and API on the services loop, OBS through the bridge."""
import asyncio
import logging
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QTimer, Signal

from .chatbot import Bot, BotStore
from .twitch_api import MOD_SCOPES, TwitchError

log = logging.getLogger("lark.bot")

TICK_MS = 2000


class LiveIO:
    """What the bot needs from the outside. Every method runs on the services loop except the sync checks."""

    def __init__(self, client, chat, channel_fn, obs=None):
        self.client, self.chat, self._channel, self.obs = client, chat, channel_fn, obs
        self._ids = {}

    def login(self):
        return self.client.state.get("login", "")

    def channel(self):
        return self._channel().lower().lstrip("#")

    def can_act(self):
        if not self.client.can_send_chat:
            return False, "Twitch isn't connected, sign in under Settings"
        if not self.chat.can_send:
            return False, "chat isn't connected yet"
        return True, ""

    def can_moderate(self):
        if not self.client.connected:
            return False, "Twitch isn't connected, sign in under Settings"
        if not set(MOD_SCOPES) <= set(self.client.state["scopes"]):
            return False, "this sign-in lacks moderator permissions, connect Twitch again in Settings"
        return True, ""

    async def say(self, text):
        return await self.chat.send(text)

    async def broadcaster_id(self):
        channel = self.channel()
        if channel == self.login().lower():
            return self.client.state["user_id"]
        if channel not in self._ids:
            body = await self.client._helix("GET", "/users", params={"login": channel})
            rows = body.get("data") or []
            if not rows:
                raise TwitchError(f"Twitch has no channel called {channel}")
            self._ids[channel] = rows[0]["id"]
        return self._ids[channel]

    async def _mod_params(self):
        return {"broadcaster_id": await self.broadcaster_id(), "moderator_id": self.client.state["user_id"]}

    async def delete(self, message_id):
        params = {**await self._mod_params(), "message_id": message_id}
        await self.client._helix("DELETE", "/moderation/chat", params=params)

    async def timeout(self, user_id, seconds, reason):
        body = {"data": {"user_id": user_id, "duration": int(seconds), "reason": reason[:500]}}
        await self.client._helix("POST", "/moderation/bans", params=await self._mod_params(), json=body)

    async def warn(self, user_id, reason):
        body = {"data": {"user_id": user_id, "reason": reason[:500]}}
        await self.client._helix("POST", "/moderation/warnings", params=await self._mod_params(), json=body)

    async def uptime_seconds(self):
        local = self.obs.stream_seconds() if self.obs else None
        if local is not None:
            return local
        body = await self.client._helix("GET", "/streams", params={"user_id": await self.broadcaster_id()})
        rows = body.get("data") or []
        if not rows or not rows[0].get("started_at"):
            return None
        started = datetime.fromisoformat(rows[0]["started_at"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - started).total_seconds()

    async def channel_info(self):
        body = await self.client._helix("GET", "/channels", params={"broadcaster_id": await self.broadcaster_id()})
        rows = body.get("data") or []
        return rows[0] if rows else {}

    async def save_replay(self):
        if self.obs is None:
            return False, "OBS is not available"
        return await self.obs.save_replay()


class ObsAdapter:
    def __init__(self, ctx):
        self.ctx = ctx

    def stream_seconds(self):
        s = self.ctx.store.stream
        return s["duration_ms"] / 1000 if s.get("active") else None

    async def save_replay(self):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def cb(ok, payload):
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result((ok, payload)))
        self.ctx.bridge.request("SaveReplayBuffer", None, cb)
        try:
            ok, payload = await asyncio.wait_for(fut, 8)
        except asyncio.TimeoutError:
            return False, "OBS did not answer"
        return ok, "" if ok else str(payload)


class BotRuntime(QObject):
    entry = Signal(object)

    def __init__(self, ctx):
        super().__init__()
        svc = ctx.services
        self.svc = svc
        self.store = BotStore()
        self.io = LiveIO(svc.twitch, svc.chat, lambda: ctx.config.get("twitch_channel"), ObsAdapter(ctx))
        self.bot = Bot(self.store.load(), self.io, on_log=self.entry.emit, on_change=self._persist)
        svc.chat_message.connect(self._chat)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    def _alive(self):
        loop = self.svc._thread.loop
        return self.bot.data["enabled"] and loop is not None and not loop.is_closed()

    def _tick(self):
        if self._alive():
            self.svc.submit(self.bot.tick)

    def _chat(self, msg):
        if self._alive():
            self.svc.submit(lambda: self.bot.handle(msg))

    def _persist(self, _data):
        try:
            self.store.save(self.bot.data)
        except OSError as e:
            log.error("could not save bot settings: %s", e)

    def update(self, data):
        new = self.bot.apply(data)
        self.store.save(new)
        return new
