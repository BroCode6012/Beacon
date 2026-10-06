"""Standalone faceless radar worker.

The desktop Scraper tab is a control/view. This module is the durable worker that
can run from Task Scheduler or a terminal and keep filling the shared SQLite index.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from beacon_engine.research.channel_discovery import ChannelDiscoveryEngine, expand_queries
from beacon_engine.research.crawler import CrawlReport, YouTubeCrawler
from beacon_engine.research.faceless_filter import classify_store_channels
from beacon_engine.research.outliers import find_outliers
from beacon_engine.research.query_policy import QUERY_MAX_TOTAL, query_attempts
from beacon_engine.research.radar_queue import RadarQueueBatch, RadarWorkQueue
from beacon_engine.research.store import YouTubeStore, resolve_index_path
from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.recommendations import IntelligenceRecommendationService
from beacon_engine.intelligence.workspaces import NicheWorkspaceService

Progress = Callable[[str], None]

STATE_ROOT = Path(os.getenv("BEACON_ENGINE_STATE_DIR") or "storage")
DEFAULT_CONFIG_PATH = STATE_ROOT / "scraper_watchlist.json"
DEFAULT_STATUS_PATH = STATE_ROOT / "radar_status.json"
DEFAULT_STOP_PATH = STATE_ROOT / "radar_stop.flag"
DEFAULT_PID_PATH = STATE_ROOT / "radar_worker.pid"
DEFAULT_DB_PATH = resolve_index_path()
DEFAULT_HARVEST_CURSOR_PATH = STATE_ROOT / "harvest_cursor.json"
DEFAULT_NICHE_CURSOR_PATH = STATE_ROOT / "niche_cursor.json"
DEFAULT_HARVEST_YIELD_PATH = STATE_ROOT / "harvest_yield.json"
DEFAULT_WORK_QUEUE_PATH = STATE_ROOT / "radar_work_queue.sqlite3"

DEFAULT_WATCHLIST = [
    "food brands exposed",
    "business documentaries explained",
    "history mysteries documentary",
    "celebrity rise and fall",
    "scary stories explained",
]

FREE_PROVIDER_NAMES = [
    "scrapetube",
    "yt-dlp",
    "piped",
    "invidious",
    "youtube-comment-downloader",
    "youtube-transcript-api",
]


@dataclass
class RadarConfig:
    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    interval_minutes: int = 5
    # Signal can opt into two independent lanes: a quick discovery loop and the
    # existing heavy organizer loop. False preserves the Beacon's
    # original single-cycle behavior.
    split_pipeline_enabled: bool = False
    discovery_interval_minutes: int = 3
    # Empty means "radar_work_queue.sqlite3 beside db_path".
    work_queue_path: str = ""
    db_path: str = str(DEFAULT_DB_PATH)
    seed_limit: int = 24
    max_channels: int = 2
    videos_per_channel: int = 24
    enrich_top: int = 8
    outliers_limit: int = 30
    # The whole-index outlier scoring pass is the heaviest cycle step (~10-20 min:
    # it re-scores every video each time). Running it every cycle crowds out channel
    # discovery, so only score every Nth cycle — the other cycles skip it and reach
    # the next harvest sooner. 1 = every cycle (original behavior; keeps the Lab as-is).
    score_outliers_every_n_cycles: int = 1
    min_score: float = 2.0
    min_views: int = 10_000
    min_channel_videos: int = 5
    # Backwards-compatible alias used by older config files. New Radar runs
    # separate "fresh" signals from the broader/global outlier library.
    max_age_days: int | None = 30
    fresh_max_age_days: int | None = 30
    global_max_age_days: int | None = 730
    faceless_mode: str = "strict"
    faceless_only: bool = True
    # Channel-discovery (breadth) harvest folded into each cycle: search a
    # rotating slice of expanded watchlist queries and collect new channels.
    harvest_enabled: bool = True
    harvest_per_cycle: int = 30
    harvest_per_query: int = 40
    # Fetch provider results concurrently, then serialize SQLite writes and
    # query-yield accounting. Default 1 preserves the original behavior.
    harvest_workers: int = 1
    harvest_expand: bool = True
    # Adaptive harvest: track each query's NEW-channel yield, stop re-searching
    # queries that keep returning only already-indexed channels (auto-retire after
    # `harvest_retire_after` zero-yield sweeps, re-probe every `harvest_reprobe_every`
    # cycles), and re-search a query that DOES have history in upload-date order so
    # it surfaces channels posted since last time. Set False for the old behavior.
    harvest_adaptive: bool = True
    harvest_retire_after: int = 3
    harvest_reprobe_every: int = 20
    # Once every expanded variation has been attempted, the always-on radar may
    # add up to two reviewed, evidence-backed base topics. Disabled by default
    # so legacy/Lab configs do not change until explicitly opted in.
    query_topup_enabled: bool = False
    query_topup_max_total: int = QUERY_MAX_TOTAL
    query_topup_max_per_run: int = 2
    query_topup_cooldown_hours: int = 12
    query_topup_check_interval_minutes: int = 60
    query_topup_min_coverage: float = 1.0
    query_topup_min_channels: int = 3
    query_topup_min_videos: int = 5
    query_topup_max_per_topic: int = 4
    query_topup_evidence_limit: int = 25000
    signal_query_policy: dict = field(default_factory=dict)
    # Crawl only a rotating slice of niches per cycle, so heavy discovery never
    # starves the channel-processing steps (catalog/face-check/GPU).
    niche_crawl_per_cycle: int = 10
    # Free CPU face-check (faceless vs on-camera) folded into each cycle, so every
    # channel with enough thumbnails gets confirmed automatically over time.
    face_check_enabled: bool = True
    face_check_per_cycle: int = 800
    face_check_min_thumbnails: int = 3
    face_check_workers: int = 10
    # Auto-catalog thin channels (pull more of their videos) so they gain enough
    # thumbnails to be face-checked -> the whole library self-completes over time.
    catalog_thin_enabled: bool = True
    catalog_thin_per_cycle: int = 400
    catalog_thin_videos: int = 30
    catalog_workers: int = 8
    # Auto channel-stats enrichment (subs, total views, uploads, creation date)
    # via the cheap official API, so new channels actually appear in the finder.
    enrich_channels_enabled: bool = True
    enrich_channels_per_cycle: int = 2000
    # Auto remote GPU VLM: when enough gray-zone ('uncertain') channels pile up,
    # show their thumbnails to the vision deployment and import the verdicts.
    vlm_remote_enabled: bool = True
    vlm_max_minutes: int = 90
    # Channel-network expansion: follow cross-promotion links in stored
    # descriptions to reach channels no keyword search surfaces.
    network_expand_enabled: bool = True
    network_expand_scan_limit: int = 30000
    # Intelligence fuel: auto-fetch transcripts + top comments for recent
    # high-view videos (the raw material for hook/pattern/audience analysis).
    intel_fuel_enabled: bool = True
    intel_fuel_per_cycle: int = 12
    intel_fuel_min_views: int = 50_000
    intel_fuel_comments: int = 20
    # Once a day: refresh published lab videos' real view counts and mine
    # learnings — the feedback loop that teaches the system the USER's channel.
    lab_sync_enabled: bool = False  # accepted only for old configs; no Lab work runs
    # Auto-hide off-target-language channels each cycle (keep Latin-script:
    # English/French/Spanish/German). Non-Latin content is detected by script.
    language_filter_enabled: bool = True
    # Autonomously keep tracked competitors fresh: once a day refresh their live
    # stats + uploads and auto-drop the dead ones (no upload in N days). Snapshots
    # for growth-over-days happen every cycle regardless (deduped daily).
    competitor_track_enabled: bool = True
    competitor_drop_inactive_days: int = 120
    # Workspace alert detection is expensive because it rebuilds tracked-channel
    # feeds and outlier signals. 0 preserves the original every-cycle behavior;
    # a positive value runs it at most once per that many minutes.
    workspace_alert_interval_minutes: int = 0
    # No-scrape constraint: don't spend the expensive per-channel work (cataloging,
    # face-check) on channels under this many subscribers. They still get cheaply
    # enriched and stay visible in the app — this only saves scrape effort on tiny
    # channels. Set 0 to disable. (Outliers stay visible via enrichment + text model.)
    min_subs_to_process: int = 700
    # Auto-exclude obvious junk each cycle (music / '- Topic' / VEVO / meme / big brands)
    # from every view AND from scraping. Reversible via store.unexclude_channels.
    junk_filter_enabled: bool = True
    # Tag videos with their YouTube category (snippet.categoryId) each cycle, using
    # the rotating API-key pool. Free stage first (backfill from stored JSON), then a
    # bounded API-fetch slice so we drain the backlog gradually without a quota spike.
    # This is what lets find_music_channels catch artist channels by category.
    categorize_enabled: bool = True
    categorize_per_cycle: int = 3000        # videos fetched via API per cycle (0 = free backfill only)
    # Assign every newly-discovered channel a clean TOPIC niche (History/Space/...).
    # Cheap (keyword classify over channels missing a topic); powers the Channels-tab
    # topic filter. See research/topic_classifier.py.
    topic_classify_enabled: bool = True


@dataclass
class RadarCycleResult:
    discovered: int = 0
    channels: int = 0
    enriched: int = 0
    outliers: int = 0
    fresh_outliers: int = 0
    global_outliers: int = 0
    new_channels: int = 0
    face_checked: int = 0
    reports: list[CrawlReport] = field(default_factory=list)


def _read_cursor(path: str | os.PathLike = DEFAULT_HARVEST_CURSOR_PATH) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    try:
        with p.open(encoding="utf-8") as fh:
            return int(json.load(fh).get("cursor", 0))
    except Exception:  # noqa: BLE001
        return 0


def _write_cursor(cursor: int, path: str | os.PathLike = DEFAULT_HARVEST_CURSOR_PATH) -> None:
    _write_json(Path(path), {"cursor": int(cursor), "updated_at": utc_now()})


def _read_yield_state(path: str | os.PathLike = DEFAULT_HARVEST_YIELD_PATH) -> dict:
    """Per-query harvest yield: {query: {"zero_streak", "total_new", "searches"}}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_yield_state(state: dict, path: str | os.PathLike = DEFAULT_HARVEST_YIELD_PATH) -> None:
    _write_json(Path(path), state)


