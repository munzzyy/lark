"""Settings, Live and Chat pages against a fake Twitch and a fake IRC server. Nothing reaches the real Twitch."""
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lark.app import MainWindow, build_context
from lark.config import Config
from lark.keyring import MemoryStore
from lark.twitch_api import TOKEN_KEY
from tests.fake_obs import FakeObs
from tests.fake_twitch import CLIENT_ID, FakeTwitch
from tests.test_ui_smoke import pump


async def instant(_):
    await asyncio.sleep(0.01)


class Backend:
    """FakeObs, FakeTwitch and a tiny IRC server on one background loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()
        self.obs, self.tw = FakeObs(password="pw"), FakeTwitch(pending_polls=2)
        self.irc_lines = []
        self.irc_writers = []
        self.obs_port = self.call(self.obs.start())
        self.call(self.tw.start())
        self.irc = self.call(asyncio.start_server(self._irc, "127.0.0.1", 0))
        self.irc_port = self.irc.sockets[0].getsockname()[1]

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    async def _irc(self, r, w):
        self.irc_writers.append(w)
        try:
            for _ in range(4):
                self.irc_lines.append((await r.readline()).decode().strip())
            w.write(b":x JOIN #munzzyy1\r\n")
            await w.drain()
            while True:
                line = await r.readline()
                if not line:
                    break
                self.irc_lines.append(line.decode().strip())
        except ConnectionError:
            pass  # Lark hung up while shutting down
        finally:
            w.close()

    async def _close_irc(self):
        for w in self.irc_writers:
            w.close()
        self.irc.close()
        await self.irc.wait_closed()

    def stop(self):
        self.call(self.obs.stop())
        self.call(self.tw.stop())
        self.call(self._close_irc())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)
        self.loop.close()


class TwitchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.b = Backend()
        cls.store = MemoryStore()
        cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        for k, v in (("obs_port", cls.b.obs_port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", "munzzyy1"),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 3)):
            cfg.set(k, v)
        cls.cfg = cfg
        cls.ctx = build_context(cfg, services_kw=dict(
            twitch_kw=dict(store=cls.store, id_base=cls.b.tw.id_base, api_base=cls.b.tw.api_base, sleep=instant),
            irc_kw=dict(host="127.0.0.1", port=cls.b.irc_port, use_tls=False, backoff=(0.1,))))
        cls.win = MainWindow(cls.ctx, "settings")
        cls.win.show()
        cls.ctx.bridge.start()
        cls.ctx.services.start()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and s.stream_key_set is not None and cls.ctx.services.port)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.services.stop()
        cls.ctx.bridge.stop()
        cls.b.stop()

    def test_1_without_a_client_id_it_only_explains_and_makes_no_requests(self):
        st = self.win.pages["settings"]
        st.client_id.setText("")
        st.connect_btn.click()
        self.assertFalse(st.need_app.isHidden())
        self.assertEqual(self.b.tw.requests, [])
        live = self.win.pages["live"]
        self.assertEqual({c.key: c.status for c in live.checks()}["title"], "skip")
        self.assertFalse(self.win.pages["chat"].send.isEnabled())
        self.assertTrue(live.twitch_card.isHidden())

    def test_2_connect_shows_code_then_everything_lights_up(self):
        st, live, chat = (self.win.pages[k] for k in ("settings", "live", "chat"))
        st.client_id.setText(CLIENT_ID)
        st.connect_btn.click()
        self.assertEqual(self.cfg.get("twitch_client_id"), CLIENT_ID)
        self.assertTrue(pump(self.app, lambda: st.code.text() == "ABCDEFGH"), st.twitch_status.text())
        self.assertIn("twitch.tv/activate", st.url.text())
        self.assertTrue(pump(self.app, lambda: "Connected as munzzyy1" in st.twitch_status.text()))
        self.assertIn(TOKEN_KEY, self.store.data)
        self.assertNotIn(self.store.data[TOKEN_KEY], self.cfg.path.read_text() if self.cfg.path.exists() else "")
        self.assertTrue(pump(self.app, lambda: not live.twitch_card.isHidden()))
        self.assertTrue(pump(self.app, lambda: live.title_edit.text() == "old title"))
        self.assertEqual({c.key: c.status for c in live.checks()}["title"], "ok")
        self.assertTrue(chat.send.isEnabled())

    def test_3_edit_title_and_category_patches_twitch(self):
        live = self.win.pages["live"]
        live.title_edit.setText("New stream title")
        live.cat_edit.setText("Rocket")
        live._search_cat()
        self.assertTrue(pump(self.app, lambda: "Rocket Quest" in live.cat_ids))
        live.cat_edit.setText("Rocket Quest")
        live._update_channel()
        self.assertTrue(pump(self.app, lambda: self.b.tw.channel["title"] == "New stream title"))
        self.assertEqual(self.b.tw.channel["game_id"], "511224")
        live.cat_edit.setText("not a real pick")
        n = len([r for r in self.b.tw.requests if r[0] == "patch_channel"])
        live._update_channel()
        self.app.processEvents()
        self.assertEqual(len([r for r in self.b.tw.requests if r[0] == "patch_channel"]), n)

    def test_4_chat_send_goes_out_over_irc_with_the_token(self):
        chat = self.win.pages["chat"]
        self.assertTrue(pump(self.app, lambda: any(l.startswith("NICK munzzyy1") for l in self.b.irc_lines), 8),
                        self.b.irc_lines)
        self.assertTrue(any(l.startswith("PASS oauth:") for l in self.b.irc_lines))
        chat.send.setText("hello from lark")
        chat._send()
        self.assertTrue(pump(self.app, lambda: "PRIVMSG #munzzyy1 :hello from lark" in self.b.irc_lines), self.b.irc_lines)
        self.assertEqual(chat.send.text(), "")

    def test_5_disconnect_clears_token_and_disables_everything(self):
        st, live, chat = (self.win.pages[k] for k in ("settings", "live", "chat"))
        st.disconnect_btn.click()
        self.assertTrue(pump(self.app, lambda: TOKEN_KEY not in self.store.data))
        self.assertTrue(pump(self.app, lambda: not chat.send.isEnabled() and live.twitch_card.isHidden()))


if __name__ == "__main__":
    unittest.main()
