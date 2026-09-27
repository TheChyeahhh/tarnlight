"""The window's edge cases: each test is one case that once went wrong."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json, sqlite3, sys, threading
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from tarnlight import feed as feed_module
from tarnlight.bands import Bands
from tarnlight.model import DecisionModel
from tarnlight.storage import JevLog, _text
from tarnlight.ui import ConsoleWindow, SeqAxis
from test_window import FakeIngest, app, choice, console, feed, keys, shown  # noqa: F401 (fixtures)


def only(make_record, i, answers):
    r = make_record(i); r["response"]["answers"] = answers; return r


def noul(p): return {"type": "noul", "noul": p}


@pytest.fixture
def small_console(app, tmp_path, monkeypatch, request):
    """A console whose feed holds only 12 rows (four calls of three decisions), or as many as the test's param says."""
    monkeypatch.setattr(feed_module, "MAX_ROWS", getattr(request, "param", 12))
    log = JevLog(tmp_path / "s.jevlog"); ingest = FakeIngest(log)
    win = ConsoleWindow(DecisionModel(bands=Bands(tmp_path / "bands.json")), ingest, log); win.timer.stop()
    win.resize(1440, 900); win.show(); win.activateWindow(); app.processEvents()
    yield win, ingest, log
    win.close(); log.close()


def top_of_selected(win): return win.table.visualRect(win.table.currentIndex()).top()


# ---- the feed while rows arrive

def test_a_selected_row_at_the_bottom_stays_on_screen(console, make_record, app):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(40)])
    win.select_row(win.feed.rowCount() - 1); app.processEvents()  # the oldest row, the table scrolled to its end
    before, chosen = top_of_selected(win), win.selected_row()
    for i in range(40, 46):
        feed(win, ingest, [make_record(i)]); app.processEvents()
        assert win.selected_row() == chosen and top_of_selected(win) == before


def test_a_selected_row_that_leaves_the_feed_is_let_go(small_console, make_record, app):
    win, ingest, log = small_console; feed(win, ingest, [make_record(i) for i in range(4)])
    win.select_row(win.feed.rowCount() - 1)  # the oldest, (0, 'go')
    feed(win, ingest, [make_record(4)])
    assert win.selected_row() is None and "left the feed" in win.s_message.text()
    keys(win, Qt.Key.Key_1)
    assert log.outcomes() == {}  # the grade key does not land on a row Qt picked instead


@pytest.mark.parametrize("small_console", [60], indirect=True)  # more rows than the table shows, so it can scroll
def test_trimming_does_not_move_a_selected_row(small_console, make_record, app):
    win, ingest, _ = small_console; feed(win, ingest, [make_record(i) for i in range(20)])
    win.select_row(2); app.processEvents(); before, chosen = top_of_selected(win), win.selected_row()
    feed(win, ingest, [make_record(20)]); app.processEvents()  # three rows in at the top, three out at the bottom
    assert win.selected_row() == chosen and win.table.currentIndex().row() == 5 and win.feed.rowCount() == 60
    assert top_of_selected(win) == before


def test_grading_a_decision_above_escalate_leaves_the_queue_alone(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, 0, 0.5), choice(make_record, 1, 0.3)])
    win.select_row(1); keys(win, Qt.Key.Key_1)  # the 50 % one: not below escalate
    assert win.model.below() == (1, 1) and win.review_button.text() == "Review queue  1"


def test_a_pause_longer_than_the_ring_is_reported(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.pause.click()
    ingest.put([make_record(i) for i in range(3, 10)]); del ingest.ring[:6]  # the ring kept only the newest (#6 on)
    win.pause.click(); win.refresh(force=True)
    assert win.feed_missed == 3 and "3 not in the feed" in win.s_packet.text()


def test_pause_shows_the_same_newest_decision_in_chart_and_feed(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(i) for i in range(4)])
    ingest.put([make_record(4)]); win.tick()  # one tick: the chart has #4, the feed not yet
    win.pause.click()
    assert win.frozen_seq == 4 and win.feed.row_at(0).seq == 4
    win.select_decision(4, "go")
    assert win.selected_row().seq == 4


# ---- the review queue and grading

