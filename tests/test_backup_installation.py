"""`kaal backup` on an installation with accounts: every vault and the accounts file are in the copy."""
import os
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

import kaal.core as kaal
from kaal import accounts, server, tokens


def run(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    monkeypatch.delenv("KAAL_HOME", raising=False)
    monkeypatch.delenv("KAAL_TOKENS_FILE", raising=False)
    run(kaal.init_vault)
    run(kaal.library_create, title="Main page", body="in the first vault", tags="", source_url="", source_title="")
    yield tmp_path
    kaal.configure_vault(saved)


def test_a_single_vault_without_accounts_is_backed_up_as_before(home):
    result = run(kaal.backup_vault, dest=str(home / "backups"), keep=3)
    backup = home / "backups" / result["backup"].rsplit("/", 1)[-1]
    assert sorted(p.name for p in backup.iterdir()) == ["attachments", "index.json", "notes"]
    assert "accounts" not in result


def test_every_vault_and_the_accounts_file_are_in_the_backup(home):
    tokens.TokenStore(home / "tokens.json").create("old", ["read"])
    store = server.open_store(server.Settings(home=home, vault=home / "data", tokens_file=home / "tokens.json", tmp_dir=home / "tmp"))
    sam = store.add_account("sam")
    sam_vault = server.create_vault(store, "Sam's notes", sam["id"])
    with kaal.use_vault(kaal.Vault(store.folder_of(sam_vault))):
        run(kaal.library_create, title="Sam's page", body="in the second vault", tags="", source_url="", source_title="")

    first = run(kaal.backup_vault, dest=str(home / "backups"), keep=3)
    assert (first["accounts"], first["other_vaults"]) == (True, 1)
    backup = home / "backups" / first["backup"].rsplit("/", 1)[-1]
    extra = backup / kaal.BACKUP_INSTALLATION_DIR
    # The first vault is where it always was, so an old restore procedure still works.
    assert (backup / "index.json").is_file() and (backup / "notes").is_dir()
    with sqlite3.connect(extra / "accounts.db") as db:
        assert [row[0] for row in db.execute("SELECT login FROM accounts ORDER BY rowid")] == ["owner", "sam"]
    assert (extra / "tokens.json").is_file()
    copied_vault = extra / "vaults" / sam_vault["id"]
    assert (copied_vault / "index.json").is_file() and (copied_vault / kaal.VAULT_KEY_NAME).is_file()
    for path in list(extra.rglob("*")):
        assert (path.stat().st_mode & 0o077) == 0, f"{path.name} is readable by others"

    # The copy of the second vault opens with the installation's key and has its note.
    restored = home / "restored"
    shutil.copytree(copied_vault, restored)
    with kaal.use_vault(kaal.Vault(restored)):
        assert [note["title"] for note in run(kaal.library_list)] == ["Sam's page"]

    # A second backup links what has not changed, in the other vault too.
    import time
    time.sleep(1.1)
    second = run(kaal.backup_vault, dest=str(home / "backups"), keep=3)
    assert second["linked"] >= 4 and second["other_vaults"] == 1
    again = home / "backups" / second["backup"].rsplit("/", 1)[-1] / kaal.BACKUP_INSTALLATION_DIR / "vaults" / sam_vault["id"]
    assert os.stat(again / "index.json").st_ino == os.stat(copied_vault / "index.json").st_ino


def test_the_backup_finds_the_installation_from_kaal_home_too(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    store = accounts.AccountStore(elsewhere / "accounts.db", elsewhere)
    store.adopt(home / "data")
    assert kaal.installation_home() is None
    monkeypatch.setenv("KAAL_HOME", str(elsewhere))
    assert kaal.installation_home() == elsewhere
    assert run(kaal.backup_vault, dest=str(home / "backups"), keep=3)["accounts"] is True
