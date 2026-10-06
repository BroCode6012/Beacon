"""SQLite-backed YouTube research index.

This store remembers every video/comment/transcript/search result the crawler
touches. The crawler can then reuse older data, refresh snapshots, and compute
NexLev-style outliers locally.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time as _time
from hashlib import sha1
from pathlib import Path
from typing import Iterable

from beacon_engine.research.providers import (
    ProviderChannel,
    ProviderComment,
    ProviderTranscript,
    ProviderVideo,
)
from beacon_engine.research.faceless_filter import CLASSIFIER_VERSION
from beacon_engine.research.providers.base import normalize_published_at, utc_now_iso

BEACON_ROOT = Path(__file__).resolve().parents[2]
BEACON_DB_PATH = BEACON_ROOT / "storage" / "youtube_index.sqlite3"
RELATIVE_DEFAULT_DB_PATH = Path("storage") / "youtube_index.sqlite3"


def resolve_index_path() -> Path:
    """Only Beacon's own index, or an explicitly configured deployment target."""
    configured = os.getenv("BEACON_INDEX_DB") or os.getenv("SIGNAL_INDEX_DB") or os.getenv("YOUTUBE_INDEX_DB")
    return Path(configured) if configured else BEACON_DB_PATH


def index_reader(**kwargs):
    path = resolve_index_path()
    if str(path) != "postgresql:beacon-index" and not path.exists():
        return None
    return YouTubeStore(str(path), read_only=True, **kwargs)


def index_warning() -> str:
    path = resolve_index_path()
    if str(path) == "postgresql:beacon-index":
        return "" if os.getenv("BEACON_POSTGRES_READ_DSN") or os.getenv("BEACON_POSTGRES_DSN") else "PostgreSQL index connection is not configured"
    return "" if path.exists() else f"index not found at {path} — it will be created empty"


def resolve_default_db_path() -> Path:
    """Current process store: Lab/session override, otherwise cwd-local DB."""
    configured = os.getenv("YOUTUBE_INDEX_DB")
    return Path(configured) if configured else RELATIVE_DEFAULT_DB_PATH


DEFAULT_DB_PATH = resolve_default_db_path()

# Bump when the schema or one-time migrations change. _init() runs the full
# (idempotent) migration once per DB, records this in PRAGMA user_version, then
# skips it on every later construction -- so opening the store stays instant even
# as the index grows to hundreds of thousands of rows.
SCHEMA_VERSION = 8

# Conservative first-pass definition of a repeatable faceless opportunity.
# These are query rules, not destructive exclusions: callers opt in with
# `replicable_only=True`, and the underlying index remains unchanged.
REPLICABLE_MIN_UPLOADS = 8
REPLICABLE_MAX_UPLOADS = 500
REPLICABLE_MIN_AVG_VIEWS = 3_000
REPLICABLE_MAX_AGE_DAYS = 1_095
REPLICABLE_SUBSCRIBER_FLOOR = 1_000
REPLICABLE_EVIDENCE_UPLOADS = 20
REPLICABLE_MIN_OPPORTUNITY_SCORE = 1.0


