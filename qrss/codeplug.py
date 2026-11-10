"""Quantar codeplug model: WinRSS .cp archive I/O, the v0x28 image layout and the comparator's
v0x61/v0x62 layout.

See quantar_codeplug.py / Quantar_Firmware_Analysis.md for the format notes."""
import struct

RANGES = ["VHF_R1", "VHF_R2", "VHF_R3", "VHF_R4", "UHF_R0", "UHF_R1", "UHF_R2", "UHF_R3",
          "UHF_R4", "800", "800_LIN", "800_LIN_DUAL", "900"]
# 0x28 = Quantar/Quantro (R020.14), 0x61/0x62 = the comparator (3.6 / 3.9)
SUPPORTED_VERSIONS = (0x28, 0x61, 0x62)
COMPARATOR_VERSIONS = (0x61, 0x62)
QUANTAR_VERSIONS = (0x28,)
# Older Quantar layouts, read and upgraded to 0x28 (see upgrade_image).  0x25/0x26 are RSS 10/12
# (18 directory entries, no multi-NAC table); 0x27 is RSS 13 and is structurally 0x28 already.
OLD_VERSIONS = (0x25, 0x26, 0x27)
# The 44-byte multi-NAC (DEXAC) table v0x27 added, as every archive codeplug carries it: u16
# count 1, the default NAC pair 0x4293/0x4293, then empty slots and a u16 0x012C.
DEXAC_DEFAULT = bytes.fromhex("000142934293" + "0" * 60 + "012c" + "0" * 12)
assert len(DEXAC_DEFAULT) == 44 and DEXAC_DEFAULT[36:38] == b"\x01\x2c"

SI_SERIAL, SI_TX, SI_RX1, SI_RX2, SI_PA, SI_PAPWR, SI_NAME = 0x02, 0x1A, 0x1E, 0x22, 0x26, 0x29, 0xD2
CH_SIZE = 54
# Access-code tables (dir[7]): a u16 count then 8-byte records - TX NAC word, RX NAC word,
# RX squelch word, TX squelch word.  The RSS manual allows up to 16 (section 3-13).
ACCESS_SIZE = 8
MAX_ACCESS = 16
# The comparator's directory has 8 entries (dir[0] STATION_INFO, dir[1] channel table, then six
# tables) and its channel record is 36 bytes: dir[1]+2+36*n == dir[2] on every comparator plug.
# STATION_INFO is 58 bytes there (dir[0]=0x2A, dir[1]=0x64) against 0xE8 on a Quantar.
CH_SIZE_BY_VERSION = {0x28: 54, 0x61: 36, 0x62: 36}
# The comparator's port table: 50-byte LINK_CTRL_DATA records (SWAP_OTC.RSC "OLNK" 50), 16 ports
# per chassis, one chassis per 16 records.
PORT_SIZE = 50
PORTS_PER_CHASSIS = 16
CHASSIS = ["PRIMARY", "SECONDARY1", "SECONDARY2", "SECONDARY3", "SECONDARY4"]

