"""A way out and a way back: a readable copy of a vault, restoring an installation from a backup,
and deleting a vault or an account for good. All data here is made up."""
import base64
import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import accounts, server, tokens

GOOD = "correct horse battery"
APP = {"X-Kaal-Request": "1"}
HERE = "https://testserver"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


@pytest.fixture()
def house(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", rate_per_minute=100_000)
    app = server.create_app(settings)
    state = app.state.kaal

    def browser(session=None):
        client = TestClient(app, base_url=HERE, headers=APP, follow_redirects=False)
        return client

    def signed_in(login):
        client = browser()
        done = client.post("/v1/setup", json={"code": state.accounts.new_setup_code(login), "password": GOOD})
        assert done.status_code == 200, done.text
        return client

    def person(login, *, vault="My notes"):
        made = state.accounts.add_account(login)
        server.create_vault(state.accounts, vault, made["id"])
        return signed_in(login)

    yield SimpleNamespace(app=app, state=state, store=state.accounts, settings=settings, browser=browser, signed_in=signed_in, person=person, home=tmp_path)
    kaal.configure_vault(saved)


def note(client, title, body="made-up text", files=()):
    made = client.post("/v1/library/notes", json={"title": title, "body": body, "tags": ["made-up"]}).json()["result"]
    note_id = (made.get("note") or made)["id"]
    attached = []
    for name, data in files:
        answer = client.post(f"/v1/library/notes/{note_id}/attachments", files={"file": (name, data, "application/octet-stream")})
        assert answer.status_code == 200, answer.text
        attached.append(answer.json()["result"]["attachment"]["id"])
    return note_id, attached


def receipt(client, text, name="receipt.txt"):
    answer = client.post("/v1/medical/receipts", files={"file": (name, text.encode(), "text/plain")})
    assert answer.status_code == 200, answer.text
    return answer.json()["result"]["id"]


def delete(client, path, body):
    return client.request("DELETE", path, json=body)


# --- a readable copy

def test_an_owner_downloads_everything_readable(house):
    owner = house.signed_in("owner")
    note_id, (picture,) = note(owner, "Made-up house notes", files=[("Boiler photo.png", PNG)])
    owner.put(f"/v1/library/notes/{note_id}", json={"title": "Made-up house notes", "tags": ["made-up"], "body": f"Pressure 1.5 bar.\n\n![The boiler](attachment:{picture})\n"})
    trashed, _ = note(owner, "Made-up old note")
    owner.post(f"/v1/library/notes/{trashed}/trash")
    receipt(owner, "MADE-UP CLINIC\nPatient: Jane Madeup\nDate of service: 03/04/2026\nAmount paid: $40.00\n")
    receipt(owner, "=HYPERLINK(\"http://example.invalid\")\nAmount paid: $1.00\n", name="=sneaky.txt")

    answer = owner.get("/v1/vault/export")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(answer.content))
    names = archive.namelist()
    assert "README.txt" in names and "receipts.csv" in names
    assert "Notes/Made-up house notes.md" in names
    assert "Notes/Made-up house notes/Boiler photo.png" in names
    assert archive.read("Notes/Made-up house notes/Boiler photo.png") == PNG
    text = archive.read("Notes/Made-up house notes.md").decode()
    assert text.startswith("---\ntitle: \"Made-up house notes\"")
    assert "![The boiler](Made-up%20house%20notes/Boiler%20photo.png)" in text
    assert "Trash/Notes/Made-up old note.md" in names
    rows = list(csv.DictReader(io.StringIO(archive.read("receipts.csv").decode())))
    assert len(rows) == 2
    assert {row["amount"] for row in rows} >= {"40.00"}
    # A value a spreadsheet would run as a formula is written so it is only text.
    assert not any(value.startswith(("=", "+", "@")) for row in rows for value in row.values())
    assert not any(name.startswith("/") or ".." in name.split("/") for name in names)


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_an_owner_may_download_the_readable_copy(house, role):
    owner = house.signed_in("owner")
    house.person("sam")
    vault_id = owner.get("/v1/session").json()["result"]["vault"]["id"]
    assert owner.post("/v1/vault/members", json={"login": "sam", "role": role}).status_code == 200
    sam = house.signed_in("sam")
    assert sam.post("/v1/session/vault", json={"vault": vault_id}).status_code == 200
    assert sam.get("/v1/vault/export").status_code == 403


