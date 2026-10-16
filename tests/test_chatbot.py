"""Bot engine against a fake io: commands, cooldowns, permissions, timers, moderation, settings file."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lark import chatmod
from lark.chatbot import DEFAULTS, Bot, BotStore, clean_out, fmt_duration, level_of, render, sanitize
from lark.twitch_api import TwitchError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeIO:
    def __init__(self):
        self.said, self.deleted, self.timeouts, self.warned, self.permits = [], [], [], [], []
        self.act = (True, "")
        self.moderate = (True, "")
        self.me = "lark_bot"
        self.chan = "munzzyy1"
        self.uptime = 4980
        self.info = {"title": "Chill stream", "game_name": "Rocket League"}
        self.replay = (True, "")
        self.fail = None
        self.sends = True

    def login(self):
        return self.me

    def channel(self):
        return self.chan

    def can_act(self):
        return self.act

    def can_moderate(self):
        return self.moderate

    async def say(self, text):
        self.said.append(text)
        return self.sends

    async def delete(self, mid):
        if self.fail:
            raise TwitchError(self.fail)
        self.deleted.append(mid)

    async def timeout(self, uid, secs, reason):
        if self.fail:
            raise TwitchError(self.fail)
        self.timeouts.append((uid, secs, reason))

    async def warn(self, uid, reason):
        self.warned.append((uid, reason))

    async def uptime_seconds(self):
        return self.uptime

    async def channel_info(self):
        return self.info

    async def save_replay(self):
        return self.replay


def msg(text, user="viewer", badges=(), id_="m1", emotes=0, uid=None):
    return {"id": id_, "user": user.title(), "login": user, "user_id": uid or f"id-{user}", "text": text,
            "badges": list(badges), "emote_count": emotes, "ts": 0, "color": "", "action": False}


def run(coro):
    return asyncio.run(coro)


def make(data=None, **kw):
    io, clock, logs, changes = FakeIO(), Clock(), [], []
    base = {"enabled": True, **(data or {})}
    bot = Bot(base, io, clock=clock, on_log=logs.append, on_change=changes.append)
    return bot, io, clock, logs, changes


CMD = {"name": "discord", "response": "Hi {user}, ask {touser}, used {count}x", "permission": "everyone",
       "cooldown": 10, "user_cooldown": 30, "aliases": ["dc"]}


class SanitizeTests(unittest.TestCase):
    def test_garbage_gives_defaults(self):
        for junk in (None, [], "x", 5, {"commands": "no", "timers": 3, "mod": [], "builtins": 1}):
            d = sanitize(junk)
            self.assertEqual(d, sanitize({}))
            self.assertFalse(d["enabled"])
            self.assertFalse(d["mod"]["enabled"])

    def test_nothing_personal_is_prefilled(self):
        d = DEFAULTS["mod"]["terms"]
        self.assertEqual((d["blocked"], d["personal"]), ([], []))

    def test_commands_are_cleaned_and_collisions_dropped(self):
        d = sanitize({"commands": [
            {"name": "!Hello", "response": "hi", "aliases": ["!hey", "uptime", "hey", "bad name", "x" * 40]},
            {"name": "uptime", "response": "taken by a built-in"},
            {"name": "hello", "response": "duplicate"},
            {"name": "ok", "response": ""},
            {"name": "../x", "response": "bad"},
            {"name": "n", "response": "r", "cooldown": -5, "user_cooldown": 1e12, "permission": "root"},
            "nope",
        ]})
        names = [c["name"] for c in d["commands"]]
        self.assertEqual(names, ["hello", "n"])
        self.assertEqual(d["commands"][0]["aliases"], ["hey"])
        self.assertEqual(d["commands"][1]["cooldown"], 0)
        self.assertEqual(d["commands"][1]["user_cooldown"], 86400)
        self.assertEqual(d["commands"][1]["permission"], "everyone")

    def test_numbers_reject_bool_nan_inf(self):
        d = sanitize({"mod": {"caps": {"percent": float("nan"), "min_letters": True, "seconds": float("inf")}}})
        self.assertEqual(d["mod"]["caps"]["percent"], 70)
        self.assertEqual(d["mod"]["caps"]["min_letters"], 12)
        self.assertEqual(d["mod"]["caps"]["seconds"], 10)

    def test_timers_and_prefix(self):
        d = sanitize({"prefix": "/", "timers": [{"name": "a", "message": "m", "interval": 0}, {"name": "a", "message": "dup"},
                                                {"name": "", "message": "x"}]})
        self.assertEqual(d["prefix"], "!")
        self.assertEqual(len(d["timers"]), 1)
        self.assertEqual(d["timers"][0]["interval"], 1)
        self.assertEqual(sanitize({"prefix": "?"})["prefix"], "?")

    def test_terms_lists_are_stripped_and_deduped(self):
        d = sanitize({"mod": {"links": {"allow": ["https://www.Example.com/x", "example.com", "  "]},
                              "terms": {"blocked": ["Foo", "Foo", 3, ""], "personal": ["  a   b "]}}})
        self.assertEqual(d["mod"]["links"]["allow"], ["example.com"])
        self.assertEqual(d["mod"]["terms"]["blocked"], ["Foo"])
        self.assertEqual(d["mod"]["terms"]["personal"], ["a b"])


class StoreTests(unittest.TestCase):
    def test_roundtrip_private_file(self):
        with tempfile.TemporaryDirectory() as t:
            st = BotStore(Path(t) / "sub" / "bot.json")
            self.assertEqual(st.load(), sanitize({}))
            d = sanitize({"enabled": True, "commands": [CMD]})
            st.save(d)
            self.assertEqual(st.load(), d)
            self.assertEqual(stat.S_IMODE(os.stat(st.path).st_mode), 0o600)
            self.assertEqual(os.listdir(st.path.parent), ["bot.json"])

    def test_corrupt_file_moved_aside_and_defaults_used(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bot.json"
            p.write_text("{not json")
            with self.assertLogs("lark.bot", "ERROR"):
                d = BotStore(p).load()
            self.assertEqual(d, sanitize({}))
            self.assertTrue((Path(t) / "bot.json.bad").exists())

    def test_hostile_file_values_are_validated(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bot.json"
            p.write_text(json.dumps({"enabled": "yes", "mod": {"enabled": 1}, "commands": [{"name": "a b", "response": "x"}]}))
            d = BotStore(p).load()
            self.assertFalse(d["enabled"])
            self.assertFalse(d["mod"]["enabled"])
            self.assertEqual(d["commands"], [])


class HelperTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(level_of(msg("x")), 0)
        self.assertEqual(level_of(msg("x", badges=["subscriber"])), 1)
        self.assertEqual(level_of(msg("x", badges=["moderator", "subscriber"])), 2)
        self.assertEqual(level_of(msg("x", badges=["broadcaster"])), 3)
        self.assertEqual(level_of(msg("x", user="munzzyy1"), "MunzzyY1"), 3)

    def test_render_is_single_pass(self):
        self.assertEqual(render("{user} {touser} {count} {nope}", user="{touser}", touser="b", count=3), "{touser} b 3 {nope}")

    def test_clean_out(self):
        self.assertEqual(clean_out("/ban someone"), "")
        self.assertEqual(clean_out("/me waves"), "/me waves")
        self.assertEqual(clean_out("a\r\nPRIVMSG #x :y"), "a PRIVMSG #x :y")
        self.assertEqual(len(clean_out("x" * 900)), 450)

    def test_duration(self):
        self.assertEqual([fmt_duration(s) for s in (-3, 45, 125, 4980)], ["0s", "45s", "2m", "1h 23m"])


class CommandTests(unittest.TestCase):
    def test_custom_command_placeholders_count_and_save(self):
        bot, io, clock, logs, changes = make({"commands": [CMD]})
        run(bot.handle(msg("!discord @Bob_1 extra")))
        self.assertEqual(io.said, ["Hi Viewer, ask Bob_1, used 1x"])
        clock.t += 100
        run(bot.handle(msg("!dc")))
        self.assertEqual(io.said[-1], "Hi Viewer, ask Viewer, used 2x")
        self.assertEqual(bot.data["counts"]["discord"], 2)
        self.assertEqual(len(changes), 2)

    def test_hostile_argument_cannot_inject(self):
        bot, io, *_ = make({"commands": [CMD]})
        run(bot.handle(msg("!discord /ban_everyone\r\nPRIVMSG #x :hi")))
        self.assertEqual(io.said, ["Hi Viewer, ask Viewer, used 1x"])
        bot, io, *_ = make({"commands": [{**CMD, "response": "/ban {touser}"}]})
        run(bot.handle(msg("!discord x")))
        self.assertEqual(io.said, [])

    def test_global_and_user_cooldowns(self):
        bot, io, clock, *_ = make({"commands": [CMD]})
        run(bot.handle(msg("!discord", "a")))
        run(bot.handle(msg("!discord", "b")))
        self.assertEqual(len(io.said), 1, "global cooldown holds back everyone")
        clock.t += 11
        run(bot.handle(msg("!discord", "a")))
        self.assertEqual(len(io.said), 1, "a is still inside their own 30s")
        run(bot.handle(msg("!discord", "b")))
        self.assertEqual(len(io.said), 2)
        clock.t += 25
        run(bot.handle(msg("!discord", "a")))
        self.assertEqual(len(io.said), 3)

    def test_permissions(self):
        cmds = [{**CMD, "name": n, "permission": p, "cooldown": 0, "user_cooldown": 0, "aliases": []}
                for n, p in (("s", "subscriber"), ("m", "mod"), ("b", "broadcaster"))]
        bot, io, *_ = make({"commands": cmds})
        got = {}
        for who, badges in (("v", []), ("sub", ["subscriber"]), ("mod", ["moderator"]), ("bc", ["broadcaster"])):
            n = len(io.said)
            for c in ("s", "m", "b"):
                run(bot.handle(msg(f"!{c}", who, badges)))
            got[who] = len(io.said) - n
        self.assertEqual(got, {"v": 0, "sub": 1, "mod": 2, "bc": 3})

    def test_disabled_command_and_disabled_bot(self):
        bot, io, *_ = make({"commands": [{**CMD, "enabled": False}]})
        run(bot.handle(msg("!discord")))
        self.assertEqual(io.said, [])
        bot, io, *_ = make({"enabled": False, "commands": [CMD]})
        run(bot.handle(msg("!discord")))
        self.assertEqual(io.said, [])

    def test_unknown_and_bare_prefix_and_own_messages_ignored(self):
        bot, io, *_ = make({"commands": [CMD]})
        for t in ("!nope", "!", "! discord", "discord", ""):
            run(bot.handle(msg(t)))
        run(bot.handle(msg("!discord", "lark_bot")))
        self.assertEqual(io.said, [])

    def test_no_token_means_silence_and_one_note(self):
        bot, io, clock, logs, _ = make({"commands": [CMD]})
        io.act = (False, "Twitch isn't connected, sign in under Settings")
        for _ in range(3):
            run(bot.handle(msg("!discord")))
        self.assertEqual(io.said, [])
        notes = [e["text"] for e in logs if e["kind"] == "note"]
        self.assertEqual(len(notes), 1)
        self.assertIn("sign in", notes[0])
        self.assertEqual(bot.data["counts"], {})

    def test_say_failure_is_logged_not_raised(self):
        bot, io, clock, logs, _ = make({"commands": [CMD]})
        io.sends = False
        run(bot.handle(msg("!discord")))
        self.assertTrue(any("Not sent" in e["text"] for e in logs))

    def test_reply_rate_limit(self):
        bot, io, clock, logs, _ = make({"commands": [{**CMD, "cooldown": 0, "user_cooldown": 0}]})

        async def go():
            for i in range(25):
                await bot.handle(msg("!discord", f"u{i}"))
        run(go())
        self.assertEqual(len(io.said), 15)
        clock.t += 31
        run(bot.handle(msg("!discord", "late")))
        self.assertEqual(len(io.said), 16)


class BuiltinTests(unittest.TestCase):
    def test_uptime_title_game(self):
        bot, io, clock, *_ = make()
        for c in ("!uptime", "!title", "!game"):
            clock.t += 100
            run(bot.handle(msg(c)))
        self.assertEqual(io.said, ["munzzyy1 has been live for 1h 23m", "Title: Chill stream", "Game: Rocket League"])
        io.uptime = None
        clock.t += 100
        run(bot.handle(msg("!uptime")))
        self.assertEqual(io.said[-1], "munzzyy1 is offline right now")

    def test_helix_failure_in_builtin_is_logged(self):
        bot, io, clock, logs, _ = make()

        async def boom():
            raise TwitchError("Twitch answered 500")
        io.channel_info = boom
        run(bot.handle(msg("!title")))
        self.assertEqual(io.said, [])
        self.assertTrue(any(e["kind"] == "error" and "500" in e["text"] for e in logs))

    def test_commands_lists_only_what_the_caller_may_use(self):
        bot, io, clock, *_ = make({"commands": [CMD, {**CMD, "name": "secret", "permission": "mod", "aliases": []}]})
        run(bot.handle(msg("!commands")))
        self.assertEqual(io.said[0], "Commands: !uptime, !title, !game, !commands, !lurk, !discord")
        clock.t += 100
        run(bot.handle(msg("!commands", "m", ["moderator"])))
        self.assertIn("!so, !clip", io.said[1])
        self.assertIn("!secret", io.said[1])
        self.assertNotIn("!permit", io.said[1])

    def test_lurk(self):
        bot, io, *_ = make()
        run(bot.handle(msg("!lurk")))
        self.assertEqual(io.said, ["Viewer is lurking. Thanks for hanging out!"])

    def test_shoutout_is_mod_only_and_links_the_channel(self):
        bot, io, clock, *_ = make()
        run(bot.handle(msg("!so Cool_Streamer")))
        self.assertEqual(io.said, [])
        run(bot.handle(msg("!so @Cool_Streamer", "m", ["moderator"])))
        self.assertEqual(io.said, ["Go check out Cool_Streamer at https://twitch.tv/cool_streamer"])
        clock.t += 100
        run(bot.handle(msg("!so", "m", ["moderator"])))
        self.assertEqual(io.said[-1], "Usage: !so <user>")
        clock.t += 100
        run(bot.handle(msg("!so https://evil.example/x", "m", ["moderator"])))
        self.assertEqual(io.said[-1], "Usage: !so <user>")

    def test_clip_saves_replay_buffer(self):
        bot, io, clock, logs, _ = make()
        run(bot.handle(msg("!clip", "m", ["moderator"])))
        self.assertEqual(io.said, ["Clip saved"])
        clock.t += 100
        io.replay = (False, "Replay buffer is not active")
        run(bot.handle(msg("!clip", "m", ["moderator"])))
        self.assertIn("replay buffer isn't running", io.said[-1])
        clock.t += 100
        run(bot.handle(msg("!clip", "viewer")))
        self.assertEqual(len(io.said), 2)

    def test_builtin_can_be_turned_off_and_retuned(self):
        bot, io, clock, *_ = make({"builtins": {"lurk": {"enabled": False}, "title": {"permission": "mod"}}})
        run(bot.handle(msg("!lurk")))
        run(bot.handle(msg("!title")))
        self.assertEqual(io.said, [])
        run(bot.handle(msg("!title", "m", ["moderator"])))
        self.assertEqual(len(io.said), 1)


class TimerTests(unittest.TestCase):
    T = {"name": "social", "message": "Follow on {channel}", "interval": 60, "min_lines": 3}

    def test_posts_after_interval_and_enough_chat(self):
        bot, io, clock, *_ = make({"timers": [self.T]})
        run(bot.tick())
        clock.t += 61
        run(bot.tick())
        self.assertEqual(io.said, [], "no chat lines yet")
        for i in range(3):
            run(bot.handle(msg("hello", f"u{i}")))
        run(bot.tick())
        self.assertEqual(io.said, ["Follow on munzzyy1"])
        run(bot.tick())
        clock.t += 61
        run(bot.tick())
        self.assertEqual(len(io.said), 1, "needs new lines after a post")
        for i in range(3):
            run(bot.handle(msg("more", f"u{i}")))
        run(bot.tick())
        self.assertEqual(len(io.said), 2)

    def test_too_soon_never_posts(self):
        bot, io, clock, *_ = make({"timers": [self.T]})
        run(bot.tick())
        for i in range(10):
            run(bot.handle(msg("hello", f"u{i}")))
        clock.t += 30
        run(bot.tick())
        self.assertEqual(io.said, [])

    def test_own_lines_do_not_count_and_disabled_timer_is_quiet(self):
        bot, io, clock, *_ = make({"timers": [self.T, {**self.T, "name": "off", "enabled": False, "min_lines": 0}]})
        run(bot.tick())
        for _ in range(5):
            run(bot.handle(msg("hello", "lark_bot")))
        clock.t += 61
        run(bot.tick())
        self.assertEqual(io.said, [])
        run(bot.handle(msg("a", "x")))
        run(bot.handle(msg("b", "x")))
        run(bot.handle(msg("c", "x")))
        run(bot.tick())
        self.assertEqual(io.said, ["Follow on munzzyy1"])

    def test_no_token_no_post(self):
        bot, io, clock, logs, _ = make({"timers": [{**self.T, "min_lines": 0}]})
        io.act = (False, "Twitch isn't connected, sign in under Settings")
        run(bot.tick())
        clock.t += 100
        run(bot.tick())
        run(bot.tick())
        self.assertEqual(io.said, [])
        self.assertEqual(len([e for e in logs if e["kind"] == "note"]), 1)

    def test_bot_off_means_no_timers(self):
        bot, io, clock, *_ = make({"enabled": False, "timers": [{**self.T, "min_lines": 0}]})
        clock.t += 1000
        run(bot.tick())
        self.assertEqual(io.said, [])


def modcfg(**over):
    mod = {"enabled": True, "links": {"action": "timeout", "seconds": 30, "allow": ["twitch.tv"]}}
    for k, v in over.items():
        mod[k] = v
    return {"mod": mod}


class RuleTests(unittest.TestCase):
    def check(self, text, rule_cfg=None, **kw):
        m = sanitize({"mod": {"enabled": True, **(rule_cfg or {})}})["mod"]
        return chatmod.check(msg(text, **kw), m)

    def test_links(self):
        cfg = {"links": {"allow": ["twitch.tv"]}}
        for bad in ("see https://evil.com/x", "go to WWW.evil.org", "visit evil.gg now", "evil.com/path",
                    "ｅｖｉｌ.ｃｏｍ", "clips.twitch.tv.evil.com"):
            self.assertEqual(self.check(bad, cfg), "links", bad)
        for fine in ("hello world", "e.g. this", "Mr.Smith said hi", "clips.twitch.tv/abc", "https://twitch.tv/x",
                     "wait... what", "3.5 hours", "file.txt"):
            self.assertIsNone(self.check(fine, cfg), fine)

    def test_caps_threshold(self):
        self.assertEqual(self.check("THIS IS VERY LOUD INDEED"), "caps")
        self.assertIsNone(self.check("LOL"))
        self.assertIsNone(self.check("This Is Fine, honest"))
        self.assertIsNone(self.check("1234567890123456 !!!!!!!!"))

    def test_emotes(self):
        self.assertEqual(self.check("Kappa " * 3, emotes=16), "emotes")
        self.assertIsNone(self.check("Kappa", emotes=15))

    def test_terms_both_lists_and_evasion(self):
        cfg = {"terms": {"blocked": ["badword"], "personal": ["Jane Doe", "Madison"]}}
        for bad in ("you BADWORD", "bad​word", "Jane  Doe is here", "from m.a.d.i.s.o.n", "madison!"):
            self.assertEqual(self.check(bad, cfg), "terms", bad)
        for fine in ("so mad is on fire", "amadisons", "bad words", "janedoes"):
            self.assertIsNone(self.check(fine, cfg), fine)
        self.assertIsNone(self.check("badword", {"terms": {"on": False, "blocked": ["badword"]}}))

    def test_contacts(self):
        for bad in ("mail me a.b@x.com", "call 555-123-4567", "+1 (555) 123 4567", "5551234567"):
            self.assertEqual(self.check(bad), "contacts", bad)
        for fine in ("I have 3 cats", "score 120-98 tonight", "year 2026"):
            self.assertIsNone(self.check(fine), fine)


class ModerationTests(unittest.TestCase):
    def test_link_gets_timeout_and_mod_is_exempt(self):
        bot, io, clock, logs, _ = make(modcfg())
        run(bot.handle(msg("check evil.com", "troll", id_="m9", uid="77")))
        self.assertEqual(io.timeouts, [("77", 30, chatmod.REASONS["links"])])
        run(bot.handle(msg("check evil.com", "m", ["moderator"])))
        run(bot.handle(msg("check evil.com", "bc", ["broadcaster"])))
        run(bot.handle(msg("check twitch.tv/x", "ok")))
        self.assertEqual(len(io.timeouts), 1)
        self.assertTrue(any(e["kind"] == "mod" and "Timed out Troll" in e["text"] for e in logs))

    def test_permit_lasts_sixty_seconds_then_expires(self):
        bot, io, clock, *_ = make(modcfg())
        run(bot.handle(msg("!permit Friend", "m", ["moderator"])))
        self.assertEqual(io.said, ["Friend, you can post links for the next 60 seconds"])
        run(bot.handle(msg("evil.com", "friend")))
        self.assertEqual(io.timeouts, [])
        clock.t += 59
        run(bot.handle(msg("evil.com", "friend")))
        self.assertEqual(io.timeouts, [])
        clock.t += 2
        run(bot.handle(msg("evil.com", "friend")))
        self.assertEqual(len(io.timeouts), 1)

    def test_permit_is_for_mods_only(self):
        bot, io, *_ = make(modcfg())
        run(bot.handle(msg("!permit troll", "troll")))
        run(bot.handle(msg("evil.com", "troll")))
        self.assertEqual(len(io.timeouts), 1)

    def test_delete_and_warn_actions(self):
        cfg = {"mod": {"enabled": True, "caps": {"action": "delete"}, "emotes": {"action": "warn", "max": 2}}}
        bot, io, *_ = make(cfg)
        run(bot.handle(msg("THIS IS VERY LOUD INDEED", "a", id_="del-me")))
        self.assertEqual(io.deleted, ["del-me"])
        run(bot.handle(msg("Kappa Kappa Kappa", "b", emotes=3, uid="9")))
        self.assertEqual(io.warned, [("9", chatmod.REASONS["emotes"])])

    def test_violating_message_does_not_also_run_a_command(self):
        bot, io, *_ = make({**modcfg(), "commands": [CMD]})
        run(bot.handle(msg("!discord evil.com", "troll")))
        self.assertEqual(io.said, [])

    def test_exempt_subs_option(self):
        c = modcfg()
        bot, io, *_ = make(c)
        run(bot.handle(msg("evil.com", "s", ["subscriber"])))
        self.assertEqual(len(io.timeouts), 1)
        c["mod"]["exempt_subs"] = True
        bot, io, *_ = make(c)
        run(bot.handle(msg("evil.com", "s", ["subscriber"])))
        self.assertEqual(io.timeouts, [])

    def test_off_by_default_even_with_bot_on(self):
        bot, io, *_ = make()
        run(bot.handle(msg("evil.com THIS IS VERY LOUD INDEED")))
        self.assertEqual((io.timeouts, io.deleted, io.warned), ([], [], []))

    def test_no_token_or_scope_is_a_noop_and_says_so_once(self):
        bot, io, clock, logs, _ = make(modcfg())
        io.moderate = (False, "Twitch isn't connected, sign in under Settings")
        for _ in range(3):
            run(bot.handle(msg("evil.com", "troll")))
        self.assertEqual((io.timeouts, io.deleted, io.warned), ([], [], []))
        notes = [e["text"] for e in logs if e["kind"] == "note"]
        self.assertEqual(len(notes), 1)
        self.assertIn("skipped", notes[0])

    def test_helix_failure_is_logged_and_the_bot_keeps_going(self):
        bot, io, clock, logs, _ = make(modcfg())
        io.fail = "You are not a moderator in this channel"
        run(bot.handle(msg("evil.com", "troll")))
        self.assertTrue(any(e["kind"] == "error" and "not a moderator" in e["text"] for e in logs))
        io.fail = None
        run(bot.handle(msg("evil.com", "troll2")))
        self.assertEqual(len(io.timeouts), 1)

    def test_missing_ids_are_reported_not_crashed_on(self):
        bot, io, clock, logs, _ = make(modcfg())
        m = msg("evil.com", "troll")
        m["user_id"] = ""
        run(bot.handle(m))
        self.assertEqual(io.timeouts, [])
        self.assertTrue(any(e["kind"] == "error" for e in logs))

    def test_apply_keeps_counts_for_surviving_commands(self):
        bot, io, clock, *_ = make({"commands": [CMD]})
        run(bot.handle(msg("!discord")))
        bot.apply({"enabled": True, "commands": [{**CMD, "response": "new"}]})
        self.assertEqual(bot.data["counts"], {"discord": 1})
        bot.apply({"enabled": True, "commands": []})
        self.assertEqual(bot.data["counts"], {})


if __name__ == "__main__":
    unittest.main()
