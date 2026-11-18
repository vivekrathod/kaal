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
    assert server.client_address(request_for(direct, "2001:db8:1:2:3:4:5:6")) == "2001:db8:1::/56"
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
    assert 0 < guard.wait("sam", "198.51.100.59") <= 60                               # the last of them to try, with one wrong try of its own
    guard.succeeded("sam", "198.51.100.59")
    assert guard.wait("sam", "198.51.100.59") == 0


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
    assert plain.get("/v1/session", headers={"Cookie": f"kaal_session={session}"}).status_code == 401     # whoever sends it
    assert plain.get("/v1/session", headers={"X-Kaal-Session": session}).status_code == 200               # how an app sends its session
    # An app installed before the change still sends the old cookie, for a sign-in older than the change: it keeps working.
    house.store.put(server.HOST_COOKIE_SINCE, "2999-01-01T00:00:00+00:00")
    older = plain.get("/v1/session", headers={"Cookie": f"kaal_session={session}"})
    assert older.status_code == 200 and "set-cookie" not in older.headers


def test_a_browser_signed_in_before_the_change_is_moved_to_the_new_cookie_once(house):
    sam = house.signed_in("sam")
    session = sam.cookies.get("__Host-kaal_session")
    browser = {"Cookie": f"kaal_session={session}", "Sec-Fetch-Site": "same-origin"}
    plain = house.visitor("192.0.2.9")
    assert plain.get("/v1/session", headers=browser).status_code == 401              # made after the change: the old name is refused
    # The same sign-in, had it been made before this server tied its cookie to the host.
    house.store.put(server.HOST_COOKIE_SINCE, "2999-01-01T00:00:00+00:00")
    moved = plain.get("/v1/session", headers=browser)
    assert moved.status_code == 200
    told = moved.headers.get_list("set-cookie")
    assert any(line.startswith(f"__Host-kaal_session={session};") and "Secure" in line and "HttpOnly" in line for line in told)
    assert any(line.startswith('kaal_session="";') and "Max-Age=0" in line for line in told)
    # A token used as a browser's sign-in (from before there were accounts) is treated the same way.
    secret = tokens.TokenStore(house.home / "tokens.json").create("made-up", ["read"])[1]
    assert plain.get("/v1/session", headers={"Cookie": f"kaal_session={secret}", "Sec-Fetch-Site": "same-origin"}).status_code == 200
    house.store.put(server.HOST_COOKIE_SINCE, "2000-01-01T00:00:00+00:00")
    assert plain.get("/v1/session", headers={"Cookie": f"kaal_session={secret}", "Sec-Fetch-Site": "same-origin"}).status_code == 401


def test_https_answers_tell_browsers_to_stay_on_https(house, tmp_path):
    # Only when asked for: the header covers every port of the host name, which may serve other things over http.
    asked = house.build(hsts=True)
    assert house.visitor(application=asked).get("/v1/health").headers["Strict-Transport-Security"] == "max-age=31536000"
    assert house.visitor().get("/v1/health").headers["Strict-Transport-Security"] == "max-age=0"     # and a browser told so earlier forgets it
    insecure = house.build(cookie_secure=False)
    assert "Strict-Transport-Security" not in TestClient(insecure).get("/v1/health").headers


def test_text_from_outside_cannot_forge_log_lines(house, caplog):
    # A token made before names were checked may still hold a line break: write one straight into the file.
    secret = house.state.tokens.create("made-up", ["read"])[1]
    stored = json.loads((house.home / "tokens.json").read_text())
    stored["tokens"][-1]["name"] = "made-up\nINFO kaal.server forged line"
    (house.home / "tokens.json").write_text(json.dumps(stored))
    house.state.tokens._cache = None
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
    assert full.status_code == 507 and "full" in full.json()["error"]              # not "too large": the clients show this message as written
    assert sam.post("/v1/library/notes", json={"title": "One more", "body": "x" * 60_000}).status_code == 507
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
    assert refused.value.status_code == 507
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


def test_an_account_id_is_not_a_second_name_to_sign_in_recover_or_share_under(house):
    sam = house.signed_in("sam")
    account_id = house.store.account("sam")["id"]
    stranger = house.visitor("198.51.100.77")
    assert stranger.post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 200
    assert stranger.post("/v1/sign-in", json={"login": account_id, "password": GOOD}).status_code == 401
    assert stranger.post("/v1/sign-in/recover", json={"login": account_id, "code": "made-up-code", "password": "another made-up password"}).status_code == 401
    alex = house.signed_in("alex", address="198.51.100.78")
    assert alex.post("/v1/vault/members", json={"login": account_id, "role": "viewer"}).status_code == 404
    assert alex.post("/v1/vault/members", json={"login": "sam", "role": "viewer"}).status_code == 200
    # A sign-in service cannot hand a newcomer a name shaped like an id either.
    assert house.store.free_login(account_id) == f"person-{account_id}"
    assert sam.get("/v1/session").status_code == 200


