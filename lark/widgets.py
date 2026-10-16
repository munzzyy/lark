"""Small reusable widgets shared by the pages."""
import time
from collections import deque

from PySide6.QtCore import Property, QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QPushButton, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)


def label(text="", name=None, wrap=False):
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def card(layout_cls=QVBoxLayout, margins=(20, 18, 20, 18), spacing=12):
    f = QFrame()
    f.setObjectName("card")
    lay = layout_cls(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


def divider():
    d = QFrame()
    d.setObjectName("divider")
    d.setFrameShape(QFrame.NoFrame)
    return d


def scroll(inner):
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setFrameShape(QFrame.NoFrame)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    s.setWidget(inner)
    return s


class Pill(QLabel):
    def __init__(self, text="", state=""):
        super().__init__(text)
        self.setObjectName("pill")
        self.set_state(state)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    def set_state(self, state, text=None):
        if text is not None:
            self.setText(text)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)


class Page(QWidget):
    """A page with a title row and a scrolling body. Pages add to self.body."""

    def __init__(self, title, subtitle=""):
        super().__init__()
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 20, 32, 0)
        outer.setSpacing(4)
        self.header = QHBoxLayout()
        self.header.addWidget(label(title, "h1"))
        self.header.addStretch(1)
        outer.addLayout(self.header)
        if subtitle:
            outer.addWidget(label(subtitle, "muted"))
        outer.addSpacing(10)
        inner = QWidget()
        inner.setObjectName("page")
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(0, 0, 8, 28)
        self.body.setSpacing(16)
        outer.addWidget(scroll(inner), 1)

    def on_show(self):
        pass


