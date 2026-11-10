"""Qt side of the station link: one worker thread owns the serial port; screens queue jobs
(callables taking the Link) and get results back on the GUI thread.  While a real-time
stream (metering / front panel monitor) is active the worker polls it and emits values."""
import queue, threading, time

from PySide6.QtCore import QObject, Signal

from .station import Link, StationError


class StationService(QObject):
    done = Signal(object, object, object)       # callback, result, error
    meter = Signal(str, str)                     # key, value from a real-time stream
    meter_error = Signal(str)                    # a stream value errored, e.g. "?(0086) ..."
    traffic = Signal(str, str)                   # direction, text (for the log panel)
    connection = Signal(bool, str)               # connected?, description
    busy = Signal(bool)

    def __init__(self):
        super().__init__()
        self.link = None
        self.modem = None            # qrss.modem.Modem while a dial-up session is up
        self.vscm = None
        self.baud = 9600
        self.desc = ""
        self.cond = None             # (conditioned, mark, id_byte) from the connect check
        self.pw_state = None        # True/False from GET STATE PASS at connect
        self.q = queue.Queue()
        self.done.connect(self._deliver)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ connection
    @property
    def connected(self):
        return self.link is not None

    def connect_port(self, port, baud=9600, mmi_baud=None, password=None, modem=None):
        """`modem`, if given, is a dict (number, cfg) for a dial-up connection: `port` is then
        the PC's modem port, which is initialized and dialed before the MMI session starts
        (RSS manual chapter 9)."""
        def job(_):
            if self.link:
                try:
                    self.link.restore_mmi_baud()     # the new link starts at 9600
                except Exception:
                    pass
                self.link.close()
            self.modem = None
            self.desc_prefix = None
            if modem:
                from .modem import Modem, ModemError
                m = Modem(port, baud, modem.get("number", ""), modem.get("cfg"),
                          log=lambda d, t: self.traffic.emit(d, "[modem] " + t))
                try:
                    dev = m.dial()
                except ModemError as e:
                    raise StationError("dial-up to %s failed: %s" % (m.number, e))
                self.modem = m
                self.desc_prefix = "dial-up %s via %s" % (m.number, port)
            if port in ("SIMULATOR", "SIMULATOR-COMPARATOR"):
                from .vscm import VirtualSCM
                # the virtual station runs a PCTL -BAUD (H026.11.05f+) application, so the fast-MMI
                # path can be exercised offline.  SIMULATOR-COMPARATOR is the ASTRO-TAC MMI.
                kind = "ASTROTAC" if port.endswith("COMPARATOR") else "QUANTAR"
                self.vscm = VirtualSCM(log=lambda m: self.traffic.emit('#', "[virtual SCM] " + m),
                                        pctl_baud=True, kind=kind)
                dev = self.vscm.host
            elif not modem:
                dev = port
            self.link = Link(dev, baud, log=lambda d, t: self.traffic.emit(d, t), password=password)
            self.baud = baud
            self.link.ensure("RSS:")
            self.link.probe()                       # QUANTAR or ASTROTAC (the comparator)
            hw = self.link.hw_ver
            where = getattr(self, "desc_prefix", None) or ("%s @ %s" % (port, baud))
            self.desc = "%s (%s%s)" % (where, hw,
                                        ", comparator" if self.link.is_comparator else "")
            # one read-only RD AL for the EEPROM conditioning marker; a station that will not
            # answer it is not a reason to fail the connection
            try:
                self.cond = self.link.conditioning_state()
            except StationError:
                self.cond = None
            if self.cond is not None and not self.cond[0]:
                self.traffic.emit('#', "EEPROM not conditioned (SA[7C] = %04X); the station "
                                   "conditions it at its next restart" % self.cond[1])
            # GET STATE PASS: one read-only command, and the one place the station says out loud
            # whether a password is set (it is otherwise only visible at the Password: prompt).
            try:
                self.pw_state = self.link.password_state()
            except StationError:
                self.pw_state = None
            if self.pw_state:
                self.traffic.emit('#', "station password protection is enabled")
            if mmi_baud and mmi_baud > baud:
                # a stock station refuses and stays at 9600; that is not an error
                if self.link.set_mmi_baud(mmi_baud) == "]-O":
                    self.desc += ", MMI %d" % mmi_baud
                    self.traffic.emit('#', "MMI speed %d baud" % mmi_baud)
                else:
                    self.traffic.emit('#', "station refused PCTL -BAUD %d - staying at %d" % (mmi_baud, baud))
            return self.desc

        def cb(res, err):
            if err:
                try:
                    if self.link:
                        self.link.close()
                except Exception:
                    pass
                self.link = None
                if self.modem:
                    self.modem.hangup()
                    self.modem = None
                self.connection.emit(False, str(err))
            else:
                self.connection.emit(True, res)
        self.q.put((job, cb, True))

    def disconnect_port(self):
        def job(_):
            if self.modem:                            # drop the call while the port is still open
                self.modem.hangup()
                self.modem = None
            if self.link:
                try:
                    self.link.restore_mmi_baud()     # leave the station at 9600, as a reset would
                except Exception:
                    pass
                self.link.close()
            self.link = None
        self.q.put((job, lambda r, e: self.connection.emit(False, "disconnected"), True))

    def shutdown(self, timeout=8.0):
        """Last thing before the process exits: put the station back to 9600 and close the port.
        Waits (bounded) for the worker itself - the done signal would need the GUI thread, which
        is the one waiting here."""
        ev = threading.Event()

        def job(_):
            try:
                if self.modem:
                    self.modem.hangup()
                    self.modem = None
                if self.link:
                    self.link.restore_mmi_baud()
                    self.link.close()
            except Exception:
                pass
            finally:
                self.link = None
                ev.set()
        self.q.put((job, None, True))
        ev.wait(timeout)

    # ------------------------------------------------------------------ jobs
    def run(self, fn, callback=None):
        """Queue fn(link) on the worker; callback(result, error) runs on the GUI thread."""
        self.q.put((fn, callback, False))

    def run_raw(self, fn, callback=None):
        """Queue fn(None) on the worker even with no station connected: the Ethernet firmware
        download talks to the station over the network, not through the serial port."""
        self.q.put((fn, callback, True))

    def call(self, template, *args, callback=None, timeout=2.5):
        self.run(lambda L: L.call(template, *args, timeout=timeout), callback)

    def stream(self, cmd, callback=None, session="RAP:"):
        self.run(lambda L: L.stream_start(cmd, session=session), callback)

    def stop_stream(self, callback=None):
        self.run(lambda L: L.stream_stop(), callback)

    def _deliver(self, cb, res, err):
        if cb:
            cb(res, err)

    def _run(self):
        while True:
            try:
                fn, cb, raw = self.q.get(timeout=0.1)
            except queue.Empty:
                L = self.link
                if L is not None and L.streaming:
                    try:
                        for k, v in L.stream_lines():
                            self.meter.emit(k, v)
                    except Exception as e:
                        self.traffic.emit('!', "stream error: %s" % e)
                        self.meter_error.emit(str(e))
                        time.sleep(0.5)
                continue
            self.busy.emit(True)
            try:
                if not raw and self.link is None:
                    raise StationError("not connected to a station (Station > Connect…)")
                res, err = fn(self.link), None
            except Exception as e:          # StationError, timeouts, serial errors
                res, err = None, e
            self.busy.emit(False)
            self.done.emit(cb, res, err)
