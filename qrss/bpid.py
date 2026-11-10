"""BPID license tokens for the Quantar SCM (CHECK ID).

A token is ``01`` + 24 bytes hex.  Eight of those 24 bytes are the IV, scattered through
the token in the order POS; the other 16 are the ciphertext.  The plaintext is 14 data
bytes plus a big-endian CRC-16/ARC over them, and the cipher is a stream XOR, so encrypt
and decrypt are the same operation.

The key selects the token's owner:

    master token    00 00 EA 00 DE AD 35 D3          (accepted by every station)
    station token   00 00 || BPN[2:6] || BPN[0:2]    (the 6-byte BPN rotated left by 2)

The 14 data bytes are never looked at - only the CRC is checked - so any 14 bytes make a
valid token.  Recovered from the application's 0x0CA99C (CHECK ID) -> 0x0CABB8 (key
setup) -> 0x0CAAE0 (parser) -> 0x120558 (cipher, via the 0x806115C dispatch table) ->
0x0CAA58 (CRC).  The cipher was transcribed from 0x12061C/0x1208D6/0x120B4E and verified
against a m68k emulation of the routines.

    python3 -m qrss.bpid 000007AFB79E          # mint a token for that station
    python3 -m qrss.bpid --master              # mint a master token
    python3 -m qrss.bpid --check TOKEN BPN     # is TOKEN valid for that BPN?
    python3 -m qrss.bpid --file BPN            # the "DECRYPT <token>" line for a package
"""

M32 = 0xFFFFFFFF

POS = (2, 5, 7, 8, 0xB, 0xD, 0x10, 0x15)      # 0x8061120: token positions holding the IV
OFF = (2, 5, 7, 3, 1, 4, 6, 0)                # 0x8061140: where each lands in the 8-byte IV

MASTER_KEY = bytes.fromhex("0000EA00DEAD35D3")
MASTER_IV = bytes.fromhex("DEADBEEFBABEDAFE")
KCONST = (0x00000000, 0x00001855, 0xB6B7A202, 0x9D679E82, 0x6017CEAB, 0x732E75DF)
MIX1 = (0xF7AC118A, 0x79136A27)                # state +0x0C / +0x14
MIX2 = (0xA98B8733, 0x5D71720D)                # state +0x20 / +0x28


