import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFormLayout, QGridLayout,
                               QHBoxLayout, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout)

from ..deck import PAD_NAMES, DeckActions, PadReader, contrast_text
from ..widgets import Page, Pill, label

COLS = 4


def _pad_path():
    return os.environ.get("LARK_PAD_DEVICE") or "/dev/input/js0"


class DeckButton(QPushButton):
    def __init__(self, key):
        super().__init__()
        self.key = key
        self.setObjectName("deckbtn")
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def paint_as(self, text, sub, color, lit):
        self.setText(f"{text}\n{sub}" if sub else text)
        fg = contrast_text(color)
        border = "#ffffff" if lit else "transparent"
        self.setStyleSheet(
            f"QPushButton#deckbtn {{ background: {color}; color: {fg}; border: 3px solid {border}; "
            f"border-radius: 16px; font-size: 18px; font-weight: 800; padding: 10px; }}"
            f"QPushButton#deckbtn:pressed {{ background: {QColor(color).darker(130).name()}; }}")


class ButtonEditor(QDialog):
    def __init__(self, parent, key, label_text, color, default_label, default_color):
        super().__init__(parent)
        self.setWindowTitle("Edit button")
        self.defaults = (default_label, default_color)
        self.color = color
        self.reset = False
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(label_text)
        self.name.setMaxLength(40)
        form.addRow("Label", self.name)
        self.pick = QPushButton(color)
        self.pick.clicked.connect(self._pick)
        form.addRow("Colour", self.pick)
        lay.addLayout(form)
        row = QHBoxLayout()
        back = QPushButton("Reset")
        back.clicked.connect(self._reset)
        ok = QPushButton("Save")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(back)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self._paint()

    def _paint(self):
        self.pick.setStyleSheet(f"background: {self.color}; color: {contrast_text(self.color)};")
        self.pick.setText(self.color)

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Button colour")
        if c.isValid():
            self.color = c.name()
            self._paint()

    def _reset(self):
        self.name.setText(self.defaults[0])
        self.color = self.defaults[1]
        self.reset = True
        self._paint()

    def result_style(self):
        text = self.name.text().strip()
        if self.reset or (text == self.defaults[0] and self.color == self.defaults[1]):
            return None
        out = {}
        if text and text != self.defaults[0]:
            out["label"] = text
        if self.color != self.defaults[1]:
            out["color"] = self.color
        return out or None


class PadMapDialog(QDialog):
    def __init__(self, parent, choices, mapping, background):
        super().__init__(parent)
        self.setWindowTitle("Pad mapping")
        self.combos = {}
        lay = QVBoxLayout(self)
        lay.addWidget(label("Pick what each Xbox pad button does.", "muted"))
        form = QFormLayout()
        for name in PAD_NAMES:
            cb = QComboBox()
            cb.addItem("Nothing", "")
            for key, text in choices:
                cb.addItem(text, key)
            i = cb.findData(mapping.get(name, ""))
            cb.setCurrentIndex(max(i, 0))
            form.addRow(name, cb)
            self.combos[name] = cb
        lay.addLayout(form)
        self.background = QCheckBox("Pad works in the background (when Lark is not the focused window)")
        self.background.setChecked(background)
        lay.addWidget(self.background)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Save")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def mapping(self):
        return {n: cb.currentData() for n, cb in self.combos.items() if cb.currentData()}


class DeckPage(Page):
    def __init__(self, ctx):
        super().__init__("Deck", "Big buttons for the things you hit mid-stream. Works with mouse, touch and an Xbox pad.")
        self.ctx, self.store, self.cfg = ctx, ctx.store, ctx.config
        self.actions = DeckActions(ctx)
        self.focus_check = lambda: QApplication.activeWindow() is not None
        self.dialog_open = False
        self.buttons = {}

        self.pad_pill = Pill("no pad")
        self.header.addWidget(self.pad_pill)
        self.edit = QPushButton("Edit buttons")
        self.edit.setCheckable(True)
        self.edit.toggled.connect(self._edit_toggled)
        self.header.addWidget(self.edit)
        self.map_btn = QPushButton("Pad mapping")
        self.map_btn.clicked.connect(self.open_mapping)
        self.header.addWidget(self.map_btn)

        self.hint = label("", "muted")
        self.body.addWidget(self.hint)
        self.grid = QGridLayout()
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignTop)
        self.body.addLayout(self.grid)
        self.body.addStretch(1)

        s = self.store
        s.scenes_changed.connect(self.refresh)
        s.outputs_changed.connect(self._repaint)
        s.inputs_changed.connect(self._repaint)
        s.input_updated.connect(lambda *_: self._repaint())
        s.conn_changed.connect(lambda *_: self.refresh())
        self.pad = PadReader(_pad_path(), parent=self)
        self.pad.status.connect(self._pad_status)
        self.pad.pressed.connect(self.on_pad)
        self._pad_status(self.pad.state)
        self.pad.start()
        self.refresh()

    def on_show(self):
        self.refresh()

    def _pad_status(self, state):
        self.pad_pill.set_state("good" if state == "pad connected" else "", state)

    def refresh(self):
        keys = self.actions.keys()
        if list(self.buttons) != keys:
            for b in self.buttons.values():
                b.setParent(None)
                b.deleteLater()
            self.buttons = {}
            for i, k in enumerate(keys):
                b = DeckButton(k)
                b.clicked.connect(lambda _=False, k=k: self.press(k))
                self.buttons[k] = b
                self.grid.addWidget(b, i // COLS, i % COLS)
            for c in range(COLS):
                self.grid.setColumnStretch(c, 1)
        self._repaint()

    def _repaint(self):
        style = self.cfg.get("deck_style")
        for k, b in self.buttons.items():
            text, color = self.actions.look(k, style)
            lit, sub = self.actions.state(k)
            b.paint_as(text, sub, color, lit)
        self.hint.setText("Edit mode: click a button to change its label or colour." if self.edit.isChecked() else "")

    def _edit_toggled(self, _):
        self._repaint()

    def press(self, key):
        if self.edit.isChecked():
            return self.edit_button(key)
        self.actions.run(key)

    def edit_button(self, key):
        style = dict(self.cfg.get("deck_style"))
        text, color = self.actions.look(key, style)
        dlg = ButtonEditor(self, key, text, color, self.actions.default_label(key), self.actions.default_color(key))
        self.dialog_open = True
        try:
            accepted = dlg.exec() == QDialog.Accepted
        finally:
            self.dialog_open = False
        if not accepted:
            return
        new = dlg.result_style()
        if new is None:
            style.pop(key, None)
        else:
            style[key] = new
        self._save("deck_style", style)
        self._repaint()

    def open_mapping(self):
        choices = [(k, self.actions.look(k, self.cfg.get("deck_style"))[0]) for k in self.actions.keys()]
        dlg = PadMapDialog(self, choices, self.cfg.get("deck_pad_map"), self.cfg.get("deck_pad_background"))
        self.dialog_open = True
        try:
            accepted = dlg.exec() == QDialog.Accepted
        finally:
            self.dialog_open = False
        if accepted:
            self._save("deck_pad_map", dlg.mapping())
            self._save("deck_pad_background", dlg.background.isChecked())

    def _save(self, key, value):
        self.cfg.set(key, value)
        try:
            self.cfg.save()
        except OSError as e:
            self.store.toast.emit(f"Could not save the deck: {e}", True)

    def on_pad(self, name):
        if self.dialog_open:
            return
        if not self.cfg.get("deck_pad_background") and not self.focus_check():
            return
        key = self.cfg.get("deck_pad_map").get(name)
        if key and key in self.actions.keys():
            self.actions.run(key)
