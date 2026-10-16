"""Framing in OBS: the drag pad, sliders and presets, and the camera mode card. Logic lives in lark/eptz.py."""
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QMenu, QPushButton, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from ..eptz import MODE_DETAIL, MODE_QUALITY, QUALITY_NOTE, ZOOM_MAX, ZOOM_MIN
from ..widgets import card, label

PAD_W, PAD_H = 320, 180


class FramePad(QWidget):
    """The whole camera frame as a small 16:9 box. The lit rectangle is what OBS shows; drag it to pan and tilt."""
    moved = Signal(float, float)
    zoomed = Signal(float)
    reset = Signal()

    def __init__(self, eptz, accent="#7c5cff"):
        super().__init__()
        self.eptz = eptz
        self.accent = QColor(accent)
        self.setFixedSize(PAD_W, PAD_H)
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag to pan and tilt, scroll to zoom, double click to reset")

    def sizeHint(self):
        return QSize(PAD_W, PAD_H)

    def window_rect(self):
        fw, fh = self.eptz.window_fraction()
        w, h = fw * PAD_W, fh * PAD_H
        x = (PAD_W - w) * (1 + self.eptz.pan) / 2
        y = (PAD_H - h) * (1 + self.eptz.tilt) / 2
        return QRectF(x, y, w, h)

    def _from_point(self, pt):
        fw, fh = self.eptz.window_fraction()
        w, h = fw * PAD_W, fh * PAD_H
        ax, ay = PAD_W - w, PAD_H - h
        left = min(max(pt.x() - w / 2, 0), ax)
        top = min(max(pt.y() - h / 2, 0), ay)
        pan = (2 * left / ax - 1) if ax > 0.5 else 0.0
        tilt = (2 * top / ay - 1) if ay > 0.5 else 0.0
        self.moved.emit(pan, tilt)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.setCursor(Qt.ClosedHandCursor)
            self._from_point(ev.position())

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            self._from_point(ev.position())

    def mouseReleaseEvent(self, _):
        self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, _):
        self.reset.emit()

    def wheelEvent(self, ev):
        step = 0.1 if ev.angleDelta().y() > 0 else -0.1
        self.zoomed.emit(self.eptz.zoom + step)
        ev.accept()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#2f3542"), 1))
        p.setBrush(QColor("#161a22"))
        p.drawRoundedRect(QRectF(0.5, 0.5, PAD_W - 1, PAD_H - 1), 8, 8)
        p.setPen(QPen(QColor("#232937"), 1))
        for i in (1, 2):
            p.drawLine(QPointF(PAD_W * i / 3, 6), QPointF(PAD_W * i / 3, PAD_H - 6))
            p.drawLine(QPointF(6, PAD_H * i / 3), QPointF(PAD_W - 6, PAD_H * i / 3))
        r = self.window_rect()
        fill = QColor(self.accent)
        fill.setAlpha(70 if self.eptz.ready else 25)
        p.setPen(QPen(self.accent if self.eptz.ready else QColor("#3a4152"), 2))
        p.setBrush(fill)
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 5, 5)


class CameraModeCard(QWidget):
    """The one place the camera mode is chosen. Stream is 1080p60, Quality is 4K30 downscaled by OBS.
    When OBS is live it asks for a second click instead of switching."""

    def __init__(self, eptz, accent=None):
        super().__init__()
        self.eptz = eptz
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame, lay = card(spacing=8)
        outer.addWidget(frame)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label("Camera mode", "h2"))
        row.addStretch(1)
        self.confirm = QPushButton("Switch anyway")
        self.confirm.hide()
        self.confirm.clicked.connect(self._confirm)
        row.addWidget(self.confirm)
        self.buttons = {}
        for name, detail in MODE_DETAIL.items():
            b = QPushButton(f"{name} ({detail})")
            b.setObjectName("seg")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=name: self._clicked(n))
            self.buttons[name] = b
            row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(label("Stream is 1080p60 with the facecam crop as it is. Quality reads the camera at 4K30 "
                            "with the crop scaled to match, so the facecam box shows the same framing but OBS "
                            "downscales it from 4K and it comes out sharper.", "small", wrap=True))
        self.warn = label(QUALITY_NOTE, "small", wrap=True)
        lay.addWidget(self.warn)
        self._asked = None
        eptz.mode_changed.connect(self.sync)
        eptz.confirm_needed.connect(self._need_confirm)
        self.sync()

    def sync(self):
        e = self.eptz
        self.confirm.hide()
        self._asked = None
        usable = e.item_id is not None or e.fourk is not None
        for name, b in self.buttons.items():
            b.setChecked(e.mode == name)
            b.setEnabled(usable)
        self.warn.setVisible(e.mode == MODE_QUALITY)

    def _clicked(self, name):
        if self.eptz.set_4k(name == MODE_QUALITY) != "started":
            self.sync_buttons()

    def sync_buttons(self):
        for name, b in self.buttons.items():
            b.setChecked(self.eptz.mode == name)

    def _need_confirm(self, on):
        self._asked = on
        self.sync_buttons()
        self.confirm.setText("Switch to Quality anyway" if on else "Switch to Stream anyway")
        self.confirm.show()

    def _confirm(self):
        on = self._asked
        self.confirm.hide()
        if on is not None:
            self.eptz.set_4k(on, force=True)
            self._asked = None


