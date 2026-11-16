"""What the web app needs from the API: browser sign-in, receipt detail, previews and paged lists."""
import io
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

RECEIPT = b"Patient: DOE, JANE\nAmount paid: $12.50\nService date: 01/02/2026\n"
MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


@pytest.fixture()
def service(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=kaal.VAULT, tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", max_upload_bytes=8 * 1024 * 1024, rate_per_minute=1000)
    store = tokens.TokenStore(settings.tokens_file)
    secrets = {name: store.create(name, scopes)[1] for name, scopes in {"full": ["read", "reveal", "write", "delete", "admin"], "reader": ["read"]}.items()}
    client = TestClient(server.create_app(settings))

    def bearer(name="full"):
        return {"Authorization": f"Bearer {secrets[name]}"}

    def upload(content=RECEIPT, name="receipt.txt"):
        response = client.post("/v1/medical/receipts", headers=bearer(), files={"file": (name, content, "application/octet-stream")})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    yield SimpleNamespace(client=client, bearer=bearer, upload=upload, secrets=secrets, store=store)
    kaal.configure_vault(saved)


# --- browser sign-in

def test_signing_in_sets_a_cookie_scripts_cannot_read(service):
    assert service.client.post("/v1/session", json={"token": "kaal_nope_nope"}).status_code == 401
    assert service.client.post("/v1/session", json={}).status_code == 400

    response = service.client.post("/v1/session", json={"token": f"  {service.secrets['full']} "})
    assert response.status_code == 200
    assert response.json()["result"] == {"name": "full", "scopes": ["read", "reveal", "write", "delete", "admin"]}
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"kaal_session={service.secrets['full']};")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=2592000"):
        assert attribute in cookie
    assert service.secrets["full"] not in response.text


def test_a_signed_in_browser_can_read_but_must_prove_writes_come_from_the_app(service):
    cookie = {"Cookie": f"kaal_session={service.secrets['full']}"}
    assert service.client.get("/v1/session", headers=cookie).json()["result"]["name"] == "full"
    assert service.client.get("/v1/medical/receipts", headers=cookie).status_code == 200

    # A request another site tricks the browser into sending carries the cookie but not this header.
    forged = service.client.post("/v1/medical/people", headers=cookie, json={"add": ["Jane Doe"]})
    assert forged.status_code == 403
    assert service.client.get("/v1/medical/people", headers=cookie).json()["result"]["people"] == []

    genuine = service.client.post("/v1/medical/people", headers={**cookie, "X-Kaal-Request": "1"}, json={"add": ["Jane Doe"]})
    assert genuine.status_code == 200
    # Tools that send the token themselves are not browsers and need no such header.
    assert service.client.post("/v1/medical/people", headers=service.bearer(), json={"add": ["John Doe"]}).status_code == 200


def test_sessions_follow_the_token_scopes_revocation_and_sign_out(service):
    reader = {"Cookie": f"kaal_session={service.secrets['reader']}"}
    assert service.client.get("/v1/session", headers=reader).json()["result"] == {"name": "reader", "scopes": ["read"]}
    assert service.client.get("/v1/medical/receipts", headers=reader).status_code == 403
    assert service.client.get("/v1/session").status_code == 401
    assert service.client.get("/v1/session", headers={"Cookie": "kaal_session=kaal_made_up"}).status_code == 401

    cleared = service.client.delete("/v1/session", headers=reader)
    assert cleared.status_code == 200 and 'kaal_session="";' in cleared.headers["set-cookie"] and "Max-Age=0" in cleared.headers["set-cookie"]

    # Changing what the token may do reaches a signed-in browser without a new sign-in.
    service.store.set_scopes("reader", ["read", "reveal", "delete"])
    assert service.client.get("/v1/session", headers=reader).json()["result"] == {"name": "reader", "scopes": ["read", "reveal", "delete"]}
    assert service.client.get("/v1/medical/receipts", headers=reader).status_code == 200

    service.store.revoke("reader")
    assert service.client.get("/v1/session", headers=reader).status_code == 401


# --- one receipt, with where each value came from

