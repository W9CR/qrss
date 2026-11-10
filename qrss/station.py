"""Serial link to a Quantar station control module (front-panel RS-232 port, ASCII MMI).

Protocol as used by WinRSS (rss.comm.protocol.ascii.*):

  * 8N1, no flow control, 9600 baud by default.
  * Prompts: "]-O " (main), "RSS: " (service), "RAP: " (real-time: metering, front
    panel monitor, PTT/access control), "CSS:", "LS:", "DEBUG>".
  * To change mode: CR; if the wanted prompt is not shown, EXIT (up to 3x) until "]-O",
    "PCTL -ECHO OFF", then DORSS / DORAP / ...
  * Command replies are matched against the WinRSS template ("HW_VER = [%s]");
    an error is reported as "?(code)".
  * Real-time streams (MTR ..., FPM) run in RAP: mode and print "KEY = VALUE" lines until
    "STOP".  While a stream runs, WinRSS sends a bare CR before any other command.
  * The comparator (ASTRO-TAC) has a Local Status session instead: DOLS enters LS:, the
    comparator prints positional status lines (S1 <16 hex>, AL/HA/LD <port>, LCM, FV, VV)
    and EXIT leaves.  See Link.stream_lines and ComparatorLocalStatusPage.

The command table (qrss/data/ascii_cmds.json) is WinRSS' ASCII_CMDS_RULES_TABLE:
cmd template, reply template, required prompt before and prompt after.
"""
import datetime, json, os, re, threading, time

DATA = os.path.join(os.path.dirname(__file__), 'data')
CMDS = json.load(open(os.path.join(DATA, 'ascii_cmds.json')))
ERRORS = json.load(open(os.path.join(DATA, 'errors.json')))
PROMPTS = ["RSS:", "]-O", "RAP:", "CSS:", "LS:", "DEBUG>", "Password:"]
MODE_CMD = {"RSS:": "DORSS", "RAP:": "DORAP", "CSS:": "DOCSS", "LS:": "DOLS", "DEBUG>": "DODEBUG"}


# The SCM's baud-rate generator runs from 25 MHz (BOOT2 table at image 0x3C57E), so the rate the
# station calls "115200" is really 111607 (-3.1 %): the PC end has to be set to the real one.
STATION_ACTUAL_BAUD = {115200: 111607}


def _norm(t):
    return " ".join((t or "").split()).upper()


TEMPLATES = {}
for _k, _v in CMDS.items():
    if _v.get('cmd'):
        TEMPLATES.setdefault(_norm(_v['cmd']), _v)


class StationError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def error_text(code):
    """Human text for a "?(00NN)" reply.  The ASCII MMI's codes are DECIMAL and index the
    station's error block at 0x20000 in errors.json (RssErrorCodes): ?(0014) -> 131086
    INVALID_CMD, ?(0037) -> 131109 CANT_CALIBRATE, ?(0086) -> 131158 RX2_BOARD_DISABLE_DETECTED.
    The other blocks (0x100D0 SER_ASCII, 0x300D0 CMD_PROT, 0x800D0 QUANTAR, ...) belong to the
    other RSS protocols; a code that also matches one of those is looked up as-is first.
    Returns just the name ("INVALID_CMD"), or None for a code the table does not have."""
    name = ERRORS['station'].get(str(code))
    if name is None:
        try:
            name = ERRORS['station'].get(str(0x20000 + int(str(code), 10)))
        except ValueError:
            pass
    return name


def _station_error(text):
    """StationError for a "?(0014) invalid command" style reply, else None."""
    m = re.search(r'\?\(([^)]*)\)[ \t]*([^\r\n]*)', text)
    if not m:
        return None
    code = m.group(1)
    # The station usually appends its own wording; when it does not, fall back to the name the
    # firmware's error table gives the code.
    detail = m.group(2).strip() or (error_text(code) or '').replace('_', ' ').lower()
    return StationError("station error %s%s" % (code, (": " + detail) if detail else ''), code)


def parse_status(s):
    """Split a `GET STATION_STATUS n` reply.  The answer is "STATUS=<4 hex id><8 hex value>
    <2 hex checksum>", e.g. "000403516fbc7c" for status 4 on the lab station.

    The SCM builds it in one function at 0x0C24B0: an 8-entry table at 0x806680A
    ({u16 id, u32 value}, 6 bytes each, ids 1-8) is filled from the station state block at
    ($806B0A4), and the entry with the requested id is formatted "%.4x%.8x%.2x".  The last
    byte is the complement of id_hi + id_lo + the four value bytes (summing routine 0x0C26DA),
    so a mismatch means a garbled line.  Every value is a set of 2-bit state codes, not flags:
    the builders OR in 0b01/0b10/0b11 (or clear to 0b00) per field, one field per station
    condition.  Returns (id, value, checksum, ok) or None if the text is not a status word.
    See decode_status() for what the entries mean and parse_status_all() for `... ALL`."""
    m = re.search(r'STATUS\s*=\s*([0-9A-Fa-f]{14})', s or '')
    if not m:
        m = re.search(r'^\s*([0-9A-Fa-f]{14})\s*$', s or '')
    if not m:
        return None
    h = m.group(1)
    sid, value, ck = int(h[:4], 16), int(h[4:12], 16), int(h[12:], 16)
    return sid, value, ck, status_checksum(sid, value) == ck


def status_checksum(sid, value):
    """The trailing byte of a status word: ~(sum of the id and value bytes) & 0xFF (0x0C26DA)."""
    return (~((sid >> 8) + (sid & 0xFF) + sum((value >> (8 * k)) & 0xFF for k in range(4)))) & 0xFF


STATUS_NAMES = {1: "RTC date", 2: "RTC time", 3: "Component states", 4: "Operating state",
                5: "Simulcast / comparator", 6: "Channel", 7: "Cached state 7",
                8: "Cached state 8"}

# A 2-bit code as the firmware's builders write it: 0 = the gate failed (not fitted / not
# configured), 2 and 3 = present, with the builder's own condition false or true.
_STATUS_CODES = {0: "-", 1: "1", 2: "a", 3: "b"}

# entry 3: (label, shift of the two-bit field) - see Quantar_Firmware_Analysis.md
_STATUS3 = [("Control board", 30), ("Transmitter", 22), ("RX1", 8), ("RX2", 10),
            ("Wireline", 12), ("Power amp", 26), ("Power supply", 16)]