# QUANTAR vs QUANTRO hardware platform.  On a platform change WinRSS rewrites the PA to the default PA
# for that platform AND band.  The per-band defaults below were extracted by running rsscore.dll itself
# (tools/cpprobe/run_platform_full.bat -> build_platform_table.py); see Quantar_Firmware_Analysis.md
# "STATION_INFO structure ...".  Universal on any platform change: the PA board byte (SI_PA) carries a
# 0x40 marker bit for QUANTRO, and POWER_SUPPLY (si+0x2C low nibble) is forced to AC LOW (0x09).  Band-
# specific: the rated power (u16 at si+0x28), the Boosted flag (si+0x51 & 0x10), and a per-channel power
# cache (chan +0x20:0x21 = channel power) and low-power byte (chan +0x23), written on every channel.
# "Boosted" = a normal low-power internal PA (e.g. 25 W) driving an EXTERNAL amplifier (~350 W on VHF);
# the Quantro's 6 W / 35 W UHF/800 configs are the same booster idea.  Table keyed by SI_PA range index
# -> (rated_u16, boosted, chan_power_u16, low_power_u8).  Only bands with a real same-band Quantro PA
# are listed; for the rest (UHF_R0, 900, ...) WinRSS remaps the band (e.g. 900 -> VHF_R1), which
# set_hw_platform refuses unless force=True.
HW_PLATFORMS = ["QUANTAR", "QUANTRO", "ASTRO-TAC RECEIVER", "QUANTAR SATELLITE RECEIVER"]
_PLATFORM_BIT = {"QUANTAR": 0x00, "QUANTRO": 0x40}
# The two receiver-only platforms: no exciter, no PA, no transmit NAC.  RX1 bit 0 is the ASTRO-TAC
# marker (set) versus the Quantar Satellite Receiver (clear).  Taken from rsscore.dll itself: two
# CpProbe hw_platform sweeps (tools/cpprobe/out_TARC 147.105/sweep and
# out_Final_Wiki_Example_Working.cp/sweep, rows 00003/00004, 2026-11-08), which agree byte for byte
# once the plug-specific serial is ignored: TX byte si+0x1A and PA byte si+0x26 keep their band
# bits with the 0x80 "fitted" bit cleared, the PA rated power goes to 0, every channel's two TX
# frequencies (chan+0x04, chan+0x08) and its power cache (chan+0x20:0x21, chan+0x23) are zeroed,
# and the eight transmit NACs (dir[18]+4, stride 4) are cleared.  ASTRO-TAC only: the wireline #1
# mode (si+0x12 bits 0xC0) is cleared, POWER_SUPPLY becomes AC LOW and si+0x94 is set to 50 - the
# Satellite Receiver keeps all three.  The Boosted flag (si+0x51) is not touched.
_RX_PLATFORM = {"ASTRO-TAC RECEIVER": 1, "QUANTAR SATELLITE RECEIVER": 0}
_PLATFORM_PA = {
    "QUANTRO": {   # range: (rated_u16, boosted, chan_power_u16, low_power_u8)
        0x00: (0x015E, 0, 0x015E, 0xAA),  # VHF_R1   350 W
        0x01: (0x015E, 0, 0x015E, 0xAA),  # VHF_R2   350 W
        0x05: (0x0006, 0, 0x0006, 0x01),  # UHF_R1   6 W booster
        0x06: (0x0006, 0, 0x0006, 0x01),  # UHF_R2   6 W booster
        0x07: (0x0050, 0, 0x0050, 0x23),  # UHF_R3   80 W
        0x08: (0x004C, 0, 0x004C, 0x20),  # UHF_R4   76 W
        0x09: (0x0023, 0, 0x0023, 0x0F),  # 800      35 W booster
    },
    "QUANTAR": {
        0x00: (0x0019, 1, 0x0012, 0x05),  # VHF_R1   25 W internal (boosted, feeds external ~350 W); ch 18 W
        0x01: (0x0019, 1, 0x0012, 0x05),  # VHF_R2   25 W internal (boosted); ch 18 W
        0x05: (0x006E, 0, 0x006E, 0x37),  # UHF_R1   110 W
        0x06: (0x006E, 0, 0x006E, 0x37),  # UHF_R2   110 W
        0x07: (0x006E, 0, 0x006E, 0x37),  # UHF_R3   110 W
        0x08: (0x0064, 0, 0x0064, 0x32),  # UHF_R4   100 W
        0x09: (0x0014, 1, 0x0010, 0x0A),  # 800      20 W internal (boosted); ch 16 W
    },
}


class PlatformUnsupported(Exception):
    """set_hw_platform refused: no same-band PA exists for this platform+band (WinRSS would remap the
    band, e.g. 900 -> VHF_R1).  Pass force=True to switch anyway (marker + power supply only; the PA
    power is left as-is for the user to set)."""
    def __init__(self, platform, band):
        self.platform, self.band = platform, band
        super().__init__("no %s PA for %s" % (platform, band))


# ---------------------------------------------------------------- S-records / .cp files
def s2_parse(line):
    raw = bytes.fromhex(line[2:].strip())
    if len(raw) != raw[0] + 1:
        raise ValueError("bad S2 length: " + line)
    if (~sum(raw[:-1])) & 0xFF != raw[-1]:
        raise ValueError("bad S2 checksum: " + line)
    return int.from_bytes(raw[1:4], 'big'), raw[4:-1]


def s2_line(addr, data):
    b = bytes([len(data) + 4]) + addr.to_bytes(3, 'big') + data
    return "S2" + b.hex().upper() + "%02X" % ((~sum(b)) & 0xFF)


def cksum(img):
    return sum(img[2:]) & 0xFFFF