def test_receipt_detail_shows_the_line_each_value_was_read_from(service):
    captured = service.upload()
    detail = service.client.get(f"/v1/medical/receipts/{captured['id']}", headers=service.bearer()).json()["result"]
    assert detail["note"]["id"] == captured["id"]
    assert detail["note"]["attachments"][0]["id"] == captured["attachment_id"]
    assert (detail["receipt"]["fields"]["amount"], detail["receipt"]["fields"]["service_date"]) == ("12.50", "2026-01-02")
    assert detail["evidence"] == {"amount": "Amount paid: $12.50", "service_date": "Service date: 01/02/2026", "patient_name": "Patient: DOE, JANE"}

    service.client.patch(f"/v1/medical/receipts/{captured['id']}/fields", headers=service.bearer(), json={"fields": {"provider": "Typed Clinic"}})
    after = service.client.get(f"/v1/medical/receipts/{captured['id']}", headers=service.bearer()).json()["result"]
    assert "provider" not in after["evidence"]  # typed by hand: the receipt does not print it

    assert service.client.get(f"/v1/medical/receipts/{captured['id']}", headers=service.bearer("reader")).status_code == 403
    assert service.client.get("/v1/medical/receipts/nope", headers=service.bearer()).status_code == 404
    assert kaal.call_captured(kaal.medical_show, SimpleNamespace(note_id=captured["id"]))[0]["evidence"]["amount"] == "Amount paid: $12.50"


# --- previews

def test_a_photo_preview_is_an_upright_image_small_enough_for_a_browser(service):
    image_module = pytest.importorskip("PIL.Image")
    photo = io.BytesIO()
    image_module.new("RGB", (3000, 4000), "white").save(photo, format="PNG")
    captured = service.upload(photo.getvalue(), name="photo.png")
    url = f"/v1/medical/receipts/{captured['id']}/attachments/{captured['attachment_id']}/preview"

    response = service.client.get(url, headers=service.bearer())
    assert (response.status_code, response.headers["content-type"]) == (200, "image/jpeg")
    with image_module.open(io.BytesIO(response.content)) as shown:
        assert shown.size == (1176, 1568)
    assert response.headers["cache-control"] == "no-store"

    assert service.client.get(url, params={"page": 2}, headers=service.bearer()).status_code == 404
    assert service.client.get(url, headers=service.bearer("reader")).status_code == 403
    assert service.client.get(url.replace(captured["attachment_id"], "nope"), headers=service.bearer()).status_code == 404


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="needs pdftoppm")
def test_a_pdf_preview_is_its_page_rendered_and_text_files_have_none(service):
    captured = service.upload(MINIMAL_PDF, name="receipt.pdf")
    response = service.client.get(f"/v1/medical/receipts/{captured['id']}/attachments/{captured['attachment_id']}/preview", headers=service.bearer())
    assert (response.status_code, response.headers["content-type"]) == (200, "image/png")
    assert response.content.startswith(b"\x89PNG")

    text = service.upload()
    assert service.client.get(f"/v1/medical/receipts/{text['id']}/attachments/{text['attachment_id']}/preview", headers=service.bearer()).status_code == 404


# --- lists

def test_receipt_lists_can_be_filtered_searched_sorted_and_paged(service):
    ids = {}
    for label, amount, when in (("small", "5.00", "01/05/2026"), ("large", "500.00", "03/01/2026"), ("middle", "50.00", "02/01/2026")):
        ids[label] = service.upload(f"Amount paid: ${amount}\nService date: {when}\n".encode(), name=f"{label}.txt")["id"]
    service.client.patch(f"/v1/medical/receipts/{ids['large']}/fields", headers={**service.bearer(), }, json={"fields": {"provider": "Riverside Clinic"}})
    service.client.post(f"/v1/medical/receipts/{ids['middle']}/review", headers=service.bearer())

    def listed(**params):
        body = service.client.get("/v1/medical/receipts", headers=service.bearer(), params=params).json()
        return [note["id"] for note in body["result"]], body["meta"]

    assert listed(sort="amount")[0] == [ids["large"], ids["middle"], ids["small"]]
    assert listed(sort="date")[0] == [ids["large"], ids["middle"], ids["small"]]
    assert listed(status="reviewed") == ([ids["middle"]], {"total": 1, "offset": 0, "limit": 0})
    assert listed(status="needs_review")[1]["total"] == 2
    assert listed(q="riverside")[0] == [ids["large"]]
    assert listed(q=ids["small"][:6].upper())[0] == [ids["small"]]
    page, meta = listed(sort="amount", limit=2, offset=1)
    assert (page, meta) == ([ids["middle"], ids["small"]], {"total": 3, "offset": 1, "limit": 2})
    assert listed(sort="amount", limit=2, offset=5) == ([], {"total": 3, "offset": 5, "limit": 2})
    assert service.client.get("/v1/medical/receipts", headers=service.bearer(), params={"sort": "colour"}).status_code == 400


