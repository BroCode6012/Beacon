"""Structured intelligence tools shared by chat and the local API."""

from __future__ import annotations

import json as _json
import time

from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.patterns import PatternIntelligenceService
from beacon_engine.intelligence.recommendations import IntelligenceRecommendationService
from beacon_engine.intelligence.workspaces import NicheWorkspaceService
from beacon_engine.research.store import YouTubeStore


def _boolean_arg(value, *, name: str) -> bool:
    """Coerce JSON-friendly booleans without treating every string as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean.")


# Tools that answer questions about the WHOLE channel universe rather than about this
# session's own scraped data. Pointed at the session DB they return almost nothing —
# a session holds a few hundred channels, the shared index holds hundreds of thousands
# — which is why the assistant used to answer "found 0 channels" to questions the index
# could answer instantly. They are all read-only; every write tool stays on the session
# database, so the ownership boundary in tests/test_db_ownership.py is untouched.
#
# Deliberately NOT here: anything scoped to this session or this workspace —
# list_competitors, get_competitor_growth, the labs, the briefs, the niche feeds and
# the status tools. Those SHOULD see only the session's own state.
GLOBAL_DISCOVERY_TOOLS = frozenset({
    "db_stats", "count_channels", "find_channels", "find_rising_videos",
    "recent_channels", "list_topics", "get_channel_details", "get_channel_avatars",
    "compare_channels", "discovery_yield",
})

# ...but a niche_id names a workspace in THIS session, and the shared index has its own
# unrelated niche table. Resolving a session niche id against it returns another
# workspace's channels — or nothing — with no error to give the mistake away. So any call
# carrying a niche_id stays on the session database, even for a tool that is otherwise
# universe-wide (find_channels/count_channels/recent_channels take it as a filter).
#
# Tools whose niche_id is REQUIRED are never global: discover_channels, get_patterns,
# get_niche_outliers, get_velocity_leaders and get_recent_uploads are all questions about
# one tracked workspace, not about the channel universe.
NICHE_SCOPED_ARG = "niche_id"


class IntelligenceAnalystTools:
    def __init__(self, store: YouTubeStore | None = None,
                 discovery_store: YouTubeStore | None = None) -> None:
        self.store = store or YouTubeStore()
        # Optional read-only handle on the big shared index. When present, the tools in
        # GLOBAL_DISCOVERY_TOOLS are served from it instead of the session database.
        self.discovery_store = discovery_store
        self._discovery = None
        self.workspaces = NicheWorkspaceService(self.store)
        self.metrics = ChannelMetricsService(self.store)
        self.recommendations = IntelligenceRecommendationService(self.store)
        self.patterns = PatternIntelligenceService(self.store)

    def routes_to_discovery(self, name: str, args: dict | None = None) -> bool:
        """Should this call be served from the shared index instead of the session DB?

        Only universe-wide READS, and only when they aren't scoped to a niche workspace.
        """
        if name not in GLOBAL_DISCOVERY_TOOLS or self.discovery is None:
            return False
        return (args or {}).get(NICHE_SCOPED_ARG) in (None, "", 0)

    @property
    def discovery(self):
        """A sibling bound to the shared index, built on first use.

        The sibling gets no discovery_store of its own, so delegation cannot recurse.
        """
        if self.discovery_store is None:
            return None
        if self._discovery is None:
            self._discovery = IntelligenceAnalystTools(store=self.discovery_store)
        return self._discovery


    # -- whole-database finder (NexLev-style) ----------------------------------
    def find_channels(self, faceless_only: bool = True, max_subs: int | None = None,
                      min_subs: int | None = None,
                      min_avg_views: int | None = None, max_avg_views: int | None = None,
                      max_days_since_start: int | None = None,
                      min_days_since_start: int | None = None,
                      min_uploads: int | None = None, max_uploads: int | None = None,
                      min_views_per_sub: float | None = None,
                      min_outlier_score: float | None = None,
                      category_contains: str | None = None, category: str | None = None,
                      topic: str | None = None,
                      video_format: str | None = None, niche_id: int | None = None,
                      replicable_only: bool = False,
                      days: int | None = None, added_within_days: int | None = None,
                      sort: str = "outlier_score", descending: bool = True,
                      limit: int = 25, **_) -> list[dict]:
        # Every arg composes into ONE query so e.g. niche + low-subs + high-views is
        # an accurate combined filter (not a post-filter). All args are optional.
        # `days`/`added_within_days` = discovery-recency window (channels ADDED to the
        # index within the last N days); optional/default-None so existing callers are
        # unaffected. Lets a "view these channels" click-through match a count_channels
        # count exactly. `days` is the friendly alias (count_channels uses it too).
        window = days if days is not None else added_within_days
        rows = self.store.channel_metrics(
            faceless_only=bool(faceless_only),
            min_subs=min_subs, max_subs=max_subs,
            min_avg_views=min_avg_views, max_avg_views=max_avg_views,
            max_days_since_start=max_days_since_start,
            min_days_since_start=min_days_since_start,
            min_uploads=min_uploads, max_uploads=max_uploads,
            min_views_per_sub=min_views_per_sub,
            min_outlier_score=min_outlier_score,
            category_contains=category_contains, category=category,
            topic=(topic or None),
            video_format=video_format,
            replicable_only=bool(replicable_only),
            niche_id=(int(niche_id) if niche_id is not None else None),
            added_within_days=(int(window) if window is not None else None),
            sort=sort, descending=bool(descending), limit=int(limit),
        )
        keep = ("channel_id", "title", "subscribers", "avg_views", "video_count",
                "days_since_start", "outlier_score", "opportunity_score",
                "category", "topic", "faceless", "est_revenue", "avatar_url",
                "views_per_sub")
        return [{k: r.get(k) for k in keep} for r in rows]

    def count_channels(self, faceless_only: bool = False, min_subs: int | None = None,
                       max_subs: int | None = None, min_avg_views: int | None = None,
                       max_avg_views: int | None = None, min_outlier_score: float | None = None,
                       max_days_since_start: int | None = None, topic: str | None = None,
                       niche_id: int | None = None, video_format: str | None = None,
                       category: str | None = None, category_contains: str | None = None,
                       days: int | None = None,
                       replicable_only: bool = False, **_) -> dict:
        """Exact COUNT of channels matching filters (same filters as find_channels),
        in one cheap query — use for 'how many …' instead of listing + tallying.
        `category_contains` = discovering-search substring; `days` = only channels ADDED to the
        index within the last N days (discovery recency). Both were previously dropped."""
        rows = self.store.channel_metrics(
            count_only=True, faceless_only=bool(faceless_only),
            min_subs=min_subs, max_subs=max_subs,
            min_avg_views=min_avg_views, max_avg_views=max_avg_views,
            min_outlier_score=min_outlier_score,
            max_days_since_start=max_days_since_start,
            topic=(topic or None), category=category,
            category_contains=(category_contains or None), video_format=video_format,
            replicable_only=bool(replicable_only),
            niche_id=(int(niche_id) if niche_id is not None else None),
            added_within_days=(int(days) if days is not None else None))
        return {"count": int(rows[0]["count"]) if rows else 0}

    def list_topics(self, **_) -> list[dict]:
        """[{topic, count}] over the whole index (excluded channels hidden), biggest
        first — powers the Channels-tab topic dropdown. Every channel has one topic."""
        return self.store.topic_counts()

    # -- agent write tools: edit / organize / clean the index (all reversible) ------
    def exclude_channels(self, channel_ids: list[str] | None = None,
                         reason: str = "agent", **_) -> dict:
        """Hide specific channels from every view AND from scraping. Reversible."""
        n = self.store.exclude_channels(channel_ids or [], reason=str(reason))
        return {"excluded": n, "reason": reason}

    def unexclude_channels(self, channel_ids: list[str] | None = None, **_) -> dict:
        """Bring previously-excluded channels back."""
        return {"unexcluded": self.store.unexclude_channels(channel_ids or [])}

    def exclude_by_filter(self, reason: str = "agent", faceless_only: bool = False,
                         min_subs: int | None = None, max_subs: int | None = None,
                         min_avg_views: int | None = None, max_avg_views: int | None = None,
                         topic: str | None = None, category: str | None = None,
                         video_format: str | None = None, **_) -> dict:
        """Exclude ALL channels matching a filter (bulk cleanup). Reversible. Refuses
        if no real filter is given (won't wipe the whole index)."""
        if not any([faceless_only, min_subs, max_subs, min_avg_views, max_avg_views,
                    topic, category, video_format]):
            return {"error": "refusing to exclude with no filter (would hit everything)"}
        rows = self.store.channel_metrics(
            faceless_only=bool(faceless_only), min_subs=min_subs, max_subs=max_subs,
            min_avg_views=min_avg_views, max_avg_views=max_avg_views,
            topic=(topic or None), category=category, video_format=video_format,
            limit=500000)
        ids = [r["channel_id"] for r in rows]
        n = self.store.exclude_channels(ids, reason=str(reason)) if ids else 0
        return {"matched": len(ids), "excluded": n, "reason": reason,
                "undo": "unexclude_channels or store.unexclude_channels"}

    def run_junk_filter(self, **_) -> dict:
        """Auto-exclude music (by name AND YouTube category) / meme / big-brand
        channels — the same cleaner the scraper runs each cycle. Reversible."""
        from beacon_engine.research.junk_filter import exclude_junk
        return exclude_junk(self.store)

    def set_channel_topic(self, channel_ids: list[str] | None = None,
                         topic: str = "", **_) -> dict:
        """Override the clean topic niche for specific channels."""
        return {"updated": self.store.set_channel_topic(channel_ids or [], str(topic)),
                "topic": topic}

    def reclassify_topics(self, only_missing: bool = True, **_) -> dict:
        """Re-run the topic classifier. only_missing=True labels just new channels;
        False re-labels the whole index (use after the classifier improves)."""
        from beacon_engine.research import topic_classifier
        res = topic_classifier.backfill(self.store, only_missing=bool(only_missing))
        return {"classified": res.get("classified", 0)}

    def create_niche(self, name: str = "", description: str = "", **_) -> dict:
        """Create a new niche workspace; returns its niche_id (use add_channel_to_niche)."""
        nid = self.workspaces.create_workspace(str(name), str(description or ""))
        return {"niche_id": int(nid), "name": name}

    def recent_channels(self, days: int | None = None, faceless_only: bool = False,
                        min_subs: int | None = None, max_subs: int | None = None,
                        min_avg_views: int | None = None, max_avg_views: int | None = None,
                        min_outlier_score: float | None = None,
                        max_days_since_start: int | None = None,
                         min_views_per_sub: float | None = None,
                         topic: str | None = None, video_format: str | None = None,
                         category_contains: str | None = None,
                         replicable_only: bool = False,
                         niche_id: int | None = None, limit: int = 25, **_) -> list[dict]:
        """Channels most recently ADDED to the index (by discovery time first_seen_at),
        newest first — the live 'real-time channels' feed. Optional `days` limits to
        channels added within the last N days; other filters compose like find_channels."""
        rows = self.store.channel_metrics(
            faceless_only=bool(faceless_only),
            min_subs=min_subs, max_subs=max_subs,
            min_avg_views=min_avg_views, max_avg_views=max_avg_views,
            min_outlier_score=min_outlier_score,
            max_days_since_start=max_days_since_start,
            min_views_per_sub=min_views_per_sub,
            topic=(topic or None), video_format=video_format,
            category_contains=category_contains,
            replicable_only=bool(replicable_only),
            niche_id=(int(niche_id) if niche_id is not None else None),
            added_within_days=(int(days) if days is not None else None),
            sort="added_at", descending=True, limit=int(limit),
        )
        keep = ("channel_id", "title", "subscribers", "avg_views", "video_count",
                "days_since_start", "outlier_score", "opportunity_score",
                "category", "topic", "faceless", "est_revenue", "avatar_url",
                "views_per_sub", "added_at")
        return [{k: r.get(k) for k in keep} for r in rows]

    def get_channel_avatars(self, channel_ids: list[str] | None = None, **_) -> dict:
        """Batch: map channel_id -> avatar image URL for the given channels."""
        return self.store.channel_avatars(channel_ids or [])

    def find_rising_videos(self, days: int = 7, limit: int = 15,
                           faceless_only: bool = True,
                           min_views_per_day: int = 1000, **_) -> list[dict]:
        return self.store.rising_videos(
            days=int(days), limit=int(limit), faceless_only=bool(faceless_only),
            min_views_per_day=int(min_views_per_day))

    def db_stats(self, **_) -> dict:
        summary = self.store.summary()
        with self.store.connect() as conn:
            summary["faceless_verified"] = int(conn.execute(
                "SELECT COUNT(*) FROM channel_vlm_check WHERE verdict='faceless'"
            ).fetchone()[0])
            summary["excluded_kids"] = int(conn.execute(
                "SELECT COUNT(*) FROM channel_exclusions").fetchone()[0])
        return summary

    def get_channel_details(self, channel_id: str, video_limit: int = 15, **_) -> dict:
        with self.store.connect() as conn:
            ch = conn.execute(
                "SELECT * FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
            if not ch:
                raise KeyError(f"Channel not in index: {channel_id}")
            videos = [dict(r) for r in conn.execute(
                """
                SELECT video_id, title, views, published_at_normalized, duration_seconds
                FROM videos WHERE channel_id=? ORDER BY views DESC LIMIT ?
                """, (channel_id, int(video_limit))).fetchall()]
            verdict = conn.execute(
                """
                SELECT COALESCE(v.verdict, f.verdict, 'unchecked')
                FROM channels c
                LEFT JOIN channel_vlm_check v ON v.channel_id=c.channel_id
                LEFT JOIN channel_face_check f ON f.channel_id=c.channel_id
                WHERE c.channel_id=?
                """, (channel_id,)).fetchone()[0]
        out = dict(ch)
        out.pop("raw_json", None)
        out["face_verdict"] = verdict
        out["top_videos"] = videos
        return out

    # -- scraper control --------------------------------------------------------
    def crawl_niche(self, query: str, seed_limit: int = 20, max_channels: int = 4,
                    videos_per_channel: int = 24, **_) -> dict:
        from beacon_engine.research.crawler import YouTubeCrawler
        from beacon_engine.research.radar import FREE_PROVIDER_NAMES
        crawler = YouTubeCrawler(self.store)
        report = crawler.crawl_niche(
            query, seed_limit=int(seed_limit), max_channels=int(max_channels),
            videos_per_channel=int(videos_per_channel), enrich=False,
            providers=FREE_PROVIDER_NAMES,
        )
        return {"query": query, "discovered": report.discovered,
                "channels": report.channels, "stored": report.stored,
                "errors": report.errors[:5]}

    def expand_network(self, scan_limit: int = 30000, **_) -> dict:
        from beacon_engine.research.network_expand import harvest_channel_links
        links, uniq, seeded = harvest_channel_links(
            self.store, scan_limit=int(scan_limit))
        return {"links_found": links, "new_channels": uniq, "seeded": seeded}

    def radar_status(self, **_) -> dict:
        import json as _json
        from pathlib import Path
        out: dict = {}
        for key, path in (("radar", "storage/radar_status.json"),
                          ("monitor", "storage/monitor_status.json")):
            try:
                out[key] = _json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                out[key] = {"state": "unavailable"}
        return out

    def edge_sync_status(self, **_) -> dict:
        """Cloud-worker inbox and last-sync state; never returns credentials."""
        from beacon_engine.edge.sync import edge_sync_status

        return edge_sync_status()

    def discovery_yield(self, hours: int = 24, limit: int = 50,
                        minimum_searches: int = 3, **_) -> dict:
        """Actual committed search yield, with history/sample coverage shown."""
        from beacon_engine.edge.discovery_yield import discovery_yield_report
        return discovery_yield_report(self.store, hours=hours, limit=limit,
                                      minimum_searches=minimum_searches)

    def import_edge_inbox(self, max_bundles: int = 20, **_) -> dict:
        """Verify and atomically import a bounded cloud inbox slice."""
        from beacon_engine.edge.sync import import_edge_inbox

        return import_edge_inbox(
            self.store.path,
            max_bundles=int(max_bundles or 20),
        )

    # -- production lab (persistent channel memory) ------------------------------









    def list_niches(self, **_) -> list[dict]:
        return self.workspaces.list_workspaces()

    def get_niche_channels(self, niche_id: int, **_) -> list[dict]:
        return self.workspaces.workspace_channels(int(niche_id))

    def query_niche_feed(
        self,
        niche_id: int,
        days: int | None = 30,
        format_mode: str = "all",
        sort: str = "newest",
        limit: int = 30,
        role: str = "all",
        **_,
    ) -> list[dict]:
        return self.workspaces.feed(
            int(niche_id),
            days=days,
            format_mode=format_mode,
            sort=sort,
            limit=limit,
            roles=None if role == "all" else {role},
        )

    def compare_channels(self, channel_ids: list[str], **_) -> list[dict]:
        return [
            metrics
            for channel_id in channel_ids[:20]
            if (metrics := self.metrics.channel_metrics(channel_id)) is not None
        ]

    def get_niche_outliers(
        self,
        niche_id: int,
        days: int | None = 365,
        limit: int = 25,
        **_,
    ) -> list[dict]:
        return self.workspaces.feed(
            int(niche_id),
            days=days,
            sort="outlier",
            limit=limit,
            roles={"competitor"},
        )

    def get_velocity_leaders(
        self,
        niche_id: int,
        days: int | None = 30,
        limit: int = 25,
        **_,
    ) -> list[dict]:
        return self.workspaces.feed(
            int(niche_id),
            days=days,
            sort="velocity",
            limit=limit,
            roles={"competitor"},
        )

    def get_recent_uploads(
        self,
        niche_id: int,
        days: int = 7,
        limit: int = 25,
        **_,
    ) -> list[dict]:
        return self.workspaces.feed(
            int(niche_id),
            days=days,
            sort="newest",
            limit=limit,
            roles={"competitor"},
        )

    def get_patterns(self, niche_id: int, days: int | None = 365, **_) -> dict:
        return self.patterns.extract(int(niche_id), days=days)

    def get_morning_brief(self, niche_id: int, **_) -> dict:
        # READ-ONLY: skip alert DETECTION (that writes the alerts table) — the radar
        # computes alerts on its write-path; here we just read the latest.
        return self.recommendations.morning_brief(int(niche_id), detect=False)

    def list_niche_signals(
        self,
        niche_id: int | None = None,
        unread_only: bool = True,
        days: int | None = 30,
        limit: int = 100,
        **_,
    ) -> dict:
        """Deduplicated niche notifications plus per-workspace signal counts."""
        return self.recommendations.list_niche_signals(
            niche_id=(int(niche_id) if niche_id is not None else None),
            unread_only=_boolean_arg(unread_only, name="unread_only"),
            days=(int(days) if days is not None else None),
            limit=int(limit),
        )

    def mark_niche_alert_read(
        self,
        alert_ids: list[int] | None = None,
        niche_id: int | None = None,
        read: bool = True,
        **_,
    ) -> dict:
        """Acknowledge exact alerts or every alert in one niche; reversible."""
        clean_read = _boolean_arg(read, name="read")
        changed = self.recommendations.mark_niche_alerts(
            alert_ids=alert_ids,
            niche_id=(int(niche_id) if niche_id is not None else None),
            read=clean_read,
        )
        return {"changed": changed, "read": clean_read}

    def set_niche_autonomous(
        self,
        niche_id: int,
        enabled: bool = True,
        **_,
    ) -> dict:
        """Pause or resume automatic snapshots, alerts, and upkeep for one niche."""
        clean_enabled = _boolean_arg(enabled, name="enabled")
        self.workspaces.set_autonomous(int(niche_id), clean_enabled)
        return {
            "niche_id": int(niche_id),
            "autonomous": clean_enabled,
        }

    def publish_niche_signal(
        self,
        niche_id: int,
        title: str,
        message: str,
        signal_type: str = "hermes_brief",
        severity: str = "info",
        subject: str = "",
        source: str = "hermes",
        evidence: dict | None = None,
        **_,
    ) -> dict:
        """Publish one validated Hermes/operator briefing into niche notifications."""
        return self.recommendations.publish_niche_signal(
            niche_id=int(niche_id),
            title=title,
            message=message,
            signal_type=signal_type,
            severity=severity,
            subject=subject,
            source=source,
            evidence=evidence,
        )

    def discover_channels(self, niche_id: int, limit: int = 20, **_) -> list[dict]:
        return self.recommendations.discovery_candidates(int(niche_id), limit=limit)

    def add_channel_to_niche(
        self,
        niche_id: int,
        channel_id: str,
        role: str = "competitor",
        **_,
    ) -> dict:
        self.workspaces.add_channel(int(niche_id), channel_id, role=role, source="analyst")
        return {"ok": True, "niche_id": int(niche_id), "channel_id": channel_id}

    def seed_channels(self, channel_ids: list[str] | None = None,
                      source: str = "manual", enrich: bool = True, **_) -> dict:
        """Put channel IDs into the index and, by default, fill their numbers NOW.

        Discovery normally introduces channels on its own schedule. This is the
        hand-picked path: an operator found a channel themselves and the crawler has
        not reached it yet. Seeding alone writes only a bare row, so `enrich` pulls
        live stats and recent uploads immediately — that is the difference between a
        freshly saved channel showing real numbers and showing blanks until the next
        radar cycle. Already-indexed IDs are kept (INSERT OR IGNORE) and simply
        refreshed, so re-saving the same link is safe.
        """
        ids = [c.strip() for c in (channel_ids or [])
               if isinstance(c, str) and c.strip().startswith("UC")]
        if not ids:
            return {"error": "no valid channel ids (expected UC…)"}
        # The radar worker and the cloud importer write this database too, and a bulk
        # import transaction routinely outlasts the 30s busy_timeout. Short retries, not
        # the enrichment module's 20s ones: this runs while an operator waits on a
        # button, so a few seconds of patience is right and a minute is not.
        from beacon_engine.research.enrichment import _retry_locked
        seeded: dict = {}
        try:
            _retry_locked(
                lambda: seeded.__setitem__(
                    "added", self.store.add_channel_seeds(ids, source=str(source or "manual"))),
                tries=4, wait=2.5)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"index busy, could not save right now: "
                             f"{type(exc).__name__}: {str(exc)[:120]}"}
        out: dict = {"requested": len(ids), "added": int(seeded.get("added") or 0)}
        if not enrich:
            return out
        from beacon_engine.research.competitor_tracker import refresh_competitor_stats
        # Reported rather than raised: the rows are already seeded, so a quota or
        # network failure here must degrade to "saved, numbers pending" (the radar
        # fills them in later) instead of losing the operator's find.
        try:
            out["refreshed"] = refresh_competitor_stats(self.store, ids)
        except Exception as exc:  # noqa: BLE001
            out["stats_error"] = f"{type(exc).__name__}: {exc}"
        # Uploads come from the CATALOGUING CRAWLER, not refresh_competitor_uploads.
        # That function's yt-dlp provider returns every video with channel_id="" and
        # views=0, so its rows land in the index orphaned — detached from the channel they
        # belong to. It still reports them as "new", which is why the damage is invisible:
        # the card keeps showing an avg_views figure (silently derived from the channel's
        # lifetime views/uploads) while the per-video evidence behind it does not exist.
        # The crawler stamps the channel id, so the analysis view has something real to read.
        try:
            from beacon_engine.research.enrichment import EnrichmentEngine
            engine = EnrichmentEngine(self.store)
            cat = engine.catalog_channels(channel_ids=ids, videos_per_channel=30)
            out["catalogued"] = int(getattr(cat, "videos_stored", 0) or 0)
            if cat.errors:
                out["catalog_errors"] = list(cat.errors)[:3]
            if out["catalogued"]:
                # Cataloguing stores ids without stats; ranking "best uploads" needs views.
                stats = engine.backfill_video_stats(channel_ids=ids, limit=200)
                out["video_stats_filled"] = int(getattr(stats, "updated", 0) or 0)
        except Exception as exc:  # noqa: BLE001 — the save still stands without videos
            out["catalog_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def enqueue_channel_save(self, link: str, **_) -> dict:
        from beacon_engine.research.channel_save_jobs import ChannelSaveQueue
        return ChannelSaveQueue(self.store).enqueue(link)

    def list_channel_save_requests(self, limit: int = 50, **_) -> list[dict]:
        from beacon_engine.research.channel_save_jobs import ChannelSaveQueue
        requests = ChannelSaveQueue(self.store).list(limit)
        revision = _json.dumps(requests, sort_keys=True, default=str)
        if revision != getattr(self, "_save_requests_revision", None):
            # A separate durable worker committed new membership/evidence.
            # Invalidate derived Finder reads when its persisted state changes.
            _CACHE.clear()
            self._save_requests_revision = revision
        return requests

    def retry_channel_save(self, request_id: int, **_) -> dict:
        from beacon_engine.research.channel_save_jobs import ChannelSaveQueue
        return ChannelSaveQueue(self.store).retry(request_id)

    def queue_saved_channel_refresh(self, niche_id: int, **_) -> dict:
        from beacon_engine.research.channel_save_jobs import ChannelSaveQueue
        return ChannelSaveQueue(self.store).refresh_saved(niche_id)

    def saved_channel_tracking(self, niche_id: int, **_) -> dict:
        from beacon_engine.research.saved_tracking import saved_channel_tracking
        return saved_channel_tracking(self.store, niche_id)

    def generate_niche_ideas(
        self,
        niche_id: int,
        mode: str = "balanced",
        count: int = 10,
        **_,
    ) -> list[dict]:
        return self.patterns.generate_ideas(
            int(niche_id),
            mode=mode,
            count=count,
            llm=None,
        )

    def list_competitors(self, niche_id: int, **_) -> list[dict]:
        """Tracked competitors for a niche, each with growth-over-days stats."""
        rows = [r for r in self.workspaces.workspace_channels(int(niche_id))
                if r.get("role") == "competitor"]
        growth = self.metrics.growth_for_channels([r["channel_id"] for r in rows])
        for r in rows:
            r["growth"] = growth.get(r["channel_id"], {})
        return rows

    def get_competitor_growth(self, channel_id: str, **_) -> dict:
        """Growth of one tracked channel: subs/views/uploads gained over the window."""
        out = dict(self.metrics.channel_growth(channel_id))
        out["history"] = self.metrics.channel_history(channel_id)
        return out

    def track_competitor(self, niche_id: int, channel: str,
                         role: str = "competitor", **_) -> dict:
        """Add a competitor (by id/handle/url) to a niche's tracked list."""
        resolved = self.workspaces.resolve_channel(channel)
        if not resolved:
            return {"ok": False, "error": f"Channel not in the index: {channel}"}
        self.workspaces.add_channel(
            int(niche_id), resolved["channel_id"], role=role, source="analyst")
        return {"ok": True, "niche_id": int(niche_id),
                "channel_id": resolved["channel_id"]}

    def untrack_competitor(self, niche_id: int, channel: str, **_) -> dict:
        """Remove a competitor from a niche's tracked list (keeps its history)."""
        resolved = self.workspaces.resolve_channel(channel)
        cid = resolved["channel_id"] if resolved else channel
        self.workspaces.remove_channel(int(niche_id), cid)
        return {"ok": True, "niche_id": int(niche_id), "channel_id": cid}

    def sync_competitors(self, niche_id: int, drop_inactive_days: int = 0, **_) -> dict:
        """Refresh tracked competitors' live stats + uploads now, snapshot growth,
        and optionally auto-drop the dead ones (no upload in N days)."""
        from beacon_engine.research.competitor_tracker import sync_competitors as _sync
        return _sync(self.store, int(niche_id), refresh=True, snapshot=True,
                     drop_inactive_days=int(drop_inactive_days or 0))

    def inspect_channel_visuals(self, channel: str, limit: int = 12, **_) -> dict:
        """LOOK at a channel: vision-read its real thumbnails (faceless? style?
        format? beatable?). Use whenever the user gives you a channel."""
        from beacon_engine.research.visual_inspect import inspect_channel_visuals as _v
        return _v(self.store, channel, limit=int(limit))

    def deep_sweep(self, niche: str, queries: int = 100, niche_id: int = 0, **_) -> dict:
        """Carpet-bomb a niche with up to N search angles (free scraping) so the
        index holds massive data: all competitors, formats, and outliers."""
        from beacon_engine.research.deep_sweep import deep_sweep as _sweep
        llm = None
        try:
            from beacon_engine.llm import LLM
            llm = LLM()
        except Exception:  # noqa: BLE001 — sweep still works with template queries
            pass
        return _sweep(self.store, niche, llm=llm, queries=int(queries or 100), niche_id=int(niche_id or 0))

    def dispatch(self, name: str, args: dict | None = None):
        fn = getattr(self, name, None)
        if name not in TOOL_SCHEMAS or not callable(fn):
            raise KeyError(f"Unknown intelligence tool: {name}")
        if self.routes_to_discovery(name, args):
            return self.discovery.dispatch(name, args)
        # Heavy read-only analyses get a short TTL cache: a morning brief takes
        # ~10s to build and doesn't change minute-to-minute — the assistant
        # should answer instantly on the second ask.
        ttl = _CACHE_TTL.get(name)
        key = None
        if ttl:
            # Keyed by database too: the same tool and args against the session DB and
            # against the shared index are different answers, and must not share a slot.
            # getattr, because callers may build this object without a real store.
            store = getattr(self, "store", None)
            db = getattr(store, "cache_identity", getattr(store, "path", ""))
            key = (f"{db}|{name}"
                   + _json.dumps(args or {}, sort_keys=True, default=str))
            hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        result = _slim(fn(**(args or {})))
        schema = TOOL_SCHEMAS[name]
        if schema.get("write") and not (
                isinstance(result, dict) and result.get("error")):
            # Workspace/index mutations can invalidate cached list_niches, feeds,
            # briefs, patterns, and other derived reads. Clear them only after a
            # successful write so all Finder clients (Signal + Beacon)
            # observe the new state on their next call.
            _CACHE.clear()
        elif key is not None:
            _CACHE[key] = (time.time(), result)
            if len(_CACHE) > 200:   # bounded — drop the oldest half
                for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:100]:
                    _CACHE.pop(k, None)
        return result


# tool name -> cache TTL seconds (read-only analyses only; writes never cached)
_CACHE_TTL = {
    "get_morning_brief": 600,
    "list_niche_signals": 60,
    "get_patterns": 600,
    "list_niches": 300,
    "get_niche_outliers": 300,
    "get_velocity_leaders": 300,
    "get_recent_uploads": 180,
    "query_niche_feed": 180,
    "discover_channels": 600,
    "find_rising_videos": 600,
    "get_daily_brief": 900,
}
_CACHE: dict[str, tuple[float, object]] = {}


# Max list rows a tool result may carry over the API. Raised from 200 so paging
# UIs (Signal's "Show more") can browse deep into large result sets — rows are
# already blob-stripped below, so this is just a backstop against a pathological
# multi-thousand-row transfer; past a few thousand the real limiter is client rendering.
_MAX_LIST = 5000


def _slim(obj, _depth: int = 0):
    """Trim tool results for agent/API consumption: drop raw payload blobs, cap
    runaway strings and lists. (One feed row carried raw_json — a 5-item call
    returned 732KB before this.) The app's own UI calls services directly and
    is unaffected."""
    if _depth > 6:
        return obj
    if isinstance(obj, list):
        return [_slim(x, _depth + 1) for x in obj[:_MAX_LIST]]
    if isinstance(obj, dict):
        return {k: _slim(v, _depth + 1) for k, v in obj.items()
                if k not in ("raw_json", "counts_json")}
    if isinstance(obj, str) and len(obj) > 4000:
        return obj[:4000] + "…(trimmed)"
    return obj


TOOL_SCHEMAS = {
    "saved_channel_tracking": {
        "description": "Verified last-check times, measured ~24h subscriber/view/upload changes and bounded video pickup signals for a saved workspace. Args: niche_id. Missing or stale evidence stays unavailable.",
        "write": False,
    },
    "queue_saved_channel_refresh": {
        "description": "Queue background checks for up to 500 saved channels. Args: niche_id.",
        "write": True,
    },
    "enqueue_channel_save": {
        "description": "Durably queue one YouTube channel/video link for saving and background analysis. Args: link.",
        "write": True,
    },
    "list_channel_save_requests": {
        "description": "Read persisted channel save requests, including saved membership and analysis state. Args: limit? (max 100).",
        "write": False,
    },
    "retry_channel_save": {
        "description": "Retry one failed persisted channel save request. Args: request_id.",
        "write": True,
    },
    "list_niches": {"description": "List all niche workspaces.", "write": False},
    "get_niche_channels": {
        "description": "List tracked channels and metrics for a niche.",
        "write": False,
    },
    "query_niche_feed": {
        "description": "Query tracked uploads by date, format, and sort.",
        "write": False,
    },
    "compare_channels": {
        "description": "Compare reproducible metrics for channel IDs.",
        "write": False,
    },
    "get_niche_outliers": {
        "description": "Return the strongest niche video outliers.",
        "write": False,
    },
    "get_velocity_leaders": {
        "description": "Return videos with the strongest observed view velocity.",
        "write": False,
    },
    "get_recent_uploads": {
        "description": "Return recent uploads from tracked channels.",
        "write": False,
    },
    "get_patterns": {
        "description": "Return recurring title frameworks and topics.",
        "write": False,
    },
    "get_morning_brief": {
        "description": "Build the current evidence-backed intelligence brief.",
        "write": False,
    },
    "list_niche_signals": {
        "description": (
            "Deduplicated niche notifications plus workspace sizes, activity, "
            "automatic-upkeep state, and signal counts. Args: niche_id?, "
            "unread_only?, days?, limit?."
        ),
        "write": False,
    },
    "mark_niche_alert_read": {
        "description": (
            "Mark exact niche alerts, or all alerts in one niche, read/unread. "
            "Reversible. Args: alert_ids? (list), niche_id?, read?."
        ),
        "write": True,
    },
    "set_niche_autonomous": {
        "description": (
            "Pause/resume automatic snapshots, alerts, and upkeep for one niche. "
            "Args: niche_id, enabled."
        ),
        "write": True,
    },
    "publish_niche_signal": {
        "description": (
            "Publish one validated Hermes/operator briefing into a niche's "
            "notification feed. Args: niche_id, title, message, signal_type?, "
            "severity?, subject?, source?, evidence?."
        ),
        "write": True,
    },
    "discover_channels": {
        "description": "Find untracked channels related to a niche.",
        "write": False,
    },
    "add_channel_to_niche": {
        "description": "Add an indexed channel to a niche workspace.",
        "write": True,
    },
    "seed_channels": {
        "description": ("Add channel IDs the crawler has not reached yet to the index "
                        "and pull their live stats + recent uploads immediately. For "
                        "hand-picked finds. Args: channel_ids, source?, enrich?."),
        "write": True,
    },
    "list_competitors": {
        "description": ("Tracked competitors for a niche with growth-over-days "
                        "(subs/views/uploads gained). Args: niche_id."),
        "write": False,
    },
    "get_competitor_growth": {
        "description": ("Growth history of one tracked channel over days. "
                        "Args: channel_id."),
        "write": False,
    },
    "track_competitor": {
        "description": ("Add a competitor (id/handle/url) to a niche's tracked list. "
                        "Args: niche_id, channel, role?."),
        "write": True,
    },
    "untrack_competitor": {
        "description": ("Remove a competitor from a niche's tracked list. "
                        "Args: niche_id, channel."),
        "write": True,
    },
    "inspect_channel_visuals": {
        "description": ("SEE a channel: vision-analyze its real thumbnails — "
                        "faceless/on-camera, style, format, beatability. "
                        "Args: channel (id/handle/name), limit?."),
        "write": False,
    },
    "deep_sweep": {
        "description": ("MASSIVE niche mapping: crawl up to ~100 search angles AND "
                        "auto-track finds as competitors. ALWAYS pass niche_id of the user's EXISTING workspace (find it via list_niches) or finds land invisibly in a new one. Args: niche, niche_id, queries?."),
        "write": True,
    },
    "sync_competitors": {
        "description": ("Refresh tracked competitors' live stats + uploads now, "
                        "snapshot growth, optionally auto-drop dead ones. "
                        "Args: niche_id, drop_inactive_days?."),
        "write": True,
    },
    "generate_niche_ideas": {
        "description": "Generate deterministic evidence-backed niche ideas.",
        "write": True,
    },
    # -- whole-database finder --------------------------------------------------
    "find_channels": {
        "description": ("Search the full channel index with NexLev-style filters, all "
                        "optional and combinable in ONE query: faceless_only, min_subs, "
                        "max_subs, min_avg_views, max_avg_views, min_days_since_start, "
                        "max_days_since_start, min_uploads, max_uploads, min_views_per_sub, "
                        "min_outlier_score, category_contains, category, topic (clean "
                        "topic niche, see list_topics), video_format "
                        "long|short, niche_id (only channels tracked in that niche), "
                        "replicable_only (faceless repeatable opportunity gate), sort "
                        "(including opportunity_score), descending, limit."),
        "write": False,
    },
    "list_topics": {
        "description": ("Clean topic niches (History, Space, True Crime, ...) with a "
                        "channel count each, biggest first. Every channel has one; "
                        "pass a topic to find_channels(topic=...) to filter."),
        "write": False,
    },
    "count_channels": {
        "description": ("Exact COUNT of channels matching filters (same filters as "
                        "find_channels: faceless_only, min_subs, max_subs, min_avg_views, "
                        "min_outlier_score, topic, niche_id, video_format, "
                        "replicable_only). Use for 'how many ...' — one cheap query, "
                        "no listing."),
        "write": False,
    },
    "exclude_channels": {
        "description": ("Hide specific channels from every view AND scraping (reversible). "
                        "Args: channel_ids (list), reason."),
        "write": True,
    },
    "unexclude_channels": {
        "description": "Bring back previously-excluded channels. Args: channel_ids (list).",
        "write": True,
    },
    "exclude_by_filter": {
        "description": ("Bulk-exclude ALL channels matching a filter (faceless_only, "
                        "min_subs, max_subs, min_avg_views, topic, category, video_format). "
                        "Reversible; refuses with no filter. Args also: reason."),
        "write": True,
    },
    "run_junk_filter": {
        "description": ("Auto-exclude music (name + YouTube category) / meme / big-brand "
                        "channels — the cleaner the scraper runs each cycle. Reversible."),
        "write": True,
    },
    "set_channel_topic": {
        "description": ("Override the clean topic niche for channels. Args: channel_ids "
                        "(list), topic (one of list_topics, or a new label)."),
        "write": True,
    },
    "reclassify_topics": {
        "description": ("Re-run the topic classifier. Args: only_missing (true = just new "
                        "channels; false = re-label the whole index)."),
        "write": True,
    },
    "create_niche": {
        "description": ("Create a new niche workspace (returns niche_id). Args: name, "
                        "description. Then use add_channel_to_niche to fill it."),
        "write": True,
    },
    "recent_channels": {
        "description": ("Channels most recently ADDED to the index (by discovery time "
                        "first_seen_at), newest first — the live real-time feed. Args: "
                        "days? (added within N days), faceless_only?, min_subs?, max_subs?, "
                        "category_contains?, niche_id?, replicable_only?, limit?."),
        "write": False,
    },
    "find_rising_videos": {
        "description": ("Videos gaining views FASTEST right now (snapshot deltas, "
                        "views/day) — live trends, not historical hits. Args: days?, "
                        "limit?, faceless_only?, min_views_per_day?."),
        "write": False,
    },
    "get_channel_avatars": {
        "description": ("Map channel ids to their avatar image URLs (batch). "
                        "Args: channel_ids (list of channel_id)."),
        "write": False,
    },
    "db_stats": {
        "description": "Whole-database counts: channels, videos, verified faceless, etc.",
        "write": False,
    },
    "get_channel_details": {
        "description": "Full profile of one channel: stats, face verdict, top videos.",
        "write": False,
    },
    # -- scraper control ----------------------------------------------------------
    "crawl_niche": {
        "description": ("Run the scraper on a niche query NOW: finds seed videos, "
                        "crawls top channels, stores everything (free providers)."),
        "write": True,
    },
    "expand_network": {
        "description": "Follow cross-promotion links in stored descriptions to seed new channels.",
        "write": True,
    },
    "radar_status": {
        "description": "Live radar + monitor state (current crawl, GPU status, pipeline counts).",
        "write": False,
    },
    "edge_sync_status": {
        "description": (
            "Oracle/remote edge-worker state: whether secure sync is configured, "
            "bundles waiting, remote status, and the last import result."
        ),
        "write": False,
    },
    "discovery_yield": {
        "description": (
            "Committed new-channel additions by discovery search/topic/provider/sort. "
            "Counts overlapping searches once, shows missing historical evidence and "
            "excludes tiny samples from ranking. Args: hours? (1-168, default 24), "
            "limit? (1-100, default 50), minimum_searches? (1-1000, default 3). "
            "Recorded provenance is not an isolated causal-yield experiment."
        ),
        "write": False,
    },
    "import_edge_inbox": {
        "description": (
            "Verify signatures and atomically import waiting edge bundles into "
            "the canonical index. Args: max_bundles? (1-100)."
        ),
        "write": True,
    },
    # -- production lab (persistent per-channel memory) ---------------------------
}
