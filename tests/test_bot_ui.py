"""The Bot page and its runtime inside the real app: edits save to bot.json, chat in over IRC drives replies, mod calls and OBS."""
import asyncio
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lark.app import MainWindow, VIEWS, build_context
from lark.chatbot import sanitize
from lark.config import Config
from lark.keyring import MemoryStore
from tests.fake_obs import FakeObs
from tests.fake_twitch import CLIENT_ID
from tests.test_chatbot_live import Irc, ModTwitch
from tests.test_ui_smoke import pump


async def instant(_):
    await asyncio.sleep(0.01)


class BotUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.old_dir = os.environ.get("LARK_CONFIG_DIR")
        cls.dir = Path(tempfile.mkdtemp())
        os.environ["LARK_CONFIG_DIR"] = str(cls.dir)
        cls.loop = asyncio.new_event_loop()
        cls.t = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.t.start()
        cls.obs, cls.tw, cls.irc = FakeObs(password="pw"), ModTwitch(pending_polls=0), Irc()
        obs_port = cls.call(cls.obs.start())
        cls.call(cls.tw.start())
        cls.call(cls.irc.start())
        cfg = Config(cls.dir / "config.json")
        for k, v in (("obs_port", obs_port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", "munzzyy1"), ("twitch_client_id", CLIENT_ID),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 7)):
            cfg.set(k, v)
        cls.ctx = build_context(cfg, services_kw=dict(
            twitch_kw=dict(store=MemoryStore(), id_base=cls.tw.id_base, api_base=cls.tw.api_base, sleep=instant),
            irc_kw=dict(host="127.0.0.1", port=cls.irc.port, use_tls=False, backoff=(0.1,))))
        cls.win = MainWindow(cls.ctx, "bot")
        cls.win.show()
        cls.page = cls.win.pages["bot"]
        cls.ctx.bridge.start()
        cls.ctx.services.start()
        assert pump(cls.app, lambda: cls.ctx.store.connected and cls.ctx.services.port)

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.ctx.services.stop()
        cls.ctx.bridge.stop()
        cls.call(cls.obs.stop())
        cls.call(cls.tw.stop())
        cls.call(cls.irc.stop())
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.t.join(5)
        cls.loop.close()
        if cls.old_dir is None:
            os.environ.pop("LARK_CONFIG_DIR", None)
        else:
            os.environ["LARK_CONFIG_DIR"] = cls.old_dir

    @classmethod
    def call(cls, coro):
        return asyncio.run_coroutine_threadsafe(coro, cls.loop).result(10)

    def push(self, *a, **kw):
        self.call(self.irc.push(*a, **kw))

    def said(self):
        return self.irc.said

    def test_1_page_is_in_the_rail_after_chat_and_starts_off_and_empty(self):
        self.assertGreater(VIEWS.index("bot"), VIEWS.index("chat"))
        self.assertIn("bot", self.win.buttons)
        p = self.page
        self.assertFalse(p.enabled.isChecked())
        self.assertFalse(p.mod_on.isChecked())
        self.assertEqual((p.blocked.toPlainText(), p.personal.toPlainText()), ("", ""))
        self.assertEqual(p.builtins.rowCount(), 8)
        self.assertEqual(p.collect()["commands"], [])

    def test_2_edits_autosave_to_a_private_bot_json(self):
        p = self.page
        p._add_command({"name": "hi", "response": "hey {user}, count {count}", "permission": "everyone", "cooldown": 0,
                        "user_cooldown": 0, "enabled": True, "aliases": []})
        p._add_timer({"name": "t", "message": "Follow!", "interval": 90, "min_lines": 2, "enabled": True})
        p.enabled.setChecked(True)
        self.assertTrue(pump(self.app, lambda: (self.dir / "bot.json").exists(), 3))
        path = self.dir / "bot.json"
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        saved = json.loads(path.read_text())
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["commands"][0]["name"], "hi")
        self.assertEqual(saved["timers"][0]["interval"], 90)
        self.assertEqual(p.timers.item(0, 3).text(), "1.5")
        self.assertEqual(self.ctx.bot.bot.data["commands"][0]["name"], "hi")

    def test_3_nothing_is_sent_before_twitch_is_connected(self):
        self.assertTrue(pump(self.app, lambda: self.irc.w is not None and self.irc.passwords))
        self.push("!hi")
        pump(self.app, lambda: False, 0.5)
        self.assertEqual(self.said(), [])
        self.assertEqual(self.tw.requests, [])
        self.assertIn("connect Twitch in Settings", self.page.notice.text())
        self.assertFalse(self.page.notice.isHidden())
        self.assertEqual(self.page.pill.text(), "Needs Twitch")

    def test_4_signed_in_commands_reply_over_irc_and_log_shows_it(self):
        self.ctx.services.twitch_connect()
        self.assertTrue(pump(self.app, lambda: self.ctx.services.twitch_state["status"] == "connected"))
        self.assertTrue(pump(self.app, lambda: self.ctx.services.chat.can_send, 8))
        self.assertEqual(self.page.pill.text(), "Bot running")
        self.push("!hi", "pal")
        self.assertTrue(pump(self.app, lambda: "hey Pal, count 1" in self.said()), self.said())
        self.assertTrue(pump(self.app, lambda: "hey Pal" in self.page.log.toPlainText()))
        self.push("!hi", "pal")
        self.assertTrue(pump(self.app, lambda: "hey Pal, count 2" in self.said()))
        self.assertTrue(pump(self.app, lambda: json.loads((self.dir / "bot.json").read_text())["counts"] == {"hi": 2}))

    def test_5_clip_saves_the_replay_buffer_through_obs(self):
        self.push("!clip", "mod", "badges=moderator/1;")
        self.assertTrue(pump(self.app, lambda: "Couldn't save a clip, the replay buffer isn't running in OBS" in self.said()),
                        self.said())
        self.obs.replay = True
        self.ctx.bot.bot._cd_global.clear()
        self.push("!clip", "mod2", "badges=moderator/1;")
        self.assertTrue(pump(self.app, lambda: "Clip saved" in self.said()), self.said())

    def test_6_timer_posts_once_chat_has_moved(self):
        bot = self.ctx.bot.bot
        n = self.said().count("Follow!")
        bot._timer_state["t"] = [bot.clock() - 500, bot._lines - 5]
        self.assertTrue(pump(self.app, lambda: self.said().count("Follow!") == n + 1, 8), self.said())

    def test_7_moderation_from_the_page_times_out_and_blocks_a_private_term(self):
        p = self.page
        p.mod_on.setChecked(True)
        p.rules["links"]["action"].setCurrentIndex(1)
        p.rules["links"]["seconds"].setValue(60)
        p.personal.setPlainText("Zed Quux")
        p.rules["terms"]["action"].setCurrentIndex(0)
        self.assertTrue(pump(self.app, lambda: self.ctx.bot.bot.data["mod"]["terms"]["personal"] == ["Zed Quux"], 3))
        self.push("see evil.com", "troll")
        self.assertTrue(pump(self.app, lambda: any(k == "ban" for k, *_ in self.tw.mod_calls)))
        kind, q, body = next(c for c in self.tw.mod_calls if c[0] == "ban")
        self.assertEqual((body["data"]["user_id"], body["data"]["duration"]), ("u-troll", 60))
        self.push("my name is zed quux", "leaker")
        self.assertTrue(pump(self.app, lambda: any(k == "delete" for k, *_ in self.tw.mod_calls)))
        self.assertTrue(pump(self.app, lambda: "Timed out Troll" in p.log.toPlainText()))
        self.assertTrue(pump(self.app, lambda: "Deleted a message from Leaker" in p.log.toPlainText()))
        self.assertNotIn("zed", p.log.toPlainText().lower())

    def test_8_page_round_trips_through_sanitize_and_reloads(self):
        p = self.page
        p.save()
        data = p.collect()
        self.assertEqual(sanitize(data)["mod"], self.ctx.bot.bot.data["mod"])
        self.assertEqual(sanitize(data)["commands"], self.ctx.bot.bot.data["commands"])
        p.load(self.ctx.bot.store.load())
        self.assertEqual(p.cmds.rowCount(), 1)
        self.assertEqual(p.personal.toPlainText(), "Zed Quux")

    def test_9_turning_the_bot_off_silences_it(self):
        self.page.enabled.setChecked(False)
        self.assertTrue(pump(self.app, lambda: not self.ctx.bot.bot.data["enabled"], 3))
        before = len(self.said())
        self.push("!hi", "late")
        self.push("evil.com", "late2")
        pump(self.app, lambda: False, 0.6)
        self.assertEqual(len(self.said()), before)
        self.assertEqual(self.page.pill.text(), "Bot off")


if __name__ == "__main__":
    unittest.main()
