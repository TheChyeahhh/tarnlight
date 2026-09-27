"""Replay: re-emit records over UDP with their original spacing, sped up by `speed`."""
import json, socket, time
from .ingest import PORT, send

PRICE_PER_MTOK = 0.042  # USD per 1M input tokens, output free (docs.typesafe.ai/models, jev-1.13)


def from_capture(cap):
    """The canonical record for one captured request/response pair (request_headers, response_headers, status,
    latency_ms, request, response)."""
    h = {k.lower(): v for k, v in cap["request_headers"].items()}
    rh = {k.lower(): v for k, v in cap["response_headers"].items()}
    ok = cap["status"] == 200
    sdk = h.get("x-typesafe-sdk")
    usage = cap["response"].get("usage") if ok and isinstance(cap["response"], dict) else None
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    try: retries = int(h.get("x-typesafe-retry-count", 0))
    except ValueError: retries = None  # a header that is not a number: unknown, not an error
    return {"v": 1, "ts": cap["ts"], "seq": None, "source": "proxy", "session_id": None, "tool_use_id": None, "project": None,
            "label": h.get("x-tarnlight-label"),
            "sdk": f"{h.get('x-typesafe-runtime', '?').split('/')[0]}/{sdk.split('/')[-1]}" if sdk else None,
            "request_id": rh.get("x-typesafe-request-id"), "latency_ms": round(cap["latency_ms"]), "status": cap["status"],
            "retry_count": retries,
            "cost_est_micro": round(tokens * PRICE_PER_MTOK) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else 0,
            "request": cap["request"], "response": cap["response"] if ok else None, "error": None if ok else cap["response"]}


def read_records(path):
    """Records from a JSONL file: canonical records (an export) or a capture file, whose Jev calls are converted."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            r = json.loads(line)
            if "request_headers" in r:  # a captured pair
                if r.get("path", "").startswith("/v1/systemone"): out.append(from_capture(r))
            else:
                out.append(r)
    return out


def replay(records, speed=1.0, addr=("127.0.0.1", PORT)):
    """Send each record at its original time offset divided by speed. Returns how many were sent."""
    if not speed > 0: raise ValueError("speed must be above 0")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start, t0, sent = time.perf_counter(), None, 0  # perf_counter: time.monotonic ticks only every 15.6 ms on Windows
    try:
        for rec in records:
            t0 = rec["ts"] if t0 is None else t0
            wait = start + (rec["ts"] - t0) / speed - time.perf_counter()
            if wait > 0: time.sleep(wait)
            send(rec, sock, addr); sent += 1
    finally:
        sock.close()
    return sent
