"""Niche workspaces and tracked-channel feeds over the shared YouTube index."""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Iterable

from beacon_engine.research.faceless_filter import (
    classify_store_channels,
    load_store_classifications,
)
from beacon_engine.research.outliers import find_outliers
from beacon_engine.research.providers.base import (
    normalize_channel_id,
    normalize_published_at,
    utc_now_iso,
)
from beacon_engine.research.store import YouTubeStore

_ROLE_VALUES = {"competitor", "inspiration", "owned"}
_FORMAT_VALUES = {"all", "long", "short"}
_SORT_VALUES = {"newest", "views", "velocity", "outlier"}

DEFAULT_NICHE_BLUEPRINTS = (
    {
        "name": "Business & Company Stories",
        "description": "Business documentaries, company breakdowns, finance, and industry stories.",
        "queries": (
            "business documentaries explained",
            "technology company rise and fall",
            "finance scams explained",
        ),
    },
    {
        "name": "History & Ancient Mysteries",
        "description": "History documentaries, lost civilizations, and unexplained discoveries.",
        "queries": (
            "history mysteries documentary",
            "ancient civilizations documentary",
        ),
    },
    {
        "name": "Mysteries & Dark Stories",
        "description": "Unsolved mysteries, scary stories, true crime, and strange events.",
        "queries": (
            "scary stories explained",
            "true crime documentary explained",
            "ocean mysteries documentary",
            "aviation mysteries explained",
        ),
    },
    {
        "name": "Science, Space & Engineering",
        "description": "Science discoveries, space mysteries, engineering, and disaster explainers.",
        "queries": (
            "science discoveries explained",
            "space mysteries explained",
            "engineering disasters documentary",
        ),
    },
    {
        "name": "Geopolitics & World Affairs",
        "description": "Geopolitics, geography, global conflicts, and country explainers.",
        "queries": (
            "geopolitics explained",
            "geography explained documentary",
        ),
    },
    {
        "name": "Celebrity & Culture",
        "description": "Celebrity careers, cultural stories, and rise-and-fall documentaries.",
        "queries": (
            "celebrity rise and fall",
            "celebrity rise and fall documentary",
        ),
    },
    {
        "name": "Food & Consumer Investigations",
        "description": "Food brands, consumer products, ingredient tests, and buying warnings.",
        "queries": (
            "food brands exposed",
            "consumer products investigated",
            "stop buying these food brands ranked",
            "worst grocery store brands ingredients exposed",
        ),
    },
)


