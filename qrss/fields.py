"""Field codecs: map WinRSS form fields (form, field name) to bytes in the codeplug image.

Each codec has get(cp, ctx) -> str and set(cp, ctx, str).  ctx carries the current
record index for array forms (e.g. ctx['channel'] for DCHN).  Fields without a codec are
shown read-only in the GUI ("not decoded yet").  Add codecs here as fields get mapped.
"""
import json, math, os, struct
from .codeplug import (TRC_OPS, trc_list, set_trc_list, _trc_ptrs, RANGES, SI_SERIAL, SI_TX,
                       SI_RX1, SI_RX2, SI_PA, SI_NAME, HW_PLATFORMS, hw_platform,
                       set_hw_platform, CHASSIS, PORTS_PER_CHASSIS)

# Display labels for every SCM VCO range (WinRSS lists only part of these)
RX_BAND_LABELS = ["VHF_R1  132-154", "VHF_R2  150-174", "VHF_R3  174-200", "VHF_R4  200-225",
                  "UHF_R0  380-433", "UHF_R1  403-433", "UHF_R2  438-470", "UHF_R3  470-494",
                  "UHF_R4  494-520", "800     806-825", "800_LIN 806-825", "800_LD  806-825",
                  "900     896-902"]
TX_BAND_LABELS = RX_BAND_LABELS[:9] + ["800     851-870", "800_LIN 851-870", "800_LD  851-870",
                                       "900     935-941"]


class NeedsConfirm(Exception):
    """set() is about to change more than the field itself and WinRSS asks first.  Raised only
    when ctx['interactive'] is set (the GUI), so offline tools (fieldmap_verify, hwtest) just go
    ahead; the GUI catches it, asks, and retries with ctx['_confirmed'] = True."""
    def __init__(self, title, text):
        self.title, self.text = title, text
        super().__init__(text)


class Codec:
    readonly = False
    editable = False      # combo accepts typed values (e.g. 'WAIT 120')
    cascade = False       # set() rewrites other fields too -> the form must refresh afterwards
    navigates = False     # set() only moves the form (which channel/table it shows), stores nothing
    clist = None          # override WinRSS choice list

    def get(self, cp, ctx): raise NotImplementedError
    def set(self, cp, ctx, value): raise NotImplementedError


class Text(Codec):
    def __init__(self, off_fn, size):
        self.off_fn, self.size = off_fn, size

    def get(self, cp, ctx):
        o = self.off_fn(cp, ctx)
        return bytes(cp.img[o:o + self.size]).split(b'\0')[0].decode('latin1')

    def set(self, cp, ctx, value):
        o = self.off_fn(cp, ctx)
        b = value.encode('latin1')[:self.size - 1] if self.size > 1 else value.encode('latin1')[:1]
        cp.img[o:o + self.size] = b.ljust(self.size, b'\0')


class FixedText(Text):
    """Fixed-width field, no terminator (e.g. serial number)."""
    def set(self, cp, ctx, value):
        o = self.off_fn(cp, ctx)
        cp.img[o:o + self.size] = value.encode('latin1')[:self.size].ljust(self.size, b'\0')


class Band(Codec):
    """Board type byte: TX/RX = 0x80 + 2*range, PA = 0x80 + range.

    TX range 27 is the one range no RSS combo offers: the SCM derives it from the module IDs
    (an R0 exciter with an R1 PA gets 403-433 MHz on the R0's VCO split) and writes it into its
    RAM codeplug, so a plug read back from such a station carries it.  It is named here so the
    GUI and the report don't call it UNKNOWN; selecting it writes only the TX byte, since the
    SCM leaves the PA byte alone.
    """
    DERIVED_TX_RANGE = 27
    DERIVED_TX_LABEL = "UHF_R0/R1 403-433 (derived)"

    def __init__(self, off, labels, pa=False, mirror=(), derived=False):
        self.off, self.labels, self.pa, self.mirror = off, labels, pa, mirror
        self.derived = derived          # the TX band: knows the SCM's derived range 27
        self.clist = labels

    def _range(self, code):
        if not self.pa:
            code &= 0xFE        # RX1 bit 0 is the ASTRO-TAC RECEIVER marker, not part of the band
        if code < 0x80: return None
        r = code - 0x80 if self.pa else (code - 0x80) // 2
        if r == self.DERIVED_TX_RANGE and self.derived:
            return r
        return r if 0 <= r < len(RANGES) else None

    def get(self, cp, ctx):
        code = cp.img[cp.si + self.off]
        r = self._range(code)
        if r is None:
            return "UNKNOWN (0x%02X)" % code
        return self.DERIVED_TX_LABEL if r == self.DERIVED_TX_RANGE and self.derived else self.labels[r]

    def set(self, cp, ctx, value):
        keep = 0 if self.pa else cp.img[cp.si + self.off] & 1   # RX1: keep the ASTRO-TAC marker
        if value == self.DERIVED_TX_LABEL and self.derived:
            cp.img[cp.si + self.off] = (0x80 + 2 * self.DERIVED_TX_RANGE) | keep
            return
        r = self.labels.index(value)
        cp.img[cp.si + self.off] = (0x80 + (r if self.pa else 2 * r)) | keep
        for off, pa in self.mirror:
            marker = cp.img[cp.si + off] & 0x40 if pa else 0   # keep the QUANTRO PA marker bit
            cp.img[cp.si + off] = marker | (0x80 + (r if pa else 2 * r))


class U8(Codec):
    def __init__(self, off_fn): self.off_fn = off_fn
    def get(self, cp, ctx): return str(cp.img[self.off_fn(cp, ctx)])
    def set(self, cp, ctx, value): cp.img[self.off_fn(cp, ctx)] = int(value) & 0xFF


class FreqHz(Codec):
    """u32 frequency in Hz, shown in MHz with 6 decimals."""
    def __init__(self, off_fn): self.off_fn = off_fn
    def get(self, cp, ctx): return "%.6f" % (struct.unpack_from('>I', cp.img, self.off_fn(cp, ctx))[0] / 1e6)
    def set(self, cp, ctx, value):
        struct.pack_into('>I', cp.img, self.off_fn(cp, ctx), int(round(float(value) * 1e6)))


class PaRated(Codec):
    """PA rated power: 16-bit watts (big-endian) at STATION_INFO+0x28, 'Boosted' flag = bit 0x10 of
    +0x51.  Both bytes are used because the 350 W rating (0x015E) does not fit in one byte - a QUANTRO
    PA is typically 350 W."""
    clist = ["%-3d Watts" % w for w in (25, 40, 75, 100, 110, 125, 225, 250, 350)] + ["25  Watts Boosted"]

    def get(self, cp, ctx):
        w = struct.unpack_from('>H', cp.img, cp.si + 0x28)[0]
        return ("%-3d Watts" % w) + (" Boosted" if cp.img[cp.si + 0x51] & 0x10 else "")

    def set(self, cp, ctx, value):
        struct.pack_into('>H', cp.img, cp.si + 0x28, int(value.split()[0]) & 0xFFFF)
        if "Boosted" in value:
            cp.img[cp.si + 0x51] |= 0x10
        else:
            cp.img[cp.si + 0x51] &= ~0x10 & 0xFF


class Callsign(Codec):
    """Channel call sign: channel entry +0x30 = index into the call-sign table (dir[5]),
    which holds 16-byte NUL-padded entries after a u16 header.  Index 0 = no call sign."""
    def _entry(self, cp, idx):
        return (cp.dir[5] & 0x7FFFFFFF) + 2 + 16 * (idx - 1)

    def get(self, cp, ctx):
        idx = cp.img[cp.chan_off(ctx.get('channel', 0)) + 0x30]
        if not idx:
            return ""
        o = self._entry(cp, idx)
        return bytes(cp.img[o:o + 16]).split(b'\0')[0].decode('latin1')

    def set(self, cp, ctx, value):
        c = cp.chan_off(ctx.get('channel', 0)) + 0x30
        value = value.strip().upper()
        if not value:
            cp.img[c] = 0
            return
        idx = cp.img[c] or 1
        o = self._entry(cp, idx)
        if o + 16 > (cp.dir[6] & 0x7FFFFFFF):
            raise ValueError("call-sign table entry %d not present" % idx)
        cp.img[c] = idx
        cp.img[o:o + 16] = value.encode('latin1')[:15].ljust(16, b'\0')


# Motorola PL codes -> tone (Hz)
PL_CODES = [("XZ", 67.0), ("WZ", 69.3), ("XA", 71.9), ("WA", 74.4), ("XB", 77.0), ("WB", 79.7),
            ("YZ", 82.5), ("YA", 85.4), ("YB", 88.5), ("ZZ", 91.5), ("ZA", 94.8), ("ZB", 97.4),
            ("1Z", 100.0), ("1A", 103.5), ("1B", 107.2), ("2Z", 110.9), ("2A", 114.8), ("2B", 118.8),
            ("3Z", 123.0), ("3A", 127.3), ("3B", 131.8), ("4Z", 136.5), ("4A", 141.3), ("4B", 146.2),
            ("5Z", 151.4), ("5A", 156.7), ("5B", 162.2), ("6Z", 167.9), ("6A", 173.8), ("6B", 179.9),
            ("7Z", 186.2), ("7A", 192.8), ("M1", 203.5), ("8Z", 206.5), ("M2", 210.7), ("M3", 218.1),
            ("M4", 225.7), ("9Z", 229.1), ("M5", 233.6), ("M6", 241.8), ("M7", 250.3), ("0Z", 254.1)]


