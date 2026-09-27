"""
The console window (1440 x 900 by default): a toolbar with sources, editable bands,
Review queue, Export and Pause; the confidence chart with question chips; the latest-decision gauge; four counters; the
decision feed with grading; the inspector; a status bar.

The window polls the ingest ring every 50 ms (20 fps), never once per record. The chart redraws its data only when its
question got new points or the view changed. Pause freezes the chart, the gauge and the feed (so rows stay put while
grading); decisions keep being stored and the counters and status bar keep running. Long names are shortened on screen
(full text in a tooltip), so no data can make the window wider than the screen.

Keys: 1 / 2 / 3 grade the selected decision correct / wrong / flagged and move to the next; space pauses; / filters;
E exports; T sets escalate just above the selected decision, so it and everything below it escalate.
"""
import json, math, os, sqlite3, subprocess, sys, threading, time, types, urllib.error, urllib.request
from urllib.parse import quote, urlsplit
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMainWindow, QMenu, QPushButton, QScrollArea, QSpinBox, QTableView,
                               QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget)
from .feed import DIST, MAX_ROWS, DistributionDelegate, FeedModel, TextDelegate, percent
from .inspector import Inspector
from .model import RATE_LIMIT, plain_words
from .storage import _is_hashed

BG, CHROME, PANEL, BORDER, EDGE = "#0b0d10", "#0f1216", "#13161b", "#1e232b", "#2a3140"
TEXT, SOFT, MUTED, DIM = "#e6e8eb", "#c7ccd4", "#8b93a1", "#4a5263"
AMBER, BLUE, RED, GREEN = "#f2a93b", "#5aa9ff", "#ff5c5c", "#3ecf8e"
SANS, MONO = "IBM Plex Sans", "JetBrains Mono"
RANGES = (("last 240", 240), ("1k", 1000), ("5k", 5000), ("all", None))
VISIBLE_CHIPS = 4       # the rest sit behind a '+N' chip
SOURCE_CHIPS = 6         # at most this many source chips are built; as many as fit show, the rest sit behind '+N'
CHIP_WIDTH, SOURCE_WIDTH, PATH_WIDTH = 120, 150, 220   # widest a chip label, a source label or the session path may be
PACKET_WIDTH = 460       # widest the status bar's packet line may be
MIN_SPAN = 10           # the chart always spans at least 10 decisions (or 10 s)
TICK_MS, STATUS_EVERY, FEED_EVERY = 50, 10, 4   # the status bar refreshes twice a second, the feed five times
MESSAGE_S = 6.0         # how long a status bar message stays
FILTER_WAIT_MS = 150    # the feed refilters once typing pauses, not on every key
PRESETS = (("all", "all"), ("review", "review queue"), ("question", "this question"), ("graded", "graded"))
FEED_WIDTHS = (96, 150, 150, 150, None, 56, 50, 92)   # per column; the distribution bar takes what is left
STALE = object()        # "the inspector must redraw", never equal to a decision's key or to None (no decision)
TOOLTIP_NAMES = 20
EXPORTS = (("jsonl", "Decisions as JSONL (full records)", "JSONL (*.jsonl)"), ("csv", "Index and grades as CSV", "CSV (*.csv)"),
           ("bundle", "Share bundle (.zip)", "Zip (*.zip)"))

QSS = f"""
QWidget#root {{ background: {BG}; }}
QWidget#bar {{ background: {CHROME}; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#rule {{ background: {BORDER}; }}
QWidget#head {{ background: transparent; }}
QPushButton#chip, QToolButton#chip {{ font-family: "{MONO}"; font-size: 11px; padding: 3px 8px; border: none; border-radius: 4px; background: {BORDER}; color: {SOFT}; }}
QPushButton#chip:checked, QToolButton#chip:checked {{ background: {AMBER}; color: {BG}; font-weight: 700; }}
QToolButton#chip::menu-indicator {{ image: none; width: 0; }}
QPushButton#range {{ font-family: "{MONO}"; font-size: 11px; padding: 2px 6px; border: none; border-radius: 4px; background: transparent; color: {MUTED}; }}
QPushButton#range:checked {{ background: {BORDER}; color: {SOFT}; }}
QPushButton#tool {{ font-family: "{SANS}"; font-size: 12px; padding: 7px 12px; border: 1px solid {EDGE}; border-radius: 6px; background: {PANEL}; color: {TEXT}; }}
QPushButton#tool:checked {{ border-color: {AMBER}; color: {AMBER}; }}
QPushButton#legend {{ font-family: "{SANS}"; font-size: 12px; padding: 0 2px; border: none; background: transparent; color: {SOFT}; text-align: left; }}
QPushButton#legend:hover {{ color: {TEXT}; }}
QFrame#sourcechip, QFrame#bands {{ border: 1px solid {EDGE}; border-radius: 6px; background: {PANEL}; }}
QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {EDGE}; font-family: "{MONO}"; font-size: 11px; }}
QMenu::item:selected {{ background: {BORDER}; }}
QSpinBox#band {{ font-family: "{MONO}"; font-size: 12px; background: transparent; border: none; border-bottom: 1px dashed {EDGE}; padding: 0 1px; }}
QSpinBox#band:focus {{ border-bottom: 1px solid {AMBER}; }}
QSpinBox#band:disabled {{ border-bottom-color: {BORDER}; }}
QPushButton#tool:disabled {{ color: {DIM}; border-color: {BORDER}; }}
QLineEdit#filter {{ font-family: "{MONO}"; font-size: 11px; background: {CHROME}; color: {TEXT}; border: 1px solid {EDGE}; border-radius: 4px; padding: 2px 6px; }}
QLineEdit#filter:focus {{ border-color: {AMBER}; }}
QTableView#feed {{ background: {PANEL}; color: {TEXT}; border: none; selection-background-color: {BORDER}; selection-color: {TEXT}; outline: 0; }}
QTableView#feed QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; font-family: "{MONO}"; font-size: 11px; padding: 3px 6px; }}
QScrollArea#inspect {{ background: transparent; border: none; }}
QScrollArea#inspect > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: {PANEL}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {EDGE}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""


def api_key():
    """TYPESAFE_API_KEY from this process's environment, else the Windows user's (set after this process started). Only
    ever used for a replay's Authorization header; never stored, logged or shown."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key or sys.platform != "win32": return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: return winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0]
    except OSError:
        return None


def stranded_by(url, proxy):
    """True when url sends calls to this machine at a port where no proxy of this console listens."""
    try:
        parts = urlsplit(url); port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    return parts.hostname in ("127.0.0.1", "localhost") and (proxy is None or proxy.port != port)


def load_fonts():
    """The two typefaces ship with the package (SIL OFL); nothing needs to be installed."""
    for f in (Path(__file__).parent / "fonts").glob("*.ttf"): QFontDatabase.addApplicationFont(str(f))


def short_path(path):
    try: return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError: return path.as_posix()


def band_color(value, bands):
    return RED if value < bands[0] else AMBER if value < bands[1] else GREEN


def money(d):
    return f"${d:,.2f}" if d >= 1 else "< $0.0001" if 0 < d < 0.00005 else f"${d:.4f}"


def font(mono=False, size=13, weight=QFont.Weight.Normal, spacing=0):
    f = QFont(MONO if mono else SANS); f.setPixelSize(size); f.setWeight(weight)
    if spacing: f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + spacing)
    return f


def elided(s, f, width, mode=Qt.TextElideMode.ElideRight):
    return QFontMetrics(f).elidedText(s, mode, width)


def text(s="", size=13, color=TEXT, mono=False, weight=QFont.Weight.Normal, spacing=0, width=None, mode=Qt.TextElideMode.ElideRight):
    """A label. With width, long text is shortened to fit and the full text goes in a tooltip."""
    label = QLabel(); f = font(mono, size, weight, spacing); label.setFont(f)
    label.setStyleSheet(f"color: {color}; background: transparent;")
    set_text(label, s, width, mode)
    return label