def test_the_readable_copy_is_written_privately_and_never_into_the_vault(house, tmp_path):
    house.signed_in("owner")
    out = tmp_path / "copy.zip"
    result, _ = kaal.call_captured(kaal.export_readable, SimpleNamespace(output=str(out)))
    assert result["status"] == "exported" and (out.stat().st_mode & 0o777) == 0o600
    with pytest.raises(kaal.KaalError):
        kaal.call_captured(kaal.export_readable, SimpleNamespace(output=str(out)))
    with pytest.raises(kaal.KaalError):
        kaal.call_captured(kaal.export_readable, SimpleNamespace(output=str(house.home / "data" / "inside.zip")))


# --- a way back

def test_an_installation_comes_back_whole_from_a_backup(house, tmp_path, monkeypatch):
    owner = house.signed_in("owner")
    first_note, _ = note(owner, "Made-up note in the first vault", files=[("pic.png", PNG)])
    sam = house.person("sam", vault="Sam's made-up notes")
    sam_note, _ = note(sam, "Made-up note in Sam's vault")
    secret = house.state.tokens.create("made-up-script", ["read", "reveal"])[1]
    monkeypatch.setenv("KAAL_HOME", str(house.home))
    monkeypatch.setenv("KAAL_TOKENS_FILE", str(house.home / "tokens.json"))
    backups = tmp_path / "backups"
    kaal.call_captured(kaal.backup_vault, SimpleNamespace(dest=str(backups), keep=5))
    backup = next(backups.iterdir())

    # Things go wrong after the backup: a note is lost, an account is added, a vault is damaged.
    owner.post(f"/v1/library/notes/{first_note}/trash")
    owner.request("DELETE", f"/v1/library/notes/{first_note}", params={"confirm": "true"})
    house.store.add_account("stranger")
    sam_folder = house.store.folder_of(house.store.vaults_of(house.store.account("sam")["id"])[0])
    shutil.rmtree(sam_folder / "notes")

    result, _ = kaal.call_captured(kaal.restore_backup, SimpleNamespace(backup=str(backup)))
    assert result["status"] == "restored" and result["accounts"] is True and result["other_vaults"] == 1
    assert result["problems"] == [] and result["notes"] == 2
    aside = Path(result["moved_aside"])
    assert aside.is_dir() and aside.parent == house.home.resolve()

    # A fresh server on the restored installation sees it as it was.
    fresh = server.create_app(house.settings)
    client = TestClient(fresh, base_url=HERE, headers=APP)
    signed = client.post("/v1/sign-in", json={"login": "sam", "password": GOOD})
    assert signed.status_code == 200
    assert client.get(f"/v1/library/notes/{sam_note}").status_code == 200
    assert fresh.state.kaal.accounts.account("stranger") is None
    script = TestClient(fresh, base_url=HERE, headers={"Authorization": f"Bearer {secret}"})
    assert script.get(f"/v1/library/notes/{first_note}").status_code == 200


def test_a_backup_the_key_cannot_open_is_refused_before_anything_moves(house, tmp_path, monkeypatch):
    house.signed_in("owner")
    monkeypatch.setenv("KAAL_HOME", str(house.home))
    backups = tmp_path / "backups"
    kaal.call_captured(kaal.backup_vault, SimpleNamespace(dest=str(backups), keep=5))
    backup = next(backups.iterdir())
    other_key = tmp_path / "other.key"
    other_key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    other_key.chmod(0o600)
    monkeypatch.setenv("KAAL_KEY_FILE", str(other_key))
    before = sorted(path.name for path in house.home.iterdir())
    with pytest.raises(kaal.KaalError) as refused:
        kaal.call_captured(kaal.restore_backup, SimpleNamespace(backup=str(backup)))
    assert refused.value.kind == "forbidden"
    assert sorted(path.name for path in house.home.iterdir()) == before