class Squelch(Codec):
    """Access-code table squelch word (u16): 0 = CSQ, 0x1000 | tone*10 = PL,
    0x2000 | octal code = DPL.  kind='type' or 'code'.  addr is the record address function:
    `access` (the table the DACC form is showing) by default, `daccess` for a DMCS row.

    The DMCS form's own store (rsscore FUN_1002e480/FUN_1002e790) spells the same thing with
    the type in bits 12-15 as 6 = PL and 7 = DPL and no CSQ - see StoreSquelch."""
    TYPES = {0: "CSQ", 1: "PL", 2: "DPL"}
    WANT = {"PL": 1, "DPL": 2}
    CSQ = 0
    DEF = {1: 670, 2: 0o23}                 # XZ 67.0, D023

    def __init__(self, off, kind, addr=None):
        self.off, self.kind, self.addr = off, kind, addr or access
        if kind == 'type':
            self.clist = ["PL", "DPL", "CSQ"]

    def _a(self, cp, ctx):
        return self.addr(cp, ctx, self.off)

    def get(self, cp, ctx):
        w = struct.unpack_from('>H', cp.img, self._a(cp, ctx))[0]
        t = w >> 12
        if self.kind == 'type':
            return self.TYPES.get(t, "UNKNOWN (%04X)" % w)
        if t == self.WANT["PL"]:
            hz = (w & 0xFFF) / 10.0
            for c, f in PL_CODES:
                if abs(f - hz) < 0.05:
                    return c
            return "%.1f" % hz
        if t == self.WANT["DPL"]:
            return "%03o" % (w & 0xFFF)
        return ""

    def set(self, cp, ctx, value):
        a = self._a(cp, ctx)
        w = struct.unpack_from('>H', cp.img, a)[0]
        v = value.strip().upper()
        if self.kind == 'type':
            if v == "CSQ": w = self.CSQ
            elif v == "PL" and w >> 12 != self.WANT["PL"]:
                w = (self.WANT["PL"] << 12) | self.DEF[self.WANT["PL"]]
            elif v == "DPL" and w >> 12 != self.WANT["DPL"]:
                w = (self.WANT["DPL"] << 12) | self.DEF[self.WANT["DPL"]]
        else:
            t = w >> 12
            if t == self.WANT["PL"]:
                hz = dict(PL_CODES).get(v)
                if hz is None:
                    try:
                        hz = float(v)
                    except ValueError:
                        raise ValueError("unknown PL code %r (use a Motorola code like 4B or a tone like 146.2)" % v)
                w = (t << 12) | int(round(hz * 10))
            elif t == self.WANT["DPL"]:
                w = (t << 12) | int(v, 8)
            else:
                raise ValueError("set squelch type to PL or DPL first")
        struct.pack_into('>H', cp.img, a, w)


class StoreSquelch(Squelch):
    """The DMCS store's spelling: 6 = PL, 7 = DPL, no CSQ (rsscore FUN_1002e480)."""
    TYPES = {6: "PL", 7: "DPL"}
    WANT = {"PL": 6, "DPL": 7}
    DEF = {6: 670, 7: 0o23}


class Nac(Codec):
    """12-bit hex codeword (NAC) in the low bits of a u16 in the access-code table."""
    def __init__(self, off): self.off = off
    def _a(self, cp, ctx): return access(cp, ctx, self.off)
    def get(self, cp, ctx): return "%03X" % (struct.unpack_from('>H', cp.img, self._a(cp, ctx))[0] & 0xFFF)
    def set(self, cp, ctx, value):
        a = self._a(cp, ctx); w = struct.unpack_from('>H', cp.img, a)[0]
        struct.pack_into('>H', cp.img, a, (w & 0xF000) | (int(value, 16) & 0xFFF))


class Tot(Codec):
    """Time-out timers: the PTT time-out table (dir[4]), one byte per PTT source in 10 s
    units; a WinRSS field writes all sources of its kind.  The field-to-byte map is a CpProbe
    sweep of all three (`tools/cpprobe/out_TARC 147.105_fields2/sweep`, 2026-11-08): setting
    line_tot writes +0x0C/0x0D/0x0E/0x11, local_tot +0x08, rptr_tot +0x09/0x0A/0x0B/0x10,
    and rsscore's decoder reads the first byte of each set.  mrti_tot has no sweep, but
    rsscore decodes MRTOT from the same entry as LOCAL/RPTR (FUN_1002eae0's DCHN decoder:
    LOCAL +5, LINE +7, MRTOT +9, RPTR +0xb of the entry, and ENC_DEC's MRTOT/LOCAL/LINE/
    RPTR records all decode as "10*X"), which puts it at +0x07 - the one byte left of the
    four single-slot/short-group offsets."""
    def __init__(self, offs): self.offs = offs
    def get(self, cp, ctx): return str(cp.img[(cp.dir[4] & 0x7FFFFFFF) + self.offs[0]] * 10)
    def set(self, cp, ctx, value):
        v = int(value)
        if not 0 <= v <= 2550: raise ValueError("0-2550 s")
        for o in self.offs:
            cp.img[(cp.dir[4] & 0x7FFFFFFF) + o] = int(round(v / 10.0))


class ChannelNumber(Codec):
    readonly = True
    def get(self, cp, ctx): return str(ctx.get('channel', 0) + 1)


class ChannelOf(Codec):
    readonly = True
    def get(self, cp, ctx): return "CHANNEL# %d of %d" % (ctx.get('channel', 0) + 1, cp.nchan())


class Band2(Band):
    """Second receiver: same type codes as RX1, values below 0x80 mean no receiver (0x02)."""
    def __init__(self, off, labels):
        super().__init__(off, labels + ["NONE"])
        self.labels = labels

    def get(self, cp, ctx):
        return "NONE" if cp.img[cp.si + self.off] < 0x80 else super().get(cp, ctx)

    def set(self, cp, ctx, value):
        # rsscore also flags "second receiver present" in STATION_INFO+0x50 bit 0
        if value == "NONE":
            cp.img[cp.si + self.off] = 0x02
            cp.img[cp.si + 0x50] &= 0xFE
        else:
            super().set(cp, ctx, value)
            cp.img[cp.si + 0x50] |= 0x01


def resolve(cp, ctx, loc, off):
    """Absolute offset for a location tag from fieldmap.json."""
    d = [x & 0x7FFFFFFF for x in cp.dir]
    ch = ctx.get('channel', 0)
    if loc == 'si':
        return d[0] + off
    if loc == 'chan':
        return cp.chan_off(ch) + off
    if loc == 'port':
        return cp.port_off(ctx.get('row', 0)) + off
    if loc == 'xchan':
        return d[17] + 2 + 64 * ch + off
    if loc.startswith('dir'):
        o = d[int(loc[3:])] + off
        # the access-code table (dir[7]) is a record per table: see `access`
        return o + 8 * ctx.get('table', 0) if loc == 'dir7' else o
    raise ValueError(loc)


def access(cp, ctx, off):
    """A word in the access-code table the DACC form is showing (ctx['table'], 0-based).
    dir[7] is a u16 count then 8-byte records; within a record the offsets are +0 TX NAC,
    +2 RX NAC, +4 RX squelch word, +6 TX squelch word - so dir[7] + 8*table + off is the
    address the codecs already used for table 1.  The record layout comes from the Final_Wiki
    and TARC CpProbe sweeps (tools/cpprobe/out_*), which move exactly these bytes."""
    return (cp.dir[7] & 0x7FFFFFFF) + 8 * ctx.get('table', 0) + off


def daccess(cp, ctx, off):
    """A byte of the access-code record for entry ctx['row'] - the DMCS form's rows are the
    access code tables themselves, one record each (dir[7] count + 8-byte records).  Record
    +0 TX NAC word, +2 RX NAC word, +3 user access (the RX word's high byte), +4 RX squelch
    word, +6 TX squelch word."""
    return cp.access_off(ctx.get('row', 0)) + off


class Enum(Codec):
    """Pick-list field learned by CpProbe: a set of (location, offset, mask) slots and the
    masked byte pattern for every option."""
    def __init__(self, e):
        self.slots = e['slots']
        self.options = {k: v for k, v in e['options'].items()}
        self.clist = e['clist']

    def get(self, cp, ctx):
        cur = [cp.img[resolve(cp, ctx, l, o)] & m for l, o, m in self.slots]
        for k, v in self.options.items():
            if v == cur:
                return k
        return "UNKNOWN (%s)" % " ".join("%02X" % x for x in cur)

    def set(self, cp, ctx, value):
        v = self.options[value]
        for (l, o, m), b in zip(self.slots, v):
            a = resolve(cp, ctx, l, o)
            cp.img[a] = (cp.img[a] & ~m & 0xFF) | b