def test_the_review_queue_says_when_the_rest_are_older_than_the_feed(small_console, make_record):
    win, ingest, _ = small_console
    feed(win, ingest, [only(make_record, i, {"q": choice(make_record, i, 0.3)["response"]["answers"]["pick"]}) for i in range(20)])
    assert win.review_button.text() == "Review queue  20" and win.feed.rowCount() == 12
    win.review_button.click()
    for _ in range(12): keys(win, Qt.Key.Key_1)
    assert win.review_button.text() == "Review queue  8" and "8 more below escalate are older than the feed holds" in win.s_message.text()


def test_one_grade_draws_the_inspector_once(console, make_record, monkeypatch):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, i, 0.3) for i in range(5)])
    win.review_button.click()
    drawn = []; real = win.inspector.show_decision
    monkeypatch.setattr(win.inspector, "show_decision", lambda *a, **k: (drawn.append(a[1]), real(*a, **k)))
    keys(win, Qt.Key.Key_1)
    assert len(drawn) == 1 and win.inspector.record["seq"] == 3


def test_the_inspector_empties_when_the_last_row_is_graded(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, 0, 0.3)])
    win.review_button.click(); keys(win, Qt.Key.Key_1)
    assert win.selected_row() is None and win.inspector.record is None


def test_a_grade_the_database_refuses_is_reported(console, make_record):
    win, ingest, log = console; feed(win, ingest, [make_record(0)]); log._flush()
    other = sqlite3.connect(log.path); other.execute("BEGIN IMMEDIATE")
    try:
        win.select_row(0); keys(win, Qt.Key.Key_1)
    finally:
        other.rollback(); other.close()
    assert win.s_message.text().startswith("Grade not saved") and win.feed.data(win.feed.index(0, 7)) == ""


def test_t_on_a_band_edge_escalates_everywhere(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, 0, 0.57)])
    win.select_row(0); keys(win, Qt.Key.Key_T)
    assert win.model.bands["pick"] == (58, 70) and win.model.below() == (1, 1) and win.review_button.text() == "Review queue  1"
    assert win.feed.data(win.feed.index(0, 5), Qt.ItemDataRole.ForegroundRole).name() == "#ff5c5c" and win.gauge.value == 57.0


def test_t_on_a_certain_decision_says_it_cannot(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [only(make_record, 0, {"go": noul(1.0)})])
    win.select_row(0); keys(win, Qt.Key.Key_T)
    assert win.model.bands["go"] == (40, 70) and "cannot be escalated" in win.s_message.text()


def test_t_on_an_odd_name_moves_the_band_the_chart_shows(console, make_record):
    name = "odd" + chr(0xD800)
    win, ingest, _ = console; feed(win, ingest, [only(make_record, 0, {name: noul(0.6)})])
    win.select_row(0); keys(win, Qt.Key.Key_T)
    assert win.question == _text(name) and win.esc_box.value() == 61 and win.model.below() == (1, 1)


# ---- bands and the charted question

def test_this_question_follows_the_chart_when_it_switches_by_itself(console, make_record):
    win, ingest, _ = console
    win.set_preset("question")  # picked before any data
    feed(win, ingest, [only(make_record, i, {"alpha": noul(0.9)}) for i in range(3)])
    assert win.question == "alpha" and {q for _, q in shown(win)} == {"alpha"}
    feed(win, ingest, [only(make_record, i, {"beta": noul(0.9)}) for i in range(3, 12)])
    assert win.question == "beta" and {q for _, q in shown(win)} == {"beta"}


def test_the_chart_holds_its_question_while_a_band_is_typed(console, make_record, app):
    win, ingest, _ = console; feed(win, ingest, [only(make_record, i, {"alpha": noul(0.9)}) for i in range(3)])
    win.esc_box.setFocus(); app.processEvents()
    win.esc_box.selectAll(); QTest.keyClicks(win.esc_box, "55")
    feed(win, ingest, [only(make_record, i, {"beta": noul(0.9)}) for i in range(3, 12)])
    assert win.question == "alpha"  # no switch under the edit
    QTest.keyClick(win.esc_box, Qt.Key.Key_Return)
    feed(win, ingest, [only(make_record, 12, {"beta": noul(0.9)})])
    assert win.question == "beta"


def test_escape_in_a_band_box_keeps_the_band(console, make_record, app, tmp_path):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, 0, 0.5)])
    win.esc_box.setFocus(); app.processEvents()
    win.esc_box.selectAll(); QTest.keyClicks(win.esc_box, "12")
    QTest.keyClick(win.esc_box, Qt.Key.Key_Escape); app.processEvents()
    assert win.model.bands["pick"] == (40, 70) and win.esc_box.value() == 40 and win.esc_box.text() == "40"
    assert app.focusWidget() is win.table and not (tmp_path / "bands.json").exists()


