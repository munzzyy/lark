import logging
import signal
import sys
from types import SimpleNamespace

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QMainWindow, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import __version__
from .camera_backend import CameraWorker
from .config import Config
from .eptz import Eptz
from .icons import nav_icon
from .obsbridge import ObsBridge
from .pages.audio import AudioPage
from .pages.bot import BotPage
from .pages.camera import CameraPage
from .pages.chat import ChatPage
from .pages.events import EventsPage
from .pages.highlights import HighlightsPage
from .pages.deck import DeckPage
from .pages.live import LivePage
from .pages.overlays import OverlaysPage
from .pages.settings import SettingsPage
from .pages.scenes import ScenesPage
from .services import Services
from .shell import install as install_shell, restore_view
from .shortcuts import Shortcuts
from .store import Store
from .theme import build_stylesheet
from .widgets import Page, Pill, StickyToast, Toast, label

log = logging.getLogger("lark")

NAV = [("live", "Live"), ("scenes", "Scenes"), ("audio", "Audio"), ("camera", "Camera"),
       ("overlays", "Overlays"), ("chat", "Chat"), ("events", "Events"), ("highlights", "Highlights"),
       ("deck", "Deck"), ("bot", "Bot"), ("settings", "Settings")]
VIEWS = [k for k, _ in NAV]

CONN_PILL = {
    "connected": ("good", "OBS connected"),
    "connecting": ("warn", "Connecting"),
    "disconnected": ("", "OBS not running"),
    "auth_failed": ("bad", "Wrong password"),
}


class Placeholder(Page):
    def __init__(self, title):
        super().__init__(title)
        self.body.addWidget(label("Not built yet.", "muted"))
        self.body.addStretch(1)


def make_pages(ctx):
    pages = {}
    for key, title in NAV:
        pages[key] = Placeholder(title)
    pages["live"] = LivePage(ctx)
    pages["scenes"] = ScenesPage(ctx)
    pages["audio"] = AudioPage(ctx)
    pages["camera"] = CameraPage(ctx)
    pages["overlays"] = OverlaysPage(ctx)
    pages["chat"] = ChatPage(ctx)
    pages["events"] = EventsPage(ctx)
    pages["bot"] = BotPage(ctx)
    pages["highlights"] = HighlightsPage(ctx)
    pages["deck"] = DeckPage(ctx)
    pages["settings"] = SettingsPage(ctx)
    return pages


class MainWindow(QMainWindow):
    def __init__(self, ctx, view="live"):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Lark")
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(208)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(14, 22, 14, 18)
        rl.setSpacing(4)
        logo = label("Lark", "logo")
        rl.addWidget(logo)
        rl.addSpacing(18)

        self.stack = QStackedWidget()
        self.pages = make_pages(ctx)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        accent = ctx.config.get("accent")
        for i, (key, title) in enumerate(NAV):
            b = QPushButton(title)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setIcon(nav_icon(key, on=accent))
            b.setIconSize(QSize(20, 20))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_view(k))
            self.group.addButton(b, i)
            self.buttons[key] = b
            rl.addWidget(b)
            self.stack.addWidget(self.pages[key])
        rl.addStretch(1)
        self.conn_pill = Pill("OBS not running")
        rl.addWidget(self.conn_pill)
        rl.addWidget(label(f"Lark {__version__}", "small"))
        lay.addWidget(rail)
        lay.addWidget(self.stack, 1)

        self.shortcuts = Shortcuts(self)
        self.toast = Toast(root)
        ctx.store.toast.connect(self.toast.show_message)
        self.sticky = StickyToast(root, ctx.store.sticky_clicked.emit)
        ctx.store.sticky_show.connect(self.sticky.show_item)
        ctx.store.sticky_hide.connect(self.sticky.hide_item)
        ctx.store.conn_changed.connect(self._conn)
        self._conn(ctx.store.conn, "")
        self.show_view(view)
        install_shell(self)

    def refresh_icons(self):
        accent = self.ctx.config.get("accent")
        for key, b in self.buttons.items():
            b.setIcon(nav_icon(key, on=accent))

    def _conn(self, state, detail):
        s, t = CONN_PILL.get(state, ("", state))
        self.conn_pill.set_state(s, t)
        self.conn_pill.setToolTip(detail)

    def show_view(self, key):
        if key not in self.pages:
            raise KeyError(key)
        self.buttons[key].setChecked(True)
        self.stack.setCurrentWidget(self.pages[key])
        self.pages[key].on_show()


def build_context(config=None, services_kw=None):
    config = config or Config()
    bridge = ObsBridge(config)
    store = Store(bridge, config)
    camera = CameraWorker(config)
    ctx = SimpleNamespace(config=config, bridge=bridge, store=store, camera=camera, services=Services(config, **(services_kw or {})))
    ctx.apply_settings = lambda old=None: None
    ctx.eptz = Eptz(ctx)
    return ctx


def run(view=None, size=None, config=None):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Lark")
    app.setDesktopFileName("lark")
    f = QFont(app.font())
    f.setPixelSize(14)
    app.setFont(f)
    ctx = build_context(config)
    app.setStyleSheet(build_stylesheet(ctx.config.get("accent")))
    win = MainWindow(ctx, view or restore_view(ctx.config, VIEWS))

    def apply_settings(old=None):
        app.setStyleSheet(build_stylesheet(ctx.config.get("accent")))
        win.refresh_icons()
        ctx.bridge.reconnect()
        ctx.camera.refresh()
        ctx.services.apply_settings()
    ctx.apply_settings = apply_settings
    if size:
        win.resize(*size)
    win.show()
    ctx.bridge.start()
    ctx.camera.start()
    ctx.services.start()
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()
    tick.start(250)
    tick.timeout.connect(lambda: None)
    app.aboutToQuit.connect(lambda: shutdown(ctx))
    return app.exec()


def shutdown(ctx):
    for name in ("camera", "services"):
        svc = getattr(ctx, name, None)
        if svc is not None:
            svc.stop()
    ctx.bridge.stop()
