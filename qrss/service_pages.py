"""Service screens (WinRSS "Service" menu): software version, status report, status panel
(front panel monitor), metering, alignment, and a raw terminal.

Command sequences follow the WinRSS screen makers (QuantarAlignmentScreenMaker etc.);
see qrss/station.py for the protocol.
"""
import datetime, os, time

from PySide6.QtCore import QDate, Qt, QSettings, QTime, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QProgressBar, QPushButton,
    QListWidget, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QTimeEdit, QVBoxLayout, QWidget)

from . import dlg
from .help import text as help_text
from .station import parse_status, decode_status, STATUS_NAMES, StationError

MONO = None


def mono():
    global MONO
    if MONO is None:
        MONO = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    return MONO


def show_help(parent, tags):
    """Help buttons: the topics go to the main window's help pane (WinRSS opens a modal
    dialog); outside the main window, fall back to a dialog."""
    w = parent.window()
    if hasattr(w, 'show_help_topics'):
        w.show_help_topics(getattr(parent, 'title', '') or "Help", tags)
        return
    d = QDialog(parent)
    d.setWindowTitle("Help")
    v = QVBoxLayout(d)
    t = QTextBrowser()
    t.setFont(mono())
    t.setPlainText(help_text(tags) or "No help available.")
    v.addWidget(t)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(d.reject)
    v.addWidget(bb)
    d.resize(760, 620)
    dlg._run(d, parent)


def button(text, fn, tip=None):
    b = QPushButton(text)
    b.clicked.connect(lambda _=False: fn())
    if tip:
        b.setToolTip(tip)
    return b


def ro(text="N/A"):
    l = QLabel(text)
    l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    l.setMinimumWidth(110)
    return l


def para(text):
    l = QLabel(text)
    l.setWordWrap(True)
    l.setMinimumWidth(200)
    return l


def mhz(hz):
    try:
        return "%.6f" % (int(float(hz)) / 1e6)
    except (TypeError, ValueError):
        return str(hz)


# ====================================================================== base

UNSUPPORTED = {"0014", "0017"}      # ?(0014) invalid command / ?(0017) invalid option


def show_err(e):
    """Table text for a failed read; commands the firmware lacks get a plain note."""
    if getattr(e, 'code', None) in UNSUPPORTED:
        return "(not supported by this station)"
    return "(%s)" % e

class ServicePage(QWidget):
    title = ""
    help_tags = ()
    station_controls = True           # access enable/disable + reset buttons in the header

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.svc = main.station
        self.active = False
        self.v = QVBoxLayout(self)
        top = QHBoxLayout()
        _t = QLabel("<b>%s</b>" % self.title); _t.setObjectName("pageTitle")
        top.addWidget(_t)
        top.addStretch(1)
        self.note = QLabel("")
        self.note.setStyleSheet("font-weight: bold;")
        top.addWidget(self.note)
        self.v.addLayout(top)
        h = QHBoxLayout()
        if self.station_controls:
            h.addWidget(button("Access Disable", self.access_disable,
                               "Stop normal station activity (do this before any alignment)"))
            h.addWidget(button("Access Enable", self.access_enable))
            h.addWidget(button("Reset Station", self.reset_station))
        h.addStretch(1)
        if self.help_tags:
            h.addWidget(button("Help", lambda: show_help(self, self.help_tags)))
        self.v.addLayout(h)
        self.conn = QLabel()
        self.conn.setStyleSheet("color: #b00;")
        self.v.addWidget(self.conn)
        self.svc.connection.connect(lambda ok, d: self._conn_changed())
        self._conn_changed()

    # -- helpers ----------------------------------------------------------------
    def _conn_changed(self):
        self.conn.setText("" if self.svc.connected else
                          "Not connected to a station - use Station > Connect… (or the simulator).")
        self.conn.setVisible(not self.svc.connected)

    def status(self, text):
        self.main.statusBar().showMessage(text, 8000)

    def fail(self, err, what=""):
        self.status("%s%s" % (what + ": " if what else "", err))
        dlg.warning(self, self.title, "%s%s" % (what + "\n\n" if what else "", err))

    def job(self, fn, then=None, what=""):
        """Run fn(link) on the station thread; then(result) on success."""
        def cb(res, err):
            if err:
                self.fail(err, what)
            elif then:
                then(res)
        self.svc.run(fn, cb)

    def call(self, template, *args, then=None, timeout=2.5, what=""):
        self.job(lambda L: L.call(template, *args, timeout=timeout), then, what or template.split('[')[0].strip())

    def seq(self, steps, then=None, what=""):
        """steps: list of (template, args[, timeout]); then(list of results)."""
        def fn(L):
            out = []
            for s in steps:
                out.append(L.call(s[0], *s[1], timeout=s[2] if len(s) > 2 else 2.5))
            return out
        self.job(fn, then, what)

    def access_disable(self):
        self.set_access(True)

    def access_enable(self):
        self.set_access(False)

    def set_access(self, disabled):
        """The station's own access state (WinRSS's StationEnableDisable screen,
        ` SET STATION_DISABLE ON|OFF`) - not the front panel's ACC_DIS key, which is
        `FP ACC_DIS` and leaves the access state alone.  Reads the state back afterwards:
        the station drops the session to ]-O while it applies the change."""
        def fn(L):
            L.set_station_disabled(disabled)
            return L.station_access()
        def then(res):
            state, value = res
            self.note.setText("STATION ACCESS %s" % state.upper())
            self.status("Station access %s" % state.lower())
            self.show_access(state, value)
        self.job(fn, then, "Station access")

    def show_access(self, state, value=None):
        """Hook for a page that displays the access state (the status panel does)."""

    def reset_station(self):
        if dlg.question(self, "Reset", "Reset the connected station?") != dlg.SB.Yes:
            return
        self.status("Resetting the station - the app restarts, ~12 s")
        self.job(lambda L: L.reset(), lambda r: self.status("Station reset"), "Reset")

    # -- lifecycle (called by the main window) -----------------------------------
    def activate(self):
        self.active = True

    def deactivate(self):
        self.active = False

    def refresh(self):
        pass


# ====================================================================== version / report
class VersionPage(ServicePage):
    title = "Software Version"
    comparator = True
    help_tags = ("dswv01_screen_help",)
    station_controls = False
    ITEMS = [("Hardware version", "GET HW_VER"), ("Station control firmware", "GET FW_VER SC"),
             ("Station control timestamp", "GET FW_VER SC.O"), ("Wireline firmware", "GET FW_VER WL"),
             ("Wireline timestamp", "GET FW_VER WL.O"), ("Exciter firmware", "GET FW_VER EX"),
             ("Boot code", "GET FW_VER BOOT.O"), ("Boot 2 code", "GET FW_VER BOOT2.O"),
             ("Codeplug version", "GET CP VER"), ("Front panel version", "GET FP VER"),
             ("Board part number (BPN)", "GET BPN"), ("Station name", "GET STN NAME"),
             ("Station serial number", "GET STN SN"), ("Station time", "GET TIME"),
             ("Station system", "GET STN SYS"), ("Maximum channels", "GET CHN MAX")]
    # The comparator's MMI has its own GETs (OC firmware strings, OC.img 0x29d68) and no
    # FW_VER SC / EX / WL, no front panel and no CHN MAX.  An item's command is a template
    # string for Link.call, or a function of the link for the GET OC versions (whose replies
    # Link.comparator_version parses; the control card is always 1).
    COMPARATOR_ITEMS = [("Hardware version", "GET HW_VER"), ("Comparator name", "GET COMPARATOR_NAME"),
                         ("Comparator mode", "GET CM_MODE"), ("Comparator number", "GET CM_NUM"),
                         ("Control card firmware", lambda L: L.comparator_version("oc.o")),
                         ("Control card boot code (B1)", lambda L: L.comparator_version("boot.o")),
                         ("Control card boot 2 code (B2)", lambda L: L.comparator_version("boot2.o")),
                         ("Wireline firmware (WL 1)", lambda L: L.comparator_version("wl.o")),
                         ("Station system", "GET STN SYS"), ("Station time", "GET TIME"),
                         ("Codeplug version", "GET CP VER"), ("Board part number (BPN)", "GET BPN"),
                         ("Access state", "GET STATE PASS"), ("Battery type", "GET PS BATT_TYPE"),
                         ("Battery equalize", "GET PS EQUAL"), ("IP address", "GET ADDR IP"),
                         ("Physical address", "GET ADDR PHYS")]

    def __init__(self, main):
        super().__init__(main)
        if getattr(main, 'system', '') == 'COMPARATOR':
            self.ITEMS = self.COMPARATOR_ITEMS
        self.tab = QTableWidget(len(self.ITEMS), 2)
        self.tab.setHorizontalHeaderLabels(["Item", "Value"])
        self.tab.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tab.verticalHeader().setVisible(False)
        for i, (lbl, _) in enumerate(self.ITEMS):
            self.tab.setItem(i, 0, QTableWidgetItem(lbl))
        self.v.addWidget(self.tab, 1)
        self.v.addWidget(button("Read from station", self.read))

    def activate(self):
        super().activate()
        if self.svc.connected:
            self.read()

    def read(self):
        def fn(L):
            out = []
            for _, cmd in self.ITEMS:
                try:
                    if callable(cmd):
                        out.append(cmd(L) or "")
                    else:
                        out.append(" ".join(L.call(cmd)))
                except Exception as e:
                    out.append(show_err(e))
            return out
        def show(vals):
            for i, v in enumerate(vals):
                self.tab.setItem(i, 1, QTableWidgetItem(v))
        self.job(fn, show, "Read versions")


class StatusReportPage(ServicePage):
    """The station's alarm log (ALARM RD, WinRSS 21158; clear 21160).  The comparator has the
    same screen (ComparatorStatusReportPanelMaker) and answers ALARM RD in the same shape, but
    internal only - it has no ALARM_EXT, so the External report is disabled there."""
    title = "Status Report (alarm log)"
    help_tags = ("alarm_screen_help",)
    station_controls = False
    comparator = True

    def __init__(self, main):
        super().__init__(main)
        h = QHBoxLayout()
        self.kind = QComboBox(); self.kind.addItems(["Internal", "External"])
        self.kind.currentIndexChanged.connect(lambda _: self.read())
        h.addWidget(QLabel("Report:")); h.addWidget(self.kind)
        h.addWidget(button("Refresh", self.read)); h.addWidget(button("Clear All", self.clear))
        h.addWidget(button("Save Report…", self.save_report)); h.addStretch(1)
        self.v.addLayout(h)
        self.tab = QTableWidget(0, 4)
        self.tab.setHorizontalHeaderLabels(["First Occurrence", "Last Occurrence", "Count", "Message"])
        self.tab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.v.addWidget(self.tab, 1)

    def activate(self):
        super().activate()
        if self.svc.connected:
            # the comparator's report is internal only (no ALARM_EXT in its MMI)
            cmp = getattr(self.svc.link, 'is_comparator', False)
            if cmp and self.kind.currentIndex() != 0:
                self.kind.setCurrentIndex(0)     # re-reads via currentIndexChanged
            else:
                self.read()
            self.kind.model().item(1).setEnabled(not cmp)

    def read(self):
        cmd = "ALARM RD" if self.kind.currentIndex() == 0 else "ALARM_EXT RD"
        def fn(L):
            try:
                return L.call(cmd, timeout=30)[0]    # a ~50-entry log takes ~8 s at 9600
            except Exception as e:
                if getattr(e, 'code', None) in UNSUPPORTED:     # R020.14.048 has no ALARM_EXT
                    return None
                raise
        self.job(fn, self.show, "Read status report")

    def show(self, text):
        if text is None:
            self.tab.setRowCount(0)
            self.status("The %s status report is not supported by this station" % self.kind.currentText().lower())
            return
        rows = []
        for line in text.replace('\r', '\n').split('\n'):
            if "ALARM" not in line or line.rstrip().endswith("NOYB"):
                continue
            t = line[line.index("ALARM"):].split()
            if len(t) < 5:
                continue
            rows.append([t[2], t[3], t[4], " ".join(t[5:])])
        self.tab.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.tab.setItem(r, c, QTableWidgetItem(v))
        self.tab.resizeColumnsToContents()
        self.status("%d status entries" % len(rows))

    def clear(self):
        if dlg.question(self, "Clear", "Clear the %s status report in the station?" % self.kind.currentText().lower()) != dlg.SB.Yes:
            return
        self.call("ALARM CLEAR" if self.kind.currentIndex() == 0 else "ALARM_EXT CLEAR", then=lambda r: self.read())

    def save_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save report", "status_report.txt", "Text (*.txt)")
        if path:
            with open(path, 'w') as f:
                for r in range(self.tab.rowCount()):
                    f.write("\t".join((self.tab.item(r, c).text() if self.tab.item(r, c) else "") for c in range(4)) + "\n")


# ====================================================================== metering
EXCITER = [("TX_PA_ID_A", "Exciter Module ID"), ("TX_VR", "Tx Reflected Voltage (V)"),
           ("TX_VF", "Tx Forward Voltage (V)"), ("TX_PCIC_VCTRL_DIV2", "Power Control Voltage (V)"),
           ("TX_SL_LEVEL_SYNTH", "VCO Steering Line Voltage (V)"), ("TX_10PT2V", "10.2 V Exciter Voltage (V)"),
           ("TX_2PT1_REF_LEVEL", "2.1 MHz Reference Level (V)"), ("TX_PF", "Internal Tx Forward Power (W)"),
           ("TX_PR", "Internal Tx Reflected Power (W)"), ("TX_VSWR", "Internal VSWR"),
           ("EXT_WM_PF", "External Tx Forward Power (W)"), ("EXT_WM_PR", "External Tx Reflected Power (W)"),
           ("EXT_WM_VSWR", "External VSWR")]
PA = [("TX_PA_ID_B", "PA Module ID"), ("TX_FPA_I2_A", "Final PA Side A Current (A)"),
      ("TX_FPA_I2_B", "Final PA Side B Current (A)"), ("TX_IPA_I", "Intermediate PA Current (A)"),
      ("TX_DPA_I", "Driver PA Current (A)"), ("TX_V_OMNI", "OMNI Control Voltage (V)"),
      ("TX_PA_TEMP", "Thermistor Voltage (V)"), ("TX_IPA_VF", "Intermediate PA Forward Voltage (V)"),
      ("TX_DPA_VF", "Driver PA Forward Voltage (V)"), ("TX_FPA_VF", "Final PA Forward Voltage (V)"),
      ("TX_28V", "28 V PA Voltage (V)"), ("TX_14PT2V", "14.2 V PA Voltage (V)"), ("PA_FULL", "PA state")]
RECEIVER = [("ID", "Module ID"), ("APLUS", "Main Power Supply Voltage"), ("10V", "Regulated 10 V Supply"),
            ("5V", "Analog 5 V Supply"), ("SL_LO1", "Steering Line Level Of OSC1"),
            ("SL_LO2", "Steering Line Level Of OSC2"), ("REF_LEVEL", "2.1 MHz Reference Level"),
            ("LO1_LOCK", "Oscillator 1 Lock Indicator"), ("INJ_LEVEL", "Injection Level"),
            ("PRE_BIAS", "Preamp Bias"), ("AD_CHK", "A/D Self Check")]
