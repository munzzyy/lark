import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import aiohttp
from PySide6.QtWidgets import QApplication, QFrame

from lark import events as ev
from lark.alerts import AlertEngine
from lark.app import NAV, VIEWS, build_context
from lark.config import Config
from lark.overlay_server import ChatHub, OverlayServer
from lark.pages.events import EventsPage
from lark.pages.settings import SettingsPage
from tests.test_ui_smoke import pump


def run(c):
    return asyncio.run(asyncio.wait_for(c, 15))


class Rig:
    def __init__(self, values=None):
        self.values = values or {}
        self.tmp = tempfile.TemporaryDirectory()
        self.seen = []

    async def __aenter__(self):
        self.engine = AlertEngine(lambda: self.values, self.seen.append, Path(self.tmp.name) / "events")
        self.srv = OverlayServer(ChatHub(), lambda: self.values)
        self.srv.extensions.append(self.engine.register)
        self.port = await self.srv.start(0)
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *a):
        await self.srv.stop()
        self.tmp.cleanup()

    async def first(self, s, path, timeout=3):
        r = await s.get(self.base + path)
        try:
            while True:
                line = (await asyncio.wait_for(r.content.readline(), timeout)).decode()
                if line.startswith("data: "):
                    return json.loads(line[6:])
        except asyncio.TimeoutError:
            return None
        finally:
            r.close()


class AlertServerTests(unittest.TestCase):
    def test_pages_static_files_and_csp(self):
        async def go():
            async with Rig() as r, aiohttp.ClientSession() as s:
                for path, needle in (("/alerts", "alerts.js"), ("/events", "events.js"), ("/static/alerts.js", "EventSource"),
                                     ("/static/events.js", "EventSource"), ("/static/alerts.css", "transparent"),
                                     ("/static/events.css", "transparent")):
                    resp = await s.get(r.base + path)
                    self.assertEqual(resp.status, 200, path)
                    self.assertIn(needle, await resp.text())
                    self.assertIn("media-src 'self'", resp.headers["Content-Security-Policy"])
                    self.assertIn("script-src 'self'", resp.headers["Content-Security-Policy"])
                resp = await s.get(r.base + "/alerts", headers={"Host": f"evil.example:{r.port}"})
                self.assertEqual(resp.status, 403)
        run(go())

    def test_alert_message_carries_text_duration_and_title(self):
        async def go():
            r = Rig({"alert_settings": {"cheer": {"duration": 9, "template": "{user} gave {amount}: {message}"}},
                     "alert_volume": 50})
            async with r, aiohttp.ClientSession() as s:
                task = asyncio.ensure_future(r.first(s, "/alerts/stream"))
                await asyncio.sleep(0.2)
                r.engine.ingest(ev.make_event("cheer", "Bob", 100, "<b>hi</b>"))
                m = await task
                self.assertEqual((m["type"], m["title"], m["duration"], m["volume"]), ("cheer", "Bits", 9, 0.5))
                self.assertEqual(m["text"], "Bob gave 100: <b>hi</b>")
                self.assertEqual(m["sound"], "")
                self.assertEqual(len(r.seen), 1)
        run(go())

    def test_disabled_type_is_logged_and_listed_but_not_alerted(self):
        async def go():
            r = Rig({"alert_settings": {"follow": {"enabled": False}}})
            async with r, aiohttp.ClientSession() as s:
                a = asyncio.ensure_future(r.first(s, "/alerts/stream", timeout=1))
                await asyncio.sleep(0.2)
                r.engine.ingest(ev.make_event("follow", "Quiet"))
                self.assertIsNone(await a)
                row = await r.first(s, "/events/stream")
                self.assertEqual((row["user"], row["label"], row["text"]), ("Quiet", "Follow", "Quiet just followed"))
                self.assertEqual(len(list((Path(r.tmp.name) / "events").glob("*.jsonl"))), 1)
        run(go())

    def test_alerts_do_not_replay_but_events_do(self):
        async def go():
            async with Rig() as r, aiohttp.ClientSession() as s:
                r.engine.ingest(ev.make_event("raid", "Old", 3))
                self.assertIsNone(await r.first(s, "/alerts/stream", timeout=1))
                self.assertEqual((await r.first(s, "/events/stream"))["user"], "Old")
        run(go())

    def test_sound_route_serves_only_the_configured_file(self):
        async def go():
            d = tempfile.mkdtemp()
            snd = Path(d) / "ding.mp3"
            snd.write_bytes(b"ID3fake")
            secret = Path(d) / "secret.txt"
            secret.write_text("nope")
            r = Rig({"alert_settings": {"follow": {"sound": str(snd)}, "raid": {"sound": str(secret)}}})
            async with r, aiohttp.ClientSession() as s:
                resp = await s.get(r.base + "/alerts/sound/follow")
                self.assertEqual((resp.status, resp.headers["Content-Type"]), (200, "audio/mpeg"))
                self.assertEqual(await resp.read(), b"ID3fake")
                for path in ("/alerts/sound/raid", "/alerts/sound/cheer", "/alerts/sound/..%2Fsecret", "/alerts/sound/nope"):
                    resp = await s.get(r.base + path)
                    self.assertEqual(resp.status, 404, path)
                task = asyncio.ensure_future(r.first(s, "/alerts/stream"))
                await asyncio.sleep(0.2)
                r.engine.ingest(ev.make_event("follow", "A"))
                self.assertRegex((await task)["sound"], r"^/alerts/sound/follow\?v=\d+$")
                snd.unlink()
                self.assertEqual((await s.get(r.base + "/alerts/sound/follow")).status, 404)
        run(go())

    def test_engine_loads_the_saved_log_into_the_events_overlay(self):
        async def go():
            r = Rig()
            async with r, aiohttp.ClientSession() as s:
                r.engine.ingest(ev.make_event("follow", "Saved"))
                again = AlertEngine(lambda: {}, log_dir=Path(r.tmp.name) / "events")
                again.load()
                self.assertEqual([e["user"] for e in again.log.recent()], ["Saved"])
                self.assertEqual([m["user"] for m in again.event_hub.recent], ["Saved"])
        run(go())


