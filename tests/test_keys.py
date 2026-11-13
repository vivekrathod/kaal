import base64
import os
import subprocess
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


class FakeSecurity:
    """Stands in for macOS `security`, recording calls without a real Keychain."""

    def __init__(self, stored: str | None = None):
        self.stored = stored
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[0] == "find-generic-password":
            if self.stored is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            out = self.stored + "\n" if "-w" in args else 'keychain: "login"\n'
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[0] == "add-generic-password":
            self.stored = args[args.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected security call: {args[0]}")


class KeychainKeyProviderTests(unittest.TestCase):
    def test_reads_existing_item(self):
        raw = os.urandom(32)
        fake = FakeSecurity(stored=b64(raw))
        provider = keys.KeychainKeyProvider(runner=fake)
        self.assertEqual(provider.get_key(), raw)
        self.assertEqual(fake.calls[0][:5], ["find-generic-password", "-s", "Hermes Secure Notes", "-a", "vault-key-v1"])

    def test_key_exists_never_asks_for_the_secret(self):
        fake = FakeSecurity(stored=b64(os.urandom(32)))
        self.assertTrue(keys.KeychainKeyProvider(runner=fake).key_exists())
        self.assertNotIn("-w", fake.calls[0])

    def test_missing_item_without_create_is_an_error(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "not found"):
            keys.KeychainKeyProvider(runner=FakeSecurity()).get_key()

    def test_create_stores_new_key(self):
        fake = FakeSecurity()
        provider = keys.KeychainKeyProvider(runner=fake)
        created = provider.get_key(create=True)
        self.assertEqual(len(created), keys.KEY_BYTES)
        self.assertEqual(base64.b64decode(fake.stored), created)
        add_call = next(call for call in fake.calls if call[0] == "add-generic-password")
        self.assertIn("-U", add_call)

    def test_wrong_size_item_is_rejected(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "wrong size"):
            keys.KeychainKeyProvider(runner=FakeSecurity(stored=b64(os.urandom(8)))).get_key()


class ProviderFromEnvTests(unittest.TestCase):
    def test_explicit_file_provider(self):
        provider = keys.provider_from_env({"KAAL_KEY_PROVIDER": "file", "KAAL_KEY_FILE": "/etc/kaal/master.key"}, platform="linux")
        self.assertIsInstance(provider, keys.FileKeyProvider)
        self.assertEqual(provider.path, Path("/etc/kaal/master.key"))

    def test_file_provider_requires_a_path(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "KAAL_KEY_FILE"):
            keys.provider_from_env({"KAAL_KEY_PROVIDER": "file"}, platform="linux")

    def test_explicit_keychain_provider_with_custom_names(self):
        provider = keys.provider_from_env({"KAAL_KEY_PROVIDER": "keychain", "KAAL_KEYCHAIN_SERVICE": "svc", "KAAL_KEYCHAIN_ACCOUNT": "acct"}, platform="darwin")
        self.assertIsInstance(provider, keys.KeychainKeyProvider)
        self.assertEqual((provider.service, provider.account), ("svc", "acct"))

    def test_defaults_to_keychain_on_macos(self):
        self.assertIsInstance(keys.provider_from_env({}, platform="darwin"), keys.KeychainKeyProvider)

    def test_defaults_to_file_elsewhere(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "KAAL_KEY_FILE"):
            keys.provider_from_env({}, platform="linux")
        provider = keys.provider_from_env({"KAAL_KEY_FILE": "/k"}, platform="linux")
        self.assertIsInstance(provider, keys.FileKeyProvider)

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(keys.KeyProviderError, "KAAL_KEY_PROVIDER"):
            keys.provider_from_env({"KAAL_KEY_PROVIDER": "vault"}, platform="linux")


if __name__ == "__main__":
    unittest.main()
