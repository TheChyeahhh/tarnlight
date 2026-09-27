import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QTableView
from tarnlight import feed
from tarnlight.bands import Bands
from tarnlight.feed import FeedModel, DistributionDelegate, rows_from
from tarnlight.inspector import Inspector, changes, curl, summarize_value
from tarnlight.storage import HASHED, Privacy


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def numbered(recs): return [{**r, "seq": i} for i, r in enumerate(recs)]


def shown(model): return [(model.row_at(i).seq, model.row_at(i).question) for i in range(model.rowCount())]


# ---- rows

def test_one_row_per_answered_question_and_one_per_failed_call(sample_records):
    recs = numbered(sample_records)
    rows = [r for rec in recs for r in rows_from(rec)]
    assert sum(r.qtype != "error" for r in rows) == sum(len(rec["response"]["answers"]) for rec in recs if rec["response"])
    failed = [r for r in rows if r.qtype == "error"]
    assert [r.seq for r in failed] == [rec["seq"] for rec in recs if not rec["response"]] and failed
    assert all(r.conf is None and r.segments == () and r.question == "" and r.answer for r in failed)


def test_row_shapes_for_each_answer_type(make_record):
    rec = numbered([make_record(0)])[0]
    rec["response"]["answers"]["go"]["noul"] = 0.3
    rows = {r.question: r for r in rows_from(rec)}
    assert (rows["go"].answer, rows["go"].conf, rows["go"].segments) == ("no", pytest.approx(70), (pytest.approx(0.7), pytest.approx(0.3)))
    assert (rows["verdict"].answer, rows["verdict"].segments) == ("approve", (0.8, 0.1, 0.1))  # the chosen share comes first
    assert rows["size"].answer == "1.2 · medium" and rows["size"].segments == (pytest.approx(0.6),)  # score 1.2 of 0..2, nearest 1
    del rec["response"]["answers"]["size"]["legend"]
    assert {r.question: r for r in rows_from(rec)}["size"].answer == "1.2"  # no legend: the number alone
    odd = numbered([make_record(1)])[0]; odd["response"]["answers"]["verdict"] = {"type": "choice"}
    assert {r.question for r in rows_from(odd)} == {"go", "size"} and [r.answer for r in rows_from({**odd, "response": None})] == ["no answer"]


# ---- the table

