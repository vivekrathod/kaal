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
    # One justification for each permission the manifest asks for, and none for one it does not.
    reasons = dict(re.findall(r"^\*\*([A-Za-z]+)[^*\n]*\*\*\n\n```text\n(.*?)\n```", listing, re.M | re.S))
    assert sorted(reasons) == sorted([*manifest["permissions"], "Host"]) and manifest["optional_host_permissions"] == ["*://*/*"]
    for permission, reason in reasons.items():
        assert 0 < len(reason) <= 1000, f"the reason for {permission} is {len(reason)} characters; the store takes 1000"
    purpose = re.search(r"### Single purpose\n\n```text\n(.*?)\n```", listing, re.S)[1]
    assert 0 < len(purpose) <= 1000
    # What the identity permission is said to be for stays true: one call, to open Kaal's own sign-in page.
    scripts = "\n".join(path.read_text() for path in EXT.glob("*.js"))
    assert re.findall(r"chrome\.identity\.(\w+)", scripts) == ["launchWebAuthFlow"]
    assert "oauth2" not in manifest and "identity.email" not in manifest["permissions"]


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


def test_a_built_package_is_unpacked_and_checked(tmp_path):
    package = package_module()
    built = package.build(tmp_path / "kaal.zip")
    assert package.check(tmp_path / "kaal.zip") == built
    version = json.loads((EXT / "manifest.json").read_text())["version"]

    def rebuilt(name, change):
        """The good zip with one thing wrong."""
        with zipfile.ZipFile(tmp_path / "kaal.zip") as good, zipfile.ZipFile(tmp_path / name, "w") as bad:
            for entry in good.namelist():
                data = change(entry, good.read(entry))
                if data is not None:
                    bad.writestr(entry, data)
            for entry, data in (change(None, None) or {}).items():
                bad.writestr(entry, data)
        return tmp_path / name

    def older(entry, data):
        return data.replace(f'"version": "{version}"'.encode(), b'"version": "0.0.1"') if entry == "manifest.json" else data

    def with_key(entry, data):
        return data.replace(b'"manifest_version": 3,', b'"manifest_version": 3, "key": "abc",') if entry == "manifest.json" else data

    for name, change, told in (
            ("missing.zip", lambda entry, data: None if entry == "lib.js" else data, "lib.js"),
            ("no-icon.zip", lambda entry, data: None if entry == "icons/icon-48.png" else data, "icon-48.png"),
            ("older.zip", older, "version"),
            ("keyed.zip", with_key, "key"),
            ("extra.zip", lambda entry, data: {"store/listing.md": b"x"} if entry is None else data, "store/listing.md"),
            ("changed.zip", lambda entry, data: data + b"\n// changed" if entry == "popup.js" else data, "popup.js")):
        with pytest.raises(package.PackageError, match=told):
            package.check(rebuilt(name, change))


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
    # The stores are given these three addresses; the listings name them the same way.
    for listing in (APP_STORE / "listing.md", WEB_STORE / "listing.md"):
        text = listing.read_text()
        assert "https://OWNER.github.io/REPO/privacy.html" in text and "https://OWNER.github.io/REPO/support.html" in text


def test_the_privacy_policy_covers_what_each_part_asks_for():
    policy = (ROOT / "docs" / "privacy.md").read_text()
    manifest = json.loads((EXT / "manifest.json").read_text())
    # Each permission of the extension, by the name Chrome shows for it.
    shown = {"activeTab": "Active tab", "alarms": "alarms", "contextMenus": "context menus", "debugger": "Debugger",
             "identity": "Identity", "sidePanel": "Side panel", "storage": "storage"}
    assert sorted(shown) == sorted(manifest["permissions"]), "a permission was added or removed: say so in docs/privacy.md"
    for permission, name in shown.items():
        assert f"**{name}**" in policy, f"docs/privacy.md does not explain {permission}"
    # Each thing the iPhone app asks permission for.
    project = (ROOT / "ios" / "Kaal.xcodeproj" / "project.pbxproj").read_text()
    asked = set(re.findall(r"INFOPLIST_KEY_NS(\w+)UsageDescription", project))
    assert asked == {"Camera", "Microphone", "SpeechRecognition"}, "the app asks for something new: say so in docs/privacy.md and ios/AppStore/"
    for name in ("**Camera**", "**Microphone and Speech Recognition**", "**Photos and files**"):
        assert name in policy
    # The session cookie by the name the server gives it, and the ways out that the web app offers.
    from kaal import server
    assert f"`{server.HOST_SESSION_COOKIE}`" in policy and f"`{server.SESSION_COOKIE}`" in policy
    settings = (ROOT / "kaal" / "web" / "views" / "settings.js").read_text() + (ROOT / "kaal" / "web" / "views" / "access.js").read_text()
    for offered in ("Delete my account", "Delete this vault", "Download a copy"):
        assert offered in settings and offered in policy, offered
    for choice in ("ollama", "anthropic", "claude-cli", "openai"):
        assert f"`{choice}`" in policy


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
