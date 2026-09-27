"""The proxy, against a fake TypeSafe on this machine: nothing here reaches the real API."""
import asyncio, json, socket, statistics, threading, time, urllib.error, urllib.request
import pytest
from aiohttp import web
from tarnlight import proxy as proxy_module
from tarnlight.ingest import Ingest
from tarnlight.proxy import Proxy
from tarnlight.storage import JevLog

KEY = "ts_FAKE_NOT_A_REAL_KEY_7f3a9c"   # stands in for a key; it must never leave the proxy except towards TypeSafe


class FakeTypeSafe:
    """A stand-in for api.typesafe.ai: answers what the test tells it to and remembers what it was sent. host: the name the
    proxy uses for it (aiohttp keeps no cookies for a bare IP, so the cookie test needs 'localhost'); delay: seconds before
    answering; cut: send the status line and half the body, then hang up."""
    def __init__(self, host="127.0.0.1"):
        self.seen, self.raw_paths, self.reply = [], [], (200, {"answers": {}, "usage": {"input_tokens": 1000, "output_tokens": 50}}, {})
        self.host, self.delay, self.cut = host, 0, False
        self.ready = threading.Event(); self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start(); self.ready.wait()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        app = web.Application(); app.router.add_route("*", r"/{path:[\s\S]*}", self._handle)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.5); self.loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0); self.loop.run_until_complete(site.start())
        self.url = f"http://{self.host}:{site._server.sockets[0].getsockname()[1]}"; self.ready.set(); self.loop.run_forever()
        proxy_module.shut(self.loop, runner)

    async def _handle(self, req):
        self.seen.append((req.method, req.path_qs, dict(req.headers), await req.read())); self.raw_paths.append(req.raw_path)
        if self.delay: await asyncio.sleep(self.delay)
        status, body, headers = self.reply
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"x-typesafe-request-id": "req_fake_1", "Content-Type": "application/json", "Set-Cookie": "session=SECRET_COOKIE", **headers}
        if self.cut:
            resp = web.StreamResponse(status=status, headers=headers); resp.content_length = len(data)
            await resp.prepare(req); await resp.write(data[:len(data) // 2]); req.transport.close(); return resp
        return web.Response(body=data, status=status, headers=headers)

    def close(self): self.loop.call_soon_threadsafe(self.loop.stop); self.thread.join()


@pytest.fixture
def world():
    """A fake TypeSafe, a proxy in front of it, and a UDP socket standing in for the ingest."""
    upstream = FakeTypeSafe()
    catch = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); catch.bind(("127.0.0.1", 0)); catch.settimeout(3)
    catch.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    p = Proxy(port=0, upstream=upstream.url, sink=catch.getsockname()).start()
    yield p, upstream, catch
    p.stop(); upstream.close(); catch.close()


def call(p, path, body=None, method=None, headers=()):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(f"http://127.0.0.1:{p.port}{path}", data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", **dict(headers)})
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


def received(catch):
    data, _ = catch.recvfrom(1 << 16); return data, json.loads(data)


def nothing_more(catch):
    catch.settimeout(0.3)
    with pytest.raises(socket.timeout): catch.recvfrom(1 << 16)


QUESTION = {"state": {"tick": 1}, "questions": {"go": {"type": "noul", "instructions": "Go?"}}}
ANSWER = {"model": "jev-1.13.0", "answers": {"go": {"type": "noul", "noul": 0.8}}, "usage": {"input_tokens": 1000, "output_tokens": 50}}


def test_a_jev_call_is_forwarded_as_it_came_and_recorded(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    status, body, headers = call(p, "/v1/systemone", QUESTION, headers={"X-TypeSafe-SDK": "typesafe-python/0.7.1", "X-TypeSafe-Runtime": "python/3.11",
                                                                      "X-TypeSafe-Retry-Count": "1", "X-Tarnlight-Label": "demo"})
    assert (status, json.loads(body), headers["x-typesafe-request-id"]) == (200, ANSWER, "req_fake_1")
    method, path, sent, raw = up.seen[0]
    assert (method, path, json.loads(raw), sent["Authorization"]) == ("POST", "/v1/systemone", QUESTION, f"Bearer {KEY}")  # the key reaches TypeSafe
    data, rec = received(catch)
    assert KEY.encode() not in data and b"SECRET_COOKIE" not in data  # and nothing else
    assert (rec["source"], rec["project"], rec["label"], rec["sdk"], rec["retry_count"], rec["request_id"], rec["status"]) == \
           ("proxy", None, "demo", "python/0.7.1", 1, "req_fake_1", 200)
    assert rec["request"] == QUESTION and rec["response"] == ANSWER and rec["error"] is None and rec["cost_est_micro"] == 42
    assert 0 <= rec["latency_ms"] < 5000 and abs(rec["ts"] - time.time()) < 5


def test_every_secret_header_is_kept_out_of_the_record(world):
    p, up, catch = world; up.reply = (200, ANSWER, {"Authorization": "Bearer UPSTREAM_SECRET"})
    secrets = {"X-Api-Key": "SECRET_1", "Api-Key": "SECRET_2", "Cookie": "SECRET_3", "Proxy-Authorization": "SECRET_4"}
    call(p, "/v1/systemone", QUESTION, headers=secrets)
    data, _ = received(catch)
    assert not any(s.encode() in data for s in (KEY, "SECRET_1", "SECRET_2", "SECRET_3", "SECRET_4", "SECRET_COOKIE", "UPSTREAM_SECRET"))
    assert all(up.seen[0][2].get(k) == v for k, v in secrets.items())  # forwarded to TypeSafe all the same


def test_a_project_path_labels_the_call(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    call(p, "/p/ticket-triage/v1/systemone?x=1", QUESTION)
    assert up.seen[0][1] == "/v1/systemone?x=1"
    assert received(catch)[1]["project"] == "ticket-triage"


def test_a_source_header_names_the_sender(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    call(p, "/v1/systemone", QUESTION, headers={"X-Tarnlight-Source": "my-app"})
    assert received(catch)[1]["source"] == "my-app"


def test_error_answers_pass_through_unchanged(world):
    p, up, catch = world
    for status, body in ((429, {"detail": "rate limited"}), (401, {"detail": "bad key"}), (422, {"detail": [{"msg": "x", "input": 3}]})):
        up.reply = (status, body, {"retry-after": "2"})
        got, raw, headers = call(p, "/v1/systemone", QUESTION)
        assert (got, json.loads(raw), headers["retry-after"]) == (status, body, "2")
        rec = received(catch)[1]
        assert (rec["status"], rec["response"], rec["error"], rec["cost_est_micro"]) == (status, None, body, 0)


def test_other_paths_pass_through_and_are_not_recorded(world):
    p, up, catch = world; up.reply = (200, {"data": [{"id": "jev-latest"}]}, {})
    status, body, _ = call(p, "/v1/models?limit=5")
    assert (status, json.loads(body)) == (200, {"data": [{"id": "jev-latest"}]}) and up.seen[0][:2] == ("GET", "/v1/models?limit=5")
    call(p, "/v2/whatever", {"a": 1}, method="PUT")
    assert up.seen[1][:2] == ("PUT", "/v2/whatever")
    call(p, "/v1/systemone", method="GET")  # not a decision either
    nothing_more(catch)


def test_an_unreachable_typesafe_fails_the_call_as_a_direct_call_would(world):
    from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient
    from typesafe_sdk import TypeSafeAPIConnectionError
    _, _, catch = world
    dead = Proxy(port=0, upstream="http://127.0.0.1:9", sink=catch.getsockname()).start()  # nothing listens on port 9
    try:
        with pytest.raises(OSError): call(dead, "/v1/systemone", QUESTION)  # no answer at all, never one made up
        with TypeSafeClient(api_key=KEY, base_url=f"http://127.0.0.1:{dead.port}", retry=RetryPolicy(max_retries=0)) as c:
            with pytest.raises(TypeSafeAPIConnectionError): c.system_one(state={"a": 1}, questions={"q": Noul(instructions="?")})
    finally:
        dead.stop()
    rec = received(catch)[1]
    assert rec["status"] is None and rec["response"] is None and "TypeSafe could not be reached" in rec["error"]["proxy"]


def test_an_answer_cut_off_keeps_its_status_and_request_id(world):
    p, up, catch = world; up.reply, up.cut = (200, ANSWER, {}), True
    with pytest.raises(OSError): call(p, "/v1/systemone", QUESTION)
    rec = received(catch)[1]
    assert (rec["status"], rec["request_id"], rec["response"]) == (200, "req_fake_1", None) and "cut off" in rec["error"]["proxy"]


def test_odd_bodies_are_recorded_as_text(world):
    p, up, catch = world
    up.reply = (200, b'{"answers": NaN}', {})
    call(p, "/v1/systemone", b"not json at all")
    rec = received(catch)[1]
    assert rec["request"] == "not json at all" and rec["response"] == '{"answers": NaN}'


def test_a_record_too_big_to_send_is_counted_and_the_caller_still_gets_the_answer(world, monkeypatch):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    monkeypatch.setattr(proxy_module.ingest, "MAX_PARTS", 1)  # anything over one chunk is too big
    status, body, _ = call(p, "/v1/systemone", {"state": "x" * 100_000, "questions": {}})
    assert status == 200 and json.loads(body) == ANSWER
    time.sleep(0.2); assert p.counts["not recorded"] == 1


def test_llms_txt_describes_the_proxy(world):
    p, up, _ = world
    status, body, headers = call(p, "/llms.txt")
    assert status == 200 and f"http://127.0.0.1:{p.port}/p/<project>" in body.decode() and not up.seen


def test_a_busy_port_is_refused_clearly(world):
    p, _, _ = world
    with pytest.raises(RuntimeError, match=f"TCP port {p.port} is in use"): Proxy(port=p.port).start()


def test_a_listener_on_all_addresses_is_not_shadowed():
    other = socket.socket(); other.bind(("0.0.0.0", 0)); other.listen(); port = other.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match="in use"): Proxy(port=port).start()  # on Windows the bind itself would succeed
    finally:
        other.close()


def test_a_bad_port_number_fails_at_once():
    t = time.monotonic()
    with pytest.raises(RuntimeError, match="could not be opened"): Proxy(port=70000).start()
    assert time.monotonic() - t < 5


def test_the_python_sdk_works_through_the_proxy(world, sample_records):
    from typesafe_sdk import Noul, TypeSafeClient
    p, up, catch = world
    sample = next(r for r in sample_records if r["status"] == 200)
    up.reply = (200, sample["response"], {})
    with TypeSafeClient(api_key=KEY, base_url=f"http://127.0.0.1:{p.port}/p/sdk-test", headers={"X-Tarnlight-Label": "sdk"}) as client:
        result = client.system_one(state=sample["request"]["state"], questions={"go": Noul(instructions="Go?")})
    assert result.usage.input_tokens == sample["response"]["usage"]["input_tokens"]
    data, rec = received(catch)
    assert KEY.encode() not in data and (rec["project"], rec["label"], rec["status"]) == ("sdk-test", "sdk", 200)
    assert rec["sdk"] is not None and rec["sdk"].startswith("python/")


def test_decisions_reach_the_log_through_the_real_ingest(tmp_path):
    log = JevLog(tmp_path / "s.jevlog"); ing = Ingest(log, port=0).start(); up = FakeTypeSafe(); up.reply = (200, ANSWER, {})
    p = Proxy(port=0, upstream=up.url, sink=("127.0.0.1", ing.port)).start()
    try:
        for _ in range(20): call(p, "/v1/systemone", QUESTION)
        end = time.monotonic() + 5
        while ing.counts["stored"] < 20 and time.monotonic() < end: time.sleep(0.02)
    finally:
        p.stop(); up.close(); ing.stop()
    recs = [r for _, r in (log._flush(), log.iter_records())[1]]
    assert len(recs) == 20 and all(r["response"] == ANSWER for r in recs) and KEY not in log.path.read_bytes().decode("latin-1")
    log.close()


def test_the_proxy_adds_little_time(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    direct, proxied = [], []
    for _ in range(60):
        for target, out in ((up.url, direct), (f"http://127.0.0.1:{p.port}", proxied)):
            t = time.perf_counter()
            urllib.request.urlopen(urllib.request.Request(target + "/v1/systemone", data=json.dumps(QUESTION).encode(),
                                                          headers={"Content-Type": "application/json"}), timeout=5).read()
            out.append((time.perf_counter() - t) * 1000)
    overhead = statistics.median(proxied) - statistics.median(direct)
    print(f"median direct {statistics.median(direct):.2f} ms, through the proxy {statistics.median(proxied):.2f} ms, overhead {overhead:.2f} ms")
    assert overhead < 5  # the target is under 2 ms; this bound only catches a gross regression on a busy machine


# ---- edge cases

def raw_request(p, lines):
    """Send bytes the way a broken client would (a stray control character, an overlong header); returns the reply."""
    s = socket.create_connection(("127.0.0.1", p.port), timeout=5); s.sendall(lines)
    reply = b""
    try:
        while chunk := s.recv(65536): reply += chunk
    except OSError:
        pass
    s.close(); return reply


def test_a_malformed_secret_header_is_never_echoed_or_logged(world, capfd, caplog):
    p, _, _ = world
    caplog.set_level("DEBUG")
    bad = [b"POST /v1/systemone HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + KEY.encode() + b"\x01\r\nContent-Length: 0\r\n\r\n",
           b"POST /v1/systemone HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + KEY.encode() + b"A" * 9000 + b"\r\n\r\n",
           b"GET /v1/models?api_key=" + KEY.encode() + b"\x01 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
           b"POST /v1/systemone HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: " + KEY.encode() + b"\x0b\r\n\r\n"]
    for lines in bad:
        reply = raw_request(p, lines)
        assert reply.split(bytes([13, 10]))[0].endswith(b" 400 Bad Request") and KEY.encode() not in reply
    time.sleep(0.2)
    out, err = capfd.readouterr()
    assert KEY not in out + err and KEY not in caplog.text and "reported a problem" in caplog.text
    assert call(p, "/v1/models")[0] == 200  # and it keeps working


def test_log_records_are_reduced_to_their_cause():
    import logging
    rec = logging.LogRecord("aiohttp.server", logging.ERROR, __file__, 1, "Error handling request %s", (f"Bearer {KEY}",), None)
    proxy_module.Scrub().filter(rec)
    assert KEY not in rec.getMessage() and rec.args == ()


def test_cookies_never_pass_from_one_caller_to_another(tmp_path):
    up = FakeTypeSafe(host="localhost"); catch = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); catch.bind(("127.0.0.1", 0))
    p = Proxy(port=0, upstream=up.url, sink=catch.getsockname()).start()
    try:
        call(p, "/v1/models"); call(p, "/v1/models"); call(p, "/v1/models", headers={"Cookie": "mine=c"})
    finally:
        p.stop(); up.close(); catch.close()
    assert [h.get("Cookie") for _, _, h, _ in up.seen] == [None, None, "mine=c"]  # the answer's Set-Cookie was not kept


def test_web_pages_are_refused(world):
    p, up, _ = world
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, {"Host": "evil.example"}):
        assert call(p, "/v1/systemone", QUESTION, headers=headers)[0] == 403
    assert not up.seen and p.counts["refused"] == 3
    assert call(p, "/llms.txt", headers={"Sec-Fetch-Site": "none"})[0] == 200  # reading the description is fine
    assert call(p, "/v1/systemone", QUESTION, headers={"Sec-Fetch-Mode": "cors"})[0] == 200  # Node's fetch (the JS SDK, or a Node hook)


def test_paths_and_queries_are_forwarded_as_they_came(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    for path in ("/v1/models?q=C%23%20and%20F%23&x=1&x=2", "/v1/a%2Fb", "/v1/models%3Fx=1", "/v1/a%0Ab"):
        call(p, path)
    assert up.raw_paths == ["/v1/models?q=C%23%20and%20F%23&x=1&x=2", "/v1/a%2Fb", "/v1/models%3Fx=1", "/v1/a%0Ab"]
    call(p, "/p/team%2Fmy-app/v1/systemone", QUESTION)
    assert up.raw_paths[-1] == "/v1/systemone" and received(catch)[1]["project"] == "team/my-app"


def test_decision_paths_with_doubled_slashes_are_recorded(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    call(p, "//v1/systemone", QUESTION); call(p, "/p//v1/systemone", QUESTION)
    assert up.raw_paths == ["//v1/systemone", "/v1/systemone"]  # forwarded unchanged (an empty project name is dropped)
    assert [received(catch)[1]["project"] for _ in range(2)] == [None, None]


def test_a_latin_1_label_is_read_as_sent(world):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    reply = raw_request(p, b"POST /v1/systemone HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Tarnlight-Label: r\xe9sum\xe9\r\nContent-Length: 2\r\n\r\n{}")
    assert reply.startswith(b"HTTP/1.1 200") and received(catch)[1]["label"] == "résumé"


def test_a_record_too_big_to_send_keeps_its_envelope(world, monkeypatch):
    p, up, catch = world; up.reply = (200, ANSWER, {})
    monkeypatch.setattr(proxy_module, "MAX_RECORD_BYTES", 10_000)
    status, body, _ = call(p, "/v1/systemone", {"state": "x" * 100_000, "questions": {}})
    assert status == 200 and json.loads(body) == ANSWER
    rec = received(catch)[1]
    assert (rec["status"], rec["request_id"], rec["request"], rec["response"]) == (200, "req_fake_1", None, None)
    assert "too large to record the bodies" in rec["error"]["proxy"]


def test_a_caller_that_hung_up_is_marked(world):
    p, up, catch = world; up.reply, up.delay = (200, ANSWER, {}), 1.0
    req = urllib.request.Request(f"http://127.0.0.1:{p.port}/v1/systemone", data=json.dumps(QUESTION).encode(), method="POST")
    with pytest.raises(OSError): urllib.request.urlopen(req, timeout=0.3)  # the SDK gave up and retried, say
    rec = received(catch)[1]
    assert rec["response"] == ANSWER and "hung up before the answer came" in rec["error"]["proxy"]


def test_many_slow_calls_do_not_hold_up_a_quick_one(world):
    p, up, _ = world; up.delay = 2.0
    slow = [threading.Thread(target=call, args=(p, "/v1/models"), daemon=True) for _ in range(110)]
    for t in slow: t.start()
    time.sleep(0.5); up.delay = 0
    t0 = time.monotonic(); status, _, _ = call(p, "/v1/models")
    assert status == 200 and time.monotonic() - t0 < 1.0  # no pool of 100 connections to wait for
    for t in slow: t.join()


def test_stop_does_not_wait_long_for_calls_in_flight(world):
    p, up, _ = world; up.delay = 10
    threading.Thread(target=lambda: pytest.raises(Exception, call, p, "/v1/models"), daemon=True).start()
    time.sleep(0.3); t0 = time.monotonic(); p.stop()
    assert time.monotonic() - t0 < 4
    p.stop = lambda: None  # already stopped for the fixture's teardown
