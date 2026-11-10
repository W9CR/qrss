"""Message boxes and input dialogs centred on the application window.

Qt's static QMessageBox helpers often centre on the screen (notably from closeEvent on
macOS), which is lost on a large monitor.  These wrappers place the dialog over the
centre of the parent's top-level window before running it.
"""
import sys
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QInputDialog, QMessageBox

SB = QMessageBox.StandardButton


def _center(dlg, parent):
    win = parent.window() if parent is not None else None
    if win is None:
        return
    dlg.adjustSize()
    g = dlg.frameGeometry()
    g.moveCenter(win.frameGeometry().center())
    dlg.move(g.topLeft())


class _Recenter(QObject):
    """Re-centre whenever the dialog is shown (the window system places it on show)."""
    def __init__(self, dlg, parent):
        super().__init__(dlg)
        self.parent_w = parent

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Show:
            _center(obj, self.parent_w)
            QTimer.singleShot(0, lambda: _center(obj, self.parent_w))
        return False


def _run(dlg, parent):
    if parent is not None:
        # macOS: a window-modal dialog with a parent is shown as a sheet attached to the
        # window itself, which is where it belongs
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        if sys.platform != 'darwin':
            f = _Recenter(dlg, parent)
            dlg.installEventFilter(f)
    return dlg.exec()


def _box(icon, parent, title, text, buttons, default=None):
    b = QMessageBox(icon, title, text, buttons, parent)
    if default is not None:
        b.setDefaultButton(default)
    _run(b, parent)
    return SB(b.standardButton(b.clickedButton())) if b.clickedButton() else SB.Cancel


def question(parent, title, text, buttons=SB.Yes | SB.No, default=None):
    return _box(QMessageBox.Icon.Question, parent, title, text, buttons, default)


def warning(parent, title, text, buttons=SB.Ok):
    return _box(QMessageBox.Icon.Warning, parent, title, text, buttons)


def critical(parent, title, text, buttons=SB.Ok):
    return _box(QMessageBox.Icon.Critical, parent, title, text, buttons)


def information(parent, title, text, buttons=SB.Ok):
    return _box(QMessageBox.Icon.Information, parent, title, text, buttons)


def about(parent, title, text):
    return _box(QMessageBox.Icon.Information, parent, title, text, SB.Ok)


def get_item(parent, title, label, items, current=0):
    d = QInputDialog(parent)
    d.setWindowTitle(title); d.setLabelText(label)
    d.setComboBoxItems(items); d.setComboBoxEditable(False)
    if items:
        d.setTextValue(items[current])
    ok = _run(d, parent) == QInputDialog.DialogCode.Accepted
    return d.textValue(), ok


def get_text(parent, title, label, text=""):
    d = QInputDialog(parent)
    d.setWindowTitle(title); d.setLabelText(label); d.setTextValue(text)
    ok = _run(d, parent) == QInputDialog.DialogCode.Accepted
    return d.textValue(), ok
