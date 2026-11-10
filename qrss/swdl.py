"""Firmware download (SWDL) to a Quantar station control module over the serial port.

Follows WinRSS (rss.swdl.SwdlOperationHandler, serial connection) step by step, but uses
qrss.netstack for SLIP/IP/TCP/FTP instead of Windows dial-up networking.

Package directory (as shipped by Motorola):
  SWDL.INI   sections [boot2], [app], [wireline] - each with STN_FILE, IMAGE_VER, SWDL_MODE,
             FLASH_MODE, FTP_CMD_FILE, VER_OUTPUT_FILE, AUTO_IP, BPID_FILE, RESET (+ PC_FILE)
  FTP_CMD_FILE  ftp script: "put <pc file> <station path>" lines; *.cmp/*.bin are firmware
             (sent binary), the last other put is the transfer command file (sent ASCII,
             e.g. copy_all -> /dev/cmd/copy_all: "CP /dev/ram/sc.o /dev/flash/sc.o" ... "DONE")
  BPID       "DECRYPT <token> <token> ..." backplane ids, one must match the station

Per section, when the station answers at its ASCII prompt (normal mode):
  1. GET FW_VER <STN_FILE>            current version (boot2 is skipped if already current)
                                      the comparator (ASTRO-TAC / OmniTAC) has no GET FW_VER
                                      <file>: there it is GET OC <n> SW VER OC|B1|B2 and
                                      GET OC <n> WL <m> SW VER WL, and no timestamp at all
  2. CHECK ID <token>                 for each BPID token until one is accepted
  3. SWDL <SWDL_MODE> <yyyy/MM/dd HH:mm> SLIP <baud>
                                      -> "FLASH ME <station ip> <pc ip>", station reboots into
                                         its boot loader speaking SLIP at <baud>
  4. ping the station over SLIP (up to 60 s)
  5. FTP (anonymous, active mode): binary put of each firmware file to /dev/ram/...,
     then ASCII put of the transfer command file to /dev/cmd/...  -> the boot loader copies
     RAM to flash and resets
  6. wait for the station to come back (ASCII prompt or ping), then GET FW_VER to verify
When the station is already in boot mode (no prompt) steps 1-3 are done over FTP instead:
/dev/cmd/fm_ver (GET_FILE_INFO), /dev/cmd/bpid (DECRYPT ...), /dev/cmd/swdl (SWDL <time>),
/dev/cmd/flash_mode (<FLASH_MODE>), results read back from /dev/ram/*.

Ethernet (WinRSS' Ethernet connection, RSS manual 11-35; `Downloader(..., ethernet=IP)` or
`--ethernet IP`): no serial port and no SLIP.  Every step is FTP to the station's own IP, in
the same order - /dev/cmd/fm_ver, /dev/cmd/bpid, /dev/cmd/swdl, then /dev/cmd/flash_mode to
put it into its boot loader, the images and the transfer command file to the boot loader over
the same FTP, then the wait for it to program, reset and answer again, and the version check.
"""
import os, re, struct, time

from .netstack import FtpClient, NetError, Stack, SocketStack, FRONT
from .station import Link, StationError, STATION_ACTUAL_BAUD


# The SCM's baud-rate generator runs from 25 MHz (BOOT2 table at image 0x3C57E: BRGC2 =
# 0x10000 | value, CD = value >> 1, rate = 25e6 / 16 / (CD + 1)).  Up to 57600 that is within
# 1 %, but "115200" comes out at 111607 (-3.1 %), so the PC side is set to the real rate
# (STATION_ACTUAL_BAUD, in station.py).  115200 needs H026.11.05a or later (application and
# BOOT2 accept-lists patched).


class SwdlError(Exception):
    pass


# ---------------------------------------------------------------------- package
def _find(dirpath, name):
    """Case-insensitive file lookup (packages mix SWDL.INI / ftp_cmds / B2_14015.cmp)."""
    p = os.path.join(dirpath, name)
    if os.path.exists(p):
        return p
    low = name.lower()
    for f in os.listdir(dirpath):
        if f.lower() == low:
            return os.path.join(dirpath, f)
    raise SwdlError("%s: file %s not found" % (dirpath, name))


def cmp_version(data):
    """Version from a .cmp/.img QUANTAR header: letter at 0x3F, BCD-ish digits at 0x38.
    The comparator's images carry an OMNITAC header with no version digits (the version is
    the package's IMAGE_VER and the image's own "OC_R.." string), so they answer None."""
    if data[:7] != b"QUANTAR" or len(data) < 0x40:
        return None
    n = data[0x38:0x3C].hex()
    letter = chr(data[0x3F]) if 0x41 <= data[0x3F] <= 0x5A else "?"
    return "%s%s.%s.%s" % (letter, n[0:3], n[3:5], n[5:8])


