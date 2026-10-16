"""Fake EventSub WebSocket server plus the Helix subscription endpoint, on top of FakeTwitch. Nothing here talks to Twitch."""
import asyncio
import itertools
import json
import time

from aiohttp import web

from tests.fake_twitch import FakeTwitch

ALL_SCOPES = ["channel:manage:broadcast", "chat:read", "chat:edit", "moderator:read:followers",
              "channel:read:subscriptions", "bits:read"]


class FakeEventSub(FakeTwitch):
    def __init__(self, scopes=None, keepalive=10, **kw):
        super().__init__(**kw)
        self.scopes = ALL_SCOPES if scopes is None else scopes
        self.keepalive = keepalive
        self.ids = itertools.count(1)
        self.sockets = {}        # session id -> ws
        self.connects = 0
        self.subs = []
        self.reject = {}         # subscription type -> http status
        self.hello = True
        self.sub_headers = []

    def app(self):
        a = super().app()
        a.router.add_post("/helix/eventsub/subscriptions", self.subscribe)
        a.router.add_get("/ws", self.ws)
        a.router.add_get("/ws2", self.ws)
        return a

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/ws"

    async def validate(self, req):
        resp = await super().validate(req)
        if resp.status != 200:
            return resp
        d = json.loads(resp.text)
        d["scopes"] = self.scopes
        return web.json_response(d)

    async def subscribe(self, req):
        bad = self._helix_guard(req)
        if bad:
            return bad
        body = await req.json()
        self.sub_headers.append(dict(req.headers))
        status = self.reject.get(body.get("type"))
        if status:
            return web.json_response({"status": status, "message": "nope"}, status=status)
        sid = body.get("transport", {}).get("session_id")
        if body.get("transport", {}).get("method") != "websocket" or sid not in self.sockets:
            return web.json_response({"status": 400, "message": "bad transport"}, status=400)
        self.subs.append(body)
        return web.json_response({"data": [{"id": f"sub{len(self.subs)}", "status": "enabled"}]}, status=202)

    def envelope(self, kind, payload=None, mid=None):
        return {"metadata": {"message_id": mid or f"m{next(self.ids)}", "message_type": kind,
                             "message_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())},
                "payload": payload or {}}

    def notification(self, sub_type, event, mid=None):
        return self.envelope("notification", {"subscription": {"type": sub_type}, "event": event}, mid)

    async def ws(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.connects += 1
        sid = f"sess{next(self.ids)}"
        self.sockets[sid] = ws
        ws.session_id = sid
        if self.hello:
            await ws.send_json(self.envelope("session_welcome", {"session": {
                "id": sid, "status": "connected", "keepalive_timeout_seconds": self.keepalive}}))
        try:
            async for _ in ws:
                pass
        finally:
            self.sockets.pop(sid, None)
        return ws

    async def send(self, msg, session=None):
        ws = self.sockets[session] if session else list(self.sockets.values())[-1]
        await ws.send_json(msg)

    async def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False
