import os
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import contextlib
import tempfile
from unittest import mock
from pathlib import Path

from lark import camera_backend as cb
from lark.camera_backend import UDEV_HINT, UsbResetError, reassert_exposure, short_stream, usb_node, usb_reset

MENU = [NS(text_id="manual_mode", name="Manual Mode", value=1, gui_hidden=False),
        NS(text_id="aperture_priority_mode", name="Aperture Priority Mode", value=3, gui_hidden=False)]


def ctrl(text_id, name, type, value, menu=None):
    return NS(text_id=text_id, name=name, type=type, value=value, min=0, max=255, step=1, default=None,
              inactive=False, readonly=False, tooltip="", menu=menu or [])


class FakeCam:
    """Starts in the dark state (manual). Loading the preset sets aperture priority, except the first
    ignore_loads times, when the write is silently dropped like the camera does without a stream."""

    def __init__(self, mode="manual_mode", ignore_loads=0):
        self.mode, self.ignore_loads = mode, ignore_loads
        self.writes = []

    def get_ctrl_pages(self):
        ctrls = [ctrl("auto_exposure", "Auto Exposure", "menu", self.mode, MENU),
                 ctrl("exposure_time_absolute", "Exposure Time, Absolute", "integer", 156), ctrl("gain", "Gain", "integer", 0)]
        return [NS(title="Basic", target="main", categories=[NS(title="Exposure", ctrls=ctrls)])]

    def setup_ctrls(self, params, errs):
        self.writes.append(dict(params))
        if "auto_exposure" in params:
            self.mode = params["auto_exposure"]
        if "preset" in params:
            if self.ignore_loads > 0:
                self.ignore_loads -= 1
            else:
                self.mode = "aperture_priority_mode"


CYCLE = [{"auto_exposure": "manual_mode"}, {"preset": "load_1"}]


class ReassertTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(cb, "_preset_mode", return_value="aperture_priority_mode")
        p.start()
        self.addCleanup(p.stop)

    def run_it(self, cam, **kw):
        errs, sleeps = [], []
        ok = reassert_exposure(cam, errs, sleep=sleeps.append, **kw)
        return ok, errs, sleeps

    def test_cycles_to_manual_then_loads_the_preset(self):
        cam = FakeCam()
        ok, errs, sleeps = self.run_it(cam)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertEqual(cam.writes, CYCLE)
        self.assertEqual(sleeps, [0.3])

    def test_never_writes_manual_exposure_values(self):
        cam = FakeCam(ignore_loads=5)
        self.run_it(cam)
        self.assertFalse(any("exposure_time_absolute" in w or "gain" in w for w in cam.writes))

    def test_uses_the_configured_preset(self):
        cam = FakeCam()
        self.run_it(cam, preset="3")
        self.assertEqual(cam.writes[1], {"preset": "load_3"})

    def test_retries_until_the_mode_matches_the_preset(self):
        cam = FakeCam(ignore_loads=2)
        ok, _, _ = self.run_it(cam)
        self.assertTrue(ok)
        self.assertEqual(cam.writes, CYCLE * 3)

    def test_gives_up_after_three_tries_and_says_so(self):
        cam = FakeCam(ignore_loads=99)
        ok, errs, _ = self.run_it(cam)
        self.assertFalse(ok)
        self.assertEqual(cam.writes, CYCLE * 3)
        self.assertEqual(len(errs), 1)
        self.assertIn("3 tries", errs[0])

    def test_no_exposure_control_is_an_error_not_a_crash(self):
        cam = FakeCam()
        cam.get_ctrl_pages = lambda: [NS(title="B", target="main", categories=[])]
        ok, errs, _ = self.run_it(cam)
        self.assertFalse(ok)
        self.assertEqual(cam.writes, [])
        self.assertTrue(errs)

    def test_library_hook_wins_when_the_fork_has_one(self):
        cam = FakeCam()
        seen = []
        cam.reassert_exposure = lambda errs: seen.append(errs)
        ok, errs, sleeps = self.run_it(cam)
        self.assertTrue(ok)
        self.assertEqual(len(seen), 1)
        self.assertEqual(cam.writes, [])

    def test_preset_mode_unreadable_falls_back_to_the_mode_before(self):
        with mock.patch.object(cb, "_preset_mode", return_value=None):
            cam = FakeCam(mode="aperture_priority_mode")
            ok, _, _ = self.run_it(cam)
        self.assertTrue(ok)


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        p = mock.patch.object(cb, "_preset_mode", return_value="aperture_priority_mode")
        p.start()
        self.addCleanup(p.stop)

    def stream(self):
        @contextlib.contextmanager
        def cm():
            self.events.append("stream+")
            yield
            self.events.append("stream-")
        return cm()

    def test_reset_after_two_misses_then_third_try_on_the_fresh_camera_inside_streams(self):
        cam, fresh = FakeCam(ignore_loads=99), FakeCam()
        errs = []
        ok = reassert_exposure(cam, errs, sleep=lambda s: None, stream=self.stream,
                               reset=lambda: self.events.append("reset"), reopen=lambda: fresh)
        self.assertTrue(ok)
        self.assertEqual(self.events, ["stream+", "stream-", "stream+", "stream-", "reset", "stream+", "stream-"])
        self.assertEqual(cam.writes, CYCLE * 2)
        self.assertEqual(fresh.writes, CYCLE)

    def test_no_reset_when_it_sticks_early(self):
        cam = FakeCam(ignore_loads=1)
        reset = []
        self.assertTrue(reassert_exposure(cam, [], sleep=lambda s: None, reset=lambda: reset.append(1)))
        self.assertEqual(reset, [])

    def test_denied_reset_surfaces_the_rule_and_still_tries_a_third_time(self):
        cam = FakeCam(ignore_loads=99)
        errs = []

        def deny():
            raise UsbResetError(UDEV_HINT)
        ok = reassert_exposure(cam, errs, sleep=lambda s: None, reset=deny)
        self.assertFalse(ok)
        self.assertEqual(cam.writes, CYCLE * 3)
        self.assertIn(UDEV_HINT, errs)

    def test_camera_that_does_not_return_stops_the_fix(self):
        cam = FakeCam(ignore_loads=99)
        errs = []
        ok = reassert_exposure(cam, errs, sleep=lambda s: None, reset=lambda: None, reopen=lambda: None)
        self.assertFalse(ok)
        self.assertEqual(cam.writes, CYCLE * 2)
        self.assertIn("did not come back", errs[-1])


