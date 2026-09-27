import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
import threading
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QToolButton
from tarnlight.model import DecisionModel
from tarnlight.storage import JevLog
from tarnlight.ui import ConsoleWindow, load_fonts


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    load_fonts()
    return a


class FakeIngest:
    """Stands in for the UDP ingest: the window only calls since(), reads port and asks if the listener is alive."""
    port, counts = 7337, {"rejected": 0}
    def __init__(self): self.ring, self.listener = [], threading.main_thread()
    def since(self, seq): return [r for r in self.ring if r["seq"] > seq]


@pytest.fixture
def console(app, tmp_path):
    log, ingest = JevLog(tmp_path / "s.jevlog"), FakeIngest()
    win = ConsoleWindow(DecisionModel(), ingest, log); win.timer.stop()  # the tests drive tick() themselves
    win.resize(1440, 900); win.show(); app.processEvents()
    yield win, ingest
    win.close(); log.close()


def feed(win, ingest, recs):
    start = win.model.last_seq + 1
    ingest.ring += [{**r, "seq": start + i} for i, r in enumerate(recs)]
    win.tick()


def plotted(win):
    x, y = win.conf_curve.getOriginalDataset()
    return list(x), list(y)


def test_it_waits_for_decisions(console):
    win, _ = console
    assert win.chart_stack.currentIndex() == 0 and win.gauge.value is None and win.question is None
    assert win.c_rate.value.text().startswith("0")


def test_it_charts_the_most_frequent_question(console, sample_records):
    win, ingest = console; feed(win, ingest, sample_records)
    q = win.model.default_question()
    assert win.question == q and win.chart_stack.currentIndex() == 1
    seq, _, conf, _, _ = win.model.series[q].last(240)
    assert plotted(win) == (list(seq), list(conf))
    assert win.gauge.value == conf[-1] and win.gauge_where.text() == f"{q} · #{int(seq[-1])}"


def test_a_question_the_user_picks_stays_picked(console, sample_records, make_record):
    win, ingest = console; feed(win, ingest, sample_records)
    other = next(b for b in win.chip_group.buttons() if b.question != win.question)
    other.click()
    assert win.question == other.question and [b.isChecked() for b in win.chip_group.buttons()].count(True) == 1
    feed(win, ingest, [make_record(i) for i in range(50)])  # new, busier questions arrive
    assert win.question == other.question and win.model.default_question() != other.question


def test_the_range_chips_set_how_many_points_are_drawn(console, sample_records):
    win, ingest = console; feed(win, ingest, sample_records)
    win.set_question("policy")  # the busiest question in the sample records: 309 points
    counts = {}
    for b in win.range_group.buttons():
        b.click(); counts[b.text()] = len(plotted(win)[0])
    assert counts == {"last 240": 240, "1k": 309, "5k": 309, "all": 309}


def test_pause_freezes_the_view_but_keeps_counting(console, sample_records, make_record):
    win, ingest = console; feed(win, ingest, sample_records[:300])
    win.pause.click()
    before, calls = plotted(win), win.model.calls
    feed(win, ingest, sample_records[300:])
    assert plotted(win) == before and win.model.calls == calls + len(sample_records) - 300
    win.pause.click()
    assert plotted(win) != before and win.pause.text() == "Pause"


def test_counters_show_the_model(console, sample_records):
    win, ingest = console; feed(win, ingest, sample_records)
    m = win.model
    assert win.c_rate.value.text().startswith(f"{m.requests_per_min():,} ")
    assert win.c_below.detail.text() == f"{m.below()[0]:,} of {m.decisions:,}"
    assert win.c_latency.value.text() == f"{m.latency_stats()[0]:.0f} ms"


def test_questions_beyond_four_sit_behind_a_more_chip(console, sample_records):
    win, ingest = console; feed(win, ingest, sample_records)
    more = next(w for w in win.findChildren(QToolButton) if w.text().endswith("more"))
    more.menu().aboutToShow.emit()  # the menu is filled when it opens
    shown = [b for b in win.chip_group.buttons() if b.isVisibleTo(win)]
    assert len(win.chip_group.buttons()) == 4 and len(more.menu().actions()) == len(win.model.series) - len(shown) > 0
    assert [a.text().split("  (")[0] for a in more.menu().actions()] == win.model.questions()[len(shown):]


