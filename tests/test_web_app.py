"""The web app is static files served by kaal-server; these tests guard how it is served and what it calls."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "kaal" / "web"
SCRIPTS = sorted(WEB.rglob("*.js"))


@pytest.fixture()
def client(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    settings = server.Settings(home=tmp_path, vault=kaal.VAULT, tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp")
    tokens.TokenStore(settings.tokens_file)
    yield TestClient(server.create_app(settings))
    kaal.configure_vault(saved)


def test_the_page_is_served_without_a_token_and_may_run_only_its_own_scripts(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    policy = response.headers["content-security-policy"]
    for rule in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'", "form-action 'none'", "base-uri 'none'"):
        assert rule in policy
    assert "unsafe" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_every_response_names_the_server_version_so_an_open_page_can_tell_it_is_stale(client):
    from kaal import __version__
    for path in ("/", "/web/app.js", "/v1/health", "/v1/session", "/nowhere"):
        assert client.get(path).headers["x-kaal-version"] == __version__, path
    api = (WEB / "api.js").read_text()
    assert 'response.headers.get("X-Kaal-Version")' in api
    assert "updateWaiting()" in (WEB / "app.js").read_text() and "location.reload()" in (WEB / "app.js").read_text()


def test_the_page_has_no_inline_script_or_style_for_that_policy_to_block():
    html = (WEB / "index.html").read_text()
    assert re.findall(r"<script(?![^>]*\bsrc=)", html) == []
    assert "<style" not in html and " style=" not in html and " onclick=" not in html.lower()
    for script in SCRIPTS:
        text = script.read_text()
        assert not re.search(r"\.style\b|setAttribute\(\s*[\"']style", text), f"{script.name} sets inline style"


@pytest.mark.parametrize("path, media_type", [
    ("/web/app.js", "text/javascript; charset=utf-8"),
    ("/web/api.js", "text/javascript; charset=utf-8"),
    ("/web/views/review.js", "text/javascript; charset=utf-8"),
    ("/web/views/notes.js", "text/javascript; charset=utf-8"),
    ("/web/markdown.js", "text/javascript; charset=utf-8"),
    ("/web/views/settings.js", "text/javascript; charset=utf-8"),
    ("/web/views/trash.js", "text/javascript; charset=utf-8"),
    ("/web/app.css", "text/css; charset=utf-8"),
    ("/web/app.webmanifest", "application/manifest+json"),
    ("/web/icon.svg", "image/svg+xml"),
    ("/web/icon-32.png", "image/png"),
    ("/web/icon-512.png", "image/png"),
    # Asked for by browsers and phones whatever the page declares.
    ("/favicon.ico", "image/png"),
    ("/apple-touch-icon.png", "image/png"),
    ("/apple-touch-icon-precomposed.png", "image/png"),
    ("/robots.txt", "text/plain; charset=utf-8"),
])
def test_assets_are_served_with_their_types(client, path, media_type):
    response = client.get(path)
    assert (response.status_code, response.headers["content-type"]) == (200, media_type)
    assert "content-security-policy" in response.headers


@pytest.mark.parametrize("path", ["/web/missing.js", "/web/../server.py", "/web/%2e%2e/server.py", "/web/views/../../core.py", "/web/app.py", "/web/.hidden.js", "/web/views", "/web/"])
def test_nothing_outside_the_web_folder_is_served(client, path):
    assert client.get(path).status_code == 404


def test_every_script_the_page_imports_exists():
    for script in SCRIPTS:
        for imported in re.findall(r"""from\s+["'](/web/[^"']+)["']""", script.read_text()):
            assert (ROOT / "kaal" / imported.lstrip("/")).is_file(), f"{script.name} imports {imported}"
    assert (WEB / "app.js").is_file()


def test_every_request_the_app_makes_is_in_the_published_api():
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text())["paths"]
    published = {(method.upper(), re.sub(r"\{[^}]+\}", "{}", path)) for path, methods in spec.items() for method in methods}
    api = (WEB / "api.js").read_text()
    calls = re.findall(r"""request\("([A-Z]+)",\s*(?:"([^"]+)"|`([^`]+)`)""", api)
    urls = re.findall(r"""Url = \([^)]*\) => `([^`]+)`""", api)
    made = {(method, re.sub(r"\$\{[^}]+\}", "{}", plain or template)) for method, plain, template in calls}
    made |= {("GET", re.sub(r"\$\{[^}]+\}", "{}", url).split("?")[0]) for url in urls}
    assert len(made) >= 25
    assert made - published == set()
    # No other file makes requests of its own.
    for script in SCRIPTS:
        if script.name != "api.js":
            text = script.read_text()
            assert "fetch(" not in text and "XMLHttpRequest" not in text and "sendBeacon" not in text, f"{script.name} makes requests itself"


def test_vault_text_is_never_written_into_the_page_as_markup():
    for script in SCRIPTS:
        text = script.read_text()
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert risky not in text, f"{script.name} uses {risky}"


def test_the_editor_and_the_renderer_agree_on_how_a_note_refers_to_its_files():
    notes = (WEB / "views" / "notes.js").read_text()
    assert "](attachment:${fileId})" in notes and "reference(file, file.id)" in notes
    assert "attachment:([0-9a-fA-F]{6,32}|" in (WEB / "markdown.js").read_text()
    # New notes and edited ones can take files, and a note can be previewed before it is saved.
    assert "api.attachNoteFile(" in notes and "renderMarkdown(body.value" in notes
    assert '"/v1/library/notes/${id(noteId)}/attachments"'.replace('"', "`") in (WEB / "api.js").read_text()


