"""Secrets go to the desktop keyring through secret-tool, never to Lark's config file."""
import shutil
import subprocess

SERVICE = "lark"


class KeyringError(Exception):
    pass


class SecretTool:
    def __init__(self, service=SERVICE, timeout=10):
        self.service = service
        self.timeout = timeout

    def available(self):
        return shutil.which("secret-tool") is not None

    def _run(self, args, stdin=None):
        if not self.available():
            raise KeyringError("secret-tool is not installed")
        try:
            return subprocess.run(["secret-tool", *args], input=stdin, capture_output=True, text=True,
                                  timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KeyringError(f"secret-tool failed: {e}") from None

    def store(self, key, value):
        r = self._run(["store", "--label", f"Lark {key}", "service", self.service, "key", key], stdin=value)
        if r.returncode != 0:
            raise KeyringError(f"could not store {key} in the keyring (exit {r.returncode})")

    def lookup(self, key):
        """The stored value, or None if there isn't one."""
        r = self._run(["lookup", "service", self.service, "key", key])
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout

    def clear(self, key):
        self._run(["clear", "service", self.service, "key", key])


class MemoryStore:
    """Stand-in for tests."""

    def __init__(self):
        self.data = {}

    def available(self):
        return True

    def store(self, key, value):
        self.data[key] = value

    def lookup(self, key):
        return self.data.get(key)

    def clear(self, key):
        self.data.pop(key, None)
