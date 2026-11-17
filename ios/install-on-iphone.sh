#!/bin/sh
# Build Kaal and put it on the iPhone plugged into this Mac.
#
# Before the first run: add your Apple ID in Xcode (Settings > Accounts), plug
# the phone in and unlock it, and turn on Developer Mode on it (Settings >
# Privacy & Security). With a free Apple ID the app stops opening after seven
# days; run this again to renew it. Nothing kept in the app is lost.
set -eu
cd "$(dirname "$0")"

say() { printf '==> %s\n' "$1"; }
stop() { printf 'Stopped: %s\n' "$1" >&2; exit 1; }

# Settings for this Mac only: your developer team and an app identifier of your own.
[ -f Local.xcconfig ] || printf 'PRODUCT_BUNDLE_IDENTIFIER = app.kaal.Kaal.dev%s\n' "$(openssl rand -hex 4)" > Local.xcconfig
team=$(sed -n 's/^DEVELOPMENT_TEAM *= *\([A-Z0-9]\{10\}\).*/\1/p' Local.xcconfig | head -1)
if [ -z "$team" ]; then
    # The team Xcode made for the Apple ID added under Settings > Accounts.
    team=$(defaults read com.apple.dt.Xcode IDEProvisioningTeams 2>/dev/null | sed -n 's/.*teamID = "\{0,1\}\([A-Z0-9]\{10\}\)"\{0,1\};.*/\1/p' | head -1)
    [ -n "$team" ] || stop "no developer team found. Add your Apple ID in Xcode (Settings > Accounts), or put DEVELOPMENT_TEAM = YOURTEAMID in ios/Local.xcconfig."
    printf 'DEVELOPMENT_TEAM = %s\n' "$team" >> Local.xcconfig
    say "Using the developer team Xcode knows; noted in Local.xcconfig"
fi
bundle=$(sed -n 's/^PRODUCT_BUNDLE_IDENTIFIER *= *\([A-Za-z0-9.-]*\).*/\1/p' Local.xcconfig | head -1)
[ -n "$bundle" ] || bundle=app.kaal.Kaal

# The first real iPhone or iPad Xcode can see.
phone=$(xcodebuild -project Kaal.xcodeproj -scheme Kaal -showdestinations 2>/dev/null \
    | sed -n 's/.*{ platform:iOS, arch:[^,]*, id:\([0-9A-Fa-f-]*\), name:.*/\1/p' | head -1)
[ -n "$phone" ] || stop "no iPhone found. Plug it in, unlock it, tap Trust, and turn on Developer Mode (Settings > Privacy & Security)."

say "Building for the iPhone"
build="${TMPDIR:-/tmp}/kaal-ios-build"
xcodebuild -project Kaal.xcodeproj -scheme Kaal -configuration Debug -destination "id=$phone" \
    -derivedDataPath "$build" -allowProvisioningUpdates -quiet build

say "Installing"
xcrun devicectl device install app --device "$phone" "$build/Build/Products/Debug-iphoneos/Kaal.app" >/dev/null

say "Opening Kaal on the phone"
xcrun devicectl device process launch --device "$phone" "$bundle" >/dev/null 2>&1 \
    || echo "Installed. If it does not open, trust the developer on the phone first: Settings > General > VPN & Device Management."
say "Done"