def decode_status(sid, value):
    """Name the fields of one STATION_STATUS value: [(label, text), ...].

    Entries 1/2 are the RTC (year 12 bits, month 5, day 5; hour 5, minute 6, second 6).
    Entry 4 byte 3 (0x8066821) carries the access state - 0b10 enabled, 0b11 disabled - which
    is the only field WinRSS reads (StationEnableDisable: 8-B "Enabled", C-F "Disabled");
    proven on the lab station 2026-11-07, `SET STATION_DISABLE ON` takes it 0xBC -> 0xFC.
    Byte 2 bit 4 is the front-panel ACC_DIS key (the FPM stream's ACC_DIS), not the access
    state: `FP ACC_DIS ON` sets it while byte 3 stays 0xBC.
    Entries 7/8 are three cached arguments plus a validity bit whose source is unidentified,
    so they are shown raw.  Layouts and the builder for every field: the firmware notes,
    "ASCII MMI error codes and GET STATION_STATUS"."""
    b = [(value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
    c = lambda v: _STATUS_CODES[v]
    if sid == 1:
        return [("Date", "%04d/%02d/%02d" % (value & 0xFFF, (value >> 12) & 0x1F,
                                              (value >> 17) & 0x1F))]
    if sid == 2:
        return [("Time", "%02d:%02d:%02d" % ((value >> 12) & 0x1F, (value >> 6) & 0x3F,
                                              value & 0x3F))]
    if sid == 3:
        return [(lbl, c((value >> sh) & 3)) for lbl, sh in _STATUS3]
    if sid == 4:
        acc = {8: "Enabled", 9: "Enabled", 10: "Enabled", 11: "Enabled",
               12: "Disabled", 13: "Disabled", 14: "Disabled", 15: "Disabled"}.get(b[3] >> 4)
        return [("Access", acc or "unknown"), ("Transmitter", c((b[3] >> 4) & 3)),
                ("Receiver", c((b[3] >> 2) & 3)), ("Repeater operation", c(b[3] & 3)),
                ("Simulcast", "on" if b[1] & 0x80 else "off"),
                ("Front-panel access disable", "on" if b[2] & 0x10 else "off"),
                ("Remote control", format(b[0] & 7, "03b")), ("Raw", "0x%08x" % value)]
    if sid == 5:
        return [("Feature 0x9C/3", c(b[3] & 3)), ("Simulcast 0x4E/7", c((b[3] >> 2) & 3)),
                ("Comparator 0x4F/6", c((b[3] >> 4) & 3)), ("Simulcast flag", c(b[3] >> 6))]
    if sid == 6:
        return [("Current channel", "%d" % b[3]), ("Channel table word", "0x%02x" % b[2])]
    if sid in (7, 8):
        return [("Arg 1", "%d" % (value & 0x3FF)), ("Arg 2", "%d" % ((value >> 10) & 0x3FF)),
                ("Arg 3", "%d" % ((value >> 20) & 0xFF)),
                ("Valid", "yes" if value & 0x10000000 else "no")]
    return [("Value", "0x%08x" % value)]


def parse_status_all(s):
    """Split a `GET STATION_STATUS ALL` reply into {id: value}.

    The station prints entries 1-5, then a checksum byte and a newline, then entries 6, 7 and
    8 followed by one more checksum that covers only 6+7+8 (the firmware clears its
    accumulator after the 1-5 group and not again before entry 8).  Returns
    ({id: value}, [ok, ...]) - one verdict per checksum - or (None, None)."""
    t = re.sub(r'\s+', '', (s or '').replace('STATUS=', ''))
    m = re.match(r'^([0-9A-Fa-f]{60})([0-9A-Fa-f]{2})([0-9A-Fa-f]{36})([0-9A-Fa-f]{2})$', t)
    if not m:
        return None, None
    groups = [m.group(1), m.group(3)]
    vals = {}
    for g in groups:
        for i in range(0, len(g), 12):      # 4 hex id + 8 hex value, no per-entry checksum
            sid, value = int(g[i:i + 4], 16), int(g[i + 4:i + 12], 16)
            vals[sid] = value
    def _sum(ids):
        out = 0
        for i in ids:
            out += (i >> 8) + (i & 0xFF) + sum((vals[i] >> (8 * k)) & 0xFF for k in range(4))
        return (~out) & 0xFF
    ok = [_sum(range(1, 6)) == int(m.group(2), 16),
          _sum((6, 7, 8)) == int(m.group(4), 16)]
    return vals, ok


def _reply_regex(tmpl):
    parts = []
    toks = tmpl.split()
    for i, t in enumerate(toks):
        if t == '[%s]' and i == len(toks) - 1:
            # last field may be empty: the station answers e.g. "RX_FREQ_BAND = " when it has no value
            parts.append(r'[ \t]*(.*?)[ \t]*$')
        else:
            parts.append((r'\s+' if i else '') + (r'(\S+)' if t == '[%s]' else re.escape(t)))
    return re.compile(''.join(parts), re.M)


class Link:
    """Synchronous station link.  `port` is a device name, or an object with
    read(n)/write(b)/close() (e.g. the simulator)."""

    def __init__(self, port, baud=9600, log=None, password=None):
        if isinstance(port, str):
            import serial
            if "://" in port:
                # a pyserial URL, e.g. socket://host:port for a station reached over a serial<->TCP
                # bridge (tools/serial_bridge.py on the machine wired to it).  Line settings live at
                # the bridge; the baud here is nominal (a raw TCP stream has none).
                self.s = serial.serial_for_url(port, baudrate=int(baud), timeout=0.05)
            else:
                self.s = serial.Serial(port, int(baud), bytesize=8, parity='N', stopbits=1,
                                       timeout=0.05, xonxoff=False, rtscts=False)
        else:
            self.s = port
        self.log = log or (lambda direction, text: None)
        self.prompt = None
        self.streaming = None          # stream command while a real-time stream runs
        self.stream_session = "RAP:"   # the prompt that stream runs in ("RAP:" or "LS:")
        self._stream_err = None
        self._rx = ""
        self._iw = None                # whether the port supports in_waiting (poll vs blocking read)
        self.mmi_baud = 9600           # rate the station's RSS port is at (None: not known)
        # The station's password, if it is password protected (the comparator always can be, a
        # Quantar from release 5.0).  Stored and sent uppercase: the firmware uppercases what is
        # typed (toupper at flash 0x0DE682 in the OC app) and the MMI is otherwise case sensitive.
        self.password = (password or "").strip().upper() or None
        self.kind = None               # 'QUANTAR' / 'ASTROTAC', from probe()
        self.hw_ver = None
        self.lock = threading.RLock()

    def close(self):
        try:
            if self.streaming:
                self.stream_stop()
        except Exception:
            pass
        self.s.close()

    # ------------------------------------------------------------------ low level
    def _write(self, text):
        self.log('>', text)
        self.s.write(text.encode('latin1') + b"\r")

    POLL = 0.002                       # idle poll interval when in_waiting is 0
    QUIET = 0.010                      # silence that ends a _flush drain (see _flush)

    def _read(self):
        """Whatever has arrived, without sitting out the port's 50 ms read timeout: poll in_waiting
        and read only what is there (like netstack.Stack._read).  Falls back to a blocking read for a
        port that has no in_waiting (some pyserial URLs)."""
        b = b""
        if self._iw is not False:
            try:
                n = self.s.in_waiting
            except Exception:
                self._iw = False
            else:
                self._iw = True
                if n:
                    b = self.s.read(n)
                else:
                    time.sleep(self.POLL)
        if self._iw is False:
            b = self.s.read(512)
        if b:
            t = b.replace(b"\0", b"").decode('latin1')
            self._rx += t
            return t
        return ""

    def _flush(self):
        """Drop anything unsolicited (boot banners, late prompts) before a command.

        Drain until QUIET of silence, not until one empty poll: the station sometimes prints a second
        prompt a few milliseconds after the first (seen when EXIT takes the session from RAP: back to
        ]-O on H026.11.05f).  With a single 2 ms poll that leftover was read as the *next* command's
        reply, and every reply after it was one behind (DORSS was answered with the old ]-O)."""
        self._rx = ""
        end = time.time() + 0.5
        quiet = time.time() + self.QUIET
        while time.time() < end:
            if self._read():
                quiet = time.time() + self.QUIET
            elif time.time() >= quiet:
                break
        if self._rx:
            self.log('<', self._rx)
        self._rx = ""

    def _read_until_prompt(self, timeout=2.5):
        """Read until a prompt ends the buffer (or timeout).  Returns (text, prompt)."""
        end = time.time() + timeout
        while time.time() < end:
            self._read()
            tail = self._rx.rstrip()
            for p in PROMPTS:
                if tail.endswith(p):
                    text, self._rx = self._rx, ""
                    self.log('<', text)
                    self.prompt = p
                    return text, p
        text, self._rx = self._rx, ""
        if text:
            self.log('<', text)
        return text, None

    # A password-protected station answers with "Password: " instead of a prompt until it gets
    # the password.  WinRSS answers it the same way wherever it turns up (SerialAsciiOperation
    # .doCmd / changePrompt): send the password, then a bare CR, and take that reply as the one
    # the command was waiting for.  The firmware (OC app, flash 0x0E3656) reads the line, checks
    # it is 5-8 characters with no illegal ones, and says nothing at all when it is accepted -
    # hence the CR.  A wrong one gets "?(0076) password incorrect" and the prompt again; five of
    # those in a row get "?(0081) access denied! try again in a minute" and a 60 s wait.
    #
    # Two words are an override rather than a password: LOST erases the station's password and
    # lets the session in, SERVICE lets it in for this session only.  Both are accepted only in
    # "special mode", which the firmware reads from the SCM's P0_IN port, bits 16-19 == 3 (the
    # Quantar app tests it at 0x0CB23E, the comparator's OC app at 0x0D920E).  That is the RSS
    # manual's "Bypassing the Station Password": hold Vol Up and Vol Down on the station control
    # front panel (two of the four P0_IN pushbutton lines) while the word goes out.  Anywhere
    # else they are just wrong passwords - ?(0076).  A password itself is stored as 15 encrypted
    # bytes in the PP record (PP[0] bit 7 = enabled), so there is no plaintext to fall back on.
    PW_TIMEOUT = 0.8                   # a rejection is immediate; silence means it was accepted
    OVERRIDE = ("LOST", "SERVICE")
    SPECIAL_MODE_HINT = (" - special mode is the front panel: hold Vol Up and Vol Down on the "
                         "station control panel while the word goes out (RSS manual, "
                         "\"Bypassing the Station Password\")")

    def _answer_password(self, timeout):
        """Answer the "Password: " prompt.  Returns (body, prompt) of the bare CR that follows
        an accepted password; raises StationError if the station has no password to give."""
        if not self.password:
            raise StationError("the station is password protected - type its password in the "
                               "Connect dialog (or in the Station Terminal); LOST and SERVICE "
                               "are the firmware's own override words in special mode")
        sent = self.password
        word = sent.strip().upper()        # the firmware uppercases the line before comparing
        self._write(sent)
        buf, p = self._read_until_prompt(self.PW_TIMEOUT)
        if p == "Password:":
            e = _station_error(buf)         # "?(0076) password incorrect", in its own words
            if e:
                if word in self.OVERRIDE:
                    raise StationError("%s was not accepted - the station is not in special "
                                       "mode (%s)%s" % (sent, e, self.SPECIAL_MODE_HINT))
                raise e
            raise StationError("the station did not accept the password (%r)" % buf.strip())
        if word == "LOST":
            self.password = None            # the station just erased it; do not send it again
        self._write("")                 # accepted silently; the CR asks for the prompt
        return self._read_until_prompt(timeout)

    def raw(self, text, timeout=2.5):
        """Send a line, return (body, prompt) with echo and prompt removed."""
        with self.lock:
            self._flush()
            self._write(text)
            buf, p = self._read_until_prompt(timeout)
            if p is None:
                raise StationError("no response to %r" % text)
            if p == "Password:":
                buf, p = self._answer_password(timeout)
                if p is None:
                    raise StationError("no response to %r (after the password)" % text)
                if p == "Password:":
                    raise StationError("the station wants its password again (%s)" % buf.strip())
            body = buf.rstrip()
            if body.endswith(p):
                body = body[:-len(p)]
            lines = body.replace('\r', '\n').split('\n')
            if lines and lines[0].strip().upper() == text.strip().upper():
                lines = lines[1:]
            return "\n".join(l.rstrip() for l in lines if l.strip()), p

    # ------------------------------------------------------------------ modes
    def ensure(self, prompt):
        if self.streaming:
            if prompt == "RAP:":
                return
            self.stream_stop()
        if self.prompt == prompt:          # last reply ended with this prompt
            return
        if self.prompt == "Password:":     # a bare CR here would be read as a password attempt
            with self.lock:
                _, p = self._answer_password(2.5)
            if p is None:
                raise StationError("the station stopped answering after the password")
        _, p = self.raw("")
        if p == prompt:
            return
        n = 0
        while p != "]-O" and n < 5:
            if n:
                time.sleep(1.0)     # after a stream the station ignores EXIT for ~3 s (measured
                                    # 3.2 s, three trials, FPM on H026.11.05f), so give each
                                    # try its own window instead of firing them back to back
            _, p = self.raw("EXIT", timeout=2.0)
            n += 1
        if p != "]-O":
            raise StationError("cannot reach the main ]-O prompt (got %r)" % p)
        self.raw("PCTL -ECHO OFF")
        if prompt != "]-O":
            _, p = self.raw(MODE_CMD[prompt])
            if p != prompt:
                raise StationError("station did not enter %s mode (got %r)" % (prompt, p))

    # ------------------------------------------------------------------ what is this?
    # The comparator (ASTRO-TAC) runs the same SCM family with a 68040 CPU and answers the same
    # ]-O / RSS: prompts and the same codeplug verbs, but its application has a smaller MMI: no
    # RD MCS (so no fast codeplug read), no wildcard block, no RD AL and no STATION_STATUS.  Its
    # GET HW_VER answers "ASTROTAC" (or "OMNITAC" on the earlier OmniTAC build, which WinRSS
    # accepts too); the Quantar's answers its platform (QUANTAR/QUANTRO/...).
    def probe(self):
        """Identify the station from GET HW_VER and remember it.  Returns 'QUANTAR' or
        'ASTROTAC' (the comparator).  Costs one read-only command."""
        with self.lock:
            self.ensure("RSS:")
            try:
                v = self.get("GET HW_VER")
            except StationError:
                v = ""
        v = (v or "").strip().upper()
        self.hw_ver = v
        self.kind = "ASTROTAC" if v.replace("-", "").replace(" ", "") in ("ASTROTAC", "OMNITAC") else "QUANTAR"
        return self.kind

    @property
    def is_comparator(self):
        return self.kind == "ASTROTAC"

    def password_state(self):
        """Whether the station has a password set: GET STATE PASS (WinRSS id 21950) answers
        "STATE PASS = ENABLED" or "= DISABLED" at the RSS: prompt.  Read-only, one command.
        None if the station does not answer it (an application older than 5.0)."""
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw("GET STATE PASS", 3.0)
        e = _station_error(body)
        if e:
            return None
        m = re.search(r'STATE\s+PASS\s*=\s*(\S+)', body, re.I)
        if not m:
            return None
        return m.group(1).strip().upper() == "ENABLED"

    def port(self, name):
        """Read one of the SCM's parallel ports: GET PORT P0_IN|P1_IN|P0_OUT|P1_OUT answers
        "\nPORT <name> = 0x<hex>" at the RSS: prompt.  Read-only.  P0_IN bits 16-19 are the
        "special mode" field the LOST/SERVICE password override is gated on (see special_mode),
        and the rest is what the station's boards are driving.  None if the station does not
        answer it."""
        name = name.strip().upper()
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw("GET PORT %s" % name, 3.0)
        if _station_error(body):
            return None
        m = re.search(r'PORT\s+\S+\s*=\s*(0x[0-9a-f]+|\d+)', body, re.I)
        return int(m.group(1), 0) if m else None

    def special_mode(self):
        """Whether the station's P0_IN port has bits 16-19 == 3, which is what makes LOST and
        SERVICE work at the Password: prompt.  The firmware tests it at 0x0CB254.  It is set
        from the front panel: hold Vol Up and Vol Down on the station control panel (the RSS
        manual's "Bypassing the Station Password").  None if the port cannot be read."""
        v = self.port("P0_IN")
        return None if v is None else ((v >> 16) & 0xF) == 3

    def set_password(self, current, new, confirm=None):
        """Change the station's password with SETPASS <current> <new> <confirm> (WinRSS id 21951,
        at the ]-O prompt).  new="" with confirm="" erases it.  The station's own wording comes
        back as "?(0080) password not matched--password unchanged" and the like, which
        _station_error turns into an exception.  Both orderings are accepted by the firmware
        (OC app 0x0D8A72 compares the stored value with each of the three arguments); this is
        WinRSS' order, from PasswordProtectionScreenMaker's CurrentPaswd/NewPaswd/ConfirmNewPaswd.
        Returns the reply text (usually empty)."""
        if confirm is None:
            confirm = new
        # The handler tokenises exactly three arguments and answers ?(0014) invalid command if
        # any is missing (the Quantar's, 0x0B45D2: three " \r\n" separators, then a null check;
        # measured on H026.11.05f).  A blank argument cannot go over the MMI - runs of spaces
        # collapse - so it is spelled NONE, which the handler understands.
        args = [(current or "").strip().upper(), (new or "").strip().upper(),
                (confirm or "").strip().upper()]
        args = [a or "NONE" for a in args]
        with self.lock:
            self.ensure("]-O")
            body, _ = self.raw(" ".join(["SETPASS"] + args), 5.0)
        e = _station_error(body)
        if e:
            raise e
        self.password = (new or "").strip().upper() or None
        return body

    # The comparator's firmware versions: it has no GET FW_VER <file>.  Its own GETs report
    # them (OC.img 0x29d68; rss.jar ids 22350/22353/22355/22356/22359) and the reply carries
    # no timestamp.  The control card number is the one GET OC NUM answers - the application
    # always prints "OC 1" - and the wireline commands take the card's number too.
    CMP_VER = {"oc.o": "GET OC %s SW VER OC", "boot.o": "GET OC %s SW VER B1",
               "boot2.o": "GET OC %s SW VER B2", "wl.o": "GET OC %s WL 1 SW VER WL"}

    def comparator_number(self):
        try:
            v = self.get("GET OC NUM")
        except StationError:
            return "1"
        return (v if isinstance(v, str) else " ".join(v)).strip() or "1"

    def comparator_version(self, name):
        """Version of one comparator image ("oc.o", "boot.o", "boot2.o", "wl.o"), or raises.
        Read with a lenient regex rather than call(): the WL reply prints an extra "WL" token
        ("OC 1 WL 1 SW VER WL = XR03.19.001", OC.img 0x2a062) that rss.jar's reply template
        for 22359 does not have, so a strict parse of the real reply fails."""
        tpl = self.CMP_VER.get(name.lower())
        if tpl is None:
            raise StationError("the comparator has no version command for %s" % name)
        cmd = tpl % self.comparator_number()
        self.ensure("RSS:")
        body, _ = self.raw(cmd)
        m = re.search(r'=\s*(\S+)\s*$', body.strip(), re.M)
        if not m:
            raise StationError("unexpected reply to %s: %r" % (cmd, body[-120:]))
        return m.group(1)

    # ------------------------------------------------------------------ MMI speed
    # H026.11.05f+ applications take "PCTL -BAUD <rate>" at the ]-O prompt and switch the RSS port
    # the command is typed on (front SMC2/BRG4, rear SMC1/BRG2).  It is not saved: every reset is
    # back at 9600, so the speed has to be raised again after each connect.  Stock firmware (and
    # 05d/05e, which switch another port) answer ?(0017) / do not change this port.
    MMI_BAUDS = (9600, 57600, 115200)

    def set_mmi_baud(self, rate, timeout=2.0):
        """Switch the station's RSS port and this end to `rate`.  Returns the prompt ("]-O") when
        the station took it, None when it refused (stock firmware) or the switch was lost; in both
        of those cases the station is still at the old rate and so is the port.

        The station switches as soon as it has parsed the command, so its prompt already comes at
        the new rate: send it, let it go out, switch our side, drop whatever arrived meanwhile,
        then a CR for a clean ]-O prompt."""
        with self.lock:
            self.ensure("]-O")
            self._flush()
            old = getattr(self.s, "baudrate", 9600)
            self._write("PCTL -BAUD %d" % rate)
            fl = getattr(self.s, "flush", None)      # pyserial: wait until it has gone out
            if fl:
                try:
                    fl()
                except Exception:
                    pass
            self._rx = ""
            text = ""
            end = time.time() + 0.3                  # a refusal (?(0017)) arrives at the old rate
            while time.time() < end:
                text += self._read()
            if text:
                self.log('<', text)
            if "?(" in text:
                try:
                    self.s.baudrate = old
                except Exception:
                    pass
                return None
            try:
                self.s.baudrate = STATION_ACTUAL_BAUD.get(rate, rate)
            except Exception:
                return None
            self.mmi_baud = rate
            time.sleep(0.05)
            self._flush()
            self.prompt = None
            try:
                p = self.raw("", timeout)[1]
            except StationError:
                p = None
            if p != "]-O":
                self.mmi_baud = None                 # switched to something, but not where we are
            return p

    def restore_mmi_baud(self):
        """Best effort back to 9600 (a station reset does it too): first where the MMI last
        answered, then at 9600 itself; blind EXIT + PCTL -BAUD 9600 only if neither answers."""
        if getattr(self.s, "baudrate", 9600) == 9600 and self.mmi_baud == 9600:
            return True
        tried = getattr(self.s, "baudrate", 9600)
        for pc in dict.fromkeys([tried, 9600]):
            try:
                self.s.baudrate = pc
            except Exception:
                pass
            time.sleep(0.05)
            self._flush()
            self.prompt = None
            try:
                self.ensure("]-O")
            except StationError:
                continue
            if self.set_mmi_baud(9600):
                return True
        try:
            for cmd in ("EXIT", "PCTL -BAUD 9600"):
                self._write(cmd)
                time.sleep(0.3)
            self.s.baudrate = 9600
            self._flush()
            self.prompt = None
            ok = self.raw("", 1.5)[1] == "]-O"
        except Exception:
            ok = False
        self.mmi_baud = 9600 if ok else None
        return ok

    # ------------------------------------------------------------------ commands
    def call(self, template, *args, timeout=2.5):
        """Run a WinRSS table command.  `template` is the command text with [%s] for
        parameters, e.g. call("AL TXDEV WR [%s] [%s] [%s] [%s]", 1, 2, 3, 4).
        Returns the list of reply fields ([] when the command has no reply template)."""
        ent = TEMPLATES.get(_norm(template))
        if ent is None:
            raise KeyError("unknown command template %r" % template)
        it = iter(args)
        cmd = " ".join(str(next(it)) if t == '[%s]' else t for t in ent['cmd'].split())
        with self.lock:
            if self.streaming and ent['pre'] == self.stream_session:
                # WinRSS sends these asynchronously while the stream runs
                self.stream_cmd(cmd)
                return []
            self.ensure(ent['pre'])
            body, p = self.raw(cmd, timeout)
        e = _station_error(body)
        if e:
            raise e
        if not ent.get('reply'):
            return []
        if _norm(ent['reply']) == '[%S]':
            return [body.strip()]
        r = _reply_regex(ent['reply']).search(body)
        if not r:
            if not body.strip():
                # The station answered with an empty line.  Seen on GET DSP RSSI when the DSP
                # has no reading to report (the lab station with its reference oscillator
                # down): the command is understood, there is just nothing to say.  The code
                # lets a caller tell it from a reply it could not parse.
                raise StationError("empty reply to %s - the station has nothing to report"
                                   % cmd, "EMPTY")
            raise StationError("unexpected reply to %s: %r" % (cmd, body[-200:]))
        return [g.strip() for g in r.groups()]

    def get(self, template, *args, **kw):
        v = self.call(template, *args, **kw)
        return v[0] if len(v) == 1 else v

    # ------------------------------------------------------------ station access
    def set_station_disabled(self, disabled=True, timeout=3.0, drop=16.0):
        """` SET STATION_DISABLE ON|OFF` (WinRSS ids 22252/22251): the station's own access
        state, which is what `GET STATION_STATUS 4` byte 3 bits 7-6 report (0b11 disabled).
        This is not the front panel's ACC_DIS key (`FP ACC_DIS`, which only moves bit 4 of
        status byte 2).

        The table entry says the reply prompt is RSS:, but the station answers there and then
        goes quiet and drops the session to ]-O about 12.5 s later (measured on H026.11.05f,
        both directions; the ON form prints `ROOT:: Station forced into
        <<STATION_DISABLE_MODE>> from serial interface.` as it goes).  Anything sent in that
        window is lost, and the late prompt would otherwise be read as the reply to the next
        command, so wait for it here and leave the link at ]-O (the caller ensures the mode it
        wants).  Returns the reply body."""
        with self.lock:
            self.ensure("RSS:")
            self._flush()
            self._write("SET STATION_DISABLE " + ("ON" if disabled else "OFF"))
            body, p = self._read_until_prompt(timeout)
            if p is None:
                raise StationError("no response to SET STATION_DISABLE")
            self.prompt = p
            self._rx = ""
            end = time.time() + drop
            while p != "]-O" and time.time() < end:
                self._read()
                if self._rx.rstrip().endswith("]-O"):
                    self.prompt = "]-O"
                    break
                time.sleep(0.02)
            self._rx = ""
        return body

    def station_access(self):
        """(state, value): 'Enabled'/'Disabled' from GET STATION_STATUS 4 - the field WinRSS
        shows - and the raw status word it came from."""
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw("GET STATION_STATUS 4")
        st = parse_status(body)
        if not st:
            raise StationError("unreadable STATION_STATUS 4 reply: %r" % body[-80:])
        return dict(decode_status(4, st[1]))["Access"], st[1]

    # ------------------------------------------------------------ date and time
    # WinRSS' Tools > Set Date And Time.  Over a serial/modem link it uses the tabulated
    # commands (rss.gui.tools.SetDateAndTimeMaker: GET TIME 21300, SET DATE 21301, SET TIME
    # 21302); its Ethernet path is the NMA pair nmaDateTime/nmaNtpOverwrite instead, which a
    # Quantar SCM does not speak.  The SCM's own SET handler (flash 0xBBD46 / 0xBFA72) takes
    # the date as YYYY/MM/DD ('/' at offsets 4 and 7) and the time as HH:MM or HH:MM:SS
    # (':' at 2, and at 5 when the seconds are there), answers ?(0040)/?(0041) when a value
    # does not parse, and ?(0095) in RSS-only mode; a good one is answered with an empty line.
    TIME_FMT = "%Y/%m/%d.%H:%M:%S"      # the GET TIME reply: " TIME = 2026/11/08.14:03:27"

    def get_time(self):
        """The station's clock: (datetime, the raw value).  Every alarm in the status log is
        stamped with it, so a wrong clock makes the Status Report unreadable."""
        raw = self.get("GET TIME")
        try:
            return datetime.datetime.strptime(raw.strip(), self.TIME_FMT), raw
        except ValueError:
            raise StationError("unreadable GET TIME reply: %r" % raw)

    def set_datetime(self, when):
        """Set the station's clock: SET DATE then SET TIME (the order WinRSS sends them in).
        Returns the time the station reads back.  Harmless - nothing in the station depends on
        the clock except the alarm stamps and the codeplug timestamp."""
        with self.lock:
            self.call("SET DATE [%s]", when.strftime("%Y/%m/%d"))
            self.call("SET TIME [%s]", when.strftime("%H:%M:%S"))
        return self.get_time()

    def eepot(self):
        """(VCO EEPOT, Ref Mod EEPOT), each 0-255 - the two bytes the SCM keeps as its copy
        of the exciter EEPROM's +3/+4 trims (exciter opcode 0x41).  The `EEPOT` engineering
        command prints them; WinRSS' command table has no entry for it.  Read-only."""
        raw = self._engineering("EEPOT")
        m = re.search(r"VCO EEPOT = (\d+),\s*Ref Mod EEPOT = (\d+)", raw)
        if not m:
            raise StationError("unreadable EEPOT reply: %r" % raw)
        return int(m.group(1)), int(m.group(2))

    def freqs(self):
        """(RX, RX2, TX) in Hz from the `FREQ` engineering command, the same three numbers
        `GET RX FREQ`, `GET RX2 FREQ` and `GET TX FREQ` report one at a time.  Read-only."""
        raw = self._engineering("FREQ")
        m = re.search(r"RX_FREQ = (\d+)\s+RX2_FREQ = (\d+)\s+TX_FREQ = (\d+)", raw)
        if not m:
            raise StationError("unreadable FREQ reply: %r" % raw)
        return tuple(int(g) for g in m.groups())

    def _engineering(self, cmd, timeout=2.5):
        """A service-port command the application has but WinRSS' table does not (EEPOT,
        FREQ, EPSILON, ASIC, IMAGE): sent as `GET <cmd>` at RSS: and returned as the reply
        body.  They hang off the same GET handler as FW_VER/HW_VER/GDW, so the verb is
        required - a bare `EEPOT` is ?(0014) invalid command."""
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw("GET " + cmd, timeout)
        e = _station_error(body)
        if e:
            raise e
        return body

    def get_addresses(self):
        """The station's network identity: (IP, physical address), the two `GET ADDR` replies
        (WinRSS' Tools > Set IP and MAC Address reads them the same way).  A conventional
        repeater has both but uses neither; IntelliRepeater and the comparator do."""
        with self.lock:
            ip = self.get("GET ADDR IP")
            phys = self.get("GET ADDR PHYS")
        return ip, phys

    def set_address(self, ip=None, phys=None):
        """`SET ADDR IP` / `SET ADDR PHYS` (WinRSS ids 21022/21023): the station takes the new
        values but only starts using them after a reset, which WinRSS sends when the dialog
        closes.  Returns what the station reads back.  An empty value is left alone."""
        with self.lock:
            if ip:
                self.call("SET ADDR IP [%s]", ip)
            if phys:
                self.call("SET ADDR PHYS [%s]", phys)
        return self.get_addresses()

    def reset(self, wait=60.0):
        """Restart the station's application (WinRSS's ' RESET', id 21352).

        This is what WinRSS sends when a service screen closes (alignment, metering, status
        panel, test/measurement, comparator) and after changing the IP address: the station
        answers the command, then the app goes away for ~12 s and comes back at the ]-O
        prompt.  The codeplug and the alignment values are re-read from NVM, so anything
        held only in RAM is lost - that is the point (alignment state, a keyed PA, etc.).

        RESTART (id 21024) is a different command - a network restart - and the app answers
        it ?(0014) invalid command.  Wait for the silence to start and end, then return with
        the link at ]-O (the caller must ensure("RSS:") again)."""
        self.ensure("RSS:")
        t0 = time.time()
        self.raw("RESET", timeout=5.0)         # answered with the prompt, then it goes away
        went_down = False
        while time.time() - t0 < wait:
            try:
                p = self.raw("", timeout=1.0)[1]
            except StationError:
                went_down = True
                time.sleep(0.25)
                continue
            if p == "]-O" and (went_down or time.time() - t0 > 2.0):
                self.prompt = "]-O"
                return True
            time.sleep(0.25)
        raise StationError("the station did not come back within %.0f s of RESET" % wait)

    # ------------------------------------------------------------------ codeplug
    CP_CHUNK = 214      # more is refused: "?(0030) must request 214 bytes or fewer"

    # The codeplug is also in RAM, and reading it there is ~3.5x faster.  Every RD CP makes the
    # resident NVM driver (0x080004E2, called from cp_read 0x0E2C1E) re-read the whole NVM image
    # first, which is a fixed ~0.38 s whatever the request length; RD MCS reads RAM directly and
    # costs ~50 ms.  The application keeps the codeplug at [0x0806AE90] + 0x100 (the 0x100 bytes
    # before it are the WC block; RD WC 0 reads them, RD AL 0 reads the block after it).  Its
    # pointers are *relocated*: where the stored image has an offset, RAM has base + offset, so
    # undo that; the directory's 0x80000000 "list of lists" bit (dir[9] on v0x28) is not kept in
    # RAM and is put back from the table's shape, but byte 0x107 is runtime state (0x03 on the
    # station, 0x00 in a stored image), so the image is the station's live copy - read_codeplug
    # (fast=False) gives the stored image byte for byte.
    MCS_BASE = 0x08002E6E       # RD MCS <a> reads RAM at 0x08002E6E + a (the app's own arithmetic)
    CP_RAM_PTR = 0x0806AE90     # -> WC block; the codeplug follows 0x100 bytes later
    CP_RAM_SKIP = 0x100

    def mcs_read(self, off, n, progress=None):
        """n bytes of RAM from MCS offset off, in CP_CHUNK-byte requests.  The S2 record address
        is the offset itself (the app formats against 0x08002E6E)."""
        from .codeplug import s2_parse
        out = bytearray()
        with self.lock:
            self.ensure("RSS:")
            while len(out) < n:
                k = min(self.CP_CHUNK, n - len(out))
                want = off + len(out)
                body, _ = self.raw("RD MCS %X %X" % (want, want + k - 1), 5.0)
                e = _station_error(body)
                if e:
                    raise e
                at = want
                for line in body.splitlines():
                    line = line.strip()
                    if not line.startswith("S2"):
                        continue
                    addr, data = s2_parse(line)
                    if addr != at:
                        raise StationError("RD MCS %X: record for %X, expected %X" % (want, addr, at))
                    out += data
                    at += len(data)
                if at == want:
                    raise StationError("RD MCS %X %X: no data" % (want, want + k - 1))
                if progress:
                    progress(min(len(out), n), n)
        del out[n:]
        return bytes(out)

    def mcs_block(self, progress=None):
        """The station's multi-coded-squelch usage accounting: `GET MCS SIZE` gives the byte
        count (8 per access code table - the application's own `8 * u16(*(0x0806B0A8))`, and
        0 when the station is not in MCS mode or its firmware predates 0x23), and the block
        itself is the first bytes of the RD MCS window.  Each 8-byte record is u32 calls and
        u32 seconds, both saturated at 249 (firmware 0x0D6366); a station that has never
        initialised it answers 0xDEADDEAD in the first record (0x0E8C96 clears the block)."""
        body, _ = self.raw("GET MCS SIZE", 2.5)
        e = _station_error(body)
        if e:
            raise e
        m = re.search(r"MCS SIZE\s*=\s*(\d+)", body)
        if not m:
            raise StationError("GET MCS SIZE: unexpected reply %r" % body.strip())
        size = int(m.group(1))
        if not size:
            return b""
        return self.mcs_read(0, size, progress)

    def clear_mcs(self):
        """`CLEAR MCS` (WinRSS' Clear Accounting): zero the usage counters in the station."""
        body, _ = self.raw("CLEAR MCS", 5.0)
        e = _station_error(body)
        if e:
            raise e
        return body

    # ------------------------------------------------------------------ EEPROM conditioning
    # The SCM conditions its own EEPROM.  At boot CHECK_NVM_CONDITIONING (0x0DE726) reads the SA
    # partition and compares SA[0x7C..0x7D] with 0xFADE and SA[0x7E] with byte 2 of the 128-byte
    # EEPROM header (the part's id byte); when either differs it runs COND_NVM_PARTITIONS
    # (0x0E04B2) - for each partition in the table at 0x13195E (DBS, MC, MI, PP, SA, SC, SR, WC)
    # read the whole partition, write 0x00 over it, write 0xFF over it, write the original bytes
    # back - then writes the marker (SA[0x7C]=FADE, SA[0x7E]=header byte 2) and SA[0x7B]=0xFF.
    # Alarms 0xDB/0xDC/0xDD are START/NVM_CONDITIONING_ERROR/FINISH_NVM_CONDITIONING.
    #
    # There is no RD SA on the MMI, but the SA partition is reachable as AL (GET AL SIZE = 128),
    # so the marker reads out read-only with RD AL 7C 7E: FA DE <id> on a conditioned station.
    AL_SIZE = 0x80
    COND_AT = 0x7C
    COND_MARK = 0xFADE

    def read_alignment(self, progress=None):
        """The station's 128-byte SA partition: the alignment values, then the conditioning
        marker at 0x7C..0x7E.  Read-only (RD AL <start> <end>, hex, inclusive).

        RD AL reads the RAM copy of the block at [0x0806AE94]+0x600 (0x0838FAF8 on the lab
        station), not the NVM partition, so it costs no NVM read - and bytes written with
        write_alignment only show here after a RESET re-reads the partition."""
        return self._read_area("AL", self.AL_SIZE, progress)

    def conditioning_state(self):
        """(conditioned, mark, id_byte) from SA[0x7C..0x7E], one read-only RD AL.

        conditioned is mark == 0xFADE: the firmware's second test, SA[0x7E] == the EEPROM
        header's byte 2, cannot be made over the MMI (the header is not in any RD area), but the
        byte stored at SA[0x7E] is the copy it compares against, and a station whose marker word
        is FADE has passed both tests at its last boot or written the marker after conditioning.

        None on the comparator: its MMI has no RD AL and no conditioning step."""
        if self.is_comparator:
            return None
        from .codeplug import s2_parse
        start, end = self.COND_AT, self.COND_AT + 2
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw("RD AL %X %X" % (start, end), 5.0)
        e = _station_error(body)
        if e:
            raise e
        data = bytearray()
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("S2"):
                continue
            addr, chunk = s2_parse(line)
            if addr != start + len(data) or len(data) + len(chunk) > 3:
                raise StationError("RD AL %X %X: record for %X" % (start, end, addr))
            data += chunk
        if len(data) != 3:
            raise StationError("RD AL %X %X: %d of 3 bytes" % (start, end, len(data)))
        mark = int.from_bytes(data[0:2], "big")
        return mark == self.COND_MARK, mark, data[2]

    # The station's other EEPROM statement is much older and much ruder: "ERASE EEPROM" (handler
    # 0x0ABE3A) wants five YES tokens, then fills 8192 bytes with 0xFF and writes the lot - the
    # codeplug, the wildcard and the alignment values all go.  WinRSS R14.13 never sends it (it
    # is not in ASCII_CMDS_RULES_TABLE); it is here because the station implements it.
    ERASE_EEPROM_CONFIRM = "ERASE EEPROM YES YES YES YES YES"

    def erase_eeprom(self, timeout=120.0):
        """Erase the whole 8 KB EEPROM to 0xFF.  DESTRUCTIVE: the station comes back with no
        codeplug, no wildcard and no alignment; archive the codeplug first, write it back and
        re-align afterwards.

        Tested on the station 2026-11-07: it answers in ~3 s with "...my mind is going...", keeps
        the RSS: prompt, and after a RESET comes back in <<RSS_ONLY_MODE>> ("NVM initialization
        not completed", GET CP SIZE = 0).  The MMI still takes NEW/WR/MOVE for CP, WC and AL, so
        writing the archive back and resetting restores it without WinRSS (the codeplug's ten
        runtime-statistics bytes at 0x9A..0xA3 come back zeroed).  qrss never calls this without
        an explicit operator action."""
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw(self.ERASE_EEPROM_CONFIRM, timeout)
        e = _station_error(body)
        if e:
            raise e
        return body

    def read_codeplug(self, progress=None, fast=True):
        """The station's whole codeplug.  With fast=True (the default) it comes out of the RAM copy
        through RD MCS, ~0.7 s instead of ~4.1 s for 1444 bytes at 115200; anything unexpected in
        the RAM image (a different firmware's layout, a short read) falls back to RD CP.

        RD CP spends ~0.4 s on every codeplug command before it answers, whatever the baud rate, so
        few large requests are what makes it fast (1444 bytes: 7 commands, not 46 of 32 bytes).
        It answers a request with 32-byte S2 records, plus an S8 record when there are several;
        every record's checksum and address are checked.  progress(done, size) after each request."""
        if fast and not self.is_comparator:    # the comparator's MMI has no RD MCS
            try:
                return self._read_codeplug_ram(progress=progress)
            except (StationError, ValueError):
                pass
        size = int(self.get("GET CP SIZE"))     # also reads NVM: ~0.4 s
        return self._read_area("CP", size, progress)

    def read_wildcard(self, progress=None, fast=True):
        """The station's WildCard block - the second S2 section of a .cp.

        The comparator has no wildcard block (its .cp archives carry one S2 section only), so
        asking one raises StationError.  With fast=True (the default) it comes out of RAM like
        the codeplug: the block is at
        [0x0806AE90], byte for byte as RD WC returns it (no relocation, no live byte - checked
        against the station), and RD WC costs ~0.2 s per command.  Anything unexpected falls
        back to RD WC.  A station with no wildcard answers GET WC SIZE with 0."""
        if self.is_comparator:
            raise StationError("the comparator has no wildcard block")
        if fast:
            try:
                return self._read_wildcard_ram(progress=progress)
            except (StationError, ValueError):
                pass
        size = int(self.get("GET WC SIZE"))
        if size <= 0:
            raise StationError("the station reports no wildcard block")
        return self._read_area("WC", size, progress)

    def _read_wildcard_ram(self, progress=None):
        """The wildcard block out of RAM.  Raises StationError when the RAM does not look like a
        wildcard block (so read_wildcard falls back to RD WC)."""
        w = int.from_bytes(self.mcs_read(self.CP_RAM_PTR - self.MCS_BASE, 4), "big")
        if not 0x08000000 <= w < 0x08400000:
            raise StationError("no wildcard block: [%08X] = %08X" % (self.CP_RAM_PTR, w))
        hdr = self.mcs_read(w - self.MCS_BASE, 6)       # header first: the block is small
        size = int.from_bytes(hdr[4:6], "big")
        if not 2 <= size <= 0x10000 or int.from_bytes(hdr[2:4], "big"):
            raise StationError("no wildcard block at %08X" % w)
        img = bytearray(self.mcs_read(w - self.MCS_BASE, size, progress))
        if len(img) != size:
            raise StationError("RD MCS: %d of %d bytes" % (len(img), size))
        return img

    def _read_area(self, area, size, progress=None):
        """RD <area> <start> <end> in CP_CHUNK-byte requests.  The station answers with 32-byte S2
        records (and an S8 after them when there are several); every record's address and
        checksum are checked."""
        from .codeplug import s2_parse
        img = bytearray(size)
        with self.lock:
            self.ensure("RSS:")
            start = 0
            while start < size:
                end = min(start + self.CP_CHUNK, size) - 1     # never past the end: RD does not check
                body, _ = self.raw("RD %s %X %X" % (area, start, end), 5.0)
                e = _station_error(body)
                if e and e.code == "0030":
                    # "?(0030) must request %d bytes or fewer": the comparator's limit is
                    # smaller than the Quantar's 214, and the reply names it.  Learn it and
                    # retry the same block; a station that keeps refusing falls back to 32.
                    m = re.search(r'(\d+)\s+bytes or fewer', str(e))
                    want = int(m.group(1)) if m else 32
                    if want >= self.CP_CHUNK:
                        want = self.CP_CHUNK // 2
                    self.CP_CHUNK = max(16, want)
                    continue
                if e:
                    raise e
                at = start
                for line in body.splitlines():
                    line = line.strip()
                    if not line.startswith("S2"):
                        continue
                    addr, data = s2_parse(line)
                    if addr != at or addr + len(data) > end + 1:
                        raise StationError("RD %s %X %X: record for %X, expected %X" % (area, start, end, addr, at))
                    img[addr:addr + len(data)] = data
                    at += len(data)
                if at != end + 1:
                    raise StationError("RD %s %X %X: %d of %d bytes"
                                       % (area, start, end, at - start, end + 1 - start))
                if progress:
                    progress(end + 1, size)
                start = end + 1
        return img

    def _read_codeplug_ram(self, size=None, progress=None):
        """The codeplug out of the application's RAM copy, relocation undone.  Raises StationError
        if the RAM does not look like the codeplug, so read_codeplug can fall back to RD CP.

        With size=None the length comes from the image's own header, so no GET CP SIZE is needed
        (that command reads NVM too, ~0.4 s); a size given must match it."""
        w = int.from_bytes(self.mcs_read(self.CP_RAM_PTR - self.MCS_BASE, 4), "big")
        base = w + self.CP_RAM_SKIP
        img = bytearray(self.mcs_read(base - self.MCS_BASE, self.CP_CHUNK))
        want = int.from_bytes(img[4:6], "big")
        if not 2 <= want <= 0x10000:
            raise StationError("no codeplug image at %08X" % base)
        if size is not None and want != size:
            raise StationError("codeplug at %08X is %d bytes, station says %d" % (base, want, size))
        size = want
        start = len(img)
        if start < size:
            if progress:
                progress(min(start, size), size)
            img += self.mcs_read(base - self.MCS_BASE + start, size - start,
                                 (lambda d, n: progress(min(start + d, size), size)) if progress else None)
        del img[size:]
        if len(img) != size:
            raise StationError("RD MCS: %d of %d bytes" % (len(img), size))
        # the image starts checksum, 0000, size, version, section count (0xB65B on the lab station
        # only because that codeplug's checksum is 0xB65B), then the directory
        if int.from_bytes(img[2:4], "big") or int.from_bytes(img[4:6], "big") != size:
            raise StationError("no codeplug image at %08X" % base)
        ndir = int.from_bytes(img[8:10], "big")
        if not 2 <= ndir <= 64:
            raise StationError("no codeplug image at %08X" % base)
        for i in range(0, size - 3, 2):          # pointers are 2-byte aligned, 4 bytes wide
            v = int.from_bytes(img[i:i + 4], "big")
            if base <= v < base + size:
                img[i:i + 4] = (v - base).to_bytes(4, "big")
        for k in range(ndir):
            at = 10 + 4 * k
            if int.from_bytes(img[at:at + 4], "big") & 0x7FFFFFFF >= size:
                raise StationError("no codeplug image at %08X" % base)
        for k in range(ndir):
            at = 10 + 4 * k                       # the directory: mark a list of lists again
            t = int.from_bytes(img[at:at + 4], "big")
            n = int.from_bytes(img[t:t + 2], "big")
            end = t + 2 + 4 * n
            ptrs = [int.from_bytes(img[t + 2 + 4 * j:t + 6 + 4 * j], "big") for j in range(n)]
            if n and end <= size and ptrs[0] == end and ptrs == sorted(set(ptrs)) and ptrs[-1] < size:
                img[at:at + 4] = (t | 0x80000000).to_bytes(4, "big")
        img[0:2] = (sum(img[2:]) & 0xFFFF).to_bytes(2, "big")   # the stored word is stale here
        return img

    # Writing: WinRSS (rsscore.dll FUN_100210f0 / FUN_10020760) sends the image in 32-byte blocks,
    # one Motorola S2 record per WR CP, and retries a record up to 3 times.  The firmware's record
    # parser (sc.o 0x9CBFC) would take a bigger block - its length is the S2 LL byte, up to 251 data
    # bytes - but there is no reason to deviate from what WinRSS has always used, and writing is the
    # one direction that can brick a codeplug, so match it exactly.  The 214-byte batching is RD CP
    # (read) only; it has nothing to do with writing.
    WR_CHUNK = 32

    def write_codeplug(self, img, progress=None, move=True, chunk=None):
        """Write a whole codeplug image to the station as WinRSS does: NEW CP <size>, then WR CP in
        `chunk`-byte S2 records (default WR_CHUNK = 32, as WinRSS; each retried up to 3 times), then
        - if move - MOVE CP to commit it from RAM to NVM.  Any error aborts the download with STOP CP
        and is re-raised, so a failed write never half-commits.  progress(done, size) after each record.

        This CHANGES the station's configuration; the caller must have the operator's approval
        (qrss never calls it without a confirmed user action).  Writing the exact bytes read back
        from the same station is a safe way to exercise the path: the committed codeplug is identical."""
        return self._write_area("CP", img, progress, move, chunk)

    def write_wildcard(self, img, progress=None, move=True, chunk=None):
        """Write the WildCard block the same way (NEW WC / WR WC / MOVE WC / STOP WC).

        The verbs are shared with the codeplug in the application: the NEW handler compares
        token[1] with "CP" then "WC" (NEW 0x9BBFC), WR with "AL"/"CP"/"WC" (0xCDFD2, one S-record
        parser at 0x9CBFC), MOVE (0xABA72) and STOP (0xC27E8) likewise.  So the same rules hold:
        54 data bytes is the most a WR line can carry (the ~126-character MMI line buffer), and a
        second NEW without a STOP is refused - for WC with ?(0084) "wildcard area already
        allocated" instead of the codeplug's ?(0002).  See write_codeplug for the approval rules."""
        return self._write_area("WC", img, progress, move, chunk)

    WR_ALREADY = {"CP": ("2", "0002"), "WC": ("84", "0084"), "AL": ("1", "0001")}

    def write_alignment(self, img, move=True, chunk=None):
        """Write the whole 128-byte SA partition (NEW AL 128 / WR AL / MOVE AL / STOP AL).

        The AL area uses the codeplug's verbs and handlers: NEW compares "AL" first (0x9BBFC,
        ?(0001) alignment area already allocated), WR's S-record parser is the same (0xCDFD2),
        and MOVE AL (0xABA72) writes the staging buffer over the "SA" partition through
        0x0E2E80 with a fixed length of 0x80 - the same write COND_NVM_PARTITIONS uses.
        PROVEN ON THE STATION 2026-11-07: the identical 128 bytes written back read back byte
        for byte, and a changed marker (SA[0x7C..0x7D] = 0000) survived the write and made the
        station condition its EEPROM at the next reset.

        RD AL shows the RAM copy, so a change is only visible after a RESET.  This CHANGES the
        station's alignment values; the caller must have the operator's approval."""
        img = bytes(img)
        if len(img) != self.AL_SIZE:
            raise ValueError("the alignment area is %d bytes, not %d" % (self.AL_SIZE, len(img)))
        return self._write_area("AL", img, None, move, chunk)

    def _write_area(self, area, img, progress=None, move=True, chunk=None):
        from .codeplug import s2_line
        img = bytes(img)
        chunk = chunk or self.WR_CHUNK
        already = self.WR_ALREADY[area]
        with self.lock:
            self.ensure("RSS:")
            body, _ = self.raw("NEW %s %d" % (area, len(img)))
            e = _station_error(body)
            if e and str(e.code) in already:
                # "area already allocated" - a prior MOVE or an aborted write left it; the station
                # keeps the staging area until STOP <area>.  Clear it and allocate again.
                self.raw("STOP %s" % area)
                body, _ = self.raw("NEW %s %d" % (area, len(img)))
                e = _station_error(body)
            if e:
                raise e
            try:
                for off in range(0, len(img), chunk):
                    rec = s2_line(off, img[off:off + chunk])
                    for attempt in range(3):
                        body, _ = self.raw("WR %s " % area + rec)
                        e = _station_error(body)
                        if e is None:
                            break
                        if attempt == 2:
                            raise e
                    if progress:
                        progress(min(off + chunk, len(img)), len(img))
                if move:
                    body, _ = self.raw("MOVE %s" % area, 15.0)    # RAM -> NVM; give it room
                    e = _station_error(body)
                    if e:
                        raise e
            except BaseException:
                try:
                    self.raw("STOP %s" % area)                # abort the half-written RAM area
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------------ whole-station backup
    def read_backup(self, progress=None):
        """The station's codeplug, wildcard block and SA (alignment) partition, as a
        qrss.backup.StationBackup.  All three are read-only paths; the codeplug uses the RAM
        fast path, so this is ~2 s on a station at 9600.  progress(done, total) is the
        codeplug read's, in bytes."""
        from .backup import StationBackup
        meta = {}
        for cmd, key in (("GET HW_VER", "hw"), ("GET FW_VER SC", "fw"), ("GET STN SN", "sn")):
            try:
                meta[key] = self.get(cmd)
            except StationError:
                pass
        parts = {}
        try:
            parts['codeplug'] = bytes(self.read_codeplug(progress=progress))
        except StationError:
            parts['codeplug'] = None
        try:
            parts['wildcard'] = bytes(self.read_wildcard())
        except StationError:
            parts['wildcard'] = None             # a station with no wildcard block
        try:
            parts['alignment'] = bytes(self.read_alignment())
        except StationError:
            parts['alignment'] = None
        if not parts['codeplug']:
            raise StationError("the station has no codeplug to back up")
        return StationBackup(parts['codeplug'], parts['wildcard'], parts['alignment'], meta)

    def write_backup(self, bk, progress=None):
        """Write a StationBackup back: the codeplug and wildcard are staged, then committed
        together, then the alignment partition is written.  The station must be RESET afterwards
        for the new codeplug and alignment to take effect (Link.reset).

        This CHANGES the station's configuration, alignment included; the caller must have the
        operator's approval.  Every path here has been proven on the station (CP and WC writes
        2026-11-05/06, AL 2026-11-07)."""
        if bk.wc is not None:
            self.write_codeplug(bk.cp, move=False, progress=progress)
            self.write_wildcard(bk.wc, move=False)
            self.call("MOVE CP", timeout=15.0)      # RAM -> NVM; give it room
            self.call("MOVE WC", timeout=15.0)
        else:
            self.write_codeplug(bk.cp, move=True, progress=progress)
        if bk.sa is not None:
            self.write_alignment(bk.sa, move=True)

    # ------------------------------------------------------------------ real-time streams
    def stream_start(self, cmd, session="RAP:"):
        """Start a real-time stream: an MTR/FPM stream in RAP: mode (the default), or the
        comparator's Local Status session (DOLS, `session="LS:"`).

        When the command *is* the mode command (DOLS), entering the session is the stream's
        first output - the comparator's initial status dump arrives as its reply - so the
        dump is kept in the receive buffer for stream_lines() instead of being swallowed."""
        with self.lock:
            if self.streaming:
                self.stream_stop()
            self._stream_err = None
            if cmd == MODE_CMD.get(session):
                if self.prompt == session:      # already in the session: it streams by itself
                    self.streaming, self.stream_session = cmd, session
                    self._rx = ""
                    return
                self.ensure("]-O")
                self._flush()
                self._write(cmd)
                self.streaming, self.stream_session = cmd, session
                body, p = self._read_until_prompt(4.0)
                if p != session:
                    self.streaming = None
                    raise StationError("station did not enter %s mode (got %r)" % (session, p))
                self._rx = body + self._rx
                return
            self.ensure(session)
            self._rx = ""
            self._write(cmd)
            self.streaming = cmd
            self.stream_session = session

    def _stream_interrupt(self):
        self._write("")
        time.sleep(0.15)

    def stream_lines(self):
        """Return complete (key, value) pairs received since the last call.

        The station prints its RAP: prompt after every value and, once each key has been
        sent, repeats only the values that change.  A "?(nnnn) text" line (e.g. "receiver
        board disabled") raises StationError once.

        In the comparator's Local Status session the lines are positional, not KEY = VALUE
        (OC.img 0x20c70-0x20d4e): "AL %02d <alias>", "HA %02d %02x", "LD %02d %03d",
        "S%01x <16 hex>", "LCM ON|OFF", "FV %02d", "VV %02d" (plus "RS %02x" and
        "TG %05x %05x", which WinRSS ignores).  Those come back as (first token, rest),
        e.g. ("S1", "0123456789ab0000"), ("LD", "01 003")."""
        out = []
        with self.lock:
            if not self.streaming:
                return out
            if self._stream_err:
                e, self._stream_err = self._stream_err, None
                raise e
            new = self._read()
            if new:
                self.log('<', new)
            if "Password:" in self._rx:
                # RSS manual 8, "Password Time-out": after 10 minutes without user activity the
                # station's own timer expires and it drops the session back to the Password:
                # prompt, which ends any real-time stream.  Answer it (the prompt and the
                # password are handled like any other) and tell the screen, which restarts the
                # stream when it is next opened.
                self.streaming = None
                self._rx = ""
                self._answer_password(2.5)
                raise StationError("the station's password time-out ended the session "
                                   "(10 minutes idle) - reopen the screen to restart it")
            *lines, self._rx = self._rx.replace('\r', '\n').split('\n')
        err = None
        ls = self.stream_session == "LS:"
        for l in lines:
            l = l.strip()
            for p in PROMPTS:
                if l.startswith(p):
                    l = l[len(p):].strip()
            if not l:
                continue
            if l.startswith("?("):
                err = err or _station_error(l)
                continue
            if ls:
                k, _, v = l.partition(' ')
                out.append((k, v.strip()))
            elif '=' in l:
                k, v = l.split('=', 1)
                if k.strip():
                    out.append((k.strip(), v.strip()))
        if err:
            if not out:
                raise err
            self._stream_err = err
        return out

    def local_status(self, settle=2.0):
        """The comparator's Local Status, read once: DOLS, GET LD, `settle` seconds listening
        for the lines it prints by itself, EXIT.  Returns the (kind, rest) lines as
        stream_lines gives them.  Read-only (it does not turn local control on)."""
        with self.lock:
            self.stream_start("DOLS", session="LS:")
            out = self.stream_lines()          # DOLS's reply is the initial dump
            self.stream_cmd("GET LD")
            end = time.time() + settle
            while time.time() < end:
                out += self.stream_lines()
                time.sleep(0.05)
            self.stream_stop()
        return out

    def stream_cmd(self, cmd):
        """Send a command while a stream runs (FP ... / RC SET ... in RAP:, GET LD /
        LCM ON|OFF / FVP|VDP <n> ON|OFF in the comparator's LS: session)."""
        with self.lock:
            self._stream_interrupt()
            self._write(cmd)

    def stream_stop(self):
        with self.lock:
            if not self.streaming:
                return
            # Drop what has already arrived: it ends with a prompt, and _read_until_prompt would
            # take that as the answer to STOP/EXIT without waiting for the real one.
            self._read()
            if "Password:" in self._rx:        # the 10-minute time-out beat us to it
                self._rx = ""
                self.streaming, self.stream_session = None, "RAP:"
                try:
                    self._answer_password(2.5)
                except StationError:
                    pass
                return
            self._rx = ""
            self._write("STOP" if self.stream_session == "RAP:" else "EXIT")
            self.streaming = None
            self.stream_session = "RAP:"
            self._read_until_prompt(2.5)
            self._rx = ""
