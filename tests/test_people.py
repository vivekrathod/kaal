"""Administrators adding people, owners sharing a vault, open sign-up, and tokens made in the web app."""
import dataclasses
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

GOOD = "correct horse battery"
APP = {"X-Kaal-Request": "1"}
ALL = ["read", "reveal", "write", "delete", "admin"]


@pytest.fixture()
def house(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp",
                               rate_per_minute=100_000, signups_per_hour=3, vaults_per_person=3)
    old_token = tokens.TokenStore(settings.tokens_file).create("old-token", ALL)[1]
    app = server.create_app(settings)
    state = app.state.kaal

    def browser(application=None):
        return TestClient(application or app, base_url="https://testserver", headers=APP)

    def signed_in(login):
        """A browser signed in as ``login``, who is given a password through a setup link."""
        client = browser()
        done = client.post("/v1/setup", json={"code": state.accounts.new_setup_code(login), "password": GOOD})
        assert done.status_code == 200, done.text
        return client

    def use_link(path, password=GOOD):
        """Follow a setup link an administrator was shown; returns that person's browser."""
        assert path.startswith("/#/setup/")
        client = browser()
        done = client.post("/v1/setup", json={"code": path.rsplit("/", 1)[1], "password": password})
        assert done.status_code == 200, done.text
        return client

    yield SimpleNamespace(app=app, state=state, store=state.accounts, browser=browser, signed_in=signed_in, use_link=use_link,
                          old_token=old_token, settings=settings, tokens=state.tokens)
    kaal.configure_vault(saved)


# --- administrators

def test_only_an_administrator_signed_in_as_themselves_may_manage_people(house):
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    token = {"Authorization": f"Bearer {house.old_token}"}
    calls = [("GET", "/v1/admin/people", None), ("POST", "/v1/admin/people", {"login": "new"}), ("POST", "/v1/admin/people/x/setup-link", None),
             ("POST", "/v1/admin/people/x/disabled", {"on": True}), ("POST", "/v1/admin/people/x/admin", {"on": True}), ("POST", "/v1/admin/sign-up", {"signup": "open"})]
    for method, path, body in calls:
        assert sam.request(method, path, json=body).status_code == 403, path
        assert house.browser().request(method, path, json=body, headers=token).status_code == 403, path
        assert house.browser().request(method, path, json=body).status_code == 401, path
    assert [person["login"] for person in house.store.accounts()] == ["owner", "sam"]


def test_an_administrator_adds_a_person_who_then_chooses_their_own_password(house):
    admin = house.signed_in("owner")
    made = admin.post("/v1/admin/people", json={"login": " Sam@Example.com ", "name": "Sam"}).json()["result"]
    assert made["account"]["login"] == "sam@example.com" and made["vault"]["name"] == "My notes"
    assert GOOD not in str(made) and "password" not in str(made).lower()

    listed = {person["login"]: person for person in admin.get("/v1/admin/people").json()["result"]["people"]}
    assert (listed["sam@example.com"]["has_password"], listed["sam@example.com"]["link_waiting"], listed["sam@example.com"]["browsers"]) == (False, True, 0)
    assert listed["owner"]["has_password"] and listed["owner"]["browsers"] == 1

    sam = house.use_link(made["setup_path"])
    session = sam.get("/v1/session").json()["result"]
    assert session["account"]["login"] == "sam@example.com" and session["vault"]["role"] == "owner"
    assert sam.post("/v1/library/notes", json={"title": "Sam's first", "body": "b"}).status_code == 200
    assert admin.get("/v1/library/notes").json()["result"] == [], "an administrator does not see into other people's vaults"
    listed = {person["login"]: person for person in admin.get("/v1/admin/people").json()["result"]["people"]}
    assert (listed["sam@example.com"]["has_password"], listed["sam@example.com"]["link_waiting"], listed["sam@example.com"]["browsers"]) == (True, False, 1)

    assert admin.post("/v1/admin/people", json={"login": "SAM@example.com"}).status_code == 409
    assert admin.post("/v1/admin/people", json={"login": "not a name"}).status_code == 400
    bare = admin.post("/v1/admin/people", json={"login": "guest", "own_vault": False}).json()["result"]
    assert bare["vault"] is None and house.store.vaults_of(bare["account"]["id"]) == []


