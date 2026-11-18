"""What the App Store and the Chrome Web Store are given stays within their rules,
and still says what the code does."""
import importlib.util
import json
import plistlib
import re
import struct
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_STORE = ROOT / "ios" / "AppStore"
WEB_STORE = ROOT / "chrome-extension" / "store"
EXT = ROOT / "chrome-extension"


def fields(listing: Path) -> dict[str, tuple[int, str]]:
    """Each heading that names a limit, such as "Subtitle (30 characters)", with the first text block under it."""
    found = {}
    for match in re.finditer(r"^#{2,4} ([^\n]+?) \((\d+) characters\)\n(.*?)^```text\n(.*?)\n```", listing.read_text(), re.M | re.S):
        found[match[1]] = (int(match[2]), match[4])
    return found


def png_size(path: Path) -> tuple[int, int, int]:
    """Width, height and colour type, from the PNG's header."""
    head = path.read_bytes()[:26]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", path.name
    width, height = struct.unpack(">II", head[16:24])
    return width, height, head[25]


def package_module():
    spec = importlib.util.spec_from_file_location("package_extension", ROOT / "bin" / "package-extension.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_app_store_text_fits_its_fields():
    found = fields(APP_STORE / "listing.md")
    assert set(found) == {"App name", "Subtitle", "Promotional text", "Description", "Keywords"}
    for name, (limit, text) in found.items():
        assert 0 < len(text) <= limit, f"{name} is {len(text)} characters; the limit is {limit}"
    keywords = found["Keywords"][1]
    assert ", " not in keywords and "kaal" not in keywords.lower()  # spaces waste the limit; the name is searched anyway


def test_the_web_store_text_fits_and_matches_the_manifest():
    manifest = json.loads((EXT / "manifest.json").read_text())
    found = fields(WEB_STORE / "listing.md")
    assert set(found) == {"Name", "Summary", "Description"}
    for name, (limit, text) in found.items():
        assert 0 < len(text) <= limit, f"{name} is {len(text)} characters; the limit is {limit}"
    # The store takes the name and the summary from the manifest itself.
    assert found["Name"][1] == manifest["name"] and found["Summary"][1] == manifest["description"]
    listing = (WEB_STORE / "listing.md").read_text()
    for permission in [*manifest["permissions"], "optional_host_permissions"]:
        assert f"**{permission}" in listing or f"({permission}" in listing, f"no justification for {permission}"


def test_the_screenshots_are_the_sizes_each_store_takes_without_transparency():
    iphone = sorted((APP_STORE / "screenshots").glob("*.png"))
    assert [path.name for path in iphone] == ["1-add.png", "2-receipts.png", "3-review.png", "4-note.png", "5-totals.png", "6-search.png"]
    chrome = sorted(WEB_STORE.glob("*.png"))
    assert len(chrome) == 6
    for path in iphone + chrome:
        wanted = (1320, 2868) if path in iphone else (440, 280) if path.name.startswith("promo") else (1280, 800)
        assert png_size(path) == (*wanted, 2), f"{path.name} must be {wanted[0]} x {wanted[1]}, RGB with no alpha"
        assert path.stat().st_size < 600_000, f"{path.name} is larger than it needs to be"
        assert path.name in (APP_STORE / "listing.md").read_text() + (WEB_STORE / "listing.md").read_text()
    assert png_size(EXT / "icons" / "icon-128.png")[:2] == (128, 128)


def test_the_package_holds_exactly_what_the_extension_uses(tmp_path):
    package = package_module()
    built = package.build(tmp_path / "kaal.zip")
    with zipfile.ZipFile(tmp_path / "kaal.zip") as archive:
        names = sorted(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert names == built and "key" not in manifest
    assert not [name for name in names if name.startswith("store/") or name.endswith((".md", ".sh", ".mjs"))]
    named = [manifest["background"]["service_worker"], manifest["action"]["default_popup"], manifest["options_ui"]["page"],
             *manifest["icons"].values(), *manifest["action"]["default_icon"].values(), "panel.html"]
    assert set(named) <= set(names)
    # Everything else in the folder that the extension runs is reached from the manifest.
    runtime = sorted(str(path.relative_to(EXT)) for path in EXT.rglob("*") if path.is_file()
                     and path.suffix in (".js", ".html", ".css", ".png", ".json") and "store" not in path.parts)
    assert runtime == names
    # The same files make the same zip.
    package.build(tmp_path / "again.zip")
    assert (tmp_path / "kaal.zip").read_bytes() == (tmp_path / "again.zip").read_bytes()


def test_a_missing_file_stops_the_package(tmp_path):
    package = package_module()
    folder = tmp_path / "extension"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"background": {"service_worker": "background.js"}}))
    (folder / "background.js").write_text('import { x } from "./gone.js";\n')
    with pytest.raises(package.PackageError, match="gone.js"):
        package.needed_files(folder)


