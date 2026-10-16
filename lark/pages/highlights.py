import os
import time
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from ..highlights import manager_for
from ..widgets import Page, Toggle, card, divider, label

TRIGGER_LABEL = {"chat_spike": "Chat spike", "manual": "Mark button", "hotkey": "Hotkey", "command": "Chat command"}
SHOWN = 100
THUMB_W, THUMB_H = 160, 90


def snippet(chat, n=3):
    lines = [f"{c['user']}: {c['text']}" for c in chat[-n:]]
    return "  /  ".join(lines) if lines else "No chat before this"


class Row(QWidget):
    def __init__(self, mgr, entry):
        super().__init__()
        self.mgr, self.id = mgr, entry["id"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 10, 0, 10)
        lay.setSpacing(16)
        outer.addLayout(lay)
        outer.addWidget(divider())
        self.thumb = QLabel("No clip")
        self.thumb.setFixedSize(THUMB_W, THUMB_H)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background: #12151c; border-radius: 8px; color: #5a6172;")
        lay.addWidget(self.thumb)
        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.head = label("", "h2")
        self.chat = label("", "muted", wrap=True)
        self.file = label("", "small")
        self.file.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.notes = QLineEdit(entry.get("notes", ""))
        self.notes.setPlaceholderText("Notes")
        self.notes.setMaxLength(500)
        self.notes.textEdited.connect(lambda t: mgr.update(self.id, notes=t))
        for w in (self.head, self.chat, self.file, self.notes):
            mid.addWidget(w)
        lay.addLayout(mid, 1)
        right = QVBoxLayout()
        right.setSpacing(8)
        krow = QHBoxLayout()
        krow.addWidget(label("Keep for Shorts", "small"))
        self.keep = Toggle()
        self.keep.set_checked_silent(bool(entry.get("keep_for_shorts")))
        self.keep.toggled.connect(lambda on: mgr.update(self.id, keep_for_shorts=on))
        krow.addWidget(self.keep)
        right.addLayout(krow)
        self.open = QPushButton("Open folder")
        self.open.clicked.connect(self._open)
        self.delete = QPushButton("Delete")
        self.delete.clicked.connect(lambda: mgr.delete(self.id))
        right.addWidget(self.open)
        right.addWidget(self.delete)
        right.addStretch(1)
        lay.addLayout(right)
        self._thumb_path = None
        self.refresh(entry)

    def refresh(self, e):
        when = time.strftime("%b %d  %H:%M:%S", time.localtime(e.get("ts", 0)))
        self.head.setText(f"{when}   {TRIGGER_LABEL.get(e.get('trigger'), e.get('trigger', ''))}")
        self.chat.setText(snippet(e.get("chat", [])))
        rp = e.get("replay")
        self.file.setText(rp if rp else "No replay file yet, the replay buffer was off or OBS has not reported it")
        self.open.setEnabled(True)
        th = e.get("thumbnail")
        if th != self._thumb_path:
            self._thumb_path = th
            pm = QPixmap(th) if th and os.path.isfile(th) else QPixmap()
            if pm.isNull():
                self.thumb.setPixmap(QPixmap())
                self.thumb.setText("No clip" if not rp else "No preview")
            else:
                self.thumb.setPixmap(pm.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        if self.keep.isChecked() != bool(e.get("keep_for_shorts")):
            self.keep.set_checked_silent(bool(e.get("keep_for_shorts")))
        if not self.notes.hasFocus() and self.notes.text() != e.get("notes", ""):
            self.notes.setText(e.get("notes", ""))

    def _open(self):
        e = self.mgr.find(self.id)
        rp = e.get("replay") if e else None
        folder = Path(rp).parent if rp and os.path.isdir(Path(rp).parent) else self.mgr.folder
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class HighlightsPage(Page):
    def __init__(self, ctx):
        super().__init__("Highlights", "Chat spikes and your own marks, saved from the replay buffer.")
        self.ctx = ctx
        self.mgr = manager_for(ctx)
        self.rows = {}

        cfg, cl = card(QHBoxLayout, spacing=14)
        self.auto = Toggle()
        self.auto.set_checked_silent(ctx.config.get("highlights_enabled"))
        self.auto.toggled.connect(lambda on: self._set("highlights_enabled", on))
        self.mult = QDoubleSpinBox()
        self.mult.setRange(1.5, 20)
        self.mult.setSingleStep(0.5)
        self.mult.setSuffix("x")
        self.mult.setValue(ctx.config.get("highlights_multiplier"))
        self.mult.valueChanged.connect(lambda v: self._set("highlights_multiplier", float(v)))
        self.cool = QSpinBox()
        self.cool.setRange(5, 3600)
        self.cool.setSuffix(" s")
        self.cool.setValue(int(ctx.config.get("highlights_cooldown")))
        self.cool.valueChanged.connect(lambda v: self._set("highlights_cooldown", int(v)))
        cl.addWidget(label("Clip on chat spikes", "h2"))
        cl.addWidget(self.auto)
        cl.addSpacing(12)
        cl.addWidget(label("Spike at", "muted"))
        cl.addWidget(self.mult)
        cl.addWidget(label("normal chat", "muted"))
        cl.addSpacing(12)
        cl.addWidget(label("Wait between clips", "muted"))
        cl.addWidget(self.cool)
        cl.addStretch(1)
        self.count = label("", "small")
        cl.addWidget(self.count)
        self.body.addWidget(cfg)

        self.list_card, self.list_lay = card(spacing=0)
        self.empty = label("No highlights yet. Press Mark on the Live page, Ctrl+Shift+R, or type !clip in chat as a "
                           "mod or the streamer.", "muted", wrap=True)
        self.body.addWidget(self.empty)
        self.body.addWidget(self.list_card)
        self.body.addStretch(1)
        self.mgr.changed.connect(self.refresh)
        self.refresh()

    def _set(self, key, value):
        self.ctx.config.set(key, value)
        try:
            self.ctx.config.save()
        except OSError as e:
            self.ctx.store.toast.emit(f"Could not save settings: {e.strerror or e}", True)

    def on_show(self):
        for e in self.mgr.entries[-SHOWN:]:
            if e.get("replay") and not e.get("thumbnail"):
                self.mgr.make_thumb(e["id"])
        self.refresh()

    def refresh(self):
        shown = list(reversed(self.mgr.entries[-SHOWN:]))
        ids = {e["id"] for e in shown}
        for i in [i for i in self.rows if i not in ids]:
            r = self.rows.pop(i)
            r.setParent(None)
            r.deleteLater()
        for pos, e in enumerate(shown):
            row = self.rows.get(e["id"])
            if row is None:
                row = self.rows[e["id"]] = Row(self.mgr, e)
                self.list_lay.insertWidget(pos, row)
            else:
                row.refresh(e)
        self.empty.setVisible(not shown)
        self.list_card.setVisible(bool(shown))
        total = len(self.mgr.entries)
        self.count.setText(f"{total} saved" if total <= SHOWN else f"Showing {SHOWN} of {total}")
