"""
The proxy: 127.0.0.1:7338 -> https://api.typesafe.ai, inside the console's process on its own thread.

  * every path and method is forwarded as it came (the raw, still percent-encoded path and query; headers minus
    hop-by-hop ones) and the answer returned as it came; only POST .../v1/systemone becomes a record.
    /p/<project>/v1/... is forwarded as /v1/..., labelled with that project
  * a record is the canonical record (storage.ENVELOPE), sent over UDP to the ingest like every other tap. It holds no header: the
    handler passes on only the few header values a record needs, so Authorization and the other secrets reach TypeSafe
    and nothing else. No log line can carry them either: aiohttp's and asyncio's log records are reduced to their
    exception's class, and a request the parser rejects gets an empty 400 instead of aiohttp's echo of the bad line
  * records are built on a worker thread from a bounded queue, so the forwarding never waits for them; a body too large
    for the ingest is recorded without its bodies
  * TypeSafe unreachable, or its answer cut off: the caller's connection is dropped, as a direct call would fail, and the
    record says it was the proxy that saw it. An error answer from TypeSafe (401, 422, 429, 5xx) passes through unchanged
  * only programs on this machine may call it: a Host other than 127.0.0.1 / localhost, or a browser's Origin or
    Sec-Fetch-Site header, are refused, so a web page cannot use it (DNS rebinding, cross-site forms)
  * GET /llms.txt describes the proxy for agents
"""
import asyncio, json, logging, queue, re, socket, threading, time
from urllib.parse import unquote
from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, web, web_protocol
from yarl import URL
from . import ingest
from .replay import from_capture

PORT = 7338
UPSTREAM = "https://api.typesafe.ai"
HOP = {"host", "content-length", "transfer-encoding", "content-encoding", "accept-encoding", "connection", "keep-alive"}
RECORD_HEADERS = ("x-tarnlight-label", "x-tarnlight-source", "x-typesafe-sdk", "x-typesafe-runtime", "x-typesafe-retry-count")
MAX_RECORD_BYTES = ingest.MAX_PARTS * ingest.CHUNK_BYTES * 9 // 10  # request + answer; beyond it the record keeps no bodies
QUEUE_SIZE = 1_000
LOGGERS = ("aiohttp.server", "aiohttp.access", "aiohttp.client", "aiohttp.internal", "aiohttp.web", "asyncio")
LLMS_TXT = """# tarnlight proxy

A local proxy in front of TypeSafe (https://api.typesafe.ai). It forwards every request unchanged and records each
Jev decision (POST /v1/systemone) in the tarnlight console on this machine. API keys are forwarded, never stored.

- Base URL: http://127.0.0.1:{port} (set TYPESAFE_BASE_URL to it; the Python and JavaScript SDKs both read it)
- Label a project: use http://127.0.0.1:{port}/p/<project> as the base URL
- Optional headers: X-Tarnlight-Label (a label for this call), X-Tarnlight-Source (who is calling)
- If the console is closed, calls to this address fail with "connection refused"
"""


class Scrub(logging.Filter):
    """Reduce a log record to its exception's class: aiohttp quotes a malformed header line (a key, maybe) in its message,
    and asyncio's debug mode prints a slow callback's arguments."""
    def filter(self, record):
        cause = type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else "no detail"
        record.msg, record.args = f"tarnlight proxy: {record.name} reported a problem ({cause})", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


_quiet_handle_error = web_protocol.RequestHandler.handle_error


def _handle_error(self, request, status=500, exc=None, message=None):
    """aiohttp's error answer without its message: for a rejected request that message repeats the bad line."""
    return _quiet_handle_error(self, request, status, exc, None)


def shut(loop, runner):
    """Stop an aiohttp server and close its loop without leaving tasks pending (the server's accept loop among them)."""
    async def cancel_the_rest():
        rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in rest: t.cancel()
        await asyncio.gather(*rest, return_exceptions=True)
    loop.run_until_complete(runner.cleanup()); loop.run_until_complete(cancel_the_rest()); loop.close()


