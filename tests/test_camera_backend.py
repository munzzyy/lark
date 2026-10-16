import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lark import camera_backend as cb
from lark.config import CAMERA_BY_ID

FAKE = '''
class M:
    def __init__(self, t, n, v): self.text_id, self.name, self.value, self.gui_hidden = t, n, v, v.startswith("save")
class C:
    def __init__(self, text_id, name, type, value=None, min=None, max=None, step=None, default=None,
                 inactive=False, readonly=False, tooltip=None, menu=None):
        self.__dict__.update(locals())
class Cat:
    def __init__(self, title, ctrls): self.title, self.ctrls = title, ctrls
class Page:
    def __init__(self, title, categories, target="main"): self.title, self.categories, self.target = title, categories, target
class CameraCtrls:
    writes = []
    def __init__(self, device, fd): self.device = device
    def get_ctrl_pages(self):
        return [Page("Basic", [Cat("Color", [C("brightness", "Brightness", "integer", 5, 0, 10, 1, 3),
                                              C("auto", "Auto", "boolean", 1, 0, 1)]),
                                Cat("Cameractrlsd", [C("x", "X", "boolean", 1)]), Cat("Empty", [])]),
                Page("Settings", [Cat("Save", [C("preset", "Preset", "button",
                     menu=[M("load_1", "Stream", "load_1"), M("save_1", "Save 1", "save_1")])])], "footer")]
    def setup_ctrls(self, params, errs):
        CameraCtrls.writes.append(params)
'''


class BackendTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("cameractrls", None)
        self.dir = tempfile.mkdtemp()
        Path(self.dir, "cameractrls.py").write_text(FAKE)

    def tearDown(self):
        sys.modules.pop("cameractrls", None)

    def test_missing_module_raises_import_error(self):
        with self.assertRaises(ImportError):
            cb.load_module(tempfile.mkdtemp())

    def test_load_does_not_leave_path_or_write_bytecode(self):
        before = list(sys.path)
        mod = cb.load_module(self.dir)
        self.assertEqual(sys.path, before)
        self.assertFalse(Path(self.dir, "__pycache__").exists())
        self.assertTrue(hasattr(mod, "CameraCtrls"))

    def test_snapshot_filters_hidden_and_splits_presets(self):
        mod = cb.load_module(self.dir)
        snap = cb.snapshot(mod.CameraCtrls("d", 0))
        self.assertEqual([p.title for p in snap.pages], ["Basic"])
        self.assertEqual([c.title for c in snap.pages[0].categories], ["Color"])
        b = snap.pages[0].categories[0].ctrls[0]
        self.assertEqual((b.text_id, b.type, b.value, b.min, b.max, b.default), ("brightness", "integer", 5, 0, 10, 3))
        self.assertEqual(snap.presets, [("load_1", "Stream")])  # the library puts [preset_names] into the menu name

    @unittest.skipUnless(os.path.exists(CAMERA_BY_ID) and
                         os.path.isfile(os.path.expanduser("~/Projects/cameractrls-emeet/cameractrls.py")),
                         "real camera or library not present")
    def test_real_camera_read_only(self):
        sys.modules.pop("cameractrls", None)
        mod = cb.load_module("~/Projects/cameractrls-emeet")
        fd = os.open(CAMERA_BY_ID, os.O_RDWR)
        try:
            snap = cb.snapshot(mod.CameraCtrls(CAMERA_BY_ID, fd))
        finally:
            os.close(fd)
        ids = {c.text_id for p in snap.pages for cat in p.categories for c in cat.ctrls}
        self.assertIn("brightness", ids)
        self.assertEqual([t for t, _ in snap.presets], ["load_1", "load_2", "load_3", "load_4"])


if __name__ == "__main__":
    unittest.main()
