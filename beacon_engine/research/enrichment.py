"""Cheap, batched stats backfill + deep cataloging to grow the local index.

Discovery (free providers) finds video ids quickly but rarely fills exact view
and subscriber counts -- the two numbers that drive every outlier score. This
engine fills them in using the official YouTube Data API in BATCH mode, where
``videos.list`` / ``channels.list`` each return up to 50 items for a single
quota unit. One free 10,000-unit/day key therefore backfills ~500,000 video
stats per day.

Three jobs:

* ``backfill_channel_stats`` -- fill subscriber counts (50 channels = 1 unit).
* ``backfill_video_stats``   -- fill view counts (50 videos = 1 unit).
* ``catalog_channels``       -- pull every known channel's full upload list via
  the free providers (no quota), exploding the pile so the stat backfills then
  have far more to fill.

Everything is quota-aware: pass ``max_units`` to cap how much of the daily
budget a single run may spend.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from beacon_engine.logging_util import get_logger
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.providers.official_youtube_provider import OfficialYouTubeProvider
from beacon_engine.research.store import YouTubeStore

def _retry_locked(fn: Callable[[], None], *, tries: int = 5, wait: float = 20.0) -> None:
    """Run fn, retrying when SQLite reports 'database is locked' (a concurrent
    radar/blitz write burst can hold the lock past busy_timeout)."""
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 == tries:
                raise
            log.warning("DB locked (attempt %d/%d) — waiting %.0fs", attempt + 1, tries, wait)
            time.sleep(wait)

log = get_logger()

Progress = Callable[[str], None]

# Official YouTube Data API v3 free daily quota for one Cloud project.
DAILY_QUOTA_UNITS = 10_000
# videos.list / channels.list return up to 50 items for 1 quota unit.
BATCH = 50


@dataclass
class EnrichReport:
    kind: str
    requested: int = 0
    fetched: int = 0
    updated: int = 0
    batches: int = 0
    quota_units: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "requested": self.requested,
            "fetched": self.fetched,
            "updated": self.updated,
            "batches": self.batches,
            "quota_units": self.quota_units,
            "errors": self.errors,
        }


@dataclass
class CatalogReport:
    channels_requested: int = 0
    channels_crawled: int = 0
    videos_discovered: int = 0
    videos_stored: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "channels_requested": self.channels_requested,
            "channels_crawled": self.channels_crawled,
            "videos_discovered": self.videos_discovered,
            "videos_stored": self.videos_stored,
            "errors": self.errors,
        }


class EnrichmentEngine:
    """Fill missing stats and deep-catalog channels using the official API."""

    def __init__(
        self,
        store: YouTubeStore | None = None,
        *,
        api_key: str | None = None,
        provider: OfficialYouTubeProvider | None = None,
    ) -> None:
        self.store = store or YouTubeStore()
        self._api_key = api_key
        # The provider (and its API-key requirement) is built lazily so that
        # catalog_channels -- which only uses free providers -- works with no key.
        self._provider = provider

    @property
    def provider(self) -> OfficialYouTubeProvider:
        """The official API provider; raises ProviderUnavailable if no key."""
        if self._provider is None:
            self._provider = OfficialYouTubeProvider(self._api_key)
        return self._provider

    # -- stats backfill -----------------------------------------------------

    def backfill_channel_stats(
        self,
        *,
        limit: int | None = None,
        max_units: int | None = None,
        max_attempts: int = 3,
        missing: str = "subs",
        on_step: Progress = lambda _m: None,
    ) -> EnrichReport:
        # 'subs' -> channels with no subscriber count; 'meta' -> channels with no
        # creation date (needed for "days since start"). Both come from one call.
        if missing == "meta":
            ids = self.store.ids_needing_channel_meta(limit=limit, max_attempts=max_attempts)
            log_entity = "channel_meta"
            need = "creation dates"
        else:
            ids = self.store.ids_needing_channel_stats(limit=limit, max_attempts=max_attempts)
            log_entity = "channel"
            need = "subscriber counts"
        report = EnrichReport(kind=f"channel-{missing}", requested=len(ids))
        on_step(f"{len(ids):,} channels need {need}")
        for chunk in _chunks(ids, BATCH):
            if max_units is not None and report.quota_units >= max_units:
                break
            try:
                channels = self.provider.channels(chunk)
            except Exception as exc:  # noqa: BLE001
                self._record_error(report, "channels.list", exc)
                continue
            report.quota_units += 1
            report.batches += 1
            report.fetched += len(channels)
            for channel in channels:
                self.store.upsert_channel(channel)
                report.updated += 1
            self.store.record_enrichment_attempt(log_entity, chunk, status="ok")
            on_step(
                f"channels: +{report.updated:,} updated "
                f"({report.quota_units} units used)"
            )
        return report

    def backfill_video_stats(
        self,
        *,
        limit: int | None = None,
        max_units: int | None = None,
        channel_ids: Iterable[str] | None = None,
        max_attempts: int = 3,
        capture_channels: bool = True,
        on_step: Progress = lambda _m: None,
    ) -> EnrichReport:
        ids = self.store.ids_needing_video_stats(
            limit=limit,
            channel_ids=channel_ids,
            max_attempts=max_attempts,
        )
        report = EnrichReport(kind="video-stats", requested=len(ids))
        on_step(f"{len(ids):,} videos need view counts")
        for chunk in _chunks(ids, BATCH):
            if max_units is not None and report.quota_units >= max_units:
                break
            try:
                videos = self.provider.videos(chunk)
            except Exception as exc:  # noqa: BLE001
                self._record_error(report, "videos.list", exc)
                continue
            report.quota_units += 1
            report.batches += 1
            report.fetched += len(videos)

            def _write_chunk() -> None:
                for video in videos:
                    self.store.upsert_video(video)
                if capture_channels:
                    self._store_channels_from_videos(videos)
                # Mark every requested id (incl. ids the API dropped as dead) so
                # we do not re-fetch deleted/private videos on the next run.
                self.store.record_enrichment_attempt("video", chunk, status="ok")

            # A radar write burst can hold the DB lock past busy_timeout; one
            # locked write must not kill a multi-hour enrichment run.
            _retry_locked(_write_chunk)
            report.updated += len(videos)
            on_step(
                f"videos: +{report.updated:,} filled "
                f"({report.quota_units} units used)"
            )
        return report

    # -- deep discovery -----------------------------------------------------

    def catalog_channels(
        self,
        *,
        limit: int | None = None,
        videos_per_channel: int = 500,
        providers: list[str] | None = None,
        channel_ids: Iterable[str] | None = None,
        on_step: Progress = lambda _m: None,
    ) -> CatalogReport:
        """Pull every known channel's full upload list (free providers, no quota).

        This is the main "make the database bigger" lever: each faceless channel
        can carry hundreds of videos the radar never collected. After this, run
        ``backfill_video_stats`` to fill the newly discovered ids cheaply.
        """
        from beacon_engine.research.crawler import YouTubeCrawler

        targets = list(dict.fromkeys(
            c.strip() for c in (channel_ids or []) if c and c.strip()
        )) or self.store.known_channel_ids(limit=limit)
        report = CatalogReport(channels_requested=len(targets))
        crawler = YouTubeCrawler(self.store)
        on_step(f"Cataloging {len(targets):,} channels (up to {videos_per_channel} each)")
        for idx, channel_id in enumerate(targets, 1):
            on_step(f"Channel {idx}/{len(targets)} ({channel_id[-6:]})")
            try:
                sub = crawler.crawl_channel(
                    channel_id,
                    limit=videos_per_channel,
                    enrich=False,  # stats come later via the cheap batch API
                    providers=providers,
                )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{channel_id}: {type(exc).__name__}: {exc}")
                continue
            report.channels_crawled += 1
            report.videos_discovered += sub.discovered
            report.videos_stored += sub.stored
            for err in sub.errors:
                if err not in report.errors:
                    report.errors.append(err)
        return report

    # -- helpers ------------------------------------------------------------

    def _store_channels_from_videos(self, videos: list[ProviderVideo]) -> None:
        seen: set[str] = set()
        for video in videos:
            if not video.channel_id or video.channel_id in seen:
                continue
            seen.add(video.channel_id)
            self.store.upsert_channel(ProviderChannel(
                channel_id=video.channel_id,
                title=video.channel,
                url=f"https://www.youtube.com/channel/{video.channel_id}",
                source=video.source,
            ))

    def _record_error(self, report: EnrichReport, where: str, exc: Exception) -> None:
        msg = f"{where}: {type(exc).__name__}: {exc}"
        if msg not in report.errors:
            report.errors.append(msg)
        log.warning("enrichment error: %s", msg)


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
