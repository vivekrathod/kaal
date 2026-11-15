"""Reading receipts after capture, re-reading stored ones, and remembered provider names."""
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import receipt_ai, receipts, server, tokens


@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield tmp_path
    kaal.configure_vault(saved)


@pytest.fixture()
def model(monkeypatch):
    box = SimpleNamespace(answer={}, error=None, calls=0)

    def fake_extract(text, images, settings):
        box.calls += 1
        if box.error:
            raise receipt_ai.ReceiptAIError(box.error)
        return dict(box.answer)

    monkeypatch.setenv("KAAL_RECEIPT_AI", "claude-cli")
    monkeypatch.setattr(receipt_ai, "extract", fake_extract)
    return box


def call(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


def stored(note_id):
    return next(note for note in kaal.load_index()["notes"] if note["id"] == note_id)["receipt"]


def capture(directory, text, name="receipt.txt", defer=False):
    source = directory / name
    source.write_text(text)
    return call(kaal.medical_capture, file=str(source), title="", source_url="", source_title="", captured_at="", defer=defer)


RECEIPT = "Amount paid: $12.50\nService date: 01/02/2026\n"


# --- deferred capture and the two-phase read

def test_deferred_capture_stores_the_original_and_reads_nothing(vault, model):
    result = capture(vault, RECEIPT, defer=True)
    receipt = stored(result["id"])
    assert result["extraction"] == "pending"
    assert (receipt["state"], receipt["status"], receipt["fields"]) == ("pending", "extracting", {})
    assert model.calls == 0
    assert kaal.pending_receipt_ids() == [result["id"]]
    assert call(kaal.medical_export_original, note_id=result["id"], attachment_id=result["attachment_id"], output=str(vault / "copy.txt"))["bytes"] == len(RECEIPT)


def test_reading_a_pending_receipt_fills_it_and_keeps_what_was_typed_meanwhile(vault, model):
    model.answer = {"provider": "Example Clinic", "amount": "$12.50"}
    note_id = capture(vault, RECEIPT, defer=True)["id"]
    call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"service_date": "2026-03-03", "insurer": "Typed Plan"}))

    assert kaal.reextract_receipt(note_id) == "done"
    receipt = stored(note_id)
    assert (receipt["state"], receipt["status"]) == ("extracted", "needs_review")
    assert receipt["fields"]["amount"] == "12.50"
    assert receipt["fields"]["provider"] == "Example Clinic"
    assert (receipt["fields"]["service_date"], receipt["field_sources"]["service_date"]) == ("2026-03-03", "manual")
    assert receipt["fields"]["insurer"] == "Typed Plan"
    assert kaal.pending_receipt_ids() == []
    assert "Amount paid" in call(kaal.medical_extracted_text, note_id=note_id, attachment_id="")["text"]


def test_a_read_that_fails_is_marked_failed_and_not_retried_forever(vault, monkeypatch):
    note_id = capture(vault, RECEIPT, defer=True)["id"]
    call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"amount": "9.99"}))

    def boom(*args, **kwargs):
        raise RuntimeError("extractor fell over")

    monkeypatch.setattr(kaal, "extract_attachment_markdown", boom)
    assert kaal.reextract_receipt(note_id) == "error"
    receipt = stored(note_id)
    assert (receipt["state"], receipt["status"], receipt["error"]) == ("error", "failed", "RuntimeError")
    assert receipt["fields"]["amount"] == "9.99"
    assert kaal.pending_receipt_ids() == []


def test_a_receipt_deleted_while_being_read_is_dropped_quietly(vault):
    note_id = capture(vault, RECEIPT, defer=True)["id"]
    call(kaal.medical_trash, note_id=note_id)
    call(kaal.medical_purge, note_id=note_id, yes=True)
    assert kaal.reextract_receipt(note_id) == "gone"


def test_status_shows_extracting_then_failed_or_needs_review():
    for state, reviewed, expected in (("pending", False, "extracting"), ("pending", True, "extracting"), ("error", False, "failed"), ("error", True, "reviewed"), ("extracted", False, "needs_review")):
        receipt = {"state": state}
        receipts.upgrade(receipt, reviewed=reviewed, people=[])
        assert receipt["status"] == expected


# --- kaal medical reextract

