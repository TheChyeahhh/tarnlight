"""
The decision feed: newest first, one row per (call, question), for the newest records the window holds.

Rows are exactly the decisions the index holds, with its names and its per-mille confidence, so the feed, the chart, the
counters and the review queue agree on every band edge. The table is virtual: Qt asks only for the rows on screen. Filters: presets (all, the review queue, the selected question, graded) plus free text, where
words match the question, source or answer and conf<40 / conf>=70 / ms>100 compare numbers.
"""
import bisect, collections, operator, re, time
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontInfo, QFontMetrics
from PySide6.QtWidgets import QStyle, QStyledItemDelegate
from .model import failure, source_label
from .storage import _text, summarize

MAX_ROWS = 100_000   # about the ring's 20,000 calls at five questions each
COLUMNS = ("time", "source", "question", "answer", "distribution", "conf.", "ms", "outcome")
DIST = COLUMNS.index("distribution")
OUTCOME_TEXT = {None: "", "correct": "correct", "wrong": "wrong", "flagged": "flagged"}   # pending says nothing
AMBER, BLUE, RED, GREEN, TEXT, SOFT, MUTED, DIM, BAR = "#f2a93b", "#5aa9ff", "#ff5c5c", "#3ecf8e", "#e6e8eb", "#c7ccd4", "#8b93a1", "#4a5263", "#1e232b"
OTHER = "#3a4150"   # every option but the chosen one, in one grey
OUTCOME_COLOR = {None: DIM, "correct": GREEN, "wrong": RED, "flagged": AMBER}
OUTCOME_TINT = {"correct": "#15291f", "wrong": "#321a1c", "flagged": "#32281a"}   # a dark tint of the same hue, for the pill
COMPARE = re.compile(r"^(conf|ms)(<=|>=|<|>|=)(\d+(?:\.\d+)?)$")
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "=": operator.eq}

Row = collections.namedtuple("Row", "seq ts source question qtype answer segments conf ms")  # a failed call: qtype "error", conf None
R = Qt.ItemDataRole
HANDLED = {R.DisplayRole, R.ForegroundRole, R.TextAlignmentRole, R.FontRole, R.UserRole}
COLORS = {c: QColor(c) for c in (RED, AMBER, GREEN, TEXT, SOFT, MUTED, DIM, BAR, OTHER, BLUE, *OUTCOME_TINT.values())}  # made once
RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
SELECTED = QColor(BAR)


def rows_from(rec):
    """Feed rows for one stored record: one per decision the index holds (its name, its per-mille confidence / 10), with
    the answer and the shares for the bar read from the answer as sent. Confidences outside 0..100 % are left out, as
    the chart and the counters leave them out."""
    try:
        answers = summarize(rec)[1]
    except Exception:
        return []
    resp = rec.get("response")
    wire = resp.get("answers") if isinstance(resp, dict) else None
    wire = {_text(k): v for k, v in wire.items()} if isinstance(wire, dict) else {}  # the index's spelling of each name
    who = source_label(rec) + (f" · {rec['label']}" if isinstance(rec.get("label"), str) and rec["label"] else "")  # which check made it
    out = []
    for seq, q, qtype, pm, _margin, _top, _p, chosen in answers:
        if pm is None or not 0 <= pm <= 1000: continue
        a = wire.get(q) if isinstance(wire.get(q), dict) else {}
        answer = ("yes" if chosen == "true" else "no") if qtype == "noul" else _score_text(a, chosen) if qtype == "score" else chosen or ""
        out.append(Row(seq, rec.get("ts"), who, q, qtype, answer, _segments(qtype, a, pm), pm / 10, rec.get("latency_ms")))
    if not out and (tag := failure(rec)):  # a failed call: one row, nothing to grade
        out.append(Row(rec.get("seq"), rec.get("ts"), who, "", "error", tag, (), None, rec.get("latency_ms")))
    return out


