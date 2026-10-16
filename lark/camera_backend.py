"""Thin, read-only wrapper over cameractrls.py (another repo, imported by path, never modified).

All device work happens on one worker thread; the UI gets plain snapshots, never the library's objects.
"""
import contextlib
import fcntl
import importlib
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

log = logging.getLogger("lark.camera")

HIDDEN_CATEGORIES = {"Cameractrlsd"}
# Changing these makes OBS drop and reopen the camera, so Lark only shows them.
DISPLAY_ONLY = {"pixelformat", "resolution", "fps"}


@dataclass
class Ctrl:
    text_id: str
    name: str
    type: str
    value: object = None
    min: object = None
    max: object = None
    step: object = None
    default: object = None
    inactive: bool = False
    readonly: bool = False
    tooltip: str = ""
    menu: list = field(default_factory=list)  # [(text_id, name)]


@dataclass
class Category:
    title: str
    ctrls: list


@dataclass
class CtrlPage:
    title: str
    categories: list


@dataclass
class Snapshot:
    pages: list
    presets: list  # [(load_text_id, label)]
    loaded: dict = None  # {"1": True, ...}: which saved presets match the camera now, None when the library cannot say


_HEX6 = re.compile(r"^#?([0-9a-fA-F]{6})$")
_FACTOR = re.compile(r"(\d+(?:\.\d+)?)\s*x", re.I)


def parse_color(v):
    """A colour control's value as '#rrggbb', or None when it is not one."""
    if isinstance(v, str):
        m = _HEX6.match(v.strip())
        return "#" + m.group(1).lower() if m else None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return f"#{v:06x}" if 0 <= v <= 0xFFFFFF else None
    if isinstance(v, (tuple, list)) and len(v) == 3 and all(
            isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in v):
        return "#%02x%02x%02x" % tuple(v)
    return None


def _find(snap, text_id):
    for p in snap.pages:
        for cat in p.categories:
            for c in cat.ctrls:
                if c.text_id == text_id:
                    return c
    return None


def _menu_name(c):
    if c is None or c.value is None:
        return ""
    return next((n for t, n in c.menu if t == c.value), "")


def summarize(snap):
    """Lens, zoom, exposure mode and loaded preset as short display strings, empty when unknown."""
    lens = _find(snap, "emeet_c60e_lens")
    step = _menu_name(_find(snap, "emeet_c60e_zoom_step"))
    zoom_ctrl = _find(snap, "zoom_absolute")
    m = _FACTOR.search(step)
    if m:
        zoom = f"{m.group(1)}x"
    elif zoom_ctrl is not None and zoom_ctrl.value is not None:
        zoom = f"zoom {zoom_ctrl.value}"
    else:
        zoom = ""
    exp = _menu_name(_find(snap, "auto_exposure")).lower()
    if not exp:
        exposure = ""
    elif "manual" in exp:
        exposure = "Manual"
    elif any(w in exp for w in ("auto", "aperture", "shutter")):
        exposure = "Auto"
    else:
        exposure = exp
    preset = next((n for n, hit in sorted((snap.loaded or {}).items()) if hit), "")
    return {"lens": str(lens.value or "") if lens is not None else "", "zoom": zoom, "exposure": exposure,
            "preset": preset}


EXPOSURE_TRIES = 3
RESET_AFTER_TRIES = 2
LIBRARY_HOOKS = ("reassert_exposure", "resync_exposure", "resync")
USBDEVFS_RESET = 0x5514
UDEV_HINT = ('Cannot reset the camera over USB. Add /etc/udev/rules.d/99-camera.rules with: '
             'SUBSYSTEM=="usb", ATTR{idVendor}=="328f", ATTR{idProduct}=="00e1", TAG+="uaccess"')


class UsbResetError(Exception):
    pass


def _library_hook(cam):
    for obj in (cam, getattr(cam, "emeet_c60e", None)):
        for name in LIBRARY_HOOKS:
            fn = getattr(obj, name, None)
            if callable(fn):
                return fn
    return None


