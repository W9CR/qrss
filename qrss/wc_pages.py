"""WildCard screens: state tables (IF states THEN actions ELSE inactions) and aux I/O."""
import os
import re
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTableWidget, QVBoxLayout, QWidget, QHeaderView)
from PySide6.QtGui import QFontDatabase

from . import dlg
from .codeplug import CpFile
from .help import HelpHook
from .wildcard import Wildcard, Table, Item, STATES, ACTIONS, COND

def _natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', s)]


STATE_NAMES = [""] + sorted(STATES, key=_natural)
ACTION_NAMES = [""] + sorted(ACTIONS, key=_natural)


def _combo(items, cur="", search=False):
    c = QComboBox()
    c.addItems(items)
    c.setStyleSheet("QComboBox { combobox-popup: 0; }")   # scrolling popup, not screen-tall
    c.setMaxVisibleItems(20)
    if search:                       # type a few letters to jump/filter
        c.setEditable(True)
        c.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        comp = c.completer()
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
    if cur and c.findText(cur) < 0:
        c.addItem(cur)
    c.setCurrentText(cur or "")
    return c


def _oneline(s):
    return " ".join(s.split())


def wildcard_oper(cp):
    """The codeplug's WildCard option: DISABLED, BASIC (4-wire) or ENHANCED (8-wire)."""
    if cp is None:
        return None
    o = cp.img[cp.si + 0x50] & 0x0F
    return {0: "DISABLED", 4: "BASIC", 8: "ENHANCED"}.get(o, "UNKNOWN (%X)" % o)


