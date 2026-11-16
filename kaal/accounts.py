"""The people and vaults of one installation.

``accounts.db`` (SQLite) sits beside the vaults and records who has an
account, which vaults exist and who belongs to each, with what role. Notes,
files and keys never go in here: a vault is a folder that is complete without
this file, so losing ``accounts.db`` loses who may open what, never the data.

Roles cap what any credential of that person may do in that vault:

  owner   everything, including whole-vault export
  editor  read, reveal, write and delete
  viewer  read and reveal
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROLES = ("owner", "editor", "viewer")
ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    "owner": ("read", "reveal", "write", "delete", "admin"),
    "editor": ("read", "reveal", "write", "delete"),
    "viewer": ("read", "reveal"),
}
_LOGIN = re.compile(r"[a-z0-9][a-z0-9._@+-]{0,63}")

# Each entry takes the store from the version before it to its own.
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE accounts (
        id TEXT PRIMARY KEY,
        login TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL,
        disabled_at TEXT
    );
    CREATE TABLE vaults (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        folder TEXT NOT NULL UNIQUE,
        created TEXT NOT NULL
    );
    CREATE TABLE memberships (
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
        created TEXT NOT NULL,
        PRIMARY KEY (account_id, vault_id)
    );
    """,
    # 2: signing in as a person.
    """
    CREATE TABLE passwords (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        hash TEXT NOT NULL,
        changed TEXT NOT NULL
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        secret_sha256 TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        vault_id TEXT REFERENCES vaults(id) ON DELETE SET NULL,
        label TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        expires TEXT NOT NULL,
        ended_at TEXT
    );
    CREATE INDEX sessions_by_account ON sessions (account_id);
    CREATE TABLE recovery_codes (
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        code_sha256 TEXT NOT NULL,
        created TEXT NOT NULL,
        used_at TEXT,
        PRIMARY KEY (account_id, code_sha256)
    );
    CREATE TABLE setup_codes (
        code_sha256 TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        created TEXT NOT NULL,
        expires TEXT NOT NULL,
        used_at TEXT
    );
    """,
)
SCHEMA_VERSION = len(_MIGRATIONS)
SESSION_PREFIX = "ks"
SESSION_DAYS = 30
SESSION_TOUCH_SECONDS = 300   # how stale "last seen" may get before it is written again
SETUP_HOURS = 24
RECOVERY_CODE_COUNT = 8


