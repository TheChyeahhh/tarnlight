"""
jevlog: the session log format.

  * records are the canonical record: the wire request and response verbatim plus an envelope, as plain JSON
  * grouped into blocks of BLOCK_N records, or flushed every FLUSH_S seconds, or on close
  * live blocks compressed with zstd level 3; compact() recompresses at level 19 and merges blocks up to 4 * BLOCK_N
  * uncompressed index rows, one per call and one per (call, question), so charts, counters and queries never
    decompress a block
  * a hot file (<name>.hot.jsonl) holds every record until its block is committed, so a crash loses nothing
  * question sets are interned in `schema`
  * the privacy mode (full | hash | redact) is applied before the hot file is written and again on export

File: one SQLite (WAL). Tables: meta, schema, block, call, qname, answer, outcome.
`answer` keeps question names as small numbers (`qname`) and is stored in (question, seq) order, so one question's
chart series is a single range read (6.0 MB instead of 9.5 MB per 20 min at 10 Hz, measured).
A call's other answers come from its record, which the inspector decompresses anyway.

Contracts:
  * one writer per file, held by an OS lock on <name>.lock for the writer's lifetime (released if the process dies)
  * a file keeps the privacy mode it was created with; opening it with a different one is refused
  * append() raises only when the record was NOT stored: not JSON (NaN, Infinity, bytes), keys that are not text, nesting
    deeper than MAX_DEPTH, a line over MAX_LINE, or a hot-file write that failed. Once it is in the hot file, append
    returns its seq. A failed flush (locked or full database) is kept in `last_error` and retried after FLUSH_S
  * a record that cannot be indexed is still stored verbatim; its index fields are NULL. Bad input never jams the log

Design notes: seq is assigned in append() and stored inside the record, so
recovery can skip records a crash left in the hot file after their block was committed; records are stored with
their wire key order (verbatim); the call table, not block ranges, maps seq to block.
"""
import csv, hashlib, json, math, os, re, sqlite3, sys, tempfile, threading, time, zipfile
from itertools import repeat
from pathlib import Path
import zstandard as zstd

FORMAT_VERSION = "1"
BLOCK_N, FLUSH_S = 512, 5.0
LIVE_LEVEL, COMPACT_LEVEL = 3, 19
HASHED = "tarnlight_hashed_state"  # marker key that replaces the state in privacy mode "hash"
ENVELOPE = ("v", "ts", "seq", "source", "session_id", "tool_use_id", "project", "label", "sdk", "request_id",
            "latency_ms", "status", "retry_count", "cost_est_micro", "request", "response", "error")  # the canonical envelope; nothing else is stored
MAX_DEPTH = 200                                  # far below Python's recursion limit, so every reader can parse what was stored
MAX_LINE, MAX_BLOCK_BYTES = 16 << 20, 64 << 20   # one record; one block (SQLite refuses blobs over 1 GB)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS schema(id TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS block(id INTEGER PRIMARY KEY, first_seq INTEGER, n INTEGER, level INTEGER, body BLOB);
CREATE TABLE IF NOT EXISTS call(seq INTEGER PRIMARY KEY, ts_ms INTEGER, block_id INTEGER, source TEXT, project TEXT, session_id TEXT,
  schema_id TEXT, request_id TEXT, latency_ms INTEGER, status INTEGER, retry_count INTEGER, sdk TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost_est_micro INTEGER, state_bytes INTEGER, state_hash TEXT);
