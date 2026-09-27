"""The taps: an app's own Jev calls, through the real SDKs, leave records in the drop box that the console imports.

Every call goes to a local stand-in for TypeSafe with a made-up key; nothing reaches the network. One record per
attempt, as the proxy does: a call the SDK retries leaves one record per try, each with its retry_count."""
import asyncio, gzip, importlib.util, json, os, shutil, socket, statistics, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
import pytest
from tarnlight.inbox import Inbox
from tarnlight.ingest import Ingest
from tarnlight.model import failure
from tarnlight.storage import JevLog

httpx2 = pytest.importorskip("httpx2")
sdk = pytest.importorskip("typesafe_sdk")
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy, TypeSafeClient

TAPS = Path(__file__).resolve().parents[1] / "taps"
KEY = "tsk_made_up_for_tests_4f2a9c"   # not a key: the stand-in never checks it
QUESTIONS = {"urgent": Noul(instructions="Does the customer need an answer today?"),
             "queue": Choice(instructions="Which queue?", criteria={"billing": None, "shipping": None})}
ANSWER = {"model": "jev-1.13.0", "answers": {"urgent": {"type": "noul", "noul": 0.81},
          "queue": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.7, "shipping": 0.3}, "confidence": 0.4}},
          "usage": {"input_tokens": 420, "output_tokens": 0}}
FAST = RetryPolicy(max_retries=2, backoff_initial=0.001, backoff_max=0.002)
BASE = "https://api.typesafe.ai"   # with a mock transport: nothing is sent anywhere
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("TYPESAFE_")}  # child processes get no TypeSafe settings


