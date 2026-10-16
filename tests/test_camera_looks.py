import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from lark import camera_backend as cb
from lark import looks
from lark.config import Config
from lark.golive import LOAD_PRESET, fix_action
from lark.obsbridge import ObsBridge
from lark.pages.camera import CtrlRow, display_text
from lark.preflight import OK, SKIP, WARN, Check, run_checks
from lark.shell import camera_chip, header_state
from tests.test_ui_smoke import FakeThread, pump

APP = QApplication.instance() or QApplication([])


def make_config(port=None):
    cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
    if port:
        cfg.set("obs_port", port)
        cfg.set("obs_password_source", "custom")
        cfg.set("obs_password", "pw")
    return cfg


class PureTests(unittest.TestCase):
    def test_builtin_looks_are_valid(self):
        self.assertEqual(list(looks.BUILTIN), ["Neutral", "Warm", "Punchy", "Soft"])
        for name, p in looks.BUILTIN.items():
            self.assertTrue(looks.valid_params(p), name)

    def test_param_validation_rejects_bad_values(self):
        for bad in ({"gamma": 99}, {"gamma": float("nan")}, {"gamma": float("inf")}, {"gamma": True},
                    {"gamma": "1"}, {"nope": 1}, {"lut_path": "/x/y.txt"}, {"lut_path": "a\x00.png"},
                    {"opacity": 101}, {"sharpness": -0.1}, [], None):
            self.assertFalse(looks.valid_params(bad), bad)
        self.assertTrue(looks.valid_params({"lut_path": "/x/y.PNG", "opacity": 0, "hue_shift": -180}))

    def test_look_names(self):
        for bad in ("", " x", "x ", "a" * 41, "tab\there", 5):
            self.assertFalse(looks.valid_name(bad), repr(bad))
        self.assertFalse(looks.valid_looks({"Warm": {}}))
        self.assertTrue(looks.valid_looks({"Mine": {"gamma": 0.1}}))

    def test_neutral_disables_everything_and_punchy_enables_color_and_sharpness(self):
        d = looks.desired(looks.BUILTIN["Neutral"])
        self.assertEqual({k: v[1] for k, v in d.items()},
                         {looks.COLOR_FILTER: False, looks.SHARP_FILTER: False, looks.LUT_FILTER: False})
        d = looks.desired(looks.BUILTIN["Punchy"])
        self.assertTrue(d[looks.COLOR_FILTER][1] and d[looks.SHARP_FILTER][1])
        self.assertFalse(d[looks.LUT_FILTER][1])
        self.assertEqual(d[looks.COLOR_FILTER][0]["contrast"], 0.25)
        self.assertTrue(looks.desired({"lut_path": "/a/b.png"})[looks.LUT_FILTER][1])

    def test_config_drops_an_invalid_looks_value(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text('{"looks": {"Mine": {"gamma": 50}}, "camera_poll_ms": 5, "camera_preflight_preset": "9"}')
        with self.assertLogs("lark.config", "WARNING"):
            cfg = Config(d / "config.json")
        self.assertEqual(cfg.get("looks"), {})
        self.assertEqual(cfg.get("camera_poll_ms"), 1000)
        self.assertEqual(cfg.get("camera_preflight_preset"), "1")


class FilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = FakeThread()

    @classmethod
    def tearDownClass(cls):
        cls.ft.stop()

    def setUp(self):
        self.fake = self.ft.fake
        self.fake.filters["Camera"] = [{"filterName": "Someone's Denoise", "filterKind": "denoise_filter",
                                        "filterEnabled": True, "filterSettings": {"keep": 1}}]
        self.fake.requests.clear()
        self.cfg = make_config(self.ft.port)
        self.bridge = ObsBridge(self.cfg)
        self.bridge.start()
        self.errors = []
        self.l = looks.Looks(self.bridge, self.cfg, lambda m, e: self.errors.append((m, e)))
        s = NS(c=False)
        self.bridge.state.connect(lambda st, d: setattr(s, "c", st == "connected"))
        self.assertTrue(pump(APP, lambda: s.c))

    def tearDown(self):
        self.bridge.stop()

    def idle(self):
        self.assertTrue(pump(APP, lambda: not self.l._busy and not self.l._dirty))

    def lark_filters(self):
        return {f["filterName"]: f for f in self.fake.filters["Camera"] if f["filterName"].startswith(looks.PREFIX)}

    def reqs(self, kind):
        return [d for t, d in self.fake.requests if t == kind]

    def test_selecting_a_look_creates_only_the_filters_it_needs(self):
        self.l.select("Punchy")
        self.idle()
        f = self.lark_filters()
        self.assertEqual(set(f), {looks.COLOR_FILTER, looks.SHARP_FILTER})
        self.assertEqual(f[looks.COLOR_FILTER]["filterKind"], "color_filter_v2")
        self.assertEqual(f[looks.SHARP_FILTER]["filterKind"], "sharpness_filter_v2")
        self.assertEqual(f[looks.COLOR_FILTER]["filterSettings"]["contrast"], 0.25)
        body = [d for d in self.reqs("CreateSourceFilter") if d["filterName"] == looks.COLOR_FILTER][0]
        self.assertEqual(body["sourceName"], "Camera")
        self.assertEqual(body["filterKind"], "color_filter_v2")
        self.assertEqual(set(body["filterSettings"]),
                         {"gamma", "contrast", "brightness", "saturation", "hue_shift", "opacity"})

    def test_slider_updates_send_settings_and_a_second_look_disables_by_enabled_flag(self):
        self.l.select("Punchy")
        self.idle()
        self.fake.requests.clear()
        self.l.set_param("gamma", 0.5)
        self.idle()
        upd = self.reqs("SetSourceFilterSettings")
        self.assertEqual([u["filterName"] for u in upd], [looks.COLOR_FILTER])
        self.assertEqual(upd[0]["filterSettings"]["gamma"], 0.5)
        self.assertEqual(self.reqs("SetSourceFilterEnabled"), [])
        self.assertEqual(self.lark_filters()[looks.COLOR_FILTER]["filterSettings"]["gamma"], 0.5)
        self.fake.requests.clear()
        self.l.select("Neutral")
        self.idle()
        en = {d["filterName"]: d["filterEnabled"] for d in self.reqs("SetSourceFilterEnabled")}
        self.assertEqual(en, {looks.COLOR_FILTER: False, looks.SHARP_FILTER: False})
        self.assertTrue(all(not f["filterEnabled"] for f in self.lark_filters().values()))
        self.assertEqual(self.reqs("RemoveSourceFilter"), [])

    def test_never_touches_a_filter_lark_did_not_create(self):
        foreign = dict(self.fake.filters["Camera"][0], filterSettings=dict(self.fake.filters["Camera"][0]["filterSettings"]))
        for name in ("Punchy", "Warm", "Neutral", "Soft"):
            self.l.select(name)
            self.l.set_param("lut_path", "/tmp/x.png")
            self.l.set_param("lut_path", "")
            self.idle()
        for t, d in self.fake.requests:
            if "filterName" in d:
                self.assertTrue(d["filterName"].startswith("Lark: "), (t, d))
        self.assertEqual(self.fake.filters["Camera"][0], foreign)
        with self.assertRaises(ValueError):
            looks.Looks._check("Someone's Denoise")
        with self.assertRaises(ValueError):
            looks.Looks._check("Lark: Other")

    def test_a_lark_prefixed_filter_with_the_wrong_name_is_left_alone(self):
        self.fake.filters["Camera"].append({"filterName": "Lark: Mine", "filterKind": "x", "filterEnabled": True,
                                            "filterSettings": {}})
        self.l.select("Punchy")
        self.idle()
        self.assertTrue(all(d["filterName"] != "Lark: Mine" for t, d in self.fake.requests if "filterName" in d))

    def test_lut_filter_only_exists_when_a_file_is_chosen(self):
        self.l.select("Warm")
        self.idle()
        self.assertNotIn(looks.LUT_FILTER, self.lark_filters())
        self.l.set_param("lut_path", "/home/me/film.png")
        self.idle()
        lut = self.lark_filters()[looks.LUT_FILTER]
        self.assertEqual(lut["filterKind"], "clut_filter")
        self.assertEqual(lut["filterSettings"]["image_path"], "/home/me/film.png")
        self.assertTrue(lut["filterEnabled"])
        with self.assertRaises(ValueError):
            self.l.set_param("lut_path", "/etc/passwd")

    def test_rapid_slider_moves_coalesce_and_the_last_value_wins(self):
        self.l.select("Warm")
        self.idle()
        for i in range(20):
            self.l.set_param("contrast", i / 10)
        self.idle()
        self.assertEqual(self.lark_filters()[looks.COLOR_FILTER]["filterSettings"]["contrast"], 1.9)
        self.assertLess(len(self.reqs("GetSourceFilterList")), 6)

    def test_out_of_range_values_clamp_and_junk_is_refused(self):
        self.l.set_param("gamma", 99)
        self.assertEqual(self.l.params["gamma"], 3.0)
        for bad in (float("nan"), "x", None, True):
            with self.assertRaises(ValueError):
                self.l.set_param("gamma", bad)
        with self.assertRaises(KeyError):
            self.l.set_param("filterName", 1)
        self.idle()

    def test_missing_camera_source_reports_an_error(self):
        self.cfg.set("camera_input", "No such source")
        self.l.select("Punchy")
        self.assertTrue(pump(APP, lambda: self.errors))
        self.assertTrue(self.errors[0][1])
        self.assertIn("Looks:", self.errors[0][0])
        self.idle()

    def test_ab_toggle_flips_between_the_last_two_looks(self):
        self.assertFalse(self.l.toggle_ab())
        self.l.select("Punchy")
        self.assertEqual((self.l.active, self.l.other), ("Punchy", "Neutral"))
        self.assertTrue(self.l.toggle_ab())
        self.assertEqual((self.l.active, self.l.other), ("Neutral", "Punchy"))
        self.idle()
        self.l.toggle_ab()
        self.idle()
        self.assertTrue(self.lark_filters()[looks.COLOR_FILTER]["filterEnabled"])

    def test_looks_persist_in_a_temp_config(self):
        self.l.select("Warm")
        self.l.set_param("saturation", 0.6)
        self.l.save_as("Stream night")
        self.idle()
        again = Config(self.cfg.path)
        self.assertEqual(again.get("looks"), {"Stream night": {"hue_shift": -5.0, "saturation": 0.6, "brightness": 0.02,
                                                                 "gamma": 0.04}})
        l2 = looks.Looks(self.bridge, again, lambda m, e: None)
        self.assertEqual((l2.active, l2.other), ("Stream night", "Warm"))
        self.assertEqual(l2.params["saturation"], 0.6)
        self.assertIn("Stream night", l2.names())
        self.assertFalse(l2.modified())
        self.assertEqual(oct(os.stat(self.cfg.path).st_mode & 0o777), "0o600")

    def test_saving_rules_and_delete(self):
        for bad in ("Warm", "", " pad", "x" * 41):
            with self.assertRaises(ValueError):
                self.l.save_as(bad)
        self.l.save_as("Mine")
        self.l.delete("Mine")
        self.assertEqual(self.cfg.get("looks"), {})
        self.assertEqual(self.l.active, "Neutral")
        with self.assertRaises(ValueError):
            self.l.delete("Warm")
        with self.assertRaises(KeyError):
            self.l.delete("Ghost")
        self.idle()

    def test_unknown_saved_active_look_falls_back_to_neutral(self):
        self.cfg.set("look_active", "Gone")
        self.cfg.set("look_other", "Gone")
        l2 = looks.Looks(self.bridge, self.cfg, lambda m, e: None)
        self.assertEqual((l2.active, l2.other), ("Neutral", ""))


FAKE = '''
import os
class M:
    def __init__(self, t, n, v=None): self.text_id, self.name, self.value, self.gui_hidden = t, n, v, False
class C:
    def __init__(self, text_id, name, type, value=None, min=None, max=None, step=None, default=None,
                 inactive=False, readonly=False, tooltip=None, menu=None):
        self.__dict__.update(locals())
class Cat:
    def __init__(self, title, ctrls): self.title, self.ctrls = title, ctrls
class Page:
    def __init__(self, title, categories, target="main"): self.title, self.categories, self.target = title, categories, target
class Preset:
    PRESET_NUMS = ["1", "2", "3", "4"]
    def preset_matches(self, n): return n == "1" and os.path.exists(os.environ["FAKE_PRESET_FLAG"])
class CameraCtrls:
    def __init__(self, device, fd):
        self.device = device
        self.config_preset = Preset()
    def get_ctrl_pages(self):
        with open(os.environ["FAKE_VALUE_FILE"]) as f:
            v = int(f.read())
        return [Page("Basic", [
            Cat("Light", [C("light_color", "Light colour", "color", "#ff8800"),
                          C("light_level", "Light level", "integer", v, 0, 100, 1, 0)]),
            Cat("Remote buttons", [C("remote", "Remote", "button", menu=[M("a", "Up"), M("b", "Down"), M("c", "OK")])]),
            Cat("Device", [C("card", "Card", "info", "EMEET C60E"), C("emeet_c60e_lens", "Lens", "info", "Tele"),
                           C("emeet_c60e_zoom_step", "Zoom step", "menu", "x3",
                             menu=[M("wide", "Wide 1x"), M("x3", "Tele 3x")]),
                           C("zoom_absolute", "Zoom", "integer", 170, 0, 400, 1, 0),
                           C("auto_exposure", "Auto exposure", "menu", "m", menu=[M("m", "Manual Mode"), M("a", "Aperture Priority Mode")])]),
        ])]
    def setup_ctrls(self, params, errs):
        pass
'''


def ctrl(**kw):
    base = dict(text_id="x", name="X", type="info", value=None, min=None, max=None, step=None, default=None,
                inactive=False, readonly=False, tooltip="", menu=[])
    base.update(kw)
    return cb.Ctrl(**base)


class RowTests(unittest.TestCase):
    def row(self, c, calls=None):
        r = CtrlRow(c, lambda *a: calls.append(a) if calls is not None else None, "#7c5cff")
        self.keep.append(r)
        return r

    def setUp(self):
        self.keep = []

    def test_colour_row_shows_a_swatch_and_the_hex(self):
        r = self.row(ctrl(type="color", value="#FF8800"))
        self.assertEqual(r.val.text(), "#ff8800")
        self.assertIn("#ff8800", r.swatch.styleSheet())
        r.update_from(ctrl(type="color", value=(1, 2, 3)))
        self.assertEqual(r.val.text(), "#010203")
        r.update_from(ctrl(type="color", value="not a colour"))
        self.assertEqual(r.val.text(), "not a colour")
        self.assertNotIn("background", r.swatch.styleSheet())

    def test_readonly_colour_has_no_picker(self):
        self.assertIsNone(self.row(ctrl(type="color", value="#000000", readonly=True)).w)
        self.assertIsNotNone(self.row(ctrl(type="color", value="#000000")).w)

    def test_button_row_wraps_and_every_button_fires(self):
        calls = []
        menu = [(f"b{i}", f"Button {i}") for i in range(12)]
        r = self.row(ctrl(text_id="remote", type="button", menu=menu), calls)
        btns = r.findChildren(QPushButton)
        self.assertEqual(len(btns), 12)
        for b in btns:
            b.click()
        self.assertEqual(calls, [("remote", t) for t, _ in menu])

    def test_readonly_rows_render_text(self):
        self.assertEqual(self.row(ctrl(type="info", value="EMEET")).val.text(), "EMEET")
        self.assertEqual(self.row(ctrl(type="boolean", value=1, readonly=True)).val.text(), "On")
        self.assertEqual(display_text(ctrl(type="menu", value="m", menu=[("m", "Manual")])), "Manual")
        self.assertEqual(self.row(ctrl(type="integer", value=7, min=0, max=9, readonly=True)).val.text(), "7")
        self.assertEqual(display_text(ctrl(value=None)), "")


class BackendTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("cameractrls", None)
        self.dir = tempfile.mkdtemp()
        Path(self.dir, "cameractrls.py").write_text(FAKE)
        self.value_file = Path(self.dir, "value")
        self.value_file.write_text("10")
        self.flag = Path(self.dir, "flag")
        os.environ["FAKE_VALUE_FILE"] = str(self.value_file)
        os.environ["FAKE_PRESET_FLAG"] = str(self.flag)

    def tearDown(self):
        sys.modules.pop("cameractrls", None)

    def test_parse_color(self):
        self.assertEqual(cb.parse_color("ABCDEF"), "#abcdef")
        self.assertEqual(cb.parse_color(0xff00), "#00ff00")
        self.assertEqual(cb.parse_color([255, 0, 128]), "#ff0080")
        for bad in (-1, 0x1000000, True, [256, 0, 0], [1, 2], "#12345", "zzzzzz", None, 1.5):
            self.assertIsNone(cb.parse_color(bad), bad)

    def test_snapshot_carries_every_row_type_and_the_summary(self):
        mod = cb.load_module(self.dir)
        snap = cb.snapshot(mod.CameraCtrls("d", 0))
        types = {c.text_id: c.type for p in snap.pages for cat in p.categories for c in cat.ctrls}
        self.assertEqual(types["light_color"], "color")
        self.assertEqual(types["remote"], "button")
        self.assertEqual(types["card"], "info")
        self.assertEqual(cb.summarize(snap), {"lens": "Tele", "zoom": "3x", "exposure": "Manual", "preset": ""})
        self.assertEqual(snap.loaded, {"1": False, "2": False, "3": False, "4": False})
        self.flag.write_text("")
        snap = cb.snapshot(mod.CameraCtrls("d", 0))
        self.assertEqual(cb.summarize(snap)["preset"], "1")

    def test_snapshot_without_preset_api_has_unknown_loaded_state(self):
        class Bare:
            def get_ctrl_pages(self):
                return []
        self.assertIsNone(cb.snapshot(Bare()).loaded)

    def test_summary_falls_back_to_raw_zoom_and_empty_when_unknown(self):
        snap = cb.Snapshot([cb.CtrlPage("B", [cb.Category("C", [ctrl(text_id="zoom_absolute", type="integer", value=285)])])], [])
        self.assertEqual(cb.summarize(snap), {"lens": "", "zoom": "zoom 285", "exposure": "", "preset": ""})
        self.assertEqual(camera_chip(cb.summarize(snap)), "zoom 285")
        self.assertEqual(camera_chip(None), "")
        self.assertEqual(camera_chip({"lens": "Wide", "zoom": "1.5x", "exposure": "Auto"}), "Wide lens, 1.5x, auto exposure")

    def test_header_state_carries_the_camera_chip(self):
        base = dict(conn="connected", scene="Game", stream={}, inputs={}, mic_name="m", twitch_connected=False, viewers=None)
        self.assertEqual(header_state(**base)["camera"], "")
        self.assertEqual(header_state(**base, camera={"lens": "Tele", "zoom": "3x", "exposure": "Manual"})["camera"],
                         "Tele lens, 3x, manual exposure")

    def test_a_remote_press_shows_up_within_two_seconds_and_the_interval_is_a_setting(self):
        dev = Path(self.dir, "dev")
        dev.write_text("")
        cfg = make_config()
        cfg.set("camera_module_dir", self.dir)
        cfg.set("camera_device", str(dev))
        self.assertEqual(cfg.get("camera_poll_ms"), 1000)
        w = cb.CameraWorker(cfg)
        seen = []
        w.snapshot_ready.connect(lambda s: seen.append(
            next(c.value for p in s.pages for cat in p.categories for c in cat.ctrls if c.text_id == "light_level")))
        w.start()
        try:
            self.assertTrue(pump(APP, lambda: seen, 4))
            self.value_file.write_text("77")
            import time
            t0 = time.time()
            self.assertTrue(pump(APP, lambda: seen and seen[-1] == 77, 2.0))
            self.assertLess(time.time() - t0, 2.0)
            w.set_interval(250)
            n = len(seen)
            pump(APP, lambda: False, 1.0)
            self.assertGreaterEqual(len(seen) - n, 3)
            self.assertIsNotNone(w.latest)
        finally:
            w.stop()


class PreflightTests(unittest.TestCase):
    BASE = dict(connected=True, scene="Game", mic_name="m", mic={"muted": False}, mic_signal=True, camera_present=True,
                stream_key_set=True)

    def status(self, **kw):
        return {c.key: c for c in run_checks(**{**self.BASE, **kw})}

    def test_preset_check_states(self):
        self.assertEqual(self.status(camera_preset=("1", True))["preset"].status, OK)
        c = self.status(camera_preset=("1", False))["preset"]
        self.assertEqual((c.status, c.detail), (WARN, "Preset 1 is not loaded"))
        self.assertEqual(self.status()["preset"].status, SKIP)
        self.assertEqual(self.status(camera_present=False, camera_preset=("1", True))["preset"].status, SKIP)
        self.assertEqual(self.status(connected=False)["preset"].status, SKIP)

    def test_an_unloaded_preset_never_blocks_going_live(self):
        from lark.preflight import can_go_live
        self.assertTrue(can_go_live(run_checks(**self.BASE, camera_preset=("1", False))))

    def test_fix_action_offers_to_load_it(self):
        self.assertEqual(fix_action(Check("preset", "x", WARN))[1:], (LOAD_PRESET, None))
        self.assertIsNone(fix_action(Check("preset", "x", SKIP)))
        self.assertIsNone(fix_action(Check("preset", "x", OK)))

    def test_camera_preset_helper(self):
        snap = cb.Snapshot([], [], {"1": True, "2": False})
        ctx = NS(config=make_config(), camera=NS(latest=snap))
        self.assertEqual(cb.camera_preset(ctx), ("1", True))
        ctx.config.set("camera_preflight_preset", "2")
        self.assertEqual(cb.camera_preset(ctx), ("2", False))
        ctx.config.set("camera_preflight_preset", "")
        self.assertIsNone(cb.camera_preset(ctx))
        ctx.config.set("camera_preflight_preset", "3")
        self.assertIsNone(cb.camera_preset(ctx))
        ctx.camera.latest = None
        ctx.config.set("camera_preflight_preset", "1")
        self.assertIsNone(cb.camera_preset(ctx))


if __name__ == "__main__":
    unittest.main()