class RemoteControl(Enum):
    """DWIREL remote_control.  Changing it rewrites the whole TRC command table (all 18 lists,
    dir[9]), not just the two bits: rsscore's endField handler end_rc_type (0x10063f30) asks
    "Will Clear TRC Commands For <type> CONTINUE?" and, if the answer is yes, rebuilds the lists
    from one of two fixed templates - 0x10062dd0 for TRC, 0x10062e80 for everything else.  Both
    are 18 x 135-byte slots, zeroed apart from:

        TRC        ft0 MORE, ft1 MONITOR, ft2 CHN 001 + KEY
        NONE/ASTRO ft2 CHN 001 + KEY ON, ft3 KEY OFF

    "CHN 001" is a literal in the DLL (0x100a59d0), not the channel that was there before, and
    every other list is cleared.  A value equal to the one the form opened with does nothing.
    Confirmed on the station's own sweep saves (out_TARC 147.105_fields_B, 00009/00010)."""
    cascade = True        # set() rewrites the TRC list table, not one field
    TEMPLATES = {'TRC': [["MORE"], ["MONITOR"], ["CHN 001", "KEY"]]}
    OTHER = [[], [], ["CHN 001", "KEY ON"], ["KEY OFF"]]
    def set(self, cp, ctx, value):
        old = self.get(cp, ctx)
        # ask before touching anything, so a "no" leaves the codeplug exactly as it was
        if value != old and ctx.get('interactive') and not ctx.get('_confirmed'):
            raise NeedsConfirm("Remote control type",
                               "Changing the remote control type clears the TRC command table "
                               "(all function tone command lists).\n\nContinue?")
        Enum.set(self, cp, ctx, value)
        if value == old:
            return
        tpl = self.TEMPLATES.get(value, self.OTHER)
        for k in range(len(_trc_ptrs(cp))):
            set_trc_list(cp, k, tpl[k] if k < len(tpl) else [])


class Int(Codec):
    """Numeric field learned by CpProbe: raw = value * scale + add (big-endian)."""
    def __init__(self, e):
        self.loc, self.off, self.size = e['loc'], e['off'], e['size']
        self.scale, self.add, self.signed = e['scale'], e['add'], e.get('signed', False)
        self.dec = e.get('decimals', 0)

    def get(self, cp, ctx):
        a = resolve(cp, ctx, self.loc, self.off)
        raw = int.from_bytes(cp.img[a:a + self.size], 'big', signed=self.signed)
        v = (raw - self.add) / self.scale
        return ("%%.%df" % self.dec) % v if self.dec else str(int(round(v)))

    def set(self, cp, ctx, value):
        raw = int(round(float(value) * self.scale + self.add))
        lo, hi = (-(1 << (8 * self.size - 1)), (1 << (8 * self.size - 1)) - 1) if self.signed else (0, (1 << (8 * self.size)) - 1)
        if not lo <= raw <= hi:
            raise ValueError("out of range")
        a = resolve(cp, ctx, self.loc, self.off)
        cp.img[a:a + self.size] = raw.to_bytes(self.size, 'big', signed=self.signed)


class Hex(Codec):
    """A small number WinRSS shows in hex (e.g. DGEN def_tx_st_addr "3F")."""
    def __init__(self, loc, off, size=1):
        self.loc, self.off, self.size = loc, off, size

    def _addr(self, cp, ctx):
        return resolve(cp, ctx, self.loc, self.off)

    def get(self, cp, ctx):
        a = self._addr(cp, ctx)
        return "%0*X" % (2 * self.size, int.from_bytes(cp.img[a:a + self.size], 'big'))

    def set(self, cp, ctx, value):
        a = self._addr(cp, ctx)
        n = int(str(value).strip(), 16) & ((1 << 8 * self.size) - 1)
        cp.img[a:a + self.size] = n.to_bytes(self.size, 'big')


class HdlcAddress(Hex):
    """DGEN ht_hdlc_address.  STATION_INFO+0x6F on a Quantar; on the comparator STATION_INFO is
    only 58 bytes (dir[0]=0x2A, dir[1]=0x64) and the same logical byte is dir[2]+0x0F (0x99),
    inside the DGEN table - measured on Training_comparator_3.9.cp (CpProbe sweep, 2026-11-07)."""
    def _addr(self, cp, ctx):
        if cp.is_quantar:
            return resolve(cp, ctx, 'si', 0x6F)
        return resolve(cp, ctx, 'dir2', 0x0F)


class TrcCmd(Codec):
    """One command slot of a TRC function-tone / DC-current command list (dir[9] lists).

    The pick-list is WinRSS' own (tools/cpprobe/fields_B.tsv: the same 29 entries on every
    slot, CHN/WAIT shown as samples); the field is editable, so any CHN 1-255 or WAIT
    10-2550 is accepted.  A slot is cleared by blanking it.  Editing a slot re-encodes the
    whole list, which can grow or shrink the codeplug image, hence cascade."""
    editable = True
    cascade = True
    clist = [""] + [TRC_OPS[o] for o in (0x11, 0x10)] + ["CHN 001", "CHN 002", "CHN 016"] + \
        [TRC_OPS[o] for o in (0x14, 0x15, 0x01, 0x03, 0x02, 0x05, 0x04, 0x0D, 0x0E, 0x09, 0x08,
                              0x07, 0x06, 0x16, 0x17, 0x19, 0x18, 0x13, 0x12, 0x0B, 0x0A)] + \
        ["WAIT 10", "WAIT 1000", "WAIT 2550"]

    def __init__(self, lst, slot): self.lst, self.slot = lst, slot

    def get(self, cp, ctx):
        c = trc_list(cp, self.lst)
        return c[self.slot] if self.slot < len(c) else ""

    def set(self, cp, ctx, value):
        c = trc_list(cp, self.lst)
        c += [""] * (self.slot + 1 - len(c))
        c[self.slot] = value.strip().upper()
        set_trc_list(cp, self.lst, c)


class PriorityChannel(Codec):
    """Scan priority channel: u16 at STATION_INFO+0xC2; 0x12C NONE, 0x12D FLOAT, else channel."""
    clist = ["NONE", "FLOAT"] + [str(i) for i in range(1, 17)]
    def get(self, cp, ctx):
        v = struct.unpack_from('>H', cp.img, cp.si + 0xC2)[0]
        return {0x12C: "NONE", 0x12D: "FLOAT"}.get(v, str(v))
    def set(self, cp, ctx, value):
        v = {"NONE": 0x12C, "FLOAT": 0x12D}.get(value.strip().upper())
        if v is None:
            v = int(value)
            if not 1 <= v <= cp.nchan():
                raise ValueError("channel 1-%d" % cp.nchan())
        struct.pack_into('>H', cp.img, cp.si + 0xC2, v)


class ScanFlag(Codec):
    """Scan list region cell: dir[16] entry (u16 channel, u8 flags, pad) per channel."""
    clist = ["ENABLED", "DISABLED"]
    def __init__(self, mask): self.mask = mask
    def _a(self, cp, ctx):
        r = ctx.get('row', 0)
        if r >= cp.nchan():
            raise IndexError("no channel %d" % (r + 1))
        return (cp.dir[16] & 0x7FFFFFFF) + 2 + 4 * r + 2
    def get(self, cp, ctx):
        try:
            return "ENABLED" if cp.img[self._a(cp, ctx)] & self.mask else "DISABLED"
        except IndexError:
            return ""
    def set(self, cp, ctx, value):
        a = self._a(cp, ctx)
        cp.img[a] = (cp.img[a] | self.mask) if value == "ENABLED" else (cp.img[a] & ~self.mask & 0xFF)


class ScanChannel(Codec):
    readonly = True
    def get(self, cp, ctx):
        r = ctx.get('row', 0)
        if r >= cp.nchan():
            return ""
        return str(struct.unpack_from('>H', cp.img, (cp.dir[16] & 0x7FFFFFFF) + 2 + 4 * r)[0])


class MultiNac(Codec):
    """Multiple-NAC table (dir[18]): rx_nacN at +2+4(N-1), tx_nacN at +4+4(N-1); 0x4000 = slot used."""
    def __init__(self, n, tx): self.off = 2 + 4 * (n - 1) + (2 if tx else 0)
    def _a(self, cp): return (cp.dir[18] & 0x7FFFFFFF) + self.off
    def get(self, cp, ctx):
        w = struct.unpack_from('>H', cp.img, self._a(cp))[0]
        return "%03X" % (w & 0xFFF) if w & 0x4000 else ""
    def set(self, cp, ctx, value):
        v = value.strip()
        struct.pack_into('>H', cp.img, self._a(cp), (0x4000 | (int(v, 16) & 0xFFF)) if v else 0)


class Flag(Codec):
    clist = ["ENABLED", "DISABLED"]
    def __init__(self, loc, off, mask, on="ENABLED", off_="DISABLED"):
        self.loc, self.off, self.mask, self.on, self.no = loc, off, mask, on, off_
        self.clist = [on, off_]
    def get(self, cp, ctx):
        return self.on if cp.img[resolve(cp, ctx, self.loc, self.off)] & self.mask else self.no
    def set(self, cp, ctx, value):
        a = resolve(cp, ctx, self.loc, self.off)
        cp.img[a] = (cp.img[a] | self.mask) if value == self.on else (cp.img[a] & ~self.mask & 0xFF)


