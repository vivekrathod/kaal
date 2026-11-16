import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import kaal.core as kaal


class CoreKeyProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.saved = (kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR, kaal.INDEX_PATH)
        kaal.VAULT = self.vault
        kaal.NOTES_DIR = self.vault / "notes"
        kaal.ATTACH_DIR = self.vault / "attachments"
        kaal.INDEX_PATH = self.vault / "index.json"
        self.saved_env = {k: os.environ.get(k) for k in ("KAAL_KEY_PROVIDER", "KAAL_KEY_FILE")}

    def tearDown(self):
        kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR, kaal.INDEX_PATH = self.saved
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def run_quietly(self, fn, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            fn(*args)
        return out.getvalue(), err.getvalue()

    def use_new_key_file(self) -> Path:
        key_file = Path(self.tmp.name) / "fresh.key"
        os.environ["KAAL_KEY_PROVIDER"] = "file"
        os.environ["KAAL_KEY_FILE"] = str(key_file)
        return key_file

    def test_init_creates_file_key_and_reports_provider_without_the_key(self):
        key_file = self.use_new_key_file()
        stdout, _ = self.run_quietly(kaal.init_vault, SimpleNamespace())
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "initialized")
        self.assertEqual(payload["key_provider"], "file")
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(key_file.read_text().strip(), stdout)

    def test_sensitive_note_round_trips_with_file_key(self):
        self.use_new_key_file()
        self.run_quietly(kaal.init_vault, SimpleNamespace())
        args = SimpleNamespace(title="Private", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="sensitive")
        self.run_quietly(kaal.add_note, args)
        meta = kaal.load_index()["notes"][-1]
        self.assertEqual(meta["storage"], "encrypted")
        self.assertNotIn("078-05-1120", kaal.encrypted_note_path(meta["id"]).read_text())
        self.assertEqual(kaal.load_note(meta)["body"], "SSN: 078-05-1120")

    def test_missing_key_file_dies_with_a_clear_message(self):
        self.use_new_key_file()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            kaal.get_key()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Key file not found", err.getvalue())

    def test_status_reports_provider_and_key_presence(self):
        key_file = self.use_new_key_file()
        self.run_quietly(kaal.init_vault, SimpleNamespace())
        stdout, _ = self.run_quietly(kaal.status, SimpleNamespace())
        payload = json.loads(stdout)
        self.assertEqual(payload["key_provider"], "file")
        self.assertTrue(payload["key_exists"])
        self.assertNotIn("keychain_key_exists", payload)
        self.assertNotIn(key_file.read_text().strip(), stdout)


if __name__ == "__main__":
    unittest.main()
