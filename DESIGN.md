# Tarnlight design

Tarnlight is a desktop app that records the decisions your programs get from Jev, shows them live, and lets you grade them. It works with Jev, the System One model from TypeSafe, through its `/v1/systemone` API.

Tarnlight is unofficial. It is not affiliated with or endorsed by TypeSafe.

It is free and open source under the Apache-2.0 license. It is written in Python 3.11+: PySide6 and pyqtgraph for the window, aiohttp for the optional proxy, SQLite and zstandard for the log. It needs no account of its own and sends nothing anywhere, except that the optional proxy forwards calls to TypeSafe. There is no telemetry and no update check. It is built on Windows, and every measurement below was taken there.

## Why

Jev answers typed questions with probabilities. For choice and score questions it also returns a confidence. Code often gates actions on that number: act above some value, hand the case to a person below it.

A confidence is only useful if it is calibrated on your own questions. A model's calibration is measured on its maker's data. On your data, 80% may be right 95% of the time, or 55%. The number alone cannot tell you which.

The usual way to find out: log each decision, grade it against what really happened, and compare confidence with how often it was right. Tarnlight does the logging, the live view and the grading, on your own machine. The comparison view (calibration) is not built yet. The grades it needs are stored today and can be exported.

## Running it

From a clone:

```
pip install .
tarnlight                              # the console: a new session log, the drop box, the UDP listener, the proxy, the window
tarnlight install                      # make the drop box folder, ~/.tarnlight/inbox
tarnlight uninstall                    # remove it; callers stop leaving copies
tarnlight replay FILE.jsonl --speed 10 # send a file of records to a running console
```

`python -m tarnlight` works the same. `--port` sets the UDP port (default 7337) and `--proxy-port` the proxy's TCP port (default 7338). Each start opens a new session log, `~/.tarnlight/sessions/<date>-<n>.jevlog`.

## How calls get in

There are three ways in. All of them end in the same checks and the same log.

### The drop box (the main way)

Callers call TypeSafe directly, as if Tarnlight did not exist. After each call, failed ones included, they append one record to a file in `~/.tarnlight/inbox`. The console imports the folder while it is open. So a closed, crashed or missing console can never fail or slow a call, and the API key never passes through Tarnlight.

`tarnlight install` makes the folder and changes nothing else: no setting, no environment variable, no background program. `tarnlight uninstall` removes it and says how many files were not imported yet.

Rules for a writer:

- Write only if `~/.tarnlight/inbox` exists. Then removing the folder turns every writer off.
- Append to `<source>-<YYYY-MM-DDTHH>.jsonl`, named for the current UTC hour. Only append.
- One record per line, UTF-8, ending in a newline. A line is read only once its newline is there.
- Wrap the write so it can never raise into the caller.
- Never write headers or the key. Only the record fields listed below are kept; any other field is dropped.
- `cost_est_micro` may be left out. The console fills it from `usage.input_tokens`.

What the console does with the folder:

- On start it reads what arrived while it was closed, then looks for new lines every 0.05 s.
- It keeps a read offset per file in `inbox/.positions.json`, keyed by file name and file id. An offset is saved only after every line before it is stored. Killing the console loses nothing: lines read but not yet stored are read again next time.
- Delivery is at least once. In kill tests with 4,800 calls, none were lost and 0 to 448 were stored twice. Duplicates are not removed.
- It deletes a file once every line in it is stored and the file's hour ended more than 5 minutes ago. It never writes to a file a caller writes to.
- A line that is not JSON, fails the checks, or is over 16 MB goes to the rejects file. So does half a line left at the end of a finished hour. NUL bytes, which a torn write can leave, separate records. A leading BOM is ignored. One file that cannot be read never stops the others.
- Once the backlog from while it was closed is stored, the status bar says "Caught up: N calls" and a blue line in the feed marks the last of them.

Why the drop box and not a proxy: a proxy breaks its callers whenever it is down. Measured: a call to a closed local port fails after 1.25 s with the JavaScript SDK and after 7.35 s with the Python SDK.

### The proxy (optional, one app at a time)

The console runs a proxy on `127.0.0.1:7338` on its own thread. Point one program at it for one run:

```
TYPESAFE_BASE_URL=http://127.0.0.1:7338/p/<project> python my_app.py
```

