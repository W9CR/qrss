"""qrss Qt GUI: a WinRSS-style codeplug editor for conventional Quantar stations.

Screens are generated from the WinRSS metadata recovered from rss.jar
(qrss/data/forms.json: fields, labels, units, choice lists; qrss/data/layouts.json:
the GridBag placement of every label/field/group/tab; qrss/data/menus.json: menu tree).
Values are read/written through qrss.fields codecs; fields without a codec are shown
disabled until they are mapped.

Run:  python3 -m qrss [file.cp]
"""
import json, os, struct, sys, time

try:
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtGui import QAction, QFont, QFontDatabase, QIcon, QKeySequence
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
        QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSplitter,
        QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
        QWidget, QPlainTextEdit)
except ImportError:
    sys.exit("PySide6 is required:  pip3 install PySide6")

from . import dlg
from . import __version__
from .codeplug import (CpFile, Codeplug, cksum, add_channel, delete_last_channel, PlatformUnsupported,
                       add_access_table, delete_access_table, reset_multi_nac, CHASSIS, PORTS_PER_CHASSIS)
from .station import StationError, Link
from .fields import codec, NeedsConfirm
from .help import HelpHook, HelpPane
from .report import codeplug_report, SYSTEM_FOR_VERSION

DATA =os.path.join(os.path.dirname(__file__), 'data')
FORMS = json.load(open(os.path.join(DATA, 'forms.json')))
LAYOUTS = json.load(open(os.path.join(DATA, 'layouts.json')))
MENUS = json.load(open(os.path.join(DATA, 'menus.json')))
SYSTEM = 'CONVENTIONAL'
# The station type follows the codeplug version: v0x28 is the Quantar/Quantro, v0x61/v0x62 the
# comparator (a 68040 SCM with the 68360 as its serial/network peripheral).  Each type has its
# own WinRSS menu tree in menus.json.  (SYSTEM_FOR_VERSION lives in report.py, which needs it too.)
NOT_DECODED = "Not decoded yet - shown read-only"
ICON = os.path.join(DATA, 'w9cr_bat.png')

# A green-accented light theme (snappier than the default grey).  Accents only - inputs keep the
# native look so the app still feels at home on each OS.
THEME = """
QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected,
QTreeView::item:selected, QListView::item:selected {
    background: #2e7d32; color: white;
}
QHeaderView::section { background: #e3efe3; color: #1b5e20; padding: 3px; border: 0; border-right: 1px solid #c6d6c6; }
QTabWidget::pane { border: 1px solid #c2d4c2; top: -1px; }
QTabBar::tab { background: #dde8dd; color: #1a241a; border: 1px solid #c2d4c2; border-bottom: none;
               border-top-left-radius: 4px; border-top-right-radius: 4px; padding: 5px 16px; margin-right: 3px; }
QTabBar::tab:selected { background: #2e7d32; color: white; }
QTabBar::tab:hover:!selected { background: #e7f0e7; }
QPushButton { border: 1px solid #a7c3a7; border-radius: 4px; padding: 4px 10px; }
QPushButton:hover { border-color: #2e7d32; background: #eaf4ea; }
QPushButton:pressed { background: #2e7d32; color: white; }
QPushButton:disabled { color: #9fab9f; border-color: #d2dad2; }
QGroupBox { border: 1px solid #c2d4c2; border-radius: 5px; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 3px; color: #1b5e20; font-weight: bold; }
QMenuBar::item:selected, QMenu::item:selected { background: #2e7d32; color: white; }
QProgressBar { border: 1px solid #c2d4c2; border-radius: 3px; text-align: center; }
QProgressBar::chunk { background: #43a047; }
QStatusBar { background: #e3efe3; }
QSplitter::handle { background: #cfdccf; }
QLabel#pageTitle { font-size: 14px; color: #1b5e20; }
"""
PAGE_TITLE = "font-size: 14px; font-weight: bold; color: #1b5e20;"   # for the per-page heading labels


def compact_popup(combo, rows=20):
    """Scrolling popup of limited height (macOS' native popup ignores maxVisibleItems and
    can grow taller than the screen)."""
    combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
    combo.setMaxVisibleItems(rows)


class FieldWidget(QWidget):
    """Label-less editor for one metadata field (line edit or combo box) plus unit label."""

    def __init__(self, form, info, attr, owner):
        super().__init__()
        self._loading = True          # signals can fire while the widget is being built
        self.form, self.name, self.owner = form, info.get('fieldName'), owner
        self.codec = codec(form, self.name)
        self.help_tag = info.get('helpTag')
        self.label = (info.get('label') or self.name or '').strip().rstrip(':')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        clist = (self.codec.clist if self.codec and self.codec.clist else None) or info.get('clist')
        if clist:
            self.edit = QComboBox()
            self.edit.addItems(clist)
            compact_popup(self.edit)
            if self.codec and self.codec.editable:      # pick from the list or type (e.g. WAIT 120)
                self.edit.setEditable(True)
                self.edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                self.edit.setMinimumWidth(110)
                self.edit.lineEdit().editingFinished.connect(self._changed)
                self.edit.activated.connect(self._changed)
            else:
                self.edit.currentTextChanged.connect(self._changed)
        else:
            self.edit = QLineEdit()
            n = attr.get('length') or 12
            self.edit.setMaxLength(n)
            self.edit.setMinimumWidth(min(min(max(n, 4), 22) * 7, 150))   # compact; was n*9 (forced forms wide)
            self.edit.editingFinished.connect(self._changed)
        lay.addWidget(self.edit)
        if info.get('unit'):
            lay.addWidget(QLabel(info['unit']))
        lay.addStretch(1)
        tip = attr.get('toolTip') or ''
        if not self.codec:
            self.edit.setEnabled(False)
            tip = (tip + "\n" if tip else "") + NOT_DECODED
            if isinstance(self.edit, QComboBox):      # don't let the first list entry pose as a value
                self.edit.insertItem(0, "")
                self.edit.setCurrentIndex(0)
        elif self.codec.readonly:
            self.edit.setEnabled(False)
        self.edit.setToolTip(tip)
        show = lambda: owner.field_help(self.label, self.help_tag)
        for w in (self, self.edit, self.edit.lineEdit() if isinstance(self.edit, QComboBox) else None):
            if w is not None:                 # the container too: clicks on a disabled editor land there
                HelpHook(w, show)
        self._loading = False

    def load(self, cp, ctx):
        if not self.codec or cp is None:
            return
        self._loading = True
        try:
            try:
                v = self.codec.get(cp, ctx)
            except (IndexError, KeyError, struct.error):
                v = "n/a"           # e.g. a dir[] table this codeplug does not have (comparator)
            if isinstance(self.edit, QComboBox):
                if self.edit.findText(v) < 0:
                    self.edit.addItem(v)
                self.edit.setCurrentText(v)
            else:
                self.edit.setText(v)
        finally:
            self._loading = False

    def _changed(self, *_):
        if self._loading or not self.codec or self.owner.cp is None:
            return
        v = self.edit.currentText() if isinstance(self.edit, QComboBox) else self.edit.text()
        try:
            old = self.codec.get(self.owner.cp, self.owner.ctx)
            if v == old:
                return
            self._apply(v, old)
        except PlatformUnsupported as e:  # no same-band PA: offer to force it (identity only)
            if dlg.question(self, "Switch platform",
                            "There is no %s PA for %s.  WinRSS would move the station to a different band; "
                            "qrss will only set the %s marker and power supply and leave the PA power for "
                            "you to set.%s\n\nSwitch to %s anyway?"
                            % (e.platform, e.band, e.platform,
                               "\n\nThis plug has no receive band set (rx_freq_band), so the exciter band "
                               "cannot be rebuilt either; set that first." if str(e.band).startswith("UNKNOWN") else "",
                               e.platform)
                            ) == QMessageBox.StandardButton.Yes:
                self.owner.ctx['_force_platform'] = True
                try:
                    self._apply(v, self.codec.get(self.owner.cp, self.owner.ctx))
                finally:
                    self.owner.ctx.pop('_force_platform', None)
            else:
                self.load(self.owner.cp, self.owner.ctx)
        except NeedsConfirm as e:   # set() would clear other fields; WinRSS asks first
            if dlg.question(self, e.title, e.text) == QMessageBox.StandardButton.Yes:
                self.owner.ctx['_confirmed'] = True
                try:
                    self._apply(v, old)
                finally:
                    self.owner.ctx.pop('_confirmed', None)
            else:
                self.load(self.owner.cp, self.owner.ctx)
        except Exception as e:  # bad input: revert
            self.owner.status("Invalid value for %s: %s" % (self.name, e))
            self.load(self.owner.cp, self.owner.ctx)

    def _apply(self, v, old):
        self.codec.set(self.owner.cp, self.owner.ctx, v)
        if self.codec.navigates:          # e.g. the access-code table number: moves the page
            self.owner.refresh()
            self.owner.status(self.codec.describe(self.owner.cp, self.owner.ctx)
                              if hasattr(self.codec, 'describe') else "Showing %s" % v)
            return
        self.owner.modified("%s: %s -> %s" % (self.name, old, v))
        if self.codec.cascade:            # set() rewrote other fields too -> re-read the whole page
            self.owner.refresh()


