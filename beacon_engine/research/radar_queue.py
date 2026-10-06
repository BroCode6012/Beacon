"""Durable operational queue for the split radar pipeline.

The main YouTube index remains the source of truth for channels and videos.
This small sidecar SQLite database stores only pending background-work batches,
so discovery can keep running without waiting for organization and an interrupted
laptop session can safely resume later.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _lease_until(now: str, seconds: int) -> str:
    stamp = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (
        stamp + dt.timedelta(seconds=max(1, int(seconds)))
    ).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RadarQueueBatch:
    first_id: int
    cutoff_id: int
    jobs: int
    channel_count: int
    worker: str


class RadarWorkQueue:
    """Coalescing, lease-based queue stored beside the shared index."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS radar_work_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    channel_count INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_radar_work_queue_pending
                    ON radar_work_queue(kind, status, id);
                """
            )

    def enqueue_organization(
        self,
        channel_count: int,
        *,
        payload: dict | None = None,
        now: str | None = None,
    ) -> int | None:
        count = max(0, int(channel_count or 0))
        if count == 0:
            return None
        stamp = now or _utc_now()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO radar_work_queue (
                    kind, status, channel_count, payload_json,
                    created_at, updated_at
                ) VALUES ('organize', 'queued', ?, ?, ?, ?)
                """,
                (count, json.dumps(payload or {}, sort_keys=True), stamp, stamp),
            )
            return int(cur.lastrowid)

    def claim_organization(
        self,
        worker: str,
        *,
        lease_seconds: int = 6 * 60 * 60,
        now: str | None = None,
    ) -> RadarQueueBatch | None:
        stamp = now or _utc_now()
        owner = str(worker or "organizer")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE radar_work_queue
                SET status='queued', lease_owner='', lease_until=NULL,
                    updated_at=?
                WHERE kind='organize'
                  AND status='running'
                  AND COALESCE(lease_until, '') <= ?
                """,
                (stamp, stamp),
            )
            row = conn.execute(
                """
                SELECT MIN(id) AS first_id, MAX(id) AS cutoff_id,
                       COUNT(*) AS jobs,
                       COALESCE(SUM(channel_count), 0) AS channel_count
                FROM radar_work_queue
                WHERE kind='organize' AND status='queued'
                """
            ).fetchone()
            if not row or int(row["jobs"] or 0) == 0:
                return None
            first_id = int(row["first_id"])
            cutoff_id = int(row["cutoff_id"])
            conn.execute(
                """
                UPDATE radar_work_queue
                SET status='running', lease_owner=?, lease_until=?,
                    attempts=attempts + 1, updated_at=?
                WHERE kind='organize' AND status='queued' AND id <= ?
                """,
                (
                    owner,
                    _lease_until(stamp, lease_seconds),
                    stamp,
                    cutoff_id,
                ),
            )
            return RadarQueueBatch(
                first_id=first_id,
                cutoff_id=cutoff_id,
                jobs=int(row["jobs"]),
                channel_count=int(row["channel_count"]),
                worker=owner,
            )

    def complete(
        self,
        batch: RadarQueueBatch,
        *,
        now: str | None = None,
    ) -> int:
        stamp = now or _utc_now()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                UPDATE radar_work_queue
                SET status='done', lease_owner='', lease_until=NULL,
                    last_error='', updated_at=?
                WHERE kind='organize' AND status='running'
                  AND lease_owner=? AND id <= ?
                """,
                (stamp, batch.worker, batch.cutoff_id),
            )
            changed = int(cur.rowcount)
        self.prune_completed()
        return changed

    def release(
        self,
        batch: RadarQueueBatch,
        error: str,
        *,
        now: str | None = None,
    ) -> int:
        stamp = now or _utc_now()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                UPDATE radar_work_queue
                SET status='queued', lease_owner='', lease_until=NULL,
                    last_error=?, updated_at=?
                WHERE kind='organize' AND status='running'
                  AND lease_owner=? AND id <= ?
                """,
                (str(error or "")[:1000], stamp, batch.worker, batch.cutoff_id),
            )
            return int(cur.rowcount)

    def stats(self) -> dict[str, int]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS batches,
                       COALESCE(SUM(channel_count), 0) AS channels
                FROM radar_work_queue
                WHERE kind='organize'
                GROUP BY status
                """
            ).fetchall()
        grouped = {
            str(row["status"]): {
                "batches": int(row["batches"]),
                "channels": int(row["channels"]),
            }
            for row in rows
        }
        queued = grouped.get("queued", {"batches": 0, "channels": 0})
        running = grouped.get("running", {"batches": 0, "channels": 0})
        return {
            "queued_batches": queued["batches"],
            "queued_channels": queued["channels"],
            "running_batches": running["batches"],
            "running_channels": running["channels"],
            "failed_batches": grouped.get("failed", {}).get("batches", 0),
            "done_batches": grouped.get("done", {}).get("batches", 0),
            "pending_batches": queued["batches"] + running["batches"],
            "pending_channels": queued["channels"] + running["channels"],
        }

    def prune_completed(self, *, keep: int = 500) -> int:
        keep_count = max(0, int(keep))
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                DELETE FROM radar_work_queue
                WHERE kind='organize' AND status='done'
                  AND id NOT IN (
                      SELECT id FROM radar_work_queue
                      WHERE kind='organize' AND status='done'
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (keep_count,),
            )
            return int(cur.rowcount)