def test_the_window_reads_the_real_ingest(app, tmp_path, make_record):
    """End to end on the real listener: UDP in, stored, slim ring entries out, drawn."""
    import socket, time
    from tarnlight.ingest import Ingest, send
    log = JevLog(tmp_path / "s.jevlog"); ingest = Ingest(log, port=0).start()
    win = ConsoleWindow(DecisionModel(), ingest, log); win.timer.stop()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(50): send(make_record(i), s, ("127.0.0.1", ingest.port))
        s.close()
        end = time.monotonic() + 10
        while ingest.counts["stored"] < 50 and time.monotonic() < end: time.sleep(0.01)
        win.tick()
        assert win.model.calls == 50 and win.question in {"go", "verdict", "size"}
        assert len(plotted(win)[0]) == 50 and win.gauge.value is not None
    finally:
        win.close(); ingest.stop(); log.close()


def test_the_streak_pill_shows_only_when_below_escalate(console, make_record):
    win, ingest = console
    low = make_record(0); low["response"]["answers"] = {"go": {"type": "noul", "noul": 0.5}}  # confidence 50
    feed(win, ingest, [low])
    assert win.streak.isHidden()
    assert win.streak.sizePolicy().retainSizeWhenHidden()  # showing it does not shift the panels
    win.model.bands["go"] = (60, 80); win.refresh(force=True)
    assert not win.streak.isHidden() and ">1<" in win.streak.text()


# ---- layout edge cases

def with_answers(make_record, i, answers):
    r = make_record(i); r["response"]["answers"] = answers; return r


def test_long_names_never_widen_the_window(app, tmp_path, make_record):
    log, ingest = JevLog(tmp_path / "s.jevlog"), FakeIngest()
    win = ConsoleWindow(DecisionModel(), ingest, log); win.timer.stop()
    try:
        recs = []
        for i in range(6):
            r = with_answers(make_record, i, {f"question_{i}_" + "x" * 200: {"type": "noul", "noul": 0.3}})
            r["source"], r["project"] = f"source_{i}_" + "s" * 200, "p" * 100; recs.append(r)
        feed(win, ingest, recs)
        win.resize(1440, 900); win.show(); app.processEvents()
        assert win.minimumSizeHint().width() <= 1440 and win.width() <= 1440
        assert all(len(b.text()) < 40 and b.toolTip() == b.question for b in win.chip_group.buttons())
        from PySide6.QtGui import QFontMetrics
        for width in (1440, 1100):
            win.resize(width, 900); app.processEvents()
            more = win.sources_row.more
            shown = [w for w in win.findChildren(QLabel) if w.toolTip().startswith("source_") and w.isVisibleTo(win) and w is not more]
            hidden = int(more.text()[1:]) if more.isVisibleTo(win) else 0
            assert len(shown) + hidden == 6 and len(more.toolTip().splitlines()) == hidden  # every sender is on a chip or in '+N'
            assert all(w.width() >= QFontMetrics(w.font()).horizontalAdvance(w.text()) for w in shown)  # shortened, never cut off
            assert all(w.parentWidget().geometry().right() <= win.sources_row.width() for w in shown)
            assert len(shown) >= (1 if width == 1440 else 0)
    finally:
        win.close(); log.close()


def test_picking_a_question_from_the_more_menu_shows_on_that_chip(console, sample_records):
    win, ingest = console; feed(win, ingest, sample_records)
    more = next(w for w in win.findChildren(QToolButton) if w.text().endswith("more"))
    more.menu().aboutToShow.emit(); action = more.menu().actions()[-1]; q = action.text().split("  (")[0]
    action.trigger()
    assert win.question == q and more.isChecked() and q[:8] in more.text()
    assert not any(b.isChecked() for b in win.chip_group.buttons())
    win.chip_group.buttons()[0].click()
    assert not more.isChecked() and more.text().endswith("more")


def test_one_or_two_points_are_drawn_as_dots(console, make_record):
    win, ingest = console
    feed(win, ingest, [make_record(0)])
    assert win.conf_curve.opts["symbol"] == "o" and len(plotted(win)[0]) == 1
    feed(win, ingest, [make_record(i) for i in range(1, 3)])
    assert win.conf_curve.opts["symbol"] is None


