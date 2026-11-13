"""Shared test setup.

Every test runs with a throwaway file key, so no test can read or write the
real macOS Keychain item, whatever machine the suite runs on. The key is set
when this module loads, before any test class's setUpClass runs, and is
re-asserted around every test.
"""
import atexit
import base64
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_KEY_DIR = Path(tempfile.mkdtemp(prefix="kaal-test-key-"))
_KEY_FILE = _KEY_DIR / "master.key"
_KEY_FILE.write_text(base64.b64encode(os.urandom(32)).decode() + "\n", encoding="ascii")
os.chmod(_KEY_FILE, 0o600)
atexit.register(shutil.rmtree, _KEY_DIR, True)

_CLIENT_VARS = ("KAAL_URL", "KAAL_TOKEN", "KAAL_TOKEN_FILE", "KAAL_LOCAL")
# Never read a real ~/.config/kaal/config.toml: it could point tests at a live server.
_NO_CONFIG = _KEY_DIR / "no-config.toml"
os.environ["KAAL_KEY_PROVIDER"] = "file"
os.environ["KAAL_KEY_FILE"] = str(_KEY_FILE)
os.environ["KAAL_CONFIG"] = str(_NO_CONFIG)
for _name in _CLIENT_VARS:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def isolated_vault_key(monkeypatch):
    monkeypatch.setenv("KAAL_KEY_PROVIDER", "file")
    monkeypatch.setenv("KAAL_KEY_FILE", str(_KEY_FILE))
    monkeypatch.setenv("KAAL_CONFIG", str(_NO_CONFIG))
    for name in _CLIENT_VARS:
        monkeypatch.delenv(name, raising=False)
    yield _KEY_FILE
