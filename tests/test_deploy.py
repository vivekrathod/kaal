"""deploy/deploy-remote.sh: install, failed install, and rollback.

The script runs as root under systemd on the server. Here it runs against a
throwaway virtualenv with stand-ins for systemctl, curl, flock, id and sleep,
and installs hand-built wheels, so nothing needs a network or a server.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "deploy-remote.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")

STUBS = {
    # `start` of the backup unit fails while $PREFIX/backup-fails exists.
    "systemctl": '#!/bin/sh\necho "$@" >> "$KAAL_PREFIX/systemctl.log"\n'
                 'if [ "$1" = start ] && [ -e "$KAAL_PREFIX/backup-fails" ]; then exit 1; fi\nexit 0\n',
    # Health fails while the installed version equals the contents of $PREFIX/unhealthy-version.
    "curl": '#!/bin/sh\nbad="$(cat "$KAAL_PREFIX/unhealthy-version" 2>/dev/null)"\n'
            'now="$("$KAAL_PREFIX/venv/bin/python" -c \'import importlib.metadata as m; print(m.version("kaal"))\')"\n'
            '[ "$bad" = "$now" ] && exit 22\nexit 0\n',
    "flock": "#!/bin/sh\nexit 0\n",
    "id": "#!/bin/sh\necho 0\n",
    "sleep": "#!/bin/sh\nexit 0\n",
}


def build_wheel(directory: Path, version: str) -> Path:
    info = f"kaal-{version}.dist-info"
    files = {
        "kaal/__init__.py": f'__version__ = "{version}"\n\ndef main():\n    print(__version__)\n',
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: kaal\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: kaal-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": "[console_scripts]\nkaal = kaal:main\nkaal-server = kaal:main\n",
    }
    files[f"{info}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{info}/RECORD"])
    wheel = directory / f"kaal-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


class Server:
    def __init__(self, prefix: Path):
        self.prefix = prefix
        self.python = prefix / "venv" / "bin" / "python"
        stubs = prefix / "stubs"
        stubs.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        self.env = {**os.environ, "KAAL_PREFIX": str(prefix), "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
        self.env.pop("PYTHONPATH", None)
        subprocess.run([sys.executable, "-m", "venv", str(prefix / "venv")], check=True)
        (prefix / "requirements.lock").write_text("")
        self.staged: list[Path] = []

    def pip_install(self, version: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = build_wheel(Path(directory), version)
            subprocess.run([str(self.python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps", str(wheel)], check=True, env=self.env, cwd=self.prefix)

    def stage(self, version: str) -> Path:
        # The script only accepts wheels staged where deploy.sh puts them.
        directory = Path(tempfile.mkdtemp(prefix="kaal-deploy-", dir="/tmp"))
        self.staged.append(directory)
        return build_wheel(directory, version)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        # Run from the server's own folder, never the source tree: a build there leaves
        # metadata that Python would find first and report as the installed version.
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env, text=True, capture_output=True, cwd=self.prefix)

    def version(self) -> str:
        code = 'import importlib.metadata as m; print(m.version("kaal"))'
        return subprocess.run([str(self.python), "-c", code], env=self.env, text=True, capture_output=True, check=True, cwd=self.prefix).stdout.strip()

    def release(self) -> str:
        path = self.prefix / "RELEASE"
        return path.read_text() if path.exists() else ""

    def snapshots(self) -> list[str]:
        return sorted(path.name for path in (self.prefix / "rollback").glob("*.tar")) if (self.prefix / "rollback").exists() else []

    def systemctl_calls(self) -> list[str]:
        log = self.prefix / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture()
def server(tmp_path):
    instance = Server(tmp_path)
    instance.pip_install("1")
    yield instance
    for directory in instance.staged:
        shutil.rmtree(directory, ignore_errors=True)


def test_status_reports_version_without_changing_anything(server):
    result = server.run("status")
    assert result.returncode == 0, result.stderr
    assert "version: 1" in result.stdout
    assert "commit: unknown" in result.stdout
    assert "health: ok" in result.stdout
    assert not (server.prefix / ".deploy.lock").exists()
    assert server.systemctl_calls() == ["is-active kaal-server.service"]


def test_install_backs_up_then_installs_and_records_the_commit(server):
    wheel = server.stage("2")
    result = server.run("install", str(wheel), "abc123", "0")
    assert result.returncode == 0, result.stderr
    assert server.version() == "2"
    assert "commit: abc123" in server.release()
    assert len(server.snapshots()) == 1
    assert not wheel.parent.exists()
    calls = server.systemctl_calls()
    assert calls[0] == "start kaal-backup.service"
    assert "restart kaal-server.service" in calls


def test_failed_backup_changes_nothing(server):
    (server.prefix / "backup-fails").touch()
    result = server.run("install", str(server.stage("2")), "abc123", "0")
    assert result.returncode != 0
    assert "nothing was changed" in result.stderr
    assert server.version() == "1"
    assert server.snapshots() == []
    assert "restart kaal-server.service" not in server.systemctl_calls()


def test_unhealthy_release_is_rolled_back_automatically(server):
    assert server.run("install", str(server.stage("2")), "aaa111", "0").returncode == 0
    (server.prefix / "unhealthy-version").write_text("3")
    result = server.run("install", str(server.stage("3")), "bbb222", "0")
    assert result.returncode != 0
    assert "rolled back" in result.stderr
    assert server.version() == "2"
    assert "commit: aaa111" in server.release()
    # Only the snapshot of version 1 remains: the one taken of version 2 equals what is installed.
    assert len(server.snapshots()) == 1 and server.snapshots()[0].endswith("-1.tar")


def test_rollback_restores_the_previous_version_and_its_entry_points(server):
    assert server.run("install", str(server.stage("2")), "aaa111", "0").returncode == 0
    result = server.run("rollback")
    assert result.returncode == 0, result.stderr
    assert server.version() == "1"
    assert server.release() == ""
    assert server.snapshots() == []
    shown = subprocess.run([str(server.prefix / "venv" / "bin" / "kaal")], env=server.env, text=True, capture_output=True)
    assert shown.stdout.strip() == "1"
    assert server.run("rollback").returncode != 0


def test_install_rejects_unexpected_arguments(server):
    wheel = server.stage("2")
    elsewhere = server.prefix / wheel.name
    shutil.copy(wheel, elsewhere)
    assert server.run("install", str(elsewhere), "abc123", "0").returncode != 0
    assert server.run("install", str(wheel), "not-a-commit; rm -rf /", "0").returncode != 0
    assert server.version() == "1"
    assert server.systemctl_calls() == []


def _accounts_file(server, version, rows):
    import sqlite3
    path = server.prefix / "accounts.db"
    server.env["KAAL_ACCOUNTS_FILE"] = str(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS people (name TEXT)")
        db.execute("DELETE FROM people")
        db.executemany("INSERT INTO people VALUES (?)", [(row,) for row in rows])
        db.execute(f"PRAGMA user_version = {version}")
    return path


def _people(path):
    import sqlite3
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute("SELECT name FROM people ORDER BY rowid")], db.execute("PRAGMA user_version").fetchone()[0]


def test_rollback_puts_the_accounts_file_back_when_the_newer_version_changed_its_layout(server):
    """An older Kaal refuses an accounts file with a newer layout, so it must get its own back."""
    path = _accounts_file(server, 1, ["jane"])
    assert server.run("install", str(server.stage("2")), "aaa111", "0").returncode == 0
    # Version 2 starts, upgrades the layout and someone is added.
    _accounts_file(server, 2, ["jane", "sam"])
    result = server.run("rollback")
    assert result.returncode == 0, result.stderr
    assert "accounts file put back" in result.stdout
    assert _people(path) == (["jane"], 1)
    assert _people(server.prefix / "accounts.db.newer-layout") == (["jane", "sam"], 2), "the newer file is kept, not thrown away"
    assert "stop kaal-server.service" in server.systemctl_calls()


def test_rollback_leaves_the_accounts_file_alone_when_its_layout_did_not_change(server):
    path = _accounts_file(server, 1, ["jane"])
    assert server.run("install", str(server.stage("2")), "aaa111", "0").returncode == 0
    _accounts_file(server, 1, ["jane", "sam"])
    assert server.run("rollback").returncode == 0
    assert _people(path) == (["jane", "sam"], 1), "people added since the install are kept"
    assert not (server.prefix / "accounts.db.newer-layout").exists()


def test_an_unhealthy_release_that_upgraded_the_accounts_file_is_rolled_back_whole(server):
    import sqlite3
    path = _accounts_file(server, 1, ["jane"])
    assert server.run("install", str(server.stage("2")), "aaa111", "0").returncode == 0
    (server.prefix / "unhealthy-version").write_text("3")
    # Stand-in for version 3 upgrading the file as it starts: the first health check does it.
    stub = server.prefix / "stubs" / "curl"
    upgrade = ('if [ -e "$KAAL_PREFIX/upgrade-once" ]; then rm "$KAAL_PREFIX/upgrade-once"; '
               '"$KAAL_PREFIX/venv/bin/python" -c "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); d.execute(\'PRAGMA user_version = 2\'); d.commit()" "$KAAL_ACCOUNTS_FILE"; fi\n')
    stub.write_text(stub.read_text().replace("#!/bin/sh\n", "#!/bin/sh\n" + upgrade, 1))
    (server.prefix / "upgrade-once").write_text("")
    result = server.run("install", str(server.stage("3")), "bbb222", "0")
    assert result.returncode != 0 and "rolled back" in result.stderr, result.stderr
    assert server.version() == "2" and _people(path) == (["jane"], 1)
    with sqlite3.connect(server.prefix / "accounts.db.newer-layout") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
