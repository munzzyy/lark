import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from lark import events as ev
from lark.config import Config


class NormaliseTests(unittest.TestCase):
    def test_each_notification_type(self):
        n = ev.from_notification
        self.assertEqual(n("channel.follow", {"user_name": "A"}, 5)["type"], "follow")
        sub = n("channel.subscribe", {"user_name": "A", "tier": "2000"}, 5)
        self.assertEqual((sub["type"], sub["message"]), ("subscribe", "Tier 2"))
        g = n("channel.subscription.gift", {"user_name": "A", "total": "5", "is_anonymous": True}, 5)
        self.assertEqual((g["user"], g["amount"]), ("Anonymous", 5))
        r = n("channel.subscription.message", {"user_name": "A", "cumulative_months": 3, "message": {"text": "hi"}}, 5)
        self.assertEqual((r["amount"], r["message"]), (3, "hi"))
        c = n("channel.cheer", {"user_name": "A", "bits": 100, "message": "m"}, 5)
        self.assertEqual((c["type"], c["amount"]), ("cheer", 100))
        self.assertEqual(n("channel.raid", {"from_broadcaster_user_name": "R", "viewers": 9}, 5)["user"], "R")
        self.assertEqual(n("stream.online", {}, 5)["type"], "stream_online")
        self.assertEqual(n("stream.offline", {}, 5)["type"], "stream_offline")
        u = n("channel.update", {"title": "T", "category_name": "G"}, 5)
        self.assertEqual(u["message"], "T (G)")

    def test_gifted_subscribe_rows_are_dropped_and_junk_is_survivable(self):
        self.assertIsNone(ev.from_notification("channel.subscribe", {"user_name": "A", "is_gift": True}, 5))
        self.assertIsNone(ev.from_notification("channel.follow", None, 5))
        self.assertIsNone(ev.from_notification("nope", {}, 5))
        for bad in (None, "x", -4, float("nan"), float("inf"), [1], {"a": 1}):
            e = ev.from_notification("channel.cheer", {"user_name": bad, "bits": bad, "message": bad}, 5)
            self.assertIn(e["amount"], (None, 0))
            self.assertIsInstance(e["user"], str)
        e = ev.from_notification("channel.raid", {"from_broadcaster_user_name": "x" * 1000, "viewers": "12"}, 5)
        self.assertEqual((len(e["user"]), e["amount"]), (100, 12))

    def test_control_characters_are_stripped(self):
        e = ev.make_event("follow", "a\x00b\x1b[31m", message="x\ny")
        self.assertNotIn("\x00", e["user"])
        self.assertNotIn("\x1b", e["user"])

    def test_make_event_rejects_bad_type_and_amount(self):
        with self.assertRaises(ValueError):
            ev.make_event("nope")
        with self.assertRaises(ValueError):
            ev.make_event("cheer", amount=True)
        with self.assertRaises(ValueError):
            ev.make_event("cheer", amount=1.5)

    def test_every_type_has_label_title_template_and_sample(self):
        for k in ev.TYPES:
            self.assertTrue(ev.LABELS[k] and ev.TITLES[k] and ev.DEFAULT_TEMPLATES[k])
            s = ev.sample_event(k)
            self.assertTrue(s["test"])
            self.assertTrue(ev.render(ev.DEFAULT_TEMPLATES[k], s))


class TemplateTests(unittest.TestCase):
    def test_render(self):
        e = ev.make_event("cheer", "Bob", 50, "go {user}")
        self.assertEqual(ev.render("{user} sent {amount} {message} {other}", e), "Bob sent 50 go {user} {other}")
        self.assertEqual(ev.render("{amount}|{user}", ev.make_event("follow", "Z")), "|Z")


