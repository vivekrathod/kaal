"""bin/demo-data.py fills a real server, which reads receipts after it has stored
them, and never leaves its own throwaway server behind."""
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("PIL", reason="bin/demo-data.py draws its made-up receipts with Pillow")


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("demo_data", ROOT / "bin" / "demo-data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield tmp_path
    kaal.configure_vault(saved)


class InProcess:
    """What demo-data's `Kaal` does, against an app in this process instead of an address."""

    def __init__(self, home: Path, background: bool):
        settings = server.Settings(home=home, vault=kaal.VAULT, tokens_file=home / "tokens.json", tmp_dir=home / "tmp",
                                   rate_per_minute=100_000, background_extraction=background)
        _record, secret = tokens.TokenStore(settings.tokens_file).create("demo-data", ["read", "reveal", "write"])
        self.app = server.create_app(settings)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": f"Bearer {secret}"}
        self.worker = self.app.state.kaal.worker
        self.seen_unread = 0

    def call(self, method, path, *, json_body=None, file=None, fields=None):
        if file is not None:
            name, content, kind = file
            response = self.client.request(method, path, headers=self.headers, files={"file": (name, content, kind)}, data=fields or {})
        else:
            response = self.client.request(method, path, headers=self.headers, json=json_body)
        if response.status_code != 200:
            # As demo-data's own client does with a refusal.
            raise SystemExit(f"Kaal refused {method} {path} ({response.status_code}): {response.text[:300]}")
        told = response.json()
        if method == "GET" and path.startswith("/v1/medical/receipts/") and told["result"]["receipt"].get("status") == "extracting":
            self.seen_unread += 1
        return told

    def receipts(self):
        return self.client.get("/v1/medical/receipts?limit=100", headers=self.headers).json()["result"]

    def notes(self):
        return self.client.get("/v1/library/notes", headers=self.headers).json()["result"]


def test_fill_waits_for_a_server_that_reads_receipts_after_storing_them(demo, vault, monkeypatch):
    # A server started with `kaal-server serve`, or by the Docker install, reads in the background.
    service = InProcess(vault, background=True)
    # The reading happens while fill waits, as it would in the server's own thread.
    # Only the script's own clock is replaced: the worker itself waits on programs with the real one.
    monkeypatch.setattr(demo, "time", SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=lambda _seconds: service.worker.drain()))

    counted = demo.fill(service)

    assert counted == {"notes": len(demo.NOTES), "receipts": len(demo.RECEIPTS)}
    assert service.seen_unread > 0, "the server answered before it had read a receipt, and fill waited"
    stored = service.receipts()
    assert len(stored) == len(demo.RECEIPTS) and len(service.notes()) == len(demo.NOTES)
    assert not [entry for entry in stored if entry["receipt"]["status"] == "extracting"]
    reviewed = sum(1 for entry in stored if entry.get("reviewed_at") or entry["receipt"]["status"] == "reviewed")
    assert reviewed == sum(1 for receipt in demo.RECEIPTS if receipt.get("review"))
    assert sum(1 for entry in stored if entry["receipt"].get("reimbursed_at")) == sum(1 for receipt in demo.RECEIPTS if receipt.get("reimbursed"))
    # The one the screenshots open is read and waiting to be checked, with the amount printed on it.
    waiting = [entry for entry in stored if entry["receipt"]["status"] == "needs_review" and entry["receipt"]["fields"].get("amount") == "64.50"]
    assert len(waiting) == 1


def test_fill_gives_up_on_a_receipt_that_is_never_read(demo, vault, monkeypatch):
    service = InProcess(vault, background=True)   # and nothing ever reads
    monkeypatch.setattr(demo, "time", SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=lambda _seconds: None))
    made = service.call("POST", "/v1/medical/receipts", file=("made-up.pdf", demo.receipt_pdf(demo.RECEIPTS[0]), "application/pdf"))["result"]
    with pytest.raises(SystemExit, match="had not read a receipt"):
        demo.read_receipt(service, made["id"], seconds=0.05)


def test_filling_a_vault_twice_adds_nothing_unless_asked(demo, vault):
    service = InProcess(vault, background=False)
    demo.fill(service)
    with pytest.raises(SystemExit, match="already holds the made-up notes"):
        demo.fill(service)
    assert len(service.notes()) == len(demo.NOTES) and len(service.receipts()) == len(demo.RECEIPTS)
    demo.fill(service, again=True)
    assert len(service.notes()) == 2 * len(demo.NOTES)


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_stopping_it_while_it_fills_stops_its_server_and_deletes_its_folder(tmp_path):
    with socket.socket() as chosen:
        chosen.bind(("127.0.0.1", 0))
        port = chosen.getsockname()[1]
    # Its throwaway folder goes where this test can see it.
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    running = subprocess.Popen([sys.executable, str(ROOT / "bin" / "demo-data.py"), "serve", "--port", str(port)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and running.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        assert running.poll() is None and _listening(port), "the throwaway Kaal did not start"
        assert list(tmp_path.glob("kaal-demo-*")), "its folder, with its key, is there while it runs"
        # Its server has only just answered, so it is still filling the vault: the moment a
        # script that gave up waiting, or failed early, would stop it.
        running.send_signal(signal.SIGTERM)
        output, _ = running.communicate(timeout=30)
    finally:
        if running.poll() is None:
            running.kill()
            running.wait()
    assert running.returncode == 0, output
    assert "Ctrl-C stops it" not in output, "it was stopped before it had finished filling"
    assert not _listening(port), "its server was left running"
    assert not list(tmp_path.glob("kaal-demo-*")), "its folder, with its key, was left behind"
