"""Importing notes from other apps: Google Keep's Takeout .zip and Joplin's .jex, by the API and the command line."""
import io
import json
import os
import tarfile
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
    settings = server.Settings(home=tmp_path, vault=kaal.VAULT, tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", max_upload_bytes=16 * 1024 * 1024, rate_per_minute=1000)
    store = tokens.TokenStore(settings.tokens_file)
    secrets = {name: store.create(name, scopes)[1] for name, scopes in {"full": ["read", "reveal", "write", "delete"], "reader": ["read"]}.items()}
    client = TestClient(server.create_app(settings))

    def post(content, name="full", route="keep", accept="application/json", **form):
        return client.post(f"/v1/library/import/{route}", headers={"Authorization": f"Bearer {secrets[name]}", "Accept": accept},
                           files={"file": ("export", content, "application/octet-stream")}, data=form)

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
    assert shopping["tags"] == ["home-things"] and shopping["source"]["type"] == "keep"   # the label is a tag; where it came from is not
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


def test_an_export_larger_than_an_ordinary_request_is_accepted(service):
    # Real exports run to tens of MiB, more than the 8 MiB any route but an upload accepts.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("Takeout/Keep/Big.json", json.dumps({"title": "Big", "textContent": "x", "createdTimestampUsec": MADE,
                                                               "attachments": [{"filePath": "big.png", "mimetype": "image/png"}]}))
        archive.writestr("Takeout/Keep/big.png", PICTURE + os.urandom(9 * 1024 * 1024))
    response = service.post(out.getvalue())
    assert response.status_code == 200, response.text
    assert response.json()["result"]["files"] == 1


def test_a_client_that_asks_is_told_note_by_note_and_then_given_the_answer(service):
    lines = [json.loads(line) for line in service.post(takeout_zip(), accept="application/x-ndjson").text.splitlines()]
    *steps, last = lines
    assert steps == [{"at": at, "total": 6} for at in range(1, 7)]
    assert last["status"] == 200 and last["result"]["notes"] == 4
    refused = [json.loads(line) for line in service.post(b"not a zip", accept="application/x-ndjson").text.splitlines()]
    assert refused[-1]["status"] == 400 and "zip" in refused[-1]["error"]


def joplin_item(title, body, **props):
    return f"{title}\n\n{body}\n\n" + "".join(f"{key}: {value}\n" for key, value in props.items())


def jex(extra=()):
    """A .jex as Joplin makes it: a tar of the RAW export, notes and resources at its top."""
    note, picture, other = "a" * 32, "b" * 32, "c" * 32
    files = {f"{note}.md": joplin_item("Made-up trip", f"Pack snacks.\n\n![map.png](:/{picture})", id=note, type_=1),
             f"{other}.md": joplin_item("Made-up list", "Eggs", id=other, type_=1),
             f"{picture}.md": joplin_item("map.png", "", id=picture, mime="image/png", file_extension="png", type_=4),
             f"resources/{picture}.png": PICTURE, **dict(extra)}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return out.getvalue()


def test_a_joplin_jex_becomes_notes_once_and_nothing_in_it_reaches_outside(service, tmp_path):
    escape = joplin_item("Escape", "y", id="d" * 32, type_=1)
    response = service.post(jex({"../../escape.md": escape}), route="joplin")
    assert response.status_code == 200, response.text
    done = response.json()["result"]
    assert done["imported_notes"] == 2 and done["imported_attachments"] == 1
    notes = service.notes()
    assert set(notes) == {"Made-up trip", "Made-up list"} and len(notes["Made-up trip"]["attachments"]) == 1
    assert not (tmp_path.parent / "escape.md").exists()
    again = service.post(jex(), route="joplin").json()["result"]
    assert again["imported_notes"] == 0 and again["existing_notes"] == 2 and len(service.notes()) == 2
    assert service.post(b"not a tar", route="joplin").status_code == 400


def test_notes_can_be_listed_by_where_they_came_from(service):
    service.post(takeout_zip())
    kaal.call_captured(kaal.library_create, SimpleNamespace(title="Made-up own note", tags="", body="written here", source_url="", source_title=""))
    listed, options = kaal.call_captured(kaal.library_list, SimpleNamespace(trash=False, limit=100, offset=0, q="", origin="keep"))
    assert {n["title"] for n in listed} == {"Shopping", "Passport", "Garden", "Call the plumber"}
    assert options["meta"]["origins"] == {"keep": 4, "kaal": 1}
    found, options = kaal.call_captured(kaal.library_search, SimpleNamespace(query="note", trash=False, limit=50, offset=0, origin="kaal"))
    assert [n["title"] for n in found] == ["Made-up own note"] and options["meta"]["origins"] == {"keep": 4, "kaal": 1}


def test_older_import_tags_come_off_imported_notes_only_and_their_times_stay(service):
    made = kaal.create_note("Made-up Joplin note", "x", tags=["imported", "joplin", "joplin-notebook-travel", "joplin-notebook-2024", "kept"],
                            source={"type": "joplin", "id": "f" * 32}, created="2020-01-01T00:00:00+00:00")
    mine = kaal.create_note("Made-up own note", "y", tags=["imported", "joplin-notebook-travel"])
    counted, _ = kaal.call_captured(kaal.tidy_import_tags, SimpleNamespace(dry_run=True))
    assert counted == {"status": "dry-run", "notes": 1}
    assert kaal.call_captured(kaal.tidy_import_tags, SimpleNamespace(dry_run=False))[0] == {"status": "tidied", "notes": 1}
    by_id = {n["id"]: n for n in kaal.load_index()["notes"]}
    assert by_id[made["id"]]["tags"] == ["2024", "kept", "travel"] and by_id[made["id"]]["updated"] == "2020-01-01T00:00:00+00:00"
    assert kaal.load_note(by_id[made["id"]])["tags"] == ["2024", "kept", "travel"]
    assert by_id[mine["id"]]["tags"] == ["imported", "joplin-notebook-travel"], "a note written in Kaal keeps its tags"
    assert kaal.call_captured(kaal.tidy_import_tags, SimpleNamespace(dry_run=False))[0]["notes"] == 0