def upgrade_image(img):
    """Convert a pre-R14 Quantar codeplug (RSS manual 7-19, "Codeplug upgrade") to the v0x28
    layout.  Returns (new_image, old_version), or (a copy, None) when it is already current
    (0x28, or the comparator's 0x61/0x62).

    The layouts were recovered by diffing the training codeplug as RSS 10/12/13 saved it
    (`CodePlug-archive/Training_Quantar_10/12/13/14.cp`).  Between 0x25/0x26 and 0x27 the
    whole image moves by the four bytes one more directory entry takes (0x52 -> 0x56), and
    v0x27's 44-byte multi-NAC table (dir[18]) is inserted just before dir[15]'s section;
    every section keeps its size and its content.  The TRC/DC table (dir[9]) carries absolute
    pointers, which move with everything else.  0x27 and 0x28 have the same directory and the
    same section sizes, so only the version word changes.  New data gets the defaults every
    v0x28 archive carries; the old image's own bytes are otherwise kept as they are."""
    img = bytearray(img)
    size, ver, ndir = struct.unpack_from(">HHH", img, 4)
    if ver == 0x28 or ver in COMPARATOR_VERSIONS:
        # current Quantar layout, or a comparator plug - the RSS manual's upgrade (7-19) is a
        # Quantar thing, so there is nothing to convert in either.
        return img, None
    if ver not in OLD_VERSIONS:
        raise ValueError("codeplug version 0x%02X cannot be upgraded" % ver)
    if ver == 0x27:                       # same layout: the version word is the whole change
        struct.pack_into(">H", img, 6, 0x28)
        struct.pack_into(">H", img, 0, cksum(img))
        return img, ver
    if ndir != 18:
        raise ValueError("codeplug version 0x%02X with %d directory entries" % (ver, ndir))
    dirs = list(struct.unpack_from(">18I", img, 10))
    offs = [d & 0x7FFFFFFF for d in dirs]
    order = sorted(range(len(offs)), key=lambda i: offs[i])
    lens = {}
    for k, i in enumerate(order):
        end = offs[order[k + 1]] if k + 1 < len(order) else size
        lens[i] = end - offs[i]
    ins = offs[15]                         # the multi-NAC table goes in front of this section
    new = [o + 4 + (len(DEXAC_DEFAULT) if o >= ins else 0) for o in offs]
    out = bytearray(10 + 19 * 4)
    for i in order:
        if offs[i] == ins:
            out += DEXAC_DEFAULT
        out += img[offs[i]:offs[i] + lens[i]]
    struct.pack_into(">19I", out, 10, *[new[i] | (dirs[i] & 0x80000000) for i in range(18)],
                     new[15] - len(DEXAC_DEFAULT))
    struct.pack_into(">HHH", out, 4, len(out), 0x28, 19)
    t = new[9]                             # dir[9]: u16 count then u32 pointers to its lists
    n = struct.unpack_from(">H", out, t)[0]
    for k in range(n):
        o = t + 2 + 4 * k
        struct.pack_into(">I", out, o, struct.unpack_from(">I", out, o)[0] + 4)
    struct.pack_into(">H", out, 0, cksum(out))
    return out, ver


