#!/usr/bin/env python3
"""Build the zip the Chrome Web Store takes, from exactly the files the extension uses.

    bin/package-extension.py                  # writes dist/kaal-chrome-<version>.zip
    bin/package-extension.py --output FILE
    bin/package-extension.py --list           # only say what would go in

It starts from chrome-extension/manifest.json and follows every file named
from there: the pages, worker and icons in the manifest, the styles and
scripts each page loads, the modules each script imports, and the pages a
script opens (the side panel). Whatever is not reached stays out: the
folder's README, and the store's text and pictures in store/. A file that is
named but missing stops the build.

The Web Store gives an extension an id of its own and refuses a manifest with
a "key" field, so the manifest in the zip has none. Everything else is the
folder as it is: there is no build step.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "chrome-extension"
# Every file goes in with the same date, so that the same files make the same zip.
STAMP = (2020, 1, 1, 0, 0, 0)

PAGE_REFERENCE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"'#?]+)""")
SCRIPT_IMPORT = re.compile(r"""(?:\bimport|\bfrom)\s*\(?\s*["']([^"']+)["']""")
# A file named in a script, such as "manifest.json" or `panel.html?tab=…`.
NAMED_FILE = re.compile(r"""[`"']((?:\.{0,2}/)?[\w./-]+\.(?:html|js|mjs|css|json|png|svg))(?=[?#`"'])""")
ELSEWHERE = ("http:", "https:", "data:", "blob:", "chrome:", "chrome-extension:", "mailto:")


class PackageError(Exception):
    pass


def named_by_manifest(manifest: dict) -> list[str]:
    named = [manifest.get("background", {}).get("service_worker", ""),
             manifest.get("action", {}).get("default_popup", ""),
             manifest.get("options_ui", {}).get("page", ""),
             manifest.get("options_page", ""),
             manifest.get("side_panel", {}).get("default_path", "")]
    for icons in (manifest.get("icons", {}), manifest.get("action", {}).get("default_icon", {})):
        named += [icons] if isinstance(icons, str) else list(icons.values())
    for entry in manifest.get("web_accessible_resources", []):
        named += entry.get("resources", [])
    for script in manifest.get("content_scripts", []):
        named += script.get("js", []) + script.get("css", [])
    return ["/" + name.lstrip("/") for name in named if name]


def named_by(path: str, text: str) -> list[str]:
    if path.endswith(".html"):
        found = PAGE_REFERENCE.findall(text)
    elif path.endswith((".js", ".mjs")):
        found = SCRIPT_IMPORT.findall(text) + NAMED_FILE.findall(text)
    else:
        return []
    here = posixpath.dirname(path)
    return [name if name.startswith("/") else posixpath.join(here, name) for name in found if not name.startswith(ELSEWHERE)]


def needed_files(folder: Path = EXTENSION) -> list[str]:
    """Every file the extension uses, as paths inside the extension."""
    manifest = json.loads((folder / "manifest.json").read_text())
    waiting = named_by_manifest(manifest)
    seen = {"manifest.json"}
    while waiting:
        name = posixpath.normpath(waiting.pop()).lstrip("/")
        if name in seen:
            continue
        if name.startswith("..") or "*" in name:
            raise PackageError(f"{name} is not a file inside the extension.")
        file = folder / name
        if not file.is_file():
            raise PackageError(f"{name} is used by the extension but is not in {folder.name}/.")
        seen.add(name)
        if file.suffix in (".html", ".js", ".mjs"):
            waiting += named_by("/" + name, file.read_text())
    return sorted(seen)


def left_out(folder: Path, needed: list[str]) -> list[str]:
    everything = sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())
    return [name for name in everything if name not in needed and not posixpath.basename(name).startswith(".")]


def build(output: Path, folder: Path = EXTENSION) -> list[str]:
    needed = needed_files(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest.pop("key", None)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in needed:
            data = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode() if name == "manifest.json" else (folder / name).read_bytes()
            entry = zipfile.ZipInfo(name, STAMP)
            entry.external_attr = 0o644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return needed


def main() -> int:
    version = json.loads((EXTENSION / "manifest.json").read_text())["version"]
    parser = argparse.ArgumentParser(description="Build the Chrome Web Store zip of the extension.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"kaal-chrome-{version}.zip")
    parser.add_argument("--list", action="store_true", help="Only print the files that would go in")
    args = parser.parse_args()
    try:
        needed = needed_files() if args.list else build(args.output)
    except PackageError as problem:
        print(f"Not built: {problem}", file=sys.stderr)
        return 1
    print("\n".join(needed))
    print(f"\nLeft out: {', '.join(left_out(EXTENSION, needed)) or 'nothing'}")
    if not args.list:
        print(f"Wrote {args.output} ({args.output.stat().st_size // 1024} KB, version {version}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