def _score_text(a, fallback):
    """The score and, when the answer carries a legend, the level it is nearest to: "1.12 · medium"."""
    try:
        score = float(a["score"]); legend = a.get("legend")
        level = legend.get(str(round(score))) if isinstance(legend, dict) else None
        return f"{score:g} · {level}" if isinstance(level, str) and level else f"{score:g}"
    except (KeyError, TypeError, ValueError, OverflowError):
        return fallback or ""


def _segments(qtype, a, pm):
    """Shares for the stacked bar: the chosen one first. An odd shape gives an empty bar, never a missing row."""
    try:
        if qtype == "noul": return (pm / 1000, 1 - pm / 1000)
        probs = a["probabilities"]
        if qtype == "score": return (float(a["score"]) / max(1, len(probs) - 1),)
        chosen = a["choice"]
        return (float(probs.get(chosen, 0)), *sorted((float(v) for k, v in probs.items() if k != chosen), reverse=True))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return ()


class FeedModel(QAbstractTableModel):
    """The rows the feed shows, newest first, after the current filter."""
    def __init__(self, bands, outcomes=None):
        super().__init__()
        self.bands, self.outcomes = bands, outcomes if outcomes is not None else {}
        self.caught_up_seq = None  # set when the drop box has caught up: rows up to it arrived while the console was closed
        self.rows = collections.deque(maxlen=MAX_ROWS)
        self.view = []                       # filtered rows, oldest first; row i on screen is view[-1 - i]
        self.preset, self.question, self.text, self.words = "all", None, "", []
        self.mono = QFont("JetBrains Mono"); self.mono.setPixelSize(12)

    # ---- data in
    def add(self, records):
        """Add the rows of new records; returns how many appeared at the top of the view."""
        new = [r for rec in records for r in rows_from(rec)][-self.rows.maxlen:]  # more than the feed holds: the newest only
        if not new: return 0
        self.rows.extend(new)
        dropped = len(self.view) and self.view[0].seq < self.rows[0].seq
        keep = [r for r in new if self._matches(r)]
        if dropped:  # the oldest rows left the deque: drop them from the bottom of the view too
            oldest = self.rows[0].seq
            gone = next((i for i, r in enumerate(self.view) if r.seq >= oldest), len(self.view))
            self.beginRemoveRows(QModelIndex(), len(self.view) - gone, len(self.view) - 1)
            del self.view[:gone]; self.endRemoveRows()
        if keep:
            self.beginInsertRows(QModelIndex(), 0, len(keep) - 1)
            self.view.extend(keep); self.endInsertRows()
        return len(keep)

    def set_filter(self, preset=None, question=None, text=None):
        self.preset = preset if preset is not None else self.preset
        self.question = question if question is not None else self.question
        self.text = text if text is not None else self.text
        self.words = [(m.group(1), OPS[m.group(2)], float(m.group(3))) if m else word  # parsed once, not once per row
                      for word in self.text.lower().split() for m in [COMPARE.match(word)]]
        self.beginResetModel()
        self.view = list(self.rows) if self.preset == "all" and not self.words else [r for r in self.rows if self._matches(r)]
        self.endResetModel()

    def set_outcome(self, row, outcome):
        """Show a grade on a row already saved to the log; a filter that no longer matches drops the row."""
        self.outcomes[(row.seq, row.question)] = outcome
        if outcome is None: self.outcomes.pop((row.seq, row.question), None)
        i = self.index_of(row)
        if i is None: return
        if self._matches(row):
            self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLUMNS) - 1))
        else:
            self.beginRemoveRows(QModelIndex(), i, i); del self.view[len(self.view) - 1 - i]; self.endRemoveRows()

    def refilter(self): self.set_filter()

    # ---- filtering
    def _matches(self, r):
        if self.preset == "review" and not (self.needs_you(r) and (r.seq, r.question) not in self.outcomes): return False
        if self.preset == "question" and r.question != self.question: return False
        if self.preset == "graded" and (r.seq, r.question) not in self.outcomes: return False
        for word in self.words:
            if isinstance(word, tuple):
                field, op, limit = word; value = r.conf if field == "conf" else r.ms
                if value is None or not op(value, limit): return False
            elif word not in r.question.lower() and word not in r.source.lower() and word not in r.answer.lower():
                return False
        return True

    def needs_you(self, r):
        """Below its question's escalate: the rows marked red and listed in the review queue."""
        return r.conf is not None and r.conf < self.bands[r.question][0]

    # ---- Qt
    def row_at(self, i): return self.view[len(self.view) - 1 - i] if 0 <= i < len(self.view) else None

    def line_above(self, i):
        """True for the newest row from while the console was closed, when a newer one is above it: the caught-up line."""
        r, above = self.row_at(i), self.row_at(i - 1) if i > 0 else None
        return self.caught_up_seq is not None and above is not None and r.seq <= self.caught_up_seq < above.seq

    def same_call_above(self, i):
        """True when the row above on screen is from the same call: its time, source and ms are shown there once."""
        return i > 0 and self.row_at(i - 1) is not None and self.row_at(i - 1).seq == self.row_at(i).seq

    def index_of(self, row):
        """The on-screen index of the row with this (seq, question), or None. The view is in seq order: a binary search."""
        i = bisect.bisect_left(self.view, row.seq, key=lambda r: r.seq)
        while i < len(self.view) and self.view[i].seq == row.seq:
            if self.view[i].question == row.question: return len(self.view) - 1 - i
            i += 1
        return None

    def rowCount(self, parent=QModelIndex()): return 0 if parent.isValid() else len(self.view)
    def columnCount(self, parent=QModelIndex()): return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole: return COLUMNS[section]
        return None

    def data(self, index, role=R.DisplayRole):
        if role not in HANDLED: return None  # Qt asks about a dozen roles per cell; answer the unused ones first
        if role == R.FontRole: return self.mono
        c = index.column()
        if role == R.TextAlignmentRole: return RIGHT if c in (5, 6) else None
        r = self.row_at(index.row())
        if r is None: return None
        if role == R.UserRole: return r
        if role == R.DisplayRole:
            if c in (0, 1, 6) and self.same_call_above(index.row()): return ""
            if c == 0: return time.strftime("%H:%M:%S", time.localtime(r.ts)) + f".{int((r.ts % 1) * 1000):03d}" if r.ts else ""
            if c == 7: return OUTCOME_TEXT[self.outcomes.get((r.seq, r.question))]
            return (None, r.source, r.question, r.answer, None, percent(r.conf), "" if r.ms is None else str(r.ms))[c]
        if c == 3 and r.qtype == "error": return COLORS[RED]
        if c == 5:
            if r.conf is None: return COLORS[MUTED]
            esc, rev = self.bands[r.question]
            return COLORS[RED if r.conf < esc else AMBER if r.conf < rev else GREEN]
        if c == 7: return COLORS[OUTCOME_COLOR[self.outcomes.get((r.seq, r.question))]]
        if c == 1 and r.source.startswith("replay"): return COLORS[BLUE]  # a replay sent from this console, not a caller's call
        return COLORS[(SOFT, MUTED, TEXT, TEXT, TEXT, TEXT, MUTED)[c]]


