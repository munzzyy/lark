"""Alert and event pipeline: every event, real or test, goes through AlertEngine.ingest on the services loop."""
import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from . import events as ev
from .overlay_server import ChatHub, STATIC

log = logging.getLogger("lark.alerts")


def _json(obj):
    return json.dumps(obj, separators=(",", ":"))


class AlertEngine:
    def __init__(self, get_values, on_event=None, log_dir=None):
        self.get_values = get_values
        self.on_event = on_event or (lambda e: None)
        self.log = ev.EventLog(log_dir)
        self.alert_hub = ChatHub(keep=0)
        self.event_hub = ChatHub(keep=50)

    def load(self):
        self.log.load()
        for e in self.log.recent()[-50:]:
            self.event_hub.recent.append(self._row(e))

    def _row(self, e):
        s = ev.alert_settings(self.get_values(), e["type"])
        return {**e, "label": ev.LABELS[e["type"]], "text": ev.render(s["template"], e)}

    def ingest(self, e):
        self.log.add(e)
        self.event_hub.publish(self._row(e))
        s = ev.alert_settings(self.get_values(), e["type"])
        if s["enabled"]:
            self.alert_hub.publish(self._alert(e, s))
        self.on_event(e)

    def _alert(self, e, s):
        sound = ""
        if s["sound"] and ev.sound_problem(s["sound"]) is None:
            try:
                sound = f"/alerts/sound/{e['type']}?v={int(Path(s['sound']).expanduser().stat().st_mtime)}"
            except OSError:
                sound = ""
        vol = self.get_values().get("alert_volume", 80)
        return {"id": e["id"], "type": e["type"], "title": ev.TITLES[e["type"]], "text": ev.render(s["template"], e),
                "user": e["user"], "amount": e["amount"], "message": e["message"], "duration": s["duration"],
                "sound": sound, "volume": vol / 100}

    def register(self, app):
        r = app.router
        for path, name in (("/alerts", "alerts.html"), ("/events", "events.html")):
            r.add_get(path, self._page(name))
        r.add_get("/alerts/stream", self._sse(self.alert_hub))
        r.add_get("/events/stream", self._sse(self.event_hub))
        r.add_get("/alerts/sound/{kind}", self._sound)

    def _page(self, name):
        async def h(_):
            return web.Response(body=(STATIC / name).read_bytes(), content_type="text/html", charset="utf-8")
        return h

    def _sse(self, hub):
        async def h(request):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"})
            await resp.prepare(request)
            q = asyncio.Queue(maxsize=500)
            for m in list(hub.recent):
                q.put_nowait(m)
            hub.subs.add(q)
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
                hub.subs.discard(q)
            return resp
        return h

    async def _sound(self, request):
        kind = request.match_info["kind"]
        if kind not in ev.TYPES:
            raise web.HTTPNotFound()
        path = ev.alert_settings(self.get_values(), kind)["sound"]
        if not path or ev.sound_problem(path) is not None:
            raise web.HTTPNotFound()
        p = Path(path).expanduser()
        return web.FileResponse(p, headers={"Content-Type": SOUND_TYPES.get(p.suffix.lower(), "application/octet-stream")})


SOUND_TYPES = {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".opus": "audio/ogg",
               ".flac": "audio/flac", ".m4a": "audio/mp4"}