def _body(raw):
    """A body as JSON when it is JSON (NaN and Infinity refused, as the ingest refuses them), otherwise as text."""
    if not raw: return None
    try: return ingest._parse(raw)
    except ValueError: return raw.decode("utf-8", "replace")


def _text(v):
    """A header value as sent: aiohttp decodes bytes as UTF-8 with surrogates for the rest, which Latin-1 senders
    (Node's fetch) produce; those are read back as Latin-1."""
    if v is None: return None
    raw = v.encode("utf-8", "surrogateescape")
    try: return raw.decode("utf-8")
    except UnicodeDecodeError: return raw.decode("latin-1")


def record(ts, project, headers, request_id, status, latency_ms, request, response):
    """The canonical record of one Jev call. headers: only the RECORD_HEADERS values, never the whole header map."""
    h = {k: _text(v) for k, v in headers.items() if v is not None}
    rec = from_capture({"ts": ts, "request_headers": h, "response_headers": {"x-typesafe-request-id": request_id} if request_id else {},
                        "status": status, "latency_ms": latency_ms, "request": request, "response": response})
    rec["project"] = project
    if h.get("x-tarnlight-source"): rec["source"] = h["x-tarnlight-source"]
    return rec


class Proxy:
    """Start with start(); it binds 127.0.0.1:port (0 = any free port, for tests) or raises RuntimeError."""
    def __init__(self, port=PORT, upstream=UPSTREAM, sink=("127.0.0.1", ingest.PORT)):
        self.port, self.upstream, self.sink = port, upstream.rstrip("/"), sink
        self.counts = {"forwarded": 0, "recorded": 0, "not recorded": 0, "unreachable": 0, "refused": 0}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ready, self.error, self.loop = threading.Event(), None, None
        self.records = queue.Queue(QUEUE_SIZE)
        self.thread = threading.Thread(target=self._run, name="tarnlight-proxy", daemon=True)
        self.writer = threading.Thread(target=self._write, name="tarnlight-proxy-records", daemon=True)

    def start(self):
        if self.port and _someone_listens(self.port):  # on Windows a 0.0.0.0 listener would not stop our bind, but be shadowed by it
            raise RuntimeError(f"TCP port {self.port} is in use: is another tarnlight (or another program) listening there?")
        self.thread.start(); self.ready.wait()
        if self.error is not None:
            self.thread.join(); self.sock.close()
            e = self.error
            if isinstance(e, OSError) and e.errno in (98, 10048):
                raise RuntimeError(f"TCP port {self.port} is in use: is another tarnlight (or another program) listening there?") from e
            raise RuntimeError(f"TCP port {self.port} could not be opened: {getattr(e, 'strerror', None) or e}") from e
        self.writer.start()
        return self

    def stop(self):
        if self.loop is not None and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop); self.thread.join()
        if self.writer.is_alive(): self.records.put(None); self.writer.join()
        self.sock.close()

    # ---- the proxy's own thread
    def _run(self):
        loop = self.loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        for name in LOGGERS:
            logger = logging.getLogger(name)
            if not any(isinstance(f, Scrub) for f in logger.filters): logger.addFilter(Scrub())
        web_protocol.RequestHandler.handle_error = _handle_error  # this process serves only the proxy
        app = web.Application(client_max_size=64 << 20); app.cleanup_ctx.append(self._session)
        app.router.add_route("*", r"/{path:[\s\S]*}", self._forward)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)  # calls in flight get 1 s when the console closes
        try:
            loop.run_until_complete(runner.setup())
            loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", self.port).start())
            self.port = runner.addresses[0][1]
        except BaseException as e:  # a bad port number too: start() must never wait forever
            self.error = e; shut(loop, runner); self.ready.set(); return
        self.ready.set()
        try:
            loop.run_forever()
        finally:
            shut(loop, runner)

    async def _session(self, app):
        self.http = ClientSession(auto_decompress=True, cookie_jar=DummyCookieJar(),  # no cookie passes from one caller to another
                                  connector=TCPConnector(limit=0), timeout=ClientTimeout(total=None, sock_connect=30, sock_read=300))
        yield
        await self.http.close()

    async def _forward(self, req):
        raw_path, raw_query = req.rel_url.raw_path, req.rel_url.raw_query_string
        local = (req.host or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")  # a rebound DNS name is not local
        if local and req.method == "GET" and raw_path == "/llms.txt":  # plain text, forwards nothing: a browser may read it
            return web.Response(text=LLMS_TXT.format(port=self.port), content_type="text/plain")
        if not local or {"origin", "sec-fetch-site"} & {k.lower() for k in req.headers}:  # what browsers send; Node's fetch
            self.counts["refused"] += 1                                                    # sends only sec-fetch-mode
            return web.Response(status=403, text="tarnlight proxy: only programs on this machine may call it, not web pages")
        project = None
        if raw_path.startswith("/p/"):  # /p/<project>/v1/...: the project labels the call, TypeSafe sees /v1/...
            name, slash, tail = raw_path[3:].partition("/")
            project, raw_path = unquote(name) or None, "/" + tail if slash else "/"
        target = URL(self.upstream + raw_path + (f"?{raw_query}" if raw_query else ""), encoded=True)
        raw, t0, ts = await req.read(), time.perf_counter(), time.time()
        headers = [(k, v) for k, v in req.headers.items() if k.lower() not in HOP]
        decision = req.method == "POST" and re.sub("/+", "/", unquote(raw_path)).rstrip("/") == "/v1/systemone"
        meta = {k: req.headers.get(k) for k in RECORD_HEADERS} if decision else None
        self.counts["forwarded"] += 1
        status = up_headers = data = failure = None
        try:
            async with self.http.request(req.method, target, data=raw, headers=headers, allow_redirects=False) as up:
                status, up_headers = up.status, list(up.headers.items())
                data = await up.read()
        except (ClientError, asyncio.TimeoutError, OSError) as e:
            failure = f"tarnlight proxy: {'the answer from TypeSafe was cut off' if status else 'TypeSafe could not be reached'} ({type(e).__name__})"
            self.counts["unreachable"] += 1
        latency_ms = (time.perf_counter() - t0) * 1000
        gone = req.transport is None or req.transport.is_closing()
        if decision:
            request_id = next((v for k, v in up_headers or () if k.lower() == "x-typesafe-request-id"), None)
            self._queue(ts, project, meta, request_id, status, latency_ms, raw, data, failure, gone)
        if failure is not None:  # as a direct call would see it: no answer at all, never one made up here
            if req.transport is not None: req.transport.abort()
            return web.Response(status=502)
        return web.Response(body=data, status=status, headers=[(k, v) for k, v in up_headers if k.lower() not in HOP])

    def _queue(self, *item):
        try: self.records.put_nowait(item)
        except queue.Full: self.counts["not recorded"] += 1

    # ---- the records thread
    def _write(self):
        while (item := self.records.get()) is not None:
            ts, project, meta, request_id, status, latency_ms, raw, data, failure, gone = item
            try:
                small = len(raw) + len(data or b"") <= MAX_RECORD_BYTES
                rec = record(ts, project, meta, request_id, status, latency_ms, _body(raw) if small else None,
                             _body(data) if small and failure is None else None)
                notes = [failure] if failure else []
                if not small: notes.append(f"tarnlight proxy: {len(raw) + len(data or b''):,} bytes, too large to record the bodies")
                if gone: notes.append("tarnlight proxy: the caller hung up before the answer came")
                if notes: rec["error"] = {"proxy": " / ".join(notes), **({"answer": rec["error"]} if rec["error"] is not None else {})}
                ingest.send(rec, self.sock, self.sink); self.counts["recorded"] += 1
            except Exception:
                self.counts["not recorded"] += 1


def _someone_listens(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2): return True
    except OSError:
        return False
