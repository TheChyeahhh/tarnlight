# Taps

A tap is one small file you copy into your own app. After every Jev call your app makes, the tap leaves a copy of that call in Tarnlight's drop box, the folder `~/.tarnlight/inbox`. The Tarnlight console reads that folder, so the call shows up there, even when the console was closed at the time.

Your app still calls TypeSafe directly, the same as before. If Tarnlight is closed, missing or broken, your calls are not slowed down or stopped.

First, once: open Tarnlight (or run `tarnlight install`). Either makes the folder. A tap writes nothing until the folder exists.

## Python (typesafe-sdk)

Copy `python/tarnlight_tap.py` next to the script you run, or anywhere on your import path, then:

```python
try:
    from tarnlight_tap import attach
except Exception:                    # no tap file, or one that cannot load: the app runs as before
    def attach(client, **_): return client

client = attach(TypeSafeClient(), source="ticket-triage", project="support")
```

`AsyncTypeSafeClient` works the same way. Nothing to install: the file uses the standard library and the httpx2 that typesafe-sdk already brings.

If the code that makes the client sits inside a package (run as `python -m yourapp.main`), put the file in that package and import it as `from .tarnlight_tap import attach`. A plain `from tarnlight_tap import attach` does not find it there, the guard above hides that, and nothing is recorded.

## JavaScript (@typesafe-ai/sdk)

Copy `js/tarnlight-tap.mjs` next to your code, then, in an ES module (a `.mjs` file, or `"type": "module"` in package.json):

```js
const tap = await import('./tarnlight-tap.mjs').catch(() => null)   // no tap file: the app runs as before
const client = new TypeSafeClient({ fetch: tap?.tarnlightFetch({ source: 'ticket-triage', project: 'support' }) })
```

In CommonJS (a `.js` file that uses `require`, which is the default), top-level `await` is not allowed. Use:

```js
let tap = null
try { tap = require('./tarnlight-tap.mjs') } catch {}   // no tap file: the app runs as before
const client = new TypeSafeClient({ fetch: tap?.tarnlightFetch({ source: 'ticket-triage', project: 'support' }) })
```

That `require` needs Node 20.19, 22.12 or later. On an older Node it fails, the `catch` keeps the app running, and nothing is recorded.

If you already pass your own `fetch`, hand it to the tap, and keep it for when the tap file is missing:

```js
const client = new TypeSafeClient({ fetch: tap ? tap.tarnlightFetch({ fetch: myFetch, source: 'ticket-triage' }) : myFetch })
```

Node 20 or later, no dependencies.

## Naming your calls

- `source` names your app in the console (its chip) and in the file name. The default is `python` or `node`.
- `project` is an optional group name.
- Both are written as text, which is what the console accepts: a number such as `2024` is written as `"2024"`.
- A label for one call: send the header `X-Tarnlight-Label` with that call (Python: `extra_headers={...}`; JavaScript: the call's `headers` option). Like any header you add, it also goes to TypeSafe.

## What gets written

One line of JSON per attempt, in `~/.tarnlight/inbox/<source>-<UTC date and hour>.jsonl`: the time, source, project, label, SDK version, request id, how long it took, the HTTP status, the retry count, the request body (state and questions) and the answer.

When the SDK retries a call, each try is its own line, with `retry_count` 0, 1, 2. A try that failed says why: the error body TypeSafe sent back, or a short note such as `timeout`, `the answer was cut off` or `TypeSafe could not be reached`. Your app still sees the same answer or the same error it would see without the tap.

## What never gets written

- No headers. Your API key is never written. If the key turns up inside a state or an answer, it is replaced with `[redacted]`.
- Nothing at all when the folder is missing, or when its disk has less than 1 GB free.
- A tap never throws an error into your app. If a line cannot be written, the call goes on and only that line is lost.

## Turning it off

`tarnlight uninstall` removes the folder. Every tap stops writing at once, and the code can stay in your app. To take a tap out for good, delete its file: the guarded import above keeps the app running.

Use a tap or the proxy for an app, not both, or each call shows up twice.

## Cost

About 0.4 ms per call with a small state, most of it opening the file. It grows with the size of the call, since the tap reads, checks and writes all of it: roughly 6 ms more per MB of state in Python, about 2 ms in JavaScript. The work runs in your app's own thread (in JavaScript and in async Python, on the event loop). Many threads or processes can write to the same file: each line lands whole.
