"""Importing Google Keep notes from a Google Takeout .zip, by the API and the command line."""
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

PICTURE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
# 2024-03-01T10:00:00Z and an hour later, in Keep's microseconds.
MADE, EDITED = 1709287200_000000, 1709290800_000000

NOTES = {
    "Shopping.json": {"title": "Shopping", "textContent": "Milk\nBread", "labels": [{"name": "Home Things"}],
                      "annotations": [{"url": "https://example.com/list", "title": "The list", "source": "WEBLINK"}],
                      "isTrashed": False, "isArchived": False, "createdTimestampUsec": MADE, "userEditedTimestampUsec": EDITED},
    "Packing.json": {"title": "", "listContent": [{"text": "Passport", "isChecked": True}, {"text": "Charger", "isChecked": False}],
                     "isTrashed": False, "isArchived": True, "createdTimestampUsec": MADE + 1, "userEditedTimestampUsec": EDITED},
    "Garden.json": {"title": "Garden", "textContent": "", "attachments": [{"filePath": "beds.jpeg", "mimetype": "image/png"}],
                    "isTrashed": False, "isArchived": False, "createdTimestampUsec": MADE + 2, "userEditedTimestampUsec": EDITED},
    "Old.json": {"title": "Old", "textContent": "gone", "isTrashed": True, "createdTimestampUsec": MADE + 3},
    "Blank.json": {"title": "", "textContent": "", "isTrashed": False, "createdTimestampUsec": MADE + 4},
    "Reminder.json": {"title": "Call the plumber", "textContent": "", "isTrashed": False, "createdTimestampUsec": MADE + 5},
}


def takeout_zip(notes=NOTES):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, note in notes.items():
            archive.writestr(f"Takeout/Keep/{name}", json.dumps(note))
            archive.writestr(f"Takeout/Keep/{name[:-5]}.html", "<html></html>")
        archive.writestr("Takeout/Keep/beds.png", PICTURE)  # Takeout names the file .png though the note says .jpeg
        archive.writestr("Takeout/archive_browser.html", "<html></html>")
    return out.getvalue()


@pytest.fixture()
def service(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=kaal.VAULT, tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", max_upload_bytes=8 * 1024 * 1024, rate_per_minute=1000)
    store = tokens.TokenStore(settings.tokens_file)
    secrets = {name: store.create(name, scopes)[1] for name, scopes in {"full": ["read", "reveal", "write", "delete"], "reader": ["read"]}.items()}
    client = TestClient(server.create_app(settings))

    def post(content, name="full", **form):
        return client.post("/v1/library/import/keep", headers={"Authorization": f"Bearer {secrets[name]}"},
                           files={"file": ("takeout.zip", content, "application/zip")}, data=form)

    def notes():
        listed = client.get("/v1/library/notes", headers={"Authorization": f"Bearer {secrets['full']}"}).json()["result"]
        return {n["title"]: client.get(f"/v1/library/notes/{n['id']}", headers={"Authorization": f"Bearer {secrets['full']}"}).json()["result"]["note"] for n in listed}

    yield SimpleNamespace(post=post, notes=notes, tmp_path=tmp_path)
    kaal.configure_vault(saved)


def test_a_takeout_zip_becomes_notes_with_their_checklists_pictures_tags_and_times(service):
    response = service.post(takeout_zip())
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"status": "imported", "notes": 4, "files": 1, "already_here": 0, "trashed": 1, "empty": 1, "unreadable": 0, "missing_files": 0}

    notes = service.notes()
    assert set(notes) == {"Shopping", "Passport", "Garden", "Call the plumber"}
    assert notes["Call the plumber"]["body"].strip() == "Call the plumber"  # a title alone is a note too
    shopping = notes["Shopping"]
    assert shopping["body"].startswith("Milk\nBread") and "[The list](<https://example.com/list>)" in shopping["body"]
    assert {"imported", "keep", "home-things"} <= set(shopping["tags"])
    assert shopping["created"] == "2024-03-01T10:00:00+00:00" and shopping["updated"] == "2024-03-01T11:00:00+00:00"

    packing = notes["Passport"]  # no title in Keep: the first line names it
    assert "- [x] Passport\n- [ ] Charger" in packing["body"] and "archived" in packing["tags"]

    garden = notes["Garden"]
    [picture] = garden["attachments"]
    assert garden["body"].strip() == f"![beds.png](attachment:{picture['id']})"
    assert garden["updated"] == "2024-03-01T11:00:00+00:00"  # attaching the picture did not move it to today


def test_importing_again_adds_only_what_is_new_and_a_dry_run_writes_nothing(service):
    counted = service.post(takeout_zip(), dry_run="true").json()["result"]
    assert counted["status"] == "dry-run" and counted["notes"] == 4 and service.notes() == {}
    assert service.post(takeout_zip()).json()["result"]["notes"] == 4
    more = dict(NOTES, **{"New.json": {"title": "New", "textContent": "fresh", "createdTimestampUsec": MADE + 9}})
    again = service.post(takeout_zip(more)).json()["result"]
    assert again["notes"] == 1 and again["already_here"] == 4
    assert len(service.notes()) == 5


def test_what_is_not_a_keep_export_is_refused_and_readers_cannot_import(service):
    assert service.post(b"not a zip").status_code == 400
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("Takeout/Mail/all.mbox", "x")
    refused = service.post(empty.getvalue())
    assert refused.status_code == 400 and "Keep" in refused.text
    assert service.post(takeout_zip(), name="reader").status_code == 403


def test_names_in_the_zip_cannot_reach_outside_and_file_names_in_notes_cannot_either(service, tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Takeout/Keep/Trip.json", json.dumps({"title": "Trip", "textContent": "x", "createdTimestampUsec": MADE,
                                                                "attachments": [{"filePath": "../../secret.png", "mimetype": "image/png"}]}))
        archive.writestr("../../Keep/escape.json", json.dumps({"title": "Escape", "textContent": "y", "createdTimestampUsec": MADE + 1}))
    (tmp_path / "secret.png").write_bytes(PICTURE)
    result = service.post(out.getvalue()).json()["result"]
    assert result["missing_files"] == 1 and service.notes()["Trip"]["attachments"] == []
    assert not (tmp_path.parent / "Keep").exists()


def test_the_command_line_reads_an_unpacked_folder(service, tmp_path):
    folder = tmp_path / "unpacked" / "Takeout" / "Keep"
    folder.mkdir(parents=True)
    (folder / "Note.json").write_text(json.dumps({"title": "Folder note", "textContent": "hello", "createdTimestampUsec": MADE}))
    result, _ = kaal.call_captured(kaal.import_keep, SimpleNamespace(path=str(tmp_path / "unpacked"), dry_run=False, tags="from-keep"))
    assert result["notes"] == 1
    assert "from-keep" in service.notes()["Folder note"]["tags"]
