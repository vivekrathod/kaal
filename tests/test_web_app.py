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
    ("/web/views/settings.js", "text/javascript; charset=utf-8"),
    ("/web/views/trash.js", "text/javascript; charset=utf-8"),
    ("/web/app.css", "text/css; charset=utf-8"),
    ("/web/app.webmanifest", "application/manifest+json"),
    ("/web/icon.svg", "image/svg+xml"),
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
    # No other file reaches the API directly.
    for script in SCRIPTS:
        if script.name != "api.js":
            assert "/v1/" not in script.read_text(), f"{script.name} calls the API itself"


def test_vault_text_is_never_written_into_the_page_as_markup():
    for script in SCRIPTS:
        text = script.read_text()
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert risky not in text, f"{script.name} uses {risky}"


def test_the_web_files_are_packaged_with_kaal():
    assert '"web/*"' in (ROOT / "pyproject.toml").read_text()


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to parse the scripts")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(script, tmp_path):
    module = tmp_path / (script.stem + ".mjs")
    module.write_text(script.read_text())
    done = subprocess.run(["node", "--check", str(module)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
