"""Highlights: spike detector, thumbnails, and the whole flow against the fake OBS server and synthetic chat."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lark.app import MainWindow, build_context
from lark.config import Config
from lark.highlights import SpikeDetector, make_thumbnail
from tests.test_ui_smoke import FakeThread, pump

FFMPEG = shutil.which("ffmpeg")


def make_clip(path, seconds=2):
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=duration={seconds}:size=64x48:rate=5", str(path)], check=True, timeout=30)


def steady(det, start, seconds, every=5.0):
    t, fired = start, False
    while t < start + seconds:
        fired = det.add(t) or fired
        t += every
    return t, fired


class DetectorTests(unittest.TestCase):
    def det(self, mult=3.0, cool=60, floor=8):
        return SpikeDetector(lambda: (mult, cool, floor))

    def test_steady_chat_never_fires(self):
        d = self.det()
        _, fired = steady(d, 1000.0, 600)
        self.assertFalse(fired)

    def test_burst_fires_once_then_cools_down(self):
        d = self.det()
        t, _ = steady(d, 1000.0, 120)
        hits = [d.add(t + i * 0.2) for i in range(30)]
        self.assertEqual(hits.count(True), 1)
        self.assertEqual(hits.index(True), 6)
        t2 = t + 6
        self.assertFalse(any(d.add(t2 + i * 0.1) for i in range(40)))

    def test_fires_again_after_cooldown(self):
        d = self.det(cool=30)
        t, _ = steady(d, 1000.0, 120)
        self.assertTrue(any(d.add(t + i * 0.2) for i in range(20)))
        t += 200
        t, _ = steady(d, t, 60)
        self.assertTrue(any(d.add(t + i * 0.2) for i in range(20)))

    def test_no_fire_during_warmup(self):
        d = self.det()
        self.assertFalse(any(d.add(1000.0 + i * 0.1) for i in range(50)))

    def test_threshold_adapts_to_busy_chat(self):
        d = self.det(floor=2)
        t, _ = steady(d, 1000.0, 300, every=0.5)
        self.assertFalse(any(d.add(t + i * 0.5) for i in range(12)))

    def test_floor_holds_in_dead_chat(self):
        d = self.det(floor=8)
        t, _ = steady(d, 1000.0, 120, every=30)
        self.assertFalse(any(d.add(t + i) for i in range(6)))
        self.assertTrue(any(d.add(t + 10 + i * 0.1) for i in range(12)))

    def test_peak_tracks_the_busiest_window(self):
        d = self.det(floor=1000)
        for i in range(25):
            d.add(5000.0 + i * 0.1)
        self.assertEqual(d.peak, 25)


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class ThumbnailTests(unittest.TestCase):
    def test_makes_a_jpeg_from_a_tiny_clip(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        clip = d / "clip.mkv"
        make_clip(clip)
        out = d / "thumbs" / "a.jpg"
        self.assertTrue(make_thumbnail(FFMPEG, str(clip), str(out)))
        self.assertEqual(out.read_bytes()[:3], b"\xff\xd8\xff")

    def test_missing_file_is_skipped_without_running_ffmpeg(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        out = d / "a.jpg"
        self.assertFalse(make_thumbnail(FFMPEG, str(d / "nope.mkv"), str(out)))
        self.assertFalse(out.exists())
        self.assertFalse(make_thumbnail("/nonexistent/ffmpeg", "relative.mkv", str(out)))

    def test_garbage_file_fails_cleanly(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        bad = d / "bad.mkv"
        bad.write_bytes(b"not a video")
        out = d / "a.jpg"
        with self.assertLogs("lark.highlights", "WARNING"):
            self.assertFalse(make_thumbnail(FFMPEG, str(bad), str(out)))
        self.assertFalse(out.exists())


def chat(user, text, ts, badges=()):
    return {"id": "", "user": user, "color": "", "text": text, "action": False, "ts": ts, "badges": list(badges)}


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        cls.dir = Path(tempfile.mkdtemp())
        cfg = Config(cls.dir / "config.json")
        for k, v in (("obs_port", cls.ft.port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", ""),
                     ("highlights_dir", str(cls.dir / "obs")),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 5)):
            cfg.set(k, v)
        cls.ctx = build_context(cfg)
        cls.win = MainWindow(cls.ctx, "live")
        cls.win.show()
        cls.ctx.bridge.start()
        s = cls.ctx.store
        assert pump(cls.app, lambda: s.connected and s.scenes and s.stream_key_set is not None)
        cls.mgr = cls.ctx.highlights
        cls.clips = cls.dir / "clips"
        cls.clips.mkdir()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.win.deleteLater()
        cls.ctx.bridge.stop()
        cls.ft.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.fake = self.ft.fake
        self.mgr.entries.clear()
        self.mgr.context.clear()
        self.mgr.detector = SpikeDetector(self.mgr._params)
        self.mgr._pending.clear()
        self.mgr.session = None
        self.ctx.config.set("highlights_enabled", True)
        self.fake.requests.clear()
        self.fake.replay_paths.clear()
        self.fake.last_replay = None
        self.set_replay(False)

    def tearDown(self):
        self.assertTrue(pump(self.app, lambda: not self.mgr._thumbing))

    def set_replay(self, on):
        s = self.ctx.store
        self.ctx.bridge.request("StartReplayBuffer" if on else "StopReplayBuffer")
        self.assertTrue(pump(self.app, lambda: self.fake.replay == on))
        s.refresh_outputs()
        self.assertTrue(pump(self.app, lambda: s.replay == on))

    def clip(self):
        type(self).n += 1
        p = self.clips / f"Replay {self.n}.mkv"
        make_clip(p)
        self.fake.replay_paths.append(str(p))
        return str(p)

    def saved(self):
        return json.loads((self.dir / "obs" / "highlights.json").read_text())

    def requested(self, name):
        return [d for t, d in self.fake.requests if t == name]

    def background_chat(self, start=1_000_000.0):
        t = start
        while t < start + 60:
            self.ctx.services.chat_message.emit(chat("viewer", f"hello {t}", t))
            t += 5.0
        return t

    def burst(self, t, n=14, badges=()):
        for i in range(n):
            self.ctx.services.chat_message.emit(chat(f"fan{i}", f"POG {i}", t + i * 0.2, badges))

    def test_mark_saves_replay_and_writes_the_file_the_shorts_pipeline_reads(self):
        self.set_replay(True)
        clip = self.clip()
        for i in range(25):
            self.ctx.services.chat_message.emit(chat("u", f"line {i}", 1.0 + i))
        e = self.mgr.mark("manual")
        self.assertTrue(pump(self.app, lambda: e["replay"] == clip and e["thumbnail"]))
        self.assertEqual(len(self.requested("SaveReplayBuffer")), 1)
        self.assertTrue(Path(e["thumbnail"]).is_file())
        data = self.saved()
        self.assertEqual(data["version"], 1)
        on_disk = data["highlights"][0]
        self.assertEqual(on_disk["replay"], clip)
        self.assertEqual(on_disk["trigger"], "manual")
        self.assertEqual(len(on_disk["chat"]), 20)
        self.assertEqual(on_disk["chat"][-1], {"user": "u", "text": "line 24"})
        self.assertEqual(on_disk["chat"][0]["text"], "line 5")
        self.assertEqual(set(on_disk), {"id", "time", "ts", "trigger", "chat", "replay", "thumbnail",
                                        "keep_for_shorts", "notes"})
        self.assertEqual(os.stat(self.dir / "obs" / "highlights.json").st_mode & 0o777, 0o600)

    def test_two_marks_get_their_own_files(self):
        self.set_replay(True)
        a, b = self.clip(), self.clip()
        e1 = self.mgr.mark("manual")
        self.assertTrue(pump(self.app, lambda: e1["replay"] == a))
        e2 = self.mgr.mark("manual")
        self.assertTrue(pump(self.app, lambda: e2["replay"] == b))

    def test_chat_spike_saves_the_replay_once(self):
        self.set_replay(True)
        clip = self.clip()
        t = self.background_chat()
        self.burst(t)
        self.assertTrue(pump(self.app, lambda: self.mgr.entries and self.mgr.entries[0]["replay"] == clip))
        self.assertEqual(self.mgr.entries[0]["trigger"], "chat_spike")
        self.burst(t + 3)
        self.app.processEvents()
        self.assertEqual(len(self.mgr.entries), 1)
        self.assertEqual(len(self.requested("SaveReplayBuffer")), 1)

    def test_spike_with_replay_buffer_off_records_without_saving(self):
        t = self.background_chat()
        self.burst(t)
        self.assertEqual(len(self.mgr.entries), 1)
        self.assertIsNone(self.mgr.entries[0]["replay"])
        self.app.processEvents()
        self.assertEqual(self.requested("SaveReplayBuffer"), [])

    def test_spike_does_nothing_when_auto_clip_is_off(self):
        self.ctx.config.set("highlights_enabled", False)
        t = self.background_chat()
        self.burst(t)
        self.assertEqual(self.mgr.entries, [])

    def test_clip_command_only_from_mods_and_streamer(self):
        self.ctx.services.chat_message.emit(chat("rando", "!clip", 5.0))
        self.assertEqual(self.mgr.entries, [])
        self.ctx.services.chat_message.emit(chat("modder", "!clip nice one", 6.0, ["moderator"]))
        self.assertEqual([e["trigger"] for e in self.mgr.entries], ["command"])
        self.ctx.services.chat_message.emit(chat("modder", "!clipping", 7.0, ["moderator"]))
        self.assertEqual(len(self.mgr.entries), 1)

    def test_hotkey_with_buffer_off_starts_it_and_still_records(self):
        self.win.activateWindow()
        QTest.keyClick(self.win, Qt.Key_R, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertTrue(pump(self.app, lambda: self.ctx.store.replay))
        self.assertEqual([e["trigger"] for e in self.mgr.entries], ["hotkey"])
        self.assertEqual(len(self.requested("StartReplayBuffer")), 1)

    def test_hotkey_with_buffer_on_saves_and_records(self):
        self.set_replay(True)
        clip = self.clip()
        self.win.activateWindow()
        QTest.keyClick(self.win, Qt.Key_R, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertTrue(pump(self.app, lambda: self.mgr.entries and self.mgr.entries[0]["replay"] == clip))
        self.assertEqual(self.mgr.entries[0]["trigger"], "hotkey")

    def test_failed_save_keeps_the_entry_and_toasts(self):
        self.set_replay(True)
        toasts = []
        self.ctx.store.toast.connect(lambda m, bad: toasts.append((m, bad)))
        self.fake.replay = False
        e = self.mgr.mark("manual")
        self.assertTrue(pump(self.app, lambda: any(bad and "Save replay" in m for m, bad in toasts)))
        self.assertIsNone(e["replay"])
        self.assertEqual(self.mgr._pending, [])

    def test_live_page_mark_button_and_highlights_page_row(self):
        live = self.win.pages["live"]
        live.mark.click()
        self.assertEqual([e["trigger"] for e in self.mgr.entries], ["manual"])
        page = self.win.pages["highlights"]
        self.win.show_view("highlights")
        self.assertIn(self.mgr.entries[0]["id"], page.rows)
        row = page.rows[self.mgr.entries[0]["id"]]
        row.keep.setChecked(True)
        row.notes.textEdited.emit("great save")
        self.mgr.save()
        saved = self.saved()["highlights"][0]
        self.assertTrue(saved["keep_for_shorts"])
        self.assertEqual(saved["notes"], "great save")
        row.delete.click()
        self.assertEqual(self.saved()["highlights"], [])
        self.assertNotIn(saved["id"], page.rows)

    def test_entries_survive_a_restart(self):
        self.mgr.mark("manual")
        self.mgr.save()
        self.mgr.entries.clear()
        self.mgr.load()
        self.assertEqual(len(self.mgr.entries), 1)

    def test_corrupt_file_is_moved_aside(self):
        path = self.dir / "obs" / "highlights.json"
        path.write_text("{not json")
        with self.assertLogs("lark.highlights", "ERROR"):
            self.mgr.load()
        self.assertEqual(self.mgr.entries, [])
        self.assertTrue(path.with_name("highlights.json.bad").exists())

    def test_stream_summary_written_at_stop(self):
        s, f = self.ctx.store, self.fake
        sessions = self.dir / "obs" / "sessions"
        shutil.rmtree(sessions, ignore_errors=True)
        s.start_stream()
        self.assertTrue(pump(self.app, lambda: s.stream["active"] and self.mgr.session is not None))
        t = self.background_chat(2_000_000.0)
        self.burst(t)
        s.switch_scene("BRB")
        self.assertTrue(pump(self.app, lambda: len(self.mgr.session["scenes"]) == 1))
        s.switch_scene("Game")
        self.assertTrue(pump(self.app, lambda: len(self.mgr.session["scenes"]) == 2))
        s.stop_stream()
        self.assertTrue(pump(self.app, lambda: not s.stream["active"] and self.mgr.session is None))
        files = list(sessions.glob("*.json"))
        self.assertEqual([p.name for p in files], [time.strftime("%Y-%m-%d") + ".json"])
        rec = json.loads(files[0].read_text())["streams"][0]
        self.assertEqual(rec["highlight_count"], 1)
        self.assertGreaterEqual(rec["peak_chat_per_10s"], 14)
        self.assertEqual(rec["chat_messages"], 12 + 14)
        self.assertEqual([c["scene"] for c in rec["scene_changes"]], ["BRB", "Game"])
        self.assertEqual(rec["scene_change_count"], 2)
        s.start_stream()
        self.assertTrue(pump(self.app, lambda: s.stream["active"]))
        s.stop_stream()
        self.assertTrue(pump(self.app, lambda: not s.stream["active"] and self.mgr.session is None))
        self.assertEqual(len(json.loads(files[0].read_text())["streams"]), 2)


if __name__ == "__main__":
    unittest.main()