def test_a_band_box_follows_a_chip_click_even_with_focus(console, make_record, app):
    win, ingest, _ = console
    feed(win, ingest, [only(make_record, i, {"alpha": noul(0.9), "beta": noul(0.9)}) for i in range(3)])
    win.model.bands.set("beta", 25, 80); win.set_question("alpha")
    win.esc_box.setFocus(); app.processEvents()
    next(b for b in win.chip_group.buttons() if b.question == "beta").click()
    assert win.esc_box.value() == 25 and win.rev_box.value() == 80


def test_a_dragged_line_is_not_pulled_back_by_new_data(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [choice(make_record, 0, 0.5)])
    win.escalate_line.moving = True; win.escalate_line.setValue(58.1)  # held by the mouse
    feed(win, ingest, [choice(make_record, 1, 0.6)]); win.refresh(force=True)
    assert win.escalate_line.value() == 58.1 and win.question == "pick"
    win.escalate_line.moving = False; win.escalate_line.sigPositionChangeFinished.emit(win.escalate_line)
    assert win.model.bands["pick"] == (58, 70)


# ---- chips, focus, status

def test_chips_never_take_the_keyboard(console, make_record, app):
    win, ingest, log = console; feed(win, ingest, [make_record(i) for i in range(3)])
    win.select_row(0)
    QTest.mouseClick(win.chip_group.buttons()[1], Qt.MouseButton.LeftButton)
    assert app.focusWidget() is win.table
    feed(win, ingest, [only(make_record, 3, {"new": noul(0.9)})])  # the chips are rebuilt
    assert all(b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in win.chip_group.buttons())


def test_chips_that_do_not_fit_go_behind_more(console, sample_records, app):
    from PySide6.QtGui import QFontMetrics
    win, ingest, _ = console; feed(win, ingest, sample_records)
    for width in (1440, 1280, 1100):
        win.resize(width, 800); app.processEvents()
        visible = [b for b in win.chip_group.buttons() if b.isVisibleTo(win)]
        assert all(b.width() >= QFontMetrics(b.font()).horizontalAdvance(b.text()) for b in visible)  # none cut
        assert len(visible) + len(win.chip_row.hidden) == len(win.model.series)
        assert all(b.geometry().right() <= win.chip_row.width() for b in visible + [win.more])
    win.set_question(win.chip_row.hidden[0])
    assert win.more.isChecked() and win.chip_row.hidden[0][:6] in win.more.text()


def test_the_status_bar_counts_refused_datagrams(console):
    win, ingest, _ = console
    ingest.counts["rejected"] = 3; win.refresh(force=True)
    assert "3 refused" in win.s_packet.text()
    win.model.missed, win.feed_missed = 12345, 67890; win.refresh(force=True)  # every note at once: shortened, never wider
    assert "refused" in win.s_packet.toolTip() and win.minimumSizeHint().width() <= 1440 and win.width() <= 1440
    ingest.counts["rejected"] = 0


def test_small_things(console):
    win, _, _ = console
    assert win.timer.timerType() == Qt.TimerType.PreciseTimer
    assert SeqAxis("bottom").tickStrings([-3.0, 0.0, 2.0, 2.5], 1, 1) == ["", "#0", "#2", ""]


def test_a_decision_the_log_does_not_know_is_reported(console, make_record, monkeypatch):
    win, ingest, log = console; feed(win, ingest, [make_record(0)])
    def unknown(*a): raise KeyError("no decision")
    monkeypatch.setattr(log, "grade", unknown)  # e.g. a record whose index rows could not be written
    win.select_row(0); keys(win, Qt.Key.Key_1)
    assert "cannot be graded" in win.s_message.text() and win.feed.data(win.feed.index(0, 7)) == ""


def test_the_feed_header_and_messages_never_set_the_minimum_width(console, app):
    win, _, _ = console
    win.say("x" * 300); app.processEvents()
    assert win.minimumSizeHint().width() <= 1120  # about 1,107 px, set by the chart header and the fixed right column


# ---- the proxy in the window

class FakeProxy:
    port, thread = 7338, threading.main_thread()
    def __init__(self): self.counts = {"not recorded": 0}


