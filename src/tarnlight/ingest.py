"""
Ingest: UDP datagrams on 127.0.0.1:7337 -> validated canonical records -> jevlog -> ring buffer.

  * one JSON record per datagram. A record over MAX_DATAGRAM bytes travels as chunks {"id", "part", "of", "data"},
    where data is base64 of that slice of the record's UTF-8 JSON, and is reassembled within CHUNK_TIMEOUT_S
  * only the envelope is checked at the door (v, ts, the text and number fields). request, response and error may be
    any JSON, because a failed call can carry a body the proxy could not parse, and the log keeps it verbatim
  * anything malformed goes to the rejects file with a reason, never a crash. The rejects file holds the reason, size
    and hash of a datagram, never its content, so a stray key or a hashed state cannot reach disk that way
  * two threads: the listener parses, validates and queues; the writer appends to the log and then puts the record,
    now with its seq, in the ring the UI reads. Nothing waits on the UI, and the ring only drops what is on disk
  * the ring holds each record as one compact JSON line: the envelope without request or error, and of the response
    only what the window reads (per answer: type, p, confidence, probabilities, legend, choice, score; token usage),
    plus "failed", a short tag for a failed call (model.failure), since the error body stays in the log.
    The inspector reads the full record from the log. Lines are plain strings, so they cost nothing for the garbage
    collector to walk, and the ring keeps at most RING_SIZE of them and MAX_RING_BYTES in all
  * limits keep memory bounded whatever a sender does: MAX_BACKLOG_BYTES of records received but not yet stored
    (beyond it a datagram is refused as "overloaded"), MAX_PENDING unfinished chunked records, and a rejects file
    that stops growing at MAX_REJECTS_BYTES (the count keeps going)
"""
import base64, collections, hashlib, json, math, queue, socket, threading, time, uuid
from pathlib import Path
from .model import failure
from .storage import ENVELOPE

PORT = 7337
MAX_DATAGRAM = 60_000
CHUNK_BYTES = 44_880            # a multiple of 3: its base64 (59,840) plus the chunk header stays under MAX_DATAGRAM
MAX_CHUNK_DATA = CHUNK_BYTES // 3 * 4
CHUNK_TIMEOUT_S = 5.0
MAX_PARTS, MAX_PENDING = 64, 256
RING_SIZE = 20_000
MAX_RING_BYTES = 64 << 20
RETRY_S = 1.0                   # how long the writer waits before retrying a record a full disk refused
MAX_BACKLOG_BYTES = 64 << 20
MAX_REJECTS_BYTES = 10 << 20
STOP_DRAIN_S = 1.0              # after stop(), how long to keep reading what already arrived
TEXT_FIELDS = ("source", "session_id", "tool_use_id", "project", "label", "sdk", "request_id")
NUMBER_FIELDS = ("latency_ms", "status", "retry_count", "cost_est_micro")
RING_FIELDS = tuple(k for k in ENVELOPE if k not in ("request", "error"))
ANSWER_FIELDS = ("type", "noul", "confidence", "probabilities", "legend", "choice", "score")


def _finite(x):  # an int of any size is finite; math.isfinite would overflow on a huge one
    return not isinstance(x, bool) and (isinstance(x, int) or isinstance(x, float) and math.isfinite(x))


def _slim(resp):
    """The part of a response the window reads: each answer's own fields and the token counts."""
    if not isinstance(resp, dict): return None
    answers, usage = resp.get("answers"), resp.get("usage")
    return {"answers": {q: {k: a[k] for k in ANSWER_FIELDS if k in a} if isinstance(a, dict) else None for q, a in answers.items()}
                       if isinstance(answers, dict) else None,
            "usage": {k: usage[k] for k in ("input_tokens", "output_tokens") if k in usage} if isinstance(usage, dict) else None}


def _no_constants(name): raise ValueError(f"{name} is not JSON")


def _finite_float(text):  # 1e999 is valid JSON text but parses to infinity: refuse it at the door like NaN
    x = float(text)
    if not math.isfinite(x): raise ValueError("a number too large for a float")
    return x


def _parse(data): return json.loads(data, parse_constant=_no_constants, parse_float=_finite_float)


def validate(rec):
    """None if rec's envelope (storage.ENVELOPE) is one the log can take, otherwise the reason it is not."""
    if not isinstance(rec, dict): return "not a JSON object"
    if type(rec.get("v")) is not int or rec["v"] != 1: return "v is not 1"
    if not _finite(rec.get("ts")): return "ts is not a number"
    for k in TEXT_FIELDS:
        if rec.get(k) is not None and not isinstance(rec[k], str): return f"{k} is not text"
    for k in NUMBER_FIELDS:
        if rec.get(k) is not None and not _finite(rec[k]): return f"{k} is not a number"
    return None


