import base64, json, socket, threading, time
import pytest
from tarnlight import demo, ingest
from tarnlight.ingest import Ingest, send
from tarnlight.replay import read_records, replay
from tarnlight.storage import JevLog, Privacy, HASHED


def stored(recs): return [{**r, "seq": i} for i, r in enumerate(recs)]


def saved(log):
    """Everything the log holds; stop() leaves the newest records in the hot file until the next flush."""
    with log.lock: log._flush()
    return [r for _, r in log.iter_records()]


def wait_until(check, timeout=10.0):
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() > end: raise AssertionError("timed out waiting")
        time.sleep(0.005)


@pytest.fixture
def console(tmp_path):
    """Start a log plus ingest on a free port; stop both afterwards."""
    running = []
    def start(**kw):
        log = JevLog(tmp_path / "s.jevlog", privacy=kw.pop("privacy", None))
        ing = Ingest(log, port=0, **kw).start(); running.append((ing, log))
        return log, ing
    yield start
    for ing, log in running:
        if ing.listener.is_alive(): ing.stop()
        log.close()


@pytest.fixture
def sock():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); yield s; s.close()


def test_replay_of_the_sample_records_at_100_per_second(console, sample_records):
    log, ing = console()
    recs = [{**r, "ts": 1790400000 + i * 0.01} for i, r in enumerate(sample_records)]  # evenly 100 per second
    t0 = time.monotonic()
    assert replay(recs, speed=1.0, addr=("127.0.0.1", ing.port)) == len(recs)
    took = time.monotonic() - t0
    wait_until(lambda: ing.counts["stored"] == len(recs), timeout=0.1)  # every decision is in within 100 ms of the last send
    assert took == pytest.approx(len(recs) / 100, rel=0.1)
    ing.stop()
    assert saved(log) == stored(recs)
    assert ing.counts["rejected"] == 0 and not ing.rejects.exists()
    assert [r["seq"] for r in ing.snapshot()] == list(range(len(recs)))


