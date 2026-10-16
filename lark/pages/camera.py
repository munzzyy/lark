from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QComboBox, QFrame, QGridLayout, QHBoxLayout, QMenu, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from ..camera_backend import DISPLAY_ONLY, parse_color
from ..eptz import zoom_factor
from .camera_looks import LooksSection
from ..widgets import EmptyState, Page, Toggle, card, divider, label
from .framing import CameraModeCard, FramingCard


def show_value(text_id, v):
    if text_id == "zoom_absolute":
        return f"{v}  {zoom_factor(v):.1f}x"
    return str(v)


BUTTONS_PER_ROW = 5
POLL_CHOICES = [("0.5 s", 500), ("1 s", 1000), ("2 s", 2000), ("5 s", 5000)]


def display_text(c):
    """What a read-only row shows for a control."""
    if c.value is None:
        return ""
    if c.type == "boolean":
        return "On" if c.value else "Off"
    if c.type == "menu":
        return next((n for t, n in c.menu if t == c.value), str(c.value))
    return str(c.value)


class CtrlRow(QWidget):
    """One control. Writes go out through on_change(text_id, value_string)."""

    def __init__(self, c, on_change, accent):
        super().__init__()
        self.c, self.on_change = c, on_change
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(14)
        self.name = label(c.name)
        self.name.setFixedWidth(200)
        if c.tooltip:
            self.setToolTip(c.tooltip)
        lay.addWidget(self.name)
        self.w = None
        self._timer = None
        if c.type == "integer" and c.text_id not in DISPLAY_ONLY and c.min is not None and not c.readonly:
            self.w = QSlider(Qt.Horizontal)
            self.w.setRange(int(c.min), int(c.max))
            self.w.setSingleStep(int(c.step or 1))
            self.w.setPageStep(max(1, (int(c.max) - int(c.min)) // 10))
            self.val = label("", "muted")
            self.val.setFixedWidth(96 if c.text_id == "zoom_absolute" else 56)
            self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(80)
            self._timer.timeout.connect(lambda: on_change(c.text_id, str(self.w.value())))
            self.w.valueChanged.connect(self._moved)
            lay.addWidget(self.w, 1)
            lay.addWidget(self.val)
        elif c.type == "boolean" and not c.readonly:
            self.w = Toggle()
            self.w.setProperty("accent", accent)
            self.w.toggled.connect(lambda on: on_change(c.text_id, "1" if on else "0"))
            lay.addStretch(1)
            lay.addWidget(self.w)
        elif c.type == "menu" and c.text_id not in DISPLAY_ONLY and not c.readonly:
            self.w = QComboBox()
            for tid, nm in c.menu:
                self.w.addItem(nm, tid)
            self.w.setMinimumWidth(220)
            self.w.activated.connect(lambda _: on_change(c.text_id, self.w.currentData()))
            lay.addStretch(1)
            lay.addWidget(self.w)
        elif c.type == "button":
            lay.addStretch(1)
            grid = QGridLayout()
            grid.setSpacing(6)
            for i, (tid, nm) in enumerate(c.menu):
                b = QPushButton(nm)
                b.clicked.connect(lambda _=False, t=tid: on_change(c.text_id, t))
                grid.addWidget(b, i // BUTTONS_PER_ROW, i % BUTTONS_PER_ROW)
            lay.addLayout(grid)
        elif c.type in ("color", "colour"):
            self.val = label("", "muted")
            self.swatch = QFrame()
            self.swatch.setFixedSize(34, 22)
            lay.addStretch(1)
            lay.addWidget(self.val)
            lay.addWidget(self.swatch)
            if not c.readonly:
                self.w = QPushButton("Pick")
                self.w.clicked.connect(self._pick)
                lay.addWidget(self.w)
        else:
            self.val = label("", "muted")
            self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.val.setWordWrap(True)
            lay.addWidget(self.val, 1)
        self.update_from(c)

    def _pick(self):
        cur = parse_color(self.c.value)
        col = QColorDialog.getColor(QColor(cur) if cur else QColor("#ffffff"), self, "Pick a colour")
        if col.isValid():
            self.on_change(self.c.text_id, col.name())

    def _moved(self, v):
        self.val.setText(show_value(self.c.text_id, v))
        self._timer.start()

    def update_from(self, c):
        self.c = c
        enabled = not c.inactive and not c.readonly
        w = self.w
        if c.type in ("color", "colour"):
            hexv = parse_color(c.value)
            self.val.setText(hexv or display_text(c))
            self.swatch.setStyleSheet(f"background:{hexv}; border:1px solid #4a5160; border-radius:4px;"
                                      if hexv else "border:1px dashed #4a5160; border-radius:4px;")
        elif isinstance(w, QSlider):
            if not w.isSliderDown() and not self._timer.isActive():
                w.blockSignals(True)
                w.setValue(int(c.value or 0))
                w.blockSignals(False)
                self.val.setText(show_value(c.text_id, int(c.value or 0)))
        elif isinstance(w, Toggle):
            w.set_checked_silent(bool(c.value))
        elif isinstance(w, QComboBox):
            if not w.view().isVisible():
                i = w.findData(c.value)
                w.blockSignals(True)
                w.setCurrentIndex(i)
                w.blockSignals(False)
        elif w is None and hasattr(self, "val"):
            self.val.setText(display_text(c))
        if w is not None:
            w.setEnabled(enabled)


class CameraPage(Page):
    def __init__(self, ctx):
        super().__init__("Camera", "Changes go straight to the camera, and anything changed elsewhere shows up here.")
        self.ctx = ctx
        self.worker = ctx.camera
        self.accent = ctx.config.get("accent")
        self.rows = {}
        self.signature = None
        self.snap = None
        self.mode_card = CameraModeCard(ctx.eptz, self.accent)
        self.body.addWidget(self.mode_card)
        self.framing = FramingCard(ctx, self.camera_zoom)
        self.body.addWidget(self.framing)
        self.status = EmptyState("Looking for the camera...")
        self.body.addWidget(self.status)

        opts = QHBoxLayout()
        opts.addStretch(1)
        opts.addWidget(label("Refresh every", "muted"))
        self.poll = QComboBox()
        for text, ms in POLL_CHOICES:
            self.poll.addItem(text, ms)
        self.poll.setToolTip("How often Lark re-reads the camera, so a press on the remote shows up here")
        self.poll.setCurrentIndex(max(0, self.poll.findData(ctx.config.get("camera_poll_ms"))))
        self.poll.activated.connect(self._poll_changed)
        opts.addWidget(self.poll)
        self.body.addLayout(opts)

        self.presets_card, pl = card(QHBoxLayout, spacing=10)
        pl.addWidget(label("Presets", "h2"))
        pl.addStretch(1)
        self.preset_box = pl
        self.presets_card.hide()
        self.body.addWidget(self.presets_card)

        self.tabs = QHBoxLayout()
        self.tabs.setSpacing(6)
        self.tab_buttons = []
        self.body.addLayout(self.tabs)
        self.content = QVBoxLayout()
        self.content.setSpacing(16)
        self.body.addLayout(self.content)
        self.looks_section = LooksSection(ctx)
        self.body.addWidget(self.looks_section)
        self.body.addStretch(1)
        self.current_tab = 0

        w = self.worker
        w.snapshot_ready.connect(self._snapshot)
        w.unavailable.connect(self._unavailable)
        w.error.connect(lambda m: ctx.store.toast.emit(m, True))

    def on_show(self):
        self.worker.refresh()
        self.ctx.eptz.refresh()

    def camera_zoom(self):
        for p in (self.snap.pages if self.snap else []):
            for cat in p.categories:
                for c in cat.ctrls:
                    if c.text_id == "zoom_absolute":
                        return zoom_factor(c.value)
        return 1.0

    def _poll_changed(self, _):
        ms = self.poll.currentData()
        try:
            self.ctx.config.set("camera_poll_ms", ms)
            self.ctx.config.save()
        except (ValueError, OSError) as e:
            self.ctx.store.toast.emit(f"Could not save the refresh rate ({e})", True)
        self.worker.set_interval(ms)

    def _unavailable(self, msg):
        self.status.set_content("Camera not available", msg, "Try again", self.worker.refresh)
        self.status.show()
        self.signature = None
        self._clear(self.content)
        self._clear(self.tabs)
        self.presets_card.hide()

    @staticmethod
    def _clear(layout):
        while layout.count():
            w = layout.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def _sig(self, snap):
        return [(p.title, [(c.title, [(x.text_id, x.type, x.min, x.max, x.inactive, x.readonly) for x in c.ctrls])
                           for c in p.categories]) for p in snap.pages], snap.presets

    def _snapshot(self, snap):
        self.snap = snap
        self.framing.update_total()
        self.status.hide()
        sig = self._sig(snap)
        if sig != self.signature:
            self.signature = sig
            self._build(snap)
        for p in snap.pages:
            for cat in p.categories:
                for c in cat.ctrls:
                    r = self.rows.get(c.text_id)
                    if r:
                        r.update_from(c)

    def _build(self, snap):
        self._clear(self.tabs)
        self.tab_buttons = []
        for i, p in enumerate(snap.pages):
            b = QPushButton(p.title)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, i=i: self._select(i))
            self.tabs.addWidget(b)
            self.tab_buttons.append(b)
        self.tabs.addStretch(1)
        self._fill_presets(snap.presets)
        self.current_tab = min(self.current_tab, max(0, len(snap.pages) - 1))
        self._select(self.current_tab)

    def _fill_presets(self, presets):
        while self.preset_box.count() > 2:
            w = self.preset_box.takeAt(2).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.presets_card.setVisible(bool(presets))
        for tid, name in presets:
            b = QPushButton(name)
            b.setMinimumWidth(48)
            b.setToolTip(f"Load preset {name}. Right click to save the current settings into it.")
            b.clicked.connect(lambda _=False, t=tid: self._change("preset", t))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, t=tid, bb=b: self._save_menu(bb, pos, t))
            self.preset_box.addWidget(b)

    def _save_menu(self, btn, pos, load_id):
        m = QMenu(btn)
        n = load_id.split("_")[-1]
        act = m.addAction(f"Save current settings to preset {n}")
        if m.exec(btn.mapToGlobal(pos)) is act:
            self._change("preset", f"save_{n}")

    def _select(self, i):
        self.current_tab = i
        for j, b in enumerate(self.tab_buttons):
            b.setChecked(j == i)
        self._clear(self.content)
        self.rows = {}
        if not self.snap or i >= len(self.snap.pages):
            return
        for cat in self.snap.pages[i].categories:
            f, lay = card(spacing=4)
            lay.addWidget(label(cat.title, "h2"))
            lay.addSpacing(4)
            for k, c in enumerate(cat.ctrls):
                if k:
                    lay.addWidget(divider())
                r = CtrlRow(c, self._change, self.accent)
                self.rows[c.text_id] = r
                lay.addWidget(r)
            self.content.addWidget(f)

    def _change(self, text_id, value):
        self.worker.apply({text_id: value})
