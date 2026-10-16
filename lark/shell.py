"""The frame around every page: status header, collapsing rail, window title and remembered layout."""
import logging

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .camera_backend import summarize
from .camwatch import light_label
from .viewers import fetch_viewers
from .widgets import Pill, label

log = logging.getLogger("lark.shell")

RAIL_COLLAPSE_BELOW = 1100
RAIL_WIDE, RAIL_NARROW = 208, 68
VIEWER_POLL_MS = 30000

NAV_TIPS = {
    "live": "Go live, pre-flight checks and stream stats",
    "scenes": "Switch scenes and show or hide sources",
    "audio": "Mic and desktop levels, volume and mute",
    "camera": "Camera controls and presets",
    "overlays": "Browser sources for OBS: chat and starting soon",
    "chat": "Twitch chat",
    "settings": "OBS connection, Twitch, accent colour (Ctrl+,)",
}


def fmt_clock(ms):
    s = int(ms // 1000)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def camera_chip(summary):
    """'Wide lens, 1.5x, auto exposure' from camera_backend.summarize, empty when nothing is known."""
    if not summary:
        return ""
    parts = [f"{summary['lens']} lens" if summary.get("lens") else "", summary.get("zoom", ""),
             f"{summary['exposure'].lower()} exposure" if summary.get("exposure") else ""]
    return ", ".join(p for p in parts if p)


def header_state(*, conn, scene, stream, inputs, mic_name, twitch_connected, viewers, camera=None, mode="", light=None):
    """Everything the status header shows, as plain data."""
    dot, text = {"connected": ("good", "OBS connected"), "connecting": ("warn", "Connecting"),
                 "auth_failed": ("bad", "Wrong OBS password")}.get(conn, ("bad", "OBS not running"))
    live = conn == "connected" and bool(stream.get("active"))
    mic = inputs.get(mic_name)
    luma, dark = light if light else (None, False)
    light_text = "" if luma is None or conn != "connected" else f"Light {light_label(luma)}"
    light_state = "bad" if light_text and dark else "warn" if light_text.endswith("dim") else ""
    if conn != "connected":
        mic_state = ("", "Mic --")
    elif mic is None:
        mic_state = ("warn", "No mic")
    elif mic.get("muted"):
        mic_state = ("bad", "Mic muted")
    else:
        mic_state = ("good", "Mic on")
    return {
        "dot": dot, "conn": text,
        "scene": scene if conn == "connected" and scene else "",
        "live": live, "reconnecting": live and bool(stream.get("reconnecting")),
        "clock": fmt_clock(stream.get("duration_ms", 0)) if live else "",
        "viewers": viewers if twitch_connected else None,
        "twitch": twitch_connected,
        "camera": camera_chip(camera),
        "camera_mode": mode,
        "light": (light_state, light_text, f"{luma:.0f} of 255" if light_text else ""),
        "mic": mic_state, "mic_togglable": conn == "connected" and mic is not None,
    }


def window_title(state):
    if state["live"]:
        return f"Lark - {'RECONNECTING' if state['reconnecting'] else 'LIVE'} {state['clock']}"
    return "Lark"


class StatusHeader(QFrame):
    def __init__(self, ctx):
        super().__init__()
        self.setObjectName("statusbar")
        self.ctx, self.store = ctx, ctx.store
        self.viewers = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(32, 10, 32, 10)
        lay.setSpacing(14)
        self.dot = QLabel()
        self.dot.setObjectName("dot")
        self.dot.setFixedSize(10, 10)
        self.conn = label("", "statustext")
        self.scene = label("", "chip")
        self.scene.setToolTip("Current scene in OBS")
        self.live = Pill("LIVE", "bad")
        self.live.setToolTip("Time on air")
        self.view_label = label("", "chip")
        self.view_label.setToolTip("People watching on Twitch right now")
        self.cam_label = label("", "chip")
        self.cam_label.setToolTip("Camera mode (Stream or Quality), lens, zoom and exposure mode")
        self.light_label = label("", "chip")
        self.cam_summary = None
        self.mic = QPushButton("Mic")
        self.mic.setObjectName("chipbtn")
        self.mic.setCursor(Qt.PointingHandCursor)
        self.mic.setToolTip("Mic state. Click to mute or unmute (Ctrl+M)")
        self.mic.clicked.connect(self._toggle_mic)
        for w in (self.dot, self.conn, self.scene):
            lay.addWidget(w)
        lay.addStretch(1)
        for w in (self.live, self.view_label, self.cam_label, self.light_label, self.mic):
            lay.addWidget(w)
        self.state = None
        s = self.store
        for sig in (s.conn_changed, s.scenes_changed, s.outputs_changed, s.inputs_changed, s.input_updated,
                    s.cam_light_changed):
            sig.connect(lambda *a: self.refresh())
        ctx.services.twitch_changed.connect(self._twitch)
        cam = getattr(ctx, "camera", None)
        if cam is not None:
            cam.snapshot_ready.connect(self._camera)
            cam.unavailable.connect(self._camera_gone)
        eptz = getattr(ctx, "eptz", None)
        if eptz is not None:
            eptz.mode_changed.connect(self.refresh)
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self.refresh)
        self._tick.start()
        self._poll = QTimer(self)
        self._poll.setInterval(VIEWER_POLL_MS)
        self._poll.timeout.connect(self.poll_viewers)
        self._poll.start()
        self.refresh()

    def _twitch(self, st):
        if st["status"] != "connected":
            self.viewers = None
        else:
            self.poll_viewers()
        self.refresh()

    def _camera_gone(self, _msg):
        self._camera(None)

    def _camera(self, snap):
        self.cam_summary = summarize(snap) if snap is not None else None
        self.refresh()

    def poll_viewers(self):
        svc = self.ctx.services
        if svc.twitch_state["status"] != "connected":
            return

        def got(ok, res):
            self.viewers = res if ok else None
            self.refresh()
        svc.submit(lambda: fetch_viewers(svc.twitch), got)

    def _toggle_mic(self):
        name = self.ctx.config.get("mic_input")
        i = self.store.inputs.get(name)
        if i is not None and self.store.connected:
            self.store.set_mute(name, not i.get("muted", False))

    def _mode_text(self):
        eptz = getattr(self.ctx, "eptz", None)
        return eptz.mode_text() if eptz is not None and self.store.connected else ""

    def refresh(self):
        s = self.store
        mic_name = self.ctx.config.get("mic_input")
        st = header_state(conn=s.conn, scene=s.current_scene, stream=s.stream, inputs=s.inputs, mic_name=mic_name,
                          twitch_connected=self.ctx.services.twitch_state["status"] == "connected",
                          viewers=self.viewers, camera=self.cam_summary,
                          mode=self._mode_text(), light=(s.cam_luma, s.cam_dark))
        self.state = st
        self._set_state(self.dot, st["dot"])
        self.conn.setText(st["conn"])
        self.scene.setText(f"Scene: {st['scene']}" if st["scene"] else "")
        self.scene.setVisible(bool(st["scene"]))
        self.live.setVisible(st["live"])
        if st["live"]:
            self.live.set_state("warn" if st["reconnecting"] else "bad",
                                f"{'RECONNECTING' if st['reconnecting'] else 'LIVE'}  {st['clock']}")
        self.view_label.setVisible(st["twitch"])
        if st["twitch"]:
            self.view_label.setText("Viewers --" if st["viewers"] is None else f"Viewers {st['viewers']:,}")
        chip = ", ".join(p for p in (st["camera_mode"], st["camera"]) if p)
        self.cam_label.setVisible(bool(chip))
        self.cam_label.setText(chip)
        lstate, ltext, ltip = st["light"]
        self.light_label.setVisible(bool(ltext))
        self.light_label.setText(ltext)
        self.light_label.setToolTip(f"Camera image brightness {ltip}, read from OBS" if ltip else "")
        self._set_state(self.light_label, lstate)
        mstate, mtext = st["mic"]
        self.mic.setText(mtext)
        self._set_state(self.mic, mstate)
        self.mic.setEnabled(st["mic_togglable"])
        win = self.window()
        if win is not self:
            title = window_title(st)
            if win.windowTitle() != title:
                win.setWindowTitle(title)

    @staticmethod
    def _set_state(w, state):
        if w.property("state") != state:
            w.setProperty("state", state)
            w.style().unpolish(w)
            w.style().polish(w)


