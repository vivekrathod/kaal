"""Several vaults in one process: each has its own folder, key, locks and search memory.

The last test drives every route of the server with the default vault booby-trapped,
so a piece of server code that forgets to say which vault it means fails loudly.
"""
import logging
import os
import shutil
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
RECEIPT = b"Patient: DOE, JANE\nAmount paid: $12.50\nService date: 01/02/2026\n"


def run(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


def new_vault(folder, **kwargs):
    vault = kaal.Vault(folder, **kwargs)
    with kaal.use_vault(vault):
        run(kaal.init_vault)
    return vault


def add_note(title, body):
    return run(kaal.library_create, title=title, body=body, tags="", source_url="", source_title="")["note"]["id"]


def titles():
    return sorted(note["title"] for note in run(kaal.library_list))


def found(query):
    return sorted(hit["title"] for hit in run(kaal.library_search, query=query))


@pytest.fixture()
def two(tmp_path):
    """Two vaults with different keys, and the default vault pointed at an empty folder."""
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "default")
    yield SimpleNamespace(a=new_vault(tmp_path / "a", key=os.urandom(32)), b=new_vault(tmp_path / "b", key=os.urandom(32)),
                          default=tmp_path / "default")
    kaal.configure_vault(saved)


def test_each_vault_sees_only_its_own_notes(two):
    with kaal.use_vault(two.a):
        in_a = add_note("Alpha", "the alpha word is aardvark")
    with kaal.use_vault(two.b):
        in_b = add_note("Beta", "the beta word is badger")

    with kaal.use_vault(two.a):
        assert titles() == ["Alpha"]
        assert found("aardvark") == ["Alpha"] and found("badger") == []
        with pytest.raises(kaal.KaalError):
            run(kaal.library_show, note_id=in_b)
    with kaal.use_vault(two.b):
        assert titles() == ["Beta"]
        assert found("badger") == ["Beta"] and found("aardvark") == []
        with pytest.raises(kaal.KaalError):
            run(kaal.library_show, note_id=in_a)

    assert sorted(path.name for path in two.a.notes_dir.iterdir()) != sorted(path.name for path in two.b.notes_dir.iterdir())
    assert not two.default.exists(), "work in a chosen vault must never touch the default one"


def test_one_vaults_files_cannot_be_read_with_another_vaults_key(two, tmp_path):
    with kaal.use_vault(two.a):
        add_note("Alpha", "aardvark")
    # Someone copies vault A's files under vault B's key.
    stolen = tmp_path / "stolen"
    shutil.copytree(two.a.root, stolen)
    with kaal.use_vault(kaal.Vault(stolen, key=two.b.key)):
        with pytest.raises(kaal.KaalError):
            titles()
    # With the right key the copy reads fine, so the failure above was the key.
    with kaal.use_vault(kaal.Vault(stolen, key=two.a.key)):
        assert titles() == ["Alpha"]


def test_a_vault_without_its_own_key_uses_the_installations_key(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "default")
    try:
        plain = new_vault(tmp_path / "plain")
        with kaal.use_vault(plain):
            add_note("Plain", "pangolin")
        with kaal.use_vault(kaal.Vault(tmp_path / "plain")):
            assert titles() == ["Plain"]
        with kaal.use_vault(kaal.Vault(tmp_path / "plain", key=os.urandom(32))):
            with pytest.raises(kaal.KaalError):
                titles()
    finally:
        kaal.configure_vault(saved)


def test_the_chosen_vault_is_put_back_afterwards_even_after_an_error(two):
    assert kaal.current_vault() is kaal.default_vault()
    with kaal.use_vault(two.a):
        assert kaal.current_vault() is two.a
        with kaal.use_vault(two.b):
            assert kaal.current_vault() is two.b
        assert kaal.current_vault() is two.a
        with pytest.raises(RuntimeError):
            with kaal.use_vault(two.b):
                raise RuntimeError("boom")
        assert kaal.current_vault() is two.a
    assert kaal.current_vault() is kaal.default_vault()


def test_a_new_thread_starts_on_the_default_vault_not_on_its_parents(two):
    seen = []
    with kaal.use_vault(two.a):
        thread = threading.Thread(target=lambda: seen.append(kaal.current_vault()))
        thread.start()
        thread.join()
    assert seen == [kaal.default_vault()]