def _select_harvest_queries(
    queries: list[str],
    state: dict,
    *,
    cursor: int,
    take: int,
    retire_after: int,
    reprobe: bool,
    unattempted_limit: int = 10,
) -> tuple[list[str], int]:
    """Prioritize a few never-attempted variations, then continue the rotation."""
    n = len(queries)
    if n == 0 or take <= 0:
        return [], 0
    cursor %= n
    ordered = [queries[(cursor + offset) % n] for offset in range(n)]
    priority = [
        query
        for query in ordered
        if query_attempts(state.get(query)) == 0
    ][: min(max(0, int(unattempted_limit)), take)]
    selected = list(priority)
    selected_set = set(selected)
    span = 0
    while len(selected) < take and span < n:
        query = ordered[span]
        retired = (
            int((state.get(query) or {}).get("zero_streak", 0) or 0)
            >= retire_after
        )
        if query not in selected_set and (reprobe or not retired):
            selected.append(query)
            selected_set.add(query)
        span += 1
    return selected, (cursor + max(span, take)) % n


def _harvest_slice(
    config: RadarConfig,
    store: YouTubeStore,
    *,
    cursor_path: str | os.PathLike = DEFAULT_HARVEST_CURSOR_PATH,
    yield_path: str | os.PathLike = DEFAULT_HARVEST_YIELD_PATH,
    cycle: int = 0,
    categorize: bool = True,
    on_step: Progress = lambda _m: None,
) -> int:
    """Search a rotating slice of expanded watchlist queries for new channels.

    Each cycle advances a persistent cursor so, over many cycles, the worker
    sweeps the whole expanded query space and keeps surfacing fresh channels.

    With `harvest_adaptive` (default), queries that keep returning only
    already-indexed channels are skipped (re-probed periodically) so the per-cycle
    budget is spent on productive/unexplored queries, and any query with prior
    history is re-searched newest-first to catch channels posted since last sweep.
    """
    bases = _clean_watchlist(config.watchlist) or list(DEFAULT_WATCHLIST)
    queries = expand_queries(bases) if config.harvest_expand else bases
    if not queries:
        return 0
    engine = ChannelDiscoveryEngine(store)
    n = len(queries)
    take = min(config.harvest_per_cycle, n)

    if not getattr(config, "harvest_adaptive", True):
        # legacy behavior (unchanged): a plain rotating slice, relevance order
        cursor = _read_cursor(cursor_path) % n
        slice_ = (queries + queries)[cursor:cursor + take]
        report = engine.harvest(
            slice_, per_query=config.harvest_per_query,
            providers=FREE_PROVIDER_NAMES, workers=config.harvest_workers,
            on_step=on_step)
        if categorize:
            engine.categorize(
                faceless_mode=config.faceless_mode,
                only_missing=True,
            )
        _write_cursor((cursor + take) % n, cursor_path)
        return report.new_channels

    state = _read_yield_state(yield_path)
    retire_after = max(1, int(getattr(config, "harvest_retire_after", 3)))
    reprobe_every = int(getattr(config, "harvest_reprobe_every", 20))
    reprobe = reprobe_every > 0 and (cycle % reprobe_every == 0)

    # Pull a small number of never-attempted variations forward so first-pass
    # coverage cannot sit at 99.9% for a full rotation. Fill the remaining budget
    # from the normal adaptive cursor.
    cursor = _read_cursor(cursor_path) % n
    selected, next_cursor = _select_harvest_queries(
        queries,
        state,
        cursor=cursor,
        take=take,
        retire_after=retire_after,
        reprobe=reprobe,
    )

    if not selected:
        _write_cursor(next_cursor, cursor_path)
        on_step("Harvest: this slice is all tapped-out queries; skipping until the "
                "next re-probe (add fresh topics on Overview to find more).")
        return 0

    def record(query: str, delta: int) -> None:
        st = state.setdefault(query, {"zero_streak": 0, "total_new": 0, "searches": 0})
        st["searches"] = int(st.get("searches", 0)) + 1
        st["total_new"] = int(st.get("total_new", 0)) + int(delta)
        st["zero_streak"] = 0 if delta > 0 else int(st.get("zero_streak", 0)) + 1

    def record_attempt(query: str) -> None:
        st = state.setdefault(
            query,
            {"zero_streak": 0, "total_new": 0, "searches": 0, "attempts": 0},
        )
        st["attempts"] = int(st.get("attempts", 0) or 0) + 1
        st["last_attempted_at"] = utc_now()

    # A query with prior history is re-searched newest-first (fresh uploads);
    # a brand-new query uses default relevance for the broad first pass.
    def is_revisit(query: str) -> bool:
        return int(state.get(query, {}).get("searches", 0)) > 0

    # Persist the cursor AND the (in-memory-mutated) yield state together in a
    # finally, so a mid-harvest error can't advance the cursor while dropping the
    # per-query yield we already recorded this cycle (they stay consistent).
    total_new = 0
    try:
        for sort_key, group in (
            (None, [q for q in selected if not is_revisit(q)]),
            ("newest", [q for q in selected if is_revisit(q)]),
        ):
            if not group:
                continue
            report = engine.harvest(
                group, per_query=config.harvest_per_query, providers=FREE_PROVIDER_NAMES,
                sort=sort_key, workers=config.harvest_workers,
                on_query_result=record, on_query_attempt=record_attempt,
                on_step=on_step)
            total_new += report.new_channels
        if categorize:
            engine.categorize(
                faceless_mode=config.faceless_mode,
                only_missing=True,
            )
    finally:
        _write_cursor(next_cursor, cursor_path)
        _write_yield_state(state, yield_path)
    return total_new


