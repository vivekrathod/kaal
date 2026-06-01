import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kaal_module", ROOT / "bin" / "secure-notes.py")
kaal = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(kaal)


DUMMY_SSN = "078-05-1120"
DUMMY_CC = "4111 1111 1111 1111"


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

    def call_silently(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def add_note(self, title="Note", body="# Note\n", tags="", sensitivity="auto"):
        args = SimpleNamespace(title=title, tags=tags, body=body, body_file=None, sensitivity=sensitivity)
        self.call_silently(kaal.add_note, args)
        return kaal.load_index()["notes"][-1]

    def with_fake_crypto(self):
        test_case = self

        class FakeCrypto:
            def __enter__(self):
                self.original_encrypt = kaal.encrypt_bytes
                self.original_decrypt = kaal.decrypt_payload

                def fake_encrypt(data, *, aad=b""):
                    payload = {
                        "alg": "FAKE",
                        "aad": aad.decode() if isinstance(aad, bytes) else str(aad),
                        "plaintext": data.decode() if isinstance(data, bytes) else str(data),
                    }
                    return payload

                def fake_decrypt(payload, *, aad=b""):
                    expected = aad.decode() if isinstance(aad, bytes) else str(aad)
                    test_case.assertEqual(payload.get("aad"), expected)
                    return payload["plaintext"].encode()

                kaal.encrypt_bytes = fake_encrypt
                kaal.decrypt_payload = fake_decrypt
                return self

            def __exit__(self, exc_type, exc, tb):
                kaal.encrypt_bytes = self.original_encrypt
                kaal.decrypt_payload = self.original_decrypt

        return FakeCrypto()

    def test_classify_text_marks_ssn_as_sensitive(self):
        result = kaal.classify_text(f"SSN: {DUMMY_SSN}\nDOB: 1970-01-01")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertGreaterEqual(result["score"], kaal.SENSITIVE_THRESHOLD)
        self.assertTrue(any("SSN" in reason for reason in result["reasons"]))

    def test_classify_text_marks_grocery_list_as_public(self):
        result = kaal.classify_text("# Groceries\n\n- milk\n- eggs\n- coffee")
        self.assertEqual(result["sensitivity"], "public")
        self.assertEqual(result["score"], 0)

    def test_classify_text_marks_valid_luhn_credit_card_as_sensitive(self):
        result = kaal.classify_text(f"Card: {DUMMY_CC}")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertTrue(any("CreditCard" in reason for reason in result["reasons"]))

    def test_classify_text_marks_bank_account_and_routing_as_sensitive(self):
        result = kaal.classify_text("Routing: 021000021\nAccount: 123456789012")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertTrue(any("BankRouting" in reason for reason in result["reasons"]))
        self.assertTrue(any("BankAccount" in reason for reason in result["reasons"]))

    def test_classify_text_marks_credentials_as_sensitive(self):
        result = kaal.classify_text("api_key = abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertTrue(any("Credential" in reason for reason in result["reasons"]))

    def test_classify_text_uses_filename_context_for_tax_documents(self):
        result = kaal.classify_text("ordinary looking content", context="2025 W-2.pdf")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertTrue(any("TaxDocument" in reason for reason in result["reasons"]))

    def test_decide_storage_respects_public_override_for_sensitive_text(self):
        storage, classification = kaal.decide_storage(f"SSN: {DUMMY_SSN}", sensitivity="public")
        self.assertEqual(storage, "plaintext")
        self.assertEqual(classification["sensitivity"], "public")
        self.assertTrue(classification["reasons"][0], "forced public")

    def test_decide_storage_respects_sensitive_override_for_public_text(self):
        storage, classification = kaal.decide_storage("# Groceries", sensitivity="sensitive")
        self.assertEqual(storage, "encrypted")
        self.assertEqual(classification["sensitivity"], "sensitive")
        self.assertTrue(classification["reasons"][0], "forced sensitive")

    def test_add_auto_public_note_stores_plaintext_markdown(self):
        meta = self.add_note(title="Groceries", tags="home", body="# Groceries\n\n- milk")
        self.assertEqual(meta["format"], "markdown")
        self.assertEqual(meta["sensitivity"], "public")
        self.assertEqual(meta["storage"], "plaintext")
        note_file = kaal.note_path(meta["id"])
        self.assertEqual(note_file.suffix, ".md")
        self.assertIn("# Groceries", note_file.read_text())

    def test_add_auto_sensitive_note_stores_encrypted_json(self):
        with self.with_fake_crypto():
            meta = self.add_note(title="ID", tags="pii", body=f"SSN: {DUMMY_SSN}")
        self.assertEqual(meta["sensitivity"], "sensitive")
        self.assertEqual(meta["storage"], "encrypted")
        self.assertEqual(kaal.note_path(meta["id"]).suffix, ".json")

    def test_add_reads_markdown_body_file(self):
        src = self.vault / "body.md"
        src.write_text("# From file\n\n- item", encoding="utf-8")
        args = SimpleNamespace(title="From File", tags="docs", body=None, body_file=str(src), sensitivity="auto")
        self.call_silently(kaal.add_note, args)
        meta = kaal.load_index()["notes"][0]
        self.assertEqual(meta["storage"], "plaintext")
        self.assertIn("# From file", kaal.plaintext_note_path(meta["id"]).read_text())

    def test_append_sensitive_text_upgrades_public_note_to_encrypted(self):
        meta = self.add_note(title="Mixed", body="# Public start")
        self.assertTrue(kaal.plaintext_note_path(meta["id"]).exists())
        with self.with_fake_crypto():
            self.call_silently(kaal.append_note, SimpleNamespace(query=meta["id"], body=f"SSN: {DUMMY_SSN}"))
        updated = kaal.load_index()["notes"][0]
        self.assertEqual(updated["storage"], "encrypted")
        self.assertEqual(updated["sensitivity"], "sensitive")
        self.assertFalse(kaal.plaintext_note_path(meta["id"]).exists())
        self.assertTrue(kaal.encrypted_note_path(meta["id"]).exists())

    def test_extract_text_from_txt_attachment_returns_markdown(self):
        src = self.vault / "sample.txt"
        src.write_text("Account note but no numbers", encoding="utf-8")
        extracted = kaal.extract_attachment_markdown(src, ocr=False, extract=True)
        self.assertIn("# Extracted text: sample.txt", extracted)
        self.assertIn("Account note", extracted)

    def test_extract_pdf_uses_markitdown_hook(self):
        src = self.vault / "sample.pdf"
        src.write_bytes(b"%PDF fake for unit test")
        original = kaal.run_markitdown
        try:
            kaal.run_markitdown = lambda path: "PDF text from MarkItDown"
            extracted = kaal.extract_attachment_markdown(src, ocr=False, extract=True)
        finally:
            kaal.run_markitdown = original
        self.assertIn("Method: markitdown", extracted)
        self.assertIn("PDF text from MarkItDown", extracted)

    def test_extract_image_uses_tesseract_hook(self):
        src = self.vault / "scan.jpg"
        src.write_bytes(b"fake image for unit test")
        original = kaal.run_tesseract
        try:
            kaal.run_tesseract = lambda path: "OCR text from Tesseract"
            extracted = kaal.extract_attachment_markdown(src, ocr=True, extract=False)
        finally:
            kaal.run_tesseract = original
        self.assertIn("Method: tesseract", extracted)
        self.assertIn("OCR text from Tesseract", extracted)

    def test_public_attachment_with_extract_preserves_original_and_sidecar_plaintext(self):
        meta = self.add_note(title="Public", body="# Public")
        src = self.vault / "public.txt"
        src.write_text("Harmless project note", encoding="utf-8")
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="auto", extract=True, ocr=False))

        updated = kaal.load_index()["notes"][0]
        att = updated["attachments"][0]
        self.assertEqual(att["storage"], "plaintext")
        att_dir = kaal.attachment_note_dir(meta["id"]) / att["id"]
        self.assertEqual((att_dir / "public.txt").read_text(), "Harmless project note")
        self.assertIn("Harmless project note", (att_dir / "extracted.md").read_text())

    def test_sensitive_attachment_with_extract_encrypts_original_and_sidecar(self):
        meta = self.add_note(title="Public", body="# Public")
        src = self.vault / "sensitive.txt"
        src.write_text(f"SSN: {DUMMY_SSN}", encoding="utf-8")
        with self.with_fake_crypto():
            self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="auto", extract=True, ocr=False))

        updated = kaal.load_index()["notes"][0]
        att = updated["attachments"][0]
        self.assertEqual(att["storage"], "encrypted")
        att_dir = kaal.attachment_note_dir(meta["id"]) / att["id"]
        self.assertTrue((att_dir / "original.json").exists())
        self.assertTrue((att_dir / "extracted.md.json").exists())
        self.assertFalse((att_dir / "sensitive.txt").exists())

    def test_attachment_to_sensitive_parent_is_encrypted_even_when_harmless(self):
        with self.with_fake_crypto():
            meta = self.add_note(title="Private", body=f"SSN: {DUMMY_SSN}")
            src = self.vault / "harmless.txt"
            src.write_text("Harmless project note", encoding="utf-8")
            self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="auto", extract=True, ocr=False))

        updated = kaal.load_index()["notes"][0]
        att = updated["attachments"][0]
        self.assertEqual(att["storage"], "encrypted")
        self.assertTrue(any("parent note" in reason for reason in att["classification"]["reasons"]))

    def test_export_plaintext_attachment_round_trips_original_bytes(self):
        meta = self.add_note(title="Public", body="# Public")
        src = self.vault / "public.txt"
        src.write_text("Harmless project note", encoding="utf-8")
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="public", extract=True, ocr=False))
        att = kaal.load_index()["notes"][0]["attachments"][0]
        out = self.vault / "exported.txt"

        self.call_silently(kaal.export_attachment, SimpleNamespace(query=meta["id"], attachment=att["id"], output=str(out), force=False))

        self.assertEqual(out.read_text(), "Harmless project note")


if __name__ == "__main__":
    unittest.main()
