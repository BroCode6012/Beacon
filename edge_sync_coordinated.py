"""Coordinated cloud sync — the PERMANENT fix for laptop-scraper vs cloud-import contention.

SQLite allows only ONE writer at a time. The local radar worker (organization) and the cloud
import both write the one canonical index, and under load they starve each other:
"database is locked" failures + a WAL that bloats to gigabytes, so the import crawls.

This runner makes the two writers TAKE TURNS instead of fighting:
  1. Pause the local scraper (persistent pause marker; the auto-start keep-alive respects it).
  2. Wait for the worker to release the database (force-kill only if it won't exit — WAL-safe).
  3. Run the EXISTING hidden sync wrapper unchanged, so all of the edge sync's behavior is
     preserved (OCI SSH-allowlist IP refresh, signed transport, catch-up rounds, plan publish).
     With the scraper paused the import has the DB to itself — it drains fast and the WAL
     self-checkpoints back to ~0 between batches.
  4. Resume the scraper (only if WE paused it — a deliberate user Stop is respected).

Beacon-owned; it never edits the shared engine. Run hidden by the BeaconEdgeSync task:
    D:\\Youtube\\Beacon\\.venv\\Scripts\\pythonw.exe D:\\Youtube\\Beacon\\edge_sync_coordinated.py

EXIT CODES ARE REAL (added 2026-09-11). 0 = a sync round actually completed; non-zero = it did
not, so the BeaconEdgeSync task goes red. This matters more than it sounds: the wrapper .vbs was
deleted from disk on ~2026-09-01 and the old runner swallowed the launch failure and returned 0
regardless, so the task reported success every two minutes for ten days while importing nothing
and channel discovery sat at zero. A green task must mean data moved. Reasons are appended to
storage/edge_sync_coordinated.log, because a hidden task has no console to print to.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import engine_link  # noqa: F401 — sets the shared-engine path + DB env before beacon_engine imports
import sweep_control

VBS = sweep_control.ENGINE_ROOT / "scripts" / "run_edge_sync_hidden.vbs"
DB = os.environ["YOUTUBE_INDEX_DB"]
# A hidden task has no console, so a failure printed to stdout goes nowhere. Everything this
# runner decides is appended here — it is the only place an operator can see WHY a sync round
# did nothing. (Task Scheduler shows a code; this shows the reason.)
LOG_FILE = os.path.join(os.path.dirname(DB), "edge_sync_coordinated.log")
# Who paused the scraper. Lives beside the radar's own flags, because it describes the same
# pause they do and must be found by any later run of this script, not just this process.
OWNER_PATH = sweep_control.STORAGE / "edge_sync_pause_owner.json"
# Same arguments the BeaconEdgeSync task passed to the hidden wrapper before this runner
# wrapped it (max bundles / catch-up minutes / catch-up rounds).
SYNC_ARGS = ["100", "90", "100"]
WORKER_EXIT_WAIT_S = 45          # give the worker a chance to exit its current step cleanly
SYNC_TIMEOUT_S = 45 * 60         # hard cap so a wedged sync can't keep the scraper paused forever
# Only pause the scraper (worth the restart cost) when a real backlog has built up. Small
# routine imports just run alongside — the importer's busy_timeout waits out brief contention.
# This is adaptive + self-correcting: if small imports fall behind, the backlog crosses the
# threshold and the next run does a clean pause-and-drain. No fixed schedule change needed.
PAUSE_BACKLOG_THRESHOLD = 40


def _pending_backlog() -> int:
    """Bundles waiting to import: Oracle's remote_pending + what's already in the local inbox."""
    total = 0
    base = sweep_control.ENGINE_ROOT / "storage" / "edge_sync"
    try:
        import json
        t = json.loads((base / "transport_status.json").read_text(encoding="utf-8"))
        total += max(0, int(t.get("remote_pending") or 0))
    except Exception:  # noqa: BLE001
        pass
    try:
        total += len(list((base / "inbox").glob("*.gz")))
    except Exception:  # noqa: BLE001
        pass
    return total


# Windows idle-sleep suppression. ES_SYSTEM_REQUIRED alone keeps the MACHINE awake while
# leaving the DISPLAY free to switch off — the laptop sits dark on the desk and still imports.
# ES_DISPLAY_REQUIRED is deliberately NOT set: nobody wants a screen burning all night.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def _keep_awake():
    """Ask Windows not to fall asleep while the import runs.

    This is the other half of the 2026-09-13 failure. Pause recovery heals a sync that got
    cut off; this stops most of those cuts happening at all — the laptop idling into sleep a
    minute into a 100-bundle import.

    IMPORTANT, and the reason this is not a complete fix: it only blocks IDLE sleep. Closing
    the lid, choosing Sleep, or the battery dying still stops the machine immediately, because
    Windows always honours an explicit instruction from the person using it — and overriding
    that would mean editing the operator's power settings, which is theirs to decide, not a
    background task's. So pause recovery stays essential.

    The flag is per-process and released automatically when this process exits, so a killed
    sync can never leave the machine permanently unable to sleep.
    """
    reset = None
    if sys.platform == "win32":
        try:
            import ctypes
            set_state = ctypes.windll.kernel32.SetThreadExecutionState
            set_state.argtypes = [ctypes.c_uint]
            set_state.restype = ctypes.c_uint
            if set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
                reset = set_state          # non-zero = the request was accepted
            else:
                _log("could not ask Windows to stay awake — the import may be cut off by sleep")
        except Exception as exc:  # noqa: BLE001 — never fail a sync over a power hint
            _log(f"keep-awake unavailable: {type(exc).__name__}: {exc}")
    try:
        yield reset is not None
    finally:
        if reset is not None:
            try:
                reset(_ES_CONTINUOUS)      # back to normal: the machine may sleep again
            except Exception:  # noqa: BLE001
                pass


def _claim_pause() -> None:
    """Record that WE paused the scraper, and which process did it.

    `stop_worker` writes a PERSISTENT "paused" marker that only an explicit Start clears —
    by design, so the keep-alive can never override a real Stop. The cost is that a pause
    WE created is indistinguishable from one the operator created, so if this process dies
    between pausing and resuming (laptop sleeps, machine shuts down), every later run reads
    "already stopped", assumes the operator meant it, and walks past forever. That happened
    on 2026-09-13: the laptop slept a minute into a sync and local scraping stayed off for
    22.5 hours. This file is the missing piece of evidence — an owner and a heartbeat.
    """
    try:
        OWNER_PATH.write_text(
            json.dumps({"pid": os.getpid(), "at": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8")
    except OSError:
        pass        # best-effort: never block the sync over bookkeeping


def _release_pause() -> None:
    """Drop the ownership record — the pause it described is over."""
    try:
        OWNER_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _recover_orphaned_pause() -> bool:
    """Resume a scraper that WE paused and then never un-paused. Returns True if resumed.

    Acts when our ownership file exists AND the process named in it is gone. A pause with no
    ownership file belongs to the operator and is left strictly alone — that distinction is
    the whole point.

    Deliberately does NOT require the worker to be fully stopped first. It exits only after
    finishing its current step, which can take minutes; gating on that would skip recovery
    during the shutdown window and leave the pause marker in place for another two-minute
    round. start_worker is a no-op when the worker is alive, so clearing a stale claim early
    is safe and strictly better than clearing it late.
    """
    try:
        raw = OWNER_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False                      # nobody claimed this pause → not ours to undo
    owner: dict = {}
    pid = 0
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            owner = loaded
            pid = int(owner.get("pid") or 0)
    except (ValueError, TypeError):
        pass        # a half-written claim still means OUR pause — recover it anyway
    # Our own still-running sibling holds it (task overlap) — leave that one alone.
    if pid and pid != os.getpid() and sweep_control._pid_alive(pid):
        return False
    _log(f"resuming the scraper — it was left paused by a sync that died "
         f"(owner pid {pid or '?'}, claimed {owner.get('at') or '?'})")
    try:
        sweep_control.start_worker()      # clear_stop=True clears both stop + pause markers
    except Exception as exc:  # noqa: BLE001
        _log(f"could not resume the scraper: {type(exc).__name__}: {exc}")
        return False
    _release_pause()
    return True


def _force_kill_worker() -> None:
    """Force-stop the radar worker if it won't exit in time. Safe: WAL rolls back its open
    transaction; its organization work is redone on the next start."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -like '*radar-worker*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=30)
        for pid in out.stdout.split():
            if pid.strip().isdigit():
                subprocess.run(["taskkill", "/PID", pid.strip(), "/F"], capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    """Append one timestamped line. Never raises: logging must not break a sync round."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _run_sync() -> int:
    """Run the hidden sync wrapper. Returns its exit code; non-zero means the sync FAILED.

    This used to swallow everything and tell the caller nothing, which is how a broken cloud
    lane stayed invisible for ten days: the wrapper .vbs had been deleted from disk, wscript
    could not start it, and the task still reported success every two minutes while importing
    nothing. A missing wrapper is now a hard, loud failure — the one condition that must never
    look like a quiet "nothing to do".
    """
    if not VBS.exists():
        _log(f"FAILED: sync wrapper is missing: {VBS}")
        _log("  restore it with: git -C "
             f"{sweep_control.ENGINE_ROOT} checkout -- scripts/run_edge_sync_hidden.vbs")
        return 2
    try:
        proc = subprocess.run(
            ["wscript", "//B", "//NoLogo", str(VBS), DB, *SYNC_ARGS],
            cwd=str(sweep_control.ENGINE_ROOT), timeout=SYNC_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — report it, but still let main() resume the scraper
        _log(f"FAILED: {type(exc).__name__}: {exc}")
        return 3
    if proc.returncode != 0:
        _log(f"FAILED: sync wrapper exited {proc.returncode}")
        return proc.returncode
    _log("sync round completed")
    return 0


def main() -> int:
    # Before anything else: if a previous run of THIS script paused the scraper and never
    # got to resume it, put it back. Checked every run, so recovery happens on the next
    # two-minute tick rather than waiting for a human to notice local scraping is off.
    try:
        _recover_orphaned_pause()
    except Exception as exc:  # noqa: BLE001 — recovery must never block the sync itself
        _log(f"pause recovery failed: {type(exc).__name__}: {exc}")

    try:
        was_running = bool(sweep_control.is_running())
    except Exception:  # noqa: BLE001
        was_running = False

    # Hold off idle sleep for the WHOLE window, not just the import: the scraper is paused
    # from here to the resume below, and that gap is precisely what must not be interrupted.
    with _keep_awake():
        # Small routine import: run alongside the scraper (busy_timeout handles brief
        # contention). Only a real backlog is worth pausing + restarting the scraper for.
        if was_running and _pending_backlog() < PAUSE_BACKLOG_THRESHOLD:
            return _run_sync()

        if was_running:
            # Pause the scraper so the import gets the database to itself. stop_worker writes
            # the persistent pause marker, so the BeaconRadarAutostart keep-alive won't revive
            # it mid-sync. Claim it FIRST: if this process dies between the two calls, the
            # pause must already carry our name, or the next run cannot tell it apart from an
            # operator's Stop.
            _claim_pause()
            try:
                sweep_control.stop_worker()
            except Exception:  # noqa: BLE001
                pass
            exited = False
            for _ in range(WORKER_EXIT_WAIT_S):
                try:
                    if not sweep_control.is_running():
                        exited = True
                        break
                except Exception:  # noqa: BLE001
                    exited = True
                    break
                time.sleep(1)
            if not exited:
                _force_kill_worker()

        try:
            return _run_sync()
        finally:
            # ALWAYS resume the scraper if we paused it (start_worker clears the pause marker).
            # If it wasn't running when we started (a deliberate user Stop), leave it paused.
            # This stays in `finally` so a failing sync can never strand the scraper paused —
            # the failure is reported through the exit code, not by skipping the resume.
            if was_running:
                try:
                    sweep_control.start_worker()
                    _release_pause()      # only after the resume actually succeeded
                except Exception:  # noqa: BLE001
                    pass                  # marker stays → the next run recovers it


if __name__ == "__main__":
    sys.exit(main())
