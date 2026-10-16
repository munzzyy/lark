import asyncio
import json
import unittest

from lark.keyring import MemoryStore
from lark.twitch_api import TwitchClient, TwitchError, TOKEN_KEY
from tests.fake_twitch import CLIENT_ID, FakeTwitch


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


async def fast_sleep(_):
    await asyncio.sleep(0)


def run(c):
    return asyncio.run(asyncio.wait_for(c, 20))


class Rig:
    def __init__(self, client_id=CLIENT_ID, **fake_kw):
        self.fake = FakeTwitch(**fake_kw)
        self.store = MemoryStore()
        self.updates = []
        self.clock = Clock()
        self.client_id = client_id

    async def __aenter__(self):
        await self.fake.start()
        self.c = TwitchClient(lambda: self.client_id, self.store, self.updates.append, self.fake.id_base,
                              self.fake.api_base, clock=self.clock, sleep=fast_sleep)
        return self

    async def __aexit__(self, *a):
        await self.fake.stop()

    async def signed_in(self):
        await self.c.connect()
        assert self.c.connected, self.c.state


class DeviceFlowTests(unittest.TestCase):
    def test_full_flow_shows_code_then_connects_and_stores_token_in_the_store(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                pending = [u for u in r.updates if u["status"] == "pending"]
                self.assertEqual(pending[0]["user_code"], "ABCDEFGH")
                self.assertIn("twitch.tv/activate", pending[0]["verification_uri"])
                self.assertEqual(r.c.state["login"], "munzzyy1")
                self.assertEqual(r.c.state["channel"]["title"], "old title")
                saved = json.loads(r.store.data[TOKEN_KEY])
                self.assertEqual(saved["login"], "munzzyy1")
                self.assertEqual(r.fake.polls, 3)
                self.assertFalse(r.fake.secret_seen, "a public client must not send a secret")
                dev = [d for k, d in r.fake.requests if k == "device"][0]
                self.assertIn("chat:edit", dev["scopes"])
        run(go())

    def test_no_client_id_connect_raises_and_restore_is_quiet(self):
        async def go():
            async with Rig(client_id="") as r:
                with self.assertRaises(TwitchError):
                    await r.c.connect()
                await r.c.restore()
                self.assertEqual(r.c.state["status"], "off")
                self.assertEqual(r.fake.requests, [])
        run(go())

    def test_denied_ends_in_error_state(self):
        async def go():
            async with Rig() as r:
                r.fake.deny = True
                await r.c.connect()
                self.assertEqual(r.c.state["status"], "error")
                self.assertIn("access_denied", r.c.state["message"])
                self.assertNotIn(TOKEN_KEY, r.store.data)
        run(go())

    def test_cancel_returns_to_off(self):
        async def go():
            async with Rig(pending_polls=10_000) as r:
                t = asyncio.ensure_future(r.c.connect())
                for _ in range(200):
                    if r.c.state["status"] == "pending":
                        break
                    await asyncio.sleep(0.01)
                r.c.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await t
                self.assertEqual(r.c.state["status"], "off")
        run(go())

    def test_unreachable_twitch_is_a_clear_error(self):
        async def go():
            async with Rig() as r:
                r.c._id = "http://127.0.0.1:1"
                await r.c.connect()
                self.assertEqual(r.c.state["status"], "error")
                self.assertIn("Could not reach Twitch", r.c.state["message"])
        run(go())


class TokenTests(unittest.TestCase):
    def test_refresh_on_expiry_rotates_and_saves_the_new_refresh_token(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                old = json.loads(r.store.data[TOKEN_KEY])
                r.clock.t += 14400 - 60  # inside the refresh margin
                a = await r.c.access_token()
                new = json.loads(r.store.data[TOKEN_KEY])
                self.assertEqual(r.fake.refresh_calls, 1)
                self.assertNotEqual(new["refresh_token"], old["refresh_token"])
                self.assertEqual(new["access_token"], a)
                r.clock.t += 14400 - 60
                await r.c.access_token()
                self.assertEqual(r.fake.refresh_calls, 2)
        run(go())

    def test_concurrent_callers_refresh_only_once(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                r.clock.t += 14400
                toks = await asyncio.gather(*[r.c.access_token() for _ in range(5)])
                self.assertEqual(len(set(toks)), 1)
                self.assertEqual(r.fake.refresh_calls, 1)
        run(go())

    def test_restore_from_store_uses_refresh_when_stale(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                blob = r.store.data[TOKEN_KEY]
                r2 = TwitchClient(lambda: CLIENT_ID, r.store, None, r.fake.id_base, r.fake.api_base,
                                  clock=r.clock, sleep=fast_sleep)
                r.clock.t += 20000
                await r2.restore()
                self.assertTrue(r2.connected)
                self.assertEqual(r.fake.refresh_calls, 1)
                self.assertNotEqual(r.store.data[TOKEN_KEY], blob)
        run(go())

    def test_dead_refresh_token_clears_the_store_and_says_so(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                r.fake.refresh.clear()
                r.clock.t += 20000
                with self.assertRaises(TwitchError):
                    await r.c.access_token()
                self.assertNotIn(TOKEN_KEY, r.store.data)
                self.assertEqual(r.c.state["status"], "error")
        run(go())

    def test_restore_ignores_a_token_from_another_client_id_and_garbage(self):
        async def go():
            async with Rig() as r:
                r.store.data[TOKEN_KEY] = json.dumps({"client_id": "other", "refresh_token": "x"})
                await r.c.restore()
                self.assertEqual(r.c.state["status"], "off")
                r.store.data[TOKEN_KEY] = "{not json"
                with self.assertLogs("lark.twitch", "WARNING"):
                    await r.c.restore()
                self.assertEqual(r.c.state["status"], "off")
        run(go())

    def test_disconnect_clears_everything(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                await r.c.disconnect()
                self.assertNotIn(TOKEN_KEY, r.store.data)
                self.assertEqual(r.c.state["status"], "off")
                self.assertIsNone(await r.c.chat_auth())
        run(go())


class HelixTests(unittest.TestCase):
    def test_get_set_channel_and_search(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                ch = await r.c.set_channel("  new   title ", "511224")
                self.assertEqual(ch["title"], "new title")
                self.assertEqual(ch["game_id"], "511224")
                patch = [x for x in r.fake.requests if x[0] == "patch_channel"][0]
                self.assertEqual(patch[1]["broadcaster_id"], "4242")
                self.assertEqual(patch[2], {"title": "new title", "game_id": "511224"})
                cats = await r.c.search_categories("Rocket")
                self.assertEqual(cats[0], {"id": "511224", "name": "Rocket Quest"})
                self.assertEqual(await r.c.search_categories("   "), [])
        run(go())

    def test_title_validation(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                for bad in ("", "   ", "x" * 141):
                    with self.assertRaises(TwitchError):
                        await r.c.set_channel(bad)
                self.assertFalse(any(x[0] == "patch_channel" for x in r.fake.requests))
        run(go())

    def test_a_401_triggers_one_refresh_and_a_retry(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                r.fake.fail_helix_once = True
                ch = await r.c.get_channel()
                self.assertEqual(ch["title"], "old title")
                self.assertEqual(r.fake.refresh_calls, 1)
        run(go())

    def test_chat_auth_gives_login_and_oauth_token(self):
        async def go():
            async with Rig() as r:
                await r.signed_in()
                login, tok = await r.c.chat_auth()
                self.assertEqual(login, "munzzyy1")
                self.assertTrue(tok.startswith("oauth:") and len(tok) > 10)
        run(go())


if __name__ == "__main__":
    unittest.main()
