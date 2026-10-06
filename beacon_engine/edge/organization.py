"""Signed, database-free channel organization jobs for a remote edge node.

The organizer receives only bounded immutable channel/video records. It never
opens Signal's canonical SQLite database. It may:

* fetch a deeper public upload catalog with scrapetube;
* enrich channel/video metadata and video categories with the authorized API
  key pool;
* recompute the deterministic text faceless score on the larger sample; and
* run the free CPU thumbnail face check.

The result is another signed immutable bundle that the laptop imports into the
one canonical index.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from beacon_engine.edge.collector import (
    EDGE_FREE_SEARCH_ATTEMPTS,
    EDGE_FREE_SEARCH_BACKOFF_SECONDS,
    OfficialPool,
    _channel_record,
    _classification_record,
    _retryable_free_search_error,
    _safe_error,
    _video_record,
    daily_youtube_pool,
)
from beacon_engine.edge.protocol import (
    EdgeBundle,
    EdgeProtocolError,
    build_bundle,
    load_bundle,
    utc_now,
    write_bundle,
)
from beacon_engine.edge.worker import EdgeWorkerLock
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.providers.official_youtube_provider import (
    _channel_from_item,
    _video_from_item,
)
from beacon_engine.research.providers.scrapetube_provider import ScrapetubeProvider

ORGANIZATION_JOB_KIND = "youtube.organization.job.v1"
ORGANIZATION_RESULT_KIND = "youtube.organization.v1"
ORGANIZATION_STATE_VERSION = 1
MAX_JOB_CHANNELS = 500
MAX_JOB_VIDEOS = 10_000
DEFAULT_DISCOVERY_CHANNELS_PER_JOB = 200
_ALLOWED_VERDICTS = {"faceless", "on_camera", "uncertain", "unknown"}


class CatalogProvider(Protocol):
    name: str

    def channel_videos(
        self,
        channel: str,
        *,
        limit: int = 200,
    ) -> list[ProviderVideo]: ...


@dataclass(frozen=True)
class OrganizationSettings:
    catalog_enabled: bool = True
    catalog_videos: int = 30
    catalog_workers: int = 4
    categories_enabled: bool = True
    channel_enrichment_enabled: bool = True
    max_quota_units: int = 500
    face_check_enabled: bool = True
    face_workers: int = 2
    face_min_thumbnails: int = 3
    face_max_thumbnails: int = 12
    min_subscribers: int = 700
    refresh_after_days: int = 30

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_enabled": self.catalog_enabled,
            "catalog_videos": self.catalog_videos,
            "catalog_workers": self.catalog_workers,
            "categories_enabled": self.categories_enabled,
            "channel_enrichment_enabled": self.channel_enrichment_enabled,
            "max_quota_units": self.max_quota_units,
            "face_check_enabled": self.face_check_enabled,
            "face_workers": self.face_workers,
            "face_min_thumbnails": self.face_min_thumbnails,
            "face_max_thumbnails": self.face_max_thumbnails,
            "min_subscribers": self.min_subscribers,
            "refresh_after_days": self.refresh_after_days,
        }


@dataclass(frozen=True)
class OrganizationJob:
    bundle: EdgeBundle
    job_id: str
    controller_id: str
    channels: tuple[dict[str, Any], ...]
    videos: tuple[dict[str, Any], ...]
    settings: OrganizationSettings

    @property
    def revision(self) -> str:
        return self.bundle.bundle_id

    @property
    def created_at(self) -> str:
        return str(self.bundle.payload.get("created_at") or "")


@dataclass(frozen=True)
class ChannelOrganizationOutcome:
    channel_id: str
    status: str
    catalog_attempted: bool
    catalog_status: str
    catalog_attempts: int
    videos_collected: int
    categories_filled: int
    face_status: str
    error: str
    started_at: str
    finished_at: str

    def as_record(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "status": self.status,
            "catalog_attempted": int(self.catalog_attempted),
            "catalog_status": self.catalog_status,
            "catalog_attempts": self.catalog_attempts,
            "videos_collected": self.videos_collected,
            "categories_filled": self.categories_filled,
            "face_status": self.face_status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class OrganizationBatch:
    node_id: str
    job_id: str
    run_id: str
    captured_at: str
    records: dict[str, list[dict[str, Any]]]
    quota_units: int
    warnings: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        runs = self.records.get("organization_runs", [])
        return {
            "run_id": self.run_id,
            "captured_at": self.captured_at,
            "channels": len(self.records.get("channels", [])),
            "videos": len(self.records.get("videos", [])),
            "channel_classifications": len(
                self.records.get("channel_classifications", [])
            ),
            "channel_face_checks": len(
                self.records.get("channel_face_checks", [])
            ),
            "catalog_succeeded": sum(
                1
                for row in runs
                if str(row.get("catalog_status") or "").startswith("ok")
            ),
            "catalog_failed": sum(
                1 for row in runs if row.get("catalog_status") == "error"
            ),
            "quota_units": self.quota_units,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class OrganizationWorkerPaths:
    root: Path
    jobs: Path
    archive: Path
    failed: Path
    state: Path
    status: Path
    lock: Path
    stop: Path
    models: Path
    outbox: Path

    @classmethod
    def under(
        cls,
        root: str | os.PathLike,
        *,
        outbox: str | os.PathLike | None = None,
    ) -> "OrganizationWorkerPaths":
        base = Path(root)
        return cls(
            root=base,
            jobs=base / "jobs",
            archive=base / "archive",
            failed=base / "failed",
            state=base / "organization_state.sqlite3",
            status=base / "organizer_status.json",
            lock=base / "organizer.lock",
            stop=base / "stop.flag",
            models=base / "models",
            outbox=Path(outbox) if outbox is not None else base / "outbox",
        )

    def ensure(self) -> None:
        for path in (
            self.root,
            self.jobs,
            self.archive,
            self.failed,
            self.models,
            self.outbox,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class OrganizationWorkerReport:
    job_id: str
    revision: str
    status: str
    result_bundle: str = ""
    channels: int = 0
    videos: int = 0
    quota_units: int = 0
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "revision": self.revision,
            "status": self.status,
            "result_bundle": self.result_bundle,
            "channels": self.channels,
            "videos": self.videos,
            "quota_units": self.quota_units,
            "message": self.message,
        }


def _integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EdgeProtocolError(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise EdgeProtocolError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return number


def _boolean(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise EdgeProtocolError(f"{field} must be true or false")


def _required_text(value: object, field: str, *, maximum: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        raise EdgeProtocolError(f"{field} is required")
    if len(text) > maximum:
        raise EdgeProtocolError(f"{field} exceeds {maximum} characters")
    return text


def _optional_text(value: object, *, maximum: int = 10_000) -> str:
    return str(value or "").strip()[:maximum]


def _normalize_settings(
    value: OrganizationSettings | Mapping[str, Any] | None,
) -> OrganizationSettings:
    data: Mapping[str, Any]
    if isinstance(value, OrganizationSettings):
        data = value.as_dict()
    else:
        data = value or {}
    face_min = _integer(
        data.get("face_min_thumbnails", 3),
        "face_min_thumbnails",
        minimum=1,
        maximum=20,
    )
    face_max = _integer(
        data.get("face_max_thumbnails", 12),
        "face_max_thumbnails",
        minimum=1,
        maximum=20,
    )
    if face_max < face_min:
        raise EdgeProtocolError(
            "face_max_thumbnails must be at least face_min_thumbnails"
        )
    return OrganizationSettings(
        catalog_enabled=_boolean(
            data.get("catalog_enabled", True),
            "catalog_enabled",
        ),
        catalog_videos=_integer(
            data.get("catalog_videos", 30),
            "catalog_videos",
            minimum=1,
            maximum=100,
        ),
        catalog_workers=_integer(
            data.get("catalog_workers", 4),
            "catalog_workers",
            minimum=1,
            maximum=16,
        ),
        categories_enabled=_boolean(
            data.get("categories_enabled", True),
            "categories_enabled",
        ),
        channel_enrichment_enabled=_boolean(
            data.get("channel_enrichment_enabled", True),
            "channel_enrichment_enabled",
        ),
        max_quota_units=_integer(
            data.get("max_quota_units", 500),
            "max_quota_units",
            minimum=0,
            maximum=10_000,
        ),
        face_check_enabled=_boolean(
            data.get("face_check_enabled", True),
            "face_check_enabled",
        ),
        face_workers=_integer(
            data.get("face_workers", 2),
            "face_workers",
            minimum=1,
            maximum=8,
        ),
        face_min_thumbnails=face_min,
        face_max_thumbnails=face_max,
        min_subscribers=_integer(
            data.get("min_subscribers", 700),
            "min_subscribers",
            minimum=0,
            maximum=1_000_000_000,
        ),
        refresh_after_days=_integer(
            data.get("refresh_after_days", 30),
            "refresh_after_days",
            minimum=1,
            maximum=365,
        ),
    )


def _normalize_channels(
    rows: Iterable[Mapping[str, Any]],
    *,
    maximum: int | None = MAX_JOB_CHANNELS,
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise EdgeProtocolError("organization channels must be objects")
        channel_id = _required_text(
            row.get("channel_id"),
            "channel_id",
            maximum=128,
        )
        if channel_id in seen:
            continue
        seen.add(channel_id)
        out.append(
            {
                "channel_id": channel_id,
                "title": _optional_text(row.get("title"), maximum=500),
                "handle": _optional_text(row.get("handle"), maximum=500),
                "subscribers": max(0, int(row.get("subscribers") or 0)),
                "video_count": max(0, int(row.get("video_count") or 0)),
                "view_count": max(0, int(row.get("view_count") or 0)),
                "description": _optional_text(
                    row.get("description"),
                    maximum=20_000,
                ),
                "url": _optional_text(row.get("url"), maximum=2_000),
                "channel_published_at": _optional_text(
                    row.get("channel_published_at")
                    or row.get("published_at"),
                    maximum=100,
                ),
                "source": _optional_text(row.get("source"), maximum=200),
                "category": _optional_text(row.get("category"), maximum=500),
                "topic": _optional_text(row.get("topic"), maximum=500),
            }
        )
        if maximum is not None and len(out) > maximum:
            raise EdgeProtocolError(
                f"organization jobs may contain at most {maximum} channels"
            )
    return tuple(out)


def _normalize_videos(
    rows: Iterable[Mapping[str, Any]],
    *,
    maximum: int | None = MAX_JOB_VIDEOS,
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise EdgeProtocolError("organization videos must be objects")
        video_id = _required_text(
            row.get("video_id"),
            "video_id",
            maximum=128,
        )
        if video_id in seen:
            continue
        seen.add(video_id)
        out.append(
            {
                "video_id": video_id,
                "title": _optional_text(row.get("title"), maximum=1_000),
                "channel": _optional_text(row.get("channel"), maximum=500),
                "channel_id": _optional_text(
                    row.get("channel_id"),
                    maximum=128,
                ),
                "views": max(0, int(row.get("views") or 0)),
                "published_at": _optional_text(
                    row.get("published_at"),
                    maximum=100,
                ),
                "url": _optional_text(row.get("url"), maximum=2_000),
                "duration_seconds": max(
                    0,
                    int(row.get("duration_seconds") or 0),
                ),
                "description": _optional_text(
                    row.get("description"),
                    maximum=20_000,
                ),
                "thumbnail_url": _optional_text(
                    row.get("thumbnail_url"),
                    maximum=2_000,
                ),
                "source": _optional_text(row.get("source"), maximum=200),
                "category_id": _optional_text(
                    row.get("category_id"),
                    maximum=50,
                ),
            }
        )
        if maximum is not None and len(out) > maximum:
            raise EdgeProtocolError(
                f"organization jobs may contain at most {maximum} videos"
            )
    return tuple(out)


def build_organization_job(
    *,
    controller_id: str,
    job_id: str,
    channels: Iterable[Mapping[str, Any]] = (),
    videos: Iterable[Mapping[str, Any]] = (),
    settings: OrganizationSettings | Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
    created_at: str | None = None,
    signing_key: str | bytes | None,
) -> EdgeBundle:
    clean_channels = _normalize_channels(channels)
    clean_videos = _normalize_videos(videos)
    if not clean_channels and not clean_videos:
        raise EdgeProtocolError(
            "an organization job requires at least one channel or video"
        )
    clean_settings = _normalize_settings(settings)
    return build_bundle(
        node_id=controller_id,
        kind=ORGANIZATION_JOB_KIND,
        job_id=job_id,
        records={
            "channels": list(clean_channels),
            "videos": list(clean_videos),
        },
        metadata={
            "settings": clean_settings.as_dict(),
            "channel_count": len(clean_channels),
            "video_count": len(clean_videos),
            "source": dict(source or {}),
        },
        created_at=created_at,
        signing_key=signing_key,
    )


def organization_job_from_bundle(bundle: EdgeBundle) -> OrganizationJob:
    if bundle.kind != ORGANIZATION_JOB_KIND:
        raise EdgeProtocolError(
            f"expected job kind '{ORGANIZATION_JOB_KIND}', got '{bundle.kind}'"
        )
    unknown = set(bundle.records) - {"channels", "videos"}
    if unknown:
        raise EdgeProtocolError(
            "unsupported organization job datasets: "
            + ", ".join(sorted(unknown))
        )
    job_id = _required_text(
        bundle.payload.get("job_id"),
        "organization job_id",
        maximum=128,
    )
    metadata = bundle.payload.get("metadata")
    if not isinstance(metadata, dict):
        raise EdgeProtocolError("organization metadata must be an object")
    channels = _normalize_channels(bundle.records.get("channels", []))
    videos = _normalize_videos(bundle.records.get("videos", []))
    if not channels and not videos:
        raise EdgeProtocolError(
            "an organization job requires at least one channel or video"
        )
    expected_channels = metadata.get("channel_count")
    expected_videos = metadata.get("video_count")
    if expected_channels is not None and int(expected_channels) != len(channels):
        raise EdgeProtocolError(
            "organization channel_count does not match its records"
        )
    if expected_videos is not None and int(expected_videos) != len(videos):
        raise EdgeProtocolError(
            "organization video_count does not match its records"
        )
    return OrganizationJob(
        bundle=bundle,
        job_id=job_id,
        controller_id=bundle.node_id,
        channels=channels,
        videos=videos,
        settings=_normalize_settings(metadata.get("settings")),
    )


def load_organization_job(
    path: str | os.PathLike,
    *,
    signing_key: str | bytes | None,
    require_signature: bool = True,
) -> OrganizationJob:
    return organization_job_from_bundle(
        load_bundle(
            path,
            signing_key=signing_key,
            require_signature=require_signature,
        )
    )


def _provider_channel(row: Mapping[str, Any]) -> ProviderChannel:
    return ProviderChannel(
        channel_id=str(row.get("channel_id") or ""),
        title=str(row.get("title") or ""),
        handle=str(row.get("handle") or ""),
        subscribers=max(0, int(row.get("subscribers") or 0)),
        video_count=max(0, int(row.get("video_count") or 0)),
        view_count=max(0, int(row.get("view_count") or 0)),
        description=str(row.get("description") or ""),
        url=str(row.get("url") or ""),
        published_at=str(row.get("channel_published_at") or ""),
        source=str(row.get("source") or "edge-organization-job"),
    )


def _provider_video(row: Mapping[str, Any]) -> ProviderVideo:
    category_id = str(row.get("category_id") or "")
    raw = (
        {"snippet": {"categoryId": category_id}}
        if category_id
        else {"edge_provider": str(row.get("source") or "organization-job")}
    )
    return ProviderVideo(
        video_id=str(row.get("video_id") or ""),
        title=str(row.get("title") or ""),
        channel=str(row.get("channel") or ""),
        channel_id=str(row.get("channel_id") or ""),
        views=max(0, int(row.get("views") or 0)),
        published_at=str(row.get("published_at") or ""),
        url=str(row.get("url") or ""),
        duration_seconds=max(0, int(row.get("duration_seconds") or 0)),
        description=str(row.get("description") or ""),
        thumbnail_url=str(row.get("thumbnail_url") or ""),
        source=str(row.get("source") or "edge-organization-job"),
        raw=raw,
    )


def _catalog_channel(
    channel: ProviderChannel,
    *,
    limit: int,
    provider_factory: Callable[[], CatalogProvider],
) -> tuple[list[ProviderVideo], int, str]:
    attempts = 0
    while attempts < EDGE_FREE_SEARCH_ATTEMPTS:
        attempts += 1
        try:
            provider = provider_factory()
            videos = provider.channel_videos(
                channel.channel_id,
                limit=limit,
            )
            normalized: list[ProviderVideo] = []
            seen: set[str] = set()
            for video in videos:
                if not video.video_id or video.video_id in seen:
                    continue
                seen.add(video.video_id)
                normalized.append(
                    replace(
                        video,
                        channel=video.channel or channel.title,
                        channel_id=video.channel_id or channel.channel_id,
                    )
                )
            return normalized, attempts, ""
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
            return [], attempts, _safe_error(exc)
    raise AssertionError("catalog retry loop exited unexpectedly")


def _merge_video(
    current: ProviderVideo | None,
    incoming: ProviderVideo,
) -> ProviderVideo:
    if current is None or incoming.source == "official-youtube":
        return incoming
    return current


def _category_id(video: ProviderVideo) -> str:
    if not isinstance(video.raw, dict):
        return ""
    snippet = video.raw.get("snippet")
    return (
        str(snippet.get("categoryId") or "")
        if isinstance(snippet, dict)
        else ""
    )


def _face_checker_factory(models_dir: Path) -> Callable[[], Any]:
    def create():
        from beacon_engine.research.face_check import FaceChecker

        return FaceChecker(models_dir=models_dir)

    return create


def collect_organization_job(
    job: OrganizationJob,
    *,
    node_id: str,
    run_id: str,
    captured_at: str | None = None,
    catalog_factory: Callable[[], CatalogProvider] = ScrapetubeProvider,
    pool_factory: Callable[[], OfficialPool] = daily_youtube_pool,
    face_checker_factory: Callable[[], Any] | None = None,
    models_dir: str | os.PathLike | None = None,
) -> OrganizationBatch:
    """Execute one bounded organization job without opening Signal's database."""
    captured = captured_at or utc_now()
    settings = job.settings
    channel_rows = list(job.channels)
    channels = {
        row["channel_id"]: _provider_channel(row)
        for row in channel_rows
    }
    categories = {
        row["channel_id"]: str(row.get("category") or "")
        for row in channel_rows
    }
    videos: dict[str, ProviderVideo] = {}
    for row in job.videos:
        video = _provider_video(row)
        videos[video.video_id] = video

    warnings: list[str] = []
    outcomes: dict[str, dict[str, Any]] = {
        channel_id: {
            "channel_id": channel_id,
            "started_at": captured,
            "catalog_attempted": False,
            "catalog_status": "not_requested",
            "catalog_attempts": 0,
            "videos_collected": 0,
            "categories_filled": 0,
            "face_status": "not_requested",
            "error": "",
        }
        for channel_id in channels
    }

    pool: OfficialPool | None = None
    pool_start_units = 0
    quota_ceiling = 0
    if (
        settings.max_quota_units > 0
        and (
            settings.catalog_enabled
            or settings.categories_enabled
            or settings.channel_enrichment_enabled
        )
    ):
        try:
            pool = pool_factory()
            pool_start_units = int(pool.units or 0)
            quota_ceiling = pool_start_units + settings.max_quota_units
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                "official organization unavailable: " + _safe_error(exc)
            )

    uploads_by_channel: dict[str, str] = {}
    if (
        pool is not None
        and channels
        and (
            settings.catalog_enabled
            or settings.channel_enrichment_enabled
        )
        and int(pool.units or 0) < quota_ceiling
    ):
        capacity = max(0, quota_ceiling - int(pool.units or 0)) * 50
        ids = list(channels)[:capacity]
        part = (
            "snippet,statistics,contentDetails"
            if settings.channel_enrichment_enabled
            else "contentDetails"
        )
        try:
            for item in pool.channels(ids, part=part):
                channel_id = str(item.get("id") or "")
                if not channel_id:
                    continue
                content = item.get("contentDetails") or {}
                related = (
                    content.get("relatedPlaylists")
                    if isinstance(content, dict)
                    else {}
                ) or {}
                uploads_by_channel[channel_id] = str(
                    related.get("uploads") or ""
                )
                if settings.channel_enrichment_enabled:
                    channel = _channel_from_item(item)
                    if channel.channel_id:
                        channels[channel.channel_id] = channel
        except Exception as exc:  # noqa: BLE001
            warnings.append("official channel enrichment: " + _safe_error(exc))

    official_catalog_resolved: set[str] = set()
    playlist_fetch = (
        getattr(pool, "playlist_video_ids", None)
        if pool is not None
        else None
    )
    if (
        settings.catalog_enabled
        and callable(playlist_fetch)
        and int(pool.units or 0) < quota_ceiling
    ):
        official_targets = [
            (channel, uploads_by_channel.get(channel.channel_id, ""))
            for channel in channels.values()
            if (
                int(channel.subscribers or 0) >= settings.min_subscribers
                and uploads_by_channel.get(channel.channel_id, "")
            )
        ]
        units_per_catalog = max(
            1,
            (max(1, settings.catalog_videos) + 49) // 50,
        )
        remaining_units = max(
            0,
            quota_ceiling - int(pool.units or 0),
        )
        target_capacity = remaining_units // units_per_catalog
        selected_targets = official_targets[:target_capacity]
        if len(selected_targets) < len(official_targets):
            warnings.append(
                "official upload catalog stopped at the per-job quota budget"
            )
        workers = min(
            len(selected_targets),
            max(1, int(getattr(pool, "parallel_workers", 1) or 1)),
        )
        for channel, _playlist_id in selected_targets:
            outcome = outcomes[channel.channel_id]
            outcome["catalog_attempted"] = True
            outcome["catalog_attempts"] = (
                int(outcome["catalog_attempts"]) + 1
            )
        if selected_targets:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="edge-official-catalog",
            ) as executor:
                futures = {
                    executor.submit(
                        playlist_fetch,
                        playlist_id,
                        limit=settings.catalog_videos,
                    ): channel
                    for channel, playlist_id in selected_targets
                }
                for future in as_completed(futures):
                    channel = futures[future]
                    outcome = outcomes[channel.channel_id]
                    try:
                        video_ids = future.result()
                    except Exception as exc:  # noqa: BLE001
                        outcome["catalog_status"] = "error"
                        outcome["error"] = _safe_error(exc)
                        continue
                    official_catalog_resolved.add(channel.channel_id)
                    outcome["catalog_status"] = "ok"
                    outcome["error"] = ""
                    outcome["videos_collected"] = len(video_ids)
                    for video_id in video_ids:
                        videos[video_id] = _merge_video(
                            videos.get(video_id),
                            ProviderVideo(
                                video_id=video_id,
                                channel=channel.title,
                                channel_id=channel.channel_id,
                                source="official-youtube-catalog",
                                raw={
                                    "edge_provider": (
                                        "official-youtube-catalog"
                                    )
                                },
                            ),
                        )

    catalog_targets = [
        channel
        for channel in channels.values()
        if settings.catalog_enabled
        and int(channel.subscribers or 0) >= settings.min_subscribers
        and channel.channel_id not in official_catalog_resolved
    ]
    if catalog_targets:
        with ThreadPoolExecutor(
            max_workers=min(settings.catalog_workers, len(catalog_targets)),
            thread_name_prefix="edge-catalog",
        ) as executor:
            futures = {
                executor.submit(
                    _catalog_channel,
                    channel,
                    limit=settings.catalog_videos,
                    provider_factory=catalog_factory,
                ): channel
                for channel in catalog_targets
            }
            for future in as_completed(futures):
                channel = futures[future]
                collected, attempts, error = future.result()
                outcome = outcomes[channel.channel_id]
                outcome["catalog_attempted"] = True
                outcome["catalog_attempts"] = (
                    int(outcome["catalog_attempts"]) + attempts
                )
                outcome["catalog_status"] = "error" if error else "ok"
                outcome["error"] = error
                outcome["videos_collected"] = len(collected)
                for video in collected:
                    videos[video.video_id] = _merge_video(
                        videos.get(video.video_id),
                        video,
                    )

    for channel in channels.values():
        if (
            settings.catalog_enabled
            and int(channel.subscribers or 0) < settings.min_subscribers
        ):
            outcomes[channel.channel_id]["catalog_status"] = (
                "skipped_low_subscribers"
            )

    if (
        pool is not None
        and settings.categories_enabled
        and int(pool.units or 0) < quota_ceiling
    ):
        missing = [
            video_id
            for video_id, video in videos.items()
            if not _category_id(video)
        ]
        capacity = max(
            0,
            quota_ceiling - int(pool.units or 0),
        ) * 50
        selected_missing = missing[:capacity]
        if len(selected_missing) < len(missing):
            warnings.append(
                "video category enrichment stopped at the per-job quota budget"
            )
        if selected_missing:
            try:
                items = pool.videos(
                    selected_missing,
                    part="snippet,statistics,contentDetails",
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append("official video enrichment: " + _safe_error(exc))
            else:
                returned: set[str] = set()
                for item in items:
                    video = _video_from_item(item)
                    if not video.video_id:
                        continue
                    returned.add(video.video_id)
                    videos[video.video_id] = _merge_video(
                        videos.get(video.video_id),
                        video,
                    )
                for video_id in selected_missing:
                    if video_id in returned:
                        continue
                    current = videos[video_id]
                    videos[video_id] = replace(
                        current,
                        raw={"snippet": {"categoryId": "na"}},
                    )

    videos_by_channel: dict[str, list[ProviderVideo]] = {}
    for video in videos.values():
        if video.channel_id:
            videos_by_channel.setdefault(video.channel_id, []).append(video)

    face_rows: list[dict[str, Any]] = []
    if settings.face_check_enabled and channels:
        targets = [
            (
                channel_id,
                list(
                    dict.fromkeys(
                        video.thumbnail_url
                        for video in videos_by_channel.get(channel_id, [])
                        if video.thumbnail_url
                    )
                )[: settings.face_max_thumbnails],
            )
            for channel_id in channels
        ]
        targets = [
            item
            for item in targets
            if len(item[1]) >= settings.face_min_thumbnails
        ]
        if targets:
            factory = face_checker_factory or _face_checker_factory(
                Path(models_dir or "storage/models")
            )
            local = threading.local()

            def checker():
                instance = getattr(local, "checker", None)
                if instance is None:
                    instance = factory()
                    local.checker = instance
                return instance

            def check(item: tuple[str, list[str]]):
                channel_id, urls = item
                return checker().check_channel(
                    channel_id,
                    urls,
                    max_thumbnails=settings.face_max_thumbnails,
                )

            try:
                with ThreadPoolExecutor(
                    max_workers=min(settings.face_workers, len(targets)),
                    thread_name_prefix="edge-face",
                ) as executor:
                    future_map = {
                        executor.submit(check, item): item[0]
                        for item in targets
                    }
                    for future in as_completed(future_map):
                        channel_id = future_map[future]
                        try:
                            result = future.result()
                        except Exception as exc:  # noqa: BLE001
                            outcomes[channel_id]["face_status"] = "error"
                            if not outcomes[channel_id]["error"]:
                                outcomes[channel_id]["error"] = _safe_error(exc)
                            continue
                        verdict = str(result.verdict or "unknown")
                        if verdict not in _ALLOWED_VERDICTS:
                            verdict = "unknown"
                        outcomes[channel_id]["face_status"] = verdict
                        face_rows.append(
                            {
                                "channel_id": channel_id,
                                "thumbnails_checked": max(
                                    0,
                                    int(result.thumbnails_checked or 0),
                                ),
                                "faces_found": max(
                                    0,
                                    int(result.faces_found or 0),
                                ),
                                "recurring_count": max(
                                    0,
                                    int(result.recurring_count or 0),
                                ),
                                "verdict": verdict,
                                "confidence": max(
                                    0.0,
                                    min(1.0, float(result.confidence or 0)),
                                ),
                                "checked_at": captured,
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                warnings.append("face checking unavailable: " + _safe_error(exc))

    for channel_id, outcome in outcomes.items():
        if outcome["face_status"] == "not_requested":
            urls = [
                video.thumbnail_url
                for video in videos_by_channel.get(channel_id, [])
                if video.thumbnail_url
            ]
            if settings.face_check_enabled and len(set(urls)) < settings.face_min_thumbnails:
                outcome["face_status"] = "insufficient_thumbnails"

    result_channels = [
        _channel_record(
            channels[row["channel_id"]],
            captured_at=captured,
            category=categories.get(row["channel_id"], ""),
        )
        for row in channel_rows
    ]
    result_videos = [
        _video_record(video, captured)
        for video in videos.values()
    ]
    classifications = [
        _classification_record(
            channel_id,
            channels[channel_id].title,
            videos_by_channel.get(channel_id, []),
            captured_at=captured,
        )
        for channel_id in channels
    ]
    category_counts: dict[str, int] = {}
    for video in videos.values():
        if video.channel_id and _category_id(video):
            category_counts[video.channel_id] = (
                category_counts.get(video.channel_id, 0) + 1
            )

    run_rows: list[dict[str, Any]] = []
    enrichment_rows: list[dict[str, Any]] = []
    for channel_id, raw in outcomes.items():
        raw["categories_filled"] = category_counts.get(channel_id, 0)
        status = "error" if raw["error"] else "ok"
        run_rows.append(
            ChannelOrganizationOutcome(
                channel_id=channel_id,
                status=status,
                catalog_attempted=bool(raw["catalog_attempted"]),
                catalog_status=str(raw["catalog_status"]),
                catalog_attempts=int(raw["catalog_attempts"]),
                videos_collected=int(raw["videos_collected"]),
                categories_filled=int(raw["categories_filled"]),
                face_status=str(raw["face_status"]),
                error=str(raw["error"]),
                started_at=str(raw["started_at"]),
                finished_at=utc_now(),
            ).as_record()
        )
        if raw["catalog_attempted"]:
            enrichment_rows.append(
                {
                    "entity_type": "catalog",
                    "entity_id": channel_id,
                    "attempts": max(1, int(raw["catalog_attempts"])),
                    "status": str(raw["catalog_status"]),
                    "attempted_at": utc_now(),
                }
            )

    return OrganizationBatch(
        node_id=node_id,
        job_id=job.job_id,
        run_id=run_id,
        captured_at=captured,
        records={
            "channels": result_channels,
            "videos": result_videos,
            "channel_classifications": classifications,
            "channel_face_checks": face_rows,
            "enrichment_attempts": enrichment_rows,
            "organization_runs": run_rows,
        },
        quota_units=max(
            0,
            int(getattr(pool, "units", 0) or 0) - pool_start_units,
        ),
        warnings=tuple(warnings),
    )


class OrganizationState:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS organization_jobs (
                    revision TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_run_ts REAL NOT NULL DEFAULT 0,
                    result_bundle_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS organization_channels (
                    channel_id TEXT PRIMARY KEY,
                    processed_ts REAL NOT NULL,
                    source_job_revision TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ok'
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def row(self, revision: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM organization_jobs WHERE revision=?",
                (revision,),
            ).fetchone()

    def recent_channels(
        self,
        channel_ids: Iterable[str],
        *,
        cutoff_ts: float,
    ) -> set[str]:
        ids = list(dict.fromkeys(channel_ids))
        if not ids:
            return set()
        found: set[str] = set()
        with self.connect() as conn:
            for index in range(0, len(ids), 400):
                chunk = ids[index:index + 400]
                placeholders = ",".join("?" for _ in chunk)
                found.update(
                    str(row[0])
                    for row in conn.execute(
                        f"""
                        SELECT channel_id
                        FROM organization_channels
                        WHERE processed_ts >= ?
                          AND channel_id IN ({placeholders})
                        """,
                        [cutoff_ts, *chunk],
                    )
                )
        return found

    def mark_started(self, job: OrganizationJob, *, now_ts: float) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT attempts FROM organization_jobs WHERE revision=?",
                (job.revision,),
            ).fetchone()
            attempts = int(existing["attempts"] or 0) + 1 if existing else 1
            conn.execute(
                """
                INSERT INTO organization_jobs (
                    revision, job_id, status, attempts, next_run_ts,
                    result_bundle_id, error, updated_at
                ) VALUES (?, ?, 'running', ?, 0, '', '', ?)
                ON CONFLICT(revision) DO UPDATE SET
                    status='running',
                    attempts=excluded.attempts,
                    next_run_ts=0,
                    error='',
                    updated_at=excluded.updated_at
                """,
                (job.revision, job.job_id, attempts, utc_now()),
            )
        return attempts

    def mark_failed(
        self,
        job: OrganizationJob,
        *,
        attempts: int,
        error: str,
        now_ts: float,
    ) -> None:
        delay = min(3_600, 60 * (2 ** min(max(0, attempts - 1), 6)))
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE organization_jobs
                SET status='error', next_run_ts=?, error=?, updated_at=?
                WHERE revision=?
                """,
                (now_ts + delay, error[:400], utc_now(), job.revision),
            )

    def mark_completed(
        self,
        job: OrganizationJob,
        *,
        result_bundle_id: str,
        channel_ids: Iterable[str],
        now_ts: float,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE organization_jobs
                SET status='completed', next_run_ts=0, result_bundle_id=?,
                    error='', updated_at=?
                WHERE revision=?
                """,
                (result_bundle_id, utc_now(), job.revision),
            )
            conn.executemany(
                """
                INSERT INTO organization_channels (
                    channel_id, processed_ts, source_job_revision, status
                ) VALUES (?, ?, ?, 'ok')
                ON CONFLICT(channel_id) DO UPDATE SET
                    processed_ts=excluded.processed_ts,
                    source_job_revision=excluded.source_job_revision,
                    status='ok'
                """,
                [
                    (channel_id, now_ts, job.revision)
                    for channel_id in dict.fromkeys(channel_ids)
                    if channel_id
                ],
            )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _archive_job(paths: OrganizationWorkerPaths, source: Path) -> Path:
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    directory = paths.archive / month
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if target.exists():
        target = directory / (
            source.name
            + "."
            + dt.datetime.now(dt.timezone.utc).strftime("%H%M%S%f")
        )
    os.replace(source, target)
    return target


def _quarantine_job(paths: OrganizationWorkerPaths, source: Path) -> Path:
    paths.failed.mkdir(parents=True, exist_ok=True)
    target = paths.failed / source.name
    if target.exists():
        target = paths.failed / (
            source.name
            + "."
            + dt.datetime.now(dt.timezone.utc).strftime("%H%M%S%f")
        )
    os.replace(source, target)
    return target


class OrganizationWorker:
    def __init__(
        self,
        *,
        paths: OrganizationWorkerPaths,
        signing_key: str | bytes,
        node_id: str,
        collect_fn: Callable[..., OrganizationBatch] = collect_organization_job,
    ) -> None:
        if not signing_key:
            raise ValueError("the organization worker requires a signing key")
        self.paths = paths
        self.signing_key = signing_key
        self.node_id = str(node_id or "").strip()
        if not self.node_id:
            raise ValueError("the organization worker requires a node_id")
        self.collect_fn = collect_fn
        self.paths.ensure()
        self.state = OrganizationState(self.paths.state)

    def write_status(self, state: str, message: str = "", **extra: Any) -> None:
        payload = {
            "version": ORGANIZATION_STATE_VERSION,
            "node_id": self.node_id,
            "pid": os.getpid(),
            "state": state,
            "message": message,
            "updated_at": utc_now(),
            "queued_jobs": len(
                list(self.paths.jobs.glob("*.signal-edge.json.gz"))
            ),
        }
        payload.update(extra)
        _atomic_json(self.paths.status, payload)

    def _job_files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.paths.jobs.glob("*.signal-edge.json.gz")
                if path.is_file()
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
        )

    def run_once(
        self,
        *,
        max_jobs: int = 1,
        now_ts: float | None = None,
    ) -> list[OrganizationWorkerReport]:
        now = float(time.time() if now_ts is None else now_ts)
        reports: list[OrganizationWorkerReport] = []
        for path in self._job_files()[: max(1, min(int(max_jobs), 20))]:
            try:
                job = load_organization_job(
                    path,
                    signing_key=self.signing_key,
                )
            except Exception as exc:  # noqa: BLE001
                _quarantine_job(self.paths, path)
                reports.append(
                    OrganizationWorkerReport(
                        job_id="",
                        revision="",
                        status="invalid",
                        message=_safe_error(exc),
                    )
                )
                continue

            existing = self.state.row(job.revision)
            if existing is not None and existing["status"] == "completed":
                _archive_job(self.paths, path)
                reports.append(
                    OrganizationWorkerReport(
                        job_id=job.job_id,
                        revision=job.revision,
                        status="duplicate",
                        result_bundle=str(existing["result_bundle_id"] or ""),
                    )
                )
                continue
            if (
                existing is not None
                and float(existing["next_run_ts"] or 0.0) > now
            ):
                continue

            cutoff = now - (job.settings.refresh_after_days * 86_400)
            job_channel_ids = list(
                dict.fromkeys(
                    [
                        *(
                            str(row.get("channel_id") or "")
                            for row in job.channels
                        ),
                        *(
                            str(row.get("channel_id") or "")
                            for row in job.videos
                        ),
                    ]
                )
            )
            recent = self.state.recent_channels(
                job_channel_ids,
                cutoff_ts=cutoff,
            )
            filtered_channels = tuple(
                row
                for row in job.channels
                if row["channel_id"] not in recent
            )
            filtered_videos = tuple(
                row
                for row in job.videos
                if (
                    not str(row.get("channel_id") or "")
                    or str(row.get("channel_id") or "") not in recent
                )
            )
            filtered = replace(
                job,
                channels=filtered_channels,
                videos=filtered_videos,
            )
            if not filtered.channels and not filtered.videos:
                attempts = self.state.mark_started(job, now_ts=now)
                self.state.mark_completed(
                    job,
                    result_bundle_id="",
                    channel_ids=(),
                    now_ts=now,
                )
                _archive_job(self.paths, path)
                reports.append(
                    OrganizationWorkerReport(
                        job_id=job.job_id,
                        revision=job.revision,
                        status="skipped",
                        message=(
                            f"all channels were organized within the last "
                            f"{job.settings.refresh_after_days} days"
                        ),
                    )
                )
                continue

            attempts = self.state.mark_started(job, now_ts=now)
            run_id = f"org-{attempts:04d}-{job.revision[:12]}"
            self.write_status(
                "running",
                f"organizing {len(filtered.channels)} channels",
                job_id=job.job_id,
                revision=job.revision,
                channels=len(filtered.channels),
                videos=len(filtered.videos),
                skipped_recent_channels=len(recent),
            )
            try:
                batch = self.collect_fn(
                    filtered,
                    node_id=self.node_id,
                    run_id=run_id,
                    captured_at=utc_now(),
                    models_dir=self.paths.models,
                )
                result = build_bundle(
                    node_id=self.node_id,
                    kind=ORGANIZATION_RESULT_KIND,
                    job_id=job.job_id,
                    records=batch.records,
                    metadata={
                        **batch.metadata(),
                        "source_job_revision": job.revision,
                        "controller_id": job.controller_id,
                        "settings": job.settings.as_dict(),
                        "skipped_recent_channels": len(recent),
                    },
                    created_at=batch.captured_at,
                    signing_key=self.signing_key,
                )
                result_path = write_bundle(result, self.paths.outbox)
                self.state.mark_completed(
                    job,
                    result_bundle_id=result.bundle_id,
                    channel_ids=(
                        str(row.get("channel_id") or "")
                        for row in (
                            *filtered.channels,
                            *filtered.videos,
                        )
                    ),
                    now_ts=now,
                )
                _archive_job(self.paths, path)
                reports.append(
                    OrganizationWorkerReport(
                        job_id=job.job_id,
                        revision=job.revision,
                        status="completed",
                        result_bundle=str(result_path),
                        channels=len(filtered.channels),
                        videos=len(batch.records.get("videos", [])),
                        quota_units=batch.quota_units,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                error = _safe_error(exc)
                self.state.mark_failed(
                    job,
                    attempts=attempts,
                    error=error,
                    now_ts=now,
                )
                reports.append(
                    OrganizationWorkerReport(
                        job_id=job.job_id,
                        revision=job.revision,
                        status="error",
                        message=error,
                    )
                )

        state = "idle" if reports else "waiting"
        self.write_status(
            state,
            "organization scheduler pass complete",
            reports=[report.as_dict() for report in reports],
        )
        return reports

    def run_forever(self, *, poll_seconds: int = 15) -> None:
        import signal

        delay = max(1, min(int(poll_seconds), 300))
        stop_event = threading.Event()

        def request_stop(_signum=None, _frame=None) -> None:
            stop_event.set()

        previous_handlers: dict[int, Any] = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
            except (ValueError, OSError):
                pass
        try:
            with EdgeWorkerLock(self.paths.lock):
                self.write_status("starting", "organization worker starting")
                while not stop_event.is_set():
                    if self.paths.stop.exists():
                        self.paths.stop.unlink(missing_ok=True)
                        break
                    try:
                        self.run_once()
                    except Exception as exc:  # noqa: BLE001
                        self.write_status("error", _safe_error(exc))
                    stop_event.wait(delay)
                self.write_status(
                    "stopped",
                    "organization worker stopped cooperatively",
                )
        finally:
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass


def read_organization_status(path: str | os.PathLike) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_discovery_organization_jobs(
    *,
    channels: Iterable[Mapping[str, Any]],
    videos: Iterable[Mapping[str, Any]],
    jobs_dir: str | os.PathLike,
    controller_id: str,
    source_job_id: str,
    source_run_id: str,
    signing_key: str | bytes,
    created_at: str,
    settings: OrganizationSettings | Mapping[str, Any] | None = None,
    channels_per_job: int = DEFAULT_DISCOVERY_CHANNELS_PER_JOB,
) -> list[Path]:
    """Split one discovery batch into bounded immutable organizer jobs."""
    channel_rows = list(_normalize_channels(channels, maximum=None))
    video_rows = list(_normalize_videos(videos, maximum=None))
    per_job = max(1, min(int(channels_per_job), MAX_JOB_CHANNELS))
    by_channel: dict[str, list[dict[str, Any]]] = {}
    unattached: list[dict[str, Any]] = []
    for video in video_rows:
        channel_id = str(video.get("channel_id") or "")
        if channel_id:
            by_channel.setdefault(channel_id, []).append(video)
        else:
            unattached.append(video)

    written: list[Path] = []
    for index in range(0, len(channel_rows), per_job):
        chunk = channel_rows[index:index + per_job]
        chunk_ids = {row["channel_id"] for row in chunk}
        chunk_videos = [
            video
            for channel_id in chunk_ids
            for video in by_channel.get(channel_id, [])
        ]
        bundle = build_organization_job(
            controller_id=controller_id,
            job_id=f"org-{source_run_id}-{(index // per_job) + 1:03d}",
            channels=chunk,
            videos=chunk_videos,
            settings=settings,
            source={
                "kind": "discovery",
                "job_id": source_job_id,
                "run_id": source_run_id,
            },
            created_at=created_at,
            signing_key=signing_key,
        )
        written.append(write_bundle(bundle, jobs_dir))

    if unattached:
        for index in range(0, len(unattached), MAX_JOB_VIDEOS):
            chunk = unattached[index:index + MAX_JOB_VIDEOS]
            bundle = build_organization_job(
                controller_id=controller_id,
                job_id=f"org-{source_run_id}-videos-{(index // MAX_JOB_VIDEOS) + 1:03d}",
                videos=chunk,
                settings=settings,
                source={
                    "kind": "discovery",
                    "job_id": source_job_id,
                    "run_id": source_run_id,
                },
                created_at=created_at,
                signing_key=signing_key,
            )
            written.append(write_bundle(bundle, jobs_dir))
    return written