class PttPriority(Codec):
    """PTT priority (station-wide, table dir[3]): one byte per PTT source, rank 9 (highest)
    .. 5 (lowest).  W = wireline, R = repeater, M = MRTI, D = data, L = local."""
    GROUPS = {'L': [0x08], 'R': [0x09, 0x0A, 0x0B], 'W': [0x0C, 0x0D, 0x0E, 0x11, 0x13],
              'D': [0x0F, 0x10], 'M': [0x07, 0x12]}
    def _t(self, cp): return cp.dir[3] & 0x7FFFFFFF
    def get(self, cp, ctx):
        t = self._t(cp)
        rank = {k: cp.img[t + v[0]] for k, v in self.GROUPS.items()}
        return ">".join(sorted(rank, key=lambda k: -rank[k]))
    def set(self, cp, ctx, value):
        order = [x.strip().upper() for x in value.replace(',', '>').split('>') if x.strip()]
        if sorted(order) != sorted(self.GROUPS):
            raise ValueError("use all of W R M D L, e.g. W>R>M>D>L")
        t = self._t(cp)
        for i, k in enumerate(order):
            for o in self.GROUPS[k]:
                cp.img[t + o] = 9 - i


class ConPriority(Codec):
    """DWIREL con_priority: no storage of its own - it is the top three rows of the PTT
    priority table (dir[3], the same table DCHN/ptt_priority shows).  rsscore's decoder
    (DEC_CON_PRI, 0x1002ecc0) builds the priority string and compares its first five
    characters with the literal "W>R>L"; the encoder (ENC_CON_PRI, 0x100370c0) writes
    "W>R>L" for ENABLED and "R>W>L" for DISABLED into that table.  Either way the ranks
    land as 9/8/7 on the W, R and L groups; M and D are left alone.  The decoder also
    requires the 6809-trunking flags (si+0x9D bit 7 among them), which is why WinRSS only
    offers the field for that system type."""
    TOP = ('W', 'R', 'L')
    clist = ["ENABLED", "DISABLED"]
    cascade = True        # set() rewrites the PTT priority table, not one bit
    def get(self, cp, ctx):
        if not cp.img[cp.si + 0x9D] & 0x80:
            return "DISABLED"
        return "ENABLED" if PttPriority().get(cp, ctx).startswith("W>R>L") else "DISABLED"
    def set(self, cp, ctx, value):
        order = self.TOP if value == "ENABLED" else (self.TOP[1], self.TOP[0], self.TOP[2])
        t = cp.dir[3] & 0x7FFFFFFF
        for k, rank in zip(order, (9, 8, 7)):
            for o in PttPriority.GROUPS[k]:
                cp.img[t + o] = rank


def si(off): return lambda cp, ctx: cp.si + off
def ch(off): return lambda cp, ctx: cp.chan_off(ctx.get('channel', 0)) + off


# Read-only decode of the station's SYSTEM TYPE.  WinRSS does not store this as one enum field - it
# derives it from a cascade of feature bytes (changing it in WinRSS rewrites many of them), which is
# why CpProbe never mapped it.  But a codeplug-diff study of controlled one-change variants (Quantar
# v14 base, 2026-11-05) found bits that identify it reliably across bands: IntelliRepeater and Trunking
# (6809) show in si+0x9D bits 6 and 7; DSS-III and Data Base in si+0x50 bits 6 and 4.  Verified on all
# 11 variants plus independent conventional/IR codeplugs.  The hardware platform is read by
# codeplug.hw_platform(): QUANTAR vs QUANTRO is the power amplifier, and the two receiver-only
# platforms (ASTRO-TAC RECEIVER, QUANTAR SATELLITE RECEIVER) are the same field with no PA fitted;
# all four are settable (see HwPlatform below and codeplug.set_hw_platform).
SYSTEM_TYPES = ["CONVENTIONAL", "INTELLIREPEATER", "TRUNKING (6809)", "DSS-III", "QUANTAR DATA BASE STATION"]


def detect_system_type(cp):
    d = cp.img[cp.si + 0x9D]
    if d & 0x40:
        return "INTELLIREPEATER"
    if d & 0x80:
        return "TRUNKING (6809)"
    s = cp.img[cp.si + 0x50]
    if s & 0x40:
        return "DSS-III"
    if s & 0x10:
        return "QUANTAR DATA BASE STATION"
    return "CONVENTIONAL"


# The station's STATION TYPE.  Also derived, from si+0x4E (the byte remote_control and
# con_priority live in) and si+0x50 bit 7: 0x02 SEC XL, 0x01 SEC CFB, 0x04 ASTRO (CAI when
# si+0x50 bit 7 is set), none of them ANALOG ONLY.  Setting it in WinRSS rewrites every
# channel's mod_type (chan+0x12/+0x14 mask 0xB8) and a handful of station bytes, so it stays
# read-only here.  Rule recovered from controlled WinRSS saves and checked against all eight
# archive plugs that have a CpProbe dump.
MODULATIONS = ["ANALOG ONLY", "SEC XL CAPABLE", "SEC CFB CAPABLE", "ASTRO CAPABLE", "ASTRO CAI CAPABLE"]


def detect_modulation(cp):
    a = cp.img[cp.si + 0x4E]
    if a & 0x02:
        return "SEC XL CAPABLE"
    if a & 0x01:
        return "SEC CFB CAPABLE"
    if a & 0x04:
        return "ASTRO CAI CAPABLE" if cp.img[cp.si + 0x50] & 0x80 else "ASTRO CAPABLE"
    return "ANALOG ONLY"


# The IntelliRepeater frequency band (DSTCG ir_freq_band).  Read-only on WinRSS' form, and
# stored: one byte at si+0x52E (image 0x584 on the 800 MHz archive plug, si 0x56).  Mapped by
# two CpProbe sweeps on that plug - setting each name in rsscore and diffing the saved images
# (tools/cpprobe/fields_irband.tsv, fields_ir800.tsv) - not derived, as the old RX1-based
# reading assumed.  Codes 0..7 are ir_freq_band's own list with NONE moved to 0; 8..14 are the
# R14 rebanding plans that only rsscore's ir_800_cp28 list (11 entries) names, in its order.
# The untouched plug stores 9 and rsscore itself reads that back as "806-824", which anchors
# the two halves.  Plugs that are not IntelliRepeater have no byte there (their images end
# before it) and read NONE.
IR_BANDS = ["NONE", "VHF", "UHF", "800-821 INTL", "800-821 INTL SP", "800-821 USA",
            "800-821 USA SP", "900", "806-825 6.25KHZ", "806-824", "806-824 SHF",
            "806-824 USA SHF", "806-824 USA SPL SHF", "806-825 INTL SHF",
            "806-825 INTL SPL SHF"]
IR_BAND_OFF = 0x52E


def detect_ir_freq_band(cp):
    if detect_system_type(cp) != "INTELLIREPEATER":
        return "NONE"
    off = cp.si + IR_BAND_OFF
    if off < len(cp.img):
        code = cp.img[off]
        if code < len(IR_BANDS):
            return IR_BANDS[code]
    # An IntelliRepeater plug too short to carry the byte: fall back to Rx band 1, which is
    # what the field follows for VHF/UHF/900 (WinRSS' help) when no plan is stored.
    code = cp.img[cp.si + SI_RX1] & 0xFE          # bit 0 is the ASTRO-TAC marker
    if code < 0x80:
        return "NONE"
    r = (code - 0x80) // 2
    if r >= len(RANGES):
        return "NONE"
    band = RANGES[r]
    if band.startswith("VHF"):
        return "VHF"
    if band.startswith("UHF"):
        return "UHF"
    return "900" if band == "900" else "NONE"


class Detected(Codec):
    """A value qrss can recognise read-only but WinRSS does not store as a single editable field."""
    readonly = True

    def __init__(self, clist, fn):
        self.clist = clist
        self.fn = fn

    def get(self, cp, ctx):
        return self.fn(cp)

    def set(self, cp, ctx, value):
        pass


class HwPlatform(Codec):
    """The hardware platform (codeplug.hw_platform): QUANTAR, QUANTRO, ASTRO-TAC RECEIVER or
    QUANTAR SATELLITE RECEIVER.  Setting one rewrites the RF identity and its power cache (a
    cascade), so the form refreshes afterwards.  See codeplug.set_hw_platform."""
    clist = HW_PLATFORMS
    cascade = True

    def get(self, cp, ctx):
        return hw_platform(cp)

    def set(self, cp, ctx, value):
        set_hw_platform(cp, value, force=ctx.get('_force_platform', False))


class VoteHyst(Codec):
    """DVOICE vote_hyst (analog voting hysteresis, 0.0-12.0 dB): not a scaled integer but a
    linear amplitude ratio, raw = (int)(32767 / 10**(dB/20)) - 0x6481 is 2.1 dB, 0x70C5 1.1 dB.
    Truncating at 32767 is what reproduces both of WinRSS's saves exactly (28869 and 25729);
    32768 with rounding gives 28870/25731.  Comparator codeplug only."""
    REF = 32767.0

    def _raw(self, cp, ctx):
        a = resolve(cp, ctx, 'chan', 6)
        return a, (cp.img[a] << 8) | cp.img[a + 1]

    def get(self, cp, ctx):
        a, raw = self._raw(cp, ctx)
        if not raw:
            return "UNKNOWN (0000)"
        return "%.1f" % (20 * math.log10(self.REF / raw))

    def set(self, cp, ctx, value):
        db = float(value)
        if not 0.0 <= db <= 12.0:
            raise ValueError("vote_hyst %r out of range 0.0-12.0" % value)
        a, _ = self._raw(cp, ctx)
        n = int(self.REF / 10 ** (db / 20.0))
        cp.img[a], cp.img[a + 1] = (n >> 8) & 0xFF, n & 0xFF


