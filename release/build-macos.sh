#!/bin/sh
# Build QBS for macOS: release/out/QBS-<version>-macos-<arch>.dmg
#
#   release/build-macos.sh
#   PYTHON=/path/to/python3 release/build-macos.sh
#
# Needs macOS with the Xcode command line tools (codesign, hdiutil), and a Python
# with PySide6, pyserial, pyobjc and PyInstaller.  The script uses PYTHON if set,
# else a .venv next to the checkout, else python3.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/release/out"

if [ -z "${PYTHON:-}" ]; then
    if [ -x "$ROOT/../.venv/bin/python" ]; then
        PYTHON="$ROOT/../.venv/bin/python"
    else
        PYTHON=python3
    fi
fi

VERSION=$("$PYTHON" "$ROOT/release/buildinfo.py" | "$PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)["display"])')
ARCH=$(uname -m)

echo "QBS $VERSION, macOS $ARCH, $PYTHON"

if ! "$PYTHON" -c "import PySide6, serial, PyInstaller" 2>/dev/null; then
    echo "missing build dependencies; run:" >&2
    echo "  $PYTHON -m pip install -r $ROOT/requirements.txt pyinstaller" >&2
    exit 1
fi

"$PYTHON" "$ROOT/release/make_icon.py"
"$PYTHON" -m PyInstaller --clean --noconfirm \
    --distpath "$OUT/dist" --workpath "$OUT/build" "$ROOT/release/qrss.spec"

APP="$OUT/dist/QBS.app"
[ -d "$APP" ] || { echo "PyInstaller did not produce $APP" >&2; exit 1; }

# Ad-hoc signature: enough for a downloaded-but-not-quarantined app to run.  A
# Developer ID + notarization would remove the right-click-Open step; see README.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

STAGE="$OUT/dmg"
DMG="$OUT/QBS-$VERSION-macos-$ARCH.dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
# -fs HFS+ is deliberate: macOS 15.5 panics (kernel data abort, repeatable) in
# hdiutil when it makes the image with its default filesystem.  HFS+ is fine for
# a read-only DMG and is what every other Mac can mount.
hdiutil create -volname "QBS $VERSION" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ \
    "$DMG" >/dev/null

echo "built $DMG"