def test_a_record_too_big_for_one_datagram_arrives_in_chunks(console, sock, make_record):
    log, ing = console()
    big = make_record(0, state="x" * 300_000)  # 7 chunks
    send(big, sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1)
    datagrams = -(-len(json.dumps(big, separators=(",", ":")).encode()) // ingest.CHUNK_BYTES)
    assert datagrams == 7 and ing.counts["received"] == datagrams  # counts datagrams, not the record they rebuild
    ing.stop()
    assert saved(log) == stored([big])


def chunks_of(rec, of=None):
    data = json.dumps(rec).encode(); size = -(-len(data) // (of or 3))
    parts = [data[i:i + size] for i in range(0, len(data), size)]
    return [json.dumps({"id": "m1", "part": i, "of": len(parts), "data": base64.b64encode(p).decode()}).encode() for i, p in enumerate(parts)]


def test_a_chunked_record_with_a_missing_part_is_rejected_after_the_timeout(console, sock, make_record):
    log, ing = console(chunk_timeout_s=0.3)
    parts = chunks_of(make_record(0))
    for i, p in enumerate(parts):
        if i != 1: sock.sendto(p, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 1)
    assert "incomplete" in json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"]
    send(make_record(1), sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1)


GOOD = {"v": 1, "ts": 1790400000.0, "seq": None, "request": {"state": "s", "questions": {}}, "response": None}
BAD = {
    "not JSON": b"not json at all",
    "a JSON array": b"[1, 2, 3]",
    "NaN": b'{"v": 1, "ts": NaN, "request": {"questions": {}}}',
    "version 2": json.dumps({**GOOD, "v": 2}).encode(),
    "ts as text": json.dumps({**GOOD, "ts": "1790400000"}).encode(),
    "a number too large for a float": b'{"v": 1, "ts": 1790400000.0, "request": {"state": 1e999}}',
    "source is a number": json.dumps({**GOOD, "source": 7}).encode(),
    "latency is text": json.dumps({**GOOD, "latency_ms": "12"}).encode(),
    "bad chunk header": json.dumps({"id": 1, "part": 0, "of": 2, "data": "x"}).encode(),
    "chunk data longer than send() makes": json.dumps({"id": "m", "part": 0, "of": 2, "data": "A" * 60_000}).encode(),
    "not UTF-8": b"\xff\xfe{\x00",
}
FAILED_CALLS = {  # what a proxy records for calls TypeSafe rejected: only the envelope is checked, the log keeps the rest
    "request body that was not JSON": {**GOOD, "status": 400, "request": '{"state": "s", "questions": '},
    "request as a list": {**GOOD, "status": 422, "request": [1, 2]},
    "no request at all": {**GOOD, "status": 422, "request": None},
    "no questions": {**GOOD, "status": 422, "request": {"state": "s"}},
    "response without answers": {**GOOD, "status": 200, "response": {"model": "jev-1.13.0"}},
}


@pytest.mark.parametrize("payload", BAD.values(), ids=BAD.keys())
def test_malformed_datagrams_are_rejected_and_ingest_keeps_going(console, sock, payload):
    log, ing = console()
    sock.sendto(payload, ("127.0.0.1", ing.port))
    send(GOOD, sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1 and ing.counts["rejected"] == 1)
    (line,) = ing.rejects.read_text(encoding="utf-8").splitlines()
    assert set(json.loads(line)) == {"t", "reason", "bytes", "blake2b"}  # the reason, never the content


@pytest.mark.parametrize("rec", FAILED_CALLS.values(), ids=FAILED_CALLS.keys())
def test_failed_calls_are_kept(console, sock, rec):
    log, ing = console()
    send(rec, sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1)
    ing.stop()
    assert saved(log) == stored([rec]) and ing.counts["rejected"] == 0


@pytest.mark.parametrize("field", ["ts", "latency_ms", "status", "retry_count", "cost_est_micro"])
def test_a_huge_whole_number_does_not_stop_the_listener(console, sock, field):
    log, ing = console()
    huge = json.dumps({**GOOD, field: 0}).replace(f'"{field}": 0', f'"{field}": 1{"0" * 400}').encode()  # 401 digits
    sock.sendto(huge, ("127.0.0.1", ing.port)); send(GOOD, sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] + ing.counts["rejected"] == 2)
    assert ing.listener.is_alive() and ing.counts["stored"] == 2  # a whole number is finite: the record is kept


def test_an_internal_error_is_rejected_and_listening_goes_on(console, sock, monkeypatch):
    log, ing = console()
    real, calls = ingest.validate, []
    def validate(rec):
        calls.append(1)
        if len(calls) == 1: raise RuntimeError("a bug")
        return real(rec)
    monkeypatch.setattr(ingest, "validate", validate)
    send(GOOD, sock, ("127.0.0.1", ing.port)); send(GOOD, sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1 and ing.counts["rejected"] == 1)
    assert "internal error" in json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"]


@pytest.mark.parametrize("number", [b"NaN", b"Infinity", b"1e999"])
def test_non_finite_numbers_anywhere_are_refused_at_the_door(console, sock, number):
    log, ing = console()
    sock.sendto(b'{"v": 1, "ts": 1790400000.0, "request": {"state": ' + number + b', "questions": {}}, "response": null}', ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 1)
    assert json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"].startswith("not JSON")


def test_too_many_unfinished_chunked_records_are_refused(console, sock):
    log, ing = console()
    for i in range(ingest.MAX_PENDING + 1):
        sock.sendto(json.dumps({"id": f"m{i}", "part": 0, "of": 2, "data": ""}).encode(), ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 1)
    assert "too many" in json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"]


def test_a_chunk_count_that_changes_mid_record_is_refused(console, sock):
    log, ing = console()
    sock.sendto(json.dumps({"id": "m", "part": 0, "of": 3, "data": ""}).encode(), ("127.0.0.1", ing.port))
    sock.sendto(json.dumps({"id": "m", "part": 1, "of": 2, "data": ""}).encode(), ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 1)
    assert "changed" in json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"]


def test_chunks_whose_data_is_not_base64_are_rejected(console, sock):
    log, ing = console()
    for i in range(2): sock.sendto(json.dumps({"id": "m", "part": i, "of": 2, "data": "!!not base64!!"}).encode(), ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 1)
    assert "base64" in json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"]


def test_the_rejects_file_never_holds_what_was_sent(console, sock):
    log, ing = console()
    leak = json.dumps({**GOOD, "v": 2, "request_headers": {"authorization": "Bearer ts_FAKE_NOT_A_REAL_KEY_0123"}}).encode()
    sock.sendto(leak, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 1)
    assert "ts_FAKE" not in ing.rejects.read_text(encoding="utf-8")


def test_the_ring_keeps_the_newest_and_everything_is_stored(console, sock, make_record):
    log, ing = console(ring_size=50)
    for i in range(120): send(make_record(i), sock, ("127.0.0.1", ing.port)); time.sleep(0.001)
    wait_until(lambda: ing.counts["stored"] == 120)
    assert [r["seq"] for r in ing.snapshot()] == list(range(70, 120))
    ing.stop()
    assert len(saved(log)) == 120


def test_stop_stores_everything_that_already_arrived(tmp_path, sock, make_record):
    log = JevLog(tmp_path / "s.jevlog")
    ing = Ingest(log, port=0)  # bound but not reading yet: the 300 datagrams wait in the socket buffer
    for i in range(300): send(make_record(i), sock, ("127.0.0.1", ing.port))
    ing.stopping.set(); ing.start(); ing.stop()  # stop before the listener ever looped
    assert [r["request_id"] for r in saved(log)] == [f"req_{i:08x}" for i in range(300)]
    log.close()


def test_the_writer_waits_for_the_listener_to_finish(tmp_path, sock, make_record):
    log = JevLog(tmp_path / "s.jevlog")
    ing = Ingest(log, port=0)
    for i in range(300): send(make_record(i), sock, ("127.0.0.1", ing.port))
    real_take = ing._take
    def slow_take(data, chunks_allowed=True):  # the listener stalls half way, longer than the writer's 0.25 s wait
        if ing.counts["received"] == 150: time.sleep(0.6)
        return real_take(data, chunks_allowed)
    ing._take = slow_take
    ing.stopping.set(); ing.start(); ing.stop()
    assert len(saved(log)) == 300
    log.close()


def test_a_port_in_use_gives_a_clear_error(tmp_path):
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); taken.bind(("127.0.0.1", 0))
    log = JevLog(tmp_path / "s.jevlog")
    try:
        with pytest.raises(RuntimeError, match="in use"): Ingest(log, port=taken.getsockname()[1])
    finally:
        taken.close(); log.close()


