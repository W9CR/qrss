# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build description for QBS (the qrss app), one spec for all platforms.

Normally run through release/build-macos.sh, build-linux.sh or build-windows.ps1,
which create release/out/ and the icons first.  By hand:

    python -m PyInstaller --clean --noconfirm \
        --distpath release/out/dist --workpath release/out/build release/qrss.spec

Output: release/out/dist/QBS/ (Linux: qbs/), plus QBS.app on macOS.
The version comes from qrss/__init__.py (release/buildinfo.py).
"""
import os
import sys

ROOT = os.path.dirname(SPECPATH)
sys.path.insert(0, ROOT)
sys.path.insert(0, SPECPATH)
import buildinfo                                              # noqa: E402

from qrss import __version__                                  # noqa: E402

V = buildinfo.parse(__version__)
ON_MAC = sys.platform == "darwin"
ON_WIN = sys.platform == "win32"
NAME = "QBS" if (ON_MAC or ON_WIN) else "qbs"

ICONS = os.path.join(SPECPATH, "out", "icons")
icon = os.path.join(ICONS, "qbs.icns") if ON_MAC else (
       os.path.join(ICONS, "qbs.ico") if ON_WIN else None)

datas = [(os.path.join(ROOT, "qrss", "data"), os.path.join("qrss", "data"))]

# PySide6 ships every Qt module as a separate wheel, and PyInstaller collects what
# it can resolve.  The app uses only QtCore/QtGui/QtWidgets/QtPrintSupport.
excludes = [
    "tkinter",
    # Qt modules QBS never imports - the big ones first
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialBus",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
]

# Windows file properties (right-click > Properties > Details).  PyInstaller wants
# this as a Python literal in a separate file.
version_file = None
if ON_WIN:
    vers4 = V["vers4"]
    version_file = os.path.join(SPECPATH, "out", "version_info.txt")
    os.makedirs(os.path.dirname(version_file), exist_ok=True)
    with open(version_file, "w", encoding="utf-8") as f:
        f.write('''VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(v)s, prodvers=%(v)s, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Bryan Fields, W9CR'),
      StringStruct('FileDescription', 'QBS - Quantar Base Station Programmer'),
      StringStruct('FileVersion', '%(ver)s'),
      StringStruct('InternalName', 'QBS'),
      StringStruct('LegalCopyright', ''),
      StringStruct('OriginalFilename', 'QBS.exe'),
      StringStruct('ProductName', 'QBS'),
      StringStruct('ProductVersion', '%(ver)s')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
''' % {"v": vers4, "ver": V["display"]})


a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_kw = dict(
    name=NAME, debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, icon=icon,
    codesign_identity=None, entitlements_file=None,
)
if ON_WIN:
    exe_kw["version"] = version_file
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kw)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)

if ON_MAC:
    app = BUNDLE(
        coll,
        name=NAME + ".app",
        icon=icon,
        bundle_identifier="net.w9cr.qbs",
        info_plist={
            "CFBundleName": "QBS",
            "CFBundleDisplayName": "QBS",
            "CFBundleIdentifier": "net.w9cr.qbs",
            "CFBundleShortVersionString": V["short"],
            "CFBundleVersion": V["short"],
            "CFBundleGetInfoString": "QBS " + V["display"],
            "NSHumanReadableCopyright": "Bryan Fields, W9CR",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
