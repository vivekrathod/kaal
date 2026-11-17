"""The container setup: the start-up script's rules about the key, and the files around it."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "deploy" / "docker"
ENTRYPOINT = DOCKER / "entrypoint.sh"

pytestmark = pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")


@pytest.fixture()
def box(tmp_path):
    """A stand-in container: empty volumes, and a `kaal` that makes a key and a vault when told to init."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "kaal").write_text('#!/bin/sh\necho "kaal $*" >> "$CALLS"\n'
                                'if [ "$1" = init ]; then mkdir -p "$KAAL_VAULT" && { [ -s "$KAAL_KEY_FILE" ] || echo made-up-key > "$KAAL_KEY_FILE"; } && echo "{}" > "$KAAL_VAULT/index.json"; fi\n')
    (stubs / "kaal-server").write_text('#!/bin/sh\necho "kaal-server $*" >> "$CALLS"\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    (tmp_path / "key").mkdir()
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "CALLS": str(tmp_path / "calls"), "KAAL_HOME": str(tmp_path / "home"),
           "KAAL_VAULT": str(tmp_path / "home" / "data"), "KAAL_TMP_DIR": str(tmp_path / "home" / "tmp"), "KAAL_KEY_FILE": str(tmp_path / "key" / "master.key")}

    def start(*command):
        done = subprocess.run(["sh", str(ENTRYPOINT), *command], env=env, text=True, capture_output=True)
        calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
        return done, calls

    return tmp_path, start


def test_the_first_start_makes_a_key_and_a_vault_then_serves(box):
    home, start = box
    done, calls = start("kaal-server", "serve")
    assert done.returncode == 0, done.stderr
    assert calls == ["kaal init", "kaal-server serve"]
    assert "Keep a copy of the key" in done.stdout
    assert (home / "key" / "master.key").read_text().strip() == "made-up-key"


def test_a_later_start_makes_nothing_new(box):
    _home, start = box
    start("kaal-server", "serve")
    done, calls = start("kaal-server", "serve")
    assert done.returncode == 0
    assert calls == ["kaal init", "kaal-server serve", "kaal-server serve"]


@pytest.mark.parametrize("what_is_there", ["data/index.json", "accounts.db"])
def test_data_without_its_key_is_never_given_a_new_key(box, what_is_there):
    home, start = box
    there = home / "home" / what_is_there
    there.parent.mkdir(parents=True, exist_ok=True)
    there.write_text("made-up data from before")
    done, calls = start("kaal-server", "serve")
    assert done.returncode == 1
    assert calls == []                                  # neither a new key nor a server
    assert "its key is not" in done.stderr and "never made over existing data" in done.stderr
    assert not (home / "key" / "master.key").exists()


def test_a_key_that_is_already_there_is_kept(box):
    home, start = box
    (home / "key" / "master.key").write_text("the key that was there\n")
    start("kaal-server", "serve")
    assert (home / "key" / "master.key").read_text() == "the key that was there\n"


def test_other_commands_run_as_they_are(box):
    _home, start = box
    done, calls = start("kaal-server", "account", "list")
    assert done.returncode == 0 and calls == ["kaal-server account list"]


@pytest.mark.parametrize("script", ["entrypoint.sh", "install.sh", "kaalctl"])
def test_the_scripts_are_plain_sh(script):
    text = (DOCKER / script).read_text()
    assert text.startswith("#!/bin/sh\n")
    assert subprocess.run(["sh", "-n", str(DOCKER / script)], capture_output=True).returncode == 0
    for bashism in ("[[", "function ", "<<<", "echo -e"):
        assert bashism not in text, f"{script} uses {bashism!r}, which plain sh does not have"
    assert not any(line.strip().startswith("local ") for line in text.splitlines())


def test_the_key_is_kept_apart_from_the_data_and_only_this_machine_reaches_kaal_directly():
    compose = (DOCKER / "compose.yaml").read_text()
    assert "kaal-data:/srv/kaal" in compose and "kaal-key:/etc/kaal" in compose
    assert "${KAAL_LISTEN:-127.0.0.1:8765}:8765" in compose
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "KAAL_KEY_FILE=/etc/kaal/master.key" in dockerfile and "USER kaal" in dockerfile
    for line in dockerfile.splitlines():
        assert not line.startswith("COPY .") or line.startswith("COPY ./"), "copy named files only, so nothing private in the folder ends up in an image"
