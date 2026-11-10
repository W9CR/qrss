# Building the QBS installers

One script per platform; each produces a self-contained bundle with PyInstaller and
wraps it for that platform.  PyInstaller cannot cross-compile, so each package is built
on its own OS (that is what the GitHub Actions workflow does).

| script | output in `release/out/` |
|---|---|
| `build-macos.sh` | `QBS-<version>-macos-<arch>.dmg` (app bundle, ad-hoc signed) |
| `build-linux.sh` | `QBS-<version>-linux-<arch>.tar.gz`, `QBS-<version>-<arch>.AppImage`, `qbs_<debversion>_<arch>.deb` |
| `build-windows.ps1` | `QBS-<version>-win64-setup.exe` (Inno Setup, if installed) and `QBS-<version>-win64.zip` |

Prerequisites: Python 3.9+ with `pip install -r requirements.txt pyinstaller`.  The
scripts use `$PYTHON` if set, else `../.venv/bin/python` if there is one next to the
checkout (how the development tree runs it), else `python3`/`python`.  Linux also needs
`curl` and `dpkg-deb`; Windows uses Inno Setup 6 if `ISCC.exe` is found, and skips the
installer otherwise.  macOS needs the Xcode command line tools.

The version comes from `qrss/__init__.py` only (`YY.MM.DD` + stage, e.g. `26.11.09a1`);
`release/buildinfo.py` derives what each installer needs from it (a numeric
`CFBundleShortVersionString`, the Windows version resource, a Debian version where
alpha/beta sort before the release).

The icons are generated from `qrss/data/w9cr_bat.png` by `release/make_icon.py` - a
`.icns` and a `.ico`, both written directly as PNG-in-container files, so no `iconutil`,
ImageMagick or Pillow is needed.

## GitHub Actions

`.github/workflows/release.yml` builds all three on a `v*` tag, checks that the tag
matches `qrss/__init__.py`, and attaches the packages to a **draft** release (marked
pre-release for an alpha or beta).  The "Run workflow" button builds the same artifacts
without making a release.  Release notes are generated from the commit log.

Each job runs its own artifact once (`QBS --version`, answered before Qt starts, so it
works on a headless runner) and fails the job if the bundle does not start.  A failed
build's log tail is republished as check-run annotations, which - unlike the job log -
can be read without a GitHub login.

The Linux job installs Qt's runtime libraries (`libegl1`, `libgl1`, `libxkbcommon*`,
`libdbus-1-3`, `libfontconfig1`, `libxcb-*`) before building: PySide6's import needs
them, the ubuntu runner image has none, and the first failure was a bare
`ImportError: libEGL.so.1` out of `import PySide6.QtGui`.

## Troubleshooting

### macOS: `hdiutil` panics the kernel (macOS 15.5, Apple Silicon)

`hdiutil create ... -srcfolder ... -format UDZO out.dmg` without a filesystem
argument **kernel-panics the machine** on macOS 15.5 (24F74) on an M2 Mac Studio:

    panic(cpu 12 caller ...): Kernel data abort. at pc ...5cc
    Panicked task: kernel_task        (hdiutil in the process list)

It is repeatable - three builds, three panics, each at the same kernel PC offset.  The
panic happens while `hdiutil` makes the image, so the DMG left behind is a 3 KB stub
that will not mount ("no mountable file systems"); PyInstaller and `codesign` had
already finished and are not involved.

The workaround, which is in `build-macos.sh`, is to give the image an explicit HFS+
filesystem:

    hdiutil create -volname "QBS $VERSION" -srcfolder "$STAGE" -ov -format UDZO \
        -fs HFS+ "$DMG"

That takes about 6 s for the 105 MB app and produces a 43 MB DMG that mounts and
installs normally.  HFS+ is read-only in the DMG, so it costs nothing.  If a future
macOS fixes the default, `-fs HFS+` can stay - it is what most Macs expect anyway.

### PySide6 6.12: `QBuffer(QByteArray())` segfaults

`release/make_icon.py` keeps its `QByteArray` in a variable.  Passing a temporary -
`QBuffer(QByteArray())` - segfaults inside `QImage.save()` on PySide6 6.12.

### AppImage on a machine without FUSE

`appimagetool` is itself an AppImage.  The script sets `APPIMAGE_EXTRACT_AND_RUN=1`,
so it works in containers and on distributions without `libfuse2`.

### Unsigned builds

The macOS app is ad-hoc signed (`codesign -s -`), enough to run locally but not to pass
Gatekeeper for a downloaded file: the first launch needs right-click > Open.  The
Windows installer is unsigned, so SmartScreen warns once.  A Developer ID + notarization
and a code-signing certificate would remove both steps; add them to the scripts when
available.
