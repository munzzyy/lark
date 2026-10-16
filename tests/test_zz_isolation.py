import os
import unittest
from pathlib import Path

import tests


class Isolation(unittest.TestCase):
    def test_home_is_redirected(self):
        self.assertEqual(os.environ["HOME"], tests._TMP_HOME)
        self.assertNotEqual(Path.home(), tests.REAL_HOME)
        self.assertEqual(Path("~/Videos/OBS").expanduser(), Path(tests._TMP_HOME) / "Videos" / "OBS")

    def test_real_paths_untouched(self):
        tests.isolation_guard()

    def test_guard_detects_a_touch(self):
        key = str(tests.PROTECTED[1])
        saved = tests._BEFORE[key]
        tests._BEFORE[key] = [("/nowhere", 0, 0)]
        try:
            with self.assertRaises(AssertionError):
                tests.isolation_guard()
        finally:
            tests._BEFORE[key] = saved
