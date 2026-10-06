"""AI-guided scraper flow: channel insight -> crawl plan -> local index -> bundle.

This is the bridge between the "smart entity" and the raw crawler. The LLM plans
where to search, the free-provider crawler executes the plan, and the result is
rendered back into the same ResearchBundle shape used by Strategy, Ideas, Write,
and the chat assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Callable

from pydantic import BaseModel, Field

from beacon_engine.config import Tier
from beacon_engine.json_utils import as_list, extract_json
from beacon_engine.llm import LLM
from beacon_engine.research.channel_style import split_by_format
from beacon_engine.research.crawler import CrawlReport, YouTubeCrawler
from beacon_engine.research.faceless_filter import classify_store_channels, score_channel_text
from beacon_engine.research.models import (
    CeilingFloor,
    ChannelInsight,
    ChannelProfile,
    CompetitorChannel,
    NicheReport,
    ResearchBundle,
    TitlePattern,
    VideoStat,
)
from beacon_engine.research.niche_report import build_niche_report
from beacon_engine.research.outliers import IndexedOutlier, find_outliers
from beacon_engine.research.store import YouTubeStore

Progress = Callable[[str], None]

FREE_PROVIDER_NAMES = [
    "scrapetube",
    "yt-dlp",
    "piped",
    "invidious",
    "youtube-comment-downloader",
    "youtube-transcript-api",
]


class CrawlBudget(BaseModel):
    # Wider by default so the niche search surfaces MANY competitor channels, not
    # just a handful. More queries + more results per query = more channels found.
    seed_results: int = 45
    max_channels: int = 5
    videos_per_channel: int = 40
    enrich_top: int = 15
    queries_to_run: int = 6


class SmartScrapePlan(BaseModel):
    niche: str
    target_format: str = ""
    audience: str = ""
    seed_queries: list[str] = Field(default_factory=list)
    adjacent_queries: list[str] = Field(default_factory=list)
    # Same FORMAT, different topic: the channel's format ("X brands exposed")
    # transferred onto other product categories (shampoo, supplements, cars...).
    format_transfer_queries: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    relevant_channel_traits: list[str] = Field(default_factory=list)
    notes: str = ""
    budget: CrawlBudget = Field(default_factory=CrawlBudget)

    def queries(self) -> list[str]:
        seen, out = set(), []
        for q in self.seed_queries + self.adjacent_queries:
            key = q.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(q.strip())
        return out[: max(self.budget.queries_to_run, 1)]

    def transfer_queries(self, limit: int = 6) -> list[str]:
        seen, out = set(), []
        for q in self.format_transfer_queries:
            key = q.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(q.strip())
        return out[:limit]


@dataclass
class SmartScrapeResult:
    profile: ChannelProfile
    insight: ChannelInsight
    plan: SmartScrapePlan
    bundle: ResearchBundle
    reports: list[CrawlReport] = field(default_factory=list)
    outliers: list[IndexedOutlier] = field(default_factory=list)
    db_path: str = ""
    niche_report: NicheReport | None = None   # vision-driven competition/opportunity read

    @property
    def discovered(self) -> int:
        return sum(r.discovered for r in self.reports)

    @property
    def channels(self) -> int:
        return sum(r.channels for r in self.reports)

    @property
    def enriched(self) -> int:
        return sum(r.enriched for r in self.reports)


_PLAN_SYSTEM = """You are the crawl planner for a faceless YouTube research app.
Given one analyzed channel, design a focused scraping plan that finds similar channels,
adjacent niches, and breakout videos WITHOUT wandering into generic or wrong-format
results.

Return ONLY JSON:
{
  "niche": "...",
  "target_format": "...",
  "audience": "...",
  "seed_queries": ["6-10 specific viewer searches, 4-8 words each, each a DIFFERENT angle on the niche"],
  "adjacent_queries": ["4-6 nearby niches/formats worth checking"],
  "format_transfer_queries": ["5-8 searches that keep this channel's FORMAT but swap the TOPIC to a totally different product/category"],
  "avoid": ["wrong formats/topics to filter mentally"],
  "relevant_channel_traits": ["what makes a scraped channel relevant"],
  "notes": "short strategy note",
  "budget": {
    "seed_results": 30-80,
    "max_channels": 3-8,
    "videos_per_channel": 20-80,
    "enrich_top": 10-25,
    "queries_to_run": 5-10
  }
}

seed_queries: MANY distinct angles on the SAME topic (more varied searches surface
more competitors). Bias the budget toward the higher end.

