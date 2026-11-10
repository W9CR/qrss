"""Virtual Quantar station control module.

Normal mode: the ASCII MMI of qrss.simstation (]-O / RSS: / RAP:, GET/SET/AL commands,
metering streams), plus the firmware-download commands GET FW_VER <file>, CHECK ID <token>
and SWDL <mode> <date time> SLIP <baud>.  With kind="ASTROTAC" it is a comparator: versions
come from GET OC <n> SW VER OC|B1|B2 and GET OC <n> WL <m> SW VER WL instead (no timestamp),
and the images are oc.o / boot2.o / wl.o.

After SWDL it answers "FLASH ME 193.0.0.2 193.0.0.5" and switches to "boot loader" mode:
SLIP/IP (qrss.netstack) with ping and an anonymous FTP server.  Files written to
/dev/ram/... are kept; files written to /dev/cmd/... are executed:
  CP <src> <dst>             copy (rejected like BOOT2 if the file's sum32 is not 0)
  DONE [CL_NO]               "program flash" (a few seconds) and reboot to normal mode
  GET_FILE_INFO <f> <out>    /dev/ram/<out> = "FW_VER = <ver> TIMESTAMP = <date time>"
  DECRYPT <tokens>           /dev/ram/bpn_stat = "DECRYPT STATUS OK  \0" (NUL terminated, as real)
  SWDL <date time>           /dev/ram/dl_stat = "SWDL OK"
  GET_MODE_INFO              /dev/ram/fp_stat
  BUILD_ERROR_LOG_FILE       /dev/ram/error_log
  NORMAL_MODE                reboot to normal mode
  FLASH_MODE / FORCE_BOOT1   reboot into the boot loader
Firmware versions come from the QUANTAR header of the flashed image.

Run it on a serial port (e.g. a USB null-modem cable to a PC running WinRSS) or a pty:
    python3 -m qrss.vscm /dev/ttyUSB0 [--baud 9600]
    python3 -m qrss.vscm --pty
"""
import os, re, sys, threading, time

from .netstack import FRONT, SocketStack, Stack, Wire, NetError
from .simstation import ComparatorSim, SimStation
from .swdl import cmp_version, file_sum32


def _app_checks_boot2(cmp):
    """True if this application image still has the exact-BOOT2-version check
    (cmpi.l #$02014015,-$be(a6) followed by beq.b; H026.11.05b+ patch the beq to bra)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        import quantar_cmp as qc
        p = qc.decompress_payload(cmp[qc.HDR:])
    except Exception:
        return True                       # stock behaviour if we cannot look
    return b"\x0c\xae\x02\x01\x40\x15\xff\x42\x67" in p


def _app_pctl_baud(cmp):
    """PCTL -BAUD in this application image: None (stock), "ok" (quantar_patch --pctl-baud as it is
    now, H026.11.05f+: switches the RSS port it is typed on) or "inert" (an older cave, H026.11.05d/e:
    takes -BAUD but reprograms another port's baud-rate generator)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        import quantar_cmp as qc, quantar_patch as qp
        p = qc.decompress_payload(cmp[qc.HDR:])
        if p[qp.PCTL_HOOK - qp.RT:qp.PCTL_HOOK - qp.RT + 6] != b"\x4e\xf9" + qp.PCTL_CAVE.to_bytes(4, "big"):
            return None
        code = qp.pctl_baud_code()
        return "ok" if p[qp.PCTL_CAVE - qp.RT:qp.PCTL_CAVE - qp.RT + len(code)] == code else "inert"
    except Exception:
        return None


class _HoldTx:
    """Like the real boot loader: what the station sends is held until it next receives a
    byte (see netstack.Stack.KICK).  Without the PC's kick bytes every reply is one behind."""

    def __init__(self, dev):
        self.dev, self.held, self.lock = dev, [], threading.Lock()

    def write(self, data):
        with self.lock:
            self.held.append(data)

    def read(self, n=1024, *a):
        d = self.dev.read(n, *a)
        if d:
            with self.lock:
                out, self.held = self.held, []
            for x in out:
                self.dev.write(x)
        return d


class FtpServer:
    def __init__(self, stack, scm):
        self.stack, self.scm = stack, scm
        self.lst = stack.listen(21)
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self):
        self.running = False
        self.lst.close()

    def _serve(self):
        while self.running:
            try:
                c = self.lst.accept(1.0)
            except NetError:
                continue
            threading.Thread(target=self._session, args=(c,), daemon=True).start()

    def _session(self, c):
        say = lambda t: c.send((t + "\r\n").encode())
        port = None
        try:
            say("220 193.0.0.2 pSOSystem FTP server (NTL68K/1.0.5C) ready.")
            while self.running:
                try:
                    line = c.readline(120)
                except NetError:
                    return
                if not line:
                    return
                verb, _, arg = line.partition(" ")
                verb = verb.upper()
                if verb == "USER":
                    say("331 Guest login ok, send 'psos' as password.")
                elif verb == "PASS":
                    say("230 Guest login ok, access restrictions apply.")
                elif verb == "TYPE":
                    say("200 Type set to " + arg)
                elif verb == "PORT":
                    p = [int(x) for x in arg.split(",")]
                    port = (".".join(map(str, p[:4])), p[4] * 256 + p[5])
                    say("200 Port set okay")
                elif verb in ("STOR", "RETR"):
                    if not port:
                        say("425 No PORT")
                        continue
                    if verb == "RETR" and arg not in self.scm.files:
                        say("550 %s: no such file" % arg)
                        continue
                    say("150 Opening data connection")
                    dc = self.stack.connect(port[0], port[1], 20)
                    if verb == "STOR":
                        buf = bytearray()
                        while True:
                            d = dc.recv(65536, 60)
                            if not d:
                                break
                            buf += d
                        dc.close()
                        self.scm.files[arg] = bytes(buf)
                        say("226 Transfer complete")
                        if arg.startswith("/dev/cmd/"):
                            self.scm.command_file(arg, bytes(buf).decode('latin1'))
                    else:
                        dc.send(self.scm.files[arg])
                        dc.drain(60)
                        dc.close()
                        say("226 Transfer complete")
                    port = None
                elif verb == "QUIT":
                    say("221 Bye")
                    c.close()
                    return
                elif verb == "NOOP":
                    say("200 NOOP")
                else:
                    say("502 Command not implemented")
        except Exception as e:
            self.scm.log("ftp session error: %s" % e)