def test_a_new_link_replaces_the_old_and_resets_a_lost_password(house):
    admin = house.signed_in("owner")
    made = admin.post("/v1/admin/people", json={"login": "sam"}).json()["result"]
    again = admin.post(f"/v1/admin/people/{made['account']['id']}/setup-link").json()["result"]["setup_path"]
    assert again != made["setup_path"]
    assert house.browser().post("/v1/setup/check", json={"code": made["setup_path"].rsplit("/", 1)[1]}).status_code == 404
    sam = house.use_link(again)
    # Later Sam forgets the password and has no codes. An administrator cannot make the link any more
    # (it would let them into Sam's vaults); whoever runs the server can, and it signs old browsers out.
    refused = admin.post(f"/v1/admin/people/{made['account']['id']}/setup-link")
    assert refused.status_code == 409 and "kaal-server account setup-link" in refused.json()["error"]
    house.use_link(f"/#/setup/{house.store.new_setup_code('sam')}", "a different password")
    assert sam.get("/v1/session").status_code == 401
    assert house.browser().post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 401
    assert house.browser().post("/v1/sign-in", json={"login": "sam", "password": "a different password"}).status_code == 200
    assert admin.post("/v1/admin/people/nobody/setup-link").status_code == 404


def test_an_administrator_can_switch_people_off_and_share_the_job_but_not_lock_everyone_out(house):
    admin = house.signed_in("owner")
    me = admin.get("/v1/admin/people").json()["result"]["you"]
    sam_id = admin.post("/v1/admin/people", json={"login": "sam"}).json()["result"]["account"]["id"]
    sam = house.signed_in("sam")

    assert admin.post(f"/v1/admin/people/{sam_id}/disabled", json={"on": True}).json()["result"]["account"]["disabled_at"]
    assert sam.get("/v1/session").status_code == 401
    assert admin.post(f"/v1/admin/people/{sam_id}/disabled", json={"on": False}).json()["result"]["account"]["disabled_at"] is None
    assert admin.post(f"/v1/admin/people/{me}/disabled", json={"on": True}).status_code == 400, "not one's own account"
    assert admin.post("/v1/admin/people/nobody/disabled", json={"on": True}).status_code == 404

    assert admin.post(f"/v1/admin/people/{me}/admin", json={"on": False}).status_code == 409, "the only administrator"
    assert admin.post(f"/v1/admin/people/{sam_id}/admin", json={"on": True}).json()["result"]["account"]["admin"] is True
    sam = house.browser()
    assert sam.post("/v1/sign-in", json={"login": "sam", "password": GOOD}).status_code == 200
    assert sam.get("/v1/admin/people").status_code == 200
    assert admin.post(f"/v1/admin/people/{me}/admin", json={"on": False}).json()["result"]["account"]["admin"] is False
    assert admin.get("/v1/admin/people").status_code == 403


# --- open sign-up

