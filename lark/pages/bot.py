import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLineEdit,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from .. import icons
from ..botruntime import BotRuntime
from ..chatbot import BUILTINS
from ..twitch_api import MOD_SCOPES
from ..widgets import Page, Pill, Toggle, card, divider, label

WHO = [("everyone", "Everyone"), ("subscriber", "Subscribers"), ("mod", "Mods"), ("broadcaster", "Only me")]
ACTION = [("delete", "Delete message"), ("timeout", "Time out"), ("warn", "Warn")]
BUILTIN_HELP = {
    "uptime": "Says how long the stream has been live", "title": "Says the stream title",
    "game": "Says the current category", "commands": "Lists the commands the person can use",
    "clip": "Saves the OBS replay buffer", "permit": "!permit <user> lets them post links for a minute",
}
EDITABLE_RESPONSE = ("lurk", "so")
KIND_COLOR = {"say": "#e8eaf0", "mod": "#ffb454", "error": "#ff5d6c", "note": "#8a92a3", "clip": "#3ddc97"}
TABLE_CSS = ("QTableWidget { background: #1d212a; border: 1px solid #2a2f3b; border-radius: 8px; gridline-color: #2a2f3b; }"
             "QHeaderView::section { background: #161920; color: #8a92a3; border: none; padding: 6px; font-weight: 600; }"
             "QTableWidget::item { padding: 4px; }"
             "QTableWidget QComboBox { padding: 2px 8px; min-height: 26px; margin: 3px 4px; }"
             "QTableWidget::item:selected { background: #2a2f3b; color: #e8eaf0; }")


def _bot_icon(p):
    p.drawRoundedRect(QRectF(3.5, 6, 13, 10), 3, 3)
    p.drawLine(QPointF(10, 6), QPointF(10, 3))
    p.drawEllipse(QPointF(10, 2.6), 1, 1)
    p.drawPoint(QPointF(7.2, 10.5))
    p.drawPoint(QPointF(12.8, 10.5))
    p.drawLine(QPointF(7.5, 13.2), QPointF(12.5, 13.2))


icons.DRAW["bot"] = _bot_icon


def _num(text, default):
    try:
        return int(float(str(text).strip()))
    except ValueError:
        return default


def _combo(options, current):
    c = QComboBox()
    for key, name in options:
        c.addItem(name, key)
    keys = [k for k, _ in options]
    c.setCurrentIndex(keys.index(current) if current in keys else 0)
    return c


def _spin(lo, hi, value, suffix=""):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setMinimumWidth(90)
    return s


def _table(headers, stretch, height):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(40)
    t.setStyleSheet(TABLE_CSS)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setMinimumHeight(height)
    t.setShowGrid(True)
    h = t.horizontalHeader()
    for i in range(len(headers)):
        h.setSectionResizeMode(i, QHeaderView.Stretch if i == stretch else QHeaderView.ResizeToContents)
    return t


def _text_item(text, editable=True):
    it = QTableWidgetItem(str(text))
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        it.setForeground(Qt.gray)
    return it