def caught_up_line(painter, option, index):
    if index.model().line_above(index.row()): painter.fillRect(QRectF(option.rect.x(), option.rect.y(), option.rect.width(), 1), COLORS[BLUE])


def percent(conf):
    """69, not 69.0 (real answers come in whole percents); 85.5 keeps its decimal; nothing for a failed call."""
    return "" if conf is None else f"{conf:.0f}" if conf == int(conf) else f"{conf:.1f}"


class TextDelegate(QStyledItemDelegate):
    """Draws a text cell straight onto the painter. The styled path (under the window's stylesheet) costs about 50 us a
    cell, and the table repaints five times a second while decisions arrive."""
    def __init__(self, parent=None):
        super().__init__(parent); self.metrics = {}

    def paint(self, painter, option, index):
        m = index.model()
        if option.state & QStyle.StateFlag.State_Selected: painter.fillRect(option.rect, SELECTED)
        if index.column() == 0 and m.needs_you(m.row_at(index.row())):  # a thin red edge: only what needs a person is marked
            painter.fillRect(QRectF(option.rect.x(), option.rect.y() + 3, 3, option.rect.height() - 6), COLORS[RED])
        caught_up_line(painter, option, index)
        text = m.data(index, R.DisplayRole)
        if not text: return
        if index.column() == 7: return self.pill(painter, option.rect, text, m)
        fm, advance = self.metrics.get(id(m)) or self.metrics.setdefault(id(m), self.measure(m.mono))
        rect = option.rect.adjusted(6, 0, -6, 0)
        painter.setFont(m.mono); painter.setPen(m.data(index, R.ForegroundRole))
        painter.drawText(rect, m.data(index, R.TextAlignmentRole) or LEFT, self.fit(text, rect.width(), fm, advance))

    @staticmethod
    def pill(painter, rect, outcome, m):
        """A graded row's outcome: a dot and a word on a dark tint of the same hue."""
        fm = QFontMetrics(m.mono); w = fm.horizontalAdvance(outcome) + 22
        box = QRectF(rect.x() + 6, rect.y() + (rect.height() - 16) / 2, w, 16)
        painter.save(); painter.setRenderHint(painter.RenderHint.Antialiasing); painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(COLORS[OUTCOME_TINT[outcome]]); painter.drawRoundedRect(box, 8, 8)
        painter.setBrush(COLORS[OUTCOME_COLOR[outcome]]); painter.drawEllipse(QRectF(box.x() + 7, box.center().y() - 2.5, 5, 5))
        painter.setFont(m.mono); painter.setPen(COLORS[OUTCOME_COLOR[outcome]])
        painter.drawText(box.adjusted(16, 0, 0, 0), LEFT, outcome); painter.restore()

    @staticmethod
    def fit(text, width, fm, advance):
        """The text shortened to width. A monospaced ASCII line that fits is returned as it is, without measuring."""
        if advance and text.isascii() and len(text) * advance <= width: return text
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, width)


    @staticmethod
    def measure(font):
        """The font's metrics, and its character width if it really is monospaced (else 0: always measure)."""
        fm = QFontMetrics(font)
        return fm, fm.horizontalAdvance("0") if QFontInfo(font).fixedPitch() else 0


