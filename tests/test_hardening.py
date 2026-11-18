"""Kaal on the open internet: limits per visitor, password guessing, request sizes, cookies,
what a read-only token sees, and the programs that read stored files. All data is made up."""
import io
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import passwords, receipt_ai, server, tokens, tools

GOOD = "correct horse battery"
APP = {"X-Kaal-Request": "1"}
HERE = "https://testserver"


@pytest.fixture()
def house(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())

    def build(**settings):
        made = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp",
                               rate_per_minute=100_000, **settings)
        return server.create_app(made)

    app = build()
    state = app.state.kaal

    def visitor(address="203.0.113.7", application=None, **headers):
        return TestClient(application or app, base_url=HERE, headers={**APP, **headers}, follow_redirects=False, client=(address, 50000))

    def signed_in(login, address="203.0.113.7"):
        if state.accounts.account(login) is None:
            made = state.accounts.add_account(login)
            server.create_vault(state.accounts, "My notes", made["id"])
        client = visitor(address)
        done = client.post("/v1/setup", json={"code": state.accounts.new_setup_code(login), "password": GOOD})
        assert done.status_code == 200, done.text
        return client

    yield SimpleNamespace(app=app, state=state, store=state.accounts, build=build, visitor=visitor, signed_in=signed_in, home=tmp_path)
    kaal.configure_vault(saved)
    passwords.set_concurrency(4)


def request_for(app, peer, forwarded=""):
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-for", forwarded.encode())] if forwarded else [],
             "client": (peer, 1), "app": app, "query_string": b""}
    return server.Request(scope)


# --- who is asking

def test_the_visitor_is_found_behind_trusted_proxies_only(house):
    behind = house.build(trusted_proxies=server.trusted_proxy_list("private"))
    assert server.client_address(request_for(behind, "172.18.0.3", "198.51.100.9, 172.18.0.1")) == "198.51.100.9"
    assert server.client_address(request_for(behind, "198.51.100.20", "10.1.1.1")) == "198.51.100.20"       # a stranger's claim is ignored
    direct = house.build()
    assert server.client_address(request_for(direct, "127.0.0.1", "198.51.100.9")) == "198.51.100.9"          # Tailscale serve on this machine
    assert server.client_address(request_for(direct, "172.18.0.3", "198.51.100.9")) == "172.18.0.3"          # not trusted by default
    assert server.client_address(request_for(direct, "2001:db8:1:2:3:4:5:6")) == "2001:db8:1:2::/64"
    assert server.client_address(request_for(direct, "not-an-address")) == "unknown"
    with pytest.raises(ValueError):
        server.trusted_proxy_list("10.0.0.0/8, nonsense")
    assert server.trusted_proxy_list("none") == ()


def test_each_address_may_call_the_sign_in_routes_only_so_often(house):
    house.store.add_account("sam")
    one, other = house.visitor("198.51.100.1"), house.visitor("198.51.100.2")
    answers = [one.post("/v1/sign-in", json={"login": "sam", "password": "wrong guess"}).status_code for _ in range(32)]
    assert answers.count(429) >= 1 and 429 not in answers[:5]
    assert one.post("/v1/sign-up", json={"login": "x", "password": "y" * 12}).status_code == 429     # one budget for all such routes
    assert other.post("/v1/sign-in/with/google", json={}).status_code != 429                         # another visitor is unaffected


# --- password guessing

def test_guessing_from_one_address_is_slowed_but_the_person_can_still_sign_in_from_elsewhere(house):
    house.signed_in("sam")
    attacker, sam = house.visitor("198.51.100.66"), house.visitor("192.0.2.10")
    for _ in range(6):        # five are free; the sixth wrong one starts the wait
        assert attacker.post("/v1/sign-in", json={"login": "sam", "password": "wrong guess"}).status_code == 401
    blocked = attacker.post("/v1/sign-in", json={"login": "sam", "password": GOOD})
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert sam.post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 200


