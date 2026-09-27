"""
Made-up Jev traffic for `tarnlight demo` and the tests. Nothing here is real: three invented senders (a support-ticket
triage bot, a code-review bot and a forum's moderation queue) in the exact wire shape Jev returns (DESIGN.md, "The
canonical record" and "Wire format facts"). The same seed always gives the same records.

  * about MEAN_GAP_S between calls, so 600 calls span about 13 minutes; the demo plays them SPEED times faster
  * the moderation queue sends over half the calls and asks "policy" on every one: the clearly busiest question
  * confidences run from sure to unsure, some below the default escalate of 40. About 1 in 200 choice answers is a near
    tie where Jev's pick is not the likeliest option, as on the real API
  * about 3% of calls fail: a 429 with its error body, an SDK timeout, and a call the proxy could not deliver
  * cost_est_micro is left out, as a drop box writer may leave it; priced() fills it the way the drop box does
  * every record is labelled "demo", so the feed, an export and a replay of one show that it is made up
"""
import itertools, json, math, random, socket, threading, time
from .ingest import send
from .replay import PRICE_PER_MTOK

T0 = 1790409600.0       # when the made-up traffic starts
MEAN_GAP_S = 1.25       # between two calls, all senders together
SPEED = 5               # the demo's default: a pass of 600 calls takes about 2.5 minutes, at about 4 calls a second
NEAR_TIE = 0.005        # share of choice answers where Jev picks the runner-up of a near tie
FAIL_SHARE = 0.03
FAILURES = ("429", "timeout", "429", "unreachable", "timeout", "429", "timeout", "429", "unreachable")  # in this rotation
RATE_LIMITED = {"detail": "Rate limit exceeded: 1200 requests per minute. Try again shortly."}
UNREACHABLE = {"proxy": "tarnlight proxy: TypeSafe could not be reached (ClientConnectorError)"}

POLICY = {"type": "choice", "instructions": "Which board rule does this post break, if any?",
          "criteria": {"fine": "Breaks no rule", "spam": "Advertising or repeated links", "rude": "Insults or attacks another member",
                       "off_topic": "Belongs on another board"}}
HIDE_NOW = {"type": "noul", "instructions": "Should this post be hidden before a moderator sees it?",
            "criteria": {"true": "It could hurt someone or is plainly spam", "false": "It can wait for a moderator"}}
ROUTE = {"type": "choice", "instructions": "Which team should handle this ticket?",
         "criteria": {"account": "Sign-in, profile and settings", "bug": "Something is broken", "how_to": "Asks how to do something",
                      "feature_request": "Asks for something new"}}
URGENCY = {"type": "score", "instructions": "How soon does this ticket need an answer?", "criteria": ["can wait", "this week", "today", "right now"]}
NEEDS_HUMAN = {"type": "noul", "instructions": "Does this ticket need a person rather than a help article?"}
RECOMMENDATION = {"type": "choice", "instructions": "What should the reviewer do with this change?",
                  "criteria": {"approve": None, "request_changes": None, "comment_only": None}}
RISK = {"type": "score", "instructions": "How risky is this change to merge?", "criteria": ["trivial", "low", "moderate", "high"]}
TESTS_COVER = {"type": "noul", "instructions": "Do the tests in this change cover what it changes?"}

# (board, text, the rule it really breaks, how clear that is from 0 to 1)
POSTS = (("gardening", "Has anyone kept basil alive indoors over the winter?", "fine", 0.9),
         ("gardening", "Thanks, watering in the morning worked for me too.", "fine", 0.9),
         ("photography", "Which settings would you use for stars over a lake?", "fine", 0.85),
         ("cooking", "I made this with less sugar and it was still great.", "fine", 0.9),
         ("cycling", "I disagree, but I see where you are coming from.", "fine", 0.6),
         ("cooking", "Honestly this recipe is bad and so is the advice in it.", "rude", 0.25),
         ("photography", "Nobody asked for your opinion, go away.", "rude", 0.8),
         ("cycling", "This is the dumbest question on the whole board.", "rude", 0.75),
         ("gardening", "Cheap garden tools, huge sale, link in my profile!!!", "spam", 0.9),
         ("cooking", "Free followers fast, message me now, limited offer.", "spam", 0.85),
         ("photography", "I wrote about night shots on my blog, it covers this.", "spam", 0.2),
         ("cooking", "Does anyone know a good film for tonight?", "off_topic", 0.7),
         ("gardening", "Looking for people to join a Sunday running club.", "off_topic", 0.45))
