import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lark.config import Config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.obs = self.d / "obs.json"

    def cfg(self):
        return Config(self.d / "lark" / "config.json", obs_config_paths=[self.obs])

    def test_defaults_and_roundtrip(self):
        c = self.cfg()
        self.assertEqual(c.get("overlay_port"), 4777)
        c.set("overlay_port", 5000)
        c.save()
        self.assertEqual(self.cfg().get("overlay_port"), 5000)
        mode = stat.S_IMODE(os.stat(c.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_camera_watch_settings(self):
        c = self.cfg()
        self.assertEqual((c.get("camera_input"), c.get("camera_dark_threshold")), ("Camera", 22))
        c.set("camera_dark_threshold", 30)
        for bad in (0, 4, 61, True, "22", float("nan")):
            with self.assertRaises(ValueError, msg=bad):
                c.set("camera_dark_threshold", bad)
        with self.assertRaises(ValueError):
            c.set("camera_input", "  ")

    def test_invalid_values_rejected_and_ignored_on_load(self):
        c = self.cfg()
        for k, v in [("overlay_port", 70000), ("overlay_port", True), ("accent", "red"),
                     ("twitch_channel", "a b"), ("obs_host", "x; rm"), ("chat_fade_seconds", -1)]:
            with self.assertRaises(ValueError, msg=(k, v)):
                c.set(k, v)
        with self.assertRaises(KeyError):
            c.set("nope", 1)
        c.path.parent.mkdir(parents=True)
        c.path.write_text(json.dumps({"overlay_port": "x", "accent": "#112233", "junk": 1}))
        with self.assertLogs("lark.config", "WARNING") as logs:
            c2 = self.cfg()
        self.assertTrue(any("overlay_port" in m for m in logs.output))
        self.assertEqual(c2.get("overlay_port"), 4777)
        self.assertEqual(c2.get("accent"), "#112233")
        self.assertNotIn("junk", c2.values)

    def test_corrupt_file_is_kept_aside(self):
        p = self.d / "lark" / "config.json"
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        with self.assertLogs("lark.config", "ERROR"):
            c = self.cfg()
        self.assertEqual(c.get("overlay_port"), 4777)
        self.assertTrue(p.with_suffix(".json.bad").exists())

    def test_obs_password_comes_from_obs_file_not_lark_file(self):
        self.obs.write_text(json.dumps({"server_port": 4460, "server_password": "s3cret", "auth_required": True}))
        c = self.cfg()
        self.assertEqual(c.obs_connection(), ("127.0.0.1", 4460, "s3cret"))
        c.save()
        self.assertNotIn("s3cret", c.path.read_text())

    def test_custom_password_and_port_override(self):
        self.obs.write_text(json.dumps({"server_port": 4460, "server_password": "a"}))
        c = self.cfg()
        c.set("obs_password_source", "custom")
        c.set("obs_password", "mine")
        c.set("obs_port", 1234)
        self.assertEqual(c.obs_connection(), ("127.0.0.1", 1234, "mine"))

    def test_auth_not_required_and_missing_file(self):
        self.obs.write_text(json.dumps({"server_password": "x", "auth_required": False}))
        self.assertEqual(self.cfg().obs_connection()[2], "")
        self.obs.unlink()
        self.assertEqual(self.cfg().obs_connection(), ("127.0.0.1", 4455, ""))
        self.assertIn("not found", self.cfg().obs_file_status())


if __name__ == "__main__":
    unittest.main()
