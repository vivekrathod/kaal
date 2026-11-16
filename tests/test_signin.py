"""Signing in as a person: setup links, passwords, sessions, recovery codes and one's own account."""
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import accounts, passwords, server, tokens

GOOD = "correct horse battery"
APP = {"X-Kaal-Request": "1"}


@pytest.fixture()
def house(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", rate_per_minute=100_000)
    token = tokens.TokenStore(settings.tokens_file).create("old-token", ["read", "reveal", "write", "delete", "admin"])[1]
    app = server.create_app(settings)
    state = app.state.kaal

    def browser():
        """A separate browser: its own cookie jar. Over https, as the session cookie is Secure."""
        return TestClient(app, base_url="https://testserver")

    def set_up(login, password=GOOD, client=None):
        """Give an account a password through a setup link; returns (browser, answer)."""
        client = client or browser()
        response = client.post("/v1/setup", json={"code": state.accounts.new_setup_code(login), "password": password})
        assert response.status_code == 200, response.text
        return client, response.json()["result"]

    yield SimpleNamespace(app=app, state=state, store=state.accounts, browser=browser, set_up=set_up, token=token, settings=settings, home=tmp_path)
    kaal.configure_vault(saved)


# --- setup links

def test_a_setup_link_lets_its_holder_choose_a_password_once(house):
    code = house.store.new_setup_code("owner")
    client = house.browser()
    assert client.post("/v1/setup/check", json={"code": code}).json()["result"] == {"login": "owner", "name": "Owner"}
    assert client.post("/v1/setup/check", json={"code": "not-a-code"}).status_code == 404

    # A poor password is refused and the link is still good afterwards.
    for poor in ("short", "owner", "x" * 300):
        refused = client.post("/v1/setup", json={"code": code, "password": poor})
        assert refused.status_code == 400, poor
    assert client.post("/v1/setup/check", json={"code": code}).status_code == 200

    done = client.post("/v1/setup", json={"code": code, "password": GOOD, "name": "  Jane Doe "})
    assert done.status_code == 200
    told = done.json()["result"]
    assert told["account"] == {"login": "owner", "name": "Jane Doe", "admin": True}
    assert told["vault"]["name"] == "Main" and told["vault"]["role"] == "owner"
    assert told["scopes"] == ["read", "reveal", "write", "delete", "admin"]
    assert len(told["recovery_codes"]) == 8 and len(set(told["recovery_codes"])) == 8
    assert GOOD not in done.text

    # The browser is signed in, and the link is spent.
    assert client.get("/v1/session").json()["result"]["account"]["login"] == "owner"
    assert client.post("/v1/setup/check", json={"code": code}).status_code == 404
    assert house.browser().post("/v1/setup", json={"code": code, "password": "another good password"}).status_code == 404


def test_a_new_link_cancels_the_old_one_and_links_run_out(house):
    first = house.store.new_setup_code("owner")
    second = house.store.new_setup_code("owner")
    client = house.browser()
    assert client.post("/v1/setup/check", json={"code": first}).status_code == 404
    assert client.post("/v1/setup/check", json={"code": second}).status_code == 200
    stale = house.store.new_setup_code("owner", hours=-1)
    assert client.post("/v1/setup", json={"code": stale, "password": GOOD}).status_code == 404
    house.store.add_account("sam")
    house.store.set_disabled("sam", True)
    assert client.post("/v1/setup/check", json={"code": house.store.new_setup_code("sam")}).status_code == 404
    with pytest.raises(accounts.AccountError):
        house.store.new_setup_code("nobody")


# --- passwords and sessions

def test_signing_in_with_a_password_starts_a_session_the_server_can_end(house):
    house.set_up("owner")
    client = house.browser()
    for login, password in (("owner", "wrong password here"), ("nobody", GOOD), ("owner", "")):
        refused = client.post("/v1/sign-in", json={"login": login, "password": password})
        assert refused.status_code in (400, 401)
        if refused.status_code == 401:
            assert refused.json()["error"] == "That sign-in name and password do not match."

    response = client.post("/v1/sign-in", json={"login": "  OWNER ", "password": GOOD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert value.startswith("ks_") and GOOD not in cookie
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=2592000"):
        assert attribute in cookie
    assert value not in response.text and GOOD not in response.text

    # The session can use the vault; a change needs the header only the web app sends.
    assert client.get("/v1/library/notes").status_code == 200
    assert client.post("/v1/library/notes", json={"title": "T", "body": "b"}).status_code == 403
    assert client.post("/v1/library/notes", json={"title": "T", "body": "b"}, headers=APP).status_code == 200

    # Signing out ends it on the server: the same cookie value is dead even if replayed.
    assert client.delete("/v1/session", headers=APP).status_code == 200
    replay = house.browser()
    assert replay.get("/v1/session", headers={"Cookie": f"kaal_session={value}"}).status_code == 401


def test_only_a_hash_of_the_password_and_of_the_session_is_stored(house):
    client, _ = house.set_up("owner")
    value = client.cookies.get("kaal_session")
    stored = (house.home / "accounts.db").read_bytes()
    for name in ("accounts.db-wal",):
        if (house.home / name).exists():
            stored += (house.home / name).read_bytes()
    assert GOOD.encode() not in stored
    assert value.split("_", 2)[2].encode() not in stored
    assert house.store.password_hash(house.store.account("owner")["id"]).startswith("argon2id$")


def test_wrong_passwords_are_slowed_down_for_that_sign_in_name_only(house, monkeypatch):
    house.set_up("owner")
    house.store.add_account("sam")
    house.set_up("sam")
    client = house.browser()
    for _ in range(5):
        assert client.post("/v1/sign-in", json={"login": "owner", "password": "wrong password"}).status_code == 401
    # Five wrong tries are free; the sixth starts the waiting.
    assert client.post("/v1/sign-in", json={"login": "owner", "password": "wrong password"}).status_code == 401
    locked = client.post("/v1/sign-in", json={"login": "Owner", "password": GOOD})
    assert locked.status_code == 429 and 0 < int(locked.headers["retry-after"]) <= 60
    assert client.post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 200

    # When the wait is over the right password works and the count starts again.
    clock = server.time.monotonic()
    monkeypatch.setattr(server.time, "monotonic", lambda: clock + 61)
    assert client.post("/v1/sign-in", json={"login": "owner", "password": GOOD}).status_code == 200
    assert house.state.guard.wait("owner") == 0


def test_the_guard_waits_longer_each_time_up_to_a_limit(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    guard = server.SignInGuard()
    waits = []
    for _ in range(12):
        guard.failed("Jane")
        waits.append(guard.wait("jane"))
        now[0] += waits[-1]
    assert waits == [0, 0, 0, 0, 0, 60, 120, 240, 480, 900, 900, 900]
    guard.succeeded("JANE")
    assert guard.wait("jane") == 0
    for index in range(guard.REMEMBERED + 50):
        guard.failed(f"name{index}")
    assert len(guard._seen) <= guard.REMEMBERED


def test_a_disabled_account_cannot_sign_in_and_its_browsers_stop_working(house):
    house.store.add_account("sam")
    server.create_vault(house.store, "Sam's notes", "sam")
    client, _ = house.set_up("sam")
    assert client.get("/v1/library/notes").status_code == 200
    house.store.set_disabled("sam", True)
    assert client.get("/v1/library/notes").status_code == 401
    assert house.browser().post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 401
    house.store.set_disabled("sam", False)
    assert client.get("/v1/library/notes").status_code == 200


# --- one's own account

def test_changing_the_password_needs_the_old_one_and_signs_other_browsers_out(house):
    here, _ = house.set_up("owner")
    there = house.browser()
    assert there.post("/v1/sign-in", json={"login": "owner", "password": GOOD}).status_code == 200
    shown = here.get("/v1/account").json()["result"]
    assert shown["has_password"] and shown["recovery_codes_left"] == 8
    assert [entry["current"] for entry in shown["sessions"]] == [True, False]
    assert all(set(entry) == {"id", "label", "created", "last_seen", "current"} for entry in shown["sessions"])

    assert here.post("/v1/account/password", json={"current": "not it at all", "new": "a brand new password"}, headers=APP).status_code == 403
    assert here.post("/v1/account/password", json={"current": GOOD, "new": "short"}, headers=APP).status_code == 400
    assert here.post("/v1/account/password", json={"current": GOOD, "new": "a brand new password"}).status_code == 403, "needs the web app's header"
    changed = here.post("/v1/account/password", json={"current": GOOD, "new": "a brand new password"}, headers=APP)
    assert changed.json()["result"] == {"status": "changed", "signed_out_elsewhere": 1}

    assert here.get("/v1/session").status_code == 200
    assert there.get("/v1/session").status_code == 401
    fresh = house.browser()
    assert fresh.post("/v1/sign-in", json={"login": "owner", "password": GOOD}).status_code == 401
    assert fresh.post("/v1/sign-in", json={"login": "owner", "password": "a brand new password"}).status_code == 200


def test_a_person_can_sign_their_other_browsers_out_but_nobody_elses(house):
    house.store.add_account("sam")
    owner_here, _ = house.set_up("owner")
    owner_there = house.browser()
    owner_there.post("/v1/sign-in", json={"login": "owner", "password": GOOD})
    owner_third = house.browser()
    owner_third.post("/v1/sign-in", json={"login": "owner", "password": GOOD})
    sam, _ = house.set_up("sam")
    sam_session = sam.get("/v1/account").json()["result"]["sessions"][0]["id"]

    assert owner_here.delete(f"/v1/account/sessions/{sam_session}", headers=APP).status_code == 404
    assert sam.get("/v1/session").status_code == 200

    other = next(entry["id"] for entry in owner_here.get("/v1/account").json()["result"]["sessions"] if not entry["current"])
    assert owner_here.delete(f"/v1/account/sessions/{other}", headers=APP).status_code == 200
    assert owner_here.delete(f"/v1/account/sessions/{other}", headers=APP).status_code == 404
    assert owner_there.get("/v1/session").status_code == 401 and owner_third.get("/v1/session").status_code == 200
    assert owner_here.post("/v1/account/sessions/sign-out-others", headers=APP).json()["result"]["count"] == 1
    assert owner_third.get("/v1/session").status_code == 401 and owner_here.get("/v1/session").status_code == 200


def test_a_recovery_code_sets_a_new_password_once(house):
    old_browser, told = house.set_up("owner")
    code = told["recovery_codes"][0]
    client = house.browser()
    assert client.post("/v1/sign-in/recover", json={"login": "owner", "code": "0000-0000-0000", "password": "the next password"}).status_code == 401
    assert client.post("/v1/sign-in/recover", json={"login": "owner", "code": code, "password": "short"}).status_code == 400
    typed = code.upper().replace("-", " ")
    done = client.post("/v1/sign-in/recover", json={"login": "Owner", "code": typed, "password": "the next password"})
    assert done.status_code == 200 and done.json()["result"]["account"]["login"] == "owner"
    assert old_browser.get("/v1/session").status_code == 401, "recovering signs every other browser out"
    assert client.get("/v1/account").json()["result"]["recovery_codes_left"] == 7
    assert house.browser().post("/v1/sign-in/recover", json={"login": "owner", "code": code, "password": "yet another password"}).status_code == 401
    assert house.browser().post("/v1/sign-in", json={"login": "owner", "password": "the next password"}).status_code == 200

    # New codes need the password and replace the old ones.
    assert client.post("/v1/account/recovery-codes", json={"password": "wrong wrong wrong"}, headers=APP).status_code == 403
    fresh = client.post("/v1/account/recovery-codes", json={"password": "the next password"}, headers=APP).json()["result"]["recovery_codes"]
    assert len(fresh) == 8 and not set(fresh) & set(told["recovery_codes"])
    assert house.browser().post("/v1/sign-in/recover", json={"login": "owner", "code": told["recovery_codes"][1], "password": "yet another password"}).status_code == 401


def test_a_token_can_never_manage_the_account_it_acts_for(house):
    """Otherwise any script's token could set its owner's password and take everything."""
    bearer = {"Authorization": f"Bearer {house.token}"}
    cookie = {"Cookie": f"kaal_session={house.token}", **APP}
    for headers in (bearer, cookie):
        assert house.browser().get("/v1/account", headers=headers).status_code == 403
        for path, body in (("/v1/account/password", {"current": "x", "new": "a brand new password"}), ("/v1/account/recovery-codes", {"password": "x"}),
                           ("/v1/account/sessions/sign-out-others", None), ("/v1/session/vault", {"vault": "x"})):
            assert house.browser().post(path, json=body, headers=headers).status_code == 403, path
        assert house.browser().delete("/v1/account/sessions/abc", headers=headers).status_code == 403
    assert house.browser().get("/v1/account").status_code == 401
    # The token still does what it always did, and its session answer has no account in it.
    assert house.browser().get("/v1/session", headers=bearer).json()["result"] == {"name": "old-token", "scopes": ["read", "reveal", "write", "delete", "admin"]}


# --- which vault a person is on

def test_a_person_moves_between_their_vaults_and_their_role_decides_what_they_may_do(house):
    house.store.add_account("sam")
    own = server.create_vault(house.store, "Sam's notes", "sam")
    main = house.store.defaults()[1]
    house.store.grant("sam", main, "viewer")
    owner, _ = house.set_up("owner")
    assert owner.post("/v1/library/notes", json={"title": "Household page", "body": "b"}, headers=APP).status_code == 200
    sam, told = house.set_up("sam")

    assert told["vault"] == {"id": own["id"], "name": "Sam's notes", "role": "owner"}
    assert [(v["name"], v["role"]) for v in told["vaults"]] == [("Sam's notes", "owner"), ("Main", "viewer")]
    assert sam.get("/v1/library/notes").json()["result"] == []

    moved = sam.post("/v1/session/vault", json={"vault": main}, headers=APP).json()["result"]
    assert moved["vault"]["name"] == "Main" and moved["scopes"] == ["read", "reveal"]
    assert [n["title"] for n in sam.get("/v1/library/notes").json()["result"]] == ["Household page"]
    refused = sam.post("/v1/library/notes", json={"title": "No", "body": "no"}, headers=APP)
    assert refused.status_code == 403 and "role" in refused.json()["error"]
    assert sam.post("/v1/session/vault", json={"vault": "nope"}, headers=APP).status_code == 404

    # Taken out of that vault, the session falls back to one that is still theirs.
    house.store.ungrant("sam", main)
    assert sam.get("/v1/session").json()["result"]["vault"]["name"] == "Sam's notes"
    assert sam.get("/v1/library/notes").json()["result"] == []


def test_a_person_with_no_vault_can_sign_in_and_manage_their_account_only(house):
    house.store.add_account("visitor")
    client, told = house.set_up("visitor")
    assert told["vault"] is None and told["vaults"] == [] and told["scopes"] == []
    assert client.get("/v1/account").status_code == 200
    refused = client.get("/v1/library/notes")
    assert refused.status_code == 403 and "do not belong to a vault" in refused.json()["error"]


def test_the_log_names_the_account_by_id_never_by_name_or_password(house, caplog):
    caplog.set_level(logging.INFO, logger="kaal.server")
    house.store.add_account("jane.doe@example.com")
    client, _ = house.set_up("jane.doe@example.com")
    client.post("/v1/sign-in", json={"login": "jane.doe@example.com", "password": GOOD})
    client.get("/v1/account")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"account:{house.store.account('jane.doe@example.com')['id']}" in logged
    assert "jane.doe" not in logged and GOOD not in logged


# --- on the server's command line

def cli(monkeypatch, capsys, home, *argv):
    monkeypatch.setenv("KAAL_HOME", str(home))
    monkeypatch.setenv("KAAL_VAULT", str(home / "data"))
    monkeypatch.setenv("KAAL_TOKENS_FILE", str(home / "tokens.json"))
    args = server.build_parser().parse_args(list(argv))
    args.func(args)
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_command_line_prints_a_setup_link_and_can_set_a_password(house, monkeypatch, capsys):
    monkeypatch.setenv("KAAL_PUBLIC_URL", "https://kaal.example/")
    out, err = cli(monkeypatch, capsys, house.home, "account", "setup-link", "owner", "--hours", "2")
    link = out.strip()
    assert link.startswith("https://kaal.example/#/setup/") and "2 hours" in err
    assert house.browser().post("/v1/setup/check", json={"code": link.rsplit("/", 1)[1]}).status_code == 200
    monkeypatch.delenv("KAAL_PUBLIC_URL")
    out, _ = cli(monkeypatch, capsys, house.home, "account", "setup-link", "owner")
    assert out.startswith("https://YOUR-KAAL-ADDRESS/#/setup/")

    typed = iter(["typed at the terminal", "typed at the terminal"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(typed))
    out, _ = cli(monkeypatch, capsys, house.home, "account", "password", "owner")
    assert json.loads(out)["status"] == "password set" and "typed at the terminal" not in out
    assert house.browser().post("/v1/sign-in", json={"login": "owner", "password": "typed at the terminal"}).status_code == 200
    out, _ = cli(monkeypatch, capsys, house.home, "account", "rename", "owner", "Jane")
    assert json.loads(out)["account"]["login"] == "jane"
    assert house.browser().post("/v1/sign-in", json={"login": "jane", "password": "typed at the terminal"}).status_code == 200
    assert house.browser().post("/v1/sign-in", json={"login": "owner", "password": "typed at the terminal"}).status_code == 401
    house.store.add_account("sam")
    with pytest.raises(SystemExit):
        cli(monkeypatch, capsys, house.home, "account", "rename", "sam", "JANE")
    cli(monkeypatch, capsys, house.home, "account", "rename", "jane", "owner")
    for answers in (["one password here", "a different one"], ["short", "short"]):
        typed = iter(answers)
        with pytest.raises(SystemExit):
            cli(monkeypatch, capsys, house.home, "account", "password", "owner")


# --- hashing

def test_password_hashes_verify_only_the_right_password_and_carry_their_cost():
    stored = passwords.hash_password(GOOD)
    assert stored.startswith("argon2id$m=19456,t=2,p=1$") and GOOD not in stored
    assert stored != passwords.hash_password(GOOD), "each hash has its own salt"
    assert passwords.verify_password(stored, GOOD)
    for wrong in ("", GOOD + " ", GOOD.upper()):
        assert not passwords.verify_password(stored, wrong)
    assert not passwords.verify_password(None, GOOD) and not passwords.verify_password("nonsense", GOOD)
    assert not passwords.needs_rehash(stored)
    assert passwords.needs_rehash(stored.replace("m=19456", "m=8192"))
    weaker = "argon2id$m=8192,t=1,p=1$" + stored.split("$", 2)[2]
    assert not passwords.verify_password(weaker, GOOD), "a hash only verifies with the cost it was made with"
