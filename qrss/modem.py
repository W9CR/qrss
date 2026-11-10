"""Dial-up access to a station through a PC modem (RSS manual chapter 9).

WinRSS runs a Hayes modem on the same serial port the direct connection uses: initialize it,
dial the station's number, wait for CONNECT, and from then on the port is a transparent
9600 8N1 link to the station's MMI.  Everything here mirrors rss.comm.protocol.ascii's
SerialAsciiOperation (initModem / dialModem / hangupModem) and the defaults in
ModemConfiguration:

    init    ATZ, ATE0, ATV1, AT&D2, then the configured init string (ATE0Q0M0V1X4&K0S0=0)
    dial    ATS7=<wait for connect>, then ATDT + [call-waiting prefix,] + [outside line,] + number
    hangup  drop DTR, "+++ATH0", restore DTR, close

The dial answers WinRSS watches for are CONNECT (good), BUSY, and the four failures
NO CARRIER / NO DIALTONE / ERROR / TIMEOUT.  A wrong answer is an error, not a hang.

The station's own modem is fixed: the RSS manual gives its setup string as
ATE0Q1&D0&K0N0S37=9S0=1 (auto-answer, 9600, no flow control), so nothing here configures
the far end.
"""
import time


class ModemError(Exception):
    """A dial failed.  `stage` is "init", "dial" or "hangup"; `reply` is what the modem said."""

    def __init__(self, msg, stage=None, reply=""):
        super().__init__(msg)
        self.stage = stage
        self.reply = reply


DEFAULTS = {
    "init": "ATE0Q0M0V1X4&K0S0=0",     # ModemConfiguration.MODEM_INIT_STRING
    "prefix": "ATDT",                  # MODEM_DIAL_PREFIX
    "hangup": "+++ATH0",               # MODEM_HANGUP_STRING
    "drop_dtr": True,                  # MODEM_DROP_DTR = ENABLED
    "wait": 60,                        # MODEM_WAIT_CONNECT, seconds
    "pause": 5,                        # MODEM_REDIAL_PAUSE, seconds
    "outside": False,                  # MODEM_OUTSIDE_LINE_SET
    "outside_val": "9",                # MODEM_OUTSIDE_LINE_VAL
    "callwait": False,                 # MODEM_CALL_WAITING_SET
    "callwait_val": "*70",             # MODEM_CALL_WAITING_VAL[0]
    "connect": "CONNECT",              # MODEM_CONNECT_RESP
    "busy": "BUSY",                    # MODEM_BUSY_RESP
    "fail": ["NO CARRIER", "NO DIALTONE", "ERROR", "TIMEOUT"],   # MODEM_FAIL1..4_RESP
}

FAIL_TEXT = {
    "NO CARRIER": "no carrier",
    "NO DIALTONE": "no dial tone",
    "ERROR": "the modem returned ERROR",
    "TIMEOUT": "the modem timed out",
}


def config(cfg=None):
    c = dict(DEFAULTS)
    if cfg:
        c.update({k: v for k, v in cfg.items() if v is not None})
    return c


class Modem:
    """One dial-up session.  `dial()` returns the open port, ready to hand to station.Link;
    `hangup()` drops the call.  `log(direction, text)` gets every AT exchange, like the
    terminal log does for the MMI."""

    def __init__(self, port, baud=9600, number="", cfg=None, log=None):
        self.port = port
        self.baud = int(baud)
        self.number = (number or "").strip()
        self.cfg = config(cfg)
        self.log = log or (lambda d, t: None)
        self.s = None

    # ------------------------------------------------------------------ low level
    def _open(self):
        import serial
        self.s = serial.Serial(self.port, self.baud, bytesize=8, parity='N', stopbits=1,
                               timeout=0.1, xonxoff=False, rtscts=False)

    def _send(self, text):
        self.log('>', text)
        self.s.write(text.encode('latin1') + b"\r")

    def _expect(self, timeout, want=("OK",)):
        """Read until one of `want` (or a failure string) arrives, or `timeout` seconds pass.
        Returns the whole reply with the echo removed."""
        end = time.time() + timeout
        buf = ""
        while time.time() < end:
            b = self.s.read(256)
            if b:
                buf += b.decode('latin1', 'replace')
                up = buf.upper()
                for w in want:
                    if w in up:
                        self.log('<', buf)
                        return buf
            else:
                time.sleep(0.02)
        self.log('<', buf)
        return buf

    def _at(self, cmd, timeout=10):
        self._send(cmd)
        r = self._expect(timeout)
        if "OK" not in r.upper():
            raise ModemError("the modem did not accept %s" % cmd, "init", r)
        return r

    # ------------------------------------------------------------------ the session
    def dial(self):
        """Initialize, dial, and wait for CONNECT.  Returns the open serial port; on failure
        the port is closed and ModemError says which stage and what the modem answered."""
        c = self.cfg
        if not self.number:
            raise ModemError("no phone number to dial", "dial")
        try:
            self._open()
            # WinRSS sends these four, then the configured init string, waiting for OK each time
            self._at("ATZ")
            self._at("ATE0")
            self._at("ATV1")
            self._at("AT&D2")
            self._at(c["init"])
            self._at("ATS7=%d" % int(c["wait"]))
            dial = c["prefix"]
            if c["callwait"]:
                dial += str(c["callwait_val"]) + ","
            if c["outside"]:
                dial += str(c["outside_val"]) + ","
            dial += self.number
            self._send(dial)
            r = self._expect(int(c["wait"]), want=tuple([c["connect"], c["busy"]] + c["fail"]))
        except ModemError:
            self._close()
            raise
        except Exception as e:
            self._close()
            raise ModemError(str(e), "dial")
        up = r.upper()
        if c["connect"].upper() in up:
            return self.s
        self._close()
        if c["busy"].upper() in up:
            raise ModemError("the station's line was busy", "dial", r)
        for f in c["fail"]:
            if f.upper() in up:
                raise ModemError(FAIL_TEXT.get(f, "the modem returned %s" % f), "dial", r)
        raise ModemError("no answer from the modem within %d s" % int(c["wait"]), "dial", r)

    def hangup(self):
        """Drop the call the way WinRSS does: DTR down (if configured), the escape and hangup
        string, DTR back, and the port closed.  Quiet about a port that is already gone."""
        c = self.cfg
        if self.s is None:
            return
        try:
            if c["drop_dtr"]:
                try:
                    self.s.dtr = False
                except Exception:
                    pass
            self._send(c["hangup"])
            self._expect(3, want=("OK", "NO CARRIER"))
            try:
                self.s.dtr = True
            except Exception:
                pass
        except Exception:
            pass
        finally:
            self._close()

    def _close(self):
        try:
            if self.s is not None:
                self.s.close()
        except Exception:
            pass
        self.s = None
