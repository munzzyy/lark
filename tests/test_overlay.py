import asyncio
import json
import unittest

import aiohttp

from lark.overlay_server import ChatHub, OverlayServer


def run(c):
    return asyncio.run(asyncio.wait_for(c, 15))


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"chat_fade_seconds": 12, "starting_title": "Soon", "twitch_channel": "c"}

    async def serve(self):
        hub = ChatHub()
        srv = OverlayServer(hub, lambda: self.settings)
        port = await srv.start(0)
        return hub, srv, port

    def test_pages_headers_and_static(self):
        async def go():
            hub, srv, port = await self.serve()
            async with aiohttp.ClientSession() as s:
                for path, needle in (("/chat", "chat.js"), ("/starting", "starting.js"), ("/static/chat.js", "EventSource"),
                                     ("/static/chat.css", "transparent")):
                    r = await s.get(f"http://127.0.0.1:{port}{path}")
                    self.assertEqual(r.status, 200, path)
                    self.assertIn(needle, await r.text())
                    self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
                    self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
                r = await s.get(f"http://127.0.0.1:{port}/chat/config")
                self.assertEqual((await r.json())["fade"], 12)
                for bad in ("/static/../config.py", "/static/nope.js", "/static/chat.html", "/nope"):
                    r = await s.get(f"http://127.0.0.1:{port}{bad}")
                    self.assertEqual(r.status, 404, bad)
            await srv.stop()
        run(go())

    def test_binds_loopback_only_and_rejects_foreign_host(self):
        async def go():
            hub, srv, port = await self.serve()
            self.assertEqual(srv._runner.addresses[0][0], "127.0.0.1")
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/chat", headers={"Host": "evil.example:%d" % port})
                self.assertEqual(r.status, 403)
                r = await s.get(f"http://127.0.0.1:{port}/chat", headers={"Host": f"localhost:{port}"})
                self.assertEqual(r.status, 200)
            await srv.stop()
        run(go())

    def test_sse_backlog_live_and_delete(self):
        async def go():
            hub, srv, port = await self.serve()
            hub.publish({"id": "1", "user": "a", "text": "old", "ts": 1})
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"http://127.0.0.1:{port}/chat/events")
                self.assertEqual(r.headers["Content-Type"], "text/event-stream")
                got = []

                async def reader():
                    async for line in r.content:
                        line = line.decode().strip()
                        if line.startswith("data: "):
                            got.append(json.loads(line[6:]))
                            if len(got) == 3:
                                return
                t = asyncio.ensure_future(reader())
                await asyncio.sleep(0.2)
                hub.publish({"id": "2", "user": "b", "text": "new", "ts": 2})
                hub.publish({"delete": {"id": "2"}})
                await asyncio.wait_for(t, 5)
                r.close()
                self.assertEqual([g.get("text") or "del" for g in got], ["old", "new", "del"])
                self.assertEqual([m["id"] for m in hub.recent], ["1"])
            await srv.stop()
        run(go())

    def test_starting_state(self):
        async def go():
            hub, srv, port = await self.serve()
            async with aiohttp.ClientSession() as s:
                d = await (await s.get(f"http://127.0.0.1:{port}/starting/state")).json()
                self.assertEqual((d["title"], d["end"]), ("Soon", None))
                srv.starting = {"title": "Custom", "end": 123.0}
                d = await (await s.get(f"http://127.0.0.1:{port}/starting/state")).json()
                self.assertEqual((d["title"], d["end"]), ("Custom", 123.0))
            await srv.stop()
        run(go())

    def test_clearchat_all_clears(self):
        hub = ChatHub()
        hub.publish({"id": "1", "user": "a", "text": "x"})
        hub.publish({"delete": {"user": None}})
        self.assertEqual(len(hub.recent), 0)


if __name__ == "__main__":
    unittest.main()
