"""Find NEW channels (breadth), not deeper videos on known channels.

Cataloging goes *deeper* on channels you already have. This engine goes *wider*:
it runs many niche searches through the free providers and harvests every unique
channel that appears, then labels each channel with a category derived from the
queries that surfaced it. The goal is to grow the channel count from thousands
toward tens of thousands, then have those channels cleanly categorized so video
filters can be layered on later.

Two jobs:

* ``harvest``    -- run searches wide, collect every new channel (free, no quota).
* ``categorize`` -- label channels faceless/not + assign a niche category from
  the discovery queries (free; computed from data already collected).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

from beacon_engine.logging_util import get_logger
from beacon_engine.research.crawler import CrawlReport, YouTubeCrawler
from beacon_engine.research.faceless_filter import classify_store_channels
from beacon_engine.research.providers import ProviderVideo
from beacon_engine.research.store import YouTubeStore

log = get_logger()

Progress = Callable[[str], None]

# Faceless-format modifiers used to multiply a base topic into many real search
# queries, so one niche fans out into the many phrasings real channels rank for.
QUERY_MODIFIERS = [
    "documentary",
    "explained",
    "mystery",
    "facts",
    "story",
    "history",
    "the truth about",
    "rise and fall",
    "case study",
    "exposed",
]


def expand_queries(bases: Iterable[str], *, modifiers: list[str] | None = None) -> list[str]:
    """Fan each base topic out into many phrasings, de-duplicated, order-stable."""
    mods = modifiers or QUERY_MODIFIERS
    out: list[str] = []
    for base in bases:
        base = (base or "").strip()
        if not base:
            continue
        out.append(base)
        low = base.lower()
        for mod in mods:
            if mod in low:
                continue
            out.append(f"{base} {mod}" if not mod.startswith("the ") else f"{mod} {base}")
    return list(dict.fromkeys(out))


@dataclass
class HarvestReport:
    queries: int = 0
    searched: int = 0
    videos_seen: int = 0
    channels_before: int = 0
    channels_after: int = 0
    providers_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def new_channels(self) -> int:
        return max(0, self.channels_after - self.channels_before)

    def as_dict(self) -> dict:
        return {
            "queries": self.queries,
            "searched": self.searched,
            "videos_seen": self.videos_seen,
            "channels_before": self.channels_before,
            "channels_after": self.channels_after,
            "new_channels": self.new_channels,
            "providers_used": self.providers_used,
            "errors": self.errors,
        }


@dataclass
class CategorizeReport:
    faceless: int = 0
    not_faceless: int = 0
    categorized: int = 0
    categories: list[tuple[str, int]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "faceless": self.faceless,
            "not_faceless": self.not_faceless,
            "categorized": self.categorized,
            "categories": self.categories,
        }


class ChannelDiscoveryEngine:
    def __init__(
        self,
        store: YouTubeStore | None = None,
        crawler: YouTubeCrawler | None = None,
        crawler_factory: Callable[[], YouTubeCrawler] | None = None,
    ) -> None:
        self.store = store or YouTubeStore()
        self.crawler = crawler or YouTubeCrawler(self.store)
        self._crawler_factory = crawler_factory or (
            lambda: YouTubeCrawler(self.store))

    def harvest(
        self,
        queries: Iterable[str],
        *,
        per_query: int = 60,
        expand: bool = False,
        providers: list[str] | None = None,
        max_queries: int | None = None,
        sort: str | None = None,
        workers: int = 1,
        on_query_result: Callable[[str, int], None] | None = None,
        on_query_attempt: Callable[[str], None] | None = None,
        on_step: Progress = lambda _m: None,
    ) -> HarvestReport:
        """Search wide and collect every new channel the results expose.

        `sort` is forwarded to the search providers (e.g. "newest" for upload-date
        order). `on_query_attempt(query)` is called after every successful provider
        response, including a successful search with no results.
        `on_query_result(query, new_channels)` is called after each query
        with how many NEW channels that single query added — used by the radar to
        track per-query yield and retire tapped-out queries. The callbacks are
        optional and default to the original behavior."""
        all_queries = expand_queries(queries) if expand else list(dict.fromkeys(
            q.strip() for q in queries if q and q.strip()
        ))
        if max_queries is not None:
            all_queries = all_queries[:max_queries]
        report = HarvestReport(
            queries=len(all_queries),
            channels_before=self.store.count_channels(),
        )
        on_step(
            f"Harvesting channels across {len(all_queries):,} queries "
            f"({'expanded' if expand else 'as-is'}, {per_query} videos each)"
        )
        prev = report.channels_before
        worker_count = max(1, int(workers or 1))
        can_fetch_parallel = (
            bool(all_queries)
            and worker_count > 1
            and hasattr(self.crawler, "fetch_search")
            and hasattr(self.crawler, "store_fetched_search")
        )
        fetched: list[tuple[list[ProviderVideo], CrawlReport] | Exception] = []
        if can_fetch_parallel:
            fetched = [RuntimeError("search result missing")] * len(all_queries)
            local = threading.local()

            def fetch_one(query: str):
                crawler = getattr(local, "crawler", None)
                if crawler is None:
                    crawler = self._crawler_factory()
                    local.crawler = crawler
                return crawler.fetch_search(
                    query,
                    limit=per_query,
                    providers=providers,
                    sort=sort,
                )

            with ThreadPoolExecutor(
                max_workers=min(worker_count, len(all_queries)),
                thread_name_prefix="harvest",
            ) as executor:
                futures = {
                    executor.submit(fetch_one, query): index
                    for index, query in enumerate(all_queries)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        fetched[index] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        fetched[index] = exc

        for idx, query in enumerate(all_queries, 1):
            try:
                if can_fetch_parallel:
                    result = fetched[idx - 1]
                    if isinstance(result, Exception):
                        raise result
                    videos, sub = result
                    self.crawler.store_fetched_search(query, videos, sub)
                else:
                    # Stay strictly additive: only pass `sort` when explicitly
                    # requested, so older crawler contracts remain unchanged.
                    search_kwargs: dict = {
                        "limit": per_query,
                        "enrich": False,
                        "providers": providers,
                    }
                    if sort is not None:
                        search_kwargs["sort"] = sort
                    sub = self.crawler.crawl_search(query, **search_kwargs)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue                    # a hard failure is NOT a yield signal
            report.searched += 1
            report.videos_seen += sub.discovered
            for name in sub.providers_used:
                if name not in report.providers_used:
                    report.providers_used.append(name)
            running = self.store.count_channels()
            if on_query_attempt is not None and sub.providers_used:
                try:
                    on_query_attempt(query)
                except Exception:  # noqa: BLE001
                    pass
            # Only report yield when the search actually returned results — a
            # blocked/rate-limited/empty search (discovered == 0) must NOT be
            # counted as "zero new", or a temporary block would retire a query
            # that is actually still productive.
            if on_query_result is not None and sub.discovered > 0:
                try:
                    on_query_result(query, max(0, running - prev))
                except Exception:  # noqa: BLE001
                    pass
            prev = running
            on_step(
                f"{idx}/{len(all_queries)} '{query[:40]}' "
                f"-> {running - report.channels_before:,} new channels so far"
            )
        report.channels_after = self.store.count_channels()
        return report

    def categorize(
        self,
        *,
        faceless_mode: str = "balanced",
        only_missing: bool = False,
        on_step: Progress = lambda _m: None,
    ) -> CategorizeReport:
        """Label channels faceless/not and tag each with a niche category."""
        on_step("Classifying channels (faceless vs not)...")
        signals = classify_store_channels(
            self.store,
            mode=faceless_mode,
            only_missing=only_missing,
        )
        faceless = sum(1 for s in signals.values() if s.matches(faceless_mode))

        on_step("Assigning niche categories from discovery queries...")
        dominant = self.store.channel_dominant_queries(
            only_missing=only_missing)
        categorized = self.store.set_channel_categories(
            {cid: query for cid, (query, _hits) in dominant.items()}
        )
        return CategorizeReport(
            faceless=faceless,
            not_faceless=len(signals) - faceless,
            categorized=categorized,
            categories=self.store.category_breakdown(),
        )
