import csv, json, zipfile
import pytest
import zstandard as zstd
from tarnlight import storage
from tarnlight.bands import Bands, DEFAULT
from tarnlight.storage import JevLog, Privacy


@pytest.fixture(autouse=True)
def no_timed_flush(monkeypatch):
    monkeypatch.setattr(storage, "FLUSH_S", 1e9)


def filled(tmp_path, recs, **kw):
    log = JevLog(tmp_path / "s.jevlog", **kw)
    for r in recs: log.append(r)
    return log


# ---- grading

def test_grade_regrade_and_clear(tmp_path, make_record):
    log = filled(tmp_path, [make_record(i) for i in range(3)])
    log.grade(1, "verdict", "wrong", note="approved without the receipt")
    assert log.outcomes() == {(1, "verdict"): "wrong"}
    log.grade(1, "verdict", "correct")
    assert log.outcomes() == {(1, "verdict"): "correct"}
    log.grade(1, "verdict", None)
    assert log.outcomes() == {}


def test_a_decision_still_in_the_hot_file_can_be_graded(tmp_path, make_record):
    log = filled(tmp_path, [make_record(0)])
    assert log.buf  # shown in the window, not in a block yet
    log.grade(0, "go", "flagged")
    log.close()
    assert JevLog(tmp_path / "s.jevlog").outcomes() == {(0, "go"): "flagged"}  # and it survives a reopen


def test_grading_refuses_unknown_decisions_and_outcomes(tmp_path, make_record):
    log = filled(tmp_path, [make_record(0)])
    with pytest.raises(KeyError): log.grade(0, "nope", "correct")
    with pytest.raises(KeyError): log.grade(9, "go", "correct")
    with pytest.raises(ValueError): log.grade(0, "go", "maybe")


# ---- inspector lookups

def test_record_comes_from_the_buffer_or_a_block(tmp_path, make_record):
    log = filled(tmp_path, [make_record(i) for i in range(3)])
    assert log.record(2)["request_id"] == "req_00000002"  # buffered
    log._flush()
    assert log.record(0)["request_id"] == "req_00000000" and log.record(7) is None  # from a block


def test_previous_call_from_the_same_sender(tmp_path, make_record):
    recs = [make_record(i) for i in range(6)]
    for i in (1, 3): recs[i]["project"] = "other"
    log = filled(tmp_path, recs[:4]); log._flush()
    for r in recs[4:]: log.append(r)
    assert log.previous_seq(5, "proxy", "test") == 4   # both in the buffer
    assert log.previous_seq(4, "proxy", "test") == 2   # found in the committed call table
    assert log.previous_seq(3, "proxy", "other") == 1 and log.previous_seq(0, "proxy", "test") is None


# ---- exports

def test_csv_has_one_row_per_decision_with_outcomes(tmp_path, make_record):
    err = make_record(2); err.update(status=400, response=None, error={"detail": "bad"})
    log = filled(tmp_path, [make_record(0), make_record(1), err])
    log.grade(1, "go", "correct", note="ok")
    log.export_csv(tmp_path / "out.csv")
    rows = list(csv.DictReader(open(tmp_path / "out.csv", encoding="utf-8")))
    assert len(rows) == 3 + 3 + 1  # three questions per answered call, one row for the failed call
    graded = [r for r in rows if r["outcome"]]
    assert [(r["seq"], r["question"], r["outcome"], r["note"]) for r in graded] == [("1", "go", "correct", "ok")]
    verdict = next(r for r in rows if r["seq"] == "0" and r["question"] == "verdict")
    assert (verdict["confidence"], verdict["chosen"], verdict["status"]) == ("700", "approve", "200")
    failed = next(r for r in rows if r["seq"] == "2")
    assert failed["question"] == "" and failed["status"] == "400"