def sse_reader(url, out, ready, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.readline()
            ready.set()
            for raw in resp:
                line = raw.decode()
                if line.startswith("data: "):
                    out.append(json.loads(line[6:]))
                    return
    except OSError:
        pass
    finally:
        ready.set()


class EventsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.mkdtemp()
        cfg = Config(Path(cls.tmp) / "config.json")
        cfg.set("overlay_port", 1024 + (os.getpid() % 30000) + 9)
        cfg.set("camera_module_dir", "/nonexistent")
        cls.cfg = cfg
        cls.ctx = build_context(cfg, services_kw={"log_dir": Path(cls.tmp) / "events"})
        cls.ctx.services.start()
        assert pump(cls.app, lambda: cls.ctx.services.port)
        cls.page = EventsPage(cls.ctx)
        cls.settings = SettingsPage(cls.ctx)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.services.stop()

    def alert_after(self, action, timeout=4):
        out, ready = [], threading.Event()
        t = threading.Thread(target=sse_reader, args=(self.ctx.services.url("/alerts/stream"), out, ready, timeout), daemon=True)
        t.start()
        self.assertTrue(pump(self.app, ready.is_set))
        action()
        pump(self.app, lambda: bool(out), timeout)
        t.join(timeout + 1)
        return out[0] if out else None

    def alert_card(self):
        return next(w for w in self.settings.findChildren(QFrame) if hasattr(w, "save_alerts"))

    def test_1_events_page_is_in_the_rail_after_chat(self):
        self.assertEqual(VIEWS[VIEWS.index("chat") + 1], "events")
        self.assertIn(("events", "Events"), NAV)
        self.assertEqual(set(self.page.test_buttons), set(ev.TYPES))

    def test_2_test_button_goes_through_the_real_path_to_list_and_overlay(self):
        m = self.alert_after(self.page.test_buttons["follow"].click)
        self.assertEqual((m["type"], m["text"]), ("follow", "TestViewer just followed"))
        self.assertTrue(pump(self.app, lambda: "TestViewer just followed" in self.page.log.toPlainText()))
        self.assertIn("(test)", self.page.log.toPlainText())
        self.assertEqual(list((Path(self.tmp) / "events").glob("*.jsonl")), [])

    def test_3_type_filters(self):
        self.page.test_buttons["raid"].click()
        self.assertTrue(pump(self.app, lambda: "TestRaider" in self.page.log.toPlainText()))
        self.page.filters["follow"].setChecked(False)
        self.assertNotIn("TestViewer just followed", self.page.log.toPlainText())
        self.assertIn("TestRaider", self.page.log.toPlainText())
        self.page.filters["follow"].setChecked(True)
        self.assertIn("TestViewer just followed", self.page.log.toPlainText())

    def test_4_settings_section_saves_and_applies_live(self):
        card = self.alert_card()
        on, secs, text, sound = card.rows["cheer"]
        text.setText("{user} dropped {amount} bits")
        secs.setValue(3)
        card.rows["follow"][0].setChecked(False)
        card.save_alerts()
        saved = Config(Path(self.tmp) / "config.json").get("alert_settings")
        self.assertEqual(saved["cheer"]["duration"], 3)
        self.assertFalse(saved["follow"]["enabled"])
        m = self.alert_after(self.page.test_buttons["cheer"].click)
        self.assertEqual((m["text"], m["duration"]), ("TestViewer dropped 500 bits", 3))
        self.assertIsNone(self.alert_after(self.page.test_buttons["follow"].click, timeout=1))

    def test_5_settings_section_refuses_a_missing_sound_file(self):
        card = self.alert_card()
        card.rows["raid"][3].setText("/nonexistent/ding.mp3")
        card.save_alerts()
        self.assertEqual(self.cfg.get("alert_settings").get("raid", {}).get("sound", ""), "")
        card.rows["raid"][3].setText("")

    def test_6_eventsub_stays_idle_without_a_token(self):
        self.assertTrue(pump(self.app, lambda: self.ctx.services.eventsub_status[0] == "idle"))
        self.assertIn("Sign in", self.ctx.services.eventsub_status[1])
        self.assertEqual(self.page.pill.text(), "Idle")


class ServicesEventSubTests(unittest.TestCase):
    """Sign in through the fake Twitch, then watch a pushed notification reach the log and the alert hub."""

    def test_signed_in_services_receive_events_end_to_end(self):
        from lark.keyring import MemoryStore
        from lark.services import Services
        from tests.fake_eventsub import FakeEventSub
        from tests.fake_twitch import CLIENT_ID

        async def instant(_):
            await asyncio.sleep(0.01)
        app = QApplication.instance() or QApplication([])
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()

        def call(coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result(10)
        fake = FakeEventSub(pending_polls=1)
        call(fake.start())
        tmp = tempfile.mkdtemp()
        cfg = Config(Path(tmp) / "config.json")
        cfg.set("overlay_port", 1024 + (os.getpid() % 30000) + 11)
        cfg.set("twitch_client_id", CLIENT_ID)
        cfg.set("twitch_channel", "")
        svc = Services(cfg, twitch_kw=dict(store=MemoryStore(), id_base=fake.id_base, api_base=fake.api_base, sleep=instant),
                       eventsub_kw=dict(url=fake.url, backoff=(0.05, 0.2)), log_dir=Path(tmp) / "events")
        got = []
        svc.event_added.connect(got.append)
        svc.start()
        try:
            self.assertTrue(pump(app, lambda: svc.port and svc.eventsub_status[0] == "idle"))
            svc.twitch_connect()
            self.assertTrue(pump(app, lambda: svc.eventsub_status[0] == "connected", 10), svc.eventsub_status)
            self.assertEqual(len(fake.subs), 9)
            call(fake.send(fake.notification("channel.cheer", {"user_name": "Zed", "bits": 42, "message": "gg"})))
            self.assertTrue(pump(app, lambda: got))
            self.assertEqual((got[0]["type"], got[0]["user"], got[0]["amount"]), ("cheer", "Zed", 42))
            lines = [json.loads(x) for f in (Path(tmp) / "events").glob("*.jsonl") for x in f.read_text().splitlines()]
            self.assertEqual([x["user"] for x in lines], ["Zed"])
            svc.twitch_disconnect()
            self.assertTrue(pump(app, lambda: svc.eventsub_status[0] == "idle" and not fake.sockets, 10))
        finally:
            svc.stop()
            call(fake.stop())
            loop.call_soon_threadsafe(loop.stop)
            time.sleep(0.1)
            loop.close()


if __name__ == "__main__":
    unittest.main()
