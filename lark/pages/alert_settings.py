from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QSlider, QSpinBox)
from PySide6.QtCore import Qt

from ..events import DEFAULT_TEMPLATES, LABELS, SOUND_EXTS, TYPES, alert_settings, sound_problem
from ..widgets import card, label

PILL_TEXT = {"idle": "Not running", "connecting": "Connecting", "connected": "Listening", "error": "Retrying"}


def alert_settings_card(ctx):
    """The Alerts section of Settings. Saves on its own button and takes effect without a restart."""
    frame, lay = card(spacing=10)
    lay.addWidget(label("Alerts", "h2"))
    status = label("", "muted", wrap=True)
    lay.addWidget(status)

    def show_state(s, d):
        status.setText(f"Twitch events: {PILL_TEXT.get(s, s)}. {d}".strip())
    ctx.services.eventsub_state.connect(show_state)
    show_state(*ctx.services.eventsub_status)

    vol_row = QHBoxLayout()
    vol_label = label("Sound volume", "muted")
    vol_label.setMinimumWidth(150)
    volume = QSlider(Qt.Horizontal)
    volume.setRange(0, 100)
    volume.setValue(int(ctx.config.get("alert_volume")))
    volume.setMaximumWidth(260)
    vol_row.addWidget(vol_label)
    vol_row.addWidget(volume)
    vol_row.addStretch(1)
    lay.addLayout(vol_row)

    grid = QGridLayout()
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(8)
    for col, t in enumerate(("Type", "On", "Seconds", "Text, with {user} {amount} {message}", "Sound file")):
        grid.addWidget(label(t, "small"), 0, col)
    rows = {}
    for i, kind in enumerate(TYPES, 1):
        s = alert_settings(ctx.config.values, kind)
        on = QCheckBox()
        on.setChecked(s["enabled"])
        secs = QSpinBox()
        secs.setRange(1, 60)
        secs.setValue(int(s["duration"]))
        text = QLineEdit(s["template"])
        text.setMaxLength(200)
        text.setMinimumWidth(300)
        sound = QLineEdit(s["sound"])
        sound.setPlaceholderText("No sound")
        sound.setMinimumWidth(200)
        pick = QPushButton("Browse")
        pick.clicked.connect(lambda _=False, e=sound: _browse(frame, e))
        srow = QHBoxLayout()
        srow.addWidget(sound, 1)
        srow.addWidget(pick)
        grid.addWidget(label(LABELS[kind]), i, 0)
        grid.addWidget(on, i, 1)
        grid.addWidget(secs, i, 2)
        grid.addWidget(text, i, 3)
        grid.addLayout(srow, i, 4)
        rows[kind] = (on, secs, text, sound)
    grid.setColumnStretch(3, 1)
    grid.setColumnStretch(4, 1)
    lay.addLayout(grid)
    lay.addWidget(label("Sounds play inside the OBS browser source, from a file on this computer. "
                        "mp3, ogg, wav, opus, flac or m4a, up to 10 MB.", "small", wrap=True))

    btns = QHBoxLayout()
    save = QPushButton("Save alerts")
    save.setObjectName("primary")
    reset = QPushButton("Reset text")
    btns.addWidget(save)
    btns.addWidget(reset)
    btns.addStretch(1)
    lay.addLayout(btns)

    def reset_text():
        for kind, (_, _, text, _) in rows.items():
            text.setText(DEFAULT_TEMPLATES[kind])
    reset.clicked.connect(reset_text)

    def do_save():
        new = {}
        for kind, (on, secs, text, sound) in rows.items():
            path = sound.text().strip()
            bad = sound_problem(path)
            if bad:
                ctx.store.toast.emit(f"{LABELS[kind]}: {bad}", True)
                return
            tpl = text.text().strip()
            if not tpl:
                ctx.store.toast.emit(f"{LABELS[kind]}: the text can't be empty", True)
                return
            new[kind] = {"enabled": on.isChecked(), "duration": secs.value(), "sound": path, "template": tpl}
        old = dict(ctx.config.values)
        try:
            ctx.config.set("alert_settings", new)
            ctx.config.set("alert_volume", volume.value())
            ctx.config.save()
        except ValueError as e:
            ctx.config.values = old
            ctx.store.toast.emit(f"Not saved: {e}", True)
            return
        except OSError as e:
            ctx.store.toast.emit(f"Could not write the config: {e.strerror}", True)
            return
        ctx.store.toast.emit("Alerts saved", False)
    save.clicked.connect(do_save)
    frame.save_alerts = do_save
    frame.rows = rows
    frame.volume = volume
    return frame


def _browse(parent, edit):
    pattern = " ".join(f"*{e}" for e in SOUND_EXTS)
    path, _ = QFileDialog.getOpenFileName(parent, "Pick an alert sound", edit.text() or "", f"Sounds ({pattern})")
    if path:
        edit.setText(path)
