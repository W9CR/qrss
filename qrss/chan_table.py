"""Spreadsheet pages: every channel (or comparator port) as a row, every decoded field as a column."""
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import dlg
from .codeplug import CHASSIS, PORTS_PER_CHASSIS, add_channel, delete_last_channel
from .fields import codec

# per-channel fields, in WinRSS order (global ones such as the time-out timers are left out)
SKIP = {'channel', 'channel_num', 'line_tot', 'local_tot', 'rptr_tot', 'ptt_priority'}
# Port Control: the chassis/port labels are derived from the row, not stored
SKIP_PORT = {'cur_chassis_num', 'port_number', 'port_range_msg'}


class ComboDelegate(QStyledItemDelegate):
    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def createEditor(self, parent, option, index):
        cl = self.page.columns[index.column()][2]
        if not cl:
            return super().createEditor(parent, option, index)
        c = QComboBox(parent)
        c.addItems(cl)
        c.setStyleSheet("QComboBox { combobox-popup: 0; }")
        c.setMaxVisibleItems(20)
        return c

    def setEditorData(self, editor, index):
        if isinstance(editor, QComboBox):
            editor.setCurrentText(index.data())
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText())
        else:
            super().setModelData(editor, model, index)


class TablePage(QWidget):
    """A whole table (channels or ports) as one grid.  Subclasses set FORM/CTX/SKIP and say how
    many rows there are and what to call them."""
    FORM = 'DCHN'
    CTX = 'channel'
    SKIP = SKIP
    TITLE = ""
    HINT = ("double-click to edit, select several cells in a column and use <i>Set Selected</i> "
            "or Ctrl+D (fill down) to change them together.")
    ADD_DELETE = True

    def __init__(self, main, forms):
        super().__init__()
        self.main = main
        meta = forms[self.FORM]
        self.columns = []                    # (field, label, clist)
        self.col_help = []                   # (label, WinRSS helpTag) per column, for the help pane
        for f in meta['fields']:
            n = f.get('fieldName')
            c = codec(self.FORM, n)
            if not n or n in self.SKIP or c is None or c.readonly:
                continue
            label = (f.get('label') or n).rstrip(':')
            self.col_help.append((label, f.get('helpTag')))
            if f.get('unit'):
                label += " (%s)" % f['unit']
            self.columns.append((n, label, c.clist or f.get('clist')))
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>%s</b> — %s" % (self.TITLE, self.HINT)))
        self.tab = QTableWidget(0, len(self.columns))
        self.tab.setHorizontalHeaderLabels([c[1] for c in self.columns])
        self.tab.setItemDelegate(ComboDelegate(self))
        self.tab.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tab.itemChanged.connect(self._edited)
        self.tab.currentCellChanged.connect(lambda _r, c, _pr, _pc: self._help(c))
        v.addWidget(self.tab, 1)
        a = QAction("Fill down", self); a.setShortcut(QKeySequence("Ctrl+D")); a.triggered.connect(self.fill_down)
        self.addAction(a)
        h = QHBoxLayout()
        buttons = [("Set Selected…", self.set_selected), ("Fill Down", self.fill_down)]
        if self.ADD_DELETE:
            buttons += [("Add Channel (copy row)", self.add), ("Delete Last Channel", self.delete)]
        for text, fn in buttons:
            b = QPushButton(text); b.clicked.connect(fn); h.addWidget(b)
        h.addStretch(1)
        self.button_bar = QWidget(); self.button_bar.setLayout(h)
        self._loading = False

    @property
    def cp(self):
        return self.main.cp

    def nrows(self):
        return self.cp.nchan() if self.cp is not None else 0

    def row_label(self, r):
        return str(r + 1)

    def row_name(self, r):
        """What the status bar calls row r ("Channel 3", "Port 2-5")."""
        return "Channel %s" % self.row_label(r)

    def _help(self, c):
        """The selected column's WinRSS topic in the help pane."""
        if 0 <= c < len(self.col_help) and hasattr(self.main, 'help'):
            self.main.help.show_field(*self.col_help[c])

    def refresh(self):
        self._loading = True
        n = self.nrows()
        self.tab.setRowCount(n)
        self.tab.setVerticalHeaderLabels([self.row_label(r) for r in range(n)])
        for r in range(n):
            for c, (name, _l, _cl) in enumerate(self.columns):
                try:
                    val = codec(self.FORM, name).get(self.cp, {self.CTX: r})
                except Exception:
                    val = "?"
                self.tab.setItem(r, c, QTableWidgetItem(val))
        self.tab.resizeColumnsToContents()
        self._loading = False

    def _set(self, r, c, value):
        name = self.columns[c][0]
        cd = codec(self.FORM, name)
        old = cd.get(self.cp, {self.CTX: r})
        if value == old:
            return False
        cd.set(self.cp, {self.CTX: r}, value)
        return True

    def _edited(self, item):
        if self._loading or self.cp is None:
            return
        r, c = item.row(), item.column()
        try:
            if self._set(r, c, item.text()):
                self.main.set_dirty(True, "%s %s = %s" % (self.row_name(r), self.columns[c][1],
                                                           item.text()))
        except Exception as e:
            self.main.statusBar().showMessage("Invalid value: %s" % e, 6000)
        self.refresh()

    def _apply(self, cells, value):
        bad = 0
        for r, c in cells:
            try:
                self._set(r, c, value)
            except Exception:
                bad += 1
        self.main.set_dirty(True, "Set %d cells to %s%s" % (len(cells) - bad, value, (" (%d refused)" % bad) if bad else ""))
        self.refresh()

    def set_selected(self):
        cells = [(i.row(), i.column()) for i in self.tab.selectedIndexes()]
        if not cells or self.cp is None:
            return
        cols = {c for _, c in cells}
        if len(cols) != 1:
            dlg.information(self, "Set selected", "Select cells in one column."); return
        c = cols.pop(); cl = self.columns[c][2]
        cur = self.tab.item(cells[0][0], c).text()
        if cl:
            v, ok = dlg.get_item(self, "Set selected", self.columns[c][1], cl, max(cl.index(cur), 0) if cur in cl else 0)
        else:
            v, ok = dlg.get_text(self, "Set selected", self.columns[c][1], cur)
        if ok:
            self._apply(cells, v)

    def fill_down(self):
        cells = sorted((i.row(), i.column()) for i in self.tab.selectedIndexes())
        if len(cells) < 2 or self.cp is None:
            return
        by_col = {}
        for r, c in cells:
            by_col.setdefault(c, []).append(r)
        for c, rows in by_col.items():
            v = self.tab.item(rows[0], c).text()
            for r in rows[1:]:
                try:
                    self._set(r, c, v)
                except Exception:
                    pass
        self.main.set_dirty(True, "Filled down")
        self.refresh()

    def add(self):
        if self.cp is None:
            return
        rows = sorted({i.row() for i in self.tab.selectedIndexes()})
        n = add_channel(self.cp, rows[0] if rows else None)
        self.main.set_dirty(True, "Added channel %d" % (n + 1))
        self.refresh()

    def delete(self):
        if self.cp is None or self.cp.nchan() <= 1:
            return
        n = self.cp.nchan()
        if dlg.question(self, "Delete channel", "Delete channel %d (the last channel)?" % n) == QMessageBox.StandardButton.Yes:
            delete_last_channel(self.cp)
            self.main.set_dirty(True, "Deleted channel %d" % n)
            self.refresh()


class ChannelTablePage(TablePage):
    TITLE = "Channel Table"


class PortTablePage(TablePage):
    """The comparator's 80 port records (5 chassis x 16 ports) in one grid."""
    FORM = 'DPORTCTL'
    CTX = 'row'
    SKIP = SKIP_PORT
    TITLE = "Port Table"
    ADD_DELETE = False

    def nrows(self):
        return self.cp.nports() if self.cp is not None else 0

    def row_label(self, r):
        chassis = min(r // PORTS_PER_CHASSIS, len(CHASSIS) - 1)
        return "%d-%d" % (chassis + 1, r % PORTS_PER_CHASSIS + 1)

    def row_name(self, r):
        chassis = min(r // PORTS_PER_CHASSIS, len(CHASSIS) - 1)
        return "Port %s %d" % (CHASSIS[chassis], r % PORTS_PER_CHASSIS + 1)
