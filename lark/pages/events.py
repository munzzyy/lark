import time

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QTextBrowser

from ..events import LABELS, TYPES, alert_settings, render
from ..widgets import Page, Pill, card, label

MAX_ROWS = 300
PILL = {"connected": "good", "connecting": "warn", "error": "bad", "idle": ""}


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class EventsPage(Page):
    def __init__(self, ctx):
        super().__init__("Events", "Follows, subs, bits and raids as they happen. Test alerts use the same path.")
        self.ctx, self.svc, self.store = ctx, ctx.services, ctx.store
        self.pill = Pill("Idle")
        self.header.addWidget(self.pill)
        self.shown = set(TYPES)

        src, sl = card(spacing=10)
        sl.addWidget(label("Overlays", "h2"))
        sl.addWidget(label("Add the alert box and the recent events list to OBS. Open the alert URL with ?test=1 "
                           "to see a looping sample while you position it.", "muted", wrap=True))
        self.sources = []
        for title, path, w, h, name in (("Alerts", "/alerts", 1280, 720, "Lark alerts"),
                                        ("Recent events", "/events", 600, 500, "Lark events")):
            row = QHBoxLayout()
            row.setSpacing(10)
            lab = label(title, "muted")
            lab.setMinimumWidth(110)
            url = QLineEdit()
            url.setReadOnly(True)
            copy = QPushButton("Copy URL")
            add = QPushButton("Add to OBS")
            add.setObjectName("primary")
            copy.clicked.connect(lambda _=False, u=url: self._copy(u.text()))
            add.clicked.connect(lambda _=False, u=url, n=name, w=w, h=h: self.store.add_browser_source(n, u.text(), w, h))
            row.addWidget(lab)
            row.addWidget(url, 1)
            row.addWidget(copy)
            row.addWidget(add)
            sl.addLayout(row)
            self.sources.append((path, url, add))
        self.body.addWidget(src)

        tst, tl = card(QGridLayout, spacing=8)
        tl.addWidget(label("Test alerts", "h2"), 0, 0, 1, 3)
        self.test_buttons = {}
        for i, kind in enumerate(TYPES):
            b = QPushButton(f"Test {LABELS[kind].lower()}")
            b.clicked.connect(lambda _=False, k=kind: self.svc.test_alert(k))
            self.test_buttons[kind] = b
            tl.addWidget(b, 1 + i // 3, i % 3)
        tl.addWidget(label("A test shows in the list below and the alert box, and is never written to the event log.",
                           "small"), 5, 0, 1, 3)
        self.body.addWidget(tst)

        lst, ll = card(spacing=10)
        ll.addWidget(label("Recent events", "h2"))
        frow = QHBoxLayout()
        frow.setSpacing(6)
        self.filters = {}
        for kind in TYPES:
            b = QPushButton(LABELS[kind])
            b.setCheckable(True)
            b.setChecked(True)
            b.setStyleSheet(f"QPushButton:checked {{ border-color: {ctx.config.get('accent')}; }} "
                            "QPushButton:!checked { color: #5a6172; }")
            b.toggled.connect(lambda on, k=kind: self._filter(k, on))
            self.filters[kind] = b
            frow.addWidget(b)
        frow.addStretch(1)
        ll.addLayout(frow)
        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.setMinimumHeight(300)
        self.log.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        ll.addWidget(self.log, 1)
        self.body.addWidget(lst, 1)

        self.events = list(self.svc.alerts.log.recent())
        self._render()
        self.svc.event_added.connect(self._added)
        self.svc.eventsub_state.connect(self._state)
        self._state(*self.svc.eventsub_status)
        self.svc.overlay_state.connect(lambda *_: self._urls())
        self._urls()

    def _copy(self, text):
        if text:
            QGuiApplication.clipboard().setText(text)
            self.store.toast.emit("URL copied", False)

    def _urls(self):
        for path, url, add in self.sources:
            u = self.svc.url(path)
            url.setText(u or "")
            add.setEnabled(bool(u))

    def on_show(self):
        self._urls()

    def _state(self, status, detail):
        text = {"connected": "Listening", "connecting": "Connecting", "error": "Retrying", "idle": "Idle"}.get(status, status)
        self.pill.set_state(PILL.get(status, ""), text)
        self.pill.setToolTip(detail)

    def _filter(self, kind, on):
        (self.shown.add if on else self.shown.discard)(kind)
        self._render()

    def _added(self, e):
        self.events.append(e)
        del self.events[:-MAX_ROWS]
        if e["type"] not in self.shown:
            return
        if self._empty:
            self._render()
        else:
            sb = self.log.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 60
            self.log.append(self._line(e))
            if at_bottom:
                sb.setValue(sb.maximum())

    def _render(self):
        self.log.clear()
        rows = [self._line(e) for e in self.events if e["type"] in self.shown]
        self._empty = not rows
        if rows:
            self.log.setHtml("<br>".join(rows))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        else:
            self.log.setHtml('<span style="color:#5a6172">Nothing here yet. Press a test button to see how it looks.</span>')

    def _line(self, e):
        text = render(alert_settings(self.ctx.config.values, e["type"])["template"], e)
        ts = time.strftime("%H:%M", time.localtime(e["ts"]))
        tag = ' <i style="color:#8a92a3">(test)</i>' if e["test"] else ""
        return (f'<span style="color:#5a6172">{ts}</span>&nbsp; <b style="color:#b9a8ff">{esc(LABELS[e["type"]])}</b>'
                f'&nbsp; {esc(text)}{tag}')