Both official SDKs read `TYPESAFE_BASE_URL`. Never set it machine-wide: every call through it fails while the console is closed. If the console finds the variable pointing at a local port where its proxy is not listening, it shows a red banner. It looks in this program's environment, the Windows user environment and Claude Code's `~/.claude/settings.json`. `tarnlight install` warns when the variable points at the proxy's address, `127.0.0.1:7338`, or at any `localhost` port.

- It forwards every method and path unchanged and returns the answer unchanged. Only `POST .../v1/systemone` becomes a record.
- `/p/<project>/` labels the project and is removed before forwarding.
- Optional request headers: `X-Tarnlight-Label` (a label for this call) and `X-Tarnlight-Source` (who is calling).
- A record carries no header. The proxy passes on only the label, the source, the three `X-TypeSafe-*` request headers and the response's request id.
- If TypeSafe cannot be reached, or its answer is cut off, the caller's connection is dropped, as a direct call would fail. The proxy never makes up an answer. The record's `error` says what the proxy saw. Error answers from TypeSafe (401, 429, 5xx) pass through unchanged.
- Only programs on this machine may use it. A Host other than 127.0.0.1 or localhost, or a browser's Origin or Sec-Fetch-Site header, gets a 403.
- Records are built on a worker thread from a queue of 1,000. Past that, a call is still forwarded but counted as not recorded. A call whose bodies exceed about 2.6 MB is recorded without its bodies.
- `GET /llms.txt` describes the proxy in plain text for agents.
- Measured: it adds 0.56 ms per call (median).

### UDP and replay

The console listens on UDP `127.0.0.1:7337`, one JSON record per datagram. A record over 60,000 bytes travels as chunks `{"id", "part", "of", "data"}`, where `data` is base64 of a 44,880-byte slice of the record's UTF-8 JSON and `part` counts from 0. A record may have at most 64 parts (about 2.9 MB), at most 256 records may be unfinished at once, and a record not complete within 5 s is rejected. The proxy sends its records this way.

`tarnlight replay FILE` re-sends a JSONL file of records (a JSONL export, for example) with its original spacing, divided by `--speed`. It also reads captured pairs and turns each into a record. A captured pair is one JSON line with `ts`, `path`, `request_headers`, `response_headers`, `status`, `latency_ms`, `request` and `response`. Only pairs whose `path` starts with `/v1/systemone` become records; the others, and pairs with no `path`, are skipped. A pair missing any other field stops the replay with an error before anything is sent. Records keep their own `source`; a captured pair gets `proxy`. Measured: 612 real calls replayed at 100 per second all arrived, and the last was stored within 100 ms of being sent.

### Checks at the door

The drop box and UDP run the same checks, on the envelope only: `v` is 1, `ts` is a number, the text fields are text or null, the number fields are finite numbers or null. NaN, Infinity and numbers too large for a float are refused. `request`, `response` and `error` may be any JSON, so a failed call whose body was not JSON is kept.

Anything refused goes to `<session>.rejects.jsonl` with the reason, the byte size and a blake2b hash, never the content. That file stops growing at 10 MB; the count keeps going and shows in the status bar.

## The canonical record

One JSON object per call. The wire request and response are stored verbatim, in their key order, inside this envelope:

| Field | Meaning |
|---|---|
| `v` | Format version, always 1 |
| `ts` | When the call was made, seconds since the epoch |
| `seq` | Assigned by the log on arrival; a sender's value is ignored |
| `source` | Who sent it; names the sender's chip ("unknown" when missing) |
| `session_id`, `tool_use_id` | Optional: which agent session or tool call made it |
| `project`, `label` | Optional labels; the proxy fills `project` from `/p/<project>/` |
| `sdk` | `<language>/<sdk version>`, for example `python/0.7.1` |
| `request_id` | The `x-typesafe-request-id` response header |
| `latency_ms`, `status` | Measured by the sender; HTTP status, or null when no answer came |
| `retry_count` | From `X-TypeSafe-Retry-Count`; the SDKs leave it out on a first attempt, which means 0 |
| `cost_est_micro` | Estimated cost in millionths of a dollar |
| `request`, `response` | The wire bodies, verbatim; never headers. `response` is null for a failed call |
| `error` | The error body verbatim, or a note from the sender or the proxy, or null |

## Wire format facts that matter

Checked on a capture of 616 real calls. A request is `POST https://api.typesafe.ai/v1/systemone` with a `state` (any JSON), a `model` and a map of named `questions`, each `noul` (yes or no), `choice` or `score`. The answer:

