"""Lightweight YouTube discovery collection for a remote Signal edge node."""

from __future__ import annotations

import datetime as dt
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from beacon_engine.edge.plan import DiscoveryPlan, EdgeQuery
from beacon_engine.edge.protocol import utc_now
from beacon_engine.research.faceless_filter import (
    CLASSIFIER_VERSION as FACELESS_CLASSIFIER_VERSION,
    score_channel_text,
)
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.providers.base import normalize_published_at
from beacon_engine.research.providers.official_youtube_provider import (
    _channel_from_item,
    _video_from_item,
)
from beacon_engine.research.providers.scrapetube_provider import ScrapetubeProvider
from beacon_engine.research.topic_classifier import (
    CLASSIFIER_VERSION as TOPIC_CLASSIFIER_VERSION,
    classify_topic,
)
from beacon_engine.research.youtube_pool import YouTubePool

_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]+\b")
_KEY_PARAM_RE = re.compile(r"(?i)(\bkey=)[^&\s]+")
EDGE_FACELESS_MODE = "balanced"
EDGE_FREE_SEARCH_ATTEMPTS = 3
EDGE_FREE_SEARCH_BACKOFF_SECONDS = 0.75


class SearchProvider(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        limit: int,
        sort: str | None = None,
    ) -> list[ProviderVideo]: ...


class OfficialPool(Protocol):
    units: int

    def search_ids(
        self,
        query: str,
        *,
        limit: int = 50,
        order: str = "viewCount",
    ) -> list[str]: ...

    def videos(
        self,
        ids: list[str],
        *,
        part: str = "snippet,statistics,contentDetails",
    ) -> list[dict]: ...

    def channels(
        self,
        ids: list[str],
        *,
        part: str = "snippet,statistics",
    ) -> list[dict]: ...

    def playlist_video_ids(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
    ) -> list[str]: ...


_DAILY_POOL_LOCK = threading.Lock()
_DAILY_POOL_DATE = ""
_DAILY_POOL: OfficialPool | None = None


def daily_youtube_pool() -> OfficialPool:
    """Reuse exhausted-key knowledge within one UTC day, then reset quotas."""
    global _DAILY_POOL, _DAILY_POOL_DATE

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with _DAILY_POOL_LOCK:
        if _DAILY_POOL is None or _DAILY_POOL_DATE != today:
            _DAILY_POOL = YouTubePool()
            _DAILY_POOL_DATE = today
        return _DAILY_POOL


@dataclass(frozen=True)
class ScheduledQuery:
    spec: EdgeQuery
    sort: str = ""
    previous_video_ids: tuple[str, ...] = ()
    previous_video_views: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class QueryOutcome:
    query: str
    topic: str
    sort: str
    provider: str
    status: str
    videos: tuple[ProviderVideo, ...]
    previous_video_ids: tuple[str, ...]
    started_at: str
    finished_at: str
    previous_video_views: tuple[tuple[str, int], ...] = ()
    error: str = ""
    attempts: int = 1

    @property
    def video_ids(self) -> tuple[str, ...]:
        return tuple(video.video_id for video in self.videos if video.video_id)

    @property
    def new_result_count(self) -> int:
        previous = set(self.previous_video_ids)
        return sum(1 for video_id in self.video_ids if video_id not in previous)

    @property
    def changed_video_ids(self) -> tuple[str, ...]:
        previous = {
            str(video_id): max(0, int(views or 0))
            for video_id, views in self.previous_video_views
            if video_id
        }
        if not previous:
            return ()
        return tuple(
            video.video_id
            for video in self.videos
            if (
                video.video_id in previous
                and int(video.views or 0) > 0
                and int(video.views or 0) != previous[video.video_id]
            )
        )

    @property
    def delta_video_ids(self) -> tuple[str, ...]:
        previous = set(self.previous_video_ids)
        changed = set(self.changed_video_ids)
        return tuple(
            video_id
            for video_id in self.video_ids
            if video_id not in previous or video_id in changed
        )


