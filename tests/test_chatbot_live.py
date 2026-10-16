"""Bot wired to the real IRC client, Twitch client and LiveIO, against a local IRC server and a fake Helix."""
import asyncio
import unittest

from aiohttp import web

from lark.botruntime import LiveIO
from lark.chatbot import Bot
from lark.keyring import MemoryStore
from lark.twitch_api import MOD_SCOPES, TwitchClient
from lark.twitch_irc import TwitchChat
from tests.fake_twitch import CLIENT_ID, FakeTwitch


async def fast_sleep(_):
    await asyncio.sleep(0)


class ModTwitch(FakeTwitch):
    def __init__(self, scopes=None, **kw):
        super().__init__(**kw)
        self.scopes = scopes if scopes is not None else ["channel:manage:broadcast", "chat:read", "chat:edit", *MOD_SCOPES]
        self.mod_calls = []
        self.deny_mod = None
        self.stream = []

    def app(self):
        a = super().app()
        a.router.add_delete("/helix/moderation/chat", self.mod("delete"))
        a.router.add_post("/helix/moderation/bans", self.mod("ban"))
        a.router.add_post("/helix/moderation/warnings", self.mod("warn"))
        a.router.add_get("/helix/users", self.users)
        a.router.add_get("/helix/streams", self.streams)
        return a

    async def validate(self, req):
        r = await super().validate(req)
        if r.status != 200:
            return r
        return web.json_response({"client_id": CLIENT_ID, "login": "munzzyy1", "user_id": "4242",
                                  "scopes": self.scopes, "expires_in": 100})

    def mod(self, kind):
        async def handler(req):
            bad = self._helix_guard(req)
            if bad:
                return bad
            body = await req.json() if req.can_read_body else None
            self.mod_calls.append((kind, dict(req.query), body))
            if self.deny_mod:
                return web.json_response({"status": 403, "message": self.deny_mod}, status=403)
            return web.json_response({"data": [{}]}) if kind != "delete" else web.Response(status=204)
        return handler

    async def users(self, req):
        bad = self._helix_guard(req)
        return bad or web.json_response({"data": [{"id": "999", "login": req.query.get("login")}]})

    async def streams(self, req):
        bad = self._helix_guard(req)
        self.requests.append(("streams", dict(req.query)))
        return bad or web.json_response({"data": self.stream})


