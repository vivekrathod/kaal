#!/usr/bin/env python3
"""Copy the web files the Chrome extension's side panel shares with the web app.

The originals live in kaal/web/. The extension cannot load code from the
server, so it carries copies; run this after changing an original.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ("api.js", "ui.js", "app.css", "views/review.js")


def main() -> int:
    changed = 0
    for name in SHARED:
        source, copy = ROOT / "kaal" / "web" / name, ROOT / "chrome-extension" / "web" / name
        if not copy.exists() or copy.read_bytes() != source.read_bytes():
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copy)
            changed += 1
            print(f"updated chrome-extension/web/{name}")
    print("The extension's copies are up to date." if not changed else f"{changed} file(s) copied. Reload the extension in Chrome.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
