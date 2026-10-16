"""Fake Twitch id and Helix endpoints for tests. Nothing here talks to the real Twitch."""
import asyncio
import secrets

from aiohttp import web

CLIENT_ID = "testclientid1234567890abcdef"


class FakeTwitch:
    def __init__(self, pending_polls=2, expires_in=14400):
        self.pending_polls = pending_polls
        self.expires_in = expires_in
        self.polls = 0
        self.device_code = None
        self.access = {}      # access token -> True
        self.refresh = {}     # refresh token -> True (one use)
        self.refresh_calls = 0
        self.channel = {"title": "old title", "game_id": "1", "game_name": "Just Chatting"}
        self.requests = []
        self.deny = False
        self.fail_helix_once = False
        self.secret_seen = False

    def app(self):
        a = web.Application()
        a.router.add_post("/oauth2/device", self.device)
        a.router.add_post("/oauth2/token", self.token)
        a.router.add_get("/oauth2/validate", self.validate)
        a.router.add_get("/helix/channels", self.get_channel)
        a.router.add_patch("/helix/channels", self.patch_channel)
        a.router.add_get("/helix/search/categories", self.search)
        return a

    async def start(self):
        self.runner = web.AppRunner(self.app(), shutdown_timeout=0.2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.id_base = f"http://127.0.0.1:{self.port}"
        self.api_base = f"http://127.0.0.1:{self.port}/helix"

    async def stop(self):
        # A client cancelled mid-connect can leave a socket the server accepted but hasn't wrapped yet. If the
        # runner shuts down first, that transport is finalised after the server is gone and asyncio prints a
        # TypeError from its __del__. Let the loop drain accepts first.
        await asyncio.sleep(0.1)
        await self.runner.cleanup()

    def _new_tokens(self):
        a, r = secrets.token_hex(8), secrets.token_hex(8)
        self.access[a] = True
        self.refresh[r] = True
        return {"access_token": a, "refresh_token": r, "expires_in": self.expires_in,
                "scope": ["channel:manage:broadcast", "chat:read", "chat:edit"], "token_type": "bearer"}

    async def device(self, req):
        f = await req.post()
        self.requests.append(("device", dict(f)))
        if f.get("client_id") != CLIENT_ID or not f.get("scopes"):
            return web.json_response({"status": 400, "message": "invalid client"}, status=400)
        self.device_code = secrets.token_hex(8)
        return web.json_response({"device_code": self.device_code, "expires_in": 300, "interval": 1,
                                  "user_code": "ABCDEFGH", "verification_uri": "https://www.twitch.tv/activate?public=true"})

    async def token(self, req):
        f = await req.post()
        self.requests.append(("token", dict(f)))
        self.secret_seen = self.secret_seen or "client_secret" in f
        if f.get("grant_type") == "refresh_token":
            self.refresh_calls += 1
            if not self.refresh.pop(f.get("refresh_token"), None):
                return web.json_response({"status": 400, "message": "Invalid refresh token"}, status=400)
            return web.json_response(self._new_tokens())
        if f.get("grant_type") != "urn:ietf:params:oauth:grant-type:device_code" or f.get("device_code") != self.device_code:
            return web.json_response({"status": 400, "message": "invalid device code"}, status=400)
        if self.deny:
            return web.json_response({"status": 400, "message": "access_denied"}, status=400)
        self.polls += 1
        if self.polls <= self.pending_polls:
            return web.json_response({"status": 400, "message": "authorization_pending"}, status=400)
        return web.json_response(self._new_tokens())

    def _authed(self, req):
        tok = req.headers.get("Authorization", "")
        return tok.split(" ", 1)[-1] in self.access and tok.startswith(("Bearer ", "OAuth "))

    async def validate(self, req):
        if not self._authed(req) or not req.headers["Authorization"].startswith("OAuth "):
            return web.json_response({"status": 401, "message": "invalid access token"}, status=401)
        return web.json_response({"client_id": CLIENT_ID, "login": "munzzyy1", "user_id": "4242",
                                  "scopes": ["channel:manage:broadcast", "chat:read", "chat:edit"], "expires_in": 100})

    def _helix_guard(self, req):
        if self.fail_helix_once:
            self.fail_helix_once = False
            self.access.clear()
        if req.headers.get("Client-Id") != CLIENT_ID or not self._authed(req):
            return web.json_response({"status": 401, "message": "Invalid OAuth token"}, status=401)
        return None

    async def get_channel(self, req):
        bad = self._helix_guard(req)
        if bad:
            return bad
        self.requests.append(("get_channel", dict(req.query)))
        return web.json_response({"data": [{"broadcaster_id": req.query.get("broadcaster_id"), **self.channel}]})

    async def patch_channel(self, req):
        bad = self._helix_guard(req)
        if bad:
            return bad
        body = await req.json()
        self.requests.append(("patch_channel", dict(req.query), body))
        self.channel["title"] = body.get("title", self.channel["title"])
        if "game_id" in body:
            self.channel["game_id"] = body["game_id"]
            self.channel["game_name"] = "Game " + body["game_id"]
        return web.Response(status=204)

    async def search(self, req):
        bad = self._helix_guard(req)
        if bad:
            return bad
        q = req.query.get("query", "")
        return web.json_response({"data": [{"id": "511224", "name": f"{q} Quest"}, {"id": "33214", "name": f"{q} Arena"}]})
