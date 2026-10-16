import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QTextBrowser

from ..widgets import EmptyState, Page, Pill, card, label

MAX_LINES = 500


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ChatPage(Page):
    def __init__(self, ctx):
        super().__init__("Chat", "")
        self.ctx, self.svc = ctx, ctx.services
        self.pill = Pill("Idle")
        self.header.addWidget(self.pill)
        f, lay = card(spacing=12)
        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.setMinimumHeight(420)
        self.log.document().setMaximumBlockCount(MAX_LINES)
        self.log.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        self.empty = EmptyState()
        self._seen = False
        lay.addWidget(self.empty, 1)
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.send = QLineEdit()
        self.send.setMaxLength(500)
        self.btn = QPushButton("Send")
        self.btn.setObjectName("primary")
        self.send.returnPressed.connect(self._send)
        self.btn.clicked.connect(self._send)
        row.addWidget(self.send, 1)
        row.addWidget(self.btn)
        lay.addLayout(row)
        self.body.addWidget(f, 1)
        self.svc.chat_message.connect(self._message)
        self.svc.chat_state.connect(self._state)
        self.svc.twitch_changed.connect(lambda st: self._can_send(st))
        self._can_send(self.svc.twitch_state)
        self._empty_state("idle", "")

    def _can_send(self, st):
        ok = st["status"] == "connected" and "chat:edit" in st["scopes"]
        self.send.setEnabled(ok)
        self.btn.setEnabled(ok)
        self.send.setPlaceholderText("Send a message" if ok else "Connect Twitch in Settings to send messages")

    def _send(self):
        text = self.send.text().strip()
        if not text:
            return
        self.send.clear()

        def done(ok, res):
            if not ok:
                self.ctx.store.toast.emit(f"Not sent: {res}", True)
        self.svc.send_chat(text, done)

    def _state(self, state, detail):
        label_ = {"connected": ("good", f"Reading #{detail}"), "connecting": ("warn", "Connecting"),
                  "disconnected": ("bad", "Reconnecting"), "idle": ("", detail)}.get(state, ("", state))
        self.pill.set_state(*label_)
        self.pill.setToolTip(detail)
        self._empty_state(state, detail)

    def _empty_state(self, state, detail):
        chan = self.ctx.config.get("twitch_channel")
        if not chan:
            self.empty.set_content("No channel set", "Type your Twitch channel in Settings to read chat here.",
                                   "Open Settings", lambda: self.ctx.goto("settings"))
        elif state == "connected":
            self.empty.set_content("Chat is quiet", f"Messages from #{chan} show up here as they arrive.")
        else:
            self.empty.set_content("Connecting to chat", f"Reading #{chan}. This needs no sign-in.")
        self.empty.setVisible(not self._seen)
        self.log.setVisible(self._seen)

    def _message(self, m):
        if not self._seen:
            self._seen = True
            self.empty.hide()
            self.log.show()
        ts = time.strftime("%H:%M", time.localtime(m["ts"]))
        color = m["color"] or "#8a92a3"
        body = f"<i>{esc(m['text'])}</i>" if m["action"] else esc(m["text"])
        sb = self.log.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 60
        self.log.append(f'<span style="color:#5a6172">{ts}</span>&nbsp; '
                        f'<b style="color:{color}">{esc(m["user"])}</b>&nbsp; {body}')
        if at_bottom:
            sb.setValue(sb.maximum())
