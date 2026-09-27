"""
Tarnlight tap for the TypeSafe Python SDK (typesafe-sdk). Copy this one file into your app. After every Jev call, and
every failed one, it appends one record to the Tarnlight drop box, ~/.tarnlight/inbox, where the console picks it up.

    try:
        from tarnlight_tap import attach  # inside a package: from .tarnlight_tap import attach
    except Exception:                     # no tap file, or one that cannot load: the app runs exactly as before
        def attach(client, **_): return client

    client = attach(TypeSafeClient(), source="ticket-triage", project="support")   # AsyncTypeSafeClient too

  * the call is left alone: attach() wraps the transport the client already has, so proxies, timeouts and the SDK's
    retries work as before. The answer is copied as the SDK reads it, never read ahead of it: reading it in the
    transport breaks the SDK's retry of an answer cut off halfway
  * one record per attempt, as the proxy does: a call the SDK retries leaves one record per try, each with its
    retry_count. A try that failed says why in error, {"jev": "..."}, in the words the console sorts failures by
  * writes only while ~/.tarnlight/inbox exists (`tarnlight uninstall` removes it and so turns every tap off) and
    more than MIN_FREE_BYTES are free on its disk. Never a header, never the key: the key is also cut out of the
    bodies. Nothing here can raise into the app
  * whole lines even with many threads and processes on one file: a process-wide lock, and a file handle opened for
    appending only. On Windows open("ab") seeks, then writes, and lost lines when several processes shared a file
  * a label or source for one call: extra_headers={"X-Tarnlight-Label": "..."} (or X-Tarnlight-Source), as with the
    proxy. Like any header you add, it also goes to TypeSafe

Importing it needs only the standard library; the httpx2 classes it builds on come with typesafe-sdk and are loaded
by attach(). This file never imports the tarnlight package.
"""
import json, os, re, shutil, sys, threading, time
from pathlib import Path

try: FOLDER = Path.home() / ".tarnlight" / "inbox"
except Exception: FOLDER = None   # no home folder to be found (a bare environment): the tap is off, the app runs on
MIN_FREE_BYTES = 1 << 30   # never help fill a disk
_lock = threading.Lock()   # one line at a time from this process
_kinds = None              # (httpx2.AsyncClient, Tap, AsyncTap), made by the first attach()


def attach(client, *, source="python", project=None, label=None):
    """Record every Jev call this TypeSafeClient or AsyncTypeSafeClient makes. Returns the client; one this version
    cannot tap is returned as it was, still working, just not recorded."""
    try:
        async_client, tap, async_tap = _classes()
        http = client._http_client
        kind = async_tap if isinstance(http, async_client) else tap
        text = lambda x: None if x is None else str(x)  # the console takes these as text only: an id of 7 becomes "7"
        settings = (re.sub(r"[^A-Za-z0-9._-]+", "_", str(source))[:64] or "python", text(project), text(label))
        wrap = lambda t: t if t is None or isinstance(t, (tap, async_tap)) else kind(t, settings)
        http._transport = wrap(http._transport)
        http._mounts = {pattern: wrap(t) for pattern, t in http._mounts.items()}  # proxies set in the environment
    except Exception:
        pass
    return client


def _classes():
    """The transports and streams, made on first use from httpx2, so importing this file never needs it."""
    global _kinds
    if _kinds: return _kinds
    import httpx2

    class Copy(httpx2.SyncByteStream):
        """The answer's bytes as the SDK reads them: a read error reaches the SDK untouched, so it retries as before."""
        def __init__(self, inner, attempt): self.inner, self.attempt, self.parts = inner, attempt, []

        def __iter__(self):
            try:
                for chunk in self.inner:
                    self.parts.append(chunk); yield chunk
            except GeneratorExit:
                raise  # the reader stopped early: close() records it
            except BaseException as e:
                self.attempt.end(e); raise
            self.attempt.end(None, self.parts)

        def close(self):
            try: self.inner.close()
            finally: self.attempt.end(None)

    class AsyncCopy(httpx2.AsyncByteStream):
        def __init__(self, inner, attempt): self.inner, self.attempt, self.parts = inner, attempt, []

        async def __aiter__(self):
            try:
                async for chunk in self.inner:
                    self.parts.append(chunk); yield chunk
            except GeneratorExit:
                raise
            except BaseException as e:
                self.attempt.end(e); raise
            self.attempt.end(None, self.parts)

        async def aclose(self):
            try: await self.inner.aclose()
            finally: self.attempt.end(None)

    class Tap(httpx2.BaseTransport):
        def __init__(self, inner, settings): self.inner, self.settings = inner, settings

        def handle_request(self, request):
            if not _decision(request): return self.inner.handle_request(request)
            attempt = _Attempt(request, self.settings)
            try:
                response = self.inner.handle_request(request)
            except BaseException as e:
                attempt.end(e); raise
            return attempt.watch(response, Copy)

        def close(self): self.inner.close()

    class AsyncTap(httpx2.AsyncBaseTransport):
        def __init__(self, inner, settings): self.inner, self.settings = inner, settings

        async def handle_async_request(self, request):
            if not _decision(request): return await self.inner.handle_async_request(request)
            attempt = _Attempt(request, self.settings)
            try:
                response = await self.inner.handle_async_request(request)
            except BaseException as e:
                attempt.end(e); raise
            return attempt.watch(response, AsyncCopy)

        async def aclose(self): await self.inner.aclose()

    _kinds = (httpx2.AsyncClient, Tap, AsyncTap)
    return _kinds


