#!/usr/bin/env bash
# Build the Cardloop Android APK (debug-signed, for sideloading).
#
# The app is a thin Capacitor shell: on first run it asks for the operator's server URL and
# then navigates to that origin, so from the second launch onwards it runs the page the
# SERVER serves — an ordinary deploy reaches the app without any APK work, and the in-app
# build watcher (useBuildWatch) picks it up on its own.
#
# Rebuild the APK when the SHELL changes: the first-run server-setup screen, a native plugin,
# the app version, or to give a fresh install a current bundle.
#
# ⚠️ Order is load-bearing. `cap sync` is what copies web/dist into
# android/app/src/main/assets/public, so the web build must exist FIRST — running sync against
# a stale dist silently ships the previous bundle, and the app then looks "not updated" for
# reasons that have nothing to do with the app.
#
# Version bumps live in web/android/app/build.gradle (versionCode + versionName). Android
# refuses to install an APK whose versionCode is not higher than the installed one, so bump it
# when you intend to replace an existing install.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$HERE/web"
OUT_DIR="${APK_OUT_DIR:-/tmp}"

cd "$WEB"

if [[ "${SKIP_WEB_BUILD:-0}" != "1" ]]; then
  echo "==> building web assets"
  npm run build
else
  echo "==> skipping web build (SKIP_WEB_BUILD=1) — dist must already be current"
fi

echo "==> syncing dist into the Android project"
npx cap sync android

VERSION="$(grep -oP 'versionName\s+"\K[^"]+' android/app/build.gradle || echo unknown)"

echo "==> gradle assembleDebug (version $VERSION)"
cd "$WEB/android"
./gradlew assembleDebug -q

BUILT="$WEB/android/app/build/outputs/apk/debug/app-debug.apk"
[[ -f "$BUILT" ]] || { echo "APK not found at $BUILT" >&2; exit 1; }

DEST="$OUT_DIR/cardloop-$VERSION.apk"
cp "$BUILT" "$DEST"
echo "==> $DEST ($(du -h "$DEST" | cut -f1))"
echo "Hand it to the operator with: cockpit-file $DEST"