class Toggle(QAbstractButton):
    """A small on/off switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 22)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"pos_", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)
        self.accent = QColor("#7c5cff")

    def _animate(self, on):
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def set_checked_silent(self, on):
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def get_pos(self):
        return self._pos

    def set_pos(self, v):
        self._pos = v
        self.update()

    pos_ = Property(float, get_pos, set_pos)

    def sizeHint(self):
        return QSize(40, 22)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor("#2f3542"), QColor(self.property("accent") or "#7c5cff")
        t = self._pos
        col = QColor(round(off.red() + (on.red() - off.red()) * t), round(off.green() + (on.green() - off.green()) * t),
                     round(off.blue() + (on.blue() - off.blue()) * t))
        if not self.isEnabled():
            col.setAlphaF(0.4)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0, 0, 40, 22), 11, 11)
        p.setBrush(QColor("#f2f4f8"))
        p.drawEllipse(QPointF(11 + 18 * t, 11), 8, 8)


class MeterBar(QWidget):
    """Horizontal level meter, -60..0 dB, with a falling peak tick."""
    FLOOR = -60.0

    def __init__(self):
        super().__init__()
        self.setFixedHeight(12)
        self.setMinimumWidth(120)
        self.level = self.FLOOR
        self.peak = self.FLOOR
        self._peak_hold = 0
        self.muted = False
        self._decay = QTimer(self)
        self._decay.setInterval(40)
        self._decay.timeout.connect(self._fall)
        self._decay.start()

    def push(self, level_db, peak_db):
        lvl = max(self.FLOOR, level_db)
        self.level = lvl if lvl > self.level else self.level * 0.6 + lvl * 0.4
        if peak_db >= self.peak:
            self.peak = max(self.FLOOR, peak_db)
            self._peak_hold = 20
        self.update()

    def _fall(self):
        changed = False
        if self.level > self.FLOOR:
            self.level = max(self.FLOOR, self.level - 1.2)
            changed = True
        if self._peak_hold > 0:
            self._peak_hold -= 1
        elif self.peak > self.FLOOR:
            self.peak = max(self.FLOOR, self.peak - 0.8)
            changed = True
        if changed:
            self.update()

    def set_muted(self, m):
        self.muted = m
        self.update()

    def _x(self, db):
        return (max(self.FLOOR, min(0.0, db)) - self.FLOOR) / -self.FLOOR * self.width()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1d212a"))
        p.drawRoundedRect(r, 6, 6)
        w = self._x(self.level)
        if w > 1 and not self.muted:
            g = QLinearGradient(0, 0, self.width(), 0)
            g.setColorAt(0.0, QColor("#2fbf84"))
            g.setColorAt(0.70, QColor("#3ddc97"))
            g.setColorAt(0.85, QColor("#ffb454"))
            g.setColorAt(1.0, QColor("#ff5d6c"))
            p.setBrush(g)
            p.setClipRect(QRectF(0, 0, w, r.height()))
            p.drawRoundedRect(r, 6, 6)
            p.setClipping(False)
        if self.peak > self.FLOOR + 1 and not self.muted:
            x = min(self._x(self.peak), r.width() - 2)
            p.setPen(QPen(QColor("#e8eaf0"), 2))
            p.drawLine(QPointF(x, 1), QPointF(x, r.height() - 1))


class StatTile(QFrame):
    def __init__(self, title, value="--"):
        super().__init__()
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        self._title = label(title.upper(), "statlabel")
        self._value = label(value, "statvalue")
        lay.addWidget(self._title)
        lay.addWidget(self._value)

    def set(self, text, state=None):
        self._value.setText(text)
        color = {"good": "#3ddc97", "warn": "#ffb454", "bad": "#ff5d6c"}.get(state, "#e8eaf0")
        self._value.setStyleSheet(f"color: {color};")


class EmptyState(QFrame):
    """What a page shows when it has nothing: a title, one line of guidance and one action."""

    def __init__(self, title="", text="", action=None, on_action=None):
        super().__init__()
        self.setObjectName("empty")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 28, 28, 28)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignCenter)
        self.title_label = label("", "h2")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.text_label = label("", "muted", wrap=True)
        self.text_label.setAlignment(Qt.AlignCenter)
        self.button = QPushButton()
        self.button.setObjectName("primary")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(self._fire)
        self._cb = None
        for w in (self.title_label, self.text_label):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(self.button, 0, Qt.AlignCenter)
        self.set_content(title, text, action, on_action)

    def set_content(self, title, text="", action=None, on_action=None):
        self.title_label.setText(title)
        self.text_label.setText(text)
        self.text_label.setVisible(bool(text))
        self.button.setText(action or "")
        self.button.setVisible(bool(action))
        self._cb = on_action

    def text(self):
        return f"{self.title_label.text()} {self.text_label.text()}".strip()

    def _fire(self):
        if self._cb:
            self._cb()


class Toast(QWidget):
    """Non-blocking messages at the bottom of the window. They never take focus or clicks,
    dismiss themselves, and queue up when more than MAX_VISIBLE arrive at once."""
    MAX_VISIBLE = 3
    GOOD_S, BAD_S = 3.0, 5.0

    def __init__(self, parent, clock=time.monotonic):
        super().__init__(parent)
        self.setObjectName("toasts")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self._clock = clock
        self.items = []
        self.pending = deque()
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(8)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.tick)
        parent.installEventFilter(self)
        self.hide()

    def show_message(self, text, bad=False):
        text = str(text)
        for it in self.items:
            if it["text"] == text and it["bad"] == bool(bad):
                it["expires"] = self._clock() + (self.BAD_S if bad else self.GOOD_S)
                return
        if any(p == (text, bool(bad)) for p in self.pending):
            return
        self.pending.append((text, bool(bad)))
        self.tick()

    def tick(self):
        now = self._clock()
        for it in [i for i in self.items if i["expires"] <= now]:
            self.items.remove(it)
            self._lay.removeWidget(it["label"])
            it["label"].setParent(None)
            it["label"].deleteLater()
        while self.pending and len(self.items) < self.MAX_VISIBLE:
            text, bad = self.pending.popleft()
            lb = QLabel(text)
            lb.setObjectName("toast")
            lb.setWordWrap(True)
            lb.setFocusPolicy(Qt.NoFocus)
            lb.setProperty("bad", bad)
            self._lay.addWidget(lb)
            self.items.append({"text": text, "bad": bad, "label": lb,
                               "expires": now + (self.BAD_S if bad else self.GOOD_S)})
        if self.items:
            self._place()
            self.show()
            self.raise_()
            if not self._timer.isActive():
                self._timer.start()
        else:
            self.hide()
            self._timer.stop()

    def _place(self):
        par = self.parentWidget()
        self.setFixedWidth(min(560, max(300, par.width() - 120)))
        self.adjustSize()
        self.move((par.width() - self.width()) // 2, par.height() - self.height() - 24)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize and self.items:
            self._place()
        return False



class StickyToast(QFrame):
    """One message with an optional button that stays until it is hidden. Unlike Toast it takes clicks."""

    def __init__(self, parent, on_click):
        super().__init__(parent)
        self.setObjectName("sticky")
        self.key = None
        self._on_click = on_click
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 12, 10)
        lay.setSpacing(14)
        self.text = QLabel()
        self.text.setObjectName("stickytext")
        self.button = QPushButton()
        self.button.setObjectName("primary")
        self.button.clicked.connect(lambda: self._on_click(self.key))
        lay.addWidget(self.text, 1)
        lay.addWidget(self.button)
        parent.installEventFilter(self)
        self.hide()

    def show_item(self, key, text, button=""):
        self.key = key
        self.text.setText(text)
        self.button.setText(button)
        self.button.setVisible(bool(button))
        self.show()
        self._place()
        self.raise_()

    def hide_item(self, key):
        if key == self.key:
            self.key = None
            self.hide()

    def _place(self):
        par = self.parentWidget()
        self.setFixedWidth(min(520, max(300, par.width() - 120)))
        self.adjustSize()
        self.move(par.width() - self.width() - 28, par.height() - self.height() - 28)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize and self.isVisible():
            self._place()
        return False
