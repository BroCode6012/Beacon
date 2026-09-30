"""The index's size, counted once and shared — so asking for it is instant everywhere.

Counting every row of the library takes ~13 s on the laptop but ~6 minutes on the server's
slower disk (measured 2026-09-29: 364 s), so the Overview's "Index snapshot" sat on "Loading
index stats…" for minutes, and each open screen and Muse's door ran their own count.

Now there is one snapshot file, storage/index_stats.json, written by whoever counts:

  * `read()` returns the last count instantly (with when it was taken) — callers show it at once;
  * `refresh_in_background(count)` recounts when the snapshot is older than REFRESH_SECONDS,
    in a background thread, and a lock file makes sure only ONE count runs at a time across the
    screens and the door (a stale lock from a killed process expires after LOCK_STALE_SECONDS);
  * `ensure(count)` = the snapshot if there is one (refreshing it quietly when old), else one
    count right now (the very first time only).

The numbers change slowly (~100 new channels an hour out of 260,000), so a count up to an hour
old is honest as long as its age is shown — callers display "counted N minutes ago".
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "storage" / "index_stats.json"
LOCK = HERE / "storage" / "index_stats.lock"
REFRESH_SECONDS = int(os.getenv("BEACON_INDEX_STATS_REFRESH", "3600"))
LOCK_STALE_SECONDS = 3600


def read(path: Path | None = None) -> dict | None:
    """{"stats": {...}, "counted_at": epoch, "seconds": how long the count took} or None."""
    try:
        data = json.loads(Path(path or SNAPSHOT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stats"), dict):
        return None
    return data


def age_seconds(snapshot: dict, now: float | None = None) -> float:
    return max(0.0, (now or time.time()) - float(snapshot.get("counted_at") or 0))


def counted_text(snapshot: dict, now: float | None = None) -> str:
    minutes = int(age_seconds(snapshot, now) / 60)
    if minutes < 1:
        return "counted just now"
    if minutes < 120:
        return f"counted {minutes} min ago"
    return f"counted {minutes // 60} h ago"


def write(stats: dict, seconds: float, path: Path | None = None) -> dict:
    target = Path(path or SNAPSHOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"stats": stats, "counted_at": time.time(), "seconds": round(seconds, 1)}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, default=str), encoding="utf-8")
    os.replace(tmp, target)                   # readers never see half a file
    return snapshot


def _take_lock(lock: Path) -> bool:
    """One counter at a time, across processes. A lock left by a killed process expires."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def count_now(count: Callable[[], dict], path: Path | None = None,
              lock: Path | None = None) -> dict | None:
    """Count and save. None if another process is already counting (its result will land in
    the same file) — never two counts of a 34 GB library at once."""
    lock = Path(lock or LOCK)
    if not _take_lock(lock):
        return None
    try:
        started = time.time()
        stats = count()
        if not isinstance(stats, dict):
            raise ValueError(f"the count came back as {type(stats).__name__}, not numbers")
        return write(stats, time.time() - started, path)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def refresh_in_background(count: Callable[[], dict], path: Path | None = None,
                          lock: Path | None = None, *, max_age: float | None = None,
                          on_done: Callable[[dict], None] | None = None) -> bool:
    """Start a quiet recount if the snapshot is missing or older than `max_age`. True if one
    was started. Errors are swallowed: the old count stays on screen, the next call retries."""
    snapshot = read(path)
    limit = REFRESH_SECONDS if max_age is None else max_age
    if snapshot is not None and age_seconds(snapshot) < limit:
        return False

    def work():
        try:
            fresh = count_now(count, path, lock)
        except Exception:  # noqa: BLE001
            return
        if fresh is not None and on_done is not None:
            on_done(fresh)

    threading.Thread(target=work, name="index-stats-count", daemon=True).start()
    return True


def ensure(count: Callable[[], dict], path: Path | None = None, lock: Path | None = None,
           *, on_refreshed: Callable[[dict], None] | None = None) -> dict:
    """The snapshot, instantly when there is one (refreshed quietly if old); otherwise count
    right now — the only slow case, and only the first time."""
    snapshot = read(path)
    if snapshot is not None:
        refresh_in_background(count, path, lock, on_done=on_refreshed)
        return snapshot
    fresh = count_now(count, path, lock)
    if fresh is not None:
        return fresh
    # Someone else is counting right now: wait for their result rather than counting twice.
    deadline = time.time() + LOCK_STALE_SECONDS
    while time.time() < deadline:
        time.sleep(2)
        snapshot = read(path)
        if snapshot is not None:
            return snapshot
        if not Path(lock or LOCK).exists():
            break
    started = time.time()
    return write(count(), time.time() - started, path)