def test_a_cookie_whose_name_only_looks_like_ours_is_not_ours(house):
    sam = house.signed_in("sam")
    mine = sam.cookies.get("__Host-kaal_session")
    mallory = house.signed_in("mallory", address="198.51.100.90").cookies.get("__Host-kaal_session")
    plain = house.visitor("192.0.2.9")
    both = plain.get("/v1/session", headers={"Cookie": f"__Host-kaal_session={mine}; \xa0__Host-kaal_session={mallory}".encode("latin-1")})
    assert both.json()["result"]["account"]["login"] == "sam"
    assert plain.get("/v1/session", headers={"Cookie": f"\x85__Host-kaal_session={mallory}".encode("latin-1")}).status_code == 401
    assert server.cookie_named('a=1;  __Host-kaal_session="x" ; b=2', "__Host-kaal_session") == "x"


def test_approving_an_app_needs_a_sign_in_just_made_or_the_password(house, monkeypatch):
    sam = house.signed_in("sam")
    ask = {"challenge": "A" * 43}
    assert sam.post("/v1/account/app-sign-ins", json=ask).status_code == 200           # signed in a moment ago
    monkeypatch.setattr(server, "APPROVE_WITHIN_SECONDS", -1)                           # as if signed in long ago
    stale = sam.post("/v1/account/app-sign-ins", json=ask)
    assert stale.status_code == 403 and stale.json()["kind"] == "proof_needed"
    assert sam.post("/v1/account/app-sign-ins", json={**ask, "password": "not it"}).status_code == 403
    assert sam.post("/v1/account/app-sign-ins", json={**ask, "password": GOOD}).status_code == 200