class CpFile:
    """Keeps the file's comment lines verbatim; sections are regenerated on save."""

    def __init__(self, path):
        self.items = []          # ('c', line) | ('s', bytearray)
        cur = {}
        text = open(path, 'r', encoding='latin1').read()
        self.final_newline = text.endswith('\n')
        for line in text.splitlines():
            if line.startswith('S2'):
                a, d = s2_parse(line)
                cur[a] = d
            elif line.startswith('S8') or line.startswith('S9'):
                end = max(a + len(d) for a, d in cur.items()) if cur else 0
                img = bytearray(end)
                for a, d in cur.items():
                    img[a:a + len(d)] = d
                self.items.append(('s', img))
                cur = {}
            else:
                self.items.append(('c', line))

    @classmethod
    def from_image(cls, img, comments=(), wc=None):
        """A CpFile holding a raw codeplug image (e.g. read from a station), so it can be saved as a
        .cp archive.  `comments` are header lines kept verbatim above the S-records; `wc` is the
        wildcard block, written as the second S2 section as WinRSS does."""
        self = cls.__new__(cls)
        self.items = [('c', c) for c in comments] + [('s', bytearray(img))]
        self.final_newline = True
        if wc is not None:
            self.set_wc(wc)
        return self

    def set_wc(self, wc):
        """Replace (or add) the wildcard section, with the comment lines WinRSS writes before it -
        this is how a WildCard merge from another archive lands in the file."""
        wc = bytearray(wc)
        v = struct.unpack_from('>H', wc, 6)[0] if len(wc) >= 8 else 0
        items = [('c', '// OB OE'), ('c', '// WILDCARD VERSION %02X %02X' % (v >> 8, v & 0xFF)),
                 ('c', '// '), ('s', wc)]
        secs = [k for k, (t, _v) in enumerate(self.items) if t == 's']
        if len(secs) > 1:
            # drop the old wildcard section and the comment lines that introduce it, keep the rest
            k = secs[1]
            while k > secs[0] + 1 and self.items[k - 1][0] == 'c' and self.items[k - 1][1].startswith('// '):
                k -= 1
            self.items = self.items[:k] + items + self.items[secs[1] + 1:]
        else:
            self.items += items

    def upgrade(self):
        """Convert the codeplug section to v0x28 in place (RSS manual 7-19).  Returns the version
        it was, or None if it was already current.  The "// VERSION" comment line is rewritten
        too, so a save keeps the file's header honest."""
        secs = [k for k, (t, _v) in enumerate(self.items) if t == 's']
        if not secs:
            return None
        img, was = upgrade_image(self.items[secs[0]][1])
        if was is None:
            return None
        self.items[secs[0]] = ('s', img)
        for k in range(secs[0] - 1, -1, -1):
            t, v = self.items[k]
            if t == 's':
                break
            if v.upper().startswith('// VERSION'):
                self.items[k] = ('c', '// VERSION 00 28')
                break
        return was

    @property
    def sections(self):
        return [v for k, v in self.items if k == 's']

    @property
    def cp(self):
        return self.sections[0]

    @property
    def wc(self):
        s = self.sections
        return s[1] if len(s) > 1 else None

    def save(self, path):
        out = []
        for k, v in self.items:
            if k == 'c':
                out.append(v)
            else:
                v[0:2] = struct.pack('>H', cksum(v))
                for a in range(0, len(v), 32):
                    out.append(s2_line(a, bytes(v[a:a + 32])))
                out.append("S804000000FB")
        text = "\r\n".join(out) + ("\r\n" if self.final_newline else "")
        open(path, 'w', encoding='latin1', newline='').write(text)


