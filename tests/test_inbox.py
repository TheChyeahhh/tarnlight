"""The drop box: callers leave records in a folder; the console imports them into its log when it is open."""
import json, os, time
from datetime import datetime, timedelta, timezone
import pytest
from tarnlight import inbox as inbox_module, ingest as ingest_module
from tarnlight.inbox import Inbox, hour_over
from tarnlight.ingest import Ingest
from tarnlight.storage import JevLog

NOW = datetime(2026, 9, 26, 19, 30, tzinfo=timezone.utc)
HOUR = "my-app-2026-09-26T19.jsonl"


def jev_record(i, **kw):
    """What a Jev hook leaves in the drop box: one JSON line per call."""
    return {"v": 1, "ts": 1790450000.0 + i, "source": "my-app", "session_id": "s1", "label": "nightly", "sdk": "node",
            "request_id": f"req_{i}", "latency_ms": 200 + i, "status": 200,
            "request": {"model": "jev-latest", "state": {"text": f"item {i}"}, "questions": {"needs_follow_up": {"type": "noul", "instructions": "?"}}},
            "response": {"model": "jev-1.13.0", "answers": {"needs_follow_up": {"type": "noul", "noul": 0.2}}, "usage": {"input_tokens": 500}},
            "error": None, **kw}


def lines(*recs): return "".join(json.dumps(r) + "\n" for r in recs)


@pytest.fixture
def world(tmp_path):
    folder = tmp_path / "inbox"; folder.mkdir()
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0)  # its writer only: nothing is sent over UDP here
    ing.writer.start()
    yield folder, log, ing
    ing.stopping.set(); ing.writer.join(); log.close()


def stored(log, ing, n, wait=5):
    end = time.monotonic() + wait
    while ing.counts["stored"] < n and time.monotonic() < end: time.sleep(0.01)
    log._flush(); return [r for _, r in log.iter_records()]


def test_what_arrived_while_closed_is_imported_once(world):
    folder, log, ing = world
    (folder / HOUR).write_text(lines(*(jev_record(i) for i in range(5))), encoding="utf-8")
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 5
    recs = stored(log, ing, 5)
    assert [r["request_id"] for r in recs] == [f"req_{i}" for i in range(5)] and recs[0]["source"] == "my-app"
    assert recs[0]["cost_est_micro"] == 21 and recs[0]["label"] == "nightly"  # 500 tokens at $0.042 per million
    box.poll(NOW)  # the next look saves how far the stored lines go
    assert Inbox(ing, folder).poll(NOW) == 0  # a console opened later starts where this one stopped
    assert json.loads((folder / ".positions.json").read_text(encoding="utf-8"))


def test_a_line_still_being_written_waits_for_its_newline(world):
    folder, log, ing = world
    f = folder / HOUR
    f.write_text(lines(jev_record(0)) + json.dumps(jev_record(1))[:40], encoding="utf-8")
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 1
    with open(f, "a", encoding="utf-8") as out: out.write(json.dumps(jev_record(1))[40:] + "\n")
    assert box.poll(NOW) == 1 and [r["request_id"] for r in stored(log, ing, 2)] == ["req_0", "req_1"]


def test_a_bad_line_is_refused_and_the_rest_still_come_in(world):
    folder, log, ing = world
    (folder / HOUR).write_text(lines(jev_record(0)) + "not json\n" + json.dumps({"v": 2}) + "\n" + lines(jev_record(1)), encoding="utf-8")
    Inbox(ing, folder).poll(NOW)
    assert [r["request_id"] for r in stored(log, ing, 2)] == ["req_0", "req_1"] and ing.counts["rejected"] == 2
    assert "drop box line is not JSON" in ing.rejects.read_text(encoding="utf-8")


def test_a_busy_writer_leaves_lines_for_later(world, monkeypatch):
    folder, log, ing = world
    (folder / HOUR).write_text(lines(*(jev_record(i) for i in range(3))), encoding="utf-8")
    monkeypatch.setattr(ingest_module, "MAX_BACKLOG_BYTES", 0)  # the writer counts as behind
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 0 and box.counts["waiting bytes"] > 0 and ing.counts["rejected"] == 0  # kept, not refused
    monkeypatch.setattr(ingest_module, "MAX_BACKLOG_BYTES", 64 << 20)
    assert box.poll(NOW) == 3 and len(stored(log, ing, 3)) == 3


def test_finished_hours_are_deleted_and_the_current_one_kept(world):
    folder, log, ing = world
    old, current, odd = folder / "my-app-2026-09-26T17.jsonl", folder / HOUR, folder / "my-app.jsonl"
    for i, f in enumerate((old, current, odd)): f.write_text(lines(jev_record(i)), encoding="utf-8")
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 3  # a file is deleted only once stored: see the killed-console test
    stored(log, ing, 3); box.poll(NOW)
    assert not old.exists() and current.exists() and odd.exists()  # a name without an hour is never deleted
    box.poll(NOW + timedelta(hours=2))
    assert not current.exists()
    assert set(json.loads((folder / ".positions.json").read_text(encoding="utf-8"))) == {f"my-app.jsonl:{os.stat(odd).st_ino}"}