def test_accounts_are_by_invitation_until_an_administrator_opens_sign_up(house):
    stranger = house.browser()
    assert stranger.get("/v1/sign-in/options").json()["result"] == {"signup": "invitation", "services": []}
    refused = stranger.post("/v1/sign-up", json={"login": "stranger", "password": GOOD})
    assert refused.status_code == 403 and house.store.account("stranger") is None

    admin = house.signed_in("owner")
    assert admin.post("/v1/admin/sign-up", json={"signup": "anything"}).status_code == 400
    assert admin.post("/v1/admin/sign-up", json={"signup": "open"}).json()["result"] == {"signup": "open"}
    assert stranger.get("/v1/sign-in/options").json()["result"] == {"signup": "open", "services": []}

    assert stranger.post("/v1/sign-up", json={"login": "stranger", "password": "short"}).status_code == 400
    assert stranger.post("/v1/sign-up", json={"login": "owner", "password": GOOD}).status_code == 409
    joined = stranger.post("/v1/sign-up", json={"login": "Stranger", "name": "A Stranger", "password": GOOD})
    assert joined.status_code == 200
    told = joined.json()["result"]
    assert told["account"] == {"login": "stranger", "name": "A Stranger", "admin": False, "has_password": True}
    assert told["vault"]["role"] == "owner" and len(told["recovery_codes"]) == 8
    assert stranger.post("/v1/library/notes", json={"title": "Mine", "body": "b"}).status_code == 200
    assert admin.get("/v1/library/notes").json()["result"] == []

    assert admin.post("/v1/admin/sign-up", json={"signup": "invitation"}).status_code == 200
    assert house.browser().post("/v1/sign-up", json={"login": "late", "password": GOOD}).status_code == 403


def test_open_sign_up_is_capped_per_hour(house):
    house.store.put("signup", "open")
    made = [house.browser().post("/v1/sign-up", json={"login": f"person{n}", "password": GOOD}).status_code for n in range(5)]
    assert made == [200, 200, 200, 429, 429]
    assert len(house.store.accounts()) == 4


# --- sharing a vault

def test_an_owner_shares_their_vault_and_takes_it_back(house):
    owner = house.signed_in("owner")
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    assert owner.post("/v1/library/notes", json={"title": "Household page", "body": "b"}).status_code == 200
    main = owner.get("/v1/session").json()["result"]["vault"]["id"]

    shown = owner.get("/v1/vault/members").json()["result"]
    assert shown["vault"]["name"] == "Main" and [(m["login"], m["role"]) for m in shown["members"]] == [("owner", "owner")]
    assert owner.post("/v1/vault/members", json={"login": "nobody", "role": "viewer"}).status_code == 404
    assert owner.post("/v1/vault/members", json={"login": "sam", "role": "boss"}).status_code == 400
    members = owner.post("/v1/vault/members", json={"login": " SAM ", "role": "viewer"}).json()["result"]["members"]
    assert [(m["login"], m["role"]) for m in members] == [("owner", "owner"), ("sam", "viewer")]

    # Sam has no vault of their own here, so the shared one is where they land.
    assert sam.get("/v1/session").json()["result"]["vault"] == {"id": main, "name": "Main", "role": "viewer"}
    assert [n["title"] for n in sam.get("/v1/library/notes").json()["result"]] == ["Household page"]
    assert sam.post("/v1/library/notes", json={"title": "No", "body": "no"}).status_code == 403
    for method, path, body in (("GET", "/v1/vault/members", None), ("POST", "/v1/vault/members", {"login": "sam", "role": "owner"}),
                               ("DELETE", f"/v1/vault/members/{house.store.account('owner')['id']}", None)):
        assert sam.request(method, path, json=body).status_code == 403, "only an owner manages who shares a vault"

    assert owner.post("/v1/vault/members", json={"login": "sam", "role": "editor"}).status_code == 200
    assert sam.post("/v1/library/notes", json={"title": "From Sam", "body": "b"}).status_code == 200
    assert owner.post("/v1/vault/members", json={"login": "owner", "role": "editor"}).status_code == 409, "a vault keeps an owner"
    assert owner.delete(f"/v1/vault/members/{house.store.account('owner')['id']}").status_code == 409

    gone = owner.delete(f"/v1/vault/members/{house.store.account('sam')['id']}").json()["result"]["members"]
    assert [m["login"] for m in gone] == ["owner"]
    assert sam.get("/v1/library/notes").status_code == 403, "taken out, Sam is out at once"
    token = {"Authorization": f"Bearer {house.old_token}"}
    assert house.browser().get("/v1/vault/members", headers=token).status_code == 403