class VoiceHoldOp(Codec):
    """DVOICE voicehold_op has no storage of its own: it is ENABLED while first_vote_hld
    (chan+12) is zero - WinRSS enabling it wrote 0x00 over the 250 ms hold time and nothing
    else.  Turning it back on restores WinRSS's 250 ms default.  Comparator codeplug only."""
    clist = ["DISABLED", "ENABLED"]
    DEFAULT = 250

    def _a(self, cp, ctx):
        return resolve(cp, ctx, 'chan', 12)

    def get(self, cp, ctx):
        a = self._a(cp, ctx)
        return "ENABLED" if cp.img[a] == 0 and cp.img[a + 1] == 0 else "DISABLED"

    def set(self, cp, ctx, value):
        a = self._a(cp, ctx)
        if value == "ENABLED":
            cp.img[a] = cp.img[a + 1] = 0
        elif value == "DISABLED":
            if cp.img[a] == 0 and cp.img[a + 1] == 0:
                cp.img[a], cp.img[a + 1] = (self.DEFAULT >> 8), self.DEFAULT & 0xFF
        else:
            raise ValueError("voicehold_op %r" % value)


class DiuRepeat(Codec):
    """DGEN diu_repeat (comparator codeplug only): a full byte in the dir[2] digital table that
    WinRSS leaves at the 0xDE "unprogrammed" filler until the feature is enabled, then stores 1.
    Every DGEN save also clears dir[2]+0x17..0x19 (0xDEADDEAD -> 0), which is form noise."""
    clist = ["DISABLED", "ENABLED"]

    def _a(self, cp, ctx):
        return resolve(cp, ctx, 'dir2', 0x16)

    def get(self, cp, ctx):
        return "ENABLED" if cp.img[self._a(cp, ctx)] == 1 else "DISABLED"

    def set(self, cp, ctx, value):
        if value not in self.clist:
            raise ValueError("diu_repeat %r" % value)
        cp.img[self._a(cp, ctx)] = 1 if value == "ENABLED" else 0


class OutLinkTimer(Codec):
    """DWIREL out_link_timer: station info +0xC0, u16 big-endian, raw = the displayed number.
    The encoder rounds the typed value to a multiple of ten first - its ENC_DEC block is
    `(x+5)/10` then `*10`, and the decoder is `*10` - so 121 and 120 both store 120.  The
    field is disabled on every plug (rsscore has no enable call for it) but the value is
    still read and written when the form is forced."""
    OFF = 0xC0

    def _a(self, cp, ctx):
        return resolve(cp, ctx, 'si', self.OFF)

    def get(self, cp, ctx):
        a = self._a(cp, ctx)
        return str(int.from_bytes(cp.img[a:a + 2], 'big'))

    def set(self, cp, ctx, value):
        n = (int(round(float(value))) + 5) // 10 * 10
        if not 0 <= n <= 0xFFFF:
            raise ValueError("out of range")
        a = self._a(cp, ctx)
        cp.img[a:a + 2] = n.to_bytes(2, 'big')


class AstroCarSquelch(Codec):
    """DACC astro_car_squ, "RX NAC Operation".  The mode is bits 7, 5 and 4 of the high byte
    of the table's TX and RX NAC words; bit 6 (0x4000) belongs to another field and is kept.
    Choosing a mode also writes the RX codeword the mode carries.  From the Final_Wiki sweep
    00082..00084: NORMAL -> TX 0x02, RX 0x0293; DCS -> 0x82, 0x8000; NAC $F7E -> 0x92,
    0x9F7E; CR $F7F -> 0xA2, 0xAF7F.  The codeword in each option is the base plug's own for
    NORMAL, so coming back to NORMAL from another mode writes 293 (as WinRSS does).  The mode
    is read from the TX word alone, so a codeword the operator edits afterwards does not make
    the field unreadable the way a whole-word compare (fieldmap's learned masks) would."""
    MODES = [("NORMAL", 0x00, 0x293), ("DCS", 0x80, 0x000),
             ("NAC $F7E", 0x90, 0xF7E), ("CR $F7F", 0xA0, 0xF7F)]

    def __init__(self):
        self.clist = [m[0] for m in self.MODES]

    def get(self, cp, ctx):
        hi = cp.img[access(cp, ctx, 2)] & 0xB0
        for name, bits, _code in self.MODES:
            if bits == hi:
                return name
        return "UNKNOWN (%02X)" % hi

    def set(self, cp, ctx, value):
        bits, code = {n: (b, c) for n, b, c in self.MODES}[value]
        a = access(cp, ctx, 2)                          # TX word: the mode bits only
        cp.img[a] = (cp.img[a] & ~0xB0 & 0xFF) | bits
        r = access(cp, ctx, 4)                          # RX word: the mode and its codeword
        w = struct.unpack_from('>H', cp.img, r)[0]
        struct.pack_into('>H', cp.img, r, (w & 0x4000) | (bits << 8) | code)


class AccessTableLabel(Codec):
    """DACC access_tb_range: a label, not a field.  rsscore builds its text with
    sprintf("Access Code Table # %d of %d", ...) (format string at 0x100a5f10, called from
    0x1006a5e5) out of the form's own table counter, and nothing of it reaches the codeplug -
    a forced sweep of it saved no bytes at all.  The count is the codeplug's own: dir[7] is a
    u16 count followed by the 8-byte records (see `access`)."""
    readonly = True

    def get(self, cp, ctx):
        return "Access Code Table # %d of %d" % (ctx.get('table', 0) + 1, max(cp.naccess(), 1))


class AccessTableNumber(Codec):
    """DACC access_table: which access code table the form shows, 1..count (the RSS manual
    allows 16; a CpProbe sweep of the field saved no bytes, so it only moves the form)."""
    navigates = True

    def get(self, cp, ctx):
        return str(ctx.get('table', 0) + 1)

    def set(self, cp, ctx, value):
        n = cp.naccess()
        try:
            t = int(value)
        except ValueError:
            raise ValueError("enter a table number, 1-%d" % n)
        if not 1 <= t <= n:
            raise ValueError("this codeplug has %d access code table(s), so 1-%d" % (n, n))
        ctx['table'] = t - 1

    def describe(self, cp, ctx):
        return "Access code table %d of %d" % (ctx.get('table', 0) + 1, cp.naccess())


class AccessChannels(Codec):
    """DACC channels, "Channel(s) Using This Table": status only, from each channel's
    access-code table number (channel entry +0x29)."""
    readonly = True

    def get(self, cp, ctx):
        t = ctx.get('table', 0) + 1
        return " ".join(str(i + 1) for i in range(cp.nchan())
                        if cp.img[cp.chan_off(i) + 0x29] == t)


class SecondCode(Codec):
    """DACC rx_pl_dpl_2 / tx_pl_dpl_2: WinRSS creates both as label fields with an empty label
    (DACCConfigWindow.java createLabelField, DACCMetadata.java), and no sweep ever saved a byte
    for them.  They would show the second code of a multi-coded squelch entry, which lives in
    the station's MCS data (RD MCS) rather than in the codeplug, so in a codeplug they are blank."""
    readonly = True

    def get(self, cp, ctx):
        return ""


# ---------------------------------------------------------------- multi-coded squelch (DMCS)
# The DMCS form is WinRSS' other view of dir[7]: its rows ARE the access code tables, one
# 8-byte record each, and the User Access byte is the high byte of the record's RX NAC word
# (rsscore's store keeps TX NAC at +4, RX NAC at +6 and the access code at +7 - the same byte;
# FUN_1002e480 renders it, FUN_1002e790 packs it).  The usage counters are not in the codeplug
# at all: they come from the station's RAM over RD MCS (Link.mcs_block).

class UserAccess(Codec):
    """User Access (access record +3, bits 0-1): 0 DISABLED, 1 ENABLED, 2 GATED, 3 DELETE.
    The other bits of the byte are the RX NAC mode (see AstroCarSquelch), so they are kept."""
    clist = ["DISABLED", "ENABLED", "GATED", "DELETE"]

    def get(self, cp, ctx):
        return self.clist[cp.img[daccess(cp, ctx, 3)] & 0x03]

    def set(self, cp, ctx, value):
        a = daccess(cp, ctx, 3)
        cp.img[a] = (cp.img[a] & 0xFC) | self.clist.index(value)


class McsEntryNumber(Codec):
    """DMCS mcs_entry.entry_no, and the "Modify Entry #" the dialog opens on: the row the
    form is showing, 1-based.  Blank past the last entry (the region is 12 rows)."""
    readonly = True

    def get(self, cp, ctx):
        r = ctx.get('row', 0)
        return str(r + 1) if r < cp.naccess() else ""


class McsCalls(Codec):
    """Accounting "Number of Calls": u32 at the entry's +0.  The station stops counting at
    249 (0xF9) - firmware 0x0D6366 compares and saturates."""
    readonly = True

    def get(self, cp, ctx):
        e = cp.mcs_entry(ctx.get('row', 0))
        return "%6d" % e[0] if e else ""


