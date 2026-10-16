import os
import socket
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lark.app import MainWindow, build_context
from lark.config import Config
from tests.test_ui_smoke import close_window, pump


class PortFallback(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.busy = socket.socket()
        self.busy.bind(("127.0.0.1", 0))
        self.busy.listen(1)
        self.busy_port = self.busy.getsockname()[1]
        cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        cfg.set("overlay_port", self.busy_port)
        cfg.set("camera_module_dir", "/nonexistent")
        cfg.set("twitch_channel", "")
        cfg.set("highlights_dir", tempfile.mkdtemp())
        self.ctx = build_context(cfg)
        self.win = MainWindow(self.ctx, "overlays")
        self.toasts, self.states = [], []
        self.ctx.store.toast.connect(lambda t, bad: self.toasts.append((t, bad)))
        self.ctx.services.overlay_state.connect(lambda s, d: self.states.append((s, d)))
        self.ctx.services.start()

    def tearDown(self):
        close_window(self.app, self.win)
        self.ctx.services.stop()
        self.busy.close()

    def test_busy_port_falls_back_and_urls_show_the_real_port(self):
        svc = self.ctx.services
        self.assertTrue(pump(self.app, lambda: svc.port is not None and self.states))
        self.assertNotEqual(svc.port, self.busy_port)
        self.assertEqual(self.states[-1][0], "fallback")
        self.assertIn(str(self.busy_port), self.states[-1][1])
        real = f"http://127.0.0.1:{svc.port}"
        ov = self.win.pages["overlays"]
        self.assertEqual(ov.chat_card[0].url.text(), real + "/chat")
        self.assertEqual(ov.start_card[0].url.text(), real + "/starting")
        self.assertIn(str(svc.port), ov.server_pill.text())
        ev = self.win.pages["events"]
        self.assertTrue(all(u.text().startswith(real) for _, u, _ in ev.sources))

    def test_fallback_toast_is_informational_not_an_error(self):
        self.assertTrue(pump(self.app, lambda: self.states))
        pump(self.app, lambda: False, 0.2)
        self.assertTrue(self.toasts)
        self.assertFalse(any(bad for _, bad in self.toasts))

    def test_free_port_still_used_as_configured(self):
        self.ctx.services.stop()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        cfg = self.ctx.config
        cfg.set("overlay_port", free)
        ctx = build_context(cfg)
        ctx.services.start()
        try:
            self.assertTrue(pump(self.app, lambda: ctx.services.port is not None))
            self.assertEqual(ctx.services.port, free)
        finally:
            ctx.services.stop()


if __name__ == "__main__":
    unittest.main()
