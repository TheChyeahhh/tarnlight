import collections, math
import pytest
from tarnlight import model
from tarnlight.demo import priced
from tarnlight.model import DecisionModel, Series, plain_words, top_two, source_label
from tarnlight.storage import summarize


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def numbered(recs, start=0): return [{**r, "seq": start + i} for i, r in enumerate(recs)]


def only(rec, answers):
    rec["response"]["answers"] = answers; return rec


def noul(p): return {"type": "noul", "noul": p}


def test_one_series_per_question_with_the_index_values(sample_records):
    recs = numbered(sample_records); m = DecisionModel(Clock()); m.add(recs)
    expected = collections.defaultdict(list)
    for r in recs:
        for seq, q, qtype, conf, margin, top, p_yes, _c in summarize(r)[1]:
            second = p_yes if qtype == "noul" else top  # noul charts p(yes) itself
            expected[q].append((seq, r["ts"], conf / 10, second / 10, margin / 10))
    assert set(m.series) == set(expected)
    for q, points in expected.items():
        seq, ts, conf, second, margin = m.series[q].last()
        assert list(zip(seq, ts, conf, second, margin)) == [pytest.approx(p) for p in points]  # never mixed across questions
    assert m.decisions == sum(len(p) for p in expected.values()) and m.calls == len(recs)


def test_counters_follow_the_records(sample_records):
    recs = numbered(list(map(priced, sample_records))); m = DecisionModel(Clock()); m.add(recs)  # with the drop box's cost estimates
    lat = [r["latency_ms"] for r in recs[-200:]]
    mean, p95 = m.latency_stats()
    nearest_rank = min(v for v in lat if sum(x <= v for x in lat) >= 0.95 * len(lat))  # the textbook definition
    assert mean == pytest.approx(sum(lat) / 200) and p95 == nearest_rank
    spent, per_hour = m.cost()
    assert spent > 0 and spent == pytest.approx(sum(r.get("cost_est_micro", 0) for r in recs) / 1e6)  # a failed call has no tokens to price
    assert per_hour == pytest.approx(spent * 60)  # everything arrived within the last minute
    confs = [c for s in m.series.values() for c in s.conf]
    assert m.below() == (sum(c < 40 for c in confs),) * 2
    assert m.requests_per_min() == len(recs)
    assert m.rate_limited == sum(r["status"] == 429 for r in recs)


def test_p95_on_small_samples():
    m = DecisionModel(Clock())
    for xs, p95 in (([7], 7), ([1, 2], 2), (list(range(1, 21)), 19), (list(range(1, 101)), 95)):
        m.latency.clear(); m.latency.extend(reversed(xs))
        assert m.latency_stats()[1] == p95


def test_below_uses_each_questions_own_escalate_and_leaves_out_graded(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([only(make_record(i), {"a": noul(p), "b": noul(p)}) for i, p in enumerate((0.5, 0.45, 0.2))]))  # 50, 55, 80
    assert m.below() == (0, 0)
    m.bands["a"] = (56, 70)
    assert m.below() == (2, 2)
    m.graded[(0, "a")] = 500; m.graded[(2, "a")] = 800  # the second is not below, so it does not count
    assert m.below() == (2, 1)


def test_the_default_question_is_the_most_frequent_lately(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([only(make_record(0), {"rare": noul(0.5)})] + [make_record(i) for i in range(1, 5)]))
    assert m.default_question() in {"go", "verdict", "size"}
    assert m.questions()[-1] == "rare"


def test_the_default_question_does_not_flip_on_a_near_tie(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([only(make_record(i), {"a": noul(0.5)}) for i in range(10)]))
    m.add(numbered([only(make_record(i), {"b": noul(0.5)}) for i in range(12)], start=10))
    assert m.default_question() == m.questions()[0] == "b"  # no current choice: the leader
    assert m.default_question("a") == "a"                   # 12 is not clearly more than 10
    m.add(numbered([only(make_record(0), {"b": noul(0.5)})], start=22))
    assert m.default_question("a") == "b"                   # 13 is (1.25 x 10 = 12.5)
    assert m.default_question("gone") == "b"


def test_a_tie_goes_to_the_first_chip(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([only(make_record(i), {"zeta": noul(0.5), "alpha": noul(0.5)}) for i in range(3)]))
    assert m.default_question() == m.questions()[0] == "alpha"


def test_values_outside_0_to_100_are_not_charted(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([only(make_record(0), {"a": noul(1.5), "b": noul(-0.2), "c": noul(0.3)})]))
    assert set(m.series) == {"c"} and m.decisions == 1


