"""What the App Store and the Chrome Web Store are given stays within their rules,
and still says what the code does."""
import importlib.util
import inspect
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


def app_code() -> str:
    """Every Swift file that ends up in the app or an extension of it: all of ios/ but the tests."""
    files = [path for path in sorted((ROOT / "ios").rglob("*.swift")) if not any(part.endswith("Tests") for part in path.relative_to(ROOT).parts)]
    assert len(files) > 20, "the app's code was not found where this test looks for it"
    return "\n".join(path.read_text() for path in files)


def section(text: str, heading: str) -> str:
    """One `## heading` section of a Markdown page."""
    found = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert found, f"no section called {heading!r}"
    return found[1]


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
    assert sorted(reasons) == sorted([*manifest["permissions"], "Host"]) and manifest["optional_host_permissions"] == ["*://*/*", "file:///*"]
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
    assert [path.name for path in iphone] == ["1-notes.png", "2-note.png", "3-search.png", "4-add.png", "5-receipts.png", "6-review.png", "7-totals.png"]
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
    # Wherever the code is: the app's own folder, code shared with an extension, the extension itself.
    code = app_code()
    # Apple's list of APIs that need a reason, by what using them looks like in Swift (any spelling:
    # creationDate, .contentModificationDateKey, fileModificationDate(), URLResourceKey.volumeTotalCapacityKey…).
    # C's stat is told from a function of the app's own called stat by the pointer it is handed.
    needs = {
        "NSPrivacyAccessedAPICategoryUserDefaults": r"\bUserDefaults\b|@AppStorage",
        "NSPrivacyAccessedAPICategoryFileTimestamp": r"creationDate|modificationDate|attributesOfItem|\b(?:f|l)?stat(?:at)?\([^)\n]*&|getattrlist",
        "NSPrivacyAccessedAPICategorySystemBootTime": r"systemUptime|mach_absolute_time|mach_continuous_time",
        "NSPrivacyAccessedAPICategoryDiskSpace": r"volume\w*Capacity|systemFreeSize|systemSize|statv?fs",
        "NSPrivacyAccessedAPICategoryActiveKeyboards": r"activeInputModes",
    }
    used = {category for category, pattern in needs.items() if re.search(pattern, code, re.I)}
    assert used == declared, f"used {sorted(used)}, declared {sorted(declared)}"
    for category, pattern in needs.items():
        for spelling in {"FileTimestamp": ["values.contentModificationDate", ".fileModificationDate()", "[.creationDateKey]", "stat(path, &info)", "fstat(fd, &info)"],
                         "DiskSpace": ["volumeTotalCapacityKey", "volumeAvailableCapacityForImportantUsage", "statfs(path, &info)"]}.get(category.rsplit("Category", 1)[1], []):
            assert re.search(pattern, spelling, re.I), f"{spelling} would not be noticed"


def test_what_is_said_about_passwords_matches_the_apps_own_fields():
    code = app_code()
    policy = section((ROOT / "docs" / "privacy.md").read_text(), "Kaal for iPhone")
    said = {"docs/privacy.md, Kaal for iPhone": policy, **{f"ios/AppStore/{path.name}": path.read_text() for path in sorted(APP_STORE.glob("*.md"))}}
    # Signing in happens on Kaal's own page, but the app has password fields of its own in Settings,
    # which App Review and any reader can see: "never sees your password" is not true of it.
    assert re.search(r'SecureField\("[^"]*password', code, re.I), "the app no longer has a password field: the texts can say so again"
    for where, text in said.items():
        assert not re.search(r"never\s+(?:sees?|shown|given)\s+(?:your|the|a)?\s*(?:password|them)\b", text, re.I), f"{where} says the app never sees the password"
    # Each request the app sends a password with, by what the policy calls it.
    asks = {"changePassword": "change it", "newRecoveryCodes": "new recovery codes", "createToken": "a token", "deleteAccount": "delete your account"}
    sends = {name for name, parameters in re.findall(r"func (\w+)\(([^)]*)\) async throws", code) if re.search("password", name + parameters, re.I)}
    assert sends and sends <= set(asks), f"the app sends a password with {sorted(sends - set(asks))}: say what for in docs/privacy.md and ios/AppStore/privacy-labels.md"
    for name in sends:
        assert asks[name] in " ".join(policy.split()), f"docs/privacy.md does not say the app asks for the password to {asks[name]}"
    assert "does not keep it" in policy


def test_what_app_review_is_told_about_accounts_matches_the_app():
    code = app_code()
    notes = " ".join((APP_STORE / "review-notes.md").read_text().split())
    # An administrator can add a person from the app, so "the app creates no accounts" would be false,
    # and Apple asks for deletion in the app wherever accounts can be made in it.
    assert "addPerson(" in code and "creates no accounts" not in notes and "Settings → People" in notes
    assert 'result("DELETE", "/v1/account"' in code and '"Delete my account"' in code
    assert "Settings → Delete my account" in notes
    policy = " ".join((ROOT / "docs" / "privacy.md").read_text().split())
    assert 'Kaal for iPhone has "Delete my account" under Settings' in policy
    # What stops an account being deleted, each said in the policy.
    from kaal import accounts, server
    plan = inspect.getsource(accounts.AccountStore.plan_account_removal) + inspect.getsource(server.removal_plan)
    for refusal, told in (("the only administrator", "only administrator"), ("installation's only account", "installation's only account"),
                          ("installation's first vault", "first vault the installation was set up with"), ("its only owner", "the only owner")):
        assert refusal in plan and told in policy, refusal