def test_a_person_names_their_vault_when_signing_up_and_can_rename_it(house):
    house.store.put("signup", "open")
    newcomer = house.browser()
    told = newcomer.post("/v1/sign-up", json={"login": "riley", "password": GOOD, "vault_name": "  Riley's papers "}).json()["result"]
    assert told["vault"]["name"] == "Riley's papers"
    plain = house.browser().post("/v1/sign-up", json={"login": "casey", "password": GOOD}).json()["result"]
    assert plain["vault"]["name"] == "My notes"

    assert newcomer.post("/v1/library/notes", json={"title": "Kept", "body": "b"}).status_code == 200
    renamed = newcomer.post("/v1/vault/name", json={"name": "Receipts and notes"}).json()["result"]["vault"]
    assert renamed["name"] == "Receipts and notes" and renamed["id"] == told["vault"]["id"]
    assert newcomer.get("/v1/session").json()["result"]["vault"]["name"] == "Receipts and notes"
    assert [n["title"] for n in newcomer.get("/v1/library/notes").json()["result"]] == ["Kept"], "only the name changed"
    assert newcomer.post("/v1/vault/name", json={"name": "   "}).status_code == 400

    # Only an owner renames a vault, and an administrator names the vault of someone they add.
    main = house.store.defaults()[1]
    house.store.grant("riley", main, "editor")
    newcomer.post("/v1/session/vault", json={"vault": main})
    assert newcomer.post("/v1/vault/name", json={"name": "Taken over"}).status_code == 403
    admin = house.signed_in("owner")
    made = admin.post("/v1/admin/people", json={"login": "sam", "vault_name": "Sam's vault"}).json()["result"]
    assert made["vault"]["name"] == "Sam's vault"
    assert house.browser().post("/v1/vault/name", json={"name": "x"}, headers={"Authorization": f"Bearer {house.old_token}"}).status_code == 403


def test_a_person_can_make_more_vaults_of_their_own_up_to_a_limit(house):
    owner = house.signed_in("owner")
    made = owner.post("/v1/account/vaults", json={"name": " Household "}).json()["result"]["vault"]
    assert (made["name"], made["role"]) == ("Household", "owner")
    assert owner.post("/v1/session/vault", json={"vault": made["id"]}).json()["result"]["vault"]["name"] == "Household"
    assert owner.post("/v1/library/notes", json={"title": "In the household vault", "body": "b"}).status_code == 200
    with kaal.use_vault(house.state.vaults.get(made["id"])):
        assert len(kaal.load_index()["notes"]) == 1
    assert owner.post("/v1/account/vaults", json={"name": "Third"}).status_code == 200
    assert owner.post("/v1/account/vaults", json={"name": "Fourth"}).status_code == 409
    assert owner.post("/v1/account/vaults", json={"name": ""}).status_code == 400


# --- tokens made in the web app

def test_a_person_makes_and_revokes_tokens_for_their_own_scripts(house):
    owner = house.signed_in("owner")
    main = owner.get("/v1/session").json()["result"]["vault"]["id"]
    # The first account also sees the tokens from before accounts existed.
    assert [(t["name"], t["vault_name"]) for t in owner.get("/v1/account/tokens").json()["result"]["tokens"]] == [("old-token", "Main")]

    made = owner.post("/v1/account/tokens", json={"name": "backup-script", "scopes": ["read", "reveal", "admin"], "vault": main, "password": GOOD}).json()["result"]
    assert made["token"]["scopes"] == ["read", "reveal", "admin"] and made["secret"].startswith("kaal_")
    bearer = {"Authorization": f"Bearer {made['secret']}"}
    script = TestClient(house.app, base_url="https://testserver")
    assert script.get("/v1/export", headers=bearer).status_code == 200
    assert script.post("/v1/library/notes", headers=bearer, json={"title": "T", "body": "b"}).status_code == 403
    listed = owner.get("/v1/account/tokens").json()["result"]["tokens"]
    assert [t["name"] for t in listed] == ["old-token", "backup-script"] and made["secret"] not in str(listed)

    assert owner.post("/v1/account/tokens", json={"name": "backup-script", "scopes": ["read"], "vault": main, "password": GOOD}).status_code == 400, "name already in use"
    assert owner.post("/v1/account/tokens", json={"name": "x", "scopes": ["read", "fly"], "vault": main, "password": GOOD}).status_code == 400
    assert owner.post("/v1/account/tokens", json={"name": "x", "scopes": ["read"], "vault": "not-mine", "password": GOOD}).status_code == 404

    assert owner.delete(f"/v1/account/tokens/{made['token']['id']}").status_code == 200
    assert script.get("/v1/export", headers=bearer).status_code == 401
    assert owner.delete(f"/v1/account/tokens/{made['token']['id']}").status_code == 404