def file_sum32(d):
    n = len(d) // 4
    return sum(struct.unpack('>%dI' % n, d[:n * 4])) & 0xFFFFFFFF


STAMP_FMT = "%Y/%m/%d %H:%M"


def parse_fw_info(text):
    """'FW_VER = <ver> TIMESTAMP = <yyyy/mm/dd hh:mm>' -> (ver, timestamp string or None)."""
    m = re.search(r'FW_VER\s*=\s*(\S+)', text or "")
    t = re.search(r'TIMESTAMP\s*=\s*(\d{4}/\d\d/\d\d \d\d:\d\d)', text or "")
    return (m.group(1) if m else None), (t.group(1) if t else None)


def version_matches(dev, want):
    """WinRSS compareVersion: drop the leading letter(s), compare the rest."""
    strip = lambda s: re.sub(r'^[^0-9]+', '', (s or '').strip()).lower()
    return bool(strip(dev)) and strip(dev) == strip(want)


class Section:
    def __init__(self, name, kv, pkgdir):
        self.name, self.kv, self.dir = name, kv, pkgdir
        self.stn_file = kv.get('STN_FILE', '')
        self.image_ver = kv.get('IMAGE_VER', '')
        self.swdl_mode = kv.get('SWDL_MODE', '')
        self.flash_mode = kv.get('FLASH_MODE', '')
        self.ver_out = kv.get('VER_OUTPUT_FILE', '')
        self.bpid_file = kv.get('BPID_FILE', '')
        self.reset = kv.get('RESET', '1') not in ('0', '')
        self.firmware = []          # (pc path, station path)
        self.transfer = None        # (pc path, station path)
        missing = [k for k in ('STN_FILE', 'IMAGE_VER', 'SWDL_MODE', 'FLASH_MODE', 'FTP_CMD_FILE',
                               'VER_OUTPUT_FILE', 'BPID_FILE') if k not in kv]
        if missing:
            raise SwdlError("[%s]: missing %s" % (name, ", ".join(missing)))
        for line in open(_find(pkgdir, kv['FTP_CMD_FILE']), encoding='latin1'):
            t = line.split()
            if len(t) >= 3 and t[0].lower() == 'put':
                if t[1].lower().endswith(('.cmp', '.bin')):
                    self.firmware.append((_find(pkgdir, t[1]), t[2]))
                else:
                    self.transfer = (_find(pkgdir, t[1]), t[2])
        if not self.firmware or not self.transfer:
            raise SwdlError("[%s]: %s has no firmware or no transfer command put" % (name, kv['FTP_CMD_FILE']))

    def bpid_tokens(self):
        t = open(_find(self.dir, self.bpid_file), encoding='latin1').read().split()
        if not t or t[0].upper() != "DECRYPT":
            raise SwdlError("%s does not start with DECRYPT" % self.bpid_file)
        return t[1:]

    def describe(self):
        out = ["[%s]  %s  IMAGE_VER %s  mode %s / %s" % (self.name, self.stn_file, self.image_ver,
                                                         self.swdl_mode, self.flash_mode)]
        for pc, stn in self.firmware:
            d = open(pc, 'rb').read()
            v = cmp_version(d)
            s = file_sum32(d)
            out.append("   put %-14s -> %-18s %7d bytes  header version %s  file sum %s"
                       % (os.path.basename(pc), stn, len(d), v or "?", "OK" if s == 0 else "%08X (BOOT2 WILL REJECT IT)" % s))
        pc, stn = self.transfer
        out.append("   then %-13s -> %s:  %s" % (os.path.basename(pc), stn,
                                                 " / ".join(l.strip() for l in open(pc, encoding='latin1') if l.strip())))
        return "\n".join(out)


def parse_package(pkgdir):
    ini = _find(pkgdir, "SWDL.INI")
    sections, cur, kv = [], None, {}
    for line in open(ini, encoding='latin1'):
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        if line.startswith('['):
            if cur:
                sections.append(Section(cur, kv, pkgdir))
            cur, kv = line.strip('[]').lower(), {}
        elif '=' in line and cur:
            k, v = line.split('=', 1)
            kv[k.strip().upper()] = v.strip()
    if cur:
        sections.append(Section(cur, kv, pkgdir))
    if not sections:
        raise SwdlError("no sections in %s" % ini)
    return sections


def validate_package(sections):
    """Problems that would make the flash fail (or WinRSS refuse it)."""
    problems = []
    for s in sections:
        for pc, _ in s.firmware:
            d = open(pc, 'rb').read()
            if d[:7] not in (b"QUANTAR", b"OMNITAC"):
                problems.append("%s: no QUANTAR/OMNITAC header" % os.path.basename(pc))
            elif file_sum32(d) != 0:
                problems.append("%s: whole-file sum32 is not 0, BOOT2's CP would silently delete it"
                                % os.path.basename(pc))
    return problems