def _image_version(d):
    """The version a flashed image reports: the QUANTAR header's, or, for a comparator image,
    the "OC_R.." string the application prints for GET OC (its OMNITAC header has none)."""
    v = cmp_version(d)
    if v:
        return v
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        import quantar_cmp as qc
        p = qc.decompress_payload(d[qc.HDR:])
        m = re.search(rb'[A-Z0-9]{2,4}_R\d\d\.\d\d\.\d\d\d', p)   # OC_R03.19.001, B2_R03.11.000, ...
        if m:
            return m.group(0).decode()
    except Exception:
        pass
    return None


class _Sim(SimStation):
    def __init__(self, scm):
        super().__init__()
        self.scm = scm

    def _rss(self, c):
        a = c.split()
        if c.startswith("GET FW_VER") and len(a) == 3 and a[2] not in ("SC", "EX", "WL"):
            v, ts = self.scm.flash.get(a[2].lower(), ("UNKNOWN", "1970/01/01 00:00"))
            return self._say("FW_VER = %s TIMESTAMP = %s" % (v, ts))
        if c.startswith("GET FW_VER SC") and len(a) == 3:
            return self._say(" FW_VER SC = %s" % self.scm.flash["sc.o"][0])
        if c.startswith("CHECK ID"):
            return self._say()
        if c.startswith("SWDL ") and " SLIP " in c:
            if len(a) >= 4 and not self.scm.stamp_ok(" ".join(a[2:4])):
                return self._say("?(0092) Requested timestamp is equal or older than stations timestamp")
            self.scm.stamp = " ".join(a[2:4])
            self.out += b"\r\nFLASH ME %s %s\r\n" % (FRONT[0].encode(), FRONT[1].encode())
            self.scm.log("SWDL received: %s" % c)
            threading.Timer(0.3, self.scm.enter_boot).start()
            return
        return super()._rss(c)


def _comparator_image():
    """A codeplug for the virtual comparator: the first comparator .cp in CodePlug-archive when
    there is one (this tree), else an empty v0x62 image with the same directory, so the screens
    open and the codeplug verbs work."""
    import glob
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in sorted(glob.glob(os.path.join(here, "CodePlug-archive", "*.cp"))):
        try:
            from .codeplug import CpFile
            f = CpFile(path)
            if len(f.cp) > 8 and f.cp[6] == 0x62:
                return bytes(f.cp)
        except Exception:
            continue
    img = bytearray(0x1144)                      # the 3.9 layout, every table empty
    img[4:6] = (0x1144).to_bytes(2, "big")
    img[6:8] = b"\x00\x62"
    img[8:10] = (8).to_bytes(2, "big")
    for i, off in enumerate((0x2A, 0x64, 0x8A, 0xAC, 0xC6, 0xF8, 0x109A, 0x10AC)):
        img[10 + 4 * i:14 + 4 * i] = off.to_bytes(4, "big")
    img[0:2] = (sum(img[2:]) & 0xFFFF).to_bytes(2, "big")
    return bytes(img)


