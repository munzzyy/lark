import asyncio
import logging
import unittest

from lark.eventsub import EventSubClient, SUBSCRIPTIONS
from lark.keyring import MemoryStore
from lark.twitch_api import TwitchClient
from tests.fake_eventsub import ALL_SCOPES, FakeEventSub
from tests.fake_twitch import CLIENT_ID


async def fast_sleep(_):
    await asyncio.sleep(0)


def setUpModule():
    logging.getLogger("lark.eventsub").disabled = True


def tearDownModule():
    logging.getLogger("lark.eventsub").disabled = False


def run(c):
    return asyncio.run(asyncio.wait_for(c, 30))


class Rig:
    def __init__(self, **kw):
        self.fake = FakeEventSub(**kw)
        self.events, self.states = [], []

    async def __aenter__(self):
        await self.fake.start()
        self.tw = TwitchClient(lambda: CLIENT_ID, MemoryStore(), None, self.fake.id_base, self.fake.api_base,
                               sleep=fast_sleep)
        self.es = EventSubClient(self.tw, self.events.append, lambda s, d: self.states.append((s, d)),
                                 url=self.fake.url, backoff=(0.05, 0.2), grace=0.3, hello_wait=1)
        self.task = asyncio.ensure_future(self.es.run())
        return self

    async def __aexit__(self, *a):
        await self.es.stop()
        await asyncio.wait({self.task}, timeout=5)
        await self.fake.stop()

    async def sign_in(self):
        await self.tw.connect()
        assert self.tw.connected
        self.es.kick()

    def status(self):
        return self.states[-1][0] if self.states else None


