"""Provider-routed YouTube crawler that feeds the local research index."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from beacon_engine.logging_util import get_logger
from beacon_engine.research.providers import (
    ProviderChannel,
    ProviderComment,
    ProviderError,
    ProviderTranscript,
    ProviderUnavailable,
    ProviderVideo,
    normalize_channel_id,
)
from beacon_engine.research.providers.comments_provider import CommentDownloaderProvider
from beacon_engine.research.providers.invidious_provider import InvidiousProvider
from beacon_engine.research.providers.official_youtube_provider import OfficialYouTubeProvider
from beacon_engine.research.providers.piped_provider import PipedProvider
from beacon_engine.research.providers.scrapetube_provider import ScrapetubeProvider
from beacon_engine.research.providers.transcript_provider import TranscriptProvider
from beacon_engine.research.providers.ytdlp_provider import YtDlpProvider
from beacon_engine.research.faceless_filter import looks_faceless_by_text, score_channel_text
from beacon_engine.research.store import YouTubeStore

log = get_logger()


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 50) -> list[ProviderVideo]: ...


class ChannelProvider(Protocol):
    name: str

    def channel_videos(self, channel: str, *, limit: int = 200) -> list[ProviderVideo]: ...


class VideoProvider(Protocol):
    name: str

    def video(self, url_or_id: str) -> ProviderVideo | None: ...


class CommentsProvider(Protocol):
    name: str

    def comments(self, url_or_id: str, *, limit: int = 100) -> list[ProviderComment]: ...


@dataclass
class CrawlReport:
    kind: str
    query: str
    requested: int
    discovered: int = 0
    channels: int = 0
    stored: int = 0
    enriched: int = 0
    comments: int = 0
    transcripts: int = 0
    providers_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "requested": self.requested,
            "discovered": self.discovered,
            "channels": self.channels,
            "stored": self.stored,
            "enriched": self.enriched,
            "comments": self.comments,
            "transcripts": self.transcripts,
            "providers_used": self.providers_used,
            "errors": self.errors,
        }


class YouTubeCrawler:
    """A free-provider-first crawler with official API fallback."""

    # Provider circuit breaker, shared process-wide: a provider that keeps
    # failing (dead Piped/Invidious mirrors) is skipped for a cooldown instead
    # of adding its connection-timeout to EVERY crawl call.
    _breaker: dict[str, dict] = {}
    _breaker_lock = threading.Lock()
    _BREAKER_THRESHOLD = 3
    _BREAKER_COOLDOWN = 600.0   # seconds a dead provider sits out

    @classmethod
    def _provider_blocked(cls, name: str) -> bool:
        import time as _t
        with cls._breaker_lock:
            state = cls._breaker.get(name)
            if not state:
                return False
            if state.get("blocked_until", 0) > _t.time():
                return True
            if state.get("blocked_until"):
                state["fails"] = 0          # cooldown over — fresh chance
                state["blocked_until"] = 0
            return False

    @classmethod
    def _provider_result(cls, name: str, ok: bool) -> None:
        import time as _t
        with cls._breaker_lock:
            state = cls._breaker.setdefault(name, {"fails": 0, "blocked_until": 0})
            if ok:
                state["fails"] = 0
                state["blocked_until"] = 0
                return
            state["fails"] += 1
            if state["fails"] >= cls._BREAKER_THRESHOLD and not state["blocked_until"]:
                state["blocked_until"] = _t.time() + cls._BREAKER_COOLDOWN
                log.warning("provider '%s' failed %d times — skipping it for %.0f min",
                            name, state["fails"], cls._BREAKER_COOLDOWN / 60)

    def __init__(self, store: YouTubeStore | None = None) -> None:
        self.store = store or YouTubeStore()
        self.search_providers: list[SearchProvider] = []
        self.channel_providers: list[ChannelProvider] = []
        self.video_providers: list[VideoProvider] = []
        self.comment_providers: list[CommentsProvider] = []
        self.transcript_provider = TranscriptProvider()
        self._load_providers()

    def provider_names(self) -> dict[str, list[str]]:
        return {
            "search": [p.name for p in self.search_providers],
            "channel": [p.name for p in self.channel_providers],
            "video": [p.name for p in self.video_providers],
            "comments": [p.name for p in self.comment_providers],
            "transcripts": [self.transcript_provider.name],
        }

    def crawl_search(
        self,
        query: str,
        *,
        limit: int = 100,
        enrich: bool = True,
        comments_per_video: int = 0,
        transcripts: bool = False,
        providers: list[str] | None = None,
        sort: str | None = None,
    ) -> CrawlReport:
        report = CrawlReport(kind="search", query=query, requested=limit)
        videos = self._discover_search(
            query, limit=limit, report=report, providers=providers, sort=sort)
        self._store_discovery(videos, report, search_query=query)
        selected = videos[:limit]
        if enrich:
            report.enriched += self.refresh_videos(
                [v.video_id for v in selected],
                report=report,
                store_base=False,
                providers=providers,
            )
        self._extras(selected, report, comments_per_video=comments_per_video, transcripts=transcripts)
        return report

    def fetch_search(
        self,
        query: str,
        *,
        limit: int = 100,
        providers: list[str] | None = None,
        sort: str | None = None,
    ) -> tuple[list[ProviderVideo], CrawlReport]:
        """Fetch one search without touching SQLite.

        Radar worker threads use this method for overlapping provider I/O. The
        caller then serializes all writes through `store_fetched_search()` so
        SQLite and per-query yield tracking stay deterministic.
        """
        report = CrawlReport(kind="search", query=query, requested=limit)
        all_videos: list[ProviderVideo] = []
        seen: set[str] = set()
        for provider in self.search_providers:
            if providers and provider.name not in providers:
                continue
            if self._provider_blocked(provider.name):
                continue
            try:
                extra = {"sort": sort} if (sort and provider.name == "scrapetube") else {}
                videos = provider.search(query, limit=limit, **extra)
                self._provider_result(provider.name, True)
                self._mark_used(report, provider.name)
                for video in videos:
                    if video.video_id and video.video_id not in seen:
                        all_videos.append(video)
                        seen.add(video.video_id)
                    if len(all_videos) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                self._record_error(report, provider.name, exc)
                self._provider_result(provider.name, False)
            if len(all_videos) >= limit:
                break
        report.discovered = len(all_videos)
        return all_videos, report

    def store_fetched_search(
        self,
        query: str,
        videos: list[ProviderVideo],
        report: CrawlReport,
    ) -> None:
        """Persist one network-only search result in a serialized step."""
        provider_label = ",".join(report.providers_used) or "none"
        run_id = self.store.start_run(
            kind="search", query=query, provider=provider_label)
        try:
            self._store_discovery(videos, report)
            for provider in {v.source for v in videos if v.source}:
                self.store.save_search_results(
                    query,
                    provider,
                    [v for v in videos if v.source == provider],
                    run_id=run_id,
                )
            status = "error" if report.errors and not videos else "ok"
            self.store.finish_run(
                run_id,
                status=status,
                message="; ".join(report.errors[:3]) if status == "error" else "",
                counts={"videos": len(videos)},
            )
        except Exception as exc:
            self.store.finish_run(
                run_id,
                status="error",
                message=f"{type(exc).__name__}: {exc}",
            )
            raise

    def crawl_channel(
        self,
        channel: str,
        *,
        limit: int = 200,
        enrich: bool = True,
        comments_per_video: int = 0,
        transcripts: bool = False,
        providers: list[str] | None = None,
    ) -> CrawlReport:
        report = CrawlReport(kind="channel", query=channel, requested=limit)
        videos = self._discover_channel(channel, limit=limit, report=report, providers=providers)
        self._store_discovery(videos, report)
        selected = videos[:limit]
        if enrich:
            report.enriched += self.refresh_videos(
                [v.video_id for v in selected],
                report=report,
                store_base=False,
                providers=providers,
            )
        self._extras(selected, report, comments_per_video=comments_per_video, transcripts=transcripts)
        return report

    def crawl_niche(
        self,
        query: str,
        *,
        seed_limit: int = 50,
        max_channels: int = 5,
        videos_per_channel: int = 100,
        enrich: bool = False,
        comments_per_video: int = 0,
        transcripts: bool = False,
        extras_limit: int = 20,
        providers: list[str] | None = None,
        faceless_only: bool = False,
        on_step: Callable[[str], None] = lambda _m: None,
    ) -> CrawlReport:
        """Search a niche, crawl discovered channels, and save everything locally."""
        report = CrawlReport(kind="niche", query=query, requested=seed_limit)
        on_step(f"Searching seed videos for '{query}'...")
        seed_videos = self._discover_search(
            query, limit=seed_limit, report=report, providers=providers)
        self._store_discovery(seed_videos, report, search_query=query)

        channel_ids = _top_channel_ids(
            seed_videos,
            max_channels=max_channels,
            faceless_only=faceless_only,
        )
        if faceless_only:
            on_step(f"Faceless text filter kept {len(channel_ids)} seed channels.")
        if faceless_only and not channel_ids and seed_videos:
            report.errors.append("Faceless text filter found no channels; skipped channel expansion.")
        report.channels = len(channel_ids)
        on_step(f"Found {len(channel_ids)} channels to expand.")

        all_channel_videos: list[ProviderVideo] = []
        seen: set[str] = {v.video_id for v in seed_videos if v.video_id}
        for idx, channel_id in enumerate(channel_ids, 1):
            on_step(
                f"Crawling channel {idx}/{len(channel_ids)} "
                f"({channel_id[-6:]})..."
            )
            sub = CrawlReport(kind="channel", query=channel_id, requested=videos_per_channel)
            channel_videos = self._discover_channel(
                channel_id, limit=videos_per_channel, report=sub, providers=providers)
            for name in sub.providers_used:
                self._mark_used(report, name)
            for err in sub.errors:
                if err not in report.errors:
                    report.errors.append(err)
            for video in channel_videos:
                if video.video_id and video.video_id not in seen:
                    all_channel_videos.append(video)
                    seen.add(video.video_id)

        report.discovered = len(seed_videos) + len(all_channel_videos)
        self._store_discovery(all_channel_videos, report)

        candidates = sorted(
            seed_videos + all_channel_videos,
            key=lambda v: int(v.views or 0),
            reverse=True,
        )
        if enrich:
            on_step(f"Enriching top {min(len(candidates), extras_limit)} videos...")
            report.enriched += self.refresh_videos(
                [v.video_id for v in candidates[:extras_limit]],
                report=report,
                store_base=False,
                providers=providers,
            )
        if comments_per_video > 0 or transcripts:
            on_step("Fetching comments/transcripts for top videos...")
        self._extras(
            candidates[:extras_limit],
            report,
            comments_per_video=comments_per_video,
            transcripts=transcripts,
        )
        return report

    def refresh_videos(
        self,
        video_ids: Iterable[str],
        *,
        report: CrawlReport | None = None,
        store_base: bool = True,
        providers: list[str] | None = None,
    ) -> int:
        count = 0
        for vid in dict.fromkeys(v for v in video_ids if v):
            if store_base:
                self.store.upsert_video(ProviderVideo(video_id=vid, source="manual"))
            video = self._first_video(vid, report=report, providers=providers)
            if video:
                self.store.upsert_video(video)
                count += 1
        return count

    def _discover_search(
        self,
        query: str,
        *,
        limit: int,
        report: CrawlReport,
        providers: list[str] | None,
        sort: str | None = None,
    ) -> list[ProviderVideo]:
        all_videos: list[ProviderVideo] = []
        seen: set[str] = set()
        for provider in self.search_providers:
            if providers and provider.name not in providers:
                continue
            if self._provider_blocked(provider.name):
                continue
            run_id = self.store.start_run(kind="search", query=query, provider=provider.name)
            try:
                # Only scrapetube supports upload-date ordering; every other
                # provider is called EXACTLY as before (no new kwarg) so nothing
                # else in either app changes.
                extra = {"sort": sort} if (sort and provider.name == "scrapetube") else {}
                videos = provider.search(query, limit=limit, **extra)
                self.store.save_search_results(
                    query,
                    provider.name,
                    videos,
                    run_id=run_id,
                )
                self.store.finish_run(run_id, status="ok", counts={"videos": len(videos)})
                self._provider_result(provider.name, True)
                self._mark_used(report, provider.name)
                for video in videos:
                    if video.video_id and video.video_id not in seen:
                        all_videos.append(video)
                        seen.add(video.video_id)
                    if len(all_videos) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                self.store.finish_run(run_id, status="error", message=str(exc))
                self._record_error(report, provider.name, exc)
                self._provider_result(provider.name, False)
            if len(all_videos) >= limit:
                break
        report.discovered = len(all_videos)
        return all_videos

    def _discover_channel(
        self,
        channel: str,
        *,
        limit: int,
        report: CrawlReport,
        providers: list[str] | None,
    ) -> list[ProviderVideo]:
        all_videos: list[ProviderVideo] = []
        seen: set[str] = set()
        for provider in self.channel_providers:
            if providers and provider.name not in providers:
                continue
            if self._provider_blocked(provider.name):
                continue
            run_id = self.store.start_run(kind="channel", query=channel, provider=provider.name)
            try:
                report.step = f"{provider.name}:{channel}"
                videos = provider.channel_videos(channel, limit=limit)
                # Channel listings often omit per-video channel_id; we know which
                # channel we're crawling, so stamp it -> videos link to the channel.
                cid = normalize_channel_id(channel)
                if cid:
                    for v in videos:
                        if not v.channel_id:
                            v.channel_id = cid
                self.store.save_search_results(
                    channel,
                    provider.name,
                    videos,
                    run_id=run_id,
                    kind="channel",
                )
                self.store.finish_run(run_id, status="ok", counts={"videos": len(videos)})
                self._provider_result(provider.name, True)
                self._mark_used(report, provider.name)
                for video in videos:
                    if video.video_id and video.video_id not in seen:
                        all_videos.append(video)
                        seen.add(video.video_id)
                    if len(all_videos) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                self.store.finish_run(run_id, status="error", message=str(exc))
                self._record_error(report, provider.name, exc)
                self._provider_result(provider.name, False)
            if len(all_videos) >= limit:
                break
        report.discovered = len(all_videos)
        return all_videos

    def _store_discovery(
        self,
        videos: list[ProviderVideo],
        report: CrawlReport,
        *,
        search_query: str = "",
    ) -> None:
        self._store_channels_from_videos(videos)
        report.stored += self.store.upsert_videos(videos)
        if search_query:
            for provider in {v.source for v in videos if v.source}:
                self.store.save_search_results(
                    search_query,
                    provider,
                    [v for v in videos if v.source == provider],
                    record_event=False,
                )

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

    def _first_video(
        self,
        video_id: str,
        *,
        report: CrawlReport | None = None,
        providers: list[str] | None = None,
    ) -> ProviderVideo | None:
        for provider in self.video_providers:
            if providers and provider.name not in providers:
                continue
            try:
                video = provider.video(video_id)
                if video:
                    if report:
                        self._mark_used(report, provider.name)
                    return video
            except Exception as exc:  # noqa: BLE001
                if report:
                    self._record_error(report, provider.name, exc)
        return None

    def _extras(
        self,
        videos: list[ProviderVideo],
        report: CrawlReport,
        *,
        comments_per_video: int,
        transcripts: bool,
    ) -> None:
        if comments_per_video > 0:
            for video in videos:
                report.comments += self._comments(video.video_id, limit=comments_per_video, report=report)
        if transcripts:
            for video in videos:
                if self.store.save_transcript(self._transcript(video.video_id, report=report)):
                    report.transcripts += 1

    def _comments(self, video_id: str, *, limit: int, report: CrawlReport) -> int:
        for provider in self.comment_providers:
            try:
                comments = provider.comments(video_id, limit=limit)
                if comments:
                    self._mark_used(report, provider.name)
                    return self.store.save_comments(comments)
            except Exception as exc:  # noqa: BLE001
                self._record_error(report, provider.name, exc)
        return 0

    def _transcript(
        self, video_id: str, *, report: CrawlReport
    ) -> ProviderTranscript | None:
        try:
            transcript = self.transcript_provider.transcript(video_id)
            if transcript:
                self._mark_used(report, self.transcript_provider.name)
            return transcript
        except Exception as exc:  # noqa: BLE001
            self._record_error(report, self.transcript_provider.name, exc)
            return None

    def _load_providers(self) -> None:
        for cls, buckets in [
            # scrapetube is excellent for search discovery, but current YouTube channel
            # pages often return 0 or stall through it. Use yt-dlp/Piped/Invidious for
            # channel expansion so the app does not sit on "Crawling channel 3/7".
            (ScrapetubeProvider, ("search",)),
            (YtDlpProvider, ("search", "channel", "video")),
            (CommentDownloaderProvider, ("comments",)),
            (PipedProvider, ("channel", "video", "comments")),
            (InvidiousProvider, ("search", "channel", "video", "comments")),
            (OfficialYouTubeProvider, ("search", "channel", "video")),
        ]:
            try:
                provider = cls()
            except ProviderUnavailable as exc:
                log.info("crawler provider unavailable: %s", exc)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("crawler provider failed to initialize: %s", exc)
                continue
            for bucket in buckets:
                if bucket == "search":
                    self.search_providers.append(provider)
                elif bucket == "channel":
                    self.channel_providers.append(provider)
                elif bucket == "video":
                    self.video_providers.append(provider)
                elif bucket == "comments":
                    self.comment_providers.append(provider)

    def _mark_used(self, report: CrawlReport, provider: str) -> None:
        if provider not in report.providers_used:
            report.providers_used.append(provider)

    def _record_error(self, report: CrawlReport, provider: str, exc: Exception) -> None:
        if isinstance(exc, ProviderError):
            msg = f"{provider}: {exc}"
        else:
            msg = f"{provider}: {type(exc).__name__}: {exc}"
        if msg not in report.errors:
            report.errors.append(msg)
        log.warning("crawler provider error: %s", msg)


def _top_channel_ids(
    videos: list[ProviderVideo],
    *,
    max_channels: int,
    faceless_only: bool = False,
) -> list[str]:
    scores: dict[str, int] = {}
    grouped: dict[str, list[ProviderVideo]] = {}
    for video in videos:
        if not video.channel_id:
            continue
        grouped.setdefault(video.channel_id, []).append(video)
        scores[video.channel_id] = scores.get(video.channel_id, 0) + max(int(video.views or 0), 1)
    if faceless_only:
        filtered: dict[str, int] = {}
        for channel_id, vids in grouped.items():
            channel_title = next((v.channel for v in vids if v.channel), "")
            if not looks_faceless_by_text(vids, channel_title=channel_title):
                continue
            signal = score_channel_text(vids, channel_title=channel_title)
            boost = max(0.4, 1.0 + min(signal.score, 5.0) * 0.12)
            filtered[channel_id] = int(scores[channel_id] * boost)
        scores = filtered
    return [
        channel_id
        for channel_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    ][:max_channels]
