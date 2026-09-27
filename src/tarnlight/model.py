"""
What the console shows, computed from stored records as they arrive. No Qt here, so it tests headless.

Every chart series is one question name. Values are percent (the index's per-mille / 10). Rates use the time a record
reached the console, so a replay at 10x shows 10x the rate; everything else comes from the record itself.
"""
import collections, math, time
import numpy as np
from .storage import _text, summarize

CHART_POINTS = 20_000       # per question: a ring of the newest 20k points
DEFAULT_BANDS = (40, 70)    # escalate below 40, review below 70, auto at 70 and above
RATE_LIMIT = 1_200          # TypeSafe requests per minute (its rate limit)
CHIP_WINDOW = 1_000         # the default chip is the most frequent question of the last N decisions
SWITCH_MARGIN = 1.25        # the automatic choice moves only to a question clearly more frequent than the current one
MAX_QUESTIONS = 1_000       # names tracked for the chart; beyond it the least recently seen is dropped (never below CHIP_WINDOW)


class Series:
    """One question's chart points, oldest first; the newest CHART_POINTS are kept. Numpy rows (seq, time, confidence,
    second line, margin), so the chart draws them without converting lists every frame. The second line is the top
    probability for choice and score, and p(yes) for noul (p itself is kept for the chart)."""
    def __init__(self):
        self._rows, self.n = np.empty((5, min(256, 2 * CHART_POINTS))), 0

    def add(self, seq, ts, conf, second, margin):
        cap = self._rows.shape[1]
        if self.n == cap:
            if cap < 2 * CHART_POINTS: self._rows = np.concatenate([self._rows, np.empty((5, min(cap, 2 * CHART_POINTS - cap)))], axis=1)
            else: self._rows[:, :CHART_POINTS] = self._rows[:, cap - CHART_POINTS:]; self.n = CHART_POINTS  # trim in bulk
        self._rows[:, self.n] = (seq, ts, conf, second, margin); self.n += 1

    seq = property(lambda self: self._rows[0, :self.n])
    conf = property(lambda self: self._rows[2, :self.n])

    def last(self, n=None, upto=None):
        """The newest n points (at most CHART_POINTS), optionally only those with seq <= upto: five arrays,
        seq, time, confidence, second line, margin."""
        end = self.n if upto is None else int(np.searchsorted(self._rows[0, :self.n], upto, side="right"))
        k = min(end, CHART_POINTS if n is None else min(n, CHART_POINTS))
        return tuple(self._rows[:, end - k:end])


def source_label(rec):
    """How a sender is named on its chip: source, and project when there is one."""
    return " · ".join(str(x) for x in (rec.get("source") or "unknown", rec.get("project")) if x)


def failure(rec):
    """A short tag for a failed call, or None when it answered: 401, 429, timeout, network, unreachable, cut off, no
    answer (a 200 without answers). The ring keeps it as "failed", since it leaves the error body in the log."""
    if "failed" in rec: return rec["failed"]
    error, status, response = rec.get("error"), rec.get("status"), rec.get("response")
    answers = response.get("answers") if isinstance(response, dict) else None
    if status in (200, None) and isinstance(answers, dict) and answers: return None  # it answered, notes or not
    said = str(error.get("jev") or error.get("proxy") or "") if isinstance(error, dict) else ""
    if "cut off" in said: return "cut off"
    if "could not be reached" in said: return "unreachable"
    if isinstance(status, int) and not isinstance(status, bool) and status != 200: return str(status)
    return said[:60] or "no answer"


def top_two(answer):
    """The two most likely options of an answer as (label, percent), for the gauge. Odd shapes give fewer or none."""
    answer = answer if isinstance(answer, dict) else {}
    t = answer.get("type")
    if t == "noul":
        p = answer.get("noul")
        return sorted([("yes", 100 * p), ("no", 100 * (1 - p))], key=lambda x: -x[1]) if _prob(p) else []
    probs, legend = answer.get("probabilities"), answer.get("legend")
    probs = {k: v for k, v in probs.items() if _prob(v)} if isinstance(probs, dict) else {}
    legend = legend if isinstance(legend, dict) else {}
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])[:2]
    return [(str(legend.get(k, k)) if t == "score" else str(k), 100 * v) for k, v in ranked]