def test_note_lists_can_be_searched_and_paged(service):
    for title in ("Boiler manual", "Boiler service log", "Garden plan"):
        service.client.post("/v1/library/notes", headers=service.bearer(), json={"title": title, "body": "text", "tags": ["home"]})
    body = service.client.get("/v1/library/notes", headers=service.bearer(), params={"q": "boiler", "limit": 1, "offset": 1}).json()
    assert len(body["result"]) == 1 and body["meta"] == {"total": 2, "offset": 1, "limit": 1}
    assert service.client.get("/v1/library/notes", headers=service.bearer(), params={"q": "HOME"}).json()["meta"]["total"] == 3
    assert service.client.get("/v1/library/notes", headers=service.bearer()).json()["meta"]["total"] == 3


# --- searching inside notes

def make_note(service, title, body, tags=(), files=()):
    note = service.client.post("/v1/library/notes", headers=service.bearer(), json={"title": title, "body": body, "tags": list(tags)}).json()["result"]["note"]
    for name, content in files:
        attached = service.client.post(f"/v1/library/notes/{note['id']}/attachments", headers=service.bearer(), files={"file": (name, content, "application/octet-stream")})
        assert attached.status_code == 200, attached.text
    return note["id"]


def find(service, query, **more):
    response = service.client.post("/v1/library/search", headers=service.bearer(), json={"query": query, **more})
    assert response.status_code == 200, response.text
    return response.json()


def test_notes_are_found_by_their_text_and_by_text_read_from_their_files(service):
    boiler = make_note(service, "Boiler handbook", "Bleed the radiators every autumn.\nThe pressure should read 1.5 bar when cold.", tags=["home"])
    garden = make_note(service, "Garden plan", "Order compost and bean seeds.", files=[("soil-report.txt", b"Soil pH is 6.5.\nThe pressure washer is in the shed.\n")])
    recipe = make_note(service, "Pressure cooker lentils", "Twenty minutes, natural release.")
    service.upload()  # a medical receipt is never among note results

    found = find(service, "PRESSURE")
    assert [note["id"] for note in found["result"]] == [recipe, boiler, garden]  # by title, then own text, then a file
    assert found["meta"] == {"total": 3, "offset": 0, "limit": 50}
    by_id = {note["id"]: note["match"] for note in found["result"]}
    assert by_id[recipe] == {"where": "title"}
    assert by_id[boiler] == {"where": "note", "before": "Bleed the radiators every autumn. The ", "found": "pressure", "after": " should read 1.5 bar when cold."}
    assert by_id[garden]["where"] == "file" and by_id[garden]["file"] == "soil-report.txt" and by_id[garden]["found"] == "pressure"
    assert find(service, "pressure", limit=1, offset=1)["result"][0]["id"] == boiler

    # Every word must be somewhere in the note; quoted words must be together.
    assert [note["id"] for note in find(service, "radiators home")["result"]] == [boiler]
    assert [note["id"] for note in find(service, "compost pH")["result"]] == [garden]
    assert find(service, "compost radiators")["result"] == []
    assert [note["id"] for note in find(service, '"bean seeds"')["result"]] == [garden]
    assert find(service, '"seeds bean"')["result"] == []
    assert find(service, "(pressure")["result"] == [] and find(service, "1.5")["meta"]["total"] == 1  # typed characters are never a pattern