def test_a_file_made_again_under_the_same_name_is_read_from_the_start(world):
    folder, log, ing = world
    f = folder / HOUR
    f.write_text(lines(jev_record(0), jev_record(1)), encoding="utf-8")
    box = Inbox(ing, folder); box.poll(NOW)
    f.unlink(); f.write_text(lines(jev_record(2)), encoding="utf-8")  # shorter than the old position
    assert box.poll(NOW) == 1 and [r["request_id"] for r in stored(log, ing, 3)] == ["req_0", "req_1", "req_2"]


def test_no_folder_no_work(tmp_path):
    box = Inbox(ingest=None, folder=tmp_path / "missing")
    assert box.poll(NOW) == 0 and not (tmp_path / "missing").exists()


def test_the_thread_counts_what_came_in_while_the_console_was_closed(world):
    folder, log, ing = world
    (folder / HOUR).write_text(lines(jev_record(0), jev_record(1)), encoding="utf-8")
    box = Inbox(ing, folder).start()
    try:
        end = time.monotonic() + 5
        while box.counts["at start"] is None and time.monotonic() < end: time.sleep(0.01)
        with open(folder / HOUR, "a", encoding="utf-8") as out: out.write(lines(jev_record(2)))
        while box.counts["imported"] < 3 and time.monotonic() < end: time.sleep(0.01)
    finally:
        box.stop()
    assert box.counts["at start"] == 2 and box.counts["imported"] == 3


def test_hours_are_read_from_the_name():
    assert hour_over("my-app-2026-09-26T17.jsonl", NOW) and not hour_over(HOUR, NOW)
    assert not hour_over("my-app-2026-09-26T18.jsonl", NOW.replace(minute=2))  # grace after the hour ends
    assert not hour_over("whatever.jsonl", NOW) and not hour_over("x-2026-13-40T99.jsonl", NOW)


# ---- edge cases

@pytest.fixture
def idle(tmp_path):
    """An ingest whose writer never runs: what the drop box hands over stays queued, as in a console killed mid-import."""
    log = JevLog(tmp_path / "killed.jevlog"); yield Ingest(log, port=0); log.close()


def test_a_console_killed_while_catching_up_loses_nothing(world, idle):
    folder, log, ing = world
    old = folder / "my-app-2026-09-26T17.jsonl"
    old.write_text(lines(*(jev_record(i) for i in range(4))), encoding="utf-8")
    killed = Inbox(idle, folder)
    assert killed.poll(NOW) == 4 and killed.poll(NOW) == 0  # handed over, never stored
    assert killed.counts["at start"] == 4 and killed.caught_up() is None  # read, but not caught up until stored
    assert old.exists() and not (folder / ".positions.json").exists()  # so nothing is marked as done
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 4 and [r["request_id"] for r in stored(log, ing, 4)] == [f"req_{i}" for i in range(4)]
    box.poll(NOW); assert not old.exists()


def test_a_normal_close_waits_for_the_writer_and_leaves_no_duplicates(world):
    folder, log, ing = world
    (folder / HOUR).write_text(lines(*(jev_record(i) for i in range(3))), encoding="utf-8")
    box = Inbox(ing, folder); box.poll(NOW); box.stop()
    assert ing.handled == 3 and Inbox(ing, folder).poll(NOW) == 0


def test_one_line_that_breaks_is_refused_and_the_rest_come_in(world, monkeypatch):
    folder, log, ing = world
    huge = jev_record(1); huge["response"]["usage"]["input_tokens"] = 10 ** 400  # no cost can be made from it
    (folder / HOUR).write_text(lines(jev_record(0), huge, jev_record(2), jev_record(3)), encoding="utf-8")
    real = ing.offer
    monkeypatch.setattr(ing, "offer", lambda msg, data: (_ for _ in ()).throw(RuntimeError()) if msg["request_id"] == "req_2" else real(msg, data))
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 3 and box.counts["waiting bytes"] == 0
    assert [r["request_id"] for r in stored(log, ing, 3)] == ["req_0", "req_1", "req_3"]
    assert "drop box line not taken: RuntimeError" in ing.rejects.read_text(encoding="utf-8")