def test_reextract_reads_named_receipts_or_all_unreviewed_ones(vault, model):
    first = capture(vault, RECEIPT, name="a.txt")["id"]
    second = capture(vault, RECEIPT, name="b.txt")["id"]
    reviewed = capture(vault, RECEIPT, name="c.txt")["id"]
    call(kaal.medical_review, note_id=reviewed)
    model.answer = {"provider": "Example Clinic"}

    result = call(kaal.medical_reextract, note_ids=[], unreviewed=True, defer=False)
    assert (result["status"], sorted(result["done"]), result["errors"]) == ("updated", sorted([first, second]), [])
    assert stored(first)["fields"]["provider"] == "Example Clinic"
    assert stored(reviewed)["fields"].get("provider", "") == ""

    assert call(kaal.medical_reextract, note_ids=[reviewed], unreviewed=False, defer=False)["done"] == [reviewed]
    after = stored(reviewed)
    assert (after["fields"]["amount"], after["field_sources"]["amount"], after["status"]) == ("12.50", "confirmed", "reviewed")
    assert after["fields"]["provider"] == "Example Clinic"

    for bad in ({"note_ids": [], "unreviewed": False}, {"note_ids": [first], "unreviewed": True}, {"note_ids": ["nope"], "unreviewed": False}):
        with pytest.raises(kaal.KaalError):
            call(kaal.medical_reextract, defer=False, **bad)
    call(kaal.medical_trash, note_id=second)
    with pytest.raises(kaal.KaalError, match="Trash"):
        call(kaal.medical_reextract, note_ids=[second], unreviewed=False, defer=False)


def test_deferred_reextract_only_queues_and_keeps_the_fields_visible(vault, model):
    note_id = capture(vault, RECEIPT)["id"]
    result = call(kaal.medical_reextract, note_ids=[note_id], unreviewed=False, defer=True)
    assert result == {"status": "queued", "count": 1, "ids": [note_id]}
    receipt = stored(note_id)
    assert (receipt["status"], receipt["fields"]["amount"]) == ("extracting", "12.50")
    assert kaal.pending_receipt_ids() == [note_id]


# --- the server's background worker

class Service:
    def __init__(self, home: Path, background: bool = True):
        self.settings = server.Settings(home=home, vault=kaal.VAULT, tokens_file=home / "tokens.json", tmp_dir=home / "tmp", max_upload_bytes=64 * 1024, rate_per_minute=1000, background_extraction=background)
        store = tokens.TokenStore(self.settings.tokens_file)
        _record, secret = store.create(f"full-{len(store.list())}", ["read", "reveal", "write", "delete", "admin"])
        self.headers = {"Authorization": f"Bearer {secret}"}
        self.app = server.create_app(self.settings)
        self.client = TestClient(self.app)
        self.worker = self.app.state.kaal.worker

    def upload(self, content=RECEIPT.encode()):
        response = self.client.post("/v1/medical/receipts", headers=self.headers, files={"file": ("receipt.txt", content, "application/octet-stream")})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def receipts(self):
        return self.client.get("/v1/medical/receipts", headers=self.headers).json()["result"]


def test_the_server_returns_from_capture_before_reading_and_the_worker_finishes_it(vault, model):
    model.answer = {"provider": "Example Clinic"}
    service = Service(vault)
    captured = service.upload()
    assert captured["extraction"] == "pending"
    assert service.receipts()[0]["receipt"]["status"] == "extracting"
    assert model.calls == 0

    assert service.worker.drain() == 1
    listed = service.receipts()[0]["receipt"]
    assert (listed["status"], listed["fields"]["amount"], listed["fields"]["provider"]) == ("needs_review", "12.50", "Example Clinic")
    assert service.worker.drain() == 0

    queued = service.client.post("/v1/medical/receipts/reextract", headers=service.headers, json={"unreviewed": True}).json()["result"]
    assert (queued["status"], queued["ids"]) == ("queued", [captured["id"]])
    assert service.receipts()[0]["receipt"]["status"] == "extracting"
    assert service.worker.drain() == 1
    assert service.client.post("/v1/medical/receipts/reextract", headers=service.headers, json={}).status_code == 400