@contextlib.contextmanager
def short_stream(device, popen=subprocess.Popen, sleep=time.sleep):
    """The camera only takes exposure-mode writes while something is streaming from it. When OBS is not, run a
    two second ffmpeg capture to /dev/null around the writes."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-i", device, "-t", "2", "-f", "null", "-"]
    try:
        proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("could not start ffmpeg to hold the camera open: %s", e)
        yield
        return
    try:
        sleep(0.5)
        if proc.poll() is not None:
            log.warning("ffmpeg stopped at once (exit %s), the camera writes may be ignored", proc.returncode)
        yield
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def usb_node(video_dev, sysfs="/sys/class/video4linux", dev_root="/dev/bus/usb"):
    """The /dev/bus/usb node of the USB device behind a video node, or None."""
    name = os.path.basename(os.path.realpath(video_dev))
    d = os.path.realpath(os.path.join(sysfs, name, "device"))
    while d and d != "/":
        bus, num = os.path.join(d, "busnum"), os.path.join(d, "devnum")
        if os.path.isfile(bus) and os.path.isfile(num):
            try:
                with open(bus) as f1, open(num) as f2:
                    return os.path.join(dev_root, "%03d" % int(f1.read()), "%03d" % int(f2.read()))
            except (OSError, ValueError):
                return None
        d = os.path.dirname(d)
    return None


def usb_reset(video_dev, node_for=usb_node, opener=os.open, ioctl=fcntl.ioctl, closer=os.close):
    """USBDEVFS_RESET on the camera. Raises UsbResetError with a one-line reason, the udev rule when not allowed."""
    node = node_for(video_dev)
    if node is None:
        raise UsbResetError("Cannot find the camera's USB device")
    try:
        fd = opener(node, os.O_WRONLY)
    except PermissionError:
        raise UsbResetError(UDEV_HINT)
    except OSError as e:
        raise UsbResetError(f"Cannot open {node} ({e.strerror})")
    try:
        ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as e:
        raise UsbResetError(f"USB reset failed ({e.strerror})")
    finally:
        closer(fd)


def _preset_mode(cam, preset):
    """auto_exposure value stored in the preset, or None when the library can't say."""
    mod = sys.modules.get("cameractrls")
    try:
        import configparser
        config = configparser.ConfigParser(interpolation=None)
        config.read(mod.get_configfilename(cam.device), encoding="utf-8")
        return config.get(f"preset_{preset}", "auto_exposure", fallback=None)
    except Exception:
        log.debug("could not read the mode stored in preset %s", preset, exc_info=True)
        return None


def reassert_exposure(cam, errs, preset="1", sleep=time.sleep, stream=contextlib.nullcontext, reset=None,
                      reopen=None):
    """Kick the camera out of the dark exposure state: use the library's own re-assert when it has one, else set
    manual mode, wait 300 ms, load the preset (which sets its own exposure mode), and read auto_exposure back
    against the preset's mode, up to EXPOSURE_TRIES times. Every write happens inside stream(). After
    RESET_AFTER_TRIES misses it calls reset() (a USB reset) and reopen() for a fresh camera object before the next try.
    True once the mode reads back right."""
    hook = _library_hook(cam)
    if hook is not None:
        with stream():
            hook(errs)
        return not errs
    snap = snapshot(cam)
    mode = _find(snap, "auto_exposure")
    manual = next((t for t, n in (mode.menu if mode else []) if "manual" in n.lower()), None)
    if manual is None:
        errs.append("The camera has no exposure mode to re-assert")
        return False
    expected = _preset_mode(cam, preset) or mode.value
    for attempt in range(EXPOSURE_TRIES):
        with stream():
            cam.setup_ctrls({"auto_exposure": manual}, errs)
            sleep(0.3)
            cam.setup_ctrls({"preset": f"load_{preset}"}, errs)
            back = _find(snapshot(cam), "auto_exposure")
        if back is not None and back.value == expected:
            return True
        sleep(0.3)
        if attempt + 1 == RESET_AFTER_TRIES and reset is not None:
            try:
                reset()
            except UsbResetError as e:
                errs.append(str(e))
                continue
            cam = reopen() if reopen else cam
            if cam is None:
                errs.append("The camera did not come back after the USB reset")
                return False
    errs.append(f"The exposure mode did not stick after {EXPOSURE_TRIES} tries")
    return False


def load_module(module_dir):
    """Import cameractrls from module_dir without writing bytecode into that repo."""
    if "cameractrls" in sys.modules:
        return sys.modules["cameractrls"]
    module_dir = os.path.expanduser(module_dir)
    path = os.path.join(module_dir, "cameractrls.py")
    if not os.path.isfile(path):
        raise ImportError(f"cameractrls.py not found in {module_dir}")
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, module_dir)
    try:
        return importlib.import_module("cameractrls")
    finally:
        sys.dont_write_bytecode = old
        try:
            sys.path.remove(module_dir)
        except ValueError:
            pass


def _snap(c):
    menu = [(m.text_id, m.name) for m in (c.menu or []) if not getattr(m, "gui_hidden", False)]
    return Ctrl(c.text_id, c.name, c.type, c.value, c.min, c.max, c.step, c.default,
                bool(c.inactive), bool(c.readonly), c.tooltip or "", menu)


def snapshot(cam):
    pages, presets = [], []
    for page in cam.get_ctrl_pages():
        if page.target == "footer":
            for c in page.categories:
                for ctrl in c.ctrls:
                    if ctrl.text_id == "preset":
                        presets = [(m.text_id, m.name) for m in ctrl.menu if not m.gui_hidden]
            continue
        cats = []
        for cat in page.categories:
            if cat.title in HIDDEN_CATEGORIES or not cat.ctrls:
                continue
            cats.append(Category(cat.title, [_snap(c) for c in cat.ctrls]))
        if cats:
            pages.append(CtrlPage(page.title, cats))
    return Snapshot(pages, presets, _loaded_presets(cam))