def test_search_follows_edits_the_trash_and_deletion(service):
    note = make_note(service, "Car insurance", "Policy renews in March with Northwind.")
    assert find(service, "northwind")["meta"]["total"] == 1
    service.client.put(f"/v1/library/notes/{note}", headers=service.bearer(), json={"title": "Car insurance", "body": "Policy renews in April with Contoso."})
    assert find(service, "northwind")["meta"]["total"] == 0 and find(service, "contoso")["meta"]["total"] == 1
    service.client.post(f"/v1/library/notes/{note}/attachments", headers=service.bearer(), files={"file": ("schedule.txt", b"Excess is two hundred and fifty.\n", "text/plain")})
    assert find(service, "excess")["result"][0]["match"]["file"] == "schedule.txt"

    service.client.post(f"/v1/library/notes/{note}/trash", headers=service.bearer())
    assert find(service, "contoso")["meta"]["total"] == 0 and find(service, "contoso", trash=True)["meta"]["total"] == 1
    service.client.delete(f"/v1/library/notes/{note}", headers=service.bearer(), params={"confirm": True})
    assert find(service, "contoso", trash=True)["meta"]["total"] == 0
    assert note not in kaal._TEXT_CACHE


def test_searching_inside_notes_needs_the_reveal_scope_and_a_query(service):
    make_note(service, "Secret", "The safe code is written here.")
    refused = service.client.post("/v1/library/search", headers=service.bearer("reader"), json={"query": "safe"})
    assert refused.status_code == 403 and "safe code" not in refused.text
    assert service.client.post("/v1/library/search", json={"query": "safe"}).status_code == 401
    for bad in ({}, {"query": ""}, {"query": "x" * 201}, {"query": "safe", "limit": 1000}, {"query": '  ""  '}):
        response = service.client.post("/v1/library/search", headers=service.bearer(), json=bad)
        assert response.status_code == 400 and "safe code" not in response.text, bad


def test_a_note_is_decrypted_once_until_it_changes(service, monkeypatch):
    note = make_note(service, "Bike service", "Chain replaced and brakes bled.")
    make_note(service, "Other", "Nothing about bicycles.")
    find(service, "brakes")
    reads = []
    original = kaal.load_note
    monkeypatch.setattr(kaal, "load_note", lambda meta: reads.append(meta["id"]) or original(meta))
    assert find(service, "chain")["meta"]["total"] == 1 and reads == []
    service.client.put(f"/v1/library/notes/{note}", headers=service.bearer(), json={"title": "Bike service", "body": "New tyres fitted."})
    reads.clear()
    assert find(service, "tyres")["meta"]["total"] == 1 and reads.count(note) == 1 and len(reads) == 1


# --- files inside notes

def note_with(service, name, content):
    note = service.client.post("/v1/library/notes", headers=service.bearer(), json={"title": "With a file", "body": "text"}).json()["result"]["note"]
    attached = service.client.post(f"/v1/library/notes/{note['id']}/attachments", headers=service.bearer(), files={"file": (name, content, "application/octet-stream")})
    assert attached.status_code == 200, attached.text
    shown = service.client.get(f"/v1/library/notes/{note['id']}", headers=service.bearer()).json()["result"]["note"]
    return f"/v1/notes/{note['id']}/attachments/{shown['attachments'][0]['id']}"


def test_a_file_added_to_a_note_gets_an_id_the_note_text_can_refer_to(service):
    note = service.client.post("/v1/library/notes", headers=service.bearer(), json={"title": "Garden", "body": "text"}).json()["result"]["note"]
    picture = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    added = service.client.post(f"/v1/library/notes/{note['id']}/attachments", headers=service.bearer(), files={"file": ("beds.png", picture, "image/png")}).json()["result"]
    assert added["status"] == "attached" and added["attachment"]["name"] == "beds.png"
    file_id = added["attachment"]["id"]
    assert re.fullmatch(r"[0-9a-f]{12}", file_id)
    body = f"# Garden\n\n![beds](attachment:{file_id})"
    assert service.client.put(f"/v1/library/notes/{note['id']}", headers=service.bearer(), json={"title": "Garden", "body": body}).status_code == 200
    shown = service.client.get(f"/v1/library/notes/{note['id']}", headers=service.bearer()).json()["result"]["note"]
    assert shown["body"].strip() == body and [file["id"] for file in shown["attachments"]] == [file_id]
    assert service.client.get(f"/v1/notes/{note['id']}/attachments/{file_id}/view", headers=service.bearer()).content == picture
    # A trashed note takes no more files.
    service.client.post(f"/v1/library/notes/{note['id']}/trash", headers=service.bearer())
    refused = service.client.post(f"/v1/library/notes/{note['id']}/attachments", headers=service.bearer(), files={"file": ("more.png", picture, "image/png")})
    assert refused.status_code >= 400


