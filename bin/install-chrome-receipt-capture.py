#!/usr/bin/env python3
"""Install the local Native Messaging host used by Kaal Receipt Capture."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

HOST_NAME = "com.kaal.receipt_capture"
EXTENSION_ID = "ebmkcbpcihgiaoimcpncmadadogclmld"


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Kaal's Chrome Native Messaging receipt-capture host")
    parser.add_argument("--chrome-app-support", default=str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome"))
    parser.add_argument("--downloads-dir", default=str(Path.home() / "Downloads"), help="Chrome's configured download directory; staging is limited to its Kaal Capture subdirectory")
    parser.add_argument("--python", default=sys.executable, help="Python that has Kaal's optional PDF extraction dependencies")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    host_script = root / "bin" / "kaal-receipt-host.py"
    if not host_script.is_file():
        parser.error(f"host script not found: {host_script}")
    # Keep a virtualenv interpreter path intact. Path.resolve() follows the
    # venv's Python symlink to its base interpreter, losing the venv site-packages
    # (including Docling) for the Native Messaging host.
    python = Path(os.path.abspath(Path(args.python).expanduser()))
    if not python.is_file():
        parser.error(f"Python executable not found: {python}")
    downloads_dir = Path(args.downloads_dir).expanduser().resolve()
    staging_dir = downloads_dir / "Kaal Capture"

    install_dir = Path.home() / ".local" / "share" / "kaal" / "chrome-receipt-capture"
    install_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    wrapper = install_dir / "kaal-receipt-host"
    wrapper.write_text(
        f"#!/bin/sh\nexport KAAL_RECEIPT_STAGING_DIR={json.dumps(str(staging_dir))}\nexec {json.dumps(str(python))} {json.dumps(str(host_script))}\n",
        encoding="utf-8",
    )
    wrapper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    manifest_dir = Path(args.chrome_app_support).expanduser() / "NativeMessagingHosts"
    manifest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = manifest_dir / f"{HOST_NAME}.json"
    manifest.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "Kaal Receipt Capture local host",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }, indent=2) + "\n", encoding="utf-8")
    manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(json.dumps({"status": "installed", "host": HOST_NAME, "manifest": str(manifest), "extension_id": EXTENSION_ID, "staging_dir": str(staging_dir)}, indent=2))


if __name__ == "__main__":
    main()
