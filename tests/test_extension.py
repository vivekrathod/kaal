"""The Chrome extension is plain files; these tests guard what it may do and what it shares with the web app."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "chrome-extension"
WEB = ROOT / "kaal" / "web"
OWN_SCRIPTS = sorted(EXT.glob("*.js"))
PAGES = sorted(EXT.glob("*.html"))
SHARED = ("api.js", "ui.js", "app.css", "views/review.js")
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def manifest():
    return json.loads((EXT / "manifest.json").read_text())


def test_the_extension_asks_for_what_it_uses_and_nothing_from_the_old_design():
    data = manifest()
    assert data["manifest_version"] == 3
    assert sorted(data["permissions"]) == ["alarms", "contextMenus", "debugger", "sidePanel", "storage"]
    assert data["host_permissions"] == ["<all_urls>"]
    assert data["background"] == {"service_worker": "background.js", "type": "module"}
    assert data["action"]["default_popup"] == "popup.html" and data["options_ui"]["page"] == "options.html"
    assert set(data["commands"]) == {"_execute_action", "save"}
    assert "content_scripts" not in data and "externally_connectable" not in data and "content_security_policy" not in data
    everything = "\n".join(path.read_text() for path in [*OWN_SCRIPTS, *PAGES, EXT / "manifest.json"])
    for retired in ("nativeMessaging", "sendNativeMessage", "chrome.downloads", '"downloads"', "window.prompt", "inbox.html", "error.html"):
        assert retired not in everything, retired
    for gone in ("inbox.html", "inbox.js", "error.html", "error.js"):
        assert not (EXT / gone).exists(), gone
    assert not (ROOT / "bin" / "kaal-receipt-host.py").exists() and not (ROOT / "bin" / "install-chrome-receipt-capture.py").exists()


def test_the_side_panel_shares_the_web_apps_own_files():
    for name in SHARED:
        assert (EXT / "web" / name).read_bytes() == (WEB / name).read_bytes(), f"run bin/sync-extension-web.py: {name} differs"
    copied = sorted(str(path.relative_to(EXT / "web")) for path in (EXT / "web").rglob("*") if path.is_file())
    assert copied == sorted(SHARED)
    panel = (EXT / "panel.js").read_text()
    assert 'from "/web/views/review.js"' in panel and "connect(server, token)" in panel and "backLink: null" in panel


def test_shared_screens_never_point_a_picture_or_link_straight_at_the_server():
    # The extension has no sign-in cookie: a file must go through picture() or fileLink().
    review = (WEB / "views" / "review.js").read_text()
    assert "picture(previewUrl(" in review and "fileLink(original" in review
    assert not re.search(r"(src|href):\s*(previewUrl|originalUrl|original)\b", review)
    api = (WEB / "api.js").read_text()
    assert 'credentials: bearer ? "omit" : "same-origin"' in api


def test_pages_load_only_the_extensions_own_files_and_build_text_as_text():
    for page in PAGES:
        html = page.read_text()
        assert re.findall(r"<script(?![^>]*\bsrc=)", html) == [], page.name
        assert "<style" not in html and " style=" not in html and not re.search(r"\son\w+=", html), page.name
        for address in re.findall(r"""(?:src|href)="([^"]+)\"""", html):
            assert not re.match(r"(?:[a-z]+:)?//", address), f"{page.name} loads {address}"
            assert (EXT / address).is_file(), f"{page.name} refers to missing {address}"
    for script in [*OWN_SCRIPTS, *(EXT / "web").rglob("*.js")]:
        text = script.read_text()
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "importScripts", "executeScript"):
            assert risky not in text, f"{script.name} uses {risky}"
        for imported in re.findall(r"""from\s+["']([^"']+)["']""", text):
            target = EXT / imported.lstrip("/") if imported.startswith("/") else script.parent / imported
            assert target.is_file(), f"{script.name} imports {imported}"


def test_every_request_the_worker_makes_is_in_the_published_api():
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text())["paths"]
    worker = (EXT / "background.js").read_text()
    calls = re.findall(r"""call\("([A-Z]+)",\s*"([^"?]+)""", worker)
    assert sorted(set(calls)) == [("GET", "/v1/medical/receipts"), ("POST", "/v1/captures"), ("POST", "/v1/captures/text"), ("POST", "/v1/medical/receipts")]
    for method, path in calls:
        assert method.lower() in spec[path], (method, path)
    # Only the worker and the shared request layer talk to the server.
    for script in OWN_SCRIPTS:
        if script.name != "background.js":
            assert "fetch(" not in script.read_text(), f"{script.name} makes requests itself"
    assert "Authorization: `Bearer ${token}`" in worker and 'credentials: "omit"' in worker


def test_right_click_has_two_entries_and_the_panel_opens_with_the_click():
    worker = (EXT / "background.js").read_text()
    assert re.findall(r"contextMenus\.create\(\{[^}]*title: \"([^\"]+)\"", worker) == ["Save to Kaal", "Save as medical receipt"]
    # Chrome only opens the side panel in the same turn as the click, so it must come before the save.
    handler = worker[worker.index("chrome.contextMenus.onClicked"):worker.index("chrome.commands.onCommand")]
    assert handler.index("openPanel(tab)") < handler.index("save(tab")
    opener = worker[worker.index("function openPanel"):worker.index("// --- menus")]
    assert "await" not in opener


@needs_node
@pytest.mark.parametrize("script", OWN_SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(script, tmp_path):
    module = tmp_path / (script.stem + ".mjs")
    module.write_text(script.read_text())
    done = subprocess.run(["node", "--check", str(module)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def run_node(tmp_path, source):
    shutil.copyfile(EXT / "lib.js", tmp_path / "lib.mjs")
    shutil.copyfile(WEB / "api.js", tmp_path / "api.mjs")
    (tmp_path / "case.mjs").write_text(source)
    done = subprocess.run(["node", str(tmp_path / "case.mjs")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_main_button_follows_the_site_and_addresses_are_checked(tmp_path):
    out = run_node(tmp_path, """