def _niche_slice(queries: list[str], per_cycle: int,
                 *, cursor_path: str | os.PathLike = DEFAULT_NICHE_CURSOR_PATH) -> list[str]:
    """A rotating slice of niche queries so each cycle crawls only a few."""
    n = len(queries)
    if n == 0 or per_cycle >= n:
        return list(queries)
    cur = _read_cursor(cursor_path) % n
    _write_cursor((cur + per_cycle) % n, cursor_path)
    return (list(queries) + list(queries))[cur:cur + per_cycle]


def _catalog_thin_slice(config: RadarConfig, store: YouTubeStore) -> int:
    """Pull more videos for a batch of thin channels so they can be face-checked.
    Channels are cataloged in parallel (thread-local crawlers) — the work is
    network-bound, so 8 workers ≈ 8× the sequential throughput."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = store.channels_needing_catalog(
        min_thumbnails=config.face_check_min_thumbnails,
        limit=config.catalog_thin_per_cycle,
        min_subs=getattr(config, "min_subs_to_process", 0),
    )
    if not targets:
        return 0
    _local = threading.local()

    def _crawler() -> YouTubeCrawler:
        c = getattr(_local, "crawler", None)
        if c is None:
            c = YouTubeCrawler(store)
            _local.crawler = c
        return c

    done = 0
    lock = threading.Lock()

    def work(channel_id: str) -> None:
        nonlocal done
        try:
            _crawler().crawl_channel(
                channel_id,
                limit=config.catalog_thin_videos,
                enrich=False,
                providers=FREE_PROVIDER_NAMES,
            )
            with lock:
                done += 1
        except Exception:  # noqa: BLE001
            pass
        store.record_enrichment_attempt("catalog", [channel_id], status="ok")

    with ThreadPoolExecutor(max_workers=config.catalog_workers) as ex:
        for _ in as_completed([ex.submit(work, cid) for cid in targets]):
            pass
    # Self-heal: link any videos that came in without a channel_id so the freshly
    # cataloged channels actually become face-checkable this cycle.
    store.backlink_channel_videos()
    return done


def _intel_fuel_slice(config: RadarConfig, store: YouTubeStore,
                      transcript_provider=None, comments_provider=None) -> tuple[int, int]:
    """Fetch transcripts + top comments for recent high-view videos that lack them.

    This is the raw material the pattern/hook/audience analysis feeds on — without
    it the intelligence layer only ever sees titles. Each video is attempted once
    (tracked in enrichment_log), so unavailable transcripts don't retry forever."""
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT v.video_id FROM videos v
            LEFT JOIN transcripts t ON t.video_id = v.video_id
            WHERE t.video_id IS NULL
              AND v.views >= ?
              AND COALESCE(v.published_at_normalized, '') >= date('now', '-90 days')
              AND v.video_id NOT IN (
                  SELECT entity_id FROM enrichment_log WHERE entity_type = 'intel_fuel')
            ORDER BY v.views DESC
            LIMIT ?
            """,
            (int(config.intel_fuel_min_views), int(config.intel_fuel_per_cycle)),
        ).fetchall()
    if not rows:
        return 0, 0
    if transcript_provider is None:
        from beacon_engine.research.providers.transcript_provider import TranscriptProvider
        transcript_provider = TranscriptProvider()
    if comments_provider is None:
        from beacon_engine.research.providers.comments_provider import CommentDownloaderProvider
        comments_provider = CommentDownloaderProvider()
    got_t = got_c = 0
    for (vid,) in rows:
        try:
            tr = transcript_provider.transcript(vid)
            if tr:
                store.save_transcript(tr)
                got_t += 1
        except Exception:  # noqa: BLE001 -- no transcript is normal, move on
            pass
        try:
            cm = comments_provider.comments(vid, limit=config.intel_fuel_comments)
            if cm:
                store.save_comments(cm)
                got_c += 1
        except Exception:  # noqa: BLE001
            pass
        store.record_enrichment_attempt("intel_fuel", [vid], status="ok")
    return got_t, got_c


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_config(path: str | os.PathLike = DEFAULT_CONFIG_PATH) -> RadarConfig:
    p = Path(path)
    if not p.exists():
        cfg = RadarConfig()
        save_config(cfg, p)
        return cfg
    with p.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        data = {"watchlist": data}
    if not isinstance(data, dict):
        data = {}
    cfg = RadarConfig()
    watchlist = data.get("watchlist")
    if isinstance(watchlist, list):
        cfg.watchlist = _clean_watchlist(watchlist) or cfg.watchlist
    for name in (
        "interval_minutes",
        "discovery_interval_minutes",
        "seed_limit",
        "max_channels",
        "videos_per_channel",
        "outliers_limit",
        "min_channel_videos",
    ):
        if name in data:
            setattr(cfg, name, max(1, int(data.get(name) or getattr(cfg, name))))
    if "enrich_top" in data:
        cfg.enrich_top = max(0, int(data.get("enrich_top") or 0))
    if "min_views" in data:
        cfg.min_views = max(0, int(data.get("min_views") or 0))
    if "db_path" in data and str(data["db_path"]).strip():
        cfg.db_path = str(data["db_path"]).strip()
    if "split_pipeline_enabled" in data:
        cfg.split_pipeline_enabled = bool(data.get("split_pipeline_enabled"))
    if "work_queue_path" in data:
        cfg.work_queue_path = str(data.get("work_queue_path") or "").strip()
    if "min_score" in data:
        cfg.min_score = float(data.get("min_score") or cfg.min_score)
    if "max_age_days" in data:
        raw_age = data.get("max_age_days")
        cfg.max_age_days = None if raw_age is None else max(1, int(raw_age))
    raw_fresh_age = data.get("fresh_max_age_days", data.get("max_age_days", cfg.fresh_max_age_days))
    cfg.fresh_max_age_days = (
        None if raw_fresh_age is None else max(1, int(raw_fresh_age))
    )
    cfg.max_age_days = cfg.fresh_max_age_days
    if "global_max_age_days" in data:
        raw_global_age = data.get("global_max_age_days")
        cfg.global_max_age_days = (
            None if raw_global_age is None else max(1, int(raw_global_age))
        )
    if "faceless_mode" in data:
        mode = str(data.get("faceless_mode") or "strict").lower()
        cfg.faceless_mode = mode if mode in {"strict", "balanced", "loose"} else "strict"
    if "faceless_only" in data:
        cfg.faceless_only = bool(data.get("faceless_only"))
    if "harvest_enabled" in data:
        cfg.harvest_enabled = bool(data.get("harvest_enabled"))
    if "harvest_expand" in data:
        cfg.harvest_expand = bool(data.get("harvest_expand"))
    for name in ("harvest_per_cycle", "harvest_per_query", "niche_crawl_per_cycle"):
        if name in data:
            setattr(cfg, name, max(1, int(data.get(name) or getattr(cfg, name))))
    if "harvest_workers" in data:
        cfg.harvest_workers = max(
            1, min(8, int(data.get("harvest_workers") or cfg.harvest_workers)))
    if "harvest_adaptive" in data:
        cfg.harvest_adaptive = bool(data.get("harvest_adaptive"))
    for name in ("harvest_retire_after", "harvest_reprobe_every"):
        if name in data:
            setattr(cfg, name, max(0, int(data.get(name) or getattr(cfg, name))))
    if "query_topup_enabled" in data:
        cfg.query_topup_enabled = bool(data.get("query_topup_enabled"))
    for name in (
        "query_topup_max_total",
        "query_topup_max_per_run",
        "query_topup_cooldown_hours",
        "query_topup_check_interval_minutes",
        "query_topup_min_channels",
        "query_topup_min_videos",
        "query_topup_max_per_topic",
        "query_topup_evidence_limit",
    ):
        if name in data:
            setattr(cfg, name, max(1, int(data.get(name) or getattr(cfg, name))))
    cfg.query_topup_max_total = min(cfg.query_topup_max_total, QUERY_MAX_TOTAL)
    cfg.query_topup_max_per_run = min(cfg.query_topup_max_per_run, 2)
    cfg.query_topup_evidence_limit = max(1000, cfg.query_topup_evidence_limit)
    if "query_topup_min_coverage" in data:
        cfg.query_topup_min_coverage = min(
            1.0,
            max(0.0, float(data.get("query_topup_min_coverage") or 0.0)),
        )
    policy = data.get("signal_query_policy")
    if isinstance(policy, dict):
        cfg.signal_query_policy = dict(policy)
    if "face_check_enabled" in data:
        cfg.face_check_enabled = bool(data.get("face_check_enabled"))
    for name in ("face_check_per_cycle", "face_check_min_thumbnails",
                 "face_check_workers", "catalog_workers"):
        if name in data:
            setattr(cfg, name, max(1, int(data.get(name) or getattr(cfg, name))))
    if "catalog_thin_enabled" in data:
        cfg.catalog_thin_enabled = bool(data.get("catalog_thin_enabled"))
    for name in ("catalog_thin_per_cycle", "catalog_thin_videos"):
        if name in data:
            setattr(cfg, name, max(1, int(data.get(name) or getattr(cfg, name))))
    if "enrich_channels_enabled" in data:
        cfg.enrich_channels_enabled = bool(data.get("enrich_channels_enabled"))
    if "enrich_channels_per_cycle" in data:
        cfg.enrich_channels_per_cycle = max(
            1, int(data.get("enrich_channels_per_cycle") or cfg.enrich_channels_per_cycle))
    if "vlm_remote_enabled" in data:
        cfg.vlm_remote_enabled = bool(data.get("vlm_remote_enabled"))
    if "vlm_max_minutes" in data:
        cfg.vlm_max_minutes = max(5, int(data.get("vlm_max_minutes") or cfg.vlm_max_minutes))
    if "network_expand_enabled" in data:
        cfg.network_expand_enabled = bool(data.get("network_expand_enabled"))
    if "network_expand_scan_limit" in data:
        cfg.network_expand_scan_limit = max(
            1000, int(data.get("network_expand_scan_limit") or cfg.network_expand_scan_limit))
    if "intel_fuel_enabled" in data:
        cfg.intel_fuel_enabled = bool(data.get("intel_fuel_enabled"))
    for name in ("intel_fuel_per_cycle", "intel_fuel_min_views", "intel_fuel_comments"):
        if name in data:
            setattr(cfg, name, max(1, int(data.get(name) or getattr(cfg, name))))
    if "competitor_track_enabled" in data:
        cfg.competitor_track_enabled = bool(data.get("competitor_track_enabled"))
    if "competitor_drop_inactive_days" in data:
        cfg.competitor_drop_inactive_days = max(
            0, int(data.get("competitor_drop_inactive_days")
                   or cfg.competitor_drop_inactive_days))
    if "workspace_alert_interval_minutes" in data:
        cfg.workspace_alert_interval_minutes = max(
            0, int(data.get("workspace_alert_interval_minutes") or 0))
    if "min_subs_to_process" in data:
        cfg.min_subs_to_process = max(0, int(data.get("min_subs_to_process") or 0))
    if "junk_filter_enabled" in data:
        cfg.junk_filter_enabled = bool(data.get("junk_filter_enabled"))
    if "categorize_enabled" in data:
        cfg.categorize_enabled = bool(data.get("categorize_enabled"))
    if "categorize_per_cycle" in data:
        cfg.categorize_per_cycle = max(
            0, int(data.get("categorize_per_cycle") or 0))
    if "score_outliers_every_n_cycles" in data:
        cfg.score_outliers_every_n_cycles = max(
            1, int(data.get("score_outliers_every_n_cycles") or 1))
    if "topic_classify_enabled" in data:
        cfg.topic_classify_enabled = bool(data.get("topic_classify_enabled"))
    return cfg


def save_config(
    config: RadarConfig,
    path: str | os.PathLike = DEFAULT_CONFIG_PATH,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    config.watchlist = _clean_watchlist(config.watchlist) or list(DEFAULT_WATCHLIST)
    _write_json(p, asdict(config))


def read_status(path: str | os.PathLike = DEFAULT_STATUS_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {"state": "not_started"}
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {"state": "unknown"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "unreadable", "error": str(exc)}


def request_stop(path: str | os.PathLike = DEFAULT_STOP_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(utc_now(), encoding="utf-8")


def clear_stop(path: str | os.PathLike = DEFAULT_STOP_PATH) -> None:
    p = Path(path)
    if p.exists():
        p.unlink()


def stop_requested(path: str | os.PathLike = DEFAULT_STOP_PATH) -> bool:
    return Path(path).exists()


def _workspace_alerts_due(
    store: YouTubeStore,
    interval_minutes: int,
) -> bool:
    interval = max(0, int(interval_minutes or 0))
    if interval == 0:
        return True
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT last_attempt_at
            FROM enrichment_log
            WHERE entity_type='workspace_alerts' AND entity_id='all'
            """
        ).fetchone()
    if not row or not row[0]:
        return True
    try:
        last = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return True
    return (
        dt.datetime.now(dt.timezone.utc) - last.astimezone(dt.timezone.utc)
        >= dt.timedelta(minutes=interval)
    )