def set_text(label, s, width=None, mode=Qt.TextElideMode.ElideRight):
    short = elided(s, label.font(), width, mode) if width else s
    label.setText(short); label.setToolTip(s if short != s else "")


def rule(horizontal=True):
    line = QFrame(); line.setObjectName("rule")
    (line.setFixedHeight if horizontal else line.setFixedWidth)(1)
    return line


def dot(color, size=8):
    d = QLabel(); d.setFixedSize(size, size); d.color = None; paint_dot(d, color)
    return d


def paint_dot(d, color):
    if d.color != color:  # restyling only on a change: setStyleSheet re-polishes the widget every time
        d.setStyleSheet(f"background: {color}; border-radius: {d.width() // 2}px;"); d.color = color


def hbox(*items, spacing=8, margins=(0, 0, 0, 0)):
    box = QHBoxLayout(); box.setSpacing(spacing); box.setContentsMargins(*margins)
    for it in items:
        if it is None: box.addStretch(1)
        elif isinstance(it, QWidget): box.addWidget(it, getattr(it, "stretch", 0))
        else: box.addLayout(it)
    return box


def squeezable(layout):
    """A holder that never asks for more width than it gets: what does not fit is cut, never pushed out."""
    w = QWidget(); w.setLayout(layout)  # no stylesheet here: a plain one would cascade onto the chips inside
    w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred); w.setMinimumWidth(0)
    w.stretch = 1  # it takes the spare width in its row, not the labels beside it
    return w


class FitRow(QWidget):
    """A row of chips that shows only those that fit its width; the rest go behind the 'more' widget at its end, whose
    text label(hidden names) gives. It never asks for width, so the window cannot grow because of it."""
    stretch = 1

    def __init__(self, more, label, gap=8):
        super().__init__(); self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.gap = gap; self.box = hbox(spacing=gap); self.setLayout(self.box)
        self.more, self.label = more, label
        self.chips, self.names, self.hidden = [], [], []

    def set_chips(self, chips, names):
        """chips: the widgets, in order; names: every name, the chips' first."""
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget() and item.widget() is not self.more: item.widget().deleteLater()
        self.chips, self.names = chips, names
        for c in chips: self.box.addWidget(c)
        self.box.addWidget(self.more); self.box.addStretch(1)
        self.fit()

    def fit(self):
        widths = [c.sizeHint().width() for c in self.chips]
        for shown in range(len(self.chips), -1, -1):  # the most chips that fit beside the 'more' widget they leave
            hidden = self.names[shown:]
            if hidden: self.more.setText(self.label(hidden))
            need = sum(widths[:shown]) + self.gap * max(0, shown - 1) + ((self.gap if shown else 0) + self.more.sizeHint().width() if hidden else 0)
            if need <= self.width(): break
        for i, c in enumerate(self.chips): c.setVisible(i < shown)
        self.hidden = hidden
        more = f"\n... and {len(hidden) - TOOLTIP_NAMES:,} more" if len(hidden) > TOOLTIP_NAMES else ""
        self.more.setVisible(bool(hidden)); self.more.setToolTip("\n".join(hidden[:TOOLTIP_NAMES]) + more)

    def resizeEvent(self, event):
        super().resizeEvent(event); self.fit()


class Panel(QFrame):
    """A rounded panel with a 40 px header (title on the left, anything on the right) over its body."""
    def __init__(self, title, *header):
        super().__init__(); self.setObjectName("panel")
        outer = QVBoxLayout(self); outer.setContentsMargins(1, 1, 1, 1); outer.setSpacing(0)
        head = QWidget(); head.setObjectName("head"); head.setFixedHeight(40)
        head.setLayout(hbox(text(title, 12, TEXT, weight=QFont.Weight.DemiBold, spacing=8), *header, spacing=12, margins=(14, 0, 14, 0)))
        outer.addWidget(head); outer.addWidget(rule())
        self.body = QVBoxLayout(); self.body.setContentsMargins(14, 12, 14, 12); self.body.setSpacing(6)
        outer.addLayout(self.body, 1)


class Mark(QWidget):
    """The console's mark: a rising line ending in a dot."""
    def __init__(self):
        super().__init__(); self.setFixedSize(16, 16)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(AMBER), 1.6); pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin); p.setPen(pen)
        path = QPainterPath(QPointF(2, 12)); path.lineTo(6, 6); path.lineTo(9, 9); path.lineTo(14, 3); p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(AMBER)); p.drawEllipse(QPointF(14, 3), 1.2, 1.2)


class Swatch(QWidget):
    """A legend line: solid or dashed, painted so a dash looks like the chart's."""
    def __init__(self, color, dashed=False):
        super().__init__(); self.setFixedSize(16, 8); self.color, self.dashed = color, dashed

    def paintEvent(self, _):
        p = QPainter(self); pen = QPen(QColor(self.color), 2)
        if self.dashed: pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen); p.drawLine(0, 4, 16, 4)


class Gauge(QWidget):
    """The latest decision of the selected question as a needle on an arc of the user's bands, with one line of
    plain words under the number. The needle's band is drawn at full strength, the other two faint."""
    TIP = "Confidence is how concentrated the answer is, not whether it is right."

    def __init__(self):
        super().__init__(); self.setMinimumSize(300, 170); self.setToolTip(self.TIP)
        self.value, self.bands, self.words = None, (40, 70), ""

    def show_value(self, value, bands, words=""):
        self.value, self.bands, self.words = value, bands, words; self.update()

    def number(self): return "--" if self.value is None else f"{percent(self.value)}%"

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, r = self.width() / 2, 150.0, 120.0
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        esc, rev = self.bands
        def point(v, radius):
            a = math.radians(180 - 1.8 * v); return QPointF(cx + radius * math.cos(a), cy - radius * math.sin(a))
        here = None if self.value is None else band_color(self.value, self.bands)
        for lo, hi, color in ((0, esc, RED), (esc, rev, AMBER), (rev, 100, GREEN)):
            c = QColor(color)
            if color != here: c.setAlphaF(0.35)
            pen = QPen(c, 8); pen.setCapStyle(Qt.PenCapStyle.FlatCap); p.setPen(pen)
            p.drawArc(rect, int((180 - 1.8 * lo) * 16), int(-(hi - lo) * 1.8 * 16))
        for v in (esc, rev):
            p.setPen(QPen(QColor(TEXT), 2)); p.drawLine(point(v, r - 8), point(v, r + 8))
            p.setFont(font(True, 11, QFont.Weight.Bold))
            p.drawText(QRectF(point(v, r + 22) - QPointF(15, 8), point(v, r + 22) + QPointF(15, 8)), Qt.AlignmentFlag.AlignCenter, str(v))
        p.setFont(font(True, 11)); p.setPen(QColor(MUTED))
        p.drawText(QRectF(cx - r - 20, cy + 4, 40, 14), Qt.AlignmentFlag.AlignCenter, "0")
        p.drawText(QRectF(cx + r - 20, cy + 4, 40, 14), Qt.AlignmentFlag.AlignCenter, "100")
        p.setFont(font(False, 10)); p.setPen(QColor(DIM))
        p.drawText(QRectF(cx - 60, cy + 4, 120, 14), Qt.AlignmentFlag.AlignCenter, "your bands")  # the colours are the user's bands, not Jev's
        big = font(True, 40, QFont.Weight.Bold)
        if self.value is None:
            p.setFont(big); p.setPen(QColor(DIM)); p.drawText(QRectF(cx - 100, cy - 70, 200, 50), Qt.AlignmentFlag.AlignCenter, self.number())
            return
        pen = QPen(QColor(TEXT), 3); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)  # a short pointer across the arc, no hub
        v = max(0.0, min(100.0, self.value)); p.drawLine(point(v, r - 30), point(v, r + 6))
        p.setFont(big); p.setPen(QColor(here))
        p.drawText(QRectF(cx - 120, cy - 72, 240, 50), Qt.AlignmentFlag.AlignCenter, self.number())
        if not self.words: return
        small = font(False, 11); p.setFont(small)
        room = 2 * (r - 14)  # inside the arc
        words = elided(self.words, small, int(room)); width = QFontMetrics(small).horizontalAdvance(words) + 10
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(PANEL))  # a backing: near 0 or 100 the pointer reaches this line
        p.drawRoundedRect(QRectF(cx - width / 2, cy - 20, width, 16), 3, 3)
        p.setPen(QColor(SOFT)); p.drawText(QRectF(cx - room / 2, cy - 20, room, 16), Qt.AlignmentFlag.AlignCenter, words)


