"""
The inspector: the selected decision in full, in reading order. The answer large, its confidence, a bar
per option, what was asked (muted), the user's bands, the other questions of the call as small rows, the state
summarized by top-level keys with its hash and size (expandable to pretty JSON), what changed since the previous call
from the same sender, and one line of tokens, latency, estimated cost, status and request id. A failed call shows what
failed instead of an answer. Copy as curl, save as fixture.

This is the one place a block is decompressed: the window hands it the full record from the log.
"""
import json
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)
from .feed import GREEN, RED, percent, rows_from
from .storage import HASHED, _canon, _hash, _is_hashed, _text

MAX_CHANGES, MAX_OTHERS, MAX_ASKED = 12, 12, 280
AMBER, TEXT, SOFT, MUTED, DIM, BAR = "#f2a93b", "#e6e8eb", "#c7ccd4", "#8b93a1", "#4a5263", "#1e232b"


def summarize_value(v, width=40):
    """A short description of one state value, the way the inspector lists top-level keys."""
    if isinstance(v, dict):
        keys = list(v)
        return "{" + ", ".join(map(str, keys[:3])) + ("…" if len(keys) > 3 else "") + "}"
    if isinstance(v, list):
        if v and all(isinstance(x, (str, int, float, bool)) for x in v[:5]) and len(json.dumps(v[:5])) < width:
            return "[" + ", ".join(map(str, v[:5])) + ("…" if len(v) > 5 else "") + "]"
        return f"[{len(v)}]"
    text = json.dumps(v)
    return text if len(text) <= width else text[:width - 1] + "…"


def changes(before, after, path="", out=None):
    """Leaf values that differ between two states: (path, old, new); a missing side is None. Stops at MAX_CHANGES."""
    out = [] if out is None else out
    if len(out) >= MAX_CHANGES: return out
    if isinstance(before, dict) and isinstance(after, dict):
        for k in list(before) + [k for k in after if k not in before]:
            changes(before.get(k), after.get(k), f"{path}.{k}" if path else str(k), out)
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for i, (b, a) in enumerate(zip(before, after)): changes(b, a, f"{path}[{i}]", out)
    elif before != after:
        out.append((path or "state", before, after))
    return out[:MAX_CHANGES]


def _number(x): return isinstance(x, (int, float)) and not isinstance(x, bool)


def curl(request):
    """The call as a curl command that reads the key from the environment: the key itself is never written."""
    body = json.dumps(request, separators=(",", ":")).replace("'", "'\\''")
    return ("curl https://api.typesafe.ai/v1/systemone -H \"Authorization: Bearer $TYPESAFE_API_KEY\" "
            f"-H \"Content-Type: application/json\" -d '{body}'")


def text(s="", size=12, color=TEXT, mono=True):
    label = QLabel(s); f = QFont("JetBrains Mono" if mono else "IBM Plex Sans"); f.setPixelSize(size)
    label.setFont(f); label.setStyleSheet(f"color: {color}; background: transparent;"); label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)  # the inspector column is narrow: long lines wrap
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # and a long word never widens it
    return label


class Blocks(QWidget):
    """Ten small blocks, one per ten points of confidence: another question of the call at a glance."""
    def __init__(self, pct):
        super().__init__(); self.setFixedSize(68, 12)
        self.n = None if pct is None else round(max(0.0, min(100.0, pct)) / 10)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); p.setPen(Qt.PenStyle.NoPen)
        for i in range(10):
            p.setBrush(QColor(MUTED if self.n is not None and i < self.n else BAR)); p.drawRoundedRect(QRectF(i * 7, 3, 5, 6), 1, 1)


