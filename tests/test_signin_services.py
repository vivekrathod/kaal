"""Signing in through another service (Google, or any OpenID Connect provider)."""
import json
import logging
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import accounts, oidc, server, tokens
from signin_service import FakeService, b64

GOOD = "correct horse battery"
APP = {"X-Kaal-Request": "1"}
HERE = "https://testserver"
CALLBACK = "/v1/sign-in/with/google/callback"


@pytest.fixture()
def house(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp",
                               rate_per_minute=100_000, signups_per_hour=2)
    old_token = tokens.TokenStore(settings.tokens_file).create("old-token", ["read", "reveal", "write", "delete", "admin"])[1]
    app = server.create_app(settings)
    state = app.state.kaal
    google = FakeService()
    state.services = oidc.Services(google.fetch)

    def browser():
        return TestClient(app, base_url=HERE, headers=APP, follow_redirects=False)

    def signed_in(login):
        client = browser()
        done = client.post("/v1/setup", json={"code": state.accounts.new_setup_code(login), "password": GOOD})
        assert done.status_code == 200, done.text
        return client

    def offer_google(admin=None):
        """The administrator adds Google in the web app; returns their browser."""
        admin = admin or signed_in("owner")
        added = admin.post("/v1/admin/sign-in-services", json={"kind": "google", "client_id": google.client_id, "client_secret": google.client_secret, "address": HERE})
        assert added.status_code == 200, added.text
        return admin

    def through_google(client, sub, *, setup_code="", **who):
        """Press "Continue with Google" in this browser and sign in there as ``sub``; returns where Kaal sends the browser."""
        started = client.post("/v1/sign-in/with/google", json={"setup_code": setup_code})
        assert started.status_code == 200, started.text
        answered = client.get(google.sign_in(started.json()["result"]["url"], sub, **who))
        assert answered.status_code == 303, answered.text
        return answered.headers["location"]

    yield SimpleNamespace(app=app, state=state, store=state.accounts, google=google, browser=browser, signed_in=signed_in, offer_google=offer_google,
                          through_google=through_google, old_token=old_token, home=tmp_path, settings=settings)
    kaal.configure_vault(saved)


def connect(house, client, sub, password=GOOD, **who):
    started = client.post("/v1/account/connections/google", json={"password": password})
    assert started.status_code == 200, started.text
    return client.get(house.google.sign_in(started.json()["result"]["url"], sub, **who)).headers["location"]


# --- an administrator offers a service

def test_an_administrator_adds_google_and_the_secret_is_never_shown_or_stored_readable(house):
    assert house.browser().get("/v1/sign-in/options").json()["result"]["services"] == []
    admin = house.signed_in("owner")
    assert admin.get("/v1/admin/sign-in-services").json()["result"] == {"address": "", "services": []}
    added = admin.post("/v1/admin/sign-in-services", json={"kind": "google", "client_id": house.google.client_id, "client_secret": house.google.client_secret, "address": HERE + "/"})
    assert added.status_code == 200
    assert added.json()["result"] == {"address": HERE, "services": [{"id": "google", "name": "Google", "issuer": "https://accounts.google.com",
                                                                    "client_id": house.google.client_id, "callback": HERE + CALLBACK}]}
    assert house.google.client_secret not in added.text and house.google.client_secret not in admin.get("/v1/admin/sign-in-services").text
    stored = b"".join(path.read_bytes() for path in house.home.glob("accounts.db*"))
    assert house.google.client_secret.encode() not in stored
    assert oidc.open_secret(kaal.installation_key(), house.store.service("google")["secret"]) == house.google.client_secret
    assert house.browser().get("/v1/sign-in/options").json()["result"]["services"] == [{"id": "google", "name": "Google"}]

    # Saving again replaces the client and keeps the one entry.
    again = admin.post("/v1/admin/sign-in-services", json={"kind": "google", "client_id": "another-client", "client_secret": "another secret", "address": HERE})
    assert [entry["client_id"] for entry in again.json()["result"]["services"]] == ["another-client"]


