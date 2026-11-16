"""The model step for receipts: how it is called, what is trusted, and how it fails.

No test here reaches a real model. The backend tests run a stand-in executable;
the pipeline tests replace ``receipt_ai.extract``.
"""
import base64
import io
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

import kaal.core as kaal
from kaal import receipt_ai, receipts

FAKE_CLAUDE = '''#!{python}
import json, os, sys, time
log = os.environ["FAKE_CLAUDE_LOG"]
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd_entries": os.listdir("."), "autoupdate": os.environ.get("DISABLE_AUTOUPDATER")}}, open(log, "w"))
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "slow":
    time.sleep(30)
if mode == "auth":
    print("Failed to authenticate. API Error: 401 OAuth access token is invalid. secret-account-detail")
    sys.exit(1)
if mode == "crash":
    print("something unexpected with secret-account-detail", file=sys.stderr)
    sys.exit(3)
print(json.dumps({{"type": "system", "subtype": "init"}}))
print("not json at all")
if mode == "model-error":
    print(json.dumps({{"type": "result", "is_error": True, "result": "overloaded"}}))
elif mode == "no-answer":
    print(json.dumps({{"type": "result", "is_error": False, "result": "plain words"}}))
else:
    print(json.dumps({{"type": "result", "is_error": False, "structured_output": json.loads(os.environ["FAKE_CLAUDE_ANSWER"])}}))
'''


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake-claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", json.dumps({name: "" for name in receipt_ai.FIELDS}))
    settings = receipt_ai.Settings(backend="claude-cli", command=str(script), model="test-model", timeout=20)
    return SimpleNamespace(settings=settings, log=log, setenv=monkeypatch.setenv)


@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield tmp_path
    kaal.configure_vault(saved)


def call(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


def stored(note_id):
    return next(note for note in kaal.load_index()["notes"] if note["id"] == note_id)["receipt"]


def capture(directory, text, name="receipt.txt"):
    source = directory / name
    source.write_text(text)
    return call(kaal.medical_capture, file=str(source), title="", source_url="", source_title="", captured_at="")["id"]


# --- settings

def test_the_model_is_off_unless_configured():
    assert receipt_ai.settings_from_env({}) == receipt_ai.Settings()
    assert receipt_ai.settings_from_env({}).enabled is False
    on = receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": " Claude-CLI ", "KAAL_RECEIPT_AI_COMMAND": "/opt/x/claude", "KAAL_RECEIPT_AI_MODEL": "small", "KAAL_RECEIPT_AI_TIMEOUT": "9999"})
    assert (on.backend, on.command, on.model, on.timeout, on.enabled) == ("claude-cli", "/opt/x/claude", "small", 600.0, True)
    for bad in ({"KAAL_RECEIPT_AI": "chatgpt"}, {"KAAL_RECEIPT_AI": "claude-cli", "KAAL_RECEIPT_AI_TIMEOUT": "soon"}):
        with pytest.raises(receipt_ai.ReceiptAIError):
            receipt_ai.settings_from_env(bad)


def test_describe_reports_whether_the_command_exists_without_calling_it(fake_claude):
    assert receipt_ai.describe(receipt_ai.Settings()) == {"backend": "off", "usable": False}
    assert receipt_ai.describe(fake_claude.settings) == {"backend": "claude-cli", "model": "test-model", "usable": True}
    assert not fake_claude.log.exists()
    missing = receipt_ai.Settings(backend="claude-cli", command="/nonexistent/claude")
    assert receipt_ai.describe(missing) == {"backend": "claude-cli", "model": "haiku", "usable": False, "problem": "command not found"}


# --- the command line backend

def test_the_model_runs_with_no_tools_no_settings_and_an_empty_folder(fake_claude):
    fake_claude.setenv("FAKE_CLAUDE_ANSWER", json.dumps({
        "patient_name": "  Jane   Doe ", "provider": "", "insurer": 7, "service_date": "10/16/2026", "paid_date": "x" * 500, "amount": "$40.00", "extra": "ignored",
    }))
    fields = receipt_ai.extract("Total Paid: $40.00", [("image/png", b"\x89PNG-bytes")], fake_claude.settings)
    assert fields == {"patient_name": "Jane Doe", "service_date": "10/16/2026", "paid_date": "x" * 200, "amount": "$40.00"}

    seen = json.loads(fake_claude.log.read_text())
    argv = seen["argv"]
    assert argv[0] == "-p"
    for flag, value in (("--tools", ""), ("--setting-sources", ""), ("--model", "test-model"), ("--input-format", "stream-json"), ("--output-format", "stream-json")):
        assert argv[argv.index(flag) + 1] == value
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == list(receipt_ai.FIELDS)
    assert "untrusted" in argv[argv.index("--system-prompt") + 1]
    assert seen["cwd_entries"] == []
    assert seen["autoupdate"] == "1"
    content = json.loads(seen["stdin"])["message"]["content"]
    assert content[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(b"\x89PNG-bytes").decode()}}
    assert content[1]["text"].endswith("Total Paid: $40.00")