```
{"model": "jev-1.13.0",
 "answers": {"<name>": {"type": "noul", "noul": 0.83},
             "<name>": {"type": "choice", "choice": "<option>", "probabilities": {"<option>": 0.61, ...}, "confidence": 0.48},
             "<name>": {"type": "score", "score": 1.12, "legend": {"0": "<level>", ...}, "probabilities": {"0": 0.2, ...}, "confidence": 0.35}},
 "usage": {"input_tokens": 399, "output_tokens": 0}}
```

- One call carries many questions. Tarnlight indexes, charts and grades per (call, question name).
- `noul` has no `confidence`. Tarnlight uses max(p, 1 - p) as its confidence and keeps p itself for the chart.
- For `choice` and `score`, Tarnlight shows the API's own `confidence`, so thresholds compare with anyone else's. The formula is not published.
- `score` is a float expected value between 0 and N-1, not a level. `probabilities` and `legend` are keyed by index strings "0" to "N-1", and `legend[i]` is the request's `criteria[i]`. The feed shows the score with its nearest level.
- `choice` probability keys come in no fixed order. Tarnlight ranks options by probability.
- `choice` is not always the likeliest option (3 of 915 answers, all near ties). Tarnlight keeps the chosen answer and the top probability apart, and the gauge names Jev's own pick.
- Numbers are 2-decimal floats. The index stores per-mille integers, which lose nothing.
- The response carries no latency, cost or request id. The request id is in the `x-typesafe-request-id` header, on every response, errors too. Cost is estimated from input tokens at TypeSafe's published price, $0.042 per million (output free), and always shown as an estimate.
- Error bodies come in more than one shape, so they are stored verbatim.
- The rate limit is 1,200 requests a minute; past it TypeSafe answers 429.

## Storage

One SQLite file per session (WAL mode), with `<name>.hot.jsonl`, `<name>.lock` and `<name>.rejects.jsonl` beside it.

- Records are plain JSON lines, grouped into blocks of 512 (or whatever arrived in 5 s, or on close). Each block is compressed as one unit with zstd level 3.
- On close, every block is recompressed at level 19, neighbours are merged up to 2,048 records, and the file is vacuumed.
- Index tables are not compressed, so a query on them never decompresses a block. The window's chart, gauge, counters and feed do not query the log at all: they are fed from the in-memory ring (see below). The window reads the index for grades, the CSV export and finding a sender's previous call. Blocks are decompressed for the inspector, the JSONL export, the share bundle and the compaction on close.
  - `call`: one row per call: time, source, project, session, request id, latency, status, retries, sdk, tokens, estimated cost, state size and hash.
  - `answer`: one row per (call, question): type, confidence, margin, top probability, p(yes) and the chosen answer, as per-mille integers. Keyed by (question, seq) with no rowid, so one question's rows are one range read.
  - `qname` (question names as small numbers), `schema` (each distinct question set, once), `outcome` (grades), `meta` (format version and privacy mode).

Crash safety:

- Every record is appended to the hot file before it joins a block. Opening the file again recovers what was not committed, skipping any seq already in the log.
- `append()` raises only when a record was not stored. A failed flush (locked or full database) is retried after 5 s; the records stay safe in the hot file.
- One writer per file, enforced by an OS lock on `<name>.lock`. The OS drops it if the process dies; a relaunch waits up to 5 s for it.
- A record whose answers cannot be indexed is still stored, with empty index fields.
- Refused before anything is written, so every reader can read back what was stored: nesting deeper than 200 levels, keys that are not text, records over 16 MB, NaN and Infinity. Blocks are capped at 64 MB.
- The writer appends to the log first, then puts the record in the in-memory ring the window reads (the newest 20,000 records, at most 64 MB, without request and error). So the ring only drops records that are on disk. A full disk makes the writer wait and retry.
- Tested: after a replay of 616 records and a hard kill, reopening the session file found all 616.

Why blocks: per-record compression cannot use the redundancy between records. On Jev-shaped traffic, compressing whole blocks beat every per-record scheme tried (question-set interning, state dedup, JSON-patch deltas, quantized answers, a trained zstd dictionary). A trained dictionary hurts once blocks reach 64 records. msgpack is smaller raw but compresses worse than JSON, so records stay JSON. JSON-patch deltas fall apart on states with shifting arrays.

Measured on real Jev calls, records stored verbatim, blocks of 512 (bytes per call):

