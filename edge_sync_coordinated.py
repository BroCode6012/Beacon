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

Signal-owned; it never edits the shared engine. Run hidden by the SignalEdgeSync task:
    D:\\Youtube\\Scripter\\.venv\\Scripts\\pythonw.exe D:\\Youtube\\Signal\\edge_sync_coordinated.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import engine_link  # noqa: F401 — sets the shared-engine path + DB env before scripter imports
import sweep_control

VBS = sweep_control.SCRIPTER_ROOT / "scripts" / "run_edge_sync_hidden.vbs"
DB = os.environ["YOUTUBE_INDEX_DB"]
# Same arguments the SignalEdgeSync task passed to the hidden wrapper before this runner
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
    base = sweep_control.SCRIPTER_ROOT / "storage" / "edge_sync"
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


def _run_sync() -> None:
    try:
        subprocess.run(
            ["wscript", "//B", "//NoLogo", str(VBS), DB, *SYNC_ARGS],
            cwd=str(sweep_control.SCRIPTER_ROOT), timeout=SYNC_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — never let a sync failure strand the scraper
        print(f"coordinated sync error: {type(exc).__name__}: {exc}")


def main() -> int:
    try:
        was_running = bool(sweep_control.is_running())
    except Exception:  # noqa: BLE001
        was_running = False

    # Small routine import: run alongside the scraper (busy_timeout handles brief contention).
    # Only a real backlog is worth pausing + restarting the scraper for.
    if was_running and _pending_backlog() < PAUSE_BACKLOG_THRESHOLD:
        _run_sync()
        return 0

    if was_running:
        # Pause the scraper so the import gets the database to itself. stop_worker writes the
        # persistent pause marker, so the SignalRadarAutostart keep-alive won't revive it mid-sync.
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
        _run_sync()
    finally:
        # ALWAYS resume the scraper if we paused it (start_worker clears the pause marker).
        # If it wasn't running when we started (a deliberate user Stop), leave it paused.
        if was_running:
            try:
                sweep_control.start_worker()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