class NicheWorkspaceService:
    """CRUD and read models for global niche workspaces."""

    def __init__(self, store: YouTubeStore) -> None:
        self.store = store

    def create_workspace(self, name: str, description: str = "") -> int:
        clean_name = _required_name(name)
        now = utc_now_iso()
        with self.store.connect() as conn:
            cur = self.store.insert_id(
                conn,
                """
                INSERT INTO niches (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_name, description.strip(), now, now),
            )
            return cur

    def find_workspace_by_name(self, name: str) -> int | None:
        clean_name = _required_name(name)
        with self.store.connect() as conn:
            row = conn.execute(
                f"SELECT id FROM niches WHERE {self.store.casefold('name')}={self.store.casefold('?')}",
                (clean_name,),
            ).fetchone()
        return int(row["id"]) if row else None

    def get_or_create_workspace(self, name: str, description: str = "") -> int:
        """Return the id of the niche named `name`, creating it if absent.

        `niches.name` is UNIQUE, so this is the safe way to attach a session's
        scraped competitors to a stable per-niche workspace."""
        existing = self.find_workspace_by_name(name)
        return existing if existing is not None else self.create_workspace(name, description)

    def rename_workspace(
        self,
        niche_id: int,
        name: str,
        description: str | None = None,
    ) -> None:
        clean_name = _required_name(name)
        with self.store.connect() as conn:
            if description is None:
                cur = conn.execute(
                    """
                    UPDATE niches
                    SET name=?, updated_at=?
                    WHERE id=?
                    """,
                    (clean_name, utc_now_iso(), int(niche_id)),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE niches
                    SET name=?, description=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        clean_name,
                        description.strip(),
                        utc_now_iso(),
                        int(niche_id),
                    ),
                )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown niche workspace: {niche_id}")

    def set_autonomous(self, niche_id: int, enabled: bool) -> None:
        """Opt a niche in/out of autonomous radar upkeep (snapshots, alerts,
        competitor refresh). Off = the agent leaves it alone until reopened."""
        with self.store.connect() as conn:
            cur = conn.execute(
                "UPDATE niches SET autonomous=?, updated_at=? WHERE id=?",
                (int(bool(enabled)), utc_now_iso(), int(niche_id)),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown niche workspace: {niche_id}")

    def delete_workspace(self, niche_id: int) -> None:
        with self.store.connect() as conn:
            conn.execute("DELETE FROM niches WHERE id=?", (int(niche_id),))

    def list_workspaces(self) -> list[dict]:
        with self.store.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""
                    WITH tracked_channels AS (
                        SELECT DISTINCT channel_id
                        FROM niche_channels
                    ),
                    video_stats AS (
                        SELECT
                            v.channel_id,
                            COUNT(*) AS video_count,
                            MAX(v.last_seen_at) AS last_activity_at
                        FROM videos v
                        JOIN tracked_channels tracked
                            ON tracked.channel_id = v.channel_id
                        GROUP BY v.channel_id
                    )
                    SELECT
                        n.id,
                        n.name,
                        n.description,
                        COALESCE(n.autonomous, 1) AS autonomous,
                        n.created_at,
                        n.updated_at,
                        COUNT(nc.channel_id) AS channel_count,
                        SUM(CASE WHEN nc.enabled = 1 THEN 1 ELSE 0 END)
                            AS enabled_channel_count,
                        COALESCE(SUM(
                            CASE WHEN nc.enabled = 1 THEN vs.video_count ELSE 0 END
                        ), 0) AS video_count,
                        MAX(CASE
                            WHEN nc.enabled = 1 THEN vs.last_activity_at
                            ELSE ''
                        END) AS last_activity_at
                    FROM niches n
                    LEFT JOIN niche_channels nc ON nc.niche_id = n.id
                    LEFT JOIN video_stats vs ON vs.channel_id = nc.channel_id
                    GROUP BY n.id
                    ORDER BY
                        COALESCE(MAX(CASE WHEN nc.enabled = 1
                                          THEN vs.last_activity_at ELSE '' END),
                                 n.updated_at) DESC,
                        {self.store.casefold('n.name')}
                    """
                )
            ]

    def get_workspace(self, niche_id: int) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                """
                WITH tracked AS (
                    SELECT channel_id, enabled
                    FROM niche_channels
                    WHERE niche_id = ?
                ),
                video_stats AS (
                    SELECT
                        v.channel_id,
                        COUNT(*) AS video_count,
                        MAX(v.last_seen_at) AS last_activity_at
                    FROM videos v
                    JOIN tracked t ON t.channel_id = v.channel_id
                    GROUP BY v.channel_id
                )
                SELECT
                    n.id,
                    n.name,
                    n.description,
                    COALESCE(n.autonomous, 1) AS autonomous,
                    n.created_at,
                    n.updated_at,
                    COUNT(t.channel_id) AS channel_count,
                    SUM(CASE WHEN t.enabled = 1 THEN 1 ELSE 0 END)
                        AS enabled_channel_count,
                    COALESCE(SUM(
                        CASE WHEN t.enabled = 1 THEN vs.video_count ELSE 0 END
                    ), 0) AS video_count,
                    MAX(CASE
                        WHEN t.enabled = 1 THEN vs.last_activity_at
                        ELSE ''
                    END) AS last_activity_at
                FROM niches n
                LEFT JOIN tracked t ON 1 = 1
                LEFT JOIN video_stats vs ON vs.channel_id = t.channel_id
                WHERE n.id = ?
                GROUP BY n.id
                """,
                (int(niche_id), int(niche_id)),
            ).fetchone()
        return dict(row) if row else None

    def bootstrap_default_workspaces(
        self,
        *,
        max_channels_per_niche: int = 12,
        faceless_mode: str = "balanced",
    ) -> dict[str, int]:
        """Create and refresh broad starter niches from indexed discoveries."""
        mode = (faceless_mode or "balanced").strip().lower()
        signals = load_store_classifications(self.store)
        if not signals:
            # Fresh/small indexes may not have run the classifier yet. Keep the
            # bootstrap self-contained there, but never rescore a populated
            # production index just to refresh seven starter workspaces.
            signals = classify_store_channels(
                self.store,
                mode=mode,
                only_missing=True,
            )
        existing = {
            str(item["name"]).casefold(): int(item["id"])
            for item in self.list_workspaces()
        }
        created = 0
        added = 0
        for blueprint in DEFAULT_NICHE_BLUEPRINTS:
            name = str(blueprint["name"])
            niche_id = existing.get(name.casefold())
            if niche_id is None:
                niche_id = self.create_workspace(
                    name,
                    str(blueprint["description"]),
                )
                existing[name.casefold()] = niche_id
                created += 1
            candidates = self._discovered_channels_for_queries(
                blueprint["queries"],
                signals=signals,
                faceless_mode=mode,
                limit=max_channels_per_niche,
            )
            added += self.add_channels(
                niche_id,
                [item["channel_id"] for item in candidates],
                source="radar-bootstrap",
            )
        return {"created": created, "added": added}

    def _discovered_channels_for_queries(
        self,
        queries: Iterable[str],
        *,
        signals: dict,
        faceless_mode: str,
        limit: int,
    ) -> list[dict]:
        clean_queries = tuple(
            dict.fromkeys(str(query).strip().lower() for query in queries if str(query).strip())
        )
        if not clean_queries:
            return []
        placeholders = ", ".join("?" for _ in clean_queries)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    v.channel_id,
                    COALESCE(NULLIF(MAX(c.title), ''), MAX(v.channel)) AS title,
                    COUNT(DISTINCT v.video_id) AS indexed_videos,
                    COUNT(DISTINCT d.id) AS discovery_count,
                    MAX(v.views) AS best_views,
                    MAX(d.discovered_at) AS last_discovered_at
                FROM video_discoveries d
                JOIN videos v ON v.video_id = d.video_id
                LEFT JOIN channels c ON c.channel_id = v.channel_id
                WHERE lower(d.query) IN ({placeholders})
                  AND v.channel_id != ''
                GROUP BY v.channel_id
                ORDER BY
                    discovery_count DESC,
                    indexed_videos DESC,
                    best_views DESC
                """,
                clean_queries,
            ).fetchall()
        candidates = []
        for raw in rows:
            signal = signals.get(raw["channel_id"])
            if not signal or not signal.matches(faceless_mode):
                continue
            item = dict(raw)
            item["faceless_confidence"] = float(signal.confidence)
            item["faceless_score"] = float(signal.score)
            candidates.append(item)
        candidates.sort(
            key=lambda item: (
                item["faceless_confidence"],
                item["faceless_score"],
                int(item["indexed_videos"] or 0),
                int(item["best_views"] or 0),
            ),
            reverse=True,
        )
        return candidates[: max(int(limit), 0)]

    def resolve_channel(self, value: str) -> dict | None:
        query = (value or "").strip()
        if not query:
            return None
        channel_id = normalize_channel_id(query)
        handle = ""
        if not channel_id:
            match = re.search(r"(?:youtube\.com/)?@([A-Za-z0-9._-]+)", query)
            handle = f"@{match.group(1)}" if match else query
        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM channels
                WHERE channel_id = ?
                   OR (? != '' AND lower(handle) = lower(?))
                   OR (? != '' AND lower(title) = lower(?))
                   OR (? != '' AND lower(url) = lower(?))
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (
                    channel_id or query,
                    handle,
                    handle,
                    query,
                    query,
                    query,
                    query,
                ),
            ).fetchone()
            if row:
                return dict(row)
            row = conn.execute(
                """
                SELECT
                    channel_id,
                    channel AS title,
                    '' AS handle,
                    0 AS subscribers,
                    COUNT(*) AS video_count,
                    SUM(views) AS view_count,
                    '' AS description,
                    'https://www.youtube.com/channel/' || channel_id AS url,
                    MAX(source) AS source,
                    '{}' AS raw_json,
                    MIN(first_seen_at) AS first_seen_at,
                    MAX(last_seen_at) AS last_seen_at
                FROM videos
                WHERE channel_id = ?
                   OR lower(channel) = lower(?)
                GROUP BY channel_id, channel
                LIMIT 1
                """,
                (channel_id or query, query),
            ).fetchone()
        return dict(row) if row else None

    def add_channel(
        self,
        niche_id: int,
        channel_id: str,
        *,
        role: str = "competitor",
        source: str = "manual",
        note: str = "",
    ) -> bool:
        clean_role = role.strip().lower()
        if clean_role not in _ROLE_VALUES:
            raise ValueError(f"Unsupported channel role: {role}")
        resolved = self.resolve_channel(channel_id)
        if not resolved:
            raise KeyError(f"Channel is not present in the local index: {channel_id}")
        now = utc_now_iso()
        with self.store.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO niche_channels (
                    niche_id, channel_id, role, enabled, source, note, added_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(niche_id, channel_id) DO UPDATE SET
                    role=excluded.role,
                    enabled=1,
                    source=excluded.source,
                    note=CASE
                        WHEN excluded.note != '' THEN excluded.note
                        ELSE niche_channels.note
                    END
                """,
                (
                    int(niche_id),
                    resolved["channel_id"],
                    clean_role,
                    source.strip() or "manual",
                    note.strip(),
                    now,
                ),
            )
            conn.execute(
                "UPDATE niches SET updated_at=? WHERE id=?",
                (now, int(niche_id)),
            )
            return cur.rowcount > 0

    def remove_channel(self, niche_id: int, channel_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "DELETE FROM niche_channels WHERE niche_id=? AND channel_id=?",
                (int(niche_id), channel_id),
            )
            conn.execute(
                "UPDATE niches SET updated_at=? WHERE id=?",
                (utc_now_iso(), int(niche_id)),
            )

    def update_channel(
        self,
        niche_id: int,
        channel_id: str,
        *,
        role: str | None = None,
        enabled: bool | None = None,
        note: str | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[object] = []
        if role is not None:
            clean_role = role.strip().lower()
            if clean_role not in _ROLE_VALUES:
                raise ValueError(f"Unsupported channel role: {role}")
            updates.append("role=?")
            params.append(clean_role)
        if enabled is not None:
            updates.append("enabled=?")
            params.append(int(enabled))
        if note is not None:
            updates.append("note=?")
            params.append(note.strip())
        if not updates:
            return
        params.extend([int(niche_id), channel_id])
        with self.store.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE niche_channels
                SET {", ".join(updates)}
                WHERE niche_id=? AND channel_id=?
                """,
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(f"Channel is not tracked in workspace: {channel_id}")
            conn.execute(
                "UPDATE niches SET updated_at=? WHERE id=?",
                (utc_now_iso(), int(niche_id)),
            )

    def workspace_channels(self, niche_id: int) -> list[dict]:
        with self.store.connect() as conn:
            tracked = conn.execute(
                f"""
                WITH tracked_channels AS (
                    SELECT *
                    FROM niche_channels
                    WHERE niche_id = ?
                ),
                video_stats AS (
                    SELECT
                        v.channel_id,
                        COUNT(*) AS indexed_videos,
                        SUM(v.views) AS total_indexed_views,
                        AVG(v.views) AS average_views,
                        MAX(v.views) AS best_views,
                        MAX(
                            COALESCE(
                                NULLIF(v.published_at_normalized, ''),
                                v.first_seen_at
                            )
                        ) AS last_video_at
                    FROM videos v
                    JOIN tracked_channels tracked
                        ON tracked.channel_id = v.channel_id
                    GROUP BY v.channel_id
                )
                SELECT
                    nc.*,
                    COALESCE(NULLIF(c.title, ''), nc.channel_id) AS title,
                    COALESCE(c.handle, '') AS handle,
                    COALESCE(c.subscribers, 0) AS subscribers,
                    COALESCE(c.url, '') AS channel_url,
                    COALESCE(cc.likely_faceless, 0) AS likely_faceless,
                    COALESCE(cc.confidence, 0) AS faceless_confidence,
                    COALESCE(cc.score, 0) AS faceless_score,
                    COALESCE(vs.indexed_videos, 0) AS indexed_videos,
                    COALESCE(vs.total_indexed_views, 0) AS total_indexed_views,
                    CAST(TRUNC(COALESCE(vs.average_views, 0)) AS BIGINT)
                        AS average_views,
                    COALESCE(vs.best_views, 0) AS best_views,
                    COALESCE(vs.last_video_at, '') AS last_video_at
                FROM tracked_channels nc
                LEFT JOIN channels c ON c.channel_id = nc.channel_id
                LEFT JOIN channel_classifications cc
                    ON cc.channel_id = nc.channel_id
                LEFT JOIN video_stats vs ON vs.channel_id = nc.channel_id
                ORDER BY nc.enabled DESC, nc.role,
                         {self.store.casefold("COALESCE(NULLIF(c.title, ''), nc.channel_id)")}
                """,
                (int(niche_id),),
            ).fetchall()
        out = [dict(raw) for raw in tracked]
        for item in out:
            if not item["channel_url"]:
                item["channel_url"] = (
                    f"https://www.youtube.com/channel/{item['channel_id']}"
                )
        return out

    def feed(
        self,
        niche_id: int,
        *,
        days: int | None = 30,
        format_mode: str = "all",
        sort: str = "newest",
        limit: int | None = 200,
        roles: Iterable[str] | None = None,
    ) -> list[dict]:
        clean_format = format_mode.strip().lower()
        clean_sort = sort.strip().lower()
        if clean_format not in _FORMAT_VALUES:
            raise ValueError(f"Unsupported format: {format_mode}")
        if clean_sort not in _SORT_VALUES:
            raise ValueError(f"Unsupported sort: {sort}")
        clean_roles = {
            str(role).strip().lower()
            for role in (roles or [])
            if str(role).strip()
        }
        invalid_roles = clean_roles - _ROLE_VALUES
        if invalid_roles:
            raise ValueError(
                f"Unsupported channel roles: {', '.join(sorted(invalid_roles))}"
            )
        with self.store.connect() as conn:
            tracked = {
                row["channel_id"]: row["role"]
                for row in conn.execute(
                    """
                    SELECT channel_id, role
                    FROM niche_channels
                    WHERE niche_id=? AND enabled=1
                    """,
                    (int(niche_id),),
                )
                if not clean_roles or row["role"] in clean_roles
            }
        if not tracked:
            return []

        # Only the velocity sort needs the expensive per-video snapshot/discovery
        # aggregations; outlier/views/recency rank on views + channel baseline, so
        # they use the lite (no-aggregation) row load — seconds -> milliseconds.
        metadata = self.store.video_metadata_rows(
            tracked, lite=(clean_sort != "velocity"))
        outlier_map = {
            item.video_id: item
            for item in find_outliers(
                self.store,
                limit=None,
                min_channel_videos=2,
                min_views=0,
                min_score=0,
                max_age_days=None,
                rows=metadata,
                # feed only reads outlier_score/baseline_views, never the trend
                # fields — so skip find_outliers' ~12s whole-table scan of
                # outlier_snapshots (outlier_score_trends) that we'd throw away.
                score_trends={},
            )
            if item.channel_id in tracked
        }
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(int(days), 0))
            if days is not None
            else None
        )
        rows: list[dict] = []
        for raw in metadata:
            if raw["channel_id"] not in tracked:
                continue
            row = dict(raw)
            duration = int(row.get("duration_seconds") or 0)
            is_short = 0 < duration <= 180
            if clean_format == "short" and not is_short:
                continue
            if clean_format == "long" and (is_short or duration == 0):
                continue
            event_at = _published_date(row)
            if cutoff and (not event_at or event_at < cutoff):
                continue
            outlier = outlier_map.get(row["video_id"])
            row["outlier_score"] = (
                float(outlier.outlier_score) if outlier else 0.0
            )
            row["baseline_views"] = (
                int(outlier.baseline_views) if outlier else 0
            )
            row["is_short"] = is_short
            row["workspace_role"] = tracked[row["channel_id"]]
            rows.append(row)

        if clean_sort == "views":
            rows.sort(key=lambda row: int(row.get("views") or 0), reverse=True)
        elif clean_sort == "velocity":
            rows.sort(
                key=lambda row: (
                    int(row.get("velocity_reliable") or 0),
                    float(row.get("observed_views_per_hour") or 0),
                    int(row.get("observed_views_gained") or 0),
                ),
                reverse=True,
            )
        elif clean_sort == "outlier":
            rows.sort(
                key=lambda row: (
                    float(row.get("outlier_score") or 0),
                    int(row.get("views") or 0),
                ),
                reverse=True,
            )
        else:
            rows.sort(
                key=lambda row: (
                    _published_date(row) or dt.datetime.min.replace(
                        tzinfo=dt.timezone.utc
                    ),
                    row.get("first_seen_at") or "",
                ),
                reverse=True,
            )
        return rows if limit is None else rows[: max(int(limit), 0)]

    def add_channels(
        self,
        niche_id: int,
        channel_ids: Iterable[str],
        *,
        role: str = "competitor",
        source: str = "manual",
    ) -> int:
        count = 0
        for channel_id in dict.fromkeys(channel_ids):
            if channel_id and self.add_channel(
                niche_id,
                channel_id,
                role=role,
                source=source,
            ):
                count += 1
        return count


def _required_name(value: str) -> str:
    name = " ".join((value or "").split())
    if not name:
        raise ValueError("Workspace name is required.")
    if len(name) > 120:
        raise ValueError("Workspace name must be 120 characters or fewer.")
    return name


def _parse_date(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _published_date(row: dict) -> dt.datetime | None:
    normalized = row.get("published_at_normalized") or normalize_published_at(
        row.get("published_at") or "",
        reference=row.get("first_seen_at") or None,
    )
    return _parse_date(normalized)