class FramingCard(QWidget):
    def __init__(self, ctx, camera_zoom=None):
        super().__init__()
        self.ctx, self.eptz = ctx, ctx.eptz
        self.camera_zoom = camera_zoom or (lambda: 1.0)
        accent = ctx.config.get("accent")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame, lay = card(spacing=12)
        outer.addWidget(frame)

        head = QHBoxLayout()
        head.addWidget(label("Framing in OBS", "h2"))
        self.status = label("", "small")
        head.addWidget(self.status)
        head.addStretch(1)
        self.mode = label("", "muted")
        head.addWidget(self.mode)
        self.total = label("", "muted")
        head.addWidget(self.total)
        lay.addLayout(head)
        lay.addWidget(label("The camera cannot pan or tilt, so Lark moves the crop of the Camera item in OBS instead. "
                            "It works at any camera zoom.", "small", wrap=True))

        body = QHBoxLayout()
        body.setSpacing(24)
        self.pad = FramePad(self.eptz, accent)
        self.pad.moved.connect(lambda x, y: self.eptz.set_framing(pan=x, tilt=y, animate=False))
        self.pad.zoomed.connect(lambda z: self.eptz.set_framing(zoom=z, animate=False))
        self.pad.reset.connect(self.eptz.reset)
        body.addWidget(self.pad, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(6)
        self.sliders, self.vals = {}, {}
        for key, name, lo, hi in (("zoom", "Zoom", int(ZOOM_MIN * 100), int(ZOOM_MAX * 100)),
                                  ("pan", "Pan", -100, 100), ("tilt", "Tilt", -100, 100)):
            row = QHBoxLayout()
            n = label(name)
            n.setFixedWidth(60)
            row.addWidget(n)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.valueChanged.connect(lambda v, k=key: self._slid(k, v))
            row.addWidget(sl, 1)
            val = label("", "muted")
            val.setFixedWidth(52)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(val)
            self.sliders[key], self.vals[key] = sl, val
            col.addLayout(row)
        self.presets_row = QHBoxLayout()
        self.presets_row.setSpacing(8)
        col.addLayout(self.presets_row)
        save = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name it")
        self.name.setMaxLength(24)
        self.name.setMaximumWidth(180)
        self.name.returnPressed.connect(self._save)
        save.addWidget(self.name)
        sv = QPushButton("Save framing")
        sv.clicked.connect(self._save)
        save.addWidget(sv)
        rs = QPushButton("Reset")
        rs.clicked.connect(self.eptz.reset)
        save.addWidget(rs)
        save.addStretch(1)
        col.addLayout(save)
        col.addStretch(1)
        body.addLayout(col, 1)
        lay.addLayout(body)

        self.eptz.changed.connect(self.sync)
        self.eptz.mode_changed.connect(self.sync)
        self._preset_names = None
        self.sync()

    def _slid(self, key, v):
        if key == "zoom":
            self.eptz.set_framing(zoom=v / 100, animate=False)
        else:
            self.eptz.set_framing(**{key: v / 100}, animate=False)

    def _save(self):
        if self.eptz.save_preset(self.name.text()):
            self.name.clear()

    def sync(self):
        e = self.eptz
        vals = {"zoom": round(e.zoom * 100), "pan": round(e.pan * 100), "tilt": round(e.tilt * 100)}
        for k, sl in self.sliders.items():
            if not sl.isSliderDown():
                sl.blockSignals(True)
                sl.setValue(vals[k])
                sl.blockSignals(False)
            sl.setEnabled(e.ready)
        self.vals["zoom"].setText(f"{e.zoom:.2f}x")
        self.vals["pan"].setText(f"{vals['pan']:+d}")
        self.vals["tilt"].setText(f"{vals['tilt']:+d}")
        self.status.setText("" if e.status == "Ready" else e.status)
        self.mode.setText(f"Camera mode: {e.mode_text()}" if e.mode else "")
        self.update_total()
        names = list(e.presets())
        if names != self._preset_names:
            self._preset_names = names
            self._fill_presets(names)
        self.pad.update()

    def update_total(self):
        cam = self.camera_zoom()
        self.total.setText(f"Total zoom {cam * self.eptz.zoom:.1f}x  (camera {cam:.1f}x, framing {self.eptz.zoom:.1f}x)")

    def _fill_presets(self, names):
        while self.presets_row.count():
            w = self.presets_row.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        for n in names:
            b = QPushButton(n)
            b.setToolTip(f"Frame as {n}. Right click for more.")
            b.clicked.connect(lambda _=False, n=n: self.eptz.apply_preset(n))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, n=n, bb=b: self._menu(bb, pos, n))
            self.presets_row.addWidget(b)
        self.presets_row.addStretch(1)

    def _menu(self, btn, pos, name):
        m = QMenu(btn)
        over = m.addAction(f"Save the current framing as {name}")
        gone = m.addAction("Remove") if name in self.eptz.user_presets() else None
        act = m.exec(btn.mapToGlobal(pos))
        if act is over:
            self.eptz.save_preset(name)
        elif gone is not None and act is gone:
            self.eptz.delete_preset(name)


def camera_in_obs_card(ctx):
    """Settings page card: how long a framing change takes."""
    frame, lay = card(spacing=12)
    lay.addWidget(label("Framing in OBS", "h2"))
    row = QHBoxLayout()
    row.addWidget(label("Framing change takes", "muted"))
    spin = QSpinBox()
    spin.setRange(0, 3000)
    spin.setSingleStep(50)
    spin.setSuffix(" ms")
    spin.setSpecialValueText("Instant")
    spin.setValue(ctx.config.get("eptz_duration_ms"))

    def changed(v):
        ctx.config.set("eptz_duration_ms", v)
        try:
            ctx.config.save()
        except OSError as e:
            ctx.store.toast.emit(f"Could not write the config: {e.strerror}", True)
    spin.valueChanged.connect(changed)
    row.addWidget(spin)
    row.addStretch(1)
    lay.addLayout(row)
    return frame
