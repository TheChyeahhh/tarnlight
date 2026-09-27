"""
Check a built app before it is released:

    python packaging\\smoke.py [dist\\Tarnlight]

1. tarnlight-cli.exe --help works.
2. Tarnlight.exe opens the console and stays up, and records sent to it with `tarnlight-cli.exe replay` end up in its
   session log. (Staying up alone proves little: a failed start also stays up, showing its error box.)

The app runs with a throwaway home and temp folder and Qt's offscreen platform, so no window opens and nothing is left
behind.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RECORDS = 50


def record(i):
    return {"v": 1, "ts": time.time() + i * 0.01, "source": "smoke", "status": 200, "latency_ms": 100,
            "request": {"state": {"tick": i}, "model": "jev-latest", "questions": {"go": {"type": "noul", "instructions": "Go?"}}},
            "response": {"model": "jev-1.13.0", "answers": {"go": {"type": "noul", "noul": 0.9}},
                         "usage": {"input_tokens": 300, "output_tokens": 0}},
            "error": None}


def stored(home):
    logs = list((home / ".tarnlight" / "sessions").glob("*.jevlog"))
    if len(logs) != 1: return 0
    try:
        con = sqlite3.connect(logs[0])
        try: return con.execute("SELECT count(*) FROM call").fetchone()[0]
        finally: con.close()
    except sqlite3.Error:
        return 0


def main():
    app = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "dist" / "Tarnlight"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = Path(tmp)
        env = {k: v for k, v in os.environ.items() if k not in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL")}
        env.update(USERPROFILE=tmp, HOME=tmp, TEMP=tmp, TMP=tmp, QT_QPA_PLATFORM="offscreen")

        def cli(*args):
            return subprocess.run([app / "tarnlight-cli.exe", *args], env=env, capture_output=True, text=True, timeout=60)

        r = cli("--help")
        if r.returncode or "usage: tarnlight" not in r.stdout: sys.exit(f"tarnlight-cli.exe --help failed:\n{r.stdout}{r.stderr}")
        print("tarnlight-cli.exe --help: ok")

        (home / "records.jsonl").write_text("".join(json.dumps(record(i)) + "\n" for i in range(RECORDS)))
        gui = subprocess.Popen([app / "Tarnlight.exe", "--port", "17391", "--proxy-port", "17392"], env=env)
        try:
            time.sleep(8)
            if gui.poll() is not None: sys.exit(f"Tarnlight.exe stopped with exit code {gui.returncode}")
            r = cli("replay", str(home / "records.jsonl"), "--speed", "100", "--port", "17391")
            if r.returncode: sys.exit(f"tarnlight-cli.exe replay failed:\n{r.stdout}{r.stderr}")
            deadline = time.monotonic() + 30
            while stored(home) < RECORDS and time.monotonic() < deadline: time.sleep(0.5)
            count = stored(home)
            if count < RECORDS:
                log = home / "tarnlight" / f"Tarnlight-{gui.pid}.log"
                sys.exit(f"Tarnlight.exe stored {count} of {RECORDS} records. Its log:\n"
                         + (log.read_text("utf-8", "replace") if log.exists() else "(none)"))
            if gui.poll() is not None: sys.exit(f"Tarnlight.exe stopped with exit code {gui.returncode}")
            print(f"Tarnlight.exe: stayed up and stored all {RECORDS} replayed records")
        finally:
            gui.kill()
            gui.wait()


if __name__ == "__main__":
    main()
