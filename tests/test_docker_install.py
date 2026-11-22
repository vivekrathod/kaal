"""The container setup: the start-up script's rules about the key, and the files around it."""
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


# --- install.sh, with stand-ins for Docker and Tailscale

DOCKER_STUB = '''#!/bin/sh
echo "docker $*" >> "$CALLS"
case "$*" in
"compose version --short") echo 2.30.0 ;;
"compose ps -q kaal") echo abc ;;
inspect*) echo healthy ;;
*"account setup-link owner --new-only") echo "https://kaal.example/#/setup/made-up-code" ;;
*"cat /etc/kaal/master.key") echo made-up-key ;;
esac
exit 0
'''
TAILSCALE_STUB = '''#!/bin/sh
echo "tailscale $*" >> "$CALLS"
[ "$TAILSCALE_WORKS" = 1 ] || exit 1
case "$*" in
"status --json") printf '{"Version": "1", "TailscaleIPs": ["100.64.0.1"],\\n "Self": {"ID": "n1", "HostName": "box", "DNSName": "box.tail-made-up.ts.net.", "OS": "linux"},\\n "Peer": {"k": {"DNSName": "other.tail-made-up.ts.net."}}}\\n' ;;
esac
exit 0
'''


@pytest.fixture()
def installer(tmp_path):
    """install.sh in a copy of its folder, so the settings it writes land in the copy."""
    folder = tmp_path / "docker"
    shutil.copytree(DOCKER, folder, ignore=shutil.ignore_patterns(".env*"))
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, text in (("docker", DOCKER_STUB), ("tailscale", TAILSCALE_STUB)):
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    def run(*arguments, typed="", tailscale=False):
        env = {"PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin", "CALLS": str(tmp_path / "calls"), "HOME": str(home), "TAILSCALE_WORKS": "1" if tailscale else "0"}
        done = subprocess.run(["sh", str(folder / "install.sh"), *arguments], env=env, input=typed, text=True, capture_output=True)
        calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
        settings = dict(line.split("=", 1) for line in (folder / ".env").read_text().splitlines() if "=" in line and not line.startswith("#")) if (folder / ".env").exists() else {}
        return done, calls, settings

    return SimpleNamespace(run=run, home=home)


def test_the_installer_finds_tailscale_publishes_kaal_and_saves_a_copy_of_the_key(installer):
    # Typed: through Tailscale; yes to the name it found; yes to publishing; Enter for the usual key file.
    done, calls, settings = installer.run(typed="2\n\n\n\n", tailscale=True)
    assert done.returncode == 0, done.stderr
    assert "Tailscale is on this machine as box.tail-made-up.ts.net" in done.stdout, "this machine's own name, not another's"
    assert settings["KAAL_PUBLIC_URL"] == "https://box.tail-made-up.ts.net" and settings["KAAL_LISTEN"] == "127.0.0.1:8765"
    assert "tailscale serve --bg 8765" in calls
    key = installer.home / "kaal-master.key"
    assert key.read_text() == "made-up-key\n" and key.stat().st_mode & 0o777 == 0o600
    assert "https://kaal.example/#/setup/made-up-code" in done.stdout
    assert "still on this machine" in done.stdout and "kaalctl key-backup" not in done.stdout
    assert "Point your proxy at it" not in done.stdout, "nothing is left for the person to publish"


def test_the_installer_asks_for_the_address_without_tailscale_and_the_key_copy_can_be_skipped(installer):
    done, calls, settings = installer.run(typed="2\nhttps://kaal.example.ts.net\n-\n")
    assert done.returncode == 0, done.stderr
    assert settings["KAAL_PUBLIC_URL"] == "https://kaal.example.ts.net"
    assert "Tailscale is on this machine" not in done.stdout and "tailscale serve --bg 8765" not in calls
    assert "Point your proxy at it" in done.stdout
    assert not (installer.home / "kaal-master.key").exists()
    assert "kaalctl key-backup ~/kaal-master.key" in done.stdout, "skipped, the person is still told how"

    # A name Tailscale offers can be turned down, and the key written where the person says.
    (installer.home / "keys").mkdir()
    again = installer.home.parent / "docker" / ".env"
    again.unlink()
    done, calls, settings = installer.run(typed="2\nn\nhttps://other.example.org\n~/keys/kaal.key\n", tailscale=True)
    assert done.returncode == 0, done.stderr
    assert settings["KAAL_PUBLIC_URL"] == "https://other.example.org" and "tailscale serve --bg 8765" not in calls
    assert (installer.home / "keys" / "kaal.key").read_text() == "made-up-key\n"


def test_the_installer_asks_nothing_when_told_where_kaal_is_reached(installer):
    done, calls, settings = installer.run("--local")
    assert done.returncode == 0, done.stderr
    assert settings["KAAL_PUBLIC_URL"] == "http://localhost:8765"
    assert not any(call.startswith("tailscale") for call in calls)
    assert "kaalctl key-backup ~/kaal-master.key" in done.stdout and "File to write it to" not in done.stdout
    assert "Settings > Vaults and sharing" in done.stdout
