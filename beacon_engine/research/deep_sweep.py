"""Deep sweep — carpet-bomb a niche with ~100 search angles in one command.

A normal Smart Scrape runs a handful of queries; good for a first look, too thin
to map a whole niche. The deep sweep asks the LLM for up to N diverse viewer
searches (sub-topics, angles, phrasings), falls back to deterministic templates,
then crawls them all with a light per-query budget (free providers — no AI
tokens, just time). Every video/channel lands in the shared index, so competitor
ranking, trending, and the niche report all get massively more raw material.
"""

from __future__ import annotations

import logging
from typing import Callable

from beacon_engine.config import Tier
from beacon_engine.json_utils import as_list, extract_json
from beacon_engine.research.crawler import YouTubeCrawler
from beacon_engine.research.smart_scraper import FREE_PROVIDER_NAMES, _niche_channel_hits
from beacon_engine.research.store import YouTubeStore

Progress = Callable[[str], None]
log = logging.getLogger("beacon_engine.deep_sweep")

# Deterministic fallback: niche x these modifiers ≈ broad coverage with zero LLM.
_MODIFIERS = [
    "exposed", "to avoid", "ranked worst to best", "documentary", "investigation",
    "worst brands", "best brands", "secrets", "scandal", "honest review",
    "comparison", "tested", "truth about", "hidden dangers", "fake vs real",
    "buying guide", "mistakes", "red flags", "banned", "recalled",
    "cheap vs expensive", "ingredients explained", "what happened to",
    "rise and fall", "case study", "explained", "deep dive", "top 10",
    "you should never buy", "warning", "before you buy", "industry secrets",
]

_QUERY_SYSTEM = """Generate diverse YouTube SEARCH QUERIES to fully map one niche.
Cover every sub-topic, product category, audience angle, and phrasing a real viewer
would type — exposés, rankings, comparisons, guides, scandals, brand names, formats.
4-8 words each, all different angles, no duplicates.
Return ONLY JSON: {"queries": ["...", ...]}"""


def generate_sweep_queries(llm, niche: str, *, count: int = 100) -> list[str]:
    queries: list[str] = []
    if llm is not None:
        try:
            data = extract_json(llm.complete(
                task="analysis",
                tier=Tier.SONNET, instructions=_QUERY_SYSTEM,
                prompt=f"NICHE: {niche}\nGenerate {count} queries.",
                thinking=False, effort="medium", max_tokens=4000).text)
            if isinstance(data, dict):
                queries = [str(q).strip() for q in as_list(data.get("queries"))]
        except Exception:  # noqa: BLE001 — fall through to templates
            queries = []
    if len(queries) < count:
        queries += [f"{niche} {m}" for m in _MODIFIERS]
    seen, out = set(), []
    for q in queries:
        k = q.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out[:count]


def deep_sweep(
    store: YouTubeStore, niche: str, *, llm=None, queries: int = 100, niche_id: int = 0,
    track: bool = True,
    seed_limit: int = 25, max_channels: int = 2, videos_per_channel: int = 15,
    on_step: Progress = lambda _m: None,
) -> dict:
    """Run the sweep. Light per-query budget keeps 100 queries tractable; the
    volume comes from breadth, not depth. Returns totals + the top channels by
    niche relevance (how many sweep queries each ranked for)."""
    qs = generate_sweep_queries(llm, niche, count=max(5, int(queries)))
    crawler = YouTubeCrawler(store)
    videos = channels = errors = 0
    for i, q in enumerate(qs, 1):
        # Every step lands in the Logs page too — a long sweep must narrate itself.
        log.info("deep sweep %d/%d: %s (videos so far: %d)", i, len(qs), q, videos)
        on_step(f"Deep sweep {i}/{len(qs)}: {q}")
        try:
            r = crawler.crawl_niche(
                q, seed_limit=seed_limit, max_channels=max_channels,
                videos_per_channel=videos_per_channel, enrich=False,
                providers=FREE_PROVIDER_NAMES, faceless_only=True, on_step=on_step)
            videos += r.discovered
            channels += r.channels
        except Exception:  # noqa: BLE001 — one dead query never stops the sweep
            errors += 1
    hits = _niche_channel_hits(store, qs)
    top = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)[:40]
    names: list[dict] = []
    if top:
        ph = ",".join("?" for _ in top)
        with store.connect() as conn:
            titles = {r["channel_id"]: r["title"] for r in conn.execute(
                f"SELECT channel_id, title FROM channels WHERE channel_id IN ({ph})",
                [cid for cid, _ in top])}
        names = [{"channel_id": cid, "title": titles.get(cid, cid), "niche_hits": h}
                 for cid, h in top]
    # Curation write (niche_channels) happens ONLY when track=True — used by Signal
    # / app flows. The session-scoped chat AGENT passes track=False: it never edits
    # the Signal database; it reads `top_channels` and fills the SESSION grid instead.
    tracked = 0
    if track and niche_id:
        try:
            from beacon_engine.intelligence.workspaces import NicheWorkspaceService
            tracked = NicheWorkspaceService(store).add_channels(
                int(niche_id), [cid for cid, h in top if h >= 2],
                role="competitor", source="deep-sweep")
            on_step(f"Auto-tracked {tracked} competitors into the niche workspace.")
        except Exception:  # noqa: BLE001 — tracking is a bonus, never fails the sweep
            pass
    log.info("deep sweep DONE: %d queries, %d videos, %d channels discovered, %d tracked",
             len(qs), videos, len(hits), tracked)
    return {"queries_run": len(qs), "videos_found": videos,
            "channel_expansions": channels, "errors": errors,
            "channels_discovered": len(hits), "top_channels": names,
            "niche_id": niche_id, "auto_tracked": tracked}