def test_only_an_administrator_signed_in_as_themselves_may_manage_services(house):
    house.offer_google()
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    token = {"Authorization": f"Bearer {house.old_token}"}
    new = {"kind": "google", "client_id": "x", "client_secret": "y", "address": HERE}
    for method, path, body in (("GET", "/v1/admin/sign-in-services", None), ("POST", "/v1/admin/sign-in-services", new), ("DELETE", "/v1/admin/sign-in-services/google", None)):
        assert sam.request(method, path, json=body).status_code == 403, path
        assert house.browser().request(method, path, json=body, headers=token).status_code == 403, path
        assert house.browser().request(method, path, json=body).status_code == 401, path
    assert house.store.service("google")["client_id"] == house.google.client_id


def test_a_service_that_does_not_answer_or_a_poor_address_is_refused(house):
    admin = house.signed_in("owner")
    fields = {"client_id": "x", "client_secret": "y", "address": HERE}
    for poor in ({"kind": "other", "name": "Work", "issuer": "http://id.example"}, {"kind": "other", "name": "Work", "issuer": "https://nowhere.example"},
                 {"kind": "other", "name": "Google", "issuer": "https://accounts.google.com"}, {"kind": "other", "name": "123", "issuer": "https://accounts.google.com"},
                 {"kind": "google", "address": "http://kaal.example"}, {"kind": "google", "address": "https://kaal.example/path"}, {"kind": "google", "address": "javascript:alert(1)"}):
        refused = admin.post("/v1/admin/sign-in-services", json={**fields, **poor})
        assert refused.status_code == 400, poor
    assert house.store.services() == [] and house.store.get("public_address") is None
    # A local address may be plain http, for trying Kaal out on one's own computer.
    assert admin.post("/v1/admin/sign-in-services", json={**fields, "kind": "google", "address": "http://localhost:8765"}).status_code == 200


def test_another_openid_connect_service_gets_an_id_from_its_name(house):
    work = FakeService(issuer="https://id.work.example", client_id="work-client")
    house.state.services = oidc.Services(work.fetch)
    admin = house.signed_in("owner")
    added = admin.post("/v1/admin/sign-in-services", json={"kind": "other", "name": "Work Login", "issuer": "https://id.work.example/", "client_id": "work-client",
                                                         "client_secret": work.client_secret, "address": HERE})
    assert added.json()["result"]["services"] == [{"id": "work-login", "name": "Work Login", "issuer": "https://id.work.example", "client_id": "work-client",
                                                   "callback": f"{HERE}/v1/sign-in/with/work-login/callback"}]
    started = house.browser().post("/v1/sign-in/with/work-login", json={})
    assert started.json()["result"]["url"].startswith("https://id.work.example/authorize?")


def test_nothing_is_offered_until_kaal_knows_its_own_address(house):
    house.store.put_service("google", "Google", "https://accounts.google.com", "x", oidc.seal_secret(kaal.installation_key(), "y"))
    assert house.browser().get("/v1/sign-in/options").json()["result"]["services"] == []
    assert house.browser().post("/v1/sign-in/with/google", json={}).status_code == 404
    assert house.browser().post("/v1/sign-in/with/nothing", json={}).status_code == 404


# --- connecting, then signing in