def test_tokens_are_each_persons_own_and_cannot_exceed_their_role(house):
    owner = house.signed_in("owner")
    main = owner.get("/v1/session").json()["result"]["vault"]["id"]
    house.store.add_account("sam")
    server.create_vault(house.store, "Sam's notes", "sam")
    house.store.grant("sam", main, "viewer")
    sam = house.signed_in("sam")
    own = sam.get("/v1/session").json()["result"]["vault"]["id"]

    assert sam.get("/v1/account/tokens").json()["result"]["tokens"] == [], "Sam does not see the owner's tokens"
    # The same name as one of the owner's is fine: names are per person.
    mine = sam.post("/v1/account/tokens", json={"name": "old-token", "scopes": ["read", "write"], "vault": own, "password": GOOD}).json()["result"]
    assert mine["token"]["vault_name"] == "Sam's notes"
    # In the shared vault Sam is a viewer, so no token of Sam's may write there.
    refused = sam.post("/v1/account/tokens", json={"name": "sneaky", "scopes": ["read", "write"], "vault": main, "password": GOOD})
    assert refused.status_code == 400 and "write" in refused.json()["error"]
    reading = sam.post("/v1/account/tokens", json={"name": "reader", "scopes": ["read", "reveal"], "vault": main, "password": GOOD}).json()["result"]
    script = TestClient(house.app, base_url="https://testserver")
    assert script.get("/v1/library/notes", headers={"Authorization": f"Bearer {reading['secret']}"}).status_code == 200

    owner_tokens = owner.get("/v1/account/tokens").json()["result"]["tokens"]
    assert [t["name"] for t in owner_tokens] == ["old-token"]
    assert sam.delete(f"/v1/account/tokens/{owner_tokens[0]['id']}").status_code == 404, "nobody revokes another person's token"
    assert owner.delete(f"/v1/account/tokens/{mine['token']['id']}").status_code == 404
    assert script.get("/v1/session", headers={"Authorization": f"Bearer {house.old_token}"}).status_code == 200

    # A token made in the web app is still only a token: it cannot manage the account.
    assert script.get("/v1/account/tokens", headers={"Authorization": f"Bearer {mine['secret']}"}).status_code == 403
    assert script.post("/v1/account/tokens", headers={"Authorization": f"Bearer {mine['secret']}"}, json={"name": "y", "scopes": ["read"], "vault": own}).status_code == 403


# --- inviting someone new, picking who to share with, and the name a person signs in with