def test_streak_counts_the_latest_decisions_below_escalate(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([only(make_record(i), {"go": noul(p)}) for i, p in enumerate([0.9, 0.2, 0.9, 0.3, 0.35, 0.39])]))  # 90, 80, 90, 70, 65, 61
    assert m.streak("go") == 0
    m.bands["go"] = (75, 90)
    assert m.streak("go") == 3 and m.streak("nothing") == 0


def test_rates_only_count_the_last_minute(make_record):
    clock = Clock(); m = DecisionModel(clock)
    m.add(numbered([make_record(i) for i in range(10)]))
    clock.t += 30; m.add(numbered([make_record(i) for i in range(5)], start=10))
    clock.t += 35
    assert m.requests_per_min() == 5
    ((label, live, per_s),) = m.sources()
    assert label == "proxy · test" and not live and per_s == 0
    assert m.cost()[1] == pytest.approx(5 * 15 * 60 / 1e6)


def test_a_series_keeps_the_newest_points(monkeypatch):
    monkeypatch.setattr(model, "CHART_POINTS", 100)
    s = Series()
    for i in range(1000): s.add(i, i / 10, i % 100, 0, 0)
    seq, ts, *_ = s.last()
    assert list(seq) == list(range(900, 1000)) and len(s.seq) <= 200 and ts[0] == pytest.approx(90)
    assert list(s.last(10)[0]) == list(range(990, 1000))
    assert list(s.last(3, upto=950)[0]) == [948, 949, 950] and list(s.last(3, upto=950.5)[0]) == [948, 949, 950]
    assert len(s.last(5, upto=10)[0]) == 0  # older than anything kept


def test_top_two_reads_each_answer_type():
    assert top_two(noul(0.8)) == [("yes", pytest.approx(80)), ("no", pytest.approx(20))]
    assert top_two({"type": "choice", "probabilities": {"a": 0.1, "b": 0.7, "c": 0.2}}) == [("b", 70), ("c", 20)]
    assert top_two({"type": "score", "legend": {"0": "low", "1": "high"}, "probabilities": {"0": 0.3, "1": 0.7}}) == [("high", 70), ("low", 30)]


def test_top_two_survives_odd_shapes():
    for odd in (None, "x", {}, noul("0.5"), noul(True), noul(2), {"type": "choice", "probabilities": ["a"]},
                {"type": "score", "legend": ["x"], "probabilities": {"0": 0.5}}, {"type": "choice", "probabilities": {"a": math.nan}}):
        assert isinstance(top_two(odd), list)
    assert top_two({"type": "score", "legend": ["x"], "probabilities": {"0": 0.5, "1": "no"}}) == [("0", 50)]


def test_odd_records_are_skipped_not_fatal(make_record):
    m = DecisionModel(Clock())
    odd = make_record(1); odd["response"] = "<html>502</html>"
    m.add(numbered([make_record(0), odd, make_record(2)]))
    assert m.calls == 3 and len(m.series["go"].seq) == 2 and m.last_status == 200


def test_add_says_which_questions_got_points(make_record):
    m = DecisionModel(Clock())
    assert m.add(numbered([make_record(0)])) == {"go", "verdict", "size"}
    assert m.add(numbered([only(make_record(1), {"go": noul(0.4)})], start=1)) == {"go"}


def test_source_labels():
    assert source_label({"source": "proxy", "project": "ticket-triage"}) == "proxy · ticket-triage"
    assert source_label({"source": "my-app"}) == "my-app" and source_label({}) == "unknown"


def test_a_long_streak_never_jumps_down_on_a_trim(make_record, monkeypatch):
    monkeypatch.setattr(model, "CHART_POINTS", 50)
    m = DecisionModel(Clock()); m.bands["go"] = (60, 80); seen = []  # a noul is never below 50 %
    for i in range(300):
        m.add(numbered([only(make_record(i), {"go": noul(0.5)})], start=i)); seen.append(m.streak("go"))
    assert seen == [min(i + 1, 50) for i in range(300)]


def test_records_the_window_never_saw_are_counted(make_record):
    m = DecisionModel(Clock())
    m.add(numbered([make_record(i) for i in range(3)], start=5))  # the first batch may start anywhere
    m.add(numbered([make_record(i) for i in range(2)], start=8))
    assert m.missed == 0
    m.add(numbered([make_record(0)], start=20))
    assert m.missed == 10 and m.last_seq == 20