class _CmpSim(ComparatorSim):
    """The comparator's MMI, plus the firmware-download commands its application answers:
    GET OC <n> SW VER OC|B1|B2 and GET OC <n> WL <m> SW VER WL (no GET FW_VER <file>, and
    the replies carry no timestamp), CHECK ID, and SWDL <mode> <date time> SLIP <baud> ->
    "FLASH ME".  Its images are named oc.o / boot2.o / wl.o."""

    VER_NAMES = {"OC": "oc.o", "B1": "boot.o", "B2": "boot2.o"}

    def __init__(self, scm):
        super().__init__()
        self.scm = scm

    def _rss(self, c):
        a = c.split()
        if c.startswith("GET OC ") and " SW VER " in c:
            if " WL " in c and len(a) == 8:         # GET OC <n> WL <m> SW VER WL
                v = self.scm.flash.get("wl.o", ("UNKNOWN", ""))[0]
                # the application prints the extra WL token (OC.img 0x2a062)
                return self._say("OC %s WL %s SW VER WL = %s" % (a[2], a[4], v))
            if len(a) == 6 and a[5] in self.VER_NAMES:
                v = self.scm.flash.get(self.VER_NAMES[a[5]], ("UNKNOWN", ""))[0]
                return self._say("OC %s SW VER %s = %s" % (a[2], a[5], v))
        if c.startswith("SWDL ") and " SLIP " in c:
            # OC's own handler (0x0E2972 -> 0x0E5FBA): args[0] must be FLASH or FORCE_BOOT1,
            # and the baud must be one of the seven in the table at flash 0x160C22.  Anything
            # else answers ?(0091) with the largest listed rate that does not exceed it, and
            # never sends FLASH ME.
            if len(a) == 6 and a[4] == "SLIP":     # args[3] to the firmware: SWDL is args[-1]
                if a[1] not in ("FLASH", "FORCE_BOOT1"):
                    return self._say('?(0018) option "%s" is invalid' % a[1])
                try:
                    baud = int(a[5])
                except ValueError:
                    baud = 0
                rec = max([b for b in (4800, 9600, 14400, 19200, 28800, 38400, 57600)
                           if b <= baud] or [4800])
                if rec != baud:
                    return self._say("?(0091) invalid baudrate, recommended baudrate is %d" % rec)
            if len(a) >= 4 and not self.scm.stamp_ok(" ".join(a[2:4])):
                return self._say("?(0092) Requested timestamp is equal or older than stations timestamp")
            self.scm.stamp = " ".join(a[2:4])
            self.out += b"\r\nFLASH ME %s %s\r\n" % (FRONT[0].encode(), FRONT[1].encode())
            self.scm.log("SWDL received: %s" % c)
            threading.Timer(0.3, self.scm.enter_boot).start()
            return
        return super()._rss(c)