def test_an_administrator_invites_someone_new_straight_into_their_vault(house):
    owner = house.signed_in("owner")
    assert owner.post("/v1/library/notes", json={"title": "Household page", "body": "b"}).status_code == 200
    main = owner.get("/v1/session").json()["result"]["vault"]["id"]
    assert owner.get("/v1/vault/members").json()["result"]["can_invite"] is True

    made = owner.post("/v1/vault/invitations", json={"name": " Sam Lee ", "role": "editor"}).json()["result"]
    assert made["account"] == {"login": "samlee", "name": "Sam Lee"}, "the sign-in name is made from their name"
    assert [(m["login"], m["role"]) for m in made["members"]] == [("owner", "owner"), ("samlee", "editor")]
    assert GOOD not in str(made) and "password" not in str(made).lower()

    sam = house.use_link(made["setup_path"])
    session = sam.get("/v1/session").json()["result"]
    assert session["vault"] == {"id": main, "name": "Main", "role": "editor"}, "the vault they were invited into is the one that opens"
    assert sorted((v["name"], v["role"]) for v in session["vaults"]) == [("Main", "editor"), ("My notes", "owner")], "with a vault of their own beside it"
    assert [n["title"] for n in sam.get("/v1/library/notes").json()["result"]] == ["Household page"]

    # A second person of the same name gets a name of their own; a chosen name is kept as typed.
    assert owner.post("/v1/vault/invitations", json={"name": "Sam Lee", "role": "viewer"}).json()["result"]["account"]["login"] == "samlee2"
    assert owner.post("/v1/vault/invitations", json={"name": "Jo", "role": "viewer", "login": "Jo@Example.com"}).json()["result"]["account"]["login"] == "jo@example.com"
    assert owner.post("/v1/vault/invitations", json={"name": "Jo", "role": "viewer", "login": "jo@example.com"}).status_code == 409
    assert owner.post("/v1/vault/invitations", json={"name": "Jo", "role": "viewer", "login": "not a name"}).status_code == 400
    assert owner.post("/v1/vault/invitations", json={"name": "   ", "role": "viewer"}).status_code == 400
    assert owner.post("/v1/vault/invitations", json={"name": "Jo", "role": "boss"}).status_code == 400


def test_only_an_administrator_who_owns_the_vault_may_invite(house):
    owner = house.signed_in("owner")
    made = owner.post("/v1/admin/people", json={"login": "sam", "name": "Sam"}).json()["result"]
    sam = house.use_link(made["setup_path"])
    assert sam.get("/v1/vault/members").json()["result"]["can_invite"] is False
    refused = sam.post("/v1/vault/invitations", json={"name": "Friend", "role": "viewer"})
    assert refused.status_code == 403 and "administrator" in refused.text, "an owner who is no administrator cannot make accounts"
    assert house.store.account_named("friend") is None

    assert owner.post("/v1/vault/members", json={"login": "sam", "role": "editor"}).status_code == 200
    main = owner.get("/v1/session").json()["result"]["vault"]["id"]
    assert sam.post("/v1/session/vault", json={"vault": main}).status_code == 200
    assert sam.post("/v1/vault/invitations", json={"name": "Friend", "role": "viewer"}).status_code == 403, "nor may an editor"
    token = {"Authorization": f"Bearer {house.old_token}"}
    assert house.browser().post("/v1/vault/invitations", json={"name": "Friend", "role": "viewer"}, headers=token).status_code == 403


def test_an_owner_picks_who_to_share_with_only_while_accounts_are_by_invitation(house):
    owner = house.signed_in("owner")
    house.store.add_account("zed", "Zed")
    house.store.add_account("amy", "Amy")
    house.store.add_account("gone", "Gone")
    house.store.set_disabled("gone", True)

    shown = owner.get("/v1/vault/members").json()["result"]
    assert shown["others"] == [{"login": "amy", "name": "Amy"}, {"login": "zed", "name": "Zed"}], "by name; nobody switched off, nobody already in"
    shared = owner.post("/v1/vault/members", json={"login": "amy", "role": "viewer"}).json()["result"]
    assert [person["login"] for person in shared["others"]] == ["zed"]
    back = owner.delete(f"/v1/vault/members/{house.store.account('amy')['id']}").json()["result"]
    assert [person["login"] for person in back["others"]] == ["amy", "zed"]

    missing = owner.post("/v1/vault/members", json={"login": "nobody", "role": "viewer"})
    assert missing.status_code == 404 and "need an account" in missing.text

    # With sign-up open the people here are strangers to each other: nobody is listed.
    assert owner.post("/v1/admin/sign-up", json={"signup": "open"}).status_code == 200
    assert owner.get("/v1/vault/members").json()["result"]["others"] == []


