"""Several accounts on one installation: nobody reaches a vault they do not belong to, by any route."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

ALL = ["read", "reveal", "write", "delete", "admin"]
MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
RECEIPT = b"Patient: DOE, JANE\nAmount paid: $12.50\nService date: 01/02/2026\n"


def fingerprint(folder: Path) -> dict[str, str]:
    """Every file under a vault folder with a digest of its bytes (locks aside)."""
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file() and not path.name.endswith(".lock")}


def upload(content, name):
    return {"file": (name, content, "application/octet-stream")}


@pytest.fixture()
def house(tmp_path):
    """An installation that began with one vault and one token, then gained a second person.

    ``owner`` is the adopted first account, using a token from before accounts
    existed; ``sam`` has a vault of their own and a token that names it.
    """
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp",
                               max_upload_bytes=8 * 1024 * 1024, rate_per_minute=100_000)
    token_store = tokens.TokenStore(settings.tokens_file)
    owner_token = token_store.create("owner-old-token", ALL)[1]
    before = fingerprint(tmp_path / "data")
    app = server.create_app(settings)
    state = app.state.kaal
    client = TestClient(app)

    sam = state.accounts.add_account("sam", "Sam")
    sam_vault = server.create_vault(state.accounts, "Sam's notes", sam["id"])
    sam_token = token_store.create("sam-laptop", ALL, account=sam["id"], vault=sam_vault["id"])[1]
    owner_id, main_vault = state.accounts.defaults()

    def as_(token):
        return {"Authorization": f"Bearer {token}"}

    def ids_in(vault_id):
        with kaal.use_vault(state.vaults.get(vault_id)):
            return [entry["id"] for entry in kaal.load_index()["notes"]]

    def files_of(vault_id, note_id):
        with kaal.use_vault(state.vaults.get(vault_id)):
            return [att["id"] for att in kaal.note_by_id(note_id)["attachments"]]

    yield SimpleNamespace(client=client, app=app, state=state, settings=settings, tokens=token_store, home=tmp_path, before=before,
                          owner=as_(owner_token), owner_id=owner_id, main=main_vault, sam=as_(sam_token), sam_id=sam["id"], sam_vault=sam_vault["id"],
                          as_=as_, ids_in=ids_in, files_of=files_of, owner_token=owner_token)
    kaal.configure_vault(saved)


def test_an_existing_installation_is_adopted_in_place(house):
    people = house.state.accounts.accounts()
    assert [(p["login"], p["admin"]) for p in people] == [("owner", True), ("sam", False)]
    main = house.state.accounts.vault(house.main)
    assert (main["name"], main["folder"]) == ("Main", "data")
    assert house.state.accounts.members(house.main)[0]["role"] == "owner"

    # Nothing in the old vault was moved, re-encrypted or given a key file of its own.
    assert fingerprint(house.home / "data") == house.before
    assert not (house.home / "data" / kaal.VAULT_KEY_NAME).exists()

    # The token from before accounts still works, with everything it could do.
    session = house.client.get("/v1/session", headers=house.owner).json()["result"]
    assert (session["name"], session["scopes"]) == ("owner-old-token", ALL)
    assert house.client.post("/v1/library/notes", headers=house.owner, json={"title": "Still here", "body": "old vault"}).status_code == 200
    assert (house.home / "data" / "notes").is_dir() and len(house.ids_in(house.main)) == 1

    # Starting again adopts nothing more.
    server.create_app(house.settings)
    assert len(house.state.accounts.accounts()) == 2 and len(house.state.accounts.vaults()) == 2


def test_each_account_reads_and_writes_only_its_own_vault(house):
    assert house.client.post("/v1/library/notes", headers=house.owner, json={"title": "Owner's page", "body": "ocelot"}).status_code == 200
    assert house.client.post("/v1/library/notes", headers=house.sam, json={"title": "Sam's page", "body": "serval"}).status_code == 200

    def titles(who):
        return [note["title"] for note in house.client.get("/v1/library/notes", headers=who).json()["result"]]

    def hits(who, word):
        return [hit["title"] for hit in house.client.post("/v1/library/search", headers=who, json={"query": word}).json()["result"]]

    assert titles(house.owner) == ["Owner's page"] and titles(house.sam) == ["Sam's page"]
    assert hits(house.owner, "ocelot") == ["Owner's page"] and hits(house.owner, "serval") == []
    assert hits(house.sam, "serval") == ["Sam's page"] and hits(house.sam, "ocelot") == []

    sam_folder = house.home / "vaults" / house.sam_vault
    assert len(house.ids_in(house.main)) == 1 and len(house.ids_in(house.sam_vault)) == 1
    assert (sam_folder / kaal.VAULT_KEY_NAME).is_file(), "a new vault has a key of its own"
    with kaal.use_vault(house.state.vaults.get(house.sam_vault)):
        sam_key = kaal.get_key()
    with kaal.use_vault(house.state.vaults.get(house.main)):
        assert kaal.get_key() != sam_key
    assert house.state.vaults.get(house.sam_vault) is house.state.vaults.get(house.sam_vault), "one object per vault keeps its locks"


# What each route that names a note needs besides the ids.
BODIES = {
    ("POST", "/v1/notes/{note_id}/append"): {"json": {"body": "intruder"}},
    ("POST", "/v1/notes/{note_id}/attachments"): {"files": upload(b"intruder", "x.txt")},
    ("PUT", "/v1/library/notes/{note_id}"): {"json": {"title": "Intruder", "body": "intruder"}},
    ("POST", "/v1/library/notes/{note_id}/attachments"): {"files": upload(b"intruder", "x.txt")},
    ("PATCH", "/v1/medical/receipts/{note_id}/fields"): {"json": {"fields": {"amount": "1.00"}}},
    ("DELETE", "/v1/notes/{note_id}"): {"params": {"confirm": "true"}},
    ("DELETE", "/v1/library/notes/{note_id}"): {"params": {"confirm": "true"}},
    ("DELETE", "/v1/medical/receipts/{note_id}"): {"params": {"confirm": "true"}},
}


def test_no_route_lets_one_account_reach_another_accounts_items(house):
    client, owner, sam = house.client, house.owner, house.sam

    # The owner's vault: a plain note with a file, a page with a file, and a receipt.
    assert client.post("/v1/notes", headers=owner, json={"title": "Owner secret note", "body": "pin: 4321\nmongoose"}).status_code == 200
    note = house.ids_in(house.main)[-1]
    assert client.post(f"/v1/notes/{note}/attachments", headers=owner, files=upload(MINIMAL_PDF, "owner.pdf")).status_code == 200
    assert client.post("/v1/library/notes", headers=owner, json={"title": "Owner secret page", "body": "mongoose again"}).status_code == 200
    page = house.ids_in(house.main)[-1]
    assert client.post(f"/v1/library/notes/{page}/attachments", headers=owner, files=upload(b"mongoose file\n", "owner.txt")).status_code == 200
    assert client.post("/v1/medical/receipts", headers=owner, files=upload(RECEIPT, "owner-receipt.txt")).status_code == 200
    receipt = house.ids_in(house.main)[-1]
    house.app.state.kaal.worker.drain()
    targets = {"notes": (note, house.files_of(house.main, note)[0]), "library": (page, house.files_of(house.main, page)[0]),
               "medical": (receipt, house.files_of(house.main, receipt)[0])}
    untouched = fingerprint(house.home / "data")

    # Sam asks for each of them by id through every route that takes one.
    spec = server.openapi_spec(house.app)
    tried = []
    for path, operations in sorted(spec["paths"].items()):
        if "{note_id}" not in path:
            continue
        for method in sorted(operations):
            family = path.split("/")[2]
            for note_id, file_id in ([targets[family]] if family != "notes" else targets.values()):
                url = path.replace("{note_id}", note_id).replace("{attachment_id}", file_id).replace("{field}", "pin")
                response = client.request(method.upper(), url, headers=sam, **BODIES.get((method.upper(), path), {}))
                tried.append((method.upper(), path))
                assert response.status_code == 404, f"{method.upper()} {url} -> {response.status_code}: {response.text[:200]}"
                assert "mongoose" not in response.text and "4321" not in response.text
    assert len(set(tried)) == sum(len(ops) for path, ops in spec["paths"].items() if "{note_id}" in path) >= 25

    # Routes that find things by words or take lists of ids.
    assert client.get("/v1/notes", headers=sam).json()["result"] == []
    assert client.get("/v1/library/notes", headers=sam).json()["result"] == []
    assert client.get("/v1/medical/receipts", headers=sam).json()["result"] in ([], {"receipts": []}) or "mongoose" not in client.get("/v1/medical/receipts", headers=sam).text
    for url, body in (("/v1/notes/search", {"query": "mongoose"}), ("/v1/library/search", {"query": "mongoose"})):
        found = client.post(url, headers=sam, json=body)
        assert found.status_code == 200 and found.json()["result"] == [], url
    assert client.post("/v1/notes/lookup", headers=sam, json={"query": "Owner secret note"}).status_code == 404
    assert client.post("/v1/notes/lookup", headers=sam, json={"query": note}).status_code == 404
    for url, body in (("/v1/medical/receipts/reimburse", {"ids": [receipt], "date": "2026-02-01"}),
                      ("/v1/medical/receipts/reextract", {"ids": [receipt]}),
                      ("/v1/medical/receipts/purge", {"ids": [receipt], "confirm": True}),
                      ("/v1/notes/extract", {"query": note, "force": True})):
        response = client.post(url, headers=sam, json=body)
        assert response.status_code < 500, f"{url} -> {response.status_code}"
        assert "mongoose" not in response.text and "owner" not in response.text.lower(), url
    house.app.state.kaal.worker.drain()
    exported = client.get("/v1/export", headers=sam)
    assert exported.status_code == 200 and b"owner" not in exported.content.lower()
    assert "mongoose" not in client.post("/v1/medical/report", headers=sam, json={}).text
    assert client.get("/v1/medical/sources", headers=sam).json()["result"] == {"hosts": []}
    assert client.get("/v1/verify", headers=sam).json()["result"]["notes"] == 0

    # None of it changed the owner's vault, and the owner still has everything.
    assert fingerprint(house.home / "data") == untouched
    assert house.ids_in(house.sam_vault) == []
    assert len(client.get("/v1/notes", headers=owner).json()["result"]) == 3


def test_a_role_caps_what_a_persons_token_may_do(house):
    client = house.client
    assert client.post("/v1/library/notes", headers=house.owner, json={"title": "Household page", "body": "shared"}).status_code == 200
    # Sam is let into the owner's vault to look, and gets a token for it that asks for everything.
    house.state.accounts.grant("sam", house.main, "viewer")
    guest = house.as_(house.tokens.create("sam-in-main", ALL, account=house.sam_id, vault=house.main)[1])

    assert client.get("/v1/session", headers=guest).json()["result"]["scopes"] == ["read", "reveal"]
    assert [n["title"] for n in client.get("/v1/library/notes", headers=guest).json()["result"]] == ["Household page"]
    refused = client.post("/v1/library/notes", headers=guest, json={"title": "No", "body": "no"})
    assert refused.status_code == 403 and "write" in refused.json()["error"]
    signed_in = client.post("/v1/session", json={"token": guest["Authorization"].split()[1]})
    assert signed_in.json()["result"]["scopes"] == ["read", "reveal"]

    house.state.accounts.grant("sam", house.main, "editor")
    assert client.post("/v1/library/notes", headers=guest, json={"title": "Sam added this", "body": "ok"}).status_code == 200
    assert client.get("/v1/export", headers=guest).status_code == 403, "an editor cannot take the whole vault away"

    house.state.accounts.ungrant("sam", house.main)
    gone = client.get("/v1/library/notes", headers=guest)
    assert gone.status_code == 403 and "does not belong" in gone.json()["error"]
    assert client.post("/v1/session", json={"token": guest["Authorization"].split()[1]}).status_code == 401
    # Sam's own vault is unaffected throughout.
    assert client.get("/v1/library/notes", headers=house.sam).status_code == 200


def test_a_disabled_account_is_shut_out_everywhere_until_enabled(house):
    client = house.client
    assert client.get("/v1/status", headers=house.sam).status_code == 200
    house.state.accounts.set_disabled("sam", True)
    assert client.get("/v1/status", headers=house.sam).status_code == 401
    assert client.post("/v1/session", json={"token": house.sam["Authorization"].split()[1]}).status_code == 401
    assert client.get("/v1/status", headers=house.owner).status_code == 200
    house.state.accounts.set_disabled("sam", False)
    assert client.get("/v1/status", headers=house.sam).status_code == 200


def test_receipts_are_read_in_the_background_in_every_vault(house):
    import dataclasses
    app = server.create_app(dataclasses.replace(house.settings, background_extraction=True))
    client = TestClient(app)
    for who in (house.owner, house.sam):
        assert client.post("/v1/medical/receipts", headers=who, files=upload(RECEIPT, "receipt.txt")).status_code == 200
    assert app.state.kaal.worker.drain() == 2, "one pending receipt in each vault"
    assert app.state.kaal.worker.drain() == 0
    for who in (house.owner, house.sam):
        listed = client.get("/v1/medical/receipts", headers=who, params={"inbox": "true"}).json()["result"]
        assert len(listed) == 1


def test_a_request_that_was_never_checked_cannot_touch_a_vault(house):
    unchecked = server.Scoped(house.state, None)
    with pytest.raises(RuntimeError, match="without being checked"):
        with unchecked.in_vault():
            pass
    with pytest.raises(RuntimeError, match="without being checked"):
        unchecked.beside_vault()


def test_the_server_refuses_to_start_when_a_vaults_folder_is_missing(house):
    import shutil
    shutil.rmtree(house.home / "vaults" / house.sam_vault)
    with pytest.raises(server.ServerConfigError, match="Sam's notes"):
        server.create_app(house.settings)


# --- kaal-server account / vault / token

def cli(monkeypatch, capsys, home, *argv):
    monkeypatch.setenv("KAAL_HOME", str(home))
    monkeypatch.setenv("KAAL_VAULT", str(home / "data"))
    monkeypatch.setenv("KAAL_TOKENS_FILE", str(home / "tokens.json"))
    monkeypatch.delenv("KAAL_ACCOUNTS_FILE", raising=False)
    args = server.build_parser().parse_args(list(argv))
    args.func(args)
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_people_and_vaults_can_be_managed_from_the_servers_command_line(house, monkeypatch, capsys):
    out, _ = cli(monkeypatch, capsys, house.home, "account", "add", "Alex", "--name", "Alex A", "--vault-name", "Alex's notes")
    made = json.loads(out)
    assert made["account"]["login"] == "alex" and made["vault"]["name"] == "Alex's notes"
    assert (house.home / made["vault"]["folder"] / "index.json").is_file()

    out, _ = cli(monkeypatch, capsys, house.home, "account", "list")
    listed = {person["login"]: [(v["name"], v["role"]) for v in person["vaults"]] for person in json.loads(out)}
    assert listed == {"owner": [("Main", "owner")], "sam": [("Sam's notes", "owner")], "alex": [("Alex's notes", "owner")]}

    out, _ = cli(monkeypatch, capsys, house.home, "vault", "add", "Household", "--owner", "owner")
    household = json.loads(out)["vault"]["id"]
    cli(monkeypatch, capsys, house.home, "vault", "grant", household, "alex", "editor")
    out, _ = cli(monkeypatch, capsys, house.home, "vault", "list")
    members = {vault["name"]: [(m["login"], m["role"]) for m in vault["members"]] for vault in json.loads(out)}
    assert members["Household"] == [("owner", "owner"), ("alex", "editor")]

    # A token for Alex in the household vault works there and nowhere else.
    out, _err = cli(monkeypatch, capsys, house.home, "token", "create", "alex-phone", "--scopes", "read,reveal,write", "--account", "alex", "--vault", household)
    alex = house.as_(out.strip().splitlines()[-1])
    assert house.client.post("/v1/library/notes", headers=alex, json={"title": "From Alex", "body": "hello"}).status_code == 200
    assert len(house.ids_in(household)) == 1 and house.ids_in(house.main) == []

    # Without --vault, a token goes to the person's first vault.
    out, _err = cli(monkeypatch, capsys, house.home, "token", "create", "alex-laptop", "--scopes", "read", "--account", "alex")
    assert house.tokens.authenticate(out.strip().splitlines()[-1])["vault"] == made["vault"]["id"]

    cli(monkeypatch, capsys, house.home, "vault", "ungrant", household, "alex")
    assert house.client.get("/v1/library/notes", headers=alex).status_code == 403
    cli(monkeypatch, capsys, house.home, "account", "disable", "alex")
    assert house.state.accounts.account("alex")["disabled_at"]
    for bad in (("vault", "grant", "nope", "alex", "viewer"), ("account", "add", "sam"), ("vault", "ungrant", household, "owner"),
                ("token", "create", "x", "--scopes", "read", "--account", "nobody"), ("token", "create", "y", "--scopes", "read", "--account", "sam", "--vault", household)):
        with pytest.raises(SystemExit) as stopped:
            cli(monkeypatch, capsys, house.home, *bad)
        assert str(stopped.value).startswith("ERROR:")


def test_the_command_line_adopts_the_old_vault_before_adding_anyone(tmp_path, monkeypatch, capsys):
    """Adding a person before the server has ever started must not orphan the old tokens."""
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    try:
        kaal.call_captured(kaal.init_vault, SimpleNamespace())
        old_token = tokens.TokenStore(tmp_path / "tokens.json").create("old", ALL)[1]
        cli(monkeypatch, capsys, tmp_path, "account", "add", "sam")
        settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp")
        client = TestClient(server.create_app(settings))
        assert client.get("/v1/session", headers={"Authorization": f"Bearer {old_token}"}).json()["result"]["scopes"] == ALL
        out, _ = cli(monkeypatch, capsys, tmp_path, "account", "list")
        assert [person["login"] for person in json.loads(out)] == ["owner", "sam"]
    finally:
        kaal.configure_vault(saved)


def test_upgrading_an_installation_with_real_shaped_contents_loses_nothing(tmp_path):
    """Notes, files and a receipt made before accounts existed read the same afterwards, byte for byte on disk."""
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    try:
        run = lambda handler, **params: kaal.call_captured(handler, SimpleNamespace(**params))[0]  # noqa: E731
        run(kaal.init_vault)
        page = run(kaal.library_create, title="Old page", body="written before accounts: pangolin", tags="old", source_url="", source_title="")["note"]["id"]
        source = tmp_path / "old.pdf"
        source.write_bytes(MINIMAL_PDF)
        run(kaal.library_attach, note_id=page, file=str(source))
        old_token = tokens.TokenStore(tmp_path / "tokens.json").create("old", ALL)[1]
        before = fingerprint(tmp_path / "data")
        assert len(before) >= 3

        settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp")
        client = TestClient(server.create_app(settings))
        auth = {"Authorization": f"Bearer {old_token}"}

        assert fingerprint(tmp_path / "data") == before
        checked = client.get("/v1/verify", headers=auth, params={"deep": "true"}).json()["result"]
        assert (checked["status"], checked["notes"]) == ("ok", 1)
        shown = client.get(f"/v1/library/notes/{page}", headers=auth).json()["result"]
        assert "pangolin" in json.dumps(shown) and len(shown["note"]["attachments"] if "note" in shown else shown["attachments"]) == 1
        assert [hit["title"] for hit in client.post("/v1/library/search", headers=auth, json={"query": "pangolin"}).json()["result"]] == ["Old page"]
        # The command-line tool, which knows nothing of accounts, still reads the same vault.
        assert [note["title"] for note in run(kaal.library_list)] == ["Old page"]
        assert fingerprint(tmp_path / "data") == before, "reading must not rewrite anything"
    finally:
        kaal.configure_vault(saved)