def test_something_that_is_not_a_backup_is_refused(house, tmp_path):
    (tmp_path / "nothing").mkdir()
    with pytest.raises(kaal.KaalError) as refused:
        kaal.call_captured(kaal.restore_backup, SimpleNamespace(backup=str(tmp_path / "nothing")))
    assert refused.value.kind == "not_found"


# --- deleting a vault

def test_an_owner_deletes_a_vault_with_their_password_and_everything_goes(house):
    owner = house.signed_in("owner")
    made = owner.post("/v1/account/vaults", json={"name": "Made-up spare vault"}).json()["result"]["vault"]
    owner.post("/v1/session/vault", json={"vault": made["id"]})
    note(owner, "Made-up note to lose")
    token = owner.post("/v1/account/tokens", json={"name": "made-up", "scopes": ["read"], "vault": made["id"], "password": GOOD}).json()["result"]["secret"]
    folder = house.store.folder_of(house.store.vault(made["id"]))
    assert folder.is_dir()

    assert delete(owner, "/v1/vault", {"password": "not it"}).status_code == 403
    assert folder.is_dir() and house.store.vault(made["id"]) is not None

    gone = delete(owner, "/v1/vault", {"password": GOOD})
    assert gone.status_code == 200, gone.text
    assert gone.json()["result"]["deleted"] == "Made-up spare vault"
    assert gone.json()["result"]["vault"]["id"] != made["id"]       # back on a vault that still exists
    assert not folder.exists() and house.store.vault(made["id"]) is None
    assert house.browser().get("/v1/library/notes", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_the_first_vault_is_never_deleted(house):
    owner = house.signed_in("owner")
    refused = delete(owner, "/v1/vault", {"password": GOOD})
    assert refused.status_code == 409 and "first vault" in refused.json()["error"]
    assert (house.home / "data" / "index.json").is_file()


def test_only_an_owner_deletes_a_vault(house):
    house.person("sam")
    owner = house.signed_in("owner")
    made = owner.post("/v1/account/vaults", json={"name": "Made-up shared"}).json()["result"]["vault"]
    owner.post("/v1/session/vault", json={"vault": made["id"]})
    owner.post("/v1/vault/members", json={"login": "sam", "role": "editor"})
    sam = house.signed_in("sam")
    sam.post("/v1/session/vault", json={"vault": made["id"]})
    assert delete(sam, "/v1/vault", {"password": GOOD}).status_code == 403
    assert house.store.vault(made["id"]) is not None


# --- deleting an account

def test_a_person_deletes_their_account_and_the_vaults_only_they_had(house):
    sam = house.person("sam", vault="Sam's made-up notes")
    note(sam, "Made-up note")
    sam_vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    folder = house.store.folder_of(house.store.vault(sam_vault))
    # Sam is also an editor in the owner's first vault, which stays.
    owner = house.signed_in("owner")
    owner.post("/v1/vault/members", json={"login": "sam", "role": "editor"})
    token = sam.post("/v1/account/tokens", json={"name": "made-up", "scopes": ["read"], "vault": sam_vault, "password": GOOD}).json()["result"]["secret"]

    plan = sam.get("/v1/account/removal").json()["result"]
    assert plan == {"login": "sam", "vaults_to_delete": ["Sam's made-up notes"], "vaults_to_leave": ["Main"], "blocked": []}
    assert delete(sam, "/v1/account", {"password": "not it"}).status_code == 403
    gone = delete(sam, "/v1/account", {"password": GOOD})
    assert gone.status_code == 200, gone.text
    assert gone.json()["result"]["vaults_deleted"] == ["Sam's made-up notes"]
    assert "kaal_session=\"\"" in gone.headers.get("set-cookie", "") or "Max-Age=0" in gone.headers.get("set-cookie", "")
    assert house.store.account("sam") is None and not folder.exists()
    assert sam.get("/v1/session").status_code == 401
    assert house.browser().get("/v1/library/notes", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert owner.get("/v1/library/notes").status_code == 200
    assert [member["login"] for member in owner.get("/v1/vault/members").json()["result"]["members"]] == ["owner"]


def test_the_only_owner_of_a_shared_vault_must_hand_it_over_first(house):
    sam = house.person("sam", vault="Sam's shared made-up vault")
    house.person("alex")
    sam.post("/v1/vault/members", json={"login": "alex", "role": "viewer"})
    plan = sam.get("/v1/account/removal").json()["result"]
    assert len(plan["blocked"]) == 1 and "only owner" in plan["blocked"][0]
    refused = delete(sam, "/v1/account", {"password": GOOD})
    assert refused.status_code == 409 and house.store.account("sam") is not None
    sam.post("/v1/vault/members", json={"login": "alex", "role": "owner"})
    assert delete(sam, "/v1/account", {"password": GOOD}).status_code == 200


def test_the_only_administrator_cannot_leave(house):
    owner = house.signed_in("owner")
    house.person("sam")
    refused = delete(owner, "/v1/account", {"password": GOOD})
    assert refused.status_code == 409 and "only administrator" in refused.json()["error"]


def test_an_account_without_a_password_confirms_by_typing_its_sign_in_name(house):
    house.store.add_account("gina")
    server.create_vault(house.store, "Gina's made-up notes", house.store.account("gina")["id"])
    gina = house.signed_in("gina")
    with sqlite3.connect(house.home / "accounts.db") as db:
        db.execute("DELETE FROM passwords WHERE account_id = ?", (house.store.account("gina")["id"],))
    assert delete(gina, "/v1/account", {"confirm": "not-gina"}).status_code == 403
    assert delete(gina, "/v1/account", {"confirm": "gina"}).status_code == 200


def test_a_token_cannot_delete_an_account(house):
    house.signed_in("owner")
    secret = house.state.tokens.create("made-up", ["read", "reveal", "write", "delete", "admin"])[1]
    script = TestClient(house.app, base_url=HERE, headers={"Authorization": f"Bearer {secret}"})
    assert script.request("DELETE", "/v1/account", json={"password": GOOD}).status_code == 403


def test_an_administrator_removes_someone_after_typing_their_name(house):
    owner = house.signed_in("owner")
    sam = house.person("sam")
    sam_id = house.store.account("sam")["id"]
    assert owner.get(f"/v1/admin/people/{sam_id}/removal").json()["result"]["vaults_to_delete"] == ["My notes"]
    assert delete(owner, f"/v1/admin/people/{sam_id}", {"confirm": "someone else"}).status_code == 400
    assert delete(owner, f"/v1/admin/people/{house.store.account('owner')['id']}", {"confirm": "owner"}).status_code == 409
    gone = delete(owner, f"/v1/admin/people/{sam_id}", {"confirm": "sam"})
    assert gone.status_code == 200 and gone.json()["result"]["vaults_deleted"] == ["My notes"]
    assert sam.get("/v1/session").status_code == 401
    assert delete(sam, f"/v1/admin/people/{sam_id}", {"confirm": "sam"}).status_code == 401


def test_someone_who_is_not_an_administrator_cannot_remove_anyone(house):
    house.signed_in("owner")
    sam = house.person("sam")
    owner_id = house.store.account("owner")["id"]
    assert delete(sam, f"/v1/admin/people/{owner_id}", {"confirm": "owner"}).status_code == 403


# --- from the server's command line

def run(house, *arguments):
    environment = {**os.environ, "KAAL_HOME": str(house.home), "KAAL_VAULT": str(house.home / "data"),
                   "KAAL_TOKENS_FILE": str(house.home / "tokens.json"), "KAAL_TMP_DIR": str(house.home / "tmp")}
    return subprocess.run([sys.executable, "-m", "kaal.server", *arguments], env=environment, capture_output=True, text=True, cwd=Path(__file__).parents[1])


def test_the_command_line_says_what_it_would_remove_and_removes_only_with_yes(house):
    house.signed_in("owner")
    house.person("sam")
    preview = run(house, "account", "remove", "sam")
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["status"] == "would_remove"
    assert house.store.account("sam") is not None
    done = run(house, "account", "remove", "sam", "--yes")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["status"] == "removed"
    assert accounts.AccountStore(house.home / "accounts.db", house.home).account("sam") is None
    refused = run(house, "account", "remove", "owner", "--yes")
    assert refused.returncode != 0 and "ERROR" in refused.stderr
