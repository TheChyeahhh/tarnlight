"""
The drop box. Callers call TypeSafe directly and, after each call,
append one canonical record per line to ~/.tarnlight/inbox/<source>-<YYYY-MM-DDTHH>.jsonl (the UTC hour). So a closed,
crashed or missing console never touches a call. The console makes the folder when it starts, so the drop box is on
from the first time it is opened. While the console is open, this thread imports the folder: first
everything that arrived while it was closed, then new lines as they come (it looks every POLL_S).

  * a read position per file, kept in inbox/.positions.json and keyed by name and file id, so a file removed and made
    again under the same name starts from 0. Only complete lines are read: a line being written waits for its newline
  * every line goes through the ingest like a datagram: the same checks, privacy mode and rejects file. A missing cost
    estimate is filled from the answer's token count. When the writer is behind, the line stays and is read again. A
    line that cannot be taken (not JSON, a number too large, longer than storage.MAX_LINE) goes to the rejects file,
    and NUL bytes, which a torn write leaves behind, separate records
  * at least once: a position is saved, and a file deleted, only after the ingest has stored every line before it. So
    killing the console loses nothing; what it had read but not stored is read again next time (a duplicate is
    possible, a loss is not). A normal close waits up to STOP_WAIT_S for the writer, so it leaves no duplicates
  * a file is deleted once it is fully stored and its hour ended more than GRACE_S ago, since writers only append to the
    current hour's file. Half a line at the end of such a file will never be finished: it goes to the rejects file.
    Nothing here writes to a file a caller writes to, and a file that cannot be read never stops the others
"""
import codecs, collections, contextlib, json, os, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import ingest as ingest_module
from .replay import PRICE_PER_MTOK
from .storage import MAX_LINE

FOLDER = Path.home() / ".tarnlight" / "inbox"
POSITIONS = ".positions.json"
POLL_S, GRACE_S = 0.05, 300   # a look costs a few file stats; 0.05 s keeps a decision within 100 ms of reaching the screen
READ_BYTES = 4 << 20   # read at most this much of one file per look, so a large backlog cannot hold the thread for long
STOP_WAIT_S = 5.0