def test_delete_for_good_is_offered_only_to_a_device_allowed_to_do_it():
    for name in ("notes.js", "trash.js"):
        text = (WEB / "views" / name).read_text()
        assert '.includes("delete")' in text, name
        assert re.search(r"mayDelete \? twoStepButton\(\"Delete", text), name


def test_a_new_notes_files_can_be_placed_before_saving_and_a_linked_pdf_is_shown_once():
    notes = (WEB / "views" / "notes.js").read_text()
    # Stand-in ids are swapped for the real ones after the files are stored.
    assert "id: `new${++chosenSoFar}`" in notes and "(attachment:${waiting[position].id})" in notes
    assert "new\\d{1,4}" in (WEB / "markdown.js").read_text()
    # Cards under the note are only for PDFs the text does not mention.
    assert 'isPdf(file) && !shownInText.has(file.id)' in notes and 'a.pdf-link' in notes
    assert "save first, then edit the note" not in notes


def test_search_words_travel_in_the_request_body_and_results_are_shown_as_text():
    api = (WEB / "api.js").read_text()
    assert 'request("POST", "/v1/library/search", { json: search })' in api
    notes = (WEB / "views" / "notes.js").read_text()
    assert "api.searchNotes({ query, trash" in notes and "api.notes({ trash, limit" in notes
    assert 'h("mark", {}, match.found)' in notes and "turn !== latest" in notes


def test_the_icons_the_page_and_the_manifest_name_exist(client):
    manifest = json.loads((WEB / "app.webmanifest").read_text())
    named = [icon["src"] for icon in manifest["icons"]] + re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="([^"]+)"', (WEB / "index.html").read_text())
    assert len(named) >= 5
    for address in named:
        assert client.get(address).status_code == 200, address
    assert client.get("/apple-touch-icon.png").content == (WEB / "icon-180.png").read_bytes()
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"


def test_the_web_files_are_packaged_with_kaal():
    assert '"web/*"' in (ROOT / "pyproject.toml").read_text()


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to parse the scripts")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(script, tmp_path):
    module = tmp_path / (script.stem + ".mjs")
    module.write_text(script.read_text())
    done = subprocess.run(["node", "--check", str(module)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_notes_are_the_first_screen_and_receipts_are_one_section():
    app = (WEB / "app.js").read_text()
    # An address that names no page is the notes, and so is the page after signing in.
    assert '|| "notes").split("?")' in app and 'go("receipts")' not in app.replace('if (name === "receipt") go("receipts");', "")
    tabs = app[app.index("function tabs(active)"):app.index("function shell(")]
    # Notes first; receipts only once the vault holds one, or while on a receipts page; no tab for totals.
    assert tabs.index('tab("notes", "Notes", "notes")') < tabs.index('tab("receipts", "Receipts", "receipts", waiting)') < tabs.index('tab("settings", "Settings", "settings")')
    assert 'known.any || active === "receipts" ?' in tabs and '"totals", "Totals"' not in app
    # Totals are a view inside the receipts section, beside its two lists.
    assert 'name === "totals"' in app and app[app.index('name === "totals"'):].index('shell("receipts", content)') < 80
    inbox = (WEB / "views" / "inbox.js").read_text()
    assert inbox.index('tab("review", "To review", "receipts"') < inbox.index('tab("reviewed", "Reviewed"') < inbox.index('tab("totals", "Totals", "totals")')
    assert 'receiptSections("totals")' in (WEB / "views" / "totals.js").read_text()
    # What is left to set up is shown above the notes, and speaks of a note.
    assert 'shell("notes", [name === "notes" && params.trash === "1" ? null : setupListCard(context), content])' in app
    assert 'title: "Add your first note"' in (WEB / "views" / "setup.js").read_text()


def test_the_notes_screen_adds_a_note_in_one_press_and_the_rest_from_a_menu():
    notes = (WEB / "views" / "notes.js").read_text()
    adding = notes[notes.index("// Adding: writing a note is the one press"):notes.index("export function noteView(")]
    assert 'href: "#/notes/new", class: "button primary add-main" }, "New note")' in adding
    assert adding.index('choice("Files as a note"') < adding.index('choice("Medical receipt"'), "a receipt is the last thing offered, not the first"
    # A receipt added here is a receipt, checked on its own screen; files become a note the editor opens with.
    assert "await api.capture(file);" in adding and "go(`receipt/${encodeURIComponent(captured.id)}`)" in adding
    assert 'handedOver = chosen;' in adding and 'go("notes/new");' in adding
    # The menu can be used from the keyboard and says what it is.
    assert '"aria-haspopup": "menu"' in adding and 'role: "menuitem"' in adding and 'event.key === "Escape"' in adding and '"ArrowDown"' in adding
    assert "trash || !mayWrite ? null" in adding, "nothing to add is offered to someone who may only read"
    settings = (WEB / "views" / "settings.js").read_text()
    drawn = settings[settings.index("const forReceipts ="):settings.index("  replace(container, h(\"p\", { class: \"empty\" }, \"Loading…\"));")]
    assert drawn.index("accountSection(account") < drawn.rindex("forReceipts"), "one's own account comes before what only receipts need"
    assert '"Medical receipts"' in drawn and "!hasReceipts ? []" in drawn