def rail_is_collapsed(width):
    return width < RAIL_COLLAPSE_BELOW


def apply_rail(win, width=None):
    """Icons-only rail under RAIL_COLLAPSE_BELOW px of window width."""
    narrow = rail_is_collapsed(win.width() if width is None else width)
    rail = win.findChild(QWidget, "rail")
    if rail is None:
        return narrow
    rail.setFixedWidth(RAIL_NARROW if narrow else RAIL_WIDE)
    for key, b in win.buttons.items():
        if b.property("full_text") is None:
            b.setProperty("full_text", b.text())
        b.setText("" if narrow else b.property("full_text"))
        b.setProperty("compact", narrow)
        b.setToolTip(NAV_TIPS.get(key, "") if not narrow else f"{b.property('full_text')}: {NAV_TIPS.get(key, '')}")
        b.style().unpolish(b)
        b.style().polish(b)
    logo = rail.findChild(QLabel, "logo")
    if logo is not None:
        logo.setText("L" if narrow else "Lark")
        logo.setAlignment(Qt.AlignCenter if narrow else Qt.AlignLeft | Qt.AlignVCenter)
    for w in rail.findChildren(QLabel):
        if w.objectName() in ("small", "pill"):
            w.setVisible(not narrow)
    return narrow


def save_state(config, page=None, size=None):
    """Write the remembered page and window size into the config file. Never raises."""
    changed = False
    if page and config.get("last_page") != page:
        config.set("last_page", page)
        changed = True
    if size and config.get("window_size") != list(size):
        try:
            config.set("window_size", list(size))
            changed = True
        except ValueError:
            log.warning("not remembering window size %s", size)
    if changed:
        try:
            config.save()
        except OSError as e:
            log.error("could not remember window layout: %s", e)
    return changed