# ---------------------------------------------------------------- codeplug model
class Codeplug:
    def __init__(self, img):
        # an older Quantar image is upgraded on the way in, so every codec sees v0x28
        self.upgraded_from = None
        if struct.unpack_from('>H', img, 6)[0] in OLD_VERSIONS:
            img, self.upgraded_from = upgrade_image(img)
        self.img = img
        self.size, self.version, self.ndir = struct.unpack_from('>HHH', img, 4)
        self.dir = list(struct.unpack_from('>%dI' % self.ndir, img, 10))
        # Multi-coded-squelch usage accounting: 8 bytes per entry (u32 calls, u32 seconds),
        # read from the station's RAM with RD MCS (see Link.mcs_block).  None = not read;
        # a codeplug archive never carries it.
        self.mcs = None

    def check(self):
        if self.version not in SUPPORTED_VERSIONS:
            raise SystemExit("codeplug version 0x%02X not supported (only %s, or an older one "
                             "that upgrades to 0x28)"
                             % (self.version, ", ".join("0x%02X" % v for v in SUPPORTED_VERSIONS)))

    @property
    def si(self):
        return self.dir[0] & 0x7FFFFFFF

    @property
    def ci(self):
        return self.dir[1] & 0x7FFFFFFF

    @property
    def ch_size(self):
        return CH_SIZE_BY_VERSION.get(self.version, CH_SIZE)

    @property
    def is_quantar(self):
        return self.version in QUANTAR_VERSIONS

    def u8(self, o): return self.img[o]
    def u32(self, o): return struct.unpack_from('>I', self.img, o)[0]
    def put8(self, o, v): self.img[o] = v
    def put32(self, o, v): struct.pack_into('>I', self.img, o, v)

    def cstr(self, o, n):
        return bytes(self.img[o:o + n]).split(b'\0')[0].decode('latin1')

    def nchan(self):
        return struct.unpack_from('>H', self.img, self.ci)[0]

    def nports(self):
        """Comparator port table (dir[5]): 50-byte records, 16 ports per chassis, ending where
        dir[6] begins.  80 records on 3.9 (5 chassis), 64 on 3.6 (4).  0 on a Quantar."""
        if len(self.dir) < 7 or self.is_quantar:
            return 0
        return max(0, ((self.dir[6] & 0x7FFFFFFF) - (self.dir[5] & 0x7FFFFFFF) - 2) // PORT_SIZE)

    def port_off(self, i):
        return (self.dir[5] & 0x7FFFFFFF) + 2 + PORT_SIZE * i

    def chan_off(self, i):
        return self.ci + 2 + self.ch_size * i

    def naccess(self):
        """Access-code tables in dir[7] (a u16 count then 8-byte records).  0 on a comparator,
        whose dir[7] is a different table."""
        if not self.is_quantar or len(self.dir) < 8:
            return 0
        return struct.unpack_from('>H', self.img, self.dir[7] & 0x7FFFFFFF)[0]

    def access_off(self, i):
        return (self.dir[7] & 0x7FFFFFFF) + 2 + ACCESS_SIZE * i

    def mcs_entry(self, i):
        """(calls, seconds) of the station's multi-coded-squelch accounting for entry i (0-based,
        the same order as the access code tables), or None when it has not been read."""
        if self.mcs is None or len(self.mcs) < 8 * (i + 1):
            return None
        return struct.unpack_from('>II', self.mcs, 8 * i)

    @staticmethod
    def range_of(code, pa=False):
        r = (code - 0x80) if pa else (code - 0x80) // 2
        if code < 0x80 or (not pa and code & 1) or not 0 <= r < len(RANGES):
            return "?(0x%02X)" % code
        return RANGES[r]


def _insert(cp, pos, data):
    """Insert bytes at pos and shift every directory pointer at or after pos (except the
    table being grown, handled by the caller passing pos > its start)."""
    cp.img[pos:pos] = data
    for k, d in enumerate(cp.dir):
        if (d & 0x7FFFFFFF) >= pos:
            cp.dir[k] = d + len(data)
    _fix_subpointers(cp, lambda a: a + len(data) if a >= pos else a)


def _fix_subpointers(cp, fn):
    """Directory entries with bit 31 set point to a pointer table (u16 count, u32[count]),
    e.g. dir[9] -> DC command entries; those absolute offsets move too."""
    for d in cp.dir:
        if d & 0x80000000:
            t = d & 0x7FFFFFFF
            n = struct.unpack_from('>H', cp.img, t)[0]
            for i in range(n):
                o = t + 2 + 4 * i
                struct.pack_into('>I', cp.img, o, fn(struct.unpack_from('>I', cp.img, o)[0]))


def _delete(cp, pos, n):
    del cp.img[pos:pos + n]
    for k, d in enumerate(cp.dir):
        if (d & 0x7FFFFFFF) > pos:
            cp.dir[k] = d - n
    _fix_subpointers(cp, lambda a: a - n if a > pos else a)


def _commit_dir(cp):
    struct.pack_into('>%dI' % cp.ndir, cp.img, 10, *cp.dir)
    cp.size = len(cp.img)
    struct.pack_into('>H', cp.img, 4, cp.size)


def add_channel(cp, copy_from=None):
    """Append a channel, copying channel `copy_from` (default: last).  Matches WinRSS
    'Add Channel': +54 B channel entry, +4 B channel-number table (dir[16]), +64 B extra
    channel entry (dir[17]); counts, directory and size updated."""
    if not cp.is_quantar:
        raise ValueError("add channel is only decoded for v0x28 codeplugs")
    n = cp.nchan()
    src = n - 1 if copy_from is None else copy_from
    d = lambda k: cp.dir[k] & 0x7FFFFFFF
    ent = bytes(cp.img[cp.chan_off(src):cp.chan_off(src) + CH_SIZE])
    xe = bytes(cp.img[d(17) + 2 + 64 * src:d(17) + 2 + 64 * src + 64])
    # highest table first so earlier positions stay valid
    _insert(cp, d(17) + 2 + 64 * n, xe)
    _insert(cp, d(16) + 2 + 4 * n, struct.pack('>HH', n + 1, 0))
    _insert(cp, d(1) + 2 + CH_SIZE * n, ent)
    for k in (1, 16, 17):
        struct.pack_into('>H', cp.img, d(k), n + 1)
    _commit_dir(cp)
    return n


def delete_last_channel(cp):
    if not cp.is_quantar:
        raise ValueError("delete channel is only decoded for v0x28 codeplugs")
    n = cp.nchan()
    if n <= 1:
        raise ValueError("cannot delete the only channel")
    d = lambda k: cp.dir[k] & 0x7FFFFFFF
    _delete(cp, d(1) + 2 + CH_SIZE * (n - 1), CH_SIZE)
    _delete(cp, d(16) + 2 + 4 * (n - 1), 4)
    _delete(cp, d(17) + 2 + 64 * (n - 1), 64)
    for k in (1, 16, 17):
        struct.pack_into('>H', cp.img, d(k), n - 1)
    _commit_dir(cp)


def add_access_table(cp, copy_from=None):
    """'Add Table' on the Access Code Table form: a new 8-byte record at the end of dir[7] -
    a copy of table `copy_from` (0-based) or an empty one (CSQ both ways, no NACs) - with the
    count bumped.  Everything after it (dir[8] onwards) moves up by 8 bytes."""
    if not cp.is_quantar:
        raise ValueError("access code tables are only decoded for v0x28 codeplugs")
    n = cp.naccess()
    if n >= MAX_ACCESS:
        raise ValueError("the station holds at most %d access code tables" % MAX_ACCESS)
    rec = (bytes(cp.img[cp.access_off(copy_from):cp.access_off(copy_from) + ACCESS_SIZE])
           if copy_from is not None else bytes(ACCESS_SIZE))
    _insert(cp, cp.access_off(n), rec)
    struct.pack_into('>H', cp.img, cp.dir[7] & 0x7FFFFFFF, n + 1)
    _commit_dir(cp)
    return n


def delete_access_table(cp, i):
    """'Delete Table': drop record `i` (0-based) and bump the count down.  Refused while a
    channel still points at it (channel entry +0x29 is the table number)."""
    if not cp.is_quantar:
        raise ValueError("access code tables are only decoded for v0x28 codeplugs")
    n = cp.naccess()
    if n <= 1:
        raise ValueError("cannot delete the only access code table")
    users = [c + 1 for c in range(cp.nchan()) if cp.img[cp.chan_off(c) + 0x29] == i + 1]
    if users:
        raise ValueError("channel%s %s still use%s this table"
                         % ("s" if len(users) > 1 else "", ", ".join(map(str, users)),
                            "" if len(users) > 1 else "s"))
    _delete(cp, cp.access_off(i), ACCESS_SIZE)
    # a channel pointing at a table above the deleted one moves down with it
    for c in range(cp.nchan()):
        a = cp.chan_off(c) + 0x29
        if cp.img[a] > i + 1:
            cp.img[a] -= 1
    struct.pack_into('>H', cp.img, cp.dir[7] & 0x7FFFFFFF, n - 1)
    _commit_dir(cp)
    return n - 1


def reset_multi_nac(cp):
    """'Reset Multi-NAC': clear the eight RX/TX NAC pairs in DEXAC (dir[18]+2, 4 bytes each),
    leaving the table's own codewords, expiration time and Last-NAC flag alone."""
    if not cp.is_quantar or len(cp.dir) < 19:
        raise ValueError("this codeplug has no multi-NAC table")
    d = cp.dir[18] & 0x7FFFFFFF
    cp.img[d + 2:d + 2 + 4 * 8] = bytes(4 * 8)


def hw_platform(cp):
    """The station's hardware platform: QUANTAR, QUANTRO, QUANTAR SATELLITE RECEIVER or ASTRO-TAC
    RECEIVER, decoded the way WinRSS's DEC_HW_PLT does (rsscore.dll 0x10009ae0).

    DEC_HW_PLT tests one word of its STATION_INFO: bit 0x8000 = a PA/transmitter band is fitted,
    0x4000 = the QUANTRO PA marker.  With no PA it looks at bit 0 of the receiver byte and names
    ASTRO-TAC RECEIVER when it is set, else QUANTAR SATELLITE RECEIVER.  In the codeplug image
    those are the PA byte (SI_PA: 0x80 present, 0x40 QUANTRO marker, low 6 bits the band) and the
    RX1 byte (SI_RX1: 0x03 in the ASTRO-TAC plug, 0x02 everywhere else).  rsscore's in-memory
    struct is a repacked copy, so its offsets (+0x13, +0x1A) are not the codeplug's; the mapping
    above is what the four Training_Quantar_14 variants - the only codeplugs whose platform is
    known - agree on.  All four can be set, see set_hw_platform."""
    pa = cp.img[cp.si + SI_PA]
    if not pa & 0x80:                                  # no PA: a receiver-only station
        return "ASTRO-TAC RECEIVER" if cp.img[cp.si + SI_RX1] & 1 else "QUANTAR SATELLITE RECEIVER"
    return "QUANTRO" if pa & 0x40 else "QUANTAR"


def rx_band_range(cp):
    """The band range a receiver-only station is on: its RX1 byte is the ordinary band code
    (0x80 + 2*range) with bit 0 carrying the ASTRO-TAC marker.  None when the plug has no band set
    (the training plugs carry 0x02/0x03 there)."""
    code = cp.img[cp.si + SI_RX1] & 0xFE
    if code < 0x80 or (code - 0x80) & 1:
        return None
    r = (code - 0x80) // 2
    return r if r < len(RANGES) else None


def set_hw_platform(cp, name, force=False):
    """Switch the hardware platform the way WinRSS does.

    QUANTAR<->QUANTRO: set the PA board marker and POWER_SUPPLY, and the band-specific PA-power
    default (rated power, Boosted flag, per-channel power cache and low-power byte).  Matches
    rsscore.dll byte for byte on every band with a same-band PA.  Raises PlatformUnsupported for a
    band with no such PA (UHF_R0, 900, ...) unless force=True, in which case only the marker and
    power supply change and the PA power is left as-is (WinRSS would instead remap the band).

    To a receiver-only platform (ASTRO-TAC RECEIVER, QUANTAR SATELLITE RECEIVER) the measured
    cascade is applied: the exciter, PA and transmit NAC are cleared and the per-channel power
    cache zeroed, so the station decodes as a receiver (_RX_PLATFORM).  Back from one, the band is
    taken from the RX1 byte (rx_band_range) and the exciter and PA are rebuilt from it; with no band
    set that raises PlatformUnsupported."""
    if name not in HW_PLATFORMS:
        raise ValueError("%s is not a hardware platform (one of %s)" % (name, HW_PLATFORMS))
    si = cp.si
    if name in _RX_PLATFORM:
        _to_receiver(cp, _RX_PLATFORM[name])
        return
    from_rx = hw_platform(cp) in _RX_PLATFORM
    rng = rx_band_range(cp) if from_rx else cp.img[si + SI_PA] & 0x3F
    tbl = _PLATFORM_PA[name]
    if rng not in tbl and not force:
        raise PlatformUnsupported(name, RANGES[rng] if rng is not None else
                                  "UNKNOWN (0x%02X)" % cp.img[si + SI_RX1])
    if from_rx:                                                   # rebuild the exciter band
        if rng is not None:
            cp.img[si + SI_TX] = 0x80 + 2 * rng
            cp.img[si + SI_RX1] &= 0xFE                           # drop the ASTRO-TAC marker
        cp.img[si + SI_PA] = 0x80 | _PLATFORM_BIT[name] | (rng or 0)   # no band bits to keep: they were 0
    else:
        cp.img[si + SI_PA] = (cp.img[si + SI_PA] & 0xBF) | _PLATFORM_BIT[name]   # marker, keep range
    cp.img[si + 0x2C] = (cp.img[si + 0x2C] & 0xF0) | 0x09                    # POWER_SUPPLY -> AC LOW
    if rng in tbl:
        rated, boosted, chan_pwr, low = tbl[rng]
        struct.pack_into('>H', cp.img, si + 0x28, rated)
        cp.img[si + 0x51] = (cp.img[si + 0x51] & 0xEF) | (0x10 if boosted else 0)
        for i in range(cp.nchan()):
            o = cp.chan_off(i)
            struct.pack_into('>H', cp.img, o + 0x20, chan_pwr)
            cp.img[o + 0x23] = low


def _to_receiver(cp, atac):
    """Clear the transmit side for a receiver-only platform (see _RX_PLATFORM).  The band stays in
    the TX/RX/PA bytes with their 0x80 fitted bits cleared, so a switch back can rebuild the
    exciter; the transmit frequencies and NACs cannot be recovered."""
    si = cp.si
    cp.img[si + SI_TX] &= 0x3F                                     # no exciter, band kept
    cp.img[si + SI_RX1] = (cp.img[si + SI_RX1] & 0xFE) | atac     # bit 0 = the ASTRO-TAC marker
    cp.img[si + SI_PA] &= 0x3F                                     # no PA, band kept
    struct.pack_into('>H', cp.img, si + 0x28, 0)                  # PA rated power
    cp.img[si + 0x51] &= ~0x10 & 0xFF                             # not Boosted (training variants;
                                                                   # the sweeps' plugs were not)
    for i in range(cp.nchan()):
        o = cp.chan_off(i)
        struct.pack_into('>I', cp.img, o + 0x04, 0)               # TX frequency
        struct.pack_into('>I', cp.img, o + 0x08, 0)               # TX idle frequency
        struct.pack_into('>H', cp.img, o + 0x20, 0)               # channel power cache
        cp.img[o + 0x23] = 0
    if len(cp.dir) > 18:                                          # not a comparator plug
        d18 = cp.dir[18] & 0x7FFFFFFF
        for n in range(8):
            struct.pack_into('>H', cp.img, d18 + 4 + 4 * n, 0)    # transmit NACs, every slot
    if atac:
        cp.img[si + 0x12] &= 0x3F                                  # wireline #1 mode -> 0
        cp.img[si + 0x2C] = (cp.img[si + 0x2C] & 0xF0) | 0x09     # POWER_SUPPLY -> AC LOW
        cp.img[si + 0x94] = 0x32                                   # (unmapped) WinRSS writes 50


def mhz(hz):
    return "%.5f" % (hz / 1e6)


def parse_mhz(s):
    return int(round(float(s) * 1e6))




# ---------------------------------------------------------------- TRC / DC command lists
# dir[9] (bit 31 set) -> u16 count + u32 pointers to 18 lists.  Each list: u16 length L,
# then L bytes = command bytes, a 00 terminator, padded to an even length.
# TRC function tone N uses list N (ft0 = guard tone); DC current N uses list N-1.
TRC_OPS = {0x01: 'KEY', 0x02: 'KEY OFF', 0x03: 'KEY ON', 0x04: 'MONITOR', 0x05: 'MORE',
           0x06: 'RPT OFF', 0x07: 'RPT ON', 0x08: 'RXPL OFF', 0x09: 'RXPL ON', 0x0A: 'TXPL OFF',
           0x0B: 'TXPL ON', 0x0C: 'CHN', 0x0D: 'MRTI ON', 0x0E: 'MRTI OFF', 0x0F: 'WAIT',
           0x10: 'ALARM OFF', 0x11: 'ALARM ON', 0x12: 'SELALARM OFF', 0x13: 'SELALARM ON',
           0x14: 'GATEACC ON', 0x15: 'GATEACC OFF', 0x16: 'SCAN ON', 0x17: 'SCAN OFF',
           0x18: 'SCAN SUSPEND', 0x19: 'SCAN RESUME'}
TRC_NAMES = {v: k for k, v in TRC_OPS.items()}
TRC_ARG = {0x0C, 0x0F}          # CHN nn, WAIT nn (x 10 ms)


def _trc_ptrs(cp):
    t = cp.dir[9] & 0x7FFFFFFF
    n = struct.unpack_from('>H', cp.img, t)[0]
    return [struct.unpack_from('>I', cp.img, t + 2 + 4 * i)[0] for i in range(n)]


def trc_list(cp, k):
    """Commands of list k as display strings ('KEY', 'CHN 001', 'WAIT 500')."""
    a = _trc_ptrs(cp)[k]
    L = struct.unpack_from('>H', cp.img, a)[0]
    b = cp.img[a + 2:a + 2 + L]
    out, i = [], 0
    while i < len(b) and b[i]:
        op = b[i]
        if op in TRC_ARG:
            arg = b[i + 1] if i + 1 < len(b) else 0
            out.append("CHN %03d" % arg if op == 0x0C else "WAIT %d" % (arg * 10))
            i += 2
        else:
            out.append(TRC_OPS.get(op, "OP %02X" % op))
            i += 1
    return out


def trc_encode(cmd):
    cmd = " ".join(cmd.upper().split())
    if cmd.startswith("CHN"):
        n = int(cmd[3:])
        if not 1 <= n <= 255:
            raise ValueError("channel 1-255")
        return bytes([0x0C, n])
    if cmd.startswith("WAIT"):
        ms = int(cmd[4:])
        if not 10 <= ms <= 2550:
            raise ValueError("WAIT 10-2550 ms")
        return bytes([0x0F, int(round(ms / 10.0))])
    if cmd.startswith("OP "):
        return bytes([int(cmd[3:], 16)])
    if cmd not in TRC_NAMES or TRC_NAMES[cmd] in TRC_ARG:
        raise ValueError("unknown command %r" % cmd)
    return bytes([TRC_NAMES[cmd]])


def set_trc_list(cp, k, cmds):
    body = b''.join(trc_encode(c) for c in cmds if c.strip()) + b'\0'
    if len(body) & 1:
        body += b'\0'
    a = _trc_ptrs(cp)[k]
    L = struct.unpack_from('>H', cp.img, a)[0]
    if len(body) > L:
        _insert(cp, a + 2 + L, bytes(len(body) - L))
    elif len(body) < L:
        _delete(cp, a + 2 + len(body), L - len(body))
    a = _trc_ptrs(cp)[k]
    struct.pack_into('>H', cp.img, a, len(body))
    cp.img[a + 2:a + 2 + len(body)] = body
    _commit_dir(cp)