# (subject, body, team, urgency level, needs a person, how clear)
TICKETS = (("Cannot sign in after changing my email", "It says the account does not exist.", "account", 2, True, 0.8),
           ("Two-step code never arrives", "Waited ten minutes and tried twice.", "account", 3, True, 0.7),
           ("How do I change my display name?", "I could not find it in settings.", "how_to", 0, False, 0.5),
           ("Export button does nothing", "Clicking Export on the reports page gives no file.", "bug", 1, True, 0.85),
           ("App closes when I attach a photo", "It happens every time on my phone.", "bug", 2, True, 0.8),
           ("Search shows items from another project", "I only have access to one project.", "bug", 3, True, 0.6),
           ("How do I invite a teammate?", "", "how_to", 0, False, 0.9),
           ("Where is the dark mode setting?", "", "how_to", 0, False, 0.85),
           ("Can I undo a deleted comment?", "I removed it by mistake a minute ago.", "how_to", 2, True, 0.4),
           ("Please add a calendar view", "It would help us plan the week.", "feature_request", 0, False, 0.9),
           ("Keyboard shortcuts for the board?", "Is this planned?", "feature_request", 0, False, 0.55))
# (title, lines added, lines removed, areas, what a reviewer should do, risk level, tests cover it, how clear)
CHANGES = (("Fix a typo on the welcome screen", 1, 1, ["copy"], "approve", 0, True, 0.95),
           ("Rename an internal helper", 40, 40, ["utils"], "approve", 0, True, 0.8),
           ("Remove an unused settings flag", 12, 58, ["settings"], "approve", 1, True, 0.7),
           ("Retry failed uploads up to three times", 85, 10, ["uploads"], "approve", 2, True, 0.5),
           ("Cache search results for five minutes", 120, 15, ["search", "cache"], "request_changes", 2, False, 0.45),
           ("Rewrite session handling", 640, 410, ["sign-in", "sessions"], "request_changes", 3, False, 0.6),
           ("Change the password rules on sign-up", 60, 20, ["sign-in"], "comment_only", 2, True, 0.3),
           ("Raise the test timeout to 60 s", 1, 1, ["tests"], "comment_only", 1, False, 0.35))


def records(n=600, seed=7):
    """n made-up canonical records, oldest first, the same for the same seed."""
    rng = random.Random(seed)
    failing = dict(zip(sorted(rng.sample(range(n), round(n * FAIL_SHARE))), itertools.cycle(FAILURES)))
    out, t = [], T0
    for i in range(n):
        t += rng.expovariate(1 / MEAN_GAP_S)
        make = rng.choices((_moderation, _triage, _review), weights=(55, 27, 18))[0]
        (source, project, sdk), state, questions, answers = make(rng, i)
        request = {"state": state, "model": "jev-latest", "questions": questions}
        tokens = 120 + len(json.dumps(request)) // 3 + rng.randint(-8, 8)  # the questions and the state, plus Jev's own prompt
        latency = 90 + 0.4 * tokens + rng.lognormvariate(4.0, 0.5) + (rng.uniform(400, 1500) if rng.random() < 0.03 else 0)
        rec = {"v": 1, "ts": round(t, 3), "seq": None, "source": source, "session_id": None, "tool_use_id": None, "project": project,
               "label": "demo", "sdk": sdk, "request_id": f"req_{rng.getrandbits(96):024x}", "latency_ms": round(latency), "status": 200,
               "retry_count": 0, "request": request,
               "response": {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 0}}, "error": None}
        how = failing.get(i)
        if how == "429":  # TypeSafe answered, with an error body and its request id
            rec.update(latency_ms=rng.randint(25, 60), status=429, response=None, error=RATE_LIMITED)
        elif how == "timeout":  # the SDK gave up after its 10 s: no answer, so no request id
            rec.update(latency_ms=10000 + rng.randint(0, 30), status=None, request_id=None, response=None, error={"jev": "timeout"})
        elif how == "unreachable":  # as the proxy records a call it could not deliver
            rec.update(latency_ms=rng.randint(15, 80), status=None, request_id=None, response=None, error=UNREACHABLE)
        out.append(rec)
    return out


def priced(rec):
    """rec with the cost estimate the drop box fills in from the input tokens (the demo sends over UDP, which keeps a
    record as sent)."""
    usage = rec["response"].get("usage") if isinstance(rec.get("response"), dict) else None
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    return {**rec, "cost_est_micro": round(tokens * PRICE_PER_MTOK)} if isinstance(tokens, int) else rec


def play(addr, speed=SPEED, stop=None, seed=7):
    """Send made-up records to a console at addr as if they were happening now: their spacing divided by speed, their
    times moved to the moment they are sent. Each pass uses the next seed, so the data keeps changing. Runs until stop is
    set; returns how many were sent."""
    if not speed > 0: raise ValueError("speed must be above 0")
    stop = stop or threading.Event()
    sock, sent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), 0
    try:
        for n in itertools.count():
            recs = records(seed=seed + n)
            start, wall = time.perf_counter(), time.time()
            for rec in recs:
                at = (rec["ts"] - recs[0]["ts"]) / speed
                if stop.wait(max(0.0, start + at - time.perf_counter())): return sent
                send(priced({**rec, "ts": round(wall + at, 3)}), sock, addr); sent += 1
            if stop.wait(MEAN_GAP_S / speed): return sent
    finally:
        sock.close()


