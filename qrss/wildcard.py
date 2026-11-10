"""Quantar WildCard block (second S2 section of a .cp, "// WILDCARD VERSION 00 01").

Layout (big-endian), recovered with CpProbe from WinRSS's rsscore.dll:

  0x00 u16 checksum (16-bit byte sum from offset 2)   0x02 u16 0
  0x04 u16 size        0x06 u16 version (1)            0x08 u16 number of inputs (12)
  0x0A u16[12] input records    u16 number of outputs (10)    u16[10] output records
       record: bit15 = active level HI, bits14-12 = binary group, bits11-8 = role
               (role 0x4 = channel binary group, 0x8 = MCS group, 0xC = activation group)
  then state tables until the end of the block:
       char[16] description
       3 x { u16 state code (0xFFFF = none), u16 arg (1 = no value, else value-1) }
       u8 condition 1, u8 condition 2      (0 AND/none, 1 OR, 2 AND NOT, 3 OR NOT)
       items: u8 header, u8 opcode, [u8 index], [value, 1-2 bytes big-endian]
              header bit7 = action (else inaction), bit6 = last item of the table,
              bit5 = last item of the last table (end of block),
              bit4 = index byte present (timer number / switch), bits3-2 = value byte count
"""
import json, os, struct

CODES = json.load(open(os.path.join(os.path.dirname(__file__), 'data', 'wildcard_codes.json')))
STATES = CODES['states']                    # name -> {code, value, base?}
ACTIONS = CODES['actions']                  # name -> {op, idx, value, base?}
COND = ['AND', 'OR', 'AND NOT', 'OR NOT']
STATE_BY_CODE = {v['code']: k for k, v in STATES.items()}
ACTION_BY_KEY = {(v['op'], v['idx']): k for k, v in ACTIONS.items()}
NULL = 'NULL'


class Item:
    def __init__(self, name, value=None, raw=None):
        self.name, self.value, self.raw = name, value, raw   # raw = (op, idx, nbytes) for unknown ops

    def __repr__(self):
        return "%s%s" % (self.name, "" if self.value is None else " %s" % self.value)


class Table:
    def __init__(self, desc='', states=None, conds=(0, 0), actions=None, inactions=None):
        self.desc = desc
        self.states = states or [None, None, None]     # (name, value) or None
        self.conds = list(conds)
        self.actions = actions or []
        self.inactions = inactions or []