def _queue_path(config: RadarConfig) -> Path:
    if str(config.work_queue_path or "").strip():
        return Path(config.work_queue_path)
    if str(config.db_path) == "postgresql:beacon-index":
        return Path(os.getenv("BEACON_INDEX_STATE_DIR") or "storage") / DEFAULT_WORK_QUEUE_PATH.name
    return Path(config.db_path).with_name(DEFAULT_WORK_QUEUE_PATH.name)


def run_discovery_cycle(
    config: RadarConfig,
    queue: RadarWorkQueue,
    *,
    config_path: str | os.PathLike = DEFAULT_CONFIG_PATH,
    status_path: str | os.PathLike = DEFAULT_STATUS_PATH,
    stop_path: str | os.PathLike = DEFAULT_STOP_PATH,
    on_step: Progress = lambda _m: None,
    cycle: int = 1,
) -> int:
    """Run only the fast search harvest and enqueue organization work."""
    store = YouTubeStore(config.db_path)
    _write_lane_status(
        status_path,
        "discovery",
        "running",
        queue_stats=queue.stats(),
        cycle=cycle,
        message="searching for new channels",
    )
    if not config.harvest_enabled or stop_requested(stop_path):
        _write_lane_status(
            status_path,
            "discovery",
            "cycle_complete",
            queue_stats=queue.stats(),
            cycle=cycle,
            new_channels=0,
            message="discovery disabled",
        )
        return 0

    on_step("Fast discovery lane: harvesting new channels")
    new_channels = _harvest_slice(
        config,
        store,
        cycle=cycle,
        categorize=False,
        on_step=on_step,
    )
    topup = {
        "added": [],
        "reason": "disabled",
        "coverage": 0.0,
        "remaining": 0,
    }
    if config.query_topup_enabled and not stop_requested(stop_path):
        try:
            from beacon_engine.research.query_topup import maybe_top_up_queries

            topup = maybe_top_up_queries(
                config,
                store,
                config_path=config_path,
                yield_path=DEFAULT_HARVEST_YIELD_PATH,
            )
            if topup.get("added"):
                on_step(
                    "Radar query top-up added "
                    + ", ".join(str(query) for query in topup["added"])
                )
        except Exception as exc:  # noqa: BLE001
            topup = {
                "added": [],
                "reason": "error",
                "error": str(exc),
                "coverage": 0.0,
                "remaining": 0,
            }
            on_step(f"Radar query top-up skipped: {type(exc).__name__}: {exc}")
    if new_channels:
        queue.enqueue_organization(
            new_channels,
            payload={"discovery_cycle": cycle, "queued_at": utc_now()},
        )
    stats = queue.stats()
    _write_lane_status(
        status_path,
        "discovery",
        "cycle_complete",
        queue_stats=stats,
        cycle=cycle,
        new_channels=new_channels,
        queries_added=len(topup.get("added") or []),
        query_additions=list(topup.get("added") or []),
        query_topup_reason=topup.get("reason", ""),
        query_coverage=topup.get("coverage", 0.0),
        query_searches_remaining=topup.get("remaining", 0),
        message=f"found {new_channels:,} new channels",
    )
    on_step(
        f"Fast discovery added {new_channels:,} channels; "
        f"{stats['queued_channels']:,} waiting, "
        f"{stats['running_channels']:,} being organized"
    )
    return new_channels


