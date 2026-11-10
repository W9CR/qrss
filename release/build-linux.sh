#!/bin/sh
# Build QBS for Linux: tar.gz, AppImage and .deb in release/out/
#
#   release/build-linux.sh
#   PYTHON=/path/to/python3 release/build-linux.sh
#
# Needs a Linux host (PyInstaller cannot cross-compile), curl for appimagetool
# and dpkg-deb for the .deb.  Build on the oldest distribution you care about:
# the bundle is tied to the build host's glibc.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/release/out"
PYTHON=${PYTHON:-python3}
VERSION=$("$PYTHON" "$ROOT/release/buildinfo.py" | "$PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)["display"])')
DEBVERSION=$("$PYTHON" "$ROOT/release/buildinfo.py" | "$PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)["deb"])')
ARCH=$(uname -m)                                    # x86_64 or aarch64
case "$ARCH" in
    x86_64)  DEBARCH=amd64 ;;
    aarch64) DEBARCH=arm64 ;;
    *)       DEBARCH=$ARCH ;;
esac

echo "QBS $VERSION, linux $ARCH, $PYTHON"

if ! "$PYTHON" -c "import PySide6, serial, PyInstaller" 2>/dev/null; then
    echo "missing build dependencies; run:" >&2
    echo "  $PYTHON -m pip install -r $ROOT/requirements.txt pyinstaller" >&2
    exit 1
fi

"$PYTHON" "$ROOT/release/make_icon.py"
"$PYTHON" -m PyInstaller --clean --noconfirm \
    --distpath "$OUT/dist" --workpath "$OUT/build" "$ROOT/release/qrss.spec"

APP="$OUT/dist/qbs"
[ -x "$APP/qbs" ] || { echo "PyInstaller did not produce $APP" >&2; exit 1; }

# 1. plain tarball (any glibc-compatible distribution)
TAR="$OUT/QBS-$VERSION-linux-$ARCH.tar.gz"
tar -C "$OUT/dist" -czf "$TAR" qbs
echo "built $TAR"

# 2. AppImage (one file, no install) - needs appimagetool
TOOL=$(command -v appimagetool 2>/dev/null || true)
if [ -z "$TOOL" ]; then
    case "$ARCH" in
        aarch64) TOOLNAME=appimagetool-aarch64.AppImage ;;
        *)       TOOLNAME=appimagetool-x86_64.AppImage ;;
    esac
    TOOL="$OUT/$TOOLNAME"
    if [ ! -x "$TOOL" ]; then
        echo "downloading $TOOLNAME"
        curl -fsSL -o "$TOOL" \
            "https://github.com/AppImage/appimagetool/releases/download/continuous/$TOOLNAME"
        chmod +x "$TOOL"
    fi
fi

APPDIR="$OUT/appdir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$APP/." "$APPDIR/usr/bin/"
install -m 0755 "$ROOT/release/linux/AppRun" "$APPDIR/AppRun"
install -m 0644 "$ROOT/release/linux/qbs.desktop" "$APPDIR/qbs.desktop"
install -m 0644 "$OUT/icons/qbs.png" "$APPDIR/qbs.png"
install -m 0644 "$OUT/icons/qbs.png" "$APPDIR/.DirIcon"
install -m 0644 "$OUT/icons/qbs.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/qbs.png"

APPIMAGE="$OUT/QBS-$VERSION-$ARCH.AppImage"
rm -f "$APPIMAGE"
# APPIMAGE_EXTRACT_AND_RUN: appimagetool is itself an AppImage and needs FUSE,
# which CI containers and many distributions do not have.
ARCH=$ARCH APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" "$APPIMAGE"
echo "built $APPIMAGE"

# 3. Debian package
DEB="$OUT/deb"
rm -rf "$DEB"
mkdir -p "$DEB/DEBIAN" "$DEB/opt/qbs" "$DEB/usr/bin" \
         "$DEB/usr/share/applications" "$DEB/usr/share/icons/hicolor/256x256/apps"
cp -a "$APP/." "$DEB/opt/qbs/"
ln -s /opt/qbs/qbs "$DEB/usr/bin/qbs"
install -m 0644 "$ROOT/release/linux/qbs.desktop" "$DEB/usr/share/applications/qbs.desktop"
install -m 0644 "$OUT/icons/qbs.png" "$DEB/usr/share/icons/hicolor/256x256/apps/qbs.png"
SIZE=$(du -sk "$DEB/opt" | cut -f1)
cat > "$DEB/DEBIAN/control" <<EOF
Package: qbs
Version: $DEBVERSION
Architecture: $DEBARCH
Maintainer: Bryan Fields, W9CR <bryan@bryanfields.net>
Installed-Size: $SIZE
Section: hamradio
Priority: optional
Homepage: https://github.com/W9CR/qrss
Description: QBS - Quantar Base Station Programmer
 Cross-platform codeplug editor, service-screen and firmware-download tool
 for conventional Motorola Quantar and Quantro stations and ASTRO-TAC
 comparators, a replacement for WinRSS.  Self-contained: it needs no system
 Qt or Python.
EOF
dpkg-deb --root-owner-group --build "$DEB" "$OUT/qbs_${DEBVERSION}_${DEBARCH}.deb" >/dev/null
echo "built $OUT/qbs_${DEBVERSION}_${DEBARCH}.deb"
