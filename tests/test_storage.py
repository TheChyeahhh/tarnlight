import json, random, sqlite3, subprocess, sys
import pytest
import zstandard as zstd
from tarnlight import storage
from tarnlight.storage import JevLog, Privacy

SECRET_STATE = {"customer": {"email": "ann@example.com", "plan": "pro"},
                "messages": [{"text": "my card is 4111-1111", "at": 1}, {"text": "call me back", "at": 2}],
                "accounts": {"a1": {"iban": "DE89 3704", "name": "main"}, "a2": {"iban": "GB29 NWBK", "name": "spare"}}}
SECRETS = (b"ann@example.com", b"4111-1111", b"call me back", b"DE89 3704", b"GB29 NWBK")
REDACT = ["request.state.customer.email", "request.state.messages.*.text", "request.state.accounts.*.iban"]


def dump(r): return json.dumps(r, separators=(",", ":"))
def stored(recs): return [{**r, "seq": i} for i, r in enumerate(recs)]
def count(log, table): return log.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
def request_ids(log): return [r["request_id"] for _, r in log.iter_records()]


def fill(path, recs, **kw):
    log = JevLog(path, **kw)
    for r in recs: log.append(r)
    return log


def kill(log):
    """What a hard kill leaves behind: no flush, no close, the OS just drops the handles."""
    log.hot_f.close(); log.con.close(); log.lock_f.close()


@pytest.fixture(autouse=True)
def no_timed_flush(monkeypatch):
    """Block boundaries depend only on BLOCK_N in these tests, never on how fast the machine is."""
    monkeypatch.setattr(storage, "FLUSH_S", 1e9)


# ---- round trip and export