def test_long_text_and_extra_pages_are_cut_down(fake_claude):
    receipt_ai.extract("a" * 50_000, [("image/png", b"1"), ("image/png", b"2"), ("image/png", b"3")], fake_claude.settings)
    content = json.loads(json.loads(fake_claude.log.read_text())["stdin"])["message"]["content"]
    assert [part["type"] for part in content] == ["image", "image", "text"]
    assert len(content[-1]["text"]) < receipt_ai.MAX_TEXT_CHARS + 100


@pytest.mark.parametrize("mode, kind", [
    ("auth", "not_signed_in"),
    ("crash", "failed"),
    ("model-error", "failed"),
    ("no-answer", "bad_answer"),
])
def test_failures_are_reduced_to_a_kind_with_none_of_the_commands_own_words(fake_claude, mode, kind):
    fake_claude.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(receipt_ai.ReceiptAIError) as raised:
        receipt_ai.extract("text", [], fake_claude.settings)
    assert raised.value.kind == kind
    assert "secret-account-detail" not in str(raised.value)


def test_a_slow_model_times_out_and_other_dead_ends_are_named(fake_claude):
    fake_claude.setenv("FAKE_CLAUDE_MODE", "slow")
    hurried = receipt_ai.Settings(backend="claude-cli", command=fake_claude.settings.command, timeout=0.5)
    with pytest.raises(receipt_ai.ReceiptAIError) as raised:
        receipt_ai.extract("text", [], hurried)
    assert raised.value.kind == "timeout"
    for settings, text, kind in (
        (receipt_ai.Settings(backend="claude-cli", command="/nonexistent/claude"), "text", "unavailable"),
        (fake_claude.settings, "   ", "nothing_to_read"),
        (receipt_ai.Settings(), "text", "off"),
    ):
        with pytest.raises(receipt_ai.ReceiptAIError) as raised:
            receipt_ai.extract(text, [], settings)
        assert raised.value.kind == kind


# --- page images

