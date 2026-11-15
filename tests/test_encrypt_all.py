"""Everything is stored encrypted, and `kaal encrypt-all` brings an older vault to that state."""
import json
from types import SimpleNamespace

import pytest

import kaal.core as kaal
import legacy

MARKER = "zebra-crossing-marker"


@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield tmp_path
    kaal.configure_vault(saved)


def call(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


def meta_of(note_id):
    return next(note for note in kaal.load_index()["notes"] if note["id"] == note_id)


def add_note(directory, title, body, *, attachment=None):
    note_id = call(kaal.add_note, title=title, tags="", body=body, body_file=None, sensitivity="auto")["id"]
    if attachment:
        source = directory / attachment
        source.write_text(f"attachment text {MARKER}\n")
        call(kaal.attach_file, query=note_id, file=str(source), sensitivity="auto", extract=True, ocr=False)
    return note_id


def add_receipt(directory, name="receipt.txt"):
    source = directory / name
    source.write_text(f"Amount paid: $12.50\nService date: 01/02/2026\nmemo {MARKER}\n")
    return call(kaal.medical_capture, file=str(source), title="", source_url="", source_title="", captured_at="")["id"]


def files_holding_the_marker():
    """Vault files, other than the index and attachment metadata, in which the marker can be read."""
    found = []
    for path in kaal.VAULT.rglob("*"):
        if path.is_file() and path.name not in {"index.json", "metadata.json"} and MARKER.encode() in path.read_bytes():
            found.append(str(path.relative_to(kaal.VAULT)))
    return sorted(found)


def test_nothing_new_is_written_readable(vault):
    add_note(vault, "Groceries", f"- milk {MARKER}", attachment="list.txt")
    add_receipt(vault)
    assert files_holding_the_marker() == []
    assert list(kaal.NOTES_DIR.glob("*.md")) == []
    assert all(note["storage"] == "encrypted" and all(att["storage"] == "encrypted" for att in note["attachments"]) for note in kaal.load_index()["notes"])


def test_encrypt_all_converts_an_older_vault_and_keeps_every_byte(vault):
    note_id = add_note(vault, "Groceries", f"- milk {MARKER}", attachment="list.txt")
    receipt_id = add_receipt(vault)
    sensitive_id = call(kaal.add_note, title="Private", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="sensitive")["id"]
    fields_before = dict(meta_of(receipt_id)["receipt"]["fields"])
    legacy.make_plaintext(note_id)
    legacy.make_plaintext(receipt_id)
    assert len(files_holding_the_marker()) == 5  # one note body, two originals, two extracted texts

    before = {path: path.read_bytes() for path in kaal.VAULT.rglob("*") if path.is_file() and path.name != ".medical-capture.lock"}
    plan = call(kaal.encrypt_vault, dry_run=True)
    assert plan == {"status": "dry-run", "would_encrypt": {"notes": 2, "attachments": 2, "extracted_text": 2, "docling": 0}, "problems": []}
    assert {path: path.read_bytes() for path in before} == before

    done = call(kaal.encrypt_vault, dry_run=False)
    assert done == {"status": "ok", "encrypted": {"notes": 2, "attachments": 2, "extracted_text": 2, "docling": 0}, "problems": []}
    assert files_holding_the_marker() == []
    assert list(kaal.NOTES_DIR.glob("*.md")) == []
    for name in ("extracted.md", "docling.json", "list.txt", "receipt.txt"):
        assert list(kaal.ATTACH_DIR.rglob(name)) == []

    for checked in (note_id, receipt_id):
        meta = meta_of(checked)
        attachment = meta["attachments"][0]
        assert (meta["storage"], attachment["storage"]) == ("encrypted", "encrypted")
        assert MARKER in kaal.load_note(meta)["body"] or checked == receipt_id
        assert MARKER.encode() in kaal.attachment_original_bytes(meta, attachment)
        assert MARKER in kaal.read_attachment_extracted_sidecar(meta, attachment)
        stored_meta = json.loads((kaal.attachment_note_dir(checked) / attachment["id"] / "metadata.json").read_text())
        assert stored_meta["storage"] == "encrypted"
    assert meta_of(receipt_id)["receipt"]["fields"] == fields_before
    assert kaal.load_note(meta_of(sensitive_id))["body"] == "SSN: 078-05-1120"
    assert call(kaal.verify_vault, deep=True)["problems"] == []

    again = call(kaal.encrypt_vault, dry_run=False)
    assert again["encrypted"] == {"notes": 0, "attachments": 0, "extracted_text": 0, "docling": 0}


def test_encrypt_all_converts_stored_docling_output_too(vault):
    note_id = add_note(vault, "Scan", "body", attachment="scan.txt")
    legacy.make_plaintext(note_id)
    attachment = meta_of(note_id)["attachments"][0]
    folder = kaal.attachment_note_dir(note_id) / attachment["id"]
    (folder / "docling.json").write_text(json.dumps({"texts": [{"text": MARKER}]}))
    assert call(kaal.encrypt_vault, dry_run=False)["encrypted"]["docling"] == 1
    assert not (folder / "docling.json").exists()
    assert kaal.read_attachment_docling_sidecar(meta_of(note_id), meta_of(note_id)["attachments"][0]) == {"texts": [{"text": MARKER}]}
    assert files_holding_the_marker() == []


def test_damaged_items_are_reported_and_left_exactly_as_they_were(vault):
    damaged = add_note(vault, "Damaged", "body one", attachment="damaged.txt")
    missing = add_note(vault, "Missing", "body two")
    healthy = add_note(vault, "Healthy", f"body {MARKER}")
    for note_id in (damaged, missing, healthy):
        legacy.make_plaintext(note_id)
    attachment = meta_of(damaged)["attachments"][0]
    original = kaal.attachment_note_dir(damaged) / attachment["id"] / attachment["stored_name"]
    original.write_text("these are not the bytes that were stored")
    kaal.plaintext_note_path(missing).unlink()

    result = call(kaal.encrypt_vault, dry_run=False)
    assert result["status"] == "problems"
    assert sorted(problem["kind"] for problem in result["problems"]) == ["attachment_hash_mismatch", "note_file_missing"]
    assert result["encrypted"]["notes"] == 2  # the damaged note's own text and the healthy note
    assert original.read_text() == "these are not the bytes that were stored"
    assert meta_of(damaged)["attachments"][0]["storage"] == "plaintext"
    assert meta_of(missing)["storage"] == "plaintext"
    assert meta_of(healthy)["storage"] == "encrypted"
    # Only the untouched, damaged attachment's extracted text is still readable.
    assert files_holding_the_marker() == [f"attachments/{damaged}/{attachment['id']}/extracted.md"]


def test_a_run_that_stops_part_way_can_be_finished_later(vault, monkeypatch):
    first = add_note(vault, "First", f"one {MARKER}")
    second = add_note(vault, "Second", f"two {MARKER}", attachment="second.txt")
    for note_id in (first, second):
        legacy.make_plaintext(note_id)
    real = kaal.save_index
    saves = []

    def save_then_fail(index):
        real(index)
        saves.append(1)
        if len(saves) == 3:  # after the second note's attachment is recorded as encrypted
            raise RuntimeError("power cut")

    monkeypatch.setattr(kaal, "save_index", save_then_fail)
    with pytest.raises(RuntimeError):
        call(kaal.encrypt_vault, dry_run=False)
    monkeypatch.setattr(kaal, "save_index", real)
    # The index says encrypted, the readable copies are still there: nothing was lost.
    attachment = meta_of(second)["attachments"][0]
    assert (meta_of(first)["storage"], meta_of(second)["storage"], attachment["storage"]) == ("encrypted", "encrypted", "encrypted")
    assert MARKER.encode() in kaal.attachment_original_bytes(meta_of(second), attachment)
    assert files_holding_the_marker() != []

    assert call(kaal.encrypt_vault, dry_run=False)["status"] == "ok"
    assert files_holding_the_marker() == []
    assert call(kaal.verify_vault, deep=True)["problems"] == []


def test_labels_survive_without_deciding_storage(vault):
    public = call(kaal.add_note, title="Groceries", tags="", body="- milk", body_file=None, sensitivity="auto")
    forced = call(kaal.add_note, title="Forced", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="public")
    private = call(kaal.add_note, title="IDs", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="auto")
    assert [(meta_of(item["id"])["sensitivity"], meta_of(item["id"])["storage"]) for item in (public, forced, private)] == [("public", "encrypted"), ("public", "encrypted"), ("sensitive", "encrypted")]
