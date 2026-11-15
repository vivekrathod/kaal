"""The kaal CLI in remote mode, end to end against a real kaal-server."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import uvicorn

import kaal.core as core
from kaal import client, server, tokens

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "secure-notes.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RemoteCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name)
        cls.saved_vault = core.VAULT
        cls.settings = server.Settings(home=home, vault=home / "data", tokens_file=home / "tokens.json", tmp_dir=home / "tmp", rate_per_minute=10_000)
        core.configure_vault(cls.settings.vault)
        core.call_captured(core.init_vault, SimpleNamespace())
        store = tokens.TokenStore(cls.settings.tokens_file)
        cls.token_files = {}
        for name, scopes in {"full": ["read", "reveal", "write", "delete", "admin"], "reader": ["read"]}.items():
            _record, secret = store.create(name, scopes)
            cls.token_files[name] = tokens.write_secret_file(home / f"{name}.token", secret)
        cls.port = free_port()
        cls.uv = uvicorn.Server(uvicorn.Config(server.create_app(cls.settings), host="127.0.0.1", port=cls.port, log_level="error", access_log=False))
        cls.thread = threading.Thread(target=cls.uv.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.uv.started:
                break
            time.sleep(0.05)
        cls.url = f"http://127.0.0.1:{cls.port}"
        assert os.environ.get("KAAL_KEY_PROVIDER") == "file", "tests must never use the Keychain"

    @classmethod
    def tearDownClass(cls):
        cls.uv.should_exit = True
        cls.thread.join(5)
        core.configure_vault(cls.saved_vault)
        cls.tmp.cleanup()

    def env(self, token="full", **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KAAL_", "SECURE_NOTES_"))}
        env.update({"KAAL_URL": self.url, "KAAL_TOKEN_FILE": str(self.token_files[token]), "KAAL_CLI_NAME": "kaal", "KAAL_CONFIG": str(Path(self.tmp.name) / "no-config.toml")})
        env.pop("PYTHONPATH", None)
        env.update(extra)
        return env

    def local_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KAAL_", "SECURE_NOTES_"))}
        env.update({"SECURE_NOTES_VAULT": str(self.settings.vault), "KAAL_KEY_PROVIDER": "file", "KAAL_KEY_FILE": os.environ["KAAL_KEY_FILE"], "KAAL_CLI_NAME": "kaal", "KAAL_LOCAL": "1"})
        env.pop("PYTHONPATH", None)
        return env

    def kaal(self, *argv, env=None, stdin=None, check=True):
        proc = subprocess.run([sys.executable, str(LAUNCHER), *argv], env=env or self.env(), input=stdin, capture_output=True, text=True, timeout=120)
        if check and proc.returncode != 0:
            self.fail(f"kaal {argv} exited {proc.returncode}: {proc.stderr}")
        return proc

    def add(self, title, body="# Body\n", *extra):
        return json.loads(self.kaal("add", "--title", title, "--body", body, *extra).stdout)

    def test_read_commands_print_exactly_what_the_local_cli_prints(self):
        note = self.add("Parity note", "Passport: X1234567\nline two", "--tags", "ids,parity")
        for argv in (["list", "--json"], ["list"], ["show", note["id"], "--json"], ["show", "Parity note"], ["show", note["id"], "--full", "--json"], ["search", "parity"], ["library", "list"]):
            remote = self.kaal(*argv)
            local = self.kaal(*argv, env=self.local_env())
            self.assertEqual(remote.stdout, local.stdout, argv)
            self.assertEqual(remote.stderr, local.stderr, argv)

    def test_add_append_and_copy(self):
        note = self.add("Copy me", "Passport: X7654321")
        self.assertEqual(note["status"], "created")
        appended = json.loads(self.kaal("append", note["id"], "--body", "extra").stdout)
        self.assertEqual(appended["status"], "appended")
        bin_dir = Path(self.tmp.name) / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        clip = Path(self.tmp.name) / "clipboard.txt"
        fake = bin_dir / "pbcopy"
        fake.write_text(f"#!/bin/sh\ncat > '{clip}'\n")
        fake.chmod(0o755)
        out = self.kaal("copy", note["id"], "--field", "passport", env=self.env(PATH=f"{bin_dir}:{os.environ['PATH']}"))
        self.assertEqual(json.loads(out.stdout), {"status": "copied", "note": note["id"], "field": "passport", "characters": 8})
        self.assertNotIn("X7654321", out.stdout + out.stderr)
        self.assertEqual(clip.read_text(), "X7654321")

    def test_attach_and_export_round_trip(self):
        note = self.add("With file")
        src = Path(self.tmp.name) / "Statement 2025.pdf"
        src.write_bytes(b"%PDF-1.4 statement bytes")
        attached = json.loads(self.kaal("attach", note["id"], str(src), "--sensitivity", "sensitive").stdout)
        self.assertEqual(attached["name"], "Statement 2025.pdf")
        out = Path(self.tmp.name) / "exported.pdf"
        exported = json.loads(self.kaal("export-attachment", note["id"], "Statement", "--output", str(out)).stdout)
        self.assertEqual(out.read_bytes(), src.read_bytes())
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(exported["sha256"], hashlib.sha256(src.read_bytes()).hexdigest())
        again = self.kaal("export-attachment", note["id"], "Statement", "--output", str(out), check=False)
        self.assertEqual(again.returncode, 1)
        self.assertIn("Output exists", again.stderr)

    def test_captures(self):
        page = Path(self.tmp.name) / "page.txt"
        page.write_text("Captured page body")
        captured = json.loads(self.kaal("capture", str(page), "--source-url", "https://example.com/x?secret=1", "--source-title", "Example").stdout)
        self.assertEqual(captured["status"], "captured")
        selection = json.loads(self.kaal("capture-text", "--source-title", "Example", stdin="selected words").stdout)
        self.assertEqual(selection["type"], "browser-selection")

    def test_library_and_medical(self):
        created = json.loads(self.kaal("library", "create", "--title", "Dash note", "--tags", "x", stdin="Dashboard body").stdout)
        nid = created["note"]["id"]
        shown = json.loads(self.kaal("library", "show", nid).stdout)
        self.assertEqual(shown["note"]["body"], "Dashboard body")
        receipt_src = Path(self.tmp.name) / "receipt.txt"
        receipt_src.write_text("Amount paid: $40.00\nService date: 03/04/2026\n")
        receipt = json.loads(self.kaal("medical", "capture", str(receipt_src)).stdout)
        listed = json.loads(self.kaal("medical", "list", "--inbox", "--json").stdout)
        self.assertIn(receipt["id"], [r["id"] for r in listed])
        out = Path(self.tmp.name) / "receipt-copy.txt"
        self.kaal("medical", "export-original", receipt["id"], "--attachment-id", receipt["attachment_id"], "--output", str(out))
        self.assertEqual(out.read_text(), receipt_src.read_text())
        self.assertEqual(json.loads(self.kaal("medical", "people", "--add", "Jane Doe").stdout)["people"], ["Jane Doe"])
        self.assertEqual(json.loads(self.kaal("medical", "people").stdout)["people"], ["Jane Doe"])
        early = self.kaal("medical", "reimburse", receipt["id"], check=False)
        self.assertEqual(early.returncode, 1)
        self.assertIn("Review", early.stderr)
        self.kaal("medical", "review", receipt["id"])
        paid = json.loads(self.kaal("medical", "reimburse", receipt["id"], "--date", "5/6/2026", "--note", "ref 9").stdout)
        self.assertEqual((paid["reimbursed_at"], paid["total_amount"]), ("2026-05-06", "40.00"))
        summary = json.loads(self.kaal("medical", "report", "--year", "2026", "--reimbursed", "yes").stdout)
        self.assertEqual((summary["count"], summary["reimbursed_amount"]), (1, "40.00"))
        self.assertEqual(self.kaal("medical", "report", "--year", "2026").stdout, self.kaal("medical", "report", "--year", "2026", env=self.local_env()).stdout)
        self.assertEqual(json.loads(self.kaal("medical", "reimburse", receipt["id"], "--undo").stdout)["status"], "unreimbursed")
        self.assertEqual(json.loads(self.kaal("medical", "people", "--remove", "Jane Doe").stdout)["people"], [])
        self.kaal("medical", "trash", receipt["id"])
        refused = self.kaal("medical", "purge", receipt["id"], check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("requires --yes", refused.stderr)
        self.assertEqual(json.loads(self.kaal("medical", "purge", receipt["id"], "--yes").stdout)["status"], "purged")

    def test_ambiguous_query_prints_matches_like_the_local_cli(self):
        self.add("Ambiguous A")
        self.add("Ambiguous B")
        remote = self.kaal("show", "Ambiguous", check=False)
        local = self.kaal("show", "Ambiguous", env=self.local_env(), check=False)
        self.assertEqual((remote.returncode, remote.stderr), (local.returncode, local.stderr))
        self.assertIn("Multiple notes match", remote.stderr)

    def test_delete_requires_yes_and_scope(self):
        note = self.add("Delete me")
        refused = self.kaal("delete", note["id"], check=False)
        self.assertIn("Deletion requires --yes", refused.stderr)
        forbidden = self.kaal("delete", note["id"], "--yes", env=self.env("reader"), check=False)
        self.assertEqual(forbidden.returncode, 1)
        self.assertIn("lacks the delete scope", forbidden.stderr)
        self.assertEqual(json.loads(self.kaal("delete", note["id"], "--yes").stdout)["status"], "deleted")

    def test_verify_and_status(self):
        self.assertEqual(json.loads(self.kaal("verify").stdout)["status"], "ok")
        status = json.loads(self.kaal("status").stdout)
        self.assertEqual(status["server"], self.url)

    def test_bad_token_is_reported_without_leaking_it(self):
        bad = Path(self.tmp.name) / "bad.token"
        if not bad.exists():
            tokens.write_secret_file(bad, "kaal_deadbeef_notarealsecret")
        proc = self.kaal("list", env=self.env(KAAL_TOKEN_FILE=str(bad)), check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("rejected the token", proc.stderr)
        self.assertNotIn("notarealsecret", proc.stderr + proc.stdout)

    def test_server_only_commands_are_refused_remotely(self):
        proc = self.kaal("init", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("on the server", proc.stderr)

    def test_classify_runs_locally(self):
        proc = self.kaal("classify", env=self.env(KAAL_URL="https://unreachable.invalid"), stdin="SSN: 078-05-1120")
        self.assertEqual(json.loads(proc.stdout)["sensitivity"], "sensitive")


class RemoteConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_when_nothing_is_configured(self):
        self.assertIsNone(client.remote_config({"KAAL_CONFIG": str(self.dir / "missing.toml")}))

    def test_kaal_local_forces_local_mode(self):
        self.assertIsNone(client.remote_config({"KAAL_URL": "https://kaal.example.ts.net", "KAAL_LOCAL": "1"}))

    def test_reads_config_file(self):
        token = self.dir / "t"
        tokens.write_secret_file(token, "kaal_ab_cd")
        config = self.dir / "config.toml"
        config.write_text(f'url = "https://storage.example.ts.net"\ntoken_file = "{token}"\n')
        remote = client.remote_config({"KAAL_CONFIG": str(config)})
        self.assertEqual(remote.url, "https://storage.example.ts.net")
        self.assertEqual(remote.token, "kaal_ab_cd")

    def test_refuses_plain_http_off_loopback(self):
        with self.assertRaisesRegex(client.ClientConfigError, "https"):
            client.remote_config({"KAAL_URL": "http://192.0.2.10:8765", "KAAL_TOKEN": "kaal_a_b"})
        self.assertIsNotNone(client.remote_config({"KAAL_URL": "http://127.0.0.1:8765", "KAAL_TOKEN": "kaal_a_b"}))

    def test_refuses_token_files_others_can_read(self):
        token = self.dir / "t"
        token.write_text("kaal_ab_cd")
        os.chmod(token, 0o644)
        with self.assertRaisesRegex(client.ClientConfigError, "readable only by you"):
            client.remote_config({"KAAL_URL": "https://x.ts.net", "KAAL_TOKEN_FILE": str(token)})

    def test_refuses_config_with_inline_token_that_others_can_read(self):
        config = self.dir / "config.toml"
        config.write_text('url = "https://x.ts.net"\ntoken = "kaal_a_b"\n')
        os.chmod(config, 0o640)
        with self.assertRaisesRegex(client.ClientConfigError, "readable only by you"):
            client.remote_config({"KAAL_CONFIG": str(config)})

    def test_missing_token_is_an_error(self):
        with self.assertRaisesRegex(client.ClientConfigError, "token"):
            client.remote_config({"KAAL_URL": "https://x.ts.net", "KAAL_CONFIG": str(self.dir / "none.toml")})


if __name__ == "__main__":
    unittest.main()


class TlsContextTests(unittest.TestCase):
    """python.org builds on macOS ship with no CA certificates loaded."""

    def ca_bundle(self):
        import certifi
        return certifi.where()

    def test_falls_back_to_a_system_bundle_when_python_has_no_cas(self):
        import ssl
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.cert_store_stats()["x509_ca"], 0)
        context = client.tls_context(None, base=empty, bundles=("/nonexistent/ca.pem", self.ca_bundle()))
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_keeps_python_defaults_when_they_have_cas(self):
        import ssl
        base = ssl.create_default_context(cafile=self.ca_bundle())
        before = base.cert_store_stats()["x509_ca"]
        context = client.tls_context(None, base=base, bundles=("/nonexistent/ca.pem",))
        self.assertIs(context, base)
        self.assertEqual(context.cert_store_stats()["x509_ca"], before)

    def test_explicit_ca_file_wins(self):
        context = client.tls_context(self.ca_bundle(), bundles=())
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_https_api_uses_the_fallback(self):
        api = client.Api(client.Remote(url="https://storage.example.ts.net", token="kaal_a_b"))
        self.assertGreater(api._ssl.cert_store_stats()["x509_ca"], 0)