# ---------------------------------------------------------------------- download
class Downloader:
    """Runs the download on an already-open serial port object (pyserial or Wire)."""

    def __init__(self, port, sections, baud=9600, ascii_baud=None, station_port=FRONT,
                 log=None, progress=None, confirm=None, dry_run=False, abort=None,
                 ethernet=None, ftp_port=21):
        self.port, self.sections = port, sections
        self.baud = baud                    # SLIP baud requested from the station
        self.ascii_baud = ascii_baud        # baud of the normal ASCII port (None = leave)
        self.station_ip, self.pc_ip = station_port
        self.ethernet = ethernet            # station IP for a download over the network, no serial
        self.ftp_port = ftp_port
        self.log = log or print
        self.progress = progress or (lambda frac, text: None)
        self.confirm = confirm or (lambda text: True)
        self.dry = dry_run
        self.abort = abort or (lambda: False)
        self.stack = None
        self.link = Link(port, log=lambda d, t: None) if port is not None else None
        self.stamp = time.strftime(STAMP_FMT)

    # -- helpers ----------------------------------------------------------------
    def _note_station_stamp(self, ts):
        """The station refuses a SWDL timestamp equal to or older than its own (INVALID_TIMESTAMP).
        If it already holds a newer one (e.g. stamped by a PC whose clock was ahead), use that + 1 min."""
        if not ts or ts.startswith("0000"):
            return
        try:
            st = time.mktime(time.strptime(ts, STAMP_FMT))
            ours = time.mktime(time.strptime(self.stamp, STAMP_FMT))
        except ValueError:
            return
        if st >= ours:
            new = time.strftime(STAMP_FMT, time.localtime(st + 60))
            self.log("station timestamp %s is not older than ours (%s) - using %s so the station "
                     "does not answer INVALID_TIMESTAMP" % (ts, self.stamp, new))
            self.stamp = new

    STAMPED_FILES = ("sc.o", "wl.o", "boot2.o", "boot.o")

    def _is_comparator(self):
        """The comparator's MMI (ASTRO-TAC / OmniTAC) answers GET HW_VER with ASTROTAC/OMNITAC.
        One read-only command, cached on the link."""
        if self.link.kind is None:
            try:
                self.link.probe()
            except Exception:
                pass
        return self.link.is_comparator

    def _prime_stamp(self):
        """The station compares a SWDL timestamp with the newest one it holds for ANY file (a
        BOOT2 download was refused with ?(0092) because sc.o carried a newer stamp), so look
        at every file first."""
        if self._is_comparator():
            return                      # the comparator's GET OC replies carry no timestamp
        names = list(self.STAMPED_FILES) + [s.stn_file for s in self.sections if s.stn_file not in self.STAMPED_FILES]
        for n in names:
            try:
                body, _ = self.link.raw("GET FW_VER %s" % n)
            except Exception:
                continue
            self._note_station_stamp(parse_fw_info(body)[1])

    def _report_current(self, sec, cur):
        hdr = [cmp_version(open(pc, 'rb').read(64)) for pc, _ in sec.firmware]
        if cur and (version_matches(cur, sec.image_ver) or cur in hdr):
            self.log("station %s already holds this package's version (%s)" % (sec.stn_file, cur))

    def _check_abort(self):
        if self.abort():
            raise SwdlError("aborted by user")

    def _set_baud(self, b):
        if b and hasattr(self.port, 'baudrate') and self.port.baudrate != b:
            self.log("serial port -> %d baud" % b)
            self.port.baudrate = b

    def ascii_prompt(self):
        """True if the station answers with an ASCII prompt (normal mode)."""
        self._slip_down()
        self._set_baud(self.ascii_baud)
        try:
            _, p = self.link.raw("", 2.0)
            return p in ("]-O", "RSS:", "RAP:", "Password:")
        except StationError:
            return False

    def _slip_up(self, timeout=60):
        self._set_baud(STATION_ACTUAL_BAUD.get(self.baud, self.baud))
        if self.stack is None:
            self.stack = Stack(self.port, self.pc_ip, log=lambda m: self.log("  net: " + m))
        end = time.time() + timeout
        while time.time() < end:
            self._check_abort()
            if self.stack.ping(self.station_ip, 1.5):
                return True
        return False

    def _slip_down(self):
        if self.stack:
            st = self.stack.stats
            if st["rx"] >= 10 or st["bad"]:
                self.log("SLIP stats: %d packets in, %d out, %d bad (IP/ICMP/TCP checksum or framing)"
                         % (st["rx"], st["tx"], st["bad"]))
            self.stack.stop()
            self.stack = None

    def _ftp(self):
        return FtpClient(self.stack, self.station_ip, log=lambda m: self.log("  ftp " + m),
                         timeout=40, port=self.ftp_port)

    def _cmdfile(self, ftp, text, path, result=None, wait=1.0):
        """Send a command file to /dev/cmd/... and optionally read a result file."""
        ftp.put(text.replace("\n", "\r\n").encode('latin1'), path, binary=False)
        if result is None:
            return None
        time.sleep(wait)
        # boot loader result files are NUL terminated ("DECRYPT STATUS OK  \0")
        return ftp.get(result, binary=False).decode('latin1').replace("\0", "")

    # -- steps ------------------------------------------------------------------
    def serial_version(self, sec):
        """Current version and timestamp of a section's image, or (None, None).  The comparator
        has no GET FW_VER <file>: Link.comparator_version asks its GET OC commands, which carry
        no timestamp."""
        if self._is_comparator():
            try:
                return self.link.comparator_version(sec.stn_file), None
            except Exception as e:
                self.log("  %s: %s" % (sec.stn_file, e))
                return None, None
        try:
            self.link.ensure("RSS:")
            body, _ = self.link.raw("GET FW_VER %s" % sec.stn_file)
            ver, ts = parse_fw_info(body)
            if not ver:
                raise StationError("unexpected reply %r" % body)
            return ver, ts
        except Exception as e:
            self.log("  GET FW_VER %s: %s" % (sec.stn_file, e))
            return None, None

    def serial_bpid(self, sec):
        for tok in sec.bpid_tokens():
            try:
                self.link.call("CHECK ID [%s]", tok)
                return True
            except StationError:
                continue
        return False

    def serial_flash_mode(self, sec):
        L = self.link
        L.ensure("RSS:")
        cmd = "SWDL %s %s SLIP %d" % (sec.swdl_mode, self.stamp, self.baud)
        L._rx = ""
        L._write(cmd)
        end = time.time() + 15
        buf = ""
        while time.time() < end:                 # the station reboots right after replying
            L._read()
            buf = L._rx
            m = re.search(r'FLASH ME\s+(\S+)\s+(\S+)', buf)
            e = re.search(r'\?\(([^)]*)\)', buf)
            if e:
                raise SwdlError("station refused %s: %s" % (cmd, StationError.__name__ + " " + e.group(1)))
            if m:
                break
            time.sleep(0.05)
        else:
            raise SwdlError("no FLASH ME reply to %s (got %r)" % (cmd, buf[-120:]))
        L._rx = ""
        r = (m.group(1), m.group(2))
        self.station_ip, self.pc_ip = r[0], r[1]
        self.log("station answered FLASH ME %s %s - it is rebooting into the boot loader" % (r[0], r[1]))
        self.link.prompt = None

    def socket_version(self, sec, ftp):
        stn = sec.firmware[0][1].rsplit('/', 1)[-1]
        txt = self._cmdfile(ftp, "GET_FILE_INFO %s %s\n" % (stn, sec.ver_out), "/dev/cmd/fm_ver",
                            "/dev/ram/" + sec.ver_out)
        ver, ts = parse_fw_info(txt.splitlines()[0] if txt else "")
        if not ver and txt:                      # WinRSS: text after the first "= "
            line = txt.splitlines()[0]
            i = line.find("=")
            ver = line[i + 2:].split(" ")[0] if i > 0 else None
        return ver, ts

    def socket_bpid(self, sec, ftp):
        toks = sec.bpid_tokens()
        for i in range(0, len(toks), 10):
            r = self._cmdfile(ftp, "DECRYPT " + " ".join(toks[i:i + 10]) + " \n", "/dev/cmd/bpid",
                              "/dev/ram/bpn_stat", 3.0)
            if r.strip().split(" ")[-1].upper() == "OK":
                return True
        return False

    def socket_timestamp(self, ftp):
        r = self._cmdfile(ftp, "SWDL %s\n" % self.stamp, "/dev/cmd/swdl", "/dev/ram/dl_stat")
        last = r.strip().split(" ")[-1].upper() if r.strip() else "OK"
        if last == "INVALID_TIMESTAMP":
            raise SwdlError("station says the timestamp is equal to or older than its own")
        return last == "OK"

    def download(self, sec, ftp, base, span):
        total = sum(os.path.getsize(p) for p, _ in sec.firmware)
        done = 0
        for pc, stn in sec.firmware:
            data = open(pc, 'rb').read()
            self.log("FTP put %s -> %s (%d bytes)" % (os.path.basename(pc), stn, len(data)))
            t0 = time.time()
            ftp.put(data, stn, binary=True,
                    progress=lambda n, tot, d0=done: self.progress(base + span * (d0 + n) / total,
                                                                  "Sending %s  %d / %d bytes" % (os.path.basename(pc), n, tot)))
            done += len(data)
            self.log("  sent in %.0f s (%.0f bytes/s)" % (time.time() - t0, len(data) / max(time.time() - t0, 0.01)))
        time.sleep(2)
        pc, stn = sec.transfer
        self.log("FTP put %s -> %s (ASCII) - the boot loader now writes flash" % (os.path.basename(pc), stn))
        ftp.put(open(pc, 'rb').read(), stn, binary=False)

    def wait_programmed(self, timeout=900, gone=8.0, boot_after=120.0):
        """After the transfer command file (or NORMAL_MODE) the station keeps answering ping over
        SLIP while it programs flash (seen on a real SCM: ~250 ms replies, misses of up to ~4 s),
        then resets.  A ping silence of `gone` seconds counts as the reset - it must stay below
        the ~11.6 s a reset into the boot loader takes (FLASH ME -> SLIP up), or a station that
        resets straight back into FLASH_MODE (after "DONE CL_NO") is never noticed.  After that we
        look for the ASCII prompt, and call it "boot" only if there is still no prompt after
        `boot_after` seconds while SLIP answers steadily.  No FTP is done meanwhile."""
        self.log("waiting for the station to program flash and reset (do not power it off)")
        t0 = time.time()
        if self.stack is None:
            self._slip_up(5)
        last_ok = time.time()
        while self.stack and time.time() - t0 < timeout:
            self._check_abort()
            self.progress(None, "Station programming (%d s)" % (time.time() - t0))
            if self.stack.ping(self.station_ip, 2.0):
                last_ok = time.time()
                time.sleep(1)
            elif time.time() - last_ok >= gone:
                self.log("no ping for %.0f s after %.0f s - station is resetting" % (gone, time.time() - t0))
                break
        self._slip_down()
        t1 = time.time()
        slip_since = None
        while time.time() - t0 < timeout:
            self._check_abort()
            self.progress(None, "Waiting for the station to restart (%d s)" % (time.time() - t0))
            time.sleep(3)
            if self.ascii_prompt() and self.ascii_prompt():
                self.log("station is back at its ASCII prompt after %.0f s (normal mode)" % (time.time() - t0))
                return "normal"
            if time.time() - t1 < boot_after:
                continue
            if self._slip_up(4):
                slip_since = slip_since or time.time()
                if time.time() - slip_since >= 30:
                    self.log("no ASCII prompt after %.0f s but SLIP answers - station stayed in the boot loader"
                             % (time.time() - t0))
                    return "boot"
            else:
                slip_since = None
            self._slip_down()
        raise SwdlError("station did not come back within %d s" % timeout)

    # -- main -------------------------------------------------------------------
    def run(self):
        try:
            self._run()
        finally:
            self._slip_down()              # never leave the stack thread reading the port

    def _run(self):
        n = len(self.sections)
        normal = self.ascii_prompt()
        self.log("station is %s" % ("in normal mode (ASCII prompt)" if normal else
                                     "not at an ASCII prompt - assuming boot loader (SLIP)"))
        for i, sec in enumerate(self.sections):
            base, span = i / n, 1.0 / n
            self._check_abort()
            self.progress(base, "[%s] %s" % (sec.name, sec.stn_file))
            self.log("=== [%s] %s -> %s" % (sec.name, sec.stn_file, sec.image_ver))
            if normal:
                self._set_baud(self.ascii_baud)
                self.link.ensure("RSS:")
                self._prime_stamp()             # per section: the previous one may have stamped a file
                cur, ts = self.serial_version(sec)
                self.log("station %s version: %s  timestamp %s" % (sec.stn_file, cur, ts))
                if sec.name == "boot2" and cur and version_matches(cur, sec.image_ver):
                    self.log("boot2 already at %s - skipped" % cur)
                    continue
                self._note_station_stamp(ts)
                self._report_current(sec, cur)
                if not self.serial_bpid(sec):
                    raise SwdlError("no BPID token accepted (CHECK ID) - wrong package for this backplane")
                self.log("backplane ID accepted")
                if self.dry:
                    self.log("DRY RUN: would send SWDL %s %s SLIP %d and download %s"
                             % (sec.swdl_mode, self.stamp, self.baud, ", ".join(os.path.basename(p) for p, _ in sec.firmware)))
                    continue
                if not self.confirm("Put the station into firmware download mode and program [%s] %s?"
                                    % (sec.name, ", ".join(os.path.basename(p) for p, _ in sec.firmware))):
                    raise SwdlError("cancelled")
                self.serial_flash_mode(sec)
                time.sleep(1)
                if not self._slip_up(60):
                    raise SwdlError("no SLIP link to %s after SWDL (check the baud rate)" % self.station_ip)
                self.log("SLIP link up, %s answers ping" % self.station_ip)
                ftp = self._ftp()
            else:
                if not self._slip_up(20):
                    raise SwdlError("station answers neither the ASCII prompt nor SLIP ping")
                ftp = self._ftp()
                cur, ts = self.socket_version(sec, ftp)
                self.log("station %s version: %s  timestamp %s" % (sec.stn_file, cur, ts))
                if sec.name == "boot2" and cur and version_matches(cur, sec.image_ver):
                    ftp.quit()
                    self.log("boot2 already at %s - skipped" % cur)
                    continue
                self._note_station_stamp(ts)
                self._report_current(sec, cur)
                if not self.socket_bpid(sec, ftp):
                    raise SwdlError("no BPID token accepted - wrong package for this backplane")
                if self.dry:
                    ftp.quit()
                    self.log("DRY RUN: would set timestamp %s, flash mode %s and download"
                             % (self.stamp, sec.flash_mode))
                    continue
                if not self.confirm("Program [%s] %s?" % (sec.name, ", ".join(os.path.basename(p) for p, _ in sec.firmware))):
                    raise SwdlError("cancelled")
                self.socket_timestamp(ftp)
                self._cmdfile(ftp, sec.flash_mode + "\n", "/dev/cmd/flash_mode")
                ftp.quit()
                self._slip_down()
                time.sleep(5)
                if not self._slip_up(60):
                    raise SwdlError("station did not come back after FLASH_MODE")
                ftp = self._ftp()
            try:
                self.download(sec, ftp, base, span * 0.8)
            finally:
                ftp.quit()
            # what we just wrote now carries self.stamp; the next section needs a newer one
            # (the station refuses "equal or older"), whichever path it goes through
            self.stamp = time.strftime(STAMP_FMT, time.localtime(
                time.mktime(time.strptime(self.stamp, STAMP_FMT)) + 60))
            if not sec.reset:
                continue
            mode = self.wait_programmed()
            normal = mode == "normal"
            if normal:
                cur, _ = self.serial_version(sec)
            else:
                self._slip_up(20)
                ftp = self._ftp()
                cur, _ = self.socket_version(sec, ftp)
                ftp.quit()
            if version_matches(cur, sec.image_ver):
                self.log("verified: %s is %s" % (sec.stn_file, cur))
            else:
                self.log("NOTE: station reports %s = %s, SWDL.INI IMAGE_VER is %s (WinRSS would call this "
                         "'Firmware Verification Failed'; expected when the image carries its own version)"
                         % (sec.stn_file, cur, sec.image_ver))
        if not normal:
            # e.g. a stock application that refuses a non-020.14.015 BOOT2 drops the station
            # back into FLASH_MODE (seen 2026-11-05); try NORMAL_MODE once
            self.log("station is still in its boot loader - sending NORMAL_MODE")
            self.normal_mode()
            if self.wait_programmed(timeout=300) != "normal":
                raise SwdlError("station stayed in its boot loader after NORMAL_MODE")
        self._slip_down()
        self._set_baud(self.ascii_baud)
        self.progress(1.0, "Done")
        self.log("download finished")

    # -- Ethernet (RSS manual 11-35): FTP to the station's own IP, no serial link ---------
    def _ether_ftp(self, timeout=20):
        """A connection to the station's FTP port, or None if it does not answer."""
        try:
            self.stack = self.stack or SocketStack(dst=self.station_ip, log=lambda m: self.log("  net: " + m))
            return FtpClient(self.stack, self.station_ip, log=lambda m: self.log("  ftp " + m),
                             timeout=timeout, port=self.ftp_port)
        except (NetError, OSError):
            return None

    def _ether_wait(self, timeout=180, stable=0):
        """Wait for the station's FTP port to answer (and, with `stable`, to keep answering for
        that many consecutive tries, as WinRSS waits for its ping to stop flapping)."""
        end = time.time() + timeout
        good = 0
        while time.time() < end:
            self._check_abort()
            ftp = self._ether_ftp()
            if ftp is not None:
                good += 1
                if good > stable:
                    return ftp
                ftp.quit()
            else:
                good = 0
            time.sleep(2)
        raise SwdlError("the station at %s did not answer FTP for %d s" % (self.station_ip, timeout))

    def run_ethernet(self):
        """A download to a station reached by Ethernet, as WinRSS' Ethernet connection does it:
        everything goes over FTP to the station's own IP - the version and BPID queries, the
        timestamp, /dev/cmd/flash_mode (which puts it into its boot loader), the images and the
        transfer command file.  No serial port and no SLIP.
        """
        if not self.ethernet:
            raise SwdlError("no station address for an Ethernet download")
        self.station_ip = self.ethernet
        self.stack = None
        try:
            self._run_ethernet()
        finally:
            if self.stack:
                self.stack.stop()
            self.stack = None

    def _run_ethernet(self):
        n = len(self.sections)
        self.log("Ethernet download from %s (FTP port %d)" % (self.station_ip, self.ftp_port))
        for i, sec in enumerate(self.sections):
            base, span = i / n, 1.0 / n
            self._check_abort()
            self.progress(base, "[%s] %s" % (sec.name, sec.stn_file))
            self.log("=== [%s] %s -> %s" % (sec.name, sec.stn_file, sec.image_ver))
            ftp = self._ether_wait(60)
            cur, ts = self.socket_version(sec, ftp)
            self.log("station %s version: %s  timestamp %s" % (sec.stn_file, cur, ts))
            if sec.name == "boot2" and cur and version_matches(cur, sec.image_ver):
                ftp.quit()
                self.log("boot2 already at %s - skipped" % cur)
                continue
            self._note_station_stamp(ts)
            self._report_current(sec, cur)
            if not self.socket_bpid(sec, ftp):
                ftp.quit()
                raise SwdlError("no BPID token accepted - wrong package for this backplane")
            self.log("backplane ID accepted")
            if self.dry:
                ftp.quit()
                self.log("DRY RUN: would set timestamp %s, flash mode %s and download %s"
                         % (self.stamp, sec.flash_mode,
                            ", ".join(os.path.basename(p) for p, _ in sec.firmware)))
                continue
            if not self.confirm("Program [%s] %s over Ethernet to %s?"
                                % (sec.name, ", ".join(os.path.basename(p) for p, _ in sec.firmware),
                                   self.station_ip)):
                ftp.quit()
                raise SwdlError("cancelled")
            self.socket_timestamp(ftp)
            self._cmdfile(ftp, sec.flash_mode + "\n", "/dev/cmd/flash_mode")
            ftp.quit()
            self.log("station told to enter its boot loader - waiting for it to answer again")
            time.sleep(2)
            ftp = self._ether_wait(180)
            try:
                self.download(sec, ftp, base, span * 0.8)
            finally:
                ftp.quit()
            self.stamp = time.strftime(STAMP_FMT, time.localtime(
                time.mktime(time.strptime(self.stamp, STAMP_FMT)) + 60))
            if not sec.reset:
                continue
            self.progress(base + span * 0.85, "Waiting for the station to finish flashing")
            ftp = self._ether_wait(900, stable=4)      # programmed, reset, and back up
            try:
                cur, _ = self.socket_version(sec, ftp)
            finally:
                ftp.quit()
            if version_matches(cur, sec.image_ver):
                self.log("verified: %s is %s" % (sec.stn_file, cur))
            else:
                self.log("NOTE: station reports %s = %s, SWDL.INI IMAGE_VER is %s (WinRSS would call "
                         "this 'Firmware Verification Failed'; expected when the image carries its "
                         "own version)" % (sec.stn_file, cur, sec.image_ver))
        self.progress(1.0, "Done")
        self.log("download finished")

    # -- boot loader round trip (tests the SLIP/FTP path without writing flash) -------
    def boot_roundtrip(self):
        """SWDL into the boot loader, query it over FTP, then NORMAL_MODE back.  No firmware is
        sent and no transfer command file is written, so flash is not touched.  Returns a dict."""
        sec = next((s for s in self.sections if s.swdl_mode.upper() == "FLASH"), self.sections[-1])
        if not self.ascii_prompt():
            raise SwdlError("station is not at an ASCII prompt - start from normal mode")
        self._set_baud(self.ascii_baud)
        before, ts = self.serial_version(sec)
        self.log("station %s version: %s  timestamp %s" % (sec.stn_file, before, ts))
        self._prime_stamp()
        if not self.serial_bpid(sec):
            raise SwdlError("no BPID token accepted (CHECK ID)")
        if not self.confirm("Put the station into its boot loader (SWDL %s) and back, without programming?"
                            % sec.swdl_mode):
            raise SwdlError("cancelled")
        res = {"before": before}
        self.serial_flash_mode(sec)
        t0 = time.time()
        try:
            time.sleep(1)
            if not self._slip_up(60):
                raise SwdlError("no SLIP ping answer from %s after SWDL" % self.station_ip)
            res["slip_up_s"] = round(time.time() - t0, 1)
            self.log("SLIP link up after %.1f s, %s answers ping" % (res["slip_up_s"], self.station_ip))
            ftp = self._ftp()
            try:
                for s in self.sections:
                    v, t = self.socket_version(s, ftp)
                    res["ver " + s.stn_file] = "%s %s" % (v, t)
                    self.log("boot loader: %s = %s  timestamp %s" % (s.stn_file, v, t))
                res["bpid"] = self.socket_bpid(sec, ftp)
                self.log("boot loader: BPID %s" % ("accepted" if res["bpid"] else "NOT accepted"))
                res["mode"] = self._cmdfile(ftp, "GET_MODE_INFO\n", "/dev/cmd/fp_ask", "/dev/ram/fp_stat").strip()
                self.log("boot loader: GET_MODE_INFO -> %r" % res["mode"])
                res["error_log"] = self._cmdfile(ftp, "BUILD_ERROR_LOG_FILE\n", "/dev/cmd/bld_log",
                                                 "/dev/ram/error_log", 3.0)
                self.log("boot loader: error log %d bytes" % len(res["error_log"]))
            finally:
                ftp.quit()
        finally:
            self.log("sending NORMAL_MODE")
            try:
                self.normal_mode()
            except Exception as e:
                self.log("NORMAL_MODE failed: %s - station may still be in its boot loader "
                         "(python3 -m qrss.swdl PKG --port ... --normal-mode, or WinRSS)" % e)
                raise
        mode = self.wait_programmed(timeout=300)
        if mode != "normal":
            raise SwdlError("station stayed in its boot loader after NORMAL_MODE")
        self._set_baud(self.ascii_baud)
        res["after"], _ = self.serial_version(sec)
        res["total_s"] = round(time.time() - t0, 1)
        self.log("back in normal mode after %.0f s, %s = %s" % (res["total_s"], sec.stn_file, res["after"]))
        return res

    # -- recovery helpers (WinRSS "Normal Mode" / "Save Status") ---------------------
    def normal_mode(self):
        try:
            if not self._slip_up(20):
                raise SwdlError("no SLIP answer from %s" % self.station_ip)
            ftp = self._ftp()
            self._cmdfile(ftp, "BUILD_ERROR_LOG_FILE\n" * 10 + "NORMAL_MODE\n", "/dev/cmd/norm_md")
            ftp.quit()
        finally:
            self._slip_down()
        self.log("NORMAL_MODE sent - the station restarts its application")

    def save_status(self):
        try:
            if not self._slip_up(20):
                raise SwdlError("no SLIP answer from %s" % self.station_ip)
            ftp = self._ftp()
            txt = self._cmdfile(ftp, "BUILD_ERROR_LOG_FILE\n", "/dev/cmd/bld_log", "/dev/ram/error_log", 3.0)
            ftp.quit()
        finally:
            self._slip_down()
        return txt


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Program Quantar SCM firmware from a SWDL package over the serial port")
    ap.add_argument("package", help="package directory (with SWDL.INI)")
    ap.add_argument("--port", help="serial port; omit to run against the built-in virtual SCM")
    ap.add_argument("--baud", type=int, default=9600, help="normal ASCII baud rate (default 9600)")
    ap.add_argument("--slip-baud", type=int, default=38400, help="SLIP baud for the download: 9600..57600 on stock firmware, 115200 with H026.11.05a+ (PC side runs at the station's real 111607)")
    ap.add_argument("--back-port", action="store_true", help="station back port addresses (193.0.0.1/.4)")
    ap.add_argument("--dry-run", action="store_true", help="check versions and BPID only")
    ap.add_argument("--normal-mode", action="store_true", help="send NORMAL_MODE to a station stuck in its boot loader")
    ap.add_argument("--ethernet", metavar="IP", help="download over the network to the station's own IP "
                    "instead of the serial port (RSS manual 11-35); no --port needed")
    ap.add_argument("--ftp-port", type=int, default=21, help="station FTP port (default 21)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    a = ap.parse_args(argv)
    from .netstack import BACK
    secs = parse_package(a.package)
    for s in secs:
        print(s.describe())
    probs = validate_package(secs)
    if probs:
        print("PROBLEMS:\n  " + "\n  ".join(probs))
        if not a.dry_run:
            return 1
    if a.ethernet:
        port = None
        print("Ethernet download to %s" % a.ethernet)
    elif a.port:
        import serial
        port = serial.Serial(a.port, a.baud, timeout=0.05)
    else:
        from .vscm import VirtualSCM
        port = VirtualSCM(log=lambda m: print("        [virtual SCM] " + m)).host
        print("(no --port: using the virtual SCM)")
    ask = (lambda t: True) if a.yes else (lambda t: input(t + " [y/N] ").strip().lower() == "y")
    d = Downloader(port, secs, baud=a.slip_baud, ascii_baud=a.baud if a.port else None,
                   station_port=BACK if a.back_port else FRONT,
                   log=lambda m: print(time.strftime("%H:%M:%S"), m),
                   progress=lambda f, t: None, confirm=ask, dry_run=a.dry_run,
                   ethernet=a.ethernet, ftp_port=a.ftp_port)
    if a.ethernet:
        d.run_ethernet()
    elif a.normal_mode:
        d.normal_mode()
    else:
        d.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