import * as lib from "./lib.mjs";
const sites = lib.rememberSite("https://www.portal.example.org/pay?x=1", ["old.example.com"]);
console.log(JSON.stringify({
  sites,
  again: lib.rememberSite("https://portal.example.org/other", sites),
  ignored: lib.rememberSite("chrome://settings", sites),
  modes: ["https://portal.example.org/receipt/9", "https://news.example.com/", "chrome://extensions", ""].map((address) => lib.chooseMode(address, sites)),
  servers: ["kaal.example.ts.net", " https://kaal.example.ts.net/web/ ", "http://localhost:8765/", "localhost:8765", "http://kaal.example.ts.net",
            "ftp://kaal.example.ts.net", "https://user:pw@kaal.example.ts.net", "javascript:alert(1)", ""].map(lib.normaliseServer),
  pdf: ["https://a.example/x/Bill.PDF?download=1", "https://a.example/bill", "not an address"].map(lib.looksLikePdf),
  blocked: ["chrome://extensions", "about:blank", "https://chromewebstore.google.com/detail/x", "https://a.example/", "file:///Users/x/a.pdf", ""].map(lib.isProtectedPage),
  names: [lib.fileNameFor("Payment: receipt / March", "", "application/pdf"), lib.fileNameFor("", "https://a.example/files/statement%202.pdf", "application/pdf"),
          lib.fileNameFor("photo.JPG", "https://a.example/p", "image/jpeg; q=1"), lib.fileNameFor("", "https://a.example/data.csv", ""), lib.fileNameFor("", "", "")],
  told: [0, 401, 403, 413, 500].map((status) => lib.explain(status, "from the server")),
}));
""")
    assert out["sites"] == ["portal.example.org", "old.example.com"] and out["again"] == out["sites"] and out["ignored"] == out["sites"]
    assert out["modes"] == ["medical", "general", "general", "general"]
    assert out["servers"] == ["https://kaal.example.ts.net", "https://kaal.example.ts.net", "http://localhost:8765", "http://localhost:8765", "", "", "", "", ""]
    assert out["pdf"] == [True, False, False]
    assert out["blocked"] == [True, True, True, False, False, True]
    assert out["names"] == ["Payment receipt March.pdf", "statement 2.pdf", "photo.JPG", "data.csv", "Saved page.bin"]
    assert out["told"][1]["settings"] is True and "token" in out["told"][1]["message"]
    assert out["told"][4]["message"] == "from the server" and out["told"][0]["message"] == "Can't reach Kaal."


@needs_node
def test_the_shared_request_layer_signs_in_with_a_cookie_or_a_token(tmp_path):
    out = run_node(tmp_path, """
import { api, connect, fileAddress, holdsToken } from "./api.mjs";
const seen = [];
globalThis.fetch = async (url, options) => {
  seen.push({ url, auth: (options.headers || {}).Authorization || "", credentials: options.credentials });
  return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ result: { name: "x", scopes: [] } }), blob: async () => new Blob(["x"]) };
};
URL.createObjectURL = () => "blob:made-here";
await api.session();
const cookieFile = await fileAddress("/v1/medical/receipts/a/attachments/b/preview?page=1");
const before = holdsToken();
connect("https://kaal.example.ts.net/", "kaal_made_up");
await api.session();
const tokenFile = await fileAddress("/v1/medical/receipts/a/attachments/b/preview?page=1");
console.log(JSON.stringify({ seen, cookieFile, tokenFile, before, after: holdsToken() }));
""")
    assert out["seen"][0] == {"url": "/v1/session", "auth": "", "credentials": "same-origin"}
    assert out["seen"][1] == {"url": "https://kaal.example.ts.net/v1/session", "auth": "Bearer kaal_made_up", "credentials": "omit"}
    assert out["seen"][2] == {"url": "https://kaal.example.ts.net/v1/medical/receipts/a/attachments/b/preview?page=1", "auth": "Bearer kaal_made_up", "credentials": "omit"}
    assert len(out["seen"]) == 3  # the web app's own file needed no request of its own
    assert out["cookieFile"].startswith("/v1/") and out["tokenFile"] == "blob:made-here"
    assert (out["before"], out["after"]) == (False, True)
