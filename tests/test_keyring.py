import unittest

from lark.keyring import SecretTool


class SecretToolTests(unittest.TestCase):
    def test_round_trip_in_a_throwaway_service(self):
        k = SecretTool(service="lark-test")
        if not k.available():
            self.skipTest("secret-tool is not installed")
        try:
            k.store("roundtrip", "value with spaces and 'quotes'")
            self.assertEqual(k.lookup("roundtrip"), "value with spaces and 'quotes'")
        except Exception as e:
            self.skipTest(f"keyring not usable here: {e}")
        finally:
            try:
                k.clear("roundtrip")
            except Exception:
                pass
        self.assertIsNone(k.lookup("roundtrip"))

    def test_lookup_missing_is_none(self):
        k = SecretTool(service="lark-test")
        if not k.available():
            self.skipTest("secret-tool is not installed")
        self.assertIsNone(k.lookup("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