def test_round_trip_is_lossless_and_verbatim(tmp_path, make_record):
    recs = [make_record(i) for i in range(1300)]
    fill(tmp_path / "s.jevlog", recs).close()
    log = JevLog(tmp_path / "s.jevlog")
    assert count(log, "block") == 3  # 512 + 512 + 276
    assert [r for _, r in log.iter_records()] == stored(recs)
    log.export_jsonl(tmp_path / "out.jsonl")
    # byte for byte, including the wire key order
    assert (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines() == [dump(r) for r in stored(recs)]


def test_round_trip_on_the_sample_records(tmp_path, sample_records):
    fill(tmp_path / "s.jevlog", sample_records).close()
    log = JevLog(tmp_path / "s.jevlog")
    log.export_jsonl(tmp_path / "out.jsonl")
    assert (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines() == [dump(r) for r in stored(sample_records)]
    answered = [r for r in sample_records if r["response"]]
    assert count(log, "call") == len(sample_records)
    assert count(log, "answer") == sum(len(r["response"]["answers"]) for r in answered)


def test_export_range(tmp_path, make_record):
    recs = [make_record(i) for i in range(40)]
    log = fill(tmp_path / "s.jevlog", recs)
    log.export_jsonl(tmp_path / "out.jsonl", seq_from=10, seq_to=19)  # also flushes the buffered records first
    assert (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines() == [dump(r) for r in stored(recs)[10:20]]


def test_only_envelope_fields_are_stored(tmp_path, make_record):
    """A last line of defence for the key: fields outside the canonical envelope, such as a header map, never reach disk."""
    rec = {**make_record(0), "request_headers": {"authorization": "Bearer ts_FAKE_NOT_A_REAL_KEY_0123"}}
    log = fill(tmp_path / "s.jevlog", [rec])
    assert b"ts_FAKE_NOT_A_REAL_KEY" not in log.hot.read_bytes()
    log.close(); log = JevLog(tmp_path / "s.jevlog")
    assert b"ts_FAKE_NOT_A_REAL_KEY" not in _all_bytes(log, tmp_path)
    assert [r for _, r in log.iter_records()] == stored([make_record(0)])


# ---- index rows

def test_index_rows(tmp_path, make_record):
    ok = make_record(0); ok["session_id"] = "s-7f3a"; ok["response"]["answers"]["go"]["noul"] = 0.2
    # the API's choice is not always its top probability (it happens on real answers): both are kept
    ok["response"]["answers"]["verdict"] = {"type": "choice", "choice": "reject", "confidence": 0.1,
                                         "probabilities": {"reject": 0.43, "approve": 0.44, "defer": 0.13}}
    ok["response"]["answers"]["broken"] = {"type": "choice", "choice": "x"}  # no probabilities: stored, but not indexed
    ok["response"]["answers"]["broken2"] = {"type": "score", "score": 1, "confidence": 0.5, "probabilities": [0.5, 0.5]}  # a list
    err = make_record(1); err.update(status=400, response=None, error={"detail": "Too many score levels."})
    fill(tmp_path / "s.jevlog", [ok, err]).close()
    log = JevLog(tmp_path / "s.jevlog")
    rows = {q: rest for q, *rest in log.con.execute(
        "SELECT q.name, qtype, confidence, margin, top_prob, p_yes, chosen FROM answer JOIN qname q ON q.id = qid WHERE seq=0")}
    assert rows == {"go": ["noul", 800, 600, 800, 200, "false"],  # noul confidence = max(p, 1-p)
                    "verdict": ["choice", 100, 10, 440, None, "reject"],
                    "size": ["score", 500, 300, 600, None, "1.2"]}
    calls = [dict(zip(storage.CALL_COLS, row)) for row in log.con.execute(f"SELECT {','.join(storage.CALL_COLS)} FROM call ORDER BY seq")]
    questions = dump(ok["request"]["questions"]); state = storage._canon(ok["request"]["state"])
    assert calls[0] == {"seq": 0, "ts_ms": 1790400000000, "block_id": 1, "source": "proxy", "project": "test", "session_id": "s-7f3a",
                        "schema_id": storage._hash(questions.encode()), "request_id": "req_00000000", "latency_ms": 100,
                        "status": 200, "retry_count": 0, "sdk": "python/0.7.1", "tokens_in": 300, "tokens_out": 60,
                        "cost_est_micro": 15, "state_bytes": len(state), "state_hash": storage._hash(state)}
    assert (calls[1]["status"], calls[1]["tokens_in"], calls[1]["tokens_out"], calls[1]["request_id"]) == (400, None, None, "req_00000001")
    assert log.con.execute("SELECT COUNT(*) FROM answer WHERE seq=1").fetchone()[0] == 0
    assert dict(log.con.execute("SELECT id, body FROM schema")) == {calls[0]["schema_id"]: questions}  # interned in wire order
    assert [r for _, r in log.iter_records()] == stored([ok, err])
    meta = dict(log.con.execute("SELECT k, v FROM meta"))
    assert meta["format_version"] == "1" and json.loads(meta["privacy"])["mode"] == "full"
    assert log.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and log.con.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert {n for (n,) in log.con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")} == {"ix_call_ts"}


def test_chart_series_by_question_name(tmp_path, make_record):
    """One question's series, in seq order, across a reopen: names map to the same small number every time."""
    fill(tmp_path / "s.jevlog", [make_record(i) for i in range(30)]).close()
    log = fill(tmp_path / "s.jevlog", [make_record(i) for i in range(30, 60)])
    log.close(); log = JevLog(tmp_path / "s.jevlog")
    assert count(log, "qname") == 3
    chart = "SELECT seq, p_yes FROM answer WHERE qid=(SELECT id FROM qname WHERE name='go') ORDER BY seq"
    assert log.con.execute(chart).fetchall() == [(i, (i % 100) * 10) for i in range(60)]
    plan = " ".join(row[-1] for row in log.con.execute("EXPLAIN QUERY PLAN " + chart))
    assert "SCAN answer" not in plan and "TEMP B-TREE" not in plan  # one range read, already in seq order


def test_a_failed_flush_does_not_mix_up_question_names(tmp_path, make_record):
    log = JevLog(tmp_path / "s.jevlog")
    # a conflicting row for seq 1 makes the flush fail after record 0 has already added its question names
    log.con.execute("INSERT INTO call(seq) VALUES(1)"); log.con.commit()
    log.append(make_record(0)); log.append(make_record(1))
    with pytest.raises(sqlite3.IntegrityError): log._flush()
    log.con.execute("DELETE FROM call WHERE seq=1"); log.con.commit()
    log._flush()
    beta = make_record(2)
    beta["request"]["questions"] = {"beta": {"type": "noul", "instructions": "Beta?"}}
    beta["response"]["answers"] = {"beta": {"type": "noul", "noul": 0.9}}
    log.append(beta); log._flush()
    per_name = dict(log.con.execute("SELECT q.name, COUNT(*) FROM answer JOIN qname q ON q.id = qid GROUP BY q.name"))
    assert per_name == {"go": 2, "verdict": 2, "size": 2, "beta": 1}


# ---- bad input never jams the log

POISON = {
    "request is a list": lambda r: r.update(request=[1, 2]),
    "request is a string": lambda r: r.update(request='{"state": 1, "questions": '),
    "response is an HTML page": lambda r: r.update(response="<html>502 Bad Gateway</html>"),
    "answers is a list": lambda r: r["response"].update(answers=[1]),
    "probabilities is a list": lambda r: r["response"]["answers"]["verdict"].update(probabilities=[0.7, 0.3]),
    "choice is a list": lambda r: r["response"]["answers"]["verdict"].update(choice=["approve"]),
    "usage is a list": lambda r: r["response"].update(usage=[300]),
    "noul is huge": lambda r: r["response"]["answers"]["go"].update(noul=1e300),
    "ts is a string": lambda r: r.update(ts="1790400000"),
    "latency beyond int64": lambda r: r.update(latency_ms=2 ** 70),
    "project is a list": lambda r: r.update(project=["x"]),
    "state looks like the hash marker": lambda r: r["request"].update(state={storage.HASHED: "x"}),
    "lone surrogate in text": lambda r: r.update(project="\ud83d"),
}


@pytest.mark.parametrize("break_it", POISON.values(), ids=POISON.keys())
def test_bad_input_never_jams_the_log(tmp_path, make_record, monkeypatch, break_it):
    monkeypatch.setattr(storage, "BLOCK_N", 4)  # several flushes happen around the bad record
    recs = [make_record(i) for i in range(10)]; break_it(recs[2])
    log = fill(tmp_path / "s.jevlog", recs)  # no append raises
    assert log.last_error is None
    log.close(); log = JevLog(tmp_path / "s.jevlog")
    assert [r for _, r in log.iter_records()] == stored(recs)  # the bad record too, verbatim
    assert count(log, "call") == 10
    # indexing handled the odd field itself: the rest of the bad record's row is still there
    assert log.con.execute("SELECT request_id FROM call WHERE seq=2").fetchone()[0] == "req_00000002"


def test_a_record_that_cannot_be_indexed_is_still_stored(tmp_path, make_record, monkeypatch):
    """The backstop behind summarize(): if indexing a record fails in a way nobody foresaw, the batch still commits."""
    real = storage.summarize
    def summarize(rec):
        if rec["seq"] == 1: raise RecursionError("something unforeseen")
        return real(rec)
    monkeypatch.setattr(storage, "summarize", summarize)
    fill(tmp_path / "s.jevlog", [make_record(i) for i in range(3)]).close()
    log = JevLog(tmp_path / "s.jevlog")
    assert request_ids(log) == ["req_00000000", "req_00000001", "req_00000002"]
    assert log.con.execute("SELECT seq, request_id FROM call ORDER BY seq").fetchall() == [(0, "req_00000000"), (1, None), (2, "req_00000002")]


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["NaN", "Infinity"])
def test_non_json_numbers_are_refused_not_stored(tmp_path, make_record, value):
    log = JevLog(tmp_path / "s.jevlog")
    bad = make_record(0); bad["response"]["answers"]["go"]["noul"] = value
    with pytest.raises(ValueError): log.append(bad)
    assert log.append(make_record(1)) == 0  # the refused record used no seq, and the log keeps working
    log.close()
    assert request_ids(JevLog(tmp_path / "s.jevlog")) == ["req_00000001"]


# ---- crash safety

CHILD = """
import sys, time
from tarnlight.storage import JevLog
log = JevLog(sys.argv[1])
for i in range(100):
    log.append({"v": 1, "ts": 1790400000 + i, "seq": None, "request": {"state": i, "questions": {}}, "response": None})
print("ready", flush=True)
time.sleep(60)
"""


def test_hot_file_survives_a_hard_kill(tmp_path):
    path = tmp_path / "s.jevlog"
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        # premise: nothing was flushed, all 100 records exist only in the hot file
        assert path.with_suffix(".hot.jsonl").read_text(encoding="utf-8").count("\n") == 100
        child.kill()  # TerminateProcess / SIGKILL: no close(), no atexit
        child.wait(timeout=30)
    finally:
        if child.poll() is None: child.kill()
    log = JevLog(path)
    assert [s for s, _ in log.iter_records()] == list(range(100))
    assert log.append({"v": 1, "ts": 0, "seq": None, "request": {}, "response": None}) == 100


def test_recovery_skips_records_already_committed(tmp_path, make_record):
    """A crash after a block commits but before the hot file is cleared must not duplicate records."""
    path = tmp_path / "s.jevlog"; recs = [make_record(i) for i in range(12)]
    fill(path, recs[:10]).close()
    path.with_suffix(".hot.jsonl").write_text("".join(dump(r) + "\n" for r in stored(recs)), encoding="utf-8")
    log = JevLog(path)
    assert [r for _, r in log.iter_records()] == stored(recs)
    assert count(log, "call") == 12
    assert log.append(make_record(12)) == 12


def test_recovery_never_commits_a_seq_twice(tmp_path, make_record):
    """Two lines with one seq (only possible if the one-writer rule was broken) must not make the file unopenable."""
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    a, b = make_record(0), make_record(1)
    path.with_suffix(".hot.jsonl").write_text(dump({**a, "seq": 0}) + "\n" + dump({**b, "seq": 0}) + "\n", encoding="utf-8")
    assert request_ids(JevLog(path)) == ["req_00000000"]


def test_recovery_ignores_a_cut_off_last_line(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; recs = stored([make_record(i) for i in range(3)])
    JevLog(path).close()
    path.with_suffix(".hot.jsonl").write_text(dump(recs[0]) + "\n" + dump(recs[1]) + "\n" + dump(recs[2])[:40], encoding="utf-8")
    log = JevLog(path)
    assert [r for _, r in log.iter_records()] == recs[:2]
    assert log.seq == 2


def test_a_long_hot_file_is_recovered_in_normal_sized_blocks(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    recs = stored([make_record(i) for i in range(1100)])
    path.with_suffix(".hot.jsonl").write_text("".join(dump(r) + "\n" for r in recs), encoding="utf-8")
    log = JevLog(path)
    assert [n for (n,) in log.con.execute("SELECT n FROM block ORDER BY first_seq")] == [512, 512, 76]
    assert [r for _, r in log.iter_records()] == recs


def test_a_recovery_that_cannot_save_keeps_new_records_on_their_own_line(tmp_path, make_record, monkeypatch):
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    r0, r1 = stored([make_record(0), make_record(1)])
    path.with_suffix(".hot.jsonl").write_text(dump(r0) + "\n" + dump(r1)[:40], encoding="utf-8")  # cut off by a crash
    def disk_full(self): raise OSError(28, "No space left on device")
    real_flush = JevLog._flush; monkeypatch.setattr(JevLog, "_flush", disk_full)
    log = JevLog(path)  # recovery cannot save: the records stay in the hot file
    assert "No space" in log.last_error
    monkeypatch.setattr(JevLog, "_flush", real_flush)
    assert log.append(make_record(5)) == 1
    kill(log)
    assert request_ids(JevLog(path)) == ["req_00000000", "req_00000005"]


class HalfThenFail:
    """Stands in for the hot-file handle: the first write puts half the line on disk, then the disk is full."""
    def __init__(self, f): self.f, self.failed = f, False
    def write(self, b):
        if not self.failed:
            self.failed = True; self.f.write(bytes(b[: len(b) // 2])); raise OSError(28, "No space left on device")
        return self.f.write(b)
    def __getattr__(self, name): return getattr(self.f, name)


def test_a_half_written_line_does_not_swallow_the_next_record(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; log = JevLog(path)
    log.append(make_record(0))
    log.hot_f = HalfThenFail(log.hot_f)
    with pytest.raises(OSError): log.append(make_record(1))  # not stored, and the caller is told so
    assert log.append(make_record(2)) == 1
    kill(log)
    assert request_ids(JevLog(path)) == ["req_00000000", "req_00000002"]


def test_a_locked_database_never_fails_or_duplicates_an_append(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; log = JevLog(path)
    other = sqlite3.connect(path, isolation_level=None); other.execute("BEGIN IMMEDIATE")  # e.g. a DB browser mid-edit
    recs = [make_record(i) for i in range(600)]
    assert [log.append(r) for r in recs] == list(range(600))  # the flush at 512 fails; no append raises
    assert "locked" in log.last_error and count(log, "call") == 0
    other.execute("ROLLBACK"); other.close()
    log.close(); log = JevLog(path)
    assert [r for _, r in log.iter_records()] == stored(recs)


def test_one_writer_per_file(tmp_path, monkeypatch):
    lock = storage._lock_exclusive
    monkeypatch.setattr(storage, "_lock_exclusive", lambda f: lock(f, wait_s=0.2))
    log = JevLog(tmp_path / "s.jevlog")
    with pytest.raises(RuntimeError): JevLog(tmp_path / "s.jevlog")
    log.close()
    JevLog(tmp_path / "s.jevlog").close()


def test_other_format_versions_are_refused(tmp_path):
    log = JevLog(tmp_path / "s.jevlog")
    with log.con: log.con.execute("UPDATE meta SET v='0' WHERE k='format_version'")
    log.close()
    with pytest.raises(ValueError): JevLog(tmp_path / "s.jevlog")


def test_tick_flushes_a_quiet_session(tmp_path, make_record, monkeypatch):
    log = fill(tmp_path / "s.jevlog", [make_record(0)])
    assert count(log, "block") == 0
    monkeypatch.setattr(storage, "FLUSH_S", 5.0); log.last_flush -= 5.0
    log.tick()
    assert count(log, "block") == 1 and log.hot.read_text() == ""


def test_append_flushes_after_five_seconds(tmp_path, make_record, monkeypatch):
    monkeypatch.setattr(storage, "FLUSH_S", 5.0)
    log = JevLog(tmp_path / "s.jevlog"); log.append(make_record(0))
    assert count(log, "block") == 0
    log.last_flush -= 5.0; log.append(make_record(1))
    assert count(log, "block") == 1


# ---- compaction

def test_compact_keeps_every_seq_findable(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; recs = [make_record(i) for i in range(5100)]
    log = fill(path, recs[:5000]); log._flush()
    live = log.con.execute("SELECT SUM(LENGTH(body)) FROM block").fetchone()[0]
    log.compact()  # 9 x 512 + 392 -> 2048 + 2048 + 904
    assert log.con.execute("SELECT SUM(LENGTH(body)) FROM block").fetchone()[0] < live  # really recompressed
    first = log.con.execute("SELECT id FROM block ORDER BY first_seq").fetchall()
    for r in recs[5000:]: log.append(r)
    log.compact()  # full level-19 blocks stay as they are, 904 + 100 merge
    log.close()
    log = JevLog(path)
    blocks = log.con.execute("SELECT id, n, level, body FROM block ORDER BY first_seq").fetchall()
    assert [(n, level) for _, n, level, _ in blocks] == [(2048, 19), (2048, 19), (1004, 19)]
    assert [b[0] for b in blocks[:2]] == [i for (i,) in first[:2]]
    dz = zstd.ZstdDecompressor(); cz = zstd.ZstdCompressor(level=storage.COMPACT_LEVEL)
    assert all(body == cz.compress(dz.decompress(body)) for _, _, _, body in blocks)  # really level 19, not just labelled so
    inside = {bid: {json.loads(l)["seq"] for l in dz.decompress(body).decode().split("\n")} for bid, _, _, body in blocks}
    for seq, bid in log.con.execute("SELECT seq, block_id FROM call"):
        assert seq in inside[bid]
    assert [r for _, r in log.iter_records()] == stored(recs)


def test_compaction_while_a_reader_is_part_way_through(tmp_path, make_record):
    recs = [make_record(i) for i in range(1300)]
    log = fill(tmp_path / "s.jevlog", recs)
    reader = log.iter_records(); first = [next(reader) for _ in range(20)]
    log.compact()  # merges the blocks the reader was walking, and flushes the last 276
    assert [r for _, r in first] + [r for _, r in reader] == stored(recs)


# ---- privacy

def _all_bytes(log, folder):
    """Every byte the log wrote: the files on disk plus the decompressed blocks."""
    # the .lock file is never written, and Windows refuses to read it while the writer holds it
    raw = b"".join(p.read_bytes() for p in folder.iterdir() if p.is_file() and p.suffix != ".lock")
    return raw + b"".join(zstd.ZstdDecompressor().decompress(b) for (b,) in log.con.execute("SELECT body FROM block"))


def test_privacy_hash_keeps_no_state(tmp_path, make_record):
    h, f = tmp_path / "h", tmp_path / "f"; h.mkdir(); f.mkdir()
    log = fill(h / "s.jevlog", [make_record(0, state=SECRET_STATE)], privacy=Privacy("hash"))
    hot = log.hot.read_bytes()  # applied before the hot file is written, not only before the block
    assert storage.HASHED.encode() in hot and not any(secret in hot for secret in SECRETS)
    log.close()
    log = JevLog(h / "s.jevlog", privacy=Privacy("hash"))
    blob = _all_bytes(log, h)
    for secret in SECRETS:
        assert secret not in blob
    (_, rec), = log.iter_records()
    marker = rec["request"]["state"][storage.HASHED]
    assert marker["keys"] == ["customer", "messages", "accounts"] and marker["bytes"] == len(storage._canon(SECRET_STATE))
    fill(f / "s.jevlog", [make_record(0, state=SECRET_STATE)]).close()
    index = "SELECT state_hash, state_bytes FROM call"
    assert log.con.execute(index).fetchone() == JevLog(f / "s.jevlog").con.execute(index).fetchone()


def test_privacy_redact_drops_listed_paths(tmp_path, make_record):
    log = fill(tmp_path / "s.jevlog", [make_record(0, state=SECRET_STATE)], privacy=Privacy("redact", REDACT))
    hot = log.hot.read_bytes()
    assert b"pro" in hot and not any(secret in hot for secret in SECRETS)
    log.close()
    log = JevLog(tmp_path / "s.jevlog", privacy=Privacy("redact", REDACT))
    (_, rec), = log.iter_records()
    assert rec["request"]["state"] == {"customer": {"plan": "pro"}, "messages": [{"at": 1}, {"at": 2}],
                                       "accounts": {"a1": {"name": "main"}, "a2": {"name": "spare"}}}
    for secret in SECRETS:
        assert secret not in _all_bytes(log, tmp_path)


def test_redact_by_position_keeps_the_other_items_in_place(make_record):
    p = Privacy("redact", ["request.state.cards.0", "request.state.cards.1"])
    once = p.apply(make_record(0, state={"cards": ["S0", "S1", "public"]}))
    assert once["request"]["state"] == {"cards": [None, None, "public"]}
    assert p.apply(once) == once


def test_a_state_shaped_like_the_marker_is_still_hashed(make_record):
    spoof = {storage.HASHED: {"blake2b": "0" * 16, "bytes": 1, "ssn": "123-45-6789"}}
    rec = Privacy("hash").apply(make_record(0, state=spoof))
    assert "123-45-6789" not in dump(rec) and storage._is_hashed(rec["request"]["state"])


def test_a_file_keeps_its_privacy_mode(tmp_path, make_record):
    path = tmp_path / "s.jevlog"
    fill(path, [make_record(0, state=SECRET_STATE)], privacy=Privacy("hash")).close()
    with pytest.raises(ValueError): JevLog(path, privacy=Privacy("full"))
    log = JevLog(path)  # no mode given: the file's own
    assert log.privacy.mode == "hash"
    log.append(make_record(1, state=SECRET_STATE)); log.close()
    assert all(storage._is_hashed(r["request"]["state"]) for _, r in JevLog(path).iter_records())


def test_recovery_applies_the_files_privacy(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; JevLog(path, privacy=Privacy("hash")).close()
    raw = {**make_record(0, state=SECRET_STATE), "seq": 0}  # e.g. a hot file some other writer left behind
    path.with_suffix(".hot.jsonl").write_text(dump(raw) + "\n", encoding="utf-8")
    log = JevLog(path)
    (_, rec), = log.iter_records()
    assert storage._is_hashed(rec["request"]["state"])
    for secret in SECRETS:
        assert secret not in _all_bytes(log, tmp_path)


def test_export_applies_privacy_again(tmp_path, make_record):
    log = fill(tmp_path / "s.jevlog", [make_record(0, state=SECRET_STATE)])  # stored in full
    log.export_jsonl(tmp_path / "redacted.jsonl", privacy=Privacy("redact", REDACT))
    log.export_jsonl(tmp_path / "hashed.jsonl", privacy=Privacy("hash"))
    for name in ("redacted.jsonl", "hashed.jsonl"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert not any(secret.decode() in text for secret in SECRETS)
    assert storage.HASHED in (tmp_path / "hashed.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("privacy", [Privacy("hash"), Privacy("redact", REDACT + ["request.state.messages.0"])], ids=["hash", "redact"])
def test_privacy_is_idempotent_and_leaves_the_input_alone(make_record, privacy):
    rec = make_record(0, state=SECRET_STATE); before = dump(rec)
    once = privacy.apply(rec)
    assert privacy.apply(once) == once
    assert dump(rec) == before


@pytest.mark.parametrize("args, error", [
    (("encrypt",), ValueError),
    (("redact", "request.state.customer.email"), TypeError),  # one string, not a list: would redact nothing
    (("redact", []), ValueError),
    (("redact", ["request.state..email"]), ValueError),
    (("redact", ["seq"]), ValueError),                        # envelope fields are not redactable
    (("redact", ["state.email"]), ValueError),
], ids=["unknown mode", "one string", "no paths", "empty part", "envelope field", "wrong root"])
def test_bad_privacy_settings_are_refused(args, error):
    with pytest.raises(error): Privacy(*args)


def test_the_same_paths_in_another_order_are_the_same_mode(tmp_path, make_record):
    path = tmp_path / "s.jevlog"
    JevLog(path, privacy=Privacy("redact", ["request.state.a", "request.state.b"])).close()
    JevLog(path, privacy=Privacy("redact", ["request.state.b", "request.state.a", "request.state.b"])).close()


def test_hash_mode_hashes_a_request_that_is_not_an_object(tmp_path, make_record):
    rec = make_record(0); rec["request"] = json.dumps({"state": SECRET_STATE, "questions": {}})  # a body the proxy could not parse
    log = fill(tmp_path / "s.jevlog", [rec], privacy=Privacy("hash")); log.close()
    log = JevLog(tmp_path / "s.jevlog")
    (_, got), = log.iter_records()
    assert storage._is_hashed(got["request"])
    assert not any(secret in _all_bytes(log, tmp_path) for secret in SECRETS)


def test_a_marker_carrying_data_is_hashed_like_any_state(make_record):
    smuggle = {storage.HASHED: {"blake2b": "0" * 16, "keys": [{"ssn": "123-45-6789"}], "bytes": 1}}
    rec = Privacy("hash").apply(make_record(0, state=smuggle))
    assert "123-45-6789" not in dump(rec)


# ---- limits: refused before anything is written, so every reader can read what was stored

def nested(depth):
    x = "bottom"
    for _ in range(depth): x = [x]
    return x


def test_deep_nesting_is_refused_and_the_deepest_allowed_reads_back_anywhere(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; log = JevLog(path)
    with pytest.raises(ValueError): log.append(make_record(0, state=nested(storage.MAX_DEPTH)))
    assert log.append(make_record(1, state=nested(storage.MAX_DEPTH - 5))) == 0
    kill(log)
    def deep(n):  # read back from far down the stack, where a nearly-too-deep record would fail to parse
        return deep(n - 1) if n else request_ids(JevLog(path))
    assert deep(600) == ["req_00000001"]


def test_keys_that_are_not_text_are_refused(tmp_path, make_record):
    log = JevLog(tmp_path / "s.jevlog")
    with pytest.raises(TypeError): log.append(make_record(0, state={1: "a", "1": "b"}))  # would collide as JSON
    assert log.append(make_record(1)) == 0


def test_a_record_over_the_size_limit_is_refused(tmp_path, make_record, monkeypatch):
    monkeypatch.setattr(storage, "MAX_LINE", 2000)
    log = JevLog(tmp_path / "s.jevlog")
    with pytest.raises(ValueError): log.append(make_record(0, state="x" * 3000))
    assert log.append(make_record(1)) == 0


def test_blocks_stay_under_the_byte_limit(tmp_path, make_record, monkeypatch):
    monkeypatch.setattr(storage, "MAX_BLOCK_BYTES", 5000)  # about 2 of these records
    noise = [random.Random(i).randbytes(600).hex() for i in range(20)]  # compresses poorly, so compaction meets the limit too
    log = fill(tmp_path / "s.jevlog", [make_record(i, state=noise[i]) for i in range(20)])
    log._flush()
    assert max(n for (n,) in log.con.execute("SELECT n FROM block")) < 20
    log.compact()  # merges by compressed size, still bounded
    assert all(size <= 5000 for (size,) in log.con.execute("SELECT LENGTH(body) FROM block"))
    assert len(request_ids(log)) == 20


# ---- more ways a file could jam, and why it no longer does

def test_question_names_that_differ_only_by_a_broken_character_stay_apart(tmp_path, make_record):
    rec = make_record(0)
    rec["response"]["answers"] = {"ok?": {"type": "noul", "noul": 0.9}, "ok\ud800": {"type": "noul", "noul": 0.1}}
    log = fill(tmp_path / "s.jevlog", [rec, make_record(1)]); log._flush()
    assert log.last_error is None and count(log, "call") == 2
    assert count(log, "qname") >= 2 and log.con.execute("SELECT COUNT(*) FROM answer WHERE seq=0").fetchone()[0] == 2


def test_an_index_row_the_database_refuses_does_not_block_the_batch(tmp_path, make_record, monkeypatch):
    real = storage.summarize
    def summarize(rec):
        call, answers, questions = real(rec)
        if rec["seq"] == 1: answers[-1] = (*answers[-1][:-1], ["not", "bindable"])  # the call row goes in, then SQLite refuses this
        return call, answers, questions
    monkeypatch.setattr(storage, "summarize", summarize)
    log = fill(tmp_path / "s.jevlog", [make_record(i) for i in range(3)]); log._flush()
    assert log.last_error is None
    assert log.con.execute("SELECT seq, request_id FROM call ORDER BY seq").fetchall() == [(0, "req_00000000"), (1, None), (2, "req_00000002")]
    assert log.con.execute("SELECT COUNT(*) FROM answer WHERE seq=1").fetchone()[0] == 0  # rolled back with the rest of its rows


def test_recovery_skips_seqs_that_would_poison_the_file(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    lines = [{**make_record(0), "seq": 0}, {**make_record(1), "seq": 2 ** 63 - 1}, {**make_record(2), "seq": 2 ** 70},
             {**make_record(3), "seq": True}, {**make_record(4), "seq": 1}]
    path.with_suffix(".hot.jsonl").write_text("".join(dump(r) + "\n" for r in lines), encoding="utf-8")
    log = JevLog(path)
    assert request_ids(log) == ["req_00000000", "req_00000004"] and log.last_error is None
    assert log.append(make_record(5)) == 2
    log.close()


def test_recovery_reads_a_hot_file_an_editor_saved_with_a_bom(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    path.with_suffix(".hot.jsonl").write_text(dump({**make_record(0), "seq": 0}) + "\n", encoding="utf-8-sig")
    assert request_ids(JevLog(path)) == ["req_00000000"]


def test_recovery_stores_only_envelope_fields(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    leftover = {**make_record(0), "seq": 0, "request_headers": {"authorization": "Bearer ts_FAKE_NOT_A_REAL_KEY_0123"}}
    path.with_suffix(".hot.jsonl").write_text(dump(leftover) + "\n", encoding="utf-8")  # e.g. written by an older build
    log = JevLog(path)
    assert b"ts_FAKE_NOT_A_REAL_KEY" not in _all_bytes(log, tmp_path)
    log.export_jsonl(tmp_path / "out.jsonl")
    assert "ts_FAKE" not in (tmp_path / "out.jsonl").read_text(encoding="utf-8")


def test_export_drops_fields_an_older_build_stored(tmp_path, make_record):
    log = fill(tmp_path / "s.jevlog", [make_record(0)]); log._flush()
    old = {**make_record(0), "seq": 0, "request_headers": {"authorization": "Bearer ts_FAKE_NOT_A_REAL_KEY_0123"}}
    with log.con: log.con.execute("UPDATE block SET body=?", (zstd.ZstdCompressor().compress(dump(old).encode()),))
    log.export_jsonl(tmp_path / "out.jsonl")
    assert "ts_FAKE" not in (tmp_path / "out.jsonl").read_text(encoding="utf-8")


def test_a_busy_database_does_not_stop_a_reopen(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; JevLog(path).close()
    path.with_suffix(".hot.jsonl").write_text(dump({**make_record(0), "seq": 0}) + "\n", encoding="utf-8")
    other = sqlite3.connect(path, isolation_level=None); other.execute("BEGIN IMMEDIATE")
    log = JevLog(path)  # opens; the recovery flush waits for the other writer
    assert "locked" in log.last_error
    other.execute("ROLLBACK"); other.close()
    log.export_jsonl(tmp_path / "out.jsonl")  # any successful flush clears the error
    assert log.last_error is None and request_ids(log) == ["req_00000000"]


class NoTruncate:
    """Stands in for the hot-file handle while another program (an indexer, a virus scanner) holds the file."""
    def __init__(self, f): self.f = f
    def truncate(self, size): raise PermissionError(13, "The process cannot access the file")
    def __getattr__(self, name): return getattr(self.f, name)


def test_a_hot_file_that_cannot_be_cleared_does_not_look_like_a_failed_save(tmp_path, make_record):
    path = tmp_path / "s.jevlog"; log = JevLog(path)
    log.hot_f = NoTruncate(log.hot_f)
    for i in range(3): log.append(make_record(i))
    log._try_flush()
    assert log.last_error is None and count(log, "call") == 3
    kill(log)
    assert request_ids(JevLog(path)) == ["req_00000000", "req_00000001", "req_00000002"]  # the stale lines are skipped by seq


def test_close_twice_is_harmless(tmp_path):
    log = JevLog(tmp_path / "s.jevlog"); log.close(); log.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path spellings")
def test_another_spelling_of_the_path_is_the_same_writer(tmp_path, monkeypatch):
    lock = storage._lock_exclusive
    monkeypatch.setattr(storage, "_lock_exclusive", lambda f: lock(f, wait_s=0.2))
    log = JevLog(tmp_path / "session-long-name.jevlog")
    with pytest.raises(RuntimeError): JevLog(str(tmp_path / "session-long-name.jevlog") + ".")
    log.close()


def test_hash_mode_hides_state_a_validation_error_echoes(tmp_path, make_record):
    rec = make_record(0, state={"email": "ann@example.com"})
    rec.update(status=422, response=None,  # the SDK's 422 shape: each detail item can carry the input that failed
               error={"detail": [{"type": "string_type", "loc": ["body", "state"], "msg": "bad", "input": {"email": "ann@example.com"}}]})
    fill(tmp_path / "s.jevlog", [rec], privacy=Privacy("hash")).close()
    log = JevLog(tmp_path / "s.jevlog")
    assert b"ann@example.com" not in _all_bytes(log, tmp_path)
    (_, got), = log.iter_records()
    assert storage._is_hashed(got["error"]["detail"][0]["input"]) and got["error"]["detail"][0]["msg"] == "bad"


def test_a_huge_whole_number_blanks_only_its_own_field(tmp_path, make_record):
    rec = make_record(0); rec["response"]["usage"]["input_tokens"] = 10 ** 400
    fill(tmp_path / "s.jevlog", [rec]).close()
    assert JevLog(tmp_path / "s.jevlog").con.execute("SELECT request_id, tokens_in, tokens_out FROM call").fetchone() == ("req_00000000", None, 60)
