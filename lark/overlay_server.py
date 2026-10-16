"""Local HTTP server for the OBS browser-source overlays. Binds 127.0.0.1 only."""
import asyncio
import collections
import json
import logging
import re
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger("lark.overlay")

STATIC = Path(__file__).parent / "overlay"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; media-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors *")


class ChatHub:
    """Fan-out of chat messages to SSE clients. Touch it only from the loop thread."""

    def __init__(self, keep=100):
        self.recent = collections.deque(maxlen=keep)
        self.subs = set()

    def publish(self, msg):
        if "delete" not in msg:
            self.recent.append(msg)
        else:
            d = msg["delete"]
            self.recent = collections.deque(
                (m for m in self.recent if not ((d.get("id") and m.get("id") == d["id"]) or
                                                (d.get("user") and m.get("user", "").lower() == d["user"].lower()) or
                                                (d.get("id") is None and d.get("user") is None))),
                maxlen=self.recent.maxlen)
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.subs.discard(q)


def _json(obj):
    return json.dumps(obj, separators=(",", ":"))


class OverlayServer:
    def __init__(self, hub, get_settings):
        self.hub = hub
        self.get_settings = get_settings
        self.starting = {"title": "", "end": None}
        self.extensions = []
        self.port = None
        self._runner = None

    def make_app(self):
        app = web.Application(middlewares=[self._guard])
        r = app.router
        r.add_get("/", self._index)
        r.add_get("/chat", self._page("chat.html"))
        r.add_get("/starting", self._page("starting.html"))
        r.add_get("/chat/config", self._chat_config)
        r.add_get("/chat/events", self._events)
        r.add_get("/starting/state", self._starting_state)
        r.add_get("/static/{name}", self._static)
        for add_routes in self.extensions:
            add_routes(app)
        return app

    @web.middleware
    async def _guard(self, request, handler):
        # A page on another origin can point a DNS name at 127.0.0.1; refuse any Host we did not hand out.
        host = request.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return web.Response(status=403, text="forbidden")
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp

    def _page(self, name):
        async def h(_):
            return web.Response(body=(STATIC / name).read_bytes(), content_type="text/html", charset="utf-8")
        return h

    async def _index(self, _):
        body = ("<!doctype html><meta charset=utf-8><title>Lark overlays</title>"
                "<p>Lark overlays: <a href=/chat>/chat</a> <a href=/starting>/starting</a>")
        return web.Response(text=body, content_type="text/html")

    async def _static(self, request):
        name = request.match_info["name"]
        if not re.fullmatch(r"[a-z0-9_.-]+\.(js|css)", name):
            raise web.HTTPNotFound()
        p = STATIC / name
        if not p.is_file():
            raise web.HTTPNotFound()
        ctype = "text/javascript" if name.endswith(".js") else "text/css"
        return web.Response(body=p.read_bytes(), content_type=ctype, charset="utf-8")

    async def _chat_config(self, _):
        s = self.get_settings()
        return web.Response(text=_json({"fade": s.get("chat_fade_seconds", 30), "channel": s.get("twitch_channel", "")}),
                            content_type="application/json")

    async def _starting_state(self, _):
        s = self.get_settings()
        st = self.starting
        return web.Response(text=_json({"title": st["title"] or s.get("starting_title", "Starting soon"),
                                        "end": st["end"], "now": time.time()}), content_type="application/json")

    async def _events(self, request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q = asyncio.Queue(maxsize=500)
        for m in list(self.hub.recent):
            q.put_nowait(m)
        self.hub.subs.add(q)
        try:
            await resp.write(b"retry: 2000\n\n")
            while True:
                try:
                    m = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"data: {_json(m)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.subs.discard(q)
        return resp

    async def start(self, port):
        runner = web.AppRunner(self.make_app(), access_log=None, shutdown_timeout=0.5)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        self._runner = runner
        self.port = site._server.sockets[0].getsockname()[1]
        log.info("overlays on http://127.0.0.1:%s", self.port)
        return self.port

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