def test_trying_many_names_from_one_address_is_slowed_too(house):
    attacker = house.visitor("198.51.100.77")
    codes = [attacker.post("/v1/sign-in", json={"login": f"made-up-{n}", "password": "wrong guess"}).status_code for n in range(22)]
    assert codes[:20] == [401] * 20 and 429 in codes[20:]


def test_many_addresses_together_slow_a_name_by_at_most_a_minute():
    guard = server.SignInGuard()
    for n in range(60):
        guard.failed("sam", f"198.51.100.{n}")
    assert 0 < guard.wait("sam", "192.0.2.1") <= 60
    guard.succeeded("sam", "192.0.2.1")
    assert guard.wait("sam", "192.0.2.1") == 0


def test_flooding_the_guard_with_names_does_not_free_a_locked_one(monkeypatch):
    guard = server.SignInGuard()
    monkeypatch.setattr(server.SignInGuard, "REMEMBERED", 50)
    for _ in range(6):
        guard.failed("sam", "198.51.100.1")
    assert guard.wait("sam", "198.51.100.1") > 0
    for n in range(200):
        guard.failed(f"junk-{n}", f"203.0.113.{n % 250}")
    assert guard.wait("sam", "198.51.100.1") > 0


def test_parallel_guesses_for_one_name_are_checked_one_at_a_time(house):
    house.signed_in("sam")
    attacker = house.visitor("198.51.100.88")
    results = []
    threads = [threading.Thread(target=lambda: results.append(attacker.post("/v1/sign-in", json={"login": "sam", "password": "wrong guess"}).status_code))
               for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(401) <= 5 + 1 and results.count(429) >= 6


def test_when_too_many_passwords_are_being_checked_the_answer_is_busy_not_a_hang(house, monkeypatch):
    house.signed_in("sam")
    passwords.set_concurrency(1)
    monkeypatch.setattr(passwords, "WAIT_SECONDS", 0.2)
    held = passwords._checks
    held.acquire()
    try:
        started = time.monotonic()
        answer = house.visitor("192.0.2.5").post("/v1/sign-in", json={"login": "sam", "password": GOOD})
        assert answer.status_code == 503 and answer.json()["kind"] == "busy" and time.monotonic() - started < 3
    finally:
        held.release()
    assert house.visitor("192.0.2.5").post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 200


def test_recovery_checks_a_code_the_same_way_whether_or_not_the_name_exists(house, monkeypatch):
    asked = []
    monkeypatch.setattr(house.store, "use_recovery_code", lambda account, code: asked.append(account) or False)
    house.visitor("192.0.2.6").post("/v1/sign-in/recover", json={"login": "nobody-made-up", "code": "x", "password": "a long made-up password"})
    assert asked == ["no-such-account"]


def test_choosing_a_new_password_with_a_recovery_code_ends_the_accounts_tokens(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    secret = sam.post("/v1/account/tokens", json={"name": "made-up", "scopes": ["read"], "vault": vault, "password": GOOD}).json()["result"]["secret"]
    code = sam.post("/v1/account/recovery-codes", json={"password": GOOD}).json()["result"]["recovery_codes"][0]
    assert house.visitor("192.0.2.7").post("/v1/sign-in/recover", json={"login": "sam", "code": code, "password": "another made-up password"}).status_code == 200
    assert house.visitor("192.0.2.7").get("/v1/library/notes", headers={"Authorization": f"Bearer {secret}"}).status_code == 401


# --- tokens and setup links

def test_making_a_token_needs_the_password(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    assert sam.post("/v1/account/tokens", json={"name": "a", "scopes": ["read"], "vault": vault}).status_code == 403
    assert sam.post("/v1/account/tokens", json={"name": "a", "scopes": ["read"], "vault": vault, "password": "not it"}).status_code == 403
    assert sam.post("/v1/account/tokens", json={"name": "a", "scopes": ["read"], "vault": vault, "password": GOOD}).status_code == 200


def test_an_administrator_cannot_make_a_setup_link_for_an_account_in_use(house):
    owner = house.signed_in("owner")
    house.signed_in("sam")
    sam_id = house.store.account("sam")["id"]
    refused = owner.post(f"/v1/admin/people/{sam_id}/setup-link")
    assert refused.status_code == 409 and "whoever runs the server" in refused.json()["error"]
    fresh = owner.post("/v1/admin/people", json={"login": "made-up-new"}).json()["result"]["account"]["id"]
    assert owner.post(f"/v1/admin/people/{fresh}/setup-link").status_code == 200


# --- request sizes

def test_strangers_cannot_send_large_bodies(house):
    stranger = house.visitor("198.51.100.3")
    big = b"x" * (2 * 1024 * 1024)
    answer = stranger.post("/v1/medical/receipts", content=big, headers={"Content-Type": "multipart/form-data; boundary=x", "Authorization": "Bearer kaal_made_up_nope"})
    assert answer.status_code == 413
    assert stranger.post("/v1/sign-in", content=b'{"login":"' + b"a" * (2 * 1024 * 1024) + b'"}', headers={"Content-Type": "application/json"}).status_code == 413


def test_a_signed_in_person_may_upload_large_files_but_not_send_huge_json(house):
    sam = house.signed_in("sam")
    made = sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x"}).json()["result"]
    note_id = (made.get("note") or made)["id"]
    upload = sam.post(f"/v1/library/notes/{note_id}/attachments", files={"file": ("big.bin", b"\0" * (3 * 1024 * 1024), "application/octet-stream")})
    assert upload.status_code == 200, upload.text
    huge = sam.post("/v1/library/notes", content=b'{"title":"t","body":"' + b"a" * (9 * 1024 * 1024) + b'"}', headers={"Content-Type": "application/json"})
    assert huge.status_code == 413


# --- cookies and headers

def test_over_https_the_session_cookie_is_host_only_and_a_planted_one_is_ignored(house):
    sam = house.signed_in("sam")
    session = sam.cookies.get("__Host-kaal_session")
    assert session and not sam.cookies.get("kaal_session")
    plain = house.visitor("192.0.2.9")
    planted = plain.get("/v1/session", headers={"Cookie": f"kaal_session={session}", "Sec-Fetch-Site": "same-origin"})
    assert planted.status_code == 401
    assert plain.get("/v1/session", headers={"Cookie": f"kaal_session={session}"}).status_code == 200     # an app from before the change
    assert plain.get("/v1/session", headers={"X-Kaal-Session": session}).status_code == 200


def test_https_answers_tell_browsers_to_stay_on_https(house, tmp_path):
    assert house.visitor().get("/v1/health").headers["Strict-Transport-Security"] == "max-age=31536000"
    insecure = house.build(cookie_secure=False)
    assert "Strict-Transport-Security" not in TestClient(insecure).get("/v1/health").headers


def test_text_from_outside_cannot_forge_log_lines(house, caplog):
    secret = house.state.tokens.create("made-up\nINFO kaal.server forged line", ["read"])[1]
    caplog.set_level(logging.INFO, logger="kaal.server")
    house.visitor().get("/v1/status", headers={"Authorization": f"Bearer {secret}"})
    lines = [record.getMessage() for record in caplog.records if record.name == "kaal.server"]
    assert lines and all("\n" not in line for line in lines)
    assert server.log_safe("a b\x1b[31m") == "a?b?[31m"


# --- what the read scope sees

def test_a_read_only_token_sees_titles_but_not_receipt_values_or_full_addresses(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    sam.post("/v1/medical/receipts", files={"file": ("receipt.txt", b"Patient: Jane Madeup\nAmount paid: $40.00\n", "text/plain")},
             data={"source_url": "https://portal.example/receipt/9?token=made-up-secret"})
    reader = sam.post("/v1/account/tokens", json={"name": "reader", "scopes": ["read"], "vault": vault, "password": GOOD}).json()["result"]["secret"]
    revealer = sam.post("/v1/account/tokens", json={"name": "revealer", "scopes": ["read", "reveal"], "vault": vault, "password": GOOD}).json()["result"]["secret"]
    as_reader = house.visitor(Authorization=f"Bearer {reader}")
    for answer in (as_reader.get("/v1/notes"), as_reader.post("/v1/notes/search", json={"query": "receipt"})):
        text = answer.text
        assert answer.status_code == 200 and "Jane" not in text and "40.00" not in text and "/receipt/9" not in text
        assert "portal.example" in text
    shown = house.visitor(Authorization=f"Bearer {revealer}").get("/v1/notes").json()["result"][0]
    assert shown["receipt"]["fields"].get("amount") == "40.00"
    assert "token=" not in json.dumps(shown)                              # the query string was never kept


# --- programs that read stored files

def test_a_program_that_runs_too_long_is_stopped():
    started = time.monotonic()
    assert tools.run(["/bin/sleep", "5"], timeout=0.5) is None
    assert time.monotonic() - started < 3
    done = tools.run(["/bin/echo", "made-up"], timeout=5, text=True)
    assert done is not None and done.stdout.strip() == "made-up"


def test_a_picture_with_too_many_pixels_is_not_decoded(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    trap = tmp_path / "trap.png"
    buffer = io.BytesIO()
    Image.new("1", (12000, 12000)).save(buffer, format="PNG")
    trap.write_bytes(buffer.getvalue())
    assert len(buffer.getvalue()) < 1_000_000
    assert receipt_ai.receipt_images(trap) == []


# --- what a caller that may change the vault but not read it is told

def test_a_token_that_cannot_reveal_is_told_what_it_changed_by_id_only(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    receipt = {"file": ("receipt.txt", b"Patient: Jane Madeup\nAmount paid: $40.00\n", "text/plain")}
    for scopes in (["write"], ["read", "write"]):
        secret = sam.post("/v1/account/tokens", json={"name": "-".join(scopes), "scopes": scopes, "vault": vault, "password": GOOD}).json()["result"]["secret"]
        script = house.visitor(Authorization=f"Bearer {secret}")
        saved = script.post("/v1/medical/receipts", files=receipt)
        assert saved.status_code == 200, saved.text
        assert "Jane" not in saved.text and "40.00" not in saved.text and "receipt.txt" not in saved.text
        assert json.dumps(saved.json()["result"]).count('"id"') >= 1                 # it still learns which receipt it made
        people = script.post("/v1/medical/people", json={"add": ["Jane Madeup"]})
        assert people.status_code == 200 and "Jane" not in people.text
        note = script.post("/v1/library/notes", json={"title": "A made-up title", "body": "made-up body"})
        assert note.status_code == 200 and "made-up" not in note.text
    # Someone signed in, who may read everything, is answered in full as before.
    full = sam.post("/v1/library/notes", json={"title": "A made-up title", "body": "made-up body"})
    assert "A made-up title" in full.text


def test_ids_only_keeps_ids_and_states_and_drops_what_people_wrote():
    answer = {"note": {"id": "n1", "title": "Made-up", "tags": ["a"], "receipt": {"status": "needs_review", "fields": {"amount": "40.00"}},
                       "attachments": [{"id": "a1", "name": "made-up.pdf", "size": 12}]}, "people": ["Jane Madeup"], "count": 2}
    assert server.ids_only(answer) == {"note": {"id": "n1", "receipt": {"status": "needs_review"}, "attachments": [{"id": "a1", "size": 12}]}, "count": 2}
    assert server.ids_only("a made-up sentence") is None and server.ids_only(["x", "y"]) == []


# --- room

def test_a_vault_that_is_full_refuses_something_new_but_can_still_be_tidied(house, tmp_path):
    app = house.build(vault_quota_bytes=200_000)
    made = app.state.kaal.accounts.add_account("sam")
    server.create_vault(app.state.kaal.accounts, "My notes", made["id"])
    sam = house.visitor(application=app)
    assert sam.post("/v1/setup", json={"code": app.state.kaal.accounts.new_setup_code("sam"), "password": GOOD}).status_code == 200
    big = {"file": ("made-up.bin", b"0" * 150_000, "application/octet-stream")}
    note = sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x"}).json()["result"]
    note_id = note.get("id") or note["note"]["id"]
    assert sam.post(f"/v1/library/notes/{note_id}/attachments", files=big).status_code == 200
    full = sam.post(f"/v1/library/notes/{note_id}/attachments", files=big)
    assert full.status_code == 413 and "full" in full.json()["error"]
    assert sam.post("/v1/library/notes", json={"title": "One more", "body": "x" * 60_000}).status_code == 413
    # Reading, and moving things to the Trash, still work.
    assert sam.get("/v1/library/notes").status_code == 200
    assert sam.post(f"/v1/library/notes/{note_id}/trash").status_code == 200


def test_the_first_vault_and_a_server_without_a_limit_have_no_quota(house):
    tight = house.build(vault_quota_bytes=1)
    request = SimpleNamespace(headers={"content-length": "500000"})
    first = SimpleNamespace(root=house.home / "data")
    server._room_checked(request, tight.state.kaal, first)                           # the installation's own vault: no limit
    other = SimpleNamespace(root=house.home / "elsewhere")
    with pytest.raises(server.HTTPException) as refused:
        server._room_checked(request, tight.state.kaal, other)
    assert refused.value.status_code == 413
    server._room_checked(request, house.build(vault_quota_bytes=0).state.kaal, other)  # 0 means no limit


def test_usage_counts_a_folder_and_what_is_on_its_way(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one").write_bytes(b"0" * 1000)
    usage = server.Usage()
    assert usage.used(tmp_path) == 1000
    usage.add(tmp_path, 500)
    (tmp_path / "a" / "two").write_bytes(b"0" * 9000)
    assert usage.used(tmp_path) == 1500                                               # counted again only every half minute
    usage.forget(tmp_path)
    assert usage.used(tmp_path) == 10_000


# --- files on their way out

def test_only_so_many_files_are_held_for_one_caller_and_for_everyone():
    downloads = server.Downloads(limit_bytes=1000)
    first = downloads.take("sam", 5000)
    assert first is not None                                                          # one file always goes, however large
    assert downloads.take("alex", 10) is None                                         # but nothing more fits beside it
    downloads.give(first)
    tickets = [downloads.take("sam", 10) for _ in range(server.Downloads.PER_CALLER)]
    assert all(tickets) and downloads.take("sam", 10) is None and downloads.take("alex", 10) is not None
    for ticket in tickets:
        downloads.give(ticket)
    assert downloads.take("sam", 10) is not None


def test_a_download_is_counted_until_it_has_left_and_refused_when_too_many_are_held(house):
    sam = house.signed_in("sam")
    note = sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x"}).json()["result"]
    note_id = note.get("id") or note["note"]["id"]
    added = sam.post(f"/v1/library/notes/{note_id}/attachments", files={"file": ("made-up.txt", b"made-up words", "text/plain")})
    assert added.status_code == 200, added.text
    shown = sam.get(f"/v1/library/notes/{note_id}").json()["result"]
    attachment = (shown.get("note") or shown)["attachments"][0]["id"]
    path = f"/v1/notes/{note_id}/attachments/{attachment}/content"
    assert sam.get(path).content == b"made-up words"
    downloads = house.state.downloads
    assert downloads._held == 0 and downloads._by_caller == {}                        # given back once the answer had left
    caller = f"account:{house.store.account('sam')['id']}"
    held = [downloads.take(caller, 1) for _ in range(server.Downloads.PER_CALLER)]
    busy = sam.get(path)
    assert busy.status_code == 503 and busy.json()["kind"] == "busy"
    for ticket in held:
        downloads.give(ticket)
    assert sam.get(path).status_code == 200 and sam.get(f"/v1/notes/{note_id}/attachments/{attachment}/view").status_code == 200
    assert downloads._held == 0


# --- names, and finding out who has an account

def test_a_sign_in_name_cannot_be_shaped_like_an_account_id(house):
    victim = house.store.add_account("sam")
    with pytest.raises(server.accounts.AccountError):
        house.store.add_account(victim["id"])
    with pytest.raises(server.accounts.AccountError):
        house.store.add_account("0123456789ab")
    assert house.store.add_account("0123456789abc")["login"] == "0123456789abc"       # thirteen characters is a name like any other


def test_adding_people_to_a_vault_cannot_be_used_to_try_many_names_quickly(house):
    sam = house.signed_in("sam")
    answers = [sam.post("/v1/vault/members", json={"login": f"made-up-{index}", "role": "viewer"}).status_code for index in range(12)]
    assert answers[:10] == [404] * 10 and answers[10:] == [429, 429]


# --- sign-in services that are slow or down

def test_a_sign_in_service_that_is_down_is_asked_once_not_by_every_visitor():
    from kaal import oidc

    asked = []

    def down(url, form=None):
        asked.append(url)
        raise oidc.SignInServiceError("could not reach the sign-in service", "unreachable")

    services = oidc.Services(down)
    for _ in range(5):
        with pytest.raises(oidc.SignInServiceError):
            services.settings("https://id.example")
    assert len(asked) == 1
    with pytest.raises(oidc.SignInServiceError):
        services._signing_keys("https://id.example")
    assert len(asked) == 1                                                            # the keys need the settings, which are known to be down


def test_many_visitors_at_once_cause_one_request_to_the_sign_in_service():
    from kaal import oidc

    asked = []

    def slow(url, form=None):
        asked.append(url)
        time.sleep(0.2)
        return 200, json.dumps({"issuer": "https://id.example", "authorization_endpoint": "https://id.example/a",
                                "token_endpoint": "https://id.example/t", "jwks_uri": "https://id.example/k"}).encode()

    services = oidc.Services(slow)
    threads = [threading.Thread(target=services.settings, args=("https://id.example",)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(asked) == 1


# --- the programs that read files are told nothing about the server

def test_a_program_reading_a_file_is_not_given_the_servers_environment(monkeypatch):
    monkeypatch.setenv("KAAL_KEY_FILE", "/made-up/key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "made-up")
    done = tools.run(["/usr/bin/env"], timeout=5, text=True)
    assert done is not None and "KAAL_KEY_FILE" not in done.stdout and "ANTHROPIC_API_KEY" not in done.stdout and "PATH=" in done.stdout


# --- the installer's link

def test_the_command_line_makes_no_link_for_an_account_in_use_when_asked_not_to(house):
    import os
    import subprocess
    import sys

    house.signed_in("sam")
    house.store.add_account("alex")
    env = {**os.environ, "KAAL_HOME": str(house.home), "KAAL_VAULT": str(house.home / "data"), "KAAL_KEY_FILE": os.environ.get("KAAL_KEY_FILE", "")}

    def link(*args):
        return subprocess.run([sys.executable, "-m", "kaal.server", "account", "setup-link", *args], capture_output=True, text=True, env=env)

    refused = link("sam", "--new-only")
    assert refused.returncode == 3 and "/#/setup/" not in refused.stdout
    assert "/#/setup/" in link("alex", "--new-only").stdout
    assert "/#/setup/" in link("sam").stdout                                          # on purpose, whoever runs the server still can
