from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from ..shortcuts import SHORTCUTS
from ..widgets import Page, card, divider, label
from .alert_settings import alert_settings_card
from .framing import autoframe_card, camera_in_obs_card

ACCENTS = ["#7c5cff", "#3b82f6", "#14b8a6", "#f472b6", "#f59e0b", "#ef4444"]


class SettingsPage(Page):
    def __init__(self, ctx):
        super().__init__("Settings", "Saved to ~/.config/lark/config.json.")
        self.ctx, self.cfg = ctx, ctx.config
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setMinimumWidth(120)
        save.clicked.connect(self.save)
        self.header.addWidget(save)

        obs, g = card(QGridLayout, spacing=12)
        g.addWidget(label("OBS connection", "h2"), 0, 0, 1, 2)
        self.host = QLineEdit(self.cfg.get("obs_host"))
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("Same as OBS")
        self.port.setValue(self.cfg.get("obs_port") or 0)
        self.source = QComboBox()
        self.source.addItem("Read it from OBS", "obs_file")
        self.source.addItem("Use one I type here", "custom")
        self.source.setCurrentIndex(0 if self.cfg.get("obs_password_source") == "obs_file" else 1)
        self.password = QLineEdit(self.cfg.get("obs_password"))
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Password")
        self.obs_note = label(self.cfg.obs_file_status(), "small")
        rows = [("Host", self.host), ("Port", self.port), ("Password", self.source), ("", self.password)]
        for i, (t, w) in enumerate(rows, 1):
            g.addWidget(label(t, "muted"), i, 0)
            g.addWidget(w, i, 1)
        g.addWidget(self.obs_note, 5, 1)
        g.setColumnMinimumWidth(0, 150)
        g.setColumnStretch(2, 1)
        for w in (self.host, self.port, self.source, self.password):
            w.setMaximumWidth(420)
        self.source.currentIndexChanged.connect(self._sync_pw)
        self._sync_pw()
        self.body.addWidget(obs)

        ov, g2 = card(QGridLayout, spacing=12)
        g2.addWidget(label("Overlays, chat and camera", "h2"), 0, 0, 1, 2)
        self.oport = QSpinBox()
        self.oport.setRange(1024, 65535)
        self.oport.setValue(self.cfg.get("overlay_port"))
        self.channel = QLineEdit(self.cfg.get("twitch_channel"))
        self.fade = QSpinBox()
        self.fade.setRange(0, 3600)
        self.fade.setSpecialValueText("Never")
        self.fade.setSuffix(" s")
        self.fade.setValue(int(self.cfg.get("chat_fade_seconds")))
        self.mic = QLineEdit(self.cfg.get("mic_input"))
        self.cam_input = QLineEdit(self.cfg.get("camera_input"))
        self.cam_input.setToolTip("The camera input in OBS. Lark reads its picture to notice when it goes dark")
        self.dark = QSpinBox()
        self.dark.setRange(5, 60)
        self.dark.setValue(int(self.cfg.get("camera_dark_threshold")))
        self.dark.setToolTip("Brightness (0-255) the camera picture has to stay under for 9 seconds to count as dark. "
                             "The warning clears 13 above it. Healthy is around 100.")
        for i, (t, w) in enumerate([("Overlay port", self.oport), ("Twitch channel", self.channel),
                                    ("Chat fade out after", self.fade), ("Mic input in OBS", self.mic),
                                    ("Camera input in OBS", self.cam_input), ("Camera counts as dark under", self.dark)], 1):
            g2.addWidget(label(t, "muted"), i, 0)
            g2.addWidget(w, i, 1)
        for w in (self.oport, self.channel, self.fade, self.mic, self.cam_input, self.dark):
            w.setMaximumWidth(420)
        g2.setColumnMinimumWidth(0, 150)
        g2.setColumnStretch(2, 1)
        self.body.addWidget(ov)

        th, tl = card(spacing=12)
        tl.addWidget(label("Accent colour", "h2"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.accent = self.cfg.get("accent")
        self.swatches = []
        for c in ACCENTS:
            b = QPushButton()
            b.setFixedSize(32, 32)
            b.setCheckable(True)
            b.setCursor(b.cursor())
            b.setStyleSheet(f"QPushButton {{ background: {c}; border-radius: 16px; border: 2px solid transparent; }}"
                            f"QPushButton:checked {{ border-color: #ffffff; }}")
            b.clicked.connect(lambda _=False, c=c: self._pick(c))
            self.swatches.append((c, b))
            row.addWidget(b)
        self.hex = QLineEdit(self.accent)
        self.hex.setMaximumWidth(110)
        self.hex.editingFinished.connect(lambda: self._pick(self.hex.text().strip()))
        row.addWidget(self.hex)
        row.addStretch(1)
        tl.addLayout(row)
        self._mark()
        self.body.addWidget(th)

        tw, wl = card(spacing=10)
        wl.addWidget(label("Twitch account", "h2"))
        self.twitch_status = label("", "muted", wrap=True)
        wl.addWidget(self.twitch_status)
        cid_row = QHBoxLayout()
        lbl = label("Twitch client id", "muted")
        lbl.setMinimumWidth(150)
        cid_row.addWidget(lbl)
        self.client_id = QLineEdit(self.cfg.get("twitch_client_id"))
        self.client_id.setPlaceholderText("From dev.twitch.tv/console, a Public client")
        self.client_id.setMaximumWidth(420)
        cid_row.addWidget(self.client_id)
        cid_row.addStretch(1)
        wl.addLayout(cid_row)
        btns = QHBoxLayout()
        self.connect_btn = QPushButton("Connect Twitch")
        self.connect_btn.clicked.connect(self._twitch_click)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(lambda: self.ctx.services.twitch_disconnect())
        btns.addWidget(self.connect_btn)
        btns.addWidget(self.disconnect_btn)
        btns.addStretch(1)
        wl.addLayout(btns)
        self.code_box = QWidget()
        cl = QVBoxLayout(self.code_box)
        cl.setContentsMargins(0, 4, 0, 0)
        cl.addWidget(label("On any device, open this page and enter the code:", "muted"))
        self.code = label("", "big")
        self.url = label("", "muted")
        cl.addWidget(self.code)
        cl.addWidget(self.url)
        crow = QHBoxLayout()
        self.copy_code = QPushButton("Copy code")
        self.open_page = QPushButton("Open the page")
        crow.addWidget(self.copy_code)
        crow.addWidget(self.open_page)
        crow.addStretch(1)
        cl.addLayout(crow)
        self.copy_code.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.code.text()))
        self.open_page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.url.text())))
        self.code_box.hide()
        wl.addWidget(self.code_box)
        self.need_app = label(
            "To connect, Twitch needs an app registered for Lark. In dev.twitch.tv/console, register an application "
            "with the client type set to Public, put http://localhost as the redirect URL, then paste its client id "
            "above and save. Lark never asks for or stores a client secret, and the sign-in token goes in the system "
            "keyring, not in the config file.", "muted", wrap=True)
        self.need_app.hide()
        wl.addWidget(self.need_app)
        self.body.addWidget(tw)
        self._pending = False
        ctx.services.twitch_changed.connect(self._twitch_state)
        self._twitch_state(ctx.services.twitch_state)

        sc, sl = card(QGridLayout, spacing=8)
        sl.addWidget(label("Keyboard shortcuts", "h2"), 0, 0, 1, 2)
        for i, (k, d) in enumerate(SHORTCUTS, 1):
            sl.addWidget(label(k), i, 0)
            sl.addWidget(label(d, "muted"), i, 1)
        sl.setColumnMinimumWidth(0, 170)
        sl.setColumnStretch(1, 1)
        sl.addWidget(label("They do nothing while you are typing in a text field.", "small"), len(SHORTCUTS) + 1, 0, 1, 2)
        self.body.addWidget(sc)
        self.body.addWidget(camera_in_obs_card(ctx))
        self.body.addWidget(autoframe_card(ctx))
        self.body.addWidget(alert_settings_card(ctx))
        self.body.addStretch(1)

    def _sync_pw(self):
        custom = self.source.currentData() == "custom"
        self.password.setEnabled(custom)
        self.password.setVisible(custom)

    def _pick(self, color):
        from PySide6.QtGui import QColor
        if not QColor(color).isValid() or len(color) != 7 or not color.startswith("#"):
            self.ctx.store.toast.emit("Accent must look like #7c5cff", True)
            self.hex.setText(self.accent)
            return
        self.accent = color.lower()
        self.hex.setText(self.accent)
        self._mark()

    def _mark(self):
        for c, b in self.swatches:
            b.setChecked(c == self.accent)

    def _twitch_click(self):
        if self._pending:
            self.ctx.services.twitch_cancel()
            return
        if not self.cfg.get("twitch_client_id") and not self.client_id.text().strip():
            self.need_app.show()
            self.twitch_status.setText("Not connected. Reading chat works without an account.")
            return
        if self.client_id.text().strip() != self.cfg.get("twitch_client_id"):
            self.save()
        if not self.cfg.get("twitch_client_id"):
            return
        self.need_app.hide()
        self.ctx.services.twitch_connect()

    def _twitch_state(self, st):
        status = st["status"]
        has_id = bool(self.cfg.get("twitch_client_id"))
        self.code_box.setVisible(status == "pending")
        self.code.setText(st["user_code"])
        self.url.setText(st["verification_uri"])
        self.disconnect_btn.setVisible(status == "connected")
        self.connect_btn.setVisible(status != "connected")
        self.connect_btn.setText("Cancel" if status == "pending" else "Connect Twitch")
        self._pending = status == "pending"
        if status == "connected":
            can = "chat:edit" in st["scopes"]
            self.twitch_status.setText(f"Connected as {st['login']}." + ("" if can else " Chat sending was not allowed."))
        elif status == "pending":
            self.twitch_status.setText(st["message"])
        elif status == "error":
            self.twitch_status.setText(st["message"])
        else:
            self.twitch_status.setText("Not connected. Reading chat works without an account."
                                       + ("" if has_id else " Add a client id to connect."))

    def save(self):
        port = self.port.value() or None
        new = {"obs_host": self.host.text().strip(), "obs_port": port,
               "obs_password_source": self.source.currentData(), "obs_password": self.password.text(),
               "overlay_port": self.oport.value(), "twitch_channel": self.channel.text().strip().lstrip("#"),
               "chat_fade_seconds": self.fade.value(), "mic_input": self.mic.text(), "accent": self.accent,
               "camera_input": self.cam_input.text().strip(), "camera_dark_threshold": self.dark.value(),
               "twitch_client_id": self.client_id.text().strip()}
        old = dict(self.cfg.values)
        try:
            for k, v in new.items():
                self.cfg.set(k, v)
        except ValueError as e:
            self.cfg.values = old
            self.ctx.store.toast.emit(f"Not saved: {e}", True)
            return
        try:
            self.cfg.save()
        except OSError as e:
            self.ctx.store.toast.emit(f"Could not write the config: {e.strerror}", True)
            return
        self.obs_note.setText(self.cfg.obs_file_status())
        self.ctx.apply_settings(old)
        self.ctx.store.toast.emit("Saved", False)