| Workload | Raw | Live, zstd-3 | Compacted, zstd-19 |
|---|---|---|---|
| polling (10 Hz loop, large state) | 3,674 | 267 (13.8x) | 190 (19.4x) |
| agent (chat-paced) | 2,088 | 123 (17.0x) | 92 (22.6x) |
| triage (ticket triage) | 1,895 | 110 (17.2x) | 85 (22.4x) |

Each workload was 153 to 303 calls, so each fit in one block. Whether larger blocks pay off is not settled.

With the index: a 20 minute polling session at 10 Hz (12,000 calls, five questions each) takes about 6.1 MB, about 200 bytes per call of blocks and 300 of index. That is about 18 MB an hour, or 440 MB for a full day at 10 Hz. Writing runs at about 3,900 records a second (a 10 Hz loop needs 10). Reading one question's 12,000 rows from the index takes about 5 ms.

## Privacy and the API key

The API key is never stored.

- With the drop box, the key never reaches Tarnlight. Any field outside the record format is dropped before writing.
- The proxy forwards the key to TypeSafe and puts no header in a record. Its server log lines are cut down to the exception's class name, and a request its parser rejects gets an empty 400, so a bad header line is never echoed.
- "Copy as curl" writes `$TYPESAFE_API_KEY`, not the key. "Replay this one" reads the key from the environment for that one request and keeps it nowhere.
- Tested: after real calls through the proxy, a search of 10,070 files on the test machine found the key 0 times.
- Limit: the key passes through the console's memory while the proxy forwards a call and can stay in freed buffers. A memory dump of a running console could find it.

What is stored, all under `~/.tarnlight/` on this machine:

- `sessions/`: each call's full request (state and questions) and response, error bodies, the envelope fields, and your grades with any note and the time. Plus the rejects files: reason, size and hash only.
- `inbox/`: drop box files until they are stored and their hour is over, and the read offsets.
- `bands.json`: your bands per question name.

The log format has three privacy modes: `full` (store as is), `hash` (replace the state with its blake2b hash, top-level key names and byte size) and `redact` (drop listed JSON paths under request, response or error). The mode is applied before the hot file is written and again on every export, and a file keeps the mode it was created with. The console opens every new session in `full`; a setting to pick another mode is not built yet. `hash` is unkeyed, so a state with few possible values (a PIN, a flag, a known email) can be recovered by guessing.

## The window

Dark theme. IBM Plex Sans and JetBrains Mono ship inside the package under the SIL Open Font License.

