"""A whole-station backup: the codeplug, the wildcard block and the SA (alignment) partition.

All three live in the SCM's NVM and all three can be read and written over the MMI (RD/WR CP,
RD/WR WC, RD/WR AL), so a backup is what is needed to put a station back after an EEPROM erase,
a bad codeplug write or a firmware upgrade.  The file is text, like a .cp archive, with S2
records per section:

    QRSS STATION BACKUP 1
    // created 2026-11-07T01:45:00
    // station FW_VER SC = H026.11.05f
    codeplug 1444
    S2...
    wildcard 90
    S2...
    alignment 128
    S2...
"""
from .codeplug import s2_line, s2_parse

MAGIC = "QRSS STATION BACKUP"
VERSION = 1
SECTIONS = ("codeplug", "wildcard", "alignment")


class BackupError(ValueError):
    pass


class StationBackup:
    def __init__(self, cp, wc=None, sa=None, meta=None, created=None):
        self.cp = bytes(cp)
        self.wc = None if wc is None else bytes(wc)
        self.sa = None if sa is None else bytes(sa)
        self.meta = dict(meta or {})        # e.g. {'hw': 'INTELLIREPEATER', 'fw': 'FW_VER SC = ...'}
        self.created = created              # ISO timestamp string

    # -- the file ------------------------------------------------------------------
    def text(self):
        import time
        if self.created is None:
            self.created = time.strftime("%Y-%m-%dT%H:%M:%S")
        out = ["%s %d" % (MAGIC, VERSION), "// created %s" % self.created]
        for k in sorted(self.meta):
            out.append("// station %s = %s" % (k, self.meta[k]))
        for name, img in (("codeplug", self.cp), ("wildcard", self.wc), ("alignment", self.sa)):
            if img is None:
                continue
            out.append("%s %d" % (name, len(img)))
            for at in range(0, len(img), 32):
                out.append(s2_line(at, img[at:at + 32]))
        return "\n".join(out) + "\n"

    def save(self, path):
        with open(path, "w", encoding="latin1") as f:
            f.write(self.text())

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="latin1") as f:
            lines = f.read().splitlines()
        if not lines or not lines[0].startswith(MAGIC):
            raise BackupError("not a qrss station backup")
        try:
            version = int(lines[0].split()[-1])
        except ValueError:
            raise BackupError("bad backup header")
        if version != VERSION:
            raise BackupError("backup version %d, this qrss reads %d" % (version, VERSION))
        imgs = {}
        meta = {}
        created = None
        name = size = None
        data = None
        for line in lines[1:]:
            s = line.strip()
            if not s:
                continue
            if s.startswith("//"):
                body = s[2:].strip()
                if body.startswith("created "):
                    created = body[8:].strip()
                elif body.startswith("station "):
                    k, _, v = body[8:].partition(" = ")
                    meta[k.strip()] = v.strip()
                continue
            if s.startswith("S2"):
                if name is None:
                    raise BackupError("S-record before a section header")
                addr, chunk = s2_parse(s)
                if addr != len(data) or addr + len(chunk) > size:
                    raise BackupError("%s: record at %X, expected %X" % (name, addr, len(data)))
                data += chunk
                continue
            if s.startswith(("S8", "S9")):
                continue
            parts = s.split()
            if len(parts) == 2 and parts[0] in SECTIONS and parts[1].isdigit():
                if name is not None and len(data) != size:
                    raise BackupError("%s: %d of %d bytes" % (name, len(data), size))
                name, size = parts[0], int(parts[1])
                data = bytearray()
                imgs[name] = data
                continue
            raise BackupError("unexpected line: %r" % s)
        if name is not None and len(data) != size:
            raise BackupError("%s: %d of %d bytes" % (name, len(data), size))
        if not imgs.get("codeplug"):
            raise BackupError("the backup has no codeplug")
        return cls(imgs["codeplug"], imgs.get("wildcard"), imgs.get("alignment"), meta, created)

    # -- checks --------------------------------------------------------------------
    def summary(self):
        bits = ["codeplug %d bytes" % len(self.cp)]
        bits.append("wildcard %d bytes" % len(self.wc) if self.wc is not None else "no wildcard")
        bits.append("alignment %d bytes" % len(self.sa) if self.sa is not None else "no alignment")
        return ", ".join(bits)