format_transfer_queries: keep the channel's WINNING FORMAT/structure but apply it to
DIFFERENT products or categories, to find creators doing the same style elsewhere.
Example — if the channel does "[food] brands exposed / X to avoid" investigative
faceless videos, transfer that format to: "shampoo brands to avoid", "supplement
brands exposed", "makeup brands ranked worst", "car brands you should never buy",
"cleaning products toxic exposed". Match the FORMAT (exposé/ranking/investigation),
change the SUBJECT. These reveal how the format travels to other niches.

Prefer concrete multi-word queries. Avoid single generic terms."""


def plan_smart_scrape(llm: LLM, profile: ChannelProfile, insight: ChannelInsight) -> SmartScrapePlan:
    """Ask the model for a crawler plan. Falls back to deterministic themes if needed."""
    top = "\n".join(f"- [{v.views:,}] {v.title}" for v in profile.top_videos[:12])
    recent = "\n".join(f"- {t}" for t in profile.recent_titles[:12])
    prompt = (
        f"CHANNEL: {profile.title}\n"
        f"DESCRIPTION: {profile.description[:800]}\n"
        f"NICHE: {insight.niche}\n"
        f"SUB-NICHE: {insight.sub_niche}\n"
        f"FORMAT: {insight.format}\n"
        f"AUDIENCE: {insight.audience}\n"
        f"POSITIONING: {insight.positioning}\n\n"
        f"TOP VIDEOS:\n{top}\n\nRECENT TITLES:\n{recent}\n\n"
        "Design the scraping plan."
    )
    try:
        data = extract_json(llm.complete(
            task="analysis",
            tier=Tier.SONNET,
            instructions=_PLAN_SYSTEM,
            prompt=prompt,
            thinking=True,
            effort="medium",
            max_tokens=1800,
        ).text)
        if isinstance(data, dict):
            return _sanitize_plan(SmartScrapePlan(**data), profile, insight)
    except Exception:  # noqa: BLE001
        pass
    return fallback_plan(profile, insight)


def fallback_plan(profile: ChannelProfile, insight: ChannelInsight) -> SmartScrapePlan:
    """Deterministic plan used in tests or when the LLM is unavailable."""
    base = [q for q in insight.themes if q.strip()]
    if not base:
        base = [insight.niche or profile.title]
    niche = insight.niche or profile.title
    seed = list(dict.fromkeys(base + [
        f"{niche} explained",
        f"{niche} documentary",
        f"{niche} beginners",
        f"{niche} mistakes",
    ]))
    adjacent = [
        f"{niche} case study",
        f"{niche} transformation",
        f"{niche} expert tips",
    ]
    # Same format, other products — deterministic transfer of the "brands exposed /
    # X to avoid" investigative shape onto common consumer categories.
    transfer = [
        "shampoo brands to avoid",
        "supplement brands exposed",
        "makeup brands ranked worst",
        "cleaning products toxic exposed",
        "car brands you should never buy",
    ]
    return SmartScrapePlan(
        niche=niche,
        target_format=insight.format,
        audience=insight.audience,
        seed_queries=seed[:8],
        adjacent_queries=adjacent,
        format_transfer_queries=transfer,
        avoid=["shorts spam", "news clips", "unrelated celebrity content"],
        relevant_channel_traits=[
            "repeats the same niche topics",
            "long-form videos",
            "similar audience promise",
            "titles overlap with the analyzed channel",
        ],
        notes="Fallback plan from inferred themes.",
        budget=CrawlBudget(seed_results=45, max_channels=5, videos_per_channel=30,
                           enrich_top=15, queries_to_run=6),
    )


def run_smart_scrape(
    llm: LLM,
    profile: ChannelProfile,
    insight: ChannelInsight,
    *,
    store: YouTubeStore | None = None,
    plan: SmartScrapePlan | None = None,
    providers: list[str] | None = None,
    faceless_only: bool = True,
    vision_filter: bool = True,
    with_report: bool = True,
    with_format_twins: bool = True,
    style_threshold: float = 6.0,
    on_step: Progress = lambda _m: None,
) -> SmartScrapeResult:
    store = store or YouTubeStore()
    providers = providers or FREE_PROVIDER_NAMES
    on_step("Using supplied smart scrape plan..." if plan else "Planning the smart scrape with AI...")
    plan = plan or plan_smart_scrape(llm, profile, insight)

    crawler = YouTubeCrawler(store)
    reports: list[CrawlReport] = []
    queries = plan.queries()
    for idx, query in enumerate(queries, 1):
        on_step(f"Smart scrape query {idx}/{len(queries)}: {query}")
        report = crawler.crawl_niche(
            query,
            seed_limit=plan.budget.seed_results,
            max_channels=plan.budget.max_channels,
            videos_per_channel=plan.budget.videos_per_channel,
            enrich=True,
            extras_limit=plan.budget.enrich_top,
            providers=providers,
            faceless_only=faceless_only,
            on_step=on_step,
        )
        reports.append(report)

    # Scope everything (competitors, trending, outliers, the vision report) to the
    # channels this niche's search actually surfaced — NOT the whole shared index,
    # which is dominated by unrelated mega-channels (MrBeast, music, kids).
    niche_hits = _niche_channel_hits(store, plan.queries())
    niche_ids = _niche_scope_ids(niche_hits)
    niche_video_ids = _niche_video_ids(store, plan.queries())
    if niche_ids:
        on_step(f"Scoped to {len(niche_ids)} channels that cover this niche.")
    else:
        on_step("No niche discoveries recorded; using the global index (less precise).")

    on_step("Scoring local outliers for this niche...")
    outliers = find_outliers(
        store,
        limit=None,
        min_channel_videos=5,
        min_views=10_000,
        min_score=2.0,
        max_age_days=365,
    )
    # Outliers must be niche-relevant VIDEOS (ranked for the niche search) AND from a
    # real niche channel — so a prank channel's one keyword-matched viral clip is out.
    if niche_video_ids:
        outliers = [item for item in outliers if item.video_id in niche_video_ids]
    if niche_ids:
        outliers = [item for item in outliers if item.channel_id in niche_ids]
    if faceless_only:
        channel_signals = classify_store_channels(store, mode="strict")
        outliers = [
            item for item in outliers
            if (
                item.channel_id in channel_signals
                and channel_signals[item.channel_id].matches("strict")
            )
        ]
    outliers = outliers[:40]
    bundle = bundle_from_index(
        store,
        profile,
        insight,
        plan,
        outliers,
        llm=llm,
        require_faceless=faceless_only,
        vision_filter=vision_filter,
        style_threshold=style_threshold,
        niche_hits=niche_hits or None,
        on_step=on_step,
    )

    if with_format_twins and plan.transfer_queries():
        try:
            bundle.format_twins = _crawl_format_twins(
                crawler, store, profile, plan, reports,
                exclude_ids=niche_ids, llm=llm, providers=providers,
                require_faceless=faceless_only, vision_filter=vision_filter,
                style_threshold=style_threshold, on_step=on_step,
            )
        except Exception:  # noqa: BLE001 — format-twins are a bonus, never fatal
            on_step("Format-twin discovery skipped (error); rest of the study is intact.")

    niche_report: NicheReport | None = None
    if with_report:
        on_step("Building the vision niche report (thumbnails + competition)...")
        try:
            niche_report = build_niche_report(
                llm, profile, insight, outliers,
                niche=plan.niche, on_step=on_step,
            )
            # The thumbnail playbook doubles as the bundle's thumbnail intel, so the
            # Packaging Lab designs against what the vision pass actually saw.
            if niche_report and niche_report.thumbnail_playbook and not bundle.thumbnail_intel:
                bundle.thumbnail_intel = niche_report.thumbnail_playbook
        except Exception:  # noqa: BLE001 — a report failure must not sink the scrape
            on_step("Niche report skipped (vision step failed); scrape data is intact.")

    return SmartScrapeResult(
        profile=profile,
        insight=insight,
        plan=plan,
        bundle=bundle,
        reports=reports,
        outliers=outliers,
        db_path=str(store.path),
        niche_report=niche_report,
    )


def _topic_relevant_indices(llm, niche_desc: str, labels: list[str], *,
                            kind: str = "items") -> set[int]:
    """LLM TOPIC GATE. Discovery relevance is purely LEXICAL — a video counts as niche-
    relevant if its title matched a search keyword — so keyword collisions slip in (LEGO
    'tricks/builds/secret' videos or ant-colony clips landing in an Amish frugal-living
    niche). This asks the model which items are GENUINELY about the niche's subject and
    returns the indices to KEEP. Niche-agnostic. Fail-open (keep all) on any error so it
    can never wrongly empty a section."""
    if not (llm and niche_desc and labels):
        return set(range(len(labels)))
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(labels))
    try:
        txt = llm.complete(
            task="fast",
            tier=Tier.HAIKU,
            instructions=("You judge whether YouTube items are genuinely ABOUT a given niche's "
                          "subject matter — not just a keyword match. Output ONLY a JSON array "
                          "of the item numbers that are on-topic."),
            prompt=(f"NICHE: {niche_desc}\n\nThese {len(labels)} YouTube {kind} were found by "
                    "keyword search, so some are OFF-TOPIC collisions (a LEGO, gaming, toy, or "
                    "ant-colony video is NOT about a home/frugal/food niche just because one "
                    f"word matched):\n{numbered}\n\nReturn ONLY a JSON array of the numbers whose "
                    "ACTUAL subject is this niche. Be strict — exclude keyword-only matches."),
            thinking=False, effort="low", max_tokens=500).text
        arr = extract_json(txt)
        if isinstance(arr, list):
            keep = {int(x) - 1 for x in arr if str(x).strip().lstrip("-").isdigit()}
            keep = {i for i in keep if 0 <= i < len(labels)}
            if keep:
                return keep
    except Exception:  # noqa: BLE001 — fail-open: keep everything if the gate errors
        pass
    return set(range(len(labels)))


def bundle_from_index(
    store: YouTubeStore,
    profile: ChannelProfile,
    insight: ChannelInsight,
    plan: SmartScrapePlan,
    outliers: list[IndexedOutlier],
    *,
    llm: LLM | None = None,
    require_faceless: bool = True,
    vision_filter: bool = True,
    style_threshold: float = 6.0,
    niche_hits: dict[str, int] | None = None,
    on_step: Progress = lambda _m: None,
) -> ResearchBundle:
    # Trending + title patterns come from the VIDEOS that actually ranked for the
    # niche searches (topic-relevant), not the top-viewed videos of any channel that
    # scraped into scope. `outliers` are already video-scoped by the caller.
    scope_ids = _niche_scope_ids(niche_hits) if niche_hits else None
    faceless_ids = _faceless_channel_ids(store) if require_faceless else None
    patterns = [
        TitlePattern(title=o.title, views=o.views, channel=o.channel, url=o.url)
        for o in outliers[:30]
    ]
    patterns = _dedupe_patterns(
        patterns + _niche_top_patterns(store, plan.queries(), limit=30,
                                       channel_scope=scope_ids,
                                       prefer_channel_ids=faceless_ids)
    )[:30]
    # TOPIC GATE — drop keyword-collision junk (e.g. LEGO / ant-colony videos that matched
    # "tricks/builds/secret" but aren't about the niche) BEFORE they become title patterns.
    niche_desc = " — ".join(x for x in [insight.niche, insight.sub_niche,
                                        insight.positioning] if x)
    if llm and niche_desc and patterns:
        keep = _topic_relevant_indices(
            llm, niche_desc, [f"{p.title}  ·  {p.channel}" for p in patterns],
            kind="video titles")
        if 0 < len(keep) < len(patterns):
            on_step(f"Topic filter: cut {len(patterns) - len(keep)} off-topic title patterns.")
            patterns = [p for i, p in enumerate(patterns) if i in keep]
    ceiling_floors: list[CeilingFloor] = []
    ceiling_stat = _ceiling_from_outlier(outliers[0]) if outliers else _top_video_stat(store)
    if ceiling_stat and patterns:
        ceiling_floors.append(CeilingFloor(
            keyword=f"smart scrape: {plan.niche}",
            time_window="smart_scraper",
            ceiling=ceiling_stat,
            floor_views=patterns[-1].views,
            patterns=patterns,
        ))
    competitors = _competitors_from_store(
        store, exclude_id=profile.channel_id, niche_hits=niche_hits)
    direct_competitors, inspiration_channels = _filter_competitors_for_faceless(
        store,
        profile,
        competitors,
        llm=llm,
        require_faceless=require_faceless,
        vision_filter=vision_filter,
        style_threshold=style_threshold,
        on_step=on_step,
    )
    # Inspiration = same TOPIC, different format. Rank by how much of the niche each
    # channel covers so the tangential ones (a science channel's one food video)
    # sink below the real topic channels; drop the barely-relevant tail.
    if niche_hits and scope_ids:
        inspiration_channels = [c for c in inspiration_channels if c.channel_id in scope_ids]
        inspiration_channels.sort(
            key=lambda c: (niche_hits.get(c.channel_id, 0), c.median_views), reverse=True)

    # TOPIC GATE on the channel lists too — a LEGO channel that keyword-collided is not a
    # topic-relevant competitor OR inspiration, no matter how big it is.
    def _label(c) -> str:
        samp = (c.sample_titles[0] if getattr(c, "sample_titles", None) else "")
        return f"{c.title}  ·  {samp}" if samp else c.title

    if llm and niche_desc:
        if direct_competitors:
            keep = _topic_relevant_indices(
                llm, niche_desc, [_label(c) for c in direct_competitors], kind="channels")
            if 0 < len(keep) < len(direct_competitors):
                on_step(f"Topic filter: cut {len(direct_competitors) - len(keep)} "
                        "off-topic competitors.")
                direct_competitors = [c for i, c in enumerate(direct_competitors) if i in keep]
        if inspiration_channels:
            keep = _topic_relevant_indices(
                llm, niche_desc, [_label(c) for c in inspiration_channels], kind="channels")
            if 0 < len(keep) < len(inspiration_channels):
                on_step(f"Topic filter: cut {len(inspiration_channels) - len(keep)} "
                        "off-topic inspiration channels.")
                inspiration_channels = [c for i, c in enumerate(inspiration_channels)
                                        if i in keep]
    return ResearchBundle(
        niche=insight.niche or plan.niche,
        keywords=plan.queries(),
        ceiling_floors=ceiling_floors,
        competitors=direct_competitors[:24],
        inspiration_channels=inspiration_channels[:30],
        trending=patterns[:20],
    )


def _crawl_format_twins(
    crawler: YouTubeCrawler,
    store: YouTubeStore,
    profile: ChannelProfile,
    plan: SmartScrapePlan,
    reports: list[CrawlReport],
    *,
    exclude_ids: set[str] | None,
    llm: LLM | None,
    providers: list[str] | None,
    require_faceless: bool,
    vision_filter: bool,
    style_threshold: float,
    on_step: Progress,
) -> list[CompetitorChannel]:
    """Find channels using the SAME format on a DIFFERENT topic.

    Crawls the AI's format-transfer searches ("shampoo brands exposed", ...), then
    keeps the channels that match OUR format (faceless) but sit OUTSIDE our topic."""
    transfer = plan.transfer_queries(limit=8)
    if not transfer:
        return []
    on_step(f"Format-transfer scrape: {len(transfer)} other-topic searches...")
    for idx, query in enumerate(transfer, 1):
        on_step(f"Format-twin query {idx}/{len(transfer)}: {query}")
        reports.append(crawler.crawl_niche(
            query,
            seed_limit=min(plan.budget.seed_results, 45),
            max_channels=min(plan.budget.max_channels, 4),
            videos_per_channel=min(plan.budget.videos_per_channel, 30),
            enrich=True,
            extras_limit=plan.budget.enrich_top,
            providers=providers,
            faceless_only=require_faceless,
            on_step=on_step,
        ))

    exclude = set(exclude_ids or set()) | {profile.channel_id}
    twin_hits = {cid: h for cid, h in _niche_channel_hits(store, transfer).items()
                 if cid not in exclude}   # DIFFERENT topic -> drop our niche channels
    if not twin_hits:
        return []
    # min_hits=1: transfer searches are fresh one-shots, so a single ranked video is
    # already meaningful — the >=3 bar that guards the main niche would starve twins.
    candidates = [c for c in _competitors_from_store(
        store, exclude_id=profile.channel_id, niche_hits=twin_hits, min_hits=1)
        if c.channel_id not in exclude]
    if require_faceless:
        # "Same format as us" == faceless like us. Keep the faceless matches.
        twins, _inspiration = _filter_competitors_for_faceless(
            store, profile, candidates, llm=llm, require_faceless=True,
            vision_filter=vision_filter, style_threshold=style_threshold, on_step=on_step)
    else:
        twins = candidates
    twins.sort(key=lambda c: (twin_hits.get(c.channel_id, 0), c.median_views), reverse=True)
    on_step(f"Found {len(twins)} format twins (same format, other topics).")
    return twins[:24]


def _sanitize_plan(
    plan: SmartScrapePlan, profile: ChannelProfile, insight: ChannelInsight
) -> SmartScrapePlan:
    if not plan.niche:
        plan.niche = insight.niche or profile.title
    if not plan.seed_queries:
        plan.seed_queries = fallback_plan(profile, insight).seed_queries
    if not plan.format_transfer_queries:
        plan.format_transfer_queries = fallback_plan(profile, insight).format_transfer_queries
    plan.budget.seed_results = _clamp(plan.budget.seed_results, 20, 80)
    plan.budget.max_channels = _clamp(plan.budget.max_channels, 2, 8)
    plan.budget.videos_per_channel = _clamp(plan.budget.videos_per_channel, 10, 80)
    plan.budget.enrich_top = _clamp(plan.budget.enrich_top, 0, 25)
    plan.budget.queries_to_run = _clamp(plan.budget.queries_to_run, 3, 10)
    return plan


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(int(value or lo), hi))


def _niche_channel_hits(store: YouTubeStore, queries: list[str]) -> dict[str, int]:
    """channel_id -> how many videos it had rank for THIS scrape's niche searches.

    This is the relevance signal. Without it, competitor/trending selection reads
    the whole shared index and returns the globally-biggest channels (MrBeast,
    music, kids). Ranking by niche hits instead surfaces the channels that ACTUALLY
    cover the niche — a channel with 30 videos matching "food brands exposed" beats
    a prank channel whose one viral clip happened to match."""
    qs = [q.strip() for q in queries if q and q.strip()]
    if not qs:
        return {}
    placeholders = ",".join("?" for _ in qs)
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT v.channel_id, COUNT(DISTINCT v.video_id) AS hits
            FROM video_discoveries d
            JOIN videos v ON v.video_id = d.video_id
            WHERE d.discovery_kind = 'search' AND d.query IN ({placeholders})
              AND COALESCE(v.channel_id, '') != ''
            GROUP BY v.channel_id
            """,
            qs,
        ).fetchall()
    return {r["channel_id"]: int(r["hits"] or 0) for r in rows}


