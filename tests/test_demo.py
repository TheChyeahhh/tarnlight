"""The made-up records behind `tarnlight demo` and the tests, and the demo command itself."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import collections, json, threading, time
import pytest
from tarnlight import demo
from tarnlight.feed import rows_from
from tarnlight.ingest import Ingest, validate
from tarnlight.storage import JevLog, summarize

WIRE_KEYS = {"noul": ["type", "noul"], "choice": ["type", "choice", "probabilities", "confidence"],
             "score": ["type", "score", "legend", "probabilities", "confidence"]}


def numbered(recs): return [{**r, "seq": i} for i, r in enumerate(recs)]


def test_the_same_seed_gives_the_same_records():
    recs = demo.records()
    assert recs == demo.records() and recs != demo.records(seed=8) and len(recs) == 600 and len(demo.records(n=40)) == 40
    assert [r["ts"] for r in recs] == sorted(r["ts"] for r in recs)
    assert {r["source"] for r in recs} == {"mod-queue", "ticket-triage", "review-bot"}
    assert all(r["label"] == "demo" for r in recs)  # so a feed, an export or a replay of it says it is made up
    assert all(row.source.endswith(" · demo") for r in numbered(recs[:40]) for row in rows_from(r))
    assert {r["sdk"] for r in recs} == {"python/0.7.1", "node/0.6.0"} and all("cost_est_micro" not in r for r in recs)


def test_every_record_passes_the_door_in_the_wire_shape_and_is_fully_indexed():
    names, low, near_ties = collections.Counter(), 0, 0
    for rec in numbered(demo.records()):
        assert validate(rec) is None
        if rec["response"] is None: continue
        answers = rec["response"]["answers"]
        assert list(answers) == list(rec["request"]["questions"]) and isinstance(rec["response"]["usage"]["input_tokens"], int)
        for a in answers.values():
            assert list(a) == WIRE_KEYS[a["type"]]
            if a["type"] == "choice": near_ties += a["probabilities"][a["choice"]] < max(a["probabilities"].values())
        rows = summarize(rec)[1]
        assert [q for _, q, *_ in rows] == list(answers) and all(0 <= row[3] <= 1000 for row in rows)  # every answer indexed
        names.update(list(answers)); low += sum(row[3] < 400 for row in rows)
    (busiest, n), (_, second) = names.most_common(2)
    assert len(names) >= 7 and busiest == "policy" and n > 1.5 * second  # more chips than fit, and one clear default
    assert low > 50 and 3 <= near_ties <= 10  # below the default escalate of 40; Jev's pick is not the likeliest


def test_failed_calls_are_failed_call_rows_in_the_feed():
    recs = numbered(demo.records())
    failed = [r for r in recs if r["response"] is None]
    assert len(failed) == 18  # 3 %
    tags = collections.Counter(row.answer for r in failed for row in rows_from(r) if row.qtype == "error")
    assert tags == {"429": 8, "timeout": 6, "unreachable": 4}
    assert all(r["request_id"] for r in failed if r["status"] == 429) and not any(r["request_id"] for r in failed if r["status"] is None)


def test_priced_fills_the_cost_as_the_drop_box_does(tmp_path):
    from tarnlight.inbox import Inbox
    class Catch:  # stands in for the ingest: the drop box hands it each parsed line
        def __init__(self): self.got = []
        def offer(self, msg, data): self.got.append(msg); return "queued"
    box = Inbox(Catch(), folder=tmp_path)
    recs = demo.records(n=60)
    for r in recs: box._take_one(json.dumps(r).encode())
    assert [demo.priced(r) for r in recs] == box.ingest.got
    assert all(demo.priced(r)["cost_est_micro"] > 0 for r in recs if r["response"])


def test_play_sends_live_times_and_stops_at_once(tmp_path):
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0).start(); stop = threading.Event(); sent = []
    feeder = threading.Thread(target=lambda: sent.append(demo.play(("127.0.0.1", ing.port), speed=100, stop=stop)))
    try:
        t0 = time.time(); feeder.start()
        end = time.monotonic() + 10
        while ing.counts["stored"] < 10 and time.monotonic() < end: time.sleep(0.01)
        stop.set(); t = time.monotonic(); feeder.join(5)
        assert not feeder.is_alive() and time.monotonic() - t < 0.5
        end = time.monotonic() + 5
        while ing.counts["stored"] < sent[0] and time.monotonic() < end: time.sleep(0.01)
        ring = ing.snapshot()
        assert len(ring) == sent[0] >= 10 and all(t0 - 1 < r["ts"] < time.time() + 1 for r in ring)  # as if happening now
        assert all(r["cost_est_micro"] for r in ring if r["response"])
    finally:
        stop.set(); ing.stop(); log.close()
    with pytest.raises(ValueError): demo.play(("127.0.0.1", 9), speed=0)


def test_the_demo_saves_nothing_and_uses_no_drop_box_or_proxy(tmp_path, monkeypatch):
    import tempfile, types
    from pathlib import Path
    from PySide6.QtWidgets import QApplication, QLabel
    from tarnlight import __main__ as cli, inbox, proxy, ui
    from tarnlight.bands import Bands
    app = QApplication.instance() or QApplication([])
    home, temp = tmp_path / "home", tmp_path / "temp"; home.mkdir(); temp.mkdir()
    monkeypatch.setattr(cli.Path, "home", lambda: home)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(Bands.__init__, "__defaults__", (home / ".tarnlight" / "bands.json",))  # the defaults were read at import
    monkeypatch.setattr(cli.session_path, "__defaults__", (home / ".tarnlight" / "sessions",))
    def never(*a, **kw): raise AssertionError("the demo must not start this")
    monkeypatch.setattr(inbox.Inbox, "__init__", never); monkeypatch.setattr(proxy.Proxy, "__init__", never)
    with cli.demo_console(speed=100) as win:
        folder = win.log.path.parent
        assert folder.parent == temp and win.proxy is None and win.inbox is None
        assert "made-up" in win.windowTitle() and any(w.text() == "DEMO · made-up data" for w in win.findChildren(QLabel))
        end = time.monotonic() + 10
        while win.ingest.counts["stored"] < 5 and time.monotonic() < end: time.sleep(0.01)
        win.tick(); app.processEvents()
        assert win.model.calls >= 5
        win.model.bands.set("policy", 30, 60)
        assert (folder / "bands.json").is_file()
        offered = []  # an export is offered outside the folder that is deleted on close
        monkeypatch.setattr(ui, "QFileDialog", types.SimpleNamespace(getSaveFileName=lambda *a: (offered.append(Path(a[2])), ("", ""))[1]))
        win.ask_export("csv")
        assert offered[0].name == "tarnlight-demo.csv" and folder not in offered[0].parents and temp not in offered[0].parents
        win.timer.stop(); win.close()
    assert not folder.exists() and not any(home.iterdir()) and not any(temp.iterdir())
    with pytest.raises(SystemExit): cli.main(["demo", "--speed", "0"])