def _decision(request):
    try: return request.method == "POST" and request.url.path.rstrip("/").endswith("/v1/systemone")
    except Exception: return False


class _Attempt:
    """One try of a Jev call, from its request to its one record."""
    def __init__(self, request, settings):
        self.request, self.settings, self.response, self.done = request, settings, None, False
        self.ts, self.t0 = time.time(), time.perf_counter()

    def watch(self, response, kind):
        self.response = response
        try:
            if response.is_stream_consumed: self.end(None, [response.content], decoded=True)  # a test double read it already
            else: response.stream = kind(response.stream, self)
        except Exception:
            pass
        return response

    def end(self, error, parts=None, decoded=False):
        """Write the record, once: parts is the whole answer; otherwise error (None: the answer was not read to the end)."""
        if self.done: return
        self.done = True
        try:
            source, project, label = self.settings
            h, response = self.request.headers, self.response
            status = response.status_code if response is not None else None
            body = note = None
            if parts is not None:
                raw = b"".join(parts)
                try: body = _json(raw if decoded else _decoded(response, raw))
                except Exception: note = "the tap could not decode the answer"
            else:
                note = _note(error, response is not None)
            ok = status == 200 and note is None
            try: retries = int(h.get("x-typesafe-retry-count", 0))
            except ValueError: retries = None
            sdk, runtime = h.get("x-typesafe-sdk"), h.get("x-typesafe-runtime", "?")
            rec = {"v": 1, "ts": self.ts, "source": h.get("x-tarnlight-source") or source, "project": project,
                   "label": h.get("x-tarnlight-label") or label,
                   "sdk": f"{runtime.split('/')[0]}/{sdk.split('/')[-1]}" if sdk else None,
                   "request_id": response.headers.get("x-typesafe-request-id") if response is not None else None,
                   "latency_ms": round((time.perf_counter() - self.t0) * 1000), "status": status, "retry_count": retries,
                   "request": _json(_content(self.request)), "response": body if ok else None,
                   "error": None if ok else {"jev": note} if note else body}
            _write(rec, source, h.get("authorization", "").partition(" ")[2].strip())
        except Exception:
            pass  # a record lost, never a call


def _content(request):
    try: return request.content
    except Exception: return None  # a streamed body the transport has not read


def _decoded(response, raw):
    """The answer as the SDK saw it: a gzip or other encoded body is decoded by httpx2 itself, with its own decoders."""
    if response.headers.get("content-encoding", "identity").strip().lower() in ("", "identity"): return raw
    return type(response)(response.status_code, headers=response.headers, content=raw).content


def _not_json(name): raise ValueError(name)


def _json(raw):
    """A body as JSON (without NaN or Infinity), else as text, so a failed call keeps what came back."""
    if not raw: return None
    try: return json.loads(raw, parse_constant=_not_json)
    except (ValueError, RecursionError): return raw.decode("utf-8", "replace")


def _note(error, answered):
    """What the app saw, in the words the console sorts failed calls by: timeout, cut off, could not be reached."""
    if error is None: return "the answer was not read to the end"
    name = type(error).__name__
    what = ("timeout" if "Timeout" in name else "the answer was cut off" if answered
            else "TypeSafe could not be reached" if "Connect" in name else "no answer")
    return f"{what} ({name})"


def _write(rec, source, key):
    """Append the record as one line to this hour's file, when the drop box is there and the disk has room."""
    folder = FOLDER
    if folder is None or not os.path.isdir(folder) or shutil.disk_usage(folder).free < MIN_FREE_BYTES: return
    line = json.dumps(rec, separators=(",", ":"), allow_nan=False)
    if key: line = line.replace(json.dumps(key)[1:-1], "[redacted]")  # an app that put its key in a state
    path = os.path.join(folder, f"{source}-{time.strftime('%Y-%m-%dT%H', time.gmtime())}.jsonl")
    with _lock: _append(path, (line + "\n").encode())


if sys.platform == "win32":
    import _winapi

    def _append(path, data):
        """A handle opened for appending only: each WriteFile lands whole at the end, whoever else is appending."""
        h = _winapi.CreateFile(path, 0x4 | 0x100000, 0x7, 0, 4, 0x80, 0)  # FILE_APPEND_DATA | SYNCHRONIZE; share all; OPEN_ALWAYS
        try: _winapi.WriteFile(h, data)
        finally: _winapi.CloseHandle(h)
else:
    def _append(path, data):
        """O_APPEND: the kernel puts each write whole at the end."""
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try: os.write(fd, data)
        finally: os.close(fd)
