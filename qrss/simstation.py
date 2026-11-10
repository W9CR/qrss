"""A pretend Quantar for trying the service screens without hardware (`--sim`).

Implements just enough of the ASCII MMI: prompts and mode changes, the GET/SET/AL
commands used by the alignment screens (with state), and MTR/FPM real-time streams.
Values are plausible, not real.
"""
import datetime, random, re, struct, threading, time

from .codeplug import Codeplug
from .station import status_checksum

# Key sets, value styles and order as a real station sends them (R020.14.048 SCM, CLN6955 wireline,
# hwtest 2026-11-05).  Values are this simulator's own.  A string value is an error reply instead
# of a stream; an empty list streams nothing (no UHSO fitted).
MTR = {
    "MTR TX_PA_P1": [("PA_TEST", "OFF"), ("PA_FULL", "{pa}"), ("TX_V_OMNI", "0.000"), ("TX_PA_TEMP", "2.410"),
                     ("TX_IPA_VF", "1.200"), ("TX_DPA_VF", "2.400"), ("TX_FPA_VF", "3.100"), ("TX_VR", "0.118"),
                     ("TX_VF", "{fwd:.3f}"), ("TX_PCIC_VCTRL_DIV2", "2.310"), ("TX_SL_LEVEL_SYNTH", "4.620"),
                     ("TX_2PT1_REF_LEVEL", "3.687"), ("TX_28V", "28.053"), ("TX_14PT2V", "14.149"),
                     ("TX_10PT2V", "9.870"), ("TX_PA_ID_A", "VHF R4 EXCITER"), ("TX_PA_ID_B", "VHF R4 PA 110W"),
                     ("TX_FPA_I2_A", "{ia:.3f}"), ("TX_FPA_I2_B", "{ib:.3f}"), ("TX_IPA_I", "0.420"),
                     ("TX_DPA_I", "1.100"), ("TX_PF", "{pf:.0f}"), ("TX_PR", "{pr:.0f}"), ("TX_VSWR", "{vswr:.1f}")],
    "MTR RX_P1": [("RX_ID", "VHF R4 RECEIVER"), ("RX_APLUS", "14.15"), ("RX_10V", "9.88"), ("RX_5V", "{rx5:.2f}"),
                  ("RX_SL_LO1", "{sl1:.2f}"), ("RX_SL_LO2", "2.71"), ("RX_REF_LEVEL", "1.92"),
                  ("RX_LO1_LOCK", "4.92"), ("RX_INJ_LEVEL", "0.47"), ("RX_PRE_BIAS", "4.20"), ("RX_AD_CHK", "2.51")],
    "MTR RX2_P1": "?(0086) receiver board disabled -- can't run metering",
    "MTR PS_P1": [("PS_14V", "14.20"), ("PS_5V", "5.10"), ("PS_BAT_TEMP", "25.00"), ("PS_HS_TEMP", "{hs:.2f}"),
                  ("PS_BAT_CHR_VOLT", "0.00")],
    "MTR UHSO": [],
    "MTR WL_P1": [("SC_ID", "CLN6960A/B/C"), ("BPN_ID", "000000SIM001"), ("WL_ID", "CLN6955A, 4WIRE"),
                  ("WL_9PT6V", "9.60"), ("WL_NEG_9PT6V", "-9.60")],
    "FPM": [("TIME", "{time}"), ("CHN", "{chn}"), ("SQ", "{sq_mode}"), ("SPKRVOL", "{vol}"), ("INTCOM", "{intcom}"),
            ("ACC_DIS", "{accdis}"), ("TX_INHIBIT", "OFF"), ("AC_ON", "ON"), ("AC_FAIL", "OFF"), ("CTRL_ON", "ON"),
            ("CTRL_FAIL", "OFF"), ("TX_LOCK", "ON"), ("RX_LOCK", "ON"), ("RX2_LOCK", "NOT_INST"),
            ("RX1_ACTIVE", "{rxact}"), ("RX2_ACTIVE", "OFF"), ("RX1_FAIL", "OFF"), ("RX2_FAIL", "OFF"),
            ("PA_FULL", "{pa}"), ("PA_FAIL", "OFF"), ("PA_TEST", "OFF"), ("WL_ON", "ON"), ("WL_FAIL", "OFF"),
            ("5MHZ_NET", "OFF"), ("BOOSTER_FAIL", "NI"), ("STN REF STATUS", "ACTIVE"), ("RPTR MODE", "VOICE"),
            ("RPTR CUR", "0"), ("RPTR RX FREQ", "{rxfreq}"), ("RPTR TX FREQ", "{txfreq}"),
            ("RPTR SITE STATUS", "FAILSOFT"), ("RPTR RX STATUS", "NO_STATE"), ("RPTR TX STATUS", "NO_STATE"),
            ("RPTR WL STATUS", "NO_STATE"), ("RPTR DLM STATUS", "NO_STATE"), ("RPTR PIB STATUS", "NO_STATE"),
            ("RPTR DLAN1 STATUS", "DOWN"), ("RPTR DLAN2 STATUS", "DOWN"), ("RPTR ACT MAST", "NO"),
            ("RPTR BSI CHN", "NO"), ("RPTR CONTROL_CHN CHN", "NO"), ("RPTR DISPATCH CHN", "NO")],
}
STREAM_GAP = 0.15        # the real station sends one value about every 150 ms
ERR_CMD = "?(0014) invalid command"
ERR_OPT = "?(0017) invalid option"
ERR_INTERNAL = "?(0011) unable to comply due to internal software error"
# the alignment prerequisites the lab station (H026.11.05f) enforces - see
# Quantar_Firmware_Analysis.md, "Alignment write sequences on the station"
ERR_GO = "?(0009) you must first issue a GO subcommand"
ERR_F5 = "?(0009) you must first issue an F5, or an F6 subcommand"
ERR_RANGE = "?(0028) one or more arguments is out of range"
ERR_LOW = "?(0023) input level is too low"
ERR_CAL = "?(0037) unable to calibrate"
ERR_TASK = "?(0010) a required task has not responded!"     # first AL PEND STATUS after AUTO


# The station's clock.  SET DATE / SET TIME move it; GET TIME and the front-panel stream read
# it.  The SCM's SET handler (flash 0xBBD46 date, 0xBFA72 time) takes the date as YYYY/MM/DD
# ('/' at 4 and 7) and the time as HH:MM or HH:MM:SS (':' at 2, and at 5 with the seconds),
# answers ?(0040) / ?(0041) for a value that does not parse, and ?(0095) in RSS-only mode.
def _clock(s):
    base = s.get('clock')
    if base is None:
        return datetime.datetime.now()
    when, t0 = base
    return when + datetime.timedelta(seconds=time.monotonic() - t0)


def _set_clock(s, option, value):
    """Apply SET DATE / SET TIME.  None when the station takes it, else the error line."""
    if option == "DATE":
        digits = value[:4] + value[5:7] + value[8:]
        if len(value) != 10 or value[4] != "/" or value[7] != "/" or not digits.isdigit():
            return "?(0040) invalid date specification"
        try:
            d = datetime.date(int(value[:4]), int(value[5:7]), int(value[8:]))
        except ValueError:                      # 2026/02/30, 2026/13/01, ...
            return "?(0040) invalid date specification"
        cur = _clock(s)
        s['clock'] = (cur.replace(year=d.year, month=d.month, day=d.day), time.monotonic())
        return None
    digits = value[:2] + value[3:5] + value[6:]
    if len(value) not in (5, 8) or value[2] != ":" or (len(value) == 8 and value[5] != ":") \
            or not digits.isdigit():
        return "?(0041) invalid time specification"
    hh, mm = int(value[:2]), int(value[3:5])
    ss = int(value[6:]) if len(value) == 8 else 0
    if hh > 23 or mm > 59 or ss > 59:
        return "?(0041) invalid time specification"
    cur = _clock(s)
    s['clock'] = (cur.replace(hour=hh, minute=mm, second=ss), time.monotonic())
    return None