class Inspector(QWidget):
    def __init__(self, privacy=None):
        super().__init__()
        self.privacy, self.record, self.question = privacy, None, None
        self.box = QVBoxLayout(self); self.box.setContentsMargins(0, 0, 0, 0); self.box.setSpacing(8)
        self.empty = text("Select a decision in the feed.", 12, DIM, mono=False); self.box.addWidget(self.empty)
        self.content = QWidget(); self.layout_ = QVBoxLayout(self.content); self.layout_.setContentsMargins(0, 0, 0, 0); self.layout_.setSpacing(8)
        self.box.addWidget(self.content); self.content.hide()
        self.pretty = QPlainTextEdit(); self.pretty.setReadOnly(True); self.pretty.hide()
        self.pretty.setStyleSheet(f"background: #0f1216; color: {SOFT}; border: 1px solid {BAR}; font-family: 'JetBrains Mono'; font-size: 11px;")
        buttons = QGridLayout(); buttons.setSpacing(6)
        self.expand = QPushButton("state as JSON"); self.copy = QPushButton("copy as curl"); self.save = QPushButton("save as fixture")
        self.replay = QPushButton("replay this one"); self.on_replay = None  # set by the window when a proxy can send it
        self.replay.setToolTip("Send this call to TypeSafe again, through the proxy: a new, paid decision")
        for i, b in enumerate((self.expand, self.copy, self.save, self.replay)):
            b.setObjectName("tool"); buttons.addWidget(b, i // 2, i % 2)
        self.expand.setCheckable(True); self.expand.toggled.connect(self._toggle_json)
        self.copy.clicked.connect(self.copy_curl); self.save.clicked.connect(self.save_fixture)
        self.replay.clicked.connect(lambda: self.on_replay and self.record is not None and self.on_replay(self.record))
        self.box.addWidget(self.pretty, 1); self.box.addLayout(buttons); self.box.addStretch(0)
        for b in (self.expand, self.copy, self.save, self.replay): b.setEnabled(False)

    def show_decision(self, record, question, previous=None, bands=None):
        """Show one decision: the full record, which of its questions was selected, the previous record from the sender,
        and the user's (escalate, review) for the question when known."""
        self.record, self.question = record, question
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            if item.widget(): item.widget().hide(); item.widget().deleteLater()  # hidden now, deleted by the event loop
        self.empty.setVisible(record is None); self.content.setVisible(record is not None)
        for b in (self.expand, self.copy, self.save): b.setEnabled(record is not None)
        self.replay.setEnabled(record is not None and self.on_replay is not None)
        if record is None: self.pretty.hide(); return
        answers = (record.get("response") or {}).get("answers") if isinstance(record.get("response"), dict) else {}
        answers = {_text(k): v for k, v in answers.items()} if isinstance(answers, dict) else {}  # the feed names decisions as the index does
        add = self.layout_.addWidget
        row = next((r for r in rows_from(record) if r.question == question), None)
        if row is not None and row.conf is None:  # a failed call
            add(self._big(row.answer, RED)); add(text("A failed call: no answer, nothing to grade.", 12, MUTED, mono=False))
            if record.get("error") is not None:  # what TypeSafe, the caller or the proxy said, shortened
                said = json.dumps(record["error"]); add(text(said if len(said) <= MAX_ASKED else said[:MAX_ASKED - 1] + "…", 11, SOFT))
        else:
            add(self._big(row.answer if row else "?", TEXT))
            if row is not None:
                esc, rev = bands if bands else (None, None)
                color = SOFT if esc is None else RED if row.conf < esc else AMBER if row.conf < rev else GREEN
                add(text(f"confidence {percent(row.conf)}%", 12, color))
            for name, share, chosen in self._options(answers.get(question, {})): add(self._bar(name, share, chosen))
            asked = self._asked(record, question)
            if asked: add(text(asked, 11, MUTED, mono=False))
            if bands: add(text(f"your bands: escalate < {bands[0]} · review < {bands[1]}", 11, DIM))
        others = [(q, a) for q, a in answers.items() if q != question]
        for q, a in others[:MAX_OTHERS]: add(self._other(q, a))
        if len(others) > MAX_OTHERS: add(text(f"+{len(others) - MAX_OTHERS} more questions", 11, DIM))
        state = (record.get("request") or {}).get("state") if isinstance(record.get("request"), dict) else None
        if _is_hashed(state):
            m = state[HASHED]
            add(text(f"state hashed: blake2b {m['blake2b'][:8]} · {m['bytes']:,} B · keys {', '.join(m['keys'] or [])}", 11, MUTED))
        else:
            add(text(f"state blake2b {_hash(_canon(state))[:8]} · {len(_canon(state)):,} B", 11, MUTED))
            if isinstance(state, dict):
                for k, v in list(state.items())[:8]: add(text(f"▸ {k} {summarize_value(v)}", 11, SOFT))
        if previous is not None:
            before = (previous.get("request") or {}).get("state") if isinstance(previous.get("request"), dict) else None
            diff = changes(before, state)
            add(text(f"changed since #{previous.get('seq')} ({len(diff)}{'+' if len(diff) == MAX_CHANGES else ''} fields)", 11, MUTED))
            for path, old, new in diff: add(text(f"{path}  {summarize_value(old, 24)} → {summarize_value(new, 24)}", 11, SOFT))
        usage = (record.get("response") or {}).get("usage") if isinstance(record.get("response"), dict) else None
        usage = usage if isinstance(usage, dict) else {}
        cost = record.get("cost_est_micro")
        add(text(f"{usage.get('input_tokens', '-')} in · {usage.get('output_tokens', '-')} out · {record.get('latency_ms', '-')} ms · "
                 f"{'$%.6f' % (cost / 1e6) if isinstance(cost, (int, float)) else '-'} est. · status {record.get('status', '-')} · "
                 f"request {record.get('request_id') or '-'}", 11, DIM))
        if self.expand.isChecked(): self._toggle_json(True)

    def _options(self, a):
        """(option, share, chosen) for the selected answer, most likely first. Shares that are not numbers are left out."""
        a = a if isinstance(a, dict) else {}
        if a.get("type") == "noul":
            p = a.get("noul")
            return [("yes", p, p >= 0.5), ("no", 1 - p, p < 0.5)] if _number(p) else []
        probs, legend = a.get("probabilities"), a.get("legend")
        probs = {k: v for k, v in probs.items() if _number(v)} if isinstance(probs, dict) else {}
        legend = legend if isinstance(legend, dict) else {}
        chosen = a.get("choice") if a.get("type") == "choice" else None
        ranked = sorted(probs.items(), key=lambda kv: -kv[1])
        if chosen is None and ranked: chosen = ranked[0][0]
        return [(str(legend.get(k, k)), v, k == chosen) for k, v in ranked]

    @staticmethod
    def _brief(a):
        """One other question of the call, briefly: (its answer, its confidence 0..100 or None)."""
        t = a.get("type") if isinstance(a, dict) else None
        if t == "noul" and _number(a.get("noul")): return ("yes" if a["noul"] >= 0.5 else "no"), 100 * max(a["noul"], 1 - a["noul"])
        if t in ("choice", "score") and _number(a.get("confidence")):
            answer = a.get("choice" if t == "choice" else "score")
            return (f"{answer:g}" if isinstance(answer, float) else _text(str(answer))), 100 * a["confidence"]
        return "?", None

    @staticmethod
    def _asked(record, question):
        """The question's instructions as sent, shortened: what Jev was asked."""
        questions = (record.get("request") or {}).get("questions") if isinstance(record.get("request"), dict) else None
        questions = {_text(k): v for k, v in questions.items()} if isinstance(questions, dict) else {}
        q = questions.get(question)
        asked = q.get("instructions") if isinstance(q, dict) else q
        if asked is None: return ""
        asked = _text(asked) if isinstance(asked, str) else json.dumps(asked)
        return asked if len(asked) <= MAX_ASKED else asked[:MAX_ASKED - 1] + "…"

    @staticmethod
    def _fit(label, s, width):
        """Shorten a label to its width, the full text in a tooltip."""
        label.setFixedWidth(width); short = QFontMetrics(label.font()).elidedText(s, Qt.TextElideMode.ElideRight, width)
        label.setText(short); label.setToolTip(s if short != s else "")
        label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred); label.setWordWrap(False)

    @staticmethod
    def _big(answer, color):
        return text(answer, 20, color)  # it wraps: a fixed width would widen the narrow column

    def _other(self, question, a):
        answer, pct = self._brief(a)
        row = QWidget(); box = QHBoxLayout(row); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(8)
        value = text("?" if pct is None else f"{max(0.0, min(100.0, pct)):.0f}", 11, SOFT); value.setAlignment(Qt.AlignmentFlag.AlignRight)
        name, said = text("", 11, MUTED), text("", 11, SOFT)
        self._fit(value, value.text(), 24); self._fit(name, question, 100); self._fit(said, answer, 80)  # 296 px in all
        for w in (Blocks(pct), value, name, said): box.addWidget(w)
        box.addStretch(1)
        return row

    def _bar(self, name, share, chosen):
        row = QWidget(); box = QHBoxLayout(row); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(8)
        option = text("", 12, TEXT if chosen else MUTED)
        self._fit(option, name, 120)  # a long name is shortened, the full one in a tooltip
        bar = QLabel(); bar.setFixedHeight(6)
        pct = round(100 * max(0.0, min(1.0, share)))  # a share past 1 would overflow the bar (and round(inf) raises)
        bar.setStyleSheet(f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {AMBER if chosen else DIM}, "
                          f"stop:{pct / 100:.3f} {AMBER if chosen else DIM}, stop:{min(1, pct / 100 + 0.001):.3f} {BAR}, stop:1 {BAR}); border-radius: 2px;")
        value = text(f"{pct}", 12, TEXT if chosen else MUTED); value.setFixedWidth(28); value.setAlignment(Qt.AlignmentFlag.AlignRight)
        value.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred); value.setWordWrap(False)  # fixed columns
        box.addWidget(option); box.addWidget(bar, 1); box.addWidget(value)
        return row

    def _toggle_json(self, on):
        if on and self.record is not None:
            state = (self.record.get("request") or {}).get("state") if isinstance(self.record.get("request"), dict) else None
            self.pretty.setPlainText(json.dumps(state, indent=2, ensure_ascii=False))
        self.pretty.setVisible(on)

    def copy_curl(self):
        if self.record is not None: QGuiApplication.clipboard().setText(curl(self.record.get("request")))

    def save_fixture(self, path=None):
        """Write the record, through the log's privacy filter, to a JSON file."""
        if self.record is None: return
        path = path or QFileDialog.getSaveFileName(self, "Save as fixture", f"decision-{self.record.get('seq')}.json", "JSON (*.json)")[0]
        if not path: return
        rec = self.privacy.apply(self.record) if self.privacy else self.record
        body = json.dumps(rec, indent=2, ensure_ascii=False)
        try: body.encode("utf-8")
        except UnicodeEncodeError: body = json.dumps(rec, indent=2)  # a lone surrogate cannot be UTF-8: write it escaped
        with open(path, "w", encoding="utf-8") as f: f.write(body)