class UsbTests(unittest.TestCase):
    def test_usb_reset_ioctl_and_denial(self):
        calls = []
        usb_reset("/dev/video0", node_for=lambda d: "/dev/bus/usb/001/004", opener=lambda p, f: calls.append(("open", p)) or 99,
                  ioctl=lambda fd, req, arg: calls.append(("ioctl", fd, hex(req), arg)),
                  closer=lambda fd: calls.append(("close", fd)))
        self.assertEqual(calls, [("open", "/dev/bus/usb/001/004"), ("ioctl", 99, "0x5514", 0), ("close", 99)])

        def denied(p, f):
            raise PermissionError(13, "Permission denied")
        with self.assertRaises(UsbResetError) as cm:
            usb_reset("/dev/video0", node_for=lambda d: "/dev/bus/usb/001/004", opener=denied)
        self.assertEqual(str(cm.exception), UDEV_HINT)
        self.assertNotIn("\n", UDEV_HINT)
        with self.assertRaises(UsbResetError):
            usb_reset("/dev/video0", node_for=lambda d: None)

    def test_usb_node_from_sysfs(self):
        root = Path(tempfile.mkdtemp())
        usb = root / "devices" / "usb1" / "1-2"
        iface = usb / "1-2:1.0"
        iface.mkdir(parents=True)
        (usb / "busnum").write_text("1\n")
        (usb / "devnum").write_text("4\n")
        (root / "video4linux" / "video0").mkdir(parents=True)
        (root / "video4linux" / "video0" / "device").symlink_to(iface)
        self.assertEqual(usb_node("/dev/video0", sysfs=str(root / "video4linux"), dev_root="/dev/bus/usb"),
                         "/dev/bus/usb/001/004")
        self.assertIsNone(usb_node("/dev/video9", sysfs=str(root / "video4linux")))


class ShortStreamTests(unittest.TestCase):
    class Proc:
        def __init__(self):
            self.alive, self.terminated = True, False

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated, self.alive = True, False

        def wait(self, t=None):
            return 0

    def test_runs_ffmpeg_as_an_argument_list_and_stops_it(self):
        seen, proc = {}, self.Proc()

        def popen(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return proc
        order = []
        with short_stream("/dev/video0", popen=popen, sleep=lambda s: order.append(("sleep", s))):
            order.append("body")
        self.assertEqual(seen["cmd"], ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-i", "/dev/video0",
                                       "-t", "2", "-f", "null", "-"])
        self.assertNotIn("shell", seen["kw"])
        self.assertEqual(order, [("sleep", 0.5), "body"])
        self.assertTrue(proc.terminated)

    def test_missing_ffmpeg_still_runs_the_body(self):
        def popen(cmd, **kw):
            raise FileNotFoundError("ffmpeg")
        ran = []
        with self.assertLogs("lark.camera", "WARNING"):
            with short_stream("/dev/video0", popen=popen, sleep=lambda s: None):
                ran.append(1)
        self.assertEqual(ran, [1])

    def test_stopped_even_when_the_body_raises(self):
        proc = self.Proc()
        with self.assertRaises(RuntimeError):
            with short_stream("/dev/video0", popen=lambda c, **k: proc, sleep=lambda s: None):
                raise RuntimeError("x")
        self.assertTrue(proc.terminated)


if __name__ == "__main__":
    unittest.main()