class VirtualSCM:
    def __init__(self, dev=None, log=None, flash_seconds=3.0, boot_seconds=10.0, pctl_baud=False,
                 kind="QUANTAR", password=None, special_mode=False, tcp=None):
        """dev: the station side of the cable (Wire/pyserial/pty).  Without one a Wire pair is
        made and .host is the end to give to qrss.  pctl_baud: model an application that takes
        PCTL -BAUD (H026.11.05f+), so the fast-MMI path can be exercised offline.  password:
        model a protected station (it answers "Password: " until the MMI is given it).
        special_mode: the P0_IN pattern that lets LOST and SERVICE work at the prompt (what
        holding Vol Up and Vol Down on the station control panel puts there)."""
        if dev is None:
            self.host, dev = Wire.pair()
        self.dev = dev
        self.log = log or (lambda m: None)
        self.flash_seconds = flash_seconds
        self.boot_seconds = boot_seconds
        self.tcp = tcp                    # serve the boot loader's FTP on this TCP port (Ethernet)
        self.files = {}
        if kind == "ASTROTAC":           # the comparator's images, as GET OC reports them
            self.flash = {"oc.o": ("OC_R03.19.001", "1998/01/01 00:00"),
                          "boot2.o": ("XR03.11.000", "1998/01/01 00:00"),
                          "boot.o": ("XR03.10.000", "1998/01/01 00:00"),
                          "wl.o": ("XR03.18.000", "1998/01/01 00:00")}
        else:
            self.flash = {"sc.o": ("R020.14.048", "2011/10/26 10:00"), "boot2.o": ("R020.14.015", "2011/04/11 10:00"),
                          "wl.o": ("R003.02.000", "2007/08/27 10:00")}
        self.kind = kind
        self.sim = _CmpSim(self) if kind == "ASTROTAC" else _Sim(self)
        if kind == "ASTROTAC":
            self.sim.cp_img = _comparator_image()
        self.sim.pctl_baud = pctl_baud
        if password:
            self.sim.password = password.strip().upper()
        self.sim.special_mode = special_mode
        self.mode = "normal"
        self.app_checks_boot2 = True          # stock application
        self.stack = self.ftp = None
        self.stamp = time.strftime("%Y/%m/%d %H:%M")
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()
        if tcp:
            self._tcp_ftp()

    def _tcp_ftp(self):
        """Serve FTP over TCP (Ethernet) on self.tcp.  The real application serves FTP in normal
        mode too - WinRSS' Ethernet download asks for versions and BPIDs over it before it puts
        the station into its boot loader - and the boot loader serves it after the reset."""
        self.stack = SocketStack(ip="127.0.0.1", log=self.log, ports={21: self.tcp})
        self.ftp = FtpServer(self.stack, self)

    def stamp_ok(self, ts):
        """Like the boot loader: a new download must be stamped later than every image in flash."""
        return all(ts > fts for _, fts in self.flash.values())     # "equal or older" is refused

    def _rate_ok(self):
        """Without a real line, model the MMI speed: if the PC end of the Wire has been set to a
        rate that is not the station's (within 5 %: "115200" is 111607), nothing gets through."""
        pc = getattr(getattr(self, "host", None), "baudrate", None)
        return pc is None or abs(pc - self.sim.mmi_baud) <= 0.05 * self.sim.mmi_baud

    def _pump(self):
        while self.running:
            if self.mode != "normal":
                time.sleep(0.05)
                continue
            d = self.dev.read(256)
            if d and self._rate_ok():
                self.sim.write(d)
            out = self.sim.read(4096)
            if out and self.mode == "normal" and self._rate_ok():
                self.dev.write(out)

    # -- boot loader -----------------------------------------------------------------
    def enter_boot(self, tcp=None):
        """`tcp`: serve the boot loader's FTP on that TCP port instead of over SLIP - the
        Ethernet download path (RSS manual 11-35) without a network card in the way."""
        self.mode = "booting"
        time.sleep(0.5)
        self.sim.out = b""
        if tcp:
            self._tcp_ftp()
            self.mode = "boot"
            self.log("boot loader: FTP on 127.0.0.1:%d (Ethernet)" % tcp)
            return
        self.stack = Stack(_HoldTx(self.dev), FRONT[0], log=self.log, kick=None)
        self.ftp = FtpServer(self.stack, self)
        self.mode = "boot"
        self.log("boot loader: SLIP up as %s" % FRONT[0])

    def reboot(self, to_boot=False, flash_time=0.0):
        def go():
            time.sleep(0.5)
            if flash_time:                          # the real boot loader keeps answering ping
                self.log("programming flash (still answering over SLIP)")
                time.sleep(flash_time)
            if self.ftp:
                self.ftp.stop()
            if self.stack:
                self.stack.stop()
            self.stack = self.ftp = None
            self.mode = "rebooting"
            self.log("%s, rebooting" % ("programming flash" if flash_time else "reset"))
            time.sleep(1.0)
            while self.dev.read(4096):              # discard what arrived while "off"
                pass
            b2 = self.flash.get("boot2.o", ("", ""))[0]
            into_boot = to_boot
            if (not into_boot and self.kind != "ASTROTAC" and self.app_checks_boot2
                    and re.sub(r'^[^0-9]+', '', b2) != "020.14.015"):
                # the real application (0x122FA4 in 20.14.048) insists on BOOT2 020.14.015 and
                # drops straight back into FLASH_MODE otherwise (seen 2026-11-05)
                self.log("application refuses BOOT2 %s - back to FLASH_MODE" % b2)
                into_boot = True
            if into_boot:
                time.sleep(self.boot_seconds)          # real SCM: ~11.6 s until SLIP answers
                self.enter_boot(tcp=self.tcp)
            else:
                app = "oc.o" if self.kind == "ASTROTAC" else "sc.o"
                self.sim.prompt = "]-O "
                self.sim.stream = None
                self.sim.pw_ok, self.sim.pw_tries = False, 0     # a boot: the password again
                self.sim.out = (b"\r\nOMNITAC\r\n]-O " if self.kind == "ASTROTAC"
                                else b"\r\nQUANTAR\r\n]-O ")
                self.mode = "normal"
                if self.tcp:
                    self._tcp_ftp()               # the application serves FTP again
                self.log("normal mode, %s %s" % (app, self.flash.get(app, ("", ""))[0]))
        threading.Thread(target=go, daemon=True).start()

    def command_file(self, path, text):
        name = path.rsplit("/", 1)[-1]
        self.log("command file %s: %s" % (name, " / ".join(l.strip() for l in text.splitlines() if l.strip())))
        ram = lambda n: "/dev/ram/" + n
        for line in text.splitlines():
            t = line.split()
            if not t:
                continue
            k = t[0].upper()
            if k == "CP" and len(t) >= 3:
                d = self.files.get(t[1])
                if d is None:
                    self.log("CP: %s missing" % t[1])
                elif file_sum32(d) != 0:
                    self.log("CP: %s sum32 != 0 - deleted (BOOT2 behaviour)" % t[1])
                    self.files.pop(t[1], None)
                else:
                    self.files[t[2]] = d
                    if t[2].lower().endswith(("/sc.o", "/oc.o")):
                        if self.kind == "ASTROTAC":
                            self.app_checks_boot2 = False
                        else:
                            self.app_checks_boot2 = _app_checks_boot2(d)
                            kind = _app_pctl_baud(d)
                            self.sim.pctl_baud = kind is not None
                            self.sim.pctl_baud_inert = kind == "inert"
                    v = _image_version(d) or "UNKNOWN"
                    self.flash[t[2].rsplit("/", 1)[-1].lower()] = (v, self.stamp)
                    self.log("flashed %s (%d bytes, version %s)" % (t[2], len(d), v))
            elif k == "DONE":
                # resets into the application; the stock application itself drops back into
                # FLASH_MODE if BOOT2 is not 020.14.015 (see reboot()).  "DONE CL_NO" does NOT
                # keep the station in the boot loader (checked 2026-11-05).
                self.reboot(flash_time=self.flash_seconds)
            elif k == "GET_FILE_INFO" and len(t) >= 3:
                v, ts = self.flash.get(t[1].lower(), ("UNKNOWN", "1970/01/01 00:00"))
                self.files[ram(t[2])] = ("FW_VER = %s TIMESTAMP = %s\r\n" % (v, ts)).encode()
            elif k == "DECRYPT":
                self.files[ram("bpn_stat")] = b"DECRYPT STATUS OK  \0"
            elif k == "SWDL":
                ts = " ".join(t[1:3])
                if self.stamp_ok(ts):
                    self.stamp = ts
                    self.files[ram("dl_stat")] = b"SWDL OK\r\n"
                else:
                    self.files[ram("dl_stat")] = b"SWDL INVALID_TIMESTAMP\r\n"
            elif k == "GET_MODE_INFO":
                self.files[ram("fp_stat")] = b"FLASH_MODE\0"
            elif k == "BUILD_ERROR_LOG_FILE":
                self.files[ram("error_log")] = b"VIRTUAL SCM ERROR LOG\r\nno errors\r\n"
            elif k == "NORMAL_MODE":
                self.reboot()
            elif k in ("FLASH_MODE", "FORCE_BOOT1", "FLASH"):
                self.reboot(to_boot=True)


