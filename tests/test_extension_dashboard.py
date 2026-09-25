from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = ROOT / "chrome-extension" / "inbox.html"
DASHBOARD_JS = ROOT / "chrome-extension" / "inbox.js"


def test_dashboard_hidden_sections_override_component_display_rules() -> None:
    html = DASHBOARD_HTML.read_text(encoding="utf-8")

    assert "[hidden] { display: none !important; }" in html


def test_medical_receipt_fields_are_edited_inline_instead_of_prompted() -> None:
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    javascript = DASHBOARD_JS.read_text(encoding="utf-8")

    assert ".receipt-editor" in html
    assert 'form.className = "receipt-editor"' in javascript
    assert 'window.prompt(label' not in javascript
    assert 'action: "update"' in javascript


def test_dashboard_uses_simple_content_navigation_and_status_filters() -> None:
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    javascript = DASHBOARD_JS.read_text(encoding="utf-8")

    assert '>Notes</button>' in html
    assert '>Medical Receipts</button>' in html
    assert "All notes" not in html
    assert "All receipts" not in html
    assert '>Reviewed</button>' in html
    assert "libraryActiveButton" not in javascript
    assert 'selectView("reviewed")' in javascript


def test_receipt_cards_offer_the_managed_original_for_download() -> None:
    javascript = DASHBOARD_JS.read_text(encoding="utf-8")

    assert 'actionButton("Download original"' in javascript
    assert 'action: "download-original"' in javascript
    assert "attachmentId: attachment.id" in javascript
    assert "attachmentName: attachment.name" in javascript