CREATE TABLE IF NOT EXISTS qname(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS answer(seq INTEGER, qid INTEGER, qtype TEXT, confidence INTEGER, margin INTEGER, top_prob INTEGER,
  p_yes INTEGER, chosen TEXT, PRIMARY KEY(qid, seq)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outcome(seq INTEGER, qname TEXT, outcome TEXT, note TEXT, graded_at INTEGER, PRIMARY KEY(seq, qname));
CREATE INDEX IF NOT EXISTS ix_call_ts ON call(ts_ms);
"""

def _dump(o): return json.dumps(o, separators=(",", ":"), allow_nan=False)            # plain JSON, wire key order kept
def _canon(o): return json.dumps(o, separators=(",", ":"), sort_keys=True).encode()   # state hashing: order-independent
def _hash(b): return hashlib.blake2b(b, digest_size=8).hexdigest()
def _num(x): return not isinstance(x, bool) and (isinstance(x, int) or isinstance(x, float) and math.isfinite(x))  # ints: no overflow
def _int(x): return int(x) if _num(x) and abs(x) < 2 ** 63 else None               # anything else is NULL, never an error
def _text(x): return x.encode("utf-8", "backslashreplace").decode() if isinstance(x, str) else None  # distinct names stay distinct
def _pm(x): return _int(round(x * 1000)) if _num(x) else None                       # per-mille
def _envelope(rec): return {k: v for k, v in rec.items() if k in ENVELOPE}

def _check(rec):
    """Refuse, before anything is written, what could not be read back safely: deep nesting and keys that are not text."""
    stack = [(rec, 1)] if isinstance(rec, (dict, list, tuple)) else []
    while stack:  # only containers are visited
        x, depth = stack.pop()
        if depth > MAX_DEPTH: raise ValueError(f"nested deeper than {MAX_DEPTH} levels")
        if isinstance(x, dict):
            if not all(map(isinstance, x, repeat(str))): raise TypeError("every key must be text")
            x = x.values()
        stack.extend((v, depth + 1) for v in x if isinstance(v, (dict, list, tuple)))

def _marker(x):
    b = _canon(x)
    return {HASHED: {"blake2b": _hash(b), "keys": list(x) if isinstance(x, dict) else None, "bytes": len(b)}}

def _hide_echoes(error):
    """A validation error can echo the input that failed, which may be the state (a 422's detail[*].input)."""
    detail = error.get("detail") if isinstance(error, dict) else None
    echo = lambda d: isinstance(d, dict) and "input" in d and not _is_hashed(d["input"])
    if not isinstance(detail, list) or not any(echo(d) for d in detail): return error
    return {**error, "detail": [{**d, "input": _marker(d["input"])} if echo(d) else d for d in detail]}

def _is_hashed(state):
    m = state.get(HASHED) if isinstance(state, dict) and len(state) == 1 else None
    return (isinstance(m, dict) and set(m) == {"blake2b", "keys", "bytes"} and isinstance(m["blake2b"], str)
            and re.fullmatch(r"[0-9a-f]{16}", m["blake2b"]) is not None and type(m["bytes"]) is int and 0 <= m["bytes"] < 2 ** 63
            and (m["keys"] is None or isinstance(m["keys"], list) and all(isinstance(k, str) for k in m["keys"])))

class Privacy:
    """full: store as is. hash: replace request.state with its blake2b, top-level key names and byte size (a request that
    is text or a list is replaced whole, since it holds the state too; so is any input a validation error echoes back).
    redact: drop each listed dotted path, which starts at request, response or error ("*" = every item).
    A dropped list item becomes null, so the other items keep their positions."""
    def __init__(self, mode="full", paths=()):
        if mode not in ("full", "hash", "redact"): raise ValueError(f"unknown privacy mode {mode!r}")
        if isinstance(paths, (str, bytes)): raise TypeError("paths must be a list of dotted paths, not one string")
        self.mode, self.paths = mode, [p.split(".") for p in sorted(set(paths))]  # order and repeats do not matter
        if mode == "redact" and not self.paths: raise ValueError("redact mode needs at least one path")
        for p in self.paths:
            if "" in p or p[0] not in ("request", "response", "error"):
                raise ValueError(f"bad redact path {'.'.join(p)!r}: it must start with request, response or error, with no empty parts")

    def describe(self): return _dump({"mode": self.mode, "paths": [".".join(p) for p in self.paths]})

    @classmethod
    def from_description(cls, text): d = json.loads(text); return cls(d["mode"], d["paths"])

    def apply(self, rec):
        """Return the record as this mode stores it. Idempotent; never mutates the input."""
        if self.mode == "full": return rec
        req = rec.get("request")
        if self.mode == "hash":
            error = _hide_echoes(rec.get("error"))
            if error is not rec.get("error"): rec = {**rec, "error": error}
            if req is not None and not isinstance(req, dict): return {**rec, "request": _marker(req)}  # text or a list holds the state too
            if not isinstance(req, dict) or "state" not in req or _is_hashed(req["state"]): return rec
            return {**rec, "request": {**req, "state": _marker(req["state"])}}
        rec = json.loads(_dump(rec))
        for p in self.paths: _drop(rec, p)
        return rec

def _drop(node, parts):
    head, rest = parts[0], parts[1:]
    if isinstance(node, dict):
        for k in (list(node) if head == "*" else [head] if head in node else []):
            if rest: _drop(node[k], rest)
            else: del node[k]
    elif isinstance(node, list) and (head == "*" or re.fullmatch(r"[0-9]+", head)):
        for i in (range(len(node)) if head == "*" else [int(head)] if int(head) < len(node) else []):
            if rest: _drop(node[i], rest)
            else: node[i] = None

def summarize(rec):
    """Index rows for one record: the call row (dict), one answer row per question (tuples, per-mille ints) and the
    question set as JSON. Odd shapes give NULL fields or a skipped answer, never an error."""
    req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
    resp = rec.get("response") if isinstance(rec.get("response"), dict) else {}
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    state = req.get("state")
    if _is_hashed(state): state_hash, state_bytes = state[HASHED]["blake2b"], _int(state[HASHED]["bytes"])
    else: b = _canon(state); state_hash, state_bytes = _hash(b), len(b)
    questions = _dump(req.get("questions"))
    ts = rec.get("ts")
    call = dict(seq=rec["seq"], ts_ms=_int(ts * 1000) if _num(ts) else None, source=_text(rec.get("source")),
                project=_text(rec.get("project")), session_id=_text(rec.get("session_id")), schema_id=_hash(questions.encode()),
                request_id=_text(rec.get("request_id")), latency_ms=_int(rec.get("latency_ms")), status=_int(rec.get("status")),
                retry_count=_int(rec.get("retry_count")), sdk=_text(rec.get("sdk")), tokens_in=_int(usage.get("input_tokens")),
                tokens_out=_int(usage.get("output_tokens")), cost_est_micro=_int(rec.get("cost_est_micro")),
                state_bytes=state_bytes, state_hash=state_hash)
    answers = []
    for qname, a in (resp["answers"].items() if isinstance(resp.get("answers"), dict) else ()):
        try:
            if a["type"] == "noul":  # no confidence field on the wire: derive it, keep p itself
                p = a["noul"]; c = max(p, 1 - p)
                row = ("noul", _pm(c), _pm(abs(2 * p - 1)), _pm(c), _pm(p), "true" if p >= 0.5 else "false")
            elif a["type"] in ("choice", "score"):
                ps = sorted(a["probabilities"].values(), reverse=True)
                row = (a["type"], _pm(a["confidence"]), _pm(ps[0] - (ps[1] if len(ps) > 1 else 0)), _pm(ps[0]), None,
                       _text(a["choice"] if a["type"] == "choice" else str(a["score"])))
            else:
                continue
            answers.append((rec["seq"], _text(qname), *row))
        except Exception:
            continue  # an answer that cannot be indexed: the record is still stored verbatim
    return call, answers, questions

CALL_COLS = ("seq", "ts_ms", "block_id", "source", "project", "session_id", "schema_id", "request_id", "latency_ms", "status",
             "retry_count", "sdk", "tokens_in", "tokens_out", "cost_est_micro", "state_bytes", "state_hash")

def _lock_exclusive(f, wait_s=5.0):
    """Hold an OS lock for the writer's lifetime. The OS drops it if the process dies, but Windows may take a moment
    after a kill, so a relaunch waits up to wait_s before deciding another writer really holds it."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt; f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl; fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() > deadline:
                f.close(); raise RuntimeError(f"{f.name} is held by another tarnlight writer; a session file has one writer") from None
            time.sleep(0.05)

class JevLog:
    def __init__(self, path, privacy=None):
        self.path = Path(os.path.realpath(path))  # one name per file, so a short name or a trailing dot cannot dodge the lock
        self.hot, self.closed = self.path.with_suffix(".hot.jsonl"), False
        self.lock_f = open(self.path.with_suffix(".lock"), "a+b"); _lock_exclusive(self.lock_f)
        self.con = sqlite3.connect(self.path, check_same_thread=False, timeout=1.0)
        try:
            self._open(privacy)
        except BaseException:  # a refused open must not keep the file locked
            if hasattr(self, "hot_f"): self.hot_f.close()
            self.con.close(); self.lock_f.close(); raise

    def _open(self, privacy):
        self.con.execute("PRAGMA journal_mode=WAL"); self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMA_SQL)
        meta = dict(self.con.execute("SELECT k, v FROM meta"))
        if meta.get("format_version", FORMAT_VERSION) != FORMAT_VERSION:
            raise ValueError(f"{self.path.name} is format {meta['format_version']}, this build reads format {FORMAT_VERSION}")
        recorded = Privacy.from_description(meta["privacy"]) if "privacy" in meta else None
        if recorded and privacy is not None and privacy.describe() != recorded.describe():
            raise ValueError(f"{self.path.name} was recorded with privacy {meta['privacy']}; open it with that mode or start a new session")
        self.privacy = recorded or privacy or Privacy()
        if not recorded or "format_version" not in meta:  # a reopen writes nothing, so a busy database cannot block it
            with self.con:
                self.con.execute("INSERT OR IGNORE INTO meta VALUES('format_version', ?)", (FORMAT_VERSION,))
                self.con.execute("INSERT OR IGNORE INTO meta VALUES('privacy', ?)", (self.privacy.describe(),))
        self.lock = threading.Lock(); self.buf = []  # (seq, line) pairs: the block is built from the exact hot-file lines
        self.last_block = None  # (block id, its lines): the inspector reads neighbouring records. Ids are never reused (a merged
                                # block is inserted before the old ones are deleted), so a cached id never names other lines
        self.last_flush, self.retry_at, self.last_error = time.monotonic(), 0.0, None
        self.seq = self.con.execute("SELECT COALESCE(MAX(seq), -1) FROM call").fetchone()[0] + 1
        self.qids = dict(self.con.execute("SELECT name, id FROM qname"))
        self.cz = zstd.ZstdCompressor(level=LIVE_LEVEL)
        self._recover_hot()

    def _recover_hot(self):
        seen = set()
        if self.hot.exists():
            with open(self.hot, encoding="utf-8-sig", errors="replace") as f:  # -sig: an editor may have added a BOM
                for line in f:
                    try:
                        r = json.loads(line); s = r["seq"]
                        # below self.seq: committed before the crash cleared the hot file; seen: never commit a seq twice;
                        # the upper bound keeps a hand-edited seq from poisoning every later one
                        if type(s) is int and self.seq <= s < 2 ** 63 - 1 and s not in seen:
                            self.buf.append((s, _dump(self.privacy.apply(_envelope(r))))); seen.add(s)
                    except Exception:
                        continue  # the line being written when the process died
        self.hot_f = open(self.hot, "ab", buffering=0)
        if os.fstat(self.hot_f.fileno()).st_size:
            self.hot_f.write(b"\n")  # never glue the next record onto a cut-off line
        if self.buf:
            self.buf.sort(); self.seq = max(seen) + 1; self._try_flush()
        else:
            self._clear_hot()

    def _clear_hot(self):
        try:
            self.hot_f.truncate(0)
        except OSError:
            pass  # another program holds the file; the committed lines left in it are skipped by seq on recovery

    def append(self, rec):
        """Store one canonical record and return its seq. Raises only if the record was not stored."""
        with self.lock:
            _check(rec)
            rec = self.privacy.apply({**_envelope(rec), "seq": self.seq})
            line = _dump(rec)  # raises on NaN, Infinity or anything that is not JSON: nothing stored
            if len(line) > MAX_LINE: raise ValueError(f"record is {len(line):,} bytes; the limit is {MAX_LINE:,}")
            pos = os.fstat(self.hot_f.fileno()).st_size; data = memoryview((line + "\n").encode())
            try:
                while data: data = data[self.hot_f.write(data):]
            except OSError:
                self.hot_f.truncate(pos); raise  # a half-written line would swallow the next record after a crash
            self.buf.append((self.seq, line)); self.seq += 1
            if len(self.buf) >= BLOCK_N or time.monotonic() - self.last_flush >= FLUSH_S: self._try_flush()
            return rec["seq"]

    def _try_flush(self):
        """Flush without raising. The records are safe in the hot file; a failure is kept and retried after FLUSH_S."""
        if time.monotonic() < self.retry_at: return
        try:
            self._flush()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"; self.retry_at = time.monotonic() + FLUSH_S

    def _flush(self):
        if not self.buf: return
        chunks, chunk, size = [], [], 0  # at most BLOCK_N records and MAX_BLOCK_BYTES per block
        for item in self.buf:
            if chunk and (len(chunk) >= BLOCK_N or size + len(item[1]) > MAX_BLOCK_BYTES): chunks.append(chunk); chunk, size = [], 0
            chunk.append(item); size += len(item[1])
        chunks.append(chunk)
        try:
            new_qids = self._commit(chunks, careful=False)
        except Exception:  # some record is odd: redo the batch with a savepoint around each record
            new_qids = self._commit(chunks, careful=True)
        self.qids.update(new_qids)
        self.buf = []; self.last_flush = time.monotonic(); self.last_error, self.retry_at = None, 0.0
        self._clear_hot()

    def _commit(self, chunks, careful):
        """Write the blocks and their index rows in one transaction; return the question names it added.
        careful: one savepoint per record, so a record whose index rows fail is kept with NULL index fields."""
        new_qids = {}  # joins the cache only once the transaction commits
        def qid(name):
            if name not in self.qids and name not in new_qids:
                self.con.execute("INSERT OR IGNORE INTO qname(name) VALUES(?)", (name,))
                new_qids[name] = self.con.execute("SELECT id FROM qname WHERE name=?", (name,)).fetchone()[0]
            return self.qids[name] if name in self.qids else new_qids[name]
        def index(bid, call, answers, questions):
            call["block_id"] = bid
            if questions is not None: self.con.execute("INSERT OR IGNORE INTO schema VALUES(?,?)", (call["schema_id"], questions))
            self.con.execute(f"INSERT INTO call VALUES({','.join('?' * len(CALL_COLS))})", [call[c] for c in CALL_COLS])
            self.con.executemany("INSERT OR IGNORE INTO answer VALUES(?,?,?,?,?,?,?,?)", [(a[0], qid(a[1]), *a[2:]) for a in answers if a[1]])
        with self.con:
            for chunk in chunks:
                body = self.cz.compress("\n".join(line for _, line in chunk).encode())
                bid = self.con.execute("INSERT INTO block(first_seq,n,level,body) VALUES(?,?,?,?)",
                                       (chunk[0][0], len(chunk), LIVE_LEVEL, body)).lastrowid
                for seq, line in chunk:
                    if not careful:
                        index(bid, *summarize(json.loads(line))); continue
                    self.con.execute("SAVEPOINT rec"); names = dict(new_qids)
                    try:
                        index(bid, *summarize(json.loads(line)))
                    except Exception:  # never let one record block the others: keep it, with NULL index fields
                        self.con.execute("ROLLBACK TO rec"); new_qids.clear(); new_qids.update(names)
                        index(bid, {**dict.fromkeys(CALL_COLS), "seq": seq}, [], None)
                    self.con.execute("RELEASE rec")
        return new_qids

    def tick(self):
        """Call from a timer so a quiet session still flushes within FLUSH_S."""
        with self.lock:
            if self.buf and time.monotonic() - self.last_flush >= FLUSH_S: self._try_flush()

    def close(self):
        """Flush and close. If the flush fails, the records stay in the hot file and the next open recovers them."""
        with self.lock:
            if self.closed: return
            try:
                self._flush(); self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.closed = True; self.hot_f.close(); self.con.close(); self.lock_f.close()

    # ---- reading ----
    def iter_records(self, seq_from=0, seq_to=None):
        """Yield (seq, record) in seq order from committed blocks (flush first to include the buffer). Each block is
        looked up when it is needed, so a compaction running in between cannot pull it away."""
        hi = min(seq_to, 2 ** 63 - 1) if seq_to is not None else 2 ** 63 - 1; dz = zstd.ZstdDecompressor(); nxt = max(seq_from, 0)
        q = "SELECT body FROM block WHERE id=(SELECT block_id FROM call WHERE seq BETWEEN ? AND ? ORDER BY seq LIMIT 1)"
        while nxt <= hi:
            with self.lock: row = self.con.execute(q, (nxt, hi)).fetchone()
            if row is None: return
            recs = [json.loads(l) for l in dz.decompress(row[0]).decode().split("\n")]
            for r in recs:
                if nxt <= r["seq"] <= hi: yield r["seq"], r
            nxt = max(recs[-1]["seq"], nxt) + 1

    def compact(self):
        """Recompress live blocks at COMPACT_LEVEL; merge neighbours up to 4 * BLOCK_N records. Run on session close / idle."""
        with self.lock:
            self._flush()
            dz = zstd.ZstdDecompressor(); cz = zstd.ZstdCompressor(level=COMPACT_LEVEL)
            runs, run, run_n, run_bytes = [], [], 0, 0
            for bid, n, level, size in self.con.execute("SELECT id, n, level, LENGTH(body) FROM block ORDER BY first_seq").fetchall():
                if run and (run_n + n > 4 * BLOCK_N or run_bytes + size > MAX_BLOCK_BYTES): runs.append(run); run, run_n, run_bytes = [], 0, 0
                run.append((bid, level)); run_n += n; run_bytes += size
            if run: runs.append(run)
            with self.con:
                for run in runs:
                    if len(run) == 1 and run[0][1] == COMPACT_LEVEL: continue  # already compacted, nothing to merge
                    ids = [bid for bid, _ in run]; marks = ",".join("?" * len(ids))
                    rows = self.con.execute(f"SELECT first_seq, body FROM block WHERE id IN ({marks}) ORDER BY first_seq", ids).fetchall()
                    lines = [l for _, body in rows for l in dz.decompress(body).decode().split("\n")]
                    new = self.con.execute("INSERT INTO block(first_seq,n,level,body) VALUES(?,?,?,?)",
                                           (rows[0][0], len(lines), COMPACT_LEVEL, cz.compress("\n".join(lines).encode()))).lastrowid
                    self.con.execute(f"UPDATE call SET block_id=? WHERE block_id IN ({marks})", [new, *ids])
                    self.con.execute(f"DELETE FROM block WHERE id IN ({marks})", ids)
            self.con.execute("VACUUM")

    def export_jsonl(self, out_path, seq_from=0, seq_to=None, privacy=None):
        """Write records as JSONL, passing each through the privacy filter again (this log's mode unless one is given)."""
        with self.lock: self._flush()
        p = privacy or self.privacy
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            for _, r in self.iter_records(seq_from, seq_to): f.write(_dump(p.apply(_envelope(r))) + "\n")

    def export_csv(self, out_path):
        """One row per (call, question) from the index tables with its outcome; a call without answers gets one row.
        Values stay as stored (per-mille). No state is in it, so it is the same in every privacy mode."""
        with self.lock: self._flush()
        reader = sqlite3.connect(self.path)  # a second connection reads committed data without holding up the writer
        try:
            rows = reader.execute(f"SELECT {', '.join(CSV_SQL)} FROM call c LEFT JOIN answer a ON a.seq = c.seq "
                                  "LEFT JOIN qname q ON q.id = a.qid LEFT JOIN outcome o ON o.seq = c.seq AND o.qname = q.name "
                                  "ORDER BY c.seq, q.name")
            with open(out_path, "w", encoding="utf-8", newline="") as f:
                out = csv.writer(f); out.writerow(CSV_COLS); out.writerows(rows)
        finally:
            reader.close()

    def export_bundle(self, zip_path, privacy=None):
        """A session to share: a compacted .jevlog rebuilt through the privacy filter, with its outcomes, plus a README
        of the format, zipped. Never the hot file or the lock."""
        p = privacy or self.privacy
        with self.lock: self._flush()
        with tempfile.TemporaryDirectory() as tmp:
            copy = JevLog(Path(tmp) / "session.jevlog", privacy=p)
            renumbered = {seq: copy.append(r) for seq, r in self.iter_records()}  # append applies the privacy filter
            with copy.lock: copy._flush()
            with self.lock: graded = self.con.execute("SELECT seq, qname, outcome, note, graded_at FROM outcome").fetchall()
            with copy.con:
                copy.con.executemany("INSERT INTO outcome VALUES(?,?,?,?,?)", [(renumbered[s], *rest) for s, *rest in graded if s in renumbered])
            copy.compact(); copy.close()
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(Path(tmp) / "session.jevlog", "session.jevlog"); z.writestr("README.txt", BUNDLE_README)

    # ---- grading and the inspector
    def grade(self, seq, qname, outcome, note=None):
        """Record how a decision turned out (one of OUTCOMES), or clear it with outcome None."""
        if outcome is not None and outcome not in OUTCOMES: raise ValueError(f"outcome must be one of {OUTCOMES} or None")
        with self.lock:
            line = next((line for s, line in self.buf if s == seq), None)  # the window shows a decision before its block commits
            if line is not None: known = qname in self._answer_names(line)  # checked from its own line: no flush here
            else: known = self.con.execute("SELECT 1 FROM answer WHERE qid=(SELECT id FROM qname WHERE name=?) AND seq=?", (qname, seq)).fetchone()
            if not known: raise KeyError(f"no decision {qname!r} in call #{seq}")
            with self.con:
                if outcome is None: self.con.execute("DELETE FROM outcome WHERE seq=? AND qname=?", (seq, qname))
                else: self.con.execute("INSERT OR REPLACE INTO outcome VALUES(?,?,?,?,?)", (seq, qname, outcome, note, int(time.time() * 1000)))

    @staticmethod
    def _answer_names(line):
        """The question names the index will hold for one stored line (none if it cannot be indexed)."""
        try: return {a[1] for a in summarize(json.loads(line))[1]}
        except Exception: return set()

    def outcomes(self):
        """{(seq, question): outcome} for every graded decision."""
        with self.lock: return {(s, q): o for s, q, o in self.con.execute("SELECT seq, qname, outcome FROM outcome")}

    def record(self, seq):
        """The full stored record for seq, for the inspector (the one place a block is decompressed), or None. Only the
        wanted line is parsed, and the last block read is kept: the sender's previous call is usually in it."""
        with self.lock:
            for s, line in self.buf:
                if s == seq: return json.loads(line)
            row = self.con.execute("SELECT b.id, b.first_seq, b.body FROM call c JOIN block b ON b.id = c.block_id WHERE c.seq=?",
                                   (seq,)).fetchone()
            if row is None: return None
            bid, first, body = row
            if self.last_block is None or self.last_block[0] != bid:
                self.last_block = (bid, zstd.ZstdDecompressor().decompress(body).decode().split("\n"))
            lines = self.last_block[1]
        i = seq - first  # a block holds consecutive seqs; check, and scan if some block ever does not
        if 0 <= i < len(lines):
            r = json.loads(lines[i])
            if r.get("seq") == seq: return r
        return next((r for r in map(json.loads, lines) if r.get("seq") == seq), None)

    def previous_seq(self, seq, source, project):
        """The latest call before seq from the same sender (source and project), or None: for "changed since"."""
        with self.lock:
            for s, line in reversed(self.buf):
                if s < seq:
                    r = json.loads(line)
                    if r.get("source") == source and r.get("project") == project: return s
            row = self.con.execute("SELECT seq FROM call WHERE seq < ? AND source IS ? AND project IS ? ORDER BY seq DESC LIMIT 1",
                                   (seq, _text(source), _text(project))).fetchone()  # the index spelling: a lone surrogate cannot reach SQLite
        return row[0] if row else None


OUTCOMES = ("correct", "wrong", "flagged")
CSV_COLS = ("seq", "ts_ms", "source", "project", "session_id", "request_id", "status", "latency_ms", "tokens_in", "tokens_out",
            "cost_est_micro", "question", "qtype", "confidence", "margin", "top_prob", "p_yes", "chosen", "outcome", "note", "graded_at")
CSV_SQL = [f"c.{c}" for c in CSV_COLS[:11]] + ["q.name"] + [f"a.{c}" for c in CSV_COLS[12:18]] + [f"o.{c}" for c in CSV_COLS[18:]]
BUNDLE_README = """tarnlight session bundle

session.jevlog is a SQLite file. Tables:
  block    zstd-compressed blocks of records, one JSON record per line (the wire request and response, verbatim,
           inside an envelope: v, ts, seq, source, session_id, tool_use_id, project, label, sdk, request_id,
           latency_ms, status, retry_count, cost_est_micro, request, response, error)
  call     one row per call: seq, time in ms, block, source, project, session, request id, latency, status,
           tokens, estimated cost in millionths of a dollar, state size and hash
  qname    question names; answer rows refer to them by id
  answer   one row per (call, question): confidence, margin, top probability, p(yes) for yes/no questions,
           all in thousandths, and the chosen answer
  outcome  how graded decisions turned out: correct, wrong or flagged, with an optional note
  meta     format version and the privacy mode the records were stored with
The records were passed through the privacy mode named in meta before they were written.
Open it with tarnlight, or with any SQLite tool and zstandard to read the blocks.
"""