def test_a_person_connects_google_and_then_signs_in_with_it(house):
    admin = house.offer_google()
    shown = admin.get("/v1/account").json()["result"]
    assert shown["connections"] == [{"service": "google", "name": "Google", "connected": False, "email": ""}]

    # Someone with a password proves it before connecting anything.
    assert admin.post("/v1/account/connections/google", json={"password": "not my password"}).status_code == 403
    assert admin.post("/v1/account/connections/google", json={}).status_code == 403
    assert connect(house, admin, "g-owner", email="jane@example.com", name="Jane Doe") == "/#/settings?connected=google"
    shown = admin.get("/v1/account").json()["result"]
    assert shown["connections"] == [{"service": "google", "name": "Google", "connected": True, "email": "jane@example.com"}]
    assert admin.get("/v1/admin/people").json()["result"]["people"][0]["services"] == ["google"]

    # A fresh browser signs in with Google and is the same account, on its vault.
    fresh = house.browser()
    assert house.through_google(fresh, "g-owner", email="jane@example.com") == "/#/receipts"
    told = fresh.get("/v1/session").json()["result"]
    assert told["account"]["login"] == "owner" and told["vault"]["name"] == "Main" and told["scopes"] == ["read", "reveal", "write", "delete", "admin"]
    assert fresh.get("/v1/library/notes").status_code == 200
    assert len(fresh.get("/v1/account").json()["result"]["sessions"]) == 2

    # Disconnecting works while there is a password to fall back on, and Google then signs nobody in.
    assert fresh.delete("/v1/account/connections/google").json()["result"] == {"status": "disconnected", "service": "google"}
    assert fresh.delete("/v1/account/connections/google").status_code == 404
    assert house.through_google(house.browser(), "g-owner") == "/#/sign-in-problem/no-account"


def test_one_google_account_cannot_be_connected_to_two_accounts_here(house):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    assert connect(house, sam, "g-owner") == "/#/settings?problem=in-use"
    assert sam.get("/v1/account").json()["result"]["connections"][0]["connected"] is False
    assert house.store.identity_account("google", "g-owner")["login"] == "owner"
    # Connecting a second time replaces one's own connection instead of adding another.
    assert connect(house, sam, "g-sam-1") == "/#/settings?connected=google"
    assert connect(house, sam, "g-sam-2", email="sam@example.com") == "/#/settings?connected=google"
    assert [(entry["subject"], entry["email"]) for entry in house.store.identities_of(house.store.account("sam")["id"])] == [("g-sam-2", "sam@example.com")]
    assert house.through_google(house.browser(), "g-sam-1") == "/#/sign-in-problem/no-account"


def test_an_unverified_email_address_is_not_kept(house):
    admin = house.offer_google()
    connect(house, admin, "g-owner", email="someone.else@example.com", verified=False)
    assert admin.get("/v1/account").json()["result"]["connections"][0] == {"service": "google", "name": "Google", "connected": True, "email": ""}


def test_a_stranger_at_google_gets_no_account_where_sign_up_is_by_invitation(house):
    house.offer_google()
    before = len(house.store.accounts())
    stranger = house.browser()
    assert house.through_google(stranger, "g-stranger", email="owner@example.com", name="Owner") == "/#/sign-in-problem/no-account"
    assert stranger.get("/v1/session").status_code == 401 and len(house.store.accounts()) == before
    # Having the same email address as an account's sign-in name connects nothing.
    house.store.add_account("jane@example.com")
    assert house.through_google(house.browser(), "g-other", email="jane@example.com") == "/#/sign-in-problem/no-account"


def test_where_sign_up_is_open_google_creates_an_account_with_its_own_vault(house):
    admin = house.offer_google()
    assert admin.post("/v1/admin/sign-up", json={"signup": "open"}).status_code == 200
    first = house.browser()
    assert house.through_google(first, "g-1", email="Jane.Doe@example.com", name="Jane Doe") == "/#/receipts"
    told = first.get("/v1/session").json()["result"]
    assert told["account"] == {"login": "jane.doe", "name": "Jane Doe", "admin": False}
    assert told["vault"]["name"] == "My notes" and told["vault"]["role"] == "owner"
    shown = first.get("/v1/account").json()["result"]
    assert shown["has_password"] is False and shown["connections"][0]["connected"] is True
    # Their vault is theirs alone.
    assert first.post("/v1/library/notes", json={"title": "Made-up private note", "body": "x"}).status_code == 200
    assert "Made-up private note" not in admin.get("/v1/library/notes").text
    # Coming back signs in as the same account; someone else with the same name in their address gets another.
    assert house.through_google(house.browser(), "g-1", email="Jane.Doe@example.com") == "/#/receipts"
    second = house.browser()
    assert house.through_google(second, "g-2", email="jane.doe@elsewhere.example") == "/#/receipts"
    assert second.get("/v1/session").json()["result"]["account"]["login"] == "jane.doe2"
    assert len(house.store.accounts()) == 3
    # No more than the hourly cap, and nothing is half-made when it is reached.
    assert house.through_google(house.browser(), "g-3", email="third@example.com") == "/#/sign-in-problem/busy"
    assert len(house.store.accounts()) == 3 and len(house.store.vaults()) == 3
    # Their only way in cannot be removed.
    assert first.delete("/v1/account/connections/google").status_code == 409
    assert house.through_google(house.browser(), "g-1") == "/#/receipts"