def test_the_writer_waits_out_a_full_disk(console, sock, make_record, monkeypatch):
    monkeypatch.setattr(ingest, "RETRY_S", 0.01)
    log, ing = console()
    real, fails = log.append, [OSError(28, "No space left on device")] * 2
    def append(rec):
        if fails: raise fails.pop()
        return real(rec)
    monkeypatch.setattr(log, "append", append)
    send(make_record(0), sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1)
    assert ing.counts["rejected"] == 0


def test_a_record_the_log_refuses_is_rejected_and_ingest_keeps_going(console, sock, make_record):
    log, ing = console()
    deep = "x"
    for _ in range(300): deep = [deep]
    send(make_record(0, state=deep), sock, ("127.0.0.1", ing.port))  # valid JSON, but deeper than the log accepts
    send(make_record(1), sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1 and ing.counts["rejected"] == 1)
    entry = json.loads(ing.rejects.read_text(encoding="utf-8"))
    assert entry["reason"] == "not stored: ValueError" and entry["bytes"] > 0  # sized and hashed like a listener reject


def test_the_ring_holds_the_envelope_and_answers_only(console, sock, make_record):
    log, ing = console()
    rec = {**make_record(0, state={"email": "ann@example.com"}), "extra": "not envelope", "error": {"detail": "x"}}
    send(rec, sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1)
    (entry,) = ing.snapshot()
    assert set(entry) == set(ingest.RING_FIELDS) and entry["seq"] == 0
    assert entry["response"] == {"answers": rec["response"]["answers"], "usage": rec["response"]["usage"]}  # no "model"
    assert "ann@example.com" not in json.dumps(entry)  # the state stays in the log, for the inspector
    assert all(isinstance(line, str) for _, line in ing.ring)  # plain strings: nothing for the garbage collector to walk


def test_the_ring_follows_the_privacy_mode(console, sock, make_record):
    log, ing = console(privacy=Privacy("redact", ["response.model"]))
    send(make_record(0), sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["stored"] == 1)
    (entry,) = ing.snapshot()
    assert "model" not in entry["response"] and "answers" in entry["response"]


