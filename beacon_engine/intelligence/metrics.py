"""Reproducible channel metrics and historical snapshots."""

from __future__ import annotations

import datetime as dt
from statistics import median

from beacon_engine.research.providers.base import utc_now_iso
from beacon_engine.research.store import YouTubeStore


class ChannelMetricsService:
    def __init__(self, store: YouTubeStore) -> None:
        self.store = store

    def channel_metrics(self, channel_id: str) -> dict | None:
        with self.store.connect() as conn:
            channel = conn.execute(
                "SELECT * FROM channels WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
            videos = conn.execute(
                """
                SELECT *
                FROM videos
                WHERE channel_id=?
                ORDER BY COALESCE(NULLIF(published_at_normalized, ''), first_seen_at)
                """,
                (channel_id,),
            ).fetchall()
        return _build_channel_metrics(channel_id, channel, videos)

    def metrics_for_channels(self, channel_ids: list[str]) -> list[dict]:
        ids = list(dict.fromkeys(channel_id for channel_id in channel_ids if channel_id))
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self.store.connect() as conn:
            channels = {
                row["channel_id"]: row
                for row in conn.execute(
                    f"SELECT * FROM channels WHERE channel_id IN ({placeholders})",
                    ids,
                )
            }
            grouped: dict[str, list] = {channel_id: [] for channel_id in ids}
            for row in conn.execute(
                f"""
                SELECT *
                FROM videos
                WHERE channel_id IN ({placeholders})
                ORDER BY channel_id,
                    COALESCE(NULLIF(published_at_normalized, ''), first_seen_at)
                """,
                ids,
            ):
                grouped.setdefault(row["channel_id"], []).append(row)
        return [
            metrics
            for channel_id in ids
            if (
                metrics := _build_channel_metrics(
                    channel_id,
                    channels.get(channel_id),
                    grouped.get(channel_id, []),
                )
            ) is not None
        ]

    def workspace_metrics(
        self,
        niche_id: int,
        roles: set[str] | tuple[str, ...] | list[str] | None = None,
    ) -> list[dict]:
        clean_roles = {
            str(role).strip().lower()
            for role in (roles or [])
            if str(role).strip()
        }
        with self.store.connect() as conn:
            ids = [
                row["channel_id"]
                for row in conn.execute(
                    """
                    SELECT channel_id, role
                    FROM niche_channels
                    WHERE niche_id=? AND enabled=1
                    """,
                    (int(niche_id),),
                )
                if not clean_roles or row["role"] in clean_roles
            ]
        return self.metrics_for_channels(ids)

    def capture_channel(self, channel_id: str, source: str = "local-index",
                        *, min_interval_hours: float = 0.0) -> int:
        # Skip if we already snapshotted this channel recently — keeps the growth
        # series ~1 row/day instead of one per 5-min radar cycle. 0 = always write.
        if min_interval_hours > 0:
            with self.store.connect() as conn:
                row = conn.execute(
                    "SELECT MAX(captured_at) AS last FROM channel_snapshots "
                    "WHERE channel_id=?", (channel_id,)).fetchone()
            last = _parse_date(row["last"] if row else "")
            if last is not None:
                age_h = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 3600.0
                if age_h < min_interval_hours:
                    return 0
        metrics = self.channel_metrics(channel_id)
        if not metrics:
            raise KeyError(f"Unknown channel: {channel_id}")
        with self.store.connect() as conn:
            cur = self.store.insert_id(
                conn,
                """
                INSERT INTO channel_snapshots (
                    channel_id, subscribers, total_views, video_count,
                    average_views, median_views, upload_cadence_days,
                    captured_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    metrics["subscribers"],
                    metrics["total_views"],
                    metrics["reported_video_count"],
                    metrics["average_views"],
                    metrics["median_views"],
                    metrics["upload_cadence_days"],
                    utc_now_iso(),
                    source,
                ),
            )
            return cur

    def capture_workspace(self, niche_id: int, *, min_interval_hours: float = 0.0) -> int:
        # `workspace_metrics()` already loads every channel and its videos in two
        # bulk queries. The old path threw that work away, then called
        # `capture_channel()` for every member, repeating the snapshot lookup,
        # channel read, video read, and insert hundreds of times per radar cycle.
        rows = self.workspace_metrics(niche_id)
        if not rows:
            return 0

        if min_interval_hours > 0:
            with self.store.connect() as conn:
                latest = {
                    row["channel_id"]: row["last"]
                    for row in conn.execute(
                        """
                        SELECT cs.channel_id, MAX(cs.captured_at) AS last
                        FROM channel_snapshots cs
                        JOIN niche_channels nc ON nc.channel_id = cs.channel_id
                        WHERE nc.niche_id=? AND nc.enabled=1
                        GROUP BY cs.channel_id
                        """,
                        (int(niche_id),),
                    )
                }
            now = dt.datetime.now(dt.timezone.utc)
            due = []
            for metrics in rows:
                last = _parse_date(latest.get(metrics["channel_id"], ""))
                if last is None:
                    due.append(metrics)
                    continue
                age_h = (now - last).total_seconds() / 3600.0
                if age_h >= min_interval_hours:
                    due.append(metrics)
            rows = due
            if not rows:
                return 0

        captured_at = utc_now_iso()
        payload = [
            (
                metrics["channel_id"],
                metrics["subscribers"],
                metrics["total_views"],
                metrics["reported_video_count"],
                metrics["average_views"],
                metrics["median_views"],
                metrics["upload_cadence_days"],
                captured_at,
                "local-index",
            )
            for metrics in rows
        ]
        with self.store.connect() as conn:
            conn.executemany(
                """
                INSERT INTO channel_snapshots (
                    channel_id, subscribers, total_views, video_count,
                    average_views, median_views, upload_cadence_days,
                    captured_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def channel_history(self, channel_id: str) -> list[dict]:
        with self.store.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM channel_snapshots
                    WHERE channel_id=?
                    ORDER BY captured_at
                    """,
                    (channel_id,),
                )
            ]

    def channel_growth(self, channel_id: str) -> dict:
        """Growth of one channel between its first and latest snapshot.

        Diffs subscribers / total views / video count over the tracked window and
        turns it into per-day rates — the "checked over days" signal the UI shows.
        Returns zeros (but current values) until there are two snapshots to diff."""
        return self.growth_for_channels([channel_id]).get(
            channel_id, _empty_growth(channel_id))

    def growth_for_channels(self, channel_ids: list[str]) -> dict[str, dict]:
        """Bulk growth read: channel_id -> growth dict, in one query."""
        ids = list(dict.fromkeys(cid for cid in channel_ids if cid))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        series: dict[str, list] = {cid: [] for cid in ids}
        with self.store.connect() as conn:
            for row in conn.execute(
                f"""
                SELECT channel_id, subscribers, total_views, video_count, captured_at
                FROM channel_snapshots
                WHERE channel_id IN ({placeholders})
                ORDER BY channel_id, captured_at
                """,
                ids,
            ):
                series.setdefault(row["channel_id"], []).append(row)
        return {cid: _growth_from_series(cid, rows) for cid, rows in series.items()}

    def channel_detail(self, channel_id: str, video_limit: int = 12) -> dict | None:
        metrics = self.channel_metrics(channel_id)
        if not metrics:
            return None
        metadata = [
            dict(row)
            for row in self.store.video_metadata_rows()
            if row["channel_id"] == channel_id
        ]
        top = sorted(metadata, key=lambda row: int(row["views"] or 0), reverse=True)
        recent = sorted(
            metadata,
            key=lambda row: (
                row["published_at_normalized"]
                or row["first_seen_at"]
                or ""
            ),
            reverse=True,
        )
        growing = sorted(
            metadata,
            key=lambda row: float(row["observed_views_per_hour"] or 0),
            reverse=True,
        )
        return {
            "metrics": metrics,
            "history": self.channel_history(channel_id),
            "top_videos": top[:video_limit],
            "recent_videos": recent[:video_limit],
            "growing_videos": growing[:video_limit],
        }


def _build_channel_metrics(channel_id: str, channel, videos) -> dict | None:
        if not channel and not videos:
            return None
        views = [int(row["views"] or 0) for row in videos]
        dates = [
            parsed
            for parsed in (
                _parse_date(
                    row["published_at_normalized"]
                    or row["first_seen_at"]
                    or ""
                )
                for row in videos
            )
            if parsed
        ]
        durations = [int(row["duration_seconds"] or 0) for row in videos]
        shorts = sum(0 < duration <= 180 for duration in durations)
        long_form = sum(duration > 180 for duration in durations)
        cadence = _cadence_days(dates)
        med = float(median(views)) if views else 0.0
        avg = float(sum(views) / len(views)) if views else 0.0
        subscribers = int(channel["subscribers"] or 0) if channel else 0
        total_views = (
            int(channel["view_count"] or 0)
            if channel and int(channel["view_count"] or 0) > 0
            else sum(views)
        )
        reported_video_count = (
            int(channel["video_count"] or 0)
            if channel and int(channel["video_count"] or 0) > 0
            else len(videos)
        )
        return {
            "channel_id": channel_id,
            "title": (
                (channel["title"] or "") if channel else (videos[0]["channel"] or "")
            ) or channel_id,
            "handle": (channel["handle"] or "") if channel else "",
            "url": (
                (channel["url"] or "") if channel else ""
            ) or f"https://www.youtube.com/channel/{channel_id}",
            "subscribers": subscribers,
            "total_views": total_views,
            "reported_video_count": reported_video_count,
            "indexed_videos": len(videos),
            "average_views": round(avg, 2),
            "median_views": round(med, 2),
            "best_views": max(views, default=0),
            "channel_outlier_score": round(max(views, default=0) / max(med, 1), 2),
            "upload_cadence_days": round(cadence, 2),
            "first_upload_at": min(dates).isoformat() if dates else "",
            "last_upload_at": max(dates).isoformat() if dates else "",
            "days_since_start": (
                max((dt.datetime.now(dt.timezone.utc) - min(dates)).days, 0)
                if dates
                else 0
            ),
            "long_form_videos": long_form,
            "short_videos": shorts,
            "unknown_format_videos": len(videos) - long_form - shorts,
            "views_per_subscriber": (
                round(avg / subscribers, 3) if subscribers > 0 else None
            ),
        }


def _empty_growth(channel_id: str) -> dict:
    return {
        "channel_id": channel_id, "snapshots": 0, "days_tracked": 0.0,
        "subscribers": 0, "total_views": 0, "video_count": 0,
        "subs_gained": 0, "views_gained": 0, "videos_gained": 0,
        "subs_per_day": 0.0, "views_per_day": 0.0, "videos_per_week": 0.0,
        "first_captured_at": "", "last_captured_at": "",
    }


def _growth_from_series(channel_id: str, rows: list) -> dict:
    out = _empty_growth(channel_id)
    if not rows:
        return out
    latest = rows[-1]
    out["snapshots"] = len(rows)
    out["subscribers"] = int(latest["subscribers"] or 0)
    out["total_views"] = int(latest["total_views"] or 0)
    out["video_count"] = int(latest["video_count"] or 0)
    out["first_captured_at"] = rows[0]["captured_at"] or ""
    out["last_captured_at"] = latest["captured_at"] or ""
    if len(rows) < 2:
        return out
    first = rows[0]
    start = _parse_date(first["captured_at"] or "")
    end = _parse_date(latest["captured_at"] or "")
    days = max((end - start).total_seconds() / 86400.0, 0.0) if (start and end) else 0.0
    out["days_tracked"] = round(days, 2)
    out["subs_gained"] = int(latest["subscribers"] or 0) - int(first["subscribers"] or 0)
    out["views_gained"] = int(latest["total_views"] or 0) - int(first["total_views"] or 0)
    out["videos_gained"] = int(latest["video_count"] or 0) - int(first["video_count"] or 0)
    if days >= 0.5:   # only rate-annualize once there's a meaningful window
        out["subs_per_day"] = round(out["subs_gained"] / days, 2)
        out["views_per_day"] = round(out["views_gained"] / days, 1)
        out["videos_per_week"] = round(out["videos_gained"] / days * 7.0, 2)
    return out


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


def _cadence_days(dates: list[dt.datetime]) -> float:
    if len(dates) < 2:
        return 0.0
    ordered = sorted(set(dates))
    if len(ordered) < 2:
        return 0.0
    gaps = [
        max((later - earlier).total_seconds() / 86400.0, 0.0)
        for earlier, later in zip(ordered, ordered[1:])
    ]
    return float(median(gaps)) if gaps else 0.0