def test_work_left_pending_by_a_restart_is_picked_up(vault):
    Service(vault).upload()
    restarted = Service(vault)
    assert restarted.worker.drain() == 1
    assert restarted.receipts()[0]["receipt"]["fields"]["amount"] == "12.50"


def test_the_worker_thread_reads_receipts_as_they_arrive_and_stops_cleanly(vault):
    service = Service(vault)
    service.worker.start()
    try:
        service.upload()
        deadline = time.monotonic() + 10
        while service.receipts()[0]["receipt"]["status"] == "extracting" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert service.receipts()[0]["receipt"]["fields"]["amount"] == "12.50"
    finally:
        service.worker.stop()
    assert not service.worker._thread.is_alive()


def test_without_background_extraction_the_server_reads_during_capture(vault):
    service = Service(vault, background=False)
    assert service.upload()["extraction"] == "done"
    assert service.receipts()[0]["receipt"]["fields"]["amount"] == "12.50"
    assert server.Settings.from_env({"KAAL_HOME": str(vault)}).background_extraction is True
    assert server.Settings.from_env({"KAAL_HOME": str(vault), "KAAL_BACKGROUND_EXTRACTION": "0"}).background_extraction is False


# --- remembered provider names

def providers():
    return call(kaal.medical_providers, forget=[])["providers"]


def test_a_corrected_provider_name_is_remembered_for_the_next_receipt(vault, model):
    model.answer = {"provider": "Acme Billing, LLP"}
    first = capture(vault, RECEIPT, name="a.txt")["id"]
    assert (stored(first)["fields"]["provider"], stored(first)["field_sources"]["provider"]) == ("Acme Billing, LLP", "ai-unverified")
    assert providers() == []

    call(kaal.medical_update, note_id=first, fields_json=json.dumps({"provider": "Acme Clinic"}))
    assert providers() == [{"printed": "acme billing llp", "shown_as": "Acme Clinic"}]

    model.answer = {"provider": "ACME BILLING LLP"}
    second = stored(capture(vault, RECEIPT, name="b.txt")["id"])
    assert (second["fields"]["provider"], second["field_sources"]["provider"], second["field_raw"]["provider"]) == ("Acme Clinic", "remembered", "ACME BILLING LLP")
    assert second["model_provider"] == "ACME BILLING LLP"


def test_remembered_names_never_touch_a_typed_provider_and_can_be_forgotten(vault, model):
    model.answer = {"provider": "Acme Billing LLP"}
    first = capture(vault, RECEIPT, name="a.txt")["id"]
    call(kaal.medical_update, note_id=first, fields_json=json.dumps({"provider": "Acme Clinic"}))
    typed = capture(vault, RECEIPT, name="b.txt")["id"]
    call(kaal.medical_update, note_id=typed, fields_json=json.dumps({"provider": "Acme Billing LLP"}))
    assert (stored(typed)["fields"]["provider"], stored(typed)["field_sources"]["provider"]) == ("Acme Billing LLP", "manual")

    assert call(kaal.medical_providers, forget=["Acme Billing, LLP"])["providers"] == []
    with pytest.raises(kaal.KaalError):
        call(kaal.medical_providers, forget=["Never Seen"])
    # Saving again must not relearn what the user asked Kaal to forget.
    call(kaal.medical_people, add=["Jane Doe"], remove=[])
    assert providers() == []
    third = stored(capture(vault, RECEIPT, name="c.txt")["id"])
    assert (third["fields"]["provider"], third["field_sources"]["provider"]) == ("Acme Billing LLP", "ai-unverified")


def test_rereading_reviewed_receipts_learns_names_from_what_was_typed_before(vault, model):
    note_id = capture(vault, RECEIPT)["id"]
    call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"provider": "Acme Clinic"}))
    assert providers() == []
    model.answer = {"provider": "Acme Billing LLP"}
    call(kaal.medical_reextract, note_ids=[note_id], unreviewed=False, defer=False)
    assert stored(note_id)["fields"]["provider"] == "Acme Clinic"
    assert providers() == [{"printed": "acme billing llp", "shown_as": "Acme Clinic"}]
    model.answer = {"provider": "Dr. Someone"}
    other = stored(capture(vault, RECEIPT, name="b.txt")["id"])
    assert "model_provider" not in other
