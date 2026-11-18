#!/usr/bin/env bash
# Takes the Chrome Web Store's pictures of the extension: five screenshots at
# 1280 x 800 and the 440 x 280 promo tile, into chrome-extension/store/.
#
#   chrome-extension/store/make-screenshots.sh
#
# It starts a throwaway Kaal on this machine holding only made-up notes and
# receipts (bin/demo-data.py) and a test browser with the extension loaded,
# saves a made-up receipt (sample-portal.html) and photographs the popup, the
# side panel and the web app (screenshots.mjs). Everything is deleted again
# afterwards. It needs Chrome for Testing or Chromium, as
# tests/test_extension_e2e.py does, and Node 22 or later.
#
#   PYTHON=…           a Python with Kaal's server dependencies and Pillow
#                      (default: .venv/bin/python if there is one, else python3)
#   KAAL_E2E_BROWSER=… the browser, if it is not found by itself
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PYTHON=${PYTHON:-$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)}

# The same places the extension's browser test looks.
BROWSER=${KAAL_E2E_BROWSER:-}
if [ -z "$BROWSER" ]; then
  for found in "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"Google Chrome for Testing.app"/Contents/MacOS/"Google Chrome for Testing" \
               "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium \
               "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome; do
    [ -x "$found" ] && BROWSER=$found
  done
fi
[ -n "$BROWSER" ] || BROWSER=$(command -v chromium || command -v chromium-browser || true)
[ -n "$BROWSER" ] || { echo "Needs Chrome for Testing or Chromium: set KAAL_E2E_BROWSER." >&2; exit 1; }
command -v node >/dev/null || { echo "Needs Node 22 or later." >&2; exit 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/kaal-chrome-shots.XXXXXX")
SERVER=""
CHROME=""
finish() {
  [ -n "$CHROME" ] && kill "$CHROME" 2>/dev/null && wait "$CHROME" 2>/dev/null || true
  [ -n "$SERVER" ] && kill "$SERVER" 2>/dev/null && wait "$SERVER" 2>/dev/null || true
  rm -rf "$WORK"
}
trap finish EXIT

free_port() { "$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
PORT=$(free_port)
echo "Starting a Kaal with made-up data on port $PORT…"
"$PYTHON" "$ROOT/bin/demo-data.py" serve --port "$PORT" --info "$WORK/demo.json" >"$WORK/demo.log" 2>&1 &
SERVER=$!
for _ in $(seq 1 120); do
  [ -s "$WORK/demo.json" ] && break
  kill -0 "$SERVER" 2>/dev/null || { cat "$WORK/demo.log"; exit 1; }
  sleep 0.5
done
[ -s "$WORK/demo.json" ] || { echo "The made-up Kaal did not start."; exit 1; }
read_info() { "$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$WORK/demo.json" "$1"; }

# Chrome derives an unpacked extension's id from the key in its manifest; the server allows that id.
EXTENSION_ID=$("$PYTHON" -c '
import base64, hashlib, json, sys
key = base64.b64decode(json.load(open(sys.argv[1]))["key"])
print("".join(chr(ord("a") + int(digit, 16)) for digit in hashlib.sha256(key).hexdigest()[:32]))' "$ROOT/chrome-extension/manifest.json")

DEBUG_PORT=$(free_port)
# --use-mock-keychain: without it every web request waits on a hidden macOS Keychain prompt.
"$BROWSER" --user-data-dir="$WORK/profile" --remote-debugging-port="$DEBUG_PORT" --headless=new --use-mock-keychain \
  --password-store=basic --load-extension="$ROOT/chrome-extension" --no-first-run --no-default-browser-check \
  --hide-scrollbars --window-size=1280,800 about:blank >/dev/null 2>&1 &
CHROME=$!
for _ in $(seq 1 100); do
  curl -sf "http://127.0.0.1:$DEBUG_PORT/json/version" >/dev/null && break
  sleep 0.1
done

mkdir -p "$WORK/shots"
echo "Saving a made-up receipt and taking the pictures…"
KAAL=$(read_info url) KAAL_TOKEN=$(read_info extension_token) KAAL_LOGIN=$(read_info login) KAAL_PASSWORD=$(read_info password) \
DEBUG_PORT=$DEBUG_PORT EXTENSION_ID=$EXTENSION_ID OUT="$WORK/shots" node "$HERE/screenshots.mjs"

# The Web Store takes 24-bit pictures without transparency, at exactly these sizes.
"$PYTHON" - "$WORK/shots" "$HERE" <<'PY'
import sys
from pathlib import Path
from PIL import Image

taken, out = Path(sys.argv[1]), Path(sys.argv[2])
for path in sorted(taken.glob("*.png")):
    image = Image.open(path).convert("RGB")
    wanted = (440, 280) if path.name.startswith("promo") else (1280, 800)
    if image.size != wanted:
        sys.exit(f"{path.name} is {image.size[0]} x {image.size[1]}, not {wanted[0]} x {wanted[1]}.")
    image.save(out / path.name, optimize=True)
    print(f"chrome-extension/store/{path.name}  {(out / path.name).stat().st_size // 1024} KB")
PY