# ---------------------------------------------------------------------- serial / pty front ends
class _Pty:
    def __init__(self):
        import pty, tty
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.name = os.ttyname(self.slave)

    def read(self, n):
        import select
        r, _, _ = select.select([self.master], [], [], 0.05)
        return os.read(self.master, n) if r else b""

    def write(self, b):
        return os.write(self.master, b)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Virtual Quantar SCM on a serial port or pty")
    ap.add_argument("port", nargs="?", help="serial port (omit with --pty)")
    ap.add_argument("--pty", action="store_true", help="create a pseudo-terminal and print its name")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--password", help="model a password-protected station (5-8 characters)")
    ap.add_argument("--special-mode", action="store_true",
                    help="model the special-mode field (LOST/SERVICE work at the Password: prompt)")
    a = ap.parse_args(argv)
    if a.pty:
        dev = _Pty()
        print("virtual SCM on %s (connect qrss or a terminal to it)" % dev.name)
    elif a.port:
        import serial
        dev = serial.Serial(a.port, a.baud, timeout=0.05)
        print("virtual SCM on %s @ %d" % (a.port, a.baud))
    else:
        ap.error("give a serial port or --pty")
    VirtualSCM(dev, log=lambda m: print(time.strftime("%H:%M:%S"), m), password=a.password,
               special_mode=a.special_mode)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