def plain_words(answer):
    """The gauge's line under its number, in plain words: what the answer says and how close the runner-up came."""
    answer = answer if isinstance(answer, dict) else {}
    t, ranked = answer.get("type"), top_two(answer)
    if t == "noul":
        return f"says {ranked[0][0]} · p(yes) {answer['noul']:.2f}" if ranked else ""
    if t == "score":
        score, legend = answer.get("score"), answer.get("legend")
        if not isinstance(score, (int, float)) or isinstance(score, bool): return ""
        try:  # an integer past float range cannot be printed as one
            level = legend.get(str(round(score))) if isinstance(legend, dict) else None
            return f"about level {round(score)}: {level}" if isinstance(level, str) and level else f"score {score:g}"
        except (OverflowError, ValueError):
            return ""
    chosen = answer.get("choice") if isinstance(answer.get("choice"), str) else ranked[0][0] if ranked else None
    probs = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else {}
    others = sorted(((100 * v, str(k)) for k, v in probs.items() if _prob(v) and str(k) != chosen), reverse=True)
    if chosen is None: return ""
    if not others or not _prob(probs.get(chosen)): return f"picked {chosen}"  # Jev's pick, not always the likeliest
    (b, second), a = others[0], 100 * probs[chosen]
    if abs(a - b) <= 5: return f"picked {chosen} · near tie with {second}"
    return f"picked {chosen} · {second} is likelier ({b:.0f}%)" if b > a else f"picked {chosen} · next best {second} {b:.0f}%"


def _prob(x): return isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1