@dataclass(frozen=True)
class CollectionBatch:
    node_id: str
    job_id: str
    run_id: str
    captured_at: str
    outcomes: tuple[QueryOutcome, ...]
    records: dict[str, list[dict[str, Any]]]
    quota_units: int
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == "ok")

    @property
    def failed(self) -> int:
        return len(self.outcomes) - self.succeeded

    def metadata(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "captured_at": self.captured_at,
            "queries": len(self.outcomes),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "quota_units": self.quota_units,
            "free_search_retries": sum(
                max(0, int(outcome.attempts or 1) - 1)
                for outcome in self.outcomes
                if outcome.provider != "official-youtube"
            ),
            "warnings": list(self.warnings),
            "delta": {
                "result_records": sum(
                    len(outcome.video_ids)
                    for outcome in self.outcomes
                    if outcome.status == "ok"
                ),
                "new_results": sum(
                    outcome.new_result_count
                    for outcome in self.outcomes
                    if outcome.status == "ok"
                ),
                "changed_results": sum(
                    len(outcome.changed_video_ids)
                    for outcome in self.outcomes
                    if outcome.status == "ok"
                ),
                "emitted_videos": len(self.records.get("videos", [])),
                "suppressed_repeat_results": sum(
                    max(0, len(outcome.video_ids) - len(outcome.delta_video_ids))
                    for outcome in self.outcomes
                    if outcome.status == "ok"
                ),
            },
            "organization": {
                "topics": len(self.records.get("channels", [])),
                "text_classifications": len(
                    self.records.get("channel_classifications", [])
                ),
                "topic_classifier": TOPIC_CLASSIFIER_VERSION,
                "faceless_classifier": FACELESS_CLASSIFIER_VERSION,
                "faceless_mode": EDGE_FACELESS_MODE,
            },
        }


def _safe_error(exc: BaseException) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is not None:
        reason = ""
        try:
            details = getattr(exc, "error_details", None)
            if isinstance(details, list) and details:
                reason = str(details[0].get("reason") or "")
        except Exception:  # noqa: BLE001
            reason = ""
        suffix = f" ({reason})" if reason else ""
        return f"{type(exc).__name__} HTTP {status}{suffix}"[:300]
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = _API_KEY_RE.sub("[redacted-key]", message)
    message = _KEY_PARAM_RE.sub(r"\1[redacted]", message)
    message = re.sub(r"\s+", " ", message).strip()
    return f"{type(exc).__name__}: {message}"[:300]


def _dedupe_videos(
    videos: Iterable[ProviderVideo],
    limit: int,
) -> tuple[ProviderVideo, ...]:
    out: list[ProviderVideo] = []
    seen: set[str] = set()
    for video in videos:
        if not video.video_id or video.video_id in seen:
            continue
        seen.add(video.video_id)
        out.append(video)
        if len(out) >= limit:
            break
    return tuple(out)


def _retryable_free_search_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (json.JSONDecodeError, TimeoutError, ConnectionError, OSError),
    ):
        return True
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        any(token in name for token in ("timeout", "connection", "jsondecode"))
        or "too many requests" in message
        or "temporarily unavailable" in message
        or "http 429" in message
        or "http 503" in message
    )


