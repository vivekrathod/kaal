"""Everything is stored encrypted, and `kaal encrypt-all` brings an older vault to that state."""
import json
from types import SimpleNamespace

import pytest

import kaal.core as kaal
import legacy

MARKER = "zebra-crossing-marker"
NAME_MARKER = "walrus-moustache-marker"  # appears only in titles and attachment names


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
    note_id = call(kaal.add_note, title=f"{title} {NAME_MARKER}", tags="", body=body, body_file=None, sensitivity="auto")["id"]
    if attachment:
        source = directory / f"{NAME_MARKER}-{attachment}"
        source.write_text(f"attachment text {MARKER}\n")
        call(kaal.attach_file, query=note_id, file=str(source), sensitivity="auto", extract=True, ocr=False)
    return note_id


def add_receipt(directory, name="receipt.txt"):
    source = directory / name
    source.write_text(f"Amount paid: $12.50\nService date: 01/02/2026\nmemo {MARKER}\n")
    return call(kaal.medical_capture, file=str(source), title="", source_url="", source_title="", captured_at="")["id"]


def files_holding(marker):
    found = []
    for path in kaal.VAULT.rglob("*"):
        if path.is_file() and (marker.encode() in path.read_bytes() or marker in path.name):
            found.append(str(path.relative_to(kaal.VAULT)))
    return sorted(found)


def files_holding_the_marker():
    """Vault files in which note, attachment or extracted text can be read."""
    return files_holding(MARKER)


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
    assert plan == {"status": "dry-run", "would_encrypt": {"notes": 2, "attachments": 2, "extracted_text": 2, "docling": 0, "metadata": 2, "attachment_names": 0, "index": 1}, "problems": []}
    assert "index.json" in files_holding(NAME_MARKER)
    assert {path: path.read_bytes() for path in before} == before

    done = call(kaal.encrypt_vault, dry_run=False)
    assert done == {"status": "ok", "encrypted": {"notes": 2, "attachments": 2, "extracted_text": 2, "docling": 0, "metadata": 2, "attachment_names": 2, "index": 1}, "problems": []}
    assert files_holding_the_marker() == []
    # Titles and attachment names are gone from every stored file, the index and metadata copies included.
    assert files_holding(NAME_MARKER) == []
    assert kaal.index_is_encrypted()
    assert "notes" not in json.loads(kaal.INDEX_PATH.read_text())
    assert list(kaal.NOTES_DIR.glob("*.md")) == []
    for name in ("extracted.md", "docling.json", "*list.txt", "receipt.txt"):
        assert list(kaal.ATTACH_DIR.rglob(name)) == []

    for checked in (note_id, receipt_id):
        meta = meta_of(checked)
        attachment = meta["attachments"][0]
        assert (meta["storage"], attachment["storage"]) == ("encrypted", "encrypted")
        assert MARKER in kaal.load_note(meta)["body"] or checked == receipt_id
        assert MARKER.encode() in kaal.attachment_original_bytes(meta, attachment)
        assert MARKER in kaal.read_attachment_extracted_sidecar(meta, attachment)
        assert kaal.read_attachment_metadata(checked, attachment["id"])["storage"] == "encrypted"
    assert meta_of(receipt_id)["receipt"]["fields"] == fields_before
    assert kaal.load_note(meta_of(sensitive_id))["body"] == "SSN: 078-05-1120"
    assert call(kaal.verify_vault, deep=True)["problems"] == []

    again = call(kaal.encrypt_vault, dry_run=False)
    assert again["encrypted"] == {"notes": 0, "attachments": 0, "extracted_text": 0, "docling": 0, "metadata": 0, "attachment_names": 0, "index": 0}


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


def test_a_label_is_only_what_the_caller_said_and_never_decides_storage(vault):
    plain = call(kaal.add_note, title="Groceries", tags="", body="- milk", body_file=None, sensitivity="auto")
    forced = call(kaal.add_note, title="Forced", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="public")
    private = call(kaal.add_note, title="IDs", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="sensitive")
    assert [(meta_of(item["id"])["sensitivity"], meta_of(item["id"])["storage"]) for item in (plain, forced, private)] == [("sensitive", "encrypted"), ("public", "encrypted"), ("sensitive", "encrypted")]