def _niche_video_ids(store: YouTubeStore, queries: list[str]) -> set[str]:
    """The actual VIDEOS that ranked for this niche's searches.

    Trending / title patterns / outliers must come from these, not from "all videos
    of any channel that got into scope" — otherwise a science channel with one
    food-ish hit drags in its unrelated mega-videos (Veritasium's 96M black balls)."""
    qs = [q.strip() for q in queries if q and q.strip()]
    if not qs:
        return set()
    placeholders = ",".join("?" for _ in qs)
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT d.video_id
            FROM video_discoveries d
            WHERE d.discovery_kind = 'search' AND d.query IN ({placeholders})
            """,
            qs,
        ).fetchall()
    return {r["video_id"] for r in rows}


def _faceless_channel_ids(store: YouTubeStore) -> set[str]:
    """Channels the persisted classifier believes are faceless (cheap read)."""
    from beacon_engine.research.faceless_filter import load_store_classifications
    try:
        signals = load_store_classifications(store)
    except Exception:  # noqa: BLE001
        return set()
    return {cid for cid, sig in signals.items() if sig.likely_faceless}


def _niche_top_patterns(
    store: YouTubeStore, queries: list[str], *, limit: int = 30,
    channel_scope: set[str] | None = None,
    prefer_channel_ids: set[str] | None = None,
) -> list[TitlePattern]:
    """Top videos (by views) that ranked for the niche searches — the topic-relevant
    trending/title-pattern source.

    When `channel_scope` is given, keep only videos from channels with real niche
    relevance. This is the two-signal filter: a video must BOTH rank for the niche
    query AND come from a niche channel — so a prank channel's "SECRET McDonald's"
    (matched "fast food" but isn't a food channel) is excluded."""
    qs = [q.strip() for q in queries if q and q.strip()]
    if not qs:
        return []
    placeholders = ",".join("?" for _ in qs)
    where_scope, scope_params = "", []
    if channel_scope:
        ids = list(channel_scope)[:900]
        where_scope = " AND v.channel_id IN (%s)" % ",".join("?" for _ in ids)
        scope_params = ids
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT v.video_id, v.title, MAX(v.views) AS views, v.channel,
                   v.channel_id, v.url
            FROM video_discoveries d
            JOIN videos v ON v.video_id = d.video_id
            WHERE d.discovery_kind = 'search' AND d.query IN ({placeholders})
              AND COALESCE(v.title, '') != ''{where_scope}
            GROUP BY v.video_id
            ORDER BY views DESC
            LIMIT ?
            """,
            qs + scope_params + [int(limit) * 3],   # over-fetch so preference can act
        ).fetchall()
    rows = [r for r in rows if r["title"]]
    if prefer_channel_ids:
        # The user is a faceless creator: faceless examples come first; on-camera
        # ones only fill leftover slots (topic inspiration, not format models).
        rows.sort(key=lambda r: (r["channel_id"] in prefer_channel_ids,
                                 int(r["views"] or 0)), reverse=True)
    return [
        TitlePattern(
            title=row["title"] or "", views=int(row["views"] or 0),
            channel=row["channel"] or "",
            url=row["url"] or f"https://www.youtube.com/watch?v={row['video_id']}",
        )
        for row in rows[:limit]
    ]


def _niche_scope_ids(niche_hits: dict[str, int], *, min_hits: int = 3) -> set[str]:
    """The channels relevant enough to treat as this niche. A real niche channel
    ranks for MANY niche videos; a tangential mega-channel ranks for 1-2. Requires
    `min_hits`+ (default 3) to cut the tangential ones; relaxes only if that would
    leave too few to work with."""
    if not niche_hits:
        return set()
    strong = {cid for cid, h in niche_hits.items() if h >= min_hits}
    if len(strong) >= 5:
        return strong
    relaxed = {cid for cid, h in niche_hits.items() if h >= 2}
    return relaxed if len(relaxed) >= 5 else set(niche_hits)


def _competitors_from_store(
    store: YouTubeStore, *, exclude_id: str = "", niche_hits: dict[str, int] | None = None,
    min_hits: int = 3,
) -> list[CompetitorChannel]:
    scope = _niche_scope_ids(niche_hits, min_hits=min_hits) if niche_hits else set()
    where = "WHERE v.channel_id != '' AND v.channel_id != ?"
    params: list[object] = [exclude_id]
    having = "HAVING COUNT(*) >= 2 AND MAX(v.views) > 0"
    titles_aggregate = (
        "STRING_AGG(v.title, ' || ')" if store.backend == "postgresql"
        else "GROUP_CONCAT(v.title, ' || ')"
    )
    if scope:
        ids = list(scope)[:900]
        where += " AND v.channel_id IN (%s)" % ",".join("?" for _ in ids)
        params += ids
        # niche-scoped: keep single-hit channels but still drop dead 0-view stubs
        having = "HAVING MAX(v.views) > 0"
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT v.channel_id AS channel_id,
                   COALESCE(NULLIF(MAX(c.title), ''), MAX(v.channel)) AS channel,
                   COUNT(*) AS videos_seen,
                   SUM(v.views) AS total_views, MAX(v.views) AS ceiling_views,
                   MIN(v.views) AS floor_views, {titles_aggregate} AS titles
            FROM videos v
            LEFT JOIN channels c ON c.channel_id = v.channel_id
            {where}
            GROUP BY v.channel_id
            {having}
            ORDER BY ceiling_views DESC, videos_seen DESC
            LIMIT 120
            """,
            params,
        ).fetchall()
    out: list[CompetitorChannel] = []
    for row in rows:
        views = _channel_views(store, row["channel_id"])
        titles = [t for t in (row["titles"] or "").split(" || ") if t][:3]
        out.append(CompetitorChannel(
            channel_id=row["channel_id"],
            title=row["channel"] or row["channel_id"],
            videos_seen=int(row["videos_seen"] or 0),
            total_views=int(row["total_views"] or 0),
            ceiling_views=int(row["ceiling_views"] or 0),
            median_views=int(median(views)) if views else 0,
            floor_views=int(row["floor_views"] or 0),
            sample_titles=titles,
            url=f"https://www.youtube.com/channel/{row['channel_id']}",
        ))
    if niche_hits:
        # Rank by how much of the niche each channel covers (its # of niche videos),
        # THEN by reach — so a dedicated food channel beats a giant general channel
        # whose one viral clip happened to match the search.
        out.sort(key=lambda c: (niche_hits.get(c.channel_id, 0), c.ceiling_views),
                 reverse=True)
    return out[:40]


