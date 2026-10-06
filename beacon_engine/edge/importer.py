"""Atomic, idempotent import of trusted edge-node result bundles."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from beacon_engine.edge.protocol import EdgeBundle, EdgeProtocolError, load_bundle, utc_now, write_ack
from beacon_engine.research.faceless_filter import CLASSIFIER_VERSION
from beacon_engine.research.store import YouTubeStore

DISCOVERY_BUNDLE_KIND = "youtube.discovery.v1"
ORGANIZATION_BUNDLE_KIND = "youtube.organization.v1"
DEFAULT_IMPORT_TRANSACTION_BUNDLES = 20
DEFAULT_WAL_TRUNCATE_BYTES = 128 * 1024 * 1024
ACTIVE_WAL_TRUNCATE_TIMEOUT_MS = 250
FINAL_WAL_TRUNCATE_TIMEOUT_MS = 1500
_IMPORT_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS edge_import_batches (
    bundle_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    source_path TEXT NOT NULL DEFAULT ''
)
"""
_ORGANIZATION_IMPORT_JOBS_SQL = """
CREATE TABLE IF NOT EXISTS edge_organization_import_jobs (
    source_job_revision TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL UNIQUE,
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}'
)
"""
_ORGANIZATION_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS edge_organization_runs (
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_job_revision TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL,
    catalog_attempted INTEGER NOT NULL DEFAULT 0,
    catalog_status TEXT NOT NULL DEFAULT '',
    catalog_attempts INTEGER NOT NULL DEFAULT 0,
    videos_collected INTEGER NOT NULL DEFAULT 0,
    categories_filled INTEGER NOT NULL DEFAULT 0,
    face_status TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    PRIMARY KEY (node_id, job_id, run_id, channel_id)
)
"""
_ORGANIZATION_RUNS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_edge_organization_runs_finished
ON edge_organization_runs(finished_at DESC, status)
"""
_QUERY_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS edge_query_runs (
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    query TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    new_result_count INTEGER NOT NULL DEFAULT 0,
    sort TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (node_id, job_id, run_id, query)
)
"""
_QUERY_RUNS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_edge_query_runs_finished
ON edge_query_runs(finished_at DESC, status)
"""


@dataclass(frozen=True)
class ImportReport:
    bundle_id: str
    node_id: str
    kind: str
    status: str
    counts: dict[str, int]
    ack_path: str = ""

    @property
    def duplicate(self) -> bool:
        return self.status == "duplicate"


@dataclass(frozen=True)
class BundleImportAttempt:
    path: Path
    report: ImportReport | None = None
    error: Exception | None = None


def _text(value: object) -> str:
    return str(value or "")


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _json_text(value: object) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return "{}"
    try:
        return json.dumps(
            value or {},
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        )
    except TypeError:
        return "{}"


def _raw_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _channel_raw_text(row: dict[str, Any]) -> str:
    raw = _raw_mapping(row.get("raw_json") or row.get("raw"))
    snippet = raw.get("snippet")
    if not isinstance(snippet, dict):
        return "{}"
    thumbnails = snippet.get("thumbnails")
    if not isinstance(thumbnails, dict) or not thumbnails:
        return "{}"
    return _json_text({"snippet": {"thumbnails": thumbnails}})


def _video_raw_text(row: dict[str, Any]) -> str:
    category_id = _text(row.get("category_id")).strip()
    if not category_id:
        raw = _raw_mapping(row.get("raw_json") or row.get("raw"))
        snippet = raw.get("snippet")
        if isinstance(snippet, dict):
            category_id = _text(snippet.get("categoryId")).strip()
    return (
        _json_text({"snippet": {"categoryId": category_id}})
        if category_id
        else "{}"
    )


def _require_id(row: dict[str, Any], field: str, dataset: str) -> str:
    value = _text(row.get(field)).strip()
    if not value:
        raise EdgeProtocolError(f"{dataset} row is missing {field}")
    return value


