"""WinRSS help topics and the side pane that shows them.

qrss/data/help.json is WinRSS's xmlfiles/help.xml (from rss.jar, see tools/extract_help.py):
keyword -> text.  Windows point at topics through forms.json (form "help", field "helpTag")
and the service screens' help_tags.  WinRSS shows them in a modal dialog; qrss keeps one
non-modal, scrollable pane beside the current window instead.
"""
import html, json, os, re

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

HELP = json.load(open(os.path.join(os.path.dirname(__file__), 'data', 'help.json'), encoding='utf-8'))

# Help for qrss features that WinRSS has no topic for (help.json is WinRSS' own help.xml, kept as-is).
QRSS_HELP = {
    "qrss_swdl": """Firmware Download (SWDL)

Programs station firmware from a Motorola SWDL package directory over the front- or back-panel
serial port.  qrss runs its own SLIP / IP / FTP stack, so no dial-up networking, network setup or
admin rights are needed.  Do NOT remove power or the serial cable while the station is programming
flash; if a download is interrupted, recover with WinRSS.

Package directory
  The folder holding SWDL.INI (with the FTP command files, BPID and the .cmp firmware images).
  Browse to it and Load, and qrss lists the sections and versions it will send.

SLIP baud rate
  The speed the station's boot loader uses for the SLIP link while firmware is transferred (the
  ASCII console keeps its own speed).  Higher is faster.
  - 9600, 19200, 38400 and 57600 work on stock firmware.
  - 115200 (HAM) needs patched HAM firmware, H026.11.05b or later; stock boot loaders reject it and
    stay at their old rate.
  The SCM clocks its baud-rate generator from 25 MHz, so "115200" is really 111607 baud (-3.1 %);
  qrss sets the PC side to 111607 to match.  WinRSS offers only 9600 and 38400.  38400 is a safe
  default on any station; use 57600 or 115200 to shorten the transfer when the firmware allows it.

Station port
  Front panel (SLIP address 193.0.0.2) or the backplane connector (193.0.0.1) - whichever the serial
  cable is on.
""",
}
HELP.update(QRSS_HELP)


def text(tags):
    """Plain text of the topics that exist, '' if none."""
    return "\n\n".join(HELP[t] for t in tags if t in HELP)


_BULLET = re.compile(r'[-*•]\s|\(?\d{1,2}[.)]\s')     # list item (after the indent)
_COLS = re.compile(r'\S {3,}\S')                           # columns lined up with spaces


def _indent(line):
    return len(line) - len(line.lstrip(' '))


def _hl(s):
    """The looked-up entry: anchor "entry" (scrolled to) and a highlight."""
    return '<a name="entry"></a><span style="background-color:#ffe38a; color:#000">%s</span>' % s


def _blocks(lines):
    """Runs of non-blank lines as [(line index, line), ...]."""
    out, cur = [], []
    for i, l in enumerate(lines + [""]):
        if l.strip():
            cur.append((i, l))
        elif cur:
            out.append(cur)
            cur = []
    return out


def find_entry(topic, name):
    """Line index of `name`'s entry in a list-style topic (the WildCard states and actions):
    its description if it has one, else its row in a '|' table.  A numbered name also finds its
    family's entry, which WinRSS writes as "INPUT XX (1-12)", "TIMER DONE (1-10)",
    "START TIMER(1-10)" or "ABORT TIMER 1-10".  None if it is not there."""
    name = (name or "").strip()
    if not name:
        return None
    lines = topic.split('\n')
    pats = [re.escape(name)]
    m = re.match(r'(.*?)\s*\d+$', name)
    if m and m.group(1):
        pats.append(re.escape(m.group(1)) + r'(?:\s+X{2,}\b|\s*\(\s*\d|\s*\d+\s*-\s*\d+)')
    hits = sorted({i for p in pats for i, l in enumerate(lines) if re.match(r'\s*' + p + r'(?!\w)', l)})
    desc = [i for i in hits if '|' not in lines[i]]
    return (desc or hits or [None])[0]