# ---- the three senders: each returns (source, project, sdk), state, questions, answers

def _moderation(rng, i):
    board, text, rule, clear = rng.choice(POSTS); sure = _jitter(rng, clear)
    reports = rng.choices((0, 1, 2, 3, 5), weights=(45, 25, 15, 10, 5))[0]
    state = {"post": {"id": f"p{48000 + i}", "board": board, "text": text, "reports": reports, "author_days": rng.randint(0, 900)}}
    questions, answers = {"policy": POLICY}, {"policy": _choice(rng, POLICY, rule, sure)}
    if reports:  # a reported post: may it wait for a person?
        questions["hide_now"] = HIDE_NOW; answers["hide_now"] = _noul(rng, rule in ("spam", "rude"), sure)
    return ("mod-queue", "forum", "python/0.7.1"), state, questions, answers


def _triage(rng, i):
    subject, body, team, level, human, clear = rng.choice(TICKETS); sure = _jitter(rng, clear)
    state = {"ticket": {"id": f"T-{20000 + i}", "subject": subject, "body": body, "channel": rng.choice(("email", "chat", "form"))}}
    questions = {"route": ROUTE, "urgency": URGENCY}
    answers = {"route": _choice(rng, ROUTE, team, sure), "urgency": _score(rng, URGENCY, level, sure)}
    if rng.random() < 0.5:
        questions["needs_human"] = NEEDS_HUMAN; answers["needs_human"] = _noul(rng, human, sure)
    return ("ticket-triage", "helpdesk", "python/0.7.1"), state, questions, answers


def _review(rng, i):
    title, added, removed, areas, verdict, level, covered, clear = rng.choice(CHANGES); sure = _jitter(rng, clear)
    state = {"change": {"number": 300 + i, "title": title, "lines_added": added, "lines_removed": removed, "areas": areas}}
    questions = {"recommendation": RECOMMENDATION, "risk": RISK}
    answers = {"recommendation": _choice(rng, RECOMMENDATION, verdict, sure), "risk": _score(rng, RISK, level, sure)}
    if rng.random() < 0.6:
        questions["tests_cover_change"] = TESTS_COVER; answers["tests_cover_change"] = _noul(rng, covered, sure)
    return ("review-bot", "web-app", "node/0.6.0"), state, questions, answers


# ---- answers in the wire shape, numbers as 2-decimal floats

def _jitter(rng, clear): return min(1.0, max(0.0, clear + rng.gauss(0, 0.1)))


def _confidence(rng, shares):
    """Made up, since the API's formula is not published: high when the top share is high and well ahead."""
    top, second = shares[0], shares[1] if len(shares) > 1 else 0
    return round(min(0.99, max(0.05, top ** 1.3 * (0.85 + 0.3 * (top - second)) + rng.gauss(0, 0.04))), 2)


def _choice(rng, question, truth, sure):
    options = list(question["criteria"])
    weights = {o: rng.gammavariate(1 + 14 * sure if o == truth else 1.2, 1) for o in options}
    total = sum(weights.values())
    cents = {o: round(100 * w / total) for o, w in weights.items()}
    ranked = sorted(options, key=lambda o: -cents[o])
    choice = ranked[0]
    if rng.random() < NEAR_TIE:  # Jev's pick is not always the likeliest option: a near tie, answered the other way
        a, b = ranked[:2]; pair = cents[a] + cents[b]
        cents[a] = pair // 2 + 1; cents[b] = pair - cents[a]; choice = b
    keys = options[:]; rng.shuffle(keys)  # probability keys come in no fixed order
    probs = {o: cents[o] / 100 for o in keys}
    return {"type": "choice", "choice": choice, "probabilities": probs,
            "confidence": _confidence(rng, sorted(probs.values(), reverse=True))}


def _score(rng, question, truth, sure):
    """The expected level over probabilities around truth, which spread wider when Jev is unsure."""
    levels, width = question["criteria"], 0.35 + 1.4 * (1 - sure)
    centre = truth + rng.gauss(0, 0.6 * (1 - sure))
    weights = [math.exp(-((i - centre) / width) ** 2 / 2) for i in range(len(levels))]
    cents = [round(100 * w / sum(weights)) for w in weights]
    return {"type": "score", "score": round(sum(i * c for i, c in enumerate(cents)) / 100, 2),
            "legend": {str(i): level for i, level in enumerate(levels)}, "probabilities": {str(i): c / 100 for i, c in enumerate(cents)},
            "confidence": _confidence(rng, sorted((c / 100 for c in cents), reverse=True))}


def _noul(rng, truth, sure):
    p = 0.5 + (0.12 + 0.36 * sure) * (1 if truth else -1) + rng.gauss(0, 0.07)
    return {"type": "noul", "noul": round(min(0.99, max(0.01, p)), 2)}