def test_bundle_is_a_compacted_log_with_outcomes_through_the_privacy_filter(tmp_path, make_record):
    log = filled(tmp_path, [make_record(i, state={"email": f"user{i}@example.com"}) for i in range(5)])
    log.grade(3, "size", "wrong")
    log.export_bundle(tmp_path / "share.zip", privacy=Privacy("hash"))
    out = tmp_path / "unzipped"
    with zipfile.ZipFile(tmp_path / "share.zip") as z:
        assert sorted(z.namelist()) == ["README.txt", "session.jevlog"]
        z.extractall(out)
    copy = JevLog(out / "session.jevlog")
    assert copy.privacy.mode == "hash" and copy.outcomes() == {(3, "size"): "wrong"}
    blob = (out / "session.jevlog").read_bytes() + b"".join(
        zstd.ZstdDecompressor().decompress(b) for (b,) in copy.con.execute("SELECT body FROM block"))
    assert b"@example.com" not in blob and len(list(copy.iter_records())) == 5
    assert {level for (level,) in copy.con.execute("SELECT level FROM block")} == {storage.COMPACT_LEVEL}
    copy.close()


# ---- bands

def test_bands_default_save_and_reload(tmp_path):
    b = Bands(tmp_path / "bands.json")
    assert b["action"] == DEFAULT
    b.set("action", 35, 80)
    assert Bands(tmp_path / "bands.json")["action"] == (35, 80) and b["other"] == DEFAULT


@pytest.mark.parametrize("esc, rev", [(80, 70), (-1, 50), (40, 101), (40.5, 70), ("40", 70)])
def test_impossible_bands_are_refused(tmp_path, esc, rev):
    b = Bands(tmp_path / "bands.json")
    with pytest.raises(ValueError): b.set("q", esc, rev)
    assert not (tmp_path / "bands.json").exists()


def test_a_damaged_bands_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "bands.json").write_text('{"ok": [30, 60], "bad": [90, 10], "worse": "x"}', encoding="utf-8")
    b = Bands(tmp_path / "bands.json")
    assert b["ok"] == (30, 60) and b["bad"] == DEFAULT and b["worse"] == DEFAULT
    (tmp_path / "bands.json").write_text("not json", encoding="utf-8")
    assert Bands(tmp_path / "bands.json")["ok"] == DEFAULT


# ---- edge cases

def test_grading_a_buffered_decision_does_not_flush(tmp_path, make_record):
    log = filled(tmp_path, [make_record(i) for i in range(3)])
    log.grade(2, "verdict", "wrong")
    assert len(log.buf) == 3 and log.outcomes() == {(2, "verdict"): "wrong"}  # no flush on the window's thread
    with pytest.raises(KeyError): log.grade(2, "nope", "wrong")
    odd = make_record(3); odd["response"]["answers"] = {"x": {"type": "choice", "choice": "a", "confidence": 0.5, "probabilities": {"a": "1"}}}
    log.append(odd)
    with pytest.raises(KeyError): log.grade(3, "x", "correct")  # the index cannot hold it, so it cannot be graded
    log.close()
    assert JevLog(tmp_path / "s.jevlog").outcomes() == {(2, "verdict"): "wrong"}


def test_record_parses_only_the_line_it_needs(tmp_path, make_record, monkeypatch):
    log = filled(tmp_path, [make_record(i) for i in range(300)]); log._flush()
    parsed = []; real = storage.json.loads
    monkeypatch.setattr(storage.json, "loads", lambda s, *a, **k: parsed.append(1) or real(s, *a, **k))
    assert log.record(150)["request_id"] == "req_00000096" and log.record(149)["seq"] == 149
    assert len(parsed) == 2


def test_record_is_right_after_compaction(tmp_path, make_record):
    log = filled(tmp_path, [make_record(i) for i in range(600)]); log._flush()
    before = {s: log.record(s)["request_id"] for s in (0, 250, 599)}
    ids = lambda: [i for (i,) in log.con.execute("SELECT id FROM block ORDER BY id")]
    old = ids(); log.compact()
    assert min(ids()) > max(old)  # a block id is never reused, so the inspector's cached block can never be another one
    for i in range(600, 700): log.append(make_record(i))
    log._flush()
    assert {s: log.record(s)["request_id"] for s in (0, 250, 599)} == before
    assert [log.record(s)["seq"] for s in range(0, 700, 7)] == list(range(0, 700, 7))


def test_previous_call_with_a_lone_surrogate_sender(tmp_path, make_record):
    recs = [make_record(i) for i in range(3)]
    for r in recs: r["source"] = "odd\ud800"
    log = filled(tmp_path, recs[:2]); log._flush(); log.append(recs[2])
    assert log.previous_seq(2, "odd\ud800", "test") == 1 and log.previous_seq(1, "odd\ud800", "test") == 0
