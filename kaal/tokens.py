"""Bearer tokens for kaal-server.

A token looks like ``kaal_<id>_<secret>``. The token file keeps the id, a
name, the scopes and a SHA-256 of the secret, never the secret itself. The
secrets are 256-bit random values, so a fast hash is enough. Tokens are
issued and revoked with ``kaal-server token …`` on the server host.

Scopes:
  read    metadata: list, search, redacted views, status, verify
  reveal  plaintext: full note bodies, field values, attachment contents,
          extracted text, and medical receipt fields
  write   create, edit, attach, capture, extract, trash and restore
  delete  permanent deletion and purges
  admin   whole-vault export
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCOPES = ("read", "reveal", "write", "delete", "admin")
PREFIX = "kaal"


class TokenError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def has_scopes(record: dict[str, Any], required: Iterable[str]) -> bool:
    return set(required) <= set(record.get("scopes", []))


def write_secret_file(path: str | os.PathLike[str], secret: str) -> Path:
    """Write a token to a new owner-only file; never overwrite an existing one."""
    out = Path(path).expanduser()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(secret + "\n")
    return out


class TokenStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._cache: tuple[int, list[dict[str, Any]]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return []
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise TokenError(f"Refusing token file {self.path}: its permissions let others change it (want 600).")
        if self._cache and self._cache[0] == info.st_mtime_ns:
            return self._cache[1]
        data = json.loads(self.path.read_text(encoding="utf-8"))
        records = list(data.get("tokens", []))
        self._cache = (info.st_mtime_ns, records)
        return records

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tokens.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "tokens": records}, handle, indent=2)
                handle.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._cache = None

    def create(self, name: str, scopes: Iterable[str]) -> tuple[dict[str, Any], str]:
        name = name.strip()
        requested = {s.strip() for s in scopes if s.strip()}
        wanted = [s for s in SCOPES if s in requested]
        if not name:
            raise TokenError("A token needs a name.")
        unknown = sorted(requested - set(SCOPES))
        if unknown:
            raise TokenError(f"Unknown scope(s): {', '.join(unknown)}. Choose from: {', '.join(SCOPES)}.")
        if not wanted:
            raise TokenError("A token needs at least one scope.")
        with self._lock:
            records = [dict(r) for r in self._load()]
            if any(r["name"] == name and not r.get("revoked_at") for r in records):
                raise TokenError(f"An active token named {name!r} already exists; revoke it first.")
            token_id = secrets.token_hex(4)
            while any(r["id"] == token_id for r in records):
                token_id = secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            record = {"id": token_id, "name": name, "scopes": wanted, "created": _now(), "revoked_at": None, "sha256": _digest(secret)}
            records.append(record)
            self._save(records)
        return self._public(record), f"{PREFIX}_{token_id}_{secret}"

    def revoke(self, name_or_id: str) -> dict[str, Any]:
        with self._lock:
            records = [dict(r) for r in self._load()]
            active = [r for r in records if not r.get("revoked_at") and name_or_id in (r["id"], r["name"])]
            if not active:
                raise TokenError(f"No active token named or with id {name_or_id!r}.")
            for record in active:
                record["revoked_at"] = _now()
            self._save(records)
        return self._public(active[0])

    def list(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self._load()]

    def authenticate(self, presented: str) -> dict[str, Any] | None:
        parts = (presented or "").split("_", 2)
        if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
            return None
        records = self._load()
        match = next((r for r in records if r["id"] == parts[1] and not r.get("revoked_at")), None)
        expected = match["sha256"] if match else _digest("no such token")
        if not hmac.compare_digest(expected, _digest(parts[2])) or match is None:
            return None
        return self._public(match)

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in record.items() if k != "sha256"}