def test_a_person_chooses_their_sign_in_name_when_they_first_set_up(house):
    fresh = house.browser()
    code = house.store.new_setup_code("owner")
    assert fresh.post("/v1/setup/check", json={"code": code}).json()["result"] == {"login": "owner", "name": "Owner", "fresh": True, "named": False}

    assert fresh.post("/v1/setup", json={"code": code, "password": GOOD, "login": "not a name"}).status_code == 400
    house.store.add_account("taken")
    assert fresh.post("/v1/setup", json={"code": code, "password": GOOD, "login": "Taken"}).status_code == 409
    assert fresh.post("/v1/setup/check", json={"code": code}).status_code == 200, "a refused name leaves the link usable"

    done = fresh.post("/v1/setup", json={"code": code, "password": GOOD, "name": "Alex Doe", "login": " Alex "})
    assert done.status_code == 200, done.text
    assert done.json()["result"]["account"]["login"] == "alex" and done.json()["result"]["account"]["name"] == "Alex Doe"
    assert house.store.account_named("owner") is None and house.store.account_named("alex")["admin"] is True
    assert house.browser().post("/v1/sign-in", json={"login": "alex", "password": GOOD}).status_code == 200
    assert house.browser().post("/v1/sign-in", json={"login": "owner", "password": GOOD}).status_code == 401

    # Once the account is in use its name is no longer the link holder's to change.
    again = house.store.new_setup_code("alex")
    assert fresh.post("/v1/setup/check", json={"code": again}).json()["result"] == {"login": "alex", "name": "Alex Doe", "fresh": False, "named": True}
    assert fresh.post("/v1/setup", json={"code": again, "password": GOOD, "login": "mallory"}).status_code == 409
    assert fresh.post("/v1/setup", json={"code": again, "password": GOOD, "login": "ALEX"}).status_code == 200, "the same name is no change"


def test_a_new_installations_administrator_is_shown_what_is_left_to_do(house):
    owner = house.signed_in("owner")
    house.store.add_account("sam")
    sam = house.signed_in("sam")
    assert sam.get("/v1/admin/setup").status_code == 403 and sam.post("/v1/admin/setup", json={"hidden": True}).status_code == 403

    def steps(answer):
        return {item["id"]: item["done"] for item in answer.json()["result"]["items"]}

    first = owner.get("/v1/admin/setup")
    assert first.json()["result"]["show"] is True
    assert steps(first) == {"key": False, "first": False, "chrome": False, "invite": True, "google": False}
    assert {item["id"] for item in first.json()["result"]["items"] if item.get("optional")} == {"chrome", "invite", "google"}

    assert owner.post("/v1/library/notes", json={"title": "First", "body": "b"}).status_code == 200
    assert steps(owner.get("/v1/admin/setup")) == {"key": False, "first": True, "chrome": False, "invite": True, "google": False}

    # Connecting the extension is seen for itself; the key is the administrator's word.
    me = house.store.account_named("owner")["id"]
    house.tokens.create("Chrome extension", ["read"], account=me, vault=owner.get("/v1/session").json()["result"]["vault"]["id"])
    assert steps(owner.get("/v1/admin/setup"))["chrome"] is True and owner.get("/v1/admin/setup").json()["result"]["show"] is True
    done = owner.post("/v1/admin/setup", json={"key_saved": True})
    assert steps(done) == {"key": True, "first": True, "chrome": True, "invite": True, "google": False}
    assert done.json()["result"]["show"] is False, "nothing is left but an optional step"

    # Put away, it stays away even with steps left; and it can be brought back.
    assert owner.post("/v1/admin/setup", json={"key_saved": False}).json()["result"]["show"] is True
    assert owner.post("/v1/admin/setup", json={"hidden": True}).json()["result"]["show"] is False
    assert owner.get("/v1/admin/setup").json()["result"]["show"] is False
    assert owner.post("/v1/admin/setup", json={"hidden": False}).json()["result"]["show"] is True