class FakeTypeSafe:
    """A local stand-in for api.typesafe.ai. Each POST gets the next step of script ("ok" when it is empty): "ok" (a gzip
    answer, as the real one sends), an HTTP status, "cut" (headers promise 400 bytes, 30 come, the connection closes),
    "stall" (30 bytes, then a second of silence, longer than the client waits) or "hang" (the same for five seconds)."""
    def __init__(self):
        self.script, self.seen, self.n = [], [], 0
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.srv.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try: conn, _ = self.srv.accept()
            except OSError: return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                if not (chunk := conn.recv(65536)): return
                buf += chunk
            head, body = buf.split(b"\r\n\r\n", 1)
            headers = dict((k.strip().lower(), v.strip()) for k, _, v in (l.decode().partition(":") for l in head.split(b"\r\n")[1:]))
            while len(body) < int(headers.get("content-length", 0)): body += conn.recv(65536)
            self.seen.append(headers); self.n += 1
            step = self.script.pop(0) if self.script else "ok"
            first = f"x-typesafe-request-id: req_fake_{self.n}\r\nContent-Type: application/json\r\nConnection: close\r\n"
            if isinstance(step, int):
                data = json.dumps({"detail": {"error_type": "rate_limit", "message": "slow down"} if step == 429 else "not allowed"}).encode()
                conn.sendall(f"HTTP/1.1 {step} Nope\r\n{first}retry-after-ms: 1\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
                return
            data = gzip.compress(json.dumps(ANSWER).encode())
            if step == "ok":
                conn.sendall(f"HTTP/1.1 200 OK\r\n{first}Content-Encoding: gzip\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
                return
            conn.sendall(f"HTTP/1.1 200 OK\r\n{first}Content-Encoding: gzip\r\nContent-Length: 400\r\n\r\n".encode() + data[:30])
            time.sleep({"stall": 1.0, "hang": 5.0}.get(step, 0))

    def close(self): self.srv.close()


def load_tap():
    """The Python tap loaded from its file, as an app would copy it."""
    spec = importlib.util.spec_from_file_location("tarnlight_tap", TAPS / "python" / "tarnlight_tap.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tap(tmp_path, monkeypatch):
    """The tap, writing to a drop box under tmp_path."""
    mod = load_tap()
    folder = tmp_path / "home" / ".tarnlight" / "inbox"; folder.mkdir(parents=True)
    monkeypatch.setattr(mod, "FOLDER", folder)
    return mod


@pytest.fixture
def fake():
    f = FakeTypeSafe(); yield f; f.close()


def client(fake, tap, **kw):
    return tap.attach(TypeSafeClient(**{"api_key": KEY, "base_url": fake.url, "retry": FAST, "timeout": 5.0, **kw}),
                      source="ticket-triage", project="support")


def written(folder):
    """Every line the taps wrote, parsed."""
    return [json.loads(line) for f in sorted(Path(folder).glob("*.jsonl")) for line in f.read_text(encoding="utf-8").splitlines()]


def imported(folder, tmp_path):
    """The drop box as the console takes it in: the real Inbox, the ingest's checks, the log. (stored, rejected)"""
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0); ing.writer.start()
    try:
        n = Inbox(ing, folder).poll(datetime.now(timezone.utc))
        end = time.monotonic() + 5
        while ing.counts["stored"] < n and time.monotonic() < end: time.sleep(0.01)
        log._flush(); return [r for _, r in log.iter_records()], ing.counts["rejected"]
    finally:
        ing.stopping.set(); ing.writer.join(); log.close()


def test_each_call_becomes_one_record_the_console_imports(tap, fake, tmp_path):
    with client(fake, tap) as c:
        r = c.system_one({"ticket": "The parcel arrived damaged."}, QUESTIONS, extra_headers={"X-Tarnlight-Label": "first-look"})
        assert r.nouls["urgent"].noul == 0.81 and r.choices["queue"].choice == "billing"
        tap.attach(c)  # attaching twice records once
        c.system_one({"ticket": "Where is my refund?"}, QUESTIONS)
    recs = written(tap.FOLDER)
    assert [f.name[:len("ticket-triage-")] for f in tap.FOLDER.glob("*.jsonl")] == ["ticket-triage-"]
    assert len(recs) == 2 and [r["label"] for r in recs] == ["first-look", None]
    first = recs[0]
    assert first["source"] == "ticket-triage" and first["project"] == "support" and first["sdk"] == f"python/{sdk.__version__}"
    assert first["status"] == 200 and first["retry_count"] == 0 and first["request_id"] == "req_fake_1" and first["error"] is None
    assert first["request"]["state"] == {"ticket": "The parcel arrived damaged."} and set(first["request"]["questions"]) == {"urgent", "queue"}
    assert first["response"] == ANSWER  # decoded from gzip, key order kept
    assert isinstance(first["latency_ms"], int) and first["ts"] <= time.time()
    stored, rejected = imported(tap.FOLDER, tmp_path)
    assert rejected == 0 and [s["request_id"] for s in stored] == ["req_fake_1", "req_fake_2"]
    assert stored[0]["cost_est_micro"] == 18 and failure(stored[0]) is None  # 420 tokens at $0.042 per million, filled by the console


def test_the_async_client_is_tapped_too(tap, fake, tmp_path):
    async def run():
        async with tap.attach(AsyncTypeSafeClient(api_key=KEY, base_url=fake.url, retry=FAST), source="moderation") as c:
            return await c.system_one("Is this comment spam?", QUESTIONS)
    assert asyncio.run(run()).nouls["urgent"].noul == 0.81
    (rec,) = written(tap.FOLDER)
    assert rec["source"] == "moderation" and rec["response"] == ANSWER and rec["request"]["state"] == "Is this comment spam?"
    assert list(tap.FOLDER.glob("moderation-*.jsonl")) and imported(tap.FOLDER, tmp_path)[0][0]["request_id"] == "req_fake_1"


def test_a_cut_off_answer_is_retried_as_without_the_tap_and_each_attempt_is_one_record(tap, fake, tmp_path):
    fake.script[:] = ["cut", "ok", "stall", "ok", 429, "ok"]
    with client(fake, tap, timeout=0.3) as c:
        answers = [c.system_one({"ticket": f"t{i}"}, QUESTIONS).nouls["urgent"].noul for i in range(3)]
    assert answers == [0.81] * 3  # the app gets its answers as if no tap were there
    recs = written(tap.FOLDER)
    assert [(r["status"], r["retry_count"]) for r in recs] == [(200, 0), (200, 1), (200, 0), (200, 1), (429, 0), (200, 1)]
    cut, stall, busy = recs[0], recs[2], recs[4]
    assert cut["response"] is None and cut["error"]["jev"].startswith("the answer was cut off (")
    assert stall["response"] is None and stall["error"] == {"jev": "timeout (ReadTimeout)"}
    assert busy["error"] == {"detail": {"error_type": "rate_limit", "message": "slow down"}}  # the error body, verbatim
    assert [r["response"] == ANSWER for r in recs] == [False, True] * 3
    stored, rejected = imported(tap.FOLDER, tmp_path)
    assert rejected == 0 and [failure(s) for s in stored] == ["cut off", None, "timeout (ReadTimeout)", None, "429", None]
    fake.script[:] = ["cut", "ok"]  # the same fault without the tap: the same outcome, so the tap changed nothing
    with TypeSafeClient(api_key=KEY, base_url=fake.url, retry=FAST) as plain:
        assert plain.system_one("t", QUESTIONS).nouls["urgent"].noul == 0.81


def test_a_failed_call_still_raises_and_leaves_its_records(tap, fake, tmp_path):
    fake.script[:] = [401]
    with client(fake, tap) as c, pytest.raises(sdk.TypeSafeAuthenticationError):
        c.system_one("t", QUESTIONS)
    closed = socket.create_server(("127.0.0.1", 0)); port = closed.getsockname()[1]; closed.close()  # nothing listens there
    with tap.attach(TypeSafeClient(api_key=KEY, base_url=f"http://127.0.0.1:{port}", retry=RetryPolicy(max_retries=1, backoff_initial=0.001)),
                    source="ticket-triage") as c, pytest.raises(sdk.TypeSafeAPIConnectionError):
        c.system_one("t", QUESTIONS)
    denied, *down = written(tap.FOLDER)
    assert denied["status"] == 401 and denied["error"] == {"detail": "not allowed"} and denied["response"] is None
    assert [(r["status"], r["retry_count"], r["error"]) for r in down] == [(None, 0, {"jev": "TypeSafe could not be reached (ConnectError)"}),
                                                                         (None, 1, {"jev": "TypeSafe could not be reached (ConnectError)"})]
    stored, rejected = imported(tap.FOLDER, tmp_path)
    assert rejected == 0 and [failure(s) for s in stored] == ["401", "unreachable", "unreachable"]


def test_many_threads_on_one_client_write_whole_lines(tap, tmp_path):
    n = 0
    def answer(request):
        nonlocal n; n += 1
        return httpx2.Response(200, json=ANSWER, headers={"x-typesafe-request-id": f"req_{n}"})
    c = tap.attach(TypeSafeClient(api_key=KEY, base_url=BASE, transport=httpx2.MockTransport(answer)), source="review-bot")
    big = {"diff": "x" * 20_000}  # lines far larger than one write buffer
    def worker(t):
        for i in range(25): c.system_one({**big, "who": t, "i": i}, QUESTIONS)
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    recs = written(tap.FOLDER)
    assert sorted((r["request"]["state"]["who"], r["request"]["state"]["i"]) for r in recs) == [(t, i) for t in range(8) for i in range(25)]
    stored, rejected = imported(tap.FOLDER, tmp_path)
    assert len(stored) == 200 and rejected == 0


def test_a_client_on_its_own_http_client_and_mounts_is_tapped(tap, tmp_path):
    """attach() wraps what the client already has, mounts included (where httpx2 puts proxies from the environment)."""
    def answer(request):
        if request.method == "GET": return httpx2.Response(200, json={"models": []})
        return httpx2.Response(200, json=ANSWER, headers={"x-typesafe-request-id": "req_mounted"})
    http = httpx2.Client(mounts={"https://": httpx2.MockTransport(answer)})
    with tap.attach(TypeSafeClient(api_key=KEY, base_url=BASE, http_client=http), source="review-bot") as c:
        assert c.models.list().models == ()  # a call that is not a decision passes by unrecorded
        c.system_one("Does this change need a second reviewer?", QUESTIONS)
    (rec,) = written(tap.FOLDER)
    assert rec["request_id"] == "req_mounted" and rec["response"] == ANSWER


def test_many_processes_appending_to_one_file_lose_nothing(tap, tmp_path):
    """Windows: open("ab") seeks, then writes, and lost lines when processes shared a file. The tap opens for appending
    only, so each line lands whole."""
    script = ("import importlib.util, sys, time\n"
              "spec = importlib.util.spec_from_file_location('tarnlight_tap', sys.argv[1]); tap = importlib.util.module_from_spec(spec)\n"
              "spec.loader.exec_module(tap); tap.FOLDER = sys.argv[2]\n"
              "time.sleep(max(0, float(sys.argv[4]) - time.time()))\n"
              "for i in range(400): tap._write({'v': 1, 'ts': time.time(), 'source': 'shared', 'label': f'{sys.argv[3]}-{i}', 'error': 'x' * 3000}, 'shared', '')\n")
    start = time.time() + 2.0  # every process is running before any writes
    procs = [subprocess.Popen([sys.executable, "-c", script, str(TAPS / "python" / "tarnlight_tap.py"), str(tap.FOLDER), str(p), str(start)],
                              env=CLEAN_ENV) for p in range(4)]
    assert all(p.wait(60) == 0 for p in procs)
    (f,) = tap.FOLDER.glob("shared-*.jsonl")
    labels = [json.loads(line)["label"] for line in f.read_bytes().split(b"\n") if line]
    assert sorted(labels) == sorted(f"{p}-{i}" for p in range(4) for i in range(400))


def test_no_drop_box_means_nothing_written_and_no_error(tap, fake, tmp_path, monkeypatch):
    shutil.rmtree(tap.FOLDER)  # what `tarnlight uninstall` does
    with client(fake, tap) as c:
        assert c.system_one("t", QUESTIONS).nouls["urgent"].noul == 0.81
    assert not tap.FOLDER.exists() and not any((tmp_path / "home").rglob("*.jsonl"))
    tap.FOLDER.mkdir(); monkeypatch.setattr(tap, "MIN_FREE_BYTES", 1 << 62)  # a disk too full to help fill
    with client(fake, tap) as c:
        c.system_one("t", QUESTIONS)
    assert not list(tap.FOLDER.iterdir())


def test_no_home_folder_leaves_the_tap_off_and_the_app_running(fake, monkeypatch):
    """A bare environment (no USERPROFILE or HOMEPATH, no HOME) makes Path.home() raise. Importing the tap must not, or
    an app that starts fine without the tap file dies at start with it."""
    def no_home(): raise RuntimeError("Could not determine home directory.")
    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    mod = load_tap()
    lines = []; monkeypatch.setattr(mod, "_append", lambda path, data: lines.append(data))
    with mod.attach(TypeSafeClient(api_key=KEY, base_url=fake.url, retry=FAST), source="ticket-triage") as c:
        assert c.system_one("t", QUESTIONS).nouls["urgent"].noul == 0.81
    assert mod.FOLDER is None and lines == []


def test_a_number_for_source_project_or_label_is_written_as_text(tap, tmp_path):
    """The console takes these as text only: an id passed as a number would send every record to the rejects."""
    def answer(request): return httpx2.Response(200, json=ANSWER, headers={"x-typesafe-request-id": "req_ids"})
    with tap.attach(TypeSafeClient(api_key=KEY, base_url=BASE, transport=httpx2.MockTransport(answer)),
                    source=42, project=2024, label=17) as c:
        c.system_one("t", QUESTIONS)
    (rec,) = written(tap.FOLDER)
    assert (rec["source"], rec["project"], rec["label"]) == ("42", "2024", "17")
    stored, rejected = imported(tap.FOLDER, tmp_path)
    assert rejected == 0 and [s["project"] for s in stored] == ["2024"]


def test_the_tap_never_raises_into_the_app(tap, fake, monkeypatch):
    def broken(path, data): raise OSError("disk gone")
    monkeypatch.setattr(tap, "_append", broken)
    with client(fake, tap) as c:
        assert c.system_one("t", QUESTIONS).nouls["urgent"].noul == 0.81
    odd = object()
    assert tap.attach(odd) is odd  # not a client it knows: returned as it was


def test_the_key_and_headers_never_reach_the_drop_box(tap, fake, tmp_path):
    fake.script[:] = ["cut", "ok", 401]
    with client(fake, tap) as c:
        c.system_one({"note": f"the customer pasted {KEY} by mistake"}, QUESTIONS)
        with pytest.raises(sdk.TypeSafeAPIError): c.system_one("t", QUESTIONS)
    assert fake.seen[0]["authorization"] == f"Bearer {KEY}"  # the call itself went out with its key
    text = "".join(f.read_text(encoding="utf-8") for f in tap.FOLDER.glob("*.jsonl"))
    assert len(written(tap.FOLDER)) == 3 and "[redacted]" in text
    for secret in (KEY, "Bearer", "uthorization", "x-typesafe-runtime", "user-agent"): assert secret.lower() not in text.lower()


def test_the_added_cost_per_call_is_well_under_a_millisecond(tap):
    def answer(request): return httpx2.Response(200, json=ANSWER, headers={"x-typesafe-request-id": "req_1"})
    def median_ms(c):
        with c:
            t = []
            for _ in range(300):
                t0 = time.perf_counter(); c.system_one({"ticket": "x" * 2000}, QUESTIONS); t.append(time.perf_counter() - t0)
        return statistics.median(t) * 1000
    plain = median_ms(TypeSafeClient(api_key=KEY, base_url=BASE, transport=httpx2.MockTransport(answer)))
    tapped = median_ms(tap.attach(TypeSafeClient(api_key=KEY, base_url=BASE, transport=httpx2.MockTransport(answer))))
    assert tapped - plain < 1.0, (plain, tapped)  # measured on Windows: about 0.36 ms, most of it opening the file


# ---- JavaScript: node, the real SDK and a local stand-in. Runs only where both are there (CI may have neither).
JS_RUN = r"""
import { pathToFileURL } from 'node:url'
const [tapFile, sdkFile, base, closed, key] = process.argv.slice(2)
const { tarnlightFetch } = await import(pathToFileURL(tapFile).href)
const { TypeSafeClient, noul } = await import(pathToFileURL(sdkFile).href)
const make = (baseURL, retry) => new TypeSafeClient({ apiKey: key, baseURL, logLevel: 'off', timeout: 2000, retry,
  fetch: tarnlightFetch({ source: 'triage-js', project: 'support' }) })
const c = make(base, { maxRetries: 2, backoffInitialMs: 1, backoffMaxMs: 2 })
const q = { urgent: noul('Does the customer need an answer today?') }
const out = []
out.push((await c.systemOne({ state: { ticket: 'The parcel arrived damaged.' }, questions: q }, { headers: { 'X-Tarnlight-Label': 'first-look' } })).answers.urgent.noul)
out.push((await c.systemOne({ state: `pasted ${key} by mistake`, questions: q })).answers.urgent.noul)
out.push((await c.systemOne({ state: 'busy', questions: q })).answers.urgent.noul)
try { await c.systemOne({ state: 'denied', questions: q }); out.push('no error') } catch (e) { out.push(e.constructor.name) }
try { await make(closed, { maxRetries: 0 }).systemOne({ state: 'down', questions: q }); out.push('no error') } catch (e) { out.push(e.constructor.name) }
// an app's own fetch that drops the SDK's signal, and ids passed as numbers: the SDK's timeout still ends a stalled try
const own = (url, { signal, ...rest }) => fetch(url, rest)
const slow = new TypeSafeClient({ apiKey: key, baseURL: base, logLevel: 'off', timeout: 300,
  retry: { maxRetries: 1, backoffInitialMs: 1, backoffMaxMs: 2 }, fetch: tarnlightFetch({ source: 42, project: 7, label: 3, fetch: own }) })
const t0 = performance.now()
out.push((await slow.systemOne({ state: 'hangs', questions: q })).answers.urgent.noul, Math.round(performance.now() - t0))
console.log(JSON.stringify(out))
"""


def js_sdk():
    """The ESM entry of @typesafe-ai/sdk under TARNLIGHT_JS_SDK_DIR (a folder where `npm install @typesafe-ai/sdk` ran)."""
    pkg = Path(os.environ.get("TARNLIGHT_JS_SDK_DIR", "")) / "node_modules" / "@typesafe-ai" / "sdk"
    try:
        entry = json.loads((pkg / "package.json").read_text(encoding="utf-8"))["exports"]["."]["import"]["default"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pkg / entry if os.environ.get("TARNLIGHT_JS_SDK_DIR") and (pkg / entry).is_file() else None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_js_tap_through_the_real_js_sdk(tmp_path, fake):
    entry = js_sdk()
    if entry is None: pytest.skip("set TARNLIGHT_JS_SDK_DIR to a folder where `npm install @typesafe-ai/sdk` ran")
    home = tmp_path / "home"; folder = home / ".tarnlight" / "inbox"; folder.mkdir(parents=True)
    closed = socket.create_server(("127.0.0.1", 0)); port = closed.getsockname()[1]; closed.close()
    script = tmp_path / "run.mjs"; script.write_text(JS_RUN, encoding="utf-8")
    fake.script[:] = ["ok", "cut", "ok", 429, "ok", 401, "hang", "ok"]
    env = {**CLEAN_ENV, "USERPROFILE": str(home), "HOME": str(home)}  # os.homedir() is here, so the tap writes under tmp_path
    run = subprocess.run(["node", str(script), str(TAPS / "js" / "tarnlight-tap.mjs"), str(entry), fake.url, f"http://127.0.0.1:{port}", KEY],
                         env=env, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    *answers, hung_ms = json.loads(run.stdout)
    assert answers == [0.81, 0.81, 0.81, "AuthenticationError", "APIConnectionError", 0.81]
    assert hung_ms < 2500  # the 300 ms timeout and a retry, not the server's five seconds of silence
    recs = [r for r in written(folder) if r["source"] == "triage-js"]
    assert [(r["status"], r["retry_count"]) for r in recs] == [(200, 0), (200, 0), (200, 1), (429, 0), (200, 1), (401, 0), (None, 0)]
    first, cut = recs[0], recs[1]
    assert first["label"] == "first-look" and first["source"] == "triage-js" and first["project"] == "support"
    assert first["sdk"].startswith("node/") and first["request_id"] == "req_fake_1" and first["response"] == ANSWER
    assert first["request"]["state"] == {"ticket": "The parcel arrived damaged."}
    assert cut["response"] is None and cut["error"]["jev"].startswith("the answer was cut off (")
    assert recs[-1]["error"]["jev"].startswith("TypeSafe could not be reached (")
    text = "".join(f.read_text(encoding="utf-8") for f in folder.glob("*.jsonl"))
    assert "[redacted]" in text and KEY not in text and "bearer" not in text.lower() and "uthorization" not in text
    stored, rejected = imported(folder, tmp_path)
    assert rejected == 0 and [failure(s) for s in stored if s["source"] == "triage-js"] == [None, "cut off", None, "429", None, "401", "unreachable"]
    hung, retried = [r for r in written(folder) if r["source"] == "42"]  # the aborted try is written when it ends, before the retry
    assert (hung["project"], hung["label"], hung["retry_count"], hung["error"]) == ("7", "3", 0, {"jev": "timeout or cancelled (AbortError)"})
    assert retried["retry_count"] == 1 and retried["response"] == ANSWER and [s["source"] for s in stored].count("42") == 2