def crc16(data):
    """CRC-16/ARC (reflected 0xA001, init 0) - the station's 0x0800045E byte routine."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c & 0xFFFF


def _shift96(w, bit):
    """0x120B4E: shift the 2-long LFSR; it is called with start index 2, which the
    do-while skips, so only words 1 and 0 move."""
    for i in (1, 0):
        b = (w[i] >> 31) & 1
        w[i] = ((w[i] << 1) | bit) & M32
        bit = b


def _key_schedule(w):
    """0x1209A8 then 0x120A42: expand the 8-byte key into six 32-bit words."""
    for _ in range(0x80):
        a, b = w[0], w[1]
        _shift96(w, ((a >> 20) ^ (a >> 16) ^ (a >> 6) ^ (a >> 3) ^ (a >> 2) ^ (a >> 1) ^
                     (b >> 28) ^ (b >> 20) ^ (b >> 15) ^ (b >> 1)) & 1)
    a, b = w[0], w[1]
    out = [0] * 6
    for _ in range(0x34):
        for j in range(5):
            out[j] = ((out[j] << 1) | (1 if out[j + 1] & 0x80000000 else 0)) & M32
        out[5] = (out[5] << 1) & M32
        if a & 0x80000:
            for j in range(6):
                out[j] ^= KCONST[j]
        a = ((a << 1) | (1 if b & 0x80000000 else 0)) & M32
        b = (b << 1) & M32
    return out


class _State:
    """The cipher state block the application keeps at a2 (0x78 bytes)."""

    def __init__(self, key, iv):
        w = [int.from_bytes(key[0:4], "big") & 0xFFFF, int.from_bytes(key[4:8], "big"), 0]
        ks = _key_schedule(w)
        self.st = [int.from_bytes(iv[0:4], "big"), int.from_bytes(iv[4:8], "big"), ks[0]]
        self.ks1 = [ks[0], MIX1[0], ks[2], MIX1[1], ks[4]]
        self.ks2 = [ks[1], MIX2[0], ks[3], MIX2[1], ks[5]]
        self.reg = [[0] * 5, [0] * 5, [0] * 5]
        self.fb = [[0] * 5, [0] * 5, [0] * 5]
        self.c = [0, 0, 0, 0]
        self.t = [0, 0]

    def _step(self, k):
        """0x1208D6: one register step; leaves t0/t1."""
        w = self.reg[0][k]
        idx = 0
        for _ in range(5):
            idx = (idx << 1) | (w & 1)
            w >>= 1
        self.fb[0][k] = self.c[1]
        self.fb[1][k] = (self.ks1[k] >> idx) & 1
        self.fb[2][k] = (self.ks2[k] >> idx) & 1
        self.t[0] = self.fb[1][k]
        b7 = (self.reg[0][k] >> 7) & 1
        self.t[1] = ((b7 & (self.fb[2][k] ^ self.c[0])) | (b7 & self.c[2]) |
                     (self.c[2] & self.c[3])) & 1

    def _step_setup(self, k, src, c2b, c3b):
        """c2/c3 come from the state long at a2+0 (s0) or a2+4 (s1), per the caller."""
        self.c[2], self.c[3] = (src >> c2b) & 1, (src >> c3b) & 1
        self._step(k)
        self.c[0], self.c[1] = self.t

    def byte(self):
        """0x12061C: one keystream byte."""
        out = 0
        for _ in range(8):
            s0, s1 = self.st[0], self.st[1]
            carry = ((s1 >> 0xA) ^ (s1 >> 0xD) ^ (s1 >> 0xF) ^ (s1 >> 0x17) ^
                     (s1 >> 0x19) ^ (s1 >> 0x1A) ^ (s1 >> 0x1C) ^ (s1 >> 0x1D) ^
                     (s0 >> 1) ^ (s0 >> 2) ^ (s0 >> 0x10) ^ (s0 >> 0x1F)) & 1
            w = 0
            for v, bit in ((s0, 1), (s1, 0x1A), (s1, 0x14), (s1, 0xF), (s1, 0x7)):
                w = (w << 1) | ((v >> bit) & 1)
            self.reg[0][0] = w | (0x80 if (s0 >> 4) & 1 else 0)
            self.c[0], self.c[1] = (s1 >> 1) & 1, (s0 >> 4) & 1
            self.c[2], self.c[3] = (s1 >> 8) & 1, (s0 >> 0x1A) & 1
            self._step(0)
            self.c[0], self.c[1] = self.t
            self._step_setup(1, s1, 0x13, 0x1F)
            self._step_setup(2, s0, 9, 0x12)
            self._step_setup(3, s0, 0xC, 0x15)
            self._step_setup(4, s0, 0x1D, 0x1F)
            bit = self.t[0] ^ self.t[1]
            for k in range(3):
                for j in range(5):
                    self.reg[k][j] = ((self.reg[k][j] << 1) | self.fb[k][j]) & 0xFF
            _shift96(self.st, carry)
            out = (out << 1) | bit
        return out

    def crypt(self, data):
        return bytes(b ^ self.byte() for b in data)


def crypt(key, iv, data):
    """Encrypt == decrypt (stream XOR)."""
    return _State(key, iv).crypt(data)


def bpn_key(bpn):
    """The device key for a 6-byte BPN (hex string or bytes): 00 00 + BPN rotated left 2."""
    if isinstance(bpn, str):
        bpn = bytes.fromhex(bpn)
    if len(bpn) != 6:
        raise ValueError("BPN must be 6 bytes")
    return b"\x00\x00" + bpn[2:6] + bpn[0:2]


def parse(token):
    """Split a token string into (type, iv, ciphertext)."""
    raw = bytes.fromhex(token)
    if len(raw) != 25:
        raise ValueError("not a 24-byte token")
    body = raw[1:]
    iv = bytearray(8)
    main = bytearray()
    j = 0
    for i, b in enumerate(body):
        if j < 8 and POS[j] == i:
            iv[OFF[j]] = b
            j += 1
        else:
            main.append(b)
    return raw[0], bytes(iv), bytes(main)


def build(key, iv, payload=None, kind=1):
    """Make a token for this key/IV.  payload is the 14 data bytes (any will do)."""
    if payload is None:
        payload = bytes(range(1, 15))
    pt = bytes(payload) + crc16(bytes(payload)).to_bytes(2, "big")
    ct = crypt(key, iv, pt)
    body = bytearray(24)
    it = iter(ct)
    for i in range(24):
        body[i] = iv[OFF[POS.index(i)]] if i in POS else next(it)
    return "%02X" % kind + body.hex().upper()


def master_token(payload=None):
    """A master token (accepted by any station)."""
    return build(MASTER_KEY, MASTER_IV, payload)


def station_token(bpn, iv=None, payload=None):
    """A token for one station, from its 6-byte BPN (hex string or bytes)."""
    return build(bpn_key(bpn), MASTER_IV if iv is None else iv, payload)


def check(token, bpn=None):
    """Mimic CHECK ID: True if the token is valid for this BPN (or a master token).

    The station tries the device key first and retries once with the master key."""
    try:
        _, iv, ct = parse(token)
    except (ValueError, IndexError):
        return False
    keys = [MASTER_KEY] if bpn is None else [bpn_key(bpn), MASTER_KEY]
    for key in keys:
        pt = crypt(key, iv, ct)
        if crc16(pt[:14]) == int.from_bytes(pt[14:16], "big"):
            return True
    return False


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
    elif args[0] == "--master":
        print(master_token())
    elif args[0] == "--check":
        print("valid" if check(args[1], args[2] if len(args) > 2 else None) else "invalid")
    elif args[0] == "--file":
        # the DECRYPT line a package's BPID file starts with
        print("DECRYPT " + station_token(args[1]))
    else:
        print(station_token(args[0]))
