import base64
import os
import tempfile
import unittest
from pathlib import Path

from kaal import keys


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class FileKeyProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "master.key"

    def tearDown(self):
        self.tmp.cleanup()

    def write_key(self, text: str, mode: int = 0o600) -> None:
        self.path.write_text(text, encoding="ascii")
        os.chmod(self.path, mode)

    def test_reads_base64_key_from_owner_only_file(self):
        raw = os.urandom(32)
        self.write_key(b64(raw) + "\n")
        provider = keys.FileKeyProvider(self.path)
        self.assertEqual(provider.get_key(), raw)
        self.assertTrue(provider.key_exists())

    def test_refuses_group_readable_file(self):
        self.write_key(b64(os.urandom(32)), mode=0o640)
        with self.assertRaisesRegex(keys.KeyProviderError, "only by its owner"):
            keys.FileKeyProvider(self.path).get_key()

    def test_refuses_world_readable_file(self):
        self.write_key(b64(os.urandom(32)), mode=0o604)
        with self.assertRaisesRegex(keys.KeyProviderError, "only by its owner"):
            keys.FileKeyProvider(self.path).get_key()

    def test_refuses_symlink(self):
        target = self.dir / "real.key"
        target.write_text(b64(os.urandom(32)), encoding="ascii")
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(keys.KeyProviderError, "regular file"):
            keys.FileKeyProvider(self.path).get_key()

    def test_refuses_wrong_size_key(self):
        self.write_key(b64(os.urandom(16)))
        with self.assertRaisesRegex(keys.KeyProviderError, "wrong size"):
            keys.FileKeyProvider(self.path).get_key()

    def test_refuses_invalid_base64(self):
        self.write_key("not base64 at all!!")
        with self.assertRaisesRegex(keys.KeyProviderError, "not a valid key"):
            keys.FileKeyProvider(self.path).get_key()

    def test_missing_file_is_reported_without_creating_it(self):
        provider = keys.FileKeyProvider(self.path)
        self.assertFalse(provider.key_exists())
        with self.assertRaisesRegex(keys.KeyProviderError, "not found"):
            provider.get_key()
        self.assertFalse(self.path.exists())

    def test_create_writes_owner_only_file_and_round_trips(self):
        provider = keys.FileKeyProvider(self.path)
        created = provider.get_key(create=True)
        self.assertEqual(len(created), keys.KEY_BYTES)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(provider.get_key(), created)
        self.assertEqual(provider.get_key(create=True), created)

    def test_error_messages_never_contain_key_material(self):
        raw = os.urandom(16)
        self.write_key(b64(raw))
        with self.assertRaises(keys.KeyProviderError) as cm:
            keys.FileKeyProvider(self.path).get_key()
        self.assertNotIn(b64(raw), str(cm.exception))


class ProviderFromEnvTests(unittest.TestCase):
    def test_the_key_file_is_named_by_the_environment(self):
        for env in ({"KAAL_KEY_FILE": "/etc/kaal/master.key"}, {"KAAL_KEY_PROVIDER": "file", "KAAL_KEY_FILE": "/etc/kaal/master.key"}):
            provider = keys.provider_from_env(env)
            self.assertIsInstance(provider, keys.FileKeyProvider)
            self.assertEqual(provider.path, Path("/etc/kaal/master.key"))

    def test_a_key_file_must_be_named(self):
        for env in ({}, {"KAAL_KEY_PROVIDER": "file"}, {"KAAL_KEY_FILE": "  "}):
            with self.assertRaisesRegex(keys.KeyProviderError, "KAAL_KEY_FILE"):
                keys.provider_from_env(env)

    def test_the_keychain_is_no_longer_a_place_for_the_key(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "no longer reads its key from the macOS Keychain"):
            keys.provider_from_env({"KAAL_KEY_PROVIDER": "keychain"})
        self.assertFalse(hasattr(keys, "KeychainKeyProvider"))
        self.assertNotIn("security", Path(keys.__file__).read_text().replace("SecurityRunner", ""))

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "KAAL_KEY_PROVIDER"):
            keys.provider_from_env({"KAAL_KEY_PROVIDER": "vault", "KAAL_KEY_FILE": "/k"})


if __name__ == "__main__":
    unittest.main()
