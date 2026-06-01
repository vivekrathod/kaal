import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kaal_module", ROOT / "bin" / "secure-notes.py")
kaal = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(kaal)


class KaalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        kaal.VAULT = self.vault
        kaal.NOTES_DIR = self.vault / "notes"
        kaal.ATTACH_DIR = self.vault / "attachments"
        kaal.INDEX_PATH = self.vault / "index.json"
        kaal.VAULT.mkdir(mode=0o700, exist_ok=True)
        kaal.NOTES_DIR.mkdir(mode=0o700, exist_ok=True)
        kaal.ATTACH_DIR.mkdir(mode=0o700, exist_ok=True)
        kaal.save_index({"version": 1, "created": kaal.now_iso(), "notes": []})

    def tearDown(self):
        self.tmp.cleanup()

    def test_classify_text_marks_ssn_as_sensitive(self):
        result = kaal.classify_text("SSN: 078-05-1120\nDOB: 1970-01-01")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertGreaterEqual(result["score"], kaal.SENSITIVE_THRESHOLD)
        self.assertTrue(any("SSN" in reason for reason in result["reasons"]))

    def test_classify_text_marks_grocery_list_as_public(self):
        result = kaal.classify_text("# Groceries\n\n- milk\n- eggs\n- coffee")
        self.assertEqual(result["sensitivity"], "public")
        self.assertEqual(result["score"], 0)

    def test_add_auto_public_note_stores_plaintext_markdown(self):
        args = SimpleNamespace(title="Groceries", tags="home", body="# Groceries\n\n- milk", sensitivity="auto")
        kaal.add_note(args)

        index = kaal.load_index()
        meta = index["notes"][0]
        self.assertEqual(meta["format"], "markdown")
        self.assertEqual(meta["sensitivity"], "public")
        self.assertEqual(meta["storage"], "plaintext")
        note_file = kaal.note_path(meta["id"])
        self.assertEqual(note_file.suffix, ".md")
        self.assertIn("# Groceries", note_file.read_text())

    def test_add_auto_sensitive_note_stores_encrypted_json(self):
        calls = []
        original_encrypt = kaal.encrypt_bytes
        try:
            def fake_encrypt(data, *, aad=b""):
                calls.append((data, aad))
                return {"alg": "AES-256-GCM", "nonce": "stub", "aad": "stub", "ciphertext": "stub"}
            kaal.encrypt_bytes = fake_encrypt
            args = SimpleNamespace(title="ID", tags="pii", body="SSN: 078-05-1120", sensitivity="auto")
            kaal.add_note(args)
        finally:
            kaal.encrypt_bytes = original_encrypt

        index = kaal.load_index()
        meta = index["notes"][0]
        self.assertEqual(meta["sensitivity"], "sensitive")
        self.assertEqual(meta["storage"], "encrypted")
        self.assertEqual(kaal.note_path(meta["id"]).suffix, ".json")
        self.assertEqual(len(calls), 1)

    def test_add_reads_markdown_body_file(self):
        src = self.vault / "body.md"
        src.write_text("# From file\n\n- item", encoding="utf-8")
        args = SimpleNamespace(title="From File", tags="docs", body=None, body_file=str(src), sensitivity="auto")
        kaal.add_note(args)
        meta = kaal.load_index()["notes"][0]
        self.assertEqual(meta["storage"], "plaintext")
        self.assertIn("# From file", kaal.plaintext_note_path(meta["id"]).read_text())

    def test_extract_text_from_txt_attachment_returns_markdown(self):
        src = self.vault / "sample.txt"
        src.write_text("Account note but no numbers", encoding="utf-8")
        extracted = kaal.extract_attachment_markdown(src, ocr=False, extract=True)
        self.assertIn("# Extracted text: sample.txt", extracted)
        self.assertIn("Account note", extracted)


if __name__ == "__main__":
    unittest.main()
