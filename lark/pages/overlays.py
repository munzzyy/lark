from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QWidget

from ..widgets import Page, Pill, card, divider, label


class OverlaysPage(Page):
    def __init__(self, ctx):
        super().__init__("Overlays", "Browser sources for OBS. They run from Lark on this computer only.")
        self.ctx, self.svc, self.store = ctx, ctx.services, ctx.store
        self.server_pill = Pill("Starting")
        self.header.addWidget(self.server_pill)

        self.chat_card = self._overlay("Chat", "Twitch chat as a transparent overlay that fades out on its own.",
                                       "/chat", 600, 900, "Lark chat")
        self.start_card = self._overlay("Starting soon", "A card with a countdown for the start of the stream.",
                                        "/starting", 1920, 1080, "Lark starting soon")
        self.body.addWidget(self.chat_card[0])

        frame, lay = self.start_card
        lay.addWidget(divider())
        row = QHBoxLayout()
        row.setSpacing(10)
        self.title_edit = QLineEdit(ctx.config.get("starting_title"))
        self.title_edit.setPlaceholderText("Title")
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 180)
        self.minutes.setValue(5)
        self.minutes.setSuffix(" min")
        self.minutes.setMinimumWidth(100)
        start = QPushButton("Start countdown")
        start.clicked.connect(self._start)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        row.addWidget(self.title_edit, 1)
        row.addWidget(self.minutes)
        row.addWidget(start)
        row.addWidget(clear)
        lay.addLayout(row)
        self.body.addWidget(frame)
        self.body.addStretch(1)

        self.svc.overlay_state.connect(self._state)
        self._refresh_urls()

    def _overlay(self, title, blurb, path, w, h, source_name):
        f, lay = card(spacing=10)
        lay.addWidget(label(title, "h2"))
        lay.addWidget(label(blurb, "muted", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(10)
        url = QLineEdit()
        url.setReadOnly(True)
        copy = QPushButton("Copy URL")
        add = QPushButton("Add to OBS")
        add.setObjectName("primary")
        copy.clicked.connect(lambda: self._copy(url.text()))
        add.clicked.connect(lambda: self.store.add_browser_source(source_name, url.text(), w, h))
        row.addWidget(url, 1)
        row.addWidget(copy)
        row.addWidget(add)
        lay.addLayout(row)
        f.url, f.path, f.add = url, path, add
        return f, lay

    def _copy(self, text):
        if text:
            QGuiApplication.clipboard().setText(text)
            self.store.toast.emit("URL copied", False)

    def _refresh_urls(self):
        for f in (self.chat_card[0], self.start_card[0]):
            u = self.svc.url(f.path)
            f.url.setText(u or "")
            f.add.setEnabled(bool(u))
        up = self.svc.port is not None
        self.server_pill.set_state("good" if up else "bad", f"Running on port {self.svc.port}" if up else "Not running")

    def _state(self, state, detail):
        self._refresh_urls()
        if state == "error":
            self.store.toast.emit(detail, True)
        elif state == "fallback":
            self.store.toast.emit(detail, False)

    def on_show(self):
        self._refresh_urls()

    def _start(self):
        title = self.title_edit.text().strip() or "Starting soon"
        self.svc.set_starting(title, self.minutes.value())
        self.store.toast.emit(f"Countdown started, {self.minutes.value()} min", False)

    def _clear(self):
        self.svc.set_starting(self.title_edit.text().strip() or "Starting soon", 0)
