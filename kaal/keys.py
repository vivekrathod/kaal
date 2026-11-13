"""Vault key retrieval.

Kaal encrypts sensitive notes with one 32-byte AES-256 key, stored as base64
text. Where the key lives is a deployment choice:

* ``keychain`` (default on macOS): a macOS Keychain generic-password item.
* ``file`` (default elsewhere): a file readable only by its owner, named by
  ``KAAL_KEY_FILE``. This is what ``kaal-server`` uses on Linux.

``KAAL_KEY_PROVIDER`` picks one explicitly. Nothing in this module prints,
logs or embeds key material in an error message.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

KEY_BYTES = 32
DEFAULT_KEYCHAIN_SERVICE = "Hermes Secure Notes"
DEFAULT_KEYCHAIN_ACCOUNT = "vault-key-v1"
MAX_KEY_FILE_BYTES = 4096

SecurityRunner = Callable[..., subprocess.CompletedProcess]


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


def _run_security(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    if not shutil.which("security"):
        raise KeyProviderError(
            "macOS security CLI not found. On this system set KAAL_KEY_PROVIDER=file and KAAL_KEY_FILE."
        )
    return subprocess.run(["security", *args], input=input_text, text=True, capture_output=True, check=False)


class KeychainKeyProvider:
    name = "keychain"

    def __init__(
        self,
        service: str = DEFAULT_KEYCHAIN_SERVICE,
        account: str = DEFAULT_KEYCHAIN_ACCOUNT,
        *,
        runner: SecurityRunner | None = None,
        executable: str | None = None,
    ) -> None:
        self.service = service
        self.account = account
        self._run = runner or _run_security
        self._executable = executable or sys.executable

    def describe(self) -> dict[str, str]:
        return {"provider": self.name, "service": self.service, "account": self.account}

    def key_exists(self) -> bool:
        if self._run is _run_security and not shutil.which("security"):
            return False
        # Without -w, `security` reports the item's attributes, never the secret.
        return self._run(["find-generic-password", "-s", self.service, "-a", self.account]).returncode == 0

    def get_key(self, create: bool = False) -> bytes:
        proc = self._run(["find-generic-password", "-s", self.service, "-a", self.account, "-w"])
        if proc.returncode == 0:
            return decode_key(proc.stdout, source="Keychain item")
        if not create:
            raise KeyProviderError("Vault key not found in the Keychain. Run: kaal init")
        key = new_key()
        add = self._run([
            "add-generic-password",
            "-U",
            "-s", self.service,
            "-a", self.account,
            "-w", encode_key(key),
            "-T", "/usr/bin/security",
            "-T", self._executable,
        ])
        if add.returncode != 0:
            raise KeyProviderError(f"Could not store the key in the Keychain: {(add.stderr or add.stdout).strip()}")
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


def provider_from_env(environ: Mapping[str, str] | None = None, *, platform: str | None = None) -> KeyProvider:
    env = os.environ if environ is None else environ
    platform = platform or sys.platform
    choice = (env.get("KAAL_KEY_PROVIDER") or "").strip().lower()
    if not choice:
        choice = "keychain" if platform == "darwin" else "file"
    if choice == "keychain":
        return KeychainKeyProvider(
            service=env.get("KAAL_KEYCHAIN_SERVICE") or DEFAULT_KEYCHAIN_SERVICE,
            account=env.get("KAAL_KEYCHAIN_ACCOUNT") or DEFAULT_KEYCHAIN_ACCOUNT,
        )
    if choice == "file":
        path = (env.get("KAAL_KEY_FILE") or "").strip()
        if not path:
            raise KeyProviderError("KAAL_KEY_FILE must name the key file when the file key provider is used.")
        return FileKeyProvider(path)
    raise KeyProviderError("KAAL_KEY_PROVIDER must be 'keychain' or 'file'.")