class McsTime(Codec):
    """Accounting "Total Time Used": u32 seconds at the entry's +4, shown the way WinRSS does
    ("%4d:%2d:%2d", h:mm:ss).  It saturates at 249 s like the call count."""
    readonly = True

    def get(self, cp, ctx):
        e = cp.mcs_entry(ctx.get('row', 0))
        if not e:
            return ""
        s = e[1]
        return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


class McsStamp(Codec):
    """DMCS mcs_date / mcs_time, "Station Date:" and "Station Time:": the station's clock when
    its codeplug was read (GET TIME), shown beside the usage data.  Nothing of it is stored in
    the codeplug, so an archive shows it blank."""
    readonly = True

    def __init__(self, part):
        self.part = part

    def get(self, cp, ctx):
        t = getattr(cp, 'mcs_stamp', None)
        if not t:
            return ""
        return t[0] if self.part == 'date' else t[1]


class MultiCodedSq(Codec):
    """DSTCG multi_coded_sq, "Multi-Coded Squelch": MULTI-PL ONLY is bit 0x20 of si+0x50 and
    MULTI-NAC bit 0x04 of si+0x51.  Three sources agree: the DACC row renderer reads the u16
    at si+0x50 (`local_c+0x52`, rsscore.c ~12360) and tests 0x2000 (the byte at si+0x50) for
    MULTI-PL ONLY and 0x0004 (si+0x51) for MULTI-NAC; the CpProbe sweep that found MULTI-NAC
    put it at si+0x51; and the DMCS sweep's base codeplug - which reads back MULTI-PL ONLY -
    is the only archive with si+0x50 & 0x20, while the firmware's own MCS gate (GET MCS SIZE,
    0x0A4D94) tests `settings[0x50] & 0x20`.  WinRSS enables the DMCS form only for MULTI-PL
    ONLY and the DACC form for the other two (rsscore.c ~5057), and warns that MULTI-PL ONLY
    clears the channel and access code tables.  The 0x4000 bits of the first record's RX/TX
    NAC words that the MULTI-NAC sweep also saw are a separate marker, so they are left alone."""
    cascade = True
    clist = ["DISABLED", "MULTI-PL ONLY", "MULTI-NAC"]
    PL_ONLY = (0x50, 0x20)
    MULTI_NAC = (0x51, 0x04)

    def get(self, cp, ctx):
        if cp.img[cp.si + self.PL_ONLY[0]] & self.PL_ONLY[1]:
            return "MULTI-PL ONLY"
        return "MULTI-NAC" if cp.img[cp.si + self.MULTI_NAC[0]] & self.MULTI_NAC[1] else "DISABLED"

    def set(self, cp, ctx, value):
        old = self.get(cp, ctx)
        if value != old and value == "MULTI-PL ONLY" and ctx.get('interactive') and not ctx.get('_confirmed'):
            raise NeedsConfirm("Multi-coded squelch",
                               "WinRSS warns that setting Multi-Coded Squelch to MULTI-PL ONLY "
                               "clears the channel and access code tables, and shows the "
                               "multi-coded squelch table instead of the access code table.\n\n"
                               "qrss only sets the mode and leaves the tables alone - the DMCS "
                               "form shows them either way.\n\nContinue?")
        for bit in (self.PL_ONLY, self.MULTI_NAC):
            a = cp.si + bit[0]
            cp.img[a] = cp.img[a] & ~bit[1] & 0xFF
        want = []
        if value == "MULTI-PL ONLY":
            want.append(self.PL_ONLY)
        elif value == "MULTI-NAC":
            want.append(self.MULTI_NAC)
        for bit in want:
            a = cp.si + bit[0]
            cp.img[a] |= bit[1]


# ---------------------------------------------------------------- comparator codeplug (v0x61/v0x62)
# The comparator's directory has 8 entries: dir[0] STATION_INFO (58 bytes), dir[1] the 36-byte
# voting record, dir[2] the digital/conventional table, dir[3] the 24-byte trunking record,
# dir[4] ethernet, dir[5] the 50-byte port records, dir[6]/dir[7] two more.  Every table begins
# with a u16 count, so a record starts at dir[n]+2 (rsscore's section pointer is dir-2 and its
# field offsets are record+4).  Offsets below come from a CpProbe sweep of
# Training_comparator_3.9.cp (tools/cpprobe/out_*_fields_rest/sweep) and agree with rsscore.dll's
# own packers: FUN_10023270/FUN_10023360 for the port records, FUN_10028ac0/FUN_10028b90 for
# trunking, FUN_10028d00/FUN_10028df0 for the conventional table.

def port(off):
    return lambda cp, ctx: cp.port_off(ctx.get('row', 0)) + off


class PortLevel(Codec):
    """DPORTCTL level field: one signed byte in the port record, shown in dB/dBm.  WinRSS keeps
    in_level_diff negated (the packer writes `-(char)working`), the output levels as they are."""
    def __init__(self, off, negated=False):
        self.off_fn, self.negated = off, negated

    def get(self, cp, ctx):
        v = struct.unpack_from('>b', cp.img, self.off_fn(cp, ctx))[0]
        return str(-v if self.negated else v)

    def set(self, cp, ctx, value):
        v = int(value)
        if not -128 <= v <= 127:
            raise ValueError("level %r out of range" % value)
        struct.pack_into('>b', cp.img, self.off_fn(cp, ctx), -v if self.negated else v)


class PortLabel(Codec):
    """A DPORTCTL field WinRSS computes for display and never stores (the chassis/port number
    and the "PORT# n of 16" line): FUN_10061eb0 builds them with _sprintf out of the form's own
    position and saves nothing for them."""
    readonly = True

    def __init__(self, fn):
        self.fn = fn

    def get(self, cp, ctx):
        return self.fn(cp, ctx.get('row', 0))

    def set(self, cp, ctx, value):
        pass