def test_a_file_that_cannot_be_read_does_not_stop_the_others(world, monkeypatch):
    folder, log, ing = world
    for name, i in (("a-app-2026-09-26T19.jsonl", 0), (HOUR, 1)): (folder / name).write_text(lines(jev_record(i)), encoding="utf-8")
    real = Inbox._read
    def locked(self, key, path, pos, size):
        if path.name.startswith("a-app"): raise PermissionError("locked")
        return real(self, key, path, pos, size)
    monkeypatch.setattr(Inbox, "_read", locked)
    assert Inbox(ing, folder).poll(NOW) == 1 and [r["request_id"] for r in stored(log, ing, 1)] == ["req_1"]


def test_a_torn_write_neither_glues_records_nor_blocks_a_finished_hour(world):
    folder, log, ing = world
    old = folder / "my-app-2026-09-26T17.jsonl"
    torn = json.dumps(jev_record(0))[:30]
    old.write_bytes((torn + "\0\0\0" + json.dumps(jev_record(1)) + "\n" + json.dumps(jev_record(2))[:25]).encode())
    box = Inbox(ing, folder)
    assert box.poll(NOW) == 1 and box.counts["waiting bytes"] == 0  # the half line at the end of a finished hour is given up
    assert [r["request_id"] for r in stored(log, ing, 1)] == ["req_1"]
    reasons = ing.rejects.read_text(encoding="utf-8")
    assert "drop box line is not JSON" in reasons and "an unfinished last line of 25 bytes" in reasons
    box.poll(NOW); assert not old.exists()
    live = folder / HOUR; live.write_text(json.dumps(jev_record(3))[:25], encoding="utf-8")
    assert box.poll(NOW) == 0 and box.counts["waiting bytes"] == 25  # the current hour's half line may still be finished


def test_long_lines(world, monkeypatch):
    folder, log, ing = world
    monkeypatch.setattr(inbox_module, "READ_BYTES", 64); monkeypatch.setattr(inbox_module, "MAX_LINE", 2000)
    long = jev_record(0); long["request"]["state"]["text"] = "x" * 1000  # longer than a look, shorter than a record may be
    too_long = jev_record(1); too_long["request"]["state"]["text"] = "x" * 3000
    f = folder / HOUR; f.write_text(lines(long, too_long, jev_record(2)), encoding="utf-8")
    box = Inbox(ing, folder)
    for _ in range(40): box.poll(NOW)  # 64 bytes a look
    assert [r["request_id"] for r in stored(log, ing, 2)] == ["req_0", "req_2"] and box.counts["waiting bytes"] == 0
    assert "drop box line of 3," in ing.rejects.read_text(encoding="utf-8")
    with open(f, "a", encoding="utf-8") as out: out.write(json.dumps(long)[:900])  # a long line still being written
    box.poll(NOW); assert box.long  # how far it was scanned is kept, so the next look reads only what is new
    with open(f, "a", encoding="utf-8") as out: out.write(json.dumps(long)[900:] + "\n")
    assert box.poll(NOW) == 1 and not box.long


def test_caught_up_counts_every_look_until_nothing_waits_and_names_the_last_seq(world, monkeypatch):
    folder, log, ing = world
    monkeypatch.setattr(inbox_module, "READ_BYTES", 1500)  # about two lines a look
    (folder / HOUR).write_text(lines(*(jev_record(i) for i in range(6))), encoding="utf-8")
    box = Inbox(ing, folder)
    box.poll(NOW); assert box.counts["at start"] is None
    while box.counts["at start"] is None: box.poll(NOW)
    assert box.counts["at start"] == 6
    stored(log, ing, 6)
    assert box.caught_up() == (6, 5)  # six calls, the last stored as #5


def test_the_ingest_marks_where_a_catch_up_ends(tmp_path):
    log = JevLog(tmp_path / "m.jevlog"); ing = Ingest(log, port=0)
    try:
        for i in range(3): ing.offer(jev_record(i), b"x")
        ing.mark(2); assert ing.marked_seq is None  # the writer has not handled two yet
        ing.writer.start(); stored(log, ing, 3)
        assert ing.marked_seq == 1  # the second record handled was stored as #1
        ing.mark(1); assert ing.marked_seq == 2  # already handled: the newest stored
    finally:
        ing.stopping.set(); ing.writer.join(); log.close()


def test_a_file_that_cannot_be_looked_at_once_keeps_its_place(world, monkeypatch):
    folder, log, ing = world
    (folder / HOUR).write_text(lines(jev_record(0), jev_record(1)), encoding="utf-8")
    box = Inbox(ing, folder); box.poll(NOW); stored(log, ing, 2); box.poll(NOW)
    real = type(folder).stat
    monkeypatch.setattr(type(folder), "stat", lambda self, **kw: (_ for _ in ()).throw(PermissionError()) if self.name == HOUR else real(self, **kw))
    box.poll(NOW)  # the file is there but cannot be looked at
    monkeypatch.undo()
    assert box.poll(NOW) == 0 and len(stored(log, ing, 2)) == 2  # not read again from the start