def _filter_competitors_for_faceless(
    store: YouTubeStore,
    profile: ChannelProfile,
    competitors: list[CompetitorChannel],
    *,
    llm: LLM | None,
    require_faceless: bool,
    vision_filter: bool,
    style_threshold: float,
    on_step: Progress,
) -> tuple[list[CompetitorChannel], list[CompetitorChannel]]:
    if not competitors:
        return [], []
    videos_by_channel = _videos_by_channel_from_store(store, [c.channel_id for c in competitors[:60]])
    if require_faceless and vision_filter and llm and profile.top_videos:
        on_step("Vision-filtering channels for faceless format...")
        direct, inspiration = split_by_format(
            llm,
            profile,
            competitors[:36],
            videos_by_channel,
            threshold=style_threshold,
            require_faceless=True,
        )
        remaining = [c for c in competitors[36:] if c.channel_id not in {x.channel_id for x in direct + inspiration}]
        inspiration.extend(remaining)
        if direct:
            on_step(f"Faceless vision filter kept {len(direct)} direct channels.")
            return direct, inspiration
        on_step("Vision filter found no direct faceless channels; using text fallback.")

    if not require_faceless:
        return competitors[:24], competitors[24:48]
    return _text_faceless_split(competitors, videos_by_channel)


def _text_faceless_split(
    competitors: list[CompetitorChannel],
    videos_by_channel: dict[str, list[VideoStat]],
) -> tuple[list[CompetitorChannel], list[CompetitorChannel]]:
    direct: list[CompetitorChannel] = []
    inspiration: list[CompetitorChannel] = []
    for comp in competitors:
        signal = score_channel_text(videos_by_channel.get(comp.channel_id, []), channel_title=comp.title)
        comp.faceless = signal.likely_faceless
        if comp.faceless:
            comp.style_match = comp.style_match or max(5.0, min(8.0, 6.0 + signal.score))
            direct.append(comp)
        else:
            comp.style_match = comp.style_match or max(0.0, min(4.0, 4.0 + signal.score))
            inspiration.append(comp)
    return direct, inspiration


