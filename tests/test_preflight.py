import unittest

from lark.obsbridge import parse_meters
from lark.preflight import run_checks, can_go_live, OK, WARN, FAIL, SKIP

BASE = dict(connected=True, scene="Game", mic_name="Mic", mic={"muted": False}, mic_signal=True,
            camera_present=True, stream_key_set=True)


def st(**kw):
    return {c.key: c.status for c in run_checks(**{**BASE, **kw})}


class PreflightTests(unittest.TestCase):
    def test_all_good(self):
        r = run_checks(**BASE)
        self.assertTrue(can_go_live(r))
        self.assertEqual(st()["mic"], OK)
        self.assertEqual(st()["title"], SKIP)

    def test_disconnected_blocks_and_skips_rest(self):
        s = st(connected=False)
        self.assertEqual(s["obs"], FAIL)
        self.assertEqual(s["key"], SKIP)
        self.assertFalse(can_go_live(run_checks(**{**BASE, "connected": False})))

    def test_missing_key_blocks_but_mic_only_warns(self):
        self.assertFalse(can_go_live(run_checks(**{**BASE, "stream_key_set": False})))
        r = run_checks(**{**BASE, "mic": {"muted": True}})
        self.assertTrue(can_go_live(r))
        self.assertEqual({c.key: c.status for c in r}["mic"], WARN)

    def test_mic_states(self):
        self.assertEqual(st(mic=None)["mic"], WARN)
        self.assertEqual(st(mic_signal=None)["mic"], WARN)
        self.assertEqual(st(mic_signal=False)["mic"], WARN)

    def test_unknown_key_state_warns(self):
        self.assertEqual(st(stream_key_set=None)["key"], WARN)

    def test_no_scene_blocks(self):
        self.assertFalse(can_go_live(run_checks(**{**BASE, "scene": ""})))

    def test_twitch_title(self):
        self.assertEqual(st(twitch_connected=True, twitch_title="")["title"], WARN)
        self.assertEqual(st(twitch_connected=True, twitch_title="hi")["title"], OK)


class LightTests(unittest.TestCase):
    def test_dark_warns_and_never_blocks(self):
        r = run_checks(**BASE, camera_light=(True, 16.0))
        self.assertEqual({c.key: c.status for c in r}["light"], WARN)
        self.assertTrue(can_go_live(r))

    def test_other_states(self):
        self.assertEqual(st(camera_light=(False, 100.0))["light"], OK)
        self.assertEqual(st()["light"], SKIP)
        self.assertEqual(st(connected=False)["light"], SKIP)

    def test_order_keeps_light_after_preset(self):
        keys = [c.key for c in run_checks(**BASE)]
        self.assertEqual(keys.index("light"), keys.index("preset") + 1)
        self.assertEqual(keys, [c.key for c in run_checks(**{**BASE, "connected": False})])

    def test_fix_button_only_on_the_warning(self):
        from lark.golive import FIX_DARK, fix_action
        dark = next(c for c in run_checks(**BASE, camera_light=(True, 16.0)) if c.key == "light")
        fine = next(c for c in run_checks(**BASE, camera_light=(False, 90.0)) if c.key == "light")
        self.assertEqual(fix_action(dark)[1], FIX_DARK)
        self.assertIsNone(fix_action(fine))


class MeterTests(unittest.TestCase):
    def test_parse(self):
        m = parse_meters({"inputs": [{"inputName": "a", "inputLevelsMul": [[0.1, 0.5, 0.5], [0.2, 0.25, 0.3]]},
                                     {"inputName": "silent", "inputLevelsMul": []}]})
        self.assertAlmostEqual(m["a"][0], -13.98, 1)
        self.assertAlmostEqual(m["a"][1], -6.02, 1)
        self.assertEqual(m["silent"], (-120.0, -120.0))

    def test_empty_and_zero(self):
        self.assertEqual(parse_meters({}), {})
        self.assertEqual(parse_meters({"inputs": [{"inputName": "z", "inputLevelsMul": [[0, 0, 0]]}]})["z"][0], -120.0)


if __name__ == "__main__":
    unittest.main()
