"""How the browser extension reaches the server without access to any website."""
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

ROOT = Path(__file__).resolve().parents[1]
OURS = f"chrome-extension://{server.OFFICIAL_EXTENSION_ID}"
OTHER = "chrome-extension://" + "b" * 32
RECEIPT = b"Patient: DOE, JANE\nAmount paid: $12.50\nService date: 01/02/2026\n"


@pytest.fixture()
def service(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=kaal.VAULT, tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", rate_per_minute=1000)
    store = tokens.TokenStore(settings.tokens_file)
    secrets = {name: store.create(name, scopes)[1] for name, scopes in {"full": ["read", "reveal", "write"], "reader": ["read"]}.items()}
    yield SimpleNamespace(client=TestClient(server.create_app(settings)), settings=settings, bearer=lambda name="full": {"Authorization": f"Bearer {secrets[name]}"}, secrets=secrets)
    kaal.configure_vault(saved)


def test_the_servers_extension_id_is_the_one_the_manifest_key_gives():
    key = base64.b64decode(json.loads((ROOT / "chrome-extension" / "manifest.json").read_text())["key"])
    derived = "".join(chr(ord("a") + int(digit, 16)) for digit in hashlib.sha256(key).hexdigest()[:32])
    assert derived == server.OFFICIAL_EXTENSION_ID


def test_the_copy_from_the_chrome_web_store_is_let_in_as_well():
    assert server.extension_origins({}) == (f"chrome-extension://{server.OFFICIAL_EXTENSION_ID}", f"chrome-extension://{server.STORE_EXTENSION_ID}")
    assert server.STORE_EXTENSION_ID != server.OFFICIAL_EXTENSION_ID and len(server.STORE_EXTENSION_ID) == 32
    # Naming ids oneself still replaces the list, and "none" still allows nothing.
    assert server.extension_origins({"KAAL_EXTENSION_IDS": "a" * 32}) == ("chrome-extension://" + "a" * 32,)
    assert server.extension_origins({"KAAL_EXTENSION_IDS": "none"}) == ()


def test_the_browser_is_told_the_extension_may_call_the_api(service):
    asked = service.client.options("/v1/medical/receipts", headers={"Origin": OURS, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
    assert asked.status_code == 204 and asked.content == b""
    assert asked.headers["access-control-allow-origin"] == OURS and asked.headers["vary"] == "Origin"
    assert "POST" in asked.headers["access-control-allow-methods"] and "DELETE" in asked.headers["access-control-allow-methods"]
    for name in ("Authorization", "Content-Type", "X-Kaal-Request"):
        assert name in asked.headers["access-control-allow-headers"]

    answered = service.client.get("/v1/session", headers={**service.bearer(), "Origin": OURS})
    assert answered.status_code == 200 and answered.headers["access-control-allow-origin"] == OURS
    assert answered.headers["access-control-expose-headers"] == "X-Kaal-Version"
    # A refusal is readable too, so the extension can say what went wrong.
    refused = service.client.get("/v1/session", headers={"Origin": OURS})
    assert refused.status_code == 401 and refused.headers["access-control-allow-origin"] == OURS


def test_no_other_page_is_named_and_cookies_never_cross(service):
    for origin in (OTHER, "https://example.com", "null", "chrome-extension://" + server.OFFICIAL_EXTENSION_ID.upper()):
        asked = service.client.options("/v1/medical/receipts", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        answered = service.client.get("/v1/session", headers={**service.bearer(), "Origin": origin})
        for response in (asked, answered):
            assert not [name for name in response.headers if name.startswith("access-control-")], origin
        assert answered.status_code == 200  # a tool that is not a browser page is not affected
    # Only the API is opened; the web app's own files are not.
    assert "access-control-allow-origin" not in service.client.get("/", headers={"Origin": OURS}).headers
    assert "access-control-allow-origin" not in service.client.get("/web/app.js", headers={"Origin": OURS}).headers
    # Without "allow credentials" a browser will not send or accept a cookie across origins.
    for response in (service.client.options("/v1/session", headers={"Origin": OURS, "Access-Control-Request-Method": "GET"}),
                     service.client.get("/v1/session", headers={**service.bearer(), "Origin": OURS})):
        assert "access-control-allow-credentials" not in response.headers


def test_which_extensions_are_allowed_can_be_changed_or_switched_off(service, tmp_path):
    assert server.extension_origins({}) == server.OUR_EXTENSIONS and OURS in server.OUR_EXTENSIONS
    assert server.extension_origins({"KAAL_EXTENSION_IDS": " none "}) == ()
    assert server.extension_origins({"KAAL_EXTENSION_IDS": f"{'b' * 32}, {'c' * 32}"}) == (OTHER, "chrome-extension://" + "c" * 32)
    for bad in ("https://example.com", "abc", "B" * 32, f"{'b' * 32},*"):
        with pytest.raises(server.ServerConfigError):
            server.extension_origins({"KAAL_EXTENSION_IDS": bad})
    closed = TestClient(server.create_app(server.Settings(home=tmp_path, vault=kaal.VAULT, tokens_file=service.settings.tokens_file, tmp_dir=tmp_path / "tmp2", extension_origins=())))
    assert "access-control-allow-origin" not in closed.get("/v1/session", headers={**service.bearer(), "Origin": OURS}).headers


def test_receipt_sites_are_host_names_only_newest_first(service):
    def capture(address):
        response = service.client.post("/v1/medical/receipts", headers=service.bearer(), files={"file": ("receipt.txt", RECEIPT, "text/plain")}, data={"source_url": address, "source_title": "Payment confirmed"})
        assert response.status_code == 200, response.text
        return response.json()["result"]["id"]

    capture("https://www.portal.example.org/pay/receipt?id=42&token=secret")
    trashed = capture("https://billing.example.net/r/7")
    capture("file:///Users/someone/Downloads/receipt.pdf")
    capture("")
    index = kaal.load_index()
    for position, note in enumerate(index["notes"]):
        note["created"] = f"2026-01-0{position + 1}T00:00:00+00:00"
    kaal.save_index(index)
    capture("https://pharmacy.example.com/orders/9")
    assert service.client.get("/v1/medical/sources", headers=service.bearer()).json()["result"] == {"hosts": ["pharmacy.example.com", "billing.example.net", "portal.example.org"]}
    service.client.post(f"/v1/medical/receipts/{trashed}/trash", headers=service.bearer())
    answer = service.client.get("/v1/medical/sources", headers=service.bearer())
    assert answer.json()["result"] == {"hosts": ["pharmacy.example.com", "portal.example.org"]}
    assert "secret" not in answer.text and "receipt?id" not in answer.text and "Payment confirmed" not in answer.text
    assert service.client.get("/v1/medical/sources", headers=service.bearer("reader")).status_code == 403
    assert service.client.get("/v1/medical/sources").status_code == 401


def test_an_app_is_told_whether_the_vault_holds_receipts_and_how_many_wait(service):
    summary = lambda: service.client.get("/v1/medical/summary", headers=service.bearer())
    # A vault of notes only has no receipts section to show.
    assert service.client.post("/v1/captures/text", headers=service.bearer(), json={"text": "a note", "title": "A note"}).status_code in (200, 201)
    assert summary().json()["result"] == {"any": False, "to_review": 0}
    first = service.client.post("/v1/medical/receipts", headers=service.bearer(), files={"file": ("receipt.txt", RECEIPT, "text/plain")}).json()["result"]["id"]
    second = service.client.post("/v1/medical/receipts", headers=service.bearer(), files={"file": ("other.txt", RECEIPT + b"\nSecond", "text/plain")}).json()["result"]["id"]
    assert summary().json()["result"] == {"any": True, "to_review": 2}
    # One checked: it is still a receipt, and no longer waits.
    assert service.client.post(f"/v1/medical/receipts/{first}/review", headers=service.bearer()).status_code == 200
    assert summary().json()["result"] == {"any": True, "to_review": 1}
    # One in the Trash does not wait either, but the section stays, so the Trash can be reached.
    assert service.client.post(f"/v1/medical/receipts/{second}/trash", headers=service.bearer()).status_code == 200
    assert service.client.post(f"/v1/medical/receipts/{first}/trash", headers=service.bearer()).status_code == 200
    answer = summary()
    assert answer.json()["result"] == {"any": True, "to_review": 0}
    # Two counts and nothing of what a receipt says.
    assert "Amount" not in answer.text and "receipt.txt" not in answer.text
    assert service.client.get("/v1/medical/summary", headers=service.bearer("reader")).status_code == 403
    assert service.client.get("/v1/medical/summary").status_code == 401