class FormPage(QWidget):
    """One WinRSS configuration window rebuilt from metadata + layout."""

    hide_rest = ()          # metadata fields the form shows elsewhere (e.g. in a dialog)
    row_label = "channel"   # what a table row is called in the status line

    def __init__(self, form, main):
        super().__init__()
        self.form, self.main = form, main
        self.meta = FORMS.get(form, {'fields': [], 'attributes': [], 'regions': [], 'buttons': []})
        self.layout_info = LAYOUTS.get(form, {'containers': {}})
        self.fields = {f.get('fieldName'): f for f in self.meta['fields']}
        self.attrs = {a.get('fieldName'): a for a in self.meta['attributes']}
        self.regions = {r['regionName']: r for r in self.meta['regions']}
        self.widgets = {}
        self.ctx = {'channel': 0, 'row': 0, 'interactive': True}   # codecs may ask before a destructive cascade
        v = QVBoxLayout(self)
        title = QLabel("<b>%s</b>" % (self.meta.get('label') or form))
        title.setObjectName("pageTitle")
        v.addWidget(title)
        body = self._container('root', set())
        v.addWidget(body)
        rest = [n for n in self.fields if n and n in self.attrs and n not in self.widgets
                and n not in self.regions and n not in self.hide_rest]
        if rest:
            box = QGroupBox("Other fields")
            g = QGridLayout(box)
            for i, n in enumerate(rest):
                g.addWidget(QLabel(self.fields[n].get('label') or n), i, 0)
                g.addWidget(self._field(n), i, 1)
            v.addWidget(box)
        v.addStretch(1)
        self.button_bar = QWidget()              # shown below the scroll area, never scrolled away
        self.button_bar.setLayout(self._buttons())

    # -- layout -------------------------------------------------------------------
    def _container(self, name, seen):
        w = QWidget()
        g = QGridLayout(w)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(4)
        for it in self.layout_info['containers'].get(name, []):
            k, x, y = it['kind'], it['x'], it['y']
            cw, ch = max(it.get('w', 1), 1), max(it.get('h', 1), 1)
            if k == 'label':
                f = self.fields.get(it['name'], {})
                g.addWidget(QLabel(f.get('label') or ''), y, x, Qt.AlignmentFlag.AlignLeft)
            elif k == 'field':
                if it['name'] in self.fields:
                    g.addWidget(self._field(it['name']), y, x, 1, cw)
            elif k == 'group':
                box = QGroupBox(it.get('label', ''))
                bl = QVBoxLayout(box)
                bl.addWidget(self._container(it['name'], seen))
                g.addWidget(box, y, x, ch, cw)
            elif k == 'tabs':
                tw = QTabWidget()
                for t in it['tabs']:
                    page = QWidget()
                    pl = QVBoxLayout(page)
                    pl.addWidget(self._container(t['var'], seen))
                    pl.addStretch(1)
                    tw.addTab(page, t['label'])
                g.addWidget(tw, y, x, ch, cw if cw > 1 else 6)
            elif k == 'table':
                g.addWidget(self._table(it['name']), y, x, ch, max(cw, 6))
            elif k == 'text':
                g.addWidget(QLabel(it.get('text', '')), y, x)
        g.setColumnStretch(g.columnCount(), 1)
        return w

    def _field(self, name):
        fw = FieldWidget(self.form, self.fields[name], self.attrs.get(name, {}), self)
        self.widgets[name] = fw
        return fw

    def _table(self, region):
        r = self.regions.get(region)
        t = QTableWidget()
        t.region_cols, t.region_help = [], []
        if r:
            fl = [f for f in r['fields'] if f.get('label') or f.get('fieldName') != 'dummy']
            t.region_cols = [(f['fieldName'], codec(self.form, "%s.%s" % (region, f['fieldName']))) for f in fl]
            t.region_help = [((f.get('label') or f.get('fieldName') or '').strip().rstrip(':'), f.get('helpTag'))
                             for f in fl]
            t.currentCellChanged.connect(lambda _row, col, _pr, _pc, t=t: self._cell_help(t, col))
            t.setColumnCount(len(fl))
            t.setHorizontalHeaderLabels([f.get('label') or f.get('fieldName') for f in fl])
            t.setRowCount(r.get('rows', 0))
            t.verticalHeader().setVisible(False)
        if not any(c for _, c in t.region_cols):
            t.setEnabled(False)
            t.setToolTip(NOT_DECODED)
        t.setMinimumHeight(160)
        self.widgets[region] = t
        return t

    def _load_table(self, t):
        cp = self.cp
        rows = self.table_rows()
        t.setRowCount(rows)
        for row in range(rows):
            ctx = dict(self.ctx, row=row)
            for col, (name, cd) in enumerate(t.region_cols):
                if cd is None:
                    continue
                try:
                    v = cd.get(cp, ctx)
                except (IndexError, KeyError, struct.error):
                    v = "n/a"
                if cd.readonly or not cd.clist:
                    it = QTableWidgetItem(v)
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    t.setItem(row, col, it)
                    continue
                cb = t.cellWidget(row, col)
                if cb is None:
                    cb = QComboBox(); cb.addItems(cd.clist); compact_popup(cb)
                    HelpHook(cb, lambda t=t, col=col: self._cell_help(t, col))
                    cb.currentTextChanged.connect(
                        lambda val, cd=cd, row=row, name=name: self._cell_changed(cd, row, name, val))
                    t.setCellWidget(row, col, cb)
                cb.blockSignals(True); cb.setCurrentText(v); cb.blockSignals(False)
        t.resizeColumnsToContents()

    def table_rows(self):
        """How many rows the form's table region has (one per channel, except DMCS)."""
        return self.cp.nchan() if self.cp is not None else 0

    def _cell_changed(self, cd, row, name, val):
        if self.cp is None:
            return
        ctx = dict(self.ctx, row=row)
        old = cd.get(self.cp, ctx)
        if old != val:
            cd.set(self.cp, ctx, val)
            self.modified("%s %d %s: %s -> %s" % (self.row_label, row + 1, name, old, val))

    def _buttons(self):
        h = QHBoxLayout()
        h.addStretch(1)
        for b in self.meta.get('buttons', []):
            name = b.get('actionName')
            if name == 'help':
                continue
            btn = QPushButton(b.get('actionLabel') or name)
            if self.form == 'DCHN' and name in ('prev_chn', 'next_chn'):
                btn.clicked.connect(lambda _=False, d=(-1 if name == 'prev_chn' else 1): self.step_channel(d))
            elif self.form == 'DCHN' and name == 'add_chn':
                btn.clicked.connect(self.add_chn)
            elif self.form == 'DCHN' and name == 'delete_chn':
                btn.setToolTip("Deletes the last channel")
                btn.clicked.connect(self.delete_chn)
            elif self.form == 'DPORTCTL' and name in ('prev_port', 'next_port', 'prev_comp', 'next_comp'):
                btn.clicked.connect(lambda _=False, w=name: self.step_port(w))
            elif self.form == 'DACC' and name in ('prev_table', 'next_table'):
                btn.clicked.connect(lambda _=False, d=(-1 if name == 'prev_table' else 1): self.step_table(d))
            elif self.form == 'DACC' and name == 'add_table':
                btn.clicked.connect(self.add_table)
            elif self.form == 'DACC' and name == 'delete_table':
                btn.clicked.connect(self.delete_table)
            elif self.form == 'DACC' and name == 'reset_m_nac':
                btn.clicked.connect(self.reset_m_nac)
            else:
                # A button WinRSS has and qrss has no action for (Validate Configuration on
                # DSTCG/DHWCFG, the IntelliRepeater repeater list, the comparator/trunking
                # Add/Delete forms): leave it out rather than show one that does nothing.
                continue
            h.addWidget(btn)
        return h

    # -- data ---------------------------------------------------------------------
    @property
    def cp(self):
        return self.main.cp

    def refresh(self):
        for w in self.widgets.values():
            if isinstance(w, FieldWidget):
                w.load(self.cp, self.ctx)
            elif isinstance(w, QTableWidget) and getattr(w, 'region_cols', None) and w.isEnabled():
                self._load_table(w)

    def step_channel(self, d):
        if self.cp is None:
            return
        n = self.cp.nchan()
        self.ctx['channel'] = (self.ctx['channel'] + d) % max(n, 1)
        self.refresh()
        self.status("Channel %d of %d" % (self.ctx['channel'] + 1, n))

    def step_port(self, which):
        """DPORTCTL navigation: one port at a time within a chassis, or a whole chassis at a
        time.  The comparator's port table is 16 records per chassis."""
        if self.cp is None:
            return
        n = self.cp.nports()
        if not n:
            self.status("This codeplug has no port table")
            return
        r = min(self.ctx.get('row', 0), n - 1)     # a plug swapped under the page may be shorter
        last_chassis = (n - 1) // PORTS_PER_CHASSIS
        if which in ('prev_comp', 'next_comp'):
            c = min(max(r // PORTS_PER_CHASSIS + (-1 if which == 'prev_comp' else 1), 0), last_chassis)
            r = c * PORTS_PER_CHASSIS + r % PORTS_PER_CHASSIS
        else:
            r = (r // PORTS_PER_CHASSIS) * PORTS_PER_CHASSIS + \
                (r % PORTS_PER_CHASSIS + (-1 if which == 'prev_port' else 1)) % PORTS_PER_CHASSIS
        self.ctx['row'] = r
        self.refresh()
        self.status("Port %d of %d (%s, port %d of %d)"
                    % (r + 1, n, CHASSIS[min(r // PORTS_PER_CHASSIS, len(CHASSIS) - 1)],
                       r % PORTS_PER_CHASSIS + 1, PORTS_PER_CHASSIS))

    def step_table(self, d):
        """DACC navigation: the form shows one access code table at a time (the codeplug's
        dir[7] holds a u16 count then an 8-byte record per table; the RSS manual allows 16)."""
        if self.cp is None:
            return
        n = self.cp.naccess()
        if not n:
            self.status("This codeplug has no access code table")
            return
        self.ctx['table'] = (self.ctx.get('table', 0) + d) % n
        self.refresh()
        self.status("Access code table %d of %d" % (self.ctx['table'] + 1, n))

    def add_table(self):
        """'Add Table': append a copy of the table on screen, the way WinRSS adds one."""
        if self.cp is None:
            return
        try:
            n = add_access_table(self.cp, self.ctx.get('table', 0))
        except ValueError as e:
            self.status(str(e)); return
        self.ctx['table'] = n
        self.refresh()
        self.modified("Added access code table %d (copy of table %d)" % (n + 1, n))

    def delete_table(self):
        """'Delete Table': drop the table on screen.  WinRSS refuses while a channel uses it."""
        if self.cp is None:
            return
        t = self.ctx.get('table', 0)
        if dlg.question(self, "Delete access code table",
                        "Delete access code table %d?" % (t + 1)) != QMessageBox.StandardButton.Yes:
            return
        try:
            n = delete_access_table(self.cp, t)
        except ValueError as e:
            self.status(str(e)); return
        self.ctx['table'] = min(t, n - 1)
        self.refresh()
        self.modified("Deleted access code table %d" % (t + 1))

    def reset_m_nac(self):
        """'Reset Multi-NAC': clear the eight RX/TX NAC pairs in DEXAC (dir[18])."""
        if self.cp is None:
            return
        if dlg.question(self, "Reset Multi-NAC",
                        "Clear all eight RX and TX network access codes?") != QMessageBox.StandardButton.Yes:
            return
        try:
            reset_multi_nac(self.cp)
        except ValueError as e:
            self.status(str(e)); return
        self.refresh()
        self.modified("Reset the multi-NAC table")

    def add_chn(self):
        if self.cp is None:
            return
        n = add_channel(self.cp, self.ctx['channel'])
        self.ctx['channel'] = n
        self.refresh()
        self.modified("Added channel %d (copy of channel %d)" % (n + 1, n))

    def delete_chn(self):
        if self.cp is None:
            return
        n = self.cp.nchan()
        if n <= 1:
            self.status("Cannot delete the only channel"); return
        if dlg.question(self, "Delete channel", "Delete channel %d (the last channel)?" % n) != QMessageBox.StandardButton.Yes:
            return
        delete_last_channel(self.cp)
        self.ctx['channel'] = min(self.ctx['channel'], n - 2)
        self.refresh()
        self.modified("Deleted channel %d" % n)

    def modified(self, what):
        self.main.set_dirty(True, what)

    def status(self, text):
        self.main.statusBar().showMessage(text, 8000)

    def field_help(self, label, tag):
        self.main.help.show_field(label, tag)

    def _cell_help(self, t, col):
        if 0 <= col < len(t.region_help):
            self.field_help(*t.region_help[col])


class DmcsPage(FormPage):
    """Multi-Coded Squelch Table (RSS manual 3-16..3-19, WinRSS' DMCS form).

    The rows are the access code tables themselves (dir[7], one 8-byte record each): TX and RX
    squelch, the user access state and the entry number, plus the usage counters the station
    keeps in RAM - "Total Time Used" and "Number of Calls", read over RD MCS when the page
    opens.  The form's own fields (Modify Entry #, the squelch fields, User Access) belong to
    WinRSS' Modify User dialog, so this page puts them in one.
    """

    hide_rest = ('entry_no', 'rx_squelch_type', 'rx_code', 'tx_squelch_type', 'tx_code',
                 'access', 'keep_dialog')
    row_label = "entry"

    def __init__(self, form, main):
        self._snap = None          # (codeplug, image) as the page opened, for Cancel
        super().__init__(form, main)

    def table_rows(self):
        return self.cp.naccess() if self.cp is not None else 0

    # -- lifecycle ----------------------------------------------------------------
    def activate(self):
        cp = self.cp
        if cp is not None and (self._snap is None or self._snap[0] is not cp):
            self._snap = (cp, bytearray(cp.img))
        self.read_accounting()

    def read_accounting(self):
        """The usage counters and the station clock beside them (WinRSS: "you must read the
        codeplug from the station").  Read-only, and silent when there is no station."""
        cp, svc = self.cp, self.main.station
        if cp is None or not svc.connected or getattr(svc.link, 'is_comparator', False):
            return

        def fn(L):
            block = L.mcs_block()
            stamp = None
            try:
                when = L.get_time()[0]
                stamp = (when.strftime("%m/%d/%Y"), when.strftime("%H:%M:%S"))
            except StationError:
                pass
            return block, stamp

        def then(res):
            block, stamp = res
            cp.mcs = block or None
            if stamp:
                cp.mcs_stamp = stamp
            self.refresh()
            self.status("%d multi-coded squelch entr%s with usage data"
                        % (len(block) // 8, "y" if len(block) == 8 else "ies")
                        if block else "The station reports no MCS usage data "
                                      "(it is not in MULTI-PL ONLY mode)")
        self.main.station.run(fn, lambda res, err: then(res) if not err else self.status(str(err)))

    # -- buttons ------------------------------------------------------------------
    def _buttons(self):
        h = QHBoxLayout()
        h.addStretch(1)
        for b in self.meta.get('buttons', []):
            name = b.get('actionName')
            if name == 'help':
                continue
            btn = QPushButton(b.get('actionLabel') or name)
            if name == 'save_report':
                btn.clicked.connect(self.save_report)
            elif name == 'modify_user':
                btn.clicked.connect(self.modify_user)
            elif name == 'clear_accntng':
                btn.clicked.connect(self.clear_accounting)
            elif name == 'accept':
                btn.clicked.connect(self.accept)
            elif name == 'cancel':
                btn.clicked.connect(self.cancel)
            else:
                continue                 # see _buttons: no action, so no button
            h.addWidget(btn)
        return h

    def accept(self):
        """Accept: keep what the page changed (the edits are already in the codeplug)."""
        if self.cp is not None:
            self._snap = (self.cp, bytearray(self.cp.img))
        self.status("Multi-coded squelch table accepted")

    def cancel(self):
        """Cancel: put the codeplug back the way it was when the page opened."""
        if self.cp is None or self._snap is None or self._snap[0] is not self.cp:
            return
        self.cp.img[:] = self._snap[1]
        self.refresh()
        self.modified("Multi-coded squelch table: reverted the page's changes")

    def clear_accounting(self):
        """Clear Accounting (CLEAR MCS): zero the station's usage counters."""
        svc = self.main.station
        if not svc.connected or getattr(svc.link, 'is_comparator', False):
            dlg.warning(self, "Clear Accounting",
                        "The usage counters are in the station, not in the codeplug - "
                        "connect to the station first.")
            return
        if dlg.question(self, "Clear Accounting",
                        "Clear the multi-coded squelch usage counters "
                        "(total time used and number of calls) on the station?") != dlg.SB.Yes:
            return

        def then(_res):
            self.status("Cleared the station's MCS accounting")
            self.read_accounting()
        self.main.station.run(lambda L: L.clear_mcs(), lambda res, err: then(res) if not err else
                              self.status(str(err)))

    # -- Modify User --------------------------------------------------------------
    def modify_user(self):
        """WinRSS' Modify User dialog: pick an entry, edit its squelch codes and access state,
        add an entry or delete one."""
        cp = self.cp
        if cp is None:
            dlg.warning(self, "Modify User", "Open a codeplug first.")
            return
        d = QDialog(self)
        d.setWindowTitle("Modify User - Multi-Coded Squelch")
        g = QGridLayout(d)
        n = cp.naccess()
        spin = QSpinBox()
        spin.setRange(1, max(n, 1))
        spin.setValue(min(self.ctx.get('row', 0) + 1, max(n, 1)))
        g.addWidget(QLabel("Entry #"), 0, 0)
        g.addWidget(spin, 0, 1)
        edits = {}
        for r, (key, label, kind) in enumerate([('rx_squelch_type', 'Rx Type', 'type'),
                                                ('rx_code', 'Rx Freq/Code', 'code'),
                                                ('tx_squelch_type', 'Tx Type', 'type'),
                                                ('tx_code', 'Tx Freq/Code', 'code')], start=1):
            cd = codec('DMCS', key)
            w = QComboBox() if kind == 'type' else QLineEdit()
            if isinstance(w, QComboBox):
                w.addItems(cd.clist)
                compact_popup(w)
            edits[key] = w
            g.addWidget(QLabel(label), r, 0)
            g.addWidget(w, r, 1)
        acc = QComboBox()
        acc.addItems(codec('DMCS', 'access').clist)
        compact_popup(acc)
        g.addWidget(QLabel("User Access"), 5, 0)
        g.addWidget(acc, 5, 1)

        def load(*_):
            row = spin.value() - 1
            if row >= cp.naccess():
                return
            ctx = dict(self.ctx, row=row, interactive=True)
            for key, w in edits.items():
                v = codec('DMCS', key).get(cp, ctx)
                if isinstance(w, QComboBox):
                    w.setCurrentText(v)
                else:
                    w.setText(v)
            acc.setCurrentText(codec('DMCS', 'access').get(cp, ctx))
        spin.valueChanged.connect(load)
        load()

        def apply_entry():
            row = spin.value() - 1
            if row >= cp.naccess():
                return
            ctx = dict(self.ctx, row=row, interactive=True)
            for key, w in edits.items():
                cd = codec('DMCS', key)
                v = w.currentText() if isinstance(w, QComboBox) else w.text()
                old = cd.get(cp, ctx)
                if v != old:
                    cd.set(cp, ctx, v)
                    self.modified("entry %d %s: %s -> %s" % (row + 1, key, old, v))
            cd = codec('DMCS', 'access')
            old = cd.get(cp, ctx)
            if acc.currentText() != old:
                cd.set(cp, ctx, acc.currentText())
                self.modified("entry %d user access: %s -> %s" % (row + 1, old, acc.currentText()))

        def add_entry():
            src = spin.value() - 1
            try:
                k = add_access_table(cp, src)
            except ValueError as e:
                self.status(str(e)); return
            spin.setRange(1, cp.naccess())
            spin.setValue(k + 1)
            self.modified("Added entry %d (copy of entry %d)" % (k + 1, src + 1))
            self.refresh()

        def del_entry():
            row = spin.value() - 1
            if dlg.question(d, "Delete entry", "Delete entry %d?" % (row + 1)) != dlg.SB.Yes:
                return
            try:
                n2 = delete_access_table(cp, row)
            except ValueError as e:
                self.status(str(e)); return
            spin.setRange(1, max(n2, 1))
            spin.setValue(min(row + 1, max(n2, 1)))
            self.modified("Deleted entry %d" % (row + 1))
            self.refresh()

        h = QHBoxLayout()
        b_add, b_del = QPushButton("Add Entry"), QPushButton("Delete Entry")
        b_add.clicked.connect(add_entry)
        b_del.clicked.connect(del_entry)
        h.addWidget(b_add)
        h.addWidget(b_del)
        h.addStretch(1)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(d.reject)
        h.addWidget(b_cancel)
        ok = QPushButton("OK")
        ok.setDefault(True)

        def done():
            try:
                apply_entry()
            except Exception as e:      # a bad code leaves the dialog open
                dlg.warning(d, "Modify User", str(e))
                return
            self.ctx['row'] = spin.value() - 1
            self.refresh()
            d.accept()
        ok.clicked.connect(done)
        h.addWidget(ok)
        g.addLayout(h, 6, 0, 1, 2)
        d.exec()

    # -- Save Report --------------------------------------------------------------
    def save_report(self):
        """Save Report: the table and its usage counters as text, the way WinRSS' report reads."""
        cp = self.cp
        if cp is None:
            dlg.warning(self, "Save Report", "Open a codeplug first.")
            return
        base = os.path.splitext(self.main.path or self.main.cp_source or 'codeplug')[0]
        path, _ = QFileDialog.getSaveFileName(self, "Save MCS report", base + "_mcs.txt",
                                              "Text files (*.txt);;All files (*)")
        if not path:
            return
        cols = [("entry_no", "#", 3), ("rx_sq", "Rx Type", 8), ("rx_code", "Rx Freq/Code", 13),
                ("tx_sq", "Tx Type", 8), ("tx_code", "Tx Freq/Code", 13),
                ("access", "User Access", 12), ("time_used", "Total Time Used", 15),
                ("no_of_calls", "Number of Calls", 15)]
        cds = [(codec('DMCS', 'mcs_entry.' + k), head, w) for k, head, w in cols]
        lines = ["MULTI-CODED SQUELCH INFORMATION", "",
                 "Station Serial Number: %s" % cp.cstr(cp.si + 0x02, 10).strip()]
        stamp = getattr(cp, 'mcs_stamp', None)
        if stamp:
            lines.append("Station Date: %s   Station Time: %s" % stamp)
        if cp.mcs is None:
            lines.append("(usage data not read - read the codeplug from the station for it)")
        lines += ["", "".join(h.ljust(w) for _, h, w in cds).rstrip()]
        for row in range(cp.naccess()):
            ctx = dict(self.ctx, row=row)
            lines.append("".join(cd.get(cp, ctx).ljust(w) for cd, _, w in cds).rstrip())
        try:
            with open(path, 'w', encoding='latin1') as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            dlg.critical(self, "Save Report", "%s\n\n%s" % (path, e))
            return
        self.status("Saved MCS report %s" % path)


class InfoPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>Codeplug Information</b>"))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFrameShape(QFrame.Shape.NoFrame)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        v.addWidget(self.text)

    def refresh(self):
        m = self.main
        if m.cp is None:
            self.text.setPlainText("No codeplug loaded.  File > Open... to load a WinRSS .cp archive.")
            return
        lines = ["File: %s" % m.path, ""]
        lines += [v for k, v in m.file.items if k == 'c' and v.strip() not in ('//', '')]
        cp = m.cp
        lines += ["", "Codeplug version 0x%02X, size %d bytes, %d directory entries" % (cp.version, cp.size, cp.ndir),
                  "Checksum stored %04X, computed %04X" % (int.from_bytes(cp.img[0:2], 'big'), cksum(cp.img)),
                  "Directory: " + " ".join("%X" % d for d in cp.dir),
                  "Channels: %d" % cp.nchan()]
        if m.file.wc is not None:
            lines.append("Wildcard table: %d bytes" % len(m.file.wc))
        self.text.setPlainText("\n".join(lines))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(ICON))
        self.file = self.cp = self.path = None
        self.cp_source = None          # title/status name when there is no file path (e.g. a station read)
        self.dirty = False
        self.resize(1400, 900)
        self.pages = {}
        self.current_page = None
        from .station_qt import StationService
        self.station = StationService()
        self.station.connection.connect(self._station_conn)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self._build_config_tree(SYSTEM)
        self.tree.currentItemChanged.connect(self._select)

        self.stack = QStackedWidget()
        self.info = InfoPage(self)
        self.stack.addWidget(self.info)
        self.help = HelpPane()                # WinRSS help, beside the window instead of a modal dialog

        # left panel: the config/service tree, with station buttons beneath it
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(4)
        lv.addWidget(self.tree, 1)
        self.conn_left = QLabel("Not connected"); self.conn_left.setStyleSheet("color: #b00;")
        lv.addWidget(self.conn_left)
        # stacked in one column (not a 2-wide grid): two station buttons side by side forced the whole
        # left panel much wider than the tree on native macOS, which squeezed the pages
        self.connect_btn = QPushButton("Connect…"); self.connect_btn.clicked.connect(lambda: self.station_connect())
        self.disconnect_btn = QPushButton("Disconnect"); self.disconnect_btn.clicked.connect(lambda: self.station.disconnect_port())
        self.read_btn = QPushButton("Read from Station"); self.read_btn.clicked.connect(self.station_read_cp)
        self.read_btn.setToolTip("Read the codeplug from the station (Alt+R)")
        self.write_btn = QPushButton("Write to Station"); self.write_btn.clicked.connect(self.station_write_cp)
        self.write_btn.setToolTip("Write the codeplug to the station (Alt+W)")
        for b in (self.connect_btn, self.disconnect_btn, self.read_btn, self.write_btn):
            lv.addWidget(b)
        left.setMaximumWidth(250)          # the left column never balloons and squeezes the pages

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.stack)
        split.addWidget(self.help)
        split.setStretchFactor(0, 0)          # tree keeps its size
        split.setStretchFactor(1, 1)          # the page area takes new space when the window grows
        split.setStretchFactor(2, 0)          # help keeps its size
        split.setSizes([230, 935, 235])       # tree wide enough for the labels (left panel is capped at 250)
        self.setCentralWidget(split)

        fm = self.menuBar().addMenu("&File")
        for text, key, slot in (("&New", QKeySequence.StandardKey.New, self.new),
                                ("&Open...", QKeySequence.StandardKey.Open, self.open),
                                ("&Save", QKeySequence.StandardKey.Save, self.save),
                                ("Save &As...", QKeySequence.StandardKey.SaveAs, self.save_as),
                                (None, None, None),
                                ("Save Codeplug &Report...", None, self.save_report),
                                ("&Print Codeplug Report...", QKeySequence.StandardKey.Print, self.print_report),
                                (None, None, None),
                                ("&Merge WildCard from File...", QKeySequence("Ctrl+Shift+M"), self.merge_wildcard),
                                (None, None, None),
                                ("E&xit", QKeySequence.StandardKey.Quit, self.close)):
            if text is None:
                fm.addSeparator(); continue
            a = QAction(text, self)
            if key:
                a.setShortcut(key)
            a.triggered.connect(slot); fm.addAction(a)
        sm = self.menuBar().addMenu("&Station")
        # lambda, not self.station_connect directly: QAction.triggered passes a `checked` bool, which
        # would otherwise land in the `port` argument and connect to a bool instead of showing the dialog
        self.connect_action = QAction("&Connect…", self); self.connect_action.triggered.connect(lambda *_: self.station_connect()); sm.addAction(self.connect_action)
        self.disconnect_action = QAction("&Disconnect", self); self.disconnect_action.triggered.connect(lambda *_: self.station.disconnect_port()); sm.addAction(self.disconnect_action)
        sm.addSeparator()
        self.read_action = QAction("&Read Codeplug from Station", self)
        self.read_action.setShortcut(QKeySequence("Alt+R")); self.read_action.triggered.connect(lambda *_: self.station_read_cp()); sm.addAction(self.read_action)
        self.write_action = QAction("&Write Codeplug to Station…", self)
        self.write_action.setShortcut(QKeySequence("Alt+W")); self.write_action.triggered.connect(lambda *_: self.station_write_cp()); sm.addAction(self.write_action)
        sm.addSeparator()
        self.reset_action = QAction("&Restart Station", self)
        self.reset_action.triggered.connect(lambda *_: self.station_reset()); sm.addAction(self.reset_action)
        # WinRSS' Station Enable/Disable screen (RSS manual 5-33); the Status Panel header has
        # the same two buttons.
        self.disable_action = QAction("&Disable Station", self)
        self.disable_action.setToolTip("Stop normal station activity ( SET STATION_DISABLE ON)")
        self.disable_action.triggered.connect(lambda *_: self.station_set_access(True)); sm.addAction(self.disable_action)
        self.enable_action = QAction("&Enable Station", self)
        self.enable_action.setToolTip("Return the station to normal operation ( SET STATION_DISABLE OFF)")
        self.enable_action.triggered.connect(lambda *_: self.station_set_access(False)); sm.addAction(self.enable_action)
        self.backup_action = QAction("&Back Up Station…", self)
        self.backup_action.setToolTip("Save the station's codeplug, wildcard and alignment partitions")
        self.backup_action.triggered.connect(lambda *_: self.station_backup()); sm.addAction(self.backup_action)
        self.restore_action = QAction("Res&tore Station from Backup…", self)
        self.restore_action.setToolTip("Write a saved backup back and restart the station")
        self.restore_action.triggered.connect(lambda *_: self.station_restore()); sm.addAction(self.restore_action)
        self.pw_action = QAction("Set Device &Password…", self)
        self.pw_action.setToolTip("Change or disable the station's password (SETPASS)")
        self.pw_action.triggered.connect(lambda *_: self.station_set_password())
        sm.addAction(self.pw_action)
        self.cond_action = QAction("EEPROM &Conditioning…", self)
        self.cond_action.setToolTip("Read the EEPROM conditioning marker; optionally erase the EEPROM")
        self.cond_action.triggered.connect(lambda *_: self.station_conditioning()); sm.addAction(self.cond_action)
        tm = self.menuBar().addMenu("&Tools")
        self.datetime_action = QAction("Set Device &Date and Time…", self)
        self.datetime_action.setToolTip("Show the station's clock (GET TIME) and set it (SET DATE / SET TIME)")
        self.datetime_action.triggered.connect(lambda *_: self.station_datetime()); tm.addAction(self.datetime_action)
        self.address_action = QAction("Set &IP and MAC Address…", self)
        self.address_action.setToolTip("Show and set the station's IP and physical address (GET/SET ADDR IP, ADDR PHYS)")
        self.address_action.triggered.connect(lambda *_: self.station_address()); tm.addAction(self.address_action)
        self.conn_label = QLabel("Station: not connected")
        self.statusBar().addPermanentWidget(self.conn_label)
        hm = self.menuBar().addMenu("&Help")
        self.help_action = QAction("Show &Help Pane", self)
        self.help_action.setCheckable(True)
        self.help_action.setShortcut(QKeySequence("F1"))
        vis = QSettings("qrss", "qrss").value("help/visible", True, type=bool)
        self.help_action.setChecked(vis)
        self.help.setVisible(vis)
        self.help_action.toggled.connect(self.set_help_visible)
        hm.addAction(self.help_action)
        hm.addSeparator()
        a = QAction("&About", self); a.triggered.connect(self.about); hm.addAction(a)
        self.update_title()
        self.info.refresh()
        self.tree.setCurrentItem(self.info_item)
        self._station_ui(False, "Not connected")

    # -- navigation ---------------------------------------------------------------
    def _build_config_tree(self, system):
        """Build the left-hand tree for a station type: WinRSS's own menu for that type
        (menus.json) plus the service screens.  Called again when a codeplug of another type
        is loaded, since a comparator has five forms where a Quantar has twelve."""
        from .service_pages import SERVICE_PAGES
        self.system = system
        prev = self.tree.currentItem()
        keep = prev.data(0, Qt.ItemDataRole.UserRole) if prev is not None else None
        self.tree.blockSignals(True)
        self.tree.clear()
        root = QTreeWidgetItem(["Configuration (%s)" % system.title()])
        self.tree.addTopLevelItem(root)
        info = QTreeWidgetItem(["Codeplug Information"])
        info.setData(0, Qt.ItemDataRole.UserRole, '__info__')
        self.tree.addTopLevelItem(info)
        self.info_item = info
        for item in MENUS[system]:
            it = QTreeWidgetItem([item['label']])
            it.setData(0, Qt.ItemDataRole.UserRole, item['form'])
            root.addChild(it)
            if item['form'] == 'DCHN':
                t = QTreeWidgetItem(["Channel Table (all)"])
                t.setData(0, Qt.ItemDataRole.UserRole, '__chantable__')
                root.addChild(t)
            elif item['form'] == 'DPORTCTL':
                t = QTreeWidgetItem(["Port Table (all)"])
                t.setData(0, Qt.ItemDataRole.UserRole, '__porttable__')
                root.addChild(t)
        root.setExpanded(True)
        svc = QTreeWidgetItem(["Service (station)"])
        self.tree.addTopLevelItem(svc)
        for key, label, _cls in SERVICE_PAGES:
            if system == 'COMPARATOR' and not getattr(_cls, 'comparator', False):
                continue        # metering, alignment, status: Quantar-only commands
            if system != 'COMPARATOR' and getattr(_cls, 'comparator_only', False):
                continue        # the comparator's own Diagnostics / Alignment pages
            it = QTreeWidgetItem([label])
            it.setData(0, Qt.ItemDataRole.UserRole, key)
            svc.addChild(it)
        svc.setExpanded(True)
        # stay on the same screen when it exists in the new tree, else show the information page
        if keep:
            for top in range(self.tree.topLevelItemCount()):
                node = self.tree.topLevelItem(top)
                for i in range(node.childCount()):
                    if node.child(i).data(0, Qt.ItemDataRole.UserRole) == keep:
                        self.tree.setCurrentItem(node.child(i))
                        self.tree.blockSignals(False)
                        return
        self.tree.blockSignals(False)
        self.tree.setCurrentItem(info)

    def _select(self, item, _prev=None):
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key:
            return
        if self.current_page is not None and hasattr(self.current_page, 'deactivate'):
            self.current_page.deactivate()
        self.current_page = None
        if key == '__info__':
            self.info.refresh(); self.stack.setCurrentWidget(self.info)
            self.help.set_page(item.text(0), ()); return
        if key not in self.pages:
            if key.startswith('__svc_'):
                from .service_pages import SERVICE_PAGES
                page = dict((k, c) for k, _l, c in SERVICE_PAGES)[key](self)
            elif key == '__chantable__':
                from .chan_table import ChannelTablePage
                page = ChannelTablePage(self, FORMS)
            elif key == '__porttable__':
                from .chan_table import PortTablePage
                page = PortTablePage(self, FORMS)
            elif key == 'DWCSTATE':
                from .wc_pages import WcTablesPage
                page = WcTablesPage(self)
            elif key == 'DMCS':
                page = DmcsPage(key, self)
            elif key in ('DWCINPUT', 'DWCOUTPUT'):
                from .wc_pages import WcIoPage
                page = WcIoPage(self, key == 'DWCOUTPUT')
            else:
                page = FormPage(key, self)
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
            sa = QWidget(); lay = QVBoxLayout(sa); lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(scroll, 1)
            if getattr(page, 'button_bar', None) is not None:
                lay.addWidget(page.button_bar)
            self.pages[key] = (page, sa)
            self.stack.addWidget(sa)
        page, sa = self.pages[key]
        page.refresh()
        self.stack.setCurrentWidget(sa)
        self.current_page = page
        self.help.set_page(item.text(0), self._page_help(key, page))
        if hasattr(page, 'activate'):
            page.activate()

    # -- help pane ------------------------------------------------------------------
    @staticmethod
    def _page_help(key, page):
        """Service screens name their topics; configuration windows use their form's topic."""
        tags = tuple(getattr(page, 'help_tags', ()) or ())
        form = {'__chantable__': 'DCHN', '__porttable__': 'DPORTCTL'}.get(key, key)
        if not tags and FORMS.get(form, {}).get('help'):
            tags = (FORMS[form]['help'],)
        return tags

    def set_help_visible(self, on):
        self.help.setVisible(on)
        if self.help_action.isChecked() != on:
            self.help_action.setChecked(on)
        QSettings("qrss", "qrss").setValue("help/visible", on)

    def show_help_topics(self, label, tags):
        """Help buttons on the service screens: these topics in the pane (opened if hidden)."""
        self.set_help_visible(True)
        self.help.show_other(label, tags)

    # -- station ------------------------------------------------------------------
    def station_connect(self, port=None):
        if port is None:
            from .service_pages import ConnectDialog
            d = ConnectDialog(self)
            if dlg._run(d, self) != QDialog.DialogCode.Accepted:
                return
            port, baud, mmi, pwd, modem = d.values()
        else:
            baud, mmi, pwd, modem = 9600, 9600, None, None
        what = ("dialing %s on %s" % (modem["number"], port)) if modem else port
        self.conn_label.setText("Station: connecting to %s…" % what)
        self.conn_left.setText("Connecting to %s…" % what)
        self.station.connect_port(port, baud, mmi, pwd, modem=modem)

    def station_read_cp(self):
        if not self.station.connected:
            dlg.warning(self, "Read codeplug", "Connect to a station first (Station ▸ Connect…).")
            return
        if not self.maybe_save():
            return
        self.statusBar().showMessage("Reading codeplug from station…")

        def fn(L):
            img = bytes(L.read_codeplug(
                progress=lambda done, size: self.station.traffic.emit('#', "read %d/%d bytes" % (done, size))))
            try:
                wc = bytes(L.read_wildcard())
            except StationError:
                wc = None                     # a station with no wildcard block
            return img, wc

        def cb(res, err):
            if err:
                dlg.critical(self, "Read codeplug", "Could not read the codeplug:\n\n%s" % err)
                self.statusBar().showMessage("Codeplug read failed", 5000)
                return
            img, wc = res
            f = CpFile.from_image(img, ["! codeplug read from %s" % self.station.desc], wc=wc)
            self._set_codeplug(f, Codeplug(bytearray(img)), None, "station codeplug (unsaved)")
            self.statusBar().showMessage("Read %d bytes from the station - File ▸ Save As to keep it" % len(img), 8000)

        self.station.run(fn, cb)

    def station_reset(self):
        """Restart the station's application the way WinRSS does when a service screen closes."""
        if not self.station.connected:
            dlg.warning(self, "Restart station", "Connect to a station first (Station ▸ Connect…).")
            return
        if dlg.question(self, "Restart station",
                        "Restart the station's application?\n\n"
                        "The station stops for about 12 s and comes back with the codeplug and the "
                        "alignment values re-read from memory.  Anything changed but not saved is lost.",
                        dlg.SB.Yes | dlg.SB.No, dlg.SB.No) != dlg.SB.Yes:
            return
        self.statusBar().showMessage("Restarting the station…")

        def cb(res, err):
            if err:
                dlg.critical(self, "Restart station", "The station did not restart cleanly:\n\n%s" % err)
                self.statusBar().showMessage("Station restart failed", 5000)
            else:
                self.statusBar().showMessage("Station restarted", 8000)

        self.station.run(lambda L: L.reset(), cb)

    def station_backup(self):
        """Save the station's codeplug, wildcard and SA (alignment) partitions to one file."""
        if not self.station.connected:
            dlg.warning(self, "Back up station", "Connect to a station first (Station ▸ Connect…).")
            return
        name = "qrss-backup-%s.qbk" % time.strftime("%Y%m%d-%H%M")
        path, _ = QFileDialog.getSaveFileName(self, "Back up station", name,
                                              "qrss station backups (*.qbk)")
        if not path:
            return
        self.statusBar().showMessage("Reading the station's NVM…")

        def cb(res, err):
            if err:
                dlg.critical(self, "Back up station", "Could not read the station:\n\n%s" % err)
                self.statusBar().showMessage("Station backup failed", 5000)
                return
            try:
                res.save(path)
            except Exception as e:
                dlg.critical(self, "Back up station", "Could not write %s:\n\n%s" % (path, e))
                return
            self.statusBar().showMessage("Station backed up to %s" % path, 8000)
            dlg.information(self, "Back up station",
                            "Saved %s.\n\n%s" % (os.path.basename(path), res.summary()))

        self.station.run(lambda L: L.read_backup(
            progress=lambda done, size: self.station.traffic.emit('#', "read %d/%d bytes" % (done, size))), cb)

    def station_restore(self):
        """Write a whole-station backup back and restart the station for it to take effect."""
        if not self.station.connected:
            dlg.warning(self, "Restore station", "Connect to a station first (Station ▸ Connect…).")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Restore station from backup", "",
                                              "qrss station backups (*.qbk);;All files (*)")
        if not path:
            return
        try:
            from .backup import StationBackup
            bk = StationBackup.load(path)
        except Exception as e:
            dlg.critical(self, "Restore station", "Could not read %s:\n\n%s" % (path, e))
            return
        when = bk.created or "unknown time"
        if dlg.question(self, "Restore station",
                        "Write this backup to the station?\n\n%s\nTaken %s.\n\n"
                        "This OVERWRITES the station's codeplug, wildcard block and alignment values, "
                        "then restarts it (about 15 s)." % (bk.summary(), when),
                        dlg.SB.Yes | dlg.SB.No, dlg.SB.No) != dlg.SB.Yes:
            return
        self.statusBar().showMessage("Restoring the station…")

        def fn(L):
            L.write_backup(bk, progress=lambda done, size: self.station.traffic.emit(
                '#', "wrote %d/%d bytes" % (done, size)))
            L.reset()

        def cb(res, err):
            if err:
                dlg.critical(self, "Restore station",
                             "The restore did not complete cleanly:\n\n%s\n\n"
                             "The station's codeplug may be unchanged or partially written." % err)
                self.statusBar().showMessage("Station restore failed", 5000)
                return
            self.statusBar().showMessage("Station restored and restarted", 8000)
            dlg.information(self, "Restore station",
                            "The station was written and restarted.\n\nCheck the alignment values "
                            "before putting it back in service.")

        self.station.run(fn, cb)

    def station_set_password(self):
        """WinRSS' Tools ▸ Set Device Password: GET STATE PASS to say whether one is set, then
        SETPASS <current> <new> <new> at the ]-O prompt.  A blank new password disables it."""
        if not self.station.connected:
            dlg.warning(self, "Set device password",
                        "Connect to a station first (Station ▸ Connect…).")
            return
        from .service_pages import PasswordDialog
        d = PasswordDialog(self, self.station.pw_state)
        if dlg._run(d, self) != QDialog.DialogCode.Accepted:
            return
        cur, new, conf = d.values()
        self.statusBar().showMessage("Setting the station's password…")

        def cb(res, err):
            if err:
                dlg.critical(self, "Set device password",
                             "The station did not change its password:\n\n%s" % err)
                self.statusBar().showMessage("Password not changed", 8000)
                return
            self.station.pw_state = bool(new)
            msg = ("The station's password is now set." if new else
                   "Password protection is off - the station no longer asks for a password.")
            if new:
                # the Connect dialog keeps it, so the next connection answers the prompt
                QSettings("qrss", "qrss").setValue("station/password", new)
            self.statusBar().showMessage(msg, 10000)
            dlg.information(self, "Set device password", msg + "\n\n" + (res or "").strip())

        self.station.run(lambda L: L.set_password(cur, new, conf), cb)

    def station_set_access(self, disabled):
        """WinRSS' Station Enable/Disable: the station's own access state, ` SET STATION_DISABLE
        ON|OFF` - not the front panel's ACC_DIS key, which leaves the access state alone.
        Disabled, the station drops to ]-O and refuses DORAP, so the front panel monitor,
        metering and alignment cannot start; OFF restores them without a reset.  The station
        goes quiet for ~12 s while it applies the change, so the link waits it out."""
        if not self.station.connected:
            dlg.warning(self, "Station access", "Connect to a station first (Station ▸ Connect…).")
            return
        if disabled and dlg.question(self, "Disable station",
                                     "Stop normal station activity?\n\n"
                                     "The station drops to the main prompt for about 12 s and refuses "
                                     "the front panel monitor, metering and alignment until it is "
                                     "enabled again.  It does not restart.",
                                     dlg.SB.Yes | dlg.SB.No, dlg.SB.No) != dlg.SB.Yes:
            return
        self.statusBar().showMessage("Station access %s…" % ("disable" if disabled else "enable"))

        def fn(L):
            L.set_station_disabled(disabled)
            return L.station_access()

        def cb(res, err):
            if err:
                dlg.critical(self, "Station access",
                             "The station did not change its access state:\n\n%s" % err)
                self.statusBar().showMessage("Station access unchanged", 8000)
                return
            state, value = res
            self.statusBar().showMessage("Station access %s (status 0x%08X)" % (state.lower(), value), 10000)
            dlg.information(self, "Station access",
                             "The station reports its access state as %s.\n\n"
                             "Status word: 0x%08X" % (state.upper(), value))

        self.station.run(fn, cb)

    def station_datetime(self):
        """WinRSS' Tools ▸ Set Device Date And Time: read the station's clock (GET TIME), then
        let the operator set it (SET DATE / SET TIME).  Nothing else in the station depends on
        the clock except the alarm timestamps in the Status Report."""
        if not self.station.connected:
            dlg.warning(self, "Set device date and time",
                        "Connect to a station first (Station ▸ Connect…).")
            return
        self.station.run(lambda L: L.get_time(), self._datetime_read)

    def station_address(self):
        """WinRSS' Tools ▸ Set IP and MAC Address (RSS manual 3-6/3-7): read GET ADDR IP and
        GET ADDR PHYS, then write SET ADDR IP / SET ADDR PHYS and restart the station so it
        uses them.  Only a networked station (IntelliRepeater, comparator) uses either."""
        if not self.station.connected:
            dlg.warning(self, "Set IP and MAC address",
                        "Connect to a station first (Station ▸ Connect…).")
            return
        self.station.run(lambda L: L.get_addresses(), self._address_read)

    def _address_read(self, res, err):
        from .service_pages import NetworkAddressDialog
        d = NetworkAddressDialog(self, self.station, res, str(err) if err else "")
        dlg._run(d, self)

    def _datetime_read(self, res, err):
        from .service_pages import DateAndTimeDialog
        d = DateAndTimeDialog(self, self.station, res[0] if res else None,
                              str(err) if err else "")
        dlg._run(d, self)

    def station_conditioning(self):
        """The EEPROM conditioning marker, and (on request) the station's ERASE EEPROM.

        The application conditions its own EEPROM at boot when the marker is missing or stale
        (00, then FF, then the original bytes over every NVM partition, then SA[0x7C] = FADE
        and SA[0x7E] = the EEPROM's id byte).  WinRSS never shows this and never sends ERASE
        EEPROM; the marker is readable read-only as RD AL 7C 7E."""
        if not self.station.connected:
            dlg.warning(self, "EEPROM conditioning", "Connect to a station first (Station ▸ Connect…).")
            return
        self.station.run(lambda L: L.conditioning_state(), self._conditioning_report)

    def _conditioning_report(self, res, err):
        if err:
            dlg.critical(self, "EEPROM conditioning", "Could not read the marker:\n\n%s" % err)
            return
        cond, mark, ident = res
        if cond:
            head = ("The station's EEPROM is conditioned.\n\n"
                    "Marker SA[7C] = %04X, EEPROM id byte %02X." % (mark, ident))
        else:
            head = ("The station's EEPROM is NOT conditioned.\n\n"
                    "Marker SA[7C] = %04X, EEPROM id byte %02X (expected %04X).\n"
                    "The application conditions the EEPROM by itself at its next restart."
                    % (mark, ident, Link.COND_MARK))
        text = (head + "\n\nConditioning writes 00, then FF, then the original bytes over every NVM "
                "partition.  The station also has an older, much ruder statement - ERASE EEPROM, "
                "which fills the whole 8 KB with FF and takes the codeplug, the wildcard and the "
                "alignment values with it.\n\nErase the whole EEPROM?")
        if dlg.question(self, "EEPROM conditioning", text, dlg.SB.Yes | dlg.SB.No, dlg.SB.No) != dlg.SB.Yes:
            return
        if dlg.question(self, "Erase EEPROM",
                        "This ERASES the station's whole EEPROM (8 KB to FF).\n\n"
                        "The codeplug, the wildcard block and the alignment values are gone; the "
                        "station has to be reset, written with an archived codeplug and re-aligned.\n\n"
                        "Have you saved the codeplug?  Proceed?",
                        dlg.SB.Yes | dlg.SB.No, dlg.SB.No) != dlg.SB.Yes:
            return
        self.statusBar().showMessage("Erasing the station's EEPROM…")

        def cb(res, err):
            if err:
                dlg.critical(self, "Erase EEPROM", "The erase did not complete cleanly:\n\n%s" % err)
                self.statusBar().showMessage("EEPROM erase failed", 5000)
                return
            self.statusBar().showMessage("EEPROM erased - reset the station and write the codeplug back", 15000)
            dlg.information(self, "Erase EEPROM",
                            "The station erased its EEPROM.\n\nRestart the station (Station ▸ Restart "
                            "Station), then write the archived codeplug back and re-align it.")

        self.station.run(lambda L: L.erase_eeprom(), cb)

    def station_write_cp(self):
        if not self.station.connected:
            dlg.warning(self, "Write codeplug", "Connect to a station first (Station ▸ Connect…).")
            return
        if self.file is None or self.cp is None:
            dlg.warning(self, "Write codeplug", "No codeplug loaded.")
            return
        if self.cp.version not in SYSTEM_FOR_VERSION:
            dlg.warning(self, "Write codeplug", "Codeplug version 0x%02X is not decoded."
                        % self.cp.version)
            return
        n = len(self.cp.img)
        wc = self.file.wc if self.file is not None else None
        if dlg.question(self, "Write codeplug to station",
                        "This OVERWRITES the station's codeplug with the one in the editor (%d bytes)%s "
                        "and commits it to the station's NVM.\n\nProceed?"
                        % (n, " and its wildcard block (%d bytes)" % len(wc) if wc is not None else "")) \
                != QMessageBox.StandardButton.Yes:
            return
        img = bytearray(self.cp.img)
        img[0:2] = cksum(img).to_bytes(2, 'big')       # refresh the header checksum, as a file save does
        img = bytes(img)
        if wc is not None:
            wc = bytearray(wc)
            wc[0:2] = cksum(wc).to_bytes(2, 'big')
            wc = bytes(wc)
        self.statusBar().showMessage("Writing codeplug to station…")

        def fn(L):
            # stage both areas first, then commit both: a failure before the MOVEs leaves the
            # station's stored images untouched
            L.write_codeplug(img, move=wc is None,
                             progress=lambda done, size: self.station.traffic.emit('#', "wrote %d/%d bytes" % (done, size)))
            if wc is not None:
                L.write_wildcard(wc, move=False,
                                 progress=lambda done, size: self.station.traffic.emit('#', "wildcard %d/%d bytes" % (done, size)))
                L.call("MOVE CP", timeout=15.0)     # RAM -> NVM; give it room
                L.call("MOVE WC", timeout=15.0)
            return n

        def cb(res, err):
            if err:
                dlg.critical(self, "Write codeplug",
                             "Write failed - the station's codeplug may be unchanged or partially written:\n\n%s" % err)
                self.statusBar().showMessage("Codeplug write failed", 5000)
                return
            self.statusBar().showMessage("Wrote %d bytes to the station" % res, 8000)
            dlg.information(self, "Write codeplug",
                            "Wrote %d bytes and committed to NVM.\n\nReset the station for it to take effect." % res)

        self.station.run(fn, cb)

    def _station_conn(self, ok, desc):
        self.conn_label.setText("Station: %s" % (desc if ok else "not connected"))
        self._station_ui(ok, desc if ok else "Not connected")
        if ok and self.station.cond is not None and not self.station.cond[0]:
            self.statusBar().showMessage("EEPROM not conditioned (SA[7C] = %04X) - the station "
                                         "conditions it at its next restart" % self.station.cond[1], 10000)
        if not ok and desc not in ("disconnected",):
            dlg.warning(self, "Station", "Could not connect:\n\n%s" % desc)
        if ok and self.current_page is not None and hasattr(self.current_page, 'activate'):
            self.current_page.activate()

    def _station_ui(self, connected, text):
        """Reflect the connection state in the left-panel label, buttons and the Station menu."""
        self.conn_left.setText(text)
        self.conn_left.setStyleSheet("color: #070;" if connected else "color: #b00;")
        for w in (self.disconnect_btn, self.read_btn, self.write_btn,
                  self.disconnect_action, self.read_action, self.write_action, self.cond_action,
                  self.backup_action, self.restore_action, self.pw_action, self.datetime_action,
                  self.disable_action, self.enable_action):
            w.setEnabled(connected)

    # -- file handling --------------------------------------------------------------
    def _set_codeplug(self, f, cp, path, source):
        """Install a loaded codeplug (from a file or a station read) and refresh every page.
        `path` is the .cp path or None; `source` is the name shown in the title/status bar."""
        system = SYSTEM_FOR_VERSION.get(cp.version)
        if system is None:
            dlg.warning(self, "Codeplug version",
                                "Codeplug version 0x%02X is not decoded; it is shown read-only."
                                % cp.version)
            system = SYSTEM
        elif system != self.system:
            self._build_config_tree(system)
        self.file, self.cp, self.path, self.cp_source = f, cp, path, source
        for page, _ in self.pages.values():
            if hasattr(page, 'ctx'):
                # Back to the first record: the pages that step through a table (DCHN's
                # channel, DPORTCTL's port, DACC's access table) keep their position in ctx,
                # and a position past the new plug's table would read outside its image.
                for k in ('channel', 'row', 'table'):
                    if k in page.ctx:
                        page.ctx[k] = 0
            page.refresh()
        self.info.refresh()
        self.set_dirty(False)

    def load(self, path):
        f = CpFile(path)
        was = f.upgrade()                  # an older codeplug is converted (RSS manual 7-19)
        if was is None:
            self._set_codeplug(f, Codeplug(f.cp), path, os.path.basename(path))
            self.statusBar().showMessage("Loaded %s" % path, 5000)
            return
        # An upgraded archive opens as an unsaved codeplug, so the original file is only
        # replaced if the user saves over it deliberately.
        self._set_codeplug(f, Codeplug(f.cp), None,
                           "%s (upgraded from 0x%02X)" % (os.path.basename(path), was))
        self.statusBar().showMessage("Loaded %s - codeplug version 0x%02X upgraded to 0x28; "
                                     "Save As writes the new one" % (path, was), 10000)

    def new(self):
        """A blank conventional codeplug.  WinRSS release 14 has no File > New at all and its
        install ships no template files (the RSS manual's page 2-1 describes an older RSS), so
        qrss carries its own: qrss/data/template.cp, built by tools/mktemplate.py - a QUANTAR,
        version 0x28 plug with one blank channel, a blank access table and the standard empty
        wildcard block."""
        if not self.maybe_save():
            return
        path = os.path.join(DATA, 'template.cp')
        try:
            f = CpFile(path)
        except Exception as e:
            dlg.critical(self, "New codeplug", "%s\n\n%s" % (path, e))
            return
        self._set_codeplug(f, Codeplug(f.cp), None, "new codeplug")
        self.set_dirty(False)
        self.statusBar().showMessage("New codeplug - File ▸ Save As to keep it", 8000)

    def open(self):
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open codeplug archive", "", "Codeplug archives (*.cp *.CP);;All files (*)")
        if path:
            try:
                self.load(path)
            except Exception as e:
                dlg.critical(self, "Open failed", "%s\n\n%s" % (path, e))

    def save(self):
        if self.file is None:
            return False
        if not self.path:                 # a File > New codeplug has no path yet
            return self.save_as()
        if self.cp.version not in SYSTEM_FOR_VERSION:
            dlg.warning(self, "Save", "Codeplug version 0x%02X is not decoded."
                        % self.cp.version)
            return False
        self.file.save(self.path)
        self.set_dirty(False)
        self.info.refresh()
        self.statusBar().showMessage("Saved %s (checksum %04X)" % (self.path, cksum(self.cp.img)), 5000)
        return True

    def save_as(self):
        if self.file is None:
            return False
        path, _ = QFileDialog.getSaveFileName(self, "Save codeplug archive", self.path or "", "Codeplug archives (*.cp)")
        if not path:
            return False
        self.path = path
        self.cp_source = os.path.basename(path)
        return self.save()

    # -- codeplug report (WinRSS File > Save/Print Codeplug Report) -------------------
    def report_text(self):
        """The whole codeplug as text - qrss/report.py, which follows WinRSS' report layout."""
        return codeplug_report(self.file, name=self.path or self.cp_source)

    def save_report(self):
        if self.cp is None:
            dlg.warning(self, "Save Codeplug Report", "Open a codeplug first.")
            return
        base = os.path.splitext(self.path or self.cp_source or 'codeplug')[0]
        path, _ = QFileDialog.getSaveFileName(self, "Save codeplug report", base + ".txt",
                                              "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='latin1') as f:
                f.write(self.report_text())
        except OSError as e:
            dlg.critical(self, "Save Codeplug Report", "%s\n\n%s" % (path, e))
            return
        self.statusBar().showMessage("Saved codeplug report %s" % path, 5000)

    def print_report(self):
        if self.cp is None:
            dlg.warning(self, "Print Codeplug Report", "Open a codeplug first.")
            return
        from PySide6.QtGui import QTextDocument
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if QPrintDialog(printer, self).exec() != QDialog.DialogCode.Accepted:
            return
        doc = QTextDocument()
        # the report is column-aligned text: it needs a real monospace font (the Qt "monospace"
        # system alias can resolve to a proportional one, which loses the columns)
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        font.setPointSize(9)          # 78-column report on Letter/A4 without wrapping
        doc.setDefaultFont(font)
        doc.setPlainText(self.report_text())
        doc.print_(printer)
        self.statusBar().showMessage("Sent codeplug report to the printer", 5000)

    def merge_wildcard(self):
        """WinRSS's F5 on the Read Codeplug screen: "MERGE WildCard Data from Archive Disk File
        into Codeplug in memory.  This will overwrite any wildcard tables already existing in
        memory."  Table-by-table selection is on the WildCard Tables page (Import...)."""
        if self.file is None or self.cp is None:
            dlg.warning(self, "Merge WildCard", "Open a codeplug first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Merge WildCard from archive", "",
                                              "Codeplug archives (*.cp *.CP);;All files (*)")
        if not path:
            return
        try:
            src = CpFile(path)
        except Exception as e:
            dlg.critical(self, "Merge WildCard", "%s\n\n%s" % (path, e))
            return
        if src.wc is None:
            dlg.warning(self, "Merge WildCard", "%s has no WildCard block." % os.path.basename(path))
            return
        from .wildcard import Wildcard
        other = Wildcard(bytearray(src.wc))
        cur = Wildcard(bytearray(self.file.wc)) if self.file.wc else None
        if dlg.question(self, "Merge WildCard",
                        "Replace the %d WildCard table(s) in memory with the %d from %s (%d bytes)?\n\n"
                        "The tables can be picked one by one on the WildCard Tables page (Import...)."
                        % (len(cur.tables) if cur else 0, len(other.tables),
                           os.path.basename(path), len(src.wc))) != QMessageBox.StandardButton.Yes:
            return
        self.set_wildcard(src.wc, "merged WildCard from %s" % os.path.basename(path))

    def set_wildcard(self, wc, what):
        """Install a WildCard block in the open codeplug (a whole-block merge or an import)."""
        self.file.set_wc(wc)
        for page, _ in self.pages.values():
            if hasattr(page, 'load_wc'):
                page.refresh()
        self.info.refresh()
        self.set_dirty(True, what)

    def maybe_save(self):
        if not self.dirty:
            return True
        r = dlg.question(self, "Unsaved changes", "Save changes to %s?" % (self.cp_source or self.path),
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                         QMessageBox.StandardButton.Save)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def closeEvent(self, ev):
        if not self.maybe_save():
            ev.ignore()
            return
        if self.current_page is not None and hasattr(self.current_page, 'deactivate'):
            self.current_page.deactivate()
        self.station.shutdown()          # back to 9600 and the port closed, not left fast
        ev.accept()

    def set_dirty(self, d, what=None):
        self.dirty = d
        self.update_title()
        if what:
            self.statusBar().showMessage(what, 5000)

    def update_title(self):
        name = self.cp_source or (os.path.basename(self.path) if self.path else "no codeplug")
        self.setWindowTitle("QBS %s - %s%s" % (__version__, name, " *" if self.dirty else ""))

    def about(self):
        dlg.about(self, "About QBS",
                          "QBS %s - Quantar Base Station Programmer\n"
                          "Cross-platform codeplug editor for conventional Motorola Quantar stations.\n\n"
                          "Screens are rebuilt from WinRSS metadata. Greyed fields are not decoded yet." % __version__)


def main():
    if "--version" in sys.argv[1:]:
        # Before Qt is started, so it works on a headless machine - the release
        # builds use it as a smoke test of the frozen bundle.  A windowed Windows
        # exe has no stdout (PyInstaller sets it to None), hence the guard.
        if sys.stdout is not None:
            sys.stdout.write("QBS %s\n" % __version__)
        return
    if sys.platform == "darwin":
        # macOS reads the application-menu name ("Python" for a `python -m` run, with About/Hide/Quit)
        # from the bundle's CFBundleName.  Override it BEFORE QApplication builds the menu so it says QBS.
        try:
            from Foundation import NSBundle
            b = NSBundle.mainBundle()
            info = b.localizedInfoDictionary() or b.infoDictionary()
            if info is not None:
                info["CFBundleName"] = "QBS"
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("QBS")
    app.setWindowIcon(QIcon(ICON))        # window / taskbar icon (the W9CR bat, not the Python rocket)
    if sys.platform == "darwin":
        # setWindowIcon does not change the macOS Dock icon for a non-bundled `python -m` process;
        # set it natively when pyobjc is available (pip install pyobjc-framework-Cocoa), else no-op.
        try:
            from AppKit import NSApplication, NSImage
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initByReferencingFile_(ICON))
        except Exception:
            pass
    app.setStyleSheet(THEME)
    w = MainWindow()
    w.show()
    args = sys.argv[1:]
    if '--sim' in args:
        args.remove('--sim')
        w.station_connect("SIMULATOR")
    if args:
        try:
            w.load(args[0])
        except Exception as e:
            dlg.critical(w, "Open failed", "%s\n\n%s" % (args[0], e))
    sys.exit(app.exec())