# --- a setup link finished with Google instead of a password

def test_a_setup_link_can_be_finished_with_google_instead_of_a_password(house):
    admin = house.offer_google()
    made = admin.post("/v1/admin/people", json={"login": "sam"}).json()["result"]
    code = made["setup_path"].rsplit("/", 1)[1]
    sam = house.browser()
    assert sam.post("/v1/sign-in/with/google", json={"setup_code": "not-a-code"}).status_code == 404
    assert house.through_google(sam, "g-sam", setup_code=code, email="sam@example.com", name="Sam Roe") == "/#/receipts"
    told = sam.get("/v1/session").json()["result"]
    assert told["account"] == {"login": "sam", "name": "Sam Roe", "admin": False}
    assert sam.get("/v1/account").json()["result"]["has_password"] is False
    assert house.store.password_hash(made["account"]["id"]) is None
    # The link is spent, and Google signs them in from now on.
    assert house.browser().post("/v1/setup/check", json={"code": code}).status_code == 404
    assert house.through_google(house.browser(), "g-sam") == "/#/receipts"
    people = {person["login"]: person for person in admin.get("/v1/admin/people").json()["result"]["people"]}
    assert people["sam"]["services"] == ["google"] and people["sam"]["has_password"] is False
    # With no password, connecting again needs none, and the one way in cannot be removed.
    assert sam.delete("/v1/account/connections/google").status_code == 409
    assert connect(house, sam, "g-sam-new", password="") == "/#/settings?connected=google"


def test_a_setup_link_is_kept_when_the_google_account_belongs_to_someone_else(house):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    code = admin.post("/v1/admin/people", json={"login": "sam"}).json()["result"]["setup_path"].rsplit("/", 1)[1]
    sam = house.browser()
    assert house.through_google(sam, "g-owner", setup_code=code) == "/#/sign-in-problem/in-use"
    assert sam.get("/v1/session").status_code == 401
    assert sam.post("/v1/setup/check", json={"code": code}).status_code == 200
    # A link that runs out or is replaced while the person is away at Google gives nothing.
    started = sam.post("/v1/sign-in/with/google", json={"setup_code": code}).json()["result"]["url"]
    house.store.new_setup_code("sam")
    assert sam.get(house.google.sign_in(started, "g-sam")).headers["location"] == "/#/sign-in-problem/expired"
    assert sam.get("/v1/session").status_code == 401 and house.store.identities_of(house.store.account("sam")["id"]) == []


# --- what must not work

def test_a_trip_can_only_be_finished_once_by_the_browser_that_started_it(house):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    mine = house.browser()
    back = house.google.sign_in(mine.post("/v1/sign-in/with/google", json={}).json()["result"]["url"], "g-owner")

    # Another browser, with no trip of its own or with a different one, cannot use the address.
    other = house.browser()
    assert other.get(back).headers["location"] == "/#/sign-in-problem/expired"
    other.post("/v1/sign-in/with/google", json={})
    assert other.get(back).headers["location"] == "/#/sign-in-problem/expired"
    assert other.get("/v1/session").status_code == 401
    assert house.browser().get(CALLBACK).headers["location"] == "/#/sign-in-problem/expired"
    assert house.browser().get(CALLBACK + "?state=made-up&code=made-up").headers["location"] == "/#/sign-in-problem/expired"

    # The browser that started it finishes it, once.
    assert mine.get(back).headers["location"] == "/#/receipts"
    replay = house.browser()
    replay.cookies.set("kaal_signin", back.split("state=")[1], domain="testserver", path="/v1/sign-in/with")
    assert replay.get(back).headers["location"] == "/#/sign-in-problem/expired"
    assert replay.get("/v1/session").status_code == 401