def test_a_full_backlog_refuses_as_overloaded(console, sock, make_record, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_BACKLOG_BYTES", 3000)  # about two records
    log, ing = console()
    gate, real = threading.Event(), log.append
    monkeypatch.setattr(log, "append", lambda rec: gate.wait() and real(rec))  # the writer is stuck
    for i in range(10): send(make_record(i), sock, ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["received"] == 10)
    gate.set()
    wait_until(lambda: ing.counts["stored"] + ing.counts["rejected"] == 10)
    assert 1 <= ing.counts["stored"] <= 3 and "overloaded" in ing.rejects.read_text(encoding="utf-8")


def test_stop_returns_while_a_sender_keeps_sending(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "STOP_DRAIN_S", 0.3)
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0).start()
    flooding = threading.Event()
    def flood():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not flooding.is_set(): s.sendto(b"junk", ("127.0.0.1", ing.port))
        s.close()
    t = threading.Thread(target=flood); t.start()
    try:
        time.sleep(0.2); t0 = time.monotonic(); ing.stop(); took = time.monotonic() - t0
    finally:
        flooding.set(); t.join(); log.close()
    assert took < 5


class EmptyOnce:
    """A queue whose first get() times out although a record is waiting: the race at the writer's exit."""
    def __init__(self, q): self.q, self.first = q, True
    def get(self, timeout):
        if self.first: self.first = False; raise ingest.queue.Empty
        return self.q.get(timeout=timeout)
    def empty(self): return self.q.empty()
    def put(self, item): self.q.put(item)


def test_the_writer_never_leaves_a_record_behind_at_shutdown(tmp_path, make_record):
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0)
    ing.queue = EmptyOnce(ing.queue); ing.queue.put((make_record(0), 100)); ing.backlog = 100
    ing.stopping.set(); ing.writer.start(); ing.writer.join(timeout=10)  # the listener never ran, so it is not alive
    assert ing.counts["stored"] == 1
    ing.stop(); log.close()


def test_a_chunked_record_unfinished_at_shutdown_is_recorded(console, sock):
    log, ing = console()
    sock.sendto(json.dumps({"id": "m", "part": 0, "of": 2, "data": ""}).encode(), ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["received"] == 1)
    ing.stop()
    assert "at shutdown" in json.loads(ing.rejects.read_text(encoding="utf-8"))["reason"]


def test_the_rejects_file_stops_growing(console, sock, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_REJECTS_BYTES", 500)
    log, ing = console()
    for _ in range(50): sock.sendto(b"junk", ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == 50)
    text = ing.rejects.read_text(encoding="utf-8")
    assert len(text) < 700 and text.splitlines()[-1].count("file is full") == 1


def test_stop_without_start_frees_the_port(tmp_path):
    log = JevLog(tmp_path / "s.jevlog")
    ing = Ingest(log, port=0); port = ing.port; ing.stop()
    Ingest(log, port=port).stop(); log.close()


def test_every_datagram_stays_under_the_limit(sock, make_record):
    catch = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); catch.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    catch.bind(("127.0.0.1", 0)); catch.settimeout(2)
    send(make_record(0, state="z" * 300_000), sock, catch.getsockname())
    sizes = []
    try:
        while True: sizes.append(len(catch.recvfrom(65535)[0]))
    except socket.timeout:
        pass
    finally:
        catch.close()
    assert len(sizes) == 7 and max(sizes) <= ingest.MAX_DATAGRAM


def test_default_limits():
    assert (ingest.MAX_DATAGRAM, ingest.CHUNK_TIMEOUT_S, ingest.RING_SIZE, ingest.PORT) == (60_000, 5.0, 20_000, 7337)


def test_rejects_never_quote_a_field(console, sock):
    log, ing = console()
    wrong = {**{k: 7 for k in ingest.TEXT_FIELDS}, **{k: "SECRET-VALUE" for k in ingest.NUMBER_FIELDS}}
    for k, v in wrong.items(): sock.sendto(json.dumps({**GOOD, k: v, "note": "SECRET-VALUE"}).encode(), ("127.0.0.1", ing.port))
    wait_until(lambda: ing.counts["rejected"] == len(wrong))
    assert "SECRET-VALUE" not in ing.rejects.read_text(encoding="utf-8")


