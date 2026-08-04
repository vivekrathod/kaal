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

    def test_plaintext_attachment_with_overlong_display_name_uses_safe_stored_name(self):
        meta = self.add_note(title="Public", body="# Public")
        src = self.vault / "resource.bin"
        src.write_bytes(b"resource bytes")
        display_name = "?ui=2&" + "saddbat=" + ("x" * 340) + ".1&permmsgid=" + ("y" * 120)

        att = kaal.attach_file_to_note(meta, src, sensitivity="public", attachment_name=display_name)

        self.assertEqual(att["name"], display_name)
        self.assertLessEqual(len(att["stored_name"].encode("utf-8")), 180)
        self.assertNotEqual(att["stored_name"], display_name)
        self.assertTrue((kaal.attachment_note_dir(meta["id"]) / att["id"] / att["stored_name"]).exists())
        out = self.vault / "exported-long-name.bin"
        self.call_silently(kaal.export_attachment, SimpleNamespace(query=meta["id"], attachment=att["id"], output=str(out), force=False))
        self.assertEqual(out.read_bytes(), b"resource bytes")

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
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src_a), sensitivity="public", no_extract=True, no_ocr=True))
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src_b), sensitivity="public", no_extract=True, no_ocr=True))
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
        code, _stdout, stderr = self.assert_dies(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(self.vault / "missing.txt"), sensitivity="auto", no_extract=True, no_ocr=True))
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

    def make_joplin_raw_export(self):
        export_dir = self.vault / "joplin-raw"
        resources = export_dir / "resources"
        resources.mkdir(parents=True)
        folder_id = "11111111111111111111111111111111"
        note_id = "22222222222222222222222222222222"
        sensitive_note_id = "33333333333333333333333333333333"
        resource_id = "44444444444444444444444444444444"
        tag_id = "55555555555555555555555555555555"
        note_tag_id = "66666666666666666666666666666666"
        (export_dir / f"{folder_id}.md").write_text(
            "Personal\n\n"
            f"id: {folder_id}\n"
            "parent_id: \n"
            "created_time: 2022-01-01T00:00:00.000Z\n"
            "updated_time: 2022-01-02T00:00:00.000Z\n"
            "type_: 2\n",
            encoding="utf-8",
        )
        (export_dir / f"{note_id}.md").write_text(
            "Trip Plan\n\n"
            "Pack snacks and a printed itinerary.\n\n"
            f"![receipt.jpg](:/{resource_id})\n\n"
            f"id: {note_id}\n"
            f"parent_id: {folder_id}\n"
            "created_time: 2022-01-03T00:00:00.000Z\n"
            "updated_time: 2022-01-04T00:00:00.000Z\n"
            "type_: 1\n",
            encoding="utf-8",
        )
        (export_dir / f"{sensitive_note_id}.md").write_text(
            "Identity\n\n"
            f"SSN: {DUMMY_SSN}\n\n"
            f"id: {sensitive_note_id}\n"
            f"parent_id: {folder_id}\n"
            "created_time: 2022-01-05T00:00:00.000Z\n"
            "updated_time: 2022-01-06T00:00:00.000Z\n"
            "type_: 1\n",
            encoding="utf-8",
        )
        (export_dir / f"{resource_id}.md").write_text(
            "receipt.jpg\n\n"
            f"id: {resource_id}\n"
            "mime: image/jpeg\n"
            "file_extension: jpg\n"
            "size: 12\n"
            "type_: 4\n",
            encoding="utf-8",
        )
        (export_dir / f"{tag_id}.md").write_text(
            "travel\n\n"
            f"id: {tag_id}\n"
            "type_: 5\n",
            encoding="utf-8",
        )
        (export_dir / f"{note_tag_id}.md").write_text(
            "\n"
            f"id: {note_tag_id}\n"
            f"note_id: {note_id}\n"
            f"tag_id: {tag_id}\n"
            "type_: 6\n",
            encoding="utf-8",
        )
        (resources / f"{resource_id}.jpg").write_bytes(b"fake jpg data")
        return export_dir

    def test_parse_joplin_raw_item_splits_body_from_metadata(self):
        item = kaal.parse_joplin_raw_item(
            "Title\n\nBody line\n\nid: 22222222222222222222222222222222\nparent_id: 11111111111111111111111111111111\ntype_: 1\n"
        )
        self.assertEqual(item["title"], "Title")
        self.assertEqual(item["body"], "Body line")
        self.assertEqual(item["props"]["type_"], "1")
        self.assertEqual(item["props"]["parent_id"], "11111111111111111111111111111111")

    def test_import_joplin_raw_dry_run_reports_counts_without_writing_notes(self):
        export_dir = self.make_joplin_raw_export()
        _result, stdout, _stderr = self.capture_call(
            kaal.import_joplin_raw,
            SimpleNamespace(path=str(export_dir), dry_run=True, tags="", sensitivity="auto", no_extract=True, no_ocr=True),
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "dry-run")
        self.assertEqual(payload["notes"], 2)
        self.assertEqual(payload["folders"], 1)
        self.assertEqual(payload["resources"], 1)
        self.assertEqual(payload["tags"], 1)
        self.assertEqual(payload["referenced_resources"], 1)
        self.assertEqual(payload["likely_sensitive_notes"], 1)
        self.assertEqual(kaal.load_index()["notes"], [])

    def test_import_joplin_raw_dry_run_ignores_appledouble_sidecars(self):
        export_dir = self.make_joplin_raw_export()
        (export_dir / "._22222222222222222222222222222222.md").write_bytes(b"\x00\x05AppleDouble metadata")
        _result, stdout, _stderr = self.capture_call(
            kaal.import_joplin_raw,
            SimpleNamespace(path=str(export_dir), dry_run=True, tags="", sensitivity="auto", no_extract=True, no_ocr=True),
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["notes"], 2)
        self.assertFalse(any("._" in warning for warning in payload["warnings"]))

    def test_import_joplin_raw_dry_run_reads_large_items_before_metadata(self):
        export_dir = self.make_joplin_raw_export()
        large_note_id = "77777777777777777777777777777777"
        large_resource_id = "88888888888888888888888888888888"
        (export_dir / f"{large_note_id}.md").write_text(
            "Large clipped page\n\n"
            + ("Long body line\n" * 90000)
            + f"\n:/ {large_resource_id}\n".replace(":/ ", ":/")
            + f"\nid: {large_note_id}\n"
            "parent_id: 11111111111111111111111111111111\n"
            "type_: 1\n",
            encoding="utf-8",
        )
        (export_dir / f"{large_resource_id}.md").write_text(
            "large.pdf\n\n"
            + ("ocr text line\n" * 90000)
            + f"\nid: {large_resource_id}\n"
            "mime: application/pdf\n"
            "file_extension: pdf\n"
            "size: 9\n"
            "type_: 4\n",
            encoding="utf-8",
        )
        (export_dir / "resources" / f"{large_resource_id}.pdf").write_bytes(b"fake pdf")
        _result, stdout, _stderr = self.capture_call(
            kaal.import_joplin_raw,
            SimpleNamespace(path=str(export_dir), dry_run=True, tags="", sensitivity="auto", no_extract=True, no_ocr=True),
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["notes"], 3)
        self.assertEqual(payload["resources"], 2)
        self.assertEqual(payload["referenced_resources"], 2)
        self.assertEqual(payload["unresolved_resources"], 0)
        self.assertEqual(payload["warnings"], [])

    def test_import_joplin_raw_imports_notes_tags_folders_and_resources(self):
        export_dir = self.make_joplin_raw_export()
        with self.with_fake_crypto():
            _result, stdout, _stderr = self.capture_call(
                kaal.import_joplin_raw,
                SimpleNamespace(path=str(export_dir), dry_run=False, incremental=False, tags="migrated", sensitivity="auto", no_extract=True, no_ocr=True),
            )
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "imported")
        self.assertEqual(payload["imported_notes"], 2)
        notes = kaal.load_index()["notes"]
        by_title = {n["title"]: n for n in notes}
        self.assertEqual(by_title["Trip Plan"]["storage"], "plaintext")
        self.assertEqual(by_title["Identity"]["storage"], "encrypted")
        self.assertEqual(by_title["Trip Plan"]["source"], {"type": "joplin", "id": "22222222222222222222222222222222"})
        self.assertIn("joplin", by_title["Trip Plan"]["tags"])
        self.assertIn("migrated", by_title["Trip Plan"]["tags"])
        self.assertIn("travel", by_title["Trip Plan"]["tags"])
        self.assertIn("joplin-notebook-personal", by_title["Trip Plan"]["tags"])
        self.assertEqual(by_title["Trip Plan"]["attachments"][0]["name"], "receipt.jpg")
        self.assertEqual(by_title["Trip Plan"]["attachments"][0]["source"], {"type": "joplin", "note_id": "22222222222222222222222222222222", "resource_id": "44444444444444444444444444444444"})
        body = kaal.plaintext_note_path(by_title["Trip Plan"]["id"]).read_text(encoding="utf-8")
        self.assertIn("Pack snacks", body)
        self.assertNotIn("type_: 1", body)

    def test_import_joplin_raw_ocr_extracts_image_resources_by_default(self):
        export_dir = self.make_joplin_raw_export()
        original = kaal.run_tesseract
        try:
            kaal.run_tesseract = lambda path: "receipt OCR text"
            with self.with_fake_crypto():
                _result, stdout, _stderr = self.capture_call(
                    kaal.import_joplin_raw,
                    SimpleNamespace(path=str(export_dir), dry_run=False, incremental=False, tags="", sensitivity="auto"),
                )
        finally:
            kaal.run_tesseract = original
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "imported")
        trip = {n["title"]: n for n in kaal.load_index()["notes"]}["Trip Plan"]
        att = trip["attachments"][0]
        self.assertTrue(att["extracted_markdown"])
        sidecar = kaal.attachment_note_dir(trip["id"]) / att["id"] / "extracted.md"
        self.assertIn("receipt OCR text", sidecar.read_text(encoding="utf-8"))

    def test_ocr_attachments_updates_existing_attachment_sidecars_separately(self):
        meta = self.add_note(title="Public", body="# Public")
        src = self.vault / "scan.jpg"
        src.write_bytes(b"fake jpg data")
        self.call_silently(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="public", extract=False, ocr=False))
        original = kaal.run_tesseract
        try:
            kaal.run_tesseract = lambda path: "Driver License: D1234567"
            _result, stdout, _stderr = self.capture_call(kaal.ocr_attachments, SimpleNamespace(query="", dry_run=False, force=False, limit=0, no_extract=False, no_ocr=False))
        finally:
            kaal.run_tesseract = original
        payload = json.loads(stdout)
        self.assertEqual(payload["extracted_attachments"], 1)
        updated = kaal.load_index()["notes"][0]
        att = updated["attachments"][0]
        self.assertTrue(att["extracted_markdown"])
        sidecar = kaal.attachment_note_dir(updated["id"]) / att["id"] / "extracted.md"
        self.assertTrue(sidecar.exists())
        self.assertIn("Driver License: D1234567", sidecar.read_text(encoding="utf-8"))
        self.assertTrue((kaal.attachment_note_dir(updated["id"]) / att["id"] / "scan.jpg").exists())

    def test_import_joplin_raw_incremental_repairs_legacy_import_without_duplicates(self):
        export_dir = self.make_joplin_raw_export()
        legacy = kaal.create_note(
            "Trip Plan",
            "Pack snacks and a printed itinerary.",
            tags=["imported", "joplin", "travel", "joplin-notebook-personal"],
            sensitivity="public",
        )
        with self.with_fake_crypto():
            _result, stdout, _stderr = self.capture_call(
                kaal.import_joplin_raw,
                SimpleNamespace(path=str(export_dir), dry_run=False, incremental=True, tags="", sensitivity="auto", no_extract=True, no_ocr=True),
            )
        payload = json.loads(stdout)
        self.assertEqual(payload["mode"], "incremental")
        self.assertEqual(payload["imported_notes"], 1)
        self.assertEqual(payload["skipped_existing_notes"], 1)
        self.assertEqual(payload["imported_attachments"], 1)
        notes = kaal.load_index()["notes"]
        self.assertEqual(len([n for n in notes if n["title"] == "Trip Plan"]), 1)
        repaired = next(n for n in notes if n["id"] == legacy["id"])
        self.assertEqual(len(repaired["attachments"]), 1)
        self.assertEqual(repaired["attachments"][0]["source"]["resource_id"], "44444444444444444444444444444444")
        identity = next(n for n in notes if n["title"] == "Identity")
        self.assertEqual(identity["source"], {"type": "joplin", "id": "33333333333333333333333333333333"})

    def test_import_joplin_raw_warns_about_unresolved_resource_links(self):
        export_dir = self.vault / "joplin-missing-resource"
        export_dir.mkdir()
        (export_dir / "22222222222222222222222222222222.md").write_text(
            "Broken\n\nMissing :/99999999999999999999999999999999\n\n"
            "id: 22222222222222222222222222222222\n"
            "type_: 1\n",
            encoding="utf-8",
        )
        _result, stdout, _stderr = self.capture_call(
            kaal.import_joplin_raw,
            SimpleNamespace(path=str(export_dir), dry_run=True, tags="", sensitivity="auto", no_extract=True, no_ocr=True),
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["unresolved_resources"], 1)
        self.assertIn("missing resource", payload["warnings"][0])

    def test_medical_capture_creates_plaintext_inbox_note_and_receipt_attachment(self):
        receipt = self.vault / "payment-receipt.pdf"
        receipt.write_bytes(b"%PDF fake receipt")
        original = kaal.run_docling
        try:
            kaal.run_docling = lambda path: "Patient ID: 12345\nPaid: $42.00"
            _result, stdout, _stderr = self.capture_call(
                kaal.medical_capture,
                SimpleNamespace(
                    file=str(receipt),
                    title="City Clinic payment receipt",
                    source_url="https://portal.example.test/receipt/42",
                    source_title="City Clinic: payment confirmed",
                    captured_at="2026-07-29T12:00:00+00:00",
                ),
            )
        finally:
            kaal.run_docling = original

        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "captured")
        self.assertEqual(payload["storage"], "plaintext")
        meta = kaal.load_index()["notes"][0]
        self.assertEqual(meta["title"], "City Clinic payment receipt")
        self.assertEqual(meta["storage"], "plaintext")
        self.assertEqual(meta["sensitivity"], "public")
        self.assertTrue({"medical", "receipt", "inbox"}.issubset(set(meta["tags"])))
        self.assertEqual(meta["source"]["url"], "https://portal.example.test/receipt/42")
        attachment = meta["attachments"][0]
        self.assertEqual(attachment["storage"], "plaintext")
        self.assertEqual(attachment["sensitivity"], "public")
        self.assertTrue(attachment["extracted_markdown"])
        self.assertEqual(attachment["source"]["title"], "City Clinic: payment confirmed")
        self.assertIn("City Clinic: payment confirmed", kaal.load_note(meta)["body"])
        self.assertIn("Paid: $42.00", (kaal.attachment_note_dir(meta["id"]) / attachment["id"] / "extracted.md").read_text())

    def test_medical_list_limits_results_to_medical_receipts_and_can_filter_inbox(self):
        self.add_note(title="Not medical", tags="home", body="# Home")
        receipt = self.vault / "receipt.pdf"
        receipt.write_bytes(b"%PDF fake receipt")
        self.call_silently(
            kaal.medical_capture,
            SimpleNamespace(file=str(receipt), title="Receipt", source_url="", source_title="", captured_at=""),
        )

        _result, stdout, _stderr = self.capture_call(kaal.medical_list, SimpleNamespace(inbox=True, json=True))
        listed = json.loads(stdout)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "Receipt")
        self.assertIn("inbox", listed[0]["tags"])

    def test_medical_list_sorts_newest_first_and_honors_limit(self):
        older = self.add_note(title="Older receipt", tags="medical,receipt,inbox", body="# Older")
        newer = self.add_note(title="Newer receipt", tags="medical,receipt,inbox", body="# Newer")
        index = kaal.load_index()
        for note in index["notes"]:
            if note["id"] == older["id"]:
                note["updated"] = "2026-01-01T00:00:00+00:00"
            if note["id"] == newer["id"]:
                note["updated"] = "2026-02-01T00:00:00+00:00"
        kaal.save_index(index)

        _result, stdout, _stderr = self.capture_call(kaal.medical_list, SimpleNamespace(inbox=False, json=True, limit=1))
        listed = json.loads(stdout)
        self.assertEqual([note["title"] for note in listed], ["Newer receipt"])

    def test_medical_receipt_trash_restore_and_purge_lifecycle(self):
        receipt = self.add_note(title="Receipt to manage", tags="medical,receipt,inbox", body="# Receipt")
        attachment_source = self.vault / "original.pdf"
        attachment_source.write_bytes(b"%PDF receipt")
        kaal.attach_file_to_note(receipt, attachment_source, sensitivity="public")

        _result, stdout, _stderr = self.capture_call(kaal.medical_trash, SimpleNamespace(note_id=receipt["id"]))
        self.assertEqual(json.loads(stdout)["status"], "trashed")
        _result, stdout, _stderr = self.capture_call(kaal.medical_list, SimpleNamespace(inbox=False, json=True, limit=0, trash=False))
        self.assertEqual(json.loads(stdout), [])
        _result, stdout, _stderr = self.capture_call(kaal.medical_list, SimpleNamespace(inbox=False, json=True, limit=0, trash=True))
        self.assertEqual([note["id"] for note in json.loads(stdout)], [receipt["id"]])

        _result, stdout, _stderr = self.capture_call(kaal.medical_restore, SimpleNamespace(note_id=receipt["id"]))
        self.assertEqual(json.loads(stdout)["status"], "restored")
        _result, stdout, _stderr = self.capture_call(kaal.medical_trash, SimpleNamespace(note_id=receipt["id"]))
        self.assertEqual(json.loads(stdout)["status"], "trashed")
        _result, stdout, _stderr = self.capture_call(kaal.medical_purge, SimpleNamespace(note_id=receipt["id"], yes=True))
        self.assertEqual(json.loads(stdout)["status"], "purged")
        self.assertFalse(kaal.plaintext_note_path(receipt["id"]).exists())
        self.assertFalse(kaal.attachment_note_dir(receipt["id"]).exists())
        self.assertFalse((self.vault / ".receipt-purge").exists())
        self.assertFalse(any(note["id"] == receipt["id"] for note in kaal.load_index()["notes"]))

    def test_medical_purge_restores_the_trashed_receipt_when_staged_cleanup_fails(self):
        receipt = self.add_note(title="Receipt with failed purge", tags="medical,receipt,inbox", body="# Receipt")
        _result, _stdout, _stderr = self.capture_call(kaal.medical_trash, SimpleNamespace(note_id=receipt["id"]))
        original_rmtree = kaal.shutil.rmtree

        def fail_purge_cleanup(path, *args, **kwargs):
            if Path(path).name == receipt["id"]:
                raise OSError("simulated staged cleanup failure")
            return original_rmtree(path, *args, **kwargs)

        kaal.shutil.rmtree = fail_purge_cleanup
        try:
            with self.assertRaises(SystemExit):
                kaal.medical_purge(SimpleNamespace(note_id=receipt["id"], yes=True))
        finally:
            kaal.shutil.rmtree = original_rmtree

        restored = next(note for note in kaal.load_index()["notes"] if note["id"] == receipt["id"])
        self.assertTrue(restored.get("trashed_at"))
        self.assertTrue(kaal.plaintext_note_path(receipt["id"]).exists())

    def test_medical_bulk_purge_requires_every_receipt_to_be_trashed(self):
        trashed = self.add_note(title="Trashed receipt", tags="medical,receipt,inbox", body="# Receipt")
        active = self.add_note(title="Active receipt", tags="medical,receipt,inbox", body="# Receipt")
        self.call_silently(kaal.medical_trash, SimpleNamespace(note_id=trashed["id"]))

        with self.assertRaises(SystemExit):
            kaal.medical_purge_bulk(SimpleNamespace(note_ids=[trashed["id"], active["id"]], yes=True))

        remaining = {note["id"]: note for note in kaal.load_index()["notes"]}
        self.assertIn(trashed["id"], remaining)
        self.assertIn(active["id"], remaining)
        self.assertTrue(remaining[trashed["id"]].get("trashed_at"))

    def test_medical_bulk_purge_removes_only_kaal_managed_copies(self):
        first = self.add_note(title="First receipt", tags="medical,receipt,inbox", body="# Receipt")
        second = self.add_note(title="Second receipt", tags="medical,receipt,inbox", body="# Receipt")
        source = self.vault / "source.pdf"
        source.write_bytes(b"%PDF source")
        kaal.attach_file_to_note(first, source, sensitivity="public")
        kaal.attach_file_to_note(second, source, sensitivity="public")
        self.call_silently(kaal.medical_trash, SimpleNamespace(note_id=first["id"]))
        self.call_silently(kaal.medical_trash, SimpleNamespace(note_id=second["id"]))

        _result, stdout, _stderr = self.capture_call(
            kaal.medical_purge_bulk,
            SimpleNamespace(note_ids=[first["id"], second["id"]], yes=True),
        )

        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "purged")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(set(payload["ids"]), {first["id"], second["id"]})
        self.assertTrue(source.exists())
        self.assertFalse(any(note["id"] in {first["id"], second["id"]} for note in kaal.load_index()["notes"]))

    def test_medical_review_and_reopen_toggle_the_inbox_status(self):
        receipt = self.add_note(title="Receipt to review", tags="medical,receipt,inbox", body="# Receipt")
        receipt["receipt"] = {"fields": {"amount": "12.34", "service_date": "2026-01-01"}, "field_sources": {}}
        kaal.save_index({"version": 1, "created": kaal.now_iso(), "notes": [receipt]})

        _result, stdout, _stderr = self.capture_call(kaal.medical_review, SimpleNamespace(note_id=receipt["id"]))
        self.assertEqual(json.loads(stdout)["status"], "reviewed")
        reviewed = next(note for note in kaal.load_index()["notes"] if note["id"] == receipt["id"])
        self.assertNotIn("inbox", reviewed["tags"])
        self.assertTrue(reviewed.get("reviewed_at"))

        _result, stdout, _stderr = self.capture_call(kaal.medical_reopen, SimpleNamespace(note_id=receipt["id"]))
        self.assertEqual(json.loads(stdout)["status"], "inbox")
        reopened = next(note for note in kaal.load_index()["notes"] if note["id"] == receipt["id"])
        self.assertIn("inbox", reopened["tags"])
        self.assertNotIn("reviewed_at", reopened)

    def test_medical_extracted_text_returns_only_an_explicitly_requested_sidecar(self):
        receipt = self.add_note(title="Receipt text", tags="medical,receipt,inbox", body="# Receipt")
        source = self.vault / "receipt.txt"
        source.write_text("receipt text", encoding="utf-8")
        attachment = kaal.attach_file_to_note(receipt, source, sensitivity="public", extract=True)

        _result, stdout, _stderr = self.capture_call(kaal.medical_extracted_text, SimpleNamespace(note_id=receipt["id"], attachment_id=attachment["id"]))
        result = json.loads(stdout)
        self.assertEqual(result["attachment_id"], attachment["id"])
        self.assertIn("receipt text", result["text"])

    def test_medical_capture_rolls_back_note_when_attachment_copy_fails(self):
        receipt = self.vault / "receipt.pdf"
        receipt.write_bytes(b"%PDF fake receipt")
        original = kaal.attach_file_to_note
        try:
            def fail_attachment(*args, **kwargs):
                raise OSError("disk full")
            kaal.attach_file_to_note = fail_attachment
            with self.assertRaises(OSError):
                kaal.medical_capture(SimpleNamespace(file=str(receipt), title="Receipt", source_url="", source_title="", captured_at=""))
        finally:
            kaal.attach_file_to_note = original
        self.assertEqual(kaal.load_index()["notes"], [])
        self.assertEqual(list(kaal.NOTES_DIR.iterdir()), [])
        self.assertEqual(list(kaal.ATTACH_DIR.iterdir()), [])

    def test_docling_structured_field_resolver_uses_labelled_neighbor_cells(self):
        document = {"texts": [
            {"text": "Patient Name"},
            {"text": "Example Patient"},
            {"text": "Date of Service"},
            {"text": "2026-07-30"},
            {"text": "Amount Paid: $42.50"},
        ]}
        fields = kaal.infer_docling_fields(document)
        self.assertEqual(fields["patient_name"], "Example Patient")
        self.assertEqual(fields["service_date"], "2026-07-30")

    def test_docling_structured_field_resolver_uses_inline_cell_values(self):
        document = {"texts": [{"text": "Provider | Example Medical Practice"}]}
        self.assertEqual(kaal.infer_docling_fields(document)["provider"], "Example Medical Practice")

    def test_docling_structured_field_resolver_rejects_a_label_as_patient_value(self):
        document = {"texts": [{"text": "Patient Name"}, {"text": "Date"}]}
        self.assertNotIn("patient_name", kaal.infer_docling_fields(document))

    def test_receipt_field_inference_supports_pdftotext_layout_columns(self):
        fields = kaal.infer_receipt_fields("Patient Name          Example Patient\n")
        self.assertEqual(fields["patient_name"], "Example Patient")

    def test_pdf_extraction_escalates_when_native_text_lacks_paid_amount_or_date(self):
        receipt = self.vault / "receipt.pdf"
        receipt.write_bytes(b"%PDF-1.4")
        original_pdf = kaal.run_pdftotext
        original_vision = kaal.run_macos_vision_pdf_ocr
        original_docling = kaal.run_docling
        try:
            kaal.run_pdftotext = lambda _path: "Patient Name: Example Patient\n"
            kaal.run_macos_vision_pdf_ocr = lambda _path: "Payment received: $12.34\nDate of Service: 2026-01-01\n"
            kaal.run_docling = lambda _path: ""
            extracted = kaal.extract_attachment_markdown(receipt, extract=True, ocr=True)
        finally:
            kaal.run_pdftotext = original_pdf
            kaal.run_macos_vision_pdf_ocr = original_vision
            kaal.run_docling = original_docling
        self.assertIn("Method: macos-vision", extracted)
        fields = kaal.infer_receipt_fields(extracted)
        self.assertEqual(fields["amount"], "12.34")
        self.assertEqual(fields["service_date"], "2026-01-01")

    def test_medical_pdf_route_stops_after_docling_without_markitdown_or_tesseract(self):
        receipt = self.vault / "receipt.pdf"
        receipt.write_bytes(b"%PDF-1.4")
        original_pdf = kaal.run_pdftotext
        original_vision = kaal.run_macos_vision_pdf_ocr
        original_docling = kaal.run_docling
        original_markitdown = kaal.run_markitdown
        original_tesseract = kaal.run_pdf_ocr
        calls = {"markitdown": 0, "tesseract": 0}
        try:
            kaal.run_pdftotext = lambda _path: "Patient Name: Example Patient\n"
            kaal.run_macos_vision_pdf_ocr = lambda _path: "Patient Name: Example Patient\n"
            kaal.run_docling = lambda _path: "Payment received: $12.34\nPayment Date: 2026-01-01\n"
            kaal.run_markitdown = lambda _path: calls.__setitem__("markitdown", calls["markitdown"] + 1) or ""
            kaal.run_pdf_ocr = lambda _path: calls.__setitem__("tesseract", calls["tesseract"] + 1) or ""
            extracted = kaal.extract_attachment_markdown(receipt, extract=True, ocr=True, receipt=True)
        finally:
            kaal.run_pdftotext = original_pdf
            kaal.run_macos_vision_pdf_ocr = original_vision
            kaal.run_docling = original_docling
            kaal.run_markitdown = original_markitdown
            kaal.run_pdf_ocr = original_tesseract
        self.assertIn("Method: docling", extracted)
        self.assertEqual(calls, {"markitdown": 0, "tesseract": 0})

    def test_receipt_state_uses_local_ai_only_for_safe_missing_fields(self):
        state = kaal.receipt_extraction_state("Patient Name          Example Patient\n", ai_fields={"patient_name": "Wrong Person", "provider": "Example Medical Center"})
        self.assertEqual(state["fields"]["patient_name"], "Example Patient")
        self.assertEqual(state["field_sources"]["patient_name"], "labelled-text")
        self.assertEqual(state["fields"]["provider"], "")
        self.assertNotIn("provider", state["field_sources"])

    def test_receipt_amount_requires_an_explicit_payment_label(self):
        self.assertEqual(kaal.infer_receipt_fields("Total: $500.00\n")["amount"], "")
        self.assertEqual(kaal.infer_receipt_fields("Total Amount: $23.24\n")["amount"], "")
        self.assertEqual(kaal.infer_receipt_fields("Payment received: 202607319498541\n")["amount"], "")
        self.assertEqual(kaal.infer_receipt_fields("Payment received: $112.45\n")["amount"], "112.45")
        self.assertEqual(
            kaal.infer_receipt_fields("SALE - APPROVED\nTotal Amount: $23.24\n")["amount"],
            "23.24",
        )
        self.assertEqual(kaal.infer_receipt_fields("Date: 10/09/2025\n")["paid_date"], "")
        self.assertEqual(
            kaal.infer_receipt_fields("SALE - APPROVED\nDate: 10/09/2025\n")["paid_date"],
            "10/09/2025",
        )
        self.assertEqual(
            kaal.infer_receipt_fields("Payment Date\n07/31/2026\n")["paid_date"],
            "07/31/2026",
        )
        self.assertEqual(kaal.infer_receipt_fields("Payment Date\nMember Identifier\n")["paid_date"], "")

    def test_supplied_receipt_fixtures_detect_user_confirmed_paid_amounts(self):
        fixture_dir = ROOT / ".hermes" / "desktop-attachments"
        fixtures = {
            "Payment receipt.pdf": "23.24",
            "Labcorp-Receipt.pdf": "112.45",
        }
        if not all((fixture_dir / name).is_file() for name in fixtures):
            self.skipTest("user-supplied receipt fixtures are not present")
        for name, expected_amount in fixtures.items():
            extracted = kaal.extract_attachment_markdown(fixture_dir / name, extract=True, ocr=True, receipt=True)
            receipt = kaal.build_receipt_state(extracted)
            self.assertEqual(receipt["fields"].get("amount"), expected_amount, name)

    def test_receipt_state_does_not_accept_an_unverified_ai_amount(self):
        state = kaal.receipt_extraction_state("Total: $500.00\n", ai_fields={"amount": "500.00"})
        self.assertEqual(state["fields"]["amount"], "")
        self.assertNotIn("amount", state["field_sources"])

    def test_receipt_provider_rejects_individual_clinician_candidates(self):
        self.assertEqual(kaal.infer_receipt_fields("Provider: Dr. Example\n")["provider"], "")
        state = kaal.receipt_extraction_state("", ai_fields={"provider": "Dr. Example"})
        self.assertEqual(state["fields"].get("provider", ""), "")
        self.assertNotIn("provider", state["field_sources"])

    def test_receipt_state_rejects_semantic_guesses_for_amount_and_dates(self):
        state = kaal.receipt_extraction_state("", ai_fields={"amount": "500.00", "service_date": "2026-01-01", "paid_date": "2026-01-02"})
        self.assertEqual(state["fields"], {})
        self.assertEqual(state["field_sources"], {})

    def test_build_receipt_state_preserves_manual_critical_fields_on_reextraction(self):
        prior = {
            "fields": {"amount": "12.34", "service_date": "2026-01-01"},
            "field_sources": {"amount": "manual", "service_date": "manual"},
            "manually_updated_at": "2026-01-01T00:00:00+00:00",
        }
        receipt = kaal.build_receipt_state("Amount Paid: $99.00\nDate of Service: 2026-02-01\n", {}, prior)
        self.assertEqual(receipt["fields"]["amount"], "12.34")
        self.assertEqual(receipt["fields"]["service_date"], "2026-01-01")
        self.assertEqual(receipt["field_sources"]["amount"], "manual")
        self.assertEqual(receipt["field_sources"]["service_date"], "manual")

    def test_review_requires_paid_amount_and_a_date(self):
        note = self.add_note(title="Receipt", tags="medical,receipt")
        note["receipt"] = {"fields": {"amount": "", "service_date": "", "paid_date": ""}, "field_sources": {}}
        kaal.save_index({"version": 1, "created": kaal.now_iso(), "notes": [note]})
        code, _stdout, stderr = self.assert_dies(kaal.medical_review, SimpleNamespace(note_id=note["id"]))
        self.assertEqual(code, 1)
        self.assertIn("amount and a date", stderr)

        note["receipt"]["fields"].update({"amount": "12.34", "service_date": "2026-01-01"})
        kaal.save_index({"version": 1, "created": kaal.now_iso(), "notes": [note]})
        self.call_silently(kaal.medical_review, SimpleNamespace(note_id=note["id"]))
        reviewed = kaal.load_index()["notes"][0]
        self.assertEqual(reviewed["receipt"]["field_sources"]["amount"], "confirmed")
        self.assertEqual(reviewed["receipt"]["field_sources"]["service_date"], "confirmed")

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
        self.assertEqual(parser.parse_args(["ocr-attachments", "--dry-run", "--limit", "5"]).func, kaal.ocr_attachments)
        self.assertEqual(parser.parse_args(["import-joplin-raw", "export", "--dry-run", "--tags", "migrated", "--no-extract", "--no-ocr"]).func, kaal.import_joplin_raw)
        self.assertEqual(parser.parse_args(["medical", "capture", "receipt.pdf", "--source-url", "https://example.test"]).func, kaal.medical_capture)
        self.assertEqual(parser.parse_args(["medical", "list", "--inbox", "--json"]).func, kaal.medical_list)
        self.assertEqual(parser.parse_args(["medical", "list", "--limit", "25"]).limit, 25)
        self.assertEqual(parser.parse_args(["medical", "trash", "abc123"]).func, kaal.medical_trash)
        self.assertEqual(parser.parse_args(["medical", "restore", "abc123"]).func, kaal.medical_restore)
        self.assertEqual(parser.parse_args(["medical", "purge", "abc123", "--yes"]).func, kaal.medical_purge)
        self.assertEqual(parser.parse_args(["medical", "purge-bulk", "abc123", "def456", "--yes"]).func, kaal.medical_purge_bulk)
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
