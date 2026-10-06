"""Outlier scoring over the local YouTube index."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from statistics import median

from beacon_engine.research.store import YouTubeStore

_REL_RE = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)


@dataclass(slots=True)
class IndexedOutlier:
    video_id: str
    title: str
    channel: str
    channel_id: str
    views: int
    baseline_views: int
    outlier_score: float
    age_days: int
    views_per_day: float
    url: str
    source: str = ""
    published_at: str = ""
    published_at_normalized: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    discovery_count: int = 0
    observed_views_gained: int = 0
    observed_hours: float = 0.0
    observed_views_per_hour: float = 0.0
    previous_outlier_score: float = 0.0
    score_change: float = 0.0
    score_snapshot_count: int = 0
    latest_score_at: str = ""


@dataclass(slots=True)
class VelocitySignal:
    video_id: str
    title: str
    channel: str
    old_views: int
    new_views: int
    gained_views: int
    hours: float
    views_per_hour: float
    url: str


def find_outliers(
    store: YouTubeStore,
    *,
    limit: int | None = 25,
    min_channel_videos: int = 3,
    min_views: int = 1_000,
    min_score: float = 2.0,
    max_age_days: int | None = None,
    rows=None,
    score_trends: dict[str, dict] | None = None,
) -> list[IndexedOutlier]:
    """Rank videos that beat their channel's indexed baseline."""
    rows = store.video_metadata_rows() if rows is None else rows
    by_channel: dict[str, list] = {}
    for row in rows:
        if row["channel_id"]:
            by_channel.setdefault(row["channel_id"], []).append(row)
    score_trends = (
        store.outlier_score_trends()
        if score_trends is None
        else score_trends
    )

    out: list[IndexedOutlier] = []
    for channel_rows in by_channel.values():
        views = [int(r["views"] or 0) for r in channel_rows if int(r["views"] or 0) > 0]
        if len(views) < min_channel_videos:
            continue
        baseline = max(int(median(views)), 1)
        for row in channel_rows:
            v = int(row["views"] or 0)
            if v < min_views:
                continue
            score = round(v / baseline, 2)
            if score < min_score:
                continue
            age = _age_days(
                row["published_at_normalized"] or row["published_at"]
            )
            if max_age_days is not None and age > max_age_days:
                continue
            trend = score_trends.get(row["video_id"], {})
            out.append(IndexedOutlier(
                video_id=row["video_id"],
                title=row["title"] or "",
                channel=row["channel"] or "",
                channel_id=row["channel_id"] or "",
                views=v,
                baseline_views=baseline,
                outlier_score=score,
                age_days=age,
                views_per_day=round(v / max(age, 1), 1),
                url=row["url"] or f"https://www.youtube.com/watch?v={row['video_id']}",
                source=row["source"] or "",
                published_at=row["published_at"] or "",
                published_at_normalized=row["published_at_normalized"] or "",
                first_seen_at=row["first_seen_at"] or "",
                last_seen_at=row["last_seen_at"] or "",
                discovery_count=int(row["discovery_count"] or 0),
                observed_views_gained=int(row["observed_views_gained"] or 0),
                observed_hours=round(float(row["observed_hours"] or 0), 2),
                observed_views_per_hour=round(
                    float(row["observed_views_per_hour"] or 0),
                    2,
                ),
                previous_outlier_score=float(trend.get("previous_score") or 0),
                score_change=round(float(trend.get("score_change") or 0), 2),
                score_snapshot_count=int(trend.get("score_snapshot_count") or 0),
                latest_score_at=str(trend.get("latest_score_at") or ""),
            ))

    def rank(item: IndexedOutlier) -> float:
        recency = 1.35 if item.age_days <= 30 else 1.15 if item.age_days <= 180 else 1.0
        return item.outlier_score * recency

    out.sort(key=rank, reverse=True)
    return out if limit is None else out[:limit]


def velocity_signals(
    store: YouTubeStore,
    *,
    limit: int = 25,
    min_hours: float = 1.0,
    min_gained_views: int = 100,
) -> list[VelocitySignal]:
    """Compute view growth from earliest to latest stored snapshots."""
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                v.video_id, v.title, v.channel, v.url,
                first.views AS old_views,
                last.views AS new_views,
                first.captured_at AS first_at,
                last.captured_at AS last_at
            FROM videos v
            JOIN (
                SELECT s1.video_id, s1.views, s1.captured_at
                FROM video_snapshots s1
                JOIN (
                    SELECT video_id, MIN(captured_at) AS captured_at
                    FROM video_snapshots
                    GROUP BY video_id
                ) m ON m.video_id = s1.video_id AND m.captured_at = s1.captured_at
            ) first ON first.video_id = v.video_id
            JOIN (
                SELECT s2.video_id, s2.views, s2.captured_at
                FROM video_snapshots s2
                JOIN (
                    SELECT video_id, MAX(captured_at) AS captured_at
                    FROM video_snapshots
                    GROUP BY video_id
                ) m ON m.video_id = s2.video_id AND m.captured_at = s2.captured_at
            ) last ON last.video_id = v.video_id
            WHERE last.captured_at > first.captured_at
            """
        ).fetchall()

    out: list[VelocitySignal] = []
    for row in rows:
        old, new = int(row["old_views"] or 0), int(row["new_views"] or 0)
        gained = new - old
        if gained < min_gained_views:
            continue
        hours = _hours_between(row["first_at"], row["last_at"])
        if hours < min_hours:
            continue
        out.append(VelocitySignal(
            video_id=row["video_id"],
            title=row["title"] or "",
            channel=row["channel"] or "",
            old_views=old,
            new_views=new,
            gained_views=gained,
            hours=round(hours, 2),
            views_per_hour=round(gained / hours, 2),
            url=row["url"] or f"https://www.youtube.com/watch?v={row['video_id']}",
        ))
    out.sort(key=lambda x: x.views_per_hour, reverse=True)
    return out[:limit]


def _age_days(published_at: str | None) -> int:
    text = (published_at or "").strip()
    if not text:
        return 99999
    if m := _REL_RE.search(text):
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("second") or unit.startswith("minute") or unit.startswith("hour"):
            return 1
        if unit.startswith("day"):
            return max(n, 1)
        if unit.startswith("week"):
            return max(n * 7, 1)
        if unit.startswith("month"):
            return max(n * 30, 1)
        if unit.startswith("year"):
            return max(n * 365, 1)
    try:
        dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return max((_dt.datetime.now(_dt.timezone.utc) - dt).days, 1)
    except ValueError:
        return 99999


def _hours_between(start: str, end: str) -> float:
    try:
        a = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max((b - a).total_seconds() / 3600, 0.0)
    except ValueError:
        return 0.0