class Irc:
    def __init__(self):
        self.lines, self.w, self.passwords = [], None, []

    async def start(self):
        self.srv = await asyncio.start_server(self._conn, "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]

    async def _conn(self, r, w):
        head = [(await r.readline()).decode().strip() for _ in range(4)]
        self.passwords.append(head[1])
        self.w = w
        w.write(b":x JOIN #munzzyy1\r\n")
        await w.drain()
        try:
            while True:
                line = await r.readline()
                if not line:
                    break
                self.lines.append(line.decode().strip())
        finally:
            w.close()

    async def push(self, text, user="viewer", tags=""):
        self.w.write(f"@{tags}display-name={user.title()};user-id=u-{user};id=id-{user} :{user}!{user}@x PRIVMSG "
                     f"#munzzyy1 :{text}\r\n".encode())
        await self.w.drain()

    @property
    def said(self):
        return [l.split(" :", 1)[1] for l in self.lines if l.startswith("PRIVMSG")]

    async def stop(self):
        self.srv.close()


async def until(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


class Rig:
    def __init__(self, data, sign_in=True, **fake_kw):
        self.data, self.sign_in = data, sign_in
        self.fake = ModTwitch(pending_polls=0, **fake_kw)
        self.irc = Irc()
        self.logs = []

    async def __aenter__(self):
        await self.fake.start()
        await self.irc.start()
        self.client = TwitchClient(lambda: CLIENT_ID, MemoryStore(), None, self.fake.id_base, self.fake.api_base,
                                   sleep=fast_sleep)
        if self.sign_in:
            await self.client.connect()
            assert self.client.connected, self.client.state
        self.tasks = set()
        self.chat = TwitchChat(lambda: "munzzyy1", self._on_message, host="127.0.0.1", port=self.irc.port, use_tls=False,
                               backoff=(0.05,), auth=self.client.chat_auth)
        self.io = LiveIO(self.client, self.chat, lambda: "munzzyy1")
        self.bot = Bot({"enabled": True, **self.data}, self.io, on_log=self.logs.append)
        self.run_task = asyncio.ensure_future(self.chat.run())
        assert await until(lambda: self.irc.w is not None)
        if self.sign_in:
            assert await until(lambda: self.chat.can_send)
        return self

    def _on_message(self, m):
        t = asyncio.ensure_future(self.bot.handle(m))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def settle(self):
        await asyncio.sleep(0.15)
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def __aexit__(self, *a):
        await self.chat.stop()
        await asyncio.wait_for(self.run_task, 3)
        await self.irc.stop()
        await self.fake.stop()


def run(c):
    asyncio.run(asyncio.wait_for(c, 20))


MOD = {"enabled": True, "links": {"action": "timeout", "seconds": 45, "allow": ["twitch.tv"]}}


class LiveTests(unittest.TestCase):
    def test_commands_answer_through_real_irc_and_helix(self):
        async def go():
            async with Rig({"commands": [{"name": "hi", "response": "hey {user}", "cooldown": 0, "user_cooldown": 0}]}) as r:
                await r.irc.push("!hi")
                await r.irc.push("!lurk", "mod", "badges=moderator/1;")
                await r.irc.push("!title", "someone")
                await r.irc.push("!uptime", "other")
                await r.irc.push("!so Friend", "mod", "badges=moderator/1;")
                assert await until(lambda: len(r.irc.said) == 5), r.irc.said
                self.assertEqual(sorted(r.irc.said), sorted([
                    "hey Viewer", "Mod is lurking. Thanks for hanging out!", "Title: old title",
                    "munzzyy1 is offline right now",
                    "Go check out Friend at https://twitch.tv/friend"]))
                self.assertTrue(r.irc.passwords[0].startswith("PASS oauth:"))
                await r.settle()
                self.assertEqual(len(r.irc.said), 5, "the bot never answers its own echoed lines")
        run(go())

    def test_uptime_from_helix_when_live(self):
        async def go():
            from datetime import datetime, timedelta, timezone
            started = (datetime.now(timezone.utc) - timedelta(minutes=95)).strftime("%Y-%m-%dT%H:%M:%SZ")
            async with Rig({}) as r:
                r.fake.stream = [{"started_at": started}]
                await r.irc.push("!uptime")
                assert await until(lambda: len(r.irc.said) == 1)
                self.assertEqual(r.irc.said, ["munzzyy1 has been live for 1h 35m"])
        run(go())

    def test_link_filter_times_out_over_helix_with_the_right_ids(self):
        async def go():
            async with Rig({"mod": MOD}) as r:
                await r.irc.push("look at evil.com now", "troll")
                assert await until(lambda: r.fake.mod_calls)
                kind, q, body = r.fake.mod_calls[0]
                self.assertEqual(kind, "ban")
                self.assertEqual(q, {"broadcaster_id": "4242", "moderator_id": "4242"})
                self.assertEqual(body["data"]["user_id"], "u-troll")
                self.assertEqual(body["data"]["duration"], 45)
                await r.irc.push("clips.twitch.tv/x is fine", "ok")
                await r.irc.push("evil.com", "boss", "badges=broadcaster/1;")
                await r.settle()
                self.assertEqual(len(r.fake.mod_calls), 1)
        run(go())

    def test_delete_and_warn_hit_the_right_endpoints(self):
        async def go():
            mod = {"enabled": True, "caps": {"action": "delete"}, "emotes": {"action": "warn", "max": 1}}
            async with Rig({"mod": mod}) as r:
                await r.irc.push("THIS IS WAY TOO LOUD FRIENDS", "loud")
                await r.irc.push("a b", "spam", "emotes=25:0-1,3-4/1902:6-7;")
                assert await until(lambda: len(r.fake.mod_calls) == 2)
                by_kind = {k: (q, b) for k, q, b in r.fake.mod_calls}
                self.assertEqual(by_kind["delete"][0]["message_id"], "id-loud")
                self.assertEqual(by_kind["warn"][1]["data"]["user_id"], "u-spam")
        run(go())

    def test_mod_in_someone_elses_channel_looks_up_the_broadcaster(self):
        async def go():
            async with Rig({"mod": MOD}) as r:
                r.io._channel = lambda: "otherstreamer"
                r.chat._channel_now = "otherstreamer"
                await r.irc.push("evil.com", "troll")
                assert await until(lambda: r.fake.mod_calls)
                self.assertEqual(r.fake.mod_calls[0][1]["broadcaster_id"], "999")
                self.assertEqual(r.fake.mod_calls[0][1]["moderator_id"], "4242")
        run(go())

    def test_helix_refusal_is_logged_and_the_bot_survives(self):
        async def go():
            async with Rig({"mod": MOD}) as r:
                r.fake.deny_mod = "The user in moderator_id is not a moderator"
                await r.irc.push("evil.com", "troll")
                assert await until(lambda: any(e["kind"] == "error" for e in r.logs))
                r.fake.deny_mod = None
                await r.irc.push("evil.com", "troll2")
                assert await until(lambda: len(r.fake.mod_calls) == 2)
        run(go())

    def test_not_signed_in_nothing_is_sent_and_nothing_hits_twitch(self):
        async def go():
            data = {"mod": MOD, "commands": [{"name": "hi", "response": "hey", "cooldown": 0, "user_cooldown": 0}]}
            async with Rig(data, sign_in=False) as r:
                self.assertEqual(r.irc.passwords, ["PASS SCHMOOZE"])
                await r.irc.push("!hi")
                await r.irc.push("!lurk", "mod", "badges=moderator/1;")
                await r.irc.push("evil.com", "troll")
                await r.settle()
                self.assertEqual(r.irc.said, [])
                self.assertEqual((r.fake.mod_calls, r.fake.requests), ([], []))
                notes = [e["text"] for e in r.logs if e["kind"] == "note"]
                self.assertTrue(any("skipped" in n and "sign in" in n for n in notes), notes)
                self.assertTrue(any("can't answer" in n for n in notes), notes)
        run(go())

    def test_old_sign_in_without_moderator_scopes_does_not_moderate(self):
        async def go():
            async with Rig({"mod": MOD}, scopes=["channel:manage:broadcast", "chat:read", "chat:edit"]) as r:
                await r.irc.push("evil.com", "troll")
                await r.settle()
                self.assertEqual(r.fake.mod_calls, [])
                notes = [e["text"] for e in r.logs if e["kind"] == "note"]
                self.assertTrue(any("moderator permissions" in n for n in notes), notes)
        run(go())


if __name__ == "__main__":
    unittest.main()
