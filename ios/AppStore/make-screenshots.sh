#!/usr/bin/env bash
# Takes the App Store's screenshots of the iPhone app: 6.9-inch, 1320 x 2868.
#
#   ios/AppStore/make-screenshots.sh
#
# It makes a simulator of its own, starts a throwaway Kaal on this machine
# holding only made-up notes and receipts (bin/demo-data.py), signs the app in
# and takes the pictures (ios/KaalUITests/StoreScreenshots.swift), then deletes
# the simulator and the throwaway Kaal. Nothing real is ever on screen.
#
#   PYTHON=…           a Python with Kaal's server dependencies and Pillow
#                      (default: .venv/bin/python if there is one, else python3)
#   KAAL_SIM_NAME=…    what to call the simulator (default: kaal-store-screenshots)
#   KAAL_SIM_TYPE=…    the simulator's device type (default: iPhone 17 Pro Max)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT="$ROOT/ios/AppStore/screenshots"
PYTHON=${PYTHON:-$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)}
SIM_NAME=${KAAL_SIM_NAME:-kaal-store-screenshots}
SIM_TYPE=${KAAL_SIM_TYPE:-com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro-Max}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/kaal-store-shots.XXXXXX")
SERVER=""
DEVICE=""
finish() {
  [ -n "$SERVER" ] && kill "$SERVER" 2>/dev/null && wait "$SERVER" 2>/dev/null || true
  if [ -n "$DEVICE" ]; then
    xcrun simctl shutdown "$DEVICE" >/dev/null 2>&1 || true
    xcrun simctl delete "$DEVICE" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap finish EXIT

echo "Making a simulator ($SIM_NAME)…"
DEVICE=$(xcrun simctl create "$SIM_NAME" "$SIM_TYPE")
xcrun simctl boot "$DEVICE"
xcrun simctl bootstatus "$DEVICE" -b >/dev/null
# The status bar Apple's own screenshots show: 9:41, full signal, a full battery (not charging).
xcrun simctl status_bar "$DEVICE" override --time 9:41 --dataNetwork wifi --wifiMode active --wifiBars 3 \
  --cellularMode active --cellularBars 4 --batteryState discharging --batteryLevel 100

PORT=$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
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

mkdir -p "$WORK/shots"
echo "Signing the app in and taking the pictures (a few minutes)…"
# Signed to run locally only: the simulator needs no developer team.
TEST_RUNNER_KAAL_UI_SERVER=$(read_info url) \
TEST_RUNNER_KAAL_UI_LOGIN=$(read_info login) \
TEST_RUNNER_KAAL_UI_PASSWORD=$(read_info password) \
TEST_RUNNER_KAAL_STORE_SHOTS="$WORK/shots" \
xcodebuild test -project "$ROOT/ios/Kaal.xcodeproj" -scheme Kaal -only-testing:KaalUITests/StoreScreenshots \
  -destination "platform=iOS Simulator,id=$DEVICE" -derivedDataPath "$WORK/DerivedData" \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= >"$WORK/xcodebuild.log" 2>&1 || {
  grep -E "error:|did not" "$WORK/xcodebuild.log" | tail -10
  # What was on screen when it stopped, and the log, are kept; everything else is deleted.
  KEPT=$(mktemp -d "${TMPDIR:-/tmp}/kaal-store-shots-failed.XXXXXX")
  cp "$WORK/xcodebuild.log" "$WORK"/shots/*.png "$KEPT"/ 2>/dev/null || true
  echo "The pictures were not taken. The log and the screen as it was are in $KEPT"
  exit 1
}

# App Store Connect takes RGB pictures without transparency, at exactly this size.
mkdir -p "$OUT"
"$PYTHON" - "$WORK/shots" "$OUT" <<'PY'
import sys
from pathlib import Path
from PIL import Image

taken, out = Path(sys.argv[1]), Path(sys.argv[2])
names = sorted(path for path in taken.glob("*.png") if not path.name.startswith("failed-"))
if len(names) < 6:
    sys.exit(f"Only {len(names)} pictures were taken.")
for path in names:
    image = Image.open(path).convert("RGB")
    if image.size != (1320, 2868):
        sys.exit(f"{path.name} is {image.size[0]} x {image.size[1]}, not 1320 x 2868: use an iPhone 17 Pro Max simulator.")
    image.save(out / path.name, optimize=True)
    print(f"ios/AppStore/screenshots/{path.name}  {(out / path.name).stat().st_size // 1024} KB")
PY
