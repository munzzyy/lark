"""Every test runs with HOME and the config dir pointed at a temp tree, so nothing can reach the real
~/.config/lark, ~/Videos/OBS or the OBS flatpak data. tests.isolation_guard() fails if it somehow did."""
import atexit
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
PROTECTED = [REAL_HOME / ".config" / "lark", REAL_HOME / "Videos" / "OBS", REAL_HOME / ".var" / "app" / "com.obsproject.Studio"]
# A running Lark or OBS rewrites these, so only their existence/file names are compared, not mtimes.
LIVE = {PROTECTED[0], PROTECTED[2]}


def _snapshot():
    snap = {}
    for root in PROTECTED:
        if not root.exists():
            snap[str(root)] = None
            continue
        entries = []
        found = [root] if root in LIVE and root == PROTECTED[2] else [root, *root.rglob("*")]
        for p in found:
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            entries.append((str(p),) if root in LIVE else (str(p), st.st_mtime_ns, st.st_size))
        snap[str(root)] = sorted(entries)
    return snap


def isolation_guard():
    now = _snapshot()
    changed = [r for r in now if now[r] != _BEFORE[r]]
    if changed:
        raise AssertionError(f"tests touched real paths: {changed}")


_BEFORE = _snapshot()
_TMP_HOME = tempfile.mkdtemp(prefix="lark-test-home-")
os.environ["HOME"] = _TMP_HOME
os.environ["LARK_CONFIG_DIR"] = str(Path(_TMP_HOME) / ".config" / "lark")


def _cleanup():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


atexit.register(_cleanup)


# PySide6 6.12 segfaults in its own exit hook while it frees widgets the tests left behind, after
# every test has already passed. Leave through os._exit with the real result instead of letting that run.
import unittest  # noqa: E402

import PySide6.QtCore  # noqa: E402,F401

_outcome = {"ok": None}
_real_run = unittest.TextTestRunner.run


def _recording_run(self, test):
    result = _real_run(self, test)
    _outcome["ok"] = result.wasSuccessful()
    return result


def _leave_cleanly():
    if _outcome["ok"] is None:
        return
    _cleanup()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if _outcome["ok"] else 1)


unittest.TextTestRunner.run = _recording_run
atexit.register(_leave_cleanly)
