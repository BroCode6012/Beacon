"""Read-only export of bounded organization jobs from Signal's canonical index."""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from beacon_engine.edge.organization import (
    MAX_JOB_CHANNELS,
    OrganizationSettings,
    build_organization_job,
)
from beacon_engine.edge.protocol import utc_now, write_bundle

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class OrganizationBackfillReport:
    batch_id: str
    channels: int
    videos: int
    jobs: tuple[Path, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "channels": self.channels,
            "videos": self.videos,
            "jobs": len(self.jobs),
            "paths": [str(path) for path in self.jobs],
        }


def _bounded_int(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return number


def _connection(db_path: str | os.PathLike) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"canonical index does not exist: {path}")
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _candidate_channels(
    conn: sqlite3.Connection,
    *,
    limit: int,
    min_subscribers: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH candidates AS (
            SELECT
                c.channel_id,
                c.title,
                c.handle,
                c.subscribers,
                c.video_count,
                c.view_count,
                c.description,
                c.url,
                c.channel_published_at,
                c.source,
                c.category,
                c.topic,
                c.last_seen_at,
                CASE
                    WHEN catalog.entity_id IS NULL
                      OR catalog.last_status != 'ok'
                    THEN 1 ELSE 0
                END AS needs_catalog,
                CASE WHEN face.channel_id IS NULL THEN 1 ELSE 0 END
                    AS needs_face,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM videos missing_category
                    WHERE missing_category.channel_id = c.channel_id
                      AND COALESCE(missing_category.category_id, '') = ''
                    LIMIT 1
                ) THEN 1 ELSE 0 END AS needs_categories
            FROM channels c
            LEFT JOIN enrichment_log catalog
              ON catalog.entity_type = 'catalog'
             AND catalog.entity_id = c.channel_id
            LEFT JOIN channel_face_check face
              ON face.channel_id = c.channel_id
            WHERE c.channel_id != ''
              AND c.subscribers >= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM channel_exclusions excluded
                  WHERE excluded.channel_id = c.channel_id
              )
        )
        SELECT *
        FROM candidates
        WHERE needs_catalog = 1
           OR needs_face = 1
           OR needs_categories = 1
        ORDER BY
            needs_catalog DESC,
            needs_face DESC,
            needs_categories DESC,
            subscribers DESC,
            last_seen_at DESC,
            channel_id
        LIMIT ?
        """,
        (min_subscribers, limit),
    ).fetchall()
    return [
        {
            "channel_id": str(row["channel_id"] or ""),
            "title": str(row["title"] or ""),
            "handle": str(row["handle"] or ""),
            "subscribers": max(0, int(row["subscribers"] or 0)),
            "video_count": max(0, int(row["video_count"] or 0)),
            "view_count": max(0, int(row["view_count"] or 0)),
            "description": str(row["description"] or ""),
            "url": str(row["url"] or ""),
            "channel_published_at": str(
                row["channel_published_at"] or ""
            ),
            "source": str(row["source"] or ""),
            "category": str(row["category"] or ""),
            "topic": str(row["topic"] or ""),
            "_needs_catalog": int(row["needs_catalog"] or 0),
            "_needs_face": int(row["needs_face"] or 0),
            "_needs_categories": int(row["needs_categories"] or 0),
        }
        for row in rows
    ]


def _seed_videos(
    conn: sqlite3.Connection,
    channel_ids: list[str],
    *,
    per_channel: int,
) -> list[dict[str, Any]]:
    if not channel_ids or per_channel <= 0:
        return []
    videos: list[dict[str, Any]] = []
    for index in range(0, len(channel_ids), 300):
        chunk = channel_ids[index:index + 300]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    v.video_id,
                    v.title,
                    v.channel,
                    v.channel_id,
                    v.views,
                    v.published_at,
                    v.published_at_normalized,
                    v.url,
                    v.duration_seconds,
                    v.description,
                    v.thumbnail_url,
                    v.source,
                    v.category_id,
                    v.first_seen_at,
                    v.last_seen_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY v.channel_id
                        ORDER BY
                            COALESCE(
                                NULLIF(v.published_at_normalized, ''),
                                NULLIF(v.published_at, ''),
                                v.last_seen_at
                            ) DESC,
                            v.views DESC,
                            v.video_id
                    ) AS row_number
                FROM videos v
                WHERE v.channel_id IN ({placeholders})
            )
            SELECT *
            FROM ranked
            WHERE row_number <= ?
            ORDER BY channel_id, row_number
            """,
            [*chunk, per_channel],
        ).fetchall()
        videos.extend(
            {
                "video_id": str(row["video_id"] or ""),
                "title": str(row["title"] or ""),
                "channel": str(row["channel"] or ""),
                "channel_id": str(row["channel_id"] or ""),
                "views": max(0, int(row["views"] or 0)),
                "published_at": str(row["published_at"] or ""),
                "published_at_normalized": str(
                    row["published_at_normalized"] or ""
                ),
                "url": str(row["url"] or ""),
                "duration_seconds": max(
                    0,
                    int(row["duration_seconds"] or 0),
                ),
                "description": str(row["description"] or ""),
                "thumbnail_url": str(row["thumbnail_url"] or ""),
                "source": str(row["source"] or ""),
                "category_id": str(row["category_id"] or ""),
                "first_seen_at": str(row["first_seen_at"] or ""),
                "last_seen_at": str(row["last_seen_at"] or ""),
            }
            for row in rows
            if row["video_id"]
        )
    return videos


