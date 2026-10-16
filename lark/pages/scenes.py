from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..wizard_dialog import WizardDialog
from ..widgets import EmptyState, Page, Pill, Toggle, card, divider, label


class SceneCard(QWidget):
    def __init__(self, store, name, accent):
        super().__init__()
        self.store, self.name, self.accent = store, name, accent
        self.frame, self.lay = card(spacing=10)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)
        head = QHBoxLayout()
        head.addWidget(label(name, "h2"))
        head.addStretch(1)
        self.pill = Pill("LIVE", "bad")
        head.addWidget(self.pill)
        self.switch = QPushButton("Switch")
        self.switch.setObjectName("primary")
        self.switch.setMinimumHeight(36)
        self.pill.setMinimumHeight(24)
        self.switch.clicked.connect(lambda: store.switch_scene(name))
        head.addWidget(self.switch)
        self.lay.addLayout(head)
        self.lay.addWidget(divider())
        self.items_box = QVBoxLayout()
        self.items_box.setSpacing(8)
        self.lay.addLayout(self.items_box)
        self.toggles = {}

    def set_live(self, live):
        self.pill.setVisible(live)
        self.switch.setVisible(not live)
        self.frame.setStyleSheet("QFrame#card { border-color: #ff5d6c; }" if live else "")

    def set_items(self, items):
        ids = [i["id"] for i in items]
        if ids != list(self.toggles):
            while self.items_box.count():
                w = self.items_box.takeAt(0).widget()
                if w:
                    w.setParent(None)
                    w.deleteLater()
            self.toggles = {}
            if not items:
                self.items_box.addWidget(label("No sources in this scene yet. Add one in OBS.", "muted"))
            for it in items:
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.addWidget(label(it["name"]), 1)
                t = Toggle()
                t.setProperty("accent", self.accent)
                t.toggled.connect(lambda on, i=it["id"]: self.store.set_item_enabled(self.name, i, on))
                rl.addWidget(t)
                self.items_box.addWidget(row)
                self.toggles[it["id"]] = t
        for it in items:
            self.toggles[it["id"]].set_checked_silent(it["enabled"])


class ScenesPage(Page):
    COLS = 2

    def __init__(self, ctx):
        super().__init__("Scenes", "Click Switch to go to a scene. Toggles show or hide a source in that scene.")
        self.ctx = ctx
        self.store = ctx.store
        wiz = QPushButton("Set up scenes")
        wiz.clicked.connect(self.open_wizard)
        self.header.addWidget(wiz)
        self.accent = ctx.config.get("accent")
        self.cards = {}
        self.grid = QGridLayout()
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignTop)
        self.ctx = ctx
        self.empty = EmptyState()
        self.body.addWidget(self.empty)
        self.first_run = label("First time here? Set up scenes builds a standard layout in OBS and only adds what is missing.", "muted")
        self.first_run.setVisible(not ctx.config.get("wizard_done"))
        self.body.addWidget(self.first_run)
        self.body.addLayout(self.grid)
        self.body.addStretch(1)
        s = self.store
        s.scenes_changed.connect(self.rebuild)
        s.items_changed.connect(self._items)
        s.conn_changed.connect(lambda *_: self.rebuild())
        self.rebuild()

    def rebuild(self):
        s = self.store
        if list(self.cards) != s.scenes:
            for c in self.cards.values():
                c.setParent(None)
                c.deleteLater()
            self.cards = {}
            for i, name in enumerate(s.scenes):
                c = SceneCard(s, name, self.accent)
                self.cards[name] = c
                self.grid.addWidget(c, i // self.COLS, i % self.COLS)
            for col in range(self.COLS):
                self.grid.setColumnStretch(col, 1)
        if not s.connected:
            self.empty.set_content("OBS is not connected", "Open OBS and your scenes show up here on their own.",
                                   "Open Settings", lambda: self.ctx.goto("settings"))
        else:
            self.empty.set_content("No scenes yet", "Add a scene in OBS, then come back.", "Refresh", s.refresh_scenes)
        self.empty.setVisible(not s.scenes)
        for name, c in self.cards.items():
            c.set_live(name == s.current_scene)
            if name in s.items:
                c.set_items(s.items[name])

    def _items(self, scene):
        c = self.cards.get(scene)
        if c and scene in self.store.items:
            c.set_items(self.store.items[scene])

    def open_wizard(self):
        WizardDialog(self.ctx, self).exec()
        self.first_run.setVisible(not self.ctx.config.get("wizard_done"))