def _videos_by_channel_from_store(
    store: YouTubeStore,
    channel_ids: list[str],
    *,
    per_channel: int = 5,
) -> dict[str, list[VideoStat]]:
    ids = [cid for cid in dict.fromkeys(channel_ids) if cid]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT video_id, title, channel, channel_id, views, published_at, url
            FROM videos
            WHERE channel_id IN ({placeholders})
            ORDER BY channel_id, views DESC
            """,
            ids,
        ).fetchall()
    out: dict[str, list[VideoStat]] = {}
    for row in rows:
        bucket = out.setdefault(row["channel_id"], [])
        if len(bucket) >= per_channel:
            continue
        bucket.append(VideoStat(
            video_id=row["video_id"] or "",
            title=row["title"] or "",
            channel=row["channel"] or "",
            channel_id=row["channel_id"] or "",
            views=int(row["views"] or 0),
            published_at=row["published_at"] or "",
            url=row["url"] or f"https://www.youtube.com/watch?v={row['video_id']}",
        ))
    return out


def _ceiling_from_outlier(item: IndexedOutlier) -> VideoStat:
    return VideoStat(
        video_id=item.video_id,
        title=item.title,
        channel=item.channel,
        channel_id=item.channel_id,
        views=item.views,
        published_at="",
        url=item.url,
    )


def _top_video_stat(store: YouTubeStore) -> VideoStat | None:
    rows = store.videos(limit=1)
    if not rows:
        return None
    row = rows[0]
    return VideoStat(
        video_id=row["video_id"],
        title=row["title"] or "",
        channel=row["channel"] or "",
        channel_id=row["channel_id"] or "",
        views=int(row["views"] or 0),
        published_at=row["published_at"] or "",
        url=row["url"] or f"https://www.youtube.com/watch?v={row['video_id']}",
    )


def _top_patterns_from_store(
    store: YouTubeStore, *, limit: int, channel_ids: set[str] | None = None,
) -> list[TitlePattern]:
    if channel_ids:
        ids = list(channel_ids)
        ids = ids[:900]   # SQLite param cap guard
        with store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT video_id, title, views, channel, url
                FROM videos
                WHERE channel_id IN ({",".join("?" for _ in ids)})
                ORDER BY views DESC
                LIMIT ?
                """,
                ids + [int(limit)],
            ).fetchall()
    else:
        rows = store.videos(limit=limit)
    return [
        TitlePattern(
            title=row["title"] or "",
            views=int(row["views"] or 0),
            channel=row["channel"] or "",
            url=row["url"] or f"https://www.youtube.com/watch?v={row['video_id']}",
        )
        for row in rows
        if row["title"]
    ]


def _dedupe_patterns(patterns: list[TitlePattern]) -> list[TitlePattern]:
    seen = set()
    out: list[TitlePattern] = []
    for pattern in patterns:
        key = (pattern.url or pattern.title).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(pattern)
    return sorted(out, key=lambda p: p.views, reverse=True)


def _channel_views(store: YouTubeStore, channel_id: str) -> list[int]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT views FROM videos WHERE channel_id=? AND views > 0",
            (channel_id,),
        ).fetchall()
    return [int(r["views"] or 0) for r in rows]
