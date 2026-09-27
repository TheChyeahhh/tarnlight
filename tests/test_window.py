"""The window: the feed, the inspector, grading keys, the review queue, bands and export."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv, json, threading, time, zipfile
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tarnlight import ingest
from tarnlight.bands import Bands
from tarnlight.model import DecisionModel
from tarnlight.storage import JevLog
from tarnlight.ui import ConsoleWindow, load_fonts


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    load_fonts()
    return a


class FakeIngest:
    """Stores each record in the real log (so the inspector and grading find it) and hands the window the same slim
    ring entry the real ingest would: no request, no error, only the answer fields."""
    port, counts = 7337, {"rejected": 0}
    def __init__(self, log): self.log, self.ring, self.listener = log, [], threading.main_thread()
    def since(self, seq): return [r for r in self.ring if r["seq"] > seq]
    def put(self, recs):
        for r in recs:
            entry = {k: v for k, v in r.items() if k in ingest.RING_FIELDS}
            entry["response"] = ingest._slim(r.get("response")); entry["seq"] = self.log.append(r)
            self.ring.append(entry)


@pytest.fixture
def console(app, tmp_path):
    log = JevLog(tmp_path / "s.jevlog"); ingest = FakeIngest(log)
    win = ConsoleWindow(DecisionModel(bands=Bands(tmp_path / "bands.json")), ingest, log); win.timer.stop()
    win.resize(1440, 900); win.show(); win.activateWindow(); app.processEvents()
    yield win, ingest, log
    win.close(); log.close()


def feed(win, ingest, recs):
    ingest.put(recs)
    for _ in range(4): win.tick()  # the feed takes new rows every fourth tick


def choice(make_record, i, conf):
    """A record with one choice question 'pick' at the given confidence."""
    r = make_record(i)
    r["response"]["answers"] = {"pick": {"type": "choice", "choice": "a", "confidence": conf, "probabilities": {"a": conf, "b": 1 - conf}}}
    return r


def keys(win, *ks):
    for k in ks: QTest.keyClick(win.table, k)


def shown(win): return [(win.feed.row_at(i).seq, win.feed.row_at(i).question) for i in range(win.feed.rowCount())]


def test_the_feed_shows_every_decision_newest_first(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(4)])
    assert win.feed.rowCount() == 12 and win.feed.row_at(0).seq == 3 and win.feed.row_at(11).seq == 0


def test_selecting_a_row_opens_the_full_record_in_the_inspector(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.select_row(0)
    assert win.inspector.record["seq"] == 2 and win.inspector.record["request"]["state"]["tick"] == 2  # from the log, not the ring
    texts = " ".join(w.text() for w in win.inspector.content.findChildren(type(win.inspector.empty)))
    assert "changed since #1" in texts and win.inspect_where.text().endswith("#2")
    win.select_row(-1)
    assert win.inspector.record is None


def test_grading_keys_save_the_grade_and_move_on(console, make_record):
    win, ingest, log = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.select_row(0); first, second = win.feed.row_at(0), win.feed.row_at(1)
    keys(win, Qt.Key.Key_1, Qt.Key.Key_2)
    assert log.outcomes() == {(first.seq, first.question): "correct", (second.seq, second.question): "wrong"}
    assert win.feed.data(win.feed.index(0, 7)) == "correct" and win.selected_row() == win.feed.row_at(2)
    keys(win, Qt.Key.Key_3)
    assert log.outcomes()[(win.feed.row_at(2).seq, win.feed.row_at(2).question)] == "flagged"
    win.select_row(0); keys(win, Qt.Key.Key_2)
    assert log.outcomes()[(first.seq, first.question)] == "wrong"  # a new grade replaces the old one


def test_typing_in_the_filter_never_grades(console, make_record):
    win, ingest, log = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.select_row(0); keys(win, Qt.Key.Key_Slash)
    assert win.filter_box.hasFocus()
    QTest.keyClicks(win.filter_box, "verdict 1"); win.filter_wait.timeout.emit()
    assert log.outcomes() == {} and win.filter_box.text() == "verdict 1" and not win.paused


def test_the_filter_box_filters_after_a_pause_in_typing(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.filter_box.setText("size")
    assert win.feed.rowCount() == 9  # not yet: it waits for typing to stop
    win.filter_wait.timeout.emit()
    assert {q for _, q in shown(win)} == {"size"}


def test_the_review_queue(console, make_record):
    win, ingest, log = console
    feed(win, ingest, [choice(make_record, i, c) for i, c in enumerate((0.3, 0.9, 0.2, 0.35))])
    assert win.review_button.text() == "Review queue  3"
    win.review_button.click()
    assert shown(win) == [(3, "pick"), (2, "pick"), (0, "pick")] and win.selected_row().seq == 3
    keys(win, Qt.Key.Key_2)
    assert shown(win) == [(2, "pick"), (0, "pick")] and win.selected_row().seq == 2  # graded rows leave; the next is selected
    assert win.review_button.text() == "Review queue  2" and win.model.below() == (3, 2)
    keys(win, Qt.Key.Key_1, Qt.Key.Key_1)
    assert shown(win) == [] and win.review_button.text() == "Review queue" and win.selected_row() is None


def test_editing_bands_saves_them_and_recounts(console, make_record, tmp_path):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, i, c) for i, c in enumerate((0.3, 0.5, 0.65))])
    assert win.question == "pick" and win.model.below() == (1, 1)
    repainted = []; win.feed.dataChanged.connect(lambda a, b: repainted.append((a.row(), a.column(), b.row(), b.column())))
    win.esc_box.setValue(60)
    assert repainted == [(0, 5, 2, 5)]  # the confidence colors are redrawn at once
    assert json.loads((tmp_path / "bands.json").read_text(encoding="utf-8")) == {"pick": [60, 70]}
    assert win.model.below() == (2, 2) and win.escalate_line.value() == 60 and win.y_axis.marks[0] == (60, "#ff5c5c")
    assert win.feed.data(win.feed.index(1, 5), Qt.ItemDataRole.ForegroundRole).name() == "#ff5c5c"  # 50 is red now
    win.rev_box.setValue(55)  # below escalate: escalate follows it down
    assert win.model.bands["pick"] == (55, 55) and win.auto_label.text() == "auto ≥ 55"
    assert Bands(tmp_path / "bands.json")["pick"] == (55, 55)  # a new session reads them back


def test_dragging_a_line_sets_its_band(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, 0, 0.5)])
    win.escalate_line.setValue(54.6); win.escalate_line.sigPositionChangeFinished.emit(win.escalate_line)
    assert win.model.bands["pick"] == (55, 70) and win.esc_box.value() == 55
    win.review_line.setValue(130); win.review_line.sigPositionChangeFinished.emit(win.review_line)
    assert win.model.bands["pick"] == (55, 100) and win.review_line.value() == 100  # clamped, and the line snaps back to it


def test_t_escalates_just_above_the_selected_decision(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, i, c) for i, c in enumerate((0.3, 0.623, 0.9))])
    win.select_row(1)  # 62.3
    keys(win, Qt.Key.Key_T)
    assert win.model.bands["pick"] == (63, 70) and win.model.below() == (2, 2) and "63" in win.s_message.text()
    win.select_row(0); keys(win, Qt.Key.Key_T)  # 90: review has to move up with it
    assert win.model.bands["pick"] == (91, 91)


def test_space_pauses_and_the_feed_holds_still_until_resume(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(2)])
    win.select_row(0); keys(win, Qt.Key.Key_Space)
    assert win.paused
    feed(win, ingest, [make_record(i) for i in range(2, 5)])
    assert win.feed.rowCount() == 6 and win.model.calls == 5  # counted, but the rows stay put while grading
    keys(win, Qt.Key.Key_Space)
    assert not win.paused and win.feed.rowCount() == 15 and win.feed.row_at(0).seq == 4


def test_clicking_the_chart_selects_the_decision_even_when_filtered_out(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(5)])
    win.set_question("verdict"); win.set_preset("graded")
    assert win.feed.rowCount() == 0
    win.select_decision(3, "verdict")
    row = win.selected_row()
    assert (row.seq, row.question) == (3, "verdict") and win.inspector.record["seq"] == 3
    assert win.feed.preset == "all" and win.preset_group.checkedButton().preset == "all"
    win.select_decision(99, "verdict")
    assert "not in the feed" in win.s_message.text()


def test_a_click_on_the_plot_picks_the_nearest_point(console, make_record, app):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(20)])
    win.set_question("go"); app.processEvents()
    vb = win.plot.getPlotItem().vb
    from PySide6.QtCore import QPointF
    target = vb.mapViewToScene(QPointF(12.2, 50))
    QTest.mouseClick(win.plot.viewport(), Qt.MouseButton.LeftButton, pos=win.plot.mapFromScene(target))
    assert (win.selected_row().seq, win.selected_row().question) == (12, "go")


def test_this_question_follows_the_chart(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.set_question("go"); win.set_preset("question")
    assert {q for _, q in shown(win)} == {"go"}
    win.set_question("size")
    assert {q for _, q in shown(win)} == {"size"}


def test_the_feed_shows_only_decisions_the_index_holds(console, make_record):
    win, ingest, log = console
    r = make_record(0); r["response"]["answers"] = {"odd": {"type": "choice", "choice": "a", "confidence": 0.8, "probabilities": {"a": "0.8", "b": "0.2"}},
                                                    "go": {"type": "noul", "noul": 0.9}}
    feed(win, ingest, [r])
    assert shown(win) == [(0, "go")]  # the odd one is stored, but neither indexed nor charted, so it is not a decision to grade


@pytest.mark.parametrize("kind", ["jsonl", "csv", "bundle"])
def test_export_runs_in_the_background_and_reports(console, make_record, tmp_path, kind):
    win, ingest, log = console; feed(win, ingest, [make_record(i) for i in range(5)])
    win.select_row(0); keys(win, Qt.Key.Key_1)
    out = tmp_path / f"out.{kind}"
    win.export_to(kind, str(out))
    win.export_job.join(10); win.refresh()
    assert win.export_job is None and win.s_message.text().startswith("Exported to")
    if kind == "jsonl": assert [json.loads(l)["seq"] for l in out.read_text(encoding="utf-8").splitlines()] == list(range(5))
    if kind == "csv": assert sum(1 for r in csv.DictReader(out.open(encoding="utf-8")) if r["outcome"] == "correct") == 1
    if kind == "bundle": assert zipfile.is_zipfile(out)


def test_a_failed_export_says_so(console, make_record, tmp_path):
    win, ingest, _ = console; feed(win, ingest, [make_record(0)])
    win.export_to("jsonl", str(tmp_path / "no-such-folder" / "out.jsonl"))
    win.export_job.join(10); win.refresh()
    assert win.s_message.text().startswith("Export failed")


def test_status_messages_fade(console, monkeypatch):
    win, _, _ = console
    win.say("hello"); assert win.s_message.text() == "hello"
    later = time.monotonic() + 10; monkeypatch.setattr(time, "monotonic", lambda: later)
    win.refresh(); assert win.s_message.text() == ""


def test_the_feed_updates_five_times_a_second_not_on_every_tick(console, make_record):
    win, ingest, _ = console
    ingest.put([make_record(0)])
    for _ in range(3): win.tick()
    assert win.model.calls == 1 and win.feed.rowCount() == 0
    win.tick()
    assert win.feed.rowCount() == 3


def test_a_selected_row_stays_put_while_rows_arrive(console, make_record, app):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(40)])
    win.select_row(5); app.processEvents()
    before = win.table.visualRect(win.table.currentIndex()).top(); chosen = win.selected_row()
    feed(win, ingest, [make_record(i) for i in range(40, 42)]); app.processEvents()
    assert win.selected_row() == chosen and win.table.currentIndex().row() == 11  # six new rows above it
    assert win.table.visualRect(win.table.currentIndex()).top() == before


def test_the_grading_keys_work_as_soon_as_the_window_opens(console, make_record, app):
    win, ingest, log = console; feed(win, ingest, [make_record(0)])
    win.select_row(0); app.processEvents()
    QTest.keyClick(app.focusWidget() or win, Qt.Key.Key_1)  # whatever has the keyboard, as a person would type
    assert log.outcomes() == {(0, win.feed.row_at(0).question): "correct"} and app.focusWidget() is win.table


def test_the_band_boxes_are_never_reached_by_the_keyboard_alone(console, make_record, app):
    win, ingest, log = console; feed(win, ingest, [make_record(0)])
    win.focus_filter(); QTest.keyClick(win.filter_box, Qt.Key.Key_Backtab)  # Shift+Tab out of the filter
    assert app.focusWidget() not in (win.esc_box, win.rev_box)
    win.select_row(0); QTest.keyClick(app.focusWidget() or win, Qt.Key.Key_1)
    assert win.model.bands[win.question] == (40, 70) and len(log.outcomes()) == 1  # the key graded, it did not type a band
