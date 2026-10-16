import asyncio
import unittest

from lark.twitch_irc import TwitchChat, parse_line, to_message, unescape_tag

PRIV = ("@badge-info=;badges=moderator/1,subscriber/12;color=#1E90FF;display-name=Some\\sOne;id=abc-123;"
        "tmi-sent-ts=1 :someone!someone@someone.tmi.twitch.tv PRIVMSG #munzzyy1 :hello there <b>x</b>\r\n")


async def close_server(srv, writers):
    # Close every accepted connection before the loop goes away, or the transport is finalised after the
    # server is gone and prints a TypeError from asyncio's __del__.
    for w in writers:
        w.close()
    srv.close()
    await srv.wait_closed()


class ParseTests(unittest.TestCase):
    def test_privmsg(self):
        m = to_message(parse_line(PRIV))
        self.assertEqual(m["user"], "Some One")
        self.assertEqual(m["color"], "#1E90FF")
        self.assertEqual(m["text"], "hello there <b>x</b>")
        self.assertEqual(m["badges"], ["moderator", "subscriber"])
        self.assertEqual(m["id"], "abc-123")

    def test_bad_color_dropped_and_nick_fallback(self):
        m = to_message(parse_line("@color=red;display-name= :bob!b@b PRIVMSG #c :hi"))
        self.assertEqual(m["color"], "")
        self.assertEqual(m["user"], "bob")

    def test_action(self):
        m = to_message(parse_line(":a!a@a PRIVMSG #c :\x01ACTION waves\x01"))
        self.assertTrue(m["action"])
        self.assertEqual(m["text"], "waves")

    def test_text_with_colon_and_long_text_capped(self):
        m = to_message(parse_line(":a!a@a PRIVMSG #c :a : b :c" + "x" * 900))
        self.assertTrue(m["text"].startswith("a : b :c"))
        self.assertEqual(len(m["text"]), 500)

    def test_clear_events(self):
        self.assertEqual(to_message(parse_line("@target-msg-id=zzz :tmi.twitch.tv CLEARMSG #c :bad")),
                         {"delete": {"id": "zzz"}})
        self.assertEqual(to_message(parse_line(":tmi.twitch.tv CLEARCHAT #c :troll")), {"delete": {"user": "troll"}})

    def test_junk(self):
        for l in ("", "\r\n", "   ", "@", ":"):
            self.assertIsNone(to_message(parse_line(l)))
        self.assertIsNone(to_message(parse_line("PING :tmi.twitch.tv")))
        self.assertEqual(parse_line("PING :tmi.twitch.tv")[3], ["tmi.twitch.tv"])

    def test_unescape(self):
        self.assertEqual(unescape_tag("a\\sb\\:c\\\\d\\"), "a b;c\\d\\")


class ClientTests(unittest.TestCase):
    def test_session_against_local_server(self):
        async def go():
            got, states, seen, writers = [], [], [], []

            async def handle(r, w):
                writers.append(w)
                try:
                    for _ in range(4):
                        seen.append((await r.readline()).decode().strip())
                    w.write(b":x JOIN #munzzyy1\r\nPING :tmi.twitch.tv\r\n" + PRIV.encode())
                    await w.drain()
                    seen.append((await r.readline()).decode().strip())
                except ConnectionError:
                    pass  # the client hung up on us while stopping
                finally:
                    w.close()

            srv = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            chat = TwitchChat(lambda: "#Munzzyy1", got.append, lambda s, d: states.append(s),
                              host="127.0.0.1", port=port, use_tls=False, backoff=(0.05,))
            t = asyncio.ensure_future(chat.run())
            for _ in range(100):
                if got and states.count("connecting") >= 2:
                    break
                await asyncio.sleep(0.05)
            await chat.stop()
            await asyncio.wait_for(t, 3)
            await close_server(srv, writers)
            self.assertEqual(got[0]["text"], "hello there <b>x</b>")
            self.assertIn("connected", states)
            self.assertTrue(seen[1] == "PASS SCHMOOZE" and seen[2].startswith("NICK justinfan"))
            self.assertEqual(seen[3], "JOIN #munzzyy1")
            self.assertEqual(seen[4], "PONG :tmi.twitch.tv")
            self.assertGreaterEqual(states.count("connecting"), 2)
        asyncio.run(asyncio.wait_for(go(), 15))


class AuthedTests(unittest.TestCase):
    def test_login_send_and_fallback_when_rejected(self):
        async def go():
            seen, got, states, writers = [], [], [], []
            mode = {"reject": False}

            async def handle(r, w):
                writers.append(w)
                try:
                    lines = [(await r.readline()).decode().strip() for _ in range(4)]
                    seen.append(lines)
                    if mode["reject"] and lines[1] != "PASS SCHMOOZE":
                        w.write(b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n")
                    else:
                        w.write(b":x JOIN #munzzyy1\r\n")
                    await w.drain()
                    if not mode["reject"]:
                        seen.append((await r.readline()).decode().strip())
                        await asyncio.sleep(0.3)
                except ConnectionError:
                    pass  # the client hung up on us while stopping
                finally:
                    w.close()

            srv = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]

            async def auth():
                return ("munzzyy1", "oauth:tok123")
            chat = TwitchChat(lambda: "munzzyy1", got.append, lambda s, d: states.append(s), host="127.0.0.1",
                              port=port, use_tls=False, backoff=(0.05,), auth=auth)
            t = asyncio.ensure_future(chat.run())
            for _ in range(100):
                if chat.can_send:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(chat.can_send)
            self.assertTrue(await chat.send("hello\r\nPRIVMSG #x :injected"))
            for _ in range(50):
                if len(seen) > 1:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(seen[0][1:3], ["PASS oauth:tok123", "NICK munzzyy1"])
            self.assertEqual(seen[1], "PRIVMSG #munzzyy1 :hello PRIVMSG #x :injected")
            self.assertEqual(got[-1]["user"], "munzzyy1")
            # a rejected token falls back to anonymous on the next connect
            mode["reject"] = True
            with self.assertLogs("lark.irc", "WARNING"):
                await asyncio.sleep(0.6)
                for _ in range(100):
                    if len(seen) >= 4 and seen[-1][2].startswith("NICK justinfan"):
                        break
                    await asyncio.sleep(0.05)
            self.assertTrue(seen[-1][2].startswith("NICK justinfan"), seen[-1])
            self.assertFalse(await chat.send("x"))
            await chat.stop()
            await asyncio.wait_for(t, 3)
            await close_server(srv, writers)
        asyncio.run(asyncio.wait_for(go(), 15))


if __name__ == "__main__":
    unittest.main()
