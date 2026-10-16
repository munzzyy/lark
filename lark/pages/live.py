from PySide6.QtCore import QTimer
from PySide6.QtCore import QStringListModel
from PySide6.QtWidgets import (QCompleter, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from PySide6.QtCore import Qt

from ..camfix import DarkFixer
from ..golive import EDIT_TITLE, FIX_DARK, LOAD_PRESET, OPEN, PICK_SCENE, UNMUTE, Countdown, fix_action
from ..onboarding import OnboardingCard
from ..preflight import FAIL, OK, SKIP, WARN, can_go_live, run_checks
from ..camera_backend import camera_preset
from ..highlights import manager_for
from ..widgets import Page, Pill, StatTile, card, divider, label

COUNTDOWN_S = 3
START_TIMEOUT_S = 15

DOT = {OK: "#3ddc97", WARN: "#ffb454", FAIL: "#ff5d6c", SKIP: "#4a5160"}
MARK = {OK: "Ready", WARN: "Check", FAIL: "Blocked", SKIP: "Skipped"}


def fmt_duration(ms):
    s = int(ms // 1000)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class CheckRow(QWidget):
    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(12)
        self.setMinimumHeight(38)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self.title = label("")
        self.title.setMinimumWidth(190)
        self.detail = label("", "muted")
        lay.addWidget(self.dot)
        lay.addWidget(self.title)
        lay.addWidget(self.detail, 1)
        self.fix = QPushButton()
        self.fix.setObjectName("fix")
        self.fix.setCursor(Qt.PointingHandCursor)
        self.fix.hide()
        lay.addWidget(self.fix)
        self.tag = label("", "small")
        self.tag.setMinimumWidth(52)
        self.tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.tag)
        self._handler = None
        self.fix.clicked.connect(lambda: self._handler() if self._handler else None)

    def set(self, c, fix=None, handler=None):
        self.dot.setStyleSheet(f"background: {DOT[c.status]}; border-radius: 5px;")
        self.title.setText(c.label)
        self.detail.setText(c.detail)
        self.tag.setText(MARK[c.status])
        self.fix.setVisible(fix is not None)
        if fix is not None:
            self.fix.setText(fix[0])
        self._handler = handler


class LivePage(Page):
    def __init__(self, ctx):
        super().__init__("Live", "Check everything, then go live.")
        self.ctx = ctx
        self.store = ctx.store
        self._stop_armed = False
        self._sticky_on = False
        self.fixer = DarkFixer(ctx)
        ctx.store.sticky_clicked.connect(lambda key: self.fixer.run() if key == "camdark" else None)
        self.status_pill = Pill("Offline")
        self.header.addWidget(self.status_pill)

        self.onboarding = OnboardingCard(ctx)
        self.body.addWidget(self.onboarding)

        hero, hl = card(spacing=14)
        self.hero = hero
        self.headline = label("Offline", "big")
        self.subline = label("Not connected to OBS", "muted")
        hl.addWidget(self.headline)
        hl.addWidget(self.subline)
        self.go = QPushButton("Go Live")
        self.go.setObjectName("golive")
        self.go.setMinimumHeight(68)
        self.go.setToolTip("Counts down 3 seconds, then starts the stream. Click again during it to cancel (Ctrl+G).")
        self.go.clicked.connect(self._go)
        self.stop = QPushButton("Stop stream")
        self.stop.setObjectName("stop")
        self.stop.setMinimumHeight(68)
        self.stop.setToolTip("Asks once: click it a second time within 3 seconds to end the stream (Ctrl+G)")
        self.stop.clicked.connect(self._stop)
        hl.addWidget(self.go)
        hl.addWidget(self.stop)
        self.body.addWidget(hero)

        pre, pl = card(spacing=4)
        pl.addWidget(label("Pre-flight", "h2"))
        pl.addWidget(label("Red blocks Go Live. Amber is a warning you can ignore.", "muted"))
        pl.addSpacing(4)
        self.rows = []
        for i in range(8):
            r = CheckRow()
            self.rows.append(r)
            pl.addWidget(r)
        self.body.addWidget(pre)

        self.twitch_card, tl = card(spacing=10)
        tl.addWidget(label("Twitch title and category", "h2"))
        trow = QGridLayout()
        trow.setSpacing(10)
        self.title_edit = QLineEdit()
        self.title_edit.setMaxLength(140)
        self.title_edit.setPlaceholderText("Stream title")
        self.cat_edit = QLineEdit()
        self.cat_edit.setPlaceholderText("Category, start typing to search")
        self.cat_model = QStringListModel()
        comp = QCompleter(self.cat_model, self.cat_edit)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        self.cat_edit.setCompleter(comp)
        self.cat_ids = {}
        self._cat_timer = QTimer(self)
        self._cat_timer.setSingleShot(True)
        self._cat_timer.setInterval(350)
        self._cat_timer.timeout.connect(self._search_cat)
        self.cat_edit.textEdited.connect(lambda _: self._cat_timer.start())
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("primary")
        self.update_btn.clicked.connect(self._update_channel)
        self.title_edit.returnPressed.connect(self._update_channel)
        trow.addWidget(label("Title", "muted"), 0, 0)
        trow.addWidget(self.title_edit, 0, 1)
        trow.addWidget(label("Category", "muted"), 1, 0)
        trow.addWidget(self.cat_edit, 1, 1)
        trow.addWidget(self.update_btn, 0, 2, 2, 1)
        trow.setColumnStretch(1, 1)
        tl.addLayout(trow)
        self.twitch_card.hide()
        self._channel_loaded = None
        self.body.addWidget(self.twitch_card)
        ctx.services.twitch_changed.connect(self._twitch)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.t_time = StatTile("Live time")
        self.t_fps = StatTile("FPS")
        self.t_drop = StatTile("Dropped")
        self.t_kbps = StatTile("Bitrate")
        self.t_cpu = StatTile("CPU")
        for i, t in enumerate((self.t_time, self.t_fps, self.t_drop, self.t_kbps, self.t_cpu)):
            grid.addWidget(t, 0, i)
            grid.setColumnStretch(i, 1)
        self.body.addLayout(grid)

        act, al = card(QHBoxLayout, spacing=12)
        self.rec = QPushButton("Record")
        self.rec.setToolTip("Record to a file in OBS, separate from the stream")
        self.rec.clicked.connect(self.store.toggle_record)
        self.replay = QPushButton("Save replay")
        self.replay.setToolTip("Save the last moments from OBS's replay buffer (Ctrl+Shift+R)")
        self.replay.clicked.connect(self.store.save_replay)
        self.mark = QPushButton("Mark")
        self.mark.setToolTip("Save the replay buffer and add a highlight")
        self.mark.clicked.connect(lambda: manager_for(ctx).mark("manual"))
        self.vcam = None
        self.actions_layout = al
        for b in (self.rec, self.replay, self.mark):
            b.setMinimumHeight(40)
            al.addWidget(b)
        al.addStretch(1)
        self.body.addWidget(act)
        self.body.addStretch(1)

        self.countdown = Countdown(COUNTDOWN_S, self)
        self.countdown.ticked.connect(lambda n: self.refresh())
        self.countdown.finished.connect(self._countdown_done)
        self.countdown.cancelled.connect(self.refresh)
        self._starting = False
        self._start_timeout = QTimer(self)
        self._start_timeout.setSingleShot(True)
        self._start_timeout.setInterval(START_TIMEOUT_S * 1000)
        self._start_timeout.timeout.connect(self._start_gave_up)
        self._announced_live = False

        self._disarm = QTimer(self)
        self._disarm.setSingleShot(True)
        self._disarm.timeout.connect(self._reset_stop)

        s = self.store
        for sig in (s.conn_changed, s.outputs_changed, s.scenes_changed, s.inputs_changed, s.stats_changed):
            sig.connect(lambda *a: self.refresh())
        s.input_updated.connect(lambda *a: self.refresh())
        s.cam_light_changed.connect(self.refresh)
        s.inputs_changed.connect(self.onboarding.refresh)
        s.conn_changed.connect(lambda *a: self.onboarding.refresh())
        ctx.services.twitch_changed.connect(lambda st: self.onboarding.refresh())
        self._meter_tick = QTimer(self)
        self._meter_tick.setInterval(1000)
        self._meter_tick.timeout.connect(self.refresh)
        self._meter_tick.start()
        self.refresh()

    def _twitch(self, st):
        on = st["status"] == "connected" and "channel:manage:broadcast" in st["scopes"]
        self.twitch_card.setVisible(on)
        ch = st["channel"]
        if on and ch and ch != self._channel_loaded:
            self._channel_loaded = ch
            if not self.title_edit.hasFocus():
                self.title_edit.setText(ch["title"])
            if not self.cat_edit.hasFocus():
                self.cat_edit.setText(ch["game_name"])
                self.cat_ids[ch["game_name"]] = ch["game_id"]
        self.refresh()

    def _search_cat(self):
        q = self.cat_edit.text().strip()
        if not q:
            return

        def got(ok, res):
            if ok:
                self.cat_ids.update({c["name"]: c["id"] for c in res})
                self.cat_model.setStringList([c["name"] for c in res])
                self.cat_edit.completer().complete()
        self.ctx.services.twitch_search(q, got)

    def _update_channel(self):
        title = self.title_edit.text()
        cat = self.cat_edit.text().strip()
        game_id = self.cat_ids.get(cat) if cat else None
        if cat and game_id is None:
            self.store.toast.emit("Pick the category from the list", True)
            return

        def done(ok, res):
            self.store.toast.emit("Twitch updated" if ok else f"Twitch: {res}", not ok)
        self.ctx.services.twitch_set_channel(title, game_id, done)

    def checks(self):
        s = self.store
        tw = self.ctx.services.twitch_state
        mic = self.ctx.config.get("mic_input")
        return run_checks(connected=s.connected, scene=s.current_scene, mic_name=mic, mic=s.inputs.get(mic),
                          mic_signal=s.has_signal(mic), camera_present=s.camera_present(),
                          stream_key_set=s.stream_key_set, twitch_connected=tw["status"] == "connected",
                          twitch_title=(tw["channel"] or {}).get("title", ""),
                          camera_preset=camera_preset(self.ctx),
                          camera_light=None if s.cam_luma is None else (s.cam_dark, s.cam_luma))

    def _go(self):
        if self.countdown.running:
            self.countdown.cancel()
            self.store.toast.emit("Go Live cancelled", False)
            return
        if self._starting or not self.go.isEnabled():
            return
        self.countdown.start()

    def _countdown_done(self):
        checks = self.checks()
        if not (self.store.connected and can_go_live(checks)):
            self.store.toast.emit("Not going live, pre-flight is blocking", True)
            self.refresh()
            return
        self._starting = True
        self._start_timeout.start()
        self.store.start_stream()
        self.refresh()

    def _start_gave_up(self):
        if self._starting:
            self._starting = False
            self.store.toast.emit("OBS did not start the stream, check OBS", True)
            self.refresh()

    def _fix(self, c):
        s = self.store
        mic = self.ctx.config.get("mic_input")
        fix = fix_action(c, mic_muted=bool((s.inputs.get(mic) or {}).get("muted")), scenes=s.scenes)
        if fix is None:
            return
        _, kind, arg = fix
        if kind == UNMUTE:
            s.set_mute(mic, False)
        elif kind == OPEN:
            self.ctx.goto(arg)
        elif kind == LOAD_PRESET:
            self.ctx.camera.apply({"preset": f"load_{self.ctx.config.get('camera_preflight_preset')}"})
        elif kind == FIX_DARK:
            self.fixer.run()
        elif kind == PICK_SCENE:
            menu = QMenu(self)
            for name in s.scenes:
                menu.addAction(name, lambda n=name: s.switch_scene(n))
            menu.exec(self.mapToGlobal(self.rect().center()))
        elif kind == EDIT_TITLE:
            if self.twitch_card.isHidden():
                self.ctx.goto("settings")
                return
            sc = self.findChild(QScrollArea)
            if sc is not None:
                sc.ensureWidgetVisible(self.twitch_card)
            self.title_edit.setFocus()
            self.title_edit.selectAll()

    def _stop(self):
        if not self._stop_armed:
            self._stop_armed = True
            self.stop.setText("Click again to end the stream")
            self._disarm.start(3000)
            return
        self._reset_stop()
        self.store.stop_stream()

    def _reset_stop(self):
        self._stop_armed = False
        self.stop.setText("Stop stream")

    def refresh(self):
        s = self.store
        checks = self.checks()
        mic_name = self.ctx.config.get("mic_input")
        muted = bool((s.inputs.get(mic_name) or {}).get("muted"))
        for row, c in zip(self.rows, checks):
            fix = fix_action(c, mic_muted=muted, scenes=s.scenes)
            row.set(c, fix, (lambda c=c: self._fix(c)) if fix else None)
        live = s.connected and s.stream["active"]
        ready = s.connected and can_go_live(checks)
        if self.countdown.running and not ready:
            self.countdown.cancel()
            self.store.toast.emit("Go Live cancelled, pre-flight changed", True)
        if live:
            if self._starting:
                self._starting = False
                self._start_timeout.stop()
            if not self._announced_live:
                self._announced_live = True
                self.store.toast.emit("You are live", False)
        else:
            self._announced_live = False
        want_sticky = bool(live and s.cam_dark)
        if want_sticky != self._sticky_on:
            self._sticky_on = want_sticky
            if want_sticky:
                s.sticky_show.emit("camdark", "Camera went dark", "Fix")
            else:
                s.sticky_hide.emit("camdark")
        counting = self.countdown.running
        self.go.setVisible(not live)
        self.stop.setVisible(live)
        self.go.setEnabled(ready and not self._starting)
        self.go.setProperty("counting", counting)
        self.go.style().unpolish(self.go)
        self.go.style().polish(self.go)
        if counting:
            self.go.setEnabled(True)
            self.go.setText(f"Going live in {self.countdown.left}   (click to cancel)")
        elif self._starting:
            self.go.setText("Starting the stream...")
        elif not ready:
            first = next((c for c in checks if c.status == FAIL), None)
            self.go.setText(first.detail or first.label if first else "Go Live")
        else:
            self.go.setText("Go Live")
        state = "live" if live else "starting" if (counting or self._starting) else ""
        if self.hero.property("stream") != state:
            self.hero.setProperty("stream", state)
            self.hero.style().unpolish(self.hero)
            self.hero.style().polish(self.hero)
        if live:
            reconnecting = s.stream["reconnecting"]
            self.headline.setText("Reconnecting" if reconnecting else "You are live")
            self.subline.setText(f"On air for {fmt_duration(s.stream['duration_ms'])}")
            self.status_pill.set_state("warn" if reconnecting else "bad", "LIVE")
        elif counting or self._starting:
            self.headline.setText("Going live" if counting else "Starting")
            self.subline.setText("Click the button to cancel" if counting else "Waiting for OBS to confirm")
            self.status_pill.set_state("warn", "Starting")
        else:
            self.headline.setText("Offline" if s.connected else "OBS is not running")
            self.subline.setText({"connected": f"Scene: {s.current_scene}", "connecting": "Connecting to OBS",
                                  "auth_failed": "OBS rejected the password, check Settings"}.get(
                s.conn, "Open OBS and Lark connects on its own"))
            self.status_pill.set_state("", "Offline")
        self._reset_stop() if not live else None

        self.t_time.set(fmt_duration(s.stream["duration_ms"]) if live else "--")
        fps = s.stats.get("activeFps")
        self.t_fps.set(f"{fps:.0f}" if fps else "--")
        if live:
            d = s.dropped_pct
            self.t_drop.set(f"{d:.1f}%", "good" if d < 1 else "warn" if d < 5 else "bad")
            self.t_kbps.set(f"{s.kbps:,.0f} kbps")
        else:
            self.t_drop.set("--")
            self.t_kbps.set("--")
        cpu = s.stats.get("cpuUsage")
        self.t_cpu.set(f"{cpu:.0f}%" if cpu is not None else "--", None if cpu is None else
                       "good" if cpu < 50 else "warn" if cpu < 80 else "bad")

        self.rec.setEnabled(s.connected)
        self.rec.setText("Stop recording" if s.record["active"] else "Record")
        self.rec.setObjectName("danger" if s.record["active"] else "")
        self.rec.setStyleSheet("")
        self.rec.style().unpolish(self.rec)
        self.rec.style().polish(self.rec)
        self.replay.setEnabled(s.connected)
        self.replay.setText("Save replay" if s.replay else "Start replay buffer")
        if s.vcam_available and self.vcam is None:
            self.vcam = QPushButton("Virtual camera")
            self.vcam.setMinimumHeight(40)
            self.vcam.setToolTip("Use the OBS output as a webcam in other apps")
            self.vcam.clicked.connect(self.store.toggle_vcam)
            self.actions_layout.insertWidget(3, self.vcam)
        elif s.vcam_available is False and self.vcam is not None:
            self.vcam.setParent(None)
            self.vcam.deleteLater()
            self.vcam = None
        if self.vcam is not None:
            self.vcam.setEnabled(s.connected)
            self.vcam.setText("Stop virtual camera" if s.vcam else "Virtual camera")