class SettingsTests(unittest.TestCase):
    def test_validator(self):
        ok = ev.valid_alert_settings
        self.assertTrue(ok({}) and ok({"follow": {}}) and ok({"raid": {"enabled": False, "duration": 5.5, "sound": "", "template": "x"}}))
        for bad in (None, [], {"nope": {}}, {"follow": []}, {"follow": {"extra": 1}}, {"follow": {"enabled": 1}},
                    {"follow": {"duration": 0}}, {"follow": {"duration": 61}}, {"follow": {"duration": True}},
                    {"follow": {"duration": float("nan")}}, {"follow": {"sound": "x" * 513}},
                    {"follow": {"template": 5}}, {"follow": {"template": "x" * 201}}):
            self.assertFalse(ok(bad), bad)
        self.assertFalse(ev.valid_volume(101) or ev.valid_volume(-1) or ev.valid_volume(True) or ev.valid_volume(float("nan")))

    def test_defaults_and_overlay(self):
        vals = {"alert_settings": {"follow": {"duration": 9}}}
        self.assertEqual(ev.alert_settings(vals, "follow")["duration"], 9)
        self.assertTrue(ev.alert_settings(vals, "follow")["enabled"])
        self.assertFalse(ev.alert_settings(vals, "stream_online")["enabled"])
        self.assertEqual(ev.alert_settings({}, "raid")["template"], ev.DEFAULT_TEMPLATES["raid"])

    def test_config_round_trip_and_rejects_bad_values(self):
        with tempfile.TemporaryDirectory() as d:
            c = Config(Path(d) / "c.json")
            self.assertEqual(c.get("alert_settings"), {})
            c.set("alert_settings", {"follow": {"enabled": False}})
            c.set("alert_volume", 40)
            c.save()
            self.assertEqual(Config(Path(d) / "c.json").get("alert_settings"), {"follow": {"enabled": False}})
            with self.assertRaises(ValueError):
                c.set("alert_settings", {"follow": {"duration": 999}})

    def test_sound_problem(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "a.mp3"
            good.write_bytes(b"x")
            self.assertIsNone(ev.sound_problem(""))
            self.assertIsNone(ev.sound_problem(str(good)))
            self.assertIn("exist", ev.sound_problem(str(Path(d) / "gone.mp3")))
            txt = Path(d) / "a.txt"
            txt.write_text("x")
            self.assertIn("Sounds must be", ev.sound_problem(str(txt)))
            big = Path(d) / "big.wav"
            big.write_bytes(b"0" * (ev.MAX_SOUND_BYTES + 1))
            self.assertIn("10 MB", ev.sound_problem(str(big)))
            self.assertIn("exist", ev.sound_problem(d + "/x.mp3"))
            (Path(d) / "dir.mp3").mkdir()
            self.assertIn("exist", ev.sound_problem(str(Path(d) / "dir.mp3")))


class LogTests(unittest.TestCase):
    def test_ring_is_bounded_and_file_gets_real_events_only(self):
        with tempfile.TemporaryDirectory() as d:
            log = ev.EventLog(Path(d) / "events", keep=3)
            for i in range(5):
                log.add(ev.make_event("follow", f"u{i}"))
            log.add(ev.sample_event("raid"))
            self.assertEqual([e["user"] for e in log.recent()], ["u3", "u4", "TestRaider"])
            files = list((Path(d) / "events").glob("events-*.jsonl"))
            self.assertEqual(len(files), 1)
            lines = files[0].read_text().splitlines()
            self.assertEqual([json.loads(x)["user"] for x in lines], [f"u{i}" for i in range(5)])
            self.assertNotIn("TestRaider", files[0].read_text())
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(files[0].parent.stat().st_mode), 0o700)

    def test_reload_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "events"
            folder.mkdir()
            good = json.dumps({"id": "1", "type": "follow", "user": "ok", "amount": None, "message": "", "ts": 5})
            (folder / "events-2026-10-14.jsonl").write_text(
                "\n".join([good, "{not json", json.dumps({"type": "bogus", "id": "2", "ts": 1}), "[1]", ""]))
            (folder / "events-2026-10-15.jsonl").write_bytes(b"\xff\xfe\n")
            log = ev.EventLog(folder)
            with self.assertLogs("lark.events", "WARNING"):
                log.load()
            self.assertEqual([e["user"] for e in log.recent()], ["ok"])

    def test_unwritable_log_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "events"
            blocker.write_text("a file, not a folder")
            log = ev.EventLog(blocker)
            with self.assertLogs("lark.events", "ERROR"):
                log.add(ev.make_event("follow", "x"))
            log.add(ev.make_event("follow", "y"))
            self.assertEqual(len(log.recent()), 2)


if __name__ == "__main__":
    unittest.main()
