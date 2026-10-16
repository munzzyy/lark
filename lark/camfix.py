"""The Fix button for the dark camera: re-assert exposure, reload the Stream preset, wait, check the picture again."""
from PySide6.QtCore import QObject, QTimer

KEY = "camdark"
RECHECK_MS = 6000


class DarkFixer(QObject):
    def __init__(self, ctx, recheck_ms=RECHECK_MS):
        super().__init__()
        self.ctx = ctx
        self.busy = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(recheck_ms)
        self._timer.timeout.connect(self._recheck)

    def run(self):
        s = self.ctx.store
        if self.busy:
            return
        if not s.connected:
            s.toast.emit("Open OBS first, Lark checks the picture through it", True)
            return
        self.busy = True
        s.sticky_show.emit(KEY, "Fixing the camera...", "")
        preset = self.ctx.config.get("camera_preflight_preset") or "1"
        hold = not s.stream["active"]
        self.ctx.camera.reassert_exposure(hold_stream=hold, preset=preset)
        self._timer.start()

    def _recheck(self):
        self.ctx.store.camwatch.sample(self._rechecked)

    def _rechecked(self, luma):
        s = self.ctx.store
        self.busy = False
        if luma is None:
            s.toast.emit("Could not read the camera image to check the fix", True)
            s.sticky_show.emit(KEY, "Camera went dark", "Try again")
        elif not s.cam_dark:
            s.sticky_hide.emit(KEY)
            s.toast.emit(f"Camera is back to normal, brightness {luma:.0f}", False)
        else:
            s.toast.emit(f"Camera is still dark, brightness {luma:.0f}", True)
            s.sticky_show.emit(KEY, "Camera is still dark", "Try again")
