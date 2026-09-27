"""
tarnlight install / uninstall. Install makes the drop box folder,
~/.tarnlight/inbox. Callers that find it leave a copy of each call there after the call. Uninstall removes it, and callers stop at once. Nothing else on the machine changes: no setting, no environment
variable, no background program. A TypeSafe call never depends on Tarnlight.
"""
import json, os, shutil, sys
from pathlib import Path
from .inbox import POSITIONS
from .proxy import PORT

VAR = "TYPESAFE_BASE_URL"
INBOX = Path.home() / ".tarnlight" / "inbox"
SETTINGS = Path.home() / ".claude" / "settings.json"
PROXY = f"127.0.0.1:{PORT}"


def install(inbox=INBOX, say=print):
    existed = inbox.is_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    say(f"Drop box {'is already there' if existed else 'made'}: {inbox}")
    say("Callers that use the drop box now leave a copy of each call there; open Tarnlight any time to see them, "
        "including the ones made while it was closed. Closing Tarnlight never affects Jev.")
    for where, url in base_urls():
        if PROXY in url or "localhost:" in url:
            say(f"Note: {VAR} in {where} still points at {url}. Calls that use it fail while Tarnlight is closed; remove it there.")


def uninstall(inbox=INBOX, say=print):
    if not inbox.is_dir():
        say(f"Nothing to undo: there is no drop box at {inbox}."); return
    try:
        done = json.loads((inbox / POSITIONS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    done = done if isinstance(done, dict) else {}
    unread = sum(1 for f in inbox.glob("*.jsonl") if done.get(f"{f.name}:{f.stat().st_ino}", 0) != f.stat().st_size)
    shutil.rmtree(inbox)
    say(f"Drop box removed: {inbox}" + (f" (with {unread} file{'s' if unread != 1 else ''} the console had not imported yet)." if unread else "."))
    say("Callers stop leaving copies at once, until Tarnlight is opened again: it turns the drop box back on. "
        "Nothing else was changed.")


def base_urls(settings=None):
    """Where TYPESAFE_BASE_URL is set on this machine, as (where, value): this process, the Windows user, Claude Code's
    settings. Read only."""
    settings = settings or SETTINGS
    found = []
    if os.environ.get(VAR): found.append(("this program's environment", os.environ[VAR]))
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: found.append(("your Windows user environment", winreg.QueryValueEx(k, VAR)[0]))
        except OSError:
            pass
    try:
        block = json.loads(settings.read_text(encoding="utf-8-sig") or "{}").get("env")
        if isinstance(block, dict) and isinstance(block.get(VAR), str): found.append((str(settings), block[VAR]))
    except (OSError, ValueError, AttributeError):
        pass
    return found


def main(argv):
    """The CLI entry for `install` and `uninstall`."""
    try:
        install() if argv == "install" else uninstall()
    except OSError as e:
        print(f"Could not change {INBOX}: {e.strerror or e}", file=sys.stderr); return 1
    return 0