def restore_view(config, valid):
    page = config.get("last_page")
    return page if page in valid else "live"


class _WindowWatcher(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._save = QTimer(self)
        self._save.setSingleShot(True)
        self._save.setInterval(600)
        self._save.timeout.connect(self.remember_size)

    def remember_size(self):
        w = self.win
        if w.isMaximized() or w.isFullScreen():
            return
        save_state(w.ctx.config, size=(w.width(), w.height()))

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Resize:
            apply_rail(self.win)
            if self.win.isVisible():
                self._save.start()
        elif t == QEvent.Close:
            self.remember_size()
        return False


def install(win):
    ctx = win.ctx
    ctx.goto = win.show_view
    saved = ctx.config.get("window_size")
    if saved:
        win.resize(*saved)

    lay = win.centralWidget().layout()
    lay.removeWidget(win.stack)
    col = QWidget()
    cl = QVBoxLayout(col)
    cl.setContentsMargins(0, 0, 0, 0)
    cl.setSpacing(0)
    win.status_header = StatusHeader(ctx)
    cl.addWidget(win.status_header)
    cl.addWidget(win.stack, 1)
    lay.addWidget(col, 1)
    win.toast.raise_()

    def page_changed(i):
        for key, p in win.pages.items():
            if win.stack.widget(i) is p:
                save_state(ctx.config, page=key)
    win.stack.currentChanged.connect(page_changed)
    win.watcher = _WindowWatcher(win)
    win.installEventFilter(win.watcher)
    for b in win.buttons.values():
        b.setFocusPolicy(Qt.TabFocus)
    win.centralWidget().setFocusPolicy(Qt.ClickFocus)
    win.centralWidget().setFocus()
    apply_rail(win)
    win.status_header.refresh()