def test_replay_keeps_the_pace(make_record):
    catch = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); catch.bind(("127.0.0.1", 0))
    recs = [{**make_record(i), "ts": 1790400000 + i * 0.1} for i in range(21)]  # 2 s of traffic
    t0 = time.perf_counter(); replay(recs, speed=10, addr=catch.getsockname()); took = time.perf_counter() - t0
    catch.close()
    assert took == pytest.approx(0.2, abs=0.05)
    with pytest.raises(ValueError): replay(recs, speed=0, addr=("127.0.0.1", 9))


def test_replay_reads_records_as_they_are_and_turns_captured_jev_calls_into_records(tmp_path):
    """A capture file's /v1/systemone pairs become proxy records (DESIGN.md, "Replay"); other paths are skipped."""
    recs = demo.records(n=200)
    ok, limited = next(r for r in recs if r["status"] == 200), next(r for r in recs if r["status"] == 429)
    def pair(r, path="/v1/systemone", **headers):  # headers in any case, as the SDKs send them
        return {"ts": r["ts"], "path": path, "request_headers": {"X-TypeSafe-SDK": "typesafe-python/0.7.1", "X-TypeSafe-Runtime": "python/3.11", **headers},
                "response_headers": {"X-TypeSafe-Request-Id": r["request_id"]}, "status": r["status"], "latency_ms": r["latency_ms"] + 0.4,
                "request": r["request"], "response": r["response"] or r["error"]}
    no_path = {k: v for k, v in pair(ok).items() if k != "path"}
    lines = [ok, pair(ok, **{"X-TypeSafe-Retry-Count": "2", "X-Tarnlight-Label": "first-look"}), pair(ok, path="/v1/models"), no_path,
             pair(limited), pair(ok, **{"X-TypeSafe-Retry-Count": "two"})]
    f = tmp_path / "pairs.jsonl"; f.write_text("".join(json.dumps(x) + "\n" for x in lines) + "\n", encoding="utf-8")
    as_proxy = {"source": "proxy", "project": None, "label": None, "sdk": "python/0.7.1", "retry_count": 0}
    cost = round(ok["response"]["usage"]["input_tokens"] * 0.042)  # $0.042 per million input tokens, in millionths of a dollar
    assert read_records(f) == [ok, {**ok, **as_proxy, "label": "first-look", "retry_count": 2, "cost_est_micro": cost},
                               {**limited, **as_proxy, "cost_est_micro": 0},  # the error body kept, no answer, no tokens
                               {**ok, **as_proxy, "retry_count": None, "cost_est_micro": cost}]  # a retry count that is not a number: unknown
    f.write_text(json.dumps({k: v for k, v in pair(ok).items() if k != "status"}) + "\n", encoding="utf-8")
    with pytest.raises(KeyError): read_records(f)  # a pair missing a field stops the replay before anything is sent


def test_the_ring_keeps_only_what_the_window_reads_and_stays_under_its_byte_cap(console, sock, make_record, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_RING_BYTES", 5000)
    log, ing = console()
    for i in range(30):
        rec = make_record(i); rec["response"]["answers"]["verdict"]["reasoning"] = "x" * 20_000  # a big field the window never reads
        rec["response"]["usage"]["cache"] = {"big": "y" * 20_000}; rec["response"]["answers"]["odd"] = "z" * 20_000
        send(rec, sock, ("127.0.0.1", ing.port)); time.sleep(0.001)
    wait_until(lambda: ing.counts["stored"] == 30)
    entries = ing.snapshot()
    assert ing.ring_bytes <= 5000 and ing.ring_bytes == sum(len(line) for _, line in ing.ring)
    assert entries[-1]["seq"] == 29 and [e["seq"] for e in entries] == list(range(30 - len(entries), 30))
    assert set(entries[-1]["response"]["answers"]["verdict"]) == {"type", "choice", "confidence", "probabilities"}
    assert entries[-1]["response"]["usage"] == {"input_tokens": 300, "output_tokens": 60} and entries[-1]["response"]["answers"]["odd"] is None
    ing.stop()
    assert "reasoning" in json.dumps(saved(log)[-1])  # the log keeps the record whole


def test_the_ring_slims_odd_responses():
    assert ingest._slim("<html>502</html>") is None and ingest._slim({"answers": [1], "usage": 3}) == {"answers": None, "usage": None}