class _BatchingConnection:
    """Turn repeated identical execute calls into one SQLite executemany call."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sql = ""
        self._parameters: list[tuple[Any, ...]] = []

    @property
    def dialect(self):
        return getattr(self._conn, "dialect", "sqlite")

    def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Cursor | None:
        if not parameters:
            self.flush()
            return self._conn.execute(sql)
        if self._sql and sql != self._sql:
            self.flush()
        self._sql = sql
        self._parameters.append(tuple(parameters))
        return None

    def flush(self) -> None:
        if not self._parameters:
            return
        sql = self._sql
        parameters = self._parameters
        self._sql = ""
        self._parameters = []
        self._conn.executemany(sql, parameters)

    def discard(self) -> None:
        self._sql = ""
        self._parameters = []


def _bulk_import(
    conn: sqlite3.Connection,
    importer: Callable[..., int],
    rows: list[dict[str, Any]],
    **kwargs: Any,
) -> int:
    buffered = _BatchingConnection(conn)
    try:
        count = importer(buffered, rows, **kwargs)
        buffered.flush()
        return count
    except Exception:
        buffered.discard()
        raise


def _import_channels(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    for row in rows:
        channel_id = _require_id(row, "channel_id", "channels")
        first_seen = _text(row.get("first_seen_at")) or utc_now()
        last_seen = _text(row.get("last_seen_at")) or first_seen
        description, raw_json = _text(row.get("description")), _channel_raw_text(row)
        if getattr(conn, 'dialect', 'sqlite') == 'postgresql':
            from beacon_engine.research.postgres_text import normalize_description
            description, raw_json = normalize_description(description, raw_json)
        conn.execute(
            """
            INSERT INTO channels (
                channel_id, title, handle, subscribers, video_count, view_count,
                description, url, channel_published_at, source, raw_json,
                first_seen_at, last_seen_at, category, topic
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                title=COALESCE(NULLIF(excluded.title, ''), channels.title),
                handle=COALESCE(NULLIF(excluded.handle, ''), channels.handle),
                subscribers=CASE WHEN excluded.subscribers > 0
                    THEN excluded.subscribers ELSE channels.subscribers END,
                video_count=CASE WHEN excluded.video_count > 0
                    THEN excluded.video_count ELSE channels.video_count END,
                view_count=CASE WHEN excluded.view_count > 0
                    THEN excluded.view_count ELSE channels.view_count END,
                description=COALESCE(
                    NULLIF(excluded.description, ''), channels.description),
                url=COALESCE(NULLIF(excluded.url, ''), channels.url),
                channel_published_at=COALESCE(
                    NULLIF(excluded.channel_published_at, ''),
                    channels.channel_published_at
                ),
                source=COALESCE(NULLIF(excluded.source, ''), channels.source),
                raw_json=CASE WHEN excluded.raw_json NOT IN ('', '{}')
                    THEN excluded.raw_json ELSE channels.raw_json END,
                first_seen_at=CASE
                    WHEN channels.first_seen_at='' THEN excluded.first_seen_at
                    WHEN excluded.first_seen_at='' THEN channels.first_seen_at
                    ELSE MIN(channels.first_seen_at, excluded.first_seen_at)
                END,
                last_seen_at=CASE
                    WHEN channels.last_seen_at='' THEN excluded.last_seen_at
                    WHEN excluded.last_seen_at='' THEN channels.last_seen_at
                    ELSE MAX(channels.last_seen_at, excluded.last_seen_at)
                END,
                category=COALESCE(NULLIF(channels.category, ''), excluded.category),
                topic=COALESCE(NULLIF(channels.topic, ''), excluded.topic)
            """,
            (
                channel_id,
                _text(row.get("title")),
                _text(row.get("handle")),
                _integer(row.get("subscribers")),
                _integer(row.get("video_count")),
                _integer(row.get("view_count")),
                description,
                _text(row.get("url")),
                _text(row.get("channel_published_at") or row.get("published_at")),
                _text(row.get("source")),
                raw_json,
                first_seen,
                last_seen,
                _text(row.get("category")),
                _text(row.get("topic")),
            ),
        )
    return len(rows)


def _import_channel_records(
    conn: sqlite3.Connection, rows: list[dict[str, Any]], *,
    new_ids: set[str] | None = None,
) -> dict[str, int]:
    """Count actual inserts, including conflicts with other database writers.

    Reserve missing parent IDs in the same transaction as the normal metadata
    merge. RETURNING reports only rows this transaction inserted; a preceding
    existence check would overcount a channel saved concurrently by the user.
    Bounded batches avoid a lookup/round trip for every individual channel.
    """
    parents: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        channel_id = _require_id(row, "channel_id", "channels")
        first_seen = _text(row.get("first_seen_at")) or utc_now()
        parents.setdefault(channel_id, (
            channel_id, first_seen, _text(row.get("last_seen_at")) or first_seen,
        ))
    values = [parents[channel_id] for channel_id in sorted(parents)]
    inserted = 0
    for offset in range(0, len(values), 250):
        batch = values[offset:offset + 250]
        parameters = tuple(value for parent in batch for value in parent)
        cursor = conn.execute(
            "INSERT INTO channels (channel_id, first_seen_at, last_seen_at) VALUES "
            + ",".join("(?,?,?)" for _ in batch)
            + " ON CONFLICT(channel_id) DO NOTHING RETURNING channel_id",
            parameters,
        )
        returned = cursor.fetchall()
        inserted += len(returned)
        if new_ids is not None:
            new_ids.update(row[0] for row in returned)
    count = _bulk_import(conn, _import_channels, rows)
    return {
        "channels": count,
        "channels_new": inserted,
        "channels_existing": len(parents) - inserted,
    }


def _import_videos(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    for row in rows:
        video_id = _require_id(row, "video_id", "videos")
        first_seen = _text(row.get("first_seen_at")) or utc_now()
        last_seen = _text(row.get("last_seen_at")) or first_seen
        description, raw_json = _text(row.get("description")), _video_raw_text(row)
        if getattr(conn, 'dialect', 'sqlite') == 'postgresql':
            from beacon_engine.research.postgres_text import normalize_description
            description, raw_json = normalize_description(description, raw_json)
        conn.execute(
            """
            INSERT INTO videos (
                video_id, title, channel, channel_id, views, published_at,
                published_at_normalized, url, duration_seconds, description,
                thumbnail_url, source, raw_json, first_seen_at, last_seen_at,
                category_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title=COALESCE(NULLIF(excluded.title, ''), videos.title),
                channel=COALESCE(NULLIF(excluded.channel, ''), videos.channel),
                channel_id=COALESCE(
                    NULLIF(excluded.channel_id, ''), videos.channel_id),
                views=CASE WHEN excluded.views > 0
                    THEN excluded.views ELSE videos.views END,
                published_at=COALESCE(
                    NULLIF(excluded.published_at, ''), videos.published_at),
                published_at_normalized=COALESCE(
                    NULLIF(excluded.published_at_normalized, ''),
                    videos.published_at_normalized
                ),
                url=COALESCE(NULLIF(excluded.url, ''), videos.url),
                duration_seconds=CASE WHEN excluded.duration_seconds > 0
                    THEN excluded.duration_seconds ELSE videos.duration_seconds END,
                description=COALESCE(
                    NULLIF(excluded.description, ''), videos.description),
                thumbnail_url=COALESCE(
                    NULLIF(excluded.thumbnail_url, ''), videos.thumbnail_url),
                source=COALESCE(NULLIF(excluded.source, ''), videos.source),
                raw_json=CASE WHEN excluded.raw_json NOT IN ('', '{}')
                    THEN excluded.raw_json ELSE videos.raw_json END,
                first_seen_at=CASE
                    WHEN videos.first_seen_at='' THEN excluded.first_seen_at
                    WHEN excluded.first_seen_at='' THEN videos.first_seen_at
                    ELSE MIN(videos.first_seen_at, excluded.first_seen_at)
                END,
                last_seen_at=CASE
                    WHEN videos.last_seen_at='' THEN excluded.last_seen_at
                    WHEN excluded.last_seen_at='' THEN videos.last_seen_at
                    ELSE MAX(videos.last_seen_at, excluded.last_seen_at)
                END,
                category_id=COALESCE(
                    NULLIF(excluded.category_id, ''), videos.category_id)
            """,
            (
                video_id,
                _text(row.get("title")),
                _text(row.get("channel")),
                _text(row.get("channel_id")),
                _integer(row.get("views")),
                _text(row.get("published_at")),
                _text(row.get("published_at_normalized")),
                _text(row.get("url")),
                _integer(row.get("duration_seconds")),
                description,
                _text(row.get("thumbnail_url")),
                _text(row.get("source")),
                raw_json,
                first_seen,
                last_seen,
                _text(row.get("category_id")),
            ),
        )
    return len(rows)


def _import_channel_classifications(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    for row in rows:
        channel_id = _require_id(
            row,
            "channel_id",
            "channel_classifications",
        )
        classifier = _text(row.get("classifier")).strip() or CLASSIFIER_VERSION
        conn.execute(
            """
            INSERT INTO channel_classifications (
                channel_id, likely_faceless, confidence, score,
                sample_count, positive_title_count, negative_title_count,
                positives_json, negatives_json, classifier, classified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                likely_faceless=excluded.likely_faceless,
                confidence=excluded.confidence,
                score=excluded.score,
                sample_count=excluded.sample_count,
                positive_title_count=excluded.positive_title_count,
                negative_title_count=excluded.negative_title_count,
                positives_json=excluded.positives_json,
                negatives_json=excluded.negatives_json,
                classifier=excluded.classifier,
                classified_at=excluded.classified_at
            WHERE (
                excluded.classifier = channel_classifications.classifier
                AND (
                    excluded.sample_count > channel_classifications.sample_count
                    OR (
                        excluded.sample_count = channel_classifications.sample_count
                        AND excluded.classified_at
                            > channel_classifications.classified_at
                    )
                )
            ) OR (
                excluded.classifier = ?
                AND channel_classifications.classifier != ?
            )
            """,
            (
                channel_id,
                int(bool(_integer(row.get("likely_faceless")))),
                max(0.0, min(1.0, _number(row.get("confidence")))),
                _number(row.get("score")),
                _integer(row.get("sample_count")),
                _integer(row.get("positive_title_count")),
                _integer(row.get("negative_title_count")),
                _json_text(
                    row.get("positives")
                    if "positives" in row
                    else row.get("positives_json")
                ),
                _json_text(
                    row.get("negatives")
                    if "negatives" in row
                    else row.get("negatives_json")
                ),
                classifier,
                _text(row.get("classified_at")) or utc_now(),
                CLASSIFIER_VERSION,
                CLASSIFIER_VERSION,
            ),
        )
    return len(rows)


def _import_search_results(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    for row in rows:
        query = _require_id(row, "query", "search_results")
        provider = _require_id(row, "provider", "search_results")
        video_id = _require_id(row, "video_id", "search_results")
        conn.execute(
            """
            INSERT INTO search_results (
                query, provider, video_id, position, captured_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(query, provider, video_id) DO UPDATE SET
                position=excluded.position,
                captured_at=MAX(search_results.captured_at, excluded.captured_at)
            """,
            (
                query,
                provider,
                video_id,
                max(1, _integer(row.get("position"))),
                _text(row.get("captured_at")) or utc_now(),
            ),
        )
    return len(rows)


def _import_video_discoveries(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    for row in rows:
        conn.execute(
            """
            INSERT INTO video_discoveries (
                video_id, query, provider, discovery_kind, position,
                run_id, discovered_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                _require_id(row, "video_id", "video_discoveries"),
                _text(row.get("query")),
                _text(row.get("provider")),
                _text(row.get("discovery_kind")) or "search",
                _integer(row.get("position")),
                _text(row.get("discovered_at")) or utc_now(),
            ),
        )
    return len(rows)


def _import_video_snapshots(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    parameters: list[tuple[Any, ...]] = []
    for row in rows:
        video_id = _require_id(row, "video_id", "video_snapshots")
        views = _integer(row.get("views"))
        captured_at = _text(row.get("captured_at")) or utc_now()
        source = _text(row.get("source"))
        parameters.append(
            (
                video_id,
                views,
                captured_at,
                source,
                video_id,
                views,
                captured_at,
                source,
            )
        )
    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO video_snapshots (video_id, views, captured_at, source)
        SELECT ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM video_snapshots
            WHERE video_id=? AND views=? AND captured_at=? AND source=?
        )
        """,
        parameters,
    )
    return max(0, conn.total_changes - before)


def _import_query_runs(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    if getattr(conn, "dialect", "sqlite") == "sqlite":
        conn.execute(_QUERY_RUNS_SQL)
        conn.execute(_QUERY_RUNS_INDEX_SQL)
    for row in rows:
        conn.execute(
            """
            INSERT INTO edge_query_runs (
                node_id, job_id, run_id, query, topic, provider, status,
                result_count, new_result_count, sort, error, started_at,
                finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id, job_id, run_id, query) DO UPDATE SET
                topic=excluded.topic,
                provider=excluded.provider,
                status=excluded.status,
                result_count=excluded.result_count,
                new_result_count=excluded.new_result_count,
                sort=excluded.sort,
                error=excluded.error,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at
            """,
            (
                _require_id(row, "node_id", "query_runs"),
                _require_id(row, "job_id", "query_runs"),
                _require_id(row, "run_id", "query_runs"),
                _require_id(row, "query", "query_runs"),
                _text(row.get("topic")),
                _text(row.get("provider")),
                _require_id(row, "status", "query_runs"),
                _integer(row.get("result_count")),
                _integer(row.get("new_result_count")),
                _text(row.get("sort")),
                _text(row.get("error"))[:1000],
                _text(row.get("started_at")),
                _text(row.get("finished_at")),
            ),
        )
    return len(rows)


def _import_channel_face_checks(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    allowed_verdicts = {"faceless", "on_camera", "uncertain", "unknown"}
    for row in rows:
        channel_id = _require_id(
            row,
            "channel_id",
            "channel_face_checks",
        )
        verdict = _text(row.get("verdict")).strip()
        if verdict not in allowed_verdicts:
            verdict = "unknown"
        conn.execute(
            """
            INSERT INTO channel_face_check (
                channel_id, thumbnails_checked, faces_found, recurring_count,
                verdict, confidence, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                thumbnails_checked=excluded.thumbnails_checked,
                faces_found=excluded.faces_found,
                recurring_count=excluded.recurring_count,
                verdict=excluded.verdict,
                confidence=excluded.confidence,
                checked_at=excluded.checked_at
            WHERE
                excluded.thumbnails_checked
                    > channel_face_check.thumbnails_checked
                OR (
                    excluded.thumbnails_checked
                        = channel_face_check.thumbnails_checked
                    AND excluded.checked_at > channel_face_check.checked_at
                )
            """,
            (
                channel_id,
                _integer(row.get("thumbnails_checked")),
                _integer(row.get("faces_found")),
                _integer(row.get("recurring_count")),
                verdict,
                max(0.0, min(1.0, _number(row.get("confidence")))),
                _text(row.get("checked_at")) or utc_now(),
            ),
        )
    return len(rows)


def _import_enrichment_attempts(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    for row in rows:
        entity_type = _require_id(
            row,
            "entity_type",
            "enrichment_attempts",
        )
        entity_id = _require_id(
            row,
            "entity_id",
            "enrichment_attempts",
        )
        conn.execute(
            """
            INSERT INTO enrichment_log (
                entity_type, entity_id, attempts, last_status, last_attempt_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                attempts=enrichment_log.attempts + excluded.attempts,
                last_status=CASE
                    WHEN excluded.last_attempt_at
                        >= enrichment_log.last_attempt_at
                    THEN excluded.last_status
                    ELSE enrichment_log.last_status
                END,
                last_attempt_at=MAX(
                    enrichment_log.last_attempt_at,
                    excluded.last_attempt_at
                )
            """,
            (
                entity_type,
                entity_id,
                max(1, _integer(row.get("attempts"))),
                _text(row.get("status")) or "tried",
                _text(row.get("attempted_at")) or utc_now(),
            ),
        )
    return len(rows)


def _organization_metadata(bundle: EdgeBundle) -> tuple[str, str, str]:
    metadata = bundle.payload.get("metadata")
    if not isinstance(metadata, dict):
        raise EdgeProtocolError("organization metadata must be an object")
    source_job_revision = _text(
        metadata.get("source_job_revision")
    ).strip()
    if not source_job_revision:
        raise EdgeProtocolError(
            "organization metadata is missing source_job_revision"
        )
    job_id = _text(bundle.payload.get("job_id")).strip()
    if not job_id:
        raise EdgeProtocolError("organization result is missing job_id")
    run_id = _text(metadata.get("run_id")).strip()
    if not run_id:
        raise EdgeProtocolError("organization metadata is missing run_id")
    return source_job_revision, job_id, run_id


def _import_organization_runs(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    bundle: EdgeBundle,
) -> int:
    source_job_revision, job_id, run_id = _organization_metadata(bundle)
    if getattr(conn, "dialect", "sqlite") == "sqlite":
        conn.execute(_ORGANIZATION_RUNS_SQL)
        conn.execute(_ORGANIZATION_RUNS_INDEX_SQL)
    for row in rows:
        conn.execute(
            """
            INSERT INTO edge_organization_runs (
                node_id, job_id, run_id, source_job_revision, channel_id,
                status, catalog_attempted, catalog_status, catalog_attempts,
                videos_collected, categories_filled, face_status, error,
                started_at, finished_at, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id, job_id, run_id, channel_id) DO UPDATE SET
                source_job_revision=excluded.source_job_revision,
                status=excluded.status,
                catalog_attempted=excluded.catalog_attempted,
                catalog_status=excluded.catalog_status,
                catalog_attempts=excluded.catalog_attempts,
                videos_collected=excluded.videos_collected,
                categories_filled=excluded.categories_filled,
                face_status=excluded.face_status,
                error=excluded.error,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                imported_at=excluded.imported_at
            """,
            (
                bundle.node_id,
                job_id,
                run_id,
                source_job_revision,
                _require_id(row, "channel_id", "organization_runs"),
                _require_id(row, "status", "organization_runs"),
                int(bool(_integer(row.get("catalog_attempted")))),
                _text(row.get("catalog_status")),
                _integer(row.get("catalog_attempts")),
                _integer(row.get("videos_collected")),
                _integer(row.get("categories_filled")),
                _text(row.get("face_status")),
                _text(row.get("error"))[:1000],
                _text(row.get("started_at")),
                _text(row.get("finished_at")),
                utc_now(),
            ),
        )
    return len(rows)


_DISCOVERY_IMPORTERS: dict[
    str,
    Callable[[sqlite3.Connection, list[dict[str, Any]]], int],
] = {
    "channels": _import_channels,
    "videos": _import_videos,
    "channel_classifications": _import_channel_classifications,
    "search_results": _import_search_results,
    "video_discoveries": _import_video_discoveries,
    "video_snapshots": _import_video_snapshots,
    "query_runs": _import_query_runs,
}


_ORGANIZATION_DATASETS = {
    "channels",
    "videos",
    "channel_classifications",
    "channel_face_checks",
    "enrichment_attempts",
    "organization_runs",
}


def _import_discovery(
    conn: sqlite3.Connection,
    bundle: EdgeBundle,
) -> dict[str, int]:
    unknown = set(bundle.records) - set(_DISCOVERY_IMPORTERS)
    if unknown:
        raise EdgeProtocolError(
            "unsupported discovery datasets: " + ", ".join(sorted(unknown))
        )
    counts: dict[str, int] = {}
    new_ids: set[str] = set()
    # Parent entities must exist before references/snapshots are inserted.
    for dataset in (
        "channels",
        "videos",
        "channel_classifications",
        "search_results",
        "video_discoveries",
        "video_snapshots",
        "query_runs",
    ):
        rows = bundle.records.get(dataset, [])
        importer = _DISCOVERY_IMPORTERS[dataset]
        if importer is _import_channels:
            counts.update(_import_channel_records(conn, rows, new_ids=new_ids))
        elif importer is _import_video_snapshots:
            counts[dataset] = importer(conn, rows)
        else:
            counts[dataset] = _bulk_import(conn, importer, rows)
    from beacon_engine.edge.discovery_yield import attribution_counts
    counts.update(attribution_counts(bundle.records, new_ids))
    return counts


def _import_organization(
    conn: sqlite3.Connection,
    bundle: EdgeBundle,
) -> dict[str, int]:
    unknown = set(bundle.records) - _ORGANIZATION_DATASETS
    if unknown:
        raise EdgeProtocolError(
            "unsupported organization datasets: "
            + ", ".join(sorted(unknown))
        )
    counts: dict[str, int] = {}
    counts.update(_import_channel_records(
        conn,
        bundle.records.get("channels", []),
    ))
    counts["videos"] = _bulk_import(
        conn,
        _import_videos,
        bundle.records.get("videos", []),
    )
    counts["channel_classifications"] = _bulk_import(
        conn,
        _import_channel_classifications,
        bundle.records.get("channel_classifications", []),
    )
    counts["channel_face_checks"] = _bulk_import(
        conn,
        _import_channel_face_checks,
        bundle.records.get("channel_face_checks", []),
    )
    counts["enrichment_attempts"] = _bulk_import(
        conn,
        _import_enrichment_attempts,
        bundle.records.get("enrichment_attempts", []),
    )
    counts["organization_runs"] = _bulk_import(
        conn,
        _import_organization_runs,
        bundle.records.get("organization_runs", []),
        bundle=bundle,
    )
    return counts


def _import_records(
    conn: sqlite3.Connection,
    bundle: EdgeBundle,
) -> dict[str, int]:
    if bundle.kind == DISCOVERY_BUNDLE_KIND:
        return _import_discovery(conn, bundle)
    if bundle.kind == ORGANIZATION_BUNDLE_KIND:
        return _import_organization(conn, bundle)
    raise EdgeProtocolError(f"no importer registered for kind '{bundle.kind}'")


def _duplicate_report(
    row: sqlite3.Row,
    bundle: EdgeBundle,
) -> ImportReport:
    try:
        counts = {
            str(name): int(value)
            for name, value in json.loads(row["counts_json"]).items()
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        counts = {}
    return ImportReport(
        bundle_id=bundle.bundle_id,
        node_id=bundle.node_id,
        kind=bundle.kind,
        status="duplicate",
        counts=counts,
    )


def _report_with_ack(
    report: ImportReport,
    bundle: EdgeBundle,
    *,
    signing_key: str | bytes | None,
    ack_dir: str | os.PathLike | None,
) -> ImportReport:
    if ack_dir is None:
        return report
    ack_path = write_ack(
        bundle,
        ack_dir,
        counts=report.counts,
        signing_key=signing_key,
        status=report.status,
    )
    return ImportReport(
        bundle_id=report.bundle_id,
        node_id=report.node_id,
        kind=report.kind,
        status=report.status,
        counts=report.counts,
        ack_path=str(ack_path),
    )


def _stage_loaded_bundle(
    conn: sqlite3.Connection,
    bundle: EdgeBundle,
    bundle_path: str | os.PathLike,
) -> ImportReport:
    if getattr(conn, "dialect", "sqlite") == "sqlite":
        conn.execute(_IMPORT_LEDGER_SQL)
    existing = conn.execute(
        """
        SELECT counts_json
        FROM edge_import_batches
        WHERE bundle_id=?
        """,
        (bundle.bundle_id,),
    ).fetchone()
    if existing is not None:
        return _duplicate_report(existing, bundle)

    source_job_revision = ""
    organization_job_id = ""
    organization_run_id = ""
    if bundle.kind == ORGANIZATION_BUNDLE_KIND:
        (
            source_job_revision,
            organization_job_id,
            organization_run_id,
        ) = _organization_metadata(bundle)
        if getattr(conn, "dialect", "sqlite") == "sqlite":
            conn.execute(_ORGANIZATION_IMPORT_JOBS_SQL)
        existing_job = conn.execute(
            """
            SELECT counts_json
            FROM edge_organization_import_jobs
            WHERE source_job_revision=?
            """,
            (source_job_revision,),
        ).fetchone()
        if existing_job is not None:
            return _duplicate_report(existing_job, bundle)

    counts = _import_records(conn, bundle)
    if bundle.kind == ORGANIZATION_BUNDLE_KIND:
        conn.execute(
            """
            INSERT INTO edge_organization_import_jobs (
                source_job_revision, bundle_id, node_id, job_id, run_id,
                imported_at, counts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_job_revision,
                bundle.bundle_id,
                bundle.node_id,
                organization_job_id,
                organization_run_id,
                utc_now(),
                json.dumps(counts, sort_keys=True),
            ),
        )
    conn.execute(
        """
        INSERT INTO edge_import_batches (
            bundle_id, node_id, kind, payload_sha256, created_at,
            imported_at, counts_json, source_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bundle.bundle_id,
            bundle.node_id,
            bundle.kind,
            str(bundle.manifest["payload_sha256"]),
            _text(bundle.payload.get("created_at")),
            utc_now(),
            json.dumps(counts, sort_keys=True),
            str(Path(bundle_path).resolve()),
        ),
    )
    return ImportReport(
        bundle_id=bundle.bundle_id,
        node_id=bundle.node_id,
        kind=bundle.kind,
        status="imported",
        counts=counts,
    )


def _begin_import(conn):
    if getattr(conn, "dialect", "sqlite") == "postgresql":
        # Serialize importer sessions, not all database writers. This protects
        # bundle/job deduplication while unrelated operator saves can commit.
        # PostgreSQL's configured lock timeout bounds a competing importer wait.
        conn.execute("SELECT pg_advisory_xact_lock(726493183)")
    else:
        conn.execute("BEGIN IMMEDIATE")


def _import_loaded_bundle(
    conn: sqlite3.Connection,
    bundle: EdgeBundle,
    bundle_path: str | os.PathLike,
    *,
    signing_key: str | bytes | None,
    ack_dir: str | os.PathLike | None = None,
) -> ImportReport:
    try:
        _begin_import(conn)
        report = _stage_loaded_bundle(conn, bundle, bundle_path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _report_with_ack(
        report,
        bundle,
        signing_key=signing_key,
        ack_dir=ack_dir,
    )


def _wal_size(db_path: Path) -> int:
    try:
        return int(Path(f"{db_path}-wal").stat().st_size)
    except OSError:
        return 0


def _checkpoint_and_trim_wal(
    db_path: str | os.PathLike,
    *,
    truncate_bytes: int = DEFAULT_WAL_TRUNCATE_BYTES,
    busy_timeout_ms: int = ACTIVE_WAL_TRUNCATE_TIMEOUT_MS,
) -> dict[str, Any]:
    """Checkpoint committed pages and briefly trim an oversized WAL."""
    path = Path(db_path)
    result: dict[str, Any] = {
        "wal_bytes_before": _wal_size(path),
        "wal_bytes_after": 0,
        "passive_busy": False,
        "passive_log_frames": 0,
        "passive_checkpointed_frames": 0,
        "truncate_attempted": False,
        "truncate_busy": False,
        "truncated": False,
        "error": "",
    }
    connection: sqlite3.Connection | None = None
    try:
        timeout_ms = max(1, int(busy_timeout_ms or 1))
        connection = sqlite3.connect(
            path,
            timeout=timeout_ms / 1000.0,
        )
        connection.execute(f"PRAGMA busy_timeout={timeout_ms}")
        passive = connection.execute(
            "PRAGMA wal_checkpoint(PASSIVE)"
        ).fetchone()
        if passive is not None:
            result["passive_busy"] = bool(int(passive[0] or 0))
            result["passive_log_frames"] = int(passive[1] or 0)
            result["passive_checkpointed_frames"] = int(passive[2] or 0)
        threshold = max(1, int(truncate_bytes or 1))
        if _wal_size(path) >= threshold:
            result["truncate_attempted"] = True
            truncated = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if truncated is not None:
                result["truncate_busy"] = bool(int(truncated[0] or 0))
            result["truncated"] = (
                not result["truncate_busy"]
                and _wal_size(path) < result["wal_bytes_before"]
            )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        result["error"] = (
            f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:300]}"
        )
    finally:
        if connection is not None:
            connection.close()
        result["wal_bytes_after"] = _wal_size(path)
    return result


class _WalMaintenanceWorker:
    """Coalesce WAL checkpoints away from the importer transaction loop."""

    def __init__(
        self,
        db_path: str | os.PathLike,
        *,
        truncate_bytes: int,
    ) -> None:
        self.db_path = Path(db_path)
        self.truncate_bytes = max(1, int(truncate_bytes or 1))
        self._condition = threading.Condition()
        self._pending = False
        self._final_requested = False
        self._stopping = False
        self._status: dict[str, Any] = {
            "attempts": 0,
            "truncate_attempts": 0,
            "truncate_successes": 0,
            "busy_attempts": 0,
            "wal_bytes_before": 0,
            "wal_bytes_after": 0,
            "error": "",
        }
        self._thread = threading.Thread(
            target=self._run,
            name="signal-edge-wal-maintenance",
            daemon=True,
        )
        self._thread.start()

    def request(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._pending = True
            self._condition.notify()

    def finish(self) -> dict[str, Any]:
        with self._condition:
            self._pending = True
            self._final_requested = True
            self._stopping = True
            self._condition.notify()
        self._thread.join()
        return dict(self._status)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if not self._pending and self._stopping:
                    return
                final_requested = self._final_requested
                self._pending = False
                self._final_requested = False
            result = _checkpoint_and_trim_wal(
                self.db_path,
                truncate_bytes=self.truncate_bytes,
                busy_timeout_ms=(
                    FINAL_WAL_TRUNCATE_TIMEOUT_MS
                    if final_requested
                    else ACTIVE_WAL_TRUNCATE_TIMEOUT_MS
                ),
            )
            self._status["attempts"] += 1
            if result["truncate_attempted"]:
                self._status["truncate_attempts"] += 1
            if result["truncated"]:
                self._status["truncate_successes"] += 1
            if result["passive_busy"] or result["truncate_busy"]:
                self._status["busy_attempts"] += 1
            self._status["wal_bytes_before"] = int(
                result["wal_bytes_before"] or 0
            )
            self._status["wal_bytes_after"] = int(
                result["wal_bytes_after"] or 0
            )
            self._status["error"] = str(result["error"] or "")
            with self._condition:
                if self._stopping and not self._pending:
                    return


class BundleImportSession:
    """Reuse one index connection and safely batch durable commits."""

    def __init__(
        self,
        db_path: str | os.PathLike,
        *,
        wal_truncate_bytes: int = DEFAULT_WAL_TRUNCATE_BYTES,
    ) -> None:
        # Schema initialization is intentionally paid once per inbox drain, not
        # once per immutable bundle. The canonical index remains the only DB.
        self.db_path = Path(db_path)
        self.store = YouTubeStore(db_path)
        self.conn = self.store.connect()
        self._wal_maintenance = None
        self.wal_maintenance_status: dict[str, Any] = {}
        self._closed = False
        if self.store.backend != "sqlite":
            return
        self.conn.row_factory = sqlite3.Row
        # A larger per-import page cache prevents repeated lookups in the
        # multi-gigabyte canonical index from constantly evicting one another.
        # These settings are connection-local and do not weaken durability.
        self.conn.execute("PRAGMA cache_size=-131072")
        self.conn.execute("PRAGMA mmap_size=268435456")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        # SQLite allows one writer at a time. The local radar worker also writes this index,
        # so BEGIN IMMEDIATE here can find the write lock held. Without a busy_timeout that
        # returns SQLITE_BUSY instantly ("database is locked") and the whole bundle is rejected
        # + re-pulled next cycle (the observed churn). Wait up to 2 minutes for the writer to
        # commit and release, so the importer takes its turn instead of failing.
        self.conn.execute("PRAGMA busy_timeout=120000")
        self._wal_maintenance = _WalMaintenanceWorker(
            self.db_path,
            truncate_bytes=wal_truncate_bytes,
        )

    def _request_wal_maintenance(self) -> None:
        if self._wal_maintenance is not None:
            self._wal_maintenance.request()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.conn.close()
        if self._wal_maintenance is not None:
            self.wal_maintenance_status = self._wal_maintenance.finish()

    def __enter__(self) -> "BundleImportSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def import_path(
        self,
        bundle_path: str | os.PathLike,
        *,
        signing_key: str | bytes | None,
        ack_dir: str | os.PathLike | None = None,
        require_signature: bool = True,
    ) -> ImportReport:
        bundle = load_bundle(
            bundle_path,
            signing_key=signing_key,
            require_signature=require_signature,
        )
        if bundle.kind not in {
            DISCOVERY_BUNDLE_KIND,
            ORGANIZATION_BUNDLE_KIND,
        }:
            raise EdgeProtocolError(
                f"no importer registered for kind '{bundle.kind}'"
            )
        report = _import_loaded_bundle(
            self.conn,
            bundle,
            bundle_path,
            signing_key=signing_key,
            ack_dir=ack_dir,
        )
        self._request_wal_maintenance()
        return report

    def import_paths(
        self,
        bundle_paths: list[str | os.PathLike],
        *,
        signing_key: str | bytes | None,
        ack_dir: str | os.PathLike | None = None,
        require_signature: bool = True,
        transaction_bundles: int = DEFAULT_IMPORT_TRANSACTION_BUNDLES,
        on_progress: (
            Callable[[list[BundleImportAttempt], int, int], None] | None
        ) = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[BundleImportAttempt]:
        """Verify first, then commit small groups with one savepoint per bundle."""
        paths = [Path(path) for path in bundle_paths]
        group_size = max(1, min(int(transaction_bundles or 1), 100))
        if self.store.backend == "postgresql":
            # Row locks last until commit. Release a completed bundle's
            # channels before unrelated bundles so operator refreshes can
            # proceed. SQLite retains its existing batched commits.
            group_size = 1
        transaction_total = (
            (len(paths) + group_size - 1) // group_size
            if paths
            else 0
        )
        attempts: list[BundleImportAttempt] = []

        def report_activity(
            phase: str,
            *,
            transaction_number: int,
            transaction_size: int,
            transaction_processed: int,
            active_bundle: int,
        ) -> None:
            if on_activity is None:
                return
            try:
                on_activity(
                    {
                        "phase": phase,
                        "transaction_number": transaction_number,
                        "transaction_total": transaction_total,
                        "transaction_processed": max(
                            0,
                            min(transaction_processed, transaction_size),
                        ),
                        "transaction_size": transaction_size,
                        "active_bundle": max(0, min(active_bundle, len(paths))),
                        "total_bundles": len(paths),
                    }
                )
            except Exception:
                # Live status is best-effort. It must never invalidate verified
                # data, roll back a transaction, or suppress acknowledgements.
                pass

        for start in range(0, len(paths), group_size):
            chunk = paths[start:start + group_size]
            transaction_number = (start // group_size) + 1
            chunk_results: list[BundleImportAttempt | None] = [None] * len(chunk)
            loaded: list[tuple[int, Path, EdgeBundle]] = []
            for index, path in enumerate(chunk):
                report_activity(
                    "verifying",
                    transaction_number=transaction_number,
                    transaction_size=len(chunk),
                    transaction_processed=index,
                    active_bundle=start + index + 1,
                )
                try:
                    bundle = load_bundle(
                        path,
                        signing_key=signing_key,
                        require_signature=require_signature,
                    )
                    if bundle.kind not in {
                        DISCOVERY_BUNDLE_KIND,
                        ORGANIZATION_BUNDLE_KIND,
                    }:
                        raise EdgeProtocolError(
                            f"no importer registered for kind '{bundle.kind}'"
                        )
                    loaded.append((index, path, bundle))
                except Exception as exc:  # noqa: BLE001
                    chunk_results[index] = BundleImportAttempt(
                        path=path,
                        error=exc,
                    )

            staged: list[tuple[int, Path, EdgeBundle, ImportReport]] = []
            if loaded:
                transaction_error: Exception | None = None
                try:
                    report_activity(
                        "waiting_for_database",
                        transaction_number=transaction_number,
                        transaction_size=len(chunk),
                        transaction_processed=0,
                        active_bundle=start + 1,
                    )
                    _begin_import(self.conn)
                    for sequence, (index, path, bundle) in enumerate(loaded):
                        report_activity(
                            "staging",
                            transaction_number=transaction_number,
                            transaction_size=len(chunk),
                            transaction_processed=index,
                            active_bundle=start + index + 1,
                        )
                        savepoint = f"edge_bundle_{sequence}"
                        self.conn.execute(f"SAVEPOINT {savepoint}")
                        try:
                            report = _stage_loaded_bundle(
                                self.conn,
                                bundle,
                                path,
                            )
                            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                            staged.append((index, path, bundle, report))
                            report_activity(
                                "staging",
                                transaction_number=transaction_number,
                                transaction_size=len(chunk),
                                transaction_processed=index + 1,
                                active_bundle=start + index + 1,
                            )
                        except Exception as exc:  # noqa: BLE001
                            try:
                                self.conn.execute(
                                    f"ROLLBACK TO SAVEPOINT {savepoint}"
                                )
                                self.conn.execute(
                                    f"RELEASE SAVEPOINT {savepoint}"
                                )
                            except Exception as cleanup_exc:  # noqa: BLE001
                                transaction_error = cleanup_exc
                                break
                            chunk_results[index] = BundleImportAttempt(
                                path=path,
                                error=exc,
                            )
                    if transaction_error is not None:
                        raise transaction_error
                    report_activity(
                        "committing",
                        transaction_number=transaction_number,
                        transaction_size=len(chunk),
                        transaction_processed=len(chunk),
                        active_bundle=start + len(chunk),
                    )
                    self.conn.commit()
                    self._request_wal_maintenance()
                except Exception as exc:  # noqa: BLE001
                    self.conn.rollback()
                    staged_indexes = {item[0] for item in staged}
                    for index, path, _bundle in loaded:
                        if (
                            index in staged_indexes
                            or chunk_results[index] is None
                        ):
                            chunk_results[index] = BundleImportAttempt(
                                path=path,
                                error=exc,
                            )
                    staged = []

            report_activity(
                "acknowledging",
                transaction_number=transaction_number,
                transaction_size=len(chunk),
                transaction_processed=len(chunk),
                active_bundle=start + len(chunk),
            )
            for index, path, bundle, report in staged:
                try:
                    report = _report_with_ack(
                        report,
                        bundle,
                        signing_key=signing_key,
                        ack_dir=ack_dir,
                    )
                    chunk_results[index] = BundleImportAttempt(
                        path=path,
                        report=report,
                    )
                except Exception as exc:  # noqa: BLE001
                    chunk_results[index] = BundleImportAttempt(
                        path=path,
                        error=exc,
                    )
            chunk_attempts = [
                result
                for result in chunk_results
                if result is not None
            ]
            attempts.extend(chunk_attempts)
            if on_progress is not None:
                try:
                    on_progress(chunk_attempts, len(attempts), len(paths))
                except Exception:
                    # Progress reporting must never invalidate committed data
                    # or prevent acknowledgements from reaching Oracle.
                    pass
        return attempts


def import_bundle(
    db_path: str | os.PathLike,
    bundle_path: str | os.PathLike,
    *,
    signing_key: str | bytes | None,
    ack_dir: str | os.PathLike | None = None,
    require_signature: bool = True,
) -> ImportReport:
    """Verify and atomically merge one edge bundle into Signal's canonical index."""
    with BundleImportSession(db_path) as session:
        return session.import_path(
            bundle_path,
            signing_key=signing_key,
            ack_dir=ack_dir,
            require_signature=require_signature,
        )
