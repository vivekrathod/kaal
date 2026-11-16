"""The Chrome extension, end to end, in a real browser.

Off by default: it needs a Chrome that still loads an unpacked extension from
the command line (Chrome for Testing or Chromium; ordinary Google Chrome no
longer does), and Node 22 or later. Run it with

    KAAL_E2E=1 .venv/bin/python -m pytest -q tests/test_extension_e2e.py

and point KAAL_E2E_BROWSER at the browser if it is not found by itself.
Everything it touches is throwaway: a vault in a temp folder, a token made for
the run, a made-up receipt page, and a fresh browser profile.
"""
import base64
import functools
import glob
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

import kaal.core as core
from kaal import server, tokens
from tests.test_client import free_port

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "chrome-extension"
DRIVER = ROOT / "tests" / "extension_e2e" / "run.mjs"

RECEIPT_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Lakeside Dental - Payment receipt</title>
<style>body{font:16px sans-serif;margin:40px}h1{background:#234;color:#fff;padding:12px}td{padding:4px 16px 4px 0}</style></head>
<body><h1>LAKESIDE DENTAL GROUP</h1><h2>PAYMENT RECEIPT</h2>
<table><tr><td>Patient:</td><td>Jane Example</td></tr><tr><td>Date of Service:</td><td>10/02/2026</td></tr>
<tr><td>Payment Date:</td><td>10/05/2026</td></tr><tr><td>Insurance:</td><td>Acme Dental Plan</td></tr>
<tr><td>Amount paid:</td><td>$64.50</td></tr><tr><td>Paid by:</td><td>Visa ending 1234</td></tr></table>
<p>Thank you for your payment. This is a made-up receipt for testing.</p></body></html>"""


def made_up_pdf() -> bytes:
    """A one-page PDF with a line of text, written by hand so the test needs no PDF library."""
    stream = b"BT /F1 18 Tf 40 150 Td (Made-up statement - nothing real) Tj ET"
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
               b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 220]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
               b"<</Length %d>>\nstream\n%s\nendstream" % (len(stream), stream), b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def find_browser() -> str | None:
    named = os.environ.get("KAAL_E2E_BROWSER")
    if named:
        return named if Path(named).exists() else None
    home = Path.home()
    patterns = [
        home / "Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        home / "Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        home / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome",
    ]
    found = sorted(path for pattern in patterns for path in glob.glob(str(pattern)))
    return found[-1] if found else shutil.which("chromium") or shutil.which("chromium-browser")


def extension_id() -> str:
    """Chrome derives an unpacked extension's id from the key in its manifest."""
    key = base64.b64decode(json.loads((EXT / "manifest.json").read_text())["key"])
    return "".join(chr(ord("a") + int(digit, 16)) for digit in hashlib.sha256(key).hexdigest()[:32])


BROWSER = find_browser()
pytestmark = [
    pytest.mark.skipif(os.environ.get("KAAL_E2E") != "1", reason="set KAAL_E2E=1 to run the browser test"),
    pytest.mark.skipif(not shutil.which("node"), reason="needs node"),
    pytest.mark.skipif(not BROWSER, reason="needs Chrome for Testing or Chromium (set KAAL_E2E_BROWSER)"),
]


@pytest.fixture()
def stage(tmp_path):
    saved = core.VAULT
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", rate_per_minute=10_000)
    core.configure_vault(settings.vault)
    core.call_captured(core.init_vault, SimpleNamespace())
    _record, token = tokens.TokenStore(settings.tokens_file).create("e2e-extension", ["read", "reveal", "write"])
    kaal_port, site_port, debug_port = free_port(), free_port(), free_port()
    app = uvicorn.Server(uvicorn.Config(server.create_app(settings), host="127.0.0.1", port=kaal_port, log_level="error", access_log=False))
    threading.Thread(target=app.run, daemon=True).start()

    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "receipt.html").write_text(RECEIPT_PAGE)
    (site_dir / "statement.pdf").write_bytes(made_up_pdf())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site_dir))
    handler.log_message = lambda *args: None
    site = http.server.ThreadingHTTPServer(("127.0.0.1", site_port), handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()

    # --use-mock-keychain: without it every web request waits on a hidden macOS Keychain prompt.
    profile = tempfile.mkdtemp(prefix="kaal-e2e-profile-")
    browser = subprocess.Popen(
        [BROWSER, f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}", "--headless=new", "--use-mock-keychain", "--password-store=basic",
         f"--load-extension={EXT}", "--no-first-run", "--no-default-browser-check", "--window-size=1200,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=1).read()
                if app.started:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("the browser or the server did not start")
        yield SimpleNamespace(kaal=f"http://localhost:{kaal_port}", site=f"http://127.0.0.1:{site_port}", token=token, debug_port=debug_port)
    finally:
        browser.terminate()
        try:
            browser.wait(10)
        except subprocess.TimeoutExpired:
            browser.kill()
        shutil.rmtree(profile, ignore_errors=True)
        site.shutdown()
        app.should_exit = True
        core.configure_vault(saved)


def test_connect_save_review_and_close_in_a_real_browser(stage):
    env = {**os.environ, "E2E_KAAL": stage.kaal, "E2E_SITE": stage.site, "E2E_TOKEN": stage.token, "E2E_DEBUG_PORT": str(stage.debug_port), "E2E_EXTENSION_ID": extension_id()}
    done = subprocess.run(["node", str(DRIVER)], env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    out = json.loads(done.stdout)

    # Settings: refuses what is wrong, accepts what is right.
    assert out["popupUnconnected"] == "Connect to Kaal"
    assert "https://" in out["settingsBadAddress"] and "didn't accept that token" in out["settingsBadToken"]
    assert out["settingsGood"] == "Connected as “e2e-extension”." and out["storedServer"] == stage.kaal

    # An ordinary page is saved as a note, from the one main button.
    assert out["popupFirst"] == ["Save to Kaal", "Save as a medical receipt instead"]
    assert out["generalSaved"].startswith("Saved to Kaal.") and out["notesAdded"] == 1 and out["badgeAfterGeneral"] == "OK"
    assert out["generalLink"] == f"{stage.kaal}/#/note/ID"

    # A receipt opens the side panel with the web app's review screen.
    assert out["receiptState"] == {"stage": "review", "mode": "medical", "hasId": True} and out["receiptsAdded"] == 1
    assert out["panelTargets"], "the side panel did not open on a real click"
    review = out["review"]
    assert review["picture"] == [True] and review["backLink"] is False and review["buttons"] == ["Looks right", "Save for later"]
    assert review["openInKaal"] == f"{stage.kaal}/#/receipt/ID" and set(review["fields"]) == {"amount", "service_date", "paid_date", "patient_name", "provider", "insurer"}
    if shutil.which("pdftotext"):
        assert review["fields"]["amount"] == "64.50" and review["fields"]["service_date"] == "2026-10-02"

    # "Looks right" marks it reviewed and the panel lets go of the tab.
    assert out["afterLooksRight"] == "reviewed" and out["stateCleared"] is True
    assert out["badgeAfterClose"]["tab"] == out["badgeAfterClose"]["all"]

    # The site is remembered, and a PDF tab is kept as the file it is.
    assert out["rememberedSites"] == ["127.0.0.1"] and out["popupSecond"][0] == "Save receipt"
    assert out["pdf"]["stage"] == "review" and out["pdf"]["source"] == f"{stage.site}/statement.pdf" and out["pdf"]["fileName"].endswith(".pdf")
