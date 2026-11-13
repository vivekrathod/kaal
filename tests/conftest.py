"""Shared test setup.

Every test runs with a throwaway file key, so no test can read or write the
real macOS Keychain item, whatever machine the suite runs on.
"""
import base64
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_vault_key(tmp_path_factory, monkeypatch):
    key_dir = tmp_path_factory.mktemp("kaal-key")
    key_file = key_dir / "master.key"
    if not key_file.exists():
        key_file.write_text(base64.b64encode(os.urandom(32)).decode() + "\n", encoding="ascii")
        os.chmod(key_file, 0o600)
    monkeypatch.setenv("KAAL_KEY_PROVIDER", "file")
    monkeypatch.setenv("KAAL_KEY_FILE", str(key_file))
    for name in ("KAAL_URL", "KAAL_TOKEN", "KAAL_TOKEN_FILE", "KAAL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    yield key_file