def test_the_app_keeps_safari_signed_in_and_the_policy_says_so():
    code = app_code()
    policy = " ".join(section((ROOT / "docs" / "privacy.md").read_text(), "Kaal for iPhone").split())
    # The sign-in sheet shares Safari's cookies on purpose, which leaves Safari signed in to that Kaal.
    assert "prefersEphemeralWebBrowserSession = false" in code
    assert "Safari stays signed in" in policy and "does not end that" in policy
    assert "What is waiting to be sent stays on this phone" in code and "waiting to be sent stays on the phone" in policy


def test_what_is_said_about_the_debugger_is_everything_the_extension_does_with_it():
    background = (EXT / "background.js").read_text()
    listing = (WEB_STORE / "listing.md").read_text()
    policy = " ".join(section((ROOT / "docs" / "privacy.md").read_text(), "Kaal for Chrome").split())
    reason = re.search(r"^\*\*debugger\*\*\n\n```text\n(.*?)\n```", listing, re.M | re.S)[1]
    # Every command sent through the debugger, and no other, is named to the store's reviewer.
    sent = set(re.findall(r'command\("([A-Za-z]+\.[A-Za-z]+)"', background))
    assert "sendCommand(target, method, params)" in background and len(re.findall(r"sendCommand\(", "\n".join(path.read_text() for path in EXT.glob("*.js")))) == 1
    assert sent == set(re.findall(r"\b([A-Z][A-Za-z]*\.[a-z][A-Za-z]*)\b", reason)), "chrome-extension/store/listing.md must name exactly the debugger commands background.js sends"
    # One of them evaluates an expression in the page being saved: a fixed one, which both texts give.
    evaluated = re.findall(r'expression:\s*("[^"]*"|[^,}]+)', background)
    assert evaluated == ['"document.contentType"'], "what the extension evaluates in a page changed: say so in the listing and docs/privacy.md"
    assert '"document.contentType"' in reason and "`document.contentType`" in policy
    for text in (policy, " ".join(listing.split())):
        assert "runs nothing in the pages you visit" not in text and "holds no access to any" not in text and "used for nothing else" not in text
    assert "runs nothing in a page you have not asked it to save" in policy
    # Chrome's own install warning for the debugger permission is broad; neither text may read as if there were none.
    assert "Read and change all your data on all websites" in policy and "Read and change all your data on all websites" in listing
    # The list of receipt sites also comes from the server, so it covers everyone who shares the vault.
    assert '"/v1/medical/sources"' in background and "anyone who shares the vault" in policy


def test_the_docker_installs_proxy_keeps_no_record_of_requests_as_the_policy_says():
    lines = [line.split("#")[0].strip() for line in (ROOT / "deploy" / "docker" / "Caddyfile").read_text().splitlines()]
    caddyfile = " ".join(line for line in lines if line)
    # Caddy writes no access log unless told to, but it logs each request that fails or is cut off,
    # with the visitor's address, the address asked for and its headers (an app's session among them),
    # under these two names. Both are left out, for everything Caddy serves.
    assert caddyfile.startswith("{ log { exclude http.log http.handlers } }"), "see the comment in deploy/docker/Caddyfile, and docs/privacy.md"
    site = caddyfile.split("} }", 1)[1]
    assert "reverse_proxy kaal:8765" in site and not re.search(r"\blog\b|\bdebug\b", site), "an access log would have to be described in docs/privacy.md"
    policy = " ".join((ROOT / "docs" / "privacy.md").read_text().split())
    assert "(Caddy) is set to keep no record of requests, failed ones included" in policy
    # An update is what brings the setting to an installation that has an older Caddyfile.
    assert "docker compose restart caddy" in (ROOT / "deploy" / "docker" / "kaalctl").read_text()


def test_a_build_that_cannot_stay_signed_in_is_not_to_be_uploaded():
    # Over https the server takes an app's session only in a header. While the app in this tree
    # still sends a cookie, the instructions for publishing must say not to upload it.
    from kaal import server
    assert server.SESSION_HEADER == "x-kaal-session"
    if '"X-Kaal-Session"' not in app_code():
        publishing = " ".join(section((ROOT / "README.md").read_text(), "Publishing").split())
        assert "Do not upload a build that sends its session as a cookie" in publishing and "`X-Kaal-Session`" in publishing
        assert "`X-Kaal-Session`" in (APP_STORE / "review-notes.md").read_text()


def test_the_app_still_uses_only_the_encryption_ios_provides():
    # ios/AppStore/export-compliance.md explains why ITSAppUsesNonExemptEncryption is false. It stops being true
    # if the app starts encrypting anything itself.
    assert plistlib.loads((ROOT / "ios" / "Info.plist").read_bytes())["ITSAppUsesNonExemptEncryption"] is False
    code = "\n".join(path.read_text() for path in (ROOT / "ios").rglob("*.swift"))
    for own_encryption in ("AES.", "ChaChaPoly", "SecKeyCreateEncryptedData", "CCCrypt", "import CommonCrypto"):
        assert own_encryption not in code, f"{own_encryption}: see ios/AppStore/export-compliance.md"