def to_html(topic, mark=None):
    """WinRSS topics are a title line plus prose hard-wrapped at ~72 columns.  Reflow the prose
    so it fits a narrow pane: a line joins the one above when it has the same indent and is not
    a bulleted or numbered item; an indent becomes a left margin; a block with columns lined
    up by spaces is shown as it is.  Line `mark` is highlighted."""
    out = []
    for n, block in enumerate(_blocks(topic.split('\n'))):
        head = block[0][1].strip()
        if n == 0 and len(head) < 80 and head == head.upper() and any(c.isalpha() for c in head):
            out.append("<h3>%s</h3>" % html.escape(head, quote=False))
            block = block[1:]
            if not block:
                continue
        if sum(1 for _, l in block if _COLS.search(l)) >= 2:
            # a smaller monospace so wide reference tables (e.g. the WildCard states) fit more of the
            # pane; the pane can be dragged wider for the widest ones
            out.append('<pre style="font-size:10px; margin:2px 0">%s</pre>' % "\n".join(
                _hl(html.escape(l, quote=False)) if i == mark
                else html.escape(l, quote=False) for i, l in block))
            continue
        rows = []                                  # [text, holds the marked line, indent]
        for i, l in block:
            if rows and _indent(l) == rows[-1][2] and not _BULLET.match(l.lstrip()):
                rows[-1][0] += " " + l.strip()
                rows[-1][1] |= i == mark
            else:
                rows.append([l.strip(), i == mark, _indent(l)])
        for k, (t, m, ind) in enumerate(rows):
            body = html.escape(t, quote=False)
            out.append('<p style="margin-top:%dpx; margin-bottom:0px; margin-left:%dpx">%s</p>'
                       % (10 if k == 0 else 2, min(ind, 16) * 5, _hl(body) if m else body))
    return "".join(out)


class HelpPane(QWidget):
    """Shows the current window's topic when the window is selected, then the topic of the
    field last clicked or focused; "Window help" goes back to the window's topic."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 0, 0, 0)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.back = QPushButton("Window help")
        self.back.setToolTip("Back to the help for the whole window")
        self.back.clicked.connect(self.page)
        top.addWidget(self.title, 1)
        top.addWidget(self.back)
        v.addLayout(top)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        v.addWidget(self.view, 1)
        self.setMinimumWidth(140)      # low floor so it can be dragged narrow when a page needs the width
        self._page = ("", ())
        self._shown = None
        self.page()

    def set_page(self, label, tags):
        self._page = (label or "", tuple(t for t in tags if t))
        self.page()

    def page(self):
        self.show_topics(*self._page)
        self.back.setEnabled(False)

    def show_field(self, label, tag, entry=None):
        """A field's topic (scrolled to `entry`, e.g. the WildCard state picked); a field
        without a topic shows the window's topic again."""
        if tag in HELP:
            self.show_other(label, (tag,), entry)
        else:
            self.page()

    def show_other(self, label, tags, entry=None):
        self.show_topics(label, tags, entry)
        self.back.setEnabled(tuple(tags) != self._page[1])

    def show_topics(self, label, tags, entry=None):
        tags = tuple(t for t in tags if t in HELP)
        entry = (entry or "").strip() or None
        if (label, tags, entry) == self._shown:
            return
        self._shown = (label, tags, entry)
        mark = find_entry(HELP[tags[0]], entry) if tags and entry else None
        head = "%s: %s" % (label, entry) if entry else (label or "Help")
        self.title.setText("<b>%s</b>" % html.escape(head, quote=False))
        self.view.setHtml("<hr>".join(to_html(HELP[t], mark if n == 0 else None) for n, t in enumerate(tags))
                          if tags else "<p><i>No WinRSS help for this window.</i></p>")
        if mark is not None:
            self.view.scrollToAnchor("entry")
        else:
            self.view.verticalScrollBar().setValue(0)


class HelpHook(QObject):
    """Event filter that calls fn() when the widget is clicked or gets keyboard focus (macOS
    does not give combo boxes focus on a click, so focus alone would miss most fields)."""

    def __init__(self, widget, fn):
        super().__init__(widget)
        self.fn = fn
        widget.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.fn()
        return False
