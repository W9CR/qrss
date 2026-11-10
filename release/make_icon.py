#!/usr/bin/env python3
"""Make QBS' platform icon files from qrss/data/w9cr_bat.png.

Writes into release/out/icons/:

    qbs.icns   macOS (PNG-based icns chunks - no iconutil needed)
    qbs.ico    Windows (PNG-compressed entries, Vista and later)
    qbs.png    Linux, 256x256, for the .desktop entry

Both containers are plain header + embedded PNGs, so this needs nothing but
Qt, which the app already uses.  Run with the repo venv:

    .venv/bin/python release/make_icon.py
"""
import os
import struct

# No QGuiApplication: QImage, QPainter and the PNG plugin work without one, which
# keeps this runnable on a headless build host (no display, no libEGL).
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt    # noqa: E402
from PySide6.QtGui import QImage, QPainter                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "qrss", "data", "w9cr_bat.png")
OUT = os.path.join(ROOT, "release", "out", "icons")

# icns chunk -> pixel size (PNG variants, macOS 10.7 and later)
ICNS = [("ic11", 32), ("ic12", 64), ("ic07", 128), ("ic13", 256),
        ("ic08", 256), ("ic14", 512), ("ic09", 512), ("ic10", 1024)]
ICO = [16, 24, 32, 48, 64, 128, 256]


def png(size):
    """The source icon as a square PNG of the given size, transparent padding."""
    src = QImage(SRC)
    if src.isNull():
        raise SystemExit("cannot read " + SRC)
    canvas = QImage(size, size, QImage.Format_RGBA8888)
    canvas.fill(Qt.transparent)
    s = src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p = QPainter(canvas)
    p.drawImage((size - s.width()) // 2, (size - s.height()) // 2, s)
    p.end()
    # QByteArray must outlive the QBuffer: a temporary here makes Qt 6.12 crash.
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    if not canvas.save(buf, "PNG"):
        raise SystemExit("Qt cannot write PNG")
    buf.close()
    return bytes(data)


def write_icns(path):
    chunks = b"".join(t.encode() + struct.pack(">I", len(d) + 8) + d
                      for t, size in ICNS for d in [png(size)])
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


def write_ico(path):
    images = [(s, png(s)) for s in ICO]
    offset = 6 + 16 * len(images)
    head, body = b"", b""
    for size, data in images:
        w = 0 if size >= 256 else size
        head += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(images)) + head + body)


def main():
    os.makedirs(OUT, exist_ok=True)
    write_icns(os.path.join(OUT, "qbs.icns"))
    write_ico(os.path.join(OUT, "qbs.ico"))
    with open(os.path.join(OUT, "qbs.png"), "wb") as f:
        f.write(png(256))
    print("icons -> " + OUT)


if __name__ == "__main__":
    main()