@pytest.mark.parametrize("name, content, media_type, sandboxed", [
    ("scan.pdf", MINIMAL_PDF, "application/pdf", False),
    ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png", True),
    ("photo.JPG", b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg", True),
    ("moving.gif", b"GIF89a" + b"\x00" * 16, "image/gif", True),
    ("readings.csv", b"date,value\n", "text/plain; charset=utf-8", True),
    ("notes.txt", b"<script>alert(1)</script>", "text/plain; charset=utf-8", True),
])
def test_pictures_pdfs_and_plain_text_open_in_the_browser(service, name, content, media_type, sandboxed):
    response = service.client.get(note_with(service, name, content) + "/view", headers=service.bearer())
    assert (response.status_code, response.headers["content-type"]) == (200, media_type)
    assert response.content == content
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert ("sandbox" in response.headers.get("content-security-policy", "")) is sandboxed


@pytest.mark.parametrize("name, content", [
    ("saved.htm", b"<html><script>alert(1)</script></html>"),
    ("page.html", b"<html></html>"),
    ("drawing.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("code.js", b"alert(1)"),
    ("letter.odt", b"PK\x03\x04"),
    ("pretend.pdf", b"<html><script>alert(1)</script></html>"),
    ("pretend.png", b"<svg onload=alert(1)>"),
    ("noextension", b"%PDF-1.4"),
])
def test_anything_that_could_run_or_is_not_what_it_claims_is_download_only(service, name, content):
    base = note_with(service, name, content)
    refused = service.client.get(base + "/view", headers=service.bearer())
    assert refused.status_code == 415
    assert content not in refused.content
    download = service.client.get(base + "/content", headers=service.bearer())
    assert (download.status_code, download.headers["content-type"], download.content) == (200, "application/octet-stream", content)


def test_note_file_views_and_previews_need_the_reveal_scope_and_a_real_file(service):
    base = note_with(service, "readings.csv", b"a,b\n")
    assert service.client.get(base + "/view", headers=service.bearer("reader")).status_code == 403
    assert service.client.get(base + "/preview", headers=service.bearer("reader")).status_code == 403
    assert service.client.get(base + "/preview", headers=service.bearer()).status_code == 404  # nothing to picture
    missing = base.rsplit("/", 1)[0] + "/nope"
    assert service.client.get(missing + "/view", headers=service.bearer()).status_code == 404
    assert service.client.get("/v1/notes/nope/attachments/nope/view", headers=service.bearer()).status_code == 404


def test_a_note_picture_preview_is_shrunk_for_the_page(service):
    image_module = pytest.importorskip("PIL.Image")
    photo = io.BytesIO()
    image_module.new("RGB", (4000, 3000), "white").save(photo, format="JPEG")
    response = service.client.get(note_with(service, "photo.jpg", photo.getvalue()) + "/preview", headers=service.bearer())
    assert (response.status_code, response.headers["content-type"]) == (200, "image/jpeg")
    with image_module.open(io.BytesIO(response.content)) as shown:
        assert shown.size == (1568, 1176)
    assert len(response.content) < len(photo.getvalue())


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="needs pdftoppm")
def test_a_note_pdf_preview_is_its_first_page(service):
    response = service.client.get(note_with(service, "scan.pdf", MINIMAL_PDF) + "/preview", headers=service.bearer())
    assert (response.status_code, response.headers["content-type"]) == (200, "image/png")
