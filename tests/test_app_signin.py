"""An app on a person's own device signs in as them, handed over by a browser."""
import base64
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import accounts, server, tokens

GOOD = "correct horse battery"
APP = {"X-Kaal-Request": "1"}
HERE = "https://testserver"
VERIFIER = "v" * 43


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@pytest.fixture()
def house(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", rate_per_minute=100_000)
    token = tokens.TokenStore(settings.tokens_file).create("a-token", ["read", "reveal", "write", "delete", "admin"])[1]
    app = server.create_app(settings)
    state = app.state.kaal

    def browser():
        return TestClient(app, base_url=HERE, headers=APP, follow_redirects=False)

    def signed_in(login):
        client = browser()
        done = client.post("/v1/setup", json={"code": state.accounts.new_setup_code(login), "password": GOOD})
        assert done.status_code == 200, done.text
        return client

    def phone(session=""):
        """The app: no cookie jar of its own, it sends the session it was given."""
        headers = {**APP, "X-Kaal-Session": session} if session else dict(APP)
        return TestClient(app, base_url=HERE, headers=headers, follow_redirects=False)

    yield SimpleNamespace(app=app, state=state, store=state.accounts, browser=browser, signed_in=signed_in, phone=phone, token=token, home=tmp_path)
    kaal.configure_vault(saved)


def approve(client, verifier=VERIFIER) -> str:
    answered = client.post("/v1/account/app-sign-ins", json={"challenge": challenge_for(verifier)})
    assert answered.status_code == 200, answered.text
    return answered.json()["result"]["code"]


def collect(house, code, verifier=VERIFIER, device="Kaal on iPhone"):
    return house.phone().post("/v1/app-sign-ins", json={"code": code, "verifier": verifier, "device": device})


def test_a_signed_in_person_lets_the_app_in_and_it_acts_as_them(house):
    owner = house.signed_in("owner")
    told = collect(house, approve(owner))
    assert told.status_code == 200, told.text
    result = told.json()["result"]
    assert result["account"]["login"] == "owner" and result["session"].startswith(accounts.SESSION_PREFIX + "_")
    assert "set-cookie" not in told.headers          # the app keeps the session itself; nothing is left in a cookie jar

    app = house.phone(result["session"])
    account = app.get("/v1/account").json()["result"]
    assert account["account"]["login"] == "owner"
    assert [entry["label"] for entry in account["sessions"] if entry["current"]] == ["Kaal on iPhone"]
    made = app.post("/v1/library/notes", json={"title": "From the phone", "body": "hello"})
    assert made.status_code == 200, made.text
    assert owner.get("/v1/library/notes").json()["meta"]["total"] == 1


def test_the_app_stays_signed_in_for_a_year_and_can_be_signed_out_from_a_browser(house):
    owner = house.signed_in("owner")
    session = collect(house, approve(owner)).json()["result"]["session"]
    with sqlite3.connect(house.home / "accounts.db") as db:
        expires = db.execute("SELECT expires FROM sessions WHERE id = ?", (session.split("_")[1],)).fetchone()[0]
    left = datetime.fromisoformat(expires) - datetime.now(timezone.utc)
    assert timedelta(days=364) < left <= timedelta(days=365)

    theirs = [entry for entry in owner.get("/v1/account").json()["result"]["sessions"] if not entry["current"]]
    assert len(theirs) == 1
    assert owner.delete(f"/v1/account/sessions/{theirs[0]['id']}").status_code == 200
    assert house.phone(session).get("/v1/account").status_code == 401


def test_a_code_works_once(house):
    code = approve(house.signed_in("owner"))
    assert collect(house, code).status_code == 200
    again = collect(house, code)
    assert again.status_code == 401 and "session" not in again.text


def test_a_code_is_useless_without_the_secret_the_app_kept(house):
    code = approve(house.signed_in("owner"))
    wrong = collect(house, code, verifier="w" * 43)
    assert wrong.status_code == 401
    # A wrong try uses the code up, so it cannot be tried again with other guesses.
    assert collect(house, code).status_code == 401


def test_a_code_runs_out_after_five_minutes(house):
    code = approve(house.signed_in("owner"))
    with sqlite3.connect(house.home / "accounts.db") as db:
        db.execute("UPDATE app_signins SET expires = ?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    assert collect(house, code).status_code == 401


def test_only_a_person_signed_in_as_themselves_can_let_an_app_in(house):
    body = {"challenge": challenge_for(VERIFIER)}
    assert house.browser().post("/v1/account/app-sign-ins", json=body).status_code == 401
    with_token = house.browser().post("/v1/account/app-sign-ins", json=body, headers={"Authorization": f"Bearer {house.token}"})
    assert with_token.status_code == 403
    # Another website cannot make a signed-in browser approve one.
    owner = house.signed_in("owner")
    forged = owner.post("/v1/account/app-sign-ins", json=body, headers={"X-Kaal-Request": ""})
    assert forged.status_code == 403


@pytest.mark.parametrize("challenge", ["", "short", "x" * 44, "!" * 43])
def test_a_malformed_challenge_is_refused(house, challenge):
    assert house.signed_in("owner").post("/v1/account/app-sign-ins", json={"challenge": challenge}).status_code == 400


def test_an_account_switched_off_meanwhile_gets_no_session(house):
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    code = approve(sam)
    house.store.set_disabled("sam", True)
    assert collect(house, code).status_code == 401


def test_the_app_opens_the_vault_the_browser_had_open(house):
    owner = house.signed_in("owner")
    second = owner.post("/v1/account/vaults", json={"name": "Household"}).json()["result"]["vault"]
    assert owner.post("/v1/session/vault", json={"vault": second["id"]}).status_code == 200
    result = collect(house, approve(owner)).json()["result"]
    assert result["vault"]["id"] == second["id"]


def test_the_app_must_mark_its_changes_like_the_web_app_does(house):
    session = collect(house, approve(house.signed_in("owner"))).json()["result"]["session"]
    bare = TestClient(house.app, base_url=HERE, headers={"X-Kaal-Session": session})
    assert bare.get("/v1/library/notes").status_code == 200
    assert bare.post("/v1/library/notes", json={"title": "t", "body": "b"}).status_code == 403


def test_nothing_readable_about_a_sign_in_is_stored(house):
    code = approve(house.signed_in("owner"))
    raw = (house.home / "accounts.db").read_bytes()
    assert code.encode() not in raw and VERIFIER.encode() not in raw
    session = collect(house, code).json()["result"]["session"]
    assert session.split("_", 2)[2].encode() not in (house.home / "accounts.db").read_bytes()


def test_an_installation_from_before_app_sign_ins_gains_the_table(tmp_path):
    store = accounts.AccountStore(tmp_path / "accounts.db", tmp_path)
    made = store.add_account("owner", admin=True)
    with sqlite3.connect(tmp_path / "accounts.db") as db:
        db.execute("DROP TABLE app_signins")
        db.execute("PRAGMA user_version = 3")
    # (layout 4 made the table; layout 5 gave it a kind; layout 6, whether every vault was allowed)
    again = accounts.AccountStore(tmp_path / "accounts.db", tmp_path)
    code = again.start_app_signin(made["id"], None, challenge_for(VERIFIER))
    assert again.finish_app_signin(code, VERIFIER) == {"account": made["id"], "vault": None, "every_vault": False}
    with sqlite3.connect(tmp_path / "accounts.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == accounts.SCHEMA_VERSION == 6


# --- the browser extension, through the same hand-over

EXTENSION = server.OFFICIAL_EXTENSION_ID


def approve_extension(client, verifier=VERIFIER, extension=EXTENSION):
    return client.post("/v1/account/app-sign-ins", json={"challenge": challenge_for(verifier), "kind": "extension", "extension": extension})


def test_the_extension_is_connected_with_a_token_capped_by_the_persons_role(house):
    owner = house.signed_in("owner")
    approved = approve_extension(owner)
    assert approved.status_code == 200, approved.text
    handed = approved.json()["result"]
    assert handed["redirect"] == f"https://{EXTENSION}.chromiumapp.org/kaal?code={handed['code']}"
    got = house.phone().post("/v1/extension-sign-ins", json={"code": handed["code"], "verifier": VERIFIER})
    assert got.status_code == 200, got.text
    result = got.json()["result"]
    assert result["scopes"] == ["read", "reveal", "write"] and result["name"] == "Chrome extension" and result["account"] == "owner"
    script = TestClient(house.app, base_url=HERE, headers={"Authorization": f"Bearer {result['token']}"})
    assert script.get("/v1/medical/receipts").status_code == 200
    assert house.phone().post("/v1/extension-sign-ins", json={"code": handed["code"], "verifier": VERIFIER}).status_code == 401
    # A second connection gets a name of its own.
    again = approve_extension(owner).json()["result"]["code"]
    assert house.phone().post("/v1/extension-sign-ins", json={"code": again, "verifier": VERIFIER}).json()["result"]["name"] == "Chrome extension 2"


def test_a_viewer_connects_the_extension_only_to_read(house):
    owner = house.signed_in("owner")
    house.store.add_account("sam")
    owner.post("/v1/vault/members", json={"login": "sam", "role": "viewer"})
    sam = house.signed_in("sam")
    vault = owner.get("/v1/session").json()["result"]["vault"]["id"]
    sam.post("/v1/session/vault", json={"vault": vault})
    code = approve_extension(sam).json()["result"]["code"]
    assert house.phone().post("/v1/extension-sign-ins", json={"code": code, "verifier": VERIFIER}).json()["result"]["scopes"] == ["read", "reveal"]


# --- an extension let into every vault of its person

def two_vaults(house):
    """The owner with a second vault of their own, "Household", and the first one open."""
    owner = house.signed_in("owner")
    first = owner.get("/v1/session").json()["result"]["vault"]
    second = owner.post("/v1/account/vaults", json={"name": "Household"}).json()["result"]["vault"]
    return owner, first, second


def connect_extension(house, client, every_vault=True):
    approved = client.post("/v1/account/app-sign-ins", json={"challenge": challenge_for(VERIFIER), "kind": "extension", "extension": EXTENSION, "every_vault": every_vault})
    assert approved.status_code == 200, approved.text
    got = house.phone().post("/v1/extension-sign-ins", json={"code": approved.json()["result"]["code"], "verifier": VERIFIER})
    assert got.status_code == 200, got.text
    return got.json()["result"]


def with_token(house, token, vault=""):
    headers = {"Authorization": f"Bearer {token}"}
    if vault:
        headers["X-Kaal-Vault"] = vault
    return TestClient(house.app, base_url=HERE, headers=headers)


def keep(client, text):
    made = client.post("/v1/captures/text", json={"text": text, "title": text})
    assert made.status_code in (200, 201), made.text
    return made.json()["result"]["id"]


def test_an_extension_let_into_every_vault_saves_where_each_request_says(house):
    owner, first, second = two_vaults(house)
    made = connect_extension(house, owner)
    assert made["every_vault"] is True and made["vault_id"] == first["id"]
    assert [vault["id"] for vault in made["vaults"]] == [first["id"], second["id"]]
    here = keep(with_token(house, made["token"]), "kept in the first")
    there = keep(with_token(house, made["token"], second["id"]), "kept in the second")
    # Each is in the vault that was named, and only there.
    assert with_token(house, made["token"], first["id"]).get(f"/v1/library/notes/{here}").status_code == 200
    assert with_token(house, made["token"], second["id"]).get(f"/v1/library/notes/{there}").status_code == 200
    assert with_token(house, made["token"], second["id"]).get(f"/v1/library/notes/{here}").status_code == 404
    assert with_token(house, made["token"]).get(f"/v1/library/notes/{there}").status_code == 404
    # The token is told where it works and where a request of its is.
    told = with_token(house, made["token"], second["id"]).get("/v1/session").json()["result"]
    assert told["every_vault"] is True and told["vault"]["id"] == second["id"] and told["elsewhere"] == 0
    assert [vault["name"] for vault in told["vaults"]] == [first["name"], "Household"]
    assert "account" not in told   # still a token: no page for managing the account opens for it


def test_a_vault_that_is_not_the_persons_is_refused_and_nothing_is_saved_elsewhere(house):
    owner, first, _second = two_vaults(house)
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    theirs = sam.post("/v1/account/vaults", json={"name": "Sam's"}).json()["result"]["vault"]
    made = connect_extension(house, owner)
    refused = with_token(house, made["token"], theirs["id"]).post("/v1/captures/text", json={"text": "not here", "title": "not here"})
    assert refused.status_code == 403 and "do not belong to that vault" in refused.text
    assert with_token(house, made["token"], "no-such-vault").get("/v1/library/notes").status_code == 403
    assert with_token(house, made["token"]).get("/v1/library/notes").json()["result"] == []
    # Let in afterwards, the same token works there without connecting again.
    assert sam.post("/v1/session/vault", json={"vault": theirs["id"]}).status_code == 200
    assert sam.post("/v1/vault/members", json={"login": "owner", "role": "viewer"}).status_code == 200
    assert with_token(house, made["token"], theirs["id"]).get("/v1/library/notes").status_code == 200
    # As far as the role there allows: a viewer reads and does not write.
    capped = with_token(house, made["token"], theirs["id"]).post("/v1/captures/text", json={"text": "no", "title": "no"})
    assert capped.status_code == 403 and "write" in capped.text
    assert with_token(house, made["token"], first["id"]).post("/v1/captures/text", json={"text": "yes", "title": "yes"}).status_code in (200, 201)


def test_a_token_for_one_vault_is_not_moved_by_a_request(house):
    owner, first, second = two_vaults(house)
    made = connect_extension(house, owner, every_vault=False)
    assert made["every_vault"] is False and [vault["id"] for vault in made["vaults"]] == [first["id"]]
    assert with_token(house, made["token"], first["id"]).get("/v1/library/notes").status_code == 200
    refused = with_token(house, made["token"], second["id"]).post("/v1/captures/text", json={"text": "not here", "title": "not here"})
    assert refused.status_code == 403 and "one vault only" in refused.text
    told = with_token(house, made["token"]).get("/v1/session").json()["result"]
    assert told["every_vault"] is False and told["elsewhere"] == 1 and [vault["id"] for vault in told["vaults"]] == [first["id"]]
    # Nor is a browser's sign-in: it works in the vault that is open in it.
    assert owner.get("/v1/library/notes", headers={"X-Kaal-Vault": second["id"]}).status_code == 403
    assert owner.get("/v1/library/notes", headers={"X-Kaal-Vault": first["id"]}).status_code == 200


def test_a_token_for_every_vault_outlives_the_vault_it_was_made_in(house):
    owner, first, second = two_vaults(house)
    assert owner.post("/v1/session/vault", json={"vault": second["id"]}).status_code == 200
    made = connect_extension(house, owner)
    assert made["vault_id"] == second["id"]
    gone = owner.request("DELETE", "/v1/vault", json={"password": GOOD})
    assert gone.status_code == 200, gone.text
    # Naming none, it now works in the person's first vault; the one that is gone is refused.
    assert with_token(house, made["token"]).get("/v1/session").json()["result"]["vault"]["id"] == first["id"]
    assert with_token(house, made["token"], second["id"]).get("/v1/library/notes").status_code == 403
    # The list of the person's tokens says that it is for every vault.
    listed = owner.get("/v1/account/tokens").json()["result"]["tokens"]
    assert [token["every_vault"] for token in listed if token["name"] == "Chrome extension"] == [True]


def test_the_extension_may_send_the_vault_it_means_from_its_own_pages(house):
    asked = TestClient(house.app, base_url=HERE).options("/v1/captures", headers={
        "Origin": f"chrome-extension://{EXTENSION}", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, x-kaal-vault"})
    assert asked.status_code in (200, 204) and "x-kaal-vault" in asked.headers["access-control-allow-headers"].lower()


def test_an_unknown_extension_is_never_handed_a_code(house):
    owner = house.signed_in("owner")
    assert approve_extension(owner, extension="a" * 32).status_code == 400
    assert approve_extension(owner, extension="not-an-id").status_code == 400


def test_a_code_made_for_the_app_does_not_connect_the_extension_or_the_other_way_round(house):
    owner = house.signed_in("owner")
    for_app = approve(owner)
    assert house.phone().post("/v1/extension-sign-ins", json={"code": for_app, "verifier": VERIFIER}).status_code == 401
    for_extension = approve_extension(owner).json()["result"]["code"]
    assert collect(house, for_extension).status_code == 401


def test_the_app_is_told_its_own_address(house):
    answer = house.signed_in("owner").post("/v1/account/app-sign-ins", json={"challenge": challenge_for(VERIFIER)}).json()["result"]
    assert answer["redirect"] == f"kaal://signed-in?code={answer['code']}"