- **Toolbar.** App name and session file. One chip per sender, with a green dot when it was heard from in the last 5 s and its calls per second; as many as fit, the rest behind "+N". Your bands for the charted question. Review queue with its count. Export. Pause.
- **Confidence chart.** One question at a time. Up to four question chips, the rest behind "+N more". Until you pick one, it shows the most frequent question of the last 1,000 decisions, and switches only when another is 1.25 times as frequent. Amber line: confidence. Blue line: top probability or margin (top minus second), toggled; p(yes) for noul. Dashed escalate (red) and review (amber) lines can be dragged; the zone below escalate is tinted red. X axis: decision number or wall clock. Ranges: last 240, 1k, 5k, all. Up to 20,000 points per question. Click a point to select it in the feed.
- **Gauge.** The charted question's latest decision as a needle on an arc coloured by your bands, the percent, and one plain line. For a choice it says what Jev picked and how close the runner-up came; for yes/no, the answer and p(yes); for a score, the nearest level and its legend. Below it, "Below escalate N decisions in a row". Its tooltip says confidence is not correctness.
- **Counters.** Requests per minute against 1,200, with the 429 count. Latency mean and p95 over the last 200 calls. Estimated cost this session and per hour at the last minute's rate. Share of decisions below escalate.
- **Feed.** Newest first, one row per (call, question): time, source, question, answer, a bar (choice and yes/no: the chosen share amber, the rest grey; score: one blue fill up to the score's position; failed call: an empty hatched bar), confidence, ms, outcome. A red edge marks rows below escalate. A failed call is one row with its status or what failed (timeout, unreachable, cut off, no answer) and cannot be graded. A call sent again with "replay this one" has its source shown in blue. Presets: all, review queue, this question, graded. The filter takes words (question, source, answer) and comparisons like `conf<40` or `ms>200`. It keeps the newest 100,000 decisions.
- **Inspector.** The selected decision, read in full from the log: the answer, its confidence in its band colour, a bar per option, what was asked, your bands, the call's other questions with ten small blocks each, the state by top-level keys with its hash and size, what changed since the same sender's previous call, and a line of tokens, latency, estimated cost, status and request id. Buttons: state as JSON, copy as curl, save as fixture (the record as a JSON file, through the privacy filter), replay this one (the same request through the proxy: a new, paid call, labelled with the old seq).
- **Status bar.** Ingest and proxy lights, last packet age, notes (refused records, calls not recorded, drop box off, catching up), log size and last write, "api key never stored", uptime.

**Bands.** Per question name: escalate below E, review below R, auto at R and above. Default 40 and 70. Set them in the toolbar boxes (applied on Enter) or by dragging the chart lines. They are saved at once to `~/.tarnlight/bands.json`, and escalate never ends up above review. A change recounts the counter and the review queue for the whole session. Jev does not set bands; you do.

**Grading keys.** The keyboard belongs to the feed. A focused text box keeps its own keys, so typing never grades.

| Key | Action |
|---|---|
| 1 / 2 / 3 | Grade the selected decision correct / wrong / flagged, then move to the next row |
| Space | Pause or resume |
| / | Jump to the filter |
| E | Export menu |
| T | Set escalate one percent above the selected decision, so it and everything below it escalate |
| Escape | Cancel a band being typed; give the keys back to the feed |

A grade is saved to the log at once, per (call, question), and a new grade replaces the old one. **Review queue:** decisions below their question's escalate that are not graded yet. Its count covers the whole session. In the queue a graded row leaves and the next takes its place. Pause freezes the chart, the gauge and the feed so rows stay put while grading; decisions keep being stored and counted.

**Export** runs in the background, and the status bar says where it went. Every export passes the privacy filter.

- JSONL: full records.
- CSV: one row per (call, question) from the index, with its grade; values per-mille as stored; no state.
- Share bundle (.zip): a compacted copy of the log with its grades, and a README of the format.

Measured drawing cost, with 20,000 points and 100 records a second arriving: about 9 ms a frame at "last 240" (p95 12.5 ms) and 11 ms at "all" (p95 14.5 ms), under the 16.7 ms of 60 frames a second. The window reads new records every 50 ms, never once per record, updates the feed five times a second, and draws nothing while minimised.

## Known limits

- The window shows only the session it started. Decisions older than the feed's newest 100,000 stay in the log but cannot be graded from the window.
- Each start opens a new session, and nothing reopens the last one. After a crash, the records it had not put in a block wait in its hot file, safe on disk, until code opens that session file again. The window cannot open an old session yet.
- The drop box delivers at least once: a hard kill while it catches up can store the last tenth of a second twice.
- Any program on this machine can send records to the UDP port or the drop box. There is no sender check.
- A replay faster than about 2,000 records a second loses datagrams in the OS socket buffer, where nothing can count them. Real traffic is capped at 1,200 requests a minute per key.
- The chart tracks at most 1,000 question names; past that the least recently seen leaves the chart. The feed and the log keep everything.
- Very large states (0.5 MB each) make a flush take seconds, and the inspector waits for it. With "state as JSON" open, each selection redraws the state (about 200 ms for 0.5 MB).
- Finding a sender's previous call scans the call table (about 70 ms at a million calls). Taking in 20,000 records at once takes about 3 s.
- The ingest backlog limit (64 MB) counts datagram bytes; parsed, that can be about 350 MB.
- The proxy has no headless mode, its upstream is fixed at `https://api.typesafe.ai`, it does not use `HTTPS_PROXY`, and header bytes that are not UTF-8 lose those bytes on the way to TypeSafe.
- Only Windows is tested.

## Not built yet

- **Calibration view.** Per question: a reliability diagram (confidence bins against accuracy from your grades), Brier score, ECE, counts per bin, and a fitted Platt curve exported as (a, b) to paste into your own code. It will draw nothing until a question has at least 50 grades, and say so.
- **Packaging.** No installer or frozen build yet. Run it from source.
- **Taps.** Small helpers that write drop box records from inside an app: a transport for the Python SDK and a fetch wrapper for the JavaScript SDK. Hooks for coding agents that fill `session_id` and `tool_use_id`, so you can see which session made a call.
- **Settings and commands.** A setting to pick the privacy mode; commands for a headless proxy, export, grading, calibration, compaction and a health check; replay that writes test fixtures.
- **Retention.** Rolling old blocks into per-minute summaries, and Parquet export.