class Counter(QFrame):
    """One of the four counters: a title, a big number, a line of detail."""
    def __init__(self, title, note=""):
        super().__init__(); self.setObjectName("panel")
        box = QVBoxLayout(self); box.setContentsMargins(12, 12, 12, 12); box.setSpacing(2)
        head = hbox(text(title, 11, MUTED, spacing=6), text(note, 11, DIM) if note else None, None, spacing=4)
        self.title = head.itemAt(0).widget()
        self.value, self.detail = text("--", 22, TEXT, mono=True, weight=QFont.Weight.Medium), text("", 11, MUTED, mono=True)
        self.detail.setWordWrap(True); self.color = TEXT
        box.addLayout(head); box.addWidget(self.value); box.addWidget(self.detail)

    def set(self, value, detail, color=TEXT):
        self.value.setText(value); self.detail.setText(detail)
        if color != self.color: self.value.setStyleSheet(f"color: {color}; background: transparent;"); self.color = color


class SeqAxis(pg.AxisItem):
    """The x axis: decision numbers (whole numbers only) or wall-clock time."""
    mode = "index"

    def tickSpacing(self, minVal, maxVal, size):
        return [(max(1.0, spacing), offset) for spacing, offset in super().tickSpacing(minVal, maxVal, size)]

    def tickStrings(self, values, scale, spacing):
        if self.mode == "time": return [time.strftime("%H:%M:%S", time.localtime(v)) for v in values]
        return [f"#{int(v)}" if float(v).is_integer() and v >= 0 else "" for v in values]


class TagAxis(pg.AxisItem):
    """The right axis. Live value tags and the threshold numbers are drawn here, in the axis column,
    so they never cover the newest points; tags that would overlap are pushed apart."""
    def __init__(self):
        super().__init__("right"); self.setWidth(56); self.tags, self.marks = [], []

    def set_tags(self, tags, marks):
        self.tags, self.marks = tags, marks; self.update()

    def paint(self, p, opt, widget):
        super().paint(p, opt, widget)
        view = self.linkedView()
        if view is None: return
        y = lambda v: self.mapFromScene(view.mapViewToScene(QPointF(0, v))).y()
        p.setFont(font(True, 11, QFont.Weight.Bold))
        for v, color in self.marks:
            p.setPen(QColor(color)); p.drawText(QRectF(8, y(v) - 8, 40, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(v))
        placed = []
        for v, label, fill in sorted(self.tags, key=lambda t: -t[0]):
            top = y(v) - 8
            if placed and top < placed[-1] + 17: top = placed[-1] + 17
            placed.append(top)
            w = QFontMetrics(p.font()).horizontalAdvance(label) + 8
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(fill)); p.drawRoundedRect(QRectF(4, top, w, 16), 3, 3)
            p.setPen(QColor(BG)); p.drawText(QRectF(4, top, w, 16), Qt.AlignmentFlag.AlignCenter, label)