def _search_free(
    scheduled: ScheduledQuery,
    search_factory: Callable[[], SearchProvider],
) -> QueryOutcome:
    started_at = utc_now()
    provider_name = "scrapetube"
    attempts = 0
    while attempts < EDGE_FREE_SEARCH_ATTEMPTS:
        attempts += 1
        try:
            provider = search_factory()
            provider_name = str(getattr(provider, "name", provider_name))
            videos = provider.search(
                scheduled.spec.query,
                limit=scheduled.spec.limit,
                sort=scheduled.sort or None,
            )
            return QueryOutcome(
                query=scheduled.spec.query,
                topic=scheduled.spec.topic,
                sort=scheduled.sort,
                provider=provider_name,
                status="ok",
                videos=_dedupe_videos(videos, scheduled.spec.limit),
                previous_video_ids=scheduled.previous_video_ids,
                started_at=started_at,
                finished_at=utc_now(),
                previous_video_views=scheduled.previous_video_views,
                attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            if (
                attempts < EDGE_FREE_SEARCH_ATTEMPTS
                and _retryable_free_search_error(exc)
            ):
                delay = (
                    EDGE_FREE_SEARCH_BACKOFF_SECONDS * (2 ** (attempts - 1))
                    + random.uniform(0.0, 0.5)
                )
                time.sleep(delay)
                continue
            return QueryOutcome(
                query=scheduled.spec.query,
                topic=scheduled.spec.topic,
                sort=scheduled.sort,
                provider=provider_name,
                status="error",
                videos=(),
                previous_video_ids=scheduled.previous_video_ids,
                started_at=started_at,
                finished_at=utc_now(),
                previous_video_views=scheduled.previous_video_views,
                error=_safe_error(exc),
                attempts=attempts,
            )
    raise AssertionError("free search retry loop exited unexpectedly")


def _free_outcomes(
    scheduled: list[ScheduledQuery],
    *,
    workers: int,
    search_factory: Callable[[], SearchProvider],
) -> list[QueryOutcome]:
    outcomes: list[QueryOutcome | None] = [None] * len(scheduled)
    with ThreadPoolExecutor(
        max_workers=min(max(1, workers), len(scheduled)),
        thread_name_prefix="edge-search",
    ) as executor:
        futures = {
            executor.submit(_search_free, item, search_factory): index
            for index, item in enumerate(scheduled)
        }
        for future in as_completed(futures):
            outcomes[futures[future]] = future.result()
    return [outcome for outcome in outcomes if outcome is not None]


def _official_outcomes(
    scheduled: list[ScheduledQuery],
    *,
    pool: OfficialPool,
    max_units: int,
) -> list[QueryOutcome]:
    outcomes: list[QueryOutcome] = []
    for item in scheduled:
        started_at = utc_now()
        if pool.units + 100 > max_units:
            outcomes.append(
                QueryOutcome(
                    query=item.spec.query,
                    topic=item.spec.topic,
                    sort=item.sort,
                    provider="official-youtube",
                    status="error",
                    videos=(),
                    previous_video_ids=item.previous_video_ids,
                    started_at=started_at,
                    finished_at=utc_now(),
                    previous_video_views=item.previous_video_views,
                    error="official search skipped: per-run quota budget reached",
                )
            )
            continue
        try:
            order = "date" if item.sort == "newest" else "viewCount"
            ids = pool.search_ids(
                item.spec.query,
                limit=item.spec.limit,
                order=order,
            )
            outcomes.append(
                QueryOutcome(
                    query=item.spec.query,
                    topic=item.spec.topic,
                    sort=item.sort,
                    provider="official-youtube",
                    status="ok",
                    videos=tuple(
                        ProviderVideo(video_id=video_id, source="official-youtube")
                        for video_id in ids
                    ),
                    previous_video_ids=item.previous_video_ids,
                    started_at=started_at,
                    finished_at=utc_now(),
                    previous_video_views=item.previous_video_views,
                )
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                QueryOutcome(
                    query=item.spec.query,
                    topic=item.spec.topic,
                    sort=item.sort,
                    provider="official-youtube",
                    status="error",
                    videos=(),
                    previous_video_ids=item.previous_video_ids,
                    started_at=started_at,
                    finished_at=utc_now(),
                    previous_video_views=item.previous_video_views,
                    error=_safe_error(exc),
                )
            )
    return outcomes


def _remaining_item_capacity(pool: OfficialPool, max_units: int) -> int:
    return max(0, max_units - int(pool.units or 0)) * 50


def _enrich_videos(
    outcomes: list[QueryOutcome],
    *,
    pool: OfficialPool,
    max_units: int,
) -> tuple[list[QueryOutcome], list[str]]:
    warnings: list[str] = []
    ids = list(
        dict.fromkeys(
            video.video_id
            for outcome in outcomes
            if outcome.status == "ok"
            for video in outcome.videos
            if video.video_id
        )
    )
    capacity = _remaining_item_capacity(pool, max_units)
    if not ids or capacity <= 0:
        return outcomes, warnings
    enriched: dict[str, ProviderVideo] = {}
    try:
        for item in pool.videos(ids[:capacity]):
            video = _video_from_item(item)
            if video.video_id:
                enriched[video.video_id] = video
    except Exception as exc:  # noqa: BLE001
        warnings.append("official video enrichment: " + _safe_error(exc))

    merged: list[QueryOutcome] = []
    for outcome in outcomes:
        merged.append(
            QueryOutcome(
                query=outcome.query,
                topic=outcome.topic,
                sort=outcome.sort,
                provider=outcome.provider,
                status=outcome.status,
                videos=tuple(
                    enriched.get(video.video_id, video) for video in outcome.videos
                ),
                previous_video_ids=outcome.previous_video_ids,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                previous_video_views=outcome.previous_video_views,
                error=outcome.error,
                attempts=outcome.attempts,
            )
        )
    return merged, warnings


def _enrich_channels(
    outcomes: list[QueryOutcome],
    *,
    pool: OfficialPool,
    max_units: int,
) -> tuple[dict[str, ProviderChannel], list[str]]:
    warnings: list[str] = []
    # Video enrichment has already observed current views. Only these delta
    # videos can emit channel records; lookups for every other channel would
    # consume quota and then be discarded by _records_from_outcomes.
    ids: list[str] = []
    for outcome in outcomes:
        if outcome.status != "ok":
            continue
        emitted = set(outcome.delta_video_ids)
        ids.extend(video.channel_id for video in outcome.videos
                   if video.channel_id and video.video_id in emitted)
    ids = list(dict.fromkeys(ids))
    capacity = _remaining_item_capacity(pool, max_units)
    if not ids or capacity <= 0:
        return {}, warnings
    channels: dict[str, ProviderChannel] = {}
    try:
        for item in pool.channels(ids[:capacity]):
            channel = _channel_from_item(item)
            if channel.channel_id:
                channels[channel.channel_id] = channel
    except Exception as exc:  # noqa: BLE001
        warnings.append("official channel enrichment: " + _safe_error(exc))
    return channels, warnings


def _slim_video_raw(video: ProviderVideo) -> dict[str, Any]:
    """Keep only raw video data still consumed after parsed columns are stored."""
    if not isinstance(video.raw, Mapping):
        return {}
    snippet = video.raw.get("snippet")
    if not isinstance(snippet, Mapping):
        return {}
    category_id = str(snippet.get("categoryId") or "")
    return (
        {"snippet": {"categoryId": category_id}}
        if category_id
        else {}
    )


def _slim_channel_raw(channel: ProviderChannel) -> dict[str, Any]:
    """Keep channel thumbnails used by Signal cards; parsed fields live in columns."""
    if not isinstance(channel.raw, Mapping):
        return {}
    snippet = channel.raw.get("snippet")
    if not isinstance(snippet, Mapping):
        return {}
    thumbnails = snippet.get("thumbnails")
    return (
        {"snippet": {"thumbnails": dict(thumbnails)}}
        if isinstance(thumbnails, Mapping) and thumbnails
        else {}
    )


def _video_record(video: ProviderVideo, captured_at: str) -> dict[str, Any]:
    raw = _slim_video_raw(video)
    category_id = ""
    if raw:
        category_id = str(raw["snippet"].get("categoryId") or "")
    return {
        "video_id": video.video_id,
        "title": video.title,
        "channel": video.channel,
        "channel_id": video.channel_id,
        "views": max(0, int(video.views or 0)),
        "published_at": video.published_at,
        "published_at_normalized": normalize_published_at(
            video.published_at,
            reference=captured_at,
        ),
        "url": video.url,
        "duration_seconds": max(0, int(video.duration_seconds or 0)),
        "description": video.description,
        "thumbnail_url": video.thumbnail_url,
        "source": video.source,
        "raw": raw,
        "first_seen_at": captured_at,
        "last_seen_at": captured_at,
        "category_id": category_id,
    }


def _channel_record(
    channel: ProviderChannel,
    *,
    captured_at: str,
    category: str,
) -> dict[str, Any]:
    topic = classify_topic(
        category=category,
        title=channel.title,
        description=channel.description,
    )
    return {
        "channel_id": channel.channel_id,
        "title": channel.title,
        "handle": channel.handle,
        "subscribers": max(0, int(channel.subscribers or 0)),
        "video_count": max(0, int(channel.video_count or 0)),
        "view_count": max(0, int(channel.view_count or 0)),
        "description": channel.description,
        "url": channel.url,
        "channel_published_at": channel.published_at,
        "source": channel.source,
        "raw": _slim_channel_raw(channel),
        "first_seen_at": captured_at,
        "last_seen_at": captured_at,
        "category": category,
        "topic": topic,
    }


def _classification_record(
    channel_id: str,
    channel_title: str,
    videos: Iterable[ProviderVideo],
    *,
    captured_at: str,
) -> dict[str, Any]:
    signal = score_channel_text(videos, channel_title=channel_title)
    return {
        "channel_id": channel_id,
        "likely_faceless": int(signal.matches(EDGE_FACELESS_MODE)),
        "confidence": signal.confidence,
        "score": signal.score,
        "sample_count": signal.sample_count,
        "positive_title_count": signal.positive_title_count,
        "negative_title_count": signal.negative_title_count,
        "positives": signal.positives,
        "negatives": signal.negatives,
        "classifier": FACELESS_CLASSIFIER_VERSION,
        "classified_at": captured_at,
    }


def _records_from_outcomes(
    outcomes: list[QueryOutcome],
    *,
    node_id: str,
    job_id: str,
    run_id: str,
    captured_at: str,
    official_channels: Mapping[str, ProviderChannel],
) -> dict[str, list[dict[str, Any]]]:
    videos: dict[str, ProviderVideo] = {}
    channel_seed: dict[str, tuple[str, str]] = {}
    search_results: list[dict[str, Any]] = []
    discoveries: list[dict[str, Any]] = []
    query_runs: list[dict[str, Any]] = []
    for outcome in outcomes:
        delta_ids = set(outcome.delta_video_ids)
        query_runs.append(
            {
                "node_id": node_id,
                "job_id": job_id,
                "run_id": run_id,
                "query": outcome.query,
                "topic": outcome.topic,
                "provider": outcome.provider,
                "status": outcome.status,
                "result_count": len(outcome.video_ids),
                "new_result_count": outcome.new_result_count,
                "channel_ids": sorted({video.channel_id for video in outcome.videos
                                       if video.channel_id and video.video_id in delta_ids}),
                "sort": outcome.sort or "relevance",
                "error": outcome.error,
                "attempts": outcome.attempts,
                "started_at": outcome.started_at,
                "finished_at": outcome.finished_at,
            }
        )
        if outcome.status != "ok":
            continue
        previous_ids = set(outcome.previous_video_ids)
        for position, video in enumerate(outcome.videos, 1):
            is_new = video.video_id not in previous_ids
            if video.video_id not in delta_ids:
                continue
            current = videos.get(video.video_id)
            if current is None or (
                video.source == "official-youtube"
                and current.source != "official-youtube"
            ):
                videos[video.video_id] = video
            if video.channel_id and video.channel_id not in channel_seed:
                channel_seed[video.channel_id] = (
                    video.channel,
                    outcome.topic or outcome.query,
                )
            if is_new:
                search_results.append(
                    {
                        "query": outcome.query,
                        "provider": outcome.provider,
                        "video_id": video.video_id,
                        "position": position,
                        "captured_at": captured_at,
                    }
                )
                discoveries.append(
                    {
                        "video_id": video.video_id,
                        "query": outcome.query,
                        "provider": outcome.provider,
                        "discovery_kind": "search",
                        "position": position,
                        "discovered_at": captured_at,
                    }
                )

    channel_rows: list[dict[str, Any]] = []
    for channel_id, (title, category) in channel_seed.items():
        channel = official_channels.get(channel_id) or ProviderChannel(
            channel_id=channel_id,
            title=title,
            url=f"https://www.youtube.com/channel/{channel_id}",
            source="edge-search",
        )
        channel_rows.append(
            _channel_record(
                channel,
                captured_at=captured_at,
                category=category,
            )
        )
    video_rows = [_video_record(video, captured_at) for video in videos.values()]
    videos_by_channel: dict[str, list[ProviderVideo]] = {}
    for video in videos.values():
        if video.channel_id:
            videos_by_channel.setdefault(video.channel_id, []).append(video)
    classification_rows = [
        _classification_record(
            row["channel_id"],
            row["title"],
            videos_by_channel.get(row["channel_id"], []),
            captured_at=captured_at,
        )
        for row in channel_rows
    ]
    snapshots = [
        {
            "video_id": video.video_id,
            "views": max(0, int(video.views or 0)),
            "captured_at": captured_at,
            "source": f"edge:{video.source or 'search'}",
        }
        for video in videos.values()
        if int(video.views or 0) > 0
    ]
    return {
        "channels": channel_rows,
        "videos": video_rows,
        "channel_classifications": classification_rows,
        "search_results": search_results,
        "video_discoveries": discoveries,
        "video_snapshots": snapshots,
        "query_runs": query_runs,
    }


def collect_discovery_batch(
    plan: DiscoveryPlan,
    scheduled: Iterable[ScheduledQuery],
    *,
    node_id: str,
    run_id: str,
    captured_at: str | None = None,
    search_factory: Callable[[], SearchProvider] = ScrapetubeProvider,
    pool_factory: Callable[[], OfficialPool] = daily_youtube_pool,
) -> CollectionBatch:
    """Collect one bounded plan slice without touching Signal's SQLite index."""
    work = list(scheduled)
    if not work:
        raise ValueError("a discovery batch requires at least one scheduled query")
    captured = captured_at or utc_now()
    settings = plan.settings
    warnings: list[str] = []
    pool: OfficialPool | None = None
    pool_start_units = 0
    quota_ceiling = settings.max_quota_units_per_run

    if settings.mode == "official":
        try:
            pool = pool_factory()
            pool_start_units = int(pool.units or 0)
            quota_ceiling = (
                pool_start_units + settings.max_quota_units_per_run
            )
            outcomes = _official_outcomes(
                work,
                pool=pool,
                max_units=quota_ceiling,
            )
        except Exception as exc:  # noqa: BLE001
            warning = "official search unavailable: " + _safe_error(exc)
            warnings.append(warning)
            outcomes = [
                QueryOutcome(
                    query=item.spec.query,
                    topic=item.spec.topic,
                    sort=item.sort,
                    provider="official-youtube",
                    status="error",
                    videos=(),
                    previous_video_ids=item.previous_video_ids,
                    started_at=captured,
                    finished_at=utc_now(),
                    previous_video_views=item.previous_video_views,
                    error=warning,
                )
                for item in work
            ]
    else:
        outcomes = _free_outcomes(
            work,
            workers=settings.workers,
            search_factory=search_factory,
        )
        if settings.mode == "hybrid":
            try:
                pool = pool_factory()
                pool_start_units = int(pool.units or 0)
                quota_ceiling = (
                    pool_start_units + settings.max_quota_units_per_run
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append("official enrichment unavailable: " + _safe_error(exc))

    official_channels: dict[str, ProviderChannel] = {}
    if pool is not None:
        outcomes, video_warnings = _enrich_videos(
            outcomes,
            pool=pool,
            max_units=quota_ceiling,
        )
        warnings.extend(video_warnings)
        official_channels, channel_warnings = _enrich_channels(
            outcomes,
            pool=pool,
            max_units=quota_ceiling,
        )
        warnings.extend(channel_warnings)

    records = _records_from_outcomes(
        outcomes,
        node_id=node_id,
        job_id=plan.job_id,
        run_id=run_id,
        captured_at=captured,
        official_channels=official_channels,
    )
    return CollectionBatch(
        node_id=node_id,
        job_id=plan.job_id,
        run_id=run_id,
        captured_at=captured,
        outcomes=tuple(outcomes),
        records=records,
        quota_units=max(
            0,
            int(getattr(pool, "units", 0) or 0) - pool_start_units,
        ),
        warnings=tuple(warnings),
    )