class WcImportDialog(QDialog):
    """Pick tables out of another archive's WildCard block.

    Only the aux I/O *lines* are a hardware matter - BASIC (4-wire) has 8 inputs / 4 outputs,
    ENHANCED (8-wire) 12 / 10, and every block stores the full 12 / 10 records either way; the
    timers and functions are identical, and programming a station as ENHANCED on a 4-wire wireline
    is normal.  So nothing here is enforced: the destination keeps its own aux settings, and a
    source table naming an unwired line still imports and still runs its logic."""

    def __init__(self, parent, path, dst, src, oper=None):
        super().__init__(parent)
        self.setWindowTitle("Import WildCard tables")
        self.resize(760, 520)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>%s</b> - %d table(s), %d bytes" % (os.path.basename(path), len(src.tables), len(src.img))))
        if oper == "BASIC":
            w = QLabel("This station's WildCard is BASIC (4-wire wireline): inputs 9-12 and outputs "
                       "5-10 have no physical lines.  The tables' functions and timers are the same "
                       "as ENHANCED, so they import unchanged.")
            w.setWordWrap(True)
            v.addWidget(w)
        bad = dst.io_mismatch(src)
        if bad:
            w = QLabel("Aux line settings that differ from the source (this codeplug keeps its own): "
                       "%s." % ", ".join(bad))
            w.setWordWrap(True)
            w.setStyleSheet("color: #555;")
            v.addWidget(w)
        self.list = QListWidget()
        for i, t in enumerate(src.tables):
            it = QListWidgetItem("%2d  %-15s  %s" % (i + 1, t.desc, _oneline(src.describe(t))))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(it)
        v.addWidget(self.list, 1)
        self.replace = QCheckBox("Replace tables with the same description (otherwise both are kept)")
        self.replace.setChecked(True)
        v.addWidget(self.replace)
        h = QHBoxLayout()
        h.addStretch(1)
        for text, fn in (("Import", self.accept), ("Cancel", self.reject)):
            b = QPushButton(text); b.clicked.connect(fn); h.addWidget(b)
        v.addLayout(h)

    def selected(self):
        return [i for i in range(self.list.count()) if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def do_replace(self):
        return self.replace.isChecked()


class WcBase(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.wc = None
        self._loading = False

    def load_wc(self):
        f = self.main.file
        self.wc = Wildcard(f.wc) if f is not None and f.wc is not None else None

    def changed(self, what):
        if self._loading or self.wc is None:
            return
        self.wc.store()
        self.main.set_dirty(True, what)

    # -- help pane: WinRSS topics from forms.json DWCSTATE / DWCINPUT / DWCOUTPUT ----------
    def _help(self, label, tag, entry=None):
        if hasattr(self.main, 'help'):
            self.main.help.show_field(label, tag, entry)

    def _hook(self, w, fn):
        """fn() when w is clicked or focused; for combos also when an entry is picked."""
        HelpHook(w, fn)
        if isinstance(w, QComboBox):
            if w.lineEdit() is not None:
                HelpHook(w.lineEdit(), fn)
            w.activated.connect(lambda _i: fn())


class WcTablesPage(WcBase):
    """WildCard Tables: list of state tables + editor + full logic listing."""

    def __init__(self, main):
        super().__init__(main)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>WildCard Tables</b>"))
        split = QSplitter()
        v.addWidget(split, 1)

        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.show_table)
        lv.addWidget(self.list)
        bl = QHBoxLayout()
        for text, fn in (("Add", self.add_table), ("Delete", self.del_table), ("Up", lambda: self.move(-1)),
                         ("Down", lambda: self.move(1)), ("Import...", self.import_tables)):
            b = QPushButton(text); b.clicked.connect(fn); bl.addWidget(b)
        lv.addLayout(bl)
        split.addWidget(left)

        right = QWidget(); rv = QVBoxLayout(right)
        g = QGridLayout()
        g.addWidget(QLabel("Description:"), 0, 0)
        self.desc = QLineEdit(); self.desc.setMaxLength(15)
        self.desc.editingFinished.connect(self._desc)
        self._hook(self.desc, lambda: self._help("Description", "wc_state_description_help"))
        g.addWidget(self.desc, 0, 1, 1, 3)
        self.st, self.sv, self.cond = [], [], []
        for k in range(3):
            g.addWidget(QLabel("State %d:" % (k + 1)), 1 + 2 * k, 0)
            c = _combo(STATE_NAMES, search=True); c.currentIndexChanged.connect(lambda _i, k=k: self._state(k))
            e = QLineEdit(); e.setMaximumWidth(70); e.editingFinished.connect(lambda k=k: self._state(k))
            self.st.append(c); self.sv.append(e)
            self._hook(c, lambda k=k: self._help("State %d" % (k + 1), "wc_state_help", self.st[k].currentText()))
            self._hook(e, lambda k=k: self._help("State %d value" % (k + 1), "wc_state_value_help",
                                                 self.st[k].currentText()))
            g.addWidget(c, 1 + 2 * k, 1); g.addWidget(e, 1 + 2 * k, 2)
            if k < 2:
                cc = _combo(COND); cc.currentIndexChanged.connect(lambda _i, k=k: self._cond(k))
                self._hook(cc, lambda k=k: self._help("Condition %d" % (k + 1), "wc_condition_help"))
                self.cond.append(cc)
                g.addWidget(QLabel("Cond:"), 2 + 2 * k, 0); g.addWidget(cc, 2 + 2 * k, 1)
        g.setColumnStretch(4, 1)
        rv.addLayout(g)
        lists = QHBoxLayout()
        self.act = self._items_box("Actions (state TRUE)", lists)
        self.inact = self._items_box("Inactions (state FALSE)", lists)
        rv.addLayout(lists, 1)
        split.addWidget(right)
        split.setSizes([260, 800])

        v.addWidget(QLabel("Logic listing"))
        self.logic = QPlainTextEdit(); self.logic.setReadOnly(True)
        self.logic.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.logic.setMinimumHeight(180)
        v.addWidget(self.logic)

    def _items_box(self, title, lay):
        box = QGroupBox(title); bl = QVBoxLayout(box)
        t = QTableWidget(0, 2); t.setHorizontalHeaderLabels(["Action", "Value"])
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        t.verticalHeader().setVisible(True)
        bl.addWidget(t)
        h = QHBoxLayout()
        for text, d in (("+", 1), ("-", -1)):
            b = QPushButton(text); b.setMaximumWidth(40)
            b.clicked.connect(lambda _=False, t=t, d=d: self._rows(t, d)); h.addWidget(b)
        h.addStretch(1); bl.addLayout(h)
        lay.addWidget(box)
        return t

    # -- refresh -----------------------------------------------------------------------
    def refresh(self):
        self.load_wc()
        self._loading = True
        cur = max(self.list.currentRow(), 0)
        self.list.clear()
        if self.wc:
            for i, t in enumerate(self.wc.tables):
                self.list.addItem("%2d  %s" % (i + 1, t.desc))
        self._loading = False
        if self.wc and self.wc.tables:
            self.list.setCurrentRow(min(cur, len(self.wc.tables) - 1))
            self.show_table(self.list.currentRow())
        self._logic()

    def _logic(self):
        if not self.wc:
            self.logic.setPlainText("No WildCard block in this codeplug.")
            return
        self.logic.setPlainText("\n".join("T%-2d %-16s %s" % (i + 1, t.desc, self.wc.describe(t))
                                          for i, t in enumerate(self.wc.tables)))

    def table(self):
        i = self.list.currentRow()
        return self.wc.tables[i] if self.wc and 0 <= i < len(self.wc.tables) else None

    def show_table(self, i):
        t = self.table()
        if t is None:
            return
        self._loading = True
        self.desc.setText(t.desc)
        for k in range(3):
            s = t.states[k]
            self.st[k].setCurrentText(s[0] if s else "")
            self.sv[k].setText("" if not s or s[1] is None else str(s[1]))
            self.sv[k].setEnabled(bool(s and STATES.get(s[0], {}).get('value')))
        for k in range(2):
            self.cond[k].setCurrentIndex(t.conds[k] if t.conds[k] < 4 else 0)
        for tw, items in ((self.act, t.actions), (self.inact, t.inactions)):
            tw.setRowCount(0)
            for it in items:
                self._add_row(tw, it)
        self._loading = False

    def _add_row(self, tw, it=None):
        r = tw.rowCount(); tw.insertRow(r)
        c = _combo(ACTION_NAMES, it.name if it else "", search=True)
        e = QLineEdit("" if not it or it.value is None else str(it.value))
        c.currentIndexChanged.connect(lambda _i: self._items())
        e.editingFinished.connect(self._items)
        kind = "Action" if tw is self.act else "Inaction"
        self._hook(c, lambda: self._help(kind, "wc_command_help", c.currentText()))
        self._hook(e, lambda: self._help(kind + " value", "wc_command_value_help", c.currentText()))
        tw.setCellWidget(r, 0, c); tw.setCellWidget(r, 1, e)

    # -- edits ---------------------------------------------------------------------------
    def _desc(self):
        t = self.table()
        if t and not self._loading and self.desc.text() != t.desc:
            t.desc = self.desc.text().upper()
            self.list.currentItem().setText("%2d  %s" % (self.list.currentRow() + 1, t.desc))
            self.changed("table description"); self._logic()

    def _state(self, k):
        t = self.table()
        if t is None or self._loading:
            return
        name = self.st[k].currentText()
        if name and name not in STATES:
            return                        # still typing
        if not name:
            t.states[k] = None
        else:
            info = STATES.get(name, {})
            val = None
            if info.get('value'):
                txt = self.sv[k].text().strip() or str(info.get('base', 0))
                try:
                    val = int(txt)
                except ValueError:
                    self.main.statusBar().showMessage("State value must be a number", 5000); return
            t.states[k] = (name, val, None)
            self.sv[k].setEnabled(bool(info.get('value')))
        self.changed("state %d" % (k + 1)); self._logic()

    def _cond(self, k):
        t = self.table()
        if t is None or self._loading:
            return
        t.conds[k] = self.cond[k].currentIndex()
        self.changed("condition %d" % (k + 1)); self._logic()

    def _collect(self, tw):
        out = []
        for r in range(tw.rowCount()):
            name = tw.cellWidget(r, 0).currentText()
            if not name or name not in ACTIONS:
                continue
            txt = tw.cellWidget(r, 1).text().strip()
            a = ACTIONS.get(name, {})
            val = None
            if a.get('value'):
                val = int(txt) if txt else a.get('base', 0)
            out.append(Item(name, val))
        return out

    def _items(self):
        t = self.table()
        if t is None or self._loading:
            return
        try:
            t.actions, t.inactions = self._collect(self.act), self._collect(self.inact)
        except ValueError:
            self.main.statusBar().showMessage("Action values must be numbers", 5000); return
        self.changed("actions"); self._logic()

    def _rows(self, tw, d):
        if self.table() is None:
            return
        if d > 0:
            self._add_row(tw)
        elif tw.rowCount():
            tw.removeRow(tw.currentRow() if tw.currentRow() >= 0 else tw.rowCount() - 1)
            self._items()

    def add_table(self):
        if self.wc is None:
            return
        i = self.list.currentRow() + 1
        self.wc.tables.insert(i, Table("NEW TABLE", actions=[Item("NULL")]))
        self.changed("added table"); self.refresh(); self.list.setCurrentRow(i)

    def del_table(self):
        t = self.table()
        if t is None or len(self.wc.tables) <= 1:
            return
        if dlg.question(self, "Delete table", "Delete table %d '%s'?" % (self.list.currentRow() + 1, t.desc)) \
                != QMessageBox.StandardButton.Yes:
            return
        self.wc.tables.remove(t)
        self.changed("deleted table"); self.refresh()

    def import_tables(self):
        """Merge tables from another .cp's WildCard block into this one (see Wildcard.merge)."""
        if self.wc is None:
            dlg.warning(self, "Import WildCard tables", "This codeplug has no WildCard block.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import WildCard tables from archive", "",
                                              "Codeplug archives (*.cp *.CP);;All files (*)")
        if not path:
            return
        try:
            f = CpFile(path)
        except Exception as e:
            dlg.critical(self, "Import WildCard tables", "%s\n\n%s" % (path, e))
            return
        if not f.wc:
            dlg.warning(self, "Import WildCard tables", "%s has no WildCard block." % os.path.basename(path))
            return
        src = Wildcard(bytearray(f.wc))
        d = WcImportDialog(self, path, self.wc, src, wildcard_oper(self.main.cp))
        if dlg._run(d, self) != QDialog.DialogCode.Accepted:
            return
        picked = d.selected()
        if not picked:
            return
        log = self.wc.merge(src, picked, replace=d.do_replace())
        self.changed("imported %d WildCard table(s)" % len(log))
        self.refresh()
        self.main.statusBar().showMessage("Imported %s"
                                          % ", ".join("%s (%s)" % (n, k) for n, k in log), 8000)

    def move(self, d):
        i = self.list.currentRow(); j = i + d
        if self.wc is None or not (0 <= i < len(self.wc.tables) and 0 <= j < len(self.wc.tables)):
            return
        T = self.wc.tables; T[i], T[j] = T[j], T[i]
        self.changed("moved table"); self.refresh(); self.list.setCurrentRow(j)


class WcIoPage(WcBase):
    """WildCard Input / Output: per-line active level and binary group assignments."""

    GROUPS_IN = ["NONE", "1(INPUTS 1-4)", "2(INPUTS 1-8)", "3(INPUTS 9-10)", "4(INPUTS 9-12)", "5(INPUTS 5-8)"]
    GROUPS_OUT = ["NONE", "1(OUTPUTS 1-2)", "2(OUTPUTS 1-4)", "3(OUTPUTS 5-6)", "4(OUTPUTS 1-6)",
                  "5(OUTPUTS 7-8)", "6(OUTPUTS 7-10)", "7(OUTPUTS 3-6)"]

    def __init__(self, main, outputs):
        super().__init__(main)
        self.outputs = outputs
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>WildCard %s</b>" % ("Output" if outputs else "Input")))
        g = QGridLayout()
        self.roles = [("Channel Select Group", 0x4)] + ([("MCS Group", 0x8)] if outputs else []) + [("Activation Group", 0xC)]
        self.gcombo = {}
        self.grp_help = 'wc_out_grp_help' if outputs else 'wc_input_group'
        for r, (label, role) in enumerate(self.roles):
            g.addWidget(QLabel(label + ":"), r, 0)
            c = _combo(self.GROUPS_OUT if outputs else self.GROUPS_IN)
            c.currentTextChanged.connect(lambda _t, role=role: self._group(role))
            self._hook(c, lambda label=label: self._help(label, self.grp_help))
            self.gcombo[role] = c
            g.addWidget(c, r, 1)
        g.setColumnStretch(2, 1)
        v.addLayout(g)
        n = 10 if outputs else 12
        self.tab = QTableWidget(n, 4)
        self.tab.setHorizontalHeaderLabels(["Aux " + ("Output" if outputs else "Input"), "HW Type", "Active Level", "Group Definition"])
        self.tab.verticalHeader().setVisible(False)
        self.tab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.lvl = []
        for i in range(n):
            self.tab.setCellWidget(i, 0, QLabel(" %d" % (i + 1)))
            hw = ("Transistor" if i < 6 else "Relay") if outputs else ("Transistor Input" if i < 8 else "Optocoupler Input")
            self.tab.setCellWidget(i, 1, QLabel(" " + hw))
            c = _combo(["LO", "HI"]); c.currentTextChanged.connect(lambda _t, i=i: self._level(i))
            self._hook(c, lambda i=i: self._cell_help(i, 2))
            self.lvl.append(c); self.tab.setCellWidget(i, 2, c)
            self.tab.setCellWidget(i, 3, QLabel(""))
        self.tab.currentCellChanged.connect(lambda r, col, _pr, _pc: self._cell_help(r, col))
        v.addWidget(self.tab, 1)

    def _cell_help(self, row, col):
        aux = "Aux %s %d" % ("Output" if self.outputs else "Input", row + 1)
        if col == 2:
            self._help(aux + " active level", "wc_act_level_help")
        elif col == 3:
            self._help(aux + " group", self.grp_help)
        elif hasattr(self.main, 'help'):
            self.main.help.page()

    def words(self):
        return self.wc.outputs if self.outputs else self.wc.inputs

    def refresh(self):
        self.load_wc()
        if self.wc is None:
            return
        self._loading = True
        w = self.words()
        for i, c in enumerate(self.lvl):
            c.setCurrentText("HI" if w[i] & 0x8000 else "LO")
            hi = w[i] >> 8
            role = {0x4: "Channel", 0x8: "MCS", 0xC: "Activation"}.get(hi & 0xF, "")
            self.tab.cellWidget(i, 3).setText((" %s group %d" % (role, (hi >> 4) & 7)) if hi & 0x7F else "")
        for label, role in self.roles:
            grp = sorted({(x >> 12) & 7 for x in w if ((x >> 8) & 0xF) == role and (x >> 12) & 7})
            names = self.GROUPS_OUT if self.outputs else self.GROUPS_IN
            cur = next((n for n in names if grp and n.startswith("%d(" % grp[0])), "NONE")
            self.gcombo[role].setCurrentText(cur)
        self._loading = False

    def _level(self, i):
        if self._loading or self.wc is None:
            return
        w = self.words()
        w[i] = (w[i] & 0x7FFF) | (0x8000 if self.lvl[i].currentText() == "HI" else 0)
        self.changed("aux %d active level" % (i + 1))

    def _group(self, role):
        if self._loading or self.wc is None:
            return
        w = self.words()
        for i in range(len(w)):
            if ((w[i] >> 8) & 0xF) == role:
                w[i] &= 0x80FF
        txt = self.gcombo[role].currentText()
        m = re.match(r'(\d+)\((?:INPUTS|OUTPUTS) (\d+)-(\d+)\)', txt)
        if m:
            g, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            for i in range(a - 1, b):
                if w[i] & 0x7F00:
                    dlg.warning(self, "Group overlap", "Line %d is already in another group." % (i + 1))
                    self.refresh(); return
                w[i] = (w[i] & 0x80FF) | (g << 12) | (role << 8)
        self.changed("group assignment"); self.refresh()