def test_holding_one_vaults_lock_does_not_hold_up_another(two):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold_a():
        with kaal.use_vault(two.a), kaal.vault_capture_lock():
            entered.set()
            release.wait(10)

    def write_b():
        with kaal.use_vault(two.b):
            add_note("Beta", "badger")
        done.set()

    holder = threading.Thread(target=hold_a)
    holder.start()
    assert entered.wait(10)
    try:
        with kaal.use_vault(two.a):
            assert not kaal.vault_lock_held(), "the lock belongs to the thread that took it"
        writer = threading.Thread(target=write_b)
        writer.start()
        assert done.wait(10), "vault B was blocked by vault A's lock"
        writer.join()
    finally:
        release.set()
        holder.join()


def test_the_same_vault_still_makes_writers_take_turns(two):
    order, entered, release = [], threading.Event(), threading.Event()

    def first():
        with kaal.use_vault(two.a), kaal.vault_capture_lock():
            entered.set()
            release.wait(10)
            order.append("first")

    def second():
        with kaal.use_vault(two.a), kaal.vault_capture_lock():
            order.append("second")

    one = threading.Thread(target=first)
    one.start()
    assert entered.wait(10)
    other = threading.Thread(target=second)
    other.start()
    other.join(0.3)
    assert other.is_alive() and order == []
    release.set()
    one.join()
    other.join(10)
    assert order == ["first", "second"]


def test_each_vault_keeps_its_own_search_memory(two):
    with kaal.use_vault(two.a):
        in_a = add_note("Alpha", "aardvark")
        assert found("aardvark") == ["Alpha"]
    with kaal.use_vault(two.b):
        in_b = add_note("Beta", "badger")
        assert found("badger") == ["Beta"]
    assert set(two.a.text_cache) == {in_a}
    assert set(two.b.text_cache) == {in_b}
    assert kaal.default_vault().text_cache.keys().isdisjoint({in_a, in_b})


# --- the server names its vault everywhere

class Tripwire:
    """Stands in for the default vault and objects to being used at all."""

    def __init__(self, tripped):
        object.__setattr__(self, "_tripped", tripped)

    def __getattr__(self, name):
        self._tripped.append(name)
        raise AssertionError(f"server code used the default vault (asked for .{name}) instead of the request's vault")