POWER = [("PS_MOD_ID", "Module ID", ""), ("PS_28V", "28 V Power Supply", "27.0 to 30.0 V"),
         ("PS_14V", "14 V Power Supply", "13.4 to 14.9 V"), ("PS_5V", "5 V Power Supply", "4.8 to 5.3 V"),
         ("PS_BAT_CHR_VOLT", "Battery Charger Voltage - AC Only", "0.0 to 35.2 V"),
         ("PS_BAT_TEMP", "Battery Temperature", "-40 to +66 °C"), ("PS_HS_TEMP", "Heatsink Temperature", "-40 to +100 °C")]
UHSO = [("UHSO_MOD_ID", "Module ID", ""), ("UHSO_14PT2", "14.2 Volt Supply In", "11.3 to 17.0 V"),
        ("UHSO_10V", "10 Volt Regulator", "8.0 to 12.0 V"), ("UHSO_5V", "5 Volt Supply In", "4.0 to 6.0 V"),
        ("UHSO_8V", "8 Volt OSC Reference", "6.4 to 9.6 V"), ("UHSO_OSC_STR", "OSC Str Line (0-8V)", "0.0 to 9.6 V"),
        ("UHSO_OSC_FREQ", "OSC Frequency", "1.0 to 10.0 V"), ("UHSO_VTEMP1", "VTemp RT200", "-30 to +70 °C"),
        ("UHSO_5MHZ_ACT", "5 MHz Reference", "")]
SCWL = [("SC_ID", "Station Control Module ID", ""), ("BPN_ID", "Station ID", ""),
        ("WL_ID", "Wireline Module ID", ""), ("WL_9PT6V", "9.6V Power Supply", "9.1 to 10.1 V"),
        ("WL_NEG_9PT6V", "-9.6V Power Supply", "-10.1 to -9.1 V")]


class MeterGrid(QWidget):
    def __init__(self, groups):
        super().__init__()
        self.values = {}
        h = QHBoxLayout(self)
        for title, rows in groups:
            box = QGroupBox(title)
            g = QGridLayout(box)
            for r, row in enumerate(rows):
                keys, label = row[0], row[1]
                g.addWidget(QLabel(label), r, 0)
                val = ro()
                val.setFont(mono())
                g.addWidget(val, r, 1)
                if len(row) > 2 and row[2]:
                    g.addWidget(QLabel("<i>%s</i>" % row[2]), r, 2)
                for k in (keys if isinstance(keys, (list, tuple)) else [keys]):
                    self.values[k] = val
            g.setRowStretch(len(rows), 1)
            h.addWidget(box)

    def update(self, k, v):
        w = self.values.get(k)
        if w is not None:
            w.setText(v)
            return True
        return False

    def clear(self):
        for w in self.values.values():
            w.setText("N/A")


class MeteringPage(ServicePage):
    title = "Metering"
    help_tags = ("meter_exciter", "meter_pa", "meter_receiver", "meter_receiver2", "meter_power_supply",
                 "meter_uhso", "meter_wireline")

    def __init__(self, main):
        super().__init__(main)
        self.tabs = QTabWidget()
        rx = lambda p: [([p + k], l) for k, l in RECEIVER]
        self.defs = [("Exciter/PA", "MTR TX_PA_P1", MeterGrid([("Exciter", EXCITER), ("Power Amplifier", PA)])),
                     ("Receiver 1", "MTR RX_P1", MeterGrid([("Receiver 1", rx("RX_"))])),
                     ("Receiver 2", "MTR RX2_P1", MeterGrid([("Receiver 2", rx("RX2_"))])),
                     ("Power Supply", "MTR PS_P1", MeterGrid([("Power Supply", POWER)])),
                     ("UHSO", "MTR UHSO", MeterGrid([("UHSO", UHSO)])),
                     ("Station Control / Wireline", "MTR WL_P1", MeterGrid([("Station Control / Wireline", SCWL)]))]
        for title, _cmd, grid in self.defs:
            w = QWidget(); l = QVBoxLayout(w); l.addWidget(grid)
            if title == "Exciter/PA":
                h = QHBoxLayout()
                self.keystate = QLabel("DeKeyed")
                h.addWidget(QLabel("Station:")); h.addWidget(self.keystate)
                h.addWidget(button("KEY UP", lambda: self.rap("RC SET PTT XMIT")))
                h.addWidget(button("DEKEY", lambda: self.rap("RC SET PTT OFF")))
                h.addWidget(button("PA TEST ON", lambda: self.rap("RC SET PA TEST ON"),
                                   "Keys the PA without exciter drive checks (see help)"))
                h.addWidget(button("PA TEST OFF", lambda: self.rap("RC SET PA TEST OFF")))
                h.addStretch(1)
                l.addLayout(h)
            l.addStretch(1)
            self.tabs.addTab(w, title)
        self.tabs.currentChanged.connect(lambda i: self.start())
        self.v.addWidget(self.tabs, 1)
        self.other = QLabel("")
        self.other.setWordWrap(True)
        self.v.addWidget(self.other)
        self.err = QLabel("")
        self.err.setWordWrap(True)
        self.err.setStyleSheet("color: #c0392b;")      # a disabled/failed stream value (e.g. RX2 board)
        self.err.setVisible(False)
        self.v.addWidget(self.err)
        self.svc.meter.connect(self.on_meter)
        self.svc.meter_error.connect(self.on_meter_error)
        self.extra = {}

    def rap(self, cmd):
        self.call(cmd)

    def on_meter_error(self, msg):
        if not self.active:
            return
        self.err.setText("Stream error: " + msg)        # e.g. "?(0086) receiver board disabled" (RX2)
        self.err.setVisible(True)

    def start(self):
        if not (self.active and self.svc.connected):
            return
        _t, cmd, grid = self.defs[self.tabs.currentIndex()]
        grid.clear()
        self.extra = {}
        self.err.setVisible(False)
        self.err.setText("")
        self.svc.stream(cmd, lambda r, e: e and self.fail(e, "Start metering"))

    def on_meter(self, k, v):
        if not self.active:
            return
        grid = self.defs[self.tabs.currentIndex()][2]
        if k == "PA_FULL":
            self.keystate.setText("Keyed (%s)" % v if v.upper() in ("LOW", "FULL") else "DeKeyed")
        if not grid.update(k, v) and k:
            self.extra[k] = v
            self.other.setText("Other values: " + ", ".join("%s = %s" % kv for kv in sorted(self.extra.items())))

    def activate(self):
        super().activate()
        self.start()

    def deactivate(self):
        super().deactivate()
        if self.svc.connected:
            self.svc.stop_stream()


# ====================================================================== status panel
LEDS = [("AC_ON", "AC power"), ("AC_FAIL", "AC fail"), ("CTRL_ON", "Control"), ("CTRL_FAIL", "Control fail"),
        ("TX_LOCK", "TX locked"), ("RX_LOCK", "RX1 locked"), ("RX2_LOCK", "RX2 locked"),
        ("RX1_ACTIVE", "RX1 active"), ("RX2_ACTIVE", "RX2 active"), ("RX1_FAIL", "RX1 fail"), ("RX2_FAIL", "RX2 fail"),
        ("PA_FULL", "PA"), ("PA_FAIL", "PA fail"), ("PA_TEST", "PA test"), ("TX_INHIBIT", "TX inhibit"),
        ("WL_ON", "Wireline"), ("WL_FAIL", "Wireline fail"), ("5MHZ_NET", "5 MHz net"), ("BOOSTER_FAIL", "Booster fail"),
        ("ACC_DIS", "Access dis")]
INFO = [("CHN", "Channel"), ("RPTR RX FREQ", "RX1 frequency"), ("RPTR RX2 FREQ", "RX2 frequency"),
        ("RPTR TX FREQ", "TX frequency"), ("CHN STATUS", "Channel status"), ("RPTR MODE", "Operation mode"),
        ("STN REF STATUS", "Reference"), ("PA STATUS", "PA status"), ("SC STATUS", "Station control status"),
        ("RPTR SITE STATUS", "Site status"), ("TIME", "Station time")]
GOOD = {"ON", "FULL", "LOW", "LOCKED", "ACTIVE", "ENABLE", "UP", "WARM"}


