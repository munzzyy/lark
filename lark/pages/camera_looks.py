from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QPushButton, QSlider, QVBoxLayout, QWidget)

from ..looks import BUILTIN, NUMERIC, Looks
from ..widgets import card, label

SLIDERS = [
    ("gamma", "Gamma", 100), ("contrast", "Contrast", 100), ("brightness", "Brightness", 100),
    ("saturation", "Saturation", 100), ("hue_shift", "Hue shift", 1), ("opacity", "Opacity", 1),
    ("sharpness", "Sharpness", 100), ("lut_amount", "LUT strength", 100),
]


class LookSlider(QWidget):
    def __init__(self, key, title, scale, on_value):
        super().__init__()
        self.key, self.scale, self.on_value = key, scale, on_value
        _, _, lo, hi, _ = NUMERIC[key]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(14)
        name = label(title)
        name.setFixedWidth(200)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(lo * scale), round(hi * scale))
        self.slider.setPageStep(max(1, (self.slider.maximum() - self.slider.minimum()) // 10))
        self.val = label("", "muted")
        self.val.setFixedWidth(56)
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(60)
        self.timer.timeout.connect(lambda: self.on_value(self.key, self.slider.value() / self.scale))
        self.slider.valueChanged.connect(self._moved)
        lay.addWidget(name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.val)

    def _text(self, v):
        return f"{v:g}" if self.scale == 1 else f"{v:.2f}"

    def _moved(self, raw):
        self.val.setText(self._text(raw / self.scale))
        self.timer.start()

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(round(v * self.scale))
        self.slider.blockSignals(False)
        self.val.setText(self._text(v))


class LooksSection(QWidget):
    """Looks (OBS filters on the camera input), under the camera controls."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx, self.store = ctx, ctx.store
        self.looks = Looks(ctx.bridge, ctx.config, ctx.store.toast.emit)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)

        lf, ll = card(spacing=8)
        head = QHBoxLayout()
        self.title = label("Looks", "h2")
        head.addWidget(self.title)
        head.addStretch(1)
        self.note = label("", "muted")
        head.addWidget(self.note)
        self.ab = QPushButton("A/B")
        self.ab.setToolTip("Flip between this look and the one you had before")
        self.ab.clicked.connect(self._ab)
        head.addWidget(self.ab)
        ll.addLayout(head)
        self.chips_box = QHBoxLayout()
        self.chips_box.setSpacing(6)
        ll.addLayout(self.chips_box)
        self.sliders = {}
        for key, title, scale in SLIDERS:
            s = LookSlider(key, title, scale, self._set)
            self.sliders[key] = s
            ll.addWidget(s)
        lut = QHBoxLayout()
        lut.setSpacing(14)
        lname = label("LUT file")
        lname.setFixedWidth(200)
        self.lut_path = label("", "muted")
        pick = QPushButton("Choose")
        pick.clicked.connect(self._pick_lut)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self._set("lut_path", ""))
        lut.addWidget(lname)
        lut.addWidget(self.lut_path, 1)
        lut.addWidget(pick)
        lut.addWidget(clear)
        ll.addLayout(lut)
        acts = QHBoxLayout()
        self.save = QPushButton("Save as new look")
        self.save.clicked.connect(self._save)
        self.delete = QPushButton("Delete this look")
        self.delete.clicked.connect(self._delete)
        acts.addStretch(1)
        acts.addWidget(self.save)
        acts.addWidget(self.delete)
        ll.addLayout(acts)
        self.looks_card = lf
        outer.addWidget(lf)

        s = self.store
        s.conn_changed.connect(self._conn)
        self._rebuild_chips()
        self.refresh()
        self._conn(s.conn, "")

    def _conn(self, state, _detail):
        if state == "connected":
            self.looks.forget_remote()
        self.refresh()

    def _rebuild_chips(self):
        while self.chips_box.count():
            w = self.chips_box.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.chips = {}
        for name in self.looks.names():
            b = QPushButton(name)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=name: self._select(n))
            self.chips[name] = b
            self.chips_box.addWidget(b)
        self.chips_box.addStretch(1)

    def refresh(self):
        lk = self.looks
        if set(self.chips) != set(lk.names()):
            self._rebuild_chips()
        for name, b in self.chips.items():
            b.setChecked(name == lk.active)
        for key, s in self.sliders.items():
            s.set_value(lk.params[key])
        self.title.setText("Looks (changed)" if lk.modified() else "Looks")
        path = lk.params["lut_path"]
        self.lut_path.setText(path.rsplit("/", 1)[-1] if path else "None")
        self.lut_path.setToolTip(path)
        self.ab.setEnabled(bool(lk.other))
        self.delete.setVisible(lk.active not in BUILTIN)
        on = self.store.connected
        self.looks_card.setEnabled(on)
        self.note.setText("" if on else "Connect OBS to use looks")

    def _set(self, key, value):
        try:
            self.looks.set_param(key, value)
        except ValueError as e:
            self.store.toast.emit(str(e), True)
        self.refresh()

    def _select(self, name):
        self.looks.select(name)
        self.refresh()

    def _ab(self):
        self.looks.toggle_ab()
        self.refresh()

    def _pick_lut(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a LUT image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self._set("lut_path", path)

    def _save(self):
        name, ok = QInputDialog.getText(self, "Save look", "Name for this look")
        if not ok:
            return
        try:
            self.looks.save_as(name.strip())
        except ValueError as e:
            self.store.toast.emit(str(e), True)
        self.refresh()

    def _delete(self):
        try:
            self.looks.delete(self.looks.active)
        except (ValueError, KeyError) as e:
            self.store.toast.emit(str(e), True)
        self.refresh()