def send(rec, sock, addr=("127.0.0.1", PORT)):
    """Send one record: one datagram, or base64 chunks when it is larger than MAX_DATAGRAM."""
    data = json.dumps(rec, separators=(",", ":"), allow_nan=False).encode()
    if len(data) <= MAX_DATAGRAM:
        sock.sendto(data, addr); return
    parts = [data[i:i + CHUNK_BYTES] for i in range(0, len(data), CHUNK_BYTES)]
    if len(parts) > MAX_PARTS: raise ValueError(f"record is {len(data):,} bytes; the limit is {MAX_PARTS * CHUNK_BYTES:,}")
    mid = uuid.uuid4().hex
    for i, part in enumerate(parts):
        sock.sendto(json.dumps({"id": mid, "part": i, "of": len(parts), "data": base64.b64encode(part).decode()}).encode(), addr)


class Ingest:
    def __init__(self, log, port=PORT, rejects=None, ring_size=RING_SIZE, chunk_timeout_s=CHUNK_TIMEOUT_S):
        self.log = log
        self.rejects = Path(rejects) if rejects else log.path.with_suffix(".rejects.jsonl")
        self.rejects_bytes = self.rejects.stat().st_size if self.rejects.exists() else 0
        self.ring, self.ring_lock = collections.deque(), threading.Lock()  # (seq, JSON line)
        self.ring_size, self.ring_bytes = ring_size, 0
        self.queue, self.pending, self.chunk_timeout_s = queue.SimpleQueue(), {}, chunk_timeout_s
        self.backlog, self.backlog_lock = 0, threading.Lock()  # bytes received but not yet stored
        self.queued = self.handled = 0  # records queued, and taken off the queue by the writer (stored or refused), in order
        self.last_seq, self.mark_at, self.marked_seq = None, None, None  # see mark()
        self.counts, self.reject_lock = collections.Counter(), threading.Lock()  # received, stored, rejected
        self.stopping = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        try:
            self.sock.bind(("127.0.0.1", port))
        except OSError:
            self.sock.close()
            raise RuntimeError(f"UDP port {port} is in use: is another tarnlight console running?") from None
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.25)
        self.listener = threading.Thread(target=self._listen, name="tarnlight-ingest", daemon=True)
        self.writer = threading.Thread(target=self._write, name="tarnlight-writer", daemon=True)

    def start(self):
        self.listener.start(); self.writer.start(); return self

    def stop(self):
        """Stop listening, store everything already received, then return. The caller closes the log."""
        self.stopping.set()
        try:
            for t in (self.listener, self.writer):
                if t.ident is not None: t.join()  # a thread that never started has nothing to finish
        finally:
            self.sock.close()

    def snapshot(self):
        """The newest stored records (without request and error), oldest first, for the UI."""
        with self.ring_lock: lines = [line for _, line in self.ring]
        return [json.loads(line) for line in lines]

    def since(self, seq):
        """Stored records newer than seq, oldest first: what the UI has not seen yet. Costs only the new ones."""
        with self.ring_lock:
            new = []
            for s, line in reversed(self.ring):
                if s <= seq: break
                new.append(line)
        return [json.loads(line) for line in reversed(new)]

    # ---- listener thread
    def _listen(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except (socket.timeout, ConnectionResetError):
                self._expire(); continue
            self._take_safely(data); self._expire()
        self.sock.setblocking(False)
        deadline = time.monotonic() + STOP_DRAIN_S  # store what already arrived; a sender that never stops cannot hold up the exit
        while True:
            if time.monotonic() > deadline:
                self._reject(None, f"stopped reading {STOP_DRAIN_S:g} s after shutdown began; datagrams were still arriving"); break
            try: data, _ = self.sock.recvfrom(65535)
            except (BlockingIOError, ConnectionResetError, OSError): break
            self._take_safely(data)
        for p in self.pending.values():
            self._reject(None, f"chunked record incomplete at shutdown: {len(p['parts'])} of {p['of']} parts")
        self.pending.clear()

    def _take_safely(self, data):
        try:
            self._take(data)
        except Exception as e:  # a bug must never end the listener: every later decision would be lost
            self._reject(data, f"internal error: {type(e).__name__}")

    def _take(self, data, chunks_allowed=True):
        self.counts["received"] += 1
        try:
            msg = _parse(data)
        except (ValueError, RecursionError) as e:
            return self._reject(data, f"not JSON: {type(e).__name__}")
        if chunks_allowed and isinstance(msg, dict) and set(msg) == {"id", "part", "of", "data"}:
            return self._chunk(msg, data)
        if not self.offer(msg, data): self._reject(data, "overloaded: the writer is behind")

    def offer(self, msg, data):
        """Queue one parsed record for the writer: "queued", or "refused" when it went to the rejects file. False, and
        nothing done, when the writer is too far behind: a datagram is then refused, while the drop box keeps the line
        and tries again."""
        why = validate(msg)
        if why: self._reject(data, why); return "refused"
        with self.backlog_lock:  # one lock for the count and the queue, so the count is the queue's order
            if self.backlog + len(data) > MAX_BACKLOG_BYTES: return False
            self.backlog += len(data); self.queued += 1; self.queue.put((msg, len(data)))
        return "queued"

    def mark(self, count):
        """Set marked_seq to the seq of the newest record stored once the writer has handled `count` records (at once
        when it already has): where the drop box's catch-up ends in the log, -1 when none of it was stored."""
        with self.backlog_lock:
            if self.handled >= count: self.marked_seq = self.last_seq if self.last_seq is not None else -1
            else: self.mark_at = count

    def _chunk(self, c, raw):
        if not (isinstance(c["id"], str) and type(c["part"]) is int and type(c["of"]) is int and isinstance(c["data"], str)
                and 1 < c["of"] <= MAX_PARTS and 0 <= c["part"] < c["of"] and len(c["data"]) <= MAX_CHUNK_DATA):
            return self._reject(raw, "bad chunk header")
        if c["id"] not in self.pending and len(self.pending) >= MAX_PENDING:
            return self._reject(raw, "too many unfinished chunked records")
        p = self.pending.setdefault(c["id"], {"of": c["of"], "parts": {}, "t0": time.monotonic()})
        if p["of"] != c["of"]:
            del self.pending[c["id"]]; return self._reject(raw, "chunk count changed mid-record")
        p["parts"][c["part"]] = c["data"]
        if len(p["parts"]) < p["of"]: return
        del self.pending[c["id"]]
        try:
            whole = b"".join(base64.b64decode(p["parts"][i], validate=True) for i in range(p["of"]))
        except ValueError:
            return self._reject(raw, "chunk data is not base64")
        self.counts["received"] -= 1  # the chunks were counted; the record they make is not a new datagram
        self._take(whole, chunks_allowed=False)

    def _expire(self):
        now = time.monotonic()
        for mid in [m for m, p in self.pending.items() if now - p["t0"] > self.chunk_timeout_s]:
            p = self.pending.pop(mid)
            self._reject(None, f"chunked record incomplete after {self.chunk_timeout_s:g} s: {len(p['parts'])} of {p['of']} parts")

    def _reject(self, raw, reason):  # called from both threads
        entry = {"t": round(time.time(), 3), "reason": reason}
        if raw is not None: entry.update(bytes=len(raw), blake2b=hashlib.blake2b(raw, digest_size=8).hexdigest())
        with self.reject_lock:
            line = json.dumps(entry) + "\n"
            if self.rejects_bytes + len(line) > MAX_REJECTS_BYTES:  # the file stops growing; the count keeps going
                full = {"t": entry["t"], "reason": "the rejects file is full; later rejects are only counted"}
                line = "" if self.rejects_bytes >= MAX_REJECTS_BYTES else json.dumps(full) + "\n"
                self.rejects_bytes = MAX_REJECTS_BYTES
            else:
                self.rejects_bytes += len(line)
            if line:
                try:
                    with open(self.rejects, "a", encoding="utf-8", newline="\n") as f: f.write(line)
                except OSError:
                    pass  # a full disk must not stop ingest; the count still shows it
            self.counts["rejected"] += 1  # after the write, so whoever sees the count finds the line

    # ---- writer thread
    def _write(self):
        while True:
            try:
                msg, size = self.queue.get(timeout=0.25)
            except queue.Empty:
                # the listener is gone, so nothing more can arrive: only an empty queue means done
                if self.stopping.is_set() and not self.listener.is_alive() and self.queue.empty(): return
                self.log.tick(); continue
            try:
                self._store(msg)
            except Exception as e:  # never let one record end the writer
                self._reject(json.dumps(msg).encode(), f"not stored: {type(e).__name__}")
            finally:
                with self.backlog_lock:
                    self.backlog -= size; self.handled += 1
                    if self.handled == self.mark_at: self.marked_seq = self.last_seq if self.last_seq is not None else -1

    def _store(self, rec):
        while True:
            try:
                seq = self.log.append(rec); self.last_seq = seq; break
            except OSError:  # e.g. a full disk: keep the record and try again, unless shutting down
                if self.stopping.is_set(): raise
                time.sleep(RETRY_S)
        kept = self.log.privacy.apply({**{k: v for k, v in rec.items() if k in ENVELOPE}, "seq": seq})  # as the log keeps it
        slim = {k: kept[k] for k in RING_FIELDS if k in kept}
        if "response" in slim: slim["response"] = _slim(slim["response"])
        if tag := failure(kept): slim["failed"] = tag
        line = json.dumps(slim, separators=(",", ":"))
        with self.ring_lock:
            self.ring.append((seq, line)); self.ring_bytes += len(line)
            while len(self.ring) > 1 and (len(self.ring) > self.ring_size or self.ring_bytes > MAX_RING_BYTES):
                self.ring_bytes -= len(self.ring.popleft()[1])  # the oldest are already on disk
        self.counts["stored"] += 1