class _SQLiteConnection(sqlite3.Connection):
    """Transaction contexts also release their file handles on Windows."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class YouTubeStore:
    backend = "sqlite"

    def __new__(cls, path=None, **kwargs):
        if cls is YouTubeStore and str(path if path is not None else resolve_default_db_path()) == "postgresql:beacon-index":
            from beacon_engine.research.postgres_store import PostgresYouTubeStore
            return object.__new__(PostgresYouTubeStore)
        return object.__new__(cls)

    @property
    def state_directory(self) -> Path:
        return self.path.parent

    @property
    def cache_identity(self) -> str:
        return os.path.abspath(str(self.path))

    def insert_id(self, conn, statement, parameters):
        return int(conn.execute(statement, parameters).lastrowid)

    def casefold(self, expression):
        return f"LOWER({expression})"

    def saved_view_conflict(self):
        return "niche_id, name"

    def __init__(self, path: str | os.PathLike | None = None, *,
                 read_only: bool = False) -> None:
        # No explicit path -> resolve the env var LIVE (not baked at import), so the Lab can
        # point YOUTUBE_INDEX_DB at the CURRENT per-session DB and every no-arg store (agent
        # reads, beacon memory) follows it instead of writing Signal's shared index.
        if path is None:
            path = resolve_default_db_path()
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init()

    def connect(self) -> sqlite3.Connection:
        if self.read_only:
            # Read-only ROLE for the Lab on Signal's shared index: a normal connection (so
            # WAL reads work fine even while Signal is actively writing) with query_only=ON,
            # so any write raises sqlite3.OperationalError "attempt to write a readonly
            # database". WAL-safe (mode=ro can't read an uncheckpointed WAL db).
            conn = sqlite3.connect(self.path, timeout=30, factory=_SQLiteConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=ON")
            return conn
        conn = sqlite3.connect(self.path, timeout=30, factory=_SQLiteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Wait (instead of erroring) when the background radar holds the write
        # lock, so concurrent enrichment + radar writes coexist cleanly.
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def upsert_channel(self, channel: ProviderChannel) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    channel_id, title, handle, subscribers, video_count, view_count,
                    description, url, channel_published_at, source, raw_json,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    title=COALESCE(NULLIF(excluded.title, ''), channels.title),
                    handle=COALESCE(NULLIF(excluded.handle, ''), channels.handle),
                    subscribers=CASE WHEN excluded.subscribers > 0 THEN excluded.subscribers ELSE channels.subscribers END,
                    video_count=CASE WHEN excluded.video_count > 0 THEN excluded.video_count ELSE channels.video_count END,
                    view_count=CASE WHEN excluded.view_count > 0 THEN excluded.view_count ELSE channels.view_count END,
                    description=COALESCE(NULLIF(excluded.description, ''), channels.description),
                    url=COALESCE(NULLIF(excluded.url, ''), channels.url),
                    channel_published_at=COALESCE(NULLIF(excluded.channel_published_at, ''), channels.channel_published_at),
                    source=excluded.source,
                    raw_json=COALESCE(NULLIF(excluded.raw_json, '{}'), channels.raw_json),
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    channel.channel_id,
                    channel.title,
                    channel.handle,
                    channel.subscribers,
                    channel.video_count,
                    channel.view_count,
                    channel.description,
                    channel.url,
                    getattr(channel, "published_at", "") or "",
                    channel.source,
                    _json(channel.raw),
                    now,
                    now,
                ),
            )

    def upsert_video(self, video: ProviderVideo, *, snapshot: bool = True) -> None:
        if not video.video_id:
            return
        now = utc_now_iso()
        published_at_normalized = normalize_published_at(
            video.published_at,
            reference=now,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO videos (
                    video_id, title, channel, channel_id, views, published_at,
                    published_at_normalized, url, duration_seconds, description,
                    thumbnail_url, source, raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title=COALESCE(NULLIF(excluded.title, ''), videos.title),
                    channel=COALESCE(NULLIF(excluded.channel, ''), videos.channel),
                    channel_id=COALESCE(NULLIF(excluded.channel_id, ''), videos.channel_id),
                    views=CASE WHEN excluded.views > 0 THEN excluded.views ELSE videos.views END,
                    published_at=COALESCE(NULLIF(excluded.published_at, ''), videos.published_at),
                    published_at_normalized=COALESCE(
                        NULLIF(excluded.published_at_normalized, ''),
                        videos.published_at_normalized
                    ),
                    url=COALESCE(NULLIF(excluded.url, ''), videos.url),
                    duration_seconds=CASE WHEN excluded.duration_seconds > 0 THEN excluded.duration_seconds ELSE videos.duration_seconds END,
                    description=COALESCE(NULLIF(excluded.description, ''), videos.description),
                    thumbnail_url=COALESCE(NULLIF(excluded.thumbnail_url, ''), videos.thumbnail_url),
                    source=excluded.source,
                    raw_json=COALESCE(NULLIF(excluded.raw_json, '{}'), videos.raw_json),
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    video.video_id,
                    video.title,
                    video.channel,
                    video.channel_id,
                    int(video.views or 0),
                    video.published_at,
                    published_at_normalized,
                    video.url,
                    int(video.duration_seconds or 0),
                    video.description,
                    video.thumbnail_url,
                    video.source,
                    _json(video.raw),
                    now,
                    now,
                ),
            )
            if snapshot and video.views > 0:
                conn.execute(
                    """
                    INSERT INTO video_snapshots (video_id, views, captured_at, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (video.video_id, int(video.views), now, video.source),
                )

    def upsert_videos(self, videos: Iterable[ProviderVideo], *, snapshot: bool = True) -> int:
        count = 0
        for video in videos:
            self.upsert_video(video, snapshot=snapshot)
            count += 1
        return count

    def save_search_results(
        self,
        query: str,
        provider: str,
        videos: list[ProviderVideo],
        *,
        run_id: int | None = None,
        kind: str = "search",
        record_event: bool = True,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            for idx, video in enumerate(videos, 1):
                if not video.video_id:
                    continue
                if kind == "search":
                    conn.execute(
                        """
                        INSERT INTO search_results (
                            query, provider, video_id, position, captured_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(query, provider, video_id) DO UPDATE SET
                            position=excluded.position, captured_at=excluded.captured_at
                        """,
                        (query, provider, video.video_id, idx, now),
                    )
                if record_event:
                    conn.execute(
                        """
                        INSERT INTO video_discoveries (
                            video_id, query, provider, discovery_kind, position,
                            run_id, discovered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            video.video_id,
                            query,
                            provider,
                            kind,
                            idx,
                            run_id,
                            now,
                        ),
                    )

    def save_comments(self, comments: Iterable[ProviderComment]) -> int:
        count = 0
        now = utc_now_iso()
        with self.connect() as conn:
            for comment in comments:
                if not comment.video_id or not comment.text.strip():
                    continue
                cid = comment.comment_id or _comment_key(comment.video_id, comment.text)
                conn.execute(
                    """
                    INSERT INTO comments (
                        comment_id, video_id, text, author, like_count, published_at,
                        source, raw_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(comment_id) DO UPDATE SET
                        text=excluded.text,
                        author=COALESCE(NULLIF(excluded.author, ''), comments.author),
                        like_count=CASE WHEN excluded.like_count > 0 THEN excluded.like_count ELSE comments.like_count END,
                        published_at=COALESCE(NULLIF(excluded.published_at, ''), comments.published_at),
                        source=excluded.source,
                        raw_json=COALESCE(NULLIF(excluded.raw_json, '{}'), comments.raw_json),
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        cid,
                        comment.video_id,
                        comment.text,
                        comment.author,
                        int(comment.like_count or 0),
                        comment.published_at,
                        comment.source,
                        _json(comment.raw),
                        now,
                        now,
                    ),
                )
                count += 1
        return count

    def save_transcript(self, transcript: ProviderTranscript | None) -> bool:
        if not transcript or not transcript.video_id or not transcript.text.strip():
            return False
        now = utc_now_iso()
        language = transcript.language or ""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO transcripts (
                    video_id, language, text, generated, source, raw_json,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, language) DO UPDATE SET
                    text=excluded.text,
                    generated=excluded.generated,
                    source=excluded.source,
                    raw_json=COALESCE(NULLIF(excluded.raw_json, '{}'), transcripts.raw_json),
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    transcript.video_id,
                    language,
                    transcript.text,
                    None if transcript.generated is None else int(transcript.generated),
                    transcript.source,
                    _json(transcript.raw),
                    now,
                    now,
                ),
                )
        return True

    def save_outlier_scores(self, outliers: Iterable) -> int:
        now = utc_now_iso()
        count = 0
        YouTubeStore._trends_cache = None  # scores changed -> drop the trends TTL cache
        with self.connect() as conn:
            for item in outliers:
                video_id = getattr(item, "video_id", "")
                if not video_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO outlier_snapshots (
                        video_id, outlier_score, baseline_views, views, age_days,
                        views_per_day, captured_at, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video_id,
                        float(getattr(item, "outlier_score", 0) or 0),
                        int(getattr(item, "baseline_views", 0) or 0),
                        int(getattr(item, "views", 0) or 0),
                        int(getattr(item, "age_days", 0) or 0),
                        float(getattr(item, "views_per_day", 0) or 0),
                        now,
                        getattr(item, "source", "") or "",
                    ),
                )
                count += 1
        return count

    def save_channel_classifications(
        self,
        signals: dict[str, object],
        *,
        mode: str = "strict",
    ) -> int:
        now = utc_now_iso()
        count = 0
        with self.connect() as conn:
            for channel_id, signal in signals.items():
                if not channel_id:
                    continue
                matches = getattr(signal, "matches", None)
                likely = bool(matches(mode)) if callable(matches) else bool(
                    getattr(signal, "likely_faceless", False)
                )
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
                    """,
                    (
                        channel_id,
                        int(likely),
                        float(getattr(signal, "confidence", 0) or 0),
                        float(getattr(signal, "score", 0) or 0),
                        int(getattr(signal, "sample_count", 0) or 0),
                        int(getattr(signal, "positive_title_count", 0) or 0),
                        int(getattr(signal, "negative_title_count", 0) or 0),
                        _json(getattr(signal, "positives", []) or []),
                        _json(getattr(signal, "negatives", []) or []),
                        CLASSIFIER_VERSION,
                        now,
                    ),
                )
                count += 1
        return count

    def videos(self, *, limit: int = 100, min_views: int = 0) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT * FROM videos
                WHERE views >= ?
                ORDER BY views DESC, last_seen_at DESC
                LIMIT ?
                """,
                (min_views, limit),
            ))

    def videos_by_channel(self, *, min_channel_videos: int = 3) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT v.*, c.channel_count
                FROM videos v
                JOIN (
                    SELECT channel_id, COUNT(*) AS channel_count
                    FROM videos
                    WHERE channel_id != ''
                    GROUP BY channel_id
                    HAVING COUNT(*) >= ?
                ) c ON c.channel_id = v.channel_id
                WHERE v.views > 0
                """,
                (min_channel_videos,),
            ))

    def count_channels(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0])

    def channel_dominant_queries(
        self,
        *,
        min_count: int = 1,
        only_missing: bool = False,
    ) -> dict[str, tuple[str, int]]:
        """Map each channel to the search query that most often surfaced it.

        Uses the discovery log joined to videos, so it is computed for free from
        data already collected -- no extra scraping. This becomes the channel's
        niche/category label.
        """
        if only_missing:
            source = """
                    FROM channels missing
                    JOIN videos v ON v.channel_id = missing.channel_id
                    JOIN video_discoveries d ON d.video_id = v.video_id
                    WHERE COALESCE(missing.category, '') = ''
                      AND v.channel_id != ''
                      AND COALESCE(d.query, '') != ''
            """
        else:
            source = """
                    FROM video_discoveries d
                    JOIN videos v ON v.video_id = d.video_id
                    WHERE v.channel_id != '' AND COALESCE(d.query, '') != ''
            """
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                WITH channel_query AS (
                    SELECT
                        v.channel_id AS channel_id,
                        d.query AS query,
                        COUNT(*) AS hits,
                        ROW_NUMBER() OVER (
                            PARTITION BY v.channel_id
                            ORDER BY COUNT(*) DESC, d.query ASC
                        ) AS rank
                    {source}
                    GROUP BY v.channel_id, d.query
                )
                SELECT channel_id, query, hits
                FROM channel_query
                WHERE rank = 1 AND hits >= ?
                """,
                (int(min_count),),
            ).fetchall()
        return {row["channel_id"]: (row["query"], int(row["hits"])) for row in rows}

    def set_channel_categories(self, categories: dict[str, str]) -> int:
        """Persist channel category labels. Returns rows updated."""
        now = utc_now_iso()
        count = 0
        with self.connect() as conn:
            for channel_id, category in categories.items():
                if not channel_id or not (category or "").strip():
                    continue
                cur = conn.execute(
                    "UPDATE channels SET category=?, last_seen_at=? WHERE channel_id=?",
                    (category.strip(), now, channel_id),
                )
                count += cur.rowcount
        return count

    def category_breakdown(self, *, limit: int = 40) -> list[tuple[str, int]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(category, ''), '(uncategorized)') AS category,
                       COUNT(*) AS n
                FROM channels
                GROUP BY 1
                ORDER BY n DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [(row["category"], int(row["n"])) for row in rows]

    def ids_needing_channel_meta(
        self,
        *,
        limit: int | None = None,
        max_attempts: int = 3,
    ) -> list[str]:
        """Channels missing their creation date (for 'days since start')."""
        sql = [
            "SELECT c.channel_id",
            "FROM channels c",
            "LEFT JOIN enrichment_log e",
            "  ON e.entity_type = 'channel_meta' AND e.entity_id = c.channel_id",
            "WHERE COALESCE(c.channel_published_at, '') = ''",
            "  AND c.channel_id != '' AND COALESCE(e.attempts, 0) < ?",
            "ORDER BY COALESCE(e.attempts, 0) ASC, c.last_seen_at DESC",
        ]
        params: list[object] = [int(max_attempts)]
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        with self.connect() as conn:
            return [r[0] for r in conn.execute("\n".join(sql), params)]

    def channel_metrics(
        self,
        *,
        min_subs: int | None = None,
        max_subs: int | None = None,
        min_avg_views: int | None = None,
        max_avg_views: int | None = None,
        max_days_since_start: int | None = None,
        min_days_since_start: int | None = None,
        min_uploads: int | None = None,
        max_uploads: int | None = None,
        min_views_per_sub: float | None = None,
        min_outlier_score: float | None = None,
        faceless_only: bool = False,
        category: str | None = None,
        category_contains: str | None = None,
        categories_in: Iterable[str] | None = None,
        video_format: str | None = None,
        sort: str = "avg_views",
        descending: bool = True,
        limit: int = 100,
        rpm: float = 4.0,
        include_excluded: bool = False,
        niche_id: int | None = None,
        added_within_days: int | None = None,
        topic: str | None = None,
        replicable_only: bool = False,
        count_only: bool = False,
    ) -> list[dict]:
        """NexLev-style channel finder: compute per-channel metrics + filter/sort.

        Almost everything here is derived from channel-level stats already in the
        index (subs, total views, uploads, creation date) -- no per-video crawl.
        Excluded channels (kids/off-niche) are hidden unless include_excluded.
        """
        sort_columns = {
            "avg_views": "avg_views",
            "subscribers": "subscribers",
            "days_since_start": "days_since_start",
            "uploads": "video_count",
            "revenue": "est_revenue",
            "views_per_sub": "views_per_sub",
            "outlier_score": "outlier_score",
            "opportunity_score": "opportunity_score",
            "added_at": "added_at",      # discovery time (first_seen_at) — newest-added first
        }
        order_col = sort_columns.get(sort, "avg_views")
        where: list[str] = ["m.video_count > 0", "m.view_count > 0"]
        params: list[object] = [float(rpm)]
        if min_subs is not None:
            where.append("m.subscribers >= ?"); params.append(int(min_subs))
        if max_subs is not None:
            where.append("m.subscribers <= ?"); params.append(int(max_subs))
        if min_avg_views is not None:
            where.append("m.avg_views >= ?"); params.append(int(min_avg_views))
        if max_avg_views is not None:
            where.append("m.avg_views <= ?"); params.append(int(max_avg_views))
        if max_days_since_start is not None:
            where.append("m.days_since_start IS NOT NULL AND m.days_since_start <= ?")
            params.append(int(max_days_since_start))
        if min_days_since_start is not None:
            where.append("m.days_since_start IS NOT NULL AND m.days_since_start >= ?")
            params.append(int(min_days_since_start))
        if min_uploads is not None:
            where.append("m.video_count >= ?"); params.append(int(min_uploads))
        if max_uploads is not None:
            where.append("m.video_count <= ?"); params.append(int(max_uploads))
        if min_views_per_sub is not None:
            where.append("m.views_per_sub >= ?"); params.append(float(min_views_per_sub))
        if min_outlier_score is not None:
            where.append("m.outlier_score >= ?"); params.append(float(min_outlier_score))
        if faceless_only:
            where.append("m.faceless = 1")
        if replicable_only:
            where.extend([
                "m.faceless = 1",
                # Name-only high-confidence junk stays in the index and can still
                # be found by ordinary queries. Opportunity views suppress it.
                "opportunity_name_rejection(m.title) = 0",
                "m.subscribers > 0",
                f"m.video_count BETWEEN {REPLICABLE_MIN_UPLOADS} "
                f"AND {REPLICABLE_MAX_UPLOADS}",
                f"m.avg_views >= {REPLICABLE_MIN_AVG_VIEWS}",
                "m.days_since_start IS NOT NULL",
                f"m.days_since_start BETWEEN 1 AND {REPLICABLE_MAX_AGE_DAYS}",
                f"m.opportunity_score >= {REPLICABLE_MIN_OPPORTUNITY_SCORE}",
            ])
        if topic:
            where.append("m.topic = ?"); params.append(topic)
        if category:
            where.append("m.category = ?"); params.append(category)
        if category_contains:
            where.append(f"{self.casefold('m.category')} LIKE {self.casefold('?')}")
            params.append(f"%{category_contains}%")
        clean_cats = [c for c in (categories_in or []) if c]
        if clean_cats:
            where.append("m.category IN (%s)" % ",".join("?" for _ in clean_cats))
            params.extend(clean_cats)
        if video_format == "long":
            where.append("m.avg_duration >= 90")
        elif video_format == "short":
            where.append("m.avg_duration > 0 AND m.avg_duration < 90")
        if not include_excluded:
            where.append(
                "m.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)")
        if niche_id is not None:
            # scope to channels tracked in this niche — composes with every metric
            # filter above so "in niche X AND subs<=N AND avg>=M" is one accurate query
            where.append("m.channel_id IN (SELECT channel_id FROM niche_channels "
                         "WHERE niche_id = ? AND enabled = 1)")
            params.append(int(niche_id))
        if added_within_days is not None:
            # discovery-recency window: channels added to OUR index within N days.
            # first_seen_at is an ISO string; julianday() parses it, so compare in day-space.
            where.append("m.added_at IS NOT NULL AND "
                         "julianday(m.added_at) >= julianday('now', ?)")
            params.append(f"-{int(added_within_days)} days")
        params.append(int(limit))
        order_dir = "DESC" if descending else "ASC"
        # Bundles often give many channels the same discovery timestamp. Stable
        # ties keep pages consistent across backends and successive reads.
        null_order = (" NULLS LAST" if descending else " NULLS FIRST") if self.backend == "postgresql" else ""
        # Only join the per-channel duration aggregate when a format filter needs
        # it -- it scans the whole videos table, so skip it for the common case.
        need_duration = video_format in ("long", "short")
        postgres = self.backend == "postgresql"
        duration_col = (
            "COALESCE(dur.avg_duration, 0) AS avg_duration,"
            if need_duration else "0 AS avg_duration,"
        )
        if postgres and need_duration:
            duration_col = ""
        duration_join = (
            """
                LEFT JOIN (
                    SELECT channel_id, AVG(duration_seconds) AS avg_duration
                    FROM videos
                    WHERE duration_seconds > 0 AND channel_id != ''
                    GROUP BY channel_id
                ) dur ON dur.channel_id = ch.channel_id
            """
            if need_duration else ""
        )
        if postgres and need_duration:
            duration_join = ""
        # PostgreSQL otherwise pushes the date/score predicates into the channel
        # scan, estimates almost no survivors, and repeatedly probes every
        # classification index. On the full index that plan exceeds the bounded
        # query wait. Resolve the inexpensive eligibility/join stage before those
        # predicates. Keep SQLite's established planner behavior unchanged.
        postgres = self.backend == "postgresql"
        avatar = """CASE WHEN json_valid(ch.raw_json) THEN COALESCE(
                        json_extract(ch.raw_json, '$.snippet.thumbnails.medium.url'),
                        json_extract(ch.raw_json, '$.snippet.thumbnails.default.url'))
                    END AS avatar_url"""
        avatar_col = "" if postgres else avatar + ","
        eligible = ""
        if postgres:
            # These conditions are already mandatory in the outer query. They
            # also prevent division by zero when materializing the metrics.
            eligible = "WHERE ch.video_count > 0 AND ch.view_count > 0"
            if replicable_only:
                eligible += (
                    f" AND ch.subscribers > 0 AND ch.video_count BETWEEN "
                    f"{REPLICABLE_MIN_UPLOADS} AND {REPLICABLE_MAX_UPLOADS}"
                    f" AND ch.view_count * 1.0 / NULLIF(ch.video_count, 0)"
                    f" >= {REPLICABLE_MIN_AVG_VIEWS}"
                )
        materialized = "MATERIALIZED " if postgres else ""
        m_cte = f"""
            WITH m AS {materialized}(
                SELECT
                    ch.channel_id, ch.title, ch.subscribers, ch.view_count,
                    ch.video_count, ch.category, ch.topic, ch.channel_published_at,
                    ch.first_seen_at AS added_at,   -- when WE first indexed it (discovery time)
                    -- Channel avatar from the enrichment payload (yt3.ggpht.com).
                    {avatar_col}
                    CASE WHEN ch.video_count > 0
                         THEN ch.view_count * 1.0 / ch.video_count ELSE 0 END AS avg_views,
                    CASE WHEN COALESCE(ch.channel_published_at, '') != ''
                         THEN CAST(TRUNC(julianday('now') - julianday(ch.channel_published_at)) AS BIGINT)
                         END AS days_since_start,
                    CASE WHEN ch.subscribers > 0
                         THEN ch.view_count * 1.0 / ch.video_count / ch.subscribers
                         ELSE 0 END AS views_per_sub,
                    -- Outlier channel = high views vs subs, boosted for how fast
                    -- (recency) it got there. Newer channels with views >> subs
                    -- score highest; older ones taper off.
                    CASE WHEN ch.subscribers > 0 AND ch.video_count > 0
                         THEN (ch.view_count * 1.0 / ch.video_count / ch.subscribers)
                              * MIN(3.0, 365.0 / MAX(
                                    CASE WHEN COALESCE(ch.channel_published_at, '') != ''
                                         THEN CAST(TRUNC(julianday('now') - julianday(ch.channel_published_at)) AS BIGINT)
                                         ELSE 365 END, 30))
                         END AS outlier_score,
                    -- Replicable opportunity = performance relative to a stabilized
                    -- subscriber denominator, adjusted for channel age and evidence.
                    -- A 1-subscriber record therefore cannot win purely by arithmetic,
                    -- and channels with fewer than 20 uploads receive a confidence ramp.
                    CASE WHEN ch.subscribers > 0 AND ch.video_count > 0
                         THEN (
                              ch.view_count * 1.0 / ch.video_count
                              / MAX(ch.subscribers, {REPLICABLE_SUBSCRIBER_FLOOR})
                         )
                         * MIN(2.0, 730.0 / MAX(
                               CASE WHEN COALESCE(ch.channel_published_at, '') != ''
                                    THEN CAST(TRUNC(
                                         julianday('now')
                                         - julianday(ch.channel_published_at)) AS BIGINT)
                                    ELSE {REPLICABLE_MAX_AGE_DAYS} END,
                               365))
                         * MIN(
                               1.0,
                               ch.video_count * 1.0
                               / {REPLICABLE_EVIDENCE_UPLOADS})
                         ELSE 0
                    END AS opportunity_score,
                    {duration_col}
                    cc.likely_faceless,
                    COALESCE(vlm.verdict, fc.verdict) AS face_verdict,
                    -- Combined faceless, best signal first: the VLM verdict (on the
                    -- gray-zone channels) wins, then the CPU recurring-face check,
                    -- then the FAST text model (title+description+video-titles), and
                    -- finally the old keyword classifier. on_camera -> 0 (excludes presenters).
                    CASE
                        WHEN vlm.verdict = 'on_camera' THEN 0
                        WHEN vlm.verdict = 'faceless' THEN 1
                        WHEN fc.verdict = 'on_camera' THEN 0
                        WHEN fc.verdict = 'faceless' THEN 1
                        ELSE COALESCE(tm.faceless, cc.likely_faceless, 0)
                    END AS faceless
                FROM channels ch
                LEFT JOIN channel_classifications cc ON cc.channel_id = ch.channel_id
                LEFT JOIN channel_face_check fc ON fc.channel_id = ch.channel_id
                LEFT JOIN channel_vlm_check vlm ON vlm.channel_id = ch.channel_id
                LEFT JOIN channel_text_model tm ON tm.channel_id = ch.channel_id
                {duration_join}
                {eligible}
            )"""
        query_params = params
        if postgres and need_duration:
            # Filter channel eligibility before reading indexed video lengths.
            # Keep format filtering before ranking/LIMIT: a short channel at the
            # top of the score order must not hide lower-ranked long channels.
            format_where = [clause for clause in where if "m.avg_duration" in clause]
            candidate_where = [clause for clause in where if clause not in format_where]
            m_cte = m_cte.replace("WITH m AS", "WITH metrics_base AS", 1) + f""",
                duration_candidates AS MATERIALIZED (
                    SELECT m.* FROM metrics_base m
                    WHERE {' AND '.join(candidate_where)}
                )"""
            if count_only:
                m_cte += """, m AS MATERIALIZED (
                    SELECT candidate.*, COALESCE(dur.avg_duration, 0) AS avg_duration
                    FROM duration_candidates candidate
                    LEFT JOIN LATERAL (
                        SELECT AVG(video.duration_seconds) AS avg_duration
                        FROM videos video
                        WHERE video.channel_id = candidate.channel_id
                          AND video.channel_id != '' AND video.duration_seconds > 0
                    ) dur ON TRUE
                )"""
            else:
                # Preserve the ranked input's path order through the lateral
                # lookup. LIMIT can then stop once enough format matches exist,
                # rather than looking up video lengths for every candidate.
                # OFFSET 0 prevents flattening this ordered subquery; the
                # selected page also has an explicit ORDER BY for stable ties.
                m_cte += f""", ordered_candidates AS (
                    SELECT m.*, m.avg_views * ? / 1000.0 AS est_revenue
                    FROM duration_candidates m
                    ORDER BY {order_col} {order_dir}{null_order}, channel_id ASC
                    OFFSET 0
                )"""
            where = format_where
            # Eligibility parameters now occur in the CTE, before the RPM and
            # page-size parameters in the final SELECT.
            query_params = [*params[1:-1], params[0], params[-1]]
        # count-only fast path: reuse the SAME filters (the m-CTE has no bind params,
        # so bind just the WHERE filters = params without the rpm[0] and limit[-1]).
        if count_only:
            csql = f"{m_cte}\nSELECT COUNT(*) AS n FROM m WHERE {' AND '.join(where)}"
            with self.connect() as conn:
                if replicable_only and self.backend == "sqlite":
                    from beacon_engine.research.junk_filter import opportunity_name_rejection
                    conn.create_function("opportunity_name_rejection", 1,
                                         lambda title: int(bool(opportunity_name_rejection(title))),
                                         deterministic=True)
                n = conn.execute(csql, params[1:-1]).fetchone()[0]
            return [{"count": int(n)}]
        sql = f"""{m_cte}
            SELECT m.*, m.avg_views * ? / 1000.0 AS est_revenue
            FROM m
            WHERE {' AND '.join(where)}
            ORDER BY {order_col} {order_dir}{null_order}, channel_id ASC
            LIMIT ?
        """
        if postgres:
            # Decode provider JSON only for this page, after ranking and LIMIT.
            # Large/malformed metadata cannot increase the work for every
            # candidate; count-only reads never decode avatars at all.
            selected_sql = f"""
                SELECT m.*, m.avg_views * ? / 1000.0 AS est_revenue
                FROM m WHERE {' AND '.join(where)}
                ORDER BY {order_col} {order_dir}{null_order}, channel_id ASC LIMIT ?
            """
            if need_duration:
                duration_where = [clause.replace(
                    'm.avg_duration', 'COALESCE(dur.avg_duration, 0)') for clause in where]
                selected_sql = f"""
                    SELECT candidate.*, COALESCE(dur.avg_duration, 0) AS avg_duration
                    FROM ordered_candidates candidate
                    LEFT JOIN LATERAL (
                        SELECT AVG(video.duration_seconds) AS avg_duration
                        FROM videos video
                        WHERE video.channel_id = candidate.channel_id
                          AND video.channel_id != '' AND video.duration_seconds > 0
                    ) dur ON TRUE
                    WHERE {' AND '.join(duration_where)}
                    ORDER BY candidate.{order_col} {order_dir}{null_order},
                             candidate.channel_id ASC LIMIT ?
                """
            sql = f"""{m_cte}, selected AS MATERIALIZED ({selected_sql})
            SELECT selected.*, {avatar}
            FROM selected JOIN channels ch ON ch.channel_id = selected.channel_id
            ORDER BY selected.{order_col} {order_dir}{null_order}, selected.channel_id ASC
            """
        with self.connect() as conn:
            if replicable_only and self.backend == "sqlite":
                from beacon_engine.research.junk_filter import opportunity_name_rejection
                conn.create_function("opportunity_name_rejection", 1,
                                     lambda title: int(bool(opportunity_name_rejection(title))),
                                     deterministic=True)
            rows = conn.execute(sql, query_params).fetchall()
        return [dict(r) for r in rows]

    def topic_counts(self) -> list[dict]:
        """[{topic, count}] over the visible index (excluded channels hidden),
        biggest first — for the Channels-tab topic dropdown."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(topic, ''), 'Other') AS topic, COUNT(*) AS c
                FROM channels
                WHERE channel_id NOT IN (SELECT channel_id FROM channel_exclusions)
                GROUP BY COALESCE(NULLIF(topic, ''), 'Other')
                ORDER BY c DESC
                """
            ).fetchall()
        return [{"topic": r["topic"], "count": int(r["c"])} for r in rows]

    def set_channel_topic(self, channel_ids: Iterable[str], topic: str) -> int:
        """Override the clean topic for specific channels (agent 'fix topic')."""
        ids = [c for c in channel_ids if c]
        if not ids:
            return 0
        with self.connect() as conn:
            conn.executemany("UPDATE channels SET topic = ? WHERE channel_id = ?",
                             [(str(topic), c) for c in ids])
        return len(ids)

    def channel_avatars(self, channel_ids: Iterable[str]) -> dict[str, str]:
        """Map channel_id -> avatar image URL (yt3.ggpht.com), pulled from each
        channel's enrichment payload. Missing/unenriched channels are omitted."""
        ids = [c.strip() for c in dict.fromkeys(channel_ids or []) if c and c.strip()]
        if not ids:
            return {}
        out: dict[str, str] = {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT channel_id,
                    CASE WHEN json_valid(raw_json) THEN COALESCE(
                        json_extract(raw_json, '$.snippet.thumbnails.medium.url'),
                        json_extract(raw_json, '$.snippet.thumbnails.default.url'))
                    END AS avatar_url
                FROM channels
                WHERE channel_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        for r in rows:
            if r["avatar_url"]:
                out[r["channel_id"]] = r["avatar_url"]
        return out

    def rising_videos(
        self,
        *,
        days: int = 7,
        limit: int = 25,
        faceless_only: bool = True,
        min_views_per_day: int = 1_000,
        include_excluded: bool = False,
    ) -> list[dict]:
        """Videos gaining views FASTEST right now, from snapshot deltas.

        A video with 1M lifetime views is history; a video that gained 200K in
        the last few days is a live trend. Rate = (latest - earliest snapshot)
        within the window, per day."""
        sql = f"""
            WITH win AS (
                SELECT s.video_id,
                       MIN(s.captured_at) AS first_at,
                       MAX(s.captured_at) AS last_at,
                       COUNT(*) AS snaps
                FROM video_snapshots s
                WHERE s.captured_at >= datetime('now', ?)
                GROUP BY s.video_id
                HAVING COUNT(*) >= 2
            ),
            rates AS (
                SELECT w.video_id,
                       (SELECT views FROM video_snapshots
                        WHERE video_id = w.video_id AND captured_at = w.first_at
                        ORDER BY id LIMIT 1) AS v0,
                       (SELECT views FROM video_snapshots
                        WHERE video_id = w.video_id AND captured_at = w.last_at
                        ORDER BY id DESC LIMIT 1) AS v1,
                       julianday(w.last_at) - julianday(w.first_at) AS span_days
                FROM win w
            )
            SELECT v.video_id, v.title, v.channel, v.channel_id, v.views, v.url,
                   v.thumbnail_url, v.published_at_normalized AS published_at,
                   CAST(TRUNC(r.v1 - r.v0) AS BIGINT) AS gained,
                   CAST(TRUNC((r.v1 - r.v0) / MAX(r.span_days, 0.25)) AS BIGINT)
                       AS views_per_day,
                   ROUND(r.span_days, 1) AS measured_days,
                   CASE
                       WHEN vlm.verdict = 'on_camera' THEN 0
                       WHEN vlm.verdict = 'faceless' THEN 1
                       WHEN fc.verdict = 'on_camera' THEN 0
                       WHEN fc.verdict = 'faceless' THEN 1
                       ELSE COALESCE(tm.faceless, cc.likely_faceless, 0)
                   END AS faceless
            FROM rates r
            JOIN videos v ON v.video_id = r.video_id
            LEFT JOIN channel_classifications cc ON cc.channel_id = v.channel_id
            LEFT JOIN channel_face_check fc ON fc.channel_id = v.channel_id
            LEFT JOIN channel_vlm_check vlm ON vlm.channel_id = v.channel_id
            LEFT JOIN channel_text_model tm ON tm.channel_id = v.channel_id
            WHERE r.v1 > r.v0
              AND r.span_days >= 0.5
              AND (r.v1 - r.v0) / MAX(r.span_days, 0.25) >= ?
              {"AND v.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)"
               if not include_excluded else ""}
            ORDER BY views_per_day DESC
            LIMIT ?
        """
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                sql, (f"-{int(days)} days", int(min_views_per_day), int(limit) * 3),
            )]
        if faceless_only:
            rows = [r for r in rows if r.get("faceless")]
        return rows[: int(limit)]

    def save_face_checks(self, results: Iterable) -> int:
        now = utc_now_iso()
        count = 0
        with self.connect() as conn:
            for r in results:
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
                    """,
                    (
                        r.channel_id, int(r.thumbnails_checked), int(r.faces_found),
                        int(r.recurring_count), r.verdict, float(r.confidence), now,
                    ),
                )
                count += 1
        return count

    def channels_for_face_check(
        self,
        *,
        min_thumbnails: int = 3,
        limit: int | None = None,
        recheck: bool = False,
        max_thumbnails: int = 12,
        min_subs: int = 0,
    ) -> list[tuple[str, list[str]]]:
        """Channels with enough thumbnails to face-check, each with its URLs.
        min_subs>0 skips channels under that subscriber count — a no-scrape gate for
        tiny channels (they stay enriched/visible, just not spent on face-check)."""
        checked_clause = "" if recheck else "AND fc.channel_id IS NULL"
        sub_join = "JOIN channels chs ON chs.channel_id = e.channel_id" if min_subs > 0 else ""
        sub_clause = "  AND chs.subscribers >= ?" if min_subs > 0 else ""
        sql = [
            "SELECT e.channel_id",
            "FROM (",
            "  SELECT channel_id, COUNT(*) AS n FROM videos",
            "  WHERE COALESCE(thumbnail_url,'') != '' AND channel_id != ''",
            "  GROUP BY channel_id HAVING COUNT(*) >= ?",
            ") e",
            "LEFT JOIN channel_face_check fc ON fc.channel_id = e.channel_id",
            sub_join,
            f"WHERE 1=1 {checked_clause}",
            "  AND e.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)",
            sub_clause,
            "ORDER BY e.n DESC",
        ]
        params: list[object] = [int(min_thumbnails)]
        if min_subs > 0:
            params.append(int(min_subs))
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        with self.connect() as conn:
            channel_ids = [row[0] for row in conn.execute("\n".join(sql), params)]
            out: list[tuple[str, list[str]]] = []
            for cid in channel_ids:
                urls = [
                    row[0] for row in conn.execute(
                        "SELECT thumbnail_url FROM videos "
                        "WHERE channel_id=? AND COALESCE(thumbnail_url,'')!='' LIMIT ?",
                        (cid, int(max_thumbnails)),
                    )
                ]
                out.append((cid, urls))
        return out

    def export_uncertain_for_vlm(
        self,
        *,
        limit: int | None = None,
        max_thumbnails: int = 8,
    ) -> list[dict]:
        """Channels the CPU face-check left 'uncertain', with thumbnails for a VLM."""
        sql = [
            "SELECT fc.channel_id, ch.title",
            "FROM channel_face_check fc",
            "JOIN channels ch ON ch.channel_id = fc.channel_id",
            "LEFT JOIN channel_vlm_check vlm ON vlm.channel_id = fc.channel_id",
            "WHERE fc.verdict = 'uncertain' AND vlm.channel_id IS NULL",
            "  AND fc.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)",
            "ORDER BY fc.thumbnails_checked DESC",
        ]
        params: list[object] = []
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        out: list[dict] = []
        with self.connect() as conn:
            rows = conn.execute("\n".join(sql), params).fetchall()
            for r in rows:
                urls = [
                    u[0] for u in conn.execute(
                        "SELECT thumbnail_url FROM videos "
                        "WHERE channel_id=? AND COALESCE(thumbnail_url,'')!='' LIMIT ?",
                        (r["channel_id"], int(max_thumbnails)),
                    )
                ]
                out.append({
                    "channel_id": r["channel_id"],
                    "title": r["title"] or "",
                    "thumbnails": urls,
                })
        return out

    def count_uncertain_for_vlm(self) -> int:
        """How many 'uncertain' channels still await a VLM verdict (cheap gate)."""
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM channel_face_check fc "
                "LEFT JOIN channel_vlm_check vlm ON vlm.channel_id = fc.channel_id "
                "WHERE fc.verdict = 'uncertain' AND vlm.channel_id IS NULL "
                "AND fc.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)"
            ).fetchone()[0])

    def save_vlm_verdicts(self, verdicts: Iterable[dict], *, model: str = "") -> int:
        """Store VLM faceless verdicts (verdict in faceless/on_camera/unclear)."""
        now = utc_now_iso()
        count = 0
        with self.connect() as conn:
            for v in verdicts:
                cid = (v.get("channel_id") or "").strip()
                verdict = (v.get("verdict") or "unclear").strip().lower()
                if not cid or verdict not in {"faceless", "on_camera", "unclear"}:
                    continue
                conn.execute(
                    """
                    INSERT INTO channel_vlm_check (channel_id, verdict, model, raw, checked_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        verdict=excluded.verdict, model=excluded.model,
                        raw=excluded.raw, checked_at=excluded.checked_at
                    """,
                    (cid, verdict, v.get("model", model), str(v.get("raw", ""))[:500], now),
                )
                count += 1
        return count

    def face_check_breakdown(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                row[0]: int(row[1])
                for row in conn.execute(
                    "SELECT verdict, COUNT(*) FROM channel_face_check GROUP BY verdict"
                )
            }

    def category_group_breakdown(self) -> list[dict]:
        """Top-level category groups with channel counts and their raw members."""
        from beacon_engine.research.taxonomy import category_group

        groups: dict[str, dict] = {}
        for raw, count in self.category_breakdown(limit=1000):
            if not raw or raw == "(uncategorized)":
                continue
            grp = category_group(raw)
            entry = groups.setdefault(grp, {"group": grp, "count": 0, "categories": []})
            entry["count"] += int(count)
            entry["categories"].append(raw)
        return sorted(groups.values(), key=lambda g: g["count"], reverse=True)

    def ids_needing_video_stats(
        self,
        *,
        limit: int | None = None,
        channel_ids: Iterable[str] | None = None,
        max_attempts: int = 3,
    ) -> list[str]:
        """Video ids discovered but still missing real view counts.

        Never-attempted ids come first; ids already tried ``max_attempts`` times
        (e.g. deleted/private videos) are skipped so the backfill keeps moving
        forward instead of re-fetching dead ids forever.
        """
        clean = [c.strip() for c in (channel_ids or []) if c and c.strip()]
        sql = [
            "SELECT v.video_id",
            "FROM videos v",
            "LEFT JOIN enrichment_log e",
            "  ON e.entity_type = 'video' AND e.entity_id = v.video_id",
            "WHERE v.views = 0 AND COALESCE(e.attempts, 0) < ?",
        ]
        params: list[object] = [int(max_attempts)]
        if clean:
            sql.append("AND v.channel_id IN (%s)" % ",".join("?" for _ in clean))
            params.extend(clean)
        sql.append("ORDER BY COALESCE(e.attempts, 0) ASC, v.last_seen_at DESC")
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        with self.connect() as conn:
            return [r[0] for r in conn.execute("\n".join(sql), params)]

    def ids_needing_channel_stats(
        self,
        *,
        limit: int | None = None,
        max_attempts: int = 3,
    ) -> list[str]:
        """Channel ids still missing subscriber counts."""
        sql = [
            "SELECT c.channel_id",
            "FROM channels c",
            "LEFT JOIN enrichment_log e",
            "  ON e.entity_type = 'channel' AND e.entity_id = c.channel_id",
            "WHERE (c.subscribers = 0 OR c.subscribers IS NULL)",
            "  AND c.channel_id != '' AND COALESCE(e.attempts, 0) < ?",
            "ORDER BY COALESCE(e.attempts, 0) ASC, c.last_seen_at DESC",
        ]
        params: list[object] = [int(max_attempts)]
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        with self.connect() as conn:
            return [r[0] for r in conn.execute("\n".join(sql), params)]

    def backlink_channel_videos(self) -> int:
        """Set channel_id on videos that have none, from their channel-discovery
        records (a channel crawl records query=channel_id). Fixes videos pulled by
        listings that omit per-video channel ids, so they link to their channel."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE videos
                SET channel_id = (
                    SELECT d.query FROM video_discoveries d
                    WHERE d.video_id = videos.video_id
                      AND d.discovery_kind = 'channel' AND d.query LIKE 'UC%'
                    LIMIT 1
                )
                WHERE COALESCE(channel_id, '') = ''
                  AND video_id IN (
                    SELECT video_id FROM video_discoveries
                    WHERE discovery_kind = 'channel' AND query LIKE 'UC%'
                  )
                """
            )
            return cur.rowcount

    def add_channel_seeds(self, channel_ids: Iterable[str], source: str = "network") -> int:
        """Insert bare channel rows for newly discovered IDs so the enrich →
        catalog → face-check pipeline picks them up. Returns how many were NEW."""
        now = utc_now_iso()
        ids = [c for c in dict.fromkeys(channel_ids)
               if c and c.startswith("UC") and len(c) == 24]
        if not ids:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO channels
                    (channel_id, source, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO NOTHING
                """,
                [(cid, source, now, now) for cid in ids],
            )
            return conn.total_changes - before

    def exclude_channels(self, channel_ids: Iterable[str], reason: str = "") -> int:
        """Hide channels from every view/count (reversible — data is kept)."""
        now = utc_now_iso()
        ids = [c for c in channel_ids if c]
        if not ids:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO channel_exclusions (channel_id, reason, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET reason=excluded.reason
                """,
                [(cid, reason, now) for cid in ids],
            )
        return len(ids)

    def unexclude_channels(self, channel_ids: Iterable[str]) -> int:
        ids = [c for c in channel_ids if c]
        if not ids:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "DELETE FROM channel_exclusions WHERE channel_id = ?",
                [(cid,) for cid in ids],
            )
        return len(ids)

    def excluded_channel_ids(self) -> set[str]:
        with self.connect() as conn:
            return {r[0] for r in conn.execute(
                "SELECT channel_id FROM channel_exclusions").fetchall()}

    def count_exclusions(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM channel_exclusions").fetchone()[0])

    def channels_needing_catalog(
        self,
        *,
        min_thumbnails: int = 3,
        limit: int | None = None,
        max_attempts: int = 2,
        min_subs: int = 0,
    ) -> list[str]:
        """Channels with too few thumbnails to face-check, not recently cataloged.
        min_subs>0 skips channels under that subscriber count (no-scrape gate)."""
        sql = [
            "SELECT c.channel_id",
            "FROM channels c",
            "LEFT JOIN (",
            "  SELECT channel_id, COUNT(*) AS n FROM videos",
            "  WHERE COALESCE(thumbnail_url,'') != '' AND channel_id != ''",
            "  GROUP BY channel_id",
            ") v ON v.channel_id = c.channel_id",
            "LEFT JOIN enrichment_log e",
            "  ON e.entity_type = 'catalog' AND e.entity_id = c.channel_id",
            "WHERE c.channel_id != '' AND COALESCE(v.n, 0) < ?",
            "  AND COALESCE(e.attempts, 0) < ?",
            "  AND c.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)",
        ]
        params: list[object] = [int(min_thumbnails), int(max_attempts)]
        if min_subs > 0:
            sql.append("  AND c.subscribers >= ?")
            params.append(int(min_subs))
        sql.append("ORDER BY COALESCE(e.attempts, 0) ASC, c.last_seen_at DESC")
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        with self.connect() as conn:
            return [r[0] for r in conn.execute("\n".join(sql), params)]

    def known_channel_ids(self, *, limit: int | None = None) -> list[str]:
        """All indexed channels, freshest first — used to deep-catalog uploads."""
        sql = [
            "SELECT channel_id FROM channels",
            "WHERE channel_id != ''",
            "ORDER BY last_seen_at DESC",
        ]
        params: list[object] = []
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        with self.connect() as conn:
            return [r[0] for r in conn.execute("\n".join(sql), params)]

    def record_enrichment_attempt(
        self,
        entity_type: str,
        entity_ids: Iterable[str],
        *,
        status: str = "tried",
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO enrichment_log (
                    entity_type, entity_id, attempts, last_status, last_attempt_at
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                    attempts = enrichment_log.attempts + 1,
                    last_status = excluded.last_status,
                    last_attempt_at = excluded.last_attempt_at
                """,
                [
                    (entity_type, eid, status, now)
                    for eid in dict.fromkeys(e for e in entity_ids if e)
                ],
            )

    def faceless_summary(self) -> dict[str, int]:
        """Cheap COUNT of faceless channels + their videos (best signal wins:
        VLM > CPU face-check > text), excluding hidden (kids/language) channels.
        Replaces counting a fully-loaded 500K-row Python list."""
        faceless_channels_cte = """
            faceless_channels AS (
                SELECT ch.channel_id FROM channels ch
                LEFT JOIN channel_classifications cc ON cc.channel_id = ch.channel_id
                LEFT JOIN channel_face_check fc ON fc.channel_id = ch.channel_id
                LEFT JOIN channel_vlm_check vlm ON vlm.channel_id = ch.channel_id
                LEFT JOIN channel_text_model tm ON tm.channel_id = ch.channel_id
                WHERE ch.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)
                  AND (CASE
                        WHEN vlm.verdict = 'on_camera' THEN 0
                        WHEN vlm.verdict = 'faceless' THEN 1
                        WHEN fc.verdict = 'on_camera' THEN 0
                        WHEN fc.verdict = 'faceless' THEN 1
                        ELSE COALESCE(tm.faceless, cc.likely_faceless, 0) END) = 1
            )
        """
        with self.connect() as conn:
            ch = int(conn.execute(
                f"WITH {faceless_channels_cte} SELECT COUNT(*) FROM faceless_channels"
            ).fetchone()[0])
            vid = int(conn.execute(
                f"WITH {faceless_channels_cte} "
                "SELECT COUNT(*) FROM videos "
                "WHERE channel_id IN (SELECT channel_id FROM faceless_channels)"
            ).fetchone()[0])
        return {"faceless_channels": ch, "faceless_videos": vid}

    def video_metadata_rows(
        self,
        channel_ids: Iterable[str] | None = None,
        *,
        lite: bool = False,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Return videos enriched with discovery and observed-growth metadata.

        `lite=True` skips the two expensive aggregations (a GROUP BY over the
        whole 1M-row video_discoveries table + window functions over every
        snapshot). Those feed only the velocity/discovery-count sort modes and a
        few card extras; the finder counts, the outlier ranking, and the default
        views/recency sorts don't need them. On a large index this turns a
        3-minute full scan into a couple of seconds — the columns are still
        present, just defaulted, so no caller breaks."""
        clean_channel_ids = list(dict.fromkeys(
            channel_id.strip()
            for channel_id in (channel_ids or [])
            if channel_id and channel_id.strip()
        ))
        if lite:
            where = ""
            params: list[object] = []
            if clean_channel_ids:
                where = "WHERE channel_id IN (%s)" % ",".join("?" for _ in clean_channel_ids)
                params = list(clean_channel_ids)
            tail = ""
            if limit:
                # Most-recent window: the finder browses fresh videos, and the
                # last_seen index makes this near-instant vs a 500K-row full load.
                tail = "ORDER BY last_seen_at DESC LIMIT ?"
                params = params + [int(limit)]
            with self.connect() as conn:
                # Explicit lean columns: NEVER select raw_json / description here —
                # those are large per-row blobs (full API payloads) that, across
                # 400K+ videos, are gigabytes of IO the finder never displays.
                # That blob read — not the aggregation — was the real slow load.
                return list(conn.execute(
                    f"""
                    SELECT
                        v.video_id, v.title, v.channel, v.channel_id, v.views,
                        v.published_at, v.published_at_normalized, v.url,
                        v.duration_seconds, v.thumbnail_url, v.source,
                        v.first_seen_at, v.last_seen_at,
                        0 AS discovery_count, 0 AS discovery_query_count,
                        0 AS discovery_provider_count, 0 AS best_search_position,
                        v.first_seen_at AS first_discovered_at,
                        v.first_seen_at AS last_discovered_at,
                        0 AS snapshot_count, 0 AS first_snapshot_views,
                        0 AS latest_snapshot_views, '' AS first_snapshot_at,
                        '' AS latest_snapshot_at, 0 AS observed_views_gained,
                        0.0 AS observed_hours, 0.0 AS observed_views_per_hour,
                        0 AS velocity_reliable
                    FROM videos v {where} {tail}
                    """,
                    params,
                ))
        target_cte = ""
        video_source = "videos"
        discovery_target_join = ""
        snapshot_target_join = ""
        params: list[object] = []
        if clean_channel_ids:
            placeholders = ",".join("?" for _ in clean_channel_ids)
            target_cte = f"""
                target_videos AS (
                    -- explicit columns: NEVER pull raw_json / description here — those
                    -- are large per-row blobs the feed never displays, and reading them
                    -- across thousands of videos is the bulk of the query's IO/time.
                    SELECT
                        video_id, title, channel, channel_id, views,
                        published_at, published_at_normalized, url,
                        duration_seconds, thumbnail_url, source,
                        first_seen_at, last_seen_at
                    FROM videos
                    WHERE channel_id IN ({placeholders})
                ),
            """
            video_source = "target_videos"
            discovery_target_join = (
                "JOIN target_videos target ON target.video_id = d.video_id"
            )
            snapshot_target_join = (
                "JOIN target_videos target ON target.video_id = s.video_id"
            )
            params.extend(clean_channel_ids)
        with self.connect() as conn:
            return list(conn.execute(
                f"""
                WITH {target_cte}
                discovery AS (
                    SELECT
                        d.video_id,
                        COUNT(*) AS discovery_count,
                        COUNT(DISTINCT d.query) AS discovery_query_count,
                        COUNT(DISTINCT d.provider) AS discovery_provider_count,
                        MIN(d.position) AS best_search_position,
                        MIN(d.discovered_at) AS first_discovered_at,
                        MAX(d.discovered_at) AS last_discovered_at
                    FROM video_discoveries d
                    {discovery_target_join}
                    GROUP BY d.video_id
                ),
                snapshot_ranked AS (
                    SELECT
                        s.video_id,
                        s.views,
                        s.captured_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.video_id
                            ORDER BY s.captured_at ASC, s.id ASC
                        ) AS first_rank,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.video_id
                            ORDER BY s.captured_at DESC, s.id DESC
                        ) AS last_rank,
                        COUNT(*) OVER (PARTITION BY s.video_id) AS snapshot_count
                    FROM video_snapshots s
                    {snapshot_target_join}
                ),
                snapshot AS (
                    SELECT
                        video_id,
                        MAX(snapshot_count) AS snapshot_count,
                        MAX(CASE WHEN first_rank = 1 THEN views END) AS first_snapshot_views,
                        MAX(CASE WHEN first_rank = 1 THEN captured_at END) AS first_snapshot_at,
                        MAX(CASE WHEN last_rank = 1 THEN views END) AS latest_snapshot_views,
                        MAX(CASE WHEN last_rank = 1 THEN captured_at END) AS latest_snapshot_at
                    FROM snapshot_ranked
                    GROUP BY video_id
                ),
                enriched AS (
                    SELECT
                        -- explicit non-blob columns (skip raw_json/description) so the
                        -- feed never pays to read the big per-row payloads.
                        v.video_id, v.title, v.channel, v.channel_id, v.views,
                        v.published_at, v.published_at_normalized, v.url,
                        v.duration_seconds, v.thumbnail_url, v.source,
                        v.first_seen_at, v.last_seen_at,
                        COALESCE(d.discovery_count, 0) AS discovery_count,
                        COALESCE(d.discovery_query_count, 0) AS discovery_query_count,
                        COALESCE(d.discovery_provider_count, 0) AS discovery_provider_count,
                        COALESCE(d.best_search_position, 0) AS best_search_position,
                        COALESCE(d.first_discovered_at, v.first_seen_at) AS first_discovered_at,
                        COALESCE(d.last_discovered_at, v.first_seen_at) AS last_discovered_at,
                        COALESCE(s.snapshot_count, 0) AS snapshot_count,
                        COALESCE(s.first_snapshot_views, 0) AS first_snapshot_views,
                        COALESCE(s.latest_snapshot_views, 0) AS latest_snapshot_views,
                        COALESCE(s.first_snapshot_at, '') AS first_snapshot_at,
                        COALESCE(s.latest_snapshot_at, '') AS latest_snapshot_at,
                        MAX(
                            COALESCE(s.latest_snapshot_views, 0)
                            - COALESCE(s.first_snapshot_views, 0),
                            0
                        ) AS observed_views_gained,
                        MAX(
                            (julianday(s.latest_snapshot_at) - julianday(s.first_snapshot_at))
                            * 24.0,
                            0.0
                        ) AS observed_hours
                    FROM {video_source} v
                    LEFT JOIN discovery d ON d.video_id = v.video_id
                    LEFT JOIN snapshot s ON s.video_id = v.video_id
                )
                SELECT
                    enriched.*,
                    CASE
                        WHEN observed_hours > 0
                        THEN observed_views_gained / observed_hours
                        ELSE 0.0
                    END AS observed_views_per_hour,
                    CASE
                        WHEN snapshot_count >= 2
                         AND observed_hours >= 1.0
                         AND latest_snapshot_views >= first_snapshot_views
                        THEN 1
                        ELSE 0
                    END AS velocity_reliable
                FROM enriched
                """,
                params,
            ))

    def outlier_score_trends(self) -> dict[str, dict]:
        """Return the latest and previous stored outlier score for each video.

        Cached for 10 min: the underlying window scan over the whole snapshot
        history is ~12s, but scores drift slowly and the finder re-reads this on
        every page load + live refresh, so a fresh scan each time is wasteful."""
        key = self.cache_identity
        cache = getattr(YouTubeStore, "_trends_cache", None)
        if cache and _time.time() - cache[0] < 600 and cache[1] == key:
            return cache[2]
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        video_id,
                        outlier_score,
                        captured_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY video_id
                            ORDER BY captured_at DESC, id DESC
                        ) AS rank
                    FROM outlier_snapshots
                )
                SELECT
                    video_id,
                    MAX(CASE WHEN rank = 1 THEN outlier_score END) AS latest_score,
                    MAX(CASE WHEN rank = 2 THEN outlier_score END) AS previous_score,
                    MAX(CASE WHEN rank = 1 THEN captured_at END) AS latest_score_at,
                    COUNT(*) AS score_snapshot_count
                FROM ranked
                GROUP BY video_id
                """
            ).fetchall()
        result = {
            row["video_id"]: {
                "latest_score": float(row["latest_score"] or 0),
                "previous_score": float(row["previous_score"] or 0),
                "score_change": (
                    float(row["latest_score"] or 0)
                    - float(row["previous_score"] or row["latest_score"] or 0)
                ),
                "latest_score_at": row["latest_score_at"] or "",
                "score_snapshot_count": int(row["score_snapshot_count"] or 0),
            }
            for row in rows
        }
        YouTubeStore._trends_cache = (_time.time(), key, result)
        return result

    def summary(self) -> dict[str, int]:
        with self.connect() as conn:
            names = [
                "channels",
                "videos",
                "video_snapshots",
                "comments",
                "transcripts",
                "search_results",
                "video_discoveries",
                "channel_classifications",
                "niches",
                "niche_channels",
                "channel_snapshots",
                "saved_views",
                "alerts",
                "background_jobs",
                "research_findings",
                "idea_runs",
                "outlier_snapshots",
                "crawl_runs",
            ]
            return {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in names
            }

    def start_run(self, *, kind: str, query: str = "", provider: str = "") -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cur = self.insert_id(
                conn,
                """
                INSERT INTO crawl_runs (kind, query, provider, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (kind, query, provider, now),
            )
            return cur

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        message: str = "",
        counts: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crawl_runs
                SET status=?, ended_at=?, message=?, counts_json=?
                WHERE id=?
                """,
                (status, utc_now_iso(), message, _json(counts or {}), run_id),
            )

    def _init(self) -> None:
        with self.connect() as conn:
            # Already migrated to this version -> nothing to do. This keeps store
            # construction at ~microseconds instead of re-running migrations.
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    handle TEXT DEFAULT '',
                    subscribers INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    view_count INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    channel TEXT DEFAULT '',
                    channel_id TEXT DEFAULT '',
                    views INTEGER DEFAULT 0,
                    published_at TEXT DEFAULT '',
                    published_at_normalized TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    duration_seconds INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    thumbnail_url TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
                CREATE INDEX IF NOT EXISTS idx_videos_views ON videos(views DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_seen ON videos(last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS video_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    views INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    FOREIGN KEY(video_id) REFERENCES videos(video_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_video ON video_snapshots(video_id, captured_at);

                CREATE TABLE IF NOT EXISTS outlier_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    outlier_score REAL NOT NULL,
                    baseline_views INTEGER NOT NULL,
                    views INTEGER NOT NULL,
                    age_days INTEGER NOT NULL,
                    views_per_day REAL NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    FOREIGN KEY(video_id) REFERENCES videos(video_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_outlier_snapshots_video
                    ON outlier_snapshots(video_id, captured_at);
                CREATE INDEX IF NOT EXISTS idx_outlier_snapshots_score
                    ON outlier_snapshots(outlier_score DESC);

                CREATE TABLE IF NOT EXISTS comments (
                    comment_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    author TEXT DEFAULT '',
                    like_count INTEGER DEFAULT 0,
                    published_at TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);
                CREATE INDEX IF NOT EXISTS idx_comments_likes ON comments(like_count DESC);

                CREATE TABLE IF NOT EXISTS transcripts (
                    video_id TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    generated INTEGER,
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(video_id, language)
                );

                CREATE TABLE IF NOT EXISTS search_results (
                    query TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(query, provider, video_id)
                );

                CREATE INDEX IF NOT EXISTS idx_search_query ON search_results(query, provider, position);

                CREATE TABLE IF NOT EXISTS video_discoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    query TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    discovery_kind TEXT NOT NULL DEFAULT 'search',
                    position INTEGER NOT NULL DEFAULT 0,
                    run_id INTEGER,
                    discovered_at TEXT NOT NULL,
                    UNIQUE(
                        video_id, query, provider, discovery_kind, discovered_at
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_discoveries_video
                    ON video_discoveries(video_id, discovered_at DESC);
                CREATE INDEX IF NOT EXISTS idx_discoveries_query
                    ON video_discoveries(query, discovery_kind, discovered_at DESC);

                CREATE TABLE IF NOT EXISTS channel_classifications (
                    channel_id TEXT PRIMARY KEY,
                    likely_faceless INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    positive_title_count INTEGER NOT NULL DEFAULT 0,
                    negative_title_count INTEGER NOT NULL DEFAULT 0,
                    positives_json TEXT DEFAULT '[]',
                    negatives_json TEXT DEFAULT '[]',
                    classifier TEXT NOT NULL DEFAULT 'text-v2',
                    classified_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_channel_classifications_faceless
                    ON channel_classifications(
                        likely_faceless, confidence DESC, score DESC
                    );

                CREATE TABLE IF NOT EXISTS niches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS niche_channels (
                    niche_id INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'competitor',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source TEXT DEFAULT 'manual',
                    note TEXT DEFAULT '',
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(niche_id, channel_id),
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_niche_channels_channel
                    ON niche_channels(channel_id, enabled);

                CREATE TABLE IF NOT EXISTS channel_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    subscribers INTEGER NOT NULL DEFAULT 0,
                    total_views INTEGER NOT NULL DEFAULT 0,
                    video_count INTEGER NOT NULL DEFAULT 0,
                    average_views REAL NOT NULL DEFAULT 0,
                    median_views REAL NOT NULL DEFAULT 0,
                    upload_cadence_days REAL NOT NULL DEFAULT 0,
                    captured_at TEXT NOT NULL,
                    source TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_channel_snapshots_channel
                    ON channel_snapshots(channel_id, captured_at DESC);

                CREATE TABLE IF NOT EXISTS saved_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    niche_id INTEGER NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    entity_type TEXT NOT NULL DEFAULT 'feed',
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(niche_id, name),
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    niche_id INTEGER NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    title TEXT NOT NULL,
                    message TEXT DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    fingerprint TEXT NOT NULL UNIQUE,
                    detected_at TEXT NOT NULL,
                    read_at TEXT,
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_niche
                    ON alerts(niche_id, read_at, detected_at DESC);

                CREATE TABLE IF NOT EXISTS background_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    scope TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    schedule TEXT DEFAULT '',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_background_jobs_status
                    ON background_jobs(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS research_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    niche_id INTEGER NOT NULL,
                    finding_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    explanation TEXT DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_findings_niche
                    ON research_findings(niche_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS idea_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    niche_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    ideas_json TEXT NOT NULL DEFAULT '[]',
                    model TEXT DEFAULT '',
                    cost REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_idea_runs_niche
                    ON idea_runs(niche_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS crawl_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    query TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    message TEXT DEFAULT '',
                    counts_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS enrichment_log (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT DEFAULT '',
                    last_attempt_at TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_id)
                );

                CREATE TABLE IF NOT EXISTS channel_face_check (
                    channel_id TEXT PRIMARY KEY,
                    thumbnails_checked INTEGER NOT NULL DEFAULT 0,
                    faces_found INTEGER NOT NULL DEFAULT 0,
                    recurring_count INTEGER NOT NULL DEFAULT 0,
                    verdict TEXT NOT NULL DEFAULT 'unknown',
                    confidence REAL NOT NULL DEFAULT 0,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_vlm_check (
                    channel_id TEXT PRIMARY KEY,
                    verdict TEXT NOT NULL DEFAULT 'unclear',
                    model TEXT DEFAULT '',
                    raw TEXT DEFAULT '',
                    checked_at TEXT NOT NULL
                );

                -- Fast, vision-free faceless prediction from a text model
                -- (TF-IDF + logistic regression over channel title/description +
                -- video titles), trained on the face-check/VLM labels. Used as the
                -- verdict fallback when no vision check exists. See faceless_text_model.py.
                CREATE TABLE IF NOT EXISTS channel_text_model (
                    channel_id TEXT PRIMARY KEY,
                    faceless INTEGER NOT NULL DEFAULT 0,
                    proba REAL NOT NULL DEFAULT 0,
                    model_version TEXT DEFAULT '',
                    scored_at TEXT NOT NULL
                );

                -- Channels hidden from every view/count (e.g. kids/cartoon content
                -- that is technically faceless but off-niche). Reversible: delete the
                -- row to bring a channel back. Data is never destroyed.
                CREATE TABLE IF NOT EXISTS channel_exclusions (
                    channel_id TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            _ensure_column(
                conn,
                "videos",
                "published_at_normalized",
                "TEXT DEFAULT ''",
            )
            # YouTube video category (snippet.categoryId, e.g. '10' = Music).
            # Backfilled free from raw_json for official-API videos, then fetched
            # via the API-key pool for the rest (see research/categorize.py).
            # 'na' = fetched but the API had no category (deleted/private video).
            _ensure_column(conn, "videos", "category_id", "TEXT DEFAULT ''")
            # Channel category/niche label, assigned from the search queries that
            # keep surfacing the channel (see channel_dominant_queries).
            _ensure_column(conn, "channels", "category", "TEXT DEFAULT ''")
            # YouTube channel creation date -> "days since start".
            _ensure_column(conn, "channels", "channel_published_at", "TEXT DEFAULT ''")
            # Clean human-readable TOPIC niche (History/Space/True Crime/...), assigned
            # by research/topic_classifier.py from category+title+description. Every
            # channel gets one ('Other' if nothing matches). Backfilled + kept fresh
            # each radar cycle; used by the Channels-tab topic filter.
            _ensure_column(conn, "channels", "topic", "TEXT DEFAULT ''")
            # Per-niche opt-out of autonomous radar work (snapshots, alerts,
            # competitor refresh). 1 = the agent keeps this niche fresh on its own.
            _ensure_column(conn, "niches", "autonomous", "INTEGER DEFAULT 1")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_channels_category
                ON channels(category)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_channels_topic
                ON channels(topic)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_published_normalized
                ON videos(published_at_normalized DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_category
                ON videos(category_id)
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO video_discoveries (
                    video_id, query, provider, discovery_kind, position,
                    run_id, discovered_at
                )
                SELECT
                    video_id, query, provider, 'search', position,
                    NULL, captured_at
                FROM search_results
                """
            )
            rows = conn.execute(
                """
                SELECT video_id, published_at, first_seen_at
                FROM videos
                WHERE published_at != ''
                  AND COALESCE(published_at_normalized, '') = ''
                """
            ).fetchall()
            conn.executemany(
                """
                UPDATE videos
                SET published_at_normalized = ?
                WHERE video_id = ?
                """,
                [
                    (
                        normalize_published_at(
                            row["published_at"],
                            reference=row["first_seen_at"],
                        ),
                        row["video_id"],
                    )
                    for row in rows
                ],
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _json(data: object) -> str:
    try:
        return json.dumps(data, ensure_ascii=True, default=str)
    except TypeError:
        return "{}"


def _comment_key(video_id: str, text: str) -> str:
    return sha1(f"{video_id}:{text}".encode("utf-8", errors="ignore")).hexdigest()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