def _check_item(on):
    it = QTableWidgetItem()
    it.setFlags((it.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
    it.setCheckState(Qt.Checked if on else Qt.Unchecked)
    return it


class BotPage(Page):
    def __init__(self, ctx):
        super().__init__("Bot", "Commands, timers and moderation for your chat. Saved to ~/.config/lark/bot.json.")
        self.ctx = ctx
        self.rt = ctx.bot = BotRuntime(ctx)
        self._loading = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.save)

        self.pill = Pill("Off")
        self.header.addWidget(self.pill)
        self.enabled = Toggle()
        self.header.addSpacing(8)
        self.header.addWidget(self.enabled)

        self.notice = label("", "muted", wrap=True)
        self.body.addWidget(self.notice)

        f, lay = card(spacing=10)
        lay.addWidget(label("Commands", "h2"))
        lay.addWidget(label("Type !name in chat. {user} is whoever said it, {touser} the first name they typed after it "
                            "(or them), {count} how many times it ran. Cooldowns are in seconds.", "small", wrap=True))
        self.cmds = _table(["On", "Command", "Response", "Who", "Cooldown", "Per person", "Aliases"], 2, 190)
        lay.addWidget(self.cmds)
        row = QHBoxLayout()
        add = QPushButton("Add command")
        add.clicked.connect(lambda: self._add_command())
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self._remove(self.cmds))
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(divider())
        lay.addWidget(label("Built in", "h2"))
        self.builtins = _table(["On", "Command", "What it does", "Who", "Cooldown", "Per person"], 2, 372)
        lay.addWidget(self.builtins)
        self.body.addWidget(f)

        f, lay = card(spacing=10)
        lay.addWidget(label("Timers", "h2"))
        lay.addWidget(label("A timer posts only after its interval has passed and enough other people have chatted since "
                            "its last post, so it never talks to an empty room.", "small", wrap=True))
        self.timers = _table(["On", "Name", "Message", "Every (min)", "Chat lines between"], 2, 150)
        lay.addWidget(self.timers)
        row = QHBoxLayout()
        add = QPushButton("Add timer")
        add.clicked.connect(lambda: self._add_timer())
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self._remove(self.timers))
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lay.addLayout(row)
        self.body.addWidget(f)

        self._build_moderation()

        f, lay = card(spacing=10)
        lay.addWidget(label("What the bot did", "h2"))
        self.log = QTextBrowser()
        self.log.setMinimumHeight(200)
        self.log.document().setMaximumBlockCount(300)
        self.log.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        lay.addWidget(self.log)
        self.body.addWidget(f)

        for t in (self.cmds, self.builtins, self.timers):
            t.itemChanged.connect(self._changed)
        self.enabled.toggled.connect(self._changed)
        self.rt.entry.connect(self._entry)
        ctx.services.twitch_changed.connect(lambda _st: self._status())
        for e in list(self.rt.bot.history):
            self._entry(e)
        self.load(self.rt.bot.data)
        self._loading = False
        self._status()

    # moderation card

    def _build_moderation(self):
        f, lay = card(spacing=10)
        head = QHBoxLayout()
        head.addWidget(label("Moderation", "h2"))
        head.addStretch(1)
        self.mod_on = Toggle()
        head.addWidget(self.mod_on)
        lay.addLayout(head)
        lay.addWidget(label("Works while your account is a mod or the broadcaster in the channel and Twitch is connected. "
                            "Mods and the broadcaster are never touched.", "small", wrap=True))
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)
        self.rules = {}
        specs = [("links", "Links", "Blocks links unless the site is on the allow list or you !permit the person."),
                 ("caps", "Caps", "Too many capital letters in a message."),
                 ("emotes", "Emote spam", "More emotes than the limit in one message."),
                 ("terms", "Blocked terms", "Anything in the two lists below."),
                 ("contacts", "Emails and phone numbers", "Keeps contact details out of chat.")]
        for r, (key, title, hint) in enumerate(specs):
            on = Toggle()
            action = _combo(ACTION, "delete")
            secs = _spin(1, 1209600, 10, " s")
            action.currentIndexChanged.connect(lambda _=0, a=action, s=secs: s.setEnabled(a.currentData() == "timeout"))
            name = QVBoxLayout()
            name.setSpacing(0)
            name.addWidget(label(title))
            name.addWidget(label(hint, "small"))
            holder = QWidget()
            holder.setLayout(name)
            g.addWidget(on, r, 0)
            g.addWidget(holder, r, 1)
            g.addWidget(action, r, 2)
            g.addWidget(secs, r, 3)
            self.rules[key] = {"on": on, "action": action, "seconds": secs}
            on.toggled.connect(self._changed)
            action.currentIndexChanged.connect(self._changed)
            secs.valueChanged.connect(self._changed)
        g.setColumnStretch(1, 1)
        lay.addLayout(g)

        self.allow = QLineEdit()
        self.allow.setPlaceholderText("twitch.tv, youtube.com")
        self.permit = _spin(5, 3600, 60, " s")
        self.caps_min = _spin(1, 500, 12)
        self.caps_pct = _spin(10, 100, 70, " %")
        self.emote_max = _spin(1, 100, 15)
        self.exempt_subs = Toggle()
        extra = QGridLayout()
        extra.setHorizontalSpacing(12)
        extra.setVerticalSpacing(8)
        rows = [("Allowed sites", self.allow), ("!permit lasts", self.permit),
                ("Caps start at this many letters", self.caps_min), ("Caps percent that trips it", self.caps_pct),
                ("Most emotes in a message", self.emote_max), ("Subscribers are exempt too", self.exempt_subs)]
        for i, (t, w) in enumerate(rows):
            extra.addWidget(label(t, "muted"), i, 0)
            extra.addWidget(w, i, 1, Qt.AlignLeft)
            if isinstance(w, QLineEdit):
                w.setMinimumWidth(320)
                w.textChanged.connect(self._changed)
            elif isinstance(w, Toggle):
                w.toggled.connect(self._changed)
            else:
                w.valueChanged.connect(self._changed)
        extra.setColumnMinimumWidth(0, 230)
        extra.setColumnStretch(2, 1)
        lay.addWidget(divider())
        lay.addLayout(extra)

        lay.addWidget(divider())
        lists = QHBoxLayout()
        lists.setSpacing(16)
        self.blocked = QPlainTextEdit()
        self.blocked.setPlaceholderText("One word or phrase per line")
        self.personal = QPlainTextEdit()
        self.personal.setPlaceholderText("Your real name, school, town: one per line. Stays on this computer.")
        for title, box in (("Blocked terms", self.blocked), ("Never say in chat (private)", self.personal)):
            col = QVBoxLayout()
            col.addWidget(label(title, "muted"))
            box.setMinimumHeight(110)
            box.textChanged.connect(self._changed)
            col.addWidget(box)
            lists.addLayout(col, 1)
        lay.addLayout(lists)
        self.mod_on.toggled.connect(self._changed)
        self.body.addWidget(f)

    # table rows

    def _add_command(self, c=None):
        c = c or {"name": "", "response": "", "permission": "everyone", "cooldown": 5, "user_cooldown": 15,
                  "enabled": True, "aliases": []}
        t = self.cmds
        was = self._loading
        self._loading = True
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, _check_item(c["enabled"]))
        t.setItem(r, 1, _text_item(c["name"]))
        t.setItem(r, 2, _text_item(c["response"]))
        who = _combo(WHO, c["permission"])
        who.currentIndexChanged.connect(self._changed)
        t.setCellWidget(r, 3, who)
        t.setItem(r, 4, _text_item(c["cooldown"]))
        t.setItem(r, 5, _text_item(c["user_cooldown"]))
        t.setItem(r, 6, _text_item(", ".join(c["aliases"])))
        self._loading = was
        if not was:
            t.editItem(t.item(r, 1))
            self._changed()

    def _add_timer(self, tm=None):
        tm = tm or {"name": "", "message": "", "interval": 900, "min_lines": 5, "enabled": True}
        t = self.timers
        was = self._loading
        self._loading = True
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, _check_item(tm["enabled"]))
        t.setItem(r, 1, _text_item(tm["name"]))
        t.setItem(r, 2, _text_item(tm["message"]))
        t.setItem(r, 3, _text_item(f"{tm['interval'] / 60:g}"))
        t.setItem(r, 4, _text_item(tm["min_lines"]))
        self._loading = was
        if not was:
            t.editItem(t.item(r, 1))
            self._changed()

    def _remove(self, table):
        r = table.currentRow()
        if r >= 0:
            table.removeRow(r)
            self._changed()

    def _fill_builtins(self, builtins):
        t = self.builtins
        t.setRowCount(0)
        for name in BUILTINS:
            b = builtins[name]
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, _check_item(b["enabled"]))
            t.setItem(r, 1, _text_item(name, False))
            if name in EDITABLE_RESPONSE:
                t.setItem(r, 2, _text_item(b["response"]))
            else:
                t.setItem(r, 2, _text_item(BUILTIN_HELP[name], False))
            who = _combo(WHO, b["permission"])
            who.currentIndexChanged.connect(self._changed)
            t.setCellWidget(r, 3, who)
            t.setItem(r, 4, _text_item(b["cooldown"]))
            t.setItem(r, 5, _text_item(b["user_cooldown"]))

    # data in and out

    def load(self, d):
        self._loading = True
        self.enabled.set_checked_silent(d["enabled"])
        self.cmds.setRowCount(0)
        for c in d["commands"]:
            self._add_command(c)
        self.timers.setRowCount(0)
        for tm in d["timers"]:
            self._add_timer(tm)
        self._fill_builtins(d["builtins"])
        m = d["mod"]
        self.mod_on.set_checked_silent(m["enabled"])
        self.exempt_subs.set_checked_silent(m["exempt_subs"])
        for key, w in self.rules.items():
            cfg = m[key]
            w["on"].set_checked_silent(cfg["on"])
            w["action"].setCurrentIndex([k for k, _ in ACTION].index(cfg["action"]))
            w["seconds"].setValue(cfg["seconds"])
            w["seconds"].setEnabled(cfg["action"] == "timeout")
        self.allow.setText(", ".join(m["links"]["allow"]))
        self.permit.setValue(m["links"]["permit_seconds"])
        self.caps_min.setValue(m["caps"]["min_letters"])
        self.caps_pct.setValue(m["caps"]["percent"])
        self.emote_max.setValue(m["emotes"]["max"])
        self.blocked.setPlainText("\n".join(m["terms"]["blocked"]))
        self.personal.setPlainText("\n".join(m["terms"]["personal"]))
        self._loading = False

    def _cell(self, t, r, c):
        it = t.item(r, c)
        return it.text().strip() if it else ""

    def collect(self):
        who = lambda t, r: t.cellWidget(r, 3).currentData()
        on = lambda t, r: t.item(r, 0).checkState() == Qt.Checked
        cmds = [{"name": self._cell(self.cmds, r, 1), "response": self._cell(self.cmds, r, 2),
                 "enabled": on(self.cmds, r), "permission": who(self.cmds, r),
                 "cooldown": _num(self._cell(self.cmds, r, 4), 5), "user_cooldown": _num(self._cell(self.cmds, r, 5), 15),
                 "aliases": [a.strip() for a in self._cell(self.cmds, r, 6).split(",") if a.strip()]}
                for r in range(self.cmds.rowCount())]
        timers = []
        for r in range(self.timers.rowCount()):
            try:
                minutes = float(self._cell(self.timers, r, 3))
            except ValueError:
                minutes = 15.0
            timers.append({"name": self._cell(self.timers, r, 1), "message": self._cell(self.timers, r, 2),
                           "enabled": on(self.timers, r), "interval": round(minutes * 60),
                           "min_lines": _num(self._cell(self.timers, r, 4), 5)})
        builtins = {}
        for r in range(self.builtins.rowCount()):
            name = self._cell(self.builtins, r, 1)
            b = {"enabled": on(self.builtins, r), "permission": who(self.builtins, r),
                 "cooldown": _num(self._cell(self.builtins, r, 4), 5),
                 "user_cooldown": _num(self._cell(self.builtins, r, 5), 15)}
            if name in EDITABLE_RESPONSE:
                b["response"] = self._cell(self.builtins, r, 2)
            builtins[name] = b
        rule = {k: {"on": w["on"].isChecked(), "action": w["action"].currentData(), "seconds": w["seconds"].value()}
                for k, w in self.rules.items()}
        rule["links"].update(allow=[s.strip() for s in self.allow.text().split(",") if s.strip()],
                             permit_seconds=self.permit.value())
        rule["caps"].update(min_letters=self.caps_min.value(), percent=self.caps_pct.value())
        rule["emotes"]["max"] = self.emote_max.value()
        rule["terms"].update(blocked=self.blocked.toPlainText().splitlines(),
                             personal=self.personal.toPlainText().splitlines())
        return {"enabled": self.enabled.isChecked(), "commands": cmds, "timers": timers, "builtins": builtins,
                "mod": {"enabled": self.mod_on.isChecked(), "exempt_subs": self.exempt_subs.isChecked(), **rule}}

    def _changed(self, *_):
        if not self._loading:
            self._save_timer.start()

    def save(self):
        self._save_timer.stop()
        try:
            self.rt.update(self.collect())
        except OSError as e:
            self.ctx.store.toast.emit(f"Could not save the bot settings: {e.strerror or e}", True)
        self._status()

    # status and log

    def _status(self):
        st = self.ctx.services.twitch_state
        on = self.enabled.isChecked()
        signed = st["status"] == "connected"
        can_talk = signed and "chat:edit" in st["scopes"]
        if not on:
            self.pill.set_state("", "Bot off")
        elif can_talk:
            self.pill.set_state("good", "Bot running")
        else:
            self.pill.set_state("warn", "Needs Twitch")
        notes = []
        if not signed:
            notes.append("The bot does nothing until you connect Twitch in Settings. Until then it won't talk or moderate.")
        elif self.mod_on.isChecked() and not set(MOD_SCOPES) <= set(st["scopes"]):
            notes.append("This Twitch sign-in was made before moderation existed. Disconnect and connect Twitch again "
                         "in Settings to give Lark moderator permissions.")
        self.notice.setText(" ".join(notes))
        self.notice.setVisible(bool(notes))

    def _entry(self, e):
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        color = KIND_COLOR.get(e["kind"], "#e8eaf0")
        text = e["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log.append(f'<span style="color:#5a6172">{ts}</span>&nbsp; <span style="color:{color}">{text}</span>')

    def on_show(self):
        self._status()