def test_a_trip_runs_out(house, monkeypatch):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    client = house.browser()
    back = house.google.sign_in(client.post("/v1/sign-in/with/google", json={}).json()["result"]["url"], "g-owner")
    real = accounts._now
    monkeypatch.setattr(accounts, "_now", lambda: accounts._later(minutes=accounts.FLOW_MINUTES + 1))
    assert client.get(back).headers["location"] == "/#/sign-in-problem/expired"
    monkeypatch.setattr(accounts, "_now", real)
    assert client.get("/v1/session").status_code == 401


def test_starting_a_trip_needs_the_web_apps_own_header(house):
    house.offer_google()
    bare = TestClient(house.app, base_url=HERE, follow_redirects=False)
    assert bare.post("/v1/sign-in/with/google", json={}).status_code == 403
    signed = house.signed_in("owner")
    cookie = {"Cookie": f"kaal_session={signed.cookies.get('kaal_session')}"}
    assert bare.post("/v1/account/connections/google", json={"password": GOOD}, headers=cookie).status_code == 403
    assert bare.delete("/v1/account/connections/google", headers=cookie).status_code == 403
    # A token is not a person: it can connect nothing.
    token = {"Authorization": f"Bearer {house.old_token}"}
    assert house.browser().post("/v1/account/connections/google", json={}, headers=token).status_code == 403
    assert house.browser().delete("/v1/account/connections/google", headers=token).status_code == 403


@pytest.mark.parametrize("lie", ["nonce", "audience", "issuer", "expired", "future", "no-subject", "wrong-key", "unsigned", "garbage", "extra-audience"])
def test_a_statement_that_does_not_check_out_signs_nobody_in(house, lie):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    google = house.google
    now = int(time.time())
    twists = {"nonce": lambda c: {**c, "nonce": "someone else's"}, "audience": lambda c: {**c, "aud": "another-application"},
              "issuer": lambda c: {**c, "iss": "https://accounts.evil.example"}, "expired": lambda c: {**c, "exp": now - 3600},
              "future": lambda c: {**c, "iat": now + 3600}, "no-subject": lambda c: {key: value for key, value in c.items() if key != "sub"},
              "extra-audience": lambda c: {**c, "aud": [google.client_id, "another-application"]}}
    if lie in twists:
        google.twist = twists[lie]
    elif lie == "wrong-key":
        google.use_wrong_key()
    elif lie == "unsigned":
        google.algorithm = "none"
    else:
        google.raw_statement = "not.a.statement"
    client = house.browser()
    assert house.through_google(client, "g-owner") == "/#/sign-in-problem/failed"
    assert client.get("/v1/session").status_code == 401
    assert "kaal_session" not in client.cookies


def test_other_ways_a_trip_fails(house):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    google = house.google
    # The person presses Cancel at Google.
    client = house.browser()
    started = client.post("/v1/sign-in/with/google", json={}).json()["result"]["url"]
    assert client.get(google.sign_in(started, "g-owner", refuse=True)).headers["location"] == "/#/sign-in-problem/cancelled"
    # The service is given the wrong secret, or is down when the browser comes back.
    house.store.put_service("google", "Google", google.issuer, google.client_id, oidc.seal_secret(kaal.installation_key(), "the wrong secret"))
    assert house.through_google(house.browser(), "g-owner") == "/#/sign-in-problem/failed"
    house.store.put_service("google", "Google", google.issuer, google.client_id, "not sealed with this installation's key")
    assert house.through_google(house.browser(), "g-owner") == "/#/sign-in-problem/failed"
    house.offer_google(admin)
    client = house.browser()
    started = client.post("/v1/sign-in/with/google", json={}).json()["result"]["url"]
    google.down = True
    assert client.get(google.sign_in(started, "g-owner")).headers["location"] == "/#/sign-in-problem/failed"
    google.down = False
    # A service removed while the person was away.
    client = house.browser()
    started = client.post("/v1/sign-in/with/google", json={}).json()["result"]["url"]
    assert admin.delete("/v1/admin/sign-in-services/google").json()["result"]["services"] == []
    assert client.get(google.sign_in(started, "g-owner")).headers["location"] == "/#/sign-in-problem/failed"
    assert house.browser().post("/v1/sign-in/with/google", json={}).status_code == 404
    assert admin.delete("/v1/admin/sign-in-services/google").status_code == 404
    # Added again, the same people are still connected.
    house.offer_google(admin)
    assert house.through_google(house.browser(), "g-owner") == "/#/receipts"