def test_the_chart_ends_at_now_even_for_a_quiet_question(console, make_record):
    win, ingest = console
    feed(win, ingest, [with_answers(make_record, 0, {"quiet": {"type": "noul", "noul": 0.2}})])
    win.set_question("quiet")
    feed(win, ingest, [with_answers(make_record, i, {"busy": {"type": "noul", "noul": 0.2}}) for i in range(1, 40)])
    lo, hi = win.plot.getPlotItem().viewRange()[0]
    assert hi >= win.model.last_seq == 39 and lo <= 0 and plotted(win)[0] == [0]


def test_the_second_line_and_the_x_axis_can_be_switched(console, make_record):
    win, ingest = console; feed(win, ingest, [make_record(i) for i in range(20)])
    win.set_question("go")
    assert win.second_button.text() == "p(yes)"
    seq, ts, conf, p_yes, margin = win.model.series["go"].last(240)
    assert list(win.second_curve.getOriginalDataset()[1]) == list(p_yes)
    win.second_button.click()
    assert win.second_button.text() == "margin" and list(win.second_curve.getOriginalDataset()[1]) == list(margin)
    win.x_button.click()
    assert list(plotted(win)[0]) == list(ts) and win.x_axis.tickStrings([ts[0]], 1, 1)[0].count(":") == 2
    win.x_button.click(); win.set_question("verdict")
    assert list(plotted(win)[0]) == list(win.model.series["verdict"].seq)
    assert win.second_button.text() == "margin"


def test_value_tags_follow_the_newest_point(console, make_record):
    win, ingest = console; feed(win, ingest, [make_record(i) for i in range(5)])
    win.set_question("verdict")
    (conf_value, label, _), (top_value, _, _) = win.y_axis.tags
    assert (conf_value, label) == (70.0, "70.0") and top_value == 80.0
    assert win.y_axis.marks == [(40, "#ff5c5c"), (70, "#f2a93b")]


def test_pause_freezes_only_the_chart_and_gauge(console, make_record):
    win, ingest = console; feed(win, ingest, [make_record(i) for i in range(10)])
    win.pause.click()
    chart, gauge, rate = plotted(win), win.gauge_where.text(), win.c_rate.value.text()
    feed(win, ingest, [make_record(i) for i in range(10, 30)])
    assert plotted(win) == chart and win.gauge_where.text() == gauge
    assert win.c_rate.value.text() != rate and win.pause.text() == "Resume"
    win.range_group.buttons()[1].click()  # changing the range while paused still shows the frozen data only
    assert max(plotted(win)[0]) == 9


def test_failing_calls_are_explained_while_waiting(console, make_record):
    win, ingest = console
    bad = make_record(0); bad["status"], bad["response"] = 500, None
    feed(win, ingest, [bad])
    assert win.chart_stack.currentIndex() == 0 and "1 call so far" in win.waiting_hint.text() and "500" in win.waiting_hint.text()


def test_money_is_never_shown_as_zero_when_spent():
    from tarnlight.ui import money
    assert (money(0), money(0.00001), money(0.0012), money(3.456)) == ("$0.0000", "< $0.0001", "$0.0012", "$3.46")


def test_dense_charts_draw_with_thin_lines(console, sample_records):
    win, ingest = console; feed(win, ingest, sample_records)
    win.set_question("policy")
    assert win.conf_curve.opts["pen"].widthF() == 2.2
    win.plot.resize(200, 300); win.set_range(None)  # 309 points on 200 pixels
    assert win.conf_curve.opts["pen"].widthF() == 1 and win.second_curve.opts["pen"].widthF() == 1


def test_the_gauge_shows_whole_percents_and_plain_words(app):
    from tarnlight.ui import Gauge
    g = Gauge(); g.resize(320, 200)
    assert g.number() == "--" and "not whether it is right" in g.toolTip()
    g.show_value(17.0, (40, 70), "says no · p(yes) 0.17"); assert g.number() == "17%" and not g.grab().isNull()
    g.show_value(85.5, (40, 70), "x" * 400); assert g.number() == "85.5%" and not g.grab().isNull()  # a long line is cut, not spilled
