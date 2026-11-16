"""The accounts store: who has an account, which vaults exist, who belongs to which."""
import sqlite3
import stat

import pytest

from kaal import accounts


@pytest.fixture()
def store(tmp_path):
    return accounts.AccountStore(tmp_path / "accounts.db", tmp_path)


def test_the_file_is_owner_only_and_carries_its_schema_version(store, tmp_path):
    store.add_account("jane")
    assert stat.S_IMODE((tmp_path / "accounts.db").stat().st_mode) == 0o600
    with sqlite3.connect(tmp_path / "accounts.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == accounts.SCHEMA_VERSION
    # A second look at the same file changes nothing and sees the same people.
    assert [person["login"] for person in accounts.AccountStore(tmp_path / "accounts.db", tmp_path).accounts()] == ["jane"]


def test_a_store_written_by_a_newer_kaal_is_refused_not_guessed_at(store, tmp_path):
    store.add_account("jane")
    with sqlite3.connect(tmp_path / "accounts.db") as db:
        db.execute(f"PRAGMA user_version = {accounts.SCHEMA_VERSION + 1}")
    with pytest.raises(accounts.AccountError, match="newer Kaal"):
        accounts.AccountStore(tmp_path / "accounts.db", tmp_path).accounts()


def test_sign_in_names_are_tidied_unique_whatever_the_case_and_checked(store):
    jane = store.add_account("  Jane.Doe@Example.com ", "Jane Doe")
    assert (jane["login"], jane["name"], jane["admin"], jane["disabled_at"]) == ("jane.doe@example.com", "Jane Doe", False, None)
    assert store.add_account("sam")["name"] == "sam"
    with pytest.raises(accounts.AccountError, match="already an account"):
        store.add_account("JANE.DOE@example.com")
    for bad in ("", " ", "a b", "-dash", "x" * 65, "semi;colon", "né"):
        with pytest.raises(accounts.AccountError):
            store.add_account(bad)
    assert store.account("Jane.Doe@example.com")["id"] == jane["id"] == store.account(jane["id"])["id"]
    assert store.account("nobody") is None


def test_a_vault_starts_with_its_owner_and_its_folder_moves_with_the_installation(store, tmp_path):
    jane = store.add_account("jane")
    inside = store.add_vault("Notes", owner="jane")
    assert inside["folder"] == f"vaults/{inside['id']}" and store.folder_of(inside) == tmp_path / "vaults" / inside["id"]
    adopted = store.add_vault("Old", tmp_path / "data", owner=jane["id"])
    assert adopted["folder"] == "data"
    outside = store.add_vault("Elsewhere", tmp_path.parent / "elsewhere", owner="jane")
    assert outside["folder"] == str((tmp_path.parent / "elsewhere").resolve())
    assert store.members(inside["id"]) == [{"account": jane["id"], "login": "jane", "name": "jane", "role": "owner"}]
    assert [v["name"] for v in store.vaults_of(jane["id"])] == ["Notes", "Old", "Elsewhere"]
    with pytest.raises(accounts.AccountError, match="already belongs"):
        store.add_vault("Again", tmp_path / "data", owner="jane")
    with pytest.raises(accounts.AccountError, match="No account"):
        store.add_vault("Orphan", owner="nobody")
    assert [v["name"] for v in store.vaults()] == ["Notes", "Old", "Elsewhere"], "a refused vault leaves nothing behind"


def test_roles_can_be_given_changed_and_taken_but_a_vault_keeps_an_owner(store):
    jane, sam = store.add_account("jane"), store.add_account("sam")
    vault = store.add_vault("Household", owner="jane")["id"]
    assert store.standing(sam["id"], vault) == (store.account("sam"), None)
    store.grant("sam", vault, "viewer")
    assert store.standing(sam["id"], vault)[1] == "viewer"
    store.grant("sam", vault, "editor")
    assert [(m["login"], m["role"]) for m in store.members(vault)] == [("jane", "owner"), ("sam", "editor")]
    with pytest.raises(accounts.AccountError, match="role is one of"):
        store.grant("sam", vault, "boss")
    with pytest.raises(accounts.AccountError, match="No vault"):
        store.grant("sam", "nope", "viewer")

    with pytest.raises(accounts.AccountError, match="at least one owner"):
        store.grant("jane", vault, "editor")
    with pytest.raises(accounts.AccountError, match="at least one owner"):
        store.ungrant("jane", vault)
    store.grant("sam", vault, "owner")
    store.ungrant("jane", vault)
    assert [(m["login"], m["role"]) for m in store.members(vault)] == [("sam", "owner")]
    assert store.standing(jane["id"], vault)[1] is None
    with pytest.raises(accounts.AccountError, match="does not belong"):
        store.ungrant("jane", vault)
    assert store.standing("no-such-id", vault) == (None, None)


def test_the_last_working_administrator_cannot_be_switched_off(store):
    store.add_account("jane", admin=True)
    store.add_account("sam")
    assert store.set_disabled("sam", True)["disabled_at"]
    assert store.set_disabled("sam", False)["disabled_at"] is None
    with pytest.raises(accounts.AccountError, match="only working administrator"):
        store.set_disabled("jane", True)
    store.add_account("alex", admin=True)
    assert store.set_disabled("jane", True)["disabled_at"]
    with pytest.raises(accounts.AccountError, match="only working administrator"):
        store.set_disabled("alex", True)
    with pytest.raises(accounts.AccountError, match="No account"):
        store.set_disabled("nobody", True)


def test_an_existing_vault_is_adopted_once_as_the_first_accounts(store, tmp_path):
    assert store.defaults() == (None, None)
    first = store.adopt(tmp_path / "data")
    assert store.defaults() == (first["account"], first["vault"])
    owner = store.account(first["account"])
    assert (owner["login"], owner["admin"]) == ("owner", True)
    assert store.vault(first["vault"])["folder"] == "data"
    assert store.standing(first["account"], first["vault"])[1] == "owner"
    assert store.adopt(tmp_path / "other") is None
    assert len(store.accounts()) == 1 and len(store.vaults()) == 1


def test_what_each_role_may_do():
    assert accounts.ROLE_SCOPES == {
        "owner": ("read", "reveal", "write", "delete", "admin"),
        "editor": ("read", "reveal", "write", "delete"),
        "viewer": ("read", "reveal"),
    }