def proxied_console(app, tmp_path, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True); log = JevLog(tmp_path / "s.jevlog")
    win = ConsoleWindow(DecisionModel(bands=Bands(tmp_path / "bands.json")), FakeIngest(log), log, **kw); win.timer.stop()
    win.resize(1440, 900); win.show(); app.processEvents(); return win, log


def test_the_status_bar_shows_the_proxy(app, tmp_path):
    p = FakeProxy(); win, log = proxied_console(app, tmp_path, proxy=p)
    try:
        assert win.s_proxy.text() == "proxy :7338" and win.s_proxy_dot.color == "#3ecf8e" and not hasattr(win, "banner")
        p.counts["not recorded"] = 2; win.refresh(force=True)
        assert "2 Jev calls forwarded but not recorded" in win.s_packet.toolTip() + win.s_packet.text()
    finally:
        win.close(); log.close()


def test_a_proxy_that_could_not_start_is_loud_when_calls_point_at_it(app, tmp_path):
    win, log = proxied_console(app, tmp_path, proxy_problem="TCP port 7338 is in use.", base_urls=["http://127.0.0.1:7338"])
    try:
        assert not win.banner.isHidden() and "fail until it runs" in win.banner.text() and win.s_proxy.text() == "proxy off"
        assert win.s_proxy_dot.color == "#ff5c5c" and "in use" in win.s_proxy.toolTip()
    finally:
        win.close(); log.close()
    win, log = proxied_console(app, tmp_path / "b", proxy_problem="TCP port 7338 is in use.", base_urls=["https://api.typesafe.ai"])
    try:
        assert not hasattr(win, "banner")  # calls do not go through it: the status bar is enough
    finally:
        win.close(); log.close()


def test_the_banner_compares_ports_not_text():
    from tarnlight.ui import stranded_by
    running = FakeProxy()  # on 7338
    assert not stranded_by("http://127.0.0.1:7338", running) and not stranded_by("http://localhost:7338/p/my-app", running)
    assert stranded_by("http://localhost:7338", None) and stranded_by("http://127.0.0.1:7400", running)
    assert not stranded_by("https://api.typesafe.ai", None) and not stranded_by("http://[bad", None)


def test_the_console_finds_a_url_left_in_claude_code_settings(monkeypatch, tmp_path):
    from tarnlight import __main__ as cli, install as inst
    settings = tmp_path / "settings.json"; settings.write_text('{"env": {"TYPESAFE_BASE_URL": "http://127.0.0.1:7338"}}', encoding="utf-8")
    monkeypatch.setattr(inst, "SETTINGS", settings); monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    monkeypatch.setattr(inst.sys, "platform", "test")  # no registry
    assert cli.configured_urls() == ["http://127.0.0.1:7338"]


# ---- replay this one, through the proxy

@pytest.fixture
def replaying(app, tmp_path, monkeypatch):
    """A console with a real proxy in front of a fake TypeSafe, and a fake key in the environment."""
    from test_proxy import FakeTypeSafe
    from tarnlight.proxy import Proxy
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_FAKE_REPLAY_KEY_42")
    up = FakeTypeSafe(); log = JevLog(tmp_path / "s.jevlog"); ingest = FakeIngest(log)
    p = Proxy(port=0, upstream=up.url, sink=("127.0.0.1", 9)).start()  # the replayed decision is sent nowhere here
    win = ConsoleWindow(DecisionModel(bands=Bands(tmp_path / "bands.json")), ingest, log, proxy=p); win.timer.stop()
    win.resize(1440, 900); win.show(); app.processEvents()
    yield win, ingest, log, up
    win.close(); p.stop(); up.close(); log.close()


def wait_for(win, job):
    getattr(win, job).join(10); win.refresh()


def test_replay_sends_the_same_call_through_the_proxy(replaying, make_record):
    win, ingest, log, up = replaying
    rec = make_record(0); rec["project"] = "ticket triage"; feed(win, ingest, [rec])
    win.select_row(0)
    assert win.inspector.replay.isEnabled()
    win.inspector.replay.click(); wait_for(win, "replay_job")
    method, path, headers, body = up.seen[0]
    assert (method, path, json.loads(body)) == ("POST", "/v1/systemone", rec["request"])
    assert headers["Authorization"] == "Bearer ts_FAKE_REPLAY_KEY_42" and headers["X-Tarnlight-Label"] == "#0"
    assert "TypeSafe answered 200" in win.s_message.text() and "ts_FAKE" not in win.s_message.text()
    assert "ts_FAKE_REPLAY_KEY_42" not in log.path.read_bytes().decode("latin-1")