def run_cycle(
    config: RadarConfig,
    *,
    status_path: str | os.PathLike = DEFAULT_STATUS_PATH,
    stop_path: str | os.PathLike = DEFAULT_STOP_PATH,
    on_step: Progress = lambda _m: None,
    cycle: int = 1,
    status_lane: str | None = None,
    organization_only: bool = False,
) -> RadarCycleResult:
    def write_status(state: str, **extra) -> None:
        if status_lane:
            _write_lane_status(status_path, status_lane, state, **extra)
        else:
            _write_status(status_path, state, **extra)

    store = YouTubeStore(config.db_path)
    intelligence = IntelligenceRecommendationService(store)
    job_id = intelligence.create_job(
        "radar_cycle",
        scope=", ".join(config.watchlist[:5]),
        status="running",
    )
    crawler = YouTubeCrawler(store)
    reports: list[CrawlReport] = []
    queries = _clean_watchlist(config.watchlist) or list(DEFAULT_WATCHLIST)
    write_status(
        "running",
        cycle=cycle,
        message=(
            "organizing channel backlog"
            if organization_only
            else "cycle started"
        ),
        query_index=0,
        query_total=len(queries),
    )
    new_channels = 0
    if config.harvest_enabled and not stop_requested(stop_path):
        on_step("Harvesting new channels (rotating watchlist slice)")
        try:
            new_channels = _harvest_slice(config, store, cycle=cycle, on_step=on_step)
            on_step(f"Harvest added {new_channels:,} new channels this cycle")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Harvest skipped: {type(exc).__name__}: {exc}")

    if config.enrich_channels_enabled and not stop_requested(stop_path):
        from beacon_engine.config import settings as _settings
        if not _settings.youtube_api_key:
            on_step("Channel enrich skipped: no YOUTUBE_API_KEY")
        else:
            on_step("Enriching channel stats (subs, views, creation dates)")
            try:
                from beacon_engine.research.enrichment import EnrichmentEngine
                engine = EnrichmentEngine(store)
                subs = engine.backfill_channel_stats(
                    limit=config.enrich_channels_per_cycle, missing="subs")
                meta = engine.backfill_channel_stats(
                    limit=config.enrich_channels_per_cycle, missing="meta")
                on_step(
                    f"Enriched {subs.updated:,} channel subs, "
                    f"{meta.updated:,} creation dates "
                    f"({subs.quota_units + meta.quota_units} quota units)"
                )
            except Exception as exc:  # noqa: BLE001
                on_step(f"Channel enrich skipped: {type(exc).__name__}: {exc}")

    if (
        not organization_only
        and config.network_expand_enabled
        and not stop_requested(stop_path)
    ):
        on_step("Expanding via channel-network (cross-promotion links)")
        try:
            from beacon_engine.research.network_expand import harvest_channel_links
            _links, _uniq, seeded = harvest_channel_links(
                store, scan_limit=config.network_expand_scan_limit)
            if seeded:
                on_step(f"Network expansion seeded {seeded:,} new channels")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Network expansion skipped: {type(exc).__name__}: {exc}")

    if config.catalog_thin_enabled and not stop_requested(stop_path):
        on_step("Cataloging thin channels (so they can be face-checked)")
        try:
            cataloged = _catalog_thin_slice(config, store)
            on_step(f"Cataloged {cataloged:,} thin channels this cycle")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Catalog slice skipped: {type(exc).__name__}: {exc}")

    if not stop_requested(stop_path):
        # Auto-hide kids/cartoon channels (faceless but off-niche) each cycle so
        # newly discovered ones never clutter the results.
        try:
            from beacon_engine.research.kids_filter import find_kids_channels
            kids = find_kids_channels(store)
            new_kids = [k.channel_id for k in kids
                        if k.channel_id not in store.excluded_channel_ids()]
            if new_kids:
                store.exclude_channels(new_kids, reason="kids")
                on_step(f"Hid {len(new_kids):,} new kids/cartoon channels")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Kids filter skipped: {type(exc).__name__}: {exc}")

    if config.language_filter_enabled and not stop_requested(stop_path):
        # Auto-hide off-target-language channels (keep Latin-script: en/fr/es/de)
        # so foreign-language junk never clutters results or wastes GPU scoring.
        try:
            from beacon_engine.research.language_filter import find_wrong_language_channels
            already = store.excluded_channel_ids()
            wrong = [h["channel_id"] for h in find_wrong_language_channels(store)
                     if h["channel_id"] not in already]
            if wrong:
                store.exclude_channels(wrong, reason="language")
                on_step(f"Hid {len(wrong):,} off-target-language channels")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Language filter skipped: {type(exc).__name__}: {exc}")

    if getattr(config, "categorize_enabled", True) and not stop_requested(stop_path):
        # Tag videos with their YouTube category via the rotating API-key pool: free
        # backfill from stored JSON, then a bounded API slice. Runs BEFORE the junk
        # filter so newly-tagged music channels get caught the same cycle.
        try:
            from beacon_engine.research import categorize
            per = getattr(config, "categorize_per_cycle", 3000)
            res = categorize.run(store, fetch=bool(per), limit=(per or None),
                                 on_step=on_step)
            on_step(f"Categorized videos: +{res.get('backfilled', 0):,} free, "
                    f"+{res.get('fetched', 0):,} via API (~{res.get('units', 0):,} units)")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Categorize skipped: {type(exc).__name__}: {exc}")

    if getattr(config, "topic_classify_enabled", True) and not stop_requested(stop_path):
        # Give every newly-discovered channel a clean topic niche (History/Space/...).
        try:
            from beacon_engine.research import topic_classifier
            res = topic_classifier.backfill(store, only_missing=True, on_step=on_step)
            if res.get("classified"):
                on_step(f"Topic-labeled {res['classified']:,} new channels")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Topic classify skipped: {type(exc).__name__}: {exc}")

    if getattr(config, "junk_filter_enabled", True) and not stop_requested(stop_path):
        # Auto-hide obvious junk (music / '- Topic' / VEVO / meme / big brands) from
        # every view AND from scraping. Same reversible exclusion mechanism.
        try:
            from beacon_engine.research.junk_filter import exclude_junk
            exclude_junk(store, on_step=on_step)
        except Exception as exc:  # noqa: BLE001
            on_step(f"Junk filter skipped: {type(exc).__name__}: {exc}")

    face_checked = 0
    if config.face_check_enabled and not stop_requested(stop_path):
        on_step("Face-checking channels (faceless vs on-camera)")
        try:
            from beacon_engine.research.face_check import (
                FaceCheckUnavailable, run_face_check_parallel,
            )
            counts = run_face_check_parallel(
                store,
                workers=config.face_check_workers,
                min_thumbnails=config.face_check_min_thumbnails,
                limit=config.face_check_per_cycle,
                min_subs=getattr(config, "min_subs_to_process", 0),
                on_step=on_step,
            )
            face_checked = int(counts.get("total", 0))
            on_step(
                f"Face-checked {face_checked:,} channels "
                f"(faceless {counts.get('faceless', 0):,} / "
                f"on-camera {counts.get('on_camera', 0):,} / "
                f"uncertain {counts.get('uncertain', 0):,})"
            )
        except FaceCheckUnavailable as exc:
            on_step(f"Face-check unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Face-check skipped: {type(exc).__name__}: {exc}")

    if not stop_requested(stop_path):
        # Fast, vision-free faceless labels: score newly-discovered channels with the
        # persisted text model (title+description+video-titles). Trains once if the
        # model file is missing, then only scores channels without a prediction yet.
        try:
            from beacon_engine.research import faceless_text_model as _ftm
            if not _ftm._model_path(store).exists():
                _ftm.train_and_score(store, on_step=on_step)
            else:
                _ftm.score(store, only_missing=True, on_step=on_step)
        except Exception as exc:  # noqa: BLE001
            on_step(f"Text-model faceless scoring skipped: {type(exc).__name__}: {exc}")

    if (
        not organization_only
        and config.intel_fuel_enabled
        and not stop_requested(stop_path)
    ):
        on_step("Fetching transcripts + comments for top recent winners")
        try:
            got_t, got_c = _intel_fuel_slice(config, store)
            if got_t or got_c:
                on_step(f"Intel fuel: +{got_t} transcripts, +{got_c} comment sets")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Intel fuel skipped: {type(exc).__name__}: {exc}")

    if (
        not organization_only
        and config.vlm_remote_enabled
        and not stop_requested(stop_path)
    ):
        waiting = store.count_uncertain_for_vlm()
        if not waiting:
            on_step("No uncertain channels awaiting VLM")
        else:
            on_step(f"{waiting:,} uncertain channels — scoring thumbnails with the "
                    "vision model")
            try:
                from beacon_engine.research.vlm_scoring import VLMRunner
                res = VLMRunner().score_uncertain(
                    store,
                    max_minutes=config.vlm_max_minutes,
                    on_step=on_step,
                )
                on_step(f"VLM scored {res.scored:,}, imported {res.imported:,}")
            except Exception as exc:  # noqa: BLE001
                on_step(f"VLM skipped: {type(exc).__name__}: {exc}")

    # Niche discovery runs LAST and only a small rotating slice, so it can never
    # starve the channel-processing steps above (the bug that left catalog at 0).
    if not organization_only and not stop_requested(stop_path):
        crawl_queries = _niche_slice(queries, config.niche_crawl_per_cycle)
        for idx, query in enumerate(crawl_queries, 1):
            if stop_requested(stop_path):
                break
            msg = f"Scanning niche {idx}/{len(crawl_queries)}: {query}"
            on_step(msg)
            write_status(
                "running", cycle=cycle, current_query=query,
                query_index=idx, query_total=len(crawl_queries), message=msg,
            )
            try:
                reports.append(crawler.crawl_niche(
                    query,
                    seed_limit=config.seed_limit,
                    max_channels=config.max_channels,
                    videos_per_channel=config.videos_per_channel,
                    enrich=config.enrich_top > 0,
                    extras_limit=config.enrich_top,
                    providers=FREE_PROVIDER_NAMES,
                    faceless_only=config.faceless_only,
                    on_step=on_step,
                ))
            except Exception as exc:  # noqa: BLE001
                on_step(f"Niche crawl '{query}' skipped: {type(exc).__name__}")

    # Heaviest step of the cycle: only run it every Nth cycle (see
    # score_outliers_every_n_cycles) so the in-between cycles reach the next harvest
    # sooner and discover more channels. Skipped cycles report 0 outliers.
    fresh_outs: list = []
    global_outs: list = []
    _score_every = max(1, int(getattr(config, "score_outliers_every_n_cycles", 1) or 1))
    if (
        not organization_only
        and (cycle - 1) % _score_every == 0
        and not stop_requested(stop_path)
    ):
        on_step("Scoring fresh and global outliers")
        rows = store.video_metadata_rows()
        fresh_outs = find_outliers(
            store,
            limit=None,
            min_channel_videos=config.min_channel_videos,
            min_views=config.min_views,
            min_score=config.min_score,
            max_age_days=config.fresh_max_age_days,
            rows=rows,
        )
        global_outs = find_outliers(
            store,
            limit=None,
            min_channel_videos=config.min_channel_videos,
            min_views=config.min_views,
            min_score=config.min_score,
            max_age_days=config.global_max_age_days,
            rows=rows,
        )
        if config.faceless_only:
            signals = classify_store_channels(store, mode=config.faceless_mode)
            fresh_outs = [
                item for item in fresh_outs
                if (
                    item.channel_id in signals
                    and signals[item.channel_id].matches(config.faceless_mode)
                )
            ]
            global_outs = [
                item for item in global_outs
                if (
                    item.channel_id in signals
                    and signals[item.channel_id].matches(config.faceless_mode)
                )
            ]
        store.save_outlier_scores(global_outs)
    else:
        on_step(f"Skipping whole-index outlier scoring this cycle "
                f"(runs every {_score_every} cycles — frees this cycle for discovery)")
    workspace_service = NicheWorkspaceService(store)
    workspace_service.bootstrap_default_workspaces(
        faceless_mode=config.faceless_mode,
    )
    # Only niches the user opted IN to autonomous upkeep — a paused niche costs
    # nothing while it sits unused (its data stays; work resumes when re-enabled).
    workspace_ids = [int(w["id"]) for w in workspace_service.list_workspaces()
                     if int(w.get("autonomous", 1) or 0)]
    for niche_id in workspace_ids:
        # Snapshot growth is cheap and deduped to about one row/channel/day.
        ChannelMetricsService(store).capture_workspace(niche_id, min_interval_hours=20)
    alert_interval = int(
        getattr(config, "workspace_alert_interval_minutes", 0) or 0)
    if _workspace_alerts_due(store, alert_interval):
        on_step(f"Refreshing alerts across {len(workspace_ids)} workspaces")
        for niche_id in workspace_ids:
            intelligence.detect_alerts(niche_id)
        store.record_enrichment_attempt(
            "workspace_alerts", ["all"], status="ok")
    else:
        on_step(
            "Skipping workspace alerts "
            f"(refreshes every {alert_interval} minutes)"
        )

    if (
        not organization_only
        and config.competitor_track_enabled
        and not stop_requested(stop_path)
    ):
        # Once a day: refresh tracked competitors' LIVE stats + uploads and
        # auto-drop the dead ones. Snapshots above already captured the growth.
        try:
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT last_attempt_at FROM enrichment_log "
                    "WHERE entity_type='competitor_track' AND entity_id='daily'").fetchone()
            due = True
            if row:
                last = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                due = (dt.datetime.now(dt.timezone.utc) - last) > dt.timedelta(hours=20)
            if due:
                from beacon_engine.research.competitor_tracker import sync_competitors
                totals = {"refreshed": 0, "new_videos": 0, "dropped": 0}
                for niche_id in workspace_ids:
                    summary = sync_competitors(
                        store, niche_id, refresh=True, snapshot=True,
                        drop_inactive_days=config.competitor_drop_inactive_days,
                        on_step=on_step)
                    totals["refreshed"] += summary.get("refreshed", 0)
                    totals["new_videos"] += summary.get("new_videos", 0)
                    totals["dropped"] += len(summary.get("dropped", []) or [])
                store.record_enrichment_attempt("competitor_track", ["daily"], status="ok")
                on_step(
                    f"Competitor upkeep: {totals['refreshed']} refreshed, "
                    f"{totals['new_videos']} new uploads, {totals['dropped']} dead dropped")
        except Exception as exc:  # noqa: BLE001
            on_step(f"Competitor tracking skipped: {type(exc).__name__}: {exc}")
    result = RadarCycleResult(
        discovered=sum(r.discovered for r in reports),
        channels=sum(r.channels for r in reports),
        enriched=sum(r.enriched for r in reports),
        outliers=len(global_outs),
        fresh_outliers=len(fresh_outs),
        global_outliers=len(global_outs),
        new_channels=new_channels,
        face_checked=face_checked,
        reports=reports,
    )
    write_status(
        "cycle_complete",
        cycle=cycle,
        discovered=result.discovered,
        channels=result.channels,
        enriched=result.enriched,
        outliers=result.outliers,
        fresh_outliers=result.fresh_outliers,
        global_outliers=result.global_outliers,
        fresh_max_age_days=config.fresh_max_age_days,
        global_max_age_days=config.global_max_age_days,
        new_channels=result.new_channels,
        face_checked=result.face_checked,
        db_path=config.db_path,
        message=(
            f"{result.discovered:,} videos, {result.channels:,} channels, "
            f"+{result.new_channels:,} new channels, "
            f"{result.face_checked:,} face-checked, "
            f"{result.fresh_outliers:,} fresh outliers, "
            f"{result.global_outliers:,} global outliers"
        ),
    )
    intelligence.update_job(
        job_id,
        status="completed",
        progress={
            "discovered": result.discovered,
            "channels": result.channels,
            "outliers": result.outliers,
            "fresh_outliers": result.fresh_outliers,
            "global_outliers": result.global_outliers,
        },
    )
    return result