class Led(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(96)
        self.set(None)

    def set(self, v, fail=False):
        if v is None:
            col, fg = "#ddd", "#666"
        elif v.upper() in GOOD:
            col, fg = ("#e33", "white") if fail else ("#3b3", "white")
        elif v.upper() in ("OFF", "NO_STATE", "INACTIVE"):
            col, fg = "#ccc", "#333"
        else:
            col, fg = "#fb3", "black"
        self.setStyleSheet("background:%s; color:%s; border-radius:4px; padding:3px;" % (col, fg))
        if v is not None:
            self.setToolTip(v)


class StatusPanelPage(ServicePage):
    title = "Status Panel (front panel monitor)"
    help_tags = ("dfp01_help", "dfp02_help")

    def __init__(self, main):
        super().__init__(main)
        top = QHBoxLayout()
        hw = QGroupBox("Station")
        f = QFormLayout(hw)
        self.hw = {}
        for k, lbl in (("GET STN NAME", "Name"), ("GET STN SN", "Serial number"), ("GET HW_VER", "Hardware"),
                       ("GET FW_VER SC", "Firmware"), ("GET FP VER", "Front panel"), ("GET CHN MAX", "Max channels"),
                       ("GET STATION_STATUS 4", "Station access")):
            self.hw[k] = ro(); f.addRow(lbl, self.hw[k])
        top.addWidget(hw)
        inf = QGroupBox("Operation")
        f2 = QFormLayout(inf)
        self.info = {}
        for k, lbl in INFO:
            self.info[k] = ro(); f2.addRow(lbl, self.info[k])
        top.addWidget(inf)
        self.v.addLayout(top)
        lb = QGroupBox("Indicators")
        g = QGridLayout(lb)
        self.leds = {}
        cols = 5                           # 5 per row fits the pane when the help pane is open; 7 overflowed
        for i, (k, lbl) in enumerate(LEDS):
            self.leds[k] = Led(lbl); g.addWidget(self.leds[k], i // cols, i % cols)
        self.v.addWidget(lb)
        ctl = QGroupBox("Front panel controls")
        h = QGridLayout(ctl)
        self.maxchan = 16
        self.vol = 50
        self.sq = "OFF"
        items = [("Key Up", lambda: self.rap("RC SET PTT XMIT")), ("Dekey", lambda: self.rap("RC SET PTT OFF")),
                 ("< Channel", lambda: self.chan(-1)), ("Channel >", lambda: self.chan(1)),
                 ("Intercom On", lambda: self.rap("FP INTCOM ON")), ("Intercom Off", lambda: self.rap("FP INTCOM OFF")),
                 ("Speaker Vol −", lambda: self.volume(-5)), ("Speaker Vol +", lambda: self.volume(5)),
                 ("Rcvr Squelch OFF/CSQ/PL", self.squelch),
                 ("FP Acc Dis", lambda: self.rap("FP ACC_DIS ON")),
                 ("FP Acc En", lambda: self.rap("FP ACC_DIS OFF"))]
        for i, (t, fn) in enumerate(items):
            h.addWidget(button(t, fn), i // 5, i % 5)
        self.v.addWidget(ctl)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFont(mono())
        self.log.setMaximumBlockCount(2000)
        self.v.addWidget(para("Status messages"))
        self.v.addWidget(self.log, 1)
        self.last = {}
        self.access_state = None
        self.fpacc = False
        self.chnacc = False
        self.stream_err = None
        self.svc.meter.connect(self.on_meter)

    def rap(self, cmd, *args):
        self.call(cmd, *args)

    def chan(self, d):
        try:
            cur = int(self.info["CHN"].text())
        except ValueError:
            cur = 1
        n = (cur - 1 + d) % self.maxchan + 1
        self.rap("FP CHN [%s]", n)

    def volume(self, d):
        self.vol = max(0, min(100, self.vol + d))
        self.rap("FP SPKRVOL [%s]", self.vol)
        self.status("Speaker volume %d" % self.vol)

    def squelch(self):
        self.sq = {"OFF": "CSQ", "CSQ": "PL", "PL": "OFF"}[self.sq]
        self.rap("FP SQ [%s]", self.sq)
        self.status("Receiver squelch %s" % self.sq)

    def activate(self):
        super().activate()
        if not self.svc.connected:
            return
        def fn(L):
            out = {}
            for k in self.hw:
                try:
                    out[k] = " ".join(L.call(k))
                except Exception as e:
                    out[k] = show_err(e)
            try:
                L.stream_start("FPM")
            except StationError as e:
                # the station refuses RAP: mode while it is disabled (SET STATION_DISABLE ON)
                out["__stream__"] = str(e)
            return out
        def show(out):
            for k, v in out.items():
                if k in self.hw:
                    self.hw[k].setText(v)
            try:
                self.maxchan = int(out.get("GET CHN MAX", "16"))
            except ValueError:
                pass
            # The access state comes from the FPM stream's ACC_DIS key and from
            # GET STATION_STATUS 4, whose byte 3 (0x8066821) has it in bits 7-6 (0b10 enabled,
            # 0b11 disabled) - the only status field WinRSS reads.  The rest of the word is
            # named by station.decode_status and shown in the tooltip.
            st = parse_status(out.get("GET STATION_STATUS 4", ""))
            w = self.hw["GET STATION_STATUS 4"]
            if st:
                sid, value, ck, ok = st
                rows = decode_status(sid, value)
                self.show_access(rows[0][1], value)
                w.setText("%s (0x%08x%s)" % (rows[0][1], value, "" if ok else ", BAD checksum"))
                w.setToolTip("STATION_STATUS %d: %s\n%s" % (
                    sid, STATUS_NAMES.get(sid, "?"),
                    "\n".join("%s: %s" % r for r in rows)))
            self.stream_err = out.get("__stream__")
            if self.stream_err:
                self.render_note()
        self.job(fn, show, "Start front panel monitor")

    def show_access(self, state, value=None):
        """The station's access state (status 4) - the front panel key is the ACC_DIS LED."""
        self.access_state = state
        self.render_note()
        w = self.hw.get("GET STATION_STATUS 4")
        if w is not None:
            if value is None:
                w.setText(state)
            else:
                w.setText("%s (0x%08x)" % (state, value))
                w.setToolTip("STATION_STATUS 4: %s\n%s" % (
                    STATUS_NAMES.get(4, "?"),
                    "\n".join("%s: %s" % r for r in decode_status(4, value))))

    def render_note(self):
        """One line for the two separate things: the station's access state and the front
        panel ACC_DIS key (they are different status fields - see the firmware notes)."""
        parts = []
        if getattr(self, 'access_state', None):
            parts.append("STATION ACCESS %s" % self.access_state.upper())
        if getattr(self, 'fpacc', False):
            parts.append("FRONT PANEL ACCESS DISABLE ON")
        if getattr(self, 'chnacc', False):
            parts.append("CHANNEL ACCESS DISABLED")
        if getattr(self, 'stream_err', None):
            parts.append("front panel monitor unavailable while the station is disabled")
        self.note.setText("   ".join(parts))

    def deactivate(self):
        super().deactivate()
        if self.svc.connected:
            self.svc.stop_stream()

    def on_meter(self, k, v):
        if not self.active or not k:
            return
        if k in self.leds:
            self.leds[k].set(v, fail=k.endswith("FAIL") or k == "TX_INHIBIT")
        if k in self.info:
            self.info[k].setText(mhz(v) if "FREQ" in k and v.isdigit() else v)
        if k == "ACC_DIS":
            # the front panel's own key (status byte 2 bit 4), not the station access state
            self.fpacc = v.upper() == "ON"
            self.render_note()
        elif k == "CHN STATUS":
            self.chnacc = v.upper() == "ACC_DIS"
            self.render_note()
        elif k == "SQ" and v.upper() in ("OFF", "CSQ", "PL"):
            self.sq = v.upper()             # keep the squelch button cycling from the real state
        elif k == "SPKRVOL" and v.isdigit():
            self.vol = int(v)
        if self.last.get(k) != v and k != "TIME":
            self.log.appendPlainText("%s  %-18s %s" % (time.strftime("%H:%M:%S"), k, v))
        self.last[k] = v


# ====================================================================== comparator local status
# The comparator's own status screen (WinRSS "Local Status"): an LS: session.  State names and
# the 80-port layout are from ComparatorLocalStatusScreen.java; the line formats are the OC
# application's strings (OC.img 0x20c70-0x20d4e).
CMP_STATES = ["No Change", "Idle", "Analog Voice", "Digital Voice", "Digital Data", "Failed",
              "Disabled", "Voted Disabled", "Data 12", "Data 18", "Data Control",
              "Local Vote Disabled"]
CMP_PORTS = 80                  # MAX_IO_PORTS in WinRSS; 5 chassis x 16


class ComparatorStatusPage(ServicePage):
    """The ASTRO-TAC's Local Status (WinRSS's comparator status panel).

    DOLS enters the LS: session and the comparator prints positional status lines:

        S<chassis> <16 hex>   port states, one nibble per port (0-9, a = data control,
                               b = local vote disabled)
        AL <port> <alias>      port alias (on entry)
        HA <port> <hex>        HDLC address of the box on that port
        LD <port> <ddd>        link delay in ms (only in answer to GET LD)
        LCM ON|OFF             local control mode
        FV <port>              force-voted port (0 = none)
        VV <port>              voted port (0 = none)

    RS and TG lines are printed too and WinRSS ignores them.  Port numbers are 1-based over
    all 80 ports (chassis 1 holds ports 1-16).  The format strings are the OC application's
    own, but the session has not been exercised on comparator hardware yet - the virtual
    comparator (SIMULATOR-COMPARATOR) is where this page is tested.
    """
    title = "Comparator Local Status"
    comparator = True
    station_controls = False        # the comparator has no access state / reset here
    help_tags = ("local_status_screen_help",)

    COLS = ["Port", "Alias", "State", "HDLC Address", "Link Delay", "Voted", "Force Vote"]

    def __init__(self, main):
        super().__init__(main)
        top = QHBoxLayout()
        self.info = {}
        for k, lbl in (("GET CM_NUM", "Comparator"), ("GET CM_MODE", "Mode")):
            self.info[k] = ro(); top.addWidget(QLabel(lbl + ":")); top.addWidget(self.info[k])
        self.lcm = ro("UNKNOWN")
        top.addWidget(QLabel("Local Control Mode:")); top.addWidget(self.lcm)
        top.addStretch(1)
        self.v.addLayout(top)
        h = QHBoxLayout()
        h.addWidget(button("Local Control ON", lambda: self.call("LCM ON"),
                            "Enable local control: force vote / vote disable from this screen "
                            "(remote status panel commands are ignored until it is turned off)"))
        h.addWidget(button("Local Control OFF", lambda: self.call("LCM OFF")))
        h.addWidget(button("Get Link Delays", lambda: self.call("GET LD"),
                            "Ask the comparator for each port's link delay (only ports whose "
                            "connected station/receiver supports the measurement answer)"))
        h.addWidget(button("Toggle Vote Disable", self.toggle_vote_disable,
                            "Vote-disable / vote-enable the selected port (VDP <n> ON|OFF)"))
        h.addWidget(button("Toggle Force Vote", self.toggle_force_vote,
                            "Force the vote onto the selected port (FVP <n> ON|OFF)"))
        h.addStretch(1)
        self.v.addLayout(h)
        self.tab = QTableWidget(CMP_PORTS, len(self.COLS))
        self.tab.setHorizontalHeaderLabels(self.COLS)
        self.tab.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tab.verticalHeader().setVisible(False)
        self.tab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tab.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        for i in range(CMP_PORTS):
            self.tab.setItem(i, 0, QTableWidgetItem("%d-%d" % (i // 16 + 1, i % 16 + 1)))
            for c in range(1, len(self.COLS)):
                self.tab.setItem(i, c, QTableWidgetItem(""))
        self.v.addWidget(self.tab, 1)
        self.ports = [dict(alias="", state="", hdlc="", delay="", voted=False, force=False)
                      for _ in range(CMP_PORTS)]
        self.svc.meter.connect(self.on_meter)

    def reset_view(self):
        """Start from blank: a port that is now failed/disabled is not reported any more, so
        last session's alias, address and delay would otherwise linger."""
        self.ports = [dict(alias="", state="", hdlc="", delay="", voted=False, force=False)
                      for _ in range(CMP_PORTS)]
        self.lcm.setText("UNKNOWN")
        for i in range(CMP_PORTS):
            for c in range(1, len(self.COLS)):
                self.tab.item(i, c).setText("")

    # -- station ---------------------------------------------------------------
    def activate(self):
        super().activate()
        if not self.svc.connected:
            return
        if not getattr(self.svc.link, 'is_comparator', False):
            self.note.setText("This is the comparator's status screen - connect to an ASTRO-TAC.")
            return
        self.reset_view()
        def fn(L):
            out = {}
            for k in self.info:
                try:
                    out[k] = " ".join(L.call(k))
                except Exception as e:
                    out[k] = show_err(e)
            L.stream_start("DOLS", session="LS:")
            L.stream_cmd("GET LD")        # link delays are not part of the status stream
            return out
        def show(out):
            for k, v in out.items():
                self.info[k].setText(v)
            self.note.setText("")
        self.job(fn, show, "Start comparator status")

    def deactivate(self):
        super().deactivate()
        if self.svc.connected:
            self.svc.stop_stream()

    # -- buttons ---------------------------------------------------------------
    def selected_port(self):
        """0-based port index of the selected row, or None."""
        r = self.tab.currentRow()
        return r if 0 <= r < CMP_PORTS else None

    def _port_cmd(self, kind, on, port):
        self.call("%s [%%s] %s" % (kind, "ON" if on else "OFF"), port + 1)

    def toggle_vote_disable(self):
        n = self.selected_port()
        if n is None:
            self.status("Select a port first.")
            return
        st = self.ports[n]['state']
        if st in ("Failed", "Disabled"):
            self.status("Port %d-%d is %s: it cannot take the command." % (n // 16 + 1, n % 16 + 1, st))
            return
        self._port_cmd("VDP", st != "Local Vote Disabled", n)

    def toggle_force_vote(self):
        n = self.selected_port()
        if n is None:
            self.status("Select a port first.")
            return
        p = self.ports[n]
        if p['state'] in ("Failed", "Disabled", "Voted Disabled", "Local Vote Disabled"):
            self.status("Port %d-%d is %s: it cannot take the command." % (n // 16 + 1, n % 16 + 1, p['state']))
            return
        self._port_cmd("FVP", not p['force'], n)

    # -- LS: stream ------------------------------------------------------------
    def on_meter(self, k, v):
        if not self.active or not k:
            return
        if k == "LCM":
            self.lcm.setText("ENABLED" if v.upper() == "ON" else "DISABLED")
            return
        if k.startswith("S") and len(k) > 1 and k[1:].isdigit():
            chassis = int(k[1:])
            self.set_chassis_states(chassis, v)
            return
        if k in ("AL", "HA", "LD"):
            parts = v.split(None, 1)
            try:
                n = int(parts[0]) - 1
            except (ValueError, IndexError):
                return
            if not 0 <= n < CMP_PORTS:
                return
            rest = parts[1] if len(parts) > 1 else ""
            p = self.ports[n]
            if k == "AL":
                p['alias'] = rest
            elif k == "HA":
                p['hdlc'] = rest
            else:
                p['delay'] = rest
            self.render(n)
            return
        if k in ("FV", "VV"):
            try:
                n = int(v) - 1
            except ValueError:
                return
            key = 'force' if k == "FV" else 'voted'
            for i, p in enumerate(self.ports):
                if p[key] != (i == n):
                    p[key] = (i == n)
                    self.render(i)
            return
        # RS and TG are printed by the comparator and ignored by WinRSS

    def set_chassis_states(self, chassis, hexes):
        base = (chassis - 1) * 16
        if not 0 <= base < CMP_PORTS:
            return
        for i, ch in enumerate(hexes[:16]):
            n = base + i
            try:
                if ch in "aA":
                    st = CMP_STATES[10]        # data control
                elif ch in "bB":
                    st = CMP_STATES[11]        # local vote disabled
                else:
                    v = int(ch, 16)
                    if v == 0:
                        continue               # "No Change": the comparator repeats these
                    st = CMP_STATES[v]
            except (ValueError, IndexError):
                continue
            if self.ports[n]['state'] != st:
                self.ports[n]['state'] = st
                self.render(n)

    def render(self, n):
        p = self.ports[n]
        for c, val in enumerate((p['alias'], p['state'], p['hdlc'], p['delay'],
                                  "X" if p['voted'] else "", "X" if p['force'] else ""), start=1):
            it = self.tab.item(n, c)
            if it.text() != val:
                it.setText(val)


# ====================================================================== comparator diagnostics
class FanDiagPage(ServicePage):
    """The comparator's Diagnostics screen (WinRSS's ComparatorDiagnosticsScreenMaker): its
    only test is the station control module's cooling fan.  `SET SC_FAN ON` / `SET SC_FAN AUTO`
    (rss.jar 22300/22301); the reply carries no text and WinRSS treats an empty reply as
    success (ComparatorFanDiagPanel.updateFanONStatusLabel).  WinRSS puts the fan back to AUTO
    when the screen closes (the maker's "X" rule sends 22301), so leaving this page does too.
    Not exercised on comparator hardware yet."""
    title = "Diagnostics (fan)"
    comparator = True
    comparator_only = True          # a Quantar has no SC_FAN diagnostics screen in WinRSS
    station_controls = False
    help_tags = ("otac_fan_diag_hlp",)

    def __init__(self, main):
        super().__init__(main)
        for t in ("1. Turn the fan on - press \"Fan On\"",
                  "2. Verify that the fan is operating properly",
                  "3. Set the fan back to Auto mode - press \"Fan Auto\""):
            self.v.addWidget(QLabel(t))
        h = QHBoxLayout()
        h.addWidget(button("Fan On", lambda: self.set_fan(True), "Press to start the fan test"))
        h.addWidget(button("Fan Auto", lambda: self.set_fan(False), "Press to put fan in Auto mode"))
        self.fan = QLabel("Fan Status:")
        self.fan.setMinimumWidth(140)
        h.addWidget(self.fan)
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addWidget(para("The fan runs at full speed until it is put back to Auto, or until "
                               "this page is left."))
        self.v.addStretch(1)

    def _cmp(self):
        """False only when a Quantar is known to be connected: Link.kind is still None while
        the connect's probe runs, and the tree lists this page without a codeplug too."""
        L = self.svc.link
        return L is not None and (L.kind is None or L.is_comparator)

    def activate(self):
        super().activate()
        if self.svc.connected and not self._cmp():
            self.note.setText("This is the comparator's fan test - connect to an ASTRO-TAC.")
        else:
            self.note.setText("")

    def set_fan(self, on):
        if not self._cmp():
            self.status("The fan test is for a comparator (ASTRO-TAC) - connect to one.")
            return
        def then(_):
            self.fan.setText("Fan Status: %s" % ("ON" if on else "Auto"))
            self.status("Fan %s" % ("on" if on else "back in Auto mode"))
        self.call("SET SC_FAN " + ("ON" if on else "AUTO"), then=then, what="Fan test")

    def deactivate(self):
        super().deactivate()
        if self.svc.connected and self._cmp():
            # no callback: a failure here (e.g. on the way out of the app) is not worth a dialog
            self.svc.run(lambda L: L.call("SET SC_FAN AUTO"))
        self.fan.setText("Fan Status:")


# ====================================================================== terminal
class TerminalPage(ServicePage):
    title = "Station Terminal"
    station_controls = False
    comparator = True

    def __init__(self, main):
        super().__init__(main)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True); self.out.setFont(mono())
        self.out.setMaximumBlockCount(5000)
        self.v.addWidget(para("All traffic with the station (sent lines start with »).  Commands typed here are "
                                "sent as-is in the current mode."))
        self.v.addWidget(self.out, 1)
        h = QHBoxLayout()
        self.inp = QLineEdit(); self.inp.setFont(mono())
        self.inp.returnPressed.connect(self.send)
        h.addWidget(self.inp, 1)
        h.addWidget(button("Send", self.send))
        h.addWidget(button("Clear", self.out.clear))
        self.v.addLayout(h)
        self.svc.traffic.connect(self.on_traffic)

    def on_traffic(self, d, t):
        if d == '>':
            self.out.appendPlainText("» " + t)
        else:
            self.out.appendPlainText(t.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n'))

    def send(self):
        t = self.inp.text()
        self.inp.clear()
        self.job(lambda L: L.raw(t, 5.0), None, "Send")


# ====================================================================== alignment
class AlignTab(QWidget):
    help_tags = ()

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.v = QVBoxLayout(self)
        if self.help_tags:
            h = QHBoxLayout(); h.addStretch(1)
            h.addWidget(button("Procedure / Help", lambda: show_help(self, self.help_tags)))
            self.v.addLayout(h)

    # delegate helpers
    def call(self, *a, **k): self.page.call(*a, **k)
    def seq(self, *a, **k): self.page.seq(*a, **k)
    def job(self, *a, **k): self.page.job(*a, **k)
    def status(self, t): self.page.status(t)

    def enter(self):
        pass

    def leave(self):
        """Undo test settings when leaving the tab (WinRSS does this on exit)."""
        pass


class Stepper(QWidget):
    """Value display with -10/-1/+1/+10 buttons."""
    def __init__(self, title, fn, maximum=None, labels=("−10", "−1", "+1", "+10")):
        super().__init__()
        h = QHBoxLayout(self); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel(title))
        self.val = QLabel("N/A")
        self.val.setFont(mono())
        self.val.setMinimumWidth(40)
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.val)
        self.bar = QProgressBar()
        if maximum:
            self.bar.setRange(0, maximum)
        self.bar.setTextVisible(False)
        self.bar.setMinimumWidth(60)
        h.addWidget(self.bar, 1)
        for lbl, d in zip(labels, (-10, -1, 1, 10)):
            b = button(lbl, lambda d=d: fn(d)); b.setMaximumWidth(50)   # short ±N labels; keeps the row compact
            h.addWidget(b)

    def set(self, v):
        try:
            self.bar.setValue(int(v))
            self.val.setText(str(int(v)))
        except (TypeError, ValueError):
            pass


class RefOscTab(AlignTab):
    help_tags = ("do_station_pend",)
    REFS = [("Internal standard", "SET REF INT"), ("External 5 MHz", "SET REF 5MHZ"),
            ("External 10 MHz", "SET REF 10MHZ"), ("UHSO (high stability)", "SET REF EXT")]
    # The comparator's SET REF strcmps only "5MHZ" and "10MHZ" (flash 0x0E6B4C / 0x0E6B6A) and
    # answers ?(0018) option "<x>" is invalid for anything else, INT and EXT included.
    COMPARATOR_REFS = [("External 5 MHz", "SET REF 5MHZ"), ("External 10 MHz", "SET REF 10MHZ")]

    def __init__(self, page):
        super().__init__(page)
        self.refs = self.REFS
        f = QFormLayout()
        self.ref = QComboBox(); self.ref.addItems([r[0] for r in self.refs])
        f.addRow("Reference type", self.ref)
        self.freq = ro(); f.addRow("TX frequency (MHz)", self.freq)
        self.v.addLayout(f)
        self.step = Stepper("Oscillator setting", self.adjust, 215)
        self.v.addWidget(self.step)
        g = QGridLayout()
        for i, (t, fn) in enumerate((("KEYUP", self.keyup), ("DEKEY", self.dekey), ("Save", self.save),
                                     ("Read", self.read), ("Auto-net to external ref", self.autonet),
                                     ("Net status", self.netstatus),
                                     ("Abort auto-net", lambda: self.call("AL PEND ABORT")))):
            g.addWidget(button(t, fn), i // 4, i % 4)
        g.setColumnStretch(4, 1)
        self.v.addLayout(g)
        self.v.addWidget(para("Key up, measure the transmit frequency error and step the oscillator until the "
                                "error is as small as possible (internal standard: usually < 50 Hz), dekey, then Save."))
        self.v.addStretch(1)

    @property
    def uhso(self):
        return self.ref.currentIndex() == 3

    def enter(self):
        cmp = getattr(self.page, 'comparator', False)
        if cmp and self.refs is self.REFS:
            # The comparator's AL handler (0x085CD45C) knows RD, STATUS, NOREF, SAVE, AUTO and
            # ABORT only - no PEND UP/DN stepping, and the word UHSO appears nowhere in its
            # image, so AL UHSO ... is ?(0014).  Read and Save still work; the stepper does not.
            self.step.setEnabled(False)
            self.refs = self.COMPARATOR_REFS
            self.ref.clear(); self.ref.addItems([r[0] for r in self.refs])
        cp = self.page.main.cp
        if cp is not None:
            from .fields import codec
            c = codec('DSTCG', 'ref_selection')
            if c:
                v = c.get(cp, {}).upper()
                if cmp:
                    self.ref.setCurrentIndex(1 if "10" in v else 0)
                else:
                    self.ref.setCurrentIndex(3 if "UHSO" in v or "EXT" == v else 1 if "5" in v else 2 if "10" in v else 0)
        self.read()

    def read(self):
        def fn(L):
            L.call(self.refs[self.ref.currentIndex()][1])
            v = L.get("AL UHSO RD" if self.uhso else "AL PEND RD")
            f = L.get("AL PEND NOREF")
            return v, f
        def show(r):
            self.step.set(215 - int(r[0]) if not self.uhso else r[0])
            self.freq.setText(mhz(r[1]))
        self.job(fn, show, "Read reference oscillator")

    def adjust(self, d):
        up = d > 0
        if self.uhso:
            cmd = "AL UHSO UP [%s]" if up else "AL UHSO DN [%s]"
        else:
            cmd = "AL PEND UP [%s]" if up else "AL PEND DN [%s]"
        rd = "AL UHSO RD" if self.uhso else "AL PEND RD"
        self.job(lambda L: (L.call(cmd, abs(d)), L.get(rd))[1],
                 lambda v: self.step.set(215 - int(v) if not self.uhso else v), "Adjust oscillator")

    def keyup(self):
        self.call("KEYUP", timeout=10, then=lambda r: self.status("Keyed"))

    def dekey(self):
        self.seq(self.dekey_steps(), then=lambda r: self.status("Dekeyed"))

    def dekey_steps(self):
        # the comparator's alignment screen has DEKEY but no SET FREQ TX (it has no transmitter)
        if getattr(self.page, 'comparator', False):
            return [("DEKEY", ())]
        return [("DEKEY", ()), ("SET FREQ TX [%s]", (0,))]

    def save(self):
        self.call("AL UHSO SAVE" if self.uhso else "AL PEND SAVE", then=lambda r: self.status("Reference oscillator saved"))

    def autonet(self):
        ref = self.refs[self.ref.currentIndex()][1]
        if ref not in ("SET REF 5MHZ", "SET REF 10MHZ"):
            dlg.information(self, "Auto-net", "Select an external 5 or 10 MHz reference first.")
            return

        # The firmware only nets the pendulum against the external reference on a simulcast
        # station (it needs a 1PPS): a conventional codeplug answers `GET GPS STAT` with
        # "SIMULCAST IS DISABLED" and the state then sits at PEND UNLOCKED however good the
        # reference on the input is.  Say so rather than let the operator chase it.
        def fn(L):
            gps = L.raw("GET GPS STAT")[0].strip()
            L.call(ref)
            L.call("AL PEND AUTO", timeout=10)
            return gps

        def done(gps):
            if "DISABLED" in gps:
                self.status("Auto-netting started - the station reports SIMULCAST IS DISABLED, so it "
                            "has no 1PPS to net to and the state will stay UNLOCKED")
            else:
                self.status("Auto-netting started (%s)" % gps)

        self.job(fn, done, "Auto-net")

    def netstatus(self):
        # the netting task does not answer the first query after AL PEND AUTO (? (0010) "a
        # required task has not responded"), so retry before reporting a failure
        def fn(L):
            for i in range(4):
                try:
                    return L.call("AL PEND STATUS", timeout=6)
                except StationError:
                    if i == 3:
                        raise
                    time.sleep(1.0)
        self.job(fn, lambda r: self.status("Net status: %s" % " ".join(r)))

    def leave(self):
        self.page.seq(self.dekey_steps())


class TxDevTab(AlignTab):
    help_tags = ("do_txgn",)
    MIN, MAX = 1.82, 10.0

    def __init__(self, page):
        super().__init__(page)
        g = QGridLayout()
        g.addWidget(QLabel("<b>Test frequency</b>"), 0, 0)
        g.addWidget(QLabel("<b>Measured deviation (kHz)</b>"), 0, 1)
        g.addWidget(QLabel("<b>Stored (kHz)</b>"), 0, 2)
        self.edits, self.stored, self.keys = [], [], []
        for i in range(4):
            b = button("Key on Freq %d" % (i + 1), lambda i=i: self.key(i))
            e = QDoubleSpinBox(); e.setRange(0, 10); e.setDecimals(3); e.setSingleStep(0.01); e.setMaximumWidth(110)
            s = ro(); s.setMaximumWidth(90)
            g.addWidget(b, i + 1, 0); g.addWidget(e, i + 1, 1); g.addWidget(s, i + 1, 2)
            self.keys.append(b); self.edits.append(e); self.stored.append(s)
        g.setColumnStretch(3, 1)              # soak up spare width so the columns stay compact
        self.v.addLayout(g)
        f = QFormLayout()
        self.freq = ro(); f.addRow("Current frequency (MHz)", self.freq)
        self.keystate = ro(""); f.addRow("Key on status", self.keystate)
        self.v.addLayout(f)
        h = QHBoxLayout()
        h.addWidget(button("Save", self.save)); h.addWidget(button("Dekey", self.dekey)); h.addWidget(button("Read", self.enter))
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addWidget(para("Set the analyzer to WIDEBAND.  Key on each test frequency, measure the deviation "
                                "(higher of + and −) and enter it, then Save.  F2 must be ≥ 90 % of F1 (F4 of F3)."))
        self.v.addStretch(1)

    def enter(self):
        def show(r):
            for i, v in enumerate(r):
                try:
                    k = int(v) / 1000.0
                except ValueError:
                    continue
                self.edits[i].setValue(k)
                self.stored[i].setText("%.3f" % k)
        self.call("AL TXDEV RD", then=show)

    def key(self, i):
        def show(r):
            self.freq.setText(mhz(r[0]))
            self.keystate.setText("Keyed up on frequency %d" % (i + 1))
        self.call("AL TXDEV GO F%d" % (i + 1), timeout=10, then=show)

    def dekey(self):
        self.seq([("DEKEY", ()), ("SET FREQ TX 0", ())], then=lambda r: (self.keystate.setText("Dekeyed"),
                                                                     self.freq.setText("N/A")))

    def save(self):
        v = [e.value() for e in self.edits]
        for i in (0, 2):
            a, b = v[i], v[i + 1]
            if a == 0 and b == 0 and i == 2:
                continue
            if not self.MIN <= a <= self.MAX:
                return dlg.warning(self, "TX deviation", "Frequency %d deviation must be %.2f to %.2f kHz" % (i + 1, self.MIN, self.MAX))
            if not max(self.MIN, 0.9 * a) <= b <= self.MAX:
                return dlg.warning(self, "TX deviation", "Frequency %d deviation must be ≥ 90%% of frequency %d (%.3f kHz) and ≤ %.2f kHz"
                                   % (i + 2, i + 1, max(self.MIN, 0.9 * a), self.MAX))
        hz = [int(round(x * 1000)) for x in v]
        def done(r):
            # AL TXDEV RD shows a RAM copy that only AL TXDEV GO refreshes, so re-reading here
            # would put the values from the last key-up back in the Stored column.  The write is
            # what SAVE just committed, so show that.
            for i, h in enumerate(hz):
                self.stored[i].setText("%.3f" % (h / 1000.0))
            self.status("TX deviation saved")
        self.seq([("AL TXDEV WR [%s] [%s] [%s] [%s]", hz), ("AL TXDEV SAVE", ())], then=done)

    def leave(self):
        self.page.seq([("DEKEY", ()), ("SET TX PWR [%s]", (0,)), ("SET TEST_TONE OFF", ()), ("SET FREQ TX 0", ()),
                       ("SET EEP TXDEV -1", ())])


class PowerTab(AlignTab):
    help_tags = ("do_pwr3",)

    def __init__(self, page):
        super().__init__(page)
        f = QFormLayout()
        self.freq = ro(); f.addRow("TX frequency (MHz)", self.freq)
        self.desired = QSpinBox(); self.desired.setRange(1, 500); self.desired.setSuffix(" W")
        f.addRow("Desired power out", self.desired)
        self.actual = QDoubleSpinBox(); self.actual.setRange(0.01, 500); self.actual.setDecimals(2); self.actual.setSuffix(" W")
        f.addRow("Wattmeter reading", self.actual)
        self.v.addLayout(f)
        h = QHBoxLayout()
        for t, fn in (("PA Initialize", self.pa_init), ("KEYUP", self.keyup), ("Adjust", self.adjust),
                      ("Save", self.save), ("DEKEY", self.dekey)):
            h.addWidget(button(t, fn))
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addWidget(para("Connect the wattmeter at the PA output.  KEYUP, read the wattmeter, enter the reading "
                                "and press Adjust; repeat until it reads the desired power, then Save."))
        self.v.addStretch(1)

    def enter(self):
        cp = self.page.main.cp
        if cp is not None:
            self.desired.setValue(cp.img[cp.si + 0x29] or 100)
        self.call("GET TX FREQ", then=lambda r: self.freq.setText(mhz(r[0])))

    def pa_init(self):
        self.call("AL STNPWR RESET", timeout=10, then=lambda r: self.status("PA initialized"))

    def keyup(self):
        self.seq([("SET TX PWR [%s]", (self.desired.value(),)), ("KEYUP", (), 10)], then=lambda r: self.status("Keyed"))

    def adjust(self):
        self.call("AL STNPWR WR [%s] [%s]", int(self.desired.value() * 100), int(round(self.actual.value() * 100)),
                  timeout=10, then=lambda r: self.status("Adjusted - read the wattmeter again"))

    def save(self):
        self.seq([("AL STNPWR SAVE", (), 10), ("SET FREQ TX [%s]", (0,)), ("DEKEY", ())],
                 then=lambda r: self.status("Power output saved"))

    def dekey(self):
        self.seq([("SET FREQ TX [%s]", (0,)), ("DEKEY", ())], then=lambda r: self.status("Dekeyed"))

    def leave(self):
        self.page.seq([("DEKEY", ()), ("SET FREQ TX [%s]", (0,)), ("SET TX PWR [%s]", (0,))])


def _rx_channels(cp):
    """(first channel using RX1, first channel with an RX2 frequency), 1-based, from the loaded codeplug."""
    if cp is None:
        return None, None
    import struct
    rx1 = rx2 = None
    d17 = cp.dir[17] & 0x7FFFFFFF
    for i in range(cp.nchan()):
        if rx1 is None and struct.unpack_from('>I', cp.img, cp.chan_off(i))[0]:
            rx1 = i + 1
        if rx2 is None and struct.unpack_from('>I', cp.img, d17 + 2 + 64 * i)[0]:
            rx2 = i + 1
    return rx1, rx2


class RxSelect(QWidget):
    """Select RX1 / RX2 channel (SET CHN CUR) + show current channel / frequencies."""
    def __init__(self, tab, after):
        super().__init__()
        self.tab, self.after = tab, after
        # two compact rows so the widget does not force the page wide (it was one ~730 px row)
        g = QGridLayout(self); g.setContentsMargins(0, 0, 0, 0)
        g.addWidget(QLabel("Channel"), 0, 0)
        self.chan = QSpinBox(); self.chan.setRange(1, 255); self.chan.setMaximumWidth(70)
        g.addWidget(self.chan, 0, 1)
        g.addWidget(button("Select RX1", lambda: self.select(0)), 0, 2)
        g.addWidget(button("Select RX2", lambda: self.select(1)), 0, 3)
        self.rx1 = ro(); self.rx2 = ro()
        g.addWidget(QLabel("RX1 MHz"), 1, 0); g.addWidget(self.rx1, 1, 1)
        g.addWidget(QLabel("RX2 MHz"), 1, 2); g.addWidget(self.rx2, 1, 3)
        g.setColumnStretch(4, 1)

    def read(self):
        def fn(L):
            out = []
            for c in ("GET CHN CUR", "GET RX FREQ", "GET RX2 FREQ"):
                try:
                    out.append(L.get(c))
                except Exception:
                    out.append("")
            return out
        def show(r):
            if r[0].isdigit():
                self.chan.setValue(int(r[0]))
            self.rx1.setText(mhz(r[1]) if r[1] else "N/A")
            self.rx2.setText(mhz(r[2]) if r[2] and r[2] != "0" else "N/A")
            self.after()
        self.tab.job(fn, show, "Read receiver channel")

    def select(self, which):
        rx = _rx_channels(self.tab.page.main.cp)[which]
        n = rx or self.chan.value()
        self.tab.call("SET CHN CUR [%s]", n, then=lambda r: self.read())


class RssiTab(AlignTab):
    help_tags = ("do_rssi",)

    def __init__(self, page):
        super().__init__(page)
        self.rx = RxSelect(self, lambda: None)
        self.v.addWidget(self.rx)
        f = QFormLayout()
        self.level = QSpinBox(); self.level.setRange(-105, -50); self.level.setValue(-90); self.level.setSuffix(" dBm")
        f.addRow("Unmodulated RF level at the receiver", self.level)
        self.v.addLayout(f)
        self.result = ro("")
        h = QHBoxLayout(); h.addWidget(button("Start RSSI Calibration", self.run)); h.addWidget(self.result); h.addStretch(1)
        self.v.addLayout(h)
        self.v.addStretch(1)

    def enter(self):
        self.rx.read()

    def run(self):
        self.result.setText("Calibrating…")
        self.call("AL RSSI [%s]", -self.level.value(), timeout=30,
                  then=lambda r: self.result.setText("Calibration complete"))


class RefModTab(AlignTab):
    help_tags = ("do_refm",)

    def __init__(self, page):
        super().__init__(page)
        h = QHBoxLayout()
        h.addWidget(button("Align REF MOD 1", lambda: self.align(1)))
        h.addWidget(button("Align REF MOD 2", lambda: self.align(2)))
        self.freq = ro(); h.addWidget(QLabel("Frequency (MHz)")); h.addWidget(self.freq); h.addStretch(1)
        self.v.addLayout(h)
        self.step = Stepper("Ref mod setting", self.adjust, 127)
        self.v.addWidget(self.step)
        h2 = QHBoxLayout(); h2.addWidget(button("Save", self.save)); h2.addWidget(button("DEKEY", self.dekey)); h2.addStretch(1)
        self.v.addLayout(h2)
        self.v.addWidget(para("The station modulates with a 10 Hz square wave.  Watch the recovered audio (DC "
                                "coupled) and step until the flat parts of the square wave are as straight as possible."))
        self.v.addStretch(1)

    def align(self, n):
        self.job(lambda L: (L.get("AL REFMOD F%d" % (4 + n), timeout=10), L.get("AL REFMOD RD")),
                 lambda r: (self.freq.setText(mhz(r[0])), self.step.set(r[1])), "Ref mod alignment")

    def adjust(self, d):
        cmd = "AL REFMOD UP [%s]" if d > 0 else "AL REFMOD DN [%s]"
        self.job(lambda L: (L.call(cmd, abs(d)), L.get("AL REFMOD RD"))[1], self.step.set, "Adjust ref mod")

    def save(self):
        self.call("AL REFMOD SAVE", then=lambda r: self.status("Ref mod saved"))

    def dekey(self):
        self.seq([("DEKEY", ()), ("SET FREQ TX [%s]", (0,))], then=lambda r: self.status("Dekeyed"))

    def leave(self):
        self.page.seq([("DEKEY", ()), ("SET TX PWR [%s]", (0,)), ("SET TEST_TONE OFF", ()), ("SET FREQ TX [%s]", (0,)),
                       ("SET EEP REFMOD -1", ())])


class BatteryTab(AlignTab):
    help_tags = ("battery_equ_help",)

    def __init__(self, page):
        super().__init__(page)
        f = QFormLayout()
        self.state = ro(); f.addRow("Battery equalization", self.state)
        self.btype = ro(); f.addRow("Battery type", self.btype)
        self.v.addLayout(f)
        h = QHBoxLayout()
        h.addWidget(button("Equalization ON", lambda: self.call("SET PS EQUAL ON", then=lambda r: self.enter())))
        h.addWidget(button("Equalization OFF", lambda: self.call("SET PS EQUAL OFF", then=lambda r: self.enter())))
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addStretch(1)

    def enter(self):
        self.job(lambda L: (L.get("GET PS EQUAL"), L.get("GET PS BATT_TYPE")),
                 lambda r: (self.state.setText(r[0]), self.btype.setText(r[1])), "Battery equalization")


class SquelchTab(AlignTab):
    help_tags = ("do_sque",)

    def __init__(self, page):
        super().__init__(page)
        self.rx = RxSelect(self, self.read)
        self.v.addWidget(self.rx)
        h = QHBoxLayout()
        h.addWidget(button("Fully open (0)", lambda: self.write("AL SQ WR 0")))
        h.addWidget(button("Fully tight (127)", lambda: self.write("AL SQ WR 127")))
        h.addStretch(1)
        self.v.addLayout(h)
        self.step = Stepper("Squelch (0 open … 127 tight)", self.adjust, 127, ("−10", "−1", "+1", "+10"))
        self.v.addWidget(self.step)
        f = QFormLayout()
        self.wl = ro(); f.addRow("Wireline squelch mode", self.wl)
        self.v.addLayout(f)
        h2 = QHBoxLayout()
        h2.addWidget(button("WL squelch NORMAL", lambda: self.call("SET WL SQL NORMAL", then=lambda r: self.read())))
        h2.addWidget(button("WL squelch CSQ", lambda: self.call("SET WL SQL CSQ", then=lambda r: self.read())))
        h2.addWidget(button("Save", lambda: self.call("AL SQ SAVE", then=lambda r: self.status("Squelch saved"))))
        h2.addStretch(1)
        self.v.addLayout(h2)
        self.v.addWidget(para("Apply an unmodulated carrier at the level where squelch should open (typ. 12 dB "
                                "SINAD).  Open fully, then tighten until the squelch closes (wait 1 s between "
                                "steps), open again until it just opens, then Save."))
        self.v.addStretch(1)

    def enter(self):
        self.rx.read()

    def read(self):
        def fn(L):
            out = [L.get("AL SQ RD")]
            try:
                out.append(L.get("GET WL SQL"))
            except Exception:
                out.append("N/A")
            return out
        self.job(fn, lambda r: (self.step.set(r[0]), self.wl.setText(r[1])), "Read squelch")

    def write(self, cmd):
        self.job(lambda L: (L.call(cmd), L.get("AL SQ RD"))[1], self.step.set, "Squelch")

    def adjust(self, d):
        cmd = "AL SQ UP [%s]" if d > 0 else "AL SQ DN [%s]"
        self.job(lambda L: (L.call(cmd, abs(d)), L.get("AL SQ RD"))[1], self.step.set, "Squelch")


class RxWirelineTab(AlignTab):
    help_tags = ("rxwi_adjust_help", "rxwi_1khz_tone_help", "rxwi_tone_level_help", "lo_boost_level_help",
                 "hi_boost_level_help", "hi_boost_freq_help", "align_status_tone_help")

    def __init__(self, page, line4=False):
        self.line4 = line4
        if line4:
            self.help_tags = ("rxwi4_adjust_help", "rxwi_1khz_tone_help")
        super().__init__(page)
        c = "C2" if line4 else "C1"
        self.c = c
        items = [("1KHZ", "1 kHz line level (dBm)", -20.0, 0.0)]
        if not line4:
            items += [("LB", "Low boost level (dB)", 0.0, 6.0), ("HB", "High boost level (dB)", 0.0, 9.0),
                      ("HBF", "High boost frequency (Hz)", 2000, 3000), ("ST", "Status tone level (dBm)", -30.0, 6.0),
                      ("AT", "Alert tone level (dBm)", -45.0, 0.0)]
        g = QGridLayout()
        g.addWidget(QLabel("<b>Setting</b>"), 0, 0); g.addWidget(QLabel("<b>Current</b>"), 0, 1)
        g.addWidget(QLabel("<b>Stored</b>"), 0, 2)
        self.items = {}
        for r, (k, lbl, lo, hi) in enumerate(items, 1):
            e = QDoubleSpinBox(); e.setRange(lo, hi); e.setDecimals(0 if k == "HBF" else 1)
            e.setSingleStep(10 if k == "HBF" else 0.5); e.setMaximumWidth(110)
            e.editingFinished.connect(lambda k=k: self.write(k))
            s = ro(); s.setMaximumWidth(90)
            g.addWidget(QLabel(lbl), r, 0); g.addWidget(e, r, 1); g.addWidget(s, r, 2)
            self.items[k] = (e, s)
        g.setColumnStretch(3, 1)
        self.v.addLayout(g)
        tones = [("1 kHz test tone", "SET TEST_TONE WL %s 1KHZ" % c)]
        if not line4:
            tones += [("Low boost tone", "SET TEST_TONE WL C1 LB"), ("High boost tone", "SET TEST_TONE WL C1 HB"),
                      ("Status tone", "SET TEST_TONE WL C1 ST")]
        tones += [("Tone off", "SET TEST_TONE OFF")]
        hb = QGridLayout()                    # wrap the tone buttons (3/row) so the row does not run wide
        for i, (t, cmd) in enumerate(tones):
            hb.addWidget(button(t, lambda cmd=cmd: self.call(cmd)), i // 3, i % 3)
        hb.addWidget(button("Save", self.save), len(tones) // 3, len(tones) % 3)
        hb.setColumnStretch(3, 1)
        self.v.addLayout(hb)
        self.v.addWidget(para("Turn on a test tone, measure it on the line and change the current value "
                                "(press Enter to send it) until the level is right, then Save."))
        self.v.addStretch(1)

    def enter(self):
        def fn(L):
            out = {}
            for k in self.items:
                try:
                    out[k] = L.get("AL RXWL %s RD %s" % ("C2" if self.line4 and k == "1KHZ" else "C1", k))
                except Exception:
                    out[k] = None
            return out
        def show(out):
            for k, v in out.items():
                e, s = self.items[k]
                s.setText(v if v is not None else "N/A")
                try:
                    e.blockSignals(True); e.setValue(float(v)); e.blockSignals(False)
                except (TypeError, ValueError):
                    e.blockSignals(False)
        self.job(fn, show, "Read RX wireline")

    def write(self, k):
        e, _ = self.items[k]
        v = e.value()
        val = str(int(v)) if k == "HBF" else ("%.1f" % v)
        c = "C2" if (self.line4 and k == "1KHZ") else "C1"
        self.call("AL RXWL %s WR %s [%%s]" % (c, k), val)

    def save(self):
        self.call("AL RXWL %s SAVE" % self.c, then=lambda r: (self.status("RX wireline saved"), self.enter()))

    def leave(self):
        self.page.call("SET TEST_TONE OFF")


class TxWirelineTab(AlignTab):
    help_tags = ("do_txwi", "ref_level_hlp")

    def __init__(self, page):
        super().__init__(page)
        g = QGridLayout()
        g.addWidget(QLabel("<b>Line</b>"), 0, 0); g.addWidget(QLabel("<b>Reference level (dBm)</b>"), 0, 1)
        g.addWidget(QLabel("<b>Stored</b>"), 0, 2)
        self.lvl, self.stored = [], []
        for i, name in enumerate(("Line 1 (2/4 wire)", "Line 3 (6/8 wire)")):
            e = QSpinBox(); e.setRange(-35, 10); e.setMaximumWidth(110); s = ro(); s.setMaximumWidth(90)
            g.addWidget(QLabel(name), i + 1, 0); g.addWidget(e, i + 1, 1); g.addWidget(s, i + 1, 2)
            g.addWidget(button("Calibrate", lambda i=i: self.calibrate(i)), i + 1, 3)
            self.lvl.append(e); self.stored.append(s)
        g.setColumnStretch(4, 1)
        self.v.addLayout(g)
        h = QHBoxLayout()
        h.addWidget(button("TX wireline KEYUP", lambda: self.call("AL TXWL KEYUP 10", timeout=10)))
        h.addWidget(button("TX wireline DEKEY", lambda: self.call("AL TXWL DEKEY")))
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addWidget(para("Apply a 1 kHz tone at the reference level to the wireline, set the level here and "
                                "press Calibrate.  The station will transmit it at 60 % of rated deviation."))
        self.v.addStretch(1)

    def enter(self):
        def fn(L):
            out = []
            for c in ("C1", "C2"):
                try:
                    out.append(L.get("AL TXWL RD_REF %s 5" % c))
                except Exception:
                    out.append(None)
            return out
        def show(r):
            for i, v in enumerate(r):
                self.stored[i].setText(v if v is not None else "N/A")
                try:
                    self.lvl[i].setValue(int(float(v)))
                except (TypeError, ValueError):
                    pass
        self.job(fn, show, "Read TX wireline")

    def calibrate(self, i):
        c = "C%d" % (i + 1)
        self.seq([("AL TXWL WR_REF %s [%%s]" % c, (self.lvl[i].value(),)), ("AL TXWL GO %s 5" % c, (), 30)],
                 then=lambda r: (self.status("TX wireline calibrated"), self.enter()))

    def leave(self):
        self.page.call("AL TXWL DEKEY")


class TdataTab(AlignTab):
    help_tags = ("tdata_deviation_help",)

    def __init__(self, page):
        super().__init__(page)
        f = QFormLayout(); self.freq = ro(); f.addRow("TX frequency (MHz)", self.freq); self.v.addLayout(f)
        h = QHBoxLayout()
        h.addWidget(button("KEYUP (TDATA on)", lambda: self.seq([("SET TDATA ON", ()), ("KEYUP", (), 10)])))
        h.addWidget(button("DEKEY", self.dekey))
        h.addWidget(button("TDATA calibrate", lambda: self.call("AL TDATA GO", timeout=10,
                                                                then=lambda r: self.status("TDATA calibrated"))))
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addStretch(1)

    def enter(self):
        self.call("GET TX FREQ", then=lambda r: self.freq.setText(mhz(r[0])))

    def dekey(self):
        self.seq([("SET TDATA OFF", ()), ("DEKEY", ()), ("SET FREQ TX [%s]", (0,))])

    def leave(self):
        self.dekey()


class LtoTab(AlignTab):
    help_tags = ("desired_launch_help",)

    def __init__(self, page):
        super().__init__(page)
        f = QFormLayout()
        self.freq = ro(); f.addRow("TX frequency (MHz)", self.freq)
        self.cur = ro(); f.addRow("Stored launch time offset", self.cur)
        self.lto = QSpinBox(); self.lto.setRange(-100000, 100000); f.addRow("Desired launch time offset", self.lto)
        self.v.addLayout(f)
        h = QHBoxLayout()
        h.addWidget(button("Write", lambda: self.call("AL LTO WR  [%s]", self.lto.value())))
        h.addWidget(button("Save", lambda: self.call("AL LTO SAVE", then=lambda r: self.enter())))
        h.addWidget(button("Start test pattern", lambda: self.call("AL LTO TEST_ON")))
        h.addWidget(button("Stop test pattern", lambda: self.call("AL LTO TEST_OFF")))
        h.addStretch(1)
        self.v.addLayout(h)
        self.v.addStretch(1)

    def enter(self):
        self.job(lambda L: (L.get("GET TX FREQ"), L.get("AL LTO RD")),
                 lambda r: (self.freq.setText(mhz(r[0])), self.cur.setText(r[1])), "Read launch time offset")

    def leave(self):
        self.page.seq([("AL LTO TEST_OFF", ()), ("AL LTO RESET", ())])


class AlignmentPage(ServicePage):
    title = "Alignment"
    help_tags = ("do_aln",)

    def tabs(self):
        """(name, widget) per alignment section; the comparator overrides this."""
        return [("Reference Oscillator", RefOscTab(self)), ("TX Deviation", TxDevTab(self)),
                ("Power Output", PowerTab(self)), ("RSSI Calibration", RssiTab(self)),
                ("Reference Modulation", RefModTab(self)), ("Battery Equalization", BatteryTab(self)),
                ("Squelch Adjust", SquelchTab(self)), ("RX Wireline (line 2)", RxWirelineTab(self)),
                ("RX Wireline (line 4)", RxWirelineTab(self, True)), ("TX Wireline", TxWirelineTab(self)),
                ("Astro Simulcast (LTO)", LtoTab(self)), ("TDATA Calibration", TdataTab(self))]

    def __init__(self, main):
        super().__init__(main)
        self.v.addWidget(para("The station is <b>access disabled</b> while this screen is open.  Leaving it offers "
                                "to re-enable access and reset the station."))
        self.nav = QListWidget()
        self.nav.setMinimumWidth(180)      # show the full alignment names; it was collapsing and clipping
        self.nav.setMaximumWidth(210)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack = QStackedWidget()
        self.list = self.tabs()
        for t, w in self.list:
            w.title = t                       # heading for its topics in the help pane
            self.nav.addItem(t)
            box = QGroupBox(t)
            bl = QVBoxLayout(box)
            bl.addWidget(w)
            self.stack.addWidget(box)
        self.cur = 0
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._tab)
        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        self.v.addLayout(body, 1)

    def _tab(self, i):
        if i < 0:
            return
        self.stack.setCurrentIndex(i)
        if self.active and self.svc.connected:
            self.list[self.cur][1].leave()
            self.cur = i
            self.list[i][1].enter()
        self.cur = i
        label, tab = self.list[i]
        if tab.help_tags and hasattr(self.main, 'help'):
            self.main.help.show_other(label, tab.help_tags)

    def activate(self):
        super().activate()
        if self.svc.connected:
            self.call("FP ACC_DIS ON", then=lambda r: self.note.setText("ACCESS DISABLED"))
            self.cur = max(self.nav.currentRow(), 0)
            self.list[self.cur][1].enter()

    def deactivate(self):
        super().deactivate()
        if not self.svc.connected:
            return
        self.list[self.cur][1].leave()
        r = dlg.question(self, "Leaving alignment",
                         "Re-enable station access and reset the station?\n\n"
                         "(Unsaved alignment changes stay active until the station is reset.)",
                         dlg.SB.Yes | dlg.SB.No, dlg.SB.Yes)
        if r == dlg.SB.Yes:
            def fn(L):
                L.call("FP ACC_DIS OFF")
                L.reset()            # ' RESET' (21352): the app restarts, ~12 s, back at ]-O
            self.job(fn, lambda r: self.status("Access enabled, station reset"), "Leave alignment")


class ComparatorAlignmentPage(AlignmentPage):
    """The comparator's Alignment screen (WinRSS's ComparatorAlignmentScreenMaker): station
    access and reset, then two sections - battery equalization (GET/SET PS EQUAL, GET PS
    BATT_TYPE; rss.jar 21400-21403) and the reference oscillator / PEND (216xx), the same
    commands as the Quantar's tabs.  There is no transmitter alignment: the comparator has no
    PA.  Not exercised on comparator hardware yet."""
    title = "Alignment"
    comparator = True
    comparator_only = True          # the Quantar's Alignment page has the TX tabs as well
    help_tags = ("do_otac_pend", "otac_bateq_help")

    def tabs(self):
        ro = RefOscTab(self)
        ro.help_tags = ("do_otac_pend",)
        bat = BatteryTab(self)
        bat.help_tags = ("otac_bateq_help",)
        return [("Reference Oscillator", ro), ("Battery Equalization", bat)]


# ====================================================================== connect dialog
class ConnectDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Connect to station")
        f = QFormLayout(self)
        self.port = QComboBox(); self.port.setEditable(True)
        ports = []
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
        except Exception:
            pass
        self.port.addItems(ports + ["SIMULATOR", "SIMULATOR-COMPARATOR"])
        s = QSettings("qrss", "qrss")
        last = s.value("station/port", "")
        if last:
            self.port.setCurrentText(last)
        self.baud = QComboBox(); self.baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud.setCurrentText(str(s.value("station/baud", "9600")))
        self.mmi = QComboBox()
        for b, tag in ((9600, "  (as reset)"), (57600, ""), (115200, "  (HAM)")):
            self.mmi.addItem("%d%s" % (b, tag), b)
        self.mmi.setCurrentIndex(max(0, self.mmi.findData(s.value("station/mmi_baud", 9600, type=int))))
        self.mmi.setToolTip("Speed to run the serial link at after connecting, set with PCTL -BAUD.\n"
                            "Needs H026.11.05f+ (HAM) firmware; a stock station refuses and stays at\n"
                            "9600.  Every station reset is back at 9600.")
        self.pwd = QLineEdit(str(s.value("station/password", "")))
        self.pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd.setToolTip("The station's password, for a station that answers the MMI with\n"
                            "\"Password:\" (a comparator, or a Quantar from release 5.0).\n"
                            "Leave it empty for an unprotected station.  Sent uppercase; the\n"
                            "firmware is case-insensitive.  Password protection can be turned on,\n"
                            "changed or erased from Station ▸ Set Device Password…")
        # dial-up (RSS manual chapter 9): the serial port is then the PC's modem, which is
        # initialized and dialed before the MMI session starts
        self.kind = QComboBox()
        self.kind.addItem("Serial port (direct)", "serial")
        self.kind.addItem("Modem (dial-up)", "modem")
        self.kind.setCurrentIndex(1 if str(s.value("station/kind", "serial")) == "modem" else 0)
        self.number = QLineEdit(str(s.value("station/phone", "")))
        self.number.setToolTip("The station's phone number, dialed with the modem's ATDT prefix.")
        self.book = QComboBox()
        self.book.addItem("(phone book)", "")
        for name, num in phone_book():
            self.book.addItem("%s  -  %s" % (name, num), num)
        self.book.currentIndexChanged.connect(self._from_book)
        row = QHBoxLayout(); row.addWidget(self.number, 1); row.addWidget(self.book)
        self.book_btn = QPushButton("Phone Book…"); self.book_btn.clicked.connect(self.edit_book)
        self.mcfg = QPushButton("Modem Settings…"); self.mcfg.clicked.connect(self.edit_modem)
        row.addWidget(self.book_btn); row.addWidget(self.mcfg)
        self.mrow = QWidget(); self.mrow.setLayout(row)
        f.addRow("Connection", self.kind)
        f.addRow("Serial port", self.port)
        f.addRow("Baud rate", self.baud)
        f.addRow("MMI speed", self.mmi)
        f.addRow("Phone number", self.mrow)
        f.addRow("Password", self.pwd)
        f.addRow(QLabel("Front-panel RS-232 port, 8N1.  WinRSS uses 9600 baud.\n"
                        "SIMULATOR runs a pretend Quantar, SIMULATOR-COMPARATOR an ASTRO-TAC,\n"
                        "for trying the screens.\n"
                        "For dial-up, the serial port is the PC's modem (9600 to the station)."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

    def _kind_changed(self):
        modem = self.kind.currentData() == "modem"
        self.mrow.setEnabled(modem)
        self.number.setEnabled(modem)
        self.book.setEnabled(modem)
        self.book_btn.setEnabled(modem)
        self.mcfg.setEnabled(modem)

    def _from_book(self, i):
        num = self.book.currentData()
        if num:
            self.number.setText(num)

    def edit_book(self):
        d = PhoneBookDialog(self)
        if dlg._run(d, self) == QDialog.DialogCode.Accepted:
            self.book.blockSignals(True)
            self.book.clear(); self.book.addItem("(phone book)", "")
            for name, num in phone_book():
                self.book.addItem("%s  -  %s" % (name, num), num)
            self.book.blockSignals(False)

    def edit_modem(self):
        d = ModemSettingsDialog(self)
        dlg._run(d, self)

    def values(self):
        s = QSettings("qrss", "qrss")
        s.setValue("station/port", self.port.currentText())
        s.setValue("station/baud", self.baud.currentText())
        s.setValue("station/mmi_baud", self.mmi.currentData())
        s.setValue("station/password", self.pwd.text().strip())
        s.setValue("station/kind", self.kind.currentData())
        s.setValue("station/phone", self.number.text().strip())
        modem = None
        if self.kind.currentData() == "modem":
            modem = {"number": self.number.text().strip(), "cfg": modem_config()}
        return (self.port.currentText(), int(self.baud.currentText()), int(self.mmi.currentData()),
                self.pwd.text().strip(), modem)


def modem_config():
    """The saved Modem Settings, with WinRSS' defaults for anything unset."""
    from .modem import DEFAULTS
    s = QSettings("qrss", "qrss")
    c = {}
    for k, v in DEFAULTS.items():
        raw = s.value("modem/" + k)
        if raw is None:
            c[k] = v
        elif isinstance(v, bool):
            c[k] = str(raw) in ("true", "True", "1")
        elif isinstance(v, int):
            try:
                c[k] = int(raw)
            except (TypeError, ValueError):
                c[k] = v
        elif isinstance(v, list):
            c[k] = [x for x in str(raw).split("|") if x] if isinstance(raw, str) else list(raw)
        else:
            c[k] = str(raw)
    return c


def phone_book():
    """[(name, number)] from the settings, in the order the user keeps them."""
    s = QSettings("qrss", "qrss")
    raw = s.value("modem/phonebook", [])
    if isinstance(raw, str):
        raw = [raw] if raw else []
    out = []
    for item in raw:
        if "|" in item:
            name, num = item.split("|", 1)
            out.append((name, num))
    return out


class ModemSettingsDialog(QDialog):
    """WinRSS' Modem Configuration screen: the command strings, the response strings, and the
    line conditioning (RSS manual table 9-1 and chapter 9).  The defaults are WinRSS' own."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Modem settings")
        v = QVBoxLayout(self)
        c = modem_config()
        g = QGroupBox("Command strings"); f = QFormLayout(g)
        self.init = QLineEdit(c["init"])
        self.prefix = QLineEdit(c["prefix"])
        self.hangup = QLineEdit(c["hangup"])
        self.wait = QSpinBox(); self.wait.setRange(10, 300); self.wait.setValue(int(c["wait"]))
        self.pause = QSpinBox(); self.pause.setRange(5, 300); self.pause.setValue(int(c["pause"]))
        self.dtr = QCheckBox("Drop DTR to hang up"); self.dtr.setChecked(bool(c["drop_dtr"]))
        f.addRow("Initialization", self.init)
        f.addRow("Dial prefix", self.prefix)
        f.addRow("Hang up", self.hangup)
        f.addRow("Wait for connect (s)", self.wait)
        f.addRow("Pause between calls (s)", self.pause)
        f.addRow("", self.dtr)
        v.addWidget(g)
        g = QGroupBox("Line conditioning"); f = QFormLayout(g)
        self.outside = QCheckBox("Access outside line"); self.outside.setChecked(bool(c["outside"]))
        self.outside_val = QLineEdit(str(c["outside_val"]))
        self.callwait = QCheckBox("Disable call waiting"); self.callwait.setChecked(bool(c["callwait"]))
        self.callwait_val = QComboBox(); self.callwait_val.setEditable(True)
        self.callwait_val.addItems(["*70", "1170", "70#"])
        self.callwait_val.setCurrentText(str(c["callwait_val"]))
        f.addRow(self.outside); f.addRow("Outside line prefix", self.outside_val)
        f.addRow(self.callwait); f.addRow("Call waiting prefix", self.callwait_val)
        v.addWidget(g)
        g = QGroupBox("Modem response strings"); f = QFormLayout(g)
        self.connect = QLineEdit(c["connect"]); self.busy = QLineEdit(c["busy"])
        self.fail = [QLineEdit(x) for x in c["fail"][:4]]
        f.addRow("Connect", self.connect)
        f.addRow("Busy", self.busy)
        for lbl, w in zip(("Failure 1", "Failure 2", "Failure 3", "Failure 4"), self.fail):
            f.addRow(lbl, w)
        v.addWidget(g)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def accept(self):
        s = QSettings("qrss", "qrss")
        for k, w in (("init", self.init), ("prefix", self.prefix), ("hangup", self.hangup),
                     ("connect", self.connect), ("busy", self.busy),
                     ("outside_val", self.outside_val)):
            s.setValue("modem/" + k, w.text())
        s.setValue("modem/callwait_val", self.callwait_val.currentText())
        s.setValue("modem/wait", self.wait.value())
        s.setValue("modem/pause", self.pause.value())
        s.setValue("modem/drop_dtr", self.dtr.isChecked())
        s.setValue("modem/outside", self.outside.isChecked())
        s.setValue("modem/callwait", self.callwait.isChecked())
        s.setValue("modem/fail", "|".join(w.text() for w in self.fail))
        super().accept()


class PhoneBookDialog(QDialog):
    """WinRSS' Phone Book: a list of name/number pairs, add/edit/delete, kept in the settings."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Phone book")
        v = QVBoxLayout(self)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Number"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for name, num in phone_book():
            r = self.table.rowCount(); self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(name))
            self.table.setItem(r, 1, QTableWidgetItem(num))
        v.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("Add"); add.clicked.connect(lambda: self.add_row())
        rm = QPushButton("Delete"); rm.clicked.connect(self.del_row)
        row.addWidget(add); row.addWidget(rm); row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def add_row(self):
        r = self.table.rowCount(); self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        self.table.setItem(r, 1, QTableWidgetItem(""))

    def del_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def accept(self):
        items = []
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            num = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            if num:
                items.append("%s|%s" % (name or num, num))
        QSettings("qrss", "qrss").setValue("modem/phonebook", items)
        super().accept()


class DateAndTimeDialog(QDialog):
    """WinRSS' Tools > Set Device Date And Time (RSS manual 5-30, 3-8).

    The station's clock stamps every alarm in the Status Report, so a wrong one makes the log
    hard to read.  The dialog shows the computer's clock and the station's (read with GET TIME
    when it opens), and sends SET DATE then SET TIME for the values in the editors - the order
    and the wire formats WinRSS uses (SetDateAndTimeMaker: yyyy/MM/dd, HH:mm:ss)."""

    def __init__(self, parent, station, device=None, error=""):
        super().__init__(parent)
        self.station = station
        self.setWindowTitle("Set device date and time")
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.pc = QLabel(); self.pc.setFont(mono())
        self.dev = QLabel(); self.dev.setFont(mono())
        f.addRow("Computer date and time", self.pc)
        f.addRow("Station date and time", self.dev)
        v.addLayout(f)
        when = device or datetime.datetime.now()
        row = QHBoxLayout()
        self.date = QDateEdit(QDate(when.year, when.month, when.day))
        self.date.setCalendarPopup(True); self.date.setDisplayFormat("yyyy-MM-dd")
        self.time = QTimeEdit(QTime(when.hour, when.minute, when.second))
        self.time.setDisplayFormat("HH:mm:ss")
        row.addWidget(QLabel("Date")); row.addWidget(self.date)
        row.addWidget(QLabel("Time")); row.addWidget(self.time)
        use_pc = QPushButton("Use computer time")
        use_pc.clicked.connect(self.use_pc_time)
        row.addWidget(use_pc)
        v.addLayout(row)
        self.status = QLabel(error or ""); self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Apply
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(lambda: self.apply(True))
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(lambda: self.apply(False))
        v.addWidget(bb)
        self._show_device(device, error)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._show_pc)
        self._tick.start(1000)
        self._show_pc()

    def _show_pc(self):
        self.pc.setText(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _show_device(self, when, error=""):
        if when is None:
            self.dev.setText("not read" + (" (%s)" % error if error else ""))
        else:
            self.dev.setText(when.strftime("%Y-%m-%d %H:%M:%S") + "   (from GET TIME)")

    def use_pc_time(self):
        now = datetime.datetime.now()
        self.date.setDate(QDate(now.year, now.month, now.day))
        self.time.setTime(QTime(now.hour, now.minute, now.second))

    def apply(self, close):
        """Send SET DATE / SET TIME for what the editors hold."""
        when = datetime.datetime.combine(self.date.date().toPython(), self.time.time().toPython())
        self.status.setText("Setting the station's clock…")

        def cb(res, err):
            if err:
                self.status.setText("The station did not take it: %s" % err)
                return
            self._show_device(res[0])
            self.status.setText("The station's clock is set.")
            if close:
                self.accept()

        self.station.run(lambda L: L.set_datetime(when), cb)


class NetworkAddressDialog(QDialog):
    """WinRSS' Tools > Set IP and MAC Address (RSS manual 3-6/3-7).

    Shows the station's IP and physical (MAC) address, read with `GET ADDR IP` /
    `GET ADDR PHYS` when the dialog opens, and writes them back with `SET ADDR IP` /
    `SET ADDR PHYS`.  The station only starts using a new address after a reset, which
    WinRSS sends when the dialog closes and this dialog offers to send too.  Only a
    networked station (IntelliRepeater, comparator) uses either value; a conventional
    repeater keeps them and ignores them."""

    def __init__(self, parent, station, addresses=None, error=""):
        super().__init__(parent)
        self.station = station
        self.setWindowTitle("Set IP and MAC address")
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.read_ip = QLabel(); self.read_ip.setFont(mono())
        self.read_phys = QLabel(); self.read_phys.setFont(mono())
        f.addRow("Station IP address", self.read_ip)
        f.addRow("Station physical address", self.read_phys)
        v.addLayout(f)
        self.ip = QLineEdit()
        self.ip.setPlaceholderText("10.101.3.10")
        self.phys = QLineEdit()
        self.phys.setPlaceholderText("08003ecd1c64  (12 hex digits)")
        g = QFormLayout()
        g.addRow("New IP address", self.ip)
        g.addRow("New physical address", self.phys)
        v.addLayout(g)
        self.reset_after = QCheckBox("Restart the station afterwards (the address takes effect then)")
        self.reset_after.setChecked(True)
        v.addWidget(self.reset_after)
        self.status = QLabel(error or ""); self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._show(addresses, error)

    def _show(self, addresses, error=""):
        if not addresses:
            self.read_ip.setText("not read" + (" (%s)" % error if error else ""))
            self.read_phys.setText("")
            return
        self.read_ip.setText("%s   (from GET ADDR IP)" % addresses[0])
        self.read_phys.setText("%s   (from GET ADDR PHYS)" % addresses[1])

    @staticmethod
    def check_ip(text):
        """`a.b.c.d`, each 0-255, and not 0.0.0.0 / 255.255.255.255."""
        parts = text.strip().split('.')
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise ValueError("%r is not an IPv4 address" % text)
        if text.strip() in ("0.0.0.0", "255.255.255.255"):
            raise ValueError("%s is not a usable station address" % text.strip())
        return ".".join(str(int(p)) for p in parts)

    @staticmethod
    def check_phys(text):
        """Twelve hex digits, as `GET ADDR PHYS` prints them (08003ecd1c64).  Colons, dashes
        and dots are accepted and stripped."""
        h = "".join(c for c in text if c not in ":-. ").lower()
        if len(h) != 12 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("%r is not a 12-digit hex physical address" % text)
        if h == "0" * 12 or h == "f" * 12:
            raise ValueError("%s is not a usable physical address" % h)
        return h

    def apply(self):
        try:
            ip = self.check_ip(self.ip.text()) if self.ip.text().strip() else None
            phys = self.check_phys(self.phys.text()) if self.phys.text().strip() else None
        except ValueError as e:
            self.status.setText(str(e))
            return
        if not ip and not phys:
            self.status.setText("Nothing to set.")
            return
        self.status.setText("Setting the station's address…")

        def cb(res, err):
            if err:
                self.status.setText("The station did not take it: %s" % err)
                return
            self._show(res)
            if not self.reset_after.isChecked():
                self.status.setText("Address written.  The station uses it after its next restart.")
                self.accept()
                return
            self.status.setText("Address written; restarting the station…")

            def cb2(_res, err2):
                if err2:
                    self.status.setText("Address written, but the restart failed: %s" % err2)
                    return
                self.status.setText("The station restarted with the new address.")
                self.accept()

            self.station.run(lambda L: L.reset(), cb2)

        self.station.run(lambda L: L.set_address(ip, phys), cb)

class PasswordDialog(QDialog):
    """WinRSS' "Set Device Password" (Tools menu): the current password, the new one twice.
    Leaving the new one blank disables password protection - SETPASS with only the current
    password clears the record's enable bit (the OC handler at 0x0D8A72)."""

    RESERVED = ("LOST", "SERVICE", "QUIT")

    def __init__(self, parent, state=None):
        super().__init__(parent)
        self.setWindowTitle("Set device password")
        f = QFormLayout(self)
        self.cur = QLineEdit(); self.cur.setEchoMode(QLineEdit.EchoMode.Password)
        self.new = QLineEdit(); self.new.setEchoMode(QLineEdit.EchoMode.Password)
        self.conf = QLineEdit(); self.conf.setEchoMode(QLineEdit.EchoMode.Password)
        for w in (self.cur, self.new, self.conf):
            w.textChanged.connect(self.check)
        if state is True:
            self.cur.setFocus()
        elif state is False:
            self.cur.setEnabled(False)        # no password set: there is nothing to type here
            self.new.setFocus()
        f.addRow("Current password", self.cur)
        f.addRow("New password", self.new)
        f.addRow("Confirm new password", self.conf)
        self.msg = QLabel(); self.msg.setWordWrap(True)
        f.addRow(self.msg)
        f.addRow(QLabel("5 to 8 characters; LOST, SERVICE and QUIT are reserved.  Leave the new\n"
                        "password blank to turn password protection off.  Password protection needs\n"
                        "a comparator, or a Quantar from release 5.0."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.check()

    def check(self):
        new, conf = self.new.text().strip(), self.conf.text().strip()
        err = ""
        if new and not 5 <= len(new) <= 8:
            err = "The new password must be 5 to 8 characters."
        elif new.upper() in self.RESERVED:
            err = "%s is a reserved word." % new.upper()
        elif new != conf:
            err = "The two new passwords do not match."
        self.msg.setText(err)
        self.ok.setEnabled(not err)

    def values(self):
        """(current, new, confirm) - confirm is empty when the new password is (disable)."""
        new = self.new.text().strip()
        return (self.cur.text().strip(), new, self.conf.text().strip() if new else "")


# ====================================================================== test and measurement
# WinRSS' "Test And Measurement" screen (RSS manual 3-85..3-92): the ASTRO BER & RSSI report
# and the ASTRO test-pattern transmitter.  Both need the station access disabled - WinRSS'
# rule, "The Station must be set to Access Disable in order to start any pattern transmission".
# Commands (rss.jar 21252/21253, 21450/21454, 21600, 21850-21855; firmware reply strings
# "\nBER V52 = %d %1.1f %1.1f %1.3f", "\nRSSI = %d %1.1f %1.1f", "\nASTRO_TONE = ",
# "\nV52 = "):
#   GET DSP BER <V52|APCO> <#reports> <samples/report> <sample time> <SHORT>
#       -> BER <pattern> = <status> <abs RSSI dBm> <rel RSSI dB> <BER fraction>
#   GET DSP RSSI <#reports> <samples/report> <SHORT>  -> RSSI = <status> <abs> <rel>
# A report with no signal on the receiver answers ?(0096) BER Rx signal not detected.
BER_PATTERNS = [("V.52", "V52"), ("Project 25", "APCO")]
BER_PERIODS = [("1 second", "1"), ("10 seconds", "10"), ("100 seconds", "100")]
# The timeout WinRSS sets for one report (css.serialASCIITimeout): 5 samples per report, and
# the report spans the sampling period.
BER_TIMEOUT = {"1": 4.0, "10": 16.5, "100": 142.5}
ASTRO_PATTERNS = [("STD DEV", "STD_DEV"), ("LOW DEV", "LOW_DEV"), ("V.52", "V52"), ("C4FM MOD", "C4FM")]
ASTRO_TONE_TEXT = {"STD_DEV": "Std Dev Is On.", "LOW_DEV": "Low Dev Is On.",
                   "V52": "V.52 Test Pattern Is On.", "C4FM": "C4FM Test Pattern Is On."}


class TestMeasurementPage(ServicePage):
    title = "Test and Measurement"
    help_tags = ("do_ber", "do_test_pat")

    def __init__(self, main):
        super().__init__(main)
        self.kind = None               # "ber" | "rssi" while a report is running
        self.count = 0
        self.disabled = False           # station access state (the gate for both tabs)
        self.pattern_on = False
        self.v52_station = False        # no SET ASTRO_TONE: pre-9.4 station, SET V52 only
        self.logf = None
        self.t0 = 0.0
        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.timeout.connect(self._send)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._ber_tab(), "ASTRO BER & RSSI Report")
        self.tabs.addTab(self._pat_tab(), "ASTRO Test Pattern")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.v.addWidget(self.tabs, 1)
        self.svc.connection.connect(lambda ok, d: self.render_note())
        self.render_note()

    # -- the two tabs -----------------------------------------------------------
    def _ber_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        f = QFormLayout()
        self.rx1 = ro(); self.rx2 = ro()
        f.addRow("Receiver Frequency 1 (MHz)", self.rx1)
        f.addRow("Receiver Frequency 2 (MHz)", self.rx2)
        v.addLayout(f)
        g = QGroupBox("Settings"); gf = QFormLayout(g)
        self.pattern = QComboBox()
        for label, val in BER_PATTERNS:
            self.pattern.addItem(label, val)
        self.period = QComboBox()
        for label, val in BER_PERIODS:
            self.period.addItem(label, val)
        gf.addRow("Pattern Type", self.pattern)
        gf.addRow("Sampling Period", self.period)
        v.addWidget(g)
        g = QGroupBox("Results"); gf = QFormLayout(g)
        self.ber_val = ro(); self.rssi_val = ro(); self.rel_val = ro()
        gf.addRow("Bit Error Rate (%)", self.ber_val)
        gf.addRow("Received Signal Strength (dBm)", self.rssi_val)
        gf.addRow("Signal above noise floor (dB)", self.rel_val)
        v.addWidget(g)
        h = QHBoxLayout()
        self.ber_btn = button("Start BER Measurement", lambda: self.toggle("ber"),
                              "Averages 5 samples per report; the station must be access disabled")
        self.rssi_btn = button("Start RSSI Measurement", lambda: self.toggle("rssi"),
                               "Averages 1 sample per report; the station must be access disabled")
        self.log_btn = button("Start Log", self.toggle_log, "Starts/stops logging the report to a file")
        for b in (self.ber_btn, self.rssi_btn, self.log_btn):
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        self.ber_note = QLabel("")
        self.ber_note.setWordWrap(True)
        v.addWidget(self.ber_note)
        v.addStretch(1)
        return w

    def _pat_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        f = QFormLayout()
        self.txf = ro()
        f.addRow("Transmit Frequency (MHz)", self.txf)
        self.txpat = QComboBox()
        for label, val in ASTRO_PATTERNS:
            self.txpat.addItem(label, val)
        f.addRow("Select Pattern to Transmit", self.txpat)
        v.addLayout(f)
        self.pat_status = QLabel("No Test Patterns Active.")
        self.pat_status.setStyleSheet("font-weight: bold;")
        v.addWidget(self.pat_status)
        self.keyed = QLabel("")
        v.addWidget(self.keyed)
        h = QHBoxLayout()
        self.pat_start = button("Start Pattern Transmission", self.pattern_start)
        self.pat_stop = button("Stop Pattern Transmission", self.pattern_stop)
        h.addWidget(self.pat_start); h.addWidget(self.pat_stop); h.addStretch(1)
        v.addLayout(h)
        v.addWidget(para("The station must be set to <b>Access Disable</b> to start a pattern "
                          "transmission.  A test pattern keys the transmitter, and its power output is "
                          "reduced while one is on (half power below 150 W rated, 100 W above).  "
                          "V.52 is the pattern the BER report measures; STD DEV, LOW DEV and C4FM MOD "
                          "are for an external deviation meter or service monitor."))
        v.addStretch(1)
        return w

    # -- access state (the gate) -------------------------------------------------
    def show_access(self, state, value=None):
        self.disabled = (state == "Disabled")
        self.render_note()

    def render_note(self):
        if not self.svc.connected:
            self.note.setText("")
        elif self.disabled:
            self.note.setText("STATION ACCESS DISABLED")
        else:
            self.note.setText("Station access enabled - Access Disable is needed to start a measurement "
                              "or a test pattern")
        self._gate()

    def _gate(self):
        ok = self.svc.connected
        run = self.kind is not None
        self.ber_btn.setEnabled(ok and (self.disabled or self.kind == "ber"))
        self.rssi_btn.setEnabled(ok and (self.disabled or self.kind == "rssi"))
        self.ber_btn.setText("Stop BER Measurement" if self.kind == "ber" else "Start BER Measurement")
        self.rssi_btn.setText("Stop RSSI Measurement" if self.kind == "rssi" else "Start RSSI Measurement")
        self.pattern.setEnabled(not run)
        self.period.setEnabled(not run)
        self.log_btn.setEnabled(ok)
        self.pat_start.setEnabled(ok and self.disabled and not self.pattern_on)
        self.pat_stop.setEnabled(ok and self.pattern_on)

    # -- lifecycle ---------------------------------------------------------------
    def activate(self):
        super().activate()
        self.render_note()
        if not self.svc.connected:
            return

        def fn(L):
            out = {}
            for k, t in (("rx1", "GET RX FREQ"), ("rx2", "GET RX2 FREQ"), ("tx", "GET TX FREQ")):
                try:
                    out[k] = L.call(t)[0]
                except StationError as e:
                    out[k] = show_err(e)
            try:
                out["tone"] = L.call("GET ASTRO_TONE")[0]
            except StationError as e:
                out["tone"], out["tone_err"] = None, e.code
            try:
                out["v52"] = L.call("GET V52")[0]
            except StationError:
                out["v52"] = None
            try:
                out["access"] = L.station_access()
            except StationError:
                out["access"] = None
            return out
        self.job(fn, self._static, "Test and measurement")

    def _static(self, out):
        self.rx1.setText(mhz(out["rx1"]))
        self.rx2.setText(mhz(out["rx2"]))
        self.txf.setText(mhz(out["tx"]))
        if out.get("access"):
            self.show_access(*out["access"])
        if out.get("tone"):
            self._tone_state(out["tone"])
        elif out.get("tone_err") in UNSUPPORTED:
            # a pre-9.4 station has no SET ASTRO_TONE; only the V.52 pattern, via SET V52
            self.v52_station = True
            self._tone_state("V52" if out.get("v52") == "ON" else "OFF")

    def deactivate(self):
        super().deactivate()
        self.stop(quiet=True)
        if self.logf:
            self.logf.close()
            self.logf = None
            self.log_btn.setText("Start Log")
        if self.svc.connected and self.pattern_on:
            # never leave the station transmitting a test pattern
            tpl = "SET V52 OFF" if self.v52_station else "SET ASTRO_TONE OFF"
            self.job(lambda L: L.call(tpl), what="Stop test pattern")
            self._tone_state("OFF")

    def _tab_changed(self, i):
        self.stop()                     # a 100 s BER command would block the other tab

    # -- the BER / RSSI report --------------------------------------------------
    def toggle(self, kind):
        if self.kind == kind:
            self.stop()
        else:
            self.start(kind)

    def start(self, kind):
        if not self.svc.connected:
            return dlg.warning(self, self.title, "Connect to the station first (Station > Connect…).")
        if not self.disabled:
            return dlg.warning(self, self.title,
                               "The station must be set to Access Disable before a measurement is started.")
        self.stop(quiet=True)
        self.kind, self.count = kind, 0
        self.ber_val.setText("N/A")
        self.rssi_val.setText("N/A")
        self.rel_val.setText("N/A")
        self.ber_note.setText("")
        self._gate()
        self._send()

    def stop(self, quiet=False):
        was, self.kind = self.kind, None
        self.poll.stop()
        self._gate()
        if was and not quiet:
            self.status("%s measurement stopped" % was.upper())

    def _send(self):
        kind = self.kind
        if not kind or not self.svc.connected:
            return
        self.t0 = time.time()
        if kind == "ber":
            pat, per = self.pattern.currentData(), self.period.currentData()
            # WinRSS' first report always asks for 0.2 s samples; later ones period/5, so one
            # report (5 samples) spans the selected period.
            st = "0.2" if not self.count else "%.1f" % (float(per) / 5.0)
            to = BER_TIMEOUT[per]
            fn = lambda L: L.call("GET DSP BER [%s] [%s] [%s] [%s] [%s]", pat, 1, 5, st, "SHORT",
                                   timeout=to)
        else:
            fn = lambda L: L.call("GET DSP RSSI [%s] [%s] [%s]", 1, 1, "SHORT", timeout=4.0)

        def cb(res, err):
            if err:
                self.stop()
                self.ber_note.setText("Measurement stopped: %s" % err)
                if self.active:
                    self.fail(err, "BER/RSSI measurement")
                return
            if kind != self.kind:
                return                  # a reply that arrived after Stop
            self.count += 1
            self._show(kind, res)
            self._schedule()
        self.svc.run(fn, cb)

    def _show(self, kind, f):
        st = None
        if kind == "ber" and len(f) >= 5:
            st = f[1]
            self.rssi_val.setText("%s dBm" % f[2])
            self.rel_val.setText("%s dB" % f[3])
            self.ber_val.setText("%s %%" % f[4])
            self._log("BER", "pattern %s  RSSI %s dBm  rel %s dB  BER %s %%" % (f[0], f[2], f[3], f[4]))
        elif kind == "rssi" and len(f) >= 3:
            st = f[0]
            self.rssi_val.setText("%s dBm" % f[1])
            self.rel_val.setText("%s dB" % f[2])
            self._log("RSSI", "RSSI %s dBm  rel %s dB" % (f[1], f[2]))
        # The first field is the detector's status.  WinRSS ignores it; the station answers 1
        # with no signal (and the BER command ?(0096) instead), 0 once it is measuring.
        self.rssi_val.setToolTip("" if st in (None, "0") else
                                  "The station reported status %s (0 = measured; 1 seen with no signal)" % st)

    def _schedule(self):
        """Next report is due one sampling period after the last one started (the command
        itself takes about that long on the station; the simulator answers at once)."""
        if not self.kind:
            return
        per = float(self.period.currentData()) if self.kind == "ber" else 1.0
        self.poll.start(max(50, int((per - (time.time() - self.t0)) * 1000)))

    def toggle_log(self):
        if self.logf:
            self.logf.close()
            self.logf = None
            self.log_btn.setText("Start Log")
            return
        path, _ = QFileDialog.getSaveFileName(self, "BER and RSSI log", "astro_ber_log.txt", "Text (*.txt)")
        if not path:
            return
        try:
            self.logf = open(path, "a")
        except OSError as e:
            return dlg.warning(self, self.title, "Cannot write %s:\n%s" % (path, e))
        self.logf.write("# ASTRO BER and RSSI report, %s\n" % time.strftime("%Y-%m-%d %H:%M"))
        self.log_btn.setText("Stop Log")

    def _log(self, tag, text):
        if self.logf:
            self.logf.write("%s %-4s %s\n" % (time.strftime("%Y-%m-%d/%H:%M:%S"), tag, text))
            self.logf.flush()

    # -- the ASTRO test pattern --------------------------------------------------
    def _tone_state(self, tone):
        if tone == "OFF":
            self.pat_status.setText("No Test Patterns Active.")
            self.pattern_on = False
        elif tone == "V52" and self.v52_station:
            self.pat_status.setText("V.52 Is On.")
            self.pattern_on = True
        else:
            self.pat_status.setText(ASTRO_TONE_TEXT.get(tone, "No Test Patterns Active."))
            self.pattern_on = tone in ASTRO_TONE_TEXT
        self._gate()

    def _keyed(self):
        def then(r):
            self.keyed.setText("Station: %s" % ("Keyed" if r and r[0].upper() == "ACTIVE" else "Not keyed"))
        self.call("GET PA ON", then=then, what="Station keyed")

    def pattern_start(self):
        if not self.svc.connected:
            return dlg.warning(self, self.title, "Connect to the station first (Station > Connect…).")
        if not self.disabled:
            return dlg.warning(self, self.title,
                               "The station must be set to Access Disable to transmit a test pattern.")
        pat = self.txpat.currentData()

        def ok(_):
            self._tone_state(pat)
            self._keyed()

        def fallback(err):
            # A pre-9.4 station rejects SET ASTRO_TONE (?0017): only the V.52 pattern exists,
            # through SET V52 ON - WinRSS falls back the same way.
            self.v52_station = True
            if pat != "V52":
                return self.fail(err, "ASTRO test pattern")
            self.call("SET V52 ON", then=lambda r: (self._tone_state("V52"), self._keyed()),
                      what="V.52 test pattern")

        def cb(res, err):
            if not err:
                return ok(res)
            if getattr(err, "code", None) in UNSUPPORTED:
                return fallback(err)
            self.fail(err, "ASTRO test pattern")
        self.svc.run(lambda L: L.call("SET ASTRO_TONE [%s]", pat), cb)

    def pattern_stop(self):
        tpl = "SET V52 OFF" if self.v52_station else "SET ASTRO_TONE OFF"
        self.call(tpl, then=lambda r: (self._tone_state("OFF"), self._keyed()), what="Test pattern")


# ====================================================================== firmware download
class FirmwarePage(ServicePage):
    title = "Firmware Download (SWDL)"
    station_controls = False
    comparator = True
    help_tags = ("qrss_swdl",)
    logsig = Signal(str)
    progsig = Signal(float, str)

    def __init__(self, main):
        super().__init__(main)
        self.v.addWidget(para(
            "Programs station firmware from a Motorola SWDL package directory (SWDL.INI, ftp command "
            "files, BPID, .cmp images).  Over the serial port qrss runs its own SLIP/IP/FTP stack, so no "
            "network setup or admin rights are needed; over Ethernet it uses FTP to the station's own "
            "address, as WinRSS does.  <b>Do not remove power while the station is "
            "programming flash.</b>"))
        f = QFormLayout()
        h = QHBoxLayout()
        self.dir = QLineEdit(QSettings("qrss", "qrss").value("swdl/dir", ""))
        h.addWidget(self.dir, 1)
        h.addWidget(button("Browse…", self.browse))
        h.addWidget(button("Load", self.load))
        f.addRow("Package directory", h)
        self.conntype = QComboBox()
        self.conntype.addItems(["Serial port (SLIP)", "Ethernet (FTP to the station)"])
        self.conntype.setCurrentIndex(int(QSettings("qrss", "qrss").value("swdl/conn", 0)))
        self.conntype.setToolTip("WinRSS offers both.  Ethernet needs the station's IP address (the "
                             "station must serve FTP in normal mode - it does, at 193.0.0.x or its "
                             "configured address) and no serial connection.")
        self.conntype.currentIndexChanged.connect(self._conn_type_changed)
        f.addRow("Connection", self.conntype)
        self.eth = QLineEdit(QSettings("qrss", "qrss").value("swdl/ethernet", ""))
        self.eth.setPlaceholderText("station IP address, e.g. 192.168.1.50")
        self.eth.setToolTip("The station's own address.  The whole download is FTP to it, before and "
                            "after the reset into the boot loader.")
        f.addRow("Station IP", self.eth)
        self.baud = QComboBox()
        for b, tag in ((9600, ""), (19200, ""), (38400, ""), (57600, ""), (115200, "  (HAM)")):
            self.baud.addItem("%d%s" % (b, tag), b)     # "(HAM)" needs patched firmware (H026.11.05b+)
        self.baud.setCurrentIndex(2)                    # 38400, like WinRSS' faster default
        self.baud.setToolTip("Boot-loader SLIP speed.  Stock firmware does up to 57600; 115200 (really "
                             "111607 from the 25 MHz clock) needs HAM firmware, H026.11.05b+.  WinRSS "
                             "offers 9600 and 38400.")
        f.addRow("SLIP baud rate", self.baud)
        self.sport = QComboBox(); self.sport.addItems(["Front port (193.0.0.2)", "Back port (193.0.0.1)"])
        f.addRow("Station port", self.sport)
        self.v.addLayout(f)
        self.info = QPlainTextEdit(); self.info.setReadOnly(True); self.info.setFont(mono())
        self.info.setMaximumHeight(170)
        self.v.addWidget(self.info)
        h2 = QHBoxLayout()
        self.btns = [button("Dry run", lambda: self.start(True), "Checks versions and BPID, sends nothing that changes the station"),
                     button("Program", lambda: self.start(False)),
                     button("Normal Mode", self.normal_mode, "Tell a station stuck in its boot loader to start its application"),
                     button("Save Status Log…", self.save_status, "Read the boot loader's error log")]
        for b in self.btns:
            h2.addWidget(b)
        self.abort_btn = button("Abort", self.request_abort)
        self.abort_btn.setEnabled(False)
        h2.addWidget(self.abort_btn)
        h2.addStretch(1)
        self.v.addLayout(h2)
        self.bar = QProgressBar(); self.bar.setRange(0, 1000)
        self.stat = QLabel("")
        self.v.addWidget(self.bar); self.v.addWidget(self.stat)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFont(mono())
        self.log.setMaximumBlockCount(5000)
        self.v.addWidget(self.log, 1)
        self.logsig.connect(lambda t: self.log.appendPlainText(time.strftime("%H:%M:%S  ") + t))
        self.progsig.connect(self._prog)
        self.sections = None
        self.aborting = False
        self._conn_type_changed()
        if self.dir.text():
            self.load(quiet=True)

    def _ethernet(self):
        return self.conntype.currentIndex() == 1

    def _conn_type_changed(self, *_):
        eth = self._ethernet()
        self.eth.setEnabled(eth)
        self.baud.setEnabled(not eth)            # serial-only settings
        self.sport.setEnabled(not eth)
        QSettings("qrss", "qrss").setValue("swdl/conn", self.conntype.currentIndex())
        if self.eth.text():
            QSettings("qrss", "qrss").setValue("swdl/ethernet", self.eth.text())

    def _prog(self, frac, text):
        if frac >= 0:
            self.bar.setValue(int(frac * 1000))
        self.stat.setText(text)

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "SWDL package directory", self.dir.text())
        if d:
            self.dir.setText(d)
            self.load()

    def load(self, quiet=False):
        from .swdl import parse_package, validate_package
        try:
            self.sections = parse_package(self.dir.text())
        except Exception as e:
            self.sections = None
            self.info.setPlainText("Cannot use this directory: %s" % e)
            return
        QSettings("qrss", "qrss").setValue("swdl/dir", self.dir.text())
        txt = "\n".join(s.describe() for s in self.sections)
        probs = validate_package(self.sections)
        if probs:
            txt += "\n\nPROBLEMS:\n  " + "\n  ".join(probs)
        self.info.setPlainText(txt)

    def _busy(self, on):
        for b in self.btns:
            b.setEnabled(not on)
        self.abort_btn.setEnabled(on)

    def request_abort(self):
        self.aborting = True
        self.logsig.emit("abort requested (takes effect between steps; flash programming itself cannot be stopped)")

    def _downloader(self, L, **kw):
        from .swdl import Downloader
        from .netstack import FRONT, BACK
        eth = self._ethernet()
        return Downloader(None if eth else L.s, self.sections or [], baud=self.baud.currentData(),
                          ascii_baud=self.svc.baud if hasattr(getattr(L, "s", None), 'baudrate') else None,
                          station_port=FRONT if self.sport.currentIndex() == 0 else BACK,
                          ethernet=(self.eth.text().strip() or None) if eth else None,
                          log=self.logsig.emit,
                          progress=lambda f, t: self.progsig.emit(-1.0 if f is None else f, t),
                          abort=lambda: self.aborting, **kw)

    def _finish(self, res, err):
        self._busy(False)
        if self.svc.link:
            self.svc.link.prompt = None
        if err:
            self.logsig.emit("FAILED: %s" % err)
            dlg.warning(self, "Firmware download", str(err))
        elif res is not None:
            self.logsig.emit(str(res))

    def start(self, dry):
        eth = self._ethernet()
        if eth:
            if not self.eth.text().strip():
                return dlg.warning(self, "Firmware download", "Enter the station's IP address.")
            QSettings("qrss", "qrss").setValue("swdl/ethernet", self.eth.text().strip())
        elif not self.svc.connected:
            return dlg.warning(self, "Firmware download", "Connect to the station first (Station > Connect…).")
        if not self.sections:
            return dlg.warning(self, "Firmware download", "Load a package directory first.")
        from .swdl import validate_package
        probs = validate_package(self.sections)
        if probs and not dry:
            return dlg.warning(self, "Firmware download", "The package has problems:\n\n" + "\n".join(probs))
        if not dry and dlg.question(self, "Program station",
                                    "Program the station with:\n\n%s\n\nThe station stops operating while it is "
                                    "programmed.  Do not remove power until it has restarted." %
                                    "\n".join("[%s] %s" % (s.name, ", ".join(os.path.basename(p) for p, _ in s.firmware))
                                              for s in self.sections)) != dlg.SB.Yes:
            return
        self.aborting = False
        self._busy(True)
        self.bar.setValue(0)
        self.logsig.emit("=== %s %s%s" % ("DRY RUN" if dry else "PROGRAM", self.dir.text(),
                                           " over Ethernet to %s" % self.eth.text().strip() if eth else ""))
        if eth:
            # no serial link needed: run on the worker with no station connected
            self.svc.run_raw(lambda L: (self._downloader(L, dry_run=dry).run_ethernet(), "finished")[1],
                             self._finish)
        else:
            self.svc.run(lambda L: (self._downloader(L, dry_run=dry).run(), "finished")[1], self._finish)

    def normal_mode(self):
        if not self.svc.connected:
            return
        self._busy(True)
        self.svc.run(lambda L: (self._downloader(L).normal_mode(), "Normal mode sent")[1], self._finish)

    def save_status(self):
        if not self.svc.connected:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save boot loader status log", "swdl_status.txt", "Text (*.txt)")
        if not path:
            return
        def fn(L):
            txt = self._downloader(L).save_status()
            open(path, 'w').write(txt)
            return "status log saved to %s" % path
        self._busy(True)
        self.svc.run(fn, self._finish)


SERVICE_PAGES = [("__svc_version__", "Software Version", VersionPage),
                 ("__svc_cmp__", "Comparator Local Status", ComparatorStatusPage),
                 ("__svc_fan__", "Diagnostics (fan)", FanDiagPage),
                 ("__svc_status__", "Status Panel", StatusPanelPage),
                 ("__svc_report__", "Status Report", StatusReportPage),
                 ("__svc_meter__", "Metering", MeteringPage),
                 ("__svc_test__", "Test and Measurement", TestMeasurementPage),
                 ("__svc_align__", "Alignment", AlignmentPage),
                 ("__svc_cmp_align__", "Alignment", ComparatorAlignmentPage),
                 ("__svc_swdl__", "Firmware Download", FirmwarePage),
                 ("__svc_term__", "Station Terminal", TerminalPage)]