def test_a_series_never_holds_more_than_twice_its_points(monkeypatch):
    monkeypatch.setattr(model, "CHART_POINTS", 300)  # starts at 256, so it has to grow
    s = Series(); sizes = set()
    for i in range(2000): s.add(i, i, 50, 50, 0); sizes.add(s._rows.shape[1])
    assert max(sizes) == 600 and list(s.last()[0]) == list(range(1700, 2000))


def test_names_that_keep_changing_stay_bounded(make_record, monkeypatch):
    monkeypatch.setattr(model, "MAX_QUESTIONS", 5); monkeypatch.setattr(model, "CHIP_WINDOW", 5)
    m = DecisionModel(Clock()); m.bands = collections.defaultdict(lambda: (60, 80))  # every noul(0.55) is below escalate
    m.add(numbered([only(make_record(i), {f"rel_{i}": noul(0.55)}) for i in range(20)]))
    assert list(m.series) == [f"rel_{i}" for i in range(15, 20)] and set(m.hist) == set(m.latest) == set(m.series)
    assert m.below() == (20, 20) and m.decisions == 20  # the dropped names still count
    m.add(numbered([only(make_record(20), {"rel_15": noul(0.55)})], start=20))  # seen again: now the most recent
    m.add(numbered([only(make_record(21), {"new": noul(0.55)})], start=21))
    assert "rel_15" in m.series and "rel_16" not in m.series and m.default_question() in m.series


def test_rates_use_running_sums(make_record):
    clock = Clock(); m = DecisionModel(clock)
    m.add(numbered([make_record(i) for i in range(10)]))
    clock.t += 8; m.add(numbered([make_record(i) for i in range(4)], start=10))
    assert m.sources()[0][2] == pytest.approx(1.4)  # 14 in the last 10 s
    clock.t += 5
    assert m.sources()[0][2] == pytest.approx(0.4) and m.cost()[1] == pytest.approx(14 * 15 * 60 / 1e6)
    clock.t += 50
    assert m.cost()[1] == pytest.approx(4 * 15 * 60 / 1e6) and m.minute_cost == 4 * 15


def test_the_gauge_line_says_the_answer_in_plain_words():
    choice = lambda probs: {"type": "choice", "probabilities": probs}
    assert plain_words(noul(0.17)) == "says no · p(yes) 0.17" and plain_words(noul(0.8)) == "says yes · p(yes) 0.80"
    assert plain_words(choice({"approve": 0.38, "reject": 0.37, "defer": 0.25})) == "picked approve · near tie with reject"
    assert plain_words(choice({"approve": 0.62, "reject": 0.2, "defer": 0.18})) == "picked approve · next best reject 20%"
    assert plain_words(choice({"approve": 1.0})) == "picked approve"
    legend = {"0": "small", "1": "medium", "2": "large"}
    assert plain_words({"type": "score", "score": 1.7, "legend": legend}) == "about level 2: large"
    assert plain_words({"type": "score", "score": 1.2}) == "score 1.2"
    for odd in (None, {}, {"type": "noul", "noul": 2}, {"type": "score", "score": "x"}, {"type": "score", "score": True}, choice({})):
        assert plain_words(odd) == ""


def test_the_gauge_names_jevs_own_pick_and_survives_huge_numbers():
    pick = lambda chosen, probs: plain_words({"type": "choice", "choice": chosen, "probabilities": probs})
    assert pick("reject", {"approve": 0.41, "reject": 0.33, "defer": 0.26}) == "picked reject · approve is likelier (41%)"
    assert pick("reject", {"approve": 0.36, "reject": 0.33, "defer": 0.31}) == "picked reject · near tie with approve"
    assert pick("approve", {"approve": 0.6, "reject": 0.3, "defer": 0.1}) == "picked approve · next best reject 30%"
    assert pick("approve", {"reject": 0.5}) == "picked approve"  # no share for the pick: nothing to compare
    assert plain_words({"type": "score", "score": 10 ** 400, "legend": {"0": "x"}}) == ""


def test_failure_tags():
    from tarnlight.model import failure
    ok = {"status": 200, "error": None, "response": {"answers": {"q": {}}}}
    assert failure(ok) is None and failure({**ok, "error": {"proxy": "the caller hung up"}}) is None  # answered, with a note
    assert failure({**ok, "status": None}) is None  # a tap that sends no status
    assert failure({**ok, "response": None}) == "no answer" and failure({"status": 503, "error": "x"}) == "503"
    assert failure({"failed": "timeout"}) == "timeout" and len(failure({"error": {"jev": "x" * 500}})) == 60
