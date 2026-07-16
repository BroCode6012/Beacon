"""Ensure the background radar scraper is running — idempotent, safe to call repeatedly.

Used by the "auto-start on login" scheduled task (SignalRadarAutostart): it fires at logon
AND every 15 minutes, calling sweep_control.start_worker(), which is a no-op when a worker is
already alive. So the scraper comes up when you log in and is quietly restarted within ~15 min
if it ever dies — and it never runs two workers (the worker self-guards via its pid file, and
start_worker checks is_running first).

Run manually to start it right now:
    D:\\Youtube\\Scripter\\.venv\\Scripts\\pythonw.exe D:\\Youtube\\Signal\\scraper_autostart.py
"""
from __future__ import annotations

import sys

import engine_link  # noqa: F401  — sets the shared-engine path + DB env before scripter imports
import sweep_control


def main() -> int:
    try:
        launched = sweep_control.start_worker()
    except Exception as exc:  # noqa: BLE001
        print(f"autostart error: {type(exc).__name__}: {exc}")
        return 1
    print("started the scraper" if launched else "scraper already running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
