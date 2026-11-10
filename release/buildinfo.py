#!/usr/bin/env python3
"""Release metadata for QBS, derived from qrss.__version__.

The version is YY.MM.DD + stage: 26.11.09a1 = alpha 1 of the 2026-11-09 build,
then a2..a9, b1.. (beta), r1.. (release); plain 26.11.09 is also accepted.
The full string is what the app shows; the installers need the pieces:

  display  "26.11.09a1"   window title, About, file names
  short    "26.11.9"      CFBundleShortVersionString (Apple wants <=3 integers)
  vers4    (26, 11, 9, 1) Windows file version resource
  deb      "26.11.9~a1"   Debian version, so alpha/beta sort before the release

    python3 release/buildinfo.py            # JSON, for the build scripts
    python3 release/buildinfo.py --display  # just the version string

Reads qrss/__init__.py only; imports nothing else.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STAGE = {'a': 'alpha', 'b': 'beta', 'r': 'release'}


def version():
    """qrss.__version__ without importing the package."""
    text = open(os.path.join(ROOT, "qrss", "__init__.py"), encoding="utf-8").read()
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("no __version__ in qrss/__init__.py")
    return m.group(1)


def parse(v=None):
    v = v or version()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})(?:([abr])(\d+))?$", v)
    if not m:
        raise SystemExit("version %r is not YY.MM.DD[a|b|r][N] (e.g. 26.11.09a1)" % v)
    yy, mm, dd, stage, num = m.groups()
    short = "%d.%d.%d" % (int(yy), int(mm), int(dd))
    return {
        "display": v,
        "short": short,
        "vers4": (int(yy), int(mm), int(dd), int(num) if num else 0),
        "stage": stage,
        "stage_name": _STAGE.get(stage, "release") if stage else "release",
        "deb": short + ("~%s%s" % (stage, num or "") if stage in ("a", "b") else ""),
    }


if __name__ == "__main__":
    info = parse()
    if len(sys.argv) > 1 and sys.argv[1] == "--display":
        print(info["display"])
    else:
        print(json.dumps(info))