class DecisionModel:
    def __init__(self, clock=time.monotonic, bands=None):
        self.clock = clock
        self.bands = bands if bands is not None else collections.defaultdict(lambda: DEFAULT_BANDS)
        self.series = {}                                   # question -> Series
        self.latest = {}                                   # question -> (seq, answer as sent)
        self.question_source = {}                          # question -> source label of its latest record
        self.hist = {}                                     # question -> decisions per per-mille confidence, 0..1000
        self.seen = collections.OrderedDict()              # question names, least recently seen first
        self.dropped_below = 0                             # below-escalate decisions of dropped names, at the bands they had then
        self.graded = {}                                   # (seq, question) -> per-mille confidence, for the review queue
        self.recent = collections.deque(maxlen=CHIP_WINDOW)
        self.calls = self.decisions = self.rate_limited = self.cost_micro = 0
        self.latency = collections.deque(maxlen=200)
        self.arrivals, self.minute_cost = collections.deque(), 0  # (arrival time, cost_micro), last 60 s, and their sum
        self.source_seen, self.source_recent = {}, {}      # label -> arrival times, last 60 s and last 10 s
        self.last_seq, self.last_arrival, self.last_status = -1, None, None
        self.missed = 0                                    # records stored but never seen here (the window fell behind the ring)

    def add(self, records):
        """Take stored records (each with its seq), oldest first. Returns the question names that got points."""
        now, touched = self.clock(), set()
        for rec in records:
            try:
                call, answers, _ = summarize(rec)
            except Exception:
                continue
            if self.last_seq >= 0 and rec["seq"] > self.last_seq + 1: self.missed += rec["seq"] - self.last_seq - 1  # seqs have no gaps
            self.last_seq = max(self.last_seq, rec["seq"]); self.last_arrival = now; self.calls += 1
            self.last_status = call["status"]
            label = source_label(rec)
            self.source_seen.setdefault(label, collections.deque()).append(now)
            self.source_recent.setdefault(label, collections.deque()).append(now)
            if call["status"] == 429: self.rate_limited += 1
            if call["latency_ms"] is not None: self.latency.append(call["latency_ms"])
            cost = call["cost_est_micro"] or 0
            self.cost_micro += cost; self.minute_cost += cost; self.arrivals.append((now, cost))
            resp = rec.get("response")
            wire = resp.get("answers") if isinstance(resp, dict) else None
            wire = {_text(k): v for k, v in wire.items()} if isinstance(wire, dict) else {}  # the index's spelling of each name
            ts = call["ts_ms"] / 1000 if call["ts_ms"] is not None else math.nan
            for seq, q, qtype, conf, margin, top, p_yes, _chosen in answers:
                if conf is None or not 0 <= conf <= 1000: continue  # outside 0..100 %: not a probability, not charted
                second = p_yes if qtype == "noul" else top
                if q not in self.series:
                    self.series[q], self.hist[q] = Series(), np.zeros(1001, dtype=np.int64)
                    if len(self.series) > MAX_QUESTIONS: self._drop(next(iter(self.seen)))
                self.seen[q] = None; self.seen.move_to_end(q)
                self.series[q].add(seq, ts, conf / 10, (second or 0) / 10, (margin or 0) / 10)
                self.hist[q][conf] += 1
                self.latest[q] = (seq, wire.get(q, {})); self.question_source[q] = label
                self.recent.append(q); self.decisions += 1; touched.add(q)
        self._forget(now)
        return touched

    def _drop(self, q):
        """Stop tracking a question name (a sender whose names change on every call must not grow memory without end).
        Its below-escalate count is kept, at the bands it has now."""
        self.dropped_below += int(self.hist[q][:self.bands[q][0] * 10].sum())
        for d in (self.series, self.hist, self.seen, self.latest, self.question_source): d.pop(q, None)

    def _forget(self, now):
        while self.arrivals and now - self.arrivals[0][0] > 60: self.minute_cost -= self.arrivals.popleft()[1]
        for window, times in ((60, self.source_seen), (10, self.source_recent)):
            for seen in times.values():
                while seen and now - seen[0] > window: seen.popleft()

    # ---- what the widgets read
    def questions(self):
        """Question names for the chips: grouped by source (busiest first), then by how often they came up lately."""
        freq = collections.Counter(self.recent)
        busy = {label: len(seen) for label, seen in self.source_seen.items()}
        return sorted(self.series, key=lambda q: (-busy.get(self.question_source.get(q), 0), self.question_source.get(q, ""), -freq[q], q))

    def default_question(self, current=None):
        """The most frequent question lately, first in chip order on a tie. The current choice stays until another
        question is clearly more frequent (SWITCH_MARGIN), so near-ties do not make the chart flip back and forth."""
        freq = collections.Counter(self.recent)
        if not freq: return None
        best = max(freq.values())
        leader = next(q for q in self.questions() if freq[q] == best)
        if current in freq and freq[leader] < SWITCH_MARGIN * freq[current]: return current
        return leader

    def streak(self, q):
        """How many of this question's latest decisions in a row are below escalate, counted over the newest
        CHART_POINTS (so it never jumps down when old points are trimmed)."""
        s = self.series.get(q)
        if s is None: return 0
        conf = s.last()[2]
        at_or_above = np.flatnonzero(conf >= self.bands[q][0])
        return len(conf) - 1 - int(at_or_above[-1]) if len(at_or_above) else len(conf)

    def below(self):
        """Decisions this session below their own question's current escalate, and how many of those are not graded."""
        total = self.dropped_below + sum(int(h[:self.bands[q][0] * 10].sum()) for q, h in self.hist.items())
        graded = sum(1 for (_, q), c in self.graded.items() if c < self.bands[q][0] * 10)
        return total, total - graded

    def requests_per_min(self):
        self._forget(self.clock()); return len(self.arrivals)

    def latency_stats(self):
        """(mean, p95) in ms over the last 200 calls, or None. p95 is the nearest rank."""
        if not self.latency: return None
        xs = sorted(self.latency)
        return sum(xs) / len(xs), xs[math.ceil(0.95 * len(xs)) - 1]

    def cost(self):
        """(dollars this session, dollars per hour at the last minute's rate), both estimates."""
        self._forget(self.clock())
        return self.cost_micro / 1e6, self.minute_cost * 60 / 1e6

    def sources(self):
        """(label, heard from in the last 5 s, calls per second over the last 10 s) for each sender."""
        now = self.clock(); self._forget(now)
        return [(label, bool(seen) and now - seen[-1] < 5, len(self.source_recent[label]) / 10)
                for label, seen in sorted(self.source_seen.items())]