class DistributionDelegate(QStyledItemDelegate):
    """A thin stacked bar: the chosen share amber, every other option one grey; a score is one blue fill; a failed call
    an empty hatched bar."""
    HEIGHT = 6

    def paint(self, painter, option, index):
        if option.state & QStyle.StateFlag.State_Selected: painter.fillRect(option.rect, SELECTED)
        r = index.data(Qt.ItemDataRole.UserRole)
        if r is None: return
        caught_up_line(painter, option, index)
        pad = (option.rect.height() - self.HEIGHT) / 2
        rect = QRectF(option.rect).adjusted(8, pad, -8, -pad)
        painter.save(); painter.setRenderHint(painter.RenderHint.Antialiasing); painter.setPen(Qt.PenStyle.NoPen)
        if r.qtype == "error":
            painter.setBrush(COLORS[BAR]); painter.drawRoundedRect(rect, 3, 3)
            painter.setBrush(QBrush(COLORS[DIM], Qt.BrushStyle.BDiagPattern)); painter.drawRoundedRect(rect, 3, 3)
        elif r.qtype == "score":
            painter.setBrush(COLORS[BAR]); painter.drawRoundedRect(rect, 3, 3)
            painter.setBrush(COLORS[BLUE]); painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width() * max(0.0, min(1.0, r.segments[0] if r.segments else 0)), rect.height()), 3, 3)
        else:
            x, gap = rect.x(), 2.0
            usable = rect.width() - gap * (len(r.segments) - 1)
            for i, share in enumerate(r.segments):
                w = usable * max(0.0, share)
                if w <= 0: continue
                painter.setBrush(COLORS[AMBER if i == 0 else OTHER])
                painter.drawRoundedRect(QRectF(x, rect.y(), w, rect.height()), 3, 3); x += w + gap
        painter.restore()