def hour_over(name, now, grace_s=GRACE_S):
    """True when the file's hour (from its name) ended more than grace_s ago. A name without an hour never is."""
    try:
        start = datetime.strptime(name[-19:-6], "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now > start + timedelta(hours=1, seconds=grace_s)


class Inbox:
    def __init__(self, ingest, folder=FOLDER):
        self.ingest, self.folder = ingest, Path(folder)
        self.counts = {"imported": 0, "waiting bytes": 0, "at start": None}
        self.positions = self._load()           # saved: every line before each is stored
        self.read = dict(self.positions)        # handed to the ingest, or refused
        self.handed = collections.deque()       # (the ingest's queue count, file key, position) per line handed over
        self.long = {}                          # key -> (line start, scanned to): a long line looking for its end
        self.first = 0                          # calls queued until the first look that found nothing waiting
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="tarnlight-inbox", daemon=True)

    def start(self):
        with contextlib.suppress(OSError): self.folder.mkdir(parents=True, exist_ok=True)  # the drop box is on while the console runs
        self.thread.start(); return self

    def stop(self):
        """Stop looking, give the writer a moment to store what was handed over, and save how far that got."""
        self.stopping.set()
        if self.thread.ident is not None: self.thread.join()
        end = time.monotonic() + STOP_WAIT_S
        while self.handed and self.handed[-1][0] > self.ingest.handled and time.monotonic() < end: time.sleep(0.02)
        self._commit()

    def caught_up(self):
        """(calls that arrived while the console was closed, the seq of the last of them) once they are all stored."""
        n = self.counts["at start"]
        if n is None or (n and self.ingest.marked_seq is None): return None
        return n, self.ingest.marked_seq

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.poll()
            except Exception:
                pass  # a folder vanishing mid-look, a full disk: the next look tries again
            self.stopping.wait(POLL_S)

    # ---- one look at the folder
    def poll(self, now=None):
        """Hand the complete new lines of every file to the ingest, save what it has stored, delete finished files.
        Returns how many calls were handed over."""
        if not self.folder.is_dir(): return 0
        now, queued, waiting, sizes, unsure = now or datetime.now(timezone.utc), 0, 0, {}, set()
        for f in sorted(self.folder.glob("*.jsonl")):
            try:
                st = f.stat()
                key = f"{f.name}:{st.st_ino}"; sizes[key] = (f, st.st_size)
                pos = self.read.get(key, 0)
                if pos > st.st_size: pos = 0  # a different file under the same name and id: read it all
                if pos < st.st_size:
                    n, pos, tail = self._read(key, f, pos, st.st_size); queued += n
                    if tail and hour_over(f.name, now):  # half a line in a finished hour: nobody will finish it
                        self.ingest._reject(None, f"drop box: an unfinished last line of {st.st_size - pos:,} bytes")
                        pos = st.st_size
                if pos != self.read.get(key): self._hand(key, pos); self.read[key] = pos; self._commit()  # save as it goes
                waiting += st.st_size - pos
            except OSError:
                unsure.add(f.name)  # a file locked or vanishing mid-look: the others go on, this one is tried again
        for key in [k for k in self.read if k not in sizes and k.rsplit(":", 1)[0] not in unsure]:
            del self.read[key]; self.long.pop(key, None)  # the file is gone, or was made again with a new id
        self._commit(now, sizes)
        self.counts["imported"] += queued; self.counts["waiting bytes"] = waiting
        if self.counts["at start"] is None:
            self.first += queued
            if not waiting:  # everything from while the console was closed is handed over
                self.counts["at start"] = self.first
                if self.first: self.ingest.mark(self.ingest.queued)
        return queued

    def _hand(self, key, pos):
        """Lines of this file up to pos are handed over: saved once the writer has handled everything queued so far."""
        ticket = self.ingest.queued
        if self.handed and self.handed[-1][:2] == (ticket, key): self.handed[-1] = (ticket, key, pos)  # nothing queued since
        else: self.handed.append((ticket, key, pos))

    def _commit(self, now=None, sizes=None):
        """Save the positions the ingest has stored up to; with this look's sizes, also delete finished files."""
        before = dict(self.positions)
        while self.handed and self.handed[0][0] <= self.ingest.handled:
            _, key, pos = self.handed.popleft(); self.positions[key] = pos
        for key in [k for k in self.positions if k not in self.read]: del self.positions[key]
        for key, pos in list(self.positions.items()) if sizes is not None else ():
            f, size = sizes.get(key, (None, -1))
            if pos == size and hour_over(f.name, now):
                try:
                    f.unlink()
                except OSError:
                    continue  # a writer still holds it open: try again on a later look
                del self.positions[key]; self.read.pop(key, None)
        if self.positions != before: self._save()

    def _read(self, key, path, pos, size):
        """Hand the complete lines after pos to the ingest, READ_BYTES at most. Returns (calls queued, the position after
        the last line taken, whether what is left is half a line)."""
        with open(path, "rb") as f:
            if key in self.long: return self._long_line(f, key, pos, size)
            f.seek(pos); data = f.read(min(size - pos, READ_BYTES))
            queued, start = 0, 0
            while (end := data.find(b"\n", start)) != -1:
                took = self._take(data[start:end])
                if not took: return queued, pos + start, False  # the writer is behind: this line stays for the next look
                queued += took == "queued"; start = end + 1; self._hand(key, pos + start)
            if start or len(data) < READ_BYTES: return queued, pos + start, pos + len(data) == size and start < len(data)
            return self._long_line(f, key, pos, size)  # no newline in READ_BYTES: one long line

    def _long_line(self, f, key, pos, size):
        """A line longer than READ_BYTES: find its end, scanning only what was not scanned before, then take it, or refuse
        it when it is longer than a record can be."""
        start, scanned = self.long.get(key, (pos, pos))
        if start != pos: scanned = pos
        f.seek(scanned)
        while scanned < size:
            chunk = f.read(min(size - scanned, READ_BYTES))
            if (i := chunk.find(b"\n")) != -1: end = scanned + i; break
            scanned += len(chunk)
        else:
            self.long[key] = (pos, scanned); return 0, pos, True
        self.long.pop(key, None)
        if end - pos > MAX_LINE:
            self.ingest._reject(None, f"drop box line of {end - pos:,} bytes; the limit is {MAX_LINE:,}")
            self._hand(key, end + 1); return 0, end + 1, False
        f.seek(pos); took = self._take(f.read(end - pos))
        if not took: return 0, pos, False
        self._hand(key, end + 1); return int(took == "queued"), end + 1, False

    def _take(self, line):
        """One line to the ingest: "queued", "refused", or False when the writer is behind. NUL bytes separate records."""
        result = "refused"
        for part in line.split(b"\0"):
            part = part.strip().removeprefix(codecs.BOM_UTF8)  # a BOM, from a writer that adds one
            if not part: continue
            try:
                took = self._take_one(part)
            except Exception as e:  # one line must never stop the drop box
                self.ingest._reject(part, f"drop box line not taken: {type(e).__name__}"); took = "refused"
            if not took: return False
            if took == "queued": result = "queued"
        return result

    def _take_one(self, line):
        try:
            msg = ingest_module._parse(line)
        except (ValueError, RecursionError) as e:
            self.ingest._reject(line, f"drop box line is not JSON: {type(e).__name__}"); return "refused"
        if isinstance(msg, dict) and msg.get("cost_est_micro") is None:
            usage = (msg.get("response") or {}).get("usage") if isinstance(msg.get("response"), dict) else None
            tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            if ingest_module._finite(tokens) and 0 <= tokens < 1e12: msg["cost_est_micro"] = round(tokens * PRICE_PER_MTOK)
        return self.ingest.offer(msg, line)

    # ---- positions
    def _load(self):
        try:
            data = json.loads((self.folder / POSITIONS).read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, int) and v >= 0} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.folder / f"{POSITIONS}.tmp"
        try:
            tmp.write_text(json.dumps(self.positions), encoding="utf-8"); os.replace(tmp, self.folder / POSITIONS)
        except OSError:
            pass  # a full disk: positions stay in memory and are saved on a later look
