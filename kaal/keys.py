"""Vault key retrieval.

Kaal encrypts everything with one 32-byte AES-256 key, stored as base64 text in
a file that only its owner can read, named by ``KAAL_KEY_FILE``. The key is
never kept in the vault and never included in a backup.

Nothing in this module prints, logs or embeds key material in an error message.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

KEY_BYTES = 32
MAX_KEY_FILE_BYTES = 4096


class KeyProviderError(Exception):
    """The vault key is missing, unreadable, malformed or unsafely stored."""


class KeyProvider(Protocol):
    name: str

    def get_key(self, create: bool = False) -> bytes: ...

    def key_exists(self) -> bool: ...

    def describe(self) -> dict[str, str]: ...


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def encode_key(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def decode_key(text: str, *, source: str) -> bytes:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeyProviderError(f"{source} exists but is not a valid key (expected base64 text).") from None
    if len(key) != KEY_BYTES:
        raise KeyProviderError(f"{source} has the wrong size ({len(key)} bytes); expected {KEY_BYTES}.")
    return key


class FileKeyProvider:
    name = "file"

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    def describe(self) -> dict[str, str]:
        return {"provider": self.name, "path": str(self.path)}

    def key_exists(self) -> bool:
        return self.path.is_file() and not self.path.is_symlink()

    def get_key(self, create: bool = False) -> bytes:
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            if create:
                return self._create()
            raise KeyProviderError(f"Key file not found: {self.path}") from None
        except OSError as error:
            if self.path.is_symlink():
                raise KeyProviderError(f"Key file must be a regular file, not a symlink: {self.path}") from None
            raise KeyProviderError(f"Could not open key file {self.path}: {error.strerror}") from None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise KeyProviderError(f"Key file must be a regular file: {self.path}")
            if info.st_mode & 0o077:
                raise KeyProviderError(
                    f"Key file {self.path} must be readable only by its owner (mode 600); it is {stat.S_IMODE(info.st_mode):o}."
                )
            euid = os.geteuid()
            if euid != 0 and info.st_uid != euid:
                raise KeyProviderError(f"Key file {self.path} must be owned by the user running Kaal.")
            data = handle.read(MAX_KEY_FILE_BYTES + 1)
        if len(data) > MAX_KEY_FILE_BYTES:
            raise KeyProviderError(f"Key file {self.path} is not a valid key (too large).")
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            raise KeyProviderError(f"Key file {self.path} is not a valid key (expected base64 text).") from None
        return decode_key(text, source=f"Key file {self.path}")

    def _create(self) -> bytes:
        if not self.path.parent.is_dir():
            raise KeyProviderError(f"Key file directory does not exist: {self.path.parent}")
        key = new_key()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(encode_key(key) + "\n")
        return key


def provider_from_env(environ: Mapping[str, str] | None = None) -> KeyProvider:
    env = os.environ if environ is None else environ
    choice = (env.get("KAAL_KEY_PROVIDER") or "file").strip().lower()
    if choice == "keychain":
        raise KeyProviderError(
            "Kaal no longer reads its key from the macOS Keychain. Put the key in an owner-only file and name it with KAAL_KEY_FILE."
        )
    if choice != "file":
        raise KeyProviderError("KAAL_KEY_PROVIDER, if set, must be 'file'.")
    path = (env.get("KAAL_KEY_FILE") or "").strip()
    if not path:
        raise KeyProviderError("KAAL_KEY_FILE must name the file that holds the vault key.")
    return FileKeyProvider(path)
