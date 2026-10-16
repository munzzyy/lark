from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSlider, QVBoxLayout, QWidget

from ..widgets import EmptyState, MeterBar, Page, card, divider, label

MIN_DB, MAX_DB = -60.0, 6.0


class AudioRow(QWidget):
    def __init__(self, store, name):
        super().__init__()
        self.store, self.name = store, name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(16)
        self.title = label(name, "h2")
        self.title.setFixedWidth(180)
        lay.addWidget(self.title)
        mid = QVBoxLayout()
        mid.setSpacing(8)
        self.meter = MeterBar()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(MIN_DB * 10), int(MAX_DB * 10))
        self.slider.setSingleStep(5)
        self.slider.setPageStep(30)
        mid.addWidget(self.meter)
        mid.addWidget(self.slider)
        lay.addLayout(mid, 1)
        self.db = label("", "muted")
        self.db.setFixedWidth(72)
        self.db.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.db)
        self.mute = QPushButton("Mute")
        self.mute.setObjectName("mute")
        self.mute.setCheckable(True)
        self.mute.setToolTip("Mute or unmute this input in OBS")
        self.slider.setToolTip("Volume in dB. 0 is unchanged.")
        lay.addWidget(self.mute)

        self._send = QTimer(self)
        self._send.setSingleShot(True)
        self._send.setInterval(40)
        self._send.timeout.connect(lambda: store.set_volume(name, self.slider.value() / 10))
        self.slider.valueChanged.connect(self._moved)
        self.mute.toggled.connect(self._muted)
        self.sync()

    def _moved(self, v):
        self.db.setText(f"{v / 10:+.1f} dB")
        self._send.start()

    def _muted(self, on):
        self.mute.setText("Muted" if on else "Mute")
        self.meter.set_muted(on)
        self.store.set_mute(self.name, on)

    def sync(self):
        i = self.store.inputs.get(self.name)
        if not i:
            return
        if not self.slider.isSliderDown() and not self._send.isActive():
            self.slider.blockSignals(True)
            db = max(MIN_DB, min(MAX_DB, i.get("db", 0.0)))
            self.slider.setValue(round(db * 10))
            self.slider.blockSignals(False)
            self.db.setText(f"{i.get('db', 0.0):+.1f} dB" if i.get("db", 0) > -100 else "-inf dB")
        self.mute.blockSignals(True)
        self.mute.setChecked(i.get("muted", False))
        self.mute.setText("Muted" if i.get("muted") else "Mute")
        self.mute.blockSignals(False)
        self.meter.set_muted(i.get("muted", False))


class AudioPage(Page):
    def __init__(self, ctx):
        super().__init__("Audio", "Levels come live from OBS. Sliders change the volume in OBS.")
        self.store = ctx.store
        self.rows = {}
        self.box, self.lay = card(spacing=0)
        self.ctx = ctx
        self.empty = EmptyState()
        self.lay.addWidget(self.empty)
        self.body.addWidget(self.box)
        self.body.addStretch(1)
        s = self.store
        s.inputs_changed.connect(self.rebuild)
        s.input_updated.connect(lambda n: self.rows[n].sync() if n in self.rows else None)
        s.meters.connect(self._meters)
        s.conn_changed.connect(lambda *_: self.rebuild())
        self.rebuild()

    def rebuild(self):
        names = list(self.store.inputs)
        if names != list(self.rows):
            for r in self.rows.values():
                r.setParent(None)
                r.deleteLater()
            while self.lay.count() > 1:
                w = self.lay.takeAt(1).widget()
                if w:
                    w.setParent(None)
                    w.deleteLater()
            self.rows = {}
            for n in names:
                if self.rows:
                    self.lay.addWidget(divider())
                r = AudioRow(self.store, n)
                self.rows[n] = r
                self.lay.addWidget(r)
        for r in self.rows.values():
            r.sync()
        if not self.store.connected:
            self.empty.set_content("OBS is not connected", "Open OBS and your audio inputs show up here.",
                                   "Open Settings", lambda: self.ctx.goto("settings"))
        else:
            self.empty.set_content("No audio inputs in OBS", "Add a mic or desktop audio source in OBS.",
                                   "Refresh", self.store.refresh_inputs)
        self.empty.setVisible(not self.rows)

    def _meters(self, levels):
        for n, (lvl, peak) in levels.items():
            r = self.rows.get(n)
            if r:
                r.meter.push(lvl, peak)