def test_a_service_that_cannot_be_reached_says_so_when_starting(house):
    house.offer_google()
    house.state.services = oidc.Services(house.google.fetch)   # nothing remembered
    house.google.down = True
    refused = house.browser().post("/v1/sign-in/with/google", json={})
    assert refused.status_code == 503 and "Google could not be reached" in refused.json()["error"]


def test_an_account_that_is_switched_off_cannot_sign_in_or_connect(house):
    admin = house.offer_google()
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    connect(house, sam, "g-sam")
    waiting = sam.post("/v1/account/connections/google", json={"password": GOOD}).json()["result"]["url"]
    house.store.set_disabled("sam", True)
    assert house.through_google(house.browser(), "g-sam") == "/#/sign-in-problem/off"
    assert sam.get(house.google.sign_in(waiting, "g-sam-2")).headers["location"] == "/#/settings?problem=off"
    assert admin.get("/v1/session").status_code == 200


def test_signing_in_with_google_ends_no_other_session_but_setup_with_it_does(house):
    admin = house.offer_google()
    connect(house, admin, "g-owner")
    house.through_google(house.browser(), "g-owner")
    assert admin.get("/v1/session").status_code == 200
    code = house.store.new_setup_code("owner")
    fresh = house.browser()
    assert house.through_google(fresh, "g-owner", setup_code=code) == "/#/receipts"
    assert admin.get("/v1/session").status_code == 401 and fresh.get("/v1/session").status_code == 200


def test_the_log_names_the_account_and_nothing_from_the_trip(house, caplog):
    admin = house.offer_google()
    caplog.set_level(logging.INFO, logger="kaal.server")
    connect(house, admin, "g-owner-subject", email="jane.doe@example.com", name="Jane Doe")
    client = house.browser()
    started = client.post("/v1/sign-in/with/google", json={}).json()["result"]["url"]
    back = house.google.sign_in(started, "g-owner-subject", email="jane.doe@example.com", name="Jane Doe")
    client.get(back)
    house.google.use_wrong_key()
    house.through_google(house.browser(), "g-owner-subject", email="jane.doe@example.com")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "GET /v1/sign-in/with/{service_id}/callback 303" in logged
    assert f"account:{house.store.account('owner')['id']}" in logged and "sign-in with google failed" in logged
    code, state = (part.split("=")[1] for part in back.split("?")[1].split("&"))
    for private in (code, state, "jane.doe", "Jane Doe", "g-owner-subject", house.google.client_secret):
        assert private not in logged


# --- the store

def test_changing_a_services_issuer_drops_its_connections_and_changing_its_client_does_not(house):
    store = house.store
    owner = store.account("owner")["id"]
    store.put_service("work", "Work", "https://id.work.example", "client-1", "sealed")
    store.connect_identity(owner, "work", "w-1", "jane@work.example")
    store.put_service("work", "Work", "https://id.work.example/", "client-2", "sealed")
    assert store.identity_account("work", "w-1")["id"] == owner
    store.put_service("work", "Work", "https://id.other.example", "client-2", "sealed")
    assert store.identity_account("work", "w-1") is None
    for poor in (("Work", "W", "https://x.example", "c", "s"), ("work", "", "https://x.example", "c", "s"), ("work", "W", "http://x.example", "c", "s"),
                 ("work", "W", "https://x.example", "", "s"), ("work", "W", "https://x.example", "c", ""), ("9work", "W", "https://x.example", "c", "s")):
        with pytest.raises(accounts.AccountError):
            store.put_service(*poor) if poor[0] != "Work" else store.put_service("Wörk", *poor[1:])