def test_photos_are_turned_upright_and_shrunk_when_pillow_is_installed(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    photo = tmp_path / "photo.tiff"
    image_module.new("RGB", (4000, 3000), "white").save(photo)
    [(media_type, data)] = receipt_ai.receipt_images(photo)
    assert media_type == "image/jpeg"
    with image_module.open(io.BytesIO(data)) as shrunk:
        assert max(shrunk.size) == receipt_ai.MAX_IMAGE_EDGE


def test_without_pillow_only_small_images_the_model_accepts_are_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(receipt_ai, "_shrunk", lambda data: None)
    small = tmp_path / "small.JPG"
    small.write_bytes(b"jpeg-bytes")
    assert receipt_ai.receipt_images(small) == [("image/jpeg", b"jpeg-bytes")]
    big = tmp_path / "big.png"
    big.write_bytes(b"0" * (receipt_ai.MAX_IMAGE_BYTES + 1))
    tiff = tmp_path / "scan.tiff"
    tiff.write_bytes(b"tiff-bytes")
    note = tmp_path / "note.txt"
    note.write_text("text")
    assert [receipt_ai.receipt_images(path) for path in (big, tiff, note, tmp_path / "missing.png")] == [[], [], [], []]


# --- what is trusted

@pytest.mark.parametrize("name, value, expected", [
    ("amount", "$1,234.50", True),
    ("amount", "1234.5", True),
    ("amount", "40", True),
    ("amount", "234.50", False),
    ("amount", "40.63", False),
    ("service_date", "2026-10-16", True),
    ("service_date", "Oct 16, 2026", True),
    ("paid_date", "11/12/26", True),
    ("paid_date", "11/13/2026", False),
    ("patient_name", "jane doe", True),
    ("patient_name", "DOE, JANE", True),
    ("patient_name", "Jane Roe", False),
    ("provider", "Example Health", False),
    ("insurer", "", False),
])
def test_a_value_is_grounded_when_the_receipt_text_holds_it(name, value, expected):
    text = "Patient: Jane Doe (DOE, JANE)\nAppt 10/16/2026   Paid $40 on Nov 12, 2026\nTotal Paid:  $1,234.50   Balance 140.63\n"
    assert receipts.ground(name, value, text) is expected


# --- the pipeline, with the model replaced

@pytest.fixture()
def model(monkeypatch):
    """Switch the model on and answer with whatever the test puts in ``answer``."""
    box = SimpleNamespace(answer={}, error=None, calls=[])

    def fake_extract(text, images, settings):
        box.calls.append(SimpleNamespace(text=text, images=images, settings=settings))
        if box.error:
            raise receipt_ai.ReceiptAIError(box.error)
        return dict(box.answer)

    monkeypatch.setenv("KAAL_RECEIPT_AI", "claude-cli")
    monkeypatch.setattr(receipt_ai, "extract", fake_extract)
    return box


def test_capture_takes_the_models_fields_and_marks_what_the_text_backs(vault, model):
    model.answer = {"patient_name": "Jane Doe", "provider": "Example Health", "insurer": "Acme Plan", "service_date": "03/04/2026", "paid_date": "March 9, 2026", "amount": "$40.00"}
    note_id = capture(vault, "PAYMENT RECEIPT\nJane Doe        Acme Plan\nAppt 03/04/2026\nTotal Paid:   $40.00\n")
    receipt = stored(note_id)
    assert receipt["fields"] == {"patient_name": "Jane Doe", "provider": "Example Health", "insurer": "Acme Plan", "service_date": "2026-03-04", "paid_date": "2026-03-09", "amount": "40.00", "currency": "USD"}
    # The rules found the amount too; the model agreeing with it, from the text, is the stronger source.
    assert receipt["field_sources"] == {
        "patient_name": "ai-grounded", "insurer": "ai-grounded", "service_date": "ai-grounded", "amount": "ai-grounded",
        "provider": "ai-unverified", "paid_date": "ai-unverified", "currency": "labelled-text",
    }
    assert receipt["ai"] == {"backend": "claude-cli", "model": "haiku"}
    assert "conflicts" not in receipt
    [seen] = model.calls
    assert seen.text.startswith("PAYMENT RECEIPT") and "Method:" not in seen.text and "```" not in seen.text


def test_a_failing_model_leaves_the_rules_result_and_records_why(vault, model):
    model.error = "rate_limited"
    note_id = capture(vault, "Amount paid: $12.50\nService date: 01/02/2026\n")
    receipt = stored(note_id)
    assert (receipt["fields"]["amount"], receipt["fields"]["service_date"]) == ("12.50", "2026-01-02")
    assert receipt["field_sources"]["amount"] == "labelled-text"
    assert receipt["ai"] == {"backend": "claude-cli", "error": "rate_limited"}


def test_with_the_model_off_capture_never_calls_it(vault, monkeypatch):
    monkeypatch.setattr(receipt_ai, "extract", lambda *args: pytest.fail("the model was called"))
    receipt = stored(capture(vault, "Amount paid: $12.50\n"))
    assert "ai" not in receipt
    assert receipt["field_sources"]["amount"] == "labelled-text"


def test_a_pdf_is_read_from_its_text_layer_or_by_ocr_when_it_is_a_scan(vault, monkeypatch):
    pdf = vault / "receipt.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    layer = "PAYMENT RECEIPT for services rendered at the clinic on the stated date, thank you.\n"
    monkeypatch.setattr(kaal, "run_pdf_ocr", lambda path: "TEXT READ BY OCR FROM A SCANNED PAGE")

    monkeypatch.setattr(kaal, "run_pdftotext", lambda path: layer)
    assert "Method: pdftotext-layout" in kaal.extract_attachment_markdown(pdf, extract=True, ocr=True)
    for thin in ("Page 1\n", ""):
        monkeypatch.setattr(kaal, "run_pdftotext", lambda path, thin=thin: thin)
        scanned = kaal.extract_attachment_markdown(pdf, extract=True, ocr=True)
        assert "Method: tesseract-pdf" in scanned and "TEXT READ BY OCR" in scanned
    assert kaal.text_layer_is_real(layer) and not kaal.text_layer_is_real("Page 1\n") and not kaal.text_layer_is_real("." * 400)
    # A PDF that gives nothing either way has no text; nothing slower is tried.
    monkeypatch.setattr(kaal, "run_pdf_ocr", lambda path: "")
    assert kaal.extract_attachment_markdown(pdf, extract=True, ocr=True) == ""
    for gone in ("run_docling", "run_docling_json", "run_markitdown", "run_macos_vision_pdf_ocr"):
        assert not hasattr(kaal, gone)


def test_unverified_amounts_and_dates_stay_out_of_the_totals_until_confirmed(vault, model):
    model.answer = {"amount": "75.00", "service_date": "05/06/2026"}
    note_id = capture(vault, "a blurry photo with nothing readable\n")
    assert stored(note_id)["field_sources"] == {"amount": "ai-unverified", "service_date": "ai-unverified"}

    def report():
        return call(kaal.medical_report, year="", patient_name="", from_date="", to_date="", reimbursed="all")

    before = report()
    assert (before["count"], before["total_amount"]) == (1, "0.00")
    assert before["unverified_amount"] == [note_id] and before["undated"] == [note_id]
    assert call(kaal.medical_report, year="2026", patient_name="", from_date="", to_date="", reimbursed="all")["count"] == 0

    call(kaal.medical_review, note_id=note_id)
    assert stored(note_id)["field_sources"] == {"amount": "confirmed", "service_date": "confirmed"}
    after = report()
    assert (after["total_amount"], after["unverified_amount"], after["undated"]) == ("75.00", [], [])


def test_review_and_manual_edits_settle_disagreements(vault, model):
    model.answer = {"amount": "$30.00", "patient_name": "Jane Doe"}
    note_id = capture(vault, "Patient: Someone Else\nAmount paid: $12.00\nBalance: $30.00\nService date: 01/02/2026\n")
    receipt = stored(note_id)
    assert receipt["fields"]["amount"] == "12.00"
    assert set(receipt["conflicts"]) == {"amount", "patient_name"}

    call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"patient_name": "Jane Doe"}))
    assert set(stored(note_id)["conflicts"]) == {"amount"}
    call(kaal.medical_review, note_id=note_id)
    assert "conflicts" not in stored(note_id)
    assert stored(note_id)["field_sources"]["patient_name"] == "manual"


