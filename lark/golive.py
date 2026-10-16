"""The Go Live flow: which fix button a failing check gets, and the countdown before the stream starts."""
from PySide6.QtCore import QObject, QTimer, Signal

from .preflight import OK, SKIP, WARN

UNMUTE, PICK_SCENE, EDIT_TITLE, OPEN, LOAD_PRESET, FIX_DARK = ("unmute", "pick_scene", "edit_title", "open", "load_preset",
                                                          "fix_dark")


def fix_action(check, *, mic_muted=False, scenes=()):
    """(button text, kind, argument) for a check that is not OK, or None when there is nothing Lark can do."""
    k, st = check.key, check.status
    if st == OK:
        return None
    if k == "obs":
        return ("Open Settings", OPEN, "settings")
    if k == "scene":
        if st == SKIP:
            return None
        return ("Pick a scene", PICK_SCENE, None) if scenes else ("Open Scenes", OPEN, "scenes")
    if k == "mic":
        if st == SKIP:
            return None
        return ("Unmute", UNMUTE, None) if mic_muted else ("Open Audio", OPEN, "audio")
    if k == "camera":
        return None if st == SKIP else ("Open Camera", OPEN, "camera")
    if k == "preset":
        return ("Load it", LOAD_PRESET, None) if st == WARN else None
    if k == "light":
        return ("Fix", FIX_DARK, None) if st == WARN else None
    if k == "title":
        return ("Connect Twitch", OPEN, "settings") if st == SKIP else ("Edit title", EDIT_TITLE, None)
    return None


class Countdown(QObject):
    """Counts down whole seconds, then fires finished. cancel() stops it without firing."""
    ticked = Signal(int)
    finished = Signal()
    cancelled = Signal()

    def __init__(self, seconds=3, parent=None):
        super().__init__(parent)
        self.seconds = seconds
        self.left = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)

    @property
    def running(self):
        return self.left > 0

    def start(self):
        if self.running:
            return
        self.left = self.seconds
        self.ticked.emit(self.left)
        self._timer.start()

    def tick(self):
        if not self.running:
            return
        self.left -= 1
        if self.left > 0:
            self.ticked.emit(self.left)
            return
        self._timer.stop()
        self.finished.emit()

    def cancel(self):
        if not self.running:
            return
        self._timer.stop()
        self.left = 0
        self.cancelled.emit()
