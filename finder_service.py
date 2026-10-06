"""Beacon's authenticated Finder API, running as its own process.

The FULL key grants read/write access; the READ key grants read-only access.
Only Beacon's own engine, settings and index are used. Token files are private
Beacon state. No other application is connected automatically.

This service NEVER holds a visible console window. However you start it -- the .vbs, a
shell, a scheduled task, an agent copying a command out of the docs -- it ends up running
under pythonw.exe with no console at all (see _relaunch_hidden). Any of these are fine:

    wscript.exe D:\\Youtube\\Beacon\\start_finder_hidden.vbs
    D:\\Youtube\\Beacon\\.venv\\Scripts\\pythonw.exe D:\\Youtube\\Beacon\\finder_service.py
    D:\\Youtube\\Beacon\\.venv\\Scripts\\python.exe  D:\\Youtube\\Beacon\\finder_service.py

Output goes to storage/finder_service.log, since a hidden process has no stdout to print to.
To watch it live in a console instead, opt in explicitly:

    set FINDER_CONSOLE=1 && .venv\\Scripts\\python.exe finder_service.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

HOST = os.getenv("FINDER_HOST", "127.0.0.1")
PORT = int(os.getenv("FINDER_PORT", "8765"))

# Resolved without importing the engine, because the handoff below has to happen before
# that import: it costs ~5 seconds, and the console window is on screen for every one of
# them. engine_link puts the DB in this same storage dir, so the path comes out identical.
_db = os.getenv("YOUTUBE_INDEX_DB")
LOG_FILE = os.getenv("FINDER_LOG") or os.path.join(
    os.path.dirname(_db) if _db else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "storage"),
    "finder_service.log")


def _has_console() -> bool:
    """True when there is a real console window to print into."""
    if sys.platform != "win32":
        return sys.stdout is not None
    import ctypes
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001 — no ctypes/kernel32: fall back to the stream
        return sys.stdout is not None


def _redirect_to_log() -> None:
    """Send output to the log file whenever there is no console to print into.

    Testing "is sys.stdout None?" is not enough. A hidden process is usually launched
    with its streams pointed at DEVNULL so it can't hold its parent shell's pipe open
    (see _relaunch_hidden) -- and then sys.stdout is a perfectly real stream that throws
    everything away. Ask the OS whether a console window exists instead, and take over
    the streams rather than only filling in missing ones.
    """
    if _has_console() and sys.stdout is not None and sys.stderr is not None:
        return
    try:
        fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    except OSError:  # read-only disk / bad path -- silence beats crashing on boot
        return
    sys.stdout = fh
    sys.stderr = fh
    print(f"\n--- FINDER start {datetime.now():%Y-%m-%d %H:%M:%S} ---")


def _already_running() -> bool:
    """True if a finder is already answering on this port.

    Not a nicety: Windows' SO_REUSEADDR lets a second bind *steal* a live listening
    socket instead of failing, so a double launch does not raise "address in use" --
    it quietly hijacks the port and leaves two rival services running. Knocking on the
    door first is the only reliable check here. Any HTTP answer counts, including a
    401: somebody is home, and that is the whole question.
    """
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 — refused/timeout/DNS all mean "nobody home"
        return False


def _relaunch_hidden() -> bool:
    """If we were started with a console python, hand the service to pythonw.exe and
    let this process die -- taking its console window with it.

    This is the belt to the .vbs launcher's braces. The visible command is printed in
    every doc and README about this service, so sooner or later somebody (or some agent)
    runs it that way, and Windows 11 hands the resulting console to Windows Terminal:
    a black window parked on the desktop for as long as the service lives. Rather than
    police how it gets started, the service refuses to be the thing holding that window.

    Returns True when a hidden replacement is running and the caller should exit.
    Set FINDER_CONSOLE=1 to opt out and watch it in the foreground.
    """
    if os.getenv("FINDER_CONSOLE") == "1":
        return False
    exe = sys.executable or ""
    if os.path.basename(exe).lower() == "pythonw.exe":
        return False                       # already console-less; nothing to do
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pythonw):
        return False                       # no hidden interpreter to hand off to
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)   # outlive this shell
    # DEVNULL on all three matters: a detached child that inherits the launching shell's
    # stdout pipe holds it open forever, so the shell that started us would hang instead
    # of returning. The child still logs -- _redirect_to_log keys off "no console window",
    # not off the stream being None.
    child = subprocess.Popen(
        [pythonw, os.path.abspath(__file__), *sys.argv[1:]],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"FINDER relaunched hidden as PID {child.pid} (no console window).")
    print(f"  log : {LOG_FILE}")
    return True


# Decide who runs the service BEFORE the engine imports below. Those cost ~5 seconds,
# and a console launch that waited for them would sit on screen the whole time -- the
# very window this whole dance exists to avoid. Everything above is stdlib only.
if __name__ == "__main__":
    _redirect_to_log()
    if _already_running():
        print(f"FINDER already up on http://{HOST}:{PORT} -- this launch steps aside.")
        raise SystemExit(0)
    if _relaunch_hidden():
        raise SystemExit(0)

import engine_link  # noqa: E402,F401 — initializes Beacon engine and YOUTUBE_INDEX_DB

from beacon_engine.intelligence.local_api import create_server  # noqa: E402

DB = os.environ["YOUTUBE_INDEX_DB"]                       # set by engine_link
FULL_TOKEN = os.getenv("FINDER_TOKEN", "signal-full")     # Beacon uses this
READ_TOKEN = os.getenv("FINDER_READ_TOKEN", "lab-read")   # read-only clients
# Private token file for authorized Beacon clients
TOKEN_FILE = os.getenv("FINDER_TOKEN_FILE") or os.path.join(
    os.path.dirname(DB), "finder.tokens.json")


def _publish_tokens() -> None:
    import json
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump({"url": f"http://{HOST}:{PORT}",
                       "full": FULL_TOKEN, "read": READ_TOKEN}, fh)
    except Exception:  # noqa: BLE001 — best effort; apps can also read env
        pass


# Another program can hold the database for minutes. Measured 2026-09-19: folding a
# 1.8 GB write-ahead log back into the index locked every newcomer out from ~15:20:45 to
# ~15:23 — the cloud import and this service both died on "database is locked", and Beacon's
# window crashed with them. That is housekeeping, not a fault, so wait it out.
DB_WAIT_SECONDS = int(os.getenv("FINDER_DB_WAIT_SECONDS", "600"))
DB_RETRY_SECONDS = 5
# Beacon reads this line in the log to tell "waiting for the database" from "stuck".
WAITING_MARK = "FINDER waiting for the database"


def db_busy(exc: BaseException) -> bool:
    """SQLite's "another connection holds the database" errors: worth waiting out."""
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text


def main() -> None:
    _publish_tokens()
    deadline = time.time() + DB_WAIT_SECONDS
    while True:
        try:
            server = create_server(host=HOST, port=PORT, db_path=DB,
                                   token=FULL_TOKEN, read_token=READ_TOKEN)
            break
        except OSError as ex:
            # Almost always "address already in use": a service is up, so this one is a
            # duplicate launch and simply steps aside instead of dying with a traceback.
            print(f"FINDER not started -- port {PORT} unavailable: {ex}")
            return
        except Exception as ex:  # noqa: BLE001 — only a busy database is retried
            if not db_busy(ex) or time.time() >= deadline:
                raise
            print(f"{WAITING_MARK} -- {ex}. Another program (the background scraper or "
                  f"the cloud import) is holding it; retrying in {DB_RETRY_SECONDS}s.",
                  flush=True)
            time.sleep(DB_RETRY_SECONDS)
    print(f"FINDER service up on http://{HOST}:{PORT}")
    print(f"  DB     : {DB}")
    print(f"  keys   : full=<{len(FULL_TOKEN)} chars>  read=<{len(READ_TOKEN)} chars>")
    print(f"  tokens : {TOKEN_FILE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