class DataSitePort(Codec):
    """DPORTCTL alt_data_site / dssp_chassisnum - the two halves of one stored byte.

    The port record holds the data-site steering target as a global, 0-based port number:
    chassis = value // 16, port = value % 16 + 1.  rsscore keeps the same number in its working
    record (+0x74) and derives both controls from it - FUN_10060bb0/FUN_10060c30 move it by 0x10
    per chassis and rewrite the "PRIMARY"/"SECONDARYn" text at +0x76, and the port line is
    sprintf("PORT# %d of %d", value + 1 - chassis*16, 16).  Every archive plug carries the
    record's own index here (0..79), so port n of chassis m reads back as its own number.
    """
    def __init__(self, part):
        self.part = part
        self.cascade = True      # the byte it writes is what the other half displays
        self.clist = CHASSIS if part == 'chassis' else None

    def _raw(self, cp, ctx):
        return cp.img[port(23)(cp, ctx)]

    def get(self, cp, ctx):
        v = self._raw(cp, ctx)
        if self.part == 'chassis':
            return CHASSIS[min(v // PORTS_PER_CHASSIS, len(CHASSIS) - 1)]
        return str(v % PORTS_PER_CHASSIS + 1)

    def set(self, cp, ctx, value):
        v = self._raw(cp, ctx)
        if self.part == 'chassis':
            if value not in CHASSIS:
                raise ValueError("chassis %r" % value)
            n = CHASSIS.index(value) * PORTS_PER_CHASSIS + v % PORTS_PER_CHASSIS
        else:
            p = int(value)
            if not 1 <= p <= PORTS_PER_CHASSIS:
                raise ValueError("port %r out of range 1-%d" % (value, PORTS_PER_CHASSIS))
            n = (v // PORTS_PER_CHASSIS) * PORTS_PER_CHASSIS + p - 1
        cp.img[port(23)(cp, ctx)] = n


class TrunkChannel(Codec):
    """DTRKOTAC channel_no: a SmartZone channel 1..28, or 0x1D for NONE (the "NONE" string
    comparison in rsscore's packer writes 0x1D).  The choice list is descending, as WinRSS
    shows it."""
    NONE = 0x1D
    clist = ["NONE"] + [str(n) for n in range(28, 0, -1)]

    def _a(self, cp, ctx):
        return (cp.dir[3] & 0x7FFFFFFF) + 2

    def get(self, cp, ctx):
        v = cp.img[self._a(cp, ctx)]
        return "NONE" if v == self.NONE else str(v)

    def set(self, cp, ctx, value):
        v = str(value).strip().upper()
        if v == "NONE":
            cp.img[self._a(cp, ctx)] = self.NONE
        else:
            n = int(v)
            if not 1 <= n <= 28:
                raise ValueError("channel %r out of range 1-28" % value)
            cp.img[self._a(cp, ctx)] = n


CODECS = {}
_fm = os.path.join(os.path.dirname(__file__), 'data', 'fieldmap.json')
if os.path.exists(_fm):
    for _e in json.load(open(_fm)):
        CODECS[(_e['form'], _e['field'])] = Enum(_e) if _e['kind'] == 'enum' else Int(_e)

CODECS.update({
    ('DSTCG', 'serial_number'): FixedText(si(SI_SERIAL), 10),
    ('DSTCG', 'station_name'):  Text(si(SI_NAME), 22),
    ('DSTCG', 'rx_freq_band'):  Band(SI_RX1, RX_BAND_LABELS),
    ('DSTCG', 'pa_freq_band'):  Band(SI_TX, TX_BAND_LABELS, mirror=((SI_PA, True),), derived=True),
    ('DSTCG', 'pa_rated'):      PaRated(),
    ('DSTCG', 'rx_freq_band2'): Band2(SI_RX2, RX_BAND_LABELS),
    ('DCHN', 'channel'):        ChannelNumber(),
    ('DCHN', 'channel_num'):    ChannelOf(),
    ('DCHN', 'callsign'):       Callsign(),
    ('DCHN', 'rptr_holdoff'):   Int({'loc': 'chan', 'off': 0x26, 'size': 2, 'scale': 1, 'add': 0}),
    ('DCHN', 'local_tot'):      Tot([0x08]),
    ('DCHN', 'rptr_tot'):       Tot([0x09, 0x0A, 0x0B, 0x10]),
    ('DCHN', 'line_tot'):       Tot([0x0C, 0x0D, 0x0E, 0x11]),
    ('DCHN', 'mrti_tot'):       Tot([0x07]),
    ('DACC', 'rx_squelch_type'): Squelch(6, 'type'),
    ('DACC', 'rx_pl_dpl_1'):    Squelch(6, 'code'),
    ('DACC', 'tx_squelch_type'): Squelch(8, 'type'),
    ('DACC', 'tx_pl_dpl_1'):    Squelch(8, 'code'),
    ('DACC', 'rx_codeword'):    Nac(4),
    ('DACC', 'tx_codeword'):    Nac(2),
    ('DCHN', 'rx_freq'):        FreqHz(ch(0)),
    ('DCHN', 'tx_freq'):        FreqHz(ch(4)),
    ('DCHN', 'tx_idle_freq'):   FreqHz(ch(8)),
    ('DCHN', 'rx2_freq'):       FreqHz(lambda cp, ctx: (cp.dir[17] & 0x7FFFFFFF) + 2 + 64 * ctx.get('channel', 0)),
    ('DCHN', 'ptt_priority'):   PttPriority(),
    ('DSCANL', 'priority_chnl'): PriorityChannel(),
    ('DSCANL', 'scan_chnl_list.channel_no'): ScanChannel(),
    ('DSCANL', 'scan_chnl_list.scan'): ScanFlag(0x80),
    ('DSCANL', 'scan_chnl_list.tx_slave'): ScanFlag(0x40),
    ('DACC', 'trsmt_last_nac'): Flag('dir18', 0x22, 0x80),
    ('DHWCFG', 'simulcast'):     Flag('si', 0x22, 0x01),
    ('DHWCFG', 'comparator_name'): Text(si(0x16), 10),
    ('DGEN', 'def_tx_st_addr'):  Hex('si', 0x0B),
    ('DGEN', 'ht_hdlc_address'): HdlcAddress('si', 0x6F),
    ('DGEN', 'voice_repeat'):    Flag('si', 0x20, 0x01),
    ('DGEN', 'diu_repeat'):      DiuRepeat(),
    ('DVOICE', 'vote_hyst'):     VoteHyst(),
    ('DVOICE', 'voicehold_op'):  VoiceHoldOp(),
    ('DWIREL', 'remote_control'): RemoteControl({'slots': [['si', 0x4E, 4], ['si', 0x4F, 4]],
                                                 'options': {'NONE': [0, 0], 'TRC': [0, 4], 'ASTRO': [4, 0]},
                                                 'clist': ['NONE', 'TRC', 'ASTRO']}),
    ('DWIREL', 'con_priority'):  ConPriority(),   # derived from the PTT priority table
    ('DWIREL', 'out_link_timer'): OutLinkTimer(),
    # rsscore decodes FBCAB as (*(ushort *)(si-2+0x9e) & 0x200) >> 9 and the encoder writes
    # the same bit (FUN_1002eae0 / FUN_1002d820); in the byte-swapped image that is file
    # si+0x9C bit 1.  No sweep moved it - WinRSS shows DISABLED on every archive plug, and
    # they all have the bit clear.
    ('DWIREL', 'fallback_cabin'): Flag('si', 0x9C, 0x02),
    # The fallback timer is a big-endian u16 at si+0x42; the unlock sweep only moved its low
    # byte (si+0x43, value 0 -> 1), which is what CpProbe recorded.
    ('DWIREL', 'fallback_timer'): Int({'loc': 'si', 'off': 0x42, 'size': 2, 'scale': 1, 'add': 0}),
    # The forced sweep (tools/cpprobe/fields_force.tsv, 2026-11-07) moved all four.
    ('DRFCFG', 'simulcast_pl_rb'): Flag('si', 0xAC, 0x40, "EXTERNAL", "INTERNAL"),
    ('DRFCFG', 'rx_fade_timer'):   Int({'loc': 'si', 'off': 0x6E, 'size': 2, 'scale': 1, 'add': 0}),
    ('DRFCFG', 'fade_eom_timer'):  Int({'loc': 'si', 'off': 0x70, 'size': 2, 'scale': 1, 'add': 0}),
    # Expiration time is stored in seconds, shown in whole minutes.
    ('DACC', 'exp_time'):          Int({'loc': 'dir18', 'off': 0x24, 'size': 2, 'scale': 60, 'add': 0}),
    ('DACC', 'access_tb_range'):   AccessTableLabel(),
    ('DACC', 'astro_car_squ'):     AstroCarSquelch(),
    ('DACC', 'access_table'):      AccessTableNumber(),
    ('DACC', 'channels'):          AccessChannels(),
    ('DACC', 'rx_pl_dpl_2'):       SecondCode(),
    ('DACC', 'tx_pl_dpl_2'):       SecondCode(),
    # The DTRC form shows the same word the DWIREL form does.
    ('DTRC', 'hlgt_frequency'):    Int({'loc': 'dir8', 'off': 4, 'size': 2, 'scale': 1, 'add': 0}),
    # Comparator (tools/cpprobe/fields_force_cmp.tsv, 2026-11-07).
    ('DHWCFG', 'serial_number'):   FixedText(si(0x0C), 10),
    ('DHWCFG', 'tsbk_preamble'):   Int({'loc': 'si', 'off': 0x74, 'size': 2, 'scale': 1, 'add': 0}),
    ('DHWCFG', 'chassis_number'):  Enum({'slots': [['si', 0x9F, 0xFF]],
                                         'options': {'PRIMARY': [0], 'SECONDARY1': [1], 'SECONDARY2': [2],
                                                     'SECONDARY3': [3], 'SECONDARY4': [4]},
                                         'clist': ['PRIMARY', 'SECONDARY1', 'SECONDARY2', 'SECONDARY3', 'SECONDARY4']}),
})
for _n in range(1, 9):
    CODECS[('DACC', 'rx_nac%d' % _n)] = MultiNac(_n, False)
    CODECS[('DACC', 'tx_nac%d' % _n)] = MultiNac(_n, True)
# DTRC: ft0 is the guard tone, ft1..ft17 the function tones - one list each.  DDCRC: twelve
# rows over the same table, six currents x Detect/Undetect, so row N is list N-1.
for _ft in range(18):
    for _c in range(8):
        CODECS[('DTRC', 'ft%d_cmd%d' % (_ft, _c))] = TrcCmd(_ft, _c)
        if 1 <= _ft <= 12:
            CODECS[('DDCRC', 'ft%d_cmd%d' % (_ft, _c))] = TrcCmd(_ft - 1, _c)


CODECS[('DSTCG', 'system_type')] = Detected(SYSTEM_TYPES, detect_system_type)   # read-only decode
CODECS[('DSTCG', 'modulation')] = Detected(MODULATIONS, detect_modulation)       # read-only decode
CODECS[('DSTCG', 'ir_freq_band')] = Detected(IR_BANDS, detect_ir_freq_band)     # read-only decode
CODECS[('DSTCG', 'hw_platform')] = HwPlatform()                                 # platform toggle

# Multi-Coded Squelch mode: fieldmap knows the two options a CpProbe sweep could reach; the
# third (MULTI-PL ONLY) comes from rsscore's own packer, which writes it to the same byte.
CODECS[('DSTCG', 'multi_coded_sq')] = MultiCodedSq()

# DMCS: the form's rows are the access code tables (dir[7]); the dialog fields edit the row
# the form is on (ctx['row']), so they share the row's codecs.
_DMCS_ROW = {
    ('DMCS', 'mcs_entry.entry_no'):  McsEntryNumber(),
    ('DMCS', 'mcs_entry.rx_sq'):     StoreSquelch(4, 'type', daccess),
    ('DMCS', 'mcs_entry.rx_code'):   StoreSquelch(4, 'code', daccess),
    ('DMCS', 'mcs_entry.tx_sq'):     StoreSquelch(6, 'type', daccess),
    ('DMCS', 'mcs_entry.tx_code'):   StoreSquelch(6, 'code', daccess),
    ('DMCS', 'mcs_entry.access'):    UserAccess(),
    ('DMCS', 'mcs_entry.time_used'): McsTime(),
    ('DMCS', 'mcs_entry.no_of_calls'): McsCalls(),
    ('DMCS', 'rx_squelch_type'):     StoreSquelch(4, 'type', daccess),
    ('DMCS', 'rx_code'):             StoreSquelch(4, 'code', daccess),
    ('DMCS', 'tx_squelch_type'):     StoreSquelch(6, 'type', daccess),
    ('DMCS', 'tx_code'):             StoreSquelch(6, 'code', daccess),
    ('DMCS', 'access'):              UserAccess(),
    ('DMCS', 'entry_no'):            McsEntryNumber(),
}
for _k, _c in _DMCS_ROW.items():
    if isinstance(_c, Squelch) and _c.kind == 'type':
        _c.clist = ["PL", "DPL"]        # DMCS entries are PL/DPL only (DMCSMetadata.java)
CODECS.update(_DMCS_ROW)
CODECS.update({
    ('DMCS', 'serial_number'): FixedText(si(SI_SERIAL), 10),
    ('DMCS', 'mcs_date'):      McsStamp('date'),
    ('DMCS', 'mcs_time'):      McsStamp('time'),
})

# DHWCFG (comparator) system/comparator type: one byte each, but WinRSS also rewrites a
# per-record type code through the 50-byte table at si+0xDC, so they stay read-only.
CODECS[('DHWCFG', 'system_type')] = Detected(
    ['CONVENTIONAL', 'SMARTZONE'],
    lambda cp: {0: 'CONVENTIONAL', 2: 'SMARTZONE'}.get(cp.img[cp.si + 2], "UNKNOWN (%02X)" % cp.img[cp.si + 2]))
CODECS[('DHWCFG', 'comparator_type')] = Detected(
    ['STANDARD', 'EXPANDED'],
    lambda cp: {0: 'STANDARD', 1: 'EXPANDED'}.get(cp.img[cp.si + 0x9E], "UNKNOWN (%02X)" % cp.img[cp.si + 0x9E]))

# --- comparator: Hardware Configuration (DHWCFG) -------------------------------------
CODECS.update({
    ('DHWCFG', 'battery'):        Enum({'slots': [['si', 0x09, 0xFF]],
                                        'options': {'NONE': [0], 'LEAD_ACID_LIN': [3], 'LEAD_ACID_NON': [1],
                                                    'NICAD': [2]},
                                        'clist': ['NONE', 'LEAD_ACID_LIN', 'LEAD_ACID_NON', 'NICAD']}),
    ('DHWCFG', 'simul_lt_delay'): Int({'loc': 'si', 'off': 0x25, 'size': 1, 'scale': 1, 'add': 0}),
    ('DHWCFG', 'vote_scan'):      Flag('si', 0x2A, 0x04),
})

# --- comparator: Conventional System (DGEN) and Smartzone System (DTRKOTAC) ----------
# vote_priority is one byte at the head of the voting record (dir[1]+2) and is shown by both
# forms; the three hang times are milliseconds in the conventional table, shown in seconds.
_VOTE_PRIORITY = Enum({'slots': [['dir1', 0x02, 0xFF]],
                        'options': {'ANALOG>DIGITAL': [0], 'DIGITAL>ANALOG': [1], 'FIRST IN': [2]},
                        'clist': ['ANALOG>DIGITAL', 'DIGITAL>ANALOG', 'FIRST IN']})
CODECS.update({
    ('DGEN', 'vote_priority'):    _VOTE_PRIORITY,
    ('DGEN', 'src_base_prior'):   Enum({'slots': [['dir1', 0x03, 0xFF]],
                                         'options': {'REPEAT': [0], 'CONSOLE': [1]},
                                         'clist': ['REPEAT', 'CONSOLE']}),
    ('DGEN', 'ana_rep_hang'):     Int({'loc': 'dir2', 'off': 0x02, 'size': 2, 'scale': 1000, 'add': 0,
                                        'decimals': 1}),
    ('DGEN', 'ana_con_hang'):     Int({'loc': 'dir2', 'off': 0x04, 'size': 2, 'scale': 1000, 'add': 0,
                                        'decimals': 1}),
    ('DGEN', 'dig_rep_hang'):     Int({'loc': 'dir2', 'off': 0x06, 'size': 2, 'scale': 1000, 'add': 0,
                                        'decimals': 1}),
    ('DGEN', 'dig_con_hang'):     Int({'loc': 'dir2', 'off': 0x08, 'size': 2, 'scale': 1000, 'add': 0,
                                        'decimals': 1}),
    ('DGEN', 'dig_datcon_hang'):  Int({'loc': 'dir2', 'off': 0x0C, 'size': 2, 'scale': 1000, 'add': 0,
                                        'decimals': 1}),
    ('DTRKOTAC', 'failsoft'):     Enum({'slots': [['dir3', 0x03, 0xFF]],
                                         'options': {'ENABLED': [0], 'DISABLED': [1]},
                                         'clist': ['ENABLED', 'DISABLED']}),
    ('DTRKOTAC', 'failsoft_mode'): Enum({'slots': [['dir3', 0x04, 0xFF]],
                                          'options': {'ANALOG': [0], 'DIGITAL': [1]},
                                          'clist': ['ANALOG', 'DIGITAL']}),
    ('DTRKOTAC', 'channel_no'):   TrunkChannel(),
    ('DTRKOTAC', 'vote_priority'): _VOTE_PRIORITY,
})

# --- comparator: Analog Voting (DVOICE) ---------------------------------------------
# The 36-byte voting record: nine big-endian u16s from +2.  vote_hyst (+6) and voicehold_op
# are decoded by their own codecs above.
CODECS.update({
    ('DVOICE', 'first_vote_set'): Int({'loc': 'dir1', 'off': 0x04, 'size': 2, 'scale': 1, 'add': 0}),
    ('DVOICE', 'vote_hold'):      Int({'loc': 'dir1', 'off': 0x06, 'size': 2, 'scale': 1, 'add': 0}),
    ('DVOICE', 'vote_sample'):    Int({'loc': 'dir1', 'off': 0x0A, 'size': 2, 'scale': 1, 'add': 0}),
    ('DVOICE', 'vote_switch'):    Int({'loc': 'dir1', 'off': 0x0C, 'size': 2, 'scale': 1, 'add': 0}),
    ('DVOICE', 'first_vote_hld'): Int({'loc': 'dir1', 'off': 0x0E, 'size': 2, 'scale': 1, 'add': 0}),
    ('DVOICE', 'line_trans_set'): Int({'loc': 'dir1', 'off': 0x10, 'size': 2, 'scale': 1, 'add': 0}),
})

# --- comparator: Port Control (DPORTCTL) --------------------------------------------
# One 50-byte LINK_CTRL_DATA record per port, 16 ports per chassis, ctx['row'] selects it.
CODECS.update({
    ('DPORTCTL', 'port_alias'):      Text(port(0), 12),
    ('DPORTCTL', 'link_type'):       Enum({'slots': [['port', 12, 0xFF]],
                                            'options': {'MODEM - 37 dBm': [0], 'MODEM - 28 dBm': [1],
                                                        'V.24 LINK - EXTERNAL TRANSMIT CLOCK': [2],
                                                        'V.24 LINK - INTERNAL TRANSMIT CLOCK': [3],
                                                        'HYBRID LINK - EXTERNAL TRANSMIT CLOCK': [4],
                                                        'HYBRID LINK - INTERNAL TRANSMIT CLOCK': [5]},
                                            'clist': ['MODEM - 37 dBm', 'MODEM - 28 dBm',
                                                      'V.24 LINK - EXTERNAL TRANSMIT CLOCK',
                                                      'V.24 LINK - INTERNAL TRANSMIT CLOCK',
                                                      'HYBRID LINK - EXTERNAL TRANSMIT CLOCK',
                                                      'HYBRID LINK - INTERNAL TRANSMIT CLOCK']}),
    ('DPORTCTL', 'port_type'):       Enum({'slots': [['port', 13, 0xFF]],
                                            'options': {'DISABLED': [0], 'STATION/RECEIVER': [1], 'DIU': [2]},
                                            'clist': ['DISABLED', 'STATION/RECEIVER', 'DIU']}),
    ('DPORTCTL', 'diu_rank'):        Int({'loc': 'port', 'off': 15, 'size': 1, 'scale': 1, 'add': 0}),
    ('DPORTCTL', 'in_level_diff'):   PortLevel(port(18), negated=True),
    ('DPORTCTL', 'output_modem'):    PortLevel(port(19)),
    ('DPORTCTL', 'output_analog'):   PortLevel(port(20)),
    ('DPORTCTL', 'output_idltone'):  PortLevel(port(21)),
    ('DPORTCTL', 'an_link_timer'):   Int({'loc': 'port', 'off': 24, 'size': 2, 'scale': 1, 'add': 0}),
    ('DPORTCTL', 'input_digital'):   Int({'loc': 'port', 'off': 28, 'size': 2, 'scale': 1, 'add': 0}),
    ('DPORTCTL', 'alt_data_site'):   DataSitePort('port'),
    ('DPORTCTL', 'dssp_chassisnum'): DataSitePort('chassis'),
    ('DPORTCTL', 'cur_chassis_num'): PortLabel(lambda cp, r: CHASSIS[min(r // PORTS_PER_CHASSIS, len(CHASSIS) - 1)]),
    ('DPORTCTL', 'port_number'):     PortLabel(lambda cp, r: str(r % PORTS_PER_CHASSIS + 1)),
    ('DPORTCTL', 'port_range_msg'):  PortLabel(lambda cp, r: "PORT# %d of %d"
                                                % (r % PORTS_PER_CHASSIS + 1, PORTS_PER_CHASSIS)),
})


def codec(form, field):
    return CODECS.get((form, field))