class EventSubTests(unittest.TestCase):
    def test_idle_without_a_token_never_opens_a_socket(self):
        async def go():
            async with Rig() as r:
                await asyncio.sleep(0.3)
                self.assertEqual(r.status(), "idle")
                self.assertIn("Sign in", r.states[-1][1])
                self.assertEqual(r.fake.connects, 0)
        run(go())

    def test_subscribes_to_all_nine_types_with_the_session_id(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                self.assertTrue(await r.fake.wait_for(lambda: len(r.fake.subs) == 9))
                got = {(s["type"], s["version"]) for s in r.fake.subs}
                self.assertEqual(got, {("channel.follow", "2"), ("channel.subscribe", "1"),
                                       ("channel.subscription.gift", "1"), ("channel.subscription.message", "1"),
                                       ("channel.cheer", "1"), ("channel.raid", "1"), ("stream.online", "1"),
                                       ("stream.offline", "1"), ("channel.update", "2")})
                by = {s["type"]: s for s in r.fake.subs}
                sid = next(iter(r.fake.sockets))
                self.assertTrue(all(s["transport"] == {"method": "websocket", "session_id": sid} for s in r.fake.subs))
                self.assertEqual(by["channel.follow"]["condition"], {"broadcaster_user_id": "4242", "moderator_user_id": "4242"})
                self.assertEqual(by["channel.raid"]["condition"], {"to_broadcaster_user_id": "4242"})
                self.assertEqual(by["channel.cheer"]["condition"], {"broadcaster_user_id": "4242"})
                self.assertTrue(await r.fake.wait_for(lambda: r.status() == "connected"))
                self.assertIn("9 of 9", r.states[-1][1])
                self.assertTrue(r.fake.sub_headers[0]["Authorization"].startswith("Bearer "))
                self.assertEqual(len(SUBSCRIPTIONS), 9)
        run(go())

    def test_notifications_become_events_and_duplicates_are_dropped(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                await r.fake.wait_for(lambda: r.status() == "connected")
                f = r.fake
                await f.send(f.notification("channel.follow", {"user_name": "Ann", "followed_at": "2026-10-15T01:02:03.5Z"}, "dup"))
                await f.send(f.notification("channel.follow", {"user_name": "Ann"}, "dup"))
                await f.send(f.envelope("session_keepalive"))
                await f.send(f.notification("channel.cheer", {"user_name": "Bob", "bits": 300, "message": "hi", "is_anonymous": False}))
                await f.send(f.notification("channel.cheer", {"user_name": "Bob", "bits": 5, "message": "", "is_anonymous": True}))
                await f.send(f.notification("channel.raid", {"from_broadcaster_user_name": "Cat", "viewers": 17}))
                await f.send(f.notification("channel.subscribe", {"user_name": "Gifted", "tier": "1000", "is_gift": True}))
                await f.send(f.notification("channel.subscription.gift", {"user_name": "Dee", "total": 5, "tier": "1000", "is_anonymous": False}))
                await f.send(f.notification("channel.subscription.message", {"user_name": "Eve", "cumulative_months": 7, "message": {"text": "yo"}, "tier": "1000"}))
                await f.send(f.notification("stream.online", {"started_at": "2026-10-15T01:02:03Z"}))
                await f.send(f.notification("unknown.type", {}))
                self.assertTrue(await f.wait_for(lambda: len(r.events) == 7))
                kinds = [(e["type"], e["user"], e["amount"], e["message"]) for e in r.events]
                self.assertEqual(kinds, [("follow", "Ann", None, ""), ("cheer", "Bob", 300, "hi"),
                                         ("cheer", "Anonymous", 5, ""), ("raid", "Cat", 17, ""),
                                         ("gift", "Dee", 5, "Tier 1"), ("resub", "Eve", 7, "yo"),
                                         ("stream_online", "", None, "")])
        run(go())

    def test_missing_scopes_skip_those_subscriptions_and_say_so(self):
        async def go():
            async with Rig(scopes=["chat:read"]) as r:
                await r.sign_in()
                self.assertTrue(await r.fake.wait_for(lambda: r.status() == "connected"))
                self.assertEqual({s["type"] for s in r.fake.subs},
                                 {"channel.raid", "stream.online", "stream.offline", "channel.update"})
                self.assertIn("4 of 9", r.states[-1][1])
                self.assertIn("follows", r.states[-1][1])
        run(go())

    def test_a_refused_subscription_does_not_stop_the_others(self):
        async def go():
            async with Rig() as r:
                r.fake.reject["channel.cheer"] = 403
                await r.sign_in()
                self.assertTrue(await r.fake.wait_for(lambda: r.status() == "connected"))
                self.assertEqual(len(r.fake.subs), 8)
                self.assertIn("bits", r.states[-1][1])
        run(go())

    def test_all_refused_is_an_error_and_it_retries(self):
        async def go():
            async with Rig() as r:
                for s in SUBSCRIPTIONS:
                    r.fake.reject[s[0]] = 400
                await r.sign_in()
                self.assertTrue(await r.fake.wait_for(lambda: r.fake.connects >= 2))
                self.assertIn("error", [st for st, _ in r.states])
        run(go())

    def test_reconnect_message_moves_to_the_new_socket_without_resubscribing(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                await r.fake.wait_for(lambda: r.status() == "connected")
                old = next(iter(r.fake.sockets))
                url = r.fake.url.replace("/ws", "/ws2")
                await r.fake.send(r.fake.envelope("session_reconnect", {"session": {"id": old, "reconnect_url": url}}))
                self.assertTrue(await r.fake.wait_for(lambda: r.fake.connects == 2 and old not in r.fake.sockets))
                self.assertEqual(len(r.fake.subs), 9)
                await r.fake.send(r.fake.notification("channel.follow", {"user_name": "After"}))
                self.assertTrue(await r.fake.wait_for(lambda: [e["user"] for e in r.events] == ["After"]))
        run(go())

    def test_a_bad_reconnect_address_is_refused(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                await r.fake.wait_for(lambda: r.status() == "connected")
                await r.fake.send(r.fake.envelope("session_reconnect", {"session": {"reconnect_url": "http://evil.example/"}}))
                self.assertTrue(await r.fake.wait_for(lambda: any("bad reconnect" in d for _, d in r.states)))
        run(go())

    def test_silence_past_the_keepalive_timeout_reconnects(self):
        async def go():
            async with Rig(keepalive=1) as r:
                r.es._grace = 0.2
                await r.sign_in()
                self.assertTrue(await r.fake.wait_for(lambda: r.fake.connects >= 2, timeout=10))
                self.assertTrue(await r.fake.wait_for(lambda: r.status() == "connected"))
        run(go())

    def test_no_welcome_is_an_error_not_a_hang(self):
        async def go():
            async with Rig() as r:
                r.fake.hello = False
                await r.sign_in()
                self.assertTrue(await r.fake.wait_for(lambda: r.status() == "error", timeout=5))
                self.assertEqual(len(r.fake.subs), 0)
        run(go())

    def test_revocation_is_reported(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                await r.fake.wait_for(lambda: r.status() == "connected")
                await r.fake.send(r.fake.envelope("revocation", {"subscription": {"type": "channel.cheer", "status": "authorization_revoked"}}))
                self.assertTrue(await r.fake.wait_for(lambda: "8 of 9" in r.states[-1][1]))
        run(go())

    def test_signing_out_closes_the_socket_and_goes_idle(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                await r.fake.wait_for(lambda: r.status() == "connected")
                await r.tw.disconnect()
                r.es.kick()
                self.assertTrue(await r.fake.wait_for(lambda: r.status() == "idle" and not r.fake.sockets))
        run(go())

    def test_stale_notifications_are_ignored(self):
        async def go():
            async with Rig() as r:
                await r.sign_in()
                await r.fake.wait_for(lambda: r.status() == "connected")
                old = r.fake.notification("channel.follow", {"user_name": "Old"})
                old["metadata"]["message_timestamp"] = "2020-01-01T00:00:00Z"
                await r.fake.send(old)
                await r.fake.send(r.fake.notification("channel.follow", {"user_name": "New"}))
                self.assertTrue(await r.fake.wait_for(lambda: len(r.events) >= 1))
                self.assertEqual([e["user"] for e in r.events], ["New"])
        run(go())

    def test_scope_list_covers_what_sign_in_asks_for(self):
        from lark.twitch_api import SCOPES
        self.assertTrue(set(ALL_SCOPES[3:]) <= set(SCOPES))


if __name__ == "__main__":
    unittest.main()