class Wildcard:
    def __init__(self, img):
        self.img = img            # the CpFile section bytearray (kept for checksum/size on save)
        d = bytes(img)
        self.version = struct.unpack_from('>H', d, 6)[0]
        nin = struct.unpack_from('>H', d, 8)[0]
        o = 10
        self.inputs = list(struct.unpack_from('>%dH' % nin, d, o)); o += 2 * nin
        nout = struct.unpack_from('>H', d, o)[0]; o += 2
        self.outputs = list(struct.unpack_from('>%dH' % nout, d, o)); o += 2 * nout
        self.tables = []
        while o < len(d):
            o = self._parse_table(d, o)

    def _parse_table(self, d, o):
        t = Table(d[o:o + 16].split(b'\0')[0].decode('latin1')); o += 16
        for k in range(3):
            code, arg = struct.unpack_from('>HH', d, o); o += 4
            if code == 0xFFFF:
                t.states[k] = None
                continue
            name = STATE_BY_CODE.get(code, 'STATE %02X' % code)
            s = STATES.get(name, {})
            val = (arg + s.get('base', 0)) if s.get('value') else None
            t.states[k] = (name, val, arg)
        t.conds = [d[o], d[o + 1]]; o += 2
        while True:
            h, op = d[o], d[o + 1]; o += 2
            idx = None
            if h & 0x10:
                idx = d[o]; o += 1
            n = (h >> 2) & 3
            val = int.from_bytes(d[o:o + n], 'big') if n else None; o += n
            name = ACTION_BY_KEY.get((op, idx)) or ACTION_BY_KEY.get((op, None))
            if name is None:
                it = Item('OP %02X%s' % (op, '' if idx is None else ' %d' % idx), val, (op, idx, n))
            else:
                base = ACTIONS[name].get('base', 0)
                it = Item(name, None if val is None else val + base)
            it.nbytes = n          # keep the stored width (RSS sometimes keeps stale value bytes)
            (t.actions if h & 0x80 else t.inactions).append(it)
            if h & 0x40:
                break
        self.tables.append(t)
        return o

    # ------------------------------------------------------------------ serialize
    def to_bytes(self):
        out = bytearray(struct.pack('>HHHHH', 0, 0, 0, self.version, len(self.inputs)))
        out += struct.pack('>%dH' % len(self.inputs), *self.inputs)
        out += struct.pack('>H', len(self.outputs))
        out += struct.pack('>%dH' % len(self.outputs), *self.outputs)
        for ti, t in enumerate(self.tables):
            out += t.desc.encode('latin1')[:15].ljust(16, b'\0')
            for s in t.states:
                if s is None:
                    out += struct.pack('>HH', 0xFFFF, 0)
                    continue
                name, val, arg = (s + (None,))[:3] if len(s) == 2 else s
                info = STATES[name] if name in STATES else {'code': int(name.split()[1], 16)}
                if info.get('value'):
                    arg = int(val) - info.get('base', 0)
                elif arg is None:
                    arg = 1
                out += struct.pack('>HH', info['code'], arg)
            out += bytes(t.conds)
            items = [(True, i) for i in t.actions] + [(False, i) for i in t.inactions]
            if not items:
                items = [(True, Item(NULL))]
            for k, (is_act, it) in enumerate(items):
                if it.raw:
                    op, idx, n = it.raw
                    val = it.value
                else:
                    a = ACTIONS[it.name]
                    op, idx = a['op'], a['idx']
                    if not a['value'] and getattr(it, 'nbytes', 0) and it.value is not None:
                        val, n = it.value, it.nbytes          # stale bytes kept as-is
                    elif a['value'] and it.value is None and hasattr(it, 'nbytes') and it.nbytes == 0:
                        val, n = None, 0
                    elif a['value']:
                        val = int(it.value if it.value is not None else a.get('base', 0)) - a.get('base', 0)
                        n = getattr(it, 'nbytes', None) or (1 if val < 256 else 2)
                        if val >= 256 and n < 2:
                            n = 2
                    else:
                        val, n = None, 0
                last = k == len(items) - 1
                h = (0x80 if is_act else 0) | (0x40 if last else 0) | \
                    (0x20 if last and ti == len(self.tables) - 1 else 0) | \
                    (0x10 if idx is not None else 0) | (n << 2)
                out += bytes([h, op]) + (bytes([idx]) if idx is not None else b'') + \
                    (val.to_bytes(n, 'big') if n else b'')
        struct.pack_into('>H', out, 4, len(out))
        struct.pack_into('>H', out, 0, sum(out[2:]) & 0xFFFF)
        return bytes(out)

    def store(self):
        """Write back into the CpFile section (size changes are fine: S2 records are regenerated)."""
        self.img[:] = self.to_bytes()

    # ------------------------------------------------------------------ merge
    def io_mismatch(self, other):
        """The aux line settings (active level / binary group) that differ from another block, as
        ['input 3', 'output 7', ...].  Informational only: a merge keeps the destination's own
        settings, and BASIC vs ENHANCED differs in which lines the wireline module has, not in what
        the tables can do - every block stores the full 12 inputs / 10 outputs either way, and the
        timers and functions are identical in both tiers."""
        bad = []
        for what, a, b in (("input", self.inputs, other.inputs), ("output", self.outputs, other.outputs)):
            for i in range(max(len(a), len(b))):
                if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None):
                    bad.append("%s %d" % (what, i + 1))
        return bad

    def merge(self, other, tables=None, replace=False):
        """Bring state tables over from another Wildcard block (WinRSS's "MERGE WildCard Data").

        `tables` picks source tables by index (default: all).  A table whose description already
        exists here replaces it in place when replace=True, otherwise both are kept.  Returns
        [(description, 'added'|'replaced'), ...].  The inputs/outputs of this block are kept -
        the destination station's wiring is what the tables have to match; see io_mismatch."""
        log = []
        for i in (range(len(other.tables)) if tables is None else tables):
            t = other.tables[i]
            k = next((j for j, u in enumerate(self.tables) if u.desc == t.desc), None) if replace else None
            if k is None:
                self.tables.append(t); log.append((t.desc, 'added'))
            else:
                self.tables[k] = t; log.append((t.desc, 'replaced'))
        return log

    # ------------------------------------------------------------------ text
    def describe(self, t):
        def st(s):
            if s is None:
                return None
            return s[0] + ("" if s[1] is None else " %s" % s[1])
        parts = []
        for k, s in enumerate(t.states):
            if s is None:
                break
            if k:
                parts.append(COND[t.conds[k - 1]] if t.conds[k - 1] < 4 else '?')
            parts.append(st(s))
        cond = " ".join(parts) or "(always)"
        a = ", ".join(map(str, t.actions)) or "-"
        i = ", ".join(map(str, t.inactions)) or "-"
        return "IF %s\n   THEN %s\n   ELSE %s" % (cond, a, i)


def io_group(word):
    """(active_high, group, role) from an input/output record."""
    return bool(word & 0x8000), (word >> 12) & 7, (word >> 8) & 0x0F
