"""App-wide keyboard shortcuts. None of them fire while a text field has focus."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit

from .highlights import manager_for

SHORTCUTS = [
    ("Ctrl+1 to Ctrl+9", "Switch to scene 1 to 9, in the order OBS lists them"),
    ("Ctrl+M", "Mute or unmute the mic"),
    ("Ctrl+Shift+R", "Save the replay buffer and add a highlight"),
    ("Ctrl+G", "Go live, or stop (press twice to stop)"),
    ("Ctrl+,", "Open Settings"),
]


def is_text_input(w):
    if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return True
    return isinstance(w, QComboBox) and w.isEditable()


def text_field_has_focus():
    return is_text_input(QApplication.focusWidget())


class Shortcuts:
    def __init__(self, win):
        self.win = win
        self.shortcuts = []
        for n in range(1, 10):
            self._add(f"Ctrl+{n}", lambda n=n: self.scene(n))
        self._add("Ctrl+M", self.mic)
        self._add("Ctrl+Shift+R", self.replay)
        self._add("Ctrl+G", self.live)
        self._add("Ctrl+,", lambda: win.show_view("settings"))

    def _add(self, keys, fn):
        sc = QShortcut(QKeySequence(keys), self.win)
        sc.setContext(Qt.ApplicationShortcut)
        sc.activated.connect(lambda: None if text_field_has_focus() else fn())
        self.shortcuts.append(sc)

    @property
    def store(self):
        return self.win.ctx.store

    def _toast(self, msg, bad=False):
        self.store.toast.emit(msg, bad)

    def scene(self, n):
        s = self.store
        if not s.connected:
            return self._toast("OBS is not connected", True)
        if n > len(s.scenes):
            return self._toast(f"No scene {n}", True)
        s.switch_scene(s.scenes[n - 1])
        self._toast(f"Scene: {s.scenes[n - 1]}")

    def mic(self):
        s = self.store
        name = self.win.ctx.config.get("mic_input")
        if not s.connected or name not in s.inputs:
            return self._toast(f"No input called {name} to mute", True)
        muted = not s.inputs[name].get("muted", False)
        s.set_mute(name, muted)
        self._toast(f"{name} {'muted' if muted else 'on'}")

    def replay(self):
        if not self.store.connected:
            return self._toast("OBS is not connected", True)
        manager_for(self.win.ctx).mark("hotkey")

    def live(self):
        s = self.store
        page = self.win.pages["live"]
        if not s.connected:
            return self._toast("OBS is not connected", True)
        if s.stream["active"]:
            armed = page._stop_armed
            page._stop()
            if not armed:
                self._toast("Press Ctrl+G again to stop the stream")
        elif page.go.isEnabled():
            page._go()
        else:
            self._toast("Pre-flight is blocking Go Live", True)