def test_a_failed_reextraction_keeps_what_the_user_typed_and_the_reimbursement(vault, monkeypatch):
    note_id = capture(vault, "Amount paid: $12.50\nService date: 01/02/2026\n")
    call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"provider": "Example Clinic"}))
    call(kaal.medical_review, note_id=note_id)
    call(kaal.medical_reimburse, note_ids=[note_id], date="2026-02-01", note="ref 1", undo=False)

    def boom(*args, **kwargs):
        raise RuntimeError("extractor fell over")

    monkeypatch.setattr(kaal, "extract_attachment_markdown", boom)
    call(kaal.ocr_attachments, query=note_id, force=True, dry_run=False, no_extract=False, no_ocr=False, limit=0)
    receipt = stored(note_id)
    assert receipt["state"] == "error"
    assert (receipt["fields"]["amount"], receipt["fields"]["provider"]) == ("12.50", "Example Clinic")
    assert (receipt["reimbursed_at"], receipt["reimbursement_note"]) == ("2026-02-01", "ref 1")


def test_status_and_evaluate_say_which_model_is_in_use(vault, monkeypatch):
    assert call(kaal.status)["receipt_ai"] == {"backend": "off", "usable": False}
    monkeypatch.setenv("KAAL_RECEIPT_AI", "claude-cli")
    monkeypatch.setenv("KAAL_RECEIPT_AI_COMMAND", "/nonexistent/claude")
    assert call(kaal.status)["receipt_ai"]["problem"] == "command not found"
    assert call(kaal.medical_evaluate, ids=[], limit=0)["receipt_ai"]["backend"] == "claude-cli"
    monkeypatch.setenv("KAAL_RECEIPT_AI", "something-else")
    assert call(kaal.status)["receipt_ai"]["backend"] == "misconfigured"


