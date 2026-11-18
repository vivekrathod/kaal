import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import kaal.core as core
from kaal import server, tokens

ALL = ["read", "reveal", "write", "delete", "admin"]


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.saved_vault = core.VAULT
        self.settings = server.Settings(
            home=home,
            vault=home / "data",
            tokens_file=home / "tokens.json",
            tmp_dir=home / "tmp",
            max_upload_bytes=64 * 1024,
            rate_per_minute=1000,
        )
        core.configure_vault(self.settings.vault)
        core.call_captured(core.init_vault, SimpleNamespace())
        store = tokens.TokenStore(self.settings.tokens_file)
        self.tok = {}
        for name, scopes in {"full": ALL, "reader": ["read"], "writer": ["read", "write"], "revealer": ["read", "reveal"]}.items():
            _record, secret = store.create(name, scopes)
            self.tok[name] = secret
        self.app = server.create_app(self.settings)
        self.client = TestClient(self.app)

    def tearDown(self):
        core.configure_vault(self.saved_vault)
        self.tmp.cleanup()

    def h(self, name="full"):
        return {"Authorization": f"Bearer {self.tok[name]}"}

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()["result"] if status == 200 else response.json()

    def add(self, title="Note", body="# Note\n", tags=(), sensitivity="auto"):
        return self.ok(self.client.post("/v1/notes", headers=self.h(), json={"title": title, "body": body, "tags": list(tags), "sensitivity": sensitivity}))

    def upload(self, url, name="doc.txt", content=b"hello world", token="full", **form):
        return self.client.post(url, headers=self.h(token), files={"file": (name, content, "application/octet-stream")}, data={k: str(v) for k, v in form.items()})


