"""
Tarnlight.exe: the windowed launcher. Double-click it to open the console.

A windowed program has no console, so sys.stdout and sys.stderr are None: a print would be lost and a direct write would
crash. Here they go to a log file instead, one per run: %TEMP%\\tarnlight\\Tarnlight-<process id>.log, so a second copy
of the app never touches the first one's log. If the app stops with an error or a nonzero exit code, a message box shows
the end of that log. A run that ends cleanly with nothing logged deletes its log; of the rest, the newest few are kept.
"""
import os
import sys
import tempfile
import traceback

LOG_DIR = os.path.join(tempfile.gettempdir(), "tarnlight")
LOG = os.path.join(LOG_DIR, f"Tarnlight-{os.getpid()}.log")
KEEP = 5  # logs of earlier runs to keep, newest first, so the last crash can still be read


def tidy():
    """Delete all but the newest few logs of earlier runs. Windows refuses to delete a file another program has open,
    so a copy of Tarnlight that is still running keeps its log."""
    try:
        old = sorted((e for e in os.scandir(LOG_DIR) if e.name.startswith("Tarnlight") and e.name.endswith(".log")),
                     key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return
    for entry in old[KEEP:]:
        try:
            os.remove(entry.path)
        except OSError:
            pass


def open_log():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tidy()
        return open(LOG, "w", encoding="utf-8", errors="backslashreplace", buffering=1)
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


def tail(limit=1500):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:].strip()
    except OSError:
        return ""


def tell(text):
    """A plain Windows message box: it needs no Qt, so it works even when Qt is what failed to load."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Tarnlight", 0x10)  # 0x10: the error icon
    except Exception:
        pass


def drop_empty_log():
    """After a clean run: if nothing was logged, close the log and delete it. Output after this is dropped, as it
    would be without the launcher."""
    if log.tell(): return
    try:
        faulthandler.disable()
    except Exception:
        pass
    if sys.stdout is log: sys.stdout = None
    if sys.stderr is log: sys.stderr = None
    log.close()
    try:
        os.remove(LOG)
    except OSError:
        pass


log = open_log()
if sys.stdout is None:
    sys.stdout = log
if sys.stderr is None:
    sys.stderr = log
try:
    import faulthandler
    faulthandler.enable(log)  # a hard crash inside Qt still leaves a trace in the log
except Exception:
    pass

try:
    from tarnlight.__main__ import main
    code = main()
except SystemExit as e:  # for example argparse on a bad argument; its message went to the log
    code = e.code
except BaseException:
    traceback.print_exc(file=log)
    code = 1
if isinstance(code, str):
    print(code, file=log)
    code = 1
if code in (0, None):
    drop_empty_log()
else:
    log.flush()
    tell(f"Tarnlight stopped.\n\n{tail()}\n\nThe full log is {LOG}")
sys.exit(code)