def test_newest_first_and_filters(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json"))
    recs = numbered([make_record(i) for i in range(4)])
    for i, rec in enumerate(recs): rec["response"]["answers"]["go"]["noul"] = (0.9, 0.5, 0.55, 0.95)[i]  # go conf 90, 50, 55, 95
    m.add(recs[:2]); m.add(recs[2:])
    assert m.rowCount() == 12 and m.row_at(0).seq == 3
    m.set_filter(text="go conf<60")
    assert shown(m) == [(2, "go"), (1, "go")]
    m.set_filter(preset="question", question="size", text="")
    assert {q for _, q in shown(m)} == {"size"} and m.rowCount() == 4
    m.bands.set("go", 52, 70); m.set_filter(preset="review")
    assert shown(m) == [(1, "go")]  # only go at 50 is below its escalate of 52; everything else is at or above 50
    m.set_outcome(m.row_at(0), "wrong")
    assert m.rowCount() == 0  # graded, so it leaves the review queue
    m.set_filter(preset="graded")
    assert shown(m) == [(1, "go")] and m.data(m.index(0, 7)) == "wrong"


def test_the_oldest_rows_leave_when_the_window_is_full(app, tmp_path, make_record, monkeypatch):
    monkeypatch.setattr(feed, "MAX_ROWS", 9)  # three calls
    m = FeedModel(Bands(tmp_path / "b.json"))
    for rec in numbered([make_record(i) for i in range(5)]): m.add([rec])
    assert m.rowCount() == 9 and {m.row_at(i).seq for i in range(9)} == {2, 3, 4}


def test_the_confidence_column_is_colored_by_band(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json")); m.add(numbered([make_record(0)]))
    colors = {m.row_at(i).question: m.data(m.index(i, 5), Qt.ItemDataRole.ForegroundRole).name() for i in range(3)}
    assert colors == {"go": feed.GREEN, "verdict": feed.GREEN, "size": feed.AMBER}  # 100 (a noul at 0.0), 70 = review, 50


def test_the_table_paints(app, tmp_path, sample_records):
    m = FeedModel(Bands(tmp_path / "b.json")); m.add(numbered(sample_records))
    view = QTableView(); view.setModel(m); view.setItemDelegateForColumn(feed.DIST, DistributionDelegate(view)); view.resize(1000, 400); view.show()
    assert not view.grab().isNull()


# ---- the inspector

def test_the_inspector_shows_a_decision_and_what_changed(app, make_record):
    before, after = numbered([make_record(0), make_record(1)])
    ins = Inspector()
    ins.show_decision(after, "verdict", previous=before)
    texts = [w.text() for w in ins.content.findChildren(type(ins.empty))]
    assert any("approve" in t for t in texts) and any(t.startswith("changed since #0") for t in texts)
    assert any("tick" in t and "0 → 1" in t for t in texts)
    ins.show_decision(None, None)
    assert ins.content.isHidden() and not ins.empty.isHidden()


def test_the_inspector_shows_a_hashed_state_without_it(app, make_record):
    rec = Privacy("hash").apply(numbered([make_record(0, state={"email": "ann@example.com"})])[0])
    ins = Inspector(); ins.show_decision(rec, "go")
    texts = " ".join(w.text() for w in ins.content.findChildren(type(ins.empty)))
    assert "state hashed" in texts and "ann@example.com" not in texts


def test_curl_names_the_key_but_never_contains_it(make_record, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_FAKE_NOT_A_REAL_KEY_0123")
    command = curl(make_record(0, state="it's quoted")["request"])
    assert "$TYPESAFE_API_KEY" in command and "ts_FAKE" not in command and "it'\\''s" in command


def test_save_as_fixture_goes_through_the_privacy_filter(app, tmp_path, make_record):
    ins = Inspector(privacy=Privacy("hash")); ins.show_decision(numbered([make_record(0, state={"email": "a@b.c"})])[0], "go")
    ins.save_fixture(str(tmp_path / "f.json"))
    saved = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert HASHED in saved["request"]["state"] and "a@b.c" not in json.dumps(saved)


def test_summaries_and_changes():
    assert summarize_value({"a": 1, "b": 2, "c": 3, "d": 4}) == "{a, b, c…}"
    assert summarize_value(["defer", "approve"]) == "[defer, approve]" and summarize_value([{}] * 8) == "[8]"
    assert changes({"a": 1, "b": {"c": [1, 2]}}, {"a": 1, "b": {"c": [1, 3]}, "d": 0}) == [("b.c[1]", 2, 3), ("d", None, 0)]


def test_the_text_shortcut_holds_for_the_shipped_mono_font(app):
    from PySide6.QtGui import QFont
    from tarnlight.ui import load_fonts
    load_fonts(); f = QFont("JetBrains Mono"); f.setPixelSize(12)
    fm, advance = feed.TextDelegate.measure(f)
    assert advance > 0  # it is monospaced, so the fast path is used
    ascii_ = "".join(chr(c) for c in range(32, 127))
    assert fm.horizontalAdvance(ascii_) == len(ascii_) * advance  # what the fast path assumes
    f2 = QFont("Arial"); f2.setPixelSize(12)
    assert feed.TextDelegate.measure(f2)[1] == 0  # a proportional font always measures
    fit = lambda text, width: feed.TextDelegate.fit(text, width, fm, advance)
    assert fit("action", 100) == "action" and fit("a" * 40, 100).endswith("…") and fm.horizontalAdvance(fit("a" * 40, 100)) <= 100
    assert fit("é" * 40, 100).endswith("…") and fit("x" * 10, 10 * advance) == "x" * 10 and fit("x" * 11, 10 * advance).endswith("…")
    wide = "✓" * 6  # wider than the mono advance (the feed's own outcome mark): counting characters would let it overflow
    assert fm.horizontalAdvance(wide) > 6 * advance and fit(wide, 6 * advance).endswith("…")


def test_the_table_paints_with_long_names_and_a_selection(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json"))
    rec = numbered([make_record(0)])[0]; rec["response"]["answers"] = {"q" * 300: {"type": "noul", "noul": 0.2}, "é" * 50: {"type": "noul", "noul": 0.9}}
    m.add([rec])
    view = QTableView(); view.setModel(m); view.setItemDelegate(feed.TextDelegate(view)); view.setItemDelegateForColumn(feed.DIST, DistributionDelegate(view))
    view.resize(1000, 200); view.show(); view.selectRow(0)
    assert not view.grab().isNull()


# ---- edge cases

def test_rows_are_the_index_decisions_with_its_numbers(sample_records):
    from tarnlight.storage import summarize
    for rec in numbered(sample_records):
        indexed = [(q, pm / 10) for _, q, _, pm, *_ in summarize(rec)[1] if pm is not None and 0 <= pm <= 1000]
        assert [(r.question, r.conf) for r in rows_from(rec) if r.qtype != "error"] == indexed


def test_a_band_edge_is_the_same_edge_everywhere(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json"))
    recs = numbered([make_record(i) for i in range(3)])
    for rec, c in zip(recs, (0.57, 0.29, 0.58)): rec["response"]["answers"] = {"q": {"type": "choice", "choice": "a", "confidence": c, "probabilities": {"a": c, "b": 1 - c}}}
    m.add(recs)
    assert [m.row_at(i).conf for i in range(3)] == [58.0, 29.0, 57.0]  # 100 * 0.57 would be 56.99999999999999
    m.bands.set("q", 57, 70)
    assert m.data(m.index(2, 5), Qt.ItemDataRole.ForegroundRole).name() == feed.AMBER  # 57.0 at escalate 57: not red
    m.set_filter(preset="review")
    assert shown(m) == [(1, "q")]  # 57.0 is not below 57, as in the index and the chart
    m.set_filter(preset="all", text="conf<57")
    assert shown(m) == [(1, "q")]


def test_odd_names_use_the_index_spelling(make_record):
    rec = numbered([make_record(0)])[0]; rec["response"]["answers"] = {"odd\ud800": {"type": "noul", "noul": 0.2}}
    (row,) = rows_from(rec)
    from tarnlight.storage import _text
    assert row.question == _text("odd\ud800") != "odd\ud800" and row.answer == "no"  # the name the index, chart and bands use


def test_lookups_and_trimming(app, tmp_path, make_record, monkeypatch):
    m = FeedModel(Bands(tmp_path / "b.json"))
    assert m.add(numbered([make_record(i) for i in range(4)])) == 12
    for i in range(12):
        assert m.index_of(m.row_at(i)) == i
    assert m.index_of(feed.Row(9, None, None, "go", None, None, None, None, None)) is None
    assert m.index_of(feed.Row(2, None, None, "nope", None, None, None, None, None)) is None
    m.set_filter(text="size"); assert [m.index_of(m.row_at(i)) for i in range(4)] == [0, 1, 2, 3]
    m.set_filter(text="")
    monkeypatch.setattr(feed, "MAX_ROWS", 6)
    small = FeedModel(Bands(tmp_path / "b.json"))
    assert small.add(numbered([make_record(i) for i in range(5)])) == 6  # more than it holds at once: the newest only
    assert small.rowCount() == 6 and {small.row_at(i).seq for i in range(6)} == {3, 4}
    assert small.add([{**make_record(5), "seq": 5}]) == 3
    assert small.rowCount() == 6 and {small.row_at(i).seq for i in range(6)} == {4, 5}


def inspector_texts(ins):
    """The texts the inspector shows now (widgets it took out are only deleted later, by the event loop)."""
    from PySide6.QtWidgets import QLabel
    widgets = [ins.layout_.itemAt(i).widget() for i in range(ins.layout_.count())]
    return [x.text() for w in widgets for x in ([w] if isinstance(w, QLabel) else w.findChildren(QLabel))]


def test_confidences_outside_0_to_100_are_not_decisions(make_record):
    rec = numbered([make_record(0)])[0]
    rec["response"]["answers"] = {"high": {"type": "choice", "choice": "a", "confidence": 1.7, "probabilities": {"a": 1.0}},
                                  "low": {"type": "choice", "choice": "a", "confidence": -0.2, "probabilities": {"a": 1.0}},
                                  "ok": {"type": "choice", "choice": "a", "confidence": 1.0, "probabilities": {"a": 1.0}}}
    assert [r.question for r in rows_from(rec)] == ["ok"]  # as the chart and the counters see it


def test_the_inspector_survives_odd_answers(app, make_record):
    rec = numbered([make_record(0)])[0]
    rec["response"]["answers"]["verdict"] = {"type": "choice", "choice": "a", "confidence": "0.8", "probabilities": {"a": "0.8", "b": 0.2}}
    rec["response"]["answers"]["size"] = {"type": "score", "confidence": None, "probabilities": {"0": 5.0}}
    ins = Inspector(); ins.show_decision(rec, "go")
    texts = inspector_texts(ins)
    assert texts.count("?") >= 4 and {"verdict", "size"} <= set(texts)  # the two odd questions, as small rows with no number
    assert any("request req_" in t for t in texts)  # built to the end
    ins.show_decision(rec, "verdict")
    assert [t for t in texts if t] and any(t == "20" for t in inspector_texts(ins))  # the one numeric share still shows
    ins.show_decision(rec, "size")
    assert "100" in inspector_texts(ins)  # a share past 1 is drawn full, not an error


def test_the_inspector_shortens_long_names_with_a_tooltip(app, make_record):
    rec = numbered([make_record(0)])[0]
    rec["response"]["answers"] = {"needs_follow_up_review": {"type": "choice", "choice": "needs_a_second_reviewer", "confidence": 0.6,
                                                              "probabilities": {"needs_a_second_reviewer": 0.6, "b": 0.4}},
                                  "a_second_question_with_a_long_name": {"type": "noul", "noul": 0.9}}
    ins = Inspector(); ins.show_decision(rec, "needs_follow_up_review")
    long = [w for w in ins.content.findChildren(type(ins.empty)) if w.toolTip() and w.isVisibleTo(ins)]
    assert {w.toolTip() for w in long} == {"a_second_question_with_a_long_name", "needs_a_second_reviewer"}  # an option, another question
    assert all(w.text().endswith("…") for w in long)


def test_the_inspector_finds_a_question_by_its_index_name(app, make_record):
    from tarnlight.storage import _text
    rec = numbered([make_record(0)])[0]; rec["response"]["answers"] = {"odd\ud800": {"type": "noul", "noul": 0.2}}
    ins = Inspector(); ins.show_decision(rec, _text("odd\ud800"))
    assert "80" in inspector_texts(ins)  # the bars of that question: no 80


def test_a_fixture_with_a_lone_surrogate_is_still_valid_json(app, tmp_path, make_record):
    ins = Inspector(); ins.show_decision(numbered([make_record(0, state={"note": "bad \ud800 text"})])[0], "go")
    ins.save_fixture(str(tmp_path / "f.json"))
    assert json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))["request"]["state"]["note"] == "bad \ud800 text"


# ---- the look

def failed(make_record, i, **how):
    return {**make_record(i), "seq": i, "response": None, "status": None, "error": None, **how}


def test_a_failed_call_is_one_row_that_says_why(make_record):
    tags = [rows_from(failed(make_record, 0, **how))[0].answer for how in (
        {"status": 429, "error": {"message": "slow down"}}, {"error": {"jev": "timeout"}}, {"error": {"jev": "network"}},
        {"status": 200, "error": {"proxy": "tarnlight proxy: the answer from TypeSafe was cut off (ClientPayloadError)"}},
        {"error": {"proxy": "tarnlight proxy: TypeSafe could not be reached (ClientConnectorError)"}}, {"status": 401, "error": {}})]
    assert tags == ["429", "timeout", "network", "cut off", "unreachable", "401"]
    assert [r.answer for r in rows_from(failed(make_record, 0, status=200))] == ["no answer"]  # a 200 without answers failed too
    ok = {**make_record(0), "seq": 0}; ok["response"]["answers"] = {"q": {"type": "choice", "confidence": 1.7}}
    assert rows_from(ok) == []  # it answered, with nothing that counts as a decision: no row, and not a failure


def test_the_list_names_a_call_once_and_words_its_numbers(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json"))
    rec = {**make_record(0), "seq": 0, "label": "nightly"}; rec["response"]["answers"]["go"]["noul"] = 0.69
    m.add([rec, failed(make_record, 1, status=429, error={"message": "slow down"})])
    col = lambda c: [m.data(m.index(i, c)) for i in range(m.rowCount())]
    assert col(1) == ["proxy · test", "proxy · test · nightly", "", ""]  # newest first; a call's source on its first row only
    assert [bool(t) for t in col(0)] == [bool(t) for t in col(6)] == [True, True, False, False]
    assert col(3) == ["429", "1.2 · medium", "approve", "yes"] and col(5) == ["", "50", "70", "69"] and col(7) == ["", "", "", ""]
    assert (feed.percent(85.5), feed.percent(100.0), feed.percent(None)) == ("85.5", "100", "")
    m.set_filter(text="conf<60")
    assert shown(m) == [(0, "size")]  # a failed call has no confidence to compare
    m.bands.set("size", 60, 80); m.set_filter(preset="review", text="")
    assert shown(m) == [(0, "size")]  # nor is it in the review queue


def test_only_rows_below_escalate_get_the_red_edge(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json")); m.bands.set("go", 60, 80)
    recs = [{**make_record(i), "seq": i} for i in range(2)]
    recs[0]["response"]["answers"]["go"]["noul"] = 0.5; recs[1]["response"]["answers"]["go"]["noul"] = 0.9  # 50 and 90
    m.add(recs + [failed(make_record, 2, status=429, error={})])
    view = QTableView(); view.setModel(m); view.setItemDelegate(feed.TextDelegate(view)); view.setItemDelegateForColumn(feed.DIST, DistributionDelegate(view))
    view.resize(1000, 400); view.show()
    img = view.viewport().grab().toImage()
    def edge(i):
        rect = view.visualRect(m.index(i, 0)); return img.pixelColor(rect.x() + 1, rect.center().y()).name()
    edges = {(m.row_at(i).seq, m.row_at(i).question): edge(i) for i in range(m.rowCount())}
    red = QColor(feed.RED).name()
    assert edges[(0, "go")] == red and edges[(1, "go")] != red and edges[(2, "")] != red


def test_the_inspector_reads_top_down(app, make_record):
    rec = numbered([make_record(0)])[0]; rec["response"]["answers"]["verdict"]["confidence"] = 0.35
    ins = Inspector(); ins.show_decision(rec, "verdict", bands=(40, 70))
    texts = inspector_texts(ins)
    assert texts[:3] == ["approve", "confidence 35%", "approve"] and "Approve, reject or defer?" in texts and "your bands: escalate < 40 · review < 70" in texts
    assert ins.layout_.itemAt(1).widget().styleSheet().startswith(f"color: {feed.RED}")  # 35 is below escalate
    assert texts.index("Approve, reject or defer?") < texts.index("go") < texts.index("size") < next(i for i, t in enumerate(texts) if "request req_" in t)
    failed = {**rec, "response": None, "status": 429, "error": {"message": "slow down"}}
    ins.show_decision(failed, "")
    texts = inspector_texts(ins)
    assert texts[:2] == ["429", "A failed call: no answer, nothing to grade."] and any("slow down" in t for t in texts)


def test_the_caught_up_line_and_replays(app, tmp_path, make_record):
    m = FeedModel(Bands(tmp_path / "b.json"))
    recs = [{**make_record(i), "seq": i, "ts": 1000.0 + i} for i in range(3)]; recs[2]["source"] = "replay"
    m.add(recs)  # on screen: #2 (3 rows), #1 (3), #0 (3)
    assert not any(m.line_above(i) for i in range(m.rowCount()))  # nothing caught up yet
    m.caught_up_seq = 1  # #0 and #1 came from while the console was closed
    assert [i for i in range(m.rowCount()) if m.line_above(i)] == [3]  # between #2 and #1's top row
    assert m.data(m.index(0, 1), Qt.ItemDataRole.ForegroundRole).name() == QColor(feed.BLUE).name()
    assert m.data(m.index(3, 1), Qt.ItemDataRole.ForegroundRole).name() != QColor(feed.BLUE).name()


def test_the_inspector_lists_a_dozen_other_questions_then_counts(app, make_record):
    from PySide6.QtWidgets import QLabel
    rec = numbered([make_record(0)])[0]
    rec["response"]["answers"] = {f"q{i:02d}": {"type": "noul", "noul": 0.9} for i in range(16)}
    ins = Inspector(); ins.show_decision(rec, "q00")
    texts = inspector_texts(ins)
    assert "q12" in texts and "q13" not in texts and "+3 more questions" in texts
    ins.show_decision(numbered([make_record(1)])[0], "verdict")  # the event loop has not run: the old rows are only hidden
    shown_now = {w.text() for w in ins.content.findChildren(QLabel) if w.isVisibleTo(ins)}
    assert "q05" not in shown_now and "Approve, reject or defer?" in shown_now


def test_a_failed_call_says_why_on_the_live_path_too(tmp_path, make_record):
    from tarnlight.ingest import Ingest
    from tarnlight.storage import JevLog
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0)
    try:
        for how in ({"status": None, "error": {"jev": "timeout"}},
                    {"status": 200, "error": {"proxy": "tarnlight proxy: the answer from TypeSafe was cut off (ClientPayloadError)"}},
                    {"status": 429, "error": {"message": "slow down"}}, {"status": 200, "error": None}):
            ing._store({**make_record(0), "response": None, **how})
        ing._store(make_record(1))
        ring = ing.since(-1)  # what the window reads: the error body is not in it
        assert all("error" not in r for r in ring)
        assert [row.answer for r in ring for row in rows_from(r) if row.qtype == "error"] == ["timeout", "cut off", "429", "no answer"]
        assert "failed" not in ring[-1]  # an answered call carries no tag
    finally:
        log.close()


def test_an_integer_past_float_range_is_shown_not_raised(app, make_record):
    rec = numbered([make_record(0)])[0]; rec["response"]["answers"]["size"]["score"] = 10 ** 400
    assert {r.question: r.segments for r in rows_from(rec)}["size"] == ()  # an empty bar
    ins = Inspector(); ins.show_decision(rec, "go")
    assert any(t.startswith("1000000") for t in inspector_texts(ins))  # the other row shows the number as sent