def test_removing_an_account_removes_its_connections_and_an_older_store_is_brought_up_to_date(house, tmp_path):
    store = house.store
    sam = store.add_account("sam")["id"]
    store.put_service("google", "Google", "https://accounts.google.com", "c", "s")
    store.connect_identity(sam, "google", "g-sam")
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("DELETE FROM accounts WHERE id = ?", (sam,))
    assert store.identity_account("google", "g-sam") is None

    # A store from before sign-in services gains the new tables and keeps what it had.
    old = accounts.AccountStore(tmp_path / "old" / "accounts.db", tmp_path / "old")
    old.add_account("jane")
    with sqlite3.connect(old.path) as db:
        for table in ("signin_services", "identities", "signin_flows", "app_signins"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version = 2")
    reopened = accounts.AccountStore(old.path, old.home)
    assert reopened.account("jane")["login"] == "jane" and reopened.services() == []
    with sqlite3.connect(old.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == accounts.SCHEMA_VERSION


def test_free_sign_in_names(house):
    store = house.store
    assert store.free_login("Jane.Doe") == "jane.doe"
    assert store.free_login("owner") == "owner2"
    assert store.free_login("...") == "person" and store.free_login("") == "person"
    assert store.free_login("Zoë Ångström") == "zongstrm"
    accounts.clean_login(store.free_login("x" * 200))


# --- reading a statement

def test_statements_signed_with_an_elliptic_curve_key_are_read_too():
    service = FakeService()
    service.algorithm, service.kid = "ES256", "key-ec"
    now = int(time.time())
    claims = {"iss": "accounts.google.com", "aud": service.client_id, "sub": "g-1", "nonce": "n-1", "iat": now, "exp": now + 60}
    check = {"issuer": service.issuer, "client_id": service.client_id, "nonce": "n-1"}
    assert oidc.read_statement(service.statement(claims), service.keys()["keys"], **check)["sub"] == "g-1"
    head, body, signature = service.statement(claims).split(".")
    changed = b64(json.dumps({**claims, "sub": "g-2"}).encode())
    with pytest.raises(oidc.SignInServiceError):
        oidc.read_statement(f"{head}.{changed}.{signature}", service.keys()["keys"], **check)
    with pytest.raises(oidc.SignInServiceError):
        oidc.read_statement(service.statement(claims), service.keys()["keys"], **{**check, "nonce": ""})
    # A key under another name is not tried.
    service.kid = "key-1"
    with pytest.raises(oidc.SignInServiceError):
        oidc.read_statement(service.statement(claims), service.keys()["keys"], **check)


def test_only_https_addresses_are_ever_asked_and_keys_are_fetched_again_when_they_change():
    with pytest.raises(oidc.SignInServiceError):
        oidc.fetch("http://accounts.google.com/.well-known/openid-configuration")
    plain = FakeService(issuer="http://id.example")
    with pytest.raises(oidc.SignInServiceError):
        oidc.Services(plain.fetch).settings("http://id.example")
    assert plain.asked == []

    class Liar(FakeService):
        def fetch(self, url, form=None):
            status, body = super().fetch(url, form)
            if url.endswith("openid-configuration"):
                body = json.dumps({**json.loads(body), self.wrong[0]: self.wrong[1]}).encode()
            return status, body

    for wrong in (("issuer", "https://elsewhere.example"), ("token_endpoint", "http://accounts.google.com/token"), ("jwks_uri", "")):
        liar = Liar()
        liar.wrong = wrong
        with pytest.raises(oidc.SignInServiceError):
            oidc.Services(liar.fetch).settings(liar.issuer)

    service = FakeService()
    services = oidc.Services(service.fetch)
    services.settings(service.issuer)
    services.settings(service.issuer)
    assert service.asked.count(f"{service.issuer}/.well-known/openid-configuration") == 1


# --- on the server's command line

def cli(monkeypatch, capsys, home, *argv):
    monkeypatch.setenv("KAAL_HOME", str(home))
    monkeypatch.setenv("KAAL_VAULT", str(home / "data"))
    monkeypatch.setenv("KAAL_TOKENS_FILE", str(home / "tokens.json"))
    args = server.build_parser().parse_args(list(argv))
    args.func(args)
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_command_line_adds_lists_and_removes_a_service(house, monkeypatch, capsys):
    monkeypatch.setattr(oidc, "fetch", house.google.fetch)
    secret_file = house.home / "google.secret"
    secret_file.write_text(house.google.client_secret + "\n")
    with pytest.raises(SystemExit):
        cli(monkeypatch, capsys, house.home, "sign-in", "add", "google", "--client-id", house.google.client_id, "--secret-file", str(secret_file))
    capsys.readouterr()
    out, _ = cli(monkeypatch, capsys, house.home, "sign-in", "add", "google", "--client-id", house.google.client_id, "--secret-file", str(secret_file),
                 "--address", "https://testserver/")
    told = json.loads(out)
    assert told == {"address": HERE, "services": [{"id": "google", "name": "Google", "issuer": "https://accounts.google.com", "client_id": house.google.client_id,
                                                  "register_this_redirect_address": HERE + CALLBACK}]}
    assert house.google.client_secret not in out
    # The running server offers it at once, and it works.
    assert house.browser().get("/v1/sign-in/options").json()["result"]["services"] == [{"id": "google", "name": "Google"}]
    code = house.store.new_setup_code("owner")
    assert house.through_google(house.browser(), "g-owner", setup_code=code) == "/#/receipts"

    assert json.loads(cli(monkeypatch, capsys, house.home, "sign-in", "list")[0]) == told
    for poor in (("add", "work", "--client-id", "x", "--secret-file", str(secret_file)), ("add", "google", "--client-id", "x", "--secret-file", str(house.home / "missing")),
                 ("remove", "nothing")):
        with pytest.raises(SystemExit):
            cli(monkeypatch, capsys, house.home, "sign-in", *poor)
        capsys.readouterr()
    assert json.loads(cli(monkeypatch, capsys, house.home, "sign-in", "remove", "google")[0])["services"] == []


def test_the_command_line_asks_for_the_secret_at_a_terminal_and_reads_it_when_piped(house, monkeypatch, capsys):
    import getpass
    import io

    monkeypatch.setattr(oidc, "fetch", house.google.fetch)
    asked = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: asked.append(prompt) or f"  {house.google.client_secret}\n")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True, read=lambda: pytest.fail("a terminal is asked, not read to its end")))
    out, _ = cli(monkeypatch, capsys, house.home, "sign-in", "add", "google", "--client-id", house.google.client_id, "--address", HERE)
    assert len(asked) == 1 and "not shown" in asked[0] and house.google.client_secret not in out
    assert oidc.open_secret(kaal.installation_key(), house.store.service("google")["secret"]) == house.google.client_secret

    monkeypatch.setattr("sys.stdin", io.StringIO("a piped secret\n"))
    cli(monkeypatch, capsys, house.home, "sign-in", "add", "google", "--client-id", house.google.client_id, "--secret-file", "-", "--address", HERE)
    assert len(asked) == 1
    assert oidc.open_secret(kaal.installation_key(), house.store.service("google")["secret"]) == "a piped secret"

    # An empty answer stores nothing new.
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "   ")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    with pytest.raises(SystemExit):
        cli(monkeypatch, capsys, house.home, "sign-in", "add", "google", "--client-id", house.google.client_id, "--address", HERE)
    capsys.readouterr()
    assert oidc.open_secret(kaal.installation_key(), house.store.service("google")["secret"]) == "a piped secret"
