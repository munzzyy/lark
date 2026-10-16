import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LARK_PAD_DEVICE"] = "/nonexistent/js0"

from PySide6.QtWidgets import QApplication

from lark.app import build_context
from lark.config import CAMERA_BY_ID, Config
from lark.wizard import ALLOWED_REQUESTS, Applier, Plan, build_plan, detect, load_templates, resolve
from lark.wizard_dialog import WizardDialog
from tests.test_ui_smoke import FakeThread, pump

ENV = {"camera_device": CAMERA_BY_ID, "overlay_chat": "http://127.0.0.1:4777/chat",
       "overlay_starting": "http://127.0.0.1:4777/starting"}


def template(tid):
    return next(t for t in load_templates() if t["id"] == tid)


class TemplateTests(unittest.TestCase):
    def test_shipped_templates_load(self):
        ids = [t["id"] for t in load_templates()]
        self.assertEqual(ids[0], "standard")
        self.assertEqual(set(ids), {"standard", "dev-stream", "vertical"})

    def test_standard_has_the_five_scenes(self):
        self.assertEqual([s["name"] for s in template("standard")["scenes"]],
                         ["Game", "Starting Soon", "BRB", "Camera full", "Just Chatting"])

    def test_vertical_is_a_note_only(self):
        t = template("vertical")
        self.assertEqual(t["canvas"], [1080, 1920])
        plan = build_plan(t, [], {}, None, ENV)
        self.assertEqual(plan.work, [])
        self.assertIn("1080x1920", plan.lines()[0])

    def test_dev_stream_uses_a_window_capture(self):
        plan = build_plan(template("dev-stream"), [], {}, None, ENV)
        kinds = {s["name"]: s["kind"] for s in plan.steps if s["op"] == "create_input"}
        self.assertEqual(kinds["Window capture"], "pipewire-window-capture-source")

    def test_resolve_fills_nested_placeholders_only(self):
        out = resolve({"a": ["{x}/y", 3], "b": {"c": "{nope}"}}, {"x": "X"})
        self.assertEqual(out, {"a": ["X/y", 3], "b": {"c": "{nope}"}})

    def test_bad_template_files_are_skipped(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.json").write_text("{not json")
        (d / "b.json").write_text('{"id": "b", "name": "B"}')
        (d / "c.json").write_text('{"id": "c", "name": "C", "scenes": []}')
        with self.assertLogs("lark.wizard", "ERROR"):
            got = load_templates(d)
        self.assertEqual([t["id"] for t in got], ["c"])


class PlanTests(unittest.TestCase):
    def test_existing_scene_is_kept_whole(self):
        plan = build_plan(template("standard"), ["Game", "BRB"], {}, None, ENV)
        ops = {s["scene"]: s["op"] for s in plan.steps if s["op"] in ("keep_scene", "create_scene")}
        self.assertEqual(ops["Game"], "keep_scene")
        self.assertEqual(ops["BRB"], "keep_scene")
        self.assertEqual(ops["Just Chatting"], "create_scene")
        self.assertFalse([s for s in plan.steps if s.get("scene") in ("Game", "BRB") and s["op"] != "keep_scene"])

    def test_camera_is_created_once_then_reused(self):
        plan = build_plan(template("standard"), [], {}, None, ENV)
        cams = [s for s in plan.steps if s.get("name") == "Camera"]
        self.assertEqual([s["op"] for s in cams], ["create_input", "add_existing", "add_existing"])

    def test_existing_input_is_reused_whatever_its_kind(self):
        plan = build_plan(template("standard"), [], {"Camera": "something_else"}, None, ENV)
        self.assertFalse([s for s in plan.steps if s["op"] == "create_input" and s["name"] == "Camera"])

    def test_missing_kind_is_skipped_with_a_reason(self):
        kinds = {"v4l2_input", "browser_source", "text_ft2_source"}
        plan = build_plan(template("standard"), [], {}, kinds, ENV)
        skip = next(s for s in plan.steps if s["op"] == "skip")
        self.assertEqual(skip["name"], "Screen capture")
        self.assertIn("pipewire-screen-capture-source", skip["reason"])

    def test_scene_name_clash_with_an_item_is_skipped(self):
        t = {"id": "t", "name": "T", "scenes": [{"name": "S", "items": [
            {"name": "Game", "kind": "text_ft2_source", "settings": {}}]}]}
        plan = build_plan(t, ["Game"], {}, None, ENV)
        self.assertEqual([s["op"] for s in plan.steps], ["create_scene", "skip"])

    def test_placeholders_resolved_in_settings(self):
        plan = build_plan(template("standard"), [], {}, None, ENV)
        cam = next(s for s in plan.steps if s["op"] == "create_input" and s["name"] == "Camera")
        self.assertEqual(cam["settings"]["device_id"], CAMERA_BY_ID)
        chat = next(s for s in plan.steps if s["op"] == "create_input" and s["name"] == "Lark chat")
        self.assertEqual(chat["settings"]["url"], ENV["overlay_chat"])


class ApplierUnitTests(unittest.TestCase):
    def test_failed_scene_skips_its_items_but_not_the_next_scene(self):
        sent = []

        def request(t, d, cb):
            sent.append((t, d))
            if t == "CreateScene" and d["sceneName"] == "Game":
                cb(False, "nope")
            else:
                cb(True, {"sceneItemId": 1})
        plan = build_plan(template("standard"), [], {}, None, ENV)
        result = {}
        Applier(request, plan, on_done=lambda n, e: result.update(n=n, e=e)).start()
        self.assertEqual(len(result["e"]), 1)
        self.assertNotIn(("CreateInput", "Game"), [(t, d.get("sceneName")) for t, d in sent])
        self.assertIn("Starting Soon", [d["sceneName"] for t, d in sent if t == "CreateScene"])

    def test_transform_failure_is_reported(self):
        def request(t, d, cb):
            cb(t != "SetSceneItemTransform", {"sceneItemId": 1} if t != "SetSceneItemTransform" else "bad")
        plan = build_plan(template("standard"), ["Game", "BRB", "Camera full", "Starting Soon"], {"Camera": "v4l2_input"},
                          None, ENV)
        result = {}
        Applier(request, plan, on_done=lambda n, e: result.update(n=n, e=e)).start()
        self.assertTrue(result["e"])
        self.assertIn("could not place it", result["e"][0])

    def test_nothing_to_do_finishes_at_once(self):
        result = {}
        Applier(lambda *a: self.fail("sent a request"), Plan(), on_done=lambda n, e: result.update(n=n)).start()
        self.assertEqual(result["n"], 0)


class WizardOverObsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("cameractrls", None)
        cls.app = QApplication.instance() or QApplication([])
        cls.ft = FakeThread()
        cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        for k, v in (("obs_port", cls.ft.port), ("obs_password_source", "custom"), ("obs_password", "pw"),
                     ("camera_module_dir", "/nonexistent"), ("twitch_channel", ""),
                     ("overlay_port", 1024 + (os.getpid() % 30000) + 5)):
            cfg.set(k, v)
        cls.ctx = build_context(cfg)
        cls.ctx.bridge.start()
        assert pump(cls.app, lambda: cls.ctx.store.connected and cls.ctx.store.scenes)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.bridge.stop()
        cls.ft.stop()

    def setUp(self):
        f = self.ft.fake
        self.default_scenes = {k: list(v) for k, v in f.scenes.items()}
        self.default_inputs = {k: dict(v) for k, v in f.inputs.items()}
        self.default_ids = dict(f.item_ids)
        self.start = len(f.requests)

    def tearDown(self):
        f = self.ft.fake
        f.scenes, f.inputs, f.item_ids = self.default_scenes, self.default_inputs, self.default_ids
        f.scene_order = list(f.scenes)
        f.transforms.clear()
        f.input_kinds = ["browser_source", "text_ft2_source", "v4l2_input", "pipewire-screen-capture-source",
                         "pipewire-window-capture-source", "pulse_input_capture", "pulse_output_capture"]

    def empty_obs(self):
        f = self.ft.fake
        f.scenes, f.scene_order, f.item_ids = {}, [], {}
        f.inputs = {"Mic (Fifine)": f.inputs["Mic (Fifine)"]}
        f.current = ""

    def found(self):
        got = {}
        detect(self.ctx.bridge.request, lambda ok, p: got.update(ok=ok, p=p))
        self.assertTrue(pump(self.app, lambda: "ok" in got))
        self.assertTrue(got["ok"], got)
        return got["p"]

    def apply(self, tid):
        f = self.found()
        plan = build_plan(template(tid), f["scenes"], f["inputs"], f["kinds"], ENV)
        result = {}
        Applier(self.ctx.bridge.request, plan, on_done=lambda n, e: result.update(n=n, e=e)).start()
        self.assertTrue(pump(self.app, lambda: "n" in result))
        return result

    def sent(self):
        return self.ft.fake.requests[self.start:]

    def test_detect_reads_scenes_inputs_and_kinds(self):
        f = self.found()
        self.assertIn("Game", f["scenes"])
        self.assertEqual(f["inputs"]["Camera"], "v4l2_input")
        self.assertIn("browser_source", f["kinds"])

    def test_detect_survives_obs_without_kind_list(self):
        got = {}
        orig = self.ft.fake.handle_request

        async def no_kinds(t, d):
            return self.ft.fake.fail(204, "x") if t == "GetInputKindList" else await orig(t, d)
        self.ft.fake.handle_request = no_kinds
        try:
            detect(self.ctx.bridge.request, lambda ok, p: got.update(ok=ok, p=p))
            self.assertTrue(pump(self.app, lambda: "ok" in got))
        finally:
            del self.ft.fake.handle_request
        self.assertTrue(got["ok"])
        self.assertIsNone(got["p"]["kinds"])

    def test_only_missing_things_are_created(self):
        f = self.ft.fake
        before = {k: list(v) for k, v in f.scenes.items()}
        res = self.apply("standard")
        self.assertEqual(res["e"], [])
        for name, items in before.items():
            self.assertEqual(f.scenes[name], items, f"{name} was touched")
        self.assertEqual([n for n, _ in f.scenes["Just Chatting"]], ["Camera", "Lark chat"])
        types = [t for t, _ in self.sent()]
        self.assertEqual(types.count("CreateScene"), 1)
        self.assertEqual(types.count("CreateInput"), 1)
        self.assertEqual(types.count("CreateSceneItem"), 1)
        self.assertEqual(f.inputs["Lark chat"]["kind"], "browser_source")

    def test_second_run_has_nothing_to_do(self):
        self.apply("standard")
        f = self.found()
        plan = build_plan(template("standard"), f["scenes"], f["inputs"], f["kinds"], ENV)
        self.assertEqual(plan.work, [])

    def test_never_sends_anything_but_the_allowed_requests(self):
        self.empty_obs()
        self.apply("standard")
        self.apply("dev-stream")
        used = {t for t, _ in self.sent() if not t.startswith("Get")}
        self.assertLessEqual(used, ALLOWED_REQUESTS)
        self.assertFalse([t for t in used if t.startswith(("Remove", "Set")) and t != "SetSceneItemTransform"])

    def test_empty_obs_gets_the_whole_layout_and_transforms(self):
        self.empty_obs()
        f = self.ft.fake
        res = self.apply("standard")
        self.assertEqual(res["e"], [])
        self.assertEqual(f.scene_order, ["Game", "Starting Soon", "BRB", "Camera full", "Just Chatting"])
        self.assertEqual(f.inputs["Screen capture"]["kind"], "pipewire-screen-capture-source")
        self.assertEqual(f.inputs["BRB card"]["kind"], "text_ft2_source")
        cam = f.inputs["Camera"]
        self.assertEqual(cam["kind"], "v4l2_input")
        self.assertEqual(cam["settings"]["device_id"], CAMERA_BY_ID)
        self.assertEqual(f.inputs["Lark chat"]["settings"]["url"], ENV["overlay_chat"])
        types = [t for t, _ in self.sent()]
        self.assertEqual(types.count("CreateScene"), 5)
        self.assertEqual(sum(1 for t, d in self.sent() if t == "CreateInput" and d["inputName"] == "Camera"), 1)
        tf = f.transforms[("Game", f.item_ids[("Game", "Camera")])]
        self.assertEqual((tf["positionX"], tf["positionY"], tf["boundsWidth"], tf["boundsHeight"]), (24, 786, 480, 270))
        self.assertEqual((tf["cropLeft"], tf["cropRight"], tf["cropTop"], tf["cropBottom"]), (320, 320, 180, 180))
        self.assertEqual(1280, 1920 - tf["cropLeft"] - tf["cropRight"])
        self.assertEqual(720, 1080 - tf["cropTop"] - tf["cropBottom"])
        full = f.transforms[("Camera full", f.item_ids[("Camera full", "Camera")])]
        self.assertEqual((full["boundsWidth"], full["boundsHeight"]), (1920, 1080))
        self.assertEqual(len(f.transforms), sum(len(v) for v in f.scenes.values()))

    def test_missing_source_kind_skips_just_that_source(self):
        self.empty_obs()
        f = self.ft.fake
        f.input_kinds.remove("pipewire-screen-capture-source")
        res = self.apply("standard")
        self.assertEqual(res["e"], [])
        self.assertEqual([n for n, _ in f.scenes["Game"]], ["Camera"])
        self.assertNotIn("Screen capture", f.inputs)

    def test_dialog_previews_then_applies(self):
        f = self.ft.fake
        dlg = WizardDialog(self.ctx)
        self.assertTrue(pump(self.app, lambda: dlg.plan is not None))
        self.assertTrue(dlg.apply.isEnabled())
        self.assertIn("Just Chatting", dlg.out.toPlainText())
        self.assertNotIn("Just Chatting", f.scenes)
        dlg.apply.click()
        self.assertTrue(pump(self.app, lambda: "Just Chatting" in f.scenes and "Done." in dlg.out.toPlainText()))
        self.assertTrue(self.ctx.config.get("wizard_done"))
        self.assertTrue(pump(self.app, lambda: "Still missing: nothing" in dlg.out.toPlainText()))
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