def test_tokens_cannot_be_made_and_revoked_without_end(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    house.state.proofs = server.RateLimiter(10_000)                                   # this test is about tokens, not how often a password is proved
    answers = []
    for index in range(server.TOKENS_PER_HOUR + 1):
        made = sam.post("/v1/account/tokens", json={"name": f"made-up-{index}", "scopes": ["read"], "vault": vault, "password": GOOD})
        answers.append(made.status_code)
        if made.status_code == 200:
            sam.delete(f"/v1/account/tokens/{made.json()['result']['token']['id']}")
    assert answers == [200] * server.TOKENS_PER_HOUR + [429]


# --- a sign-in service that answers slowly, or with something that is not an answer

@pytest.fixture()
def made_up_service(tmp_path):
    """A TLS server with a certificate made for the test, whose behaviour each test chooses."""
    import datetime
    import socket
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False).sign(key, hashes.SHA256()))
    (tmp_path / "made-up.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "made-up.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(tmp_path / "made-up.pem", tmp_path / "made-up.key")
    trusting = ssl.create_default_context(cafile=str(tmp_path / "made-up.pem"))
    listener = socket.create_server(("127.0.0.1", 0))
    plays = SimpleNamespace(pieces=[b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"], pause=0.0)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except OSError:
                return
            try:
                with serving.wrap_socket(raw, server_side=True) as client:
                    client.settimeout(5)
                    client.recv(65536)
                    for piece in plays.pieces:
                        client.sendall(piece)
                        if stop.wait(plays.pause):
                            break
                    # Close politely: say there is no more, then wait for the client to finish
                    # (closing with its request unread would reset the connection under it).
                    client.shutdown(socket.SHUT_WR)
                    client.settimeout(2)
                    while client.recv(65536):
                        pass
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    yield SimpleNamespace(url=f"https://localhost:{listener.getsockname()[1]}/made-up", trusting=trusting, plays=plays)
    stop.set()
    listener.close()


def test_a_sign_in_service_is_given_one_deadline_for_its_whole_answer(made_up_service, monkeypatch):
    from kaal import oidc

    assert oidc.fetch(made_up_service.url, trusting=made_up_service.trusting) == (200, b"{}")
    made_up_service.plays.pieces = [b"HTTP/1.1 400 Bad Request\r\nContent-Length: 9\r\n\r\n", b"made-up !"]
    assert oidc.fetch(made_up_service.url, {"code": "made-up"}, trusting=made_up_service.trusting) == (400, b"made-up !")

    monkeypatch.setattr(oidc, "TOTAL_SECONDS", 1.0)
    made_up_service.plays.pause = 0.4          # each piece arrives well inside the per-step timeout, for ever
    for dripped in ([b"HTTP/1.1 200 OK\r\n"] + [b"X-Made-Up: 1\r\n"] * 50,                                     # the headers
                    [b"HTTP/1.1 400 Bad Request\r\nContent-Length: 500\r\n\r\n"] + [b"x"] * 50,                  # an error's body
                    [b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n"] + [b"x"] * 50):                           # an answer's body
        made_up_service.plays.pieces = dripped
        started = time.monotonic()
        with pytest.raises(oidc.SignInServiceError) as gave_up:
            oidc.fetch(made_up_service.url, trusting=made_up_service.trusting)
        assert gave_up.value.kind == "unreachable" and time.monotonic() - started < 4


def test_an_answer_that_is_not_http_is_a_service_that_cannot_be_reached_and_is_remembered(made_up_service):
    from kaal import oidc

    made_up_service.plays.pieces = [b"made-up nonsense, not an answer\r\n\r\n"]
    asked = []

    def through(url, form=None):
        asked.append(url)
        return oidc.fetch(made_up_service.url, trusting=made_up_service.trusting)

    services = oidc.Services(through)
    for _ in range(3):
        with pytest.raises(oidc.SignInServiceError) as refused:
            services.settings("https://id.example")
        assert refused.value.kind == "unreachable"
    assert len(asked) == 1


def test_only_a_few_requests_to_sign_in_services_wait_at_once(monkeypatch):
    from kaal import oidc

    monkeypatch.setattr(oidc, "WAIT_SECONDS", 0.2)
    release = threading.Event()

    def stuck(url, form=None):
        release.wait(5)
        return 200, b"{}"

    services = oidc.Services(stuck)
    holders = [threading.Thread(target=lambda: services._fetch("https://id.example/t", {"code": "made-up"})) for _ in range(oidc.CALLS_AT_ONCE)]
    for holder in holders:
        holder.start()
    time.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(oidc.SignInServiceError):
        services._fetch("https://id.example/t", {"code": "made-up"})
    assert time.monotonic() - started < 2                                             # told to try again, not left waiting
    release.set()
    for holder in holders:
        holder.join()
    assert services._fetch("https://id.example/t") == (200, b"{}")


# --- what is kept in memory for searching

def test_search_memory_is_bounded_and_text_let_go_is_read_again(house, monkeypatch):
    monkeypatch.setenv("KAAL_SEARCH_MEMORY_MB", "1")
    sam = house.signed_in("sam")
    for index in range(4):
        made = sam.post("/v1/library/notes", json={"title": f"Made-up {index}", "body": f"made-up-word-{index} " + "x" * 400_000})
        assert made.status_code == 200, made.text
    found = sam.post("/v1/library/search", json={"query": "made-up-word-0"})
    assert found.status_code == 200 and len(found.json()["result"]) == 1
    vault = house.state.vaults.get(sam.get("/v1/session").json()["result"]["vault"]["id"])
    assert vault.text_total <= 1024 * 1024 + 400_100 and len(vault.text_cache) < 4 and vault.text_total == sum(vault.text_sizes.values())
    again = sam.post("/v1/library/search", json={"query": "made-up-word-3"})
    assert len(again.json()["result"]) == 1


def test_a_saved_from_address_keeps_no_session_and_is_a_web_address(house):
    clean = kaal.sanitized_capture_url
    assert clean("https://portal.example/receipt;jsessionid=MADEUP/print;v=2?x=1#top") == "https://portal.example/receipt/print"
    assert clean("https://user:made-up@portal.example:8443/a?token=made-up") == "https://portal.example:8443/a"
    for refused in ("data:text/html,<p>made-up</p>", "file:///made-up/receipt.pdf", "https://portal.example\\\\@evil.example/x", "javascript:void(0)", "https:///nowhere"):
        assert clean(refused) == ""


# --- the disk, and what arrives without saying how large it is

def test_something_new_must_say_how_large_it_is(house):
    sam = house.signed_in("sam")
    unsized = sam.post("/v1/library/notes", content=iter([b'{"title": "Made-up", ', b'"body": "x"}']), headers={"Content-Type": "application/json"})
    assert unsized.status_code == 411
    assert sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x"}).status_code == 200


def test_a_nearly_full_disk_refuses_something_new_and_writes_no_export(house):
    app = house.build(free_space_bytes=1 << 60)                                       # as if the disk were nearly full
    made = app.state.kaal.accounts.add_account("sam")
    server.create_vault(app.state.kaal.accounts, "My notes", made["id"])
    sam = house.visitor(application=app)
    assert sam.post("/v1/setup", json={"code": app.state.kaal.accounts.new_setup_code("sam"), "password": GOOD}).status_code == 200
    refused = sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x"})
    assert refused.status_code == 507 and "disk" in refused.json()["error"]
    big = sam.post("/v1/captures", files={"file": ("made-up.bin", b"0" * (2 * 1024 * 1024), "application/octet-stream")})
    assert big.status_code == 507 and big.json()["kind"] == "full"                    # before the file is written anywhere
    assert sam.get("/v1/vault/export").status_code == 507
    assert sam.get("/v1/library/notes").status_code == 200                            # reading still works
    assert not list((house.home / "tmp").glob("export-*"))


def test_one_person_sends_only_a_few_large_files_at_once():
    import asyncio

    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])
        await server._send_json(send, 200, {"result": "ok"})

    limited = server.BodyLimitMiddleware(inner, 100 * 1024 * 1024, credentials=lambda headers: "token:made-up", full=lambda needed: False)
    sent = []

    async def ask():
        scope = {"type": "http", "method": "POST", "path": "/v1/captures",
                 "headers": [(b"content-type", b"multipart/form-data; boundary=x"), (b"content-length", str(5 * 1024 * 1024).encode())]}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent.append(message["status"])

        await limited(scope, receive, send)

    limited._uploading["token:made-up"] = server.BodyLimitMiddleware.UPLOADS_AT_ONCE   # that many are already arriving
    asyncio.run(ask())
    assert sent == [429] and reached == []
    limited._uploading["token:made-up"] = server.BodyLimitMiddleware.UPLOADS_AT_ONCE - 1
    asyncio.run(ask())
    assert sent == [429, 200] and limited._uploading == {"token:made-up": server.BodyLimitMiddleware.UPLOADS_AT_ONCE - 1}   # counted, then given back


def test_a_copy_of_a_vault_is_written_for_one_download_at_a_time(house):
    sam = house.signed_in("sam")
    sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x"})
    caller = f"account:{house.store.account('sam')['id']}"
    held = house.state.exports.take(caller, 1)                                        # one copy is still on its way to them
    busy = sam.get("/v1/vault/export")
    assert busy.status_code == 503 and busy.json()["kind"] == "busy"
    house.state.exports.give(held)
    done = sam.get("/v1/vault/export")
    assert done.status_code == 200 and done.content[:2] == b"PK"
    assert house.state.exports._by_caller == {}                                       # given back once it had left


# --- one note, for a caller that cannot reveal

def test_a_read_only_token_sees_one_note_without_its_address_or_its_files_fingerprints(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    saved = sam.post("/v1/captures", files={"file": ("made-up.txt", b"made-up words", "text/plain")},
                     data={"source_url": "https://bank.example/accounts/998877/statement", "source_title": "Made-up page"})
    assert saved.status_code == 200, saved.text
    reader = sam.post("/v1/account/tokens", json={"name": "reader", "scopes": ["read"], "vault": vault, "password": GOOD}).json()["result"]["secret"]
    script = house.visitor(Authorization=f"Bearer {reader}")
    note_id = script.get("/v1/notes").json()["result"][0]["id"]
    shown = script.get(f"/v1/notes/{note_id}")
    assert shown.status_code == 200
    assert "998877" not in shown.text and "sha256" not in shown.text and "bank.example" in shown.text
    assert set(shown.json()["result"]["attachments"][0]) <= set(server.META_ATTACHMENT_KEYS)
    full = sam.get(f"/v1/notes/{note_id}", params={"full": "true"})
    assert "998877" in full.text and "sha256" in full.text


def test_an_address_that_cannot_be_read_does_not_break_the_list_for_read_only_callers():
    shown = server.metadata_only({"id": "n1", "title": "Made-up", "source": {"type": "browser", "url": "https://[oops/x?a=1"}})
    assert shown["source"] == {"type": "browser"}


def test_forgetting_a_provider_name_needs_the_right_to_read_them(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    writer = sam.post("/v1/account/tokens", json={"name": "writer", "scopes": ["write"], "vault": vault, "password": GOOD}).json()["result"]["secret"]
    script = house.visitor(Authorization=f"Bearer {writer}")
    for name in ("Made-up Clinic", "Another Made-up Clinic"):
        assert script.post("/v1/medical/providers/forget", json={"forget": [name]}).status_code == 403


def test_ids_only_keeps_the_ids_of_what_was_attached_or_queued():
    assert server.ids_only({"status": "attached", "note": "n1", "attachmentId": "a1", "name": "made-up.pdf"}) == {"status": "attached", "note": "n1", "attachmentId": "a1"}
    assert server.ids_only({"status": "queued", "count": 2, "ids": ["n1", "n2"], "names": ["Jane Madeup"]}) == {"status": "queued", "count": 2, "ids": ["n1", "n2"]}
    assert server.ids_only({"note": {"id": "n1", "title": "Made-up"}}) == {"note": {"id": "n1"}}


# --- a request that is not clear about its size, or says one thing and does another

def _direct(app_or_layer, method, path, headers, body_pieces=(b"",), pause=0.0, more_to_come=False):
    """Send one request straight to an ASGI app, with exactly these headers (repeats included)."""
    import asyncio

    seen = {}

    async def go():
        pieces = list(body_pieces)

        async def receive():
            if pause:
                await asyncio.sleep(pause)
            if pieces:
                piece = pieces.pop(0)
                return {"type": "http.request", "body": piece, "more_body": bool(pieces) or more_to_come}
            await asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path, "raw_path": path.encode(),
                 "query_string": b"", "scheme": "https", "server": ("testserver", 443), "client": ("203.0.113.7", 50000), "root_path": "",
                 "headers": [(name.lower().encode(), value if isinstance(value, bytes) else value.encode()) for name, value in headers]}
        await app_or_layer(scope, receive, send)

    asyncio.run(go())
    return seen.get("status")


def test_a_request_that_is_not_clear_about_its_size_or_sender_is_refused(house):
    sam = house.signed_in("sam")
    session = sam.cookies.get("__Host-kaal_session")
    mine = [("X-Kaal-Session", session), ("X-Kaal-Request", "1")]
    body = b'{"query": "made-up"}'
    assert _direct(house.app, "POST", "/v1/library/search", [*mine, ("Content-Type", "application/json"), ("Content-Length", str(len(body)))], [body]) == 200
    # A length together with chunked encoding: this layer and the one reading the body could each believe a different one.
    assert _direct(house.app, "POST", "/v1/library/search", [*mine, ("Content-Type", "application/json"), ("Content-Length", "10"), ("Transfer-Encoding", "chunked")], [body]) == 400
    # The kind of body named twice, or the sender.
    assert _direct(house.app, "POST", "/v1/library/search", [*mine, ("Content-Type", "application/json"), ("Content-Type", "multipart/form-data; boundary=x"), ("Content-Length", str(len(body)))], [body]) == 400
    assert _direct(house.app, "GET", "/v1/session", [("X-Kaal-Session", session), ("X-Kaal-Session", "ks_made_up")]) == 400
    assert _direct(house.app, "POST", "/v1/library/search", [*mine, ("Content-Type", "application/json"), ("Content-Length", "ten")], [body]) == 400


def test_only_the_upload_routes_take_a_large_body_whatever_it_says_it_carries(house):
    sam = house.signed_in("sam")
    mine = [("X-Kaal-Session", sam.cookies.get("__Host-kaal_session")), ("X-Kaal-Request", "1")]
    multipart = ("Content-Type", "multipart/form-data; boundary=made-up")
    big = str(30 * 1024 * 1024)
    for method, path in (("POST", "/v1/notes/search"), ("DELETE", "/v1/account"), ("PATCH", "/v1/medical/receipts/made-up/fields"), ("PUT", "/v1/library/notes/made-up")):
        assert _direct(house.app, method, path, [*mine, multipart, ("Content-Length", big)]) == 413, (method, path)
    # The same size is let through to an upload route (where it then fails for being empty, not for its size).
    assert _direct(house.app, "POST", "/v1/captures", [*mine, multipart, ("Content-Length", big)], [b"--made-up--\r\n"]) != 413


def test_a_body_that_stops_arriving_is_given_up_on(house, monkeypatch):
    monkeypatch.setattr(server, "BODY_IDLE_SECONDS", 0.2)
    sam = house.signed_in("sam")
    mine = [("X-Kaal-Session", sam.cookies.get("__Host-kaal_session")), ("X-Kaal-Request", "1")]
    started = time.monotonic()
    answer = _direct(house.app, "POST", "/v1/library/search", [*mine, ("Content-Type", "application/json"), ("Content-Length", "2000")], [b'{"query": '], more_to_come=True)
    # Answered, not left waiting: 408 from this layer, or 400 where the framework reports the unfinished body itself.
    assert answer in (400, 408) and time.monotonic() - started < 5


def test_a_long_list_of_wrong_fields_is_not_all_named_back(house):
    sam = house.signed_in("sam")
    many = sam.patch("/v1/medical/receipts/made-up/fields", json={"fields": {f"k{index}": 1 for index in range(5000)}})
    assert many.status_code == 400 and len(many.content) < 2000


def test_a_tokens_name_cannot_pass_for_a_sign_in_or_hide_characters(house):
    store = tokens.TokenStore(house.home / "tokens.json")
    for name in ("account:0123456789ab", "Token:made-up", "made‮up", "made\nup", "made\x1bup"):
        with pytest.raises(tokens.TokenError):
            store.create(name, ["read"])
    assert store.create("Made-up laptop (2)", ["read"])[0]["name"] == "Made-up laptop (2)"
    assert "‮" not in server.log_safe("made‮up") and "​" not in server.log_safe("made​up")


def test_cookies_sent_as_several_headers_are_read_as_one(house):
    sam = house.signed_in("sam")
    session = sam.cookies.get("__Host-kaal_session")
    assert _direct(house.app, "GET", "/v1/session", [("Cookie", "made-up=1"), ("Cookie", f"__Host-kaal_session={session}")]) == 200


def test_ordinary_requests_are_never_counted_as_large_ones_in_flight():
    """A page loads many files at once, and none of them is an upload."""
    import asyncio

    async def inner(scope, receive, send):
        await asyncio.sleep(0.05)
        await server._send_json(send, 200, {"result": "ok"})

    asked = []

    def whose(headers):
        asked.append(1)
        return "token:made-up"

    limited = server.BodyLimitMiddleware(inner, 100 * 1024 * 1024, credentials=whose, full=lambda needed: False)

    async def one(method, headers):
        seen = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                seen.append(message["status"])

        await limited({"type": "http", "method": method, "path": "/v1/session", "headers": headers}, receive, send)
        return seen[0]

    async def many():
        plain = [one("GET", [(b"cookie", b"__Host-kaal_session=made-up")]) for _ in range(12)]
        small = [one("POST", [(b"content-length", b"40"), (b"content-type", b"application/json")]) for _ in range(12)]
        return await asyncio.gather(*plain, *small)

    assert asyncio.run(many()) == [200] * 24
    assert asked == [] and limited._uploading == {}                                   # nobody was even asked who they were


# --- what a reading program says, and waiting for a turn

def test_a_programs_complaints_are_not_kept_and_what_it_prints_is_bounded(monkeypatch):
    noisy = tools.run(["/bin/sh", "-c", "yes made-up-complaint | head -c 30000000 >&2; echo made-up"], timeout=30, text=True)
    assert noisy is not None and noisy.stdout.strip() == "made-up" and noisy.stderr == ""
    monkeypatch.setenv("KAAL_FILE_OUTPUT_MB", "1")
    chatty = tools.run(["/bin/sh", "-c", "yes made-up | head -c 20000000"], timeout=30)
    # Cut off by the system, not read into memory (shells count the limit in blocks of 512 or 1024 bytes).
    assert chatty is not None and len(chatty.stdout) <= 2 * 1024 * 1024 + 4096


def test_no_turn_in_time_is_busy_not_an_empty_file(monkeypatch):
    monkeypatch.setattr(tools, "_pools", {})
    monkeypatch.setattr(tools, "PREVIEW_WAIT_SECONDS", 0.2)
    release = threading.Event()
    holders = [threading.Thread(target=lambda: tools.run(["/bin/sh", "-c", "sleep 3"], timeout=10, pool="previews")) for _ in range(2)]
    for holder in holders:
        holder.start()
    time.sleep(0.5)
    started = time.monotonic()
    with pytest.raises(tools.Busy):
        tools.run(["/bin/echo", "made-up"], timeout=5, pool="previews")
    assert time.monotonic() - started < 2
    # The other pool is not held up by it, a turn already held needs no second one, and patience waits.
    assert tools.run(["/bin/echo", "made-up"], timeout=5, text=True).stdout.strip() == "made-up"
    assert tools.run(["/bin/echo", "made-up"], timeout=5, text=True, pool=None).stdout.strip() == "made-up"
    with tools.patient(10):
        assert tools.run(["/bin/echo", "made-up"], timeout=5, text=True, pool="previews").stdout.strip() == "made-up"
    release.set()
    for holder in holders:
        holder.join()


def _note_with_file(client, name="made-up.txt", data=b"made-up words"):
    note = client.post("/v1/library/notes", json={"title": "Made-up", "body": "x"}).json()["result"]
    note_id = note.get("id") or note["note"]["id"]
    assert client.post(f"/v1/library/notes/{note_id}/attachments", files={"file": (name, data, "application/octet-stream")}).status_code == 200
    shown = client.get(f"/v1/library/notes/{note_id}").json()["result"]
    return note_id, (shown.get("note") or shown)["attachments"][0]["id"]


def test_a_picture_of_a_file_is_answered_busy_rather_than_queued(house, monkeypatch):
    sam = house.signed_in("sam")
    note_id, attachment = _note_with_file(sam)
    path = f"/v1/notes/{note_id}/attachments/{attachment}/preview"

    def no_turn(*_args, **_kwargs):
        raise tools.Busy()

    monkeypatch.setattr(kaal, "attachment_preview", no_turn)
    busy = sam.get(path)
    assert busy.status_code == 503 and busy.json()["kind"] == "busy" and busy.headers["retry-after"]
    # And one person has only a few being drawn at a time.
    caller = f"account:{house.store.account('sam')['id']}"
    held = [house.state.previews.take(caller, 0) for _ in range(3)]
    assert all(held) and sam.get(path).status_code == 503
    for ticket in held:
        house.state.previews.give(ticket)
    assert house.state.previews._by_caller == {}


def test_one_person_holds_at_most_a_quarter_of_the_room_for_downloads():
    downloads = server.Downloads(limit_bytes=1000)
    first = downloads.take("mallory", 200)
    assert first and downloads.take("mallory", 100) is None                           # 300 would be more than her quarter
    assert downloads.take("mallory", 40) is not None and downloads.take("vic", 200) is not None
    downloads.give(first)
    assert downloads.take("mallory", 100) is not None


def test_a_token_is_counted_with_the_person_who_made_it(house):
    sam = house.signed_in("sam")
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    note_id, attachment = _note_with_file(sam)
    secret = sam.post("/v1/account/tokens", json={"name": "made-up", "scopes": ["read", "reveal"], "vault": vault, "password": GOOD}).json()["result"]["secret"]
    caller = f"account:{house.store.account('sam')['id']}"
    held = [house.state.downloads.take(caller, 1) for _ in range(server.Downloads.PER_CALLER)]
    script = house.visitor(Authorization=f"Bearer {secret}")
    assert script.get(f"/v1/notes/{note_id}/attachments/{attachment}/content").status_code == 503
    for ticket in held:
        house.state.downloads.give(ticket)
    assert script.get(f"/v1/notes/{note_id}/attachments/{attachment}/content").status_code == 200


def test_a_client_that_stops_taking_its_answer_is_given_up_on(monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "SEND_IDLE_SECONDS", 0.2)
    given_back = []

    async def inner(scope, receive, send):
        scope["kaal_held"].append(lambda: given_back.append("ticket"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"0" * (1024 * 1024)})

    sizes = []

    async def stalling_send(message):
        if message["type"] == "http.response.body":
            sizes.append(len(message["body"]))
            if len(sizes) == 2:
                await asyncio.sleep(3600)                                           # the client takes no more

    async def go():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        await server.HeldDownloadsMiddleware(inner)({"type": "http", "method": "GET", "path": "/"}, receive, stalling_send)

    started = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - started < 5 and given_back == ["ticket"]
    assert sizes == [server.SEND_PIECE_BYTES] * 2                                    # handed over a piece at a time


def test_a_request_waits_only_so_long_for_a_busy_vault(house, monkeypatch):
    monkeypatch.setattr(server, "VAULT_WAIT_SECONDS", 0.2)
    sam = house.signed_in("sam")
    vault = house.state.vaults.get(sam.get("/v1/session").json()["result"]["vault"]["id"])
    with vault.request_lock:                                                          # as if a slow change were under way
        started = time.monotonic()
        busy = sam.get("/v1/library/notes")
        assert busy.status_code == 503 and time.monotonic() - started < 3
    assert sam.get("/v1/library/notes").status_code == 200


def test_a_receipt_that_never_got_its_turn_stays_waiting(house, monkeypatch):
    app = house.build(background_extraction=True)
    made = app.state.kaal.accounts.add_account("sam")
    server.create_vault(app.state.kaal.accounts, "My notes", made["id"])
    sam = house.visitor(application=app)
    assert sam.post("/v1/setup", json={"code": app.state.kaal.accounts.new_setup_code("sam"), "password": GOOD}).status_code == 200
    saved = sam.post("/v1/medical/receipts", files={"file": ("receipt.txt", b"Amount paid: $40.00\n", "text/plain")})
    assert saved.status_code == 200, saved.text
    vault = app.state.kaal.vaults.get(sam.get("/v1/session").json()["result"]["vault"]["id"])

    def no_turn(*_args, **_kwargs):
        raise tools.Busy()

    real = kaal.extract_attachment_markdown
    monkeypatch.setattr(kaal, "extract_attachment_markdown", no_turn)
    app.state.kaal.worker.drain()
    with kaal.use_vault(vault):
        assert len(kaal.pending_receipt_ids()) == 1                                   # not recorded as read with nothing in it
    monkeypatch.setattr(kaal, "extract_attachment_markdown", real)
    app.state.kaal.worker.drain()
    with kaal.use_vault(vault):
        assert kaal.pending_receipt_ids() == []


# --- everyone behind one address, and addresses that are really one visitor

def test_a_proxy_that_hides_who_is_asking_makes_everyone_shared_and_nobody_is_locked_out(house):
    behind = house.build(trusted_proxies=server.trusted_proxy_list("private"))
    assert server.client_address(request_for(behind, "172.18.0.3", "172.18.0.1")) == server.SHARED
    assert server.client_address(request_for(behind, "172.18.0.3")) == server.SHARED
    direct = house.build()
    assert server.client_address(request_for(direct, "127.0.0.1")) == server.SHARED
    # A stranger's wrong tries for other names, from behind the same proxy, are not held against a right password.
    guard = server.SignInGuard()
    for index in range(40):
        guard.failed(f"made-up-{index}", server.SHARED)
    for _ in range(10):
        guard.failed("sam", server.SHARED)
    assert guard.wait("sam", server.SHARED) == 0
    # The same from a real address is slowed, as before.
    for _ in range(6):
        guard.failed("sam", "203.0.113.9")
    assert guard.wait("sam", "203.0.113.9") > 0 and guard.wait("sam", "198.51.100.1") == 0


def test_every_line_of_the_forwarded_header_counts_and_a_port_is_not_part_of_an_address(house):
    direct = house.build()
    scope = {"type": "http", "method": "GET", "path": "/", "client": ("127.0.0.1", 1), "app": direct, "query_string": b"",
             "headers": [(b"x-forwarded-for", b"203.0.113.50"), (b"x-forwarded-for", b"198.51.100.66")]}
    assert server.client_address(server.Request(scope)) == "198.51.100.66"            # the proxy's own line, not the visitor's claim
    assert server.client_address(request_for(direct, "127.0.0.1", "198.51.100.90:51234")) == "198.51.100.90"
    assert server.client_address(request_for(direct, "127.0.0.1", "[2001:db8:1:2::9]:443")) == "2001:db8:1::/56"


def test_an_addresss_wrong_tries_are_forgotten_after_a_while(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    guard = server.SignInGuard()
    for index in range(20):
        guard.failed(f"made-up-{index}", "203.0.113.9")                               # a household's typos
        clock[0] += 3 * 3600
    guard.failed("sam", "203.0.113.9")
    assert guard.wait("sam", "203.0.113.9") == 0                                      # each was long forgotten by the next
    for index in range(21):
        guard.failed(f"made-up-{index}", "203.0.113.9")                               # the same number at once is still slowed
    assert guard.wait("alex", "203.0.113.9") > 0


def test_wrong_passwords_do_not_keep_someone_from_their_recovery_code(house):
    sam = house.signed_in("sam")
    code = sam.post("/v1/account/recovery-codes", json={"password": GOOD}).json()["result"]["recovery_codes"][0]
    stranger = house.visitor("198.51.100.77")
    for _ in range(8):
        stranger.post("/v1/sign-in", json={"login": "sam", "password": "a wrong made-up password"})
    assert stranger.post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 429
    recovered = stranger.post("/v1/sign-in/recover", json={"login": "sam", "code": code, "password": "another made-up password 7"})
    assert recovered.status_code == 200, recovered.text


def test_a_busy_server_does_not_use_up_a_setup_link_or_half_make_an_account(house, monkeypatch):
    house.store.add_account("sam")
    code = house.store.new_setup_code("sam")
    house.store.put("signup", "open")

    def busy(*_args, **_kwargs):
        raise passwords.PasswordBusy()

    visitor = house.visitor("198.51.100.5")
    with monkeypatch.context() as patched:
        patched.setattr(passwords, "hash_password", busy)
        assert visitor.post("/v1/setup", json={"code": code, "password": GOOD}).status_code == 503
        assert visitor.post("/v1/sign-up", json={"login": "newcomer", "password": GOOD}).status_code == 503
    assert house.store.account("newcomer") is None
    assert visitor.post("/v1/setup", json={"code": code, "password": GOOD}).status_code == 200      # the link was not used up


def test_one_address_makes_only_a_few_accounts_an_hour(house):
    house.store.put("signup", "open")
    here = house.visitor("198.51.100.5")
    answers = [here.post("/v1/sign-up", json={"login": f"made-up-{index}", "password": GOOD}).status_code for index in range(4)]
    assert answers == [200, 200, 200, 429]
    assert house.visitor("198.51.100.6").post("/v1/sign-up", json={"login": "made-up-elsewhere", "password": GOOD}).status_code == 200


def test_proving_ones_password_over_and_over_is_limited_even_when_right(house):
    sam = house.signed_in("sam")
    answers = [sam.post("/v1/account/recovery-codes", json={"password": GOOD}).status_code for _ in range(8)]
    assert answers[:6] == [200] * 6 and answers[6:] == [429, 429]
    assert house.visitor("198.51.100.9").post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 200   # signing in is not counted here


def test_changing_the_password_ends_an_app_sign_in_not_yet_collected(house):
    sam = house.signed_in("sam")
    code = sam.post("/v1/account/app-sign-ins", json={"challenge": "A" * 43}).json()["result"]["code"]
    assert sam.post("/v1/account/password", json={"current": GOOD, "new": "another made-up password 7"}).status_code == 200
    assert house.visitor("198.51.100.9").post("/v1/app-sign-ins", json={"code": code, "verifier": "made-up-verifier-" + "x" * 30, "device": "made-up"}).status_code == 401


# --- what the last critics found

def test_what_goes_into_a_vaults_index_has_a_length(house):
    sam = house.signed_in("sam")
    long = "x" * 5000
    assert sam.post("/v1/notes", json={"title": "Made-up", "body": "x", "tags": [long]}).status_code == 400
    assert sam.post("/v1/library/notes", json={"title": "Made-up", "body": "x", "tags": ["made-up", long]}).status_code == 400
    assert sam.post("/v1/medical/people", json={"add": [long]}).status_code == 400
    assert sam.patch("/v1/medical/receipts/made-up/fields", json={"fields": {"provider": long}}).status_code == 400
    assert sam.post("/v1/captures", files={"file": ("made-up.txt", b"made-up", "text/plain")}, data={"source_title": "x" * 10_000}).status_code == 400
    assert sam.post("/v1/notes", json={"title": "Made-up", "body": "x", "tags": ["made-up"]}).status_code == 200
    with pytest.raises(kaal.KaalError):
        kaal.parse_tags("made-up, " + long)


def test_someone_from_a_new_place_always_has_a_first_try_whatever_others_have_done():
    guard = server.SignInGuard()
    for index in range(80):
        guard.failed("sam", f"198.51.100.{index}")                                     # a crowd of addresses, all guessing one name
    assert guard.wait("sam", "198.51.100.3") > 0                                      # each of them is slowed
    assert guard.wait("sam", "203.0.113.200") == 0                                    # the person, from their own address, is not
    guard.failed("sam", "203.0.113.200")
    assert guard.wait("sam", "203.0.113.200") > 0                                     # until they too get it wrong


def test_wrong_tries_from_elsewhere_do_not_stop_someone_signed_in_from_proving_who_they_are(house):
    sam = house.signed_in("sam")
    for index in range(60):
        house.state.guard.failed("sam", f"198.51.100.{index}")
    assert sam.post("/v1/account/recovery-codes", json={"password": GOOD}).status_code == 200
    # Nothing typed is not a wrong try; a wrong one counts against the session, not the address.
    vault = sam.get("/v1/session").json()["result"]["vault"]["id"]
    for _ in range(3):
        assert sam.post("/v1/account/tokens", json={"name": "made-up", "scopes": ["read"], "vault": vault}).status_code == 403
    assert sam.post("/v1/account/recovery-codes", json={"password": "not it"}).status_code == 403
    assert house.state.guard.wait("alex", "203.0.113.7") == 0 and not any(key[0] == "from" and key[1] == "203.0.113.7" for key in house.state.guard._seen)


def test_a_sign_in_service_offered_again_at_another_address_keeps_nobodys_connection(house):
    sam = house.store.add_account("sam")
    house.store.put_service("code", "Made-up Code", "https://code.example", "made-up-client", "made-up sealed secret")
    house.store.connect_identity(sam["id"], "code", "4821")
    house.store.remove_service("code")
    assert house.store.identities_of(sam["id"])                                       # kept while it is away
    house.store.put_service("code", "Made-up Code", "https://code.example", "made-up-client", "made-up sealed secret")
    assert house.store.identities_of(sam["id"])                                       # and when it returns at the same address
    house.store.remove_service("code")
    house.store.put_service("code", "Made-up Code", "https://mallory.example", "made-up-client", "made-up sealed secret")
    assert house.store.identities_of(sam["id"]) == [] and house.store.identity_account("code", "4821") is None


def test_devices_on_a_private_ipv6_network_are_each_a_visitor(house):
    direct = house.build()
    one = server.client_address(request_for(direct, "127.0.0.1", "fd7a:115c:a1e0::9a01:1234"))
    other = server.client_address(request_for(direct, "127.0.0.1", "fd7a:115c:a1e0::5b02:9f10"))
    assert one != other and "/" not in one


def test_a_request_that_only_declares_a_large_body_does_not_make_the_server_collect(monkeypatch):
    import asyncio

    collected = []
    monkeypatch.setattr(server.gc, "collect", lambda: collected.append(1))

    async def inner(scope, receive, send):
        await server._send_json(send, 404, {"error": "made-up"})

    limited = server.BodyLimitMiddleware(inner, 100 * 1024 * 1024, credentials=lambda headers: "token:made-up", full=lambda needed: False)

    async def ask():
        async def receive():
            await asyncio.sleep(3600)

        async def send(message):
            pass

        await limited({"type": "http", "method": "POST", "path": "/v1/nowhere", "headers": [(b"content-length", b"2000000")]}, receive, send)

    for _ in range(20):
        asyncio.run(ask())
    time.sleep(0.2)
    assert collected == []