def _loaded_presets(cam):
    cp = getattr(cam, "config_preset", None)
    if cp is None or not hasattr(cp, "preset_matches"):
        return None
    try:
        return {n: bool(cp.preset_matches(n)) for n in cp.PRESET_NUMS}
    except Exception:
        log.exception("could not compare presets")
        return None


class CameraWorker(QObject):
    snapshot_ready = Signal(object)
    unavailable = Signal(str)
    error = Signal(str)
    _do_refresh = Signal()
    _do_apply = Signal(dict, bool)
    _do_reassert = Signal(bool, str)
    _do_interval = Signal(int)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._mod = None
        self._fd = None
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._do_refresh.connect(self._refresh)
        self._do_apply.connect(self._apply)
        self._do_reassert.connect(self._reassert)
        self._do_interval.connect(self._set_interval)
        self._timer = None
        self.latest = None
        self._thread.started.connect(self._boot)

    def start(self):
        self._thread.start()

    def stop(self):
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        self._close()

    def refresh(self):
        self._do_refresh.emit()

    def apply(self, params, hold_stream=False):
        """hold_stream: run a short capture while writing, for when OBS is not streaming the camera."""
        self._do_apply.emit(params, bool(hold_stream))

    def reassert_exposure(self, hold_stream=False, preset=""):
        self._do_reassert.emit(bool(hold_stream), preset)

    def set_interval(self, ms):
        self._do_interval.emit(int(ms))

    @Slot(int)
    def _set_interval(self, ms):
        if self._timer is not None:
            self._timer.setInterval(ms)

    @Slot()
    def _boot(self):
        self._timer = QTimer()
        self._timer.setInterval(self.config.get("camera_poll_ms"))
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _open(self):
        if self._fd is not None:
            return True
        try:
            self._mod = load_module(self.config.get("camera_module_dir"))
        except Exception as e:
            self.unavailable.emit(f"Camera module not found ({e})")
            return False
        dev = self.config.get("camera_device")
        try:
            self._fd = os.open(dev, os.O_RDWR)
        except OSError as e:
            self.unavailable.emit(f"Cannot open the camera ({e.strerror})")
            return False
        return True

    def _close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                log.exception("closing camera fd")
            self._fd = None

    def _camera(self):
        return self._mod.CameraCtrls(self.config.get("camera_device"), self._fd)

    @Slot()
    def _refresh(self):
        if not self._open():
            self.latest = None
            return
        try:
            snap = snapshot(self._camera())
            self.latest = snap
            self.snapshot_ready.emit(snap)
        except Exception as e:
            self.latest = None
            log.exception("camera read failed")
            self._close()
            self.unavailable.emit(f"Camera read failed ({e})")

    def _stream(self, hold):
        device = self.config.get("camera_device")
        return (lambda: short_stream(device)) if hold else contextlib.nullcontext

    def _reopen(self):
        """A fresh camera object after the USB device re-enumerated, or None if it never came back."""
        self._close()
        device = self.config.get("camera_device")
        for _ in range(25):
            time.sleep(0.2)
            if os.path.exists(device) and self._open():
                return self._camera()
        return None

    @Slot(dict, bool)
    def _apply(self, params, hold=False):
        if not self._open():
            return
        errs = []
        try:
            with self._stream(hold)():
                self._camera().setup_ctrls(params, errs)
        except Exception as e:
            log.exception("camera write failed")
            self.error.emit(f"Camera write failed ({e})")
            return
        if errs:
            self.error.emit("; ".join(errs))
        self._refresh()


    @Slot(bool, str)
    def _reassert(self, hold=False, preset=""):
        if not self._open():
            return
        errs = []
        device = self.config.get("camera_device")
        try:
            ok = reassert_exposure(self._camera(), errs, stream=self._stream(hold), reset=lambda: usb_reset(device),
                                   reopen=self._reopen, preset=preset or "1")
        except Exception as e:
            log.exception("exposure re-assert failed")
            self.error.emit(f"Camera exposure fix failed ({e})")
            return
        if not ok:
            self.error.emit("; ".join(errs) or "Camera exposure fix failed")
        self._refresh()


def camera_preset(ctx):
    """(preset number, is it loaded) for the pre-flight check, or None when it is off or unreadable."""
    num = ctx.config.get("camera_preflight_preset")
    snap = getattr(ctx.camera, "latest", None)
    if not num or snap is None or snap.loaded is None or num not in snap.loaded:
        return None
    return num, snap.loaded[num]