def test_a_new_vault_keeps_titles_and_names_out_of_every_file(vault):
    note_id = add_note(vault, "Groceries", f"- milk {MARKER}", attachment="list.txt")
    add_receipt(vault)
    assert kaal.index_is_encrypted()
    assert files_holding(MARKER) == [] and files_holding(NAME_MARKER) == []
    attachment = meta_of(note_id)["attachments"][0]
    container = json.loads((kaal.attachment_note_dir(note_id) / attachment["id"] / "original.json").read_text())
    assert "name" not in container
    assert NAME_MARKER in attachment["name"] and NAME_MARKER in meta_of(note_id)["title"]
    assert kaal.read_attachment_metadata(note_id, attachment["id"])["name"] == attachment["name"]
    assert MARKER.encode() in kaal.attachment_original_bytes(meta_of(note_id), attachment)
    assert call(kaal.verify_vault, deep=True)["problems"] == []
    assert call(kaal.encrypt_vault, dry_run=True)["would_encrypt"] == {"notes": 0, "attachments": 0, "extracted_text": 0, "docling": 0, "metadata": 0, "attachment_names": 0, "index": 0}


def test_an_older_vault_stays_readable_by_older_versions_until_it_is_converted(vault):
    note_id = add_note(vault, "Old", "body", attachment="old.txt")
    legacy.make_plaintext(note_id)
    assert not kaal.index_is_encrypted()

    newer = add_note(vault, "Newer", "body two", attachment="newer.txt")
    assert not kaal.index_is_encrypted()
    assert "notes" in json.loads(kaal.INDEX_PATH.read_text())
    attachment = meta_of(newer)["attachments"][0]
    container = json.loads((kaal.attachment_note_dir(newer) / attachment["id"] / "original.json").read_text())
    assert container["name"] == attachment["name"]

    done = call(kaal.encrypt_vault, dry_run=False)
    assert (done["status"], done["encrypted"]["index"], done["encrypted"]["attachment_names"]) == ("ok", 1, 2)
    assert kaal.index_is_encrypted() and files_holding(NAME_MARKER) == []
    assert call(kaal.verify_vault, deep=True)["problems"] == []


def test_a_container_whose_name_disagrees_with_the_index_is_left_openable(vault):
    note_id = add_note(vault, "Old", "body", attachment="old.txt")
    legacy.make_plaintext(note_id)
    call(kaal.encrypt_vault, dry_run=False)
    # Put a different name back into the container, as if the index had drifted.
    attachment = meta_of(note_id)["attachments"][0]
    path = kaal.attachment_note_dir(note_id) / attachment["id"] / "original.json"
    container = json.loads(path.read_text())
    container["name"] = "some-other-name.txt"
    kaal.secure_write_json(path, container)
    result = call(kaal.encrypt_vault, dry_run=False)
    assert [problem["kind"] for problem in result["problems"]] == ["attachment_name_mismatch"]
    assert json.loads(path.read_text())["name"] == "some-other-name.txt"


def test_status_does_not_need_the_key(vault, monkeypatch, tmp_path):
    add_note(vault, "Groceries", "- milk")
    assert call(kaal.status)["notes"] == 1 and call(kaal.status)["index_encrypted"] is True
    monkeypatch.setenv("KAAL_KEY_FILE", str(tmp_path / "no-such.key"))
    without_key = call(kaal.status)
    assert (without_key["key_exists"], without_key["notes"], without_key["index_encrypted"]) == (False, None, True)


def test_export_and_import_carry_an_encrypted_index(vault, tmp_path):
    note_id = add_note(vault, "Groceries", f"- milk {MARKER}", attachment="list.txt")
    archive = tmp_path / "vault.tar"
    call(kaal.export_vault, output=str(archive))
    assert NAME_MARKER.encode() not in archive.read_bytes() and MARKER.encode() not in archive.read_bytes()
    kaal.configure_vault(tmp_path / "restored")
    call(kaal.import_vault, archive=str(archive))
    assert kaal.index_is_encrypted()
    assert NAME_MARKER in meta_of(note_id)["title"]
    assert MARKER in kaal.load_note(meta_of(note_id))["body"]