# --- cases found on real receipts

def test_a_rule_match_that_is_not_a_date_does_not_block_the_models_date():
    state = kaal.build_receipt_state("Paid on:   Payment Method\nAug 7, 2026 at 10:20 AM\n", ai_fields={"paid_date": "Aug 7, 2026"})
    assert state["fields"]["paid_date"] == "Aug 7, 2026"
    assert state["field_sources"]["paid_date"] == "ai-grounded"
    assert "conflicts" not in state


def test_several_payments_are_added_up_and_left_unverified():
    assert receipts.sum_of_payments("$3.22 + $60.00") == ("63.22", "3.22 + 60.00")
    assert receipts.sum_of_payments("1,000 + 0.5 + 2") == ("1002.50", "1000.00 + 0.50 + 2.00")
    for not_a_sum in ("$3.22", "$3.22 + lots", "$3.22 (RX# 1); $60.00 (RX# 2)", ""):
        assert receipts.sum_of_payments(not_a_sum) == ("", "")

    text = "RX 1 PRICE: $3.22\nRX 2 PRICE: $60.00\n"
    state = kaal.build_receipt_state(text, ai_fields={"amount": "$3.22 + $60.00"})
    assert state["fields"]["amount"] == "63.22"
    assert state["field_sources"]["amount"] == "ai-unverified"
    assert state["field_raw"]["amount"] == "3.22 + 60.00"
    assert kaal.build_receipt_state(text, ai_fields={"amount": "$3.22 (RX# 1); $60.00 (RX# 2)"})["fields"]["amount"] == ""


def test_an_amount_owed_on_a_bill_is_suggested_but_never_replaces_a_payment():
    bill = kaal.build_receipt_state("TOTAL CHARGES 509.00\nPatient responsibility $12.84\n", ai_fields={"amount": "", "amount_due": "$12.84"})
    assert (bill["fields"]["amount"], bill["field_sources"]["amount"]) == ("12.84", "ai-amount-due")
    paid = kaal.build_receipt_state("anything", ai_fields={"amount": "$40.00", "amount_due": "$12.84"})
    assert (paid["fields"]["amount"], paid["field_sources"]["amount"]) == ("$40.00", "ai-unverified")
    labelled = kaal.build_receipt_state("Amount paid: $20.00\n", ai_fields={"amount_due": "$12.84"})
    assert (labelled["fields"]["amount"], labelled["field_sources"]["amount"]) == ("20.00", "labelled-text")
    assert kaal.build_receipt_state("x", ai_fields={"amount_due": "see statement"})["fields"]["amount"] == ""


def test_an_amount_owed_stays_out_of_the_totals_until_reviewed(vault, model):
    model.answer = {"amount_due": "$12.84", "service_date": "3/14/25"}
    note_id = capture(vault, "STATEMENT\nDate of visit 3/14/25\nPlease pay $12.84\n")
    assert stored(note_id)["field_sources"]["amount"] == "ai-amount-due"
    totals = call(kaal.medical_report, year="2025", patient_name="", from_date="", to_date="", reimbursed="all")
    assert (totals["count"], totals["total_amount"], totals["unverified_amount"]) == (1, "0.00", [note_id])
    call(kaal.medical_review, note_id=note_id)
    assert call(kaal.medical_report, year="2025", patient_name="", from_date="", to_date="", reimbursed="all")["total_amount"] == "12.84"