def test_replay_needs_a_key_and_a_full_state(replaying, make_record, monkeypatch):
    win, ingest, log, up = replaying; feed(win, ingest, [make_record(0)])
    win.select_row(0)
    monkeypatch.delenv("TYPESAFE_API_KEY"); monkeypatch.setattr(sys.modules["tarnlight.ui"], "api_key", lambda: None)
    win.inspector.replay.click()
    assert "needs TYPESAFE_API_KEY" in win.s_message.text() and not up.seen
    from tarnlight.storage import Privacy
    monkeypatch.setattr(sys.modules["tarnlight.ui"], "api_key", lambda: "ts_FAKE_REPLAY_KEY_42")
    win.replay_record(Privacy("hash").apply({**make_record(1), "seq": 1}))  # a session that keeps states hashed
    assert "cannot be replayed" in win.s_message.text() and not up.seen


def test_replay_is_off_without_a_proxy(console, make_record):
    win, ingest, _ = console; feed(win, ingest, [make_record(0)])
    win.select_row(0)
    assert not win.inspector.replay.isEnabled()


# ---- the drop box in the window

class FakeInbox:
    def __init__(self, folder): self.folder, self.counts, self.seq = folder, {"imported": 0, "waiting bytes": 0, "at start": None}, None
    def caught_up(self): return None if self.counts["at start"] is None or self.seq is None else (self.counts["at start"], self.seq)


def test_the_window_tells_about_the_drop_box(app, tmp_path):
    box = FakeInbox(tmp_path / "inbox")
    win, log = proxied_console(app, tmp_path / "w", inbox=box)
    try:
        win.refresh(force=True)
        assert "drop box off (tarnlight install)" in win.s_packet.text() + win.s_packet.toolTip()
        box.folder.mkdir(); box.counts["waiting bytes"] = 500; win.refresh(force=True)
        assert "catching up from the drop box" in win.s_packet.text() + win.s_packet.toolTip()
        box.counts.update({"waiting bytes": 0, "at start": 12}); win.refresh(force=True)
        assert "Caught up" not in win.s_message.text()  # read, not all stored yet
        box.seq = 41; win.refresh(force=True)
        assert "Caught up: 12 calls from while Tarnlight was closed (below the blue line)." == win.s_message.text()
        assert win.feed.caught_up_seq == 41
        win.say("something else"); win.refresh(force=True)
        assert win.s_message.text() == "something else"  # said once, not on every refresh
    finally:
        win.close(); log.close()


def test_a_failed_call_cannot_be_graded_or_escalated(console, make_record):
    win, ingest, _ = console
    feed(win, ingest, [{**make_record(0), "response": None, "status": 429, "error": {"message": "slow down"}}])
    win.select_row(0); keys(win, Qt.Key.Key_1)
    assert win.s_message.text() == "A failed call has no answer to grade." and win.feed.data(win.feed.index(0, 7)) == ""
    keys(win, Qt.Key.Key_T)
    assert win.s_message.text() == "A failed call has no confidence to escalate at." and not win.table.grab().isNull()


def test_a_minimised_window_keeps_count_but_draws_nothing(console, make_record, monkeypatch):
    win, ingest, _ = console
    win.show(); win.showMinimized()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents(); assert win.isMinimized()
    drawn, real = [], win.refresh
    monkeypatch.setattr(win, "refresh", lambda *a, **k: (drawn.append(k.get("force", False)), real(*a, **k)))
    feed(win, ingest, [make_record(i) for i in range(3)])
    assert drawn == [] and win.model.calls == 3  # counted, not drawn
    win.showNormal(); QApplication.processEvents()
    assert drawn == [True] and win.gauge.value is not None  # drawn once, on the way back


def test_the_empty_screen_offers_the_demo_in_its_own_process(console, monkeypatch):
    import subprocess, sys
    win = console[0]
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: started.append(args))
    assert win.demo_button.isVisibleTo(win) and win.chart_stack.currentIndex() == 0
    win.demo_button.click()
    assert started == [[sys.executable, "-m", "tarnlight", "demo"]] and "second window" in win.s_message.text()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    win.demo_button.click()
    assert started[-1] == [sys.executable, "demo"]  # the packaged app starts itself with the demo command
