"""Track scraped competitors over days: attach, refresh live, snapshot, auto-drop.

Smart Scrape pulls a set of competitor channels; on its own that's a one-shot
photo. This module turns it into a monitored list:

- attach the pulled competitors to a stable per-niche workspace (``niche_channels``),
- refresh each one's LIVE stats (subs/views via the YouTube API) and LIVE uploads
  (via free yt-dlp) so growth reflects reality, not a stale index,
- snapshot them into ``channel_snapshots`` so growth-over-days is recorded,
- auto-drop the dead ones (no uploads for a long time).

Everything here is best-effort and reusable by three callers: Smart Scrape
completion (attach + day-0 snapshot), the Channel page "Refresh now" button, and
the background radar cycle (autonomous upkeep).
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable

from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.workspaces import NicheWorkspaceService
from beacon_engine.research.store import YouTubeStore

Progress = Callable[[str], None]

# A competitor with no upload for this long is treated as dead and auto-dropped.
DEFAULT_DROP_INACTIVE_DAYS = 120
# yt-dlp upload pull per channel — enough to catch new posts without a full crawl.
UPLOADS_PER_CHANNEL = 15


def _official_provider():
    """The YouTube Data API provider, or None when no key is configured."""
    try:
        from beacon_engine.research.providers.official_youtube_provider import (
            OfficialYouTubeProvider,
        )
        return OfficialYouTubeProvider()
    except Exception:  # noqa: BLE001 — missing key / import issue -> stats step skipped
        return None


def _ytdlp_provider():
    try:
        from beacon_engine.research.providers.ytdlp_provider import YtDlpProvider
        return YtDlpProvider()
    except Exception:  # noqa: BLE001
        return None


def track_bundle_competitors(
    store: YouTubeStore,
    niche_name: str,
    profile,
    bundle,
    *,
    snapshot: bool = True,
    on_step: Progress = lambda _m: None,
) -> dict:
    """Attach a Smart Scrape bundle's competitors to a per-niche workspace.

    Auto-tracks every pulled competitor (the user removes the ones they don't
    want later). Records a day-0 snapshot so growth has a baseline immediately.

    Nothing here is ever tracked as ``owned``: a scrape pulls channels the user is
    STUDYING, not channels they run. Signal reads the same table, so an auto-set
    ``owned`` role would tell it the user already competes in that niche. The role
    stays valid, but only the manual pickers in the app may assign it."""
    svc = NicheWorkspaceService(store)
    metrics = ChannelMetricsService(store)
    niche_id = svc.get_or_create_workspace(
        niche_name or "Tracked competitors",
        "Competitors auto-tracked from Smart Scrape.",
    )

    def _add(channel_id: str, role: str) -> bool:
        if not channel_id:
            return False
        try:
            return svc.add_channel(niche_id, channel_id, role=role, source="scrape")
        except (KeyError, ValueError):
            return False   # not indexed / bad role — skip quietly

    competitors = sum(_add(getattr(c, "channel_id", ""), "competitor")
                      for c in getattr(bundle, "competitors", []) or [])
    inspirations = sum(_add(getattr(c, "channel_id", ""), "inspiration")
                       for c in getattr(bundle, "inspiration_channels", []) or [])

    # The scraped-from channel is a rival the user analyzed, so it belongs in the
    # grid as a competitor -- but only if the workspace has no row for it yet.
    # add_channel overwrites the role on conflict, and skipping here is what keeps
    # a hand-set "owned" (or a role already assigned by the bundle loop above)
    # from being silently rewritten by a scrape.
    seed_id = getattr(profile, "channel_id", "") or ""
    seed_channel = 0
    if seed_id and not _is_tracked(svc, niche_id, seed_id):
        seed_channel = 1 if _add(seed_id, "competitor") else 0
        competitors += seed_channel
    on_step(f"Tracking {competitors} competitors + {inspirations} inspiration channels.")

    snapshotted = 0
    if snapshot:
        try:
            snapshotted = metrics.capture_workspace(niche_id)
        except Exception:  # noqa: BLE001 — baseline snapshot is best-effort
            pass
    return {
        "niche_id": niche_id, "competitors": competitors,
        "inspirations": inspirations, "seed_channel": seed_channel,
        "snapshotted": snapshotted,
    }


def _is_tracked(svc: NicheWorkspaceService, niche_id: int, channel_id: str) -> bool:
    """True when the workspace already holds this channel under ANY role."""
    try:
        resolved = svc.resolve_channel(channel_id)
    except Exception:  # noqa: BLE001 — unresolvable id -> let _add decide
        resolved = None
    target = (resolved or {}).get("channel_id") or channel_id
    with svc.store.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM niche_channels WHERE niche_id=? AND channel_id=? LIMIT 1",
            (int(niche_id), target),
        ).fetchone()
    return row is not None


def refresh_competitor_stats(
    store: YouTubeStore, channel_ids: list[str], *, on_step: Progress = lambda _m: None,
) -> int:
    """Refresh live subscriber/view/video counts via the YouTube API (batched)."""
    ids = [c for c in dict.fromkeys(channel_ids) if c]
    provider = _official_provider() if ids else None
    if not provider:
        return 0
    refreshed = 0
    for i in range(0, len(ids), 50):     # API batches up to 50 channels / call
        batch = ids[i:i + 50]
        try:
            for channel in provider.channels(batch):
                store.upsert_channel(channel)
                refreshed += 1
        except Exception:  # noqa: BLE001 — quota / transient errors shouldn't break upkeep
            on_step("Live stat refresh hit an API error; keeping existing numbers.")
            break
    if refreshed:
        on_step(f"Refreshed live stats for {refreshed} channels.")
    return refreshed


def refresh_competitor_uploads(
    store: YouTubeStore, channel_ids: list[str], *,
    per_channel: int = UPLOADS_PER_CHANNEL, on_step: Progress = lambda _m: None,
) -> int:
    """Pull each channel's latest uploads (free yt-dlp) and index new ones.

    Returns the count of NEW videos discovered — i.e. how many fresh uploads the
    tracked competitors posted since last check."""
    ids = [c for c in dict.fromkeys(channel_ids) if c]
    provider = _ytdlp_provider() if ids else None
    if not provider:
        return 0
    known = _known_video_ids(store, ids)
    new_total = 0
    for cid in ids:
        try:
            videos = provider.channel_videos(cid, limit=per_channel)
        except Exception:  # noqa: BLE001 — one dead channel shouldn't stop the rest
            continue
        fresh = [v for v in videos if v.video_id and v.video_id not in known]
        if videos:
            # Stamp the channel we asked for. The yt-dlp provider returns a channel's own
            # uploads with channel_id="" (it never had to look the channel up — we told it
            # which one to fetch), so these rows were landing in the index ORPHANED. The
            # damage was invisible: the count below still reported them as new uploads, and
            # channel cards kept showing an avg-views figure derived from lifetime
            # views/uploads, so a channel looked tracked while none of its videos were
            # actually attached to it. We know the id here — set it.
            for v in videos:
                if not v.channel_id:
                    v.channel_id = cid
            store.upsert_videos(videos)      # updates views + records new uploads
        new_total += len(fresh)
    if new_total:
        on_step(f"Found {new_total} new competitor uploads.")
    return new_total


def sync_competitors(
    store: YouTubeStore,
    niche_id: int,
    *,
    refresh: bool = True,
    snapshot: bool = True,
    drop_inactive_days: int = 0,
    on_step: Progress = lambda _m: None,
) -> dict:
    """One upkeep pass over a workspace's tracked competitors.

    refresh -> pull live stats + uploads; snapshot -> record growth point;
    drop_inactive_days>0 -> auto-remove competitors with no upload in that window.
    Safe to call interactively (Refresh now) or from the radar cycle."""
    svc = NicheWorkspaceService(store)
    metrics = ChannelMetricsService(store)
    rows = svc.workspace_channels(niche_id)
    competitors = [r for r in rows
                   if r.get("role") == "competitor" and int(r.get("enabled", 0))]
    ids = [r["channel_id"] for r in competitors]
    summary = {"competitors": len(ids), "refreshed": 0, "new_videos": 0,
               "snapshotted": 0, "dropped": []}
    if not ids:
        return summary

    if refresh:
        summary["refreshed"] = refresh_competitor_stats(store, ids, on_step=on_step)
        summary["new_videos"] = refresh_competitor_uploads(store, ids, on_step=on_step)
    if snapshot:
        try:
            summary["snapshotted"] = metrics.capture_workspace(niche_id)
        except Exception:  # noqa: BLE001
            pass

    if drop_inactive_days and drop_inactive_days > 0:
        # Re-read after refresh so last-upload reflects the fresh pull.
        for row in svc.workspace_channels(niche_id):
            if row.get("role") != "competitor" or not int(row.get("enabled", 0)):
                continue
            age = _days_since(row.get("last_video_at", ""))
            if age is not None and age > drop_inactive_days:
                svc.remove_channel(niche_id, row["channel_id"])
                summary["dropped"].append(row.get("title") or row["channel_id"])
        if summary["dropped"]:
            on_step(f"Auto-dropped {len(summary['dropped'])} dead competitors "
                    f"(no upload in {drop_inactive_days}+ days).")
    return summary


def _known_video_ids(store: YouTubeStore, channel_ids: list[str]) -> set[str]:
    if not channel_ids:
        return set()
    placeholders = ",".join("?" for _ in channel_ids)
    with store.connect() as conn:
        return {
            row["video_id"]
            for row in conn.execute(
                f"SELECT video_id FROM videos WHERE channel_id IN ({placeholders})",
                channel_ids,
            )
        }


def _days_since(iso_value: str):
    text = (iso_value or "").strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return max((_dt.datetime.now(_dt.timezone.utc) - parsed).days, 0)