def build_organization_backfill_jobs(
    db_path: str | os.PathLike,
    *,
    jobs_dir: str | os.PathLike,
    signing_key: str | bytes,
    controller_id: str = "signal-laptop",
    batch_id: str | None = None,
    limit: int = 100,
    channels_per_job: int = 100,
    seed_videos_per_channel: int = 12,
    min_subscribers: int = 700,
    settings: OrganizationSettings | Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> OrganizationBackfillReport:
    """Export selected backlog rows without opening the database for writes."""
    if not signing_key:
        raise ValueError("a signing key is required")
    channel_limit = _bounded_int(
        limit,
        name="limit",
        minimum=1,
        maximum=10_000,
    )
    per_job = _bounded_int(
        channels_per_job,
        name="channels_per_job",
        minimum=1,
        maximum=MAX_JOB_CHANNELS,
    )
    seeds = _bounded_int(
        seed_videos_per_channel,
        name="seed_videos_per_channel",
        minimum=0,
        maximum=50,
    )
    minimum_subscribers = _bounded_int(
        min_subscribers,
        name="min_subscribers",
        minimum=0,
        maximum=1_000_000_000,
    )
    captured = created_at or utc_now()
    identifier = (
        batch_id
        or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    if not _BATCH_ID_RE.fullmatch(identifier):
        raise ValueError(
            "batch_id must use only letters, numbers, dot, underscore, or hyphen"
        )

    with _connection(db_path) as conn:
        channels = _candidate_channels(
            conn,
            limit=channel_limit,
            min_subscribers=minimum_subscribers,
        )
        channel_ids = [row["channel_id"] for row in channels]
        videos = _seed_videos(
            conn,
            channel_ids,
            per_channel=seeds,
        )

    by_channel: dict[str, list[dict[str, Any]]] = {}
    for video in videos:
        by_channel.setdefault(video["channel_id"], []).append(video)
    clean_channels = [
        {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
        }
        for row in channels
    ]
    output = Path(jobs_dir)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index in range(0, len(clean_channels), per_job):
        chunk = clean_channels[index:index + per_job]
        chunk_ids = {row["channel_id"] for row in chunk}
        chunk_videos = [
            video
            for channel_id in chunk_ids
            for video in by_channel.get(channel_id, [])
        ]
        reason_counts = {
            "catalog": sum(
                int(row["_needs_catalog"])
                for row in channels[index:index + per_job]
            ),
            "face": sum(
                int(row["_needs_face"])
                for row in channels[index:index + per_job]
            ),
            "categories": sum(
                int(row["_needs_categories"])
                for row in channels[index:index + per_job]
            ),
        }
        bundle = build_organization_job(
            controller_id=controller_id,
            job_id=f"backfill-{identifier}-{(index // per_job) + 1:04d}",
            channels=chunk,
            videos=chunk_videos,
            settings=settings,
            source={
                "kind": "backfill",
                "batch_id": identifier,
                "reason_counts": reason_counts,
            },
            created_at=captured,
            signing_key=signing_key,
        )
        written.append(write_bundle(bundle, output))
    return OrganizationBackfillReport(
        batch_id=identifier,
        channels=len(clean_channels),
        videos=len(videos),
        jobs=tuple(written),
    )
