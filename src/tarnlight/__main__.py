"""
python -m tarnlight                                   open the console: a new session log, the drop box, the UDP listener,
                                                      the proxy (for one app at a time, never machine-wide), the window
python -m tarnlight replay FILE [--speed 10]          send a file's records (an export, or a capture file) to it
python -m tarnlight install | uninstall               make or remove the drop box, ~/.tarnlight/inbox
python -m tarnlight demo [--speed 5]                  the window on made-up data: no calls to TypeSafe, nothing kept
"""
import argparse, contextlib, datetime, shutil, sys, tempfile, threading
from pathlib import Path
from .demo import SPEED
from .ingest import PORT

SESSIONS = Path.home() / ".tarnlight" / "sessions"


def session_path(root=SESSIONS):
    """~/.tarnlight/sessions/<date>-<n>.jevlog, a new one per launch."""
    root.mkdir(parents=True, exist_ok=True)
    day, n = datetime.date.today().isoformat(), 1
    while (root / f"{day}-{n:02d}.jevlog").exists(): n += 1
    return root / f"{day}-{n:02d}.jevlog"


def configured_urls():
    """Every TYPESAFE_BASE_URL a program on this machine could be using (read only), for the window's banner."""
    from .install import base_urls
    return list(dict.fromkeys(url for _, url in base_urls()))


def tcp_port(text):
    port = int(text)
    if not 0 < port < 65536: raise argparse.ArgumentTypeError(f"{text} is not a TCP port (1 to 65535)")
    return port


def application():
    """The Qt application with the fonts that ship in the package, for the console and the demo."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from .ui import load_fonts, SANS
    app = QApplication.instance() or QApplication(sys.argv)
    load_fonts(); font = QFont(SANS); font.setPixelSize(13); app.setFont(font)
    return app


def console(port, proxy_port):
    from .bands import Bands
    from .inbox import Inbox
    from .ingest import Ingest
    from .proxy import Proxy
    from .model import DecisionModel
    from .storage import JevLog
    from .ui import ConsoleWindow
    app = application()
    log = JevLog(session_path())
    ingest = Ingest(log, port=port).start()
    inbox = Inbox(ingest).start()  # what callers left while the console was closed, then new calls as they come
    proxy, problem = None, None
    try:
        proxy = Proxy(port=proxy_port, sink=("127.0.0.1", ingest.port)).start()  # before the window, which shows whether it started
    except RuntimeError as e:
        problem = str(e)  # the console still runs; the window says what this means
    try:
        window = ConsoleWindow(DecisionModel(bands=Bands()), ingest, log, proxy=proxy, proxy_problem=problem, base_urls=configured_urls(),
                               inbox=inbox)
        window.resize(1440, 900); window.show()
        return app.exec()
    finally:
        if proxy is not None: proxy.stop()
        inbox.stop()  # before the ingest, so what it handed over is stored
        ingest.stop(); log.compact(); log.close()


@contextlib.contextmanager
def demo_console(speed):
    """The console on made-up data (demo.py), with no calls to TypeSafe: a temporary session log and bands file, deleted
    on the way out; no drop box (it deletes the files it imports) and no proxy; the UDP ingest on a free port, fed by a
    thread. Nothing is written under ~/.tarnlight. Yields the window."""
    from .bands import Bands
    from .demo import play
    from .ingest import Ingest
    from .model import DecisionModel
    from .storage import JevLog
    from .ui import ConsoleWindow
    folder, stop = Path(tempfile.mkdtemp(prefix="tarnlight-demo-")), threading.Event()
    log = ingest = feeder = None
    try:
        log = JevLog(folder / "demo.jevlog")
        ingest = Ingest(log, port=0).start()
        window = ConsoleWindow(DecisionModel(bands=Bands(folder / "bands.json")), ingest, log, demo=True)
        feeder = threading.Thread(target=play, args=(("127.0.0.1", ingest.port), speed, stop), name="tarnlight-demo", daemon=True)
        feeder.start()
        yield window
    finally:
        stop.set()
        if feeder is not None: feeder.join()
        if ingest is not None: ingest.stop()
        if log is not None: log.close()
        shutil.rmtree(folder, ignore_errors=True)


def demo(speed):
    app = application()
    with demo_console(speed) as window:
        window.resize(1440, 900); window.show()
        return app.exec()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tarnlight", description="Watch and grade Jev decisions locally.")
    parser.add_argument("--port", type=int, default=PORT, help=f"UDP port to listen on (default {PORT})")
    parser.add_argument("--proxy-port", type=tcp_port, default=7338, help="TCP port of the TypeSafe proxy (default 7338)")
    sub = parser.add_subparsers(dest="command")
    rp = sub.add_parser("replay", help="send a JSONL file of records to a running console")
    rp.add_argument("file", type=Path)
    rp.add_argument("--speed", type=float, default=1.0, help="10 plays ten times faster than recorded")
    rp.add_argument("--port", type=int, default=PORT)
    sub.add_parser("install", help="make the drop box, ~/.tarnlight/inbox, where callers leave a copy of each call")
    sub.add_parser("uninstall", help="remove the drop box; callers stop leaving copies")
    dp = sub.add_parser("demo", help="open the window on made-up data: no calls to TypeSafe, nothing kept")
    dp.add_argument("--speed", type=float, default=SPEED, help=f"how much faster than the made-up spacing (default {SPEED})")
    args = parser.parse_args(argv)
    if args.command == "demo":
        if not args.speed > 0: dp.error("--speed must be above 0")
        return demo(args.speed)
    if args.command == "replay":
        from .replay import read_records, replay
        records = read_records(args.file)
        print(f"sending {len(records)} records to 127.0.0.1:{args.port} at {args.speed:g}x", flush=True)
        replay(records, speed=args.speed, addr=("127.0.0.1", args.port))
        return 0
    if args.command in ("install", "uninstall"):
        from .install import main as install_main
        return install_main(args.command)
    return console(args.port, args.proxy_port)


if __name__ == "__main__":
    sys.exit(main())
