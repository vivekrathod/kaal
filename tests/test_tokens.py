import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from kaal import tokens


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tokens.json"
        self.store = tokens.TokenStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_returns_secret_once_and_stores_only_a_hash(self):
        record, secret = self.store.create("hermes", ["read", "write"])
        self.assertTrue(secret.startswith(f"kaal_{record['id']}_"))
        stored = self.path.read_text()
        self.assertNotIn(secret.split("_", 2)[2], stored)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(stored)["tokens"][0]["scopes"], ["read", "write"])

    def test_authenticate_accepts_the_right_token_only(self):
        record, secret = self.store.create("mac-cli", ["read"])
        self.assertEqual(self.store.authenticate(secret)["name"], "mac-cli")
        tampered = secret[:-1] + ("A" if secret[-1] != "A" else "B")
        for bad in ("", "Bearer", "kaal_", "kaal_nope_nope", tampered, secret + "x", f"kaal_{record['id']}"):
            self.assertIsNone(self.store.authenticate(bad), bad)

    def test_revoked_token_stops_working_without_restart(self):
        _record, secret = self.store.create("chrome-extension", ["write"])
        other_view = tokens.TokenStore(self.path)
        self.assertIsNotNone(other_view.authenticate(secret))
        time.sleep(0.01)
        self.store.revoke("chrome-extension")
        self.assertIsNone(other_view.authenticate(secret))

    def test_unknown_scope_and_duplicate_active_name_are_rejected(self):
        with self.assertRaisesRegex(tokens.TokenError, "Unknown scope"):
            self.store.create("x", ["read", "superuser"])
        with self.assertRaisesRegex(tokens.TokenError, "at least one scope"):
            self.store.create("x", [])
        self.store.create("hermes", ["read"])
        with self.assertRaisesRegex(tokens.TokenError, "already exists"):
            self.store.create("hermes", ["read"])

    def test_name_can_be_reused_after_revocation(self):
        self.store.create("hermes", ["read"])
        self.store.revoke("hermes")
        record, _secret = self.store.create("hermes", ["read", "write"])
        self.assertEqual(record["scopes"], ["read", "write"])

    def test_scopes_of_a_token_in_use_can_be_changed_without_a_new_secret(self):
        _record, secret = self.store.create("browser", ["read", "write"])
        other_view = tokens.TokenStore(self.path)
        self.assertEqual(other_view.authenticate(secret)["scopes"], ["read", "write"])
        time.sleep(0.01)
        changed = self.store.set_scopes("browser", ["delete", "read", " write "])
        self.assertEqual(changed["scopes"], ["read", "write", "delete"])
        self.assertNotIn("sha256", changed)
        self.assertEqual(other_view.authenticate(secret)["scopes"], ["read", "write", "delete"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for bad, message in ((["read", "root"], "Unknown scope"), ([], "at least one scope")):
            with self.assertRaisesRegex(tokens.TokenError, message):
                self.store.set_scopes("browser", bad)
        self.assertEqual(other_view.authenticate(secret)["scopes"], ["read", "write", "delete"])
        self.store.revoke("browser")
        with self.assertRaisesRegex(tokens.TokenError, "No active token"):
            self.store.set_scopes("browser", ["read"])
        with self.assertRaisesRegex(tokens.TokenError, "No active token"):
            self.store.set_scopes("nobody", ["read"])

    def test_list_never_includes_hashes(self):
        self.store.create("hermes", ["read"])
        listed = self.store.list()
        self.assertEqual(listed[0]["name"], "hermes")
        self.assertNotIn("sha256", listed[0])

    def test_refuses_a_token_file_others_can_write(self):
        self.store.create("hermes", ["read"])
        os.chmod(self.path, 0o666)
        with self.assertRaisesRegex(tokens.TokenError, "permissions"):
            tokens.TokenStore(self.path).list()

    def test_write_token_file_is_owner_only_and_never_overwrites(self):
        _record, secret = self.store.create("hermes", ["read"])
        out = Path(self.tmp.name) / "hermes.token"
        tokens.write_secret_file(out, secret)
        self.assertEqual(out.read_text().strip(), secret)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            tokens.write_secret_file(out, secret)

    def test_has_scopes(self):
        record, _ = self.store.create("hermes", ["read", "write"])
        self.assertTrue(tokens.has_scopes(record, {"read"}))
        self.assertFalse(tokens.has_scopes(record, {"read", "reveal"}))


if __name__ == "__main__":
    unittest.main()
