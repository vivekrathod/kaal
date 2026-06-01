import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def capture_call(self, fn, *args, **kwargs):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = fn(*args, **kwargs)
        return result, stdout.getvalue(), stderr.getvalue()

    def call_silently(self, fn, *args, **kwargs):
        return self.capture_call(fn, *args, **kwargs)[0]

    def assert_dies(self, fn, *args, **kwargs):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(stdout), redirect_stderr(stderr):
            fn(*args, **kwargs)
        return cm.exception.code, stdout.getvalue(), stderr.getvalue()

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

    def test_invalid_sensitivity_dies(self):
        code, _stdout, stderr = self.assert_dies(kaal.decide_storage, "body", sensitivity="mystery")
        self.assertEqual(code, 1)
        self.assertIn("sensitivity must be", stderr)

    def test_safe_read_text_falls_back_after_unicode_decode_error(self):
        src = self.vault / "utf16.txt"
        src.write_text("hello from utf16", encoding="utf-16")
        self.assertEqual(kaal.safe_read_text(src), "hello from utf16")

    def test_extract_attachment_returns_empty_when_extract_and_ocr_disabled(self):
        src = self.vault / "sample.txt"
        src.write_text("not extracted", encoding="utf-8")
        self.assertEqual(kaal.extract_attachment_markdown(src, ocr=False, extract=False), "")

    def test_extract_attachment_returns_empty_when_extractor_finds_no_text(self):
        src = self.vault / "empty.pdf"
        src.write_bytes(b"%PDF fake")
        original = kaal.run_markitdown
        try:
            kaal.run_markitdown = lambda path: "   "
            self.assertEqual(kaal.extract_attachment_markdown(src, ocr=False, extract=True), "")
        finally:
            kaal.run_markitdown = original

    def test_redact_text_hides_sensitive_values(self):
        redacted = kaal.redact_text(f"SSN: {DUMMY_SSN}\nAccount: 123456789012\nPassport: X1234567")
        self.assertIn("SSN: [REDACTED]", redacted)
        self.assertIn("Account: [REDACTED]", redacted)
        self.assertNotIn(DUMMY_SSN, redacted)
        self.assertNotIn("123456789012", redacted)

    def test_show_plaintext_note_redacts_by_default_and_full_outputs_plaintext(self):
        meta = self.add_note(title="Forced Public", body=f"SSN: {DUMMY_SSN}", sensitivity="public")

        _result, stdout, stderr = self.capture_call(kaal.show_note, SimpleNamespace(query=meta["id"], full=False, json=False))
        self.assertIn("[REDACTED]", stdout)
        self.assertIn("redacted output", stderr)
        self.assertNotIn(DUMMY_SSN, stdout)

        _result, full_stdout, full_stderr = self.capture_call(kaal.show_note, SimpleNamespace(query=meta["id"], full=True, json=False))
        self.assertIn(DUMMY_SSN, full_stdout)
        self.assertEqual(full_stderr, "")

    def test_show_json_includes_attachments_and_redacted_flag(self):
        meta = self.add_note(title="JSON Show", body=f"SSN: {DUMMY_SSN}", sensitivity="public")
        _result, stdout, _stderr = self.capture_call(kaal.show_note, SimpleNamespace(query=meta["id"], full=False, json=True))
        payload = json.loads(stdout)
        self.assertTrue(payload["redacted"])
        self.assertIn("[REDACTED]", payload["body"])
        self.assertEqual(payload["attachments"], [])

    def test_load_note_reads_encrypted_note(self):
        with self.with_fake_crypto():
            meta = self.add_note(title="Encrypted", body=f"SSN: {DUMMY_SSN}")
            note = kaal.load_note(meta)
        self.assertEqual(note["title"], "Encrypted")
        self.assertEqual(note["body"], f"SSN: {DUMMY_SSN}")

    def test_append_to_encrypted_note_stays_encrypted_when_new_text_is_public(self):
        with self.with_fake_crypto():
            meta = self.add_note(title="Encrypted", body=f"SSN: {DUMMY_SSN}")
            self.call_silently(kaal.append_note, SimpleNamespace(query=meta["id"], body="public follow-up"))
            note = kaal.load_note(kaal.load_index()["notes"][0])
        updated = kaal.load_index()["notes"][0]
        self.assertEqual(updated["storage"], "encrypted")
        self.assertEqual(updated["sensitivity"], "sensitive")
        self.assertIn("public follow-up", note["body"])

    def test_append_public_text_to_public_note_stays_plaintext(self):
        meta = self.add_note(title="Public", body="# Start")
        self.call_silently(kaal.append_note, SimpleNamespace(query=meta["id"], body="more public text"))
        updated = kaal.load_index()["notes"][0]
        self.assertEqual(updated["storage"], "plaintext")
        self.assertIn("more public text", kaal.plaintext_note_path(meta["id"]).read_text())

    def test_list_and_search_commands_emit_metadata(self):
        meta = self.add_note(title="Trip Plan", tags="travel,home", body="# Public")
        self.add_note(title="Other", tags="misc", body="# Other")

        _result, list_stdout, _stderr = self.capture_call(kaal.list_notes, SimpleNamespace(tag="travel", json=True))
        listed = json.loads(list_stdout)
        self.assertEqual([n["id"] for n in listed], [meta["id"]])

        _result, search_stdout, _stderr = self.capture_call(kaal.search_notes, SimpleNamespace(query="trip"))
        searched = json.loads(search_stdout)
        self.assertEqual([n["title"] for n in searched], ["Trip Plan"])

        _result, human_stdout, _stderr = self.capture_call(kaal.list_notes, SimpleNamespace(tag="", json=False))
        self.assertIn("Trip Plan", human_stdout)
        self.assertIn("attachments:0", human_stdout)

    def test_require_one_note_errors_for_missing_and_duplicate_matches(self):
        code, _stdout, stderr = self.assert_dies(kaal.require_one_note, "missing")
        self.assertEqual(code, 1)
        self.assertIn("No note found", stderr)

        self.add_note(title="Duplicate Alpha", body="# one")
        self.add_note(title="Duplicate Beta", body="# two")
        code, _stdout, stderr = self.assert_dies(kaal.require_one_note, "Duplicate")
        self.assertEqual(code, 1)
        self.assertIn("Multiple notes match", stderr)

    def test_copy_field_copies_value_without_printing_secret(self):
        meta = self.add_note(title="ID", body=f"ssn: {DUMMY_SSN}", sensitivity="public")
        calls = []
        original_which = kaal.shutil.which
        original_run = kaal.subprocess.run
        try:
            kaal.shutil.which = lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else original_which(name)

            def fake_run(cmd, input=None, text=None, check=None, **kwargs):
                calls.append({"cmd": cmd, "input": input, "text": text, "check": check})
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            kaal.subprocess.run = fake_run
            _result, stdout, _stderr = self.capture_call(kaal.copy_field, SimpleNamespace(query=meta["id"], field="ssn"))
        finally:
            kaal.shutil.which = original_which
            kaal.subprocess.run = original_run

        self.assertEqual(calls[0]["input"], DUMMY_SSN)
        self.assertNotIn(DUMMY_SSN, stdout)
        self.assertIn('"status": "copied"', stdout)

    def test_copy_field_dies_when_pbcopy_missing(self):
        meta = self.add_note(title="ID", body=f"ssn: {DUMMY_SSN}", sensitivity="public")
        original_which = kaal.shutil.which
        try:
            kaal.shutil.which = lambda name: None if name == "pbcopy" else original_which(name)
            code, _stdout, stderr = self.assert_dies(kaal.copy_field, SimpleNamespace(query=meta["id"], field="ssn"))
        finally:
            kaal.shutil.which = original_which
        self.assertEqual(code, 1)
        self.assertIn("pbcopy not found", stderr)

    def test_delete_requires_yes_then_removes_note_and_attachments(self):
        meta = self.add_note(title="Delete Me", body="# Public")
        att_dir = kaal.attachment_note_dir(meta["id"])
        att_dir.mkdir(parents=True)
        (att_dir / "marker.txt").write_text("attachment")

        code, _stdout, stderr = self.assert_dies(kaal.delete_note, SimpleNamespace(query=meta["id"], yes=False))
        self.assertEqual(code, 1)
        self.assertIn("Deletion requires --yes", stderr)

        self.call_silently(kaal.delete_note, SimpleNamespace(query=meta["id"], yes=True))
        self.assertEqual(kaal.load_index()["notes"], [])
        self.assertFalse(kaal.plaintext_note_path(meta["id"]).exists())
        self.assertFalse(att_dir.exists())

    def test_export_encrypted_attachment_round_trips_original_bytes(self):
        meta = self.add_note(title="Public", body="# Public")
        src = self.vault / "secret.txt"
        src.write_text(f"SSN: {DUMMY_SSN}", encoding="utf-8")
        with self.with_fake_crypto():
            self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="auto", extract=True, ocr=False))
            att = kaal.load_index()["notes"][0]["attachments"][0]
            out = self.vault / "secret-exported.txt"
            self.call_silently(kaal.export_attachment, SimpleNamespace(query=meta["id"], attachment=att["id"], output=str(out), force=False))

        self.assertEqual(out.read_text(), f"SSN: {DUMMY_SSN}")

    def test_export_attachment_dies_for_missing_duplicate_and_existing_output(self):
        meta = self.add_note(title="Public", body="# Public")
        src_a = self.vault / "same-a.txt"
        src_b = self.vault / "same-b.txt"
        src_a.write_text("A", encoding="utf-8")
        src_b.write_text("B", encoding="utf-8")
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src_a), sensitivity="public", extract=False, ocr=False))
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src_b), sensitivity="public", extract=False, ocr=False))
        updated = kaal.load_index()["notes"][0]

        code, _stdout, stderr = self.assert_dies(kaal.export_attachment, SimpleNamespace(query=meta["id"], attachment="missing", output="", force=False))
        self.assertEqual(code, 1)
        self.assertIn("No attachment matching", stderr)

        code, _stdout, stderr = self.assert_dies(kaal.export_attachment, SimpleNamespace(query=meta["id"], attachment="same", output="", force=False))
        self.assertEqual(code, 1)
        self.assertIn("Multiple attachments match", stderr)

        out = self.vault / "exists.txt"
        out.write_text("existing", encoding="utf-8")
        code, _stdout, stderr = self.assert_dies(kaal.export_attachment, SimpleNamespace(query=meta["id"], attachment=updated["attachments"][0]["id"], output=str(out), force=False))
        self.assertEqual(code, 1)
        self.assertIn("Output exists", stderr)

    def test_attach_file_dies_when_source_missing(self):
        meta = self.add_note(title="Public", body="# Public")
        code, _stdout, stderr = self.assert_dies(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(self.vault / "missing.txt"), sensitivity="auto", extract=False, ocr=False))
        self.assertEqual(code, 1)
        self.assertIn("Attachment file not found", stderr)

    def test_add_and_append_reject_empty_text(self):
        code, _stdout, stderr = self.assert_dies(kaal.add_note, SimpleNamespace(title="Empty", tags="", body="   ", body_file=None, sensitivity="auto"))
        self.assertEqual(code, 1)
        self.assertIn("Refusing to create an empty note", stderr)

        meta = self.add_note(title="Public", body="# Public")
        code, _stdout, stderr = self.assert_dies(kaal.append_note, SimpleNamespace(query=meta["id"], body="   "))
        self.assertEqual(code, 1)
        self.assertIn("No append text provided", stderr)

    def test_status_reports_counts_without_security_cli(self):
        self.add_note(title="Public", body="# Public")
        original_which = kaal.shutil.which
        try:
            kaal.shutil.which = lambda name: None if name == "security" else original_which(name)
            _result, stdout, _stderr = self.capture_call(kaal.status, SimpleNamespace())
        finally:
            kaal.shutil.which = original_which
        payload = json.loads(stdout)
        self.assertTrue(payload["vault_exists"])
        self.assertTrue(payload["index_exists"])
        self.assertFalse(payload["keychain_key_exists"])
        self.assertEqual(payload["notes"], 1)

    def test_classify_command_reads_file(self):
        src = self.vault / "classify.txt"
        src.write_text(f"SSN: {DUMMY_SSN}", encoding="utf-8")
        _result, stdout, _stderr = self.capture_call(kaal.classify_command, SimpleNamespace(file=str(src)))
        payload = json.loads(stdout)
        self.assertEqual(payload["sensitivity"], "sensitive")

    def test_init_vault_creates_dirs_index_and_reports_fake_key(self):
        fresh = self.vault / "fresh"
        kaal.VAULT = fresh
        kaal.NOTES_DIR = fresh / "notes"
        kaal.ATTACH_DIR = fresh / "attachments"
        kaal.INDEX_PATH = fresh / "index.json"
        original_get_key = kaal.get_key
        try:
            kaal.get_key = lambda create=False: b"x" * kaal.KEY_BYTES
            _result, stdout, _stderr = self.capture_call(kaal.init_vault, SimpleNamespace())
        finally:
            kaal.get_key = original_get_key
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "initialized")
        self.assertTrue(kaal.NOTES_DIR.exists())
        self.assertTrue(kaal.ATTACH_DIR.exists())
        self.assertTrue(kaal.INDEX_PATH.exists())

    def test_build_parser_parses_representative_commands(self):
        parser = kaal.build_parser()
        self.assertEqual(parser.parse_args(["add", "--title", "T", "--body", "B"]).func, kaal.add_note)
        self.assertEqual(parser.parse_args(["classify", "file.txt"]).func, kaal.classify_command)
        self.assertEqual(parser.parse_args(["list", "--json"]).func, kaal.list_notes)
        self.assertEqual(parser.parse_args(["search", "query"]).func, kaal.search_notes)
        self.assertEqual(parser.parse_args(["show", "query", "--full", "--json"]).func, kaal.show_note)
        self.assertEqual(parser.parse_args(["append", "query", "--body", "B"]).func, kaal.append_note)
        self.assertEqual(parser.parse_args(["copy", "query", "--field", "ssn"]).func, kaal.copy_field)
        self.assertEqual(parser.parse_args(["attach", "query", "file", "--extract", "--ocr"]).func, kaal.attach_file)
        self.assertEqual(parser.parse_args(["export-attachment", "query", "att", "--force"]).func, kaal.export_attachment)
        self.assertEqual(parser.parse_args(["delete", "query", "--yes"]).func, kaal.delete_note)

    def test_main_dispatches_to_parsed_command(self):
        original_argv = sys.argv
        try:
            sys.argv = ["kaal", "list", "--json"]
            _result, stdout, _stderr = self.capture_call(kaal.main)
        finally:
            sys.argv = original_argv
        self.assertEqual(json.loads(stdout), [])


if __name__ == "__main__":
    unittest.main()
