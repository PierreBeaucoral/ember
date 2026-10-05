#!/usr/bin/env bash
# Wraps dist/Ember (packaging/pyinstaller/build.py) into one file that runs on
# most distros: dist/Ember-linux-x86_64.AppImage.
#
#   bash packaging/linux/build-appimage.sh
#
# The app still needs the system WebKitGTK for its window, like the tarball.
# appimagetool and the AppImage runtime are pinned and checked against their
# sha256 (digests from the release asset lists); cached in ~/.cache/ember-build.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
TOOL_SHA="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RT_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RT_SHA="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ember-build"

[ -x "$REPO/dist/Ember/Ember" ] || {
  echo "dist/Ember/Ember not found — run packaging/pyinstaller/build.py first" >&2; exit 1; }

fetch() {   # url sha256 file
  if [ ! -f "$3" ]; then
    curl -fL --retry 3 -o "$3.part" "$1"
    mv "$3.part" "$3"
  fi
  echo "$2  $3" | sha256sum -c - >/dev/null || {
    echo "checksum mismatch for $3 — delete it and retry" >&2; exit 1; }
}
mkdir -p "$CACHE"
fetch "$TOOL_URL" "$TOOL_SHA" "$CACHE/appimagetool-1.9.1-x86_64.AppImage"
fetch "$RT_URL" "$RT_SHA" "$CACHE/runtime-20251108-x86_64"
chmod +x "$CACHE/appimagetool-1.9.1-x86_64.AppImage"

APPDIR="$REPO/build/Ember.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR"
cp -a "$REPO/dist/Ember" "$APPDIR/Ember"
cp "$REPO/launchers/linux/claude-devtools.svg" "$APPDIR/ember.svg"
cat > "$APPDIR/ember.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Ember
Comment=A workspace for Claude Code
Exec=Ember
Icon=ember
Terminal=false
Categories=Development;Utility;
EOF
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
exec "$(dirname "$(readlink -f "$0")")/Ember/Ember" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# extract-and-run: CI runners have no FUSE
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$CACHE/appimagetool-1.9.1-x86_64.AppImage" \
  --runtime-file "$CACHE/runtime-20251108-x86_64" \
  "$APPDIR" "$REPO/dist/Ember-linux-x86_64.AppImage"
echo "Built: dist/Ember-linux-x86_64.AppImage"
