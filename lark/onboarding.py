"""First-run card on the Live page: connect OBS, pick the mic, check the camera, optionally sign in to Twitch."""
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .widgets import card, label


def pending_steps(*, connected, mic_ok, camera_ok, twitch_ok):
    """Keys of the steps still to do, in order."""
    todo = []
    if not connected:
        todo.append("obs")
    if not mic_ok:
        todo.append("mic")
    if not camera_ok:
        todo.append("camera")
    if not twitch_ok:
        todo.append("twitch")
    return todo


class StepRow(QWidget):
    def __init__(self, title):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(12)
        self.mark = label("", "stepmark")
        self.mark.setFixedWidth(22)
        self.title = label(title)
        self.title.setMinimumWidth(210)
        self.detail = label("", "muted")
        lay.addWidget(self.mark)
        lay.addWidget(self.title)
        lay.addWidget(self.detail, 1)
        self.extra = QHBoxLayout()
        lay.addLayout(self.extra)

    def set(self, done, detail):
        self.mark.setText("✓" if done else "•")
        self.mark.setProperty("done", done)
        self.mark.style().unpolish(self.mark)
        self.mark.style().polish(self.mark)
        self.detail.setText(detail)


class OnboardingCard(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx, self.store = ctx, ctx.store
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.frame, lay = card(spacing=6)
        outer.addWidget(self.frame)
        head = QHBoxLayout()
        head.addWidget(label("Get set up", "h2"))
        head.addStretch(1)
        self.hide_btn = QPushButton("Hide")
        self.hide_btn.setObjectName("link")
        self.hide_btn.setToolTip("Hide this card for good. Everything here is also in Settings.")
        self.hide_btn.clicked.connect(self.finish)
        head.addWidget(self.hide_btn)
        lay.addLayout(head)
        lay.addWidget(label("Four quick things, then Go Live is ready.", "muted"))
        lay.addSpacing(4)
        self.rows = {}
        for key, title in (("obs", "Connect to OBS"), ("mic", "Pick your mic"), ("camera", "Check your camera"),
                           ("twitch", "Sign in to Twitch (optional)")):
            r = StepRow(title)
            self.rows[key] = r
            lay.addWidget(r)
        self.mic_pick = QComboBox()
        self.mic_pick.setToolTip("The OBS audio input Lark treats as your mic")
        self.mic_pick.activated.connect(self._picked)
        self.rows["mic"].extra.addWidget(self.mic_pick)
        for key, text, view in (("obs", "Open Settings", "settings"), ("camera", "Open Camera", "camera"),
                                ("twitch", "Open Settings", "settings")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, v=view: self.ctx.goto(v) if hasattr(self.ctx, "goto") else None)
            self.rows[key].extra.addWidget(b)
            self.rows[key].button = b
        self.refresh()

    def _picked(self, i):
        name = self.mic_pick.itemText(i)
        if name:
            self.ctx.config.set("mic_input", name)
            self.ctx.config.save()
            self.refresh()

    def state(self):
        s = self.store
        mic = self.ctx.config.get("mic_input")
        return dict(connected=s.connected, mic_ok=mic in s.inputs, camera_ok=s.camera_present(),
                    twitch_ok=self.ctx.services.twitch_state["status"] == "connected")

    def finish(self):
        self.ctx.config.set("onboarding_done", True)
        self.ctx.config.save()
        self.refresh()

    def refresh(self):
        if self.ctx.config.get("onboarding_done"):
            self.hide()
            return
        st = self.state()
        todo = pending_steps(**st)
        s = self.store
        mic = self.ctx.config.get("mic_input")
        self.rows["obs"].set(st["connected"], "Connected" if st["connected"] else "Open OBS, Lark connects on its own")
        self.rows["mic"].set(st["mic_ok"], mic if st["mic_ok"] else "Choose the input from OBS")
        self.rows["camera"].set(st["camera_ok"], "Found" if st["camera_ok"] else "Not found, plug it in")
        self.rows["twitch"].set(st["twitch_ok"], "Signed in" if st["twitch_ok"] else "Shows viewers and sets your title")
        names = list(s.inputs)
        if [self.mic_pick.itemText(i) for i in range(self.mic_pick.count())] != names:
            self.mic_pick.clear()
            self.mic_pick.addItems(names)
        if mic in names:
            self.mic_pick.setCurrentIndex(names.index(mic))
        elif not names:
            self.mic_pick.setPlaceholderText("No inputs yet")
            self.mic_pick.setCurrentIndex(-1)
        self.mic_pick.setVisible(bool(names))
        for key in ("obs", "camera", "twitch"):
            self.rows[key].button.setVisible(key in todo)
        required = [k for k in todo if k != "twitch"]
        if not required and st["connected"]:
            self.finish()
            return
        self.show()
