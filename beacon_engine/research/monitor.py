"""Live pipeline health monitor.

Runs in a loop until a stop file appears. Every ``interval`` seconds it:

* Snapshots the pipeline (channels, face-checkable, faceless, uncertain queue)
  so progress -- or a stall -- is visible live.
* Appends a heartbeat line to ``storage/monitor.log`` and writes the latest
  snapshot to ``storage/monitor_status.json`` for any UI to read.

This used to double as a GPU cost guard: VLM scoring rented a Lightning AI GPU,
and a run hard-killed mid-flight left it billing, so the monitor watched for a
running GPU with no managing process and force-stopped it. Scoring now goes
through the app's own vision deployment, so there is no rented machine to leak
and nothing left to guard -- only the pipeline heartbeat remains.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from beacon_engine.logging_util import get_logger

log = get_logger()
Line = Callable[[str], None]

DEFAULT_LOG_PATH = Path("storage") / "monitor.log"
DEFAULT_STATUS_PATH = Path("storage") / "monitor_status.json"
DEFAULT_STOP_PATH = Path("storage") / "monitor.stop"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


@dataclass
class Snapshot:
    ts: str = ""
    channels: int = 0
    face_checkable: int = 0
    face_checked: int = 0
    faceless: int = 0
    uncertain_queue: int = 0
    videos: int = 0
    alerts: list[str] = field(default_factory=list)


def _pipeline_snapshot(store, snap: Snapshot) -> None:
    with store.connect() as conn:
        def one(sql: str) -> int:
            try:
                return int(conn.execute(sql).fetchone()[0] or 0)
            except Exception:  # noqa: BLE001
                return 0
        snap.channels = one("SELECT COUNT(*) FROM channels")
        snap.videos = one("SELECT COUNT(*) FROM videos")
        snap.face_checked = one("SELECT COUNT(*) FROM channel_face_check")
        snap.face_checkable = one(
            "SELECT COUNT(*) FROM (SELECT channel_id FROM videos "
            "WHERE COALESCE(thumbnail_url,'')!='' AND channel_id!='' "
            "GROUP BY channel_id HAVING COUNT(*)>=3)"
        )
    try:
        snap.uncertain_queue = store.count_uncertain_for_vlm()
    except Exception:  # noqa: BLE001
        snap.uncertain_queue = 0
    try:
        snap.faceless = sum(1 for r in store.channel_metrics(limit=100000)
                            if r.get("faceless"))
    except Exception:  # noqa: BLE001
        snap.faceless = 0


def run_monitor(
    db_path: str | None = None,
    *,
    interval: int = 60,
    once: bool = False,
    log_path: Path = DEFAULT_LOG_PATH,
    status_path: Path = DEFAULT_STATUS_PATH,
    stop_path: Path = DEFAULT_STOP_PATH,
    on_line: Line = lambda _m: None,
) -> None:
    from beacon_engine.research.store import YouTubeStore, resolve_index_path

    db_path = str(db_path or resolve_index_path())
    store = YouTubeStore(db_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    last_videos = -1
    stall_ticks = 0

    def emit(msg: str) -> None:
        line = f"[{_now()}] {msg}"
        on_line(line)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    emit(f"monitor start: interval={interval}s db={db_path}")
    while True:
        snap = Snapshot(ts=_now())
        _pipeline_snapshot(store, snap)

        # Stall detection: videos count flat for many ticks while we expect work.
        if snap.videos == last_videos:
            stall_ticks += 1
        else:
            stall_ticks = 0
            last_videos = snap.videos

        # Pipeline-idle hint: a big queue with nothing working through it.
        if snap.uncertain_queue >= 1000:
            snap.alerts.append(
                f"NOTE: {snap.uncertain_queue:,} channels waiting on VLM scoring — "
                "run `cli vlm-remote` or let a radar cycle work the queue")

        status_path.write_text(json.dumps(asdict(snap), ensure_ascii=False, indent=2),
                               encoding="utf-8")
        emit(f"ch={snap.channels:,} "
             f"vids={snap.videos:,} checkable={snap.face_checkable:,} "
             f"checked={snap.face_checked:,} faceless={snap.faceless:,} "
             f"queue={snap.uncertain_queue:,}")

        if once:
            return
        if stop_path.exists():
            emit("monitor stop requested — exiting")
            stop_path.unlink(missing_ok=True)
            return
        time.sleep(max(5, interval))