class SimStation:
    def __init__(self):
        self.out = b""
        self.prompt = "]-O "
        self.lock = threading.Lock()
        self.stream = None
        self.last = 0
        self.st = dict(keyed=False, pa="OFF", pend=110, refmod=64, sq=40, txdev=[3000, 2950, 3010, 2990],
                       txdev_stored=[3000, 2950, 3010, 2990], txdev_go=False, txdev_pending=None,
                       refmod_ctx=False, wl_tone=None, tdata=False,
                       acc="ACC_EN", std=False, chn=1, ps_equal="OFF", lto=0, wl={"1KHZ": "-10.0", "LB": "2.0",
                       "HB": "4.5", "HBF": "2500", "ST": "-10.0", "AT": "-20.0"}, txwl=["0", "0"],
                       txfreq=222340000, rxfreq=223940000, rx2freq=0, fpsq="PL", vol=0, intcom="OFF",
                       netting=False, pend_task=0, rssi=None,
                       # The exciter EEPROM's +3/+4 trims as the SCM mirrors them; the EEPOT
                       # engineering command prints this pair (see Link.eepot).
                       vco_eepot=0, refmod_eepot=64,
                       # Test and Measurement: the ASTRO test pattern / V.52 state and the
                       # receiver level the BER and RSSI reports read.  rx_dbm = None models a
                       # receiver with no signal, which answers ?(0096).
                       astro_tone="OFF", v52=False, rx_dbm=-113.0, rx_rel=10.0, ber=0.0,
                       rssi_silent=False,
                       # the network identity: GET ADDR IP / GET ADDR PHYS, and what
                       # SET ADDR IP / SET ADDR PHYS leave behind (used after a reset)
                       ip="10.101.3.10", phys="08003ecd1c64")
        # The SA partition (128 bytes; the MMI calls it AL).  The alignment values that live
        # there are modelled by self.st above; what matters here is the EEPROM conditioning
        # marker the application leaves at 0x7C: FADE then the EEPROM header's id byte, so the
        # simulator starts out conditioned, as the lab station is (RD AL 7C 7E -> FA DE 08).
        self.sa_img = bytearray(0x80)
        self.sa_img[0x7C:0x7F] = b"\xFA\xDE\x08"
        # The PP partition: None = no password set (the NVM record's bit 7 clear).  Set it to
        # e.g. "HAMRADIO" (vscm --password) to model a protected station.
        self.password = None
        # Pre-9.4 applications have no SET ASTRO_TONE (they answer ?(0017)); the only test
        # pattern they can transmit is V.52, through SET V52 ON.  Set False to model one.
        self.astro_tone_cmd = True
        self.pw_ok = False             # this session has been let in
        self.pw_tries = 0
        # The SCM's parallel ports, as GET PORT prints them.  P0_IN is the one that matters:
        # bits 16-19 are the "special mode" field the LOST/SERVICE password override is gated
        # on (see the special_mode property), and the rest is what the boards drive - the
        # default is what the lab station reads.  The others are fixed here.
        self.p0_in = 0x7F70FA2B
        self.p1_in = 0x00020C38
        self.p0_out = 0x7F1101F8
        self.p1_out = 0xF000C048
        # Multi-coded squelch usage accounting: 8 bytes per access code table at 0x08002E6E,
        # u32 calls then u32 seconds, both saturating at 249 (firmware 0x0D6366).  Built on
        # first use so it follows the codeplug's table count; CLEAR MCS zeroes it.
        self.mcs_img = None

    @property
    def special_mode(self):
        """The firmware's "special mode": P0_IN bits 16-19 == 3 (tested at 0x0CB254).  Only
        then do the words LOST and SERVICE work at the Password: prompt.  It is set from the
        station control front panel - holding Vol Up and Vol Down (the RSS manual's "Bypassing
        the Station Password") closes two of those lines - so setting it moves the port bits."""
        return ((self.p0_in >> 16) & 0xF) == 3

    @special_mode.setter
    def special_mode(self, on):
        self.p0_in = (self.p0_in & ~0x000F0000) | (0x00030000 if on else 0)

    # serial-port interface ----------------------------------------------------
    MMI_LINE_MAX = 126      # the station's command-line buffer; a longer line is truncated to this,
                            # so its WR CP record loses bytes and fails its S-record checksum.
                            # Measured on hardware (H026.11.05f): WR CP of 54 data bytes = 126-char
                            # line is the largest accepted; 55 = 128 chars is rejected ?(0021).

    def write(self, b):
        # Buffer bytes until a CR, like the station's line reader: a long command (e.g. a big
        # WR CP record) can arrive split across reads, so do not assume each write holds whole lines.
        self._inbuf = getattr(self, '_inbuf', '') + b.decode('latin1')
        while '\r' in self._inbuf:
            line, self._inbuf = self._inbuf.split('\r', 1)
            self._line(line[:self.MMI_LINE_MAX])        # a too-long line is truncated, as on the station
        return len(b)

    def read(self, n):
        with self.lock:
            if self.stream and time.time() - self.last > STREAM_GAP:
                self.last = time.time()
                self.out += self._stream_next().encode()
            r, self.out = self.out[:n], self.out[n:]
        if not r:
            time.sleep(0.02)
        return r

    def close(self):
        pass

    # -------------------------------------------------------------------------
    def _get_port(self, c):
        """GET PORT P0_IN|P1_IN|P0_OUT|P1_OUT -> "PORT <name> = 0x<hex>" (the real station prints
        it lowercase, %x).  P0_IN is the interesting one: its bits 16-19 are the special-mode
        field.  Returns the reply text, or None if this is not a GET PORT command."""
        if not c.startswith("GET PORT"):
            return None
        name = c[len("GET PORT"):].strip().upper()
        v = {"P0_IN": self.p0_in, "P1_IN": self.p1_in,
             "P0_OUT": self.p0_out, "P1_OUT": self.p1_out}.get(name)
        if v is None:
            return "?(0014) invalid command"
        return "PORT %s = 0x%x" % (name, v)

    def _say(self, text=""):
        p = self.prompt
        if self.password and not self.pw_ok and p == "]-O ":
            p = "Password: "          # a protected station will not show the main prompt yet
        self.out += ((text + "\r\n") if text else "").encode('latin1') + ("\r\n" + p).encode()

    # ------------------------------------------------------------------ the password
    # The session body of the OC application (flash 0x0E3656) reads the NVM record "PP" first
    # and, when its flag byte has bit 7 set, prints "Password: " and reads a line.  The right
    # password is accepted *silently* - the prompt only comes back on the next bare CR, which is
    # why WinRSS sends one - a wrong one answers "?(0076) password incorrect" and asks again,
    # and five in a row answer "?(0081) access denied! try again in a minute" (then a 60 s wait).
    # PP[0] bit 7 = protection enabled; PP[1..15] is the password, 15 encrypted bytes (the
    # firmware encrypts what is typed and compares that, so no plaintext is stored).
    # In special mode two words are an override instead: LOST erases the password and lets the
    # session in, SERVICE lets it in for this session only.
    def expire_password(self):
        """Model the station's own 10-minute password timer (RSS manual 8, "Password
        Time-out"): the session drops back to the main prompt, which a protected station shows
        as "Password: ", and any real-time stream stops.  The password has to be given again."""
        with self.lock:
            self.pw_ok = False
            self.stream = None
            self.prompt = "]-O "
            self.out += b"\r\nPassword: "

    def _password(self, c):
        if c == "":
            return self._say()
        if c in ("LOST", "SERVICE") and self.special_mode:
            if c == "LOST":
                self.password = None            # the station erases the record's enable bit
            self.pw_ok, self.pw_tries = True, 0
            return                              # accepted silently, like the password
        if c == self.password:
            self.pw_ok, self.pw_tries = True, 0
            return                     # nothing printed at all: the next CR gets the prompt
        self.pw_tries += 1
        if self.pw_tries >= 5:
            self.pw_tries = 0
            return self._say("?(0081) access denied! try again in a minute")
        return self._say("?(0076) password incorrect")

    def _setpass(self, args):
        """SETPASS <current> <new> <confirm> (WinRSS id 21951).  The firmware (0x0D8A72) takes
        the new value from whichever argument pair matches and also accepts <new> <current>
        <current>; both are refused here only when they do not match, as it does.  The Quantar
        wants all three arguments (0x0B45D2 answers ?(0014) without them) and spells a blank
        one NONE; the shorter forms are tolerated here because the comparator's handler is."""
        cur = (self.password or "").upper()
        # The firmware encrypts the characters it is given, so only letters and digits get
        # past it (OC.img 0x085BE3A5, "?(0078) invalid character in password").
        if any(not re.fullmatch(r"[A-Za-z0-9]*", x) for x in args):
            return self._say("?(0078) invalid character in password")
        # A blank argument cannot be sent (the MMI collapses spaces), so it is spelled NONE.
        # The argument count then says which form it is: one = <current> and erase, two =
        # <new> <confirm> with no current password, three = <current> <new> <confirm>.
        a = [("" if x.upper() == "NONE" else x.upper()) for x in args]
        if len(a) == 1:
            a = [a[0], "", ""]
        elif len(a) == 2:
            a = ["", a[0], a[1]]
        a = a[:3] + ["", "", ""]
        a = a[:3]
        if a[0] == cur and a[1] == a[2]:
            new = a[2]
        elif a[1] == cur and a[2] == cur:
            new = a[0]
        else:
            return self._say("?(0080) password not matched--password unchanged")
        if new == "SERVICE":
            return self._say("?(0079) special mode not allowed at this time")
        if new and not (5 <= len(new) <= 8):
            return self._say("?(0077) invalid password length")
        self.password = new or None
        self.pw_ok, self.pw_tries = True, 0     # this session stays open, as it does on the station
        return self._say()

    # RD MCS, modelled on the station: the address is 0x08002E6E + the offset, and the
    # application keeps the codeplug in RAM with its pointers relocated (base + stored offset,
    # directory's 0x80000000 "list of lists" bit dropped).  qrss reads it there (Link.read_codeplug)
    # because RD CP re-reads the NVM device first, ~0.4 s on every command.
    MCS_BASE = 0x08002E6E
    CP_RAM_VAR = 0x0806AE90      # -> WC block; the codeplug starts 0x100 bytes later
    CP_RAM_BASE = 0x0838F4F8
    WC_AREA = 0x100              # the station's fixed WC block area; the codeplug follows it

    def _wc_base(self):
        return self.CP_RAM_BASE - self.WC_AREA

    def _cp_base(self):
        return self.CP_RAM_BASE

    def _ram_image(self):
        cp = getattr(self, 'cp_img', None)
        if cp is None:
            return None
        img = bytearray(cp)
        base, ndir = self._cp_base(), int.from_bytes(cp[8:10], 'big')
        lists = []
        for k in range(ndir):                       # the section directory, flag bit dropped
            at = 10 + 4 * k
            v = int.from_bytes(cp[at:at + 4], 'big')
            if v & 0x80000000:
                lists.append(v & 0x7FFFFFFF)
            img[at:at + 4] = (base + (v & 0x7FFFFFFF)).to_bytes(4, 'big')
        for t in lists:                             # a list of lists: its entries point to sections
            for j in range(int.from_bytes(cp[t:t + 2], 'big')):
                at = t + 2 + 4 * j
                img[at:at + 4] = (base + int.from_bytes(cp[at:at + 4], 'big')).to_bytes(4, 'big')
        if len(img) > 0x107:
            img[0x107] = 0x03                        # runtime state (0x00 stored)
        img[0:2] = (sum(img[2:]) & 0xFFFF).to_bytes(2, 'big')
        return img

    def _ram(self, lo, hi):
        """RAM[MCS_BASE+lo : MCS_BASE+hi]; DEAD fill where the station has nothing modelled."""
        img = self._ram_image()
        if img is None:
            return None
        out = bytearray(b"\xde\xad" * ((hi - lo) // 2 + 1))[:hi - lo]
        mcs = self._mcs_bytes()                    # the usage accounting sits at offset 0
        a, b = max(lo, 0), min(hi, len(mcs))
        if a < b:
            out[a - lo:b - lo] = mcs[a:b]
        var = self.CP_RAM_VAR - self.MCS_BASE
        if lo <= var and var + 4 <= hi:            # -> the WC block; the codeplug is 0x100 later
            out[var - lo:var - lo + 4] = self._wc_base().to_bytes(4, 'big')
        wc = getattr(self, 'wc_img', None)          # the WC block sits at the var, byte for byte
        if wc:
            w = self._wc_base() - self.MCS_BASE
            a, b = max(lo, w), min(hi, w + len(wc))
            if a < b:
                out[a - lo:b - lo] = wc[a - w:b - w]
        start = self._cp_base() - self.MCS_BASE
        a, b = max(lo, start), min(hi, start + len(img))
        if a < b:
            out[a - lo:b - lo] = img[a - start:b - start]
        return bytes(out)

    def _mcs_size(self):
        """GET MCS SIZE: 8 bytes per access code table, and only when the codeplug is in
        MULTI-PL ONLY mode.  The firmware (0x0A4D24) also wants application version 0x23+ and
        answers 0 otherwise."""
        cp = getattr(self, 'cp_img', None)
        if cp is None:
            return 0
        try:
            c = Codeplug(cp)
            return 8 * c.naccess() if c.img[c.si + 0x50] & 0x20 else 0
        except Exception:
            return 0

    def _mcs_bytes(self):
        """The accounting block, built once: entry i gets a plausible count and duration."""
        n = self._mcs_size()
        if self.mcs_img is None or len(self.mcs_img) != n:
            self.mcs_img = bytearray(n)
            for i in range(n // 8):
                struct.pack_into('>II', self.mcs_img, 8 * i, (7 * i + 3) % 250, (37 * i + 11) % 250)
        return self.mcs_img

    # The codeplug and the wildcard share one set of handlers in the application (NEW 0x9BBFC,
    # WR 0xCDFD2 -> S-record parser 0x9CBFC, MOVE 0xABA72, STOP 0xC27E8), so they behave alike;
    # only the "area already/not allocated" codes differ.
    AREAS = {"CP": ("cp_img", "_cp_stage", "_cp_alloc",
                    "?(0002) codeplug area already allocated", "?(0026) codeplug area has not been allocated"),
             "WC": ("wc_img", "_wc_stage", "_wc_alloc",
                    "?(0084) wildcard area already allocated", "?(0085) wildcard area has not been allocated"),
             # AL is the SA partition (handler 0xABA72 writes it to "SA" through 0x0E2E80)
             "AL": ("sa_img", "_al_stage", "_al_alloc",
                    "?(0001) alignment area already allocated", "?(0025) alignment area has not been allocated")}

    def _area_write(self, c, a):
        """NEW/WR/MOVE/STOP <CP|WC>; the reply text, or None when the line is not one of them."""
        area = a[1] if len(a) > 1 else ""
        if area not in self.AREAS or a[0] not in ("NEW", "WR", "MOVE", "STOP"):
            return None
        img_attr, stage_attr, alloc_attr, already, not_alloc = self.AREAS[area]
        stage = getattr(self, stage_attr, None)
        alloc = getattr(self, alloc_attr, False)
        if a[0] == "NEW" and len(a) == 3:
            # the area stays allocated after MOVE until STOP, so a second NEW without a STOP
            # in between is refused
            if alloc:
                return already
            try:
                setattr(self, stage_attr, bytearray(int(a[2])))
            except ValueError:
                return ERR_CMD
            setattr(self, alloc_attr, True)
            return ""
        if a[0] == "WR" and len(a) == 3:
            if not alloc:
                return not_alloc
            rec = a[2]
            if not rec.startswith("S"):
                return "?(0020) invalid S-record"
            if len(rec) < 4 or rec[1] not in "28":
                return "?(0033) unsupported S-record type"
            try:
                raw = bytes.fromhex(rec[2:])
            except ValueError:
                return "?(0020) invalid S-record"
            if len(raw) != raw[0] + 1 or (~sum(raw[:-1])) & 0xFF != raw[-1]:
                return "?(0021) invalid S-record checksum"
            if rec[1] == "2":
                at = int.from_bytes(raw[1:4], "big")
                stage[at:at + len(raw) - 5] = raw[4:-1]
            return ""
        if a[0] == "MOVE" and len(a) == 2:
            if not alloc:
                return not_alloc
            setattr(self, img_attr, bytes(stage))      # commit; the area stays allocated
            return ""
        if a[0] == "STOP" and len(a) == 2:
            setattr(self, stage_attr, None)
            setattr(self, alloc_attr, False)
            return ""
        return None

    def _srecs(self, img, a):
        """The station's RD formatter (0x9CA26): at most 214 bytes, 32-byte S2 records, an S8
        record after them when there are several; the end is not checked against the size."""
        lo, hi = int(a[2], 16), int(a[3], 16)
        if hi < lo:
            return ["?(0027) arguments are out of order"]
        if hi - lo + 1 > 214:
            return ["?(0030) must request 214 bytes or fewer"]
        recs = []
        for at in range(lo, hi + 1, 32):
            data = bytes(img[at:min(at + 32, hi + 1)])
            rec = bytes([len(data) + 4]) + at.to_bytes(3, 'big') + data
            recs.append("S2" + (rec + bytes([~sum(rec) & 0xFF])).hex().upper())
        if len(recs) > 1:
            recs.append("S804000000FB")
        return recs

    def _vals(self):
        s = self.st
        pf = (100.0 + random.uniform(-1, 1)) if s['keyed'] else 0.0
        return dict(fwd=pf / 40 + 0.118, pf=pf, pr=pf * 0.01, vswr=1.2 if pf else 1.0, ia=pf / 18, ib=pf / 18.5,
                    pa=s['pa'], sl1=2.27 + random.choice([0, 0, 0.01]), rx5=random.choice([4.90, 4.94]),
                    hs=31 + random.choice([0, 0, 0.25]), chn=s['chn'], rxact=random.choice(["ON", "OFF", "OFF"]),
                    accdis="ON" if s['acc'] == "ACC_DIS" else "OFF", time=_clock(s).strftime("%Y/%m/%d.%H:%M:%S"),
                    sq_mode=s['fpsq'], vol=s['vol'], intcom=s['intcom'], rxfreq=s['rxfreq'], txfreq=s['txfreq'])

    def _stream_next(self):
        """Next piece of a real-time stream: every key once, then only the values that change,
        each as "\r\nKEY = VALUE\r\nRAP: " like the real station."""
        keys = MTR.get(self.stream)
        if not isinstance(keys, list) or not keys:
            return ""
        v = self._vals()
        for _ in range(len(keys)):
            k, f = keys[self.spos % len(keys)]
            self.spos += 1
            val = f.format(**v)
            if self.sent.get(k) != val:
                self.sent[k] = val
                return "\r\n%s = %s\r\nRAP: " % (k, val)
        return ""

    def _line(self, line):
        with self.lock:
            c = " ".join(line.split()).upper()
            s = self.st
            if self.stream:
                if c == "STOP":
                    self.stream = None
                    self.out += self.prompt.encode()
                elif c:
                    self._rap(c)
                return
            if self.password and not self.pw_ok and self.prompt == "]-O ":
                return self._password(c)     # nothing else is taken until it is answered
            if c == "":
                return self._say()
            if c == "EXIT":
                self.prompt = "]-O "
                return self._say()
            if c.split()[:1] == ["PCTL"]:   # a main-prompt command: ?(0014) in RSS/RAP, as on the station
                return self._pctl(c.split()[1:]) if self.prompt == "]-O " else self._say(ERR_CMD)
            if self.prompt == "]-O ":
                if c.split()[:1] == ["SETPASS"]:
                    return self._setpass(c.split()[1:])
                for k, p in (("DORSS", "RSS: "), ("DORAP", "RAP: "), ("DOCSS", "CSS: ")):
                    if c == k:
                        if k == "DORAP" and s['std']:
                            break        # a disabled station refuses the front panel monitor
                        self.prompt = p
                        return self._say()
                return self._say("?(1)")
            if c == "RESET" and self.prompt != "]-O ":
                # WinRSS's " RESET" (21352): the application restarts - the session drops back
                # to ]-O, the PA is unkeyed and anything held only in RAM is gone.  (RESTART,
                # 21024, is the network restart and is ?(0014) here, as on the station.)
                self.prompt = "]-O "
                self.pw_ok, self.pw_tries = False, 0      # a new session: the password again
                self.st['keyed'], self.st['pa'] = False, "OFF"
                self.st['astro_tone'], self.st['v52'] = "OFF", False
                self.stream = None
                self._cp_stage = self._wc_stage = None
                self._cp_alloc = self._wc_alloc = False
                self._al_stage = None
                self._al_alloc = False
                sa = bytearray(self.sa_img)
                if bytes(sa[0x7C:0x7E]) != b"\xFA\xDE":
                    # CHECK_NVM_CONDITIONING: no FADE marker, so the application conditions the
                    # EEPROM at boot and writes the marker
                    sa[0x7C:0x7E] = b"\xFA\xDE"
                self.sa_img = sa
                return self._say()
            if self.prompt == "RAP: ":
                if c.startswith("MTR ") or c == "FPM":
                    if c not in MTR:
                        return self._say(ERR_OPT)
                    self.out += b"\r\nRAP: "
                    if isinstance(MTR[c], str):
                        self.out += ("\r\n%s\r\nRAP: " % MTR[c]).encode()
                    self.stream, self.sent, self.spos, self.last = c, {}, 0, time.time()
                    return
                return self._rap(c)
            return self._rss(c)

    def _rap(self, c):
        s = self.st
        if c == "RC SET PTT XMIT":
            s['keyed'], s['pa'] = True, "FULL"
        elif c == "RC SET PTT OFF":
            s['keyed'], s['pa'] = False, "OFF"
        elif c.startswith("FP ACC_DIS"):
            s['acc'] = "ACC_DIS" if c.endswith("ON") else "ACC_EN"
        elif c.startswith("FP CHN"):
            s['chn'] = int(c.split()[-1])
        elif c.startswith("FP SQ "):
            s['fpsq'] = c.split()[-1]
        elif c.startswith("FP SPKRVOL "):
            s['vol'] = int(c.split()[-1])
        elif c.startswith("FP INTCOM "):
            s['intcom'] = c.split()[-1]
        if not self.stream:
            self._say()

    PCTL_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
    pctl_baud = False          # True once a PCTL -BAUD application (H026.11.05d+) is in flash
    pctl_baud_inert = False    # H026.11.05d/e: take -BAUD but switch another port (BRG2 = the rear
                               # RSS port, BRG1 = the SCC4 modem port), not the front panel we are on
    mmi_baud = 9600

    def _pctl(self, args):
        """PCTL like the station: no arguments = port report (it shows the driver's rate code, so
        it says 9600 even after -BAUD); options -ECHO ON|OFF, and -BAUD <rate> on patched firmware."""
        if not args:
            return self._say("Current port settings:\r\n   Baud rate is 9600\r\n"
                             "   Input flow control is off\r\n   Output flow control is off")
        out = []
        while args:
            opt = args.pop(0)
            if opt == "-ECHO" or (opt == "-BAUD" and self.pctl_baud):
                if not args:
                    out.append("?(0024) missing argument")
                    continue
                val = args.pop(0)
                if opt == "-ECHO" and val not in ("ON", "OFF"):
                    out.append("?(0012) invalid argument")
                elif opt == "-BAUD":
                    if val.isdigit() and int(val) in self.PCTL_RATES:
                        if not self.pctl_baud_inert:
                            self.mmi_baud = int(val)
                    else:
                        out.append("?(0012) invalid argument")
            else:
                out.append(ERR_OPT)
        return self._say("\r\n".join(out)) if out else self._say()

    def _status(self, arg):
        """`GET STATION_STATUS n|ALL` as the firmware builds it (table at 0x0806680A, formatter
        0x0C24B0): entries 1/2 are the RTC, 3 the component state word, 4 byte 3 bits 7-6 the
        access state (0b10 enabled 0xBC / 0b11 disabled 0xFC) and byte 2 bit 4 the front-panel
        ACC_DIS key (0x6f / 0x7f - it does not change the access state), 6 the current channel,
        7/8 their build defaults (0x1FFFFFFF).  ALL prints 1-5, a checksum and a newline, then
        6-8 and a checksum covering only 6+7+8."""
        s = self.st
        t = time.localtime()
        st4 = 0x03516FBC
        if s['acc'] == "ACC_DIS":       # FP ACC_DIS ON: the key bit, access stays enabled
            st4 |= 0x00001000
        if s['std']:                     # SET STATION_DISABLE ON: access disabled
            st4 |= 0x00000040
        v = {1: (t.tm_year & 0xFFF) | (t.tm_mon << 12) | (t.tm_mday << 17),
             2: (t.tm_hour << 12) | (t.tm_min << 6) | t.tm_sec,
             3: 0xAAB3B3EA,
             4: st4,
             5: 0,
             6: ((s['chn'] & 0xFF) << 24) | (16 << 16),
             7: 0x1FFFFFFF, 8: 0x1FFFFFFF}
        word = lambda i: "%04x%08x" % (i, v[i])

        def group(ids):                 # byte sum over every id/value byte in the group
            n = sum(((i >> 8) + (i & 0xFF) + sum((v[i] >> (8 * k)) & 0xFF for k in range(4)))
                    for i in ids)
            return (~n) & 0xFF

        if arg.upper() == "ALL":
            head = "".join(word(i) for i in range(1, 6)) + "%02x\n" % group(range(1, 6))
            return "STATUS=" + head + "".join(word(i) for i in (6, 7)) + \
                "%04x%08x%02x" % (8, v[8], group((6, 7, 8)))
        if not arg.isdigit() or not 1 <= int(arg) <= 8:
            return None
        n = int(arg)
        return "STATUS=" + word(n) + "%02x" % status_checksum(n, v[n])

    def _rss(self, c):
        s = self.st
        a = c.split()
        if c in ("SET STATION_DISABLE ON", "SET STATION_DISABLE OFF"):
            # WinRSS's StationEnableDisable (22252/22251).  The station answers at RSS: and then
            # drops the session to ]-O a moment later (see the firmware notes), so a client has to
            # DORSS again before its next command.
            s['std'] = c.endswith(" ON")
            self._say("ROOT:: Station forced into <<STATION_DISABLE_MODE>> from serial interface."
                      if s['std'] else "")
            self.prompt = "]-O "
            self.out += b"\r\n]-O "
            return
        # Test and Measurement (WinRSS 21252/21253, 21850-21855).  The BER reply repeats the
        # pattern: "\nBER V52 = %d %1.1f %1.1f %1.3f" (status, absolute RSSI dBm, relative dB,
        # BER fraction); RSSI is "\nRSSI = %d %1.1f %1.1f".  With no signal the station answers
        # ?(0096) BER Rx signal not detected.
        if a[:3] == ["GET", "DSP", "BER"] and len(a) == 8:
            pat = a[3]
            if pat not in ("V52", "APCO"):
                return self._say(ERR_OPT)
            if s['rx_dbm'] is None:
                return self._say("?(0096) BER Rx signal not detected")
            return self._say("BER %s = 0 %.1f %.1f %.3f" % (pat, s['rx_dbm'], s['rx_rel'], s['ber']))
        if a[:3] == ["GET", "DSP", "RSSI"] and len(a) == 6:
            if s['rssi_silent']:
                # The station answers with an empty line when the DSP has no reading at all -
                # seen on the lab station with its reference oscillator down (Link.call
                # raises it as an "EMPTY" StationError).
                return self._say("")
            if s['rx_dbm'] is None:
                return self._say("?(0096) BER Rx signal not detected")
            return self._say("RSSI = 0 %.1f %.1f" % (s['rx_dbm'], s['rx_rel']))
        if c == "GET ASTRO_TONE":
            if not self.astro_tone_cmd:
                return self._say(ERR_OPT)
            return self._say("ASTRO_TONE = %s" % s['astro_tone'])
        if c == "GET V52":
            return self._say("V52 = %s" % ("ON" if s['v52'] else "OFF"))
        # Multi-coded squelch usage accounting (WinRSS' DMCS screen).  GET MCS SIZE is the
        # byte count of the block, 8 per access code table, 0 when the station is not in
        # MULTI-PL ONLY mode; CLEAR MCS is Clear Accounting and answers nothing.
        if c == "GET MCS SIZE":
            return self._say("MCS SIZE = %d" % self._mcs_size())
        if c == "CLEAR MCS":
            m = self._mcs_bytes()
            m[:] = bytes(len(m))
            return self._say()
        simple = {
            "GET HW_VER": "HW_VER = INTELLIREPEATER", "GET BPN": "BPN = 000000SIM001",
            "GET STN NAME": "STN NAME = SIMULATOR", "GET STN SN": "STN SN = 999SIM0001",
            "GET TIME": "TIME = " + _clock(s).strftime("%Y/%m/%d.%H:%M:%S"),
            "GET FW_VER SC": "FW_VER SC = R020.14.048", "GET FW_VER EX": "FW_VER EX = R020.09.018",
            "GET FW_VER WL": "FW_VER WL = R020.14.003",
            "GET MCS SIZE": "MCS SIZE = 0", "GET AL SIZE": "AL SIZE = 128",
            "GET FP VER": ERR_OPT, "GET CHN MAX": "CHN MAX = 16", "GET STN SYS": "STN SYS = CONV",
            "GET FW_VER BOOT.O": "FW_VER = R020.10.001 TIMESTAMP = 0000/00/00 00:00",
            "GET FW_VER BOOT2.O": "FW_VER = R020.14.015 TIMESTAMP = 2015/09/25 23:07",
            "GET FW_VER SC.O": "FW_VER = R020.14.048 TIMESTAMP = 2009/06/12 10:00",
            "GET FW_VER WL.O": "FW_VER = R020.14.003 TIMESTAMP = 2009/06/12 10:00",
            "GET TX FREQ": "TX_FREQ = %d" % s['txfreq'], "GET RX FREQ": "RX_FREQ = %d" % s['rxfreq'],
            "GET RX2 FREQ": "RX2_FREQ = %d" % s['rx2freq'], "GET CHN CUR": "CHN CUR = %d" % s['chn'],
            "GET PA ON": "PA ON = %s" % ("ACTIVE" if s['keyed'] else "INACTIVE"),
            "GET PA TEST": "PA TEST = OFF", "GET PS EQUAL": "PS EQUAL = %s" % s['ps_equal'],
            "GET PS BATT_TYPE": "PS_BATT_TYPE = NONE", "GET WL SQL": "WL SQL = NORMAL",
            "GET RX FREQ_BAND": "RX_FREQ_BAND = VHF_R4", "GET RX2 FREQ_BAND": "RX2_FREQ_BAND = NONE",
            "GET TX FREQ_BAND": "TX_FREQ_BAND = VHF_R4", "GET RX TYPE": "RX_TYPE = STANDARD",
            "GET PA TYPE": "PA_TYPE = QUANTAR", "GET PA ORD_PWR": "PA_ORD_PWR = 110", "GET TX COMPA": "TX_COMPA = TRUE",
            "GET WL PORT_STAT": "WL_PORT_STAT = 4-WIRE", "GET PS RATED_PWR": "PS_RATED_PWR = AC_LOW_PWR",
            "GET REF_TYPE": "REF_TYPE = INT_OR_EXT",
            # firmware-only (not in WinRSS' table).  The simulator is an IR (simulcast) station -
            # GET HW_VER says INTELLIREPEATER - so it has a GPS status; a conventional codeplug
            # answers "SIMULCAST IS DISABLED" and its AL PEND AUTO can never lock.
            "GET GPS STAT": "GPS STATUS = 0000",
            "GET ADDR IP": "IP ADDR = %s" % s['ip'],
            "GET ADDR PHYS": "PHYS ADDR = %s" % s['phys'],
            "ALARM_EXT RD": ERR_CMD, "ALARM_EXT CLEAR": ERR_CMD,
            "AL PEND RD": "PEND = %d" % s['pend'], "AL PEND NOREF": "TXFREQ = %d" % s['txfreq'],
            "AL PEND STATUS": "PEND ABORTED",       # idle; AUTO starts a net, see below
            "AL SQ RD": "SQ = %d" % s['sq'], "AL TXDEV RD": " TXDEV = %d %d %d %d" % tuple(s['txdev']),
            "AL LTO RD": "LTO = %d" % s['lto'],
            "AL TXWL RD_REF C1 5": "C1 = %s" % s['txwl'][0], "AL TXWL RD_REF C2 5": "C2 = %s" % s['txwl'][1],
        }
        cp = getattr(self, 'cp_img', None)
        if cp is not None and c == "GET CP SIZE":
            return self._say("CP SIZE = %d" % len(cp))
        if c == "GET WC SIZE":
            wc = getattr(self, 'wc_img', None)
            if wc is None:
                return self._say(ERR_INTERNAL)
            return self._say("WC SIZE = %d" % len(wc))
        if cp is not None and c == "GET CP VER":
            return self._say("CP VER = 00.%02X" % int.from_bytes(cp[6:8], "big"))
        if cp is not None and c.startswith("RD CP ") and len(a) == 4:
            return self._say("\r\n".join(self._srecs(cp, a)))
        if c.startswith("RD WC ") and len(a) == 4:
            wc = getattr(self, 'wc_img', None)
            if wc is None:
                return self._say(ERR_INTERNAL)
            return self._say("\r\n".join(self._srecs(wc, a)))
        if c.startswith("RD AL ") and len(a) == 4:
            # The SA partition; RD's handler (0xB20EC) takes AL, MCS, WC, CP and PP.  The
            # conditioning marker is in it at 0x7C (see Link.conditioning_state).
            return self._say("\r\n".join(self._srecs(self.sa_img, a)))
        if cp is not None and c.startswith("RD MCS ") and len(a) == 4:
            # RAM read.  The station's RD handler adds 0x08002E6E to the offset, so the S2 record
            # address is the offset itself.  It can reach the codeplug's RAM copy, which is how
            # qrss reads the codeplug quickly (see Link.read_codeplug).
            lo, hi = int(a[2], 16), int(a[3], 16)
            if hi < lo:
                return self._say("?(0027) arguments are out of order")
            if hi - lo + 1 > 214:
                return self._say("?(0030) must request 214 bytes or fewer")
            d = self._ram(lo, hi + 1)
            if d is None:
                return self._say(ERR_INTERNAL)
            recs = []
            for at in range(0, len(d), 32):
                data = d[at:at + 32]
                rec = bytes([len(data) + 4]) + (lo + at).to_bytes(3, 'big') + data
                recs.append("S2" + (rec + bytes([~sum(rec) & 0xFF])).hex().upper())
            if len(recs) > 1:
                recs.append("S804000000FB")
            return self._say("\r\n".join(recs))
        if a[0] == "ERASE" and len(a) > 1 and a[1] == "EEPROM":
            # ERASE EEPROM (handler 0xABE3A): the rest of the line must be "YES YES YES YES YES";
            # then the handler fills 8192 bytes with FF and writes the lot (codeplug, wildcard and
            # alignment gone) and pokes the watchdog ("WDTI").  Any other argument erases nothing.
            rest = " ".join(a[2:])
            if rest != "YES YES YES YES YES":
                return self._say('?(0018) option "%s" is invalid' % rest)
            self.sa_img = bytearray(b"\xFF" * 0x80)
            for attr in ("cp_img", "wc_img"):
                img = getattr(self, attr, None)
                if img is not None:
                    setattr(self, attr, b"\xFF" * len(img))
            return self._say("\r\n...my mind is going...")
        # Codeplug write, modelled on the station (handlers NEW 0x9BBFC, WR 0xCDFD2 ->
        # S-record parser 0x9CBFC, MOVE 0xABA72, STOP 0xC27E8): NEW CP allocates a RAM staging
        # area, WR CP writes one S2 record into it, MOVE CP commits it to cp_img, STOP CP discards it.
        w = self._area_write(c, a)
        if w is not None:
            return self._say(w)
        if c == "AL PEND STATUS":
            # The real station's status states are UNKNOWN / UNABLE_TO_LOCK / LOCKED / UNLOCKED /
            # ABORTED, and the netting task does not answer the first query after AL PEND AUTO
            # (? (0010), ~1 s) - see Quantar_Firmware_Analysis.md, "Alignment write sequences".
            if s['pend_task']:
                s['pend_task'] -= 1
                return self._say(ERR_TASK)
            return self._say("PEND LOCKED" if s['netting'] else "PEND ABORTED")
        if c.startswith("AL PEND AUTO"):
            s['netting'], s['pend_task'] = True, 1
            return self._say()
        if c.startswith("AL PEND ABORT"):
            s['netting'], s['pend_task'] = False, 0
            return self._say()
        if c.startswith("GET STATION_STATUS") and len(a) == 3:
            r = self._status(a[2])
            return self._say(r if r is not None else "?(0028) one or more arguments is out of range")
        if c == "GET STATE PASS":
            return self._say("STATE PASS = %s" % ("ENABLED" if self.password else "DISABLED"))
        r = self._get_port(c)
        if r is not None:
            return self._say(r)
        if c in simple:
            return self._say(simple[c])
        if c == "GET EEPOT":
            # The service port's engineering commands (handler 0xA35FC; WinRSS' table has no
            # entry).  They sit in the GET namespace - `GET HW_VER` and these share the
            # handler, and a bare `EEPOT` is ?(0014) on the station.  EEPOT prints the SCM's
            # copy of the exciter EEPROM +3/+4 trims - the string is the firmware's own at
            # 0x12963C - and FREQ the three frequencies (0x12967A).  Both are read-only.
            return self._say("VCO EEPOT = %d,  Ref Mod EEPOT = %d, (range 0 to 255)"
                             % (s['vco_eepot'], s['refmod_eepot']))
        if c == "GET FREQ":
            return self._say("RX_FREQ = %d\nRX2_FREQ = %d\nTX_FREQ = %d"
                             % (s['rxfreq'], s['rx2freq'], s['txfreq']))
        if c == "ALARM RD":
            return self._say("ALARM(001) = 1900/01/01.00:00:00 1900/01/01.00:00:00 1 SC ROOT 689 SYS_RESET 1\r\n"
                             "ALARM(002) = 1900/01/01.00:00:04 1900/01/01.00:00:04 1 SC AT_100MS_RESOLUTION_SC 1719 "
                             "REF_SYNTH REF_SYNTH_UNLOCKED\r\n"
                             "ALARM(003) = 1900/01/01.00:08:05 1900/01/01.00:08:05 1 SC AT_100MS_RESOLUTION_SC 1725 "
                             "REF_SYNTH REF_SYNTH_LOCKED")
        if c.startswith("AL RXWL") and " RD " in c:
            k = a[-1]
            return self._say("%s = %s" % (k, s['wl'].get(k, "0")))
        if c.startswith("AL RXWL") and " WR " in c:
            s['wl'][a[4]] = a[5]
            return self._say()
        if c == "AL REFMOD RD":
            # RD is ?(0009) until an F5/F6 starts a ref-mod session; SAVE ends it.
            if not s['refmod_ctx']:
                return self._say(ERR_F5)
            return self._say("REFMOD = %d" % s['refmod'])
        if c.startswith(("AL REFMOD F5", "AL REFMOD F6")):
            s['refmod_ctx'], s['keyed'] = True, True
            return self._say("%s = %d" % ("VCO1FREQ" if c.endswith("F5") else "VCO2FREQ", s['txfreq']))
        if c.startswith("AL TXDEV GO F"):
            n = int(c[-1])
            s['keyed'] = True
            s['txdev_go'] = True
            s['txdev'] = list(s['txdev_stored'])      # GO reloads the RAM copy RD reports
            return self._say("TX%dFREQ = %d" % (n, s['txfreq'] - 1000000 + n * 500000))
        if c.startswith("AL TXDEV WR"):
            if not s['txdev_go']:
                return self._say(ERR_GO)
            s['txdev_pending'] = [int(x) for x in a[3:7]]   # SAVE commits; RD keeps the GO copy
        elif c == "AL TXDEV SAVE":
            if not s['txdev_go']:
                return self._say(ERR_GO)
            if s['txdev_pending'] is not None:
                s['txdev_stored'] = s['txdev_pending']
        elif c == "AL REFMOD SAVE":
            s['refmod_ctx'] = False
        elif c in ("KEYUP", "AL TXWL KEYUP 10"):
            s['keyed'] = True
        elif c in ("DEKEY", "AL TXWL DEKEY"):
            s['keyed'] = False
        elif c.startswith("AL PEND UP"):
            s['pend'] = max(0, s['pend'] - int(a[-1]))
        elif c.startswith("AL PEND DN"):
            s['pend'] = min(215, s['pend'] + int(a[-1]))
        elif c.startswith("AL RSSI"):
            # the alignment screen sends the level's magnitude (a -90 dBm reading goes out as 90)
            v = int(a[-1])
            if not 50 <= v <= 105:
                return self._say(ERR_CAL)
            s['rssi'] = v
        elif c.startswith("AL REFMOD UP"):
            s['refmod'] = min(127, s['refmod'] + int(a[-1]))
        elif c.startswith("AL REFMOD DN"):
            s['refmod'] = max(0, s['refmod'] - int(a[-1]))
        elif c.startswith("AL SQ WR"):
            v = int(a[-1])
            if not 0 <= v <= 127:
                return self._say(ERR_RANGE)
            s['sq'] = v
        elif c.startswith("AL SQ UP"):
            s['sq'] = min(127, s['sq'] + int(a[-1]))
        elif c.startswith("AL SQ DN"):
            s['sq'] = max(0, s['sq'] - int(a[-1]))
        elif c.startswith("SET TEST_TONE"):
            s['wl_tone'] = None if c == "SET TEST_TONE OFF" else c
        elif c.startswith("SET ASTRO_TONE"):
            # A test pattern keys the transmitter.  STD_DEV/LOW_DEV/V52/C4FM are the four the
            # Test and Measurement screen offers; a pre-9.4 station rejects the command and the
            # caller falls back to SET V52 (astro_tone_cmd = False models one).
            if not self.astro_tone_cmd:
                return self._say(ERR_OPT)
            if c == "SET ASTRO_TONE OFF":
                s['astro_tone'] = "OFF"
            elif a[-1] in ("STD_DEV", "LOW_DEV", "V52", "C4FM"):
                s['astro_tone'], s['keyed'] = a[-1], True
            else:
                return self._say(ERR_OPT)
            if s['astro_tone'] == "OFF" and not s['v52']:
                s['keyed'] = False
        elif c == "SET V52 ON":
            s['v52'], s['keyed'] = True, True
        elif c == "SET V52 OFF":
            s['v52'] = False
            if s['astro_tone'] == "OFF":
                s['keyed'] = False
        elif c.startswith("SET TDATA"):
            s['tdata'] = c.endswith(" ON")
        elif c.startswith("SET PS EQUAL"):
            s['ps_equal'] = a[-1]
        elif c.startswith("SET CHN CUR"):
            s['chn'] = int(a[-1])
        elif c.startswith("AL LTO WR"):
            s['lto'] = int(a[-1])
        elif c.startswith("AL TXWL WR_REF"):
            s['txwl'][0 if "C1" in c else 1] = a[-1]
        elif c.startswith("AL TXWL GO"):
            # GO calibrates against the 1 kHz test tone on the line
            if s['wl_tone'] is None:
                return self._say(ERR_LOW)
            return self._say("TXWL DONE")
        elif c == "AL TDATA GO":
            if not s['tdata']:
                return self._say(ERR_CAL)
            return self._say()
        elif c == "RESET":
            self.prompt = "]-O "
            s['keyed'] = False
            s['acc'] = "ACC_EN"
            s['refmod_ctx'] = s['txdev_go'] = False
            s['netting'] = False
            s['pend_task'] = 0
            s['astro_tone'], s['v52'] = "OFF", False
            s['txdev'] = list(s['txdev_stored'])
        elif c.startswith(("SET DATE ", "SET TIME ")):
            # the clock (see _set_clock).  The handler's other branch is "SET DATE SITE <x>",
            # which parses its own value; nothing qrss sends uses it.
            if a[2:3] != ["SITE"]:
                err = _set_clock(s, a[1], a[2] if len(a) > 2 else "")
                if err:
                    return self._say(err)
        elif c.startswith(("SET ADDR IP ", "SET ADDR PHYS ")):
            # Tools > Set IP and MAC Address.  The station takes the value and starts using it
            # after a reset; the simulator just keeps it for GET ADDR.
            s["ip" if a[2] == "IP" else "phys"] = a[3]
        elif c.startswith(("SET ", "AL ", "ALARM")):
            pass
        elif c.startswith("GET "):
            return self._say(ERR_OPT)
        else:
            return self._say(ERR_CMD)
        self._say()


class ComparatorSim(SimStation):
    """The comparator (ASTRO-TAC) as its MMI presents it.

    Same ]-O / RSS: prompts and the same codeplug verbs (NEW/WR/MOVE/STOP/RD CP, RESET), but a
    smaller application: no RD MCS (so no fast codeplug read), no wildcard block, no RD AL, no
    STATION_STATUS and no firmware download over the MMI beyond CHECK ID/SWDL.  GET HW_VER
    answers ASTROTAC.  The ]-O prompt takes the session task's own commands (DORSS, DORAP,
    DOBT, DOBET, DODEBUG, DOLS, DO_MOSCAD, SETPASS, PCTL, EXIT - flash 0x0D68B0) rather than
    the Quantar's, and PCTL reports an input-echo line.  The reply text comes from the OC
    application's own strings (OC.img
    0x29d68: "CP SIZE = %d", "CP VER = %02x.%02x", "CM_NUM = PRIMARY", "STN SYS =
    CONVENTIONAL_OMNITAC", ...) and from rss.jar's AsciiCmdsRulesTable (the GET OC / GET CM_*
    forms WinRSS sends)."""

    kind = "ASTROTAC"
    # The comparator's WR handler (0x0E62AA) strcmps only AL, CP and SN - there is no wildcard
    # block (neither "WC" nor "wildcard" occurs in the 2 MB image).  AL/CP must be NEWed first,
    # as on the Quantar, with the same "not allocated" codes (0x0E6442 / 0x0E64BE).
    AREAS = {k: v for k, v in SimStation.AREAS.items() if k in ("CP", "AL")}
    RD_MAX = 214                    # RD CP's limit, as on the Quantar ("?(0030) must request
                                    # %d bytes or fewer"); not measured on comparator hardware
    CHASSIS = ["PRIMARY", "SECONDARY1", "SECONDARY2", "SECONDARY3", "SECONDARY4"]
    # The names in the 20-row command table (flash 0x0E2502), one row per accepted argument
    # count: a line that starts with one of these but matches no row is ?(0034), not ?(0014).
    COMPARATOR_CMDS = {"ALARM", "CHECK", "GET", "SET", "NEW", "RD", "WR", "MOVE", "STOP",
                       "SWDL", "RESET"}

    def __init__(self):
        super().__init__()
        self.st.update(cm_num=0, cm_mode="STANDARD", comp_name="SIMCMP",
                       stn_sys="CONVENTIONAL_OMNITAC", oc_num=1)
        self.fan = "AUTO"           # SET SC_FAN ON|AUTO; no read-back command exists
        self.stream_session = "RAP:"
        # The Local Status (LS:) session's port table.  State nibbles are the comparator's
        # own (OC.img: S%01x then 16 hex digits; a = data control, b = local vote disabled).
        self.ports = [dict(alias="PORT %d-%d" % (i // 16 + 1, i % 16 + 1), state=1,
                           hdlc=0x10 + i, delay=(i * 7) % 40, voted=False, force=False)
                      for i in range(80)]
        self.ports[2]['state'] = 2              # 1-3  analog voice
        self.ports[16]['state'] = 5             # 2-1  failed
        self.ports[32]['state'] = 6             # 3-1  disabled
        self.ports[48]['state'] = 11            # 4-1  local vote disabled
        self.ports[64]['state'] = 10            # 5-1  data control
        self.ports[4]['voted'] = True           # 1-5  voted
        self.lcm = "OFF"
        self.backplane = True       # GET ID: a backplane that answers 1 gives the empty reply
        self.sn = "999SIM0001"     # WR SN ... / GET STN SN ("STN SN = ", OC.img 0x0E9F5B)

    # The comparator's ]-O command set is its session task's own (flash 0x0D68B0), not the
    # 20-entry command table: EXIT, the DO* task starters, SETPASS and PCTL.  Each DO* starts
    # a pSOS task (rs, dr, bt, be, DBG1, ls, MOSC) and drops into that task's prompt; anything
    # else at ]-O is ?(0014) invalid command (not the Quantar's ?(1)).
    MAIN_PROMPT = {"DORSS": "RSS: ", "DORAP": "RAP: ", "DOBT": "BT> ", "DOBET": "BET: ",
                   "DO_MOSCAD": "MOSCAD: ", "DODEBUG": "DEBUG> "}
    MAIN_QUIET = ("EXIT", "SETPASS")
    # The DEBUG> session's command words (the debug task's own literals, OC.img 0x085BF876
    # onwards).  Anything else is ?(0093) invalid debug command (0x085BFE05).  The replies
    # each command builds are the task's own diagnostics and are not modelled here.
    DEBUG_CMDS = {"RUN_FUNCTION", "LIST_QUEUE", "LIST_TASK", "POLL_FUNCTION", "STOP_POLL",
                  "TASK_MESSAGE", "GET_MEM", "GET_DSP_MEM", "CHANGE_MODE", "LIST_ALL_ALERTS",
                  "WL_INIT", "OC_INIT", "FORCE_BOOT1", "CPLG"}
    # The BT> board test's replies for a good board (flash 0x12CB30+).
    BT_PASS = {
        "EEPROM1": [" EEPROM1 Under Test", "EEPROM1 Test Passed"],
        "EEPROM2": [" EEPROM2 Under Test", "EEPROM2 Test Passed"],
        "EEPROM3": [" EEPROM3 Under Test", "EEPROM3 Test Passed"],
        "DRAM": [" DRAM Under Test", " A 4MEG Ram module was detected", " DRAM Test Passed"],
        "DODSP": [" Host/DSP Under Test", "dsp_diag_result: 00000000", "SRAM test passed"],
        "HDLC": [" HDLC Under Test"],
        "SCC1": [" SCC1 Under Test"],
        "SCC4": [" SCC4 Under Test"],
        "SMC1": [" SMC1 Under Test"],
    }

    def _line(self, line):
        c = " ".join(line.split()).upper()
        if not self.stream and self.prompt == "]-O " and self.password and not self.pw_ok:
            return self._password(c)     # the session body asks before anything else
        if not self.stream and self.prompt == "]-O " and c.split()[:1] == ["PCTL"]:
            return self._pctl(c.split()[1:])
        if not self.stream and self.prompt == "]-O " and c.split()[:1] == ["SETPASS"]:
            return self._setpass(c.split()[1:])
        if not self.stream and self.prompt == "]-O " and c in self.MAIN_QUIET:
            return self._say()
        if not self.stream and self.prompt == "]-O " and c in self.MAIN_PROMPT:
            with self.lock:
                self.prompt = self.MAIN_PROMPT[c]
            return self._say()
        if not self.stream and self.prompt == "DEBUG> ":
            if c == "EXIT":
                with self.lock:
                    self.prompt = "]-O "
                return self._say()
            if c.split()[:1] and c.split()[0] in self.DEBUG_CMDS:
                return self._say()
            return self._say("?(0093) invalid debug command")
        if not self.stream and self.prompt == "]-O " and c == "DOLS":
            with self.lock:
                self.prompt = "LS: "
                self.stream, self.stream_session = "DOLS", "LS:"
                self.last, self._ls_last_s = time.time(), 0
                self._ls_meta = None
                self._ls_snapshot()
            return
        if self.stream_session == "LS:":
            with self.lock:
                if self.stream:
                    if c == "EXIT":
                        self.stream, self.stream_session = None, "RAP:"
                        self.prompt = "]-O "
                        return self._say()
                    if c:
                        self._ls(c)
                    return
                if self.prompt == "LS: ":
                    if c == "EXIT":
                        self.prompt = "]-O "
                        return self._say()
                    return self._ls(c)
        if not self.stream and self.prompt == "MOSCAD: ":
            if c == "EXIT":
                with self.lock:
                    self.prompt = "]-O "
                return self._say()
            if c in ("GET STATUS ALL", "GET STATUS UPDATE"):
                # flash 0x130C9A: the only two commands.  The sim has no SCADA database,
                # so the reply is the firmware's own empty-database literal (0x13103F).
                return self._say("STATUS=FFFFFFFFFFFF")
            return self._say("?(0017) invalid option")
        if not self.stream and self.prompt == "BT> ":
            # The board test (flash 0x12CB30, in the other half of the SIMM).  The sim's
            # board is good, so every test passes; the lower-case eepromN entry points
            # (which also set the part size) behave the same from here.
            if c == "EXIT":
                with self.lock:
                    self.prompt = "]-O "
                return self._say()
            # c is upper-cased above, so the lower-case eepromN entry points land here too.
            if c in self.BT_PASS:
                return self._say("\n".join(self.BT_PASS[c]))
            return self._say(" Invalid Board Test Command")
        if not self.stream and self.prompt == "RAP: ":
            # perform_rap_functions.c (flash 0x0E1E62): the comparator's RAP session takes
            # EXIT and nothing else - there are no metering streams to ask it for.
            if c == "EXIT":
                with self.lock:
                    self.prompt = "]-O "
                return self._say()
            return self._say("?(0014) invalid command")
        if not self.stream and self.prompt == "]-O ":
            return self._say(ERR_CMD if c else "")
        super()._line(line)

    # -- PCTL ----------------------------------------------------------------
    def _pctl(self, args):
        """The comparator's PCTL (flash 0x0D6B50): the same serial driver's report as the
        Quantar's, with an input-echo line the station's does not print, and no -BAUD option -
        the only option is -ECHO ON|OFF.  The six rates are the driver's BRGC table."""
        if not args:
            return self._say("Current port settings:\n   Baud rate is %d\n"
                             "   Input echo is off\n   Input flow control is off\n"
                             "   Output flow control is off" % self.mmi_baud)
        out = []
        while args:
            opt = args.pop(0)
            if opt != "-ECHO":
                out.append("?(0017) invalid option")
                continue
            if not args:
                out.append("?(0024) missing argument")
                continue
            if args.pop(0) not in ("ON", "OFF"):
                out.append("?(0012) invalid argument")
        return self._say("\r\n".join(out)) if out else self._say()

    # -- the comparator's Local Status (LS:) session ---------------------------
    # The comparator prints positional lines (no KEY = VALUE) - see ComparatorStatusPage.
    def _ls(self, c):
        a = c.split()
        if c == "GET LD":
            for i, p in enumerate(self.ports):
                if p['state'] in (5, 6):        # failed / disabled ports have no link
                    continue
                self.out += ("\r\nLD %02d %03d\r\nLS: " % (i + 1, p['delay'])).encode()
            self.out += b"\r\nLS: "
            return
        if c == "LCM ON":
            self.lcm = "ON"
            return self._say("LCM ON")
        if c == "LCM OFF":
            self.lcm = "OFF"
            return self._say("LCM OFF")
        if a and a[0] in ("FVP", "FV", "VDP", "VD") and len(a) == 3:
            try:
                i = int(a[1]) - 1
            except ValueError:
                return self._say("?(0015) invalid decimal integer supplied")
            if not 0 <= i < 80:
                return self._say(ERR_OPT)
            if a[0] in ("FVP", "FV"):
                on = a[2] == "ON"
                for j, p in enumerate(self.ports):
                    p['force'] = on and j == i
            else:
                # vote disable shows in the state nibble as Local Vote Disabled (b)
                if a[2] == "ON":
                    if self.ports[i]['state'] not in (5, 6):
                        self.ports[i]['state'] = 11
                elif self.ports[i]['state'] == 11:
                    self.ports[i]['state'] = 1
            return self._say()
        if a and a[0] == "GET":
            return self._say(ERR_OPT)
        return self._say(ERR_CMD)

    def _ls_lines(self):
        """The lines the comparator sends when the session starts: aliases, HDLC addresses,
        local control mode, force-voted and voted port, then the state lines."""
        out = []
        for i, p in enumerate(self.ports):
            out.append("AL %02d %s" % (i + 1, p['alias']))
            if p['state'] not in (5, 6, 7):     # no HDLC address when failed/disabled
                out.append("HA %02d %02x" % (i + 1, p['hdlc']))
        out.append("LCM %s" % self.lcm)
        fv = next((i + 1 for i, p in enumerate(self.ports) if p['force']), 0)
        vv = next((i + 1 for i, p in enumerate(self.ports) if p['voted']), 0)
        out += ["FV %02d" % fv, "VV %02d" % vv]
        out += [self._ls_s(c) for c in range(1, 6)]
        return out

    def _ls_s(self, chassis):
        nib = "".join("%x" % (self.ports[(chassis - 1) * 16 + k]['state'] & 0xF) for k in range(16))
        return "S%d %s" % (chassis, nib)

    def _ls_snapshot(self):
        self.out += "".join("\r\n%s\r\nLS: " % l for l in self._ls_lines()).encode()

    def _stream_next(self):
        if self.stream_session != "LS:":
            return super()._stream_next()
        # The state lines repeat as ports change (all five chassis once a second here), and
        # the force-voted / voted port and local control mode are re-sent when they change.
        now = time.time()
        if now - self._ls_last_s < 1.0:
            return ""
        self._ls_last_s = now
        fv = next((i + 1 for i, p in enumerate(self.ports) if p['force']), 0)
        vv = next((i + 1 for i, p in enumerate(self.ports) if p['voted']), 0)
        lines = []
        if getattr(self, '_ls_meta', None) != (fv, vv, self.lcm):
            self._ls_meta = (fv, vv, self.lcm)
            lines += ["FV %02d" % fv, "VV %02d" % vv, "LCM %s" % self.lcm]
        lines += [self._ls_s(c) for c in range(1, 6)]
        return "".join("\r\n%s\r\nLS: " % l for l in lines)

    def _rss(self, c):
        a = c.split()
        s = self.st
        cp = getattr(self, 'cp_img', None)
        if c == "GET HW_VER":
            return self._say("HW_VER = ASTROTAC")
        if c in ("GET CP SIZE", "GET CP VER", "GET CM_MODE", "GET CM_NUM",
                 "GET STN SN") and cp is None:
            # the application's own "?(0075) must load codeplug and reset" (OC.img 0x29dc3);
            # the CM_MODE and CM_NUM branches test the loaded-codeplug flag first too, and the
            # STN SN handler (flash 0x0E9358) compares the reader's result with 2 and takes this
            # branch for anything else
            return self._say("?(0075) must load codeplug and reset")
        if cp is not None and c == "GET STN SN":
            return self._say("STN SN = " + self.sn)
        if cp is not None and c == "GET CP SIZE":
            return self._say("CP SIZE = %d" % len(cp))
        if cp is not None and c == "GET CP VER":
            return self._say("CP VER = %02x.%02x" % (cp[6], cp[7]))
        simple = {
            "GET BPN": "BPN = 000000SIMCMP", "GET TIME": "TIME = " + _clock(s).strftime("%Y/%m/%d.%H:%M:%S"),
            "GET STN SYS": "STN SYS = " + s['stn_sys'],
            "GET COMPARATOR_NAME": "COMPARATOR_NAME = " + s['comp_name'],
            "GET CM_MODE": "CM_MODE = " + s['cm_mode'],
            "GET OC NUM": "OC NUM = %d" % s['oc_num'],
            "GET PS BATT_TYPE": "PS_BATT_TYPE = NONE", "GET PS EQUAL": "PS EQUAL = %s" % s['ps_equal'],
            "GET ADDR IP": "IP ADDR = %s" % s['ip'],
            "GET ADDR PHYS": "PHYS ADDR = %s" % s['phys'],
            "GET FW_VER OC.O": "FW_VER = XR03.19.001 TIMESTAMP = 1998/01/01 00:00",
            "GET OC_VER OC.O": "FW_VER = XR03.19.001 TIMESTAMP = 1998/01/01 00:00",
        }
        if c == "GET CM_NUM":
            # The chassis number is a runtime value the handler asks the system for (0x86214E0),
            # not codeplug data: 0-4 answer PRIMARY / SECONDARY1-4, anything else is
            # ?(0038) bogus value within task response (OC.img 0x29ec7).
            n = s['cm_num']
            if 0 <= n < len(self.CHASSIS):
                return self._say("CM_NUM = " + self.CHASSIS[n])
            return self._say("?(0038) bogus value within task response")
        if c == "GET STATE PASS":
            return self._say("STATE PASS = %s" % ("ENABLED" if self.password else "DISABLED"))
        r = self._get_port(c)
        if r is not None:
            return self._say(r)
        if c in simple:
            return self._say(simple[c])
        # The Alignment screen (WinRSS ComparatorAlignmentScreenMaker): battery equalization
        # (21400-21403) and the reference oscillator / PEND section (216xx).  There is no AL row
        # in the comparator's 20-row table: the RSS loop (flash 0x0D77CE) intercepts "AL" and
        # hands the rest of the line to the 2-row PEND table (0x0E2252), whose handler
        # (0x085CD45C) knows exactly six words - RD, STATUS, NOREF, SAVE, AUTO, ABORT.  Anything
        # else, including the Quantar's PEND UP/DN and every UHSO form, is ?(0014).
        # There is no GET PA ON: the comparator has no transmitter, and "PA ON" occurs
        # nowhere in the image, so the GET handler answers ?(0017) like any other option.
        align = {
            "AL PEND RD": "PEND = %d" % s['pend'],
            # 0x0E83BA does moveq #0,d5 - this build always answers 0, whatever the band is.
            "AL PEND NOREF": "TXFREQ = 0",
            "AL PEND STATUS": "PEND ABORTED",       # idle; AUTO would make it LOCKED
        }
        if c.split()[:1] == ["AL"] and cp is None:
            # The RSS loop's AL branch tests the loaded-codeplug flag before it looks at the
            # item at all (flash 0x0D7816): ?(0051) must load codeplug and reset before
            # aligning station - a code the Quantar's table does not have.
            return self._say("?(0051) must load codeplug and reset before aligning station")
        if c in align:
            return self._say(align[c])
        if c == "AL":
            return self._say("?(0024) missing argument")
        if c in ("AL PEND AUTO", "AL PEND ABORT", "AL PEND SAVE"):
            return self._say()
        if c in ("KEYUP", "DEKEY"):
            s['keyed'] = c == "KEYUP"
            return self._say()
        if c.startswith("GET OC ") and len(a) >= 4:
            # GET OC <n> SW VER|PART [OC|B1|B2]  and  GET OC <n> WL <m> SW VER|PART [WL|WB]
            # The replies are the application's own strings (OC.img 0x29fb3..0x2a0ba), which
            # print the WL token: "OC 1 WL %d SW VER WL = " - rss.jar's reply template for
            # 22359 has no such token, so a strict parse of the real reply fails.
            if "WL" in a:
                part = "WB" if a[-1] == "WB" else "WL"
                kind = "PART" if "PART" in a else "VER"
                return self._say("OC %s WL %s SW %s %s = %s" % (a[2], a[4], kind, part,
                                                                 "TTN4001" if kind == "PART" else "XR03.19.001"))
            part = a[-1] if a[-1] in ("OC", "B1", "B2") else "OC"
            if "PART" in a:
                return self._say("OC %s SW PART %s = TTN4001" % (a[2], part))
            return self._say("OC %s SW VER %s = XR03.19.001" % (a[2], part))
        if c.startswith("GET FW_VER") or c.startswith("GET OC_VER"):
            return self._say("FW_VER = XR03.19.001 TIMESTAMP = 1998/01/01 00:00")
        if c.startswith("CHECK ID"):
            return self._say()
        if c == "ALARM RD":
            # The comparator's own record line (handler flash 0x0E2A12, formatter 0x085CD9F8,
            # format 0x0E8A5A): first/last occurrence, count, port, device, the 40-character
            # name from its table at 0x08646CD8, and the type byte.  "System Reset." is code
            # 0x33 there, device AC1 (the control card) with no port - so 8 blank columns.
            return self._say("ALARM(001) = 1998/01/01.00:00:00 1998/01/01.00:00:00 1         "
                             "AC1 System Reset.                            51")
        if c == "ALARM CLEAR":          # 21160; the comparator has no ALARM_EXT
            return self._say()
        if c in ("SET SC_FAN ON", "SET SC_FAN OFF", "SET SC_FAN AUTO"):
            # 22300/22301: empty reply, which WinRSS takes as success (no read-back command)
            self.fan = c.rsplit(" ", 1)[1]
            return self._say()
        if a[:1] == ["RD"] and len(a) == 4 and not all(
                re.fullmatch(r"[0-9A-Fa-f]{1,8}", v) for v in a[2:4]):
            # RD's two numbers are type 3 (hex) in the command table, so a bad one is the
            # conversion error and is raised before the area is looked at.
            return self._say("?(0016) invalid hexadecimal number supplied")
        if cp is not None and c.startswith("RD CP ") and len(a) == 4:
            return self._say("\r\n".join(self._srecs(cp, a, self.RD_MAX)))
        if c in ("STOP AL", "STOP CP"):
            # The STOP handler (0x0E5F06) frees the buffer in its context and always answers
            # nothing - unlike NEW/WR/MOVE it does not check that anything was allocated.
            if c.endswith("CP"):
                self._cp_stage, self._cp_alloc = None, False
            else:
                self._al_stage, self._al_alloc = None, False
            return self._say()
        if c.startswith("WR SN"):
            # WR handler 0x0E62AA, SN branch: strlen(args[3]) must be exactly 10 (cmp.l #$a),
            # else ?(0013) argument must have %d characters (string 0x0E653E).
            if len(a) == 3 and len(a[2]) == 10:
                self.sn = a[2]
                return self._say()
            return self._say("?(0013) argument must have %d characters" % 10)
        w = self._area_write(c, a)
        if w is not None:
            return self._say(w)
        if c == "RESET":
            self.prompt = "]-O "
            self._cp_stage = None
            self._cp_alloc = False
            return self._say()
        if c.startswith("SET "):
            # The comparator's SET handler (0x085CBAA4) strcmps its options: DATE, TIME, REF,
            # SC_FAN, PS EQUAL, ADDR IP/PHYS, DIU_REP, ECHO_CANCEL, ECHO_DELAY.  REF takes only
            # 5MHZ and 10MHZ (not the Quantar's INT/EXT), the last three take numbers, and
            # anything else is ?(0017).
            if c.startswith("SET PS EQUAL"):
                if a[-1] in ("ON", "OFF"):
                    s['ps_equal'] = a[-1]
                    return self._say()
                return self._say('?(0018) option "%s" is invalid' % a[-1])
            if c.startswith("SET REF "):
                if a[-1] in ("5MHZ", "10MHZ"):
                    return self._say()
                return self._say('?(0018) option "%s" is invalid' % a[-1])
            # DATE needs a '/' at offsets 4 and 7, TIME a ':' at 2 and 5; the digits are then
            # converted with 0x8620692 and packed for the pSOS time set (OC.img 0x0E6F27 /
            # 0x0E6F53).  Anything else is the specification error, not ?(0018).
            if c.startswith(("SET DATE ", "SET TIME ")):
                err = _set_clock(s, a[1], a[2] if len(a) > 2 else "")
                if err:
                    return self._say(err)
                return self._say()
            if c.startswith(("SET ADDR IP ", "SET ADDR PHYS ")):
                # Tools > Set IP and MAC Address.  The real station takes the value and starts
                # using it after a reset; the simulator just keeps it for GET ADDR.
                key = "ip" if a[2] == "IP" else "phys"
                s[key] = a[3]
                return self._say()
            if c.startswith(("SET DIU_REP ", "SET ECHO_CANCEL ", "SET ECHO_DELAY ")):
                return self._say()
            return self._say("?(0017) invalid option")
        if c.startswith("GET "):
            if a[:2] == ["GET", "ID"] and len(a) in (2, 3):
                # The handler (RAM 0x085D025E) strcmps "ID" and asks 0x085A610E for the
                # backplane; a result of 1 answers nothing at all, anything else
                # ?(0039) backplane ID not available.  A comparator with its backplane in
                # place takes the empty reply.
                if not self.backplane:
                    return self._say("?(0039) backplane ID not available")
                return self._say()
            return self._say(ERR_OPT)
        # The table matches a name *and* an argument count, so a known name with the wrong
        # count is ?(0034) wrong number of parameters, not ?(0014); more than 16 arguments
        # is ?(0031) (parser steps 2 and 3, Quantar_Firmware_Analysis.md).
        if len(a) > 17:
            return self._say("?(0031) I can't handle that many arguments on one line!")
        if a and a[0] in self.COMPARATOR_CMDS:
            return self._say("?(0034) wrong number of parameters")
        return self._say(ERR_CMD)

    def _srecs(self, img, a, limit=None):
        """As the Quantar's, with the comparator's RD limit."""
        lo, hi = int(a[2], 16), int(a[3], 16)
        limit = limit or 214
        if hi < lo:
            return ["?(0027) arguments are out of order"]
        if hi - lo + 1 > limit:
            return ["?(0030) must request %d bytes or fewer" % limit]
        return super()._srecs(img, a)