class AuthTests(ServerTestCase):
    def test_health_needs_no_token_and_says_nothing_else(self):
        response = self.client.get("/v1/health")
        self.assertEqual(response.json(), {"status": "ok"})

    def test_a_healthy_health_check_leaves_no_log_line_but_other_requests_do(self):
        with self.assertLogs("kaal.server", level="INFO") as logged:
            self.client.get("/v1/health")
            self.client.get("/v1/notes")
        lines = [line for line in logged.output if "/v1/" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("GET /v1/notes 401", lines[0])

    def test_missing_wrong_and_revoked_tokens_get_401(self):
        self.assertEqual(self.client.get("/v1/notes").status_code, 401)
        self.assertEqual(self.client.get("/v1/notes", headers={"Authorization": "Bearer kaal_x_y"}).status_code, 401)
        self.assertEqual(self.client.get("/v1/notes", headers={"Authorization": "Basic abc"}).status_code, 401)
        tokens.TokenStore(self.settings.tokens_file).revoke("reader")
        response = self.client.get("/v1/notes", headers=self.h("reader"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(response.json()["kind"], "unauthorized")

    def test_scopes_are_enforced(self):
        note = self.add(body="Passport: X1234567")
        self.assertEqual(self.client.get("/v1/notes", headers=self.h("reader")).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/notes/{note['id']}", headers=self.h("reader")).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/notes/{note['id']}?full=true", headers=self.h("reader")).status_code, 403)
        self.assertEqual(self.client.get(f"/v1/notes/{note['id']}/fields/passport", headers=self.h("reader")).status_code, 403)
        self.assertEqual(self.client.post("/v1/notes", headers=self.h("reader"), json={"title": "x", "body": "y"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/v1/notes/{note['id']}?confirm=true", headers=self.h("writer")).status_code, 403)
        self.assertEqual(self.client.get("/v1/medical/receipts", headers=self.h("writer")).status_code, 403)
        forbidden = self.client.post("/v1/notes", headers=self.h("reader"), json={"title": "x", "body": "y"}).json()
        self.assertEqual(forbidden["kind"], "forbidden")

    def test_rate_limit_per_token(self):
        self.settings.rate_per_minute = 3
        client = TestClient(server.create_app(self.settings))
        codes = [client.get("/v1/notes", headers=self.h("reader")).status_code for _ in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)

    def test_responses_are_not_cacheable(self):
        self.assertEqual(self.client.get("/v1/notes", headers=self.h()).headers["cache-control"], "no-store")

    def test_refuses_to_start_without_a_vault(self):
        settings = server.Settings(home=Path(self.tmp.name), vault=Path(self.tmp.name) / "missing", tokens_file=self.settings.tokens_file, tmp_dir=self.settings.tmp_dir)
        with self.assertRaisesRegex(server.ServerConfigError, "Vault not found"):
            server.create_app(settings)
        self.assertFalse((Path(self.tmp.name) / "missing").exists())


class NoteTests(ServerTestCase):
    def test_create_list_show_redacted_and_full(self):
        created = self.add(title="IDs", body="SSN: 078-05-1120\n", sensitivity="sensitive")
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["storage"], "encrypted")
        listed = self.ok(self.client.get("/v1/notes", headers=self.h("reader")))
        self.assertEqual([n["id"] for n in listed], [created["id"]])
        redacted = self.ok(self.client.get(f"/v1/notes/{created['id']}", headers=self.h("reader")))
        self.assertTrue(redacted["redacted"])
        self.assertNotIn("078-05-1120", redacted["body"])
        full = self.ok(self.client.get(f"/v1/notes/{created['id']}?full=true", headers=self.h("revealer")))
        self.assertIn("078-05-1120", full["body"])

    def test_result_format_matches_the_cli(self):
        response = self.client.post("/v1/notes", headers=self.h(), json={"title": "Café", "body": "x"})
        self.assertEqual(response.json()["format"], {"indent": 2, "ensure_ascii": False})

    def test_lookup_resolves_queries(self):
        a = self.add(title="Tax 2024")
        self.add(title="Tax 2025")
        found = self.ok(self.client.post("/v1/notes/lookup", headers=self.h("reader"), json={"query": "2024"}))
        self.assertEqual(found["note"]["id"], a["id"])
        conflict = self.client.post("/v1/notes/lookup", headers=self.h("reader"), json={"query": "Tax"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(conflict.json()["details"]["matches"]), 2)
        self.assertEqual(self.client.post("/v1/notes/lookup", headers=self.h("reader"), json={"query": "zzz"}).status_code, 404)

    def test_id_endpoints_never_fall_back_to_title_matching(self):
        self.add(title="Groceries")
        self.assertEqual(self.client.get("/v1/notes/Groceries", headers=self.h()).status_code, 404)
        self.assertEqual(self.client.delete("/v1/notes/Groc?confirm=true", headers=self.h()).status_code, 404)
        self.assertEqual(len(self.ok(self.client.get("/v1/notes", headers=self.h()))), 1)

    def test_search_append_field_and_delete(self):
        note = self.add(title="Passport", body="Passport: X1234567", tags=["ids"])
        self.assertEqual(len(self.ok(self.client.post("/v1/notes/search", headers=self.h("reader"), json={"query": "ids"}))), 1)
        appended = self.ok(self.client.post(f"/v1/notes/{note['id']}/append", headers=self.h("writer"), json={"body": "more"}))
        self.assertEqual(appended["status"], "appended")
        field = self.ok(self.client.get(f"/v1/notes/{note['id']}/fields/passport", headers=self.h("revealer")))
        self.assertEqual(field, {"note": note["id"], "field": "passport", "value": "X1234567"})
        self.assertEqual(self.client.delete(f"/v1/notes/{note['id']}", headers=self.h()).status_code, 400)
        self.assertEqual(self.ok(self.client.delete(f"/v1/notes/{note['id']}?confirm=true", headers=self.h()))["status"], "deleted")

    def test_attach_and_download_round_trip(self):
        note = self.add(title="Docs")
        content = b"%PDF-1.4 pretend"
        attached = self.ok(self.upload(f"/v1/notes/{note['id']}/attachments", name="Return 2024.pdf", content=content, sensitivity="sensitive"))
        self.assertEqual(attached["name"], "Return 2024.pdf")
        self.assertEqual(attached["storage"], "encrypted")
        response = self.client.get(f"/v1/notes/{note['id']}/attachments/{attached['attachmentId']}/content", headers=self.h("revealer"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content)
        self.assertEqual(response.headers["x-kaal-sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.client.get(f"/v1/notes/{note['id']}/attachments/{attached['attachmentId']}/content", headers=self.h("reader")).status_code, 403)

    def test_upload_limit_and_cleanup(self):
        note = self.add(title="Docs")
        too_big = self.upload(f"/v1/notes/{note['id']}/attachments", content=b"x" * (self.settings.max_upload_bytes + 1))
        self.assertEqual(too_big.status_code, 413)
        self.assertEqual(too_big.json()["kind"], "too_large")
        self.ok(self.upload(f"/v1/notes/{note['id']}/attachments", name="../../escape.txt"))
        self.assertEqual(list(self.settings.tmp_dir.iterdir()), [])
        meta = core.note_by_id(note["id"])
        self.assertEqual(meta["attachments"][0]["name"], "escape.txt")

    def test_validation_errors_do_not_echo_input(self):
        response = self.client.post("/v1/notes", headers=self.h(), json={"title": "x", "body": 12345678, "sensitivity": "SECRET-MARKER"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("SECRET-MARKER", response.text)
        self.assertNotIn("12345678", response.text)

    def test_logs_carry_no_bodies_tokens_or_queries(self):
        with self.assertLogs("kaal.server", level="INFO") as logs:
            self.add(title="Secret title MARKER1", body="Secret body MARKER2")
            self.client.post("/v1/notes/search", headers=self.h(), json={"query": "MARKER3"})
            self.client.get("/v1/notes?tag=MARKER4", headers=self.h())
        text = "\n".join(logs.output)
        for marker in ("MARKER1", "MARKER2", "MARKER3", "MARKER4", self.tok["full"]):
            self.assertNotIn(marker, text)
        self.assertIn("POST /v1/notes 200 full", text)

    def test_verify_and_status(self):
        self.add()
        self.assertEqual(self.ok(self.client.get("/v1/verify", headers=self.h("reader")))["status"], "ok")
        self.assertTrue(self.ok(self.client.get("/v1/status", headers=self.h("reader")))["index_exists"])


class CaptureLibraryMedicalTests(ServerTestCase):
    def test_captures(self):
        captured = self.ok(self.upload("/v1/captures", name="page.txt", content=b"Hello page", token="writer", source_url="https://example.com/p?q=1", source_title="Page"))
        self.assertEqual(captured["status"], "captured")
        text = self.ok(self.client.post("/v1/captures/text", headers=self.h("writer"), json={"text": "Selected", "source_title": "Page"}))
        self.assertEqual(text["type"], "browser-selection")

    def test_library_lifecycle(self):
        created = self.ok(self.client.post("/v1/library/notes", headers=self.h("writer"), json={"title": "Doc", "tags": ["a"], "body": "Body"}))
        nid = created["note"]["id"]
        self.assertEqual(self.ok(self.client.get(f"/v1/library/notes/{nid}", headers=self.h("revealer")))["note"]["body"], "Body")
        self.assertEqual(self.client.get(f"/v1/library/notes/{nid}", headers=self.h("writer")).status_code, 403)
        updated = self.ok(self.client.put(f"/v1/library/notes/{nid}", headers=self.h("writer"), json={"title": "Doc 2", "tags": [], "body": "New"}))
        # A token that cannot reveal is told which note it changed, not shown it.
        self.assertEqual(updated["note"]["id"], nid)
        self.assertNotIn("Doc 2", json.dumps(updated))
        self.assertEqual(self.ok(self.client.get(f"/v1/library/notes/{nid}", headers=self.h("revealer")))["note"]["title"], "Doc 2")
        attached = self.ok(self.upload(f"/v1/library/notes/{nid}/attachments", name="a.txt", content=b"text body", token="writer"))
        att_id = attached["attachment"]["id"]
        text = self.ok(self.client.get(f"/v1/library/notes/{nid}/attachments/{att_id}/extracted-text", headers=self.h("revealer")))
        self.assertIn("text body", text["text"])
        self.assertEqual(self.ok(self.client.post(f"/v1/library/notes/{nid}/extract", headers=self.h("writer")))["status"], "updated")
        self.assertEqual(self.ok(self.client.get("/v1/library/notes", headers=self.h("reader")))[0]["id"], nid)
        self.assertEqual(self.client.delete(f"/v1/library/notes/{nid}?confirm=true", headers=self.h()).status_code, 400)
        self.ok(self.client.post(f"/v1/library/notes/{nid}/trash", headers=self.h("writer")))
        self.assertEqual(self.ok(self.client.get("/v1/library/notes?trash=true", headers=self.h("reader")))[0]["id"], nid)
        self.ok(self.client.post(f"/v1/library/notes/{nid}/restore", headers=self.h("writer")))
        self.ok(self.client.post(f"/v1/library/notes/{nid}/trash", headers=self.h("writer")))
        self.assertEqual(self.ok(self.client.delete(f"/v1/library/notes/{nid}?confirm=true", headers=self.h()))["status"], "purged")

    def test_medical_lifecycle(self):
        receipt = self.ok(self.upload("/v1/medical/receipts", name="receipt.txt", content=b"Amount paid: $12.50\nService date: 01/02/2026\n", token="writer", source_title="Portal"))
        rid, att_id = receipt["id"], receipt["attachment_id"]
        listed = self.ok(self.client.get("/v1/medical/receipts?inbox=true", headers=self.h("revealer")))
        self.assertEqual(listed[0]["id"], rid)
        self.assertEqual(listed[0]["receipt"]["fields"]["amount"], "12.50")
        self.ok(self.client.patch(f"/v1/medical/receipts/{rid}/fields", headers=self.h("writer"), json={"fields": {"provider": "Clinic"}}))
        self.assertEqual(self.ok(self.client.post(f"/v1/medical/receipts/{rid}/review", headers=self.h("writer")))["status"], "reviewed")
        self.assertEqual(self.ok(self.client.post(f"/v1/medical/receipts/{rid}/reopen", headers=self.h("writer")))["status"], "inbox")
        report = self.ok(self.client.post("/v1/medical/report", headers=self.h("revealer"), json={"year": ""}))
        self.assertEqual(report["count"], 1)
        content = self.client.get(f"/v1/medical/receipts/{rid}/attachments/{att_id}/content", headers=self.h("revealer"))
        self.assertTrue(content.content.startswith(b"Amount paid"))
        self.assertIn("Amount paid", self.ok(self.client.get(f"/v1/medical/receipts/{rid}/extracted-text", headers=self.h("revealer")))["text"])
        self.ok(self.client.post(f"/v1/medical/receipts/{rid}/trash", headers=self.h("writer")))
        self.ok(self.client.post(f"/v1/medical/receipts/{rid}/restore", headers=self.h("writer")))
        self.ok(self.client.post(f"/v1/medical/receipts/{rid}/trash", headers=self.h("writer")))
        bulk = self.client.post("/v1/medical/receipts/purge", headers=self.h(), json={"ids": [rid], "confirm": True})
        self.assertEqual(self.ok(bulk)["count"], 1)

    def test_medical_people_reimbursement_and_report(self):
        receipt = self.ok(self.upload("/v1/medical/receipts", name="receipt.txt", content=b"Patient: DOE, JANE\nAmount paid: $12.50\nService date: 01/02/2026\n", token="writer"))
        rid = receipt["id"]
        listed = self.ok(self.client.get("/v1/medical/receipts", headers=self.h("revealer")))[0]["receipt"]
        self.assertEqual((listed["schema"], listed["status"]), (2, "needs_review"))
        self.assertEqual(listed["fields"]["service_date"], "2026-01-02")
        self.assertEqual(listed["field_raw"]["service_date"], "01/02/2026")

        self.assertEqual(self.client.get("/v1/medical/people", headers=self.h("reader")).status_code, 403)
        self.assertEqual(self.client.post("/v1/medical/people", headers=self.h("revealer"), json={"add": ["Jane Doe"]}).status_code, 403)
        self.assertEqual(self.client.post("/v1/medical/people", headers=self.h("writer"), json={}).status_code, 400)
        self.assertNotIn("Jane", self.client.post("/v1/medical/people", headers=self.h("writer"), json={"add": ["Jane Doe"]}).text)
        self.assertEqual(self.ok(self.client.get("/v1/medical/people", headers=self.h("revealer")))["people"], ["Jane Doe"])

        too_early = self.client.post("/v1/medical/receipts/reimburse", headers=self.h("writer"), json={"ids": [rid], "date": "2026-02-01"})
        self.assertEqual(too_early.status_code, 400)
        self.assertEqual(self.client.patch(f"/v1/medical/receipts/{rid}/fields", headers=self.h("writer"), json={"fields": {"paid_date": "whenever"}}).status_code, 400)
        self.ok(self.client.post(f"/v1/medical/receipts/{rid}/review", headers=self.h("writer")))
        self.assertEqual(self.client.post("/v1/medical/receipts/reimburse", headers=self.h("revealer"), json={"ids": [rid]}).status_code, 403)
        done = self.client.post("/v1/medical/receipts/reimburse", headers=self.h("writer"), json={"ids": [rid], "date": "2/1/2026", "note": "ref 1"})
        self.assertEqual(done.status_code, 200)
        self.assertNotIn("12.50", done.text)   # the amounts are for someone who may reveal them: the report below

        report = self.ok(self.client.post("/v1/medical/report", headers=self.h("revealer"), json={"year": "2026"}))
        self.assertEqual((report["count"], report["total_amount"], report["unreimbursed_amount"]), (1, "12.50", "0.00"))
        self.assertEqual(report["by_person"], [{"person": "Jane Doe", "count": 1, "total_amount": "12.50", "unreimbursed_amount": "0.00"}])
        self.assertEqual(self.ok(self.client.post("/v1/medical/report", headers=self.h("revealer"), json={"reimbursed": "no"}))["count"], 0)
        self.assertEqual(self.client.post("/v1/medical/report", headers=self.h("revealer"), json={"year": "26"}).status_code, 400)
        self.assertEqual(self.ok(self.client.post("/v1/medical/receipts/reimburse", headers=self.h("writer"), json={"ids": [rid], "undo": True}))["status"], "unreimbursed")

    def test_generic_endpoints_refuse_medical_misuse(self):
        receipt = self.ok(self.upload("/v1/medical/receipts", name="r.txt", content=b"x", token="writer"))
        self.assertEqual(self.client.get(f"/v1/library/notes/{receipt['id']}", headers=self.h()).status_code, 400)
        plain = self.add(title="Not a receipt")
        self.assertEqual(self.client.post(f"/v1/medical/receipts/{plain['id']}/review", headers=self.h()).status_code, 400)


class OpenAPITests(ServerTestCase):
    def test_committed_spec_matches_the_app(self):
        committed = json.loads((Path(__file__).resolve().parents[1] / "docs" / "openapi.json").read_text())
        self.assertEqual(committed, server.openapi_spec(self.app))

    def test_docs_are_not_served(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