class ConsoleWindow(QMainWindow):
    def __init__(self, model, ingest, log, clock=time.monotonic, proxy=None, proxy_problem=None, base_urls=(), inbox=None, demo=False):
        super().__init__()
        self.model, self.ingest, self.log, self.clock, self.started = model, ingest, log, clock, clock()
        self.proxy, self.proxy_problem, self.inbox, self.caught_up_said = proxy, proxy_problem, inbox, False
        self.demo = demo  # tarnlight demo: made-up data, said in the title, the toolbar and the status bar
        self.question, self.chosen_by_user, self.range_n, self.paused, self.frozen_seq = None, False, 240, False, None
        self.second, self.x_mode, self.ticks = "top", "index", 0
        self.chip_names, self.source_names, self.drawn, self.thin = None, None, None, False
        self.feed_seq, self.shown_key, self.message_until = -1, None, 0.0
        self.feed_missed, self.grading, self.boxes_for, self.fitted_for = 0, False, None, None
        self.export_job, self.export_result = None, None
        self.replay_job, self.replay_result = None, None
        self.setWindowTitle("Tarnlight · demo with made-up data" if demo else f"Tarnlight · {log.path.name}")
        root = QWidget(); root.setObjectName("root"); root.setStyleSheet(QSS); self.setCentralWidget(root)
        page = QVBoxLayout(root); page.setContentsMargins(0, 0, 0, 0); page.setSpacing(0)
        page.addWidget(self._toolbar()); page.addWidget(rule())
        stranded = [u for u in base_urls if stranded_by(u, proxy)]
        if stranded:  # calls pointed at this machine fail while no proxy listens where they go
            why = f"the proxy could not start: {proxy_problem}" if proxy is None else f"the proxy listens on port {proxy.port}."
            self.banner = text(f"TYPESAFE_BASE_URL is {stranded[0]}, but {why} TypeSafe calls that use it fail until it runs there.", 13, "#ffb3b3")
            self.banner.setWordWrap(True)
            self.banner.setStyleSheet("color: #ffb3b3; background: #2a1517; border-bottom: 1px solid #5a2326; padding: 10px 16px;")
            page.addWidget(self.banner)
        body = QGridLayout(); body.setContentsMargins(16, 14, 16, 14); body.setHorizontalSpacing(14); body.setVerticalSpacing(14)
        body.addWidget(self._chart_panel(), 0, 0)
        body.addWidget(self._feed_panel(), 1, 0)
        right = QVBoxLayout(); right.setSpacing(14); right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self._gauge_panel()); right.addLayout(self._counters())
        right.addWidget(self._inspector_panel(), 1)
        holder = QWidget(); holder.setFixedWidth(360); holder.setLayout(right)
        body.addWidget(holder, 0, 1, 2, 1)
        body.setRowStretch(0, 11); body.setRowStretch(1, 9); body.setColumnStretch(0, 1)
        page.addLayout(body, 1)
        page.addWidget(rule()); page.addWidget(self._status_bar())
        for key, action in (("1", lambda: self.grade_selected("correct")), ("2", lambda: self.grade_selected("wrong")),
                            ("3", lambda: self.grade_selected("flagged")), ("Space", self.pause.toggle), ("/", self.focus_filter),
                            ("E", self.show_export_menu), ("T", self.escalate_at_selected), ("Escape", self.leave_edit)):
            QShortcut(QKeySequence(key), self, action)  # a focused text box keeps its own keys: typing never grades
        for w in self.findChildren(QPushButton) + self.findChildren(QToolButton) + [self.plot]:
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # clicking a button or the chart leaves the keys with the feed
        self.table.setFocus()
        self.timer = QTimer(self); self.timer.setTimerType(Qt.TimerType.PreciseTimer)  # a coarse timer lands on Windows' 15.6 ms grid
        self.timer.timeout.connect(self.tick); self.timer.start(TICK_MS)
        self.refresh(force=True)

    # ---- building
    def _toolbar(self):
        bar = QWidget(); bar.setObjectName("bar"); bar.setFixedHeight(48)
        self.sources_row = FitRow(text("", 11, MUTED, mono=True), lambda hidden: f"+{len(hidden)}")
        self.esc_box, self.rev_box = QSpinBox(), QSpinBox()
        for box, color, which in ((self.esc_box, RED, "escalate"), (self.rev_box, AMBER, "review")):
            box.setObjectName("band"); box.setRange(0, 100); box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            box.setKeyboardTracking(False)  # typed numbers apply on Enter or leaving the box, not on each digit
            box.setStyleSheet(f"color: {color};"); box.setFixedWidth(34); box.setAlignment(Qt.AlignmentFlag.AlignRight)
            box.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # never the focus by default: the grading keys belong to the feed
            box.editingFinished.connect(lambda: self.table.setFocus())
            box.setToolTip(f"{which.capitalize()} band for the charted question (saved per question name). Or drag its line on the chart.")
            box.valueChanged.connect(lambda v, which=which: self.set_band(which, v))
        self.auto_label = text("auto ≥ 70", 12, GREEN, mono=True)
        bands = QFrame(); bands.setObjectName("bands"); bands.setFixedHeight(30)
        bands.setToolTip("Your bands, set per question name: below escalate goes to the review queue. Jev does not set these.")
        bands.setLayout(hbox(text("your bands", 11, DIM), text("escalate <", 12, RED, mono=True), self.esc_box, text("·", 12, DIM), text("review <", 12, AMBER, mono=True),
                             self.rev_box, text("·", 12, DIM), self.auto_label, spacing=5, margins=(10, 0, 10, 0)))
        self.review_button = QPushButton("Review queue"); self.review_button.setObjectName("tool"); self.review_button.clicked.connect(self.show_review_queue)
        self.review_button.setToolTip(f"Decisions below escalate that are not graded yet, this session.\nThe feed holds the newest {MAX_ROWS:,}.")
        self.export_button = QPushButton("Export"); self.export_button.setObjectName("tool")
        menu = QMenu(self.export_button)
        for kind, label, _ in EXPORTS: menu.addAction(label, lambda kind=kind: self.ask_export(kind))
        self.export_button.setMenu(menu)
        self.pause = QPushButton("Pause"); self.pause.setObjectName("tool"); self.pause.setCheckable(True)
        self.pause.toggled.connect(self.set_paused)
        path = text("demo · made-up data" if self.demo else short_path(self.log.path), 12, AMBER if self.demo else MUTED, mono=True,
                    width=PATH_WIDTH, mode=Qt.TextElideMode.ElideMiddle)
        bar.setLayout(hbox(Mark(), text("TARNLIGHT", 12, TEXT, weight=QFont.Weight.DemiBold, spacing=12), path,
                           text("SOURCES", 11, MUTED, spacing=8), self.sources_row, bands,
                           self.review_button, self.export_button, self.pause, spacing=10, margins=(16, 0, 16, 0)))
        return bar

    def _chart_panel(self):
        self.chip_group = QButtonGroup(self); self.chip_group.setExclusive(False)  # exclusivity is kept by hand: see _refresh_chips
        self.more = QToolButton(); self.more.setObjectName("chip"); self.more.setCheckable(True); self.more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); self.more.setMenu(QMenu(self.more))
        self.more.menu().aboutToShow.connect(self._fill_more_menu)  # built when opened: names can number in the thousands
        self.chip_row = FitRow(self.more, self._more_label, gap=2)
        self.second_button = QPushButton("top prob."); self.second_button.setObjectName("legend")
        self.second_button.setToolTip("Second line: top probability or margin (top minus second). Click to switch.")
        self.second_button.clicked.connect(self.toggle_second)
        self.x_button = QPushButton("x: #"); self.x_button.setObjectName("range")
        self.x_button.setToolTip("x axis: decision number or wall-clock time. Click to switch."); self.x_button.clicked.connect(self.toggle_x)
        self.range_group = QButtonGroup(self); self.range_group.setExclusive(True)
        ranges = hbox(self.x_button, spacing=4)
        for name, n in RANGES:
            b = QPushButton(name); b.setObjectName("range"); b.setCheckable(True); b.setChecked(n == 240)
            b.clicked.connect(lambda _=False, n=n: self.set_range(n)); self.range_group.addButton(b); ranges.addWidget(b)
        legend = hbox(Swatch(AMBER), text("confidence", 12, SOFT), Swatch(BLUE), self.second_button,
                      Swatch(RED, dashed=True), text("escalate", 12, SOFT), spacing=5)
        panel = Panel("CONFIDENCE", self.chip_row, legend, ranges)
        panel.body.setContentsMargins(6, 6, 6, 6)
        self.chart_stack = QStackedWidget()
        self.waiting_hint = text("", 12, MUTED); self.waiting_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chart_stack.addWidget(self._waiting())
        self.x_axis, self.y_axis = SeqAxis("bottom"), TagAxis()
        self.plot = pg.PlotWidget(background=PANEL, axisItems={"bottom": self.x_axis, "right": self.y_axis})
        pi = self.plot.getPlotItem(); pi.hideButtons(); pi.setMenuEnabled(False); pi.setMouseEnabled(x=False, y=False)
        pi.showAxis("right"); pi.hideAxis("left"); pi.setYRange(0, 100, padding=0.02)
        for ax in (self.x_axis, self.y_axis):
            ax.setPen(pg.mkPen(BORDER)); ax.setTextPen(pg.mkPen(MUTED)); ax.setStyle(tickFont=font(True, 11))
        self.y_axis.setTicks([[(v, str(v)) for v in (0, 25, 50, 75, 100)]])
        for v in (25, 50, 75): pi.addItem(pg.InfiniteLine(v, angle=0, pen=pg.mkPen(BORDER)))
        self.zone = pg.LinearRegionItem((0, 40), orientation="horizontal", movable=False, brush=QColor(255, 92, 92, 13), pen=pg.mkPen(None))
        pi.addItem(self.zone)
        self.review_line = pg.InfiniteLine(70, angle=0, movable=True, pen=pg.mkPen(AMBER, width=1, style=Qt.PenStyle.DashLine),
                                           hoverPen=pg.mkPen(AMBER, width=3))
        self.escalate_line = pg.InfiniteLine(40, angle=0, movable=True, pen=pg.mkPen(RED, width=1.5, style=Qt.PenStyle.DashLine),
                                             hoverPen=pg.mkPen(RED, width=3))
        for line, which in ((self.escalate_line, "escalate"), (self.review_line, "review")):
            line.setToolTip(f"Drag to set {which} for this question")
            line.sigPositionChanged.connect(lambda l: self.zone.setRegion((0, self.escalate_line.value())))  # live while dragging
            line.sigPositionChangeFinished.connect(lambda l, which=which: self.set_band(which, round(l.value())))
            pi.addItem(line)
        # The tint under the confidence line is drawn from a smoothed copy: filling under every zig-zag of 20k jagged
        # points cost ~24 ms a frame, and at 8% opacity the smoothed tint looks the same. The lines stay exact
        # (peak downsampling keeps every high and low, about 2 points per pixel).
        self.conf_fill = pi.plot(pen=None, fillLevel=0, brush=QColor(242, 169, 59, 20))
        self.second_curve = pi.plot(pen=pg.mkPen(BLUE, width=1.6), symbolPen=None, symbolBrush=BLUE)
        self.conf_curve = pi.plot(pen=pg.mkPen(AMBER, width=2.2), symbolPen=None, symbolBrush=AMBER)
        for c, method, per_pixel in ((self.conf_fill, "mean", 0.2), (self.second_curve, "peak", 1.0), (self.conf_curve, "peak", 1.0)):
            c.setClipToView(True); c.setDownsampling(auto=True, method=method); c.opts["autoDownsampleFactor"] = per_pixel
        self.plot.scene().sigMouseClicked.connect(self._chart_clicked)
        self.chart_stack.addWidget(self.plot)
        panel.body.addWidget(self.chart_stack, 1)
        return panel

    def _feed_panel(self):
        self.preset_group = QButtonGroup(self); self.preset_group.setExclusive(True)
        presets = hbox(spacing=2)
        for key, label in PRESETS:
            b = QPushButton(label); b.setObjectName("range"); b.setCheckable(True); b.setChecked(key == "all"); b.preset = key
            b.clicked.connect(lambda _=False, key=key: self.set_preset(key)); self.preset_group.addButton(b); presets.addWidget(b)
        presets.addStretch(1)
        self.filter_box = QLineEdit(); self.filter_box.setObjectName("filter"); self.filter_box.setFixedWidth(220)
        self.filter_box.setPlaceholderText("words, conf<40, ms>200")
        self.filter_box.setToolTip("Filter the feed: words match the question, source or answer;\nconf<40, conf>=70, ms>200 compare numbers. / to jump here, Escape to leave.")
        self.filter_wait = QTimer(self); self.filter_wait.setSingleShot(True); self.filter_wait.setInterval(FILTER_WAIT_MS)
        self.filter_wait.timeout.connect(lambda: self._refilter(text=self.filter_box.text()))
        self.filter_box.textChanged.connect(lambda _: self.filter_wait.start())
        self.filter_box.returnPressed.connect(lambda: (self.filter_wait.stop(), self._refilter(text=self.filter_box.text()), self.table.setFocus()))
        panel = Panel("DECISIONS", squeezable(presets), self.filter_box, text("newest first", 11, MUTED, mono=True))
        panel.body.setContentsMargins(0, 0, 0, 0)
        self.feed = FeedModel(self.model.bands, self.log.outcomes())
        self.table = QTableView(); self.table.setObjectName("feed"); self.table.setModel(self.feed)
        self.table.setItemDelegate(TextDelegate(self.table)); self.table.setItemDelegateForColumn(DIST, DistributionDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide(); self.table.verticalHeader().setDefaultSectionSize(24); self.table.setShowGrid(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader(); header.setHighlightSections(False)
        for c, w in enumerate(FEED_WIDTHS):
            if w is None: header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
            else: header.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed); header.resizeSection(c, w)
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _: self._show_selected())
        panel.body.addWidget(self.table, 1)
        return panel

    def _inspector_panel(self):
        self.inspect_where = text("", 11, MUTED, mono=True)
        panel = Panel("INSPECTOR", None, self.inspect_where)
        self.inspector = Inspector(privacy=self.log.privacy); self.inspector.box.setContentsMargins(0, 0, 12, 0)  # room for the scrollbar
        if self.proxy is not None: self.inspector.on_replay = self.replay_record
        scroll = QScrollArea(); scroll.setObjectName("inspect"); scroll.setWidgetResizable(True); scroll.setWidget(self.inspector)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel.body.addWidget(scroll, 1)
        return panel

    def _waiting(self):
        w = QWidget(); box = QVBoxLayout(w); box.addStretch(1)
        where = " · ".join(x for x in ("drop box ~/.tarnlight/inbox", f"proxy 127.0.0.1:{self.proxy.port}" if self.proxy else "",
                                         f"UDP 127.0.0.1:{self.ingest.port}") if x)
        for label in (text("Waiting for Jev decisions", 16, TEXT),
                      text("Decisions from your apps appear here once they are connected. The README says how.", 12, MUTED, mono=False),
                      self.waiting_hint, text(f"For programmers: {where}", 11, DIM, mono=True)):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter); label.setWordWrap(True); box.addWidget(label)  # never sets the window's width
        self.demo_button = QPushButton("Try the demo (made-up data)"); self.demo_button.setObjectName("tool")
        self.demo_button.setToolTip("Opens a second window that plays made-up Jev decisions. Nothing is sent to TypeSafe and nothing is kept.")
        self.demo_button.clicked.connect(self.open_demo); self.demo_button.setVisible(not self.demo)
        box.addSpacing(8); box.addWidget(self.demo_button, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addStretch(1)
        return w

    def open_demo(self):
        """The demo in its own process and window, so its made-up data never mixes with this session."""
        args = [sys.executable, "demo"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "tarnlight", "demo"]
        try:
            subprocess.Popen(args, close_fds=True)
        except OSError as e:
            self.say(f"The demo could not start: {type(e).__name__}"); return
        self.say("The demo opens in a second window.")

    def _gauge_panel(self):
        self.gauge_where = text("", 11, MUTED, mono=True)
        panel = Panel("CONFIDENCE · LATEST", None, self.gauge_where)
        self.gauge = Gauge(); panel.body.addWidget(self.gauge, 0, Qt.AlignmentFlag.AlignHCenter)
        self.streak = text("", 12, "#ffb3b3"); self.streak.setTextFormat(Qt.TextFormat.RichText)
        self.streak.setStyleSheet("color: #ffb3b3; background: #2a1517; border: 1px solid #5a2326; border-radius: 6px; padding: 8px 10px;")
        keep = self.streak.sizePolicy(); keep.setRetainSizeWhenHidden(True); self.streak.setSizePolicy(keep)  # no jumping layout
        panel.body.addWidget(self.streak)
        return panel

    def _counters(self):
        grid = QGridLayout(); grid.setSpacing(10)
        self.c_rate, self.c_latency = Counter("REQUESTS / MIN"), Counter("LATENCY", "(measured)")
        self.c_cost, self.c_below = Counter("COST", "(est. from tokens)"), Counter("BELOW ESCALATE")
        for i, c in enumerate((self.c_rate, self.c_latency, self.c_cost, self.c_below)): grid.addWidget(c, i // 2, i % 2)
        return grid

    def _status_bar(self):
        bar = QWidget(); bar.setObjectName("bar"); bar.setFixedHeight(28)
        self.s_ingest_dot, self.s_ingest = dot(GREEN, 7), text(f"ingest :{self.ingest.port}", 11, MUTED, mono=True)
        self.s_proxy_dot = dot(GREEN if self.proxy else RED, 7)
        self.s_proxy = text(f"proxy :{self.proxy.port}" if self.proxy else "proxy off", 11, MUTED, mono=True)
        self.s_proxy.setToolTip(f"TypeSafe calls sent to http://127.0.0.1:{self.proxy.port} are forwarded and recorded" if self.proxy
                                else f"The proxy could not start: {self.proxy_problem}" if self.proxy_problem else "No proxy in this window")
        self.s_packet, self.s_log, self.s_up = (text("", 11, MUTED, mono=True) for _ in range(3))
        self.s_message = text("", 11, AMBER, mono=True)
        demo = [text("DEMO · made-up data", 11, AMBER, mono=True)] if self.demo else []
        for label in demo: label.setToolTip("Not real Jev calls. Nothing is kept: the session and its bands are deleted when the window closes.")
        bar.setLayout(hbox(hbox(self.s_ingest_dot, self.s_ingest, spacing=6), hbox(self.s_proxy_dot, self.s_proxy, spacing=6), self.s_packet, squeezable(hbox(self.s_message)), *demo, self.s_log,
                           text("api key never stored", 11, MUTED, mono=True), self.s_up, spacing=18, margins=(16, 0, 16, 0)))
        return bar

    # ---- behaviour
    def tick(self):
        new = self.ingest.since(self.model.last_seq)
        touched = self.model.add(new) if new else set()
        self.ticks += 1
        if not self.paused and self.ticks % FEED_EVERY == 0: self.feed_catch_up()
        if self.isMinimized(): return  # nothing on screen: the model keeps up, the drawing waits for changeEvent
        self.refresh(touched=touched, grew=bool(new))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and not self.isMinimized(): self.refresh(force=True)

    def feed_catch_up(self):
        """Add what arrived since the feed's newest row. Five times a second: rows scrolling faster cannot be read, and each
        update repaints the table. With a row selected or the table scrolled down, the rows on screen stay where they are."""
        records = self.ingest.since(self.feed_seq)
        if not records: return
        if self.feed_seq >= 0 and records[0]["seq"] > self.feed_seq + 1:  # paused longer than the ring holds: say so
            self.feed_missed += records[0]["seq"] - self.feed_seq - 1
        bar, chosen = self.table.verticalScrollBar(), self.selected_row()
        anchor = bar.value() > 0 or chosen is not None
        added = self.feed.add(records); self.feed_seq = max(self.feed_seq, records[-1]["seq"])
        now = self.selected_row()
        if chosen is not None and (now is None or (now.seq, now.question) != (chosen.seq, chosen.question)):
            self.select_row(-1)  # the selected row was trimmed away: never leave a grade key on the row Qt picked instead
            self.say(f"The selected decision left the feed, which holds the newest {MAX_ROWS:,}.")
        elif anchor and added:
            self.table.updateGeometries()  # the scroll range grows first, or the new value is cut at the old maximum
            bar.setValue(bar.value() + added)  # the table scrolls per row

    def set_paused(self, paused):
        self.paused = paused; self.pause.setText("Resume" if paused else "Pause")
        self.feed_catch_up()  # pausing: the feed shows what the chart shows; resuming: what arrived meanwhile
        self.frozen_seq = self.model.last_seq if paused else None
        self.refresh(force=True)

    def set_question(self, q, by_user=True):
        self.question, self.chosen_by_user = q, self.chosen_by_user or by_user
        self.refresh(force=True)

    def set_range(self, n):
        self.range_n = n; self.refresh(force=True)

    def toggle_second(self):
        self.second = "margin" if self.second == "top" else "top"; self.refresh(force=True)

    def toggle_x(self):
        self.x_mode = "time" if self.x_mode == "index" else "index"
        self.x_axis.mode = self.x_mode; self.x_button.setText("x: time" if self.x_mode == "time" else "x: #")
        self.refresh(force=True)

    def refresh(self, touched=(), grew=False, force=False):
        """Everything the window shows. While paused only the chart and gauge stand still."""
        m = self.model
        editing = self.escalate_line.moving or self.review_line.moving or self.esc_box.hasFocus() or self.rev_box.hasFocus()
        if self.question not in m.series or not (self.chosen_by_user or editing):  # no switching under a band being edited
            self.question = m.default_question(self.question)
        if self.feed.preset == "question" and self.question and self.feed.question != self.question: self._refilter(question=self.question)
        self._refresh_sources(); self._refresh_chips()
        bands = m.bands[self.question] if self.question else (40, 70)
        esc, rev = bands
        for box, v in ((self.esc_box, esc), (self.rev_box, rev)):  # a box being typed in keeps its text, unless the question changed
            if box.value() != v and (not box.hasFocus() or self.boxes_for != self.question): box.blockSignals(True); box.setValue(v); box.blockSignals(False)
        self.boxes_for = self.question
        for box in (self.esc_box, self.rev_box): box.setEnabled(self.question is not None)
        if self.auto_label.text() != f"auto ≥ {rev}": self.auto_label.setText(f"auto ≥ {rev}")
        noul = self.question is not None and (m.latest.get(self.question, (0, {}))[1] or {}).get("type") == "noul"
        self.second_button.setText("margin" if self.second == "margin" else "p(yes)" if noul else "top prob.")
        self.chart_stack.setCurrentIndex(1 if self.question else 0)
        if not self.question and m.calls:
            self.waiting_hint.setText(f"{m.calls:,} call{'s' if m.calls != 1 else ''} so far, none with an answer (last status {m.last_status}).")
        if self.question and (not self.paused or force):
            if force or self.question in touched or self.drawn != (self.question, self.range_n, self.second, self.x_mode):
                self._draw_chart(bands)
            elif grew and self.x_mode == "index":
                self._set_x_range()  # "now" moved on: keep the right edge at the newest decision
            self._refresh_gauge(bands)
        elif not self.question:
            self._refresh_gauge(bands)
        self._refresh_counters()
        if self.export_job is not None and not self.export_job.is_alive():
            self.export_job = None; self.say(self.export_result)
        if self.replay_job is not None and not self.replay_job.is_alive():
            self.replay_job = None; self.say(self.replay_result)
        if self.inbox is not None and not self.caught_up_said and (done := self.inbox.caught_up()) is not None:
            self.caught_up_said, (n, seq) = True, done  # said once all of it is stored
            if n:
                self.feed.caught_up_seq = seq; self.table.viewport().update()
                self.say(f"Caught up: {n:,} call{'s' if n != 1 else ''} from while Tarnlight was closed (below the blue line).")
        if self.message_until and time.monotonic() > self.message_until: self.s_message.setText(""); self.message_until = 0.0
        if force or self.ticks % STATUS_EVERY == 0: self._refresh_status()

    def _refresh_sources(self):
        rows = sorted(self.model.sources(), key=lambda r: not r[1])  # live senders first; a quiet one drops behind '+N'
        names = [r[0] for r in rows]
        if names != self.source_names:
            self.source_widgets, chips = {}, []
            for label, _, _ in rows[:SOURCE_CHIPS]:
                chip = QFrame(); chip.setObjectName("sourcechip"); chip.setFixedHeight(30)
                d, rate = dot(GREEN), text("0.0/s", 11, MUTED, mono=True)
                chip.setLayout(hbox(d, text(label, 12, TEXT, mono=True, width=SOURCE_WIDTH, mode=Qt.TextElideMode.ElideMiddle), rate, margins=(10, 0, 10, 0)))
                chips.append(chip); self.source_widgets[label] = (d, rate)
            self.sources_row.set_chips(chips, names)
            self.source_names = names
        for label, live, per_s in rows[:SOURCE_CHIPS]:
            d, rate = self.source_widgets[label]
            paint_dot(d, GREEN if live else DIM)
            if rate.text() != f"{per_s:.1f}/s": rate.setText(f"{per_s:.1f}/s")

    def _refresh_chips(self):
        rebuild = self.chip_names is None or self.model.series.keys() != self.chip_set  # only when the set of names changes
        if rebuild:
            names = self.model.questions()
            for b in self.chip_group.buttons(): self.chip_group.removeButton(b)
            chip_font, chips = font(True, 11, QFont.Weight.Bold), []
            for q in names[:VISIBLE_CHIPS]:
                b = QPushButton(elided(q, chip_font, CHIP_WIDTH)); b.setObjectName("chip"); b.setCheckable(True); b.question = q
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the keys stay with the feed
                if b.text() != q: b.setToolTip(q)
                b.clicked.connect(lambda _=False, q=q: self.set_question(q)); self.chip_group.addButton(b); chips.append(b)
            self.chip_row.set_chips(chips, names)
            self.chip_names, self.chip_set, self.fitted_for = names, set(names), self.question
        elif self.fitted_for != self.question:  # the 'more' chip names the charted question when it is hidden
            self.chip_row.fit(); self.fitted_for = self.question
        for b in self.chip_group.buttons():
            b.setChecked(b.question == self.question)
        self.more.setChecked(self.question in self.chip_row.hidden)

    def _more_label(self, hidden):
        label = f"+{len(hidden)} · {self.question}" if self.question in hidden else f"+{len(hidden)} more"
        return elided(label, font(True, 11, QFont.Weight.Bold), CHIP_WIDTH + 30)

    def _fill_more_menu(self):
        menu = self.more.menu(); menu.clear()
        for q in self.chip_row.hidden: menu.addAction(f"{q}  ({self.model.question_source.get(q, '')})", lambda q=q: self.set_question(q))

    def _draw_chart(self, bands):
        seq, ts, conf, second, margin = self.model.series[self.question].last(self.range_n, upto=self.frozen_seq)
        if len(seq) == 0: return
        x = (ts if self.x_mode == "time" else seq).copy()  # copies: the model trims its arrays in place
        y2 = (margin if self.second == "margin" else second).copy(); y = conf.copy()
        symbol = "o" if len(x) <= 2 else None  # one or two points draw no line: show them as dots
        for curve, values in ((self.conf_fill, y), (self.conf_curve, y), (self.second_curve, y2)):
            curve.setData(x, values, skipFiniteCheck=True)
        thin = len(x) > self.plot.width()  # more points than pixels: 1 px lines look the same and paint about 3x faster
        if thin != self.thin:
            self.conf_curve.setPen(pg.mkPen(AMBER, width=1 if thin else 2.2)); self.second_curve.setPen(pg.mkPen(BLUE, width=1 if thin else 1.6))
            self.thin = thin
        if self.conf_curve.opts["symbol"] != symbol:  # setSymbol rebuilds the curve's items: only on a change
            for curve in (self.conf_curve, self.second_curve): curve.setSymbol(symbol); curve.setSymbolSize(6)
        esc, rev = bands
        if not self.escalate_line.moving: self.zone.setRegion((0, esc)); self.escalate_line.setValue(esc)  # never under a dragging hand
        if not self.review_line.moving: self.review_line.setValue(rev)
        self.y_axis.set_tags([(float(y[-1]), f"{y[-1]:.1f}", band_color(y[-1], bands)), (float(y2[-1]), f"{y2[-1]:.1f}", BLUE)],
                             [(esc, RED), (rev, AMBER)])
        self.drawn = (self.question, self.range_n, self.second, self.x_mode)
        self.x_first = float(x[0])
        self._set_x_range()

    def _set_x_range(self):
        """Right edge = now: the newest decision of any question, so a quiet question stops short of the edge."""
        if self.x_mode == "time":
            _, ts, *_ = self.model.series[self.question].last(1, upto=self.frozen_seq)
            now = max(float(ts[-1]), self.x_first) if len(ts) else self.x_first
        else:
            now = float(self.frozen_seq if self.frozen_seq is not None else self.model.last_seq)
        self.plot.getPlotItem().setXRange(min(self.x_first, now - MIN_SPAN), now, padding=0.01)

    def _refresh_gauge(self, bands):
        q = self.question
        if not q:
            self.gauge.show_value(None, bands); set_text(self.gauge_where, ""); self.streak.hide(); return
        seq, _, conf, _, _ = self.model.series[q].last(1, upto=self.frozen_seq)
        if not len(seq): return
        latest_seq, answer = self.model.latest[q]
        self.gauge.show_value(float(conf[-1]), bands, plain_words(answer) if latest_seq == int(seq[-1]) else "")
        where = f" · #{int(seq[-1])}"
        set_text(self.gauge_where, elided(q, self.gauge_where.font(), 175 - QFontMetrics(self.gauge_where.font()).horizontalAdvance(where),
                                          Qt.TextElideMode.ElideMiddle) + where)
        self.gauge_where.setToolTip(q + where)
        n = self.model.streak(q)
        self.streak.setVisible(n > 0)
        if n: self.streak.setText(f'Below escalate <span style="font-family:\'{MONO}\'; font-weight:700">{n}</span> '
                                  f'decision{"s" if n != 1 else ""} in a row')

    def _refresh_counters(self):
        m = self.model
        rpm = m.requests_per_min()
        self.c_rate.set(f'{rpm:,} <span style="font-size:13px; color:{MUTED}">/ {RATE_LIMIT:,}</span>',
                        f"{100 * rpm / RATE_LIMIT:.0f}% of limit · {m.rate_limited}×429")
        lat = m.latency_stats()
        self.c_latency.set(f"{lat[0]:.0f} ms" if lat else "--", f"p95 {lat[1]:.0f} ms · last {len(m.latency)}" if lat else "no calls yet")
        spent, per_hour = m.cost()
        self.c_cost.set(money(spent), f"≈ {money(per_hour)} per hour")
        below, unreviewed = m.below()
        share = below / m.decisions if m.decisions else 0.0
        self.c_below.set(f"{100 * share:.1f} %", f"{below:,} of {m.decisions:,}", RED if below else TEXT)
        label = f"Review queue  {unreviewed:,}" if unreviewed else "Review queue"
        if self.review_button.text() != label: self.review_button.setText(label)

    def _refresh_status(self):
        paint_dot(self.s_ingest_dot, GREEN if self.ingest.listener.is_alive() else RED)
        paint_dot(self.s_proxy_dot, GREEN if self.proxy and self.proxy.thread.is_alive() else RED)
        age = self.clock() - self.model.last_arrival if self.model.last_arrival else None
        notes = [f"{self.model.missed:,} not counted (the window fell behind)" if self.model.missed else "",
                 f"{self.feed_missed:,} not in the feed (paused longer than the live buffer holds)" if self.feed_missed else "",
                 f"{self.ingest.counts['rejected']:,} refused (see the rejects file)" if self.ingest.counts["rejected"] else "",
                 f"{self.proxy.counts['not recorded']:,} Jev calls forwarded but not recorded" if self.proxy and self.proxy.counts["not recorded"] else "",
                 "" if self.inbox is None else "drop box off (tarnlight install)" if not self.inbox.folder.is_dir()
                 else "catching up from the drop box" if self.inbox.counts["waiting bytes"] else ""]
        set_text(self.s_packet, " · ".join([f"last packet {age:.1f} s ago" if age is not None else "no packets yet"] + [n for n in notes if n]),
                 PACKET_WIDTH)  # the notes can be long: shortened, the full line in a tooltip
        stats = []
        for p in (self.log.path, Path(f"{self.log.path}-wal"), self.log.hot):  # committed data sits in -wal until a checkpoint
            try: stats.append(os.stat(p))
            except OSError: pass
        wrote = max(0.0, time.time() - max((st.st_mtime for st in stats), default=time.time()))
        self.s_log.setText(f"log {sum(st.st_size for st in stats) / 1e6:.1f} MB · last write {wrote:.0f} s ago")
        up = int(self.clock() - self.started)
        self.s_up.setText(f"up {up // 3600}:{up // 60 % 60:02d}:{up % 60:02d}" if up >= 3600 else f"up {up // 60}:{up % 60:02d}")

    # ---- feed, grading, bands, export
    def say(self, message):
        """A short message in the status bar, gone after MESSAGE_S."""
        set_text(self.s_message, message, 520); self.message_until = time.monotonic() + MESSAGE_S

    def selected_row(self):
        i = self.table.currentIndex()
        return self.feed.row_at(i.row()) if i.isValid() else None

    def select_row(self, i):
        if 0 <= i < self.feed.rowCount():
            self.table.selectRow(i); self.table.scrollTo(self.feed.index(i, 0))
        else:
            self.table.clearSelection(); self.table.setCurrentIndex(self.feed.index(-1, -1))

    def _show_selected(self):
        """Show the selected decision in the inspector: the full record from the log and the sender's previous call."""
        if self.grading: return  # a graded row leaving the view moves Qt's selection: the inspector is drawn once, after
        row = self.selected_row()
        key = (row.seq, row.question) if row else None
        if key == self.shown_key: return  # rows arriving above move the index, not the selection
        if row is None:
            self.inspector.show_decision(None, None); set_text(self.inspect_where, "")
        else:
            rec = self.log.record(row.seq)
            prev = self.log.previous_seq(row.seq, rec.get("source"), rec.get("project")) if rec else None
            self.inspector.show_decision(rec, row.question, previous=self.log.record(prev) if prev is not None else None,
                                         bands=None if row.conf is None else self.model.bands[row.question])
            set_text(self.inspect_where, elided(f"{row.question} · #{row.seq}", self.inspect_where.font(), 200, Qt.TextElideMode.ElideMiddle))
        self.shown_key = key  # only once shown: a failure is retried on the next selection

    def _refilter(self, **how):
        """Change the feed filter and keep the selected decision selected when it still matches."""
        row = self.selected_row()
        self.feed.set_filter(**how); self.shown_key = STALE
        i = self.feed.index_of(row) if row else None
        self.select_row(i if i is not None else -1)
        self._show_selected()

    def set_preset(self, preset):
        for b in self.preset_group.buttons(): b.setChecked(b.preset == preset)
        self._refilter(preset=preset, question=self.question if preset == "question" else None)

    def show_review_queue(self):
        self.set_preset("review"); self.select_row(0); self.table.setFocus(); self._say_if_queue_left_behind()

    def _say_if_queue_left_behind(self):
        """The count is for the whole session; the feed holds only the newest decisions. Say so when that is the gap."""
        left = self.model.below()[1]
        if self.feed.preset == "review" and not self.feed.rowCount() and left:
            self.say(f"{left:,} more below escalate are older than the feed holds (the newest {MAX_ROWS:,} decisions).")

    def leave_edit(self):
        """Escape: cancel a band being typed and give the keys back to the feed."""
        if self.question is not None:
            for box, v in zip((self.esc_box, self.rev_box), self.model.bands[self.question]):
                if box.hasFocus(): box.blockSignals(True); box.setValue(v); box.blockSignals(False)  # also resets the typed text
        self.table.setFocus()

    def focus_filter(self):
        self.filter_box.setFocus(); self.filter_box.selectAll()

    def select_decision(self, seq, question):
        """Select one decision in the feed, clearing the filter if it hides it."""
        target = types.SimpleNamespace(seq=seq, question=question)
        i = self.feed.index_of(target)
        if i is None and (self.feed.preset != "all" or self.feed.text):
            self.filter_box.blockSignals(True); self.filter_box.clear(); self.filter_box.blockSignals(False)
            for b in self.preset_group.buttons(): b.setChecked(b.preset == "all")
            self.feed.set_filter(preset="all", text=""); i = self.feed.index_of(target)
        if i is None: self.say(f"#{seq} is not in the feed (older than it holds, or it arrived while paused)."); return
        self.select_row(i)

    def _chart_clicked(self, ev):
        """Click a point on the chart: select that decision in the feed."""
        vb = self.plot.getPlotItem().vb
        if not self.question or not vb.sceneBoundingRect().contains(ev.scenePos()): return
        seq, ts, *_ = self.model.series[self.question].last(self.range_n, upto=self.frozen_seq)
        if not len(seq): return
        x = vb.mapSceneToView(ev.scenePos()).x()
        i = int(np.argmin(np.abs((ts if self.x_mode == "time" else seq) - x)))
        self.select_decision(int(seq[i]), self.question)

    def grade_selected(self, outcome):
        """Grade the selected decision (saved in the log at once) and move on to the next one."""
        row, i = self.selected_row(), self.table.currentIndex().row()
        if row is None: self.say("Select a decision in the feed first."); return
        if row.conf is None: self.say("A failed call has no answer to grade."); return
        try:
            self.log.grade(row.seq, row.question, outcome)
        except KeyError:
            self.say(f"#{row.seq} {row.question} is not in the index, so it cannot be graded."); return
        except sqlite3.Error as e:
            self.say(f"Grade not saved: {e}"); return
        self.grading = True
        try: self.feed.set_outcome(row, outcome)
        finally: self.grading = False
        self.model.graded[(row.seq, row.question)] = round(row.conf * 10)  # the index's per-mille (a row's conf is it / 10)
        self.shown_key = STALE
        self.select_row(i if self.feed.index_of(row) is None else i + 1)  # a row that left the view: the next one took its place
        self._show_selected(); self.refresh(); self._say_if_queue_left_behind()

    def set_band(self, which, value):
        """Set escalate or review for the charted question; the other moves too if needed to keep escalate <= review."""
        if self.question is None: return
        self.set_bands_for(self.question, *self._ordered(self.model.bands[self.question], which, value))

    @staticmethod
    def _ordered(bands, which, value):
        esc, rev = bands; value = max(0, min(100, int(value)))
        return (value, max(rev, value)) if which == "escalate" else (min(esc, value), value)

    def set_bands_for(self, question, esc, rev):
        self.model.bands.set(question, esc, rev)  # saved at once, per question name
        if self.feed.preset == "review": self._refilter()
        elif self.feed.rowCount(): self.feed.dataChanged.emit(self.feed.index(0, 5), self.feed.index(self.feed.rowCount() - 1, 5))
        self.refresh(force=True)

    def escalate_at_selected(self):
        """T: escalate just above the selected decision, so it and everything below it escalate."""
        row = self.selected_row()
        if row is None: self.say("Select a decision in the feed first."); return
        if row.conf is None: self.say("A failed call has no confidence to escalate at."); return
        if row.conf >= 100: self.say("A 100 % decision cannot be escalated: nothing is above it."); return
        self.set_bands_for(row.question, *self._ordered(self.model.bands[row.question], "escalate", min(100, math.floor(row.conf) + 1)))
        self.say(f"{row.question}: escalate below {self.model.bands[row.question][0]}")

    def show_export_menu(self):
        self.export_button.showMenu()

    def ask_export(self, kind):
        _, label, pattern = next(e for e in EXPORTS if e[0] == kind)
        suffix = {"jsonl": ".jsonl", "csv": ".csv", "bundle": ".zip"}[kind]
        start = Path.cwd() / f"tarnlight-demo{suffix}" if self.demo else self.log.path.with_suffix(suffix)  # the demo's folder goes on close
        path, _ = QFileDialog.getSaveFileName(self, label, str(start), pattern)
        if path: self.export_to(kind, path)

    def export_to(self, kind, path):
        """Export in the background (the window keeps running); the result shows in the status bar."""
        if self.export_job is not None: self.say("An export is already running."); return
        run = {"jsonl": self.log.export_jsonl, "csv": self.log.export_csv, "bundle": self.log.export_bundle}[kind]
        def job():
            try: run(path); self.export_result = f"Exported to {path}"
            except Exception as e: self.export_result = f"Export failed: {type(e).__name__}: {e}"
        self.export_job = threading.Thread(target=job, name="tarnlight-export", daemon=True); self.export_job.start()
        self.say("Exporting...")

    def replay_record(self, rec):
        """Send a stored call to TypeSafe again, through the proxy, so the new decision is recorded like any other. The key
        is read from the environment for this one request and kept nowhere."""
        if self.replay_job is not None: self.say("A replay is already running."); return
        request = rec.get("request")
        if not isinstance(request, dict) or _is_hashed(request.get("state")):
            self.say(f"#{rec.get('seq')} cannot be replayed: this session keeps states hashed or redacted."); return
        if not api_key(): self.say("Replay needs TYPESAFE_API_KEY, which is not set for this console."); return
        project = rec.get("project")
        url = f"http://127.0.0.1:{self.proxy.port}" + (f"/p/{quote(project, safe='')}" if project else "") + "/v1/systemone"
        body = json.dumps(request).encode()
        def job():
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
                "X-Tarnlight-Label": f"#{rec.get('seq')}", "X-Tarnlight-Source": "replay"})  # listed as "replay · #12"
            try:
                with urllib.request.urlopen(req, timeout=60) as r: status = r.status
            except urllib.error.HTTPError as e: status = e.code
            except OSError as e: self.replay_result = f"Replay of #{rec.get('seq')} failed: {type(e).__name__}"; return
            self.replay_result = f"Replayed #{rec.get('seq')}: TypeSafe answered {status}; the new decision is at the top of the feed."
        self.replay_job = threading.Thread(target=job, name="tarnlight-replay", daemon=True); self.replay_job.start()
        self.say(f"Replaying #{rec.get('seq')}...")

    def closeEvent(self, event):
        if self.export_job is not None: self.export_job.join()  # let it finish before the log is compacted and closed
        super().closeEvent(event)