def test_every_route_works_on_the_servers_vault_and_never_on_the_default(tmp_path, monkeypatch, caplog):
    vault = new_vault(tmp_path / "vault")
    settings = server.Settings(home=tmp_path, vault=vault.root, tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp",
                               max_upload_bytes=8 * 1024 * 1024, rate_per_minute=100_000)
    secret = tokens.TokenStore(settings.tokens_file).create("full", ["read", "reveal", "write", "delete", "admin"])[1]
    app = server.create_app(settings)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {secret}"}
    tripped: list[str] = []
    monkeypatch.setattr(kaal, "_DEFAULT_VAULT", Tripwire(tripped))
    caplog.set_level(logging.INFO, logger="kaal.server")
    statuses: list[tuple[str, str, int]] = []

    def call(method, path, expect=(200,), **kwargs):
        headers = kwargs.pop("headers", auth)
        response = client.request(method, path, headers=headers, **kwargs)
        statuses.append((method, path, response.status_code))
        assert response.status_code in expect, f"{method} {path} -> {response.status_code}: {response.text[:300]}"
        return response

    def result(response):
        return response.json()["result"]

    def upload(content, name):
        return {"file": (name, content, "application/octet-stream")}

    def note_ids():
        with kaal.use_vault(vault):
            return {entry["id"] for entry in kaal.load_index()["notes"]}

    def made_by(method, path, **kwargs):
        """Call a route that creates a note and return the new note's id."""
        before = note_ids()
        call(method, path, **kwargs)
        (new_id,) = note_ids() - before
        return new_id

    def last_file(note_id):
        with kaal.use_vault(vault):
            return kaal.note_by_id(note_id)["attachments"][-1]["id"]

    # health, status, sign-in
    call("GET", "/v1/health", headers={})
    call("GET", "/v1/status")
    call("GET", "/v1/verify", params={"deep": "true"})
    call("POST", "/v1/session", json={"token": secret}, headers={})
    cookie = {"Cookie": f"kaal_session={secret}", "X-Kaal-Request": "1"}
    call("GET", "/v1/session", headers=cookie)

    # plain notes
    note = made_by("POST", "/v1/notes", json={"title": "Made-up note", "body": "pin: 1234\nzebra crossing", "tags": ["zz"]})
    call("GET", "/v1/notes")
    call("GET", f"/v1/notes/{note}")
    call("POST", f"/v1/notes/{note}/append", json={"body": "one more made-up line"})
    call("POST", "/v1/notes/search", json={"query": "Made-up"})
    call("POST", "/v1/notes/lookup", json={"query": "Made-up note"})
    call("GET", f"/v1/notes/{note}/fields/pin", expect=(200, 400, 404))
    call("POST", f"/v1/notes/{note}/attachments", files=upload(MINIMAL_PDF, "made-up.pdf"), data={"extract": "true"})
    file_id = last_file(note)
    call("GET", f"/v1/notes/{note}/attachments/{file_id}/content")
    call("GET", f"/v1/notes/{note}/attachments/{file_id}/view")
    call("GET", f"/v1/notes/{note}/attachments/{file_id}/preview")
    call("POST", "/v1/notes/extract", json={"query": "Made-up note", "dry_run": True}, expect=(200, 400))

    # library notes
    page = made_by("POST", "/v1/library/notes", json={"title": "Made-up page", "body": "yak shaving notes"})
    call("GET", "/v1/library/notes")
    call("GET", f"/v1/library/notes/{page}")
    call("PUT", f"/v1/library/notes/{page}", json={"title": "Made-up page", "body": "yak shaving, revised"})
    call("POST", f"/v1/library/notes/{page}/attachments", files=upload(b"plain made-up text about yaks\n", "yaks.txt"))
    page_file_id = last_file(page)
    call("POST", f"/v1/library/notes/{page}/extract", expect=(200, 400))
    call("GET", f"/v1/library/notes/{page}/attachments/{page_file_id}/extracted-text", expect=(200, 404))
    assert [hit["title"] for hit in result(call("POST", "/v1/library/search", json={"query": "yak"}))] == ["Made-up page"]
    call("POST", f"/v1/library/notes/{page}/trash")
    call("POST", f"/v1/library/notes/{page}/restore")
    call("POST", f"/v1/library/notes/{page}/trash")
    call("DELETE", f"/v1/library/notes/{page}", params={"confirm": "true"})

    # browser captures
    call("POST", "/v1/captures", files=upload(MINIMAL_PDF, "captured.pdf"), data={"title": "Made-up capture", "source_url": "https://example.com/a"})
    call("POST", "/v1/captures/text", json={"text": "made-up selected text", "title": "Made-up selection", "source_url": "https://example.com/b"})

    # medical receipts
    receipt_id = made_by("POST", "/v1/medical/receipts", files=upload(RECEIPT, "receipt.txt"), data={"source_url": "https://example.com/r"})
    assert app.state.kaal.worker.drain() >= 0
    call("GET", "/v1/medical/receipts")
    call("GET", f"/v1/medical/receipts/{receipt_id}")
    receipt_file = last_file(receipt_id)
    call("GET", f"/v1/medical/receipts/{receipt_id}/attachments/{receipt_file}/content")
    call("GET", f"/v1/medical/receipts/{receipt_id}/attachments/{receipt_file}/preview", expect=(200, 400, 404, 415))
    call("GET", f"/v1/medical/receipts/{receipt_id}/extracted-text", expect=(200, 404))
    call("PATCH", f"/v1/medical/receipts/{receipt_id}/fields", json={"fields": {"amount": "12.50"}})
    call("POST", f"/v1/medical/receipts/{receipt_id}/review", expect=(200, 400, 409))
    call("POST", "/v1/medical/receipts/reimburse", json={"ids": [receipt_id], "date": "2026-02-01"}, expect=(200, 400, 409))
    call("POST", f"/v1/medical/receipts/{receipt_id}/reopen", expect=(200, 400, 409))
    call("POST", "/v1/medical/people", json={"add": ["Jane Doe"]})
    call("GET", "/v1/medical/people")
    call("GET", "/v1/medical/providers")
    call("POST", "/v1/medical/providers/forget", json={"forget": ["Nobody Clinic"]}, expect=(200, 400, 404))
    call("POST", "/v1/medical/report", json={"year": "2026"})
    call("GET", "/v1/medical/sources")
    call("POST", "/v1/medical/receipts/reextract", json={"ids": [receipt_id]}, expect=(200, 400, 409))
    assert app.state.kaal.worker.drain() >= 0
    call("POST", f"/v1/medical/receipts/{receipt_id}/trash")
    call("POST", f"/v1/medical/receipts/{receipt_id}/restore")
    call("POST", f"/v1/medical/receipts/{receipt_id}/trash")
    call("DELETE", f"/v1/medical/receipts/{receipt_id}", params={"confirm": "true"})
    second_id = made_by("POST", "/v1/medical/receipts", files=upload(RECEIPT + b"second\n", "receipt-2.txt"))
    assert app.state.kaal.worker.drain() >= 0
    call("POST", f"/v1/medical/receipts/{second_id}/trash")
    call("POST", "/v1/medical/receipts/purge", json={"ids": [second_id], "confirm": True})

    # signing in as a person, and one's own account
    people = app.state.kaal.accounts
    code = people.new_setup_code("owner")
    call("POST", "/v1/setup/check", json={"code": code}, headers={})
    set_up = call("POST", "/v1/setup", json={"code": code, "password": "a made-up password"}, headers={})
    recovered = call("POST", "/v1/sign-in/recover", headers={},
                     json={"login": "owner", "code": result(set_up)["recovery_codes"][0], "password": "another made-up password"})
    person = {"Cookie": recovered.headers["set-cookie"].split(";", 1)[0], "X-Kaal-Request": "1"}
    call("POST", "/v1/sign-in", json={"login": "owner", "password": "another made-up password"}, headers={})
    shown = result(call("GET", "/v1/account", headers=person))
    call("DELETE", f"/v1/account/sessions/{next(entry['id'] for entry in shown['sessions'] if not entry['current'])}", headers=person)
    call("POST", "/v1/account/sessions/sign-out-others", headers=person)
    call("POST", "/v1/account/password", json={"current": "another made-up password", "new": "a third made-up password"}, headers=person)
    call("POST", "/v1/account/recovery-codes", json={"password": "a third made-up password"}, headers=person)
    call("POST", "/v1/session/vault", json={"vault": shown["vault"]["id"]}, headers=person)
    # tokens, vaults and sharing, as a person
    made_token = result(call("POST", "/v1/account/tokens", json={"name": "made-up script", "scopes": ["read"], "vault": shown["vault"]["id"]}, headers=person))
    call("GET", "/v1/account/tokens", headers=person)
    call("DELETE", f"/v1/account/tokens/{made_token['token']['id']}", headers=person)
    call("POST", "/v1/account/vaults", json={"name": "Made-up second vault"}, headers=person)
    call("GET", "/v1/vault/members", headers=person)
    call("POST", "/v1/vault/name", json={"name": "Made-up name"}, headers=person)

    # administrators, and open sign-up
    added = result(call("POST", "/v1/admin/people", json={"login": "made-up-person"}, headers=person))["account"]["id"]
    call("GET", "/v1/admin/people", headers=person)
    call("POST", f"/v1/admin/people/{added}/setup-link", headers=person)
    call("POST", f"/v1/admin/people/{added}/admin", json={"on": True}, headers=person)
    call("POST", "/v1/vault/members", json={"login": "made-up-person", "role": "viewer"}, headers=person)
    call("DELETE", f"/v1/vault/members/{added}", headers=person)
    call("POST", f"/v1/admin/people/{added}/disabled", json={"on": True}, headers=person)
    call("POST", "/v1/admin/sign-up", json={"signup": "open"}, headers=person)
    call("GET", "/v1/sign-in/options", headers={})
    call("POST", "/v1/sign-up", json={"login": "made-up-newcomer", "password": "a made-up password"}, headers={})

    # A person's session opens the vault by the same route as a token does.
    assert call("GET", "/v1/library/notes", headers=person).status_code == 200
    call("POST", "/v1/library/notes", json={"title": "Made-up, as a person", "body": "x"}, headers=person)

    # the rest
    call("GET", "/v1/export")
    call("DELETE", f"/v1/notes/{note}", params={"confirm": "true"})
    call("DELETE", "/v1/session", headers=cookie)

    assert tripped == [], f"the default vault was touched: {tripped}"
    assert not [row for row in statuses if row[2] >= 500]

    # Every route the server has was exercised above; a new route must be added here.
    spec = server.openapi_spec(app)
    every = {(method.upper(), path) for path, operations in spec["paths"].items() for method in operations}
    reached = set()
    for record in caplog.records:
        if record.name == "kaal.server" and isinstance(record.args, tuple) and len(record.args) == 5:
            reached.add((record.args[0], record.args[1]))
    assert every - reached == set(), f"routes not exercised: {sorted(every - reached)}"