def test_the_website_pages_link_only_to_what_exists():
    pages = ["index.md", "install.md", "privacy.md", "support.md"]
    index = (ROOT / "docs" / "index.md").read_text()
    for page in pages[1:] + ["openapi.json"]:
        assert f"]({page})" in index, f"index.md does not link {page}"
    for page in pages:
        for target in re.findall(r"\]\(([^)#\s]+)", (ROOT / "docs" / page).read_text()):
            if not target.startswith(("http://", "https://", "mailto:")):
                assert (ROOT / "docs" / target).exists(), f"{page} links {target}, which does not exist"
    assert "layout: kaal" in (ROOT / "docs" / "_config.yml").read_text() and (ROOT / "docs" / "_layouts" / "kaal.html").exists()


def test_the_privacy_manifest_declares_every_api_the_app_must_give_a_reason_for():
    manifest = plistlib.loads((ROOT / "ios" / "Kaal" / "PrivacyInfo.xcprivacy").read_bytes())
    assert manifest["NSPrivacyTracking"] is False and manifest["NSPrivacyCollectedDataTypes"] == [] and manifest["NSPrivacyTrackingDomains"] == []
    declared = {entry["NSPrivacyAccessedAPIType"] for entry in manifest["NSPrivacyAccessedAPITypes"]}
    code = "\n".join(path.read_text() for path in (ROOT / "ios" / "Kaal").rglob("*.swift"))
    # Apple's list of APIs that need a reason, by what using them looks like in Swift.
    needs = {
        "NSPrivacyAccessedAPICategoryUserDefaults": r"\bUserDefaults\b|@AppStorage",
        "NSPrivacyAccessedAPICategoryFileTimestamp": r"creationDate|modificationDate|contentModificationDateKey|attributesOfItem",
        "NSPrivacyAccessedAPICategorySystemBootTime": r"systemUptime|mach_absolute_time",
        "NSPrivacyAccessedAPICategoryDiskSpace": r"volumeAvailableCapacity|systemFreeSize|statfs",
        "NSPrivacyAccessedAPICategoryActiveKeyboards": r"activeInputModes",
    }
    used = {category for category, pattern in needs.items() if re.search(pattern, code)}
    assert used == declared, f"used {sorted(used)}, declared {sorted(declared)}"


def test_the_app_still_uses_only_the_encryption_ios_provides():
    # ios/AppStore/export-compliance.md explains why ITSAppUsesNonExemptEncryption is false. It stops being true
    # if the app starts encrypting anything itself.
    assert plistlib.loads((ROOT / "ios" / "Info.plist").read_bytes())["ITSAppUsesNonExemptEncryption"] is False
    code = "\n".join(path.read_text() for path in (ROOT / "ios").rglob("*.swift"))
    for own_encryption in ("AES.", "ChaChaPoly", "SecKeyCreateEncryptedData", "CCCrypt", "import CommonCrypto"):
        assert own_encryption not in code, f"{own_encryption}: see ios/AppStore/export-compliance.md"