class AccountError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _later(**delta: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tidy_recovery_code(code: str) -> str:
    """Recovery codes are typed by hand: ignore case, spaces and dashes."""
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


def _new_id() -> str:
    return secrets.token_hex(6)


def clean_login(login: str) -> str:
    cleaned = (login or "").strip().lower()
    if not _LOGIN.fullmatch(cleaned):
        raise AccountError("A sign-in name is 1 to 64 characters: letters, digits and . _ @ + - , starting with a letter or digit.")
    return cleaned


class AccountStore:
    def __init__(self, path: str | os.PathLike[str], home: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self.home = Path(home).expanduser()

    # --- the file

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._prepare()
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                # Taken up front, so two writers queue instead of one failing half-way.
                connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                if write:
                    connection.execute("ROLLBACK")
                raise
            if write:
                connection.execute("COMMIT")
        finally:
            connection.close()

    def _prepare(self) -> None:
        """Create the file owner-only and bring its tables up to date."""
        stamp = self._stamp()
        if stamp is not None and getattr(self, "_ready", None) == stamp:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                connection.execute("ROLLBACK")
                raise AccountError(f"{self.path} was written by a newer Kaal (its version {version}, this one understands {SCHEMA_VERSION}).")
            for number in range(version, SCHEMA_VERSION):
                for statement in _MIGRATIONS[number].split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {number + 1}")
            connection.execute("COMMIT")
        finally:
            connection.close()
        self._ready = self._stamp()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return None
        return (info.st_ino, info.st_dev)

    def exists(self) -> bool:
        return self.path.is_file()

    # --- small settings

    def get(self, key: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- accounts

    @staticmethod
    def _account(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"id": row["id"], "login": row["login"], "name": row["name"], "admin": bool(row["is_admin"]),
                "created": row["created"], "disabled_at": row["disabled_at"]}

    def add_account(self, login: str, name: str = "", *, admin: bool = False) -> dict[str, Any]:
        login = clean_login(login)
        with self._db(write=True) as db:
            return self._add_account(db, login, name, admin)

    def _add_account(self, db: sqlite3.Connection, login: str, name: str, admin: bool) -> dict[str, Any]:
        if db.execute("SELECT 1 FROM accounts WHERE login = ?", (login,)).fetchone():
            raise AccountError(f"There is already an account with the sign-in name {login!r}.")
        account_id = _new_id()
        db.execute("INSERT INTO accounts (id, login, name, is_admin, created) VALUES (?, ?, ?, ?, ?)",
                   (account_id, login, (name or "").strip() or login, int(admin), _now()))
        return self._account(db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    def account(self, id_or_login: str) -> dict[str, Any] | None:
        with self._db() as db:
            return self._account(db.execute("SELECT * FROM accounts WHERE id = ? OR login = ?", (id_or_login, id_or_login)).fetchone())

    def accounts(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [self._account(row) for row in db.execute("SELECT * FROM accounts ORDER BY rowid")]

    def set_disabled(self, id_or_login: str, disabled: bool) -> dict[str, Any]:
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM accounts WHERE id = ? OR login = ?", (id_or_login, id_or_login)).fetchone()
            if row is None:
                raise AccountError(f"No account {id_or_login!r}.")
            if disabled and row["is_admin"] and not db.execute(
                    "SELECT 1 FROM accounts WHERE is_admin = 1 AND disabled_at IS NULL AND id != ?", (row["id"],)).fetchone():
                raise AccountError("This is the only working administrator; make another one first.")
            db.execute("UPDATE accounts SET disabled_at = ? WHERE id = ?", (_now() if disabled else None, row["id"]))
            return self._account(db.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone())

    # --- vaults

    def _folder_text(self, folder: str | os.PathLike[str]) -> str:
        """Folders inside the installation are stored relative to it, so it can be moved."""
        path = Path(folder).expanduser()
        try:
            return path.resolve().relative_to(self.home.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def folder_of(self, vault: dict[str, Any]) -> Path:
        return self.home / vault["folder"]

    @staticmethod
    def _vault(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else {"id": row["id"], "name": row["name"], "folder": row["folder"], "created": row["created"]}

    def add_vault(self, name: str, folder: str | os.PathLike[str] | None = None, *, owner: str) -> dict[str, Any]:
        """Record a vault and its first owner. Without ``folder`` it is given one under ``vaults/``."""
        with self._db(write=True) as db:
            return self._add_vault(db, name, folder, owner)

    def _add_vault(self, db: sqlite3.Connection, name: str, folder: str | os.PathLike[str] | None, owner: str) -> dict[str, Any]:
        account = db.execute("SELECT id FROM accounts WHERE id = ? OR login = ?", (owner, owner)).fetchone()
        if account is None:
            raise AccountError(f"No account {owner!r}.")
        vault_id = _new_id()
        text = self._folder_text(folder) if folder is not None else f"vaults/{vault_id}"
        if db.execute("SELECT 1 FROM vaults WHERE folder = ?", (text,)).fetchone():
            raise AccountError("That folder already belongs to a vault.")
        now = _now()
        db.execute("INSERT INTO vaults (id, name, folder, created) VALUES (?, ?, ?, ?)", (vault_id, (name or "").strip() or "Vault", text, now))
        db.execute("INSERT INTO memberships (account_id, vault_id, role, created) VALUES (?, ?, 'owner', ?)", (account["id"], vault_id, now))
        return self._vault(db.execute("SELECT * FROM vaults WHERE id = ?", (vault_id,)).fetchone())

    def remove_vault_record(self, vault_id: str) -> None:
        """Forget a vault that was recorded but whose folder could not be made."""
        with self._db(write=True) as db:
            db.execute("DELETE FROM vaults WHERE id = ?", (vault_id,))

    def vault(self, vault_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            return self._vault(db.execute("SELECT * FROM vaults WHERE id = ?", (vault_id,)).fetchone())

    def vaults(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [self._vault(row) for row in db.execute("SELECT * FROM vaults ORDER BY rowid")]

    # --- who belongs to which vault

    def grant(self, account: str, vault_id: str, role: str) -> dict[str, Any]:
        if role not in ROLES:
            raise AccountError(f"A role is one of: {', '.join(ROLES)}.")
        with self._db(write=True) as db:
            person = db.execute("SELECT id FROM accounts WHERE id = ? OR login = ?", (account, account)).fetchone()
            if person is None:
                raise AccountError(f"No account {account!r}.")
            if not db.execute("SELECT 1 FROM vaults WHERE id = ?", (vault_id,)).fetchone():
                raise AccountError(f"No vault {vault_id!r}.")
            self._keep_an_owner(db, person["id"], vault_id, role)
            db.execute("INSERT INTO memberships (account_id, vault_id, role, created) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(account_id, vault_id) DO UPDATE SET role = excluded.role", (person["id"], vault_id, role, _now()))
        return {"account": person["id"], "vault": vault_id, "role": role}

    def ungrant(self, account: str, vault_id: str) -> None:
        with self._db(write=True) as db:
            person = db.execute("SELECT id FROM accounts WHERE id = ? OR login = ?", (account, account)).fetchone()
            if person is None or not db.execute("SELECT 1 FROM memberships WHERE account_id = ? AND vault_id = ?", (person["id"], vault_id)).fetchone():
                raise AccountError("That account does not belong to that vault.")
            self._keep_an_owner(db, person["id"], vault_id, None)
            db.execute("DELETE FROM memberships WHERE account_id = ? AND vault_id = ?", (person["id"], vault_id))

    @staticmethod
    def _keep_an_owner(db: sqlite3.Connection, account_id: str, vault_id: str, new_role: str | None) -> None:
        if new_role == "owner":
            return
        current = db.execute("SELECT role FROM memberships WHERE account_id = ? AND vault_id = ?", (account_id, vault_id)).fetchone()
        if current is None or current["role"] != "owner":
            return
        if not db.execute("SELECT 1 FROM memberships WHERE vault_id = ? AND role = 'owner' AND account_id != ?", (vault_id, account_id)).fetchone():
            raise AccountError("A vault must keep at least one owner.")

    def members(self, vault_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT a.id, a.login, a.name, m.role FROM memberships m JOIN accounts a ON a.id = m.account_id "
                              "WHERE m.vault_id = ? ORDER BY m.rowid", (vault_id,))
            return [{"account": row["id"], "login": row["login"], "name": row["name"], "role": row["role"]} for row in rows]

    def vaults_of(self, account_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT v.*, m.role FROM memberships m JOIN vaults v ON v.id = m.vault_id "
                              "WHERE m.account_id = ? ORDER BY m.rowid", (account_id,))
            return [{**self._vault(row), "role": row["role"]} for row in rows]

    def standing(self, account_id: str, vault_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """One look-up for a request: the account (None if missing) and its role in the vault (None if not a member)."""
        with self._db() as db:
            row = db.execute(
                "SELECT a.*, (SELECT role FROM memberships m WHERE m.account_id = a.id AND m.vault_id = ?) AS role FROM accounts a WHERE a.id = ?",
                (vault_id, account_id)).fetchone()
        return (self._account(row), row["role"]) if row is not None else (None, None)

    # --- the first account and vault

    def adopt(self, vault_folder: str | os.PathLike[str]) -> dict[str, str] | None:
        """Turn a single-vault installation into one with accounts, once.

        Records an administrator and the existing vault as theirs. Credentials
        that name no account or vault keep working as that person in that vault.
        Returns None when there was nothing to do.
        """
        with self._db(write=True) as db:
            if db.execute("SELECT 1 FROM accounts").fetchone():
                return None
            account = self._add_account(db, "owner", "Owner", True)
            vault = self._add_vault(db, "Main", vault_folder, account["id"])
            self._set(db, "default_account", account["id"])
            self._set(db, "default_vault", vault["id"])
            return {"account": account["id"], "vault": vault["id"]}

    def defaults(self) -> tuple[str | None, str | None]:
        """The account and vault that credentials without their own belong to."""
        with self._db() as db:
            found = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta WHERE key IN ('default_account', 'default_vault')")}
        return found.get("default_account"), found.get("default_vault")

    # --- passwords

    def set_password(self, account_id: str, password_hash: str) -> None:
        with self._db(write=True) as db:
            db.execute("INSERT INTO passwords (account_id, hash, changed) VALUES (?, ?, ?) "
                       "ON CONFLICT(account_id) DO UPDATE SET hash = excluded.hash, changed = excluded.changed", (account_id, password_hash, _now()))

    def password_hash(self, account_id: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT hash FROM passwords WHERE account_id = ?", (account_id,)).fetchone()
        return row["hash"] if row else None

    # --- sessions: a browser signed in as a person

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "account": row["account_id"], "vault": row["vault_id"], "label": row["label"],
                "created": row["created"], "last_seen": row["last_seen"], "expires": row["expires"]}

    def start_session(self, account_id: str, vault_id: str | None, label: str = "") -> tuple[dict[str, Any], str]:
        """A new session and the cookie value for it, which is never stored."""
        session_id, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
        now = _now()
        with self._db(write=True) as db:
            db.execute("INSERT INTO sessions (id, secret_sha256, account_id, vault_id, label, created, last_seen, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (session_id, _sha(secret), account_id, vault_id, (label or "")[:80], now, now, _later(days=SESSION_DAYS)))
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session(row), f"{SESSION_PREFIX}_{session_id}_{secret}"

    def session(self, presented: str) -> dict[str, Any] | None:
        """The live session a cookie value belongs to, or None."""
        parts = (presented or "").split("_", 2)
        if len(parts) != 3 or parts[0] != SESSION_PREFIX or not parts[1] or not parts[2]:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id = ? AND ended_at IS NULL", (parts[1],)).fetchone()
        expected = row["secret_sha256"] if row else _sha("no such session")
        if not hmac.compare_digest(expected, _sha(parts[2])) or row is None or row["expires"] <= _now():
            return None
        if row["last_seen"] <= _later(seconds=-SESSION_TOUCH_SECONDS):
            with self._db(write=True) as db:
                db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (_now(), row["id"]))
        return self._session(row)

    def sessions_of(self, account_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM sessions WHERE account_id = ? AND ended_at IS NULL AND expires > ? ORDER BY rowid", (account_id, _now()))
            return [self._session(row) for row in rows]

    def end_session(self, session_id: str, account_id: str | None = None) -> bool:
        """End one session; with ``account_id``, only if it is that person's."""
        with self._db(write=True) as db:
            done = db.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL AND (? IS NULL OR account_id = ?)",
                              (_now(), session_id, account_id, account_id))
            return done.rowcount > 0

    def end_sessions(self, account_id: str, *, keep: str | None = None) -> int:
        with self._db(write=True) as db:
            done = db.execute("UPDATE sessions SET ended_at = ? WHERE account_id = ? AND ended_at IS NULL AND id IS NOT ?", (_now(), account_id, keep))
            return done.rowcount

    def set_session_vault(self, session_id: str, vault_id: str) -> None:
        with self._db(write=True) as db:
            db.execute("UPDATE sessions SET vault_id = ? WHERE id = ?", (vault_id, session_id))

    # --- recovery codes: a way back in when the password is forgotten

    def new_recovery_codes(self, account_id: str) -> list[str]:
        """Replace the person's recovery codes and return the new ones, shown once."""
        raw = [secrets.token_hex(6) for _ in range(RECOVERY_CODE_COUNT)]
        now = _now()
        with self._db(write=True) as db:
            db.execute("DELETE FROM recovery_codes WHERE account_id = ?", (account_id,))
            db.executemany("INSERT INTO recovery_codes (account_id, code_sha256, created) VALUES (?, ?, ?)", [(account_id, _sha(code), now) for code in raw])
        return [f"{code[:4]}-{code[4:8]}-{code[8:]}" for code in raw]

    def use_recovery_code(self, account_id: str, code: str) -> bool:
        """Spend a recovery code; each works once."""
        with self._db(write=True) as db:
            done = db.execute("UPDATE recovery_codes SET used_at = ? WHERE account_id = ? AND code_sha256 = ? AND used_at IS NULL",
                              (_now(), account_id, _sha(tidy_recovery_code(code))))
            return done.rowcount > 0

    def recovery_codes_left(self, account_id: str) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM recovery_codes WHERE account_id = ? AND used_at IS NULL", (account_id,)).fetchone()[0]

    # --- setup codes: a single-use link for choosing a password

    def new_setup_code(self, id_or_login: str, *, hours: float = SETUP_HOURS) -> str:
        """A code that lets whoever holds it choose this account's password, once."""
        code = secrets.token_urlsafe(24)
        with self._db(write=True) as db:
            row = db.execute("SELECT id FROM accounts WHERE id = ? OR login = ?", (id_or_login, id_or_login)).fetchone()
            if row is None:
                raise AccountError(f"No account {id_or_login!r}.")
            # Asking for a new link cancels any earlier one for the same account.
            db.execute("DELETE FROM setup_codes WHERE account_id = ?", (row["id"],))
            db.execute("INSERT INTO setup_codes (code_sha256, account_id, created, expires) VALUES (?, ?, ?, ?)", (_sha(code), row["id"], _now(), _later(hours=hours)))
        return code

    def setup_account(self, code: str, *, spend: bool = False) -> dict[str, Any] | None:
        """The account a setup code is for, while it is unused and in date. ``spend`` uses it up."""
        with self._db(write=spend) as db:
            row = db.execute("SELECT a.* FROM setup_codes c JOIN accounts a ON a.id = c.account_id "
                             "WHERE c.code_sha256 = ? AND c.used_at IS NULL AND c.expires > ? AND a.disabled_at IS NULL", (_sha(code or ""), _now())).fetchone()
            if row is not None and spend:
                db.execute("UPDATE setup_codes SET used_at = ? WHERE code_sha256 = ?", (_now(), _sha(code)))
            return self._account(row)

    def set_login(self, id_or_login: str, new_login: str) -> dict[str, Any]:
        """Change the name a person signs in with."""
        new_login = clean_login(new_login)
        with self._db(write=True) as db:
            row = db.execute("SELECT id FROM accounts WHERE id = ? OR login = ?", (id_or_login, id_or_login)).fetchone()
            if row is None:
                raise AccountError(f"No account {id_or_login!r}.")
            if db.execute("SELECT 1 FROM accounts WHERE login = ? AND id != ?", (new_login, row["id"])).fetchone():
                raise AccountError(f"There is already an account with the sign-in name {new_login!r}.")
            db.execute("UPDATE accounts SET login = ? WHERE id = ?", (new_login, row["id"]))
            return self._account(db.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone())

    def rename(self, account_id: str, name: str) -> None:
        cleaned = (name or "").strip()[:100]
        if cleaned:
            with self._db(write=True) as db:
                db.execute("UPDATE accounts SET name = ? WHERE id = ?", (cleaned, account_id))