def _wait_for_stop(
    seconds: int,
    stop_path: str | os.PathLike,
    *,
    tick: Callable[[int], None] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    remaining = max(0, int(seconds))
    while (
        remaining > 0
        and not stop_requested(stop_path)
        and not (stop_event is not None and stop_event.is_set())
    ):
        if tick:
            tick(remaining)
        step = min(5, remaining)
        time.sleep(step)
        remaining -= step


def _mark_latest_radar_job_failed(config: RadarConfig, exc: Exception) -> None:
    failed_jobs = IntelligenceRecommendationService(YouTubeStore(config.db_path))
    for job in failed_jobs.list_jobs(limit=10):
        if job.get("job_type") == "radar_cycle" and job.get("status") == "running":
            failed_jobs.update_job(
                int(job["id"]),
                status="failed",
                error=str(exc),
            )
            break


def _run_split_pipeline(
    *,
    config_path: str | os.PathLike,
    status_path: str | os.PathLike,
    stop_path: str | os.PathLike,
    once: bool,
    on_step: Progress,
) -> int:
    """Run discovery and organization independently inside one durable process."""
    organizer_cycle = 0
    worker_name = f"radar-organizer-{os.getpid()}"
    first_discovery_done = threading.Event()
    shutdown_event = threading.Event()

    def discovery_loop() -> None:
        discovery_cycle = 0
        while (
            not stop_requested(stop_path)
            and not shutdown_event.is_set()
        ):
            discovery_cycle += 1
            config = load_config(config_path)
            queue = RadarWorkQueue(_queue_path(config))
            try:
                run_discovery_cycle(
                    config,
                    queue,
                    config_path=config_path,
                    status_path=status_path,
                    stop_path=stop_path,
                    on_step=on_step,
                    cycle=discovery_cycle,
                )
            except Exception as exc:  # noqa: BLE001
                on_step(f"Discovery lane error: {type(exc).__name__}: {exc}")
                _write_lane_status(
                    status_path,
                    "discovery",
                    "error",
                    queue_stats=queue.stats(),
                    cycle=discovery_cycle,
                    error=str(exc),
                    message=f"{type(exc).__name__}: {exc}",
                )
            finally:
                if discovery_cycle == 1:
                    first_discovery_done.set()
            if once:
                return
            wait = max(60, int(config.discovery_interval_minutes * 60))

            def discovery_tick(left: int) -> None:
                _write_lane_status(
                    status_path,
                    "discovery",
                    "sleeping",
                    queue_stats=queue.stats(),
                    cycle=discovery_cycle,
                    next_scan_seconds=left,
                    message=f"next search in {left}s",
                )

            _wait_for_stop(
                wait,
                stop_path,
                tick=discovery_tick,
                stop_event=shutdown_event,
            )

    if once:
        discovery_loop()
    else:
        discovery_thread = threading.Thread(
            target=discovery_loop,
            name="radar-discovery",
            daemon=False,
        )
        discovery_thread.start()
        # On process startup, let the first fast harvest establish its durable
        # batch before the expensive organizer claims work. Without this one-time
        # handshake, the organizer can start a long maintenance pass seconds too
        # early and leave the first discovered channels waiting until the next pass.
        while (
            not first_discovery_done.wait(timeout=1)
            and not stop_requested(stop_path)
            and not shutdown_event.is_set()
        ):
            pass

    try:
        while (
            not stop_requested(stop_path)
            and not shutdown_event.is_set()
        ):
            organizer_cycle += 1
            config = load_config(config_path)
            queue = RadarWorkQueue(_queue_path(config))
            batch: RadarQueueBatch | None = queue.claim_organization(worker_name)
            local_harvest_enabled = config.harvest_enabled
            organizer_config = replace(config, harvest_enabled=False)
            stats = queue.stats()
            message = (
                f"organizing {batch.channel_count:,} queued channels"
                if batch
                else "running scheduled maintenance"
            )
            _write_lane_status(
                status_path,
                "organizer",
                "running",
                queue_stats=stats,
                cycle=organizer_cycle,
                message=message,
            )
            try:
                run_cycle(
                    organizer_config,
                    status_path=status_path,
                    stop_path=stop_path,
                    on_step=on_step,
                    cycle=organizer_cycle,
                    status_lane="organizer",
                    organization_only=(
                        bool(batch) or not local_harvest_enabled
                    ),
                )
                interrupted = stop_requested(stop_path)
                if batch:
                    if interrupted:
                        queue.release(
                            batch,
                            "worker stopped before organizer cycle completed",
                        )
                    else:
                        queue.complete(batch)
                if interrupted:
                    _write_lane_status(
                        status_path,
                        "organizer",
                        "stopping",
                        queue_stats=queue.stats(),
                        cycle=organizer_cycle,
                        organized_channels=0,
                        message="paused safely; claimed work returned to the queue",
                    )
                else:
                    _write_lane_status(
                        status_path,
                        "organizer",
                        "cycle_complete",
                        queue_stats=queue.stats(),
                        cycle=organizer_cycle,
                        organized_channels=batch.channel_count if batch else 0,
                        message=(
                            f"organized {batch.channel_count:,} queued channels"
                            if batch
                            else "scheduled maintenance complete"
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                if batch:
                    queue.release(batch, str(exc))
                _mark_latest_radar_job_failed(config, exc)
                on_step(f"Organizer lane error: {type(exc).__name__}: {exc}")
                _write_lane_status(
                    status_path,
                    "organizer",
                    "error",
                    queue_stats=queue.stats(),
                    cycle=organizer_cycle,
                    error=str(exc),
                    message=f"{type(exc).__name__}: {exc}",
                )
                if once:
                    raise
            if once:
                break
            config = load_config(config_path)
            wait = max(60, int(config.interval_minutes * 60))

            def organizer_tick(left: int) -> None:
                _write_lane_status(
                    status_path,
                    "organizer",
                    "sleeping",
                    queue_stats=queue.stats(),
                    cycle=organizer_cycle,
                    next_scan_seconds=left,
                    message=f"next organization pass in {left}s",
                )

            _wait_for_stop(
                wait,
                stop_path,
                tick=organizer_tick,
                stop_event=shutdown_event,
            )
    finally:
        if not once:
            # An unexpected organizer-lane exit must also wake discovery.
            # Otherwise join() can wait forever while discovery keeps sleeping.
            shutdown_event.set()
            while discovery_thread.is_alive():
                discovery_thread.join(timeout=1)
    return organizer_cycle


def run_worker(
    *,
    config_path: str | os.PathLike = DEFAULT_CONFIG_PATH,
    status_path: str | os.PathLike = DEFAULT_STATUS_PATH,
    stop_path: str | os.PathLike = DEFAULT_STOP_PATH,
    pid_path: str | os.PathLike = DEFAULT_PID_PATH,
    once: bool = False,
    on_step: Progress = lambda _m: None,
) -> int:
    Path(status_path).parent.mkdir(parents=True, exist_ok=True)
    if _worker_already_running(pid_path, status_path):
        on_step("radar worker already running; exiting")
        status = read_status(status_path)
        status["last_skip_at"] = utc_now()
        status["last_skip_message"] = "another worker is active"
        _write_json(Path(status_path), status)
        return 0
    clear_stop(stop_path)
    Path(pid_path).parent.mkdir(parents=True, exist_ok=True)
    Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
    cycle = 0
    try:
        initial_config = load_config(config_path)
        if initial_config.split_pipeline_enabled:
            _write_status(
                status_path,
                "running",
                cycle=0,
                pipeline_mode="split",
                message="starting discovery and organizer lanes",
                db_path=initial_config.db_path,
                interval_minutes=initial_config.interval_minutes,
                discovery_interval_minutes=initial_config.discovery_interval_minutes,
                watchlist=initial_config.watchlist,
            )
            cycle = _run_split_pipeline(
                config_path=config_path,
                status_path=status_path,
                stop_path=stop_path,
                once=once,
                on_step=on_step,
            )
            return 0
        while not stop_requested(stop_path):
            cycle += 1
            config = load_config(config_path)
            _write_status(
                status_path,
                "running",
                cycle=cycle,
                message="worker running",
                db_path=config.db_path,
                interval_minutes=config.interval_minutes,
                watchlist=config.watchlist,
            )
            try:
                run_cycle(
                    config,
                    status_path=status_path,
                    stop_path=stop_path,
                    on_step=on_step,
                    cycle=cycle,
                )
            except Exception as exc:  # noqa: BLE001
                _mark_latest_radar_job_failed(config, exc)
                _write_status(status_path, "error", cycle=cycle, error=str(exc))
                if once:
                    raise
            if once:
                break
            config = load_config(config_path)
            wait = max(60, int(config.interval_minutes * 60))
            for left in range(wait, 0, -5):
                if stop_requested(stop_path):
                    break
                msg = f"sleeping {left}s before next radar scan"
                on_step(msg)
                _write_status(status_path, "sleeping", cycle=cycle, next_scan_seconds=left)
                time.sleep(min(5, left))
    finally:
        state = "stopped" if stop_requested(stop_path) else "idle"
        _write_status(status_path, state, cycle=cycle, message=f"worker {state}")
        p = Path(pid_path)
        if p.exists():
            p.unlink()
    return 0


def _worker_already_running(
    pid_path: str | os.PathLike,
    status_path: str | os.PathLike,
) -> bool:
    pid_file = Path(pid_path)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False
    if not _pid_is_running(pid):
        pid_file.unlink(missing_ok=True)
        return False
    status = read_status(status_path)
    if status.get("state") not in {"running", "sleeping"}:
        return False
    updated = str(status.get("updated_at") or "")
    try:
        stamp = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
        return age < 6 * 60 * 60
    except Exception:  # noqa: BLE001
        return True


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    # On Windows os.kill(pid, 0) is NOT a liveness probe — signal 0 maps to
    # CTRL_C_EVENT, which raises WinError 87 for an arbitrary pid and would crash
    # worker startup whenever a pidfile already exists. Use the Win32 API instead.
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _clean_watchlist(items: list) -> list[str]:
    return list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))


_STATUS_LOCK = threading.RLock()


def _write_status(path: str | os.PathLike, state: str, **extra) -> None:
    with _STATUS_LOCK:
        payload = read_status(path)
        if state != "error":
            extra.pop("error", None)
        completed = payload.get("last_completed")
        if state == "cycle_complete":
            completed = {
                "completed_at": utc_now(),
                **extra,
            }
        if state in {"running", "sleeping", "stopping", "error", "idle", "stopped"}:
            for key in (
                "discovered",
                "channels",
                "enriched",
                "outliers",
                "fresh_outliers",
                "global_outliers",
                "new_channels",
                "face_checked",
                "fresh_max_age_days",
                "global_max_age_days",
            ):
                payload.pop(key, None)
        if state != "error":
            payload.pop("error", None)
        payload.update({"state": state, "updated_at": utc_now(), **extra})
        if completed:
            payload["last_completed"] = completed
        _write_json(Path(path), payload)


def _write_lane_status(
    path: str | os.PathLike,
    lane: str,
    state: str,
    *,
    queue_stats: dict | None = None,
    **extra,
) -> None:
    """Atomically update one split-pipeline lane without erasing the other."""
    with _STATUS_LOCK:
        payload = read_status(path)
        name = str(lane)
        current = payload.get(name)
        lane_payload = dict(current) if isinstance(current, dict) else {}
        if state != "error":
            extra.pop("error", None)
            lane_payload.pop("error", None)
        if state == "cycle_complete":
            lane_payload["last_completed"] = {
                "completed_at": utc_now(),
                **extra,
            }
        lane_payload.update({"state": state, "updated_at": utc_now(), **extra})
        payload[name] = lane_payload
        payload["pipeline_mode"] = "split"
        payload["state"] = "running"
        payload["updated_at"] = utc_now()
        if queue_stats is not None:
            payload["queue"] = dict(queue_stats)
        parts = []
        for key, label in (("discovery", "Discovery"), ("organizer", "Organizer")):
            info = payload.get(key)
            if not isinstance(info, dict):
                continue
            detail = info.get("message") or info.get("current_query") or info.get("state")
            if detail:
                parts.append(f"{label}: {detail}")
        payload["message"] = " | ".join(parts) or "split pipeline running"
        payload.pop("current_query", None)
        _write_json(Path(path), payload)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
