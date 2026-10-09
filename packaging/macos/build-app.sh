#!/usr/bin/env bash
# Builds "Ember.app" — a native macOS window around the dashboard.
#
#   bash packaging/macos/build-app.sh [--dev] [output-dir]
#
# By default the app is self-contained: the dashboard files and a standalone
# Python (python-build-standalone, pinned below and checked against its
# sha256) go inside Contents/Resources, so it runs from anywhere with nothing
# else installed. --dev builds the thin app instead: it runs the checkout's
# server.py with the system python3, so edits need no rebuild.
#
# Requires the Xcode command line tools (swiftc); the self-contained build
# also downloads ~25 MB once (cached in ~/Library/Caches/ember-build). The icon
# step uses Pillow if it happens to be installed; without it the app simply
# gets the default icon.
set -euo pipefail

DEV=0
if [ "${1:-}" = "--dev" ]; then DEV=1; shift; fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$REPO/..}"

# python-build-standalone release, per architecture (update all four together;
# digests from the release's asset list)
PBS_TAG="20260929"
PBS_PY="3.13.15"
PBS_SHA_arm64="d66c67f16148c7454b1509c32747175f7669c8b8e105b97b92a0000d66af6e6e"
PBS_SHA_x86_64="73b503a2d3f47f0601265d7936744b77dc4ee75d2a3e88a470594a014dcf6822"
APP="$OUT/Ember.app"
# the one version number lives in server.py
VERSION="$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$REPO/server.py")"

command -v swiftc >/dev/null 2>&1 || {
  echo "swiftc not found — install the Xcode command line tools: xcode-select --install" >&2
  exit 1
}

echo "Building native window…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O "$REPO/native/main.swift" -o "$APP/Contents/MacOS/Ember" \
       -framework Cocoa -framework WebKit

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Ember</string>
  <key>CFBundleDisplayName</key><string>Ember</string>
  <key>CFBundleIdentifier</key><string>com.claude-devtools-lite.app</string>
  <key>CFBundleVersion</key><string>__VERSION__</string>
  <key>CFBundleShortVersionString</key><string>__VERSION__</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Ember</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
PLIST
sed -i '' "s/__VERSION__/$VERSION/g" "$APP/Contents/Info.plist"
echo "</plist>" >> "$APP/Contents/Info.plist"

if [ "$DEV" = 0 ]; then
  echo "Copying the dashboard into the app…"
  RES="$APP/Contents/Resources"
  cp "$REPO/server.py" "$REPO/winconpty.py" "$REPO/index.html" "$REPO/addons.json" "$RES/"
  cp -R "$REPO/vendor" "$RES/vendor"
  mkdir -p "$RES/docs/assets" && cp "$REPO/docs/assets/layout.svg" "$RES/docs/assets/"
  mkdir -p "$RES/tools"
  cp "$REPO/tools/devtools_hooks.py" "$RES/tools/"

  ARCH="$(uname -m)"                      # arm64 | x86_64
  case "$ARCH" in
    arm64)  TRIPLE="aarch64-apple-darwin"; SHA="$PBS_SHA_arm64" ;;
    x86_64) TRIPLE="x86_64-apple-darwin";  SHA="$PBS_SHA_x86_64" ;;
    *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
  esac
  TGZ="cpython-$PBS_PY+$PBS_TAG-$TRIPLE-install_only_stripped.tar.gz"
  CACHE="$HOME/Library/Caches/ember-build"
  if [ ! -f "$CACHE/$TGZ" ]; then
    echo "Downloading Python $PBS_PY ($ARCH)…"
    mkdir -p "$CACHE"
    curl -fL --retry 3 -o "$CACHE/$TGZ.part" \
      "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/${TGZ//+/%2B}"
    mv "$CACHE/$TGZ.part" "$CACHE/$TGZ"
  fi
  echo "$SHA  $CACHE/$TGZ" | shasum -a 256 -c - >/dev/null || {
    echo "checksum mismatch for $CACHE/$TGZ — delete it and retry" >&2; exit 1; }
  tar -xzf "$CACHE/$TGZ" -C "$RES"        # → Resources/python
  # compile once now: at run time nothing may be written inside the bundle
  "$RES/python/bin/python3" -m compileall -q -j 0 "$RES" >/dev/null || true
fi

# optional icon (needs Pillow); harmless to skip
if python3 -c "import PIL" 2>/dev/null; then
  echo "Rendering icon…"
  python3 "$HERE/make_icon.py" "$APP/Contents/Resources/AppIcon.icns" || \
    echo "  (icon step failed — continuing without one)"
else
  echo "Skipping icon (Pillow not installed)."
fi

codesign --force --sign - "$APP" 2>/dev/null || true
touch "$APP"

echo
echo "Built: $APP ($(du -sh "$APP" | cut -f1))"
if [ "$DEV" = 1 ]; then
  echo "Dev build: it runs $REPO/server.py with the system python3."
fi
echo "Drag it to /Applications, or double-click it where it is."
