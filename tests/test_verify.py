import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import kaal.core as kaal


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.saved = (kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR, kaal.INDEX_PATH)
        kaal.VAULT = self.vault
        kaal.NOTES_DIR = self.vault / "notes"
        kaal.ATTACH_DIR = self.vault / "attachments"
        kaal.INDEX_PATH = self.vault / "index.json"
        for path in (kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        kaal.save_index({"version": 1, "created": kaal.now_iso(), "notes": []})
        self.public = self.add("Groceries secret-title-marker", "- milk", "public")
        self.private = self.add("Private", "SSN: 078-05-1120", "sensitive")
        self.public_att = self.attach(self.public, "plain-name-marker.txt", "hello", "public")
        self.private_att = self.attach(self.private, "private.txt", "secret attachment", "sensitive")

    def tearDown(self):
        kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR, kaal.INDEX_PATH = self.saved
        self.tmp.cleanup()

    def add(self, title, body, sensitivity):
        result, _ = kaal.call_captured(kaal.add_note, SimpleNamespace(title=title, tags="", body=body, body_file=None, sensitivity=sensitivity))
        return result["id"]

    def attach(self, note_id, name, content, sensitivity):
        src = Path(self.tmp.name) / name
        src.write_text(content, encoding="utf-8")
        result, _ = kaal.call_captured(kaal.attach_file, SimpleNamespace(query=note_id, file=str(src), sensitivity=sensitivity, extract=False, ocr=False))
        return result["attachmentId"]

    def verify(self, deep=False):
        result, _ = kaal.call_captured(kaal.verify_vault, SimpleNamespace(deep=deep))
        return result

    def kinds(self, result):
        return sorted(problem["kind"] for problem in result["problems"])

    def test_clean_vault_is_ok(self):
        result = self.verify(deep=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notes"], 2)
        self.assertEqual(result["attachments"], 2)
        self.assertEqual(result["hashes_checked"], 2)
        self.assertEqual(result["decrypted"], 2)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["warnings"]["loose_permissions"], {"dirs": 0, "files": 0})

    def test_missing_note_file_is_a_problem(self):
        kaal.plaintext_note_path(self.public).unlink()
        result = self.verify()
        self.assertEqual(result["status"], "problems")
        self.assertEqual(self.kinds(result), ["missing_note_file"])
        self.assertEqual(result["problems"][0]["note_id"], self.public)

    def test_changed_plaintext_attachment_fails_its_hash(self):
        meta = kaal.find_notes(self.public)[0]
        att = meta["attachments"][0]
        (kaal.attachment_note_dir(self.public) / att["id"] / att["stored_name"]).write_text("tampered", encoding="utf-8")
        self.assertEqual(self.kinds(self.verify()), ["attachment_hash_mismatch"])

    def test_missing_attachment_files_are_problems(self):
        att_dir = kaal.attachment_note_dir(self.private) / self.private_att
        (att_dir / "original.json").unlink()
        (att_dir / "metadata.json").unlink()
        self.assertEqual(self.kinds(self.verify()), ["missing_attachment_file", "missing_attachment_metadata"])

    def test_deep_check_catches_undecryptable_payloads(self):
        path = kaal.encrypted_note_path(self.private)
        payload = json.loads(path.read_text())
        payload["nonce"] = payload["nonce"][::-1]
        path.write_text(json.dumps(payload))
        self.assertEqual(self.verify(deep=False)["status"], "ok")
        self.assertEqual(self.kinds(self.verify(deep=True)), ["decrypt_failed"])

    def test_orphans_and_loose_permissions_are_warnings(self):
        (kaal.NOTES_DIR / "deadbeef0001.md").write_text("orphan", encoding="utf-8")
        orphan_dir = kaal.attachment_note_dir(self.public) / "feedfacecafe"
        orphan_dir.mkdir(mode=0o700)
        os.chmod(kaal.attachment_note_dir(self.public), 0o755)
        result = self.verify()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["warnings"]["orphan_note_files"], ["deadbeef0001"])
        self.assertEqual(result["warnings"]["orphan_attachment_dirs"], [f"{self.public}/feedfacecafe"])
        self.assertEqual(result["warnings"]["loose_permissions"]["dirs"], 1)

    def test_duplicate_index_ids_are_a_problem(self):
        index = kaal.load_index()
        index["notes"].append(dict(index["notes"][0]))
        kaal.save_index(index)
        self.assertIn("duplicate_note_id", self.kinds(self.verify()))

    def test_output_has_ids_only(self):
        text = json.dumps(self.verify(deep=True))
        self.assertNotIn("secret-title-marker", text)
        self.assertNotIn("plain-name-marker", text)

    def test_cli_exits_2_when_there_are_problems(self):
        kaal.plaintext_note_path(self.public).unlink()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            kaal.verify_vault(SimpleNamespace(deep=False))
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "problems")


if __name__ == "__main__":
    unittest.main()
