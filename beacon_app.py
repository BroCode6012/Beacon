"""SIGNAL — the standalone niche investigator & finder (its own app window).

Beacon is a CLIENT of the Finder service (the shared niche-finder tool). It does NOT read the
database directly and does NOT import the discovery engine's guts — it drives the finder through
`FinderClient` with the FULL key, exactly like the Lab will with a read-only key. This keeps
Beacon small and comfortable to work on alone.

On launch it connects to a running Finder service; if none is up, it self-hosts one in-process
so Beacon always works standalone (Phase 4 will make that a proper always-on separate process).

Run (uses the Lab venv, which has the deps):
    D:\\Youtube\\Quill\\.venv\\Scripts\\python.exe D:\\Youtube\\Beacon\\beacon_app.py

DESIGN CONTRACT — seed c1c0b719
THESIS: Beacon is a daylight operations board for finding repeatable YouTube
opportunities; it refuses the generic dark analytics dashboard.
OWN-WORLD: Cool paper canvas, ink-blue navigation rail, white evidence
surfaces, signal-green state marks, cobalt analytical accents, and compact
outlined controls.
STORY: The operator sees what matters now, understands why an opportunity
qualifies, and can act without decoding the machinery underneath.
FIRST VIEWPORT: A narrow operational rail anchors a bright workspace; a clear
screen heading and live system state lead directly into the primary evidence.
FORM: Daylight Flight Deck, grounded direction 4, seed c1c0b719.
FINISH: unreviewed and undocumented is unfinished; this build ends with the
finish review, the verdict, and DESIGN.md
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import logging
import time
from pathlib import Path

import engine_link  # noqa: F401 — wires shared engine onto sys.path + sets YOUTUBE_INDEX_DB
import flet as ft

import sweep_control  # drives the shared background scraper (must import AFTER engine_link)
import index_stats    # the one shared count of the library (instant; also used by Muse's door)
# On a server the scraper is a system service (sweep_control server mode): the words on screen
# must describe that machine, not a laptop that sleeps and has a login.
ON_SERVER = bool(getattr(sweep_control, "RADAR_UNIT", ""))
import muse_powers    # the outside agent's record of changes (listed + undoable on the Agent tab)
# An agent that works on Beacon from OUTSIDE (the operator's Muse, 2026-09-29), named per machine
# in beacon_host.env. The Agent tab then introduces it and lists what it changed, with Undo —
# instead of a red error about an in-app AI this machine deliberately does not have.
OUTSIDE_AGENT = os.getenv("BEACON_OUTSIDE_AGENT", "").strip()
OUTSIDE_AGENT_URL = os.getenv("BEACON_OUTSIDE_AGENT_URL", "").strip()
import beacon_agent   # Beacon's OWN control agent (any AI provider; separate from the Lab)
import beacon_ai      # which AI answers: providers, settings, connection tests
import beacon_save    # save + analyse a channel you found yourself on YouTube
import beacon_autopilot  # optional background maintenance loop (off by default)
from scripter.intelligence.finder_client import FinderClient, FinderError

# --- palette (self-contained; Beacon owns its own look) ---------------------
DISPLAY_FONT = "Bahnschrift"
BODY_FONT = "Segoe UI Variable Text"
BG = "#F3F6FA"
PANEL = "#FFFFFF"
CARD = "#FFFFFF"
SURFACE_ALT = "#F7F9FC"
SURFACE_STRONG = "#EEF3F8"
BORDER = "#D9E1EA"
TEXT = "#14233B"
MUTED = "#66758A"
ACCENT = "#17B978"
ACCENT_DARK = "#0C8C59"
ACCENT_SOFT = "#E6F7F0"
ACCENT2 = "#4E67E8"
ACCENT2_SOFT = "#EEF1FF"
WARN = "#D94F5C"
WARN_SOFT = "#FDECEF"
AMBER = "#C98213"
AMBER_SOFT = "#FFF4DE"
NAV_BG = "#10243E"
NAV_ACTIVE = "#1B385B"
NAV_TEXT = "#F7FAFD"
NAV_MUTED = "#AFC0D2"
SHADOW = ft.BoxShadow(
    blur_radius=22,
    spread_radius=-8,
    color=ft.Colors.with_opacity(0.16, NAV_BG),
    offset=ft.Offset(0, 7),
)

HOST = os.getenv("FINDER_HOST", "127.0.0.1")
PORT = int(os.getenv("FINDER_PORT", "8765"))
FULL_TOKEN = os.getenv("FINDER_TOKEN", "signal-full")
READ_TOKEN = os.getenv("FINDER_READ_TOKEN", "lab-read")
DB = os.environ["YOUTUBE_INDEX_DB"]
TOKEN_FILE = os.getenv("FINDER_TOKEN_FILE") or os.path.join(
    os.path.dirname(DB), "finder.tokens.json")


def muted(txt: str, size: int = 12) -> ft.Text:
    return ft.Text(txt, size=size, color=MUTED)


def h(txt: str, size: int = 18) -> ft.Text:
    return ft.Text(
        txt,
        size=size,
        weight=ft.FontWeight.W_700,
        color=TEXT,
        font_family=DISPLAY_FONT,
    )


# a small rotating palette so letter-avatars aren't all one colour
AVATAR_COLORS = [
    "#35C68B",
    "#6478EE",
    "#A976E8",
    "#E67C86",
    "#E5A83C",
    "#5899DD",
    "#4DB4B0",
]


def human_num(n) -> str:
    """1234 -> '1.2K', 947635 -> '948K', 2_400_000 -> '2.4M'. Blank if unknown."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    n = int(round(n))
    if n < 1_000:
        return f"{n:,}"
    if n < 1_000_000:
        v = n / 1_000
        return (f"{v:.1f}".rstrip("0").rstrip(".")) + "K"
    if n < 1_000_000_000:
        v = n / 1_000_000
        return (f"{v:.1f}".rstrip("0").rstrip(".")) + "M"
    v = n / 1_000_000_000
    return (f"{v:.1f}".rstrip("0").rstrip(".")) + "B"


def fmt_duration(secs) -> str:
    """Seconds -> 'M:SS' or 'H:MM:SS' (YouTube-style). Blank if unknown/zero."""
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    h_, rem = divmod(secs, 3600)
    m_, s_ = divmod(rem, 60)
    if h_:
        return f"{h_}:{m_:02d}:{s_:02d}"
    return f"{m_}:{s_:02d}"


def _research_progress_state(summary: dict) -> dict:
    """Normalize the measured scraper-search summary for the Overview tracker."""
    def nonnegative_int(key: str) -> int:
        try:
            return max(0, int(summary.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    total = nonnegative_int("expanded_queries")
    tested = nonnegative_int("searched_queries")
    total = max(total, tested)
    tested = min(tested, total)
    remaining = max(0, total - tested)
    ratio = tested / total if total else 0.0
    raw_coverage_pct = ratio * 100
    decimals = 2 if remaining and raw_coverage_pct >= 99.9 else 1
    coverage_pct = round(raw_coverage_pct, decimals)
    if remaining:
        coverage_pct = min(coverage_pct, 99.99)
    coverage_label = (
        f"{coverage_pct:.2f}"
        if remaining and coverage_pct >= 99.9
        else f"{coverage_pct:.1f}"
    )
    base_ideas = nonnegative_int("count")
    max_total = nonnegative_int("max_total")
    cooldown = nonnegative_int("cooldown_remaining_minutes")
    reason = summary.get("block_reason")

    if reason == "existing_query_backlog":
        status = (
            f"Autopilot is waiting while the scraper tests the "
            f"{remaining:,} remaining searches."
        )
    elif reason == "cooldown_active":
        status = (
            f"Autopilot is pausing new research for about {cooldown:,} more "
            f"minute{'s' if cooldown != 1 else ''}."
        )
    elif reason == "watchlist_at_capacity":
        status = (
            f"Autopilot cannot add more base ideas because the "
            f"{max_total:,}-idea limit is full."
        )
    elif summary.get("can_add_autonomously"):
        status = "Autopilot may add up to 2 focused research ideas when it finds a useful gap."
    else:
        status = "Autopilot is monitoring the research list."

    return {
        "base_ideas": base_ideas,
        "total": total,
        "tested": tested,
        "remaining": remaining,
        "ratio": ratio,
        "coverage_pct": coverage_pct,
        "coverage_label": coverage_label,
        "status": status,
    }


def _cloud_sync_view_state(payload: dict) -> dict:
    """Plain-language state for the Overview cloud-worker card."""
    def nonnegative_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    configured = bool(payload.get("configured"))
    transport_configured = bool(payload.get("transport_configured"))
    pending = nonnegative_int(payload.get("pending_bundles"))
    last_sync = payload.get("last_sync")
    last_sync = last_sync if isinstance(last_sync, dict) else {}
    transport = payload.get("transport")
    transport = transport if isinstance(transport, dict) else {}
    progress = payload.get("progress")
    progress = progress if isinstance(progress, dict) else {}
    remote_pending = nonnegative_int(transport.get("remote_pending"))
    remote = payload.get("remote")
    remote = remote if isinstance(remote, dict) else {}
    failed = nonnegative_int(last_sync.get("failed_bundles"))
    imported = nonnegative_int(last_sync.get("imported_bundles"))
    duplicates = nonnegative_int(last_sync.get("duplicate_bundles"))
    synced_at = str(last_sync.get("synced_at") or "")
    remote_state = str(remote.get("state") or "unknown").lower()
    remote_message = str(remote.get("message") or "").strip()
    # Cloud discovery liveness. remote_state alone is NOT trustworthy: during the
    # 2026-07-20 outage the worker deadlocked while still reporting "running", so
    # this card said "Oracle is connected" for 17 days with nothing arriving.
    health = payload.get("health")
    health = health if isinstance(health, dict) else {}
    health_state = str(health.get("state") or "").lower()
    # The web/Finder process intentionally does not hold the signing key on
    # Oracle. Its scheduled importer does. A missing key here is not evidence
    # that the cloud pipeline needs setup when it has reported live activity.
    cloud_in_use = bool(
        configured or transport_configured or remote.get("updated_at")
        or health_state in {"ok", "error"}
    )
    health_ok = bool(health.get("ok", True))
    health_problems = [str(item) for item in (health.get("problems") or [])][:3]
    health_summary = str(health.get("summary") or "")
    progress_state = str(progress.get("state") or "").lower()
    progress_processed = nonnegative_int(progress.get("processed_bundles"))
    progress_total = nonnegative_int(progress.get("total_bundles"))
    progress_backlog = nonnegative_int(progress.get("backlog_remaining"))
    progress_eta = nonnegative_int(progress.get("eta_seconds"))
    progress_phase = str(progress.get("phase") or "").lower()
    transaction_number = nonnegative_int(progress.get("transaction_number"))
    transaction_total = nonnegative_int(progress.get("transaction_total"))
    transaction_processed = nonnegative_int(
        progress.get("transaction_processed")
    )
    transaction_size = nonnegative_int(progress.get("transaction_size"))
    active_bundle = nonnegative_int(progress.get("active_bundle"))
    sync_running = progress_state == "importing"
    progress_ratio = (
        min(1.0, progress_processed / progress_total)
        if progress_total > 0
        else 0.0
    )
    activity_label = ""
    activity_ratio = None
    transaction_label = (
        f"transaction {transaction_number:,}/{transaction_total:,}"
        if transaction_number and transaction_total
        else "current transaction"
    )
    if progress_phase == "preparing":
        activity_label = "Preparing the import round."
    elif progress_phase == "verifying":
        bundle_label = (
            f"bundle {active_bundle:,}/{progress_total:,}"
            if active_bundle and progress_total
            else "the next bundle"
        )
        activity_label = f"Verifying {bundle_label} before database import."
        if transaction_size:
            activity_ratio = min(
                1.0,
                transaction_processed / transaction_size,
            )
    elif progress_phase == "waiting_for_database":
        activity_label = (
            f"{transaction_label.capitalize()} is waiting briefly for the "
            "database writer."
        )
    elif progress_phase == "staging":
        if transaction_size:
            activity_label = (
                f"Staging {transaction_label}: "
                f"{transaction_processed:,}/{transaction_size:,} bundles prepared."
            )
            activity_ratio = min(
                1.0,
                transaction_processed / transaction_size,
            )
        else:
            activity_label = f"Staging {transaction_label}."
    elif progress_phase == "committing":
        activity_label = (
            f"Saving {transaction_label} safely to the database."
        )
        activity_ratio = 1.0
    elif progress_phase == "acknowledging":
        activity_label = (
            f"{transaction_label.capitalize()} is committed; creating secure "
            "acknowledgements."
        )
        activity_ratio = 1.0
    elif progress_phase == "transaction_complete":
        activity_label = (
            f"{transaction_label.capitalize()} is safely committed; preparing "
            "the next transaction."
        )
        activity_ratio = 1.0
    elif sync_running:
        activity_label = (
            "This import began before detailed step tracking was available. "
            "Its committed count will still advance after the current safe "
            "database transaction finishes."
        )

    if not cloud_in_use:
        badge = "setup pending"
        tone = "muted"
        message = (
            "Oracle is not connected yet. Beacon is ready, but the secure "
            "signing key and file transport still need setup."
        )
    elif sync_running:
        badge = f"{progress_processed:,}/{progress_total:,} committed"
        tone = "accent2"
        message = (
            f"Import is active. Beacon has safely committed {progress_processed:,} of "
            f"{progress_total:,} bundles in this import round."
        )
        if transaction_size and progress_processed < progress_total:
            message += (
                f" The committed count advances after this "
                f"{transaction_size:,}-bundle transaction finishes."
            )
        if progress_eta > 0:
            eta_minutes = max(1, round(progress_eta / 60))
            message += (
                f" About {eta_minutes:,} minute"
                f"{'s' if eta_minutes != 1 else ''} remain at the current speed."
            )
        if progress_backlog > max(0, progress_total - progress_processed):
            message += (
                f" {progress_backlog:,} bundles remain across the laptop "
                "and Oracle queues."
            )
    elif not health_ok:
        badge = "not delivering"
        tone = "warn"
        message = (
            health_summary
            or "Cloud discovery has stopped delivering channels."
        )
    elif failed:
        badge = "attention"
        tone = "warn"
        message = (
            f"Last sync rejected {failed:,} bundle"
            f"{'s' if failed != 1 else ''}. "
            "They stayed quarantined; nothing unsafe was imported."
        )
    elif not configured and health_state == "ok":
        badge = "delivering"
        tone = "accent"
        message = (
            "Cloud discovery is delivering channels. A separate scheduled "
            "service imports signed bundles automatically."
        )
    elif pending:
        badge = f"{pending:,} waiting"
        tone = "accent2"
        message = (
            f"{pending:,} signed cloud bundle"
            f"{'s are' if pending != 1 else ' is'} waiting safely on this laptop."
        )
    elif remote_pending:
        badge = f"{remote_pending:,} on Oracle"
        tone = "accent2"
        message = (
            f"Oracle has {remote_pending:,} signed bundle"
            f"{'s' if remote_pending != 1 else ''} ready. "
            "Sync now will fetch, verify, and import them."
        )
    elif transport_configured and remote_state in {
        "running",
        "starting",
        "sleeping",
        "idle",
    }:
        badge = "connected"
        tone = "accent"
        message = (
            "Oracle is connected. Sync now checks for new signed bundles "
            "and imports them safely."
        )
    elif transport_configured:
        badge = "ready"
        tone = "accent2"
        message = (
            "Secure Oracle transport is ready. Sync now checks the remote "
            "worker for signed bundles."
        )
    elif remote_state in {"running", "starting", "sleeping", "idle"}:
        badge = "connected"
        tone = "accent"
        message = "Oracle is connected. No cloud bundles are waiting to be imported."
    else:
        badge = "ready"
        tone = "accent2"
        message = "Secure cloud sync is ready. No bundles are waiting on this laptop."

    last_line = ""
    if synced_at:
        parts = [f"Last sync imported {imported:,}"]
        if duplicates:
            parts.append(f"skipped {duplicates:,} duplicate")
        if failed:
            parts.append(f"rejected {failed:,}")
        last_line = " · ".join(parts) + f" · {synced_at}"

    return {
        "configured": configured,
        "cloud_in_use": cloud_in_use,
        "transport_configured": transport_configured,
        "pending": pending,
        "remote_pending": remote_pending,
        "failed": failed,
        "badge": badge,
        "tone": tone,
        "message": message,
        "last_line": last_line,
        "remote_state": remote_state,
        "remote_message": remote_message,
        "sync_running": sync_running,
        "progress_ratio": progress_ratio,
        "progress_processed": progress_processed,
        "progress_total": progress_total,
        "progress_backlog": progress_backlog,
        "progress_phase": progress_phase,
        "activity_label": activity_label,
        "activity_ratio": activity_ratio,
        "health_ok": health_ok,
        "health_problems": health_problems,
        "sync_enabled": configured and not sync_running and (
            pending > 0 or transport_configured
        ),
    }


# Quick filters over Beacon's mandatory faceless + replicable opportunity gate.
# Every preset changes ranking/narrowing only; `_ch_query_args` always injects the
# shared-engine `replicable_only=True` contract.
CH_CHIPS = [
    ("Best Opportunities", {"sort": "opportunity_score"}),
    ("Low Subs & High Views", {"max_subs": 50_000, "min_avg_views": 50_000,
                                "sort": "opportunity_score"}),
    ("Above 50k Avg Views", {"min_avg_views": 50_000, "sort": "opportunity_score"}),
    ("Documentary Newcomers", {"category_contains": "documentary", "max_days_since_start": 180,
                                "min_avg_views": 20_000, "sort": "opportunity_score"}),
    ("New Opportunities", {"max_days_since_start": 90,
                            "sort": "opportunity_score"}),
    ("Long-form", {"video_format": "long", "sort": "opportunity_score"}),
]

# sortable column label -> the store's sort key (all reachable via find_channels today)
CH_SORTS = [
    ("Opportunity Score", "opportunity_score"),
    ("Subscribers", "subscribers"), ("Avg Views", "avg_views"),
    ("Days Since Start", "days_since_start"), ("Uploads", "uploads"),
    ("Revenue", "revenue"),
]

# Cards loaded per "page": the initial batch AND each "Show more" step. There is NO
# hard cap — Show more keeps paging deeper into the full result set (a grid can't
# paint tens of thousands of cards at once, so results stream in batches on demand).
# Cards per page. Each channel card is ~85 on-screen elements, so 120 cards meant ~10,000
# elements and ~1.5s for every redraw of the Channels page (measured 2026-09-16). 40 keeps
# the first screen full and makes each redraw about three times cheaper; Show more pages on.
CH_PAGE = 40
ALL_TOPICS = "__all_topics__"
# The Overview counts are an 8-12 second database job; repeating it every minute kept the
# service busy for a sixth of every minute to update numbers that barely move.
OVERVIEW_REFRESH_SECONDS = max(
    5, int(os.getenv("SIGNAL_OVERVIEW_REFRESH_SECONDS", "300")))
CLOUD_PROGRESS_REFRESH_SECONDS = max(
    2, int(os.getenv("SIGNAL_CLOUD_PROGRESS_REFRESH_SECONDS", "3")))
CACHE_ERROR_TTL_SECONDS = 15
# How long loaded data counts as current. Short lifetimes made every return to a tab a
# reload. These screens change slowly; the Refresh button always fetches immediately, and the
# Real Time feed and cloud progress keep their own live tickers while they are on screen.
CACHE_TTL_SECONDS = {
    "overview": OVERVIEW_REFRESH_SECONDS,
    "edge_sync": CLOUD_PROGRESS_REFRESH_SECONDS,
    "niche_signals": 300,
    "niche_signals:": 300,
    "topics": 1800,
    "niches": 1800,
    "channels:": 600,
    "realtime:": 120,
    "find_channels:": 600,
    }


def _pipeline_progress_state(status: dict) -> dict | None:
    """Plain-language state for the optional split discovery pipeline."""
    if str(status.get("pipeline_mode") or "") != "split":
        return None

    def lane(name: str, fallback: str) -> dict:
        raw = status.get(name)
        raw = raw if isinstance(raw, dict) else {}
        state = str(raw.get("state") or fallback)
        message = str(
            raw.get("message") or raw.get("current_query") or state
        ).strip()
        return {"state": state, "message": message}

    raw_queue = status.get("queue")
    raw_queue = raw_queue if isinstance(raw_queue, dict) else {}

    def count(name: str) -> int:
        try:
            return max(0, int(raw_queue.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    pending_channels = count("pending_channels")
    pending_batches = count("pending_batches")
    queued_channels = count("queued_channels")
    running_channels = count("running_channels")
    queued_batches = count("queued_batches")
    running_batches = count("running_batches")
    # Status written by the first split-worker release had only aggregate
    # pending counts. Keep that short transition readable during deployment.
    if pending_channels and not queued_channels and not running_channels:
        if running_batches and not queued_batches:
            running_channels = pending_channels
        else:
            queued_channels = pending_channels

    if running_channels and queued_channels:
        queue_text = (
            f"{running_channels:,} newly found channels are being organized now; "
            f"{queued_channels:,} more are waiting in {queued_batches:,} durable "
            f"{'batch' if queued_batches == 1 else 'batches'}. "
            + ("If the scraper restarts, the remaining work resumes safely." if ON_SERVER
               else "Closing the laptop pauses and safely resumes the remaining work.")
        )
    elif running_channels:
        queue_text = (
            f"{running_channels:,} newly found channels are being organized now "
            f"across {running_batches:,} durable "
            f"{'batch' if running_batches == 1 else 'batches'}. "
            "If interrupted, they safely return to the queue."
        )
    elif queued_channels:
        queue_text = (
            f"{queued_channels:,} newly found channels are waiting in "
            f"{queued_batches:,} durable organization "
            f"{'batch' if queued_batches == 1 else 'batches'}. "
            + ("They survive restarts and are picked up automatically." if ON_SERVER
               else "Closing the laptop pauses them safely; reopening resumes them.")
        )
    else:
        queue_text = (
            "Organization queue is clear. New discoveries will be saved here "
            "until the organizer finishes them."
        )

    return {
        "discovery": lane("discovery", "starting"),
        "organizer": lane("organizer", "starting"),
        "pending_channels": pending_channels,
        "pending_batches": pending_batches,
        "queued_channels": queued_channels,
        "running_channels": running_channels,
        "queue_text": queue_text,
    }


def _iso_age_seconds(value, *, now=None) -> float | None:
    """Return the non-negative age of one ISO timestamp, or None when unreadable."""
    if not value:
        return None
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (current - parsed).total_seconds())
    except (TypeError, ValueError):
        return None


def _niche_watch_state(payload, *, now=None) -> dict:
    """Normalize Finder niche-watch data and classify its two independent clocks."""
    payload = payload if isinstance(payload, dict) else {}
    signals = [item for item in payload.get("signals", []) if isinstance(item, dict)]
    workspaces = [
        item for item in payload.get("workspaces", []) if isinstance(item, dict)
    ]

    def newest(items, key):
        candidates = [str(item.get(key) or "") for item in items if item.get(key)]
        return max(candidates, default="")

    latest_beacon_at = max(
        newest(signals, "detected_at"),
        newest(workspaces, "latest_beacon_at"),
    )
    latest_activity_at = newest(workspaces, "last_activity_at")
    signal_age = _iso_age_seconds(latest_beacon_at, now=now)
    activity_age = _iso_age_seconds(latest_activity_at, now=now)
    if not workspaces:
        freshness = "no_workspaces"
    elif signal_age is None:
        freshness = "no_signals"
    elif signal_age >= 3 * 86400 and activity_age is not None and activity_age < 86400:
        freshness = "fresh_evidence_stale_alerts"
    elif signal_age >= 3 * 86400 and (
        activity_age is None or activity_age >= 3 * 86400
    ):
        freshness = "stale_workspace"
    else:
        freshness = "current"

    return {
        "signals": signals,
        "workspaces": workspaces,
        "signal_count": int(payload.get("signal_count") or len(signals)),
        "high_signal_count": int(payload.get("high_signal_count") or 0),
        "latest_beacon_at": latest_beacon_at,
        "latest_activity_at": latest_activity_at,
        "freshness": freshness,
    }


class BeaconApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.section = "Overview"
        self.hosting = False
        self.ch_niche: dict | None = None   # niche selected on Channels (filters the grid)
        self.ch_topic: str | None = None    # clean topic niche filter (History/Space/...) — survives presets
        self.ch_view = "index"              # "index" = browse whole index; "niche" = this niche's channels
        self.agent = None                   # Beacon's control agent (lazy); its chat transcript:
        self.agent_log: list[dict] = []
        self._busy = False
        self._busy_lock = threading.Lock()
        self._busy_tasks: dict[int, tuple[str, str | None]] = {}   # token -> (message, screen)
        self._busy_seq = 0
        # Which cached keys each screen displayed on its last render, so a finished load only
        # redraws the page when it changes something the user can actually see.
        self._section_keys: dict[str, set] = {}
        self._render_keys: set | None = None
        self._finder_process = None        # the finder we started, stopped when Beacon exits
        # _render() rebuilds self.content.controls (clear + many appends) and is called from
        # MANY daemon threads (cache loads, 60s/3s ticks, target-count refresh, watchers).
        # Without serialization two renders interleave and corrupt the control list (duplicated
        # / missing cards, page.update on a half-built tree). RLock: same-thread re-entrancy safe.
        self._render_lock = threading.RLock()
        self._rendering = False
        self._session_stop = threading.Event()
        page.on_disconnect = self._session_disconnected
        page.on_close = self._session_disconnected
        page.on_connect = self._session_connected
        # in-memory data cache so switching tabs is instant (load once, reuse).
        # keys: "overview", "niches", f"niche:{id}", "find_channels".
        self.cache: dict[str, dict] = {}
        self._loading: set[str] = set()
        self._epoch: dict[str, int] = {}   # bumped on invalidate → voids in-flight loads
        self._tc_lock = threading.Lock()   # guards the scrape-target count refresher state
        self._target_counts = None         # {tid: {found,days} | {"error":True}} | None
        self._target_counts_at = 0.0       # monotonic time the last pass FINISHED
        self._target_counts_busy = False
        self._rechecked: set[str] = set()  # browse keys that already got a transient-empty recheck
        self._scroll_offset = 0.0          # where the reader was, kept across re-renders
        self._scroll_section: str | None = None
        self._find_requested = False       # has the user asked to load faceless channels?
        self._find_draft = ""
        self._find_sweep_result: dict | None = None
        self._find_sweep_error: str | None = None
        # optimistic scraper state so the Start/Stop buttons flip INSTANTLY on click
        # (the worker takes a few seconds to boot/exit); reconciled to reality each render.
        self._sweep_starting = False
        self._sweep_stopping = False
        self._cloud_syncing = False
        self._niche_actions: set[str] = set()
        self._last_read_signal: dict | None = None
        self.niche_signal_shown = 6
        self.niche_workspace_expanded = False
        # Real Time feed: discovery-recency window (days) + the once-only auto-refresh guard.
        self.rt_window = 1                 # 1 / 7 / 30 days, or None for "newest all-time"
        self.rt_filters: dict = {}         # metric filters for the Real Time feed (own set)
        self.rt_topic: str | None = None   # topic filter for the Real Time feed
        self.rt_chip: str | None = None    # active preset chip on Real Time
        self._rt_watch_started = False
        self._overview_watch_started = False
        self._cloud_watch_started = False
        # progressive paging (no hard cap): how many cards are currently loaded per tab.
        # "Show more" grows these; they reset to CH_PAGE when the filter/window changes.
        self.ch_shown = CH_PAGE
        self.rt_shown = CH_PAGE
        self.find_shown = CH_PAGE
        self.saved_shown = CH_PAGE
        self._saved_refreshed_at = 0.0     # when live numbers were last pulled SUCCESSFULLY
        self._saved_refresh_failed_at = 0.0  # when the last live check failed
        self._saved_refreshing = False
        self._saved_niche_id = None        # the hand-picked shelf; resolved on first use
        self._saved_draft = ""             # survives background re-renders while typing
        self._saved_result = None          # what the last save produced
        self._saving_link = False
        self._ch_last_base = None          # detects a filter/sort/niche change → reset paging
        # NexLev-style Channels browser: the active filter set + which preset chip.
        # (keys with a leading _ are client-side only, stripped before the finder call)
        # Default = NO preset selected (just the default sort, no metric filters). Presets are
        # optional toggles the user opts into; nothing is force-highlighted. ch_chip=None means
        # "no preset" (same as "Custom": no chip lit).
        self.ch_filters: dict = {"sort": "opportunity_score", "limit": 60}
        self.ch_chip: str | None = None
        # persistent "now scraping X" confirmation on the Channels tab (a fleeting toast
        # left the user unsure anything happened); cleared when the filter changes/dismissed
        self._scrape_banner: dict | None = None

        page.title = "Beacon — Niche Finder"
        page.bgcolor = BG
        page.padding = 0
        page.theme_mode = ft.ThemeMode.LIGHT
        page.theme = ft.Theme(
            use_material3=True,
            font_family=BODY_FONT,
            color_scheme=ft.ColorScheme(
                primary=ACCENT,
                on_primary="#FFFFFF",
                primary_container=ACCENT_SOFT,
                on_primary_container=ACCENT_DARK,
                secondary=ACCENT2,
                on_secondary="#FFFFFF",
                secondary_container=ACCENT2_SOFT,
                on_secondary_container=TEXT,
                error=WARN,
                on_error="#FFFFFF",
                error_container=WARN_SOFT,
                on_error_container=WARN,
                background=BG,
                on_background=TEXT,
                surface=PANEL,
                on_surface=TEXT,
                surface_variant=SURFACE_ALT,
                on_surface_variant=MUTED,
                outline=BORDER,
                outline_variant=SURFACE_STRONG,
                shadow=NAV_BG,
            ),
            scaffold_bgcolor=BG,
            card_color=PANEL,
            divider_color=BORDER,
            dialog_bgcolor=PANEL,
            hint_color=MUTED,
        )
        try:
            page.window.width, page.window.height = 1320, 860
            page.window.min_width, page.window.min_height = 1040, 700
        except Exception:  # noqa: BLE001 — older/newer flet
            pass

        self._boot()

    def _boot(self) -> None:
        """Connect to the data service, then build the window.

        Re-runnable: when the database stays unavailable, the window explains why and offers
        "Try again" instead of Flet's bare "There was an error while processing your
        request" — which is what a locked database used to leave on screen.
        """
        page = self.page
        # The data service can take a few seconds to start in its own process; say so rather
        # than leaving an empty window.
        # expand: the "waiting for the database" line is long and must wrap, not run off.
        self._starting_text = ft.Text("Starting Beacon's data service…", size=13, color=MUTED,
                                      expand=True)
        starting = ft.Container(ft.Row([
            ft.ProgressRing(width=18, height=18, stroke_width=2, color=ACCENT),
            self._starting_text,
        ], spacing=12), padding=40)
        page.add(starting)
        try:
            page.update()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client = self._resolve_service()
        except Exception as exc:  # noqa: BLE001 — shown to the operator, never a blank crash
            page.controls.remove(starting)
            page.add(self._boot_failure(exc))
            try:
                page.update()
            except Exception:  # noqa: BLE001
                pass
            return
        page.controls.remove(starting)

        # Autopilot: optional background maintenance loop (OFF by default; starts only
        # if the user previously enabled it). Runs safe/reversible tasks + logs them.
        self.autopilot = beacon_autopilot.Autopilot(self)
        self.autopilot.apply_provider_policy()   # Azure provider ⇒ keep maintenance on
        if self.autopilot.enabled:
            self.autopilot.start()

        # layout: left nav | content | status bar
        self.content = ft.Column([], scroll=ft.ScrollMode.AUTO, spacing=18, expand=True,
                                 on_scroll=self._on_scroll, on_scroll_interval=120)
        self.status = ft.Text("", size=12, color=MUTED)
        self.spinner = ft.ProgressRing(width=15, height=15, stroke_width=2,
                                       color=ACCENT, visible=False)
        # A slim indeterminate bar across the TOP of the content area, shown whenever
        # ANY background work is in flight — so the user sees activity where their eyes
        # already are (on the cards), not only via the tiny footer spinner.
        self.busy_bar = ft.ProgressBar(visible=False, bar_height=3,
                                       color=ACCENT, bgcolor=SURFACE_STRONG)
        nav = self._nav()
        body = ft.Container(
            ft.Column([
                self.busy_bar,
                ft.Container(
                    self.content,
                    expand=True,
                    padding=ft.padding.only(left=30, right=30, top=26, bottom=34),
                ),
            ], spacing=0, expand=True),
            expand=True, padding=0, bgcolor=BG)
        status_bar = ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.RADIO_BUTTON_CHECKED, size=13, color=ACCENT),
                self.spinner,
                self.status,
            ], spacing=8),
            padding=ft.padding.symmetric(7, 18), bgcolor=PANEL,
            border=ft.border.only(top=ft.border.BorderSide(1, BORDER)))
        page.add(ft.Column([
            ft.Row([nav, body], expand=True, spacing=0,
                   vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            status_bar,
        ], expand=True, spacing=0))

        self._render()

    def _session_disconnected(self, _=None) -> None:
        """Release this browser session's recurring work when its tab goes away."""
        self._session_stop.set()
        self._overview_watch_started = False
        self._cloud_watch_started = False
        self._rt_watch_started = False
        ap = getattr(self, "autopilot", None)
        if ap is not None:
            ap.shutdown()

    def _session_connected(self, _=None) -> None:
        """Resume a session that Flet reconnected to the same page."""
        if not self._session_stop.is_set():
            return
        self._session_stop = threading.Event()
        ap = getattr(self, "autopilot", None)
        if ap is not None and ap.enabled:
            ap.start()
        if hasattr(self, "content"):
            self._render()

    # -- service wiring -----------------------------------------------------
    # A cold engine import on a busy disk; past this Beacon falls back to hosting in-process.
    FINDER_START_TIMEOUT = 60
    # ...unless the service says it is waiting for a database another program holds. It
    # retries for FINDER_DB_WAIT_SECONDS itself, so Beacon waits that long plus a margin.
    FINDER_DB_WAIT = int(os.getenv("FINDER_DB_WAIT_SECONDS", "600")) + 30
    FINDER_LOG = os.getenv("FINDER_LOG") or os.path.join(
        os.path.dirname(DB), "finder_service.log")

    @staticmethod
    def _db_busy(exc: BaseException) -> bool:
        text = str(exc).lower()
        return "database is locked" in text or "database is busy" in text

    def _finder_waiting_for_db(self) -> bool:
        """Is the service's CURRENT start waiting on a busy database? (It says so in its log.)"""
        try:
            with open(self.FINDER_LOG, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 8192))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return False
        latest = tail.rsplit("--- FINDER start", 1)[-1]
        return "FINDER waiting for the database" in latest

    def _starting_note(self, started: float, waiting: bool) -> None:
        """Keep the start-up line honest about what Beacon is waiting for."""
        text = getattr(self, "_starting_text", None)
        if text is None:
            return
        secs = int(time.time() - started)
        if waiting:
            value = ("Waiting for the database — another program (the background scraper or "
                     "the cloud import) is busy with it. Beacon opens as soon as it is free"
                     f" · {secs}s")
        elif secs >= 10:
            value = f"Starting Beacon's data service… {secs}s"
        else:
            value = "Starting Beacon's data service…"
        if text.value != value:
            text.value = value
            try:
                text.update()
            except Exception:  # noqa: BLE001
                pass

    def _boot_failure(self, exc: BaseException) -> ft.Control:
        """What the window shows when the data service cannot start, with a way forward."""
        busy = self._db_busy(exc)
        title = ("The database is still busy" if busy
                 else "Beacon couldn't start its data service")
        waited = self.FINDER_DB_WAIT
        span = f"{waited // 60} minutes" if waited >= 120 else f"{waited} seconds"
        detail = (f"Another program (the background scraper or the cloud import) held the "
                  f"database for over {span}. Nothing is lost — "
                  "try again in a minute." if busy
                  else f"{type(exc).__name__}: {exc}")

        def retry(e) -> None:
            e.control.disabled = True
            e.control.text = "Starting…"
            try:
                e.control.update()
            except Exception:  # noqa: BLE001
                pass
            self.page.controls.clear()
            self._boot()

        return ft.Container(ft.Column([
            ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, color=WARN, size=20),
                    ft.Text(title, size=16, weight=ft.FontWeight.W_600)], spacing=10),
            ft.Text(detail, size=13, color=MUTED, selectable=True),
            ft.FilledButton("Try again", icon=ft.Icons.REFRESH, on_click=retry),
        ], spacing=14, tight=True), padding=40)

    def _resolve_service(self) -> FinderClient:
        """Connect to the Finder service, starting it as its OWN process when none is running.

        It used to run inside this app's process. Python executes one thread at a time, so
        drawing a large page and answering database requests queued behind each other.
        Measured 2026-09-16 with the scraper running: the Overview counts took 144s inside
        the app against 12s from a separate process, the topic list 23s against 4s, and the
        loading bar stayed on for the whole session. A separate process has its own
        interpreter, so neither waits for the other. In-process hosting remains only as the
        fallback when the separate process cannot start.
        """
        url, full = f"http://{HOST}:{PORT}", FULL_TOKEN
        if os.path.exists(TOKEN_FILE):
            try:
                data = json.load(open(TOKEN_FILE, encoding="utf-8"))
                url, full = data.get("url", url), data.get("full", full)
            except Exception:  # noqa: BLE001
                pass
        client = FinderClient(url, full)
        if client.is_up():
            self.hosting = False
            return client
        started = time.time()
        own = self._start_finder_process()
        if own is not None:
            probe = FinderClient(f"http://{HOST}:{PORT}", FULL_TOKEN)
            deadline = started + self.FINDER_START_TIMEOUT
            hard_deadline = started + self.FINDER_DB_WAIT
            while time.time() < hard_deadline:
                if probe.is_up():
                    self.hosting = False
                    self._finder_process = own
                    # Same lifetime as the old in-process service: it ends with Beacon, so
                    # a restart always runs current engine code instead of an orphan.
                    atexit.register(self._stop_finder_process)
                    return probe
                if own.poll() is not None:
                    break              # it exited: port taken, or a start-up error
                # Past the normal start time, keep waiting ONLY while the service says it is
                # waiting for the database; anything else is a stuck start.
                waiting = self._finder_waiting_for_db()
                if time.time() >= deadline and not waiting:
                    break
                self._starting_note(started, waiting)
                time.sleep(0.5)
            if probe.is_up():          # it stepped aside for a service that came up meanwhile
                self.hosting = False
                return probe
            self._kill_process_tree(own)
        # self-host in-process (fallback) — also patient with a busy database, but only
        # until the overall deadline: if the service already waited it out, so did we.
        from scripter.intelligence.local_api import serve_in_thread
        while True:
            try:
                serve_in_thread(host=HOST, port=PORT, db_path=DB,
                                token=FULL_TOKEN, read_token=READ_TOKEN)
                break
            except Exception as exc:  # noqa: BLE001 — _boot shows anything else
                if not self._db_busy(exc) or time.time() >= started + self.FINDER_DB_WAIT:
                    raise
                self._starting_note(started, True)
                time.sleep(5)
        self.hosting = True
        return FinderClient(f"http://{HOST}:{PORT}", FULL_TOKEN)

    @staticmethod
    def _start_finder_process():
        """Launch finder_service.py hidden, with this environment (port, database, token file)."""
        script = Path(__file__).with_name("finder_service.py")
        # sys.prefix is the venv even when the venv launcher re-executed the base interpreter,
        # and only the venv's interpreter has the engine's packages.
        pythonw = Path(sys.prefix) / "Scripts" / "pythonw.exe"
        interpreter = str(pythonw if pythonw.exists() else sys.executable)
        try:
            return subprocess.Popen(
                [interpreter, str(script)], cwd=str(script.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            return None

    @staticmethod
    def _kill_process_tree(proc) -> None:
        # The venv's pythonw is a launcher that starts the real interpreter as a child, so
        # stopping only the launcher would leave the service running.
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _stop_finder_process(self) -> None:
        self._kill_process_tree(getattr(self, "_finder_process", None))

    # -- shell helpers ------------------------------------------------------
    def _nav_tiles(self) -> list[ft.Container]:
        """Build the nav tiles for the CURRENT section (rebuilt on every render
        so the green highlight follows the page you're on)."""
        items = [("Overview", ft.Icons.INSIGHTS),
                 ("Channels", ft.Icons.GRID_VIEW), ("Real Time", ft.Icons.BOLT),
                 ("Find", ft.Icons.TRAVEL_EXPLORE), ("Saved", ft.Icons.BOOKMARK_ADD),
                 ("Agent", ft.Icons.AUTO_AWESOME)]

        def tile(name, icon):
            active = name == self.section
            return ft.Container(
                ft.Row([
                    ft.Container(
                        ft.Icon(icon, size=18, color=ACCENT if active else NAV_MUTED),
                        width=30,
                        height=30,
                        border_radius=10,
                        bgcolor=(
                            ft.Colors.with_opacity(0.14, ACCENT)
                            if active
                            else ft.Colors.with_opacity(0.06, "#FFFFFF")
                        ),
                        alignment=ft.alignment.center,
                    ),
                    ft.Text(
                        name,
                        color=NAV_TEXT if active else NAV_MUTED,
                        size=13,
                        weight=(
                            ft.FontWeight.W_700
                            if active
                            else ft.FontWeight.W_500
                        ),
                    ),
                ], spacing=11),
                padding=ft.padding.symmetric(9, 11),
                border_radius=12,
                bgcolor=NAV_ACTIVE if active else None,
                ink=True,
                ink_color=ft.Colors.with_opacity(0.08, "#FFFFFF"),
                on_click=lambda _, n=name: self._go(n))

        return [tile(n, i) for n, i in items]

    def _nav(self) -> ft.Container:
        self._nav_col = ft.Column(self._nav_tiles(), spacing=5)
        brand_mark = ft.Container(
            ft.Icon(ft.Icons.SENSORS, color=NAV_BG, size=23),
            width=42,
            height=42,
            border_radius=13,
            bgcolor=ACCENT,
            alignment=ft.alignment.center,
        )
        workspace_state = ft.Container(
            ft.Column([
                ft.Row([
                    ft.Container(
                        width=8,
                        height=8,
                        border_radius=4,
                        bgcolor=ACCENT,
                    ),
                    ft.Text(
                        "Private workspace",
                        size=11,
                        color=NAV_TEXT,
                        weight=ft.FontWeight.W_600,
                    ),
                ], spacing=8),
                ft.Text("One live index", size=10, color=NAV_MUTED),
            ], spacing=3, tight=True),
            padding=12,
            border_radius=12,
            bgcolor=ft.Colors.with_opacity(0.07, "#FFFFFF"),
        )
        return ft.Container(
            ft.Column([
                ft.Row([
                    brand_mark,
                    ft.Column([
                        ft.Text(
                            "Beacon",
                            size=21,
                            weight=ft.FontWeight.W_700,
                            color=NAV_TEXT,
                            font_family=DISPLAY_FONT,
                        ),
                        ft.Text(
                            "Opportunity control",
                            size=10,
                            color=NAV_MUTED,
                        ),
                    ], spacing=0, tight=True),
                ], spacing=11),
                ft.Divider(
                    color=ft.Colors.with_opacity(0.13, "#FFFFFF"),
                    height=24,
                ),
                self._nav_col,
                ft.Container(expand=True),
                workspace_state,
            ], spacing=10, expand=True),
            width=230,
            padding=20,
            bgcolor=NAV_BG,
        )

    def _go(self, name: str) -> None:
        self.section = name
        self._scroll_offset = 0.0      # a new screen starts at the top, not mid-page
        self._scroll_section = name
        self._refresh_busy_state()     # another screen's loading must not follow the user here
        self._render()

    def toast(self, msg: str) -> None:
        self.status.value = msg
        try:
            self.status.update()
        except Exception:  # noqa: BLE001
            pass

    def _apply_busy_state(self, busy: bool, msg: str) -> None:
        self._busy = busy
        # The indicator shows only work for the screen in front of the user (or work they
        # started). The global bar used to light up for every background job on every tab,
        # and one slow job kept it on for an entire session.
        visible = self._visible_busy_message()
        shown = visible is not None
        msg = visible or ""
        self.spinner.visible = shown
        self.status.value = msg
        # Make the whole page visibly pulse while work runs, and colour the status
        # text so "something is happening" reads at a glance (not a grey 12px line).
        try:
            self.busy_bar.visible = shown
        except Exception:  # noqa: BLE001 — bar not built yet during very early boot
            pass
        self.status.color = ACCENT if shown else MUTED
        # _ensure() can start a load while _render() is still assembling the
        # control tree. Publishing that half-built tree can strand a web
        # session with only its header visible.
        if getattr(self, "_rendering", False):
            return
        try:
            self.page.update()
        except Exception:  # noqa: BLE001
            pass

    def _visible_busy_message(self) -> str | None:
        """The newest running job the user should see: their own actions, or loads for the
        screen they are on. None when nothing visible is running."""
        here = getattr(self, "section", None)
        shown = [token for token, (_msg, screen) in self._busy_tasks.items()
                 if screen is None or screen == here]
        return self._busy_tasks[max(shown)][0] if shown else None

    def _begin_busy(self, msg: str, screen: str | None = None) -> int:
        """Register one background operation and return its unique token.

        `screen` ties the loading signal to one tab; None means the user started it, so it
        shows wherever they are.
        """
        # Apply the visible state INSIDE the lock: otherwise two overlapping begin/end
        # pairs can apply out of order (a stale "not busy" landing after a "busy"), leaving
        # the spinner/bar stuck opposite to reality.
        with self._render_lock, self._busy_lock:
            self._busy_seq += 1
            token = self._busy_seq
            self._busy_tasks[token] = (msg, screen)
            self._apply_busy_state(True, msg)
        return token

    def _end_busy(self, token: int) -> None:
        """Finish one operation without hiding other concurrent work."""
        with self._render_lock, self._busy_lock:
            self._busy_tasks.pop(token, None)
            self._apply_busy_state(bool(self._busy_tasks), "")

    def _refresh_busy_state(self) -> None:
        with self._render_lock, self._busy_lock:
            self._apply_busy_state(bool(self._busy_tasks), "")

    def run_bg(self, fn, on_done, start_msg: str, *, screen: str | None = None,
               quiet: bool = False) -> None:
        """Run a finder call off the UI thread; re-render on completion.

        quiet=True refreshes data already on screen without any loading signal — the old
        values stay visible until the new ones land, the way large apps revalidate.
        """
        token = None if quiet else self._begin_busy(start_msg, screen)

        def worker():
            try:
                result = fn()
                err = None
            except Exception as ex:  # noqa: BLE001
                result, err = None, ex
            if token is not None:
                self._end_busy(token)
            if err:
                self.toast(f"⚠ {str(err)[:160]}")
            else:
                try:
                    on_done(result)
                except Exception as ex:  # noqa: BLE001
                    self.toast(f"⚠ {str(ex)[:160]}")

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            if token is not None:
                self._end_busy(token)
            raise

    # -- agent (Beacon's own assistant, drives finder + scraper) ----
    def _get_agent(self):
        if self.agent is None:
            self.agent = beacon_agent.BeaconAgent(self.client, sweep_control, ui=self)
        return self.agent

    def _ask_card_body(self, optional_only: bool, send) -> list:
        """The question box + provider picker — or, where an outside agent does the job and no
        in-app AI is set up, just a quiet way in to set one up. A box you cannot use, next to a
        red "needs setup" line, read as broken (operator review, 2026-09-29)."""
        if not optional_only:
            return [ft.Row([self._agent_input, send], spacing=10,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    self._ai_provider_row()]
        if getattr(self, "_show_ai_setup", False):
            return [self._ai_provider_row()]

        def show(_):
            self._show_ai_setup = True
            self._render()

        return [ft.TextButton("Set up an AI inside Beacon (optional)",
                              icon=ft.Icons.SETTINGS_OUTLINED, on_click=show)]

    # -- the agent that works from OUTSIDE Beacon (Muse), and what it changed --------------
    def _outside_agent_card(self) -> ft.Control:
        """Who the agent is and the recent changes it recorded."""
        name = OUTSIDE_AGENT
        records = muse_powers.ChangeLog(muse_powers.CHANGES_FILE).all()
        undone = {r.get("undo_of") for r in records if r.get("undo_of") and r.get("ok")}
        busy = self.__dict__.setdefault("_outside_undoing", set())
        visit = muse_powers.last_outside_visit()
        if visit is None:
            seen = "hasn't visited yet"
        else:
            import datetime as _dt
            seen = "last visit " + self._ago_seconds((_dt.datetime.now() - visit).total_seconds())
        rows = []
        for r in list(reversed(records))[:8]:
            cid = str(r.get("change_id") or "")
            can_undo = (bool((r.get("undo") or {}).get("steps")) and r.get("ok")
                        and not r.get("undo_of"))
            if cid in undone:
                right = self.pill("Undone", MUTED, ft.Icons.UNDO)
            elif can_undo:
                right = ft.OutlinedButton(
                    "Undoing…" if cid in busy else "Undo", icon=ft.Icons.UNDO,
                    disabled=cid in busy,
                    tooltip="Reverse exactly this change (recorded, so it shows up here too)",
                    on_click=lambda _, c=cid: self._outside_undo(c))
            else:
                right = ft.Container()
            rows.append(ft.Row([
                ft.Icon(ft.Icons.HELP_OUTLINE if r.get("pending") else
                        (ft.Icons.CHECK_CIRCLE_OUTLINE if r.get("ok") else ft.Icons.BLOCK),
                        size=16, color=ACCENT if r.get("ok") else MUTED),
                ft.Column([ft.Text(muse_powers.describe(r), size=12, color=TEXT),
                           muted(self._ago(r.get("at")), 10)], spacing=1, tight=True,
                          expand=True),
                right,
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        header = [
            ft.Container(ft.Icon(ft.Icons.SMART_TOY_OUTLINED, size=19, color=ACCENT_DARK),
                         width=38, height=38, border_radius=12, bgcolor=ACCENT_SOFT,
                         alignment=ft.alignment.center),
            ft.Column([h(f"Your agent is {name}", 17),
                       muted(f"It works on this Beacon from outside, through its own secure "
                             f"connection, with the same powers as an agent here. Talk to it "
                             f"in {name}. Its latest changes are listed below. Undo is "
                             f"available when Beacon has enough information to reverse one.",
                             11)], spacing=1, tight=True, expand=True),
            self.pill(seen, ACCENT2, ft.Icons.SCHEDULE),
        ]
        if OUTSIDE_AGENT_URL:
            header.append(ft.OutlinedButton(f"Open {name}", icon=ft.Icons.OPEN_IN_NEW,
                                            on_click=self._open_url(OUTSIDE_AGENT_URL)))
        return self.card(
            ft.Row(header, spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            h(f"Changes made by {name}", 14),
            muted(f"Showing the latest {min(8, len(records))} of {len(records)} recorded changes.", 10),
            *(rows or [muted(f"{name} hasn't changed anything yet.", 12)]),
        )

    def _outside_undo(self, change_id: str) -> None:
        busy = self.__dict__.setdefault("_outside_undoing", set())
        if change_id in busy:
            return
        busy.add(change_id)
        self._render()                       # the button shows "Undoing…" and stays disabled

        def work():
            try:
                powers = muse_powers.MusePowers.for_beacon(
                    self._get_agent(), db_path=os.environ["YOUTUBE_INDEX_DB"],
                    changes_path=muse_powers.CHANGES_FILE,
                    read_config=sweep_control._read_watchlist_config)
                return powers.undo(change_id)
            except Exception as exc:  # noqa: BLE001 — reported, and the button comes back
                return False, {"error": f"{type(exc).__name__}: {exc}"}

        def done(result):
            busy.discard(change_id)
            ok, out = result
            self.toast("Undone." if ok else f"Couldn't undo: {out.get('error', '')[:140]}")
            self._render()

        self.run_bg(work, done, "Undoing that change…", screen="Agent")

    def _agent_backend_label(self) -> str:
        """Which model answers, in plain words. Building the agent is cheap (no network
        until a turn runs), but a misconfigured .env must not stop the page rendering."""
        try:
            agent = self._get_agent()
            ok, _why = agent.ready()
            return agent.describe() if ok else "No AI set up"
        except Exception:  # noqa: BLE001 — a label must never break the Agent screen
            return "Model unavailable"

    # -- AI provider: which model answers, switchable without touching code ----------
    def _ai_provider_row(self) -> ft.Control:
        """Pick the AI that answers, and prove it actually works.

        "Set up" only means the settings are filled in. The Azure and AWS setups both had
        their keys in .env long after the accounts stopped working, so a filled-in
        setting is never shown as working — only a real reply from Test is.
        """
        try:
            providers, use = beacon_ai.load()
            current = beacon_ai.choose("agent", providers=providers, use=use)
        except Exception as exc:  # noqa: BLE001 — a bad settings file must not break the tab
            return muted(f"AI provider settings could not be read: {exc}", 11)
        options = []
        for name, prov in providers.items():
            ready, _ = beacon_ai.check(prov)
            options.append(ft.dropdown.Option(
                key=name, text=prov.title + ("" if ready else "  · needs setup")))
        chosen = providers.get(current)
        ready, why = beacon_ai.check(chosen) if chosen else (False, "unknown provider")
        testing = getattr(self, "_ai_testing", None)
        result = (getattr(self, "_ai_tests", None) or {}).get(current)
        if testing == current:
            status, color, icon = "Sending a test message…", MUTED, ft.Icons.HOURGLASS_TOP
        elif result:
            worked, message, at = result
            status = f"{message} · tested {self._ago_seconds(time.time() - at)}"
            color, icon = ((ACCENT_DARK, ft.Icons.CHECK_CIRCLE) if worked
                           else (WARN, ft.Icons.ERROR_OUTLINE))
        elif ready:
            status, color, icon = ("Settings filled in — press Test to confirm it answers.",
                                   MUTED, ft.Icons.HELP_OUTLINE)
        else:
            status, color, icon = why, WARN, ft.Icons.SETTINGS_SUGGEST
        return ft.Row([
            ft.Dropdown(
                label="AI provider", value=current, options=options, width=330,
                border_color=BORDER, dense=True,
                on_change=lambda e: self._ai_pick(e.control.value)),
            ft.OutlinedButton(
                "Testing…" if testing else "Test",
                icon=ft.Icons.NETWORK_CHECK,
                disabled=bool(testing),
                on_click=lambda _, n=current: self._ai_test(n),
                style=ft.ButtonStyle(side=ft.BorderSide(1, BORDER),
                                     shape=ft.RoundedRectangleBorder(radius=11)),
            ),
            ft.Row([
                ft.Icon(icon, size=15, color=color),
                ft.Text(status, size=11, color=color, selectable=True, expand=True),
            ], spacing=6, expand=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _ai_pick(self, name: str | None) -> None:
        if not name:
            return
        try:
            beacon_ai.save_choice("agent", name)
        except OSError as exc:
            self.toast(f"Could not save the choice: {exc}")
            return
        title = beacon_ai.get_provider(name).title
        # The conversation is kept: history is provider-neutral, so the new model simply
        # continues it.
        note = ""
        try:
            was_on = self.autopilot.enabled
            if self.autopilot.apply_provider_policy():
                note = " Autopilot stays on (prepaid credits)." if was_on else \
                       " Autopilot turned on (prepaid credits)."
            elif self.autopilot.enabled:
                # Never switched off behind their back — but say whose usage it now spends.
                note = f" Autopilot is ON and will now run on {title}."
        except Exception:  # noqa: BLE001 — the pick itself must always succeed
            pass
        self.toast(f"{title} will answer your next question.{note}")
        self._render()

    def _ai_test(self, name: str) -> None:
        if getattr(self, "_ai_testing", None):
            return
        self._ai_testing = name

        def work():
            try:
                return beacon_ai.try_provider(beacon_ai.get_provider(name))
            except Exception as exc:  # noqa: BLE001 — shown next to the button
                return False, f"{type(exc).__name__}: {exc}"

        def done(res):
            self._ai_testing = None
            if not hasattr(self, "_ai_tests"):
                self._ai_tests = {}
            worked, message = res
            self._ai_tests[name] = (bool(worked), str(message), time.time())
            self.toast("")
            if self.section == "Agent":
                self._render()

        self.run_bg(work, done, f"Testing {beacon_ai.get_provider(name).title}…")
        self._render()

    # -- app-view control the agent can drive (edit the Channels view, switch tabs) --
    def agent_open_view(self, view=None) -> dict:
        valid = {"Overview", "Channels", "Real Time", "Find", "Saved", "Agent"}
        v = next((x for x in valid if x.lower() == str(view or "").strip().lower()), None)
        if not v:
            return {"error": f"unknown view '{view}'", "valid": sorted(valid)}
        self._go(v)
        return {"opened": v}

    def agent_show_channels(self, *, topic=None, niche_name=None, faceless_only=None,
                            min_subs=None, max_subs=None, min_avg_views=None,
                            max_avg_views=None, min_outlier_score=None,
                            max_days_since_start=None, video_format=None,
                            sort=None) -> dict:
        """Apply filters to the Channels tab + switch to it so the user SEES the result."""
        chosen_sort = "opportunity_score" if sort in (None, "outlier_score") else sort
        filt: dict = {"limit": 60, "sort": chosen_sort}
        for k, v in (("min_subs", min_subs), ("max_subs", max_subs),
                     ("min_avg_views", min_avg_views), ("max_avg_views", max_avg_views),
                     ("max_days_since_start", max_days_since_start),
                     ("video_format", video_format)):
            if v is not None:
                filt[k] = v
        if min_outlier_score is not None:
            filt["_min_outlier"] = float(min_outlier_score)
        self.ch_filters = filt
        self.ch_chip = "Custom"
        self.ch_topic = self._topic_or_none(topic)
        self.ch_shown = CH_PAGE
        self.ch_niche = None
        self.ch_view = "index"
        if niche_name:
            niches = self.client.call("list_niches") or []
            if isinstance(niches, dict):
                niches = niches.get("data", [])
            match = next((n for n in niches
                          if str(n.get("name", "")).strip().lower()
                          == str(niche_name).strip().lower()), None)
            if match:
                self.ch_niche, self.ch_view = match, "niche"
        self.section = "Channels"
        self._render()
        applied = {k: v for k, v in filt.items() if k != "limit"}
        if self.ch_topic:
            applied["topic"] = self.ch_topic
        self.toast(f"Agent filtered Channels — {applied}")
        return {"opened": "Channels", "applied": applied,
                "niche": (self.ch_niche or {}).get("name")}

    def agent_clear_channels(self) -> dict:
        self.ch_filters = {"sort": "opportunity_score", "limit": 60}
        self.ch_topic = None
        self.ch_niche = None
        self.ch_view = "index"
        self.ch_chip = "Custom"
        self.ch_shown = CH_PAGE
        self.section = "Channels"
        self._render()
        return {"opened": "Channels", "cleared": True}

    def agent_data_changed(self, _tool_name: str) -> None:
        """Drop cached reads after a successful Agent/Autopilot data mutation."""
        self._invalidate_all()

    def _agent_event(self, e: dict) -> None:
        """Streamed step from the agent loop (runs on the worker thread)."""
        t = e.get("type")
        if t == "text":
            self.agent_log.append({"kind": "agent", "text": e.get("text", "")})
        elif t == "tool":
            self.agent_log.append({"kind": "tool", "name": e.get("name", ""),
                                   "input": e.get("input") or {}})
        elif t == "tool_result":
            # what the tool actually DID (count touched, success/failure) — so a real
            # change to the index is never invisible and errors show a red flag.
            self.agent_log.append({"kind": "tool_result", "name": e.get("name", ""),
                                   "status": e.get("status", "success"),
                                   "output": e.get("output")})
        else:
            return
        if self.section == "Agent":
            try:
                self._render()
            except Exception:  # noqa: BLE001
                pass

    def _agent_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        # Gate on "an agent turn is already running", NOT on the global busy flag. The
        # two are different things: `_busy` is set by ANY background work, including the
        # 3-second cloud-import poller — so the old check made the whole Agent tab
        # unusable for the entire length of an import, silently refusing every question.
        # Asking a question while data loads in the background is perfectly safe; asking
        # a SECOND question while the first is still thinking is what we must refuse.
        if getattr(self, "_agent_running", False):
            self.toast("Beacon is still working on your last question.")
            return
        self._agent_running = True
        self._agent_draft = ""          # accepted — stop restoring it on the next render
        self.agent_log.append({"kind": "user", "text": text})
        self._render()
        agent = self._get_agent()

        def done(r):
            # Backend errors + the round-limit message are RETURNED by run() (not streamed
            # via on_event), so without this the agent just appears to freeze silently.
            # Skip when it equals the already-streamed final text (avoid a duplicate bubble).
            already = (self.agent_log and self.agent_log[-1].get("kind") == "agent"
                       and self.agent_log[-1].get("text") == r)
            if isinstance(r, str) and r.strip() and not already:
                self.agent_log.append({"kind": "agent", "text": r})
            self.toast("")
            if self.section == "Agent":
                self._render()

        def work():
            try:
                return agent.run(text, on_event=self._agent_event)
            finally:
                # Cleared HERE, not in done(): run_bg skips on_done when the work raises,
                # so a single crash would otherwise leave the flag stuck and the tab
                # refusing every later question until Beacon is restarted.
                self._agent_running = False

        self.run_bg(work, on_done=done, start_msg="Agent is thinking…")

    _TOOL_NAMES = {
        "count_channels": "Counted channels", "find_channels": "Found channels",
        "recent_channels": "Newest channels", "list_topics": "Listed topics",
        "list_niches": "Listed niches", "db_stats": "Read index stats",
        "get_channel_details": "Read channel details", "get_niche_channels": "Read niche channels",
        "find_rising_videos": "Found rising videos", "discover_channels": "Discovered channels",
        "radar_status": "Checked scraper status", "scraper_status": "Checked scraper status",
        "exclude_channels": "Hid channels", "exclude_by_filter": "Hid channels by filter",
        "run_junk_filter": "Cleaned junk", "unexclude_channels": "Restored channels",
        "reclassify_topics": "Reclassified topics", "set_channel_topic": "Set channel topic",
        "create_niche": "Created niche", "add_channel_to_niche": "Added to niche",
        "crawl_niche": "Deep-swept a niche", "deep_sweep": "Deep-swept a niche",
        "start_scraper": "Started the scraper", "stop_scraper": "Stopped the scraper",
        "add_watchlist_queries": "Added searches", "remove_watchlist_queries": "Removed searches",
        "get_watchlist_queries": "Read searches", "set_scraper_config": "Updated scraper config",
        "show_channels": "Opened Channels view", "open_view": "Switched view",
        "clear_channel_filters": "Cleared Channels filters",
    }

    def _tool_pretty_name(self, name: str) -> str:
        return self._TOOL_NAMES.get(name, str(name).replace("_", " ").capitalize())

    def _tool_result_summary(self, name: str, status, output) -> str:
        """Plain-language, count-carrying summary of what a tool DID (green) or the error
        it hit (red) — so the agent's actions are legible, not raw JSON."""
        pretty = self._tool_pretty_name(name)
        if status == "error":
            msg = str(output.get("error")) if isinstance(output, dict) else ""
            return f"{pretty} failed: {msg[:140]}" if msg else f"{pretty} failed."
        o = output
        if isinstance(o, list):
            if len(o) == 1 and isinstance(o[0], dict) and "count" in o[0]:
                return f"{pretty} — {int(o[0]['count']):,} channels"
            return f"{pretty} — {len(o):,} result{'s' if len(o) != 1 else ''}"
        if isinstance(o, dict):
            for k, verb in (("count", "channels"), ("excluded", "channels hidden"),
                            ("unexcluded", "channels restored"), ("updated", "channels updated"),
                            ("added", "added"), ("removed", "removed"), ("created", "created"),
                            ("hidden", "channels hidden")):
                v = o.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return f"{pretty} — {int(v):,} {verb}"
            if o.get("opened"):
                return f"{pretty} — opened {o['opened']}"
            if o.get("niche") or o.get("niche_id"):
                return f"{pretty} — {o.get('niche') or o.get('niche_id')}"
        return f"{pretty} — done"

    def _markdown(self, value: str, *, size: int = 12) -> ft.Markdown:
        return ft.Markdown(
            value=value or "",
            selectable=True,
            shrink_wrap=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            md_style_sheet=ft.MarkdownStyleSheet(
                p_text_style=ft.TextStyle(
                    size=size,
                    color=TEXT,
                    height=1.35,
                ),
                h1_text_style=ft.TextStyle(
                    size=size + 5,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                    font_family=DISPLAY_FONT,
                ),
                h2_text_style=ft.TextStyle(
                    size=size + 3,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                    font_family=DISPLAY_FONT,
                ),
                h3_text_style=ft.TextStyle(
                    size=size + 1,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                ),
                strong_text_style=ft.TextStyle(
                    size=size,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                ),
                code_text_style=ft.TextStyle(size=size - 1, color=ACCENT2),
                block_spacing=6,
                list_indent=18,
            ),
        )

    def _agent_bubble(self, m: dict) -> ft.Control:
        kind = m.get("kind")
        if kind == "user":
            return ft.Row([ft.Container(
                ft.Text(m.get("text", ""), color=TEXT, selectable=True),
                bgcolor=ACCENT2_SOFT,
                padding=ft.padding.symmetric(9, 13), border_radius=12)],
                alignment=ft.MainAxisAlignment.END)
        if kind == "tool":
            inp = m.get("input") or {}
            detail = json.dumps(inp, indent=2, default=str) if inp else "No parameters."
            return ft.Container(
                ft.ExpansionTile(
                    title=ft.Text(
                        self._tool_pretty_name(m.get("name", "")),
                        size=11,
                        color=TEXT,
                        weight=ft.FontWeight.W_700,
                    ),
                    subtitle=ft.Text(
                        "Tool used · expand for technical details",
                        size=9,
                        color=MUTED,
                    ),
                    leading=ft.Icon(ft.Icons.BUILD, size=14, color=ACCENT2),
                    controls=[
                        ft.Container(
                            ft.Text(
                                detail,
                                size=10,
                                color=MUTED,
                                selectable=True,
                            ),
                            padding=ft.padding.only(left=36, right=12, bottom=8),
                        )
                    ],
                    dense=True,
                    tile_padding=ft.padding.symmetric(3, 8),
                    controls_padding=0,
                    collapsed_bgcolor=ACCENT2_SOFT,
                    bgcolor=ACCENT2_SOFT,
                    collapsed_shape=ft.RoundedRectangleBorder(radius=10),
                    shape=ft.RoundedRectangleBorder(radius=10),
                ),
                border_radius=10,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            )
        if kind == "tool_result":
            ok = m.get("status") != "error"
            summary = self._tool_result_summary(m.get("name", ""), m.get("status"),
                                                m.get("output"))
            col = ACCENT if ok else WARN
            return ft.Row(
                [ft.Icon(ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR_OUTLINE,
                         size=14, color=col),
                 ft.Text(summary, size=11, color=col, selectable=True, expand=True)],
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.START)
        return ft.Row([ft.Container(
            self._markdown(m.get("text", ""), size=12),
            bgcolor=SURFACE_ALT,
            padding=ft.padding.symmetric(9, 13), border_radius=12, expand=True)],
            alignment=ft.MainAxisAlignment.START)

    def _render_agent(self) -> None:
        agent = self._get_agent()
        ok, why = agent.ready()
        if OUTSIDE_AGENT:
            self.content.controls.append(self._outside_agent_card())
        elif not ok:
            self.content.controls.append(self._error_card(
                f"Agent needs setup: {why}. Add it to Beacon's .env "
                f"({beacon_ai.ENV_PATH}) and press Test — no restart needed — or "
                "pick another AI provider below."))
        # With an outside agent and no in-app AI, asking here cannot work — say why calmly
        # and keep the box disabled, rather than letting a question fail.
        optional_only = bool(OUTSIDE_AGENT) and not ok

        # The draft survives re-renders. _render() rebuilds every control, and background
        # work re-renders on its own schedule (the cloud poller ticks every 3s during an
        # import) — so without this, a half-typed question is silently wiped out from
        # under the user mid-sentence and the box appears to reset itself.
        self._agent_input = ft.TextField(
            hint_text="Ask about a topic, channel pattern, count, or system state…",
            value=getattr(self, "_agent_draft", ""),
            expand=True,
            border_color=BORDER,
            bgcolor=SURFACE_ALT,
            disabled=optional_only,
            on_change=lambda e: setattr(self, "_agent_draft", e.control.value),
            on_submit=lambda e: self._agent_send(e.control.value))
        send = ft.FilledButton(
            "Ask Beacon",
            icon=ft.Icons.SEND,
            disabled=optional_only,
            on_click=lambda _: self._agent_send(self._agent_input.value),
            style=ft.ButtonStyle(
                bgcolor=ACCENT,
                color=NAV_BG,
                shape=ft.RoundedRectangleBorder(radius=11),
                padding=ft.padding.symmetric(12, 18),
            ),
        )
        self.content.controls.append(self.card(
            ft.Row([
                ft.Container(
                    ft.Icon(ft.Icons.AUTO_AWESOME, size=19, color=ACCENT_DARK),
                    width=38,
                    height=38,
                    border_radius=12,
                    bgcolor=ACCENT_SOFT,
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    h("Ask Beacon", 17),
                    muted(
                        (f"Optional: an AI inside Beacon. None is set up on this machine — "
                         f"{OUTSIDE_AGENT} is your agent. Pick a provider below only if you "
                         f"add one.") if optional_only else
                        "It can investigate, organize, and control approved workflows.",
                        11,
                    ),
                ], spacing=1, tight=True, expand=True),
                # Name the model that actually answers. Beacon can run on many providers,
                # and "which one replied?" is the first question when an answer looks
                # off — so it should never be a guess.
                *([] if optional_only else [
                    self.pill(
                        self._agent_backend_label(),
                        ACCENT,
                        ft.Icons.MEMORY,
                    ),
                    self.pill(
                        "Writes stay reversible",
                        ACCENT2,
                        ft.Icons.HISTORY,
                    ),
                ]),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            *self._ask_card_body(optional_only, send),
        ))

        self.content.controls.append(self.section_heading(
            "Autonomous work",
            "One ledger for collection, niche upkeep, Hermes, and Beacon Autopilot.",
            ft.Icons.HUB_OUTLINED,
        ))
        self.content.controls.append(self._autonomous_desk_card())

        clear = ft.OutlinedButton(
            "Clear log",
            icon=ft.Icons.CLEAR_ALL,
            on_click=lambda _: (
                self.agent_log.clear(),
                self._get_agent().reset(),
                self._render(),
            ),
            style=ft.ButtonStyle(
                color=MUTED,
                side=ft.BorderSide(1, BORDER),
                shape=ft.RoundedRectangleBorder(radius=11),
            ),
        )
        transcript_header = ft.Row([
            ft.Icon(ft.Icons.FORUM_OUTLINED, size=18, color=ACCENT2),
            h("Investigation log", 16),
            self.pill(
                f"{len(self.agent_log)} events",
                ACCENT2 if self.agent_log else MUTED,
            ),
            ft.Container(expand=True),
            clear,
        ], spacing=9, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        if self.agent_log:
            transcript = ft.ListView(
                [self._agent_bubble(m) for m in self.agent_log],
                spacing=10,
                auto_scroll=True,
                height=300,
                padding=6,
            )
        else:
            examples = ft.Row([
                ft.Container(
                    ft.Text(
                        "How many faceless History channels are under 5K subs?",
                        size=10,
                        color=ACCENT2,
                    ),
                    padding=ft.padding.symmetric(6, 10),
                    border_radius=18,
                    bgcolor=ACCENT2_SOFT,
                ),
                ft.Container(
                    ft.Text(
                        "What is the scraper doing right now?",
                        size=10,
                        color=ACCENT2,
                    ),
                    padding=ft.padding.symmetric(6, 10),
                    border_radius=18,
                    bgcolor=ACCENT2_SOFT,
                ),
                ft.Container(
                    ft.Text(
                        "Show the strongest new opportunities.",
                        size=10,
                        color=ACCENT2,
                    ),
                    padding=ft.padding.symmetric(6, 10),
                    border_radius=18,
                    bgcolor=ACCENT2_SOFT,
                ),
            ], spacing=7, wrap=True, alignment=ft.MainAxisAlignment.CENTER)
            transcript = ft.Container(
                ft.Column([
                    ft.Container(
                        ft.Icon(ft.Icons.TRAVEL_EXPLORE, size=26, color=ACCENT2),
                        width=52,
                        height=52,
                        border_radius=17,
                        bgcolor=ACCENT2_SOFT,
                        alignment=ft.alignment.center,
                    ),
                    h("Start with an investigation", 17),
                    muted(
                        "Ask in plain language. Beacon will show every tool it "
                        "uses and what changed.",
                        11,
                    ),
                    examples,
                ], spacing=10,
                   horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                   tight=True),
                height=180,
                alignment=ft.alignment.center,
            )

        self.content.controls.append(self.card(
            transcript_header,
            ft.Divider(color=BORDER, height=1),
            transcript,
        ))
        self.content.controls.append(self.section_heading(
            "Safe automation",
            "Optional maintenance while Beacon remains open.",
            ft.Icons.AUTO_MODE,
        ))
        self.content.controls.append(self._autopilot_card())

    @staticmethod
    def _pass_kind(entry: dict) -> str:
        """A light pass reads what is new; a deep one re-sweeps the whole index."""
        kind = str(entry.get("kind") or "")
        label = {"light": "Light check", "deep": "Full sweep"}.get(kind, "Maintenance pass")
        # A failed pass did nothing — never let it read like a completed sweep.
        return f"{label} (failed)" if BeaconApp._pass_failed(entry) else label

    @staticmethod
    def _pass_failed(entry: dict) -> bool:
        """Newer entries record `ok`; older ones only say so in their summary ("⚠️ …")."""
        return entry.get("ok") is False or beacon_autopilot.pass_failed(
            str(entry.get("summary") or ""))

    @staticmethod
    def _pass_running_text(since: float | None, kind: str | None = None) -> str:
        """A pass can run for minutes, so say how long it has been going — and which kind,
        since a full sweep is expected to take ~10 minutes and a light check far less."""
        what = {"light": "running a light check…",
                "deep": "running a full sweep of the index…"}.get(
                    kind or "", "running a maintenance pass…")
        if not since:
            return what
        secs = max(0, int(time.time() - since))
        if secs < 45:
            return f"{what} started just now"
        spent = f"{secs // 60}m" if secs >= 60 else f"{secs}s"
        return f"{what} {spent} so far"

    PASS_TICK_SECONDS = 15

    def _start_pass_ticker(self) -> None:
        """Keep "…8m so far" moving while a pass runs.

        The line is computed when the card is drawn, and nothing redraws the card during a
        pass — so a 10-minute full sweep said "started just now" the whole time (seen live
        2026-09-19). This updates only that one line; the page is not rebuilt.
        """
        ticker = getattr(self, "_pass_ticker", None)
        if ticker is not None and ticker.is_alive():
            return
        self._pass_ticker = threading.Thread(target=self._tick_pass_line, daemon=True)
        self._pass_ticker.start()

    def _tick_pass_line(self) -> None:
        stop = self._session_stop
        while not stop.wait(self.PASS_TICK_SECONDS):
            ap = getattr(self, "autopilot", None)
            if ap is None or not ap.running_now:
                return        # the finishing pass redraws the card itself
            line = getattr(self, "_pass_line", None)
            if line is None or self.section != "Agent":
                continue
            value = self._pass_running_text(getattr(ap, "running_since", None),
                                            getattr(ap, "running_kind", None))
            if line.value != value:
                line.value = value
                try:
                    line.update()
                except Exception:  # noqa: BLE001 — replaced by a redraw: the next one is used
                    pass

    @staticmethod
    def _pass_outcome(entry: dict) -> tuple[bool, str]:
        """(failed, the error line) — a pass that failed must not wear a tick. The title
        already says "(failed)", so the subtitle shows the error itself."""
        if not BeaconApp._pass_failed(entry):
            return False, ""
        summary = str(entry.get("summary") or "").strip()
        first = summary.splitlines()[0] if summary else "no reply from the AI provider"
        return True, first.lstrip("⚠️ ").strip()

    @staticmethod
    def _pass_took(entry: dict) -> str:
        """' · took 12m' — the logged time is when the pass STARTED, and a pass that reads
        the whole index can run for minutes, so the duration belongs next to it."""
        secs = entry.get("took_secs")
        if not isinstance(secs, (int, float)) or secs <= 0:
            return ""
        return f" · took {int(secs) // 60}m" if secs >= 60 else f" · took {int(secs)}s"

    def _autopilot_card(self) -> ft.Control:
        ap = self.autopilot
        on = ap.enabled
        badge = self.pill(
            "On" if on else "Off",
            ACCENT if on else MUTED,
            ft.Icons.RADIO_BUTTON_CHECKED if on else ft.Icons.PAUSE_CIRCLE_OUTLINE,
        )
        seg = ft.SegmentedButton(
            allow_multiple_selection=False, allow_empty_selection=False,
            selected={"on" if on else "off"},
            segments=[ft.Segment(value="off", label=ft.Text("Off")),
                      ft.Segment(value="on", label=ft.Text("On"))],
            on_change=lambda e: self._autopilot_toggle("on" in e.control.selected))
        self._ap_interval = ft.TextField(
            label="Every (min)", value=str(ap.interval), width=110, dense=True,
            border_color=BORDER, text_size=13, keyboard_type=ft.KeyboardType.NUMBER)
        run_btn = ft.OutlinedButton(
            "Run now",
            icon=ft.Icons.PLAY_ARROW,
            disabled=ap.running_now,
            on_click=lambda _: self._autopilot_run_now(),
            style=ft.ButtonStyle(
                color=ACCENT_DARK,
                side=ft.BorderSide(1, ACCENT),
                shape=ft.RoundedRectangleBorder(radius=11),
            ),
        )
        if ap.running_now:
            running = muted(self._pass_running_text(
                getattr(ap, "running_since", None), getattr(ap, "running_kind", None)), 11)
            self._pass_line = running
            self._start_pass_ticker()
            status = ft.Row([ft.ProgressRing(width=13, height=13, stroke_width=2, color=ACCENT),
                             running], spacing=8)
        elif on:
            secs = ap.seconds_to_next()
            when = f"next run in ~{secs // 60}m {secs % 60}s" if secs is not None else "on"
            try:
                _goal, _tools, kind, why = ap.plan()
                when += f" · {'full sweep' if kind == 'deep' else 'light check'} — {why}"
            except Exception:  # noqa: BLE001 — a label must not break the card
                pass
            status = muted(when, 11)
        else:
            status = muted("off — turn on to run safe maintenance on a timer", 11)
        reason = getattr(ap, "auto_reason", "")
        policy = muted(
            f"{reason} · to switch it on and off yourself, set "
            "BEACON_FOUNDRY_AUTOPILOT=manual in Beacon/.env", 10) if reason else None
        items = []
        for e in ap.log[:3]:
            raw_tools = [t for t in e.get("tools", []) if t]
            pretty_tools = [self._tool_pretty_name(t) for t in raw_tools]
            tools = " · ".join(pretty_tools[:4])
            if len(pretty_tools) > 4:
                tools += f" · +{len(pretty_tools) - 4} more"
            query_lines = []
            for change in e.get("query_changes", []) or []:
                accepted = list(change.get("accepted") or [])
                rejected = list(change.get("rejected") or [])
                if accepted:
                    query_lines.append(
                        f"Queries added: {', '.join(accepted)} (total {change.get('total')})")
                if rejected:
                    query_lines.append(
                        "Queries rejected: " + ", ".join(
                            f"{item.get('query')} [{item.get('reason')}]"
                            for item in rejected[:3]))
            detail_controls: list[ft.Control] = []
            if query_lines:
                detail_controls.append(ft.Column(
                    [ft.Text(line, size=10, color=MUTED) for line in query_lines],
                    spacing=3,
                    tight=True,
                ))
            detail_controls.append(self._markdown(e.get("summary", ""), size=11))
            failed, problem = self._pass_outcome(e)
            items.append(ft.Container(
                ft.ExpansionTile(
                    title=ft.Text(
                        f"{self._pass_kind(e)} · {e.get('at', '')}"
                        f"{self._pass_took(e)}",
                        size=11,
                        color=TEXT,
                        weight=ft.FontWeight.W_700,
                    ),
                    subtitle=ft.Text(
                        problem or tools or "Maintenance completed",
                        size=9,
                        color=WARN if failed else MUTED,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    leading=ft.Icon(
                        ft.Icons.ERROR_OUTLINE if failed else ft.Icons.CHECK_CIRCLE_OUTLINE,
                        size=16,
                        color=WARN if failed else ACCENT,
                    ),
                    controls=[
                        ft.Container(
                            ft.Column(
                                detail_controls,
                                spacing=7,
                                tight=True,
                            ),
                            padding=ft.padding.only(
                                left=40,
                                right=12,
                                bottom=10,
                            ),
                        )
                    ],
                    dense=True,
                    tile_padding=ft.padding.symmetric(5, 9),
                    controls_padding=0,
                    collapsed_bgcolor=SURFACE_ALT,
                    bgcolor=SURFACE_ALT,
                    collapsed_shape=ft.RoundedRectangleBorder(radius=10),
                    shape=ft.RoundedRectangleBorder(radius=10),
                ),
                border_radius=10,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ))
        log_col = (ft.Column(items, spacing=8, tight=True) if items
                   else muted("No runs yet — turn On or hit Run now.", 11))
        return self.card(
            ft.Row([
                    ft.Container(
                        ft.Icon(ft.Icons.AUTO_MODE, color=ACCENT_DARK, size=18),
                        width=36,
                        height=36,
                        border_radius=11,
                        bgcolor=ACCENT_SOFT,
                        alignment=ft.alignment.center,
                    ),
                    h("Autopilot", 16),
                    badge],
                   spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row(
                [seg, self._ap_interval, run_btn],
                spacing=10,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            muted("Runs bounded maintenance on a timer, records each pass, and never "
                  "deletes channel or video rows.", 11),
            policy or ft.Container(height=0),
            ft.Container(
                status,
                padding=ft.padding.symmetric(9, 11),
                border_radius=10,
                bgcolor=ACCENT_SOFT if (on or ap.running_now) else SURFACE_ALT,
            ),
            ft.Divider(color=BORDER, height=1),
            muted("Recent activity", 11),
            log_col)

    def _autopilot_toggle(self, on: bool) -> None:
        iv = None
        try:
            if getattr(self, "_ap_interval", None) and self._ap_interval.value:
                iv = int(self._ap_interval.value)
        except (ValueError, TypeError):
            iv = None
        self.autopilot.set_enabled(bool(on), iv)
        self.toast("Autopilot ON — first pass shortly." if on else "Autopilot off.")
        self._render()

    def _autopilot_run_now(self) -> None:
        ok, why = self._get_agent().ready()
        if not ok:
            self.toast(f"Agent not configured: {why}")
            return
        self.autopilot.run_now()
        self.toast("Autopilot: running a maintenance pass…")
        self._render()

    # -- card / table builders ---------------------------------------------
    def section_heading(self, title: str, subtitle: str, icon) -> ft.Control:
        return ft.Row([
            ft.Container(
                ft.Icon(icon, size=17, color=ACCENT2),
                width=34,
                height=34,
                border_radius=11,
                bgcolor=ACCENT2_SOFT,
                alignment=ft.alignment.center,
            ),
            ft.Column([
                ft.Text(
                    title,
                    size=17,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                ),
                ft.Text(subtitle, size=11, color=MUTED),
            ], spacing=1, tight=True),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def card(self, *controls) -> ft.Container:
        return ft.Container(
            ft.Column(list(controls), spacing=12, tight=True),
            padding=20,
            bgcolor=CARD,
            border_radius=14,
            shadow=SHADOW,
        )

    def stat_grid(
        self,
        stats: dict,
        keys: list[str],
        *,
        cell_col: dict | None = None,
    ) -> ft.ResponsiveRow:
        cell_col = cell_col or {"sm": 6, "md": 3, "lg": 3}
        cells = []
        for k in keys:
            if k not in stats:
                continue
            v = stats[k]
            v = f"{v:,}" if isinstance(v, int) else str(v)
            cells.append(ft.Container(
                ft.Column([
                    ft.Text(
                        v,
                        size=20,
                        weight=ft.FontWeight.W_700,
                        color=TEXT,
                    ),
                    ft.Text(
                        k.replace("_", " "),
                        size=10,
                        color=MUTED,
                    ),
                ], spacing=2, tight=True),
                col=cell_col,
                padding=ft.padding.symmetric(11, 13),
                border_radius=12,
                bgcolor=SURFACE_ALT,
            ))
        return ft.ResponsiveRow(cells, columns=12, spacing=10, run_spacing=10)

    def table(self, rows: list[dict], cols: list[str], *, limit: int = 25) -> ft.Control:
        if not rows:
            return muted("Nothing here yet.")
        present = [c for c in cols if any(c in r for r in rows)] or list(rows[0].keys())[:5]
        header = ft.Row([ft.Container(ft.Text(c.replace("_", " "), size=11,
                                              weight=ft.FontWeight.BOLD, color=MUTED),
                                      width=180 if c in ("title", "name", "channel") else 110)
                         for c in present], spacing=6)
        body = [header]
        for r in rows[:limit]:
            cells = []
            for c in present:
                val = r.get(c, "")
                if isinstance(val, float):
                    val = f"{val:,.1f}"
                elif isinstance(val, int):
                    val = f"{val:,}"
                else:
                    val = str(val)[:60]
                cells.append(ft.Container(ft.Text(val, size=12, color=TEXT),
                                          width=180 if c in ("title", "name", "channel") else 110))
            body.append(ft.Container(ft.Row(cells, spacing=6),
                                     padding=ft.padding.symmetric(5, 0),
                                     border=ft.border.only(
                                         top=ft.border.BorderSide(1, BORDER))))
        return ft.Column(body, spacing=2, scroll=ft.ScrollMode.AUTO)

    # -- card grid builders (thumbnails + badges, NexLev-style) -------------
    CARD_W = 380          # two-up by default, three-up when the window is wide
    THUMB_H = 214         # 380 * 9/16 -> proper 16:9 video thumbnail

    def pill(self, text: str, color: str = ACCENT, icon=None) -> ft.Container:
        """A small rounded stat badge, e.g. '👁 948K' or '🔥 74.3'."""
        row = []
        if icon is not None:
            row.append(ft.Icon(icon, size=13, color=color))
        row.append(ft.Text(text, size=11, color=color, weight=ft.FontWeight.BOLD))
        return ft.Container(
            ft.Row(row, spacing=4, tight=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(3, 8), border_radius=20,
            bgcolor=ft.Colors.with_opacity(0.11, color),
            border=ft.border.all(1, ft.Colors.with_opacity(0.18, color)))

    def _shell(self, *children, on_click=None, fluid: bool = False) -> ft.Container:
        """The common card frame: fixed width, rounded, clipped, bordered.
        Pass on_click to make the whole card a clickable link (with a hover ripple)."""
        return ft.Container(
            ft.Column(list(children), spacing=0, tight=True),
            width=None if fluid else self.CARD_W,
            bgcolor=CARD,
            border_radius=14,
            shadow=SHADOW,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ink=bool(on_click), on_click=on_click,
            tooltip="Open on YouTube" if on_click else None)

    def _open_url(self, url: str):
        """Return a click handler that opens `url` in the default browser."""
        def handler(_):
            if not url:
                self.toast("No YouTube link for this item.")
                return
            try:
                self.page.launch_url(url)
            except Exception as ex:  # noqa: BLE001
                self.toast(f"Couldn't open link: {str(ex)[:120]}")
        return handler

    def _thumb_placeholder(self) -> ft.Container:
        return ft.Container(
            ft.Icon(ft.Icons.IMAGE_NOT_SUPPORTED, size=34, color=MUTED),
            width=self.CARD_W, height=self.THUMB_H, bgcolor=SURFACE_ALT,
            alignment=ft.alignment.center)

    def _avatar(self, name: str, size: int = 30, avatar_url=None) -> ft.Container:
        """A circular channel avatar. Uses the real profile image when we have
        one, and falls back to a coloured letter circle (also if the image fails)."""
        letter = (name or "?").strip()[:1].upper() or "?"
        color = AVATAR_COLORS[ord(letter) % len(AVATAR_COLORS)]
        letter_circle = ft.Container(
            ft.Text(letter, size=max(12, int(size * 0.46)),
                    weight=ft.FontWeight.BOLD, color="#FFFFFF"),
            width=size, height=size, border_radius=size / 2, bgcolor=color,
            alignment=ft.alignment.center)
        if avatar_url:
            return ft.Container(
                ft.Stack([
                    letter_circle,
                    ft.Image(
                        src=avatar_url,
                        width=size,
                        height=size,
                        fit=ft.ImageFit.COVER,
                        error_content=ft.Container(),
                    ),
                ], width=size, height=size),
                width=size, height=size, border_radius=size / 2,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS)
        return letter_circle

    def _attach_avatars(self, *row_lists) -> None:
        """Fill r['avatar_url'] for every row with a channel_id, via ONE batched
        finder call. Runs on the worker thread; silently skips on failure."""
        ids = [r["channel_id"] for rows in row_lists for r in (rows or [])
               if r.get("channel_id")]
        if not ids:
            return
        try:
            avatars = self.client.call("get_channel_avatars",
                                       channel_ids=list(dict.fromkeys(ids)))
        except FinderError:
            return
        for rows in row_lists:
            for r in (rows or []):
                url = (avatars or {}).get(r.get("channel_id"))
                if url:
                    r["avatar_url"] = url

    def video_card(self, r: dict) -> ft.Container:
        vid = str(r.get("video_id") or "")
        video_url = r.get("url") or (
            f"https://www.youtube.com/watch?v={vid}" if vid else "")
        thumb = r.get("thumbnail_url") or (
            f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None)
        title = str(r.get("title") or "Untitled")
        channel = str(r.get("channel") or r.get("channel_name") or "—")
        dur = fmt_duration(r.get("duration_seconds"))
        views = human_num(r.get("views"))
        score = r.get("outlier_score")
        vph = r.get("observed_views_per_hour") or 0

        # thumbnail with a YouTube-style duration badge overlaid bottom-right
        image = (ft.Image(src=thumb, width=self.CARD_W, height=self.THUMB_H,
                          fit=ft.ImageFit.COVER,
                          error_content=self._thumb_placeholder())
                 if thumb else self._thumb_placeholder())
        overlay = [image]
        if dur:
            overlay.append(ft.Container(
                ft.Text(dur, size=10, color="#ffffff", weight=ft.FontWeight.BOLD),
                right=6, bottom=6, padding=ft.padding.symmetric(1, 5),
                border_radius=4, bgcolor=ft.Colors.with_opacity(0.82, "#000000")))
        thumb_stack = ft.Stack(overlay, width=self.CARD_W, height=self.THUMB_H)

        stats = [self.pill(views, ACCENT2, ft.Icons.VISIBILITY)] if views else []
        if score:
            stats.append(self.pill(f"{float(score):.1f}", ACCENT,
                                   ft.Icons.LOCAL_FIRE_DEPARTMENT))
        if vph and float(vph) > 0:
            stats.append(self.pill(f"{human_num(vph)}/h", "#fcd34d",
                                   ft.Icons.TRENDING_UP))

        body = ft.Container(
            ft.Column([
                ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=TEXT,
                        max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Row([self._avatar(channel, 30, r.get("avatar_url")),
                        ft.Text(channel, size=12, color=MUTED, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS, expand=True)],
                       spacing=7),
                ft.Row(stats, spacing=6, wrap=True),
            ], spacing=8, tight=True),
            padding=12)
        return self._shell(thumb_stack, body, on_click=self._open_url(video_url))

    @staticmethod
    def _fmt_age(days) -> str:
        """days_since_start -> compact age. '312d' up to ~2y, then '3.1y'."""
        try:
            d = int(days)
        except (TypeError, ValueError):
            return "—"
        if d <= 0:
            return "—"
        return f"{d:,}d" if d < 730 else f"{d / 365:.1f}y"

    @staticmethod
    def _ago_seconds(secs: float) -> str:
        """'just now' / '4m ago' / '2h ago' from a plain elapsed-seconds value."""
        secs = max(0.0, float(secs or 0))
        if secs < 45:
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"

    @staticmethod
    def _ago(iso_ts) -> str:
        """ISO-8601 UTC stamp (e.g. 2026-07-09T12:30:14.246709Z) -> 'just now' /
        'Xm ago' / 'Xh ago' / 'Xd ago'. Blank on parse failure / missing."""
        if not iso_ts:
            return ""
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            secs = int(max(0, (datetime.now(timezone.utc) - dt).total_seconds()))
        except (ValueError, TypeError):
            return ""
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    @staticmethod
    def _outlier_level(score):
        """Map an outlier score to (colour, filled-fraction 0..1). Higher = better,
        so the band goes muted (<1x, underperforming) -> blue (1-2x) -> green (>=2x)."""
        try:
            s = float(score)
        except (TypeError, ValueError):
            return MUTED, 0.0
        color = ACCENT if s >= 2 else (ACCENT2 if s >= 1 else MUTED)
        return color, max(0.0, min(1.0, s / 8.0))   # 8x fills the whole meter

    def _stat_tile(self, label: str, value: str, icon, tint: str = MUTED) -> ft.Container:
        """One compact metric inside a shared evidence band."""
        return ft.Container(
            ft.Column([
                ft.Row([ft.Icon(icon, size=13, color=tint), muted(label, 11)],
                       spacing=5, tight=True,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(
                    value or "—",
                    size=17,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                    font_family=DISPLAY_FONT,
                ),
            ], spacing=3, tight=True),
            padding=ft.padding.symmetric(5, 7),
            expand=True,
        )

    @staticmethod
    def _number(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _opportunity_evidence(self, r: dict) -> tuple[str, str, list[tuple[str, str, object]]]:
        uploads = int(self._number(r.get("video_count", r.get("indexed_videos"))))
        age = int(self._number(r.get("days_since_start")))
        avg = self._number(r.get("avg_views", r.get("average_views")))
        subs = self._number(r.get("subscribers"))
        leverage = avg / max(subs, 1.0)

        if uploads >= 30:
            strength = "Strong evidence"
        elif uploads >= 12:
            strength = "Growing evidence"
        else:
            strength = "Early evidence"

        if leverage >= 10:
            leverage_label = "Exceptional view leverage"
        elif leverage >= 2:
            leverage_label = "Strong view leverage"
        else:
            leverage_label = "Above-baseline reach"

        if 0 < age <= 90:
            age_label = "New channel"
        elif 0 < age <= 365:
            age_label = "Recent channel"
        else:
            age_label = "Established channel"

        sample_text = (
            f"{uploads:,} uploads"
            if uploads
            else "Upload sample unavailable"
        )
        age_text = (
            f"{age:,} days old"
            if age
            else "Age unavailable"
        )
        rationale = (
            f"{sample_text} provide the performance sample; "
            f"{leverage_label.lower()} and a {age_label.lower()} "
            "support the opportunity score."
        )
        factors = [
            ("Performance sample", sample_text, ft.Icons.VIDEO_LIBRARY_OUTLINED),
            (leverage_label, f"{human_num(avg)} average views", ft.Icons.TRENDING_UP),
            (age_label, age_text, ft.Icons.CALENDAR_MONTH_OUTLINED),
        ]
        return strength, rationale, factors

    def _opportunity_evidence_band(self, r: dict, *, saved: bool = False) -> ft.Control:
        strength, rationale, factors = self._opportunity_evidence(r)
        if saved:
            rationale = (
                "These numbers describe reach. This saved channel has not passed "
                "the faceless and repeatable opportunity checks."
            )
        factor_controls = []
        for title, detail, icon in factors:
            factor_controls.append(ft.Container(
                ft.Row([
                    ft.Icon(icon, size=14, color=ACCENT2),
                    ft.Column([
                        ft.Text(
                            title,
                            size=11,
                            color=TEXT,
                            weight=ft.FontWeight.W_700,
                        ),
                        ft.Text(detail, size=10, color=MUTED),
                    ], spacing=0, tight=True),
                ], spacing=6),
                padding=ft.padding.symmetric(5, 7),
            ))
        return ft.Container(
            ft.Column([
                ft.Row([
                    ft.Text(
                        "Performance signals" if saved else "Why it qualifies",
                        size=11,
                        color=TEXT,
                        weight=ft.FontWeight.W_700,
                    ),
                    ft.Container(expand=True),
                    self.pill(strength, ACCENT2, ft.Icons.FACT_CHECK_OUTLINED),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row(factor_controls, wrap=True, spacing=4, run_spacing=4),
                ft.Text(rationale, size=11, color=MUTED),
            ], spacing=6, tight=True),
            padding=ft.padding.symmetric(9, 10),
            border_radius=10,
            bgcolor=SURFACE_ALT,
        )

    def channel_card(self, r: dict, niche_id=None, tracked=None,
                     actions: list | None = None, *, saved: bool = False) -> ft.Container:
        name = str(r.get("title") or r.get("channel") or r.get("channel_id") or "—")
        cid = str(r.get("channel_id") or "")
        channel_url = r.get("channel_url") or r.get("url") or (
            f"https://www.youtube.com/channel/{cid}" if cid else "")
        handle = str(r.get("handle") or "")
        category = str(r.get("category") or "")
        subs = human_num(r.get("subscribers"))
        avg = human_num(r.get("avg_views", r.get("average_views")))
        vids = human_num(r.get("video_count", r.get("indexed_videos")))
        score = r.get("opportunity_score")
        if score is None:  # backward-compatible fallback for older finder services
            score = r.get("outlier_score")
        days = r.get("days_since_start")
        faceless = r.get("faceless") or r.get("likely_faceless")

        topic = r.get("topic")
        subtitle = topic or handle or category or "channel"
        title_col = ft.Column([
            ft.Text(name, size=15, weight=ft.FontWeight.W_700, color=TEXT,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(subtitle, size=11, color=(ACCENT2 if topic else MUTED),
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        ], spacing=2, tight=True, expand=True)
        score_color, _ = self._outlier_level(score)
        score_text = f"{float(score):.1f}×" if score else "—"
        score_block = ft.Container(
            ft.Column([
                ft.Text(
                    score_text,
                    size=18,
                    color=score_color,
                    weight=ft.FontWeight.W_700,
                    font_family=DISPLAY_FONT,
                ),
                ft.Text("ranking score" if saved else "opportunity", size=10, color=MUTED),
            ], spacing=0, tight=True,
               horizontal_alignment=ft.CrossAxisAlignment.END),
            padding=ft.padding.symmetric(7, 10),
            border_radius=11,
            bgcolor=ft.Colors.with_opacity(0.10, score_color),
        )
        header = ft.Row(
            [
                self._avatar(name, 46, r.get("avatar_url")),
                title_col,
                score_block,
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        metric_controls = [
            self._stat_tile("Avg views", avg, ft.Icons.VISIBILITY, ACCENT),
            ft.Container(width=1, height=42, bgcolor=BORDER),
            self._stat_tile("Subscribers", subs, ft.Icons.PEOPLE, ACCENT2),
            ft.Container(width=1, height=42, bgcolor=BORDER),
            self._stat_tile("Uploads", vids, ft.Icons.VIDEO_LIBRARY, MUTED),
            ft.Container(width=1, height=42, bgcolor=BORDER),
            self._stat_tile(
                "Age",
                self._fmt_age(days),
                ft.Icons.CALENDAR_MONTH,
                MUTED,
            ),
        ]
        tiles = ft.Container(
            ft.Row(
                metric_controls,
                spacing=2,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(5, 7),
            border_radius=10,
            bgcolor=SURFACE_ALT,
        )

        # freshness badge — only present on Real Time rows (they carry added_at)
        col = []
        ago = self._ago(r.get("added_at"))
        if ago:
            col.append(ft.Row([
                self.pill(
                    f"Discovered {ago}",
                    ACCENT2,
                    ft.Icons.SCHEDULE,
                ),
                ft.Container(expand=True),
                self.pill("new signal", ACCENT, ft.Icons.BOLT),
            ]))
        qualification = (
            "Saved · review fit" if saved else
            ("Faceless + replicable" if faceless else "Replicable evidence")
        )
        col += [
            header,
            ft.Row([
                self.pill(
                    qualification,
                    ACCENT2 if saved else ACCENT,
                    ft.Icons.BOOKMARK_OUTLINE if saved else ft.Icons.VERIFIED_OUTLINED,
                ),
                ft.Container(expand=True),
                ft.Icon(ft.Icons.OPEN_IN_NEW, size=14, color=MUTED),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(color=BORDER, height=1),
            tiles,
            self._opportunity_evidence_band(r, saved=saved),
        ]

        if actions:
            # Rendered INSIDE the card but after the evidence, so the card still reads as one
            # object. Buttons handle their own taps, so hitting Remove does not also fire the
            # card's open-on-YouTube click.
            col.append(ft.Row(actions, spacing=8, wrap=True))

        body = ft.Container(ft.Column(col, spacing=12, tight=True), padding=16)
        return self._shell(
            body,
            on_click=self._open_url(channel_url),
            fluid=True,
        )

    def card_grid(self, rows: list[dict], kind: str = "video",
                  *, limit: int = 24, niche_id=None, tracked=None,
                  tracked_ids=None, actions_for=None, saved: bool = False) -> ft.Control:
        if not rows:
            return muted("Nothing here yet.")
        if kind == "video":
            cards = [self.video_card(r) for r in rows[:limit]]
            return ft.Row(cards, wrap=True, spacing=14, run_spacing=14)
        else:
            def _tracked(r):
                if tracked_ids is not None:      # per-row: is it in the niche?
                    return r.get("channel_id") in tracked_ids
                return tracked
            cards = [self.channel_card(r, niche_id=niche_id, tracked=_tracked(r),
                                       actions=(actions_for(r) if actions_for else None),
                                       saved=saved)
                     for r in rows[:limit]]
            for card in cards:
                card.col = {"xs": 12, "sm": 12, "md": 6, "lg": 6, "xl": 6}
            return ft.ResponsiveRow(
                cards,
                columns=12,
                spacing=14,
                run_spacing=14,
            )

    # -- data cache (fast tab switches, with bounded freshness) -------------
    @staticmethod
    def _cache_entry(result: dict) -> dict:
        stamped = dict(result)
        stamped["_cached_at"] = time.monotonic()
        stamped["_cached_wall"] = time.time()
        return stamped

    @staticmethod
    def _cache_ttl(key: str) -> int:
        if key in CACHE_TTL_SECONDS:
            return CACHE_TTL_SECONDS[key]
        for prefix, ttl in CACHE_TTL_SECONDS.items():
            if prefix.endswith(":") and key.startswith(prefix):
                return ttl
        return 300

    def _cache_expired(self, key: str, entry: dict) -> bool:
        cached_at = entry.get("_cached_at")
        if not isinstance(cached_at, (int, float)):
            return True
        ttl = CACHE_ERROR_TTL_SECONDS if "error" in entry else self._cache_ttl(key)
        return time.monotonic() - float(cached_at) >= ttl

    def _cache_updated_text(self, key: str) -> str:
        wall = (self.cache.get(key) or {}).get("_cached_wall")
        if not isinstance(wall, (int, float)):
            return "waiting for first update"
        return f"updated {time.strftime('%H:%M:%S', time.localtime(wall))}"

    def _ensure(self, key: str, loader, msg: str = "Loading…"):
        """Return a fresh cached entry, or refresh one stale entry in the background.

        A stale value remains visible while its replacement loads. Initial loads return
        None. Entry shape: {"data": value} or {"error": message}, plus timestamps.
        """
        keys = getattr(self, "_render_keys", None)
        if keys is not None:
            keys.add(key)                   # this screen displays `key` (see _shows)
        entry = self.cache.get(key)
        expired = entry is not None and self._cache_expired(key, entry)
        if entry is not None and not expired:
            return entry
        if key not in self._loading:
            self._loading.add(key)
            gen = self._epoch.get(key, 0)   # snapshot; if it changes, we were invalidated

            def safe_load():
                try:
                    return {"data": loader()}
                except Exception as ex:  # noqa: BLE001 — surfaced as an error card
                    return {"error": str(ex)[:200]}

            def done(result):
                self._loading.discard(key)
                if self._epoch.get(key, 0) != gen:
                    # invalidated mid-flight → drop this stale result and let the
                    # next render kick off a fresh load instead of caching old data.
                    if self._shows(key):
                        self._render()
                    return
                previous = self.cache.get(key)
                self.cache[key] = self._cache_entry(result)
                # Redraw only when the user can see the difference. Every finished load used
                # to rebuild the whole current page, even loads for other tabs and refreshes
                # that returned exactly what was already shown.
                unchanged = (previous is not None and "error" not in previous
                             and "error" not in result
                             and previous.get("data") == result.get("data"))
                if self._shows(key) and not unchanged:
                    self._render()  # re-render → now a cache hit, renders instantly
                # Transient-empty self-heal: a browse view (channels/realtime) that came
                # back with ZERO rows is often a momentary hiccup (finder busy while the
                # scraper writes, or a just-shifted time window). Silently re-check ONCE
                # a few seconds later; if rows appeared, swap them in — no infinite loop
                # (one recheck per key until it's invalidated) and genuine empties stay empty.
                data = result.get("data")
                if (isinstance(data, list) and not data and key not in self._rechecked
                        and (key.startswith("channels:") or key.startswith("realtime:"))):
                    self._rechecked.add(key)

                    def recheck():
                        time.sleep(4)
                        if self._epoch.get(key, 0) != gen:
                            return
                        # The loader reads LIVE filter state, so if the user changed the
                        # filter/window in the last 4s its result belongs to a DIFFERENT key.
                        # Bail unless our key is still the section's active one (mirrors _rt_tick),
                        # or we'd cache the new filter's rows under this stale key.
                        live = (self._channels_key() if key.startswith("channels:")
                                else self._rt_key())
                        if live != key:
                            return
                        try:
                            fresh = loader()
                        except Exception:  # noqa: BLE001
                            return
                        if isinstance(fresh, list) and fresh and self._epoch.get(key, 0) == gen:
                            self.cache[key] = self._cache_entry({"data": fresh})
                            if self.section in ("Channels", "Real Time"):
                                try:
                                    self._render()
                                except Exception:  # noqa: BLE001
                                    pass
                    threading.Thread(target=recheck, daemon=True).start()

            if entry is not None:
                # Old values stay on screen while they refresh: no loading signal at all.
                self.run_bg(safe_load, done, msg, quiet=True)
            else:
                self.run_bg(safe_load, done, msg, screen=getattr(self, "section", None))
        return entry

    def _shows(self, key: str) -> bool:
        """Does the screen in front of the user display `key`? Waits for a render in progress
        so a fast load cannot miss the screen that is still being drawn."""
        lock = getattr(self, "_render_lock", None)
        if lock is None:
            return True
        with lock:
            here = getattr(self, "section", None)
            return key in getattr(self, "_section_keys", {}).get(here, ())

    def _invalidate(self, *keys: str) -> None:
        for k in keys:
            self.cache.pop(k, None)
            self._rechecked.discard(k)                    # allow a fresh transient-empty recheck
            self._epoch[k] = self._epoch.get(k, 0) + 1  # void any in-flight load

    def _invalidate_all(self) -> None:
        """Clear everything AND void every in-flight load (used after a deep sweep,
        which changes data broadly)."""
        for k in set(self.cache) | set(self._loading):
            self._epoch[k] = self._epoch.get(k, 0) + 1
        self.cache.clear()
        self._rechecked.clear()

    def _refresh(self, _=None) -> None:
        """Re-pull the current section's data (clears just its cache keys)."""
        if self.section == "Overview":
            self._invalidate("overview", "edge_sync", "niche_signals")
        elif self.section == "Channels":
            self._invalidate("niches", "topics")
            self._invalidate(self._channels_key())
            if self._ch_niche_id() is not None:
                self._invalidate(f"niche_signals:{self._ch_niche_id()}")
        elif self.section == "Real Time":
            self._invalidate("topics")
            self._invalidate(self._rt_key())
        elif self.section == "Find":
            self._invalidate(self._find_key())
        elif self.section == "Saved":
            self._invalidate(self._saved_key())
        elif self.section == "Agent":
            self._invalidate("edge_sync", "niche_signals")
        self._render()

    def _loading_note(self, msg: str) -> ft.Control:
        return ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2, color=ACCENT),
                       muted(msg)], spacing=10)

    def _error_card(self, msg: str) -> ft.Control:
        return self.card(
            ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, color=WARN, size=18),
                    ft.Text("Couldn't load this.", color=WARN, weight=ft.FontWeight.BOLD)],
                   spacing=8),
            muted(msg),
            ft.OutlinedButton("Retry", icon=ft.Icons.REFRESH, on_click=self._refresh))

    # -- render -------------------------------------------------------------
    def _page_header(self) -> ft.Control:
        descriptions = {
            "Overview": "Index health, research coverage, and discovery operations.",
            "Channels": "Compare faceless formats with repeatable performance evidence.",
            "Real Time": "Watch newly discovered opportunities as they enter the index.",
            "Find": "Launch a focused sweep, then review the strongest formats.",
            "Saved": "Channels you found yourself, analysed on the same evidence.",
            "Agent": "Investigate, organize, and safely maintain the index with AI.",
        }
        connection = "Local finder" if self.hosting else "Finder connected"
        return ft.Row([
            ft.Column([
                ft.Text(
                    self.section,
                    size=28,
                    weight=ft.FontWeight.W_700,
                    color=TEXT,
                    font_family=DISPLAY_FONT,
                ),
                ft.Text(
                    descriptions.get(self.section, ""),
                    size=12,
                    color=MUTED,
                ),
            ], spacing=3, tight=True, expand=True),
            self.pill(connection, ACCENT, ft.Icons.LINK),
            ft.OutlinedButton(
                "Refresh",
                icon=ft.Icons.REFRESH,
                on_click=self._refresh,
                style=ft.ButtonStyle(
                    color=MUTED,
                    side=ft.BorderSide(1, BORDER),
                    shape=ft.RoundedRectangleBorder(radius=11),
                    padding=ft.padding.symmetric(10, 14),
                ),
            ),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _render(self) -> None:
        # Serialized: many daemon threads call _render(); without the lock two rebuilds
        # of self.content.controls interleave and produce duplicated/missing cards.
        with self._render_lock:
            if getattr(self, "_session_stop", None) is not None and self._session_stop.is_set():
                return
            self._rendering = True
            self.content.controls = []
            self.content.controls.append(self._page_header())
            self._render_keys = set()
            try:
                if self.section == "Overview":
                    self._render_overview()
                elif self.section == "Channels":
                    self._render_channels()
                elif self.section == "Real Time":
                    self._render_realtime()
                elif self.section == "Find":
                    self._render_find()
                elif self.section == "Saved":
                    self._render_saved()
                elif self.section == "Agent":
                    self._render_agent()
            except Exception:  # noqa: BLE001 - preserve navigation and show recovery
                logging.exception("Beacon could not draw the %s screen", self.section)
                self.content.controls = [
                    self._page_header(),
                    self._error_card("This screen hit a drawing error. Press Retry to reload it."),
                ]
            finally:
                self._section_keys[self.section] = self._render_keys
                self._render_keys = None
                # Publish the navigation and content together, only after the
                # screen has finished building (or its recovery card is ready).
                self._nav_col.controls = self._nav_tiles()
                self._rendering = False
            try:
                self.page.update()
            except Exception:  # noqa: BLE001
                logging.exception("Beacon could not publish the %s screen", self.section)
            self._restore_scroll()

    # -- scroll position across background re-renders ---------------------------------
    # _render() replaces every control, which sends the view back to the top. That is
    # correct when the USER navigates, and wrong when a background load re-renders
    # underneath them — the cloud importer, Autopilot and the niche/edge cards all
    # finish on their own schedule, and each one used to yank a half-read answer back to
    # the top. So remember where they were and put them back, but only within the same
    # screen: switching tabs should always start at the top.
    def _on_scroll(self, e) -> None:
        try:
            self._scroll_offset = max(0.0, float(e.pixels))
            self._scroll_section = self.section
        except (TypeError, ValueError):   # a scroll event without a usable position
            pass

    def _restore_scroll(self) -> None:
        offset = getattr(self, "_scroll_offset", 0.0)
        if offset <= 0 or getattr(self, "_scroll_section", None) != self.section:
            return
        try:
            # duration=0 → no animation, so it reads as "never moved" rather than a jump
            self.content.scroll_to(offset=offset, duration=0)
        except Exception:  # noqa: BLE001 — never let scroll restore break a render
            pass

    @staticmethod
    def _niche_signal_source(signal: dict) -> str:
        evidence = signal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        source = str(evidence.get("source") or "").strip()
        if source:
            return source.replace("-", " ").replace("_", " ").title()
        if str(signal.get("entity_type") or "").lower() == "operator":
            return "Hermes"
        return "Radar"

    @staticmethod
    def _niche_signal_url(signal: dict) -> str:
        evidence = signal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        for key in ("url", "video_url", "channel_url", "youtube_url"):
            url = str(evidence.get(key) or "").strip()
            if url.startswith(("https://", "http://")):
                return url
        entity_id = str(signal.get("entity_id") or "").strip()
        entity_type = str(signal.get("entity_type") or "").lower()
        if entity_id and entity_type == "video":
            return f"https://www.youtube.com/watch?v={entity_id}"
        if entity_id and entity_type == "channel":
            return f"https://www.youtube.com/channel/{entity_id}"
        return ""

    @staticmethod
    def _niche_signal_tone(signal: dict) -> str:
        severity = str(signal.get("severity") or "info").lower()
        if severity == "high":
            return WARN
        if severity == "medium":
            return AMBER
        if severity == "info":
            return ACCENT2
        return MUTED

    def _load_niche_signals(self, *, niche_id=None, limit: int = 12) -> dict:
        return self.client.call(
            "list_niche_signals",
            niche_id=niche_id,
            unread_only=True,
            days=30,
            limit=limit,
        )

    def _invalidate_niche_signals(self) -> None:
        keys = {
            key
            for key in set(self.cache) | set(self._loading)
            if key == "niche_signals" or key.startswith("niche_signals:")
        }
        if not keys:
            keys.add("niche_signals")
        self._invalidate(*keys)

    def _open_niche_workspace(self, workspace: dict) -> None:
        niche_id = workspace.get("id") or workspace.get("niche_id")
        if niche_id is None:
            self.toast("That niche workspace is no longer available.")
            return
        self.ch_niche = dict(workspace)
        self.ch_view = "niche"
        self.ch_shown = CH_PAGE
        self.section = "Channels"
        self._render()
        self.toast(f"Opened niche workspace “{workspace.get('name') or niche_id}”.")

    def _mark_niche_signal_read(self, signal: dict) -> None:
        self._set_niche_signal_read(signal, True)

    def _undo_last_niche_signal_read(self, _=None) -> None:
        signal = getattr(self, "_last_read_signal", None)
        if not signal:
            self.toast("There is no recent read notification to restore.")
            return
        self._set_niche_signal_read(signal, False)

    def _set_niche_signal_read(self, signal: dict, read: bool) -> None:
        try:
            alert_id = int(signal.get("id"))
        except (TypeError, ValueError):
            self.toast("That notification has no valid alert ID.")
            return
        action = f"alert:{alert_id}:{'read' if read else 'unread'}"
        actions = getattr(self, "_niche_actions", set())
        self._niche_actions = actions
        if action in actions:
            return
        actions.add(action)
        self._render()

        def work():
            try:
                return self.client.call(
                    "mark_niche_alert_read",
                    alert_ids=[alert_id],
                    read=bool(read),
                )
            except Exception as ex:  # noqa: BLE001
                return {"error": str(ex)[:200]}

        def done(result):
            actions.discard(action)
            if isinstance(result, dict) and result.get("error"):
                verb = "mark the notification read" if read else "restore the notification"
                self.toast(f"Couldn't {verb}: {result['error']}")
            else:
                changed = int((result or {}).get("changed") or 0)
                if read:
                    if changed:
                        self._last_read_signal = dict(signal)
                    self.toast(
                        "Notification marked read."
                        if changed
                        else "Notification was already read."
                    )
                else:
                    if int((getattr(self, "_last_read_signal", {}) or {}).get("id") or -1) == alert_id:
                        self._last_read_signal = None
                    self.toast(
                        "Notification restored to unread."
                        if changed
                        else "Notification was already unread."
                    )
                self._invalidate_niche_signals()
            self._render()

        progress = "Marking notification read…" if read else "Restoring notification…"
        self.run_bg(work, done, progress)

    def _show_more_niche_signals(self, _=None) -> None:
        shown = int(getattr(self, "niche_signal_shown", 6) or 6)
        self.niche_signal_shown = max(6, shown) + 6
        self._invalidate("niche_signals")
        self._render()

    def _toggle_niche_workspaces(self, _=None) -> None:
        self.niche_workspace_expanded = not bool(
            getattr(self, "niche_workspace_expanded", False)
        )
        self._render()

    def _set_niche_autonomous(self, workspace: dict, enabled: bool) -> None:
        try:
            niche_id = int(workspace.get("id") or workspace.get("niche_id"))
        except (TypeError, ValueError):
            self.toast("That niche workspace is no longer available.")
            return
        action = f"autonomy:{niche_id}"
        actions = getattr(self, "_niche_actions", set())
        self._niche_actions = actions
        if action in actions:
            return
        actions.add(action)
        self._render()

        def work():
            try:
                return self.client.call(
                    "set_niche_autonomous",
                    niche_id=niche_id,
                    enabled=bool(enabled),
                )
            except Exception as ex:  # noqa: BLE001
                return {"error": str(ex)[:200]}

        def done(result):
            actions.discard(action)
            if isinstance(result, dict) and result.get("error"):
                self.toast(f"Couldn't change niche upkeep: {result['error']}")
            else:
                self.toast(
                    "Automatic niche upkeep resumed."
                    if enabled
                    else "Automatic niche upkeep paused. You can resume it anytime."
                )
                self._invalidate_niche_signals()
                self._invalidate("niches")
            self._render()

        label = "Resuming" if enabled else "Pausing"
        self.run_bg(work, done, f"{label} niche upkeep…")

    def _niche_signal_row(self, signal: dict, *, compact: bool = False) -> ft.Control:
        tone = self._niche_signal_tone(signal)
        source = self._niche_signal_source(signal)
        kind = str(signal.get("alert_type") or "signal").replace("_", " ").title()
        severity = str(signal.get("severity") or "info").replace("_", " ").title()
        niche = str(signal.get("niche_name") or "Niche workspace")
        title = str(signal.get("title") or "Niche signal")
        message = str(signal.get("message") or "")
        age = self._ago(signal.get("detected_at"))
        url = self._niche_signal_url(signal)
        workspace = {
            "id": signal.get("niche_id"),
            "name": niche,
        }
        action_prefix = f"alert:{signal.get('id')}:"
        busy = any(
            str(action).startswith(action_prefix)
            for action in getattr(self, "_niche_actions", set())
        )
        meta: list[ft.Control] = [
            self.pill(niche, ACCENT2, ft.Icons.WORKSPACES_OUTLINE),
            self.pill(f"{source} · {kind} · {severity} severity", tone),
        ]
        if age:
            meta.append(ft.Text(age, size=10, color=MUTED))
        actions: list[ft.Control] = [
            ft.TextButton(
                "Open workspace",
                icon=ft.Icons.WORKSPACES_OUTLINE,
                on_click=lambda _, w=workspace: self._open_niche_workspace(w),
                style=ft.ButtonStyle(color=ACCENT2),
            ),
        ]
        if url:
            actions.append(ft.TextButton(
                "Open evidence",
                icon=ft.Icons.OPEN_IN_NEW,
                on_click=self._open_url(url),
                style=ft.ButtonStyle(color=ACCENT_DARK),
            ))
        actions.append(ft.TextButton(
            "Mark read",
            icon=ft.Icons.MARK_EMAIL_READ_OUTLINED,
            disabled=busy,
            on_click=lambda _, s=signal: self._mark_niche_signal_read(s),
            style=ft.ButtonStyle(color=MUTED),
        ))
        body: list[ft.Control] = [
            ft.Row(meta, wrap=True, spacing=6, run_spacing=5),
            ft.Text(
                title,
                size=12 if compact else 13,
                color=TEXT,
                weight=ft.FontWeight.W_700,
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ]
        if message:
            body.append(ft.Text(
                message,
                size=10 if compact else 11,
                color=MUTED,
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
            ))
        body.append(ft.Row(actions, wrap=True, spacing=2, run_spacing=2))
        return ft.Container(
            ft.Column(body, spacing=5 if compact else 7, tight=True),
            padding=ft.padding.symmetric(9 if compact else 11, 12),
            border_radius=12,
            bgcolor=SURFACE_ALT,
        )

    def _workspace_runway_row(self, workspace: dict) -> ft.Control:
        autonomous = bool(workspace.get("autonomous", 1))
        channels = int(workspace.get("enabled_channel_count")
                       or workspace.get("channel_count") or 0)
        videos = int(workspace.get("video_count") or 0)
        signals = int(workspace.get("signal_count") or 0)
        high = int(workspace.get("high_signal_count") or 0)
        activity = self._ago(workspace.get("last_activity_at")) or "no activity yet"
        description = str(workspace.get("description") or "")
        signal_text = f"{signals:,} unread"
        if high:
            signal_text += f" · {high:,} high"
        controls: list[ft.Control] = [
            ft.Row([
                ft.Text(
                    str(workspace.get("name") or "Unnamed workspace"),
                    size=12,
                    color=TEXT,
                    weight=ft.FontWeight.W_700,
                    expand=True,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                self.pill(
                    "Upkeep on" if autonomous else "Paused",
                    ACCENT if autonomous else AMBER,
                    ft.Icons.AUTO_MODE if autonomous else ft.Icons.PAUSE_CIRCLE_OUTLINE,
                ),
                ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16, color=MUTED),
            ], spacing=7, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        if description:
            controls.append(ft.Text(
                description,
                size=10,
                color=MUTED,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            ))
        controls.append(ft.Row([
            ft.Text(f"{channels:,} channels · {videos:,} videos", size=10, color=MUTED),
            ft.Text(signal_text, size=10, color=WARN if high else ACCENT2),
            ft.Text(f"last activity {activity}", size=10, color=MUTED),
        ], wrap=True, spacing=8, run_spacing=4))
        return ft.Container(
            ft.Column(controls, spacing=4, tight=True),
            padding=ft.padding.symmetric(9, 10),
            border_radius=12,
            bgcolor=SURFACE_ALT,
            ink=True,
            on_click=lambda _, w=workspace: self._open_niche_workspace(w),
            tooltip="Open this niche workspace",
        )

    def _niche_watch_section(self) -> list[ft.Control]:
        shown = max(6, int(getattr(self, "niche_signal_shown", 6) or 6))
        entry = self._ensure(
            "niche_signals",
            lambda: self._load_niche_signals(limit=max(12, shown)),
            "Loading niche watch…",
        )
        heading = self.section_heading(
            "Niche Watch",
            "Deduplicated alerts and the real size of every curated workspace.",
            ft.Icons.NOTIFICATIONS_ACTIVE_OUTLINED,
        )
        if entry is None:
            return [
                heading,
                self.card(
                    h("Watching your niches", 16),
                    self._loading_note("Loading niche notifications and workspace sizes…"),
                ),
            ]
        if "error" in entry:
            return [
                heading,
                self.card(
                    ft.Row([
                        ft.Icon(ft.Icons.ERROR_OUTLINE, color=WARN, size=18),
                        h("Niche Watch is temporarily unavailable", 16),
                    ], spacing=8),
                    muted(entry["error"]),
                    ft.OutlinedButton(
                        "Retry",
                        icon=ft.Icons.REFRESH,
                        on_click=self._refresh,
                    ),
                ),
            ]

        state = _niche_watch_state(entry.get("data"))
        signals = state["signals"][:shown]
        expanded = bool(getattr(self, "niche_workspace_expanded", False))
        workspaces = (
            state["workspaces"]
            if expanded
            else state["workspaces"][:5]
        )
        signal_header_controls: list[ft.Control] = [
            h("Unread signals", 16),
            self.pill(
                f"{state['signal_count']:,}",
                WARN if state["high_signal_count"] else ACCENT2,
                ft.Icons.NOTIFICATIONS_ACTIVE_OUTLINED,
            ),
            (
                self.pill(
                    f"{state['high_signal_count']:,} high",
                    WARN,
                    ft.Icons.PRIORITY_HIGH,
                )
                if state["high_signal_count"]
                else ft.Container()
            ),
        ]
        last_read = getattr(self, "_last_read_signal", None)
        if last_read:
            try:
                last_read_id = int(last_read.get("id"))
            except (TypeError, ValueError):
                last_read_id = -1
            undo_busy = any(
                str(action).startswith(f"alert:{last_read_id}:")
                for action in getattr(self, "_niche_actions", set())
            )
            signal_header_controls.append(ft.TextButton(
                "Undo last read",
                icon=ft.Icons.UNDO,
                disabled=undo_busy,
                on_click=self._undo_last_niche_signal_read,
                style=ft.ButtonStyle(color=ACCENT2),
            ))
        signal_header = ft.Row(
            signal_header_controls,
            spacing=8,
            wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        signal_controls: list[ft.Control] = [signal_header]
        freshness = state["freshness"]
        if freshness == "fresh_evidence_stale_alerts":
            signal_controls.append(ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=15, color=AMBER),
                    ft.Text(
                        "Workspace evidence is fresh, while the latest alert is "
                        f"{self._ago(state['latest_beacon_at'])}. That alone does not "
                        "mean collection stopped; check Discovery operations below.",
                        size=10,
                        color=AMBER,
                        expand=True,
                    ),
                ], spacing=7),
                padding=9,
                border_radius=10,
                bgcolor=AMBER_SOFT,
            ))
        elif freshness == "stale_workspace":
            signal_controls.append(ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=15, color=AMBER),
                    ft.Text(
                        "Niche alerts and workspace activity are both quiet. Check "
                        "Discovery operations below before treating the radar as stopped.",
                        size=10,
                        color=AMBER,
                        expand=True,
                    ),
                ], spacing=7),
                padding=9,
                border_radius=10,
                bgcolor=AMBER_SOFT,
            ))
        if signals:
            signal_controls.extend(
                self._niche_signal_row(signal) for signal in signals
            )
            if state["signal_count"] > len(signals):
                remaining = state["signal_count"] - len(signals)
                signal_controls.append(ft.OutlinedButton(
                    f"Show 6 more ({remaining:,} remaining)",
                    icon=ft.Icons.EXPAND_MORE,
                    on_click=self._show_more_niche_signals,
                ))
        else:
            signal_controls.append(ft.Container(
                ft.Column([
                    ft.Icon(ft.Icons.TASK_ALT, size=24, color=ACCENT),
                    h("All caught up", 15),
                    muted("No unread niche signals in the last 30 days.", 11),
                ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                padding=18,
                alignment=ft.alignment.center,
            ))
        signal_panel = self.card(*signal_controls)
        signal_panel.col = {"sm": 12, "md": 12, "lg": 12, "xl": 7}

        workspace_controls: list[ft.Control] = [
            ft.Row([
                h("Workspace runway", 16),
                ft.Container(expand=True),
                self.pill(
                    f"{len(state['workspaces']):,} niches",
                    ACCENT,
                    ft.Icons.WORKSPACES_OUTLINE,
                ),
            ], spacing=8),
            muted(
                "Channel and video counts come from each curated niche workspace, "
                "not from the Topic filter.",
                10,
            ),
        ]
        if workspaces:
            workspace_controls.extend(
                self._workspace_runway_row(workspace) for workspace in workspaces
            )
            if len(state["workspaces"]) > 5:
                workspace_controls.append(ft.TextButton(
                    (
                        "Show fewer workspaces"
                        if expanded
                        else f"Show all {len(state['workspaces']):,} workspaces"
                    ),
                    icon=ft.Icons.EXPAND_LESS if expanded else ft.Icons.EXPAND_MORE,
                    on_click=self._toggle_niche_workspaces,
                    style=ft.ButtonStyle(color=ACCENT2),
                ))
        else:
            workspace_controls.append(muted(
                "No niche workspaces yet. Create one through Agent, then organize "
                "channels into it.",
                11,
            ))
        workspace_panel = self.card(*workspace_controls)
        workspace_panel.col = {"sm": 12, "md": 12, "lg": 12, "xl": 5}
        return [
            heading,
            ft.ResponsiveRow(
                [signal_panel, workspace_panel],
                columns=12,
                spacing=16,
                run_spacing=16,
            ),
        ]

    def _niche_brief_card(self, niche_id) -> ft.Control:
        key = f"niche_signals:{niche_id}"
        entry = self._ensure(
            key,
            lambda: self._load_niche_signals(niche_id=niche_id, limit=3),
            "Loading niche workspace…",
        )
        if entry is None:
            return self.card(
                h("Niche workspace", 16),
                self._loading_note("Loading workspace size, upkeep, and signals…"),
            )
        if "error" in entry:
            return self.card(
                ft.Row([
                    ft.Icon(ft.Icons.ERROR_OUTLINE, color=WARN, size=18),
                    h("Couldn't load this niche workspace", 16),
                ], spacing=8),
                muted(entry["error"]),
                ft.OutlinedButton(
                    "Retry",
                    icon=ft.Icons.REFRESH,
                    on_click=self._refresh,
                ),
            )
        state = _niche_watch_state(entry.get("data"))
        workspace = (
            state["workspaces"][0]
            if state["workspaces"]
            else dict(self.ch_niche or {})
        )
        autonomous = bool(workspace.get("autonomous", 1))
        action = f"autonomy:{niche_id}"
        busy = action in getattr(self, "_niche_actions", set())
        name = str(workspace.get("name") or (self.ch_niche or {}).get("name") or niche_id)
        description = str(workspace.get("description") or "")
        activity = self._ago(workspace.get("last_activity_at")) or "—"
        signals = state["signals"][:3]
        body: list[ft.Control] = [
            ft.Row([
                ft.Container(
                    ft.Icon(ft.Icons.WORKSPACES_OUTLINE, size=19, color=ACCENT2),
                    width=38,
                    height=38,
                    border_radius=12,
                    bgcolor=ACCENT2_SOFT,
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    ft.Text("Niche workspace", size=10, color=MUTED),
                    h(name, 17),
                ], spacing=0, tight=True, expand=True),
                self.pill(
                    "Automatic upkeep on" if autonomous else "Upkeep paused",
                    ACCENT if autonomous else AMBER,
                    ft.Icons.AUTO_MODE if autonomous else ft.Icons.PAUSE_CIRCLE_OUTLINE,
                ),
                ft.OutlinedButton(
                    "Pause upkeep" if autonomous else "Resume upkeep",
                    icon=(
                        ft.Icons.PAUSE_CIRCLE_OUTLINE
                        if autonomous
                        else ft.Icons.PLAY_CIRCLE_OUTLINE
                    ),
                    disabled=busy,
                    on_click=lambda _, w=workspace, on=not autonomous:
                        self._set_niche_autonomous(w, on),
                    style=ft.ButtonStyle(
                        color=AMBER if autonomous else ACCENT_DARK,
                        side=ft.BorderSide(1, AMBER if autonomous else ACCENT),
                    ),
                ),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        if description:
            body.append(ft.Text(description, size=11, color=MUTED))
        body.extend([
            self.stat_grid(
                {
                    "tracked_channels": int(
                        workspace.get("enabled_channel_count")
                        or workspace.get("channel_count")
                        or 0
                    ),
                    "indexed_videos": int(workspace.get("video_count") or 0),
                    "unread_signals": state["signal_count"],
                    "last_activity": activity,
                },
                [
                    "tracked_channels",
                    "indexed_videos",
                    "unread_signals",
                    "last_activity",
                ],
                cell_col={"sm": 6, "md": 6, "lg": 6, "xl": 3},
            ),
            ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=14, color=ACCENT2),
                    ft.Text(
                        "This curated niche workspace is separate from the Topic "
                        "catalog filter above.",
                        size=10,
                        color=MUTED,
                        expand=True,
                    ),
                ], spacing=7),
                padding=9,
                border_radius=10,
                bgcolor=ACCENT2_SOFT,
            ),
        ])
        if signals:
            body.extend([
                ft.Divider(color=BORDER, height=1),
                h("Latest unread signals", 14),
                *[self._niche_signal_row(signal, compact=True) for signal in signals],
            ])
        else:
            body.append(muted("No unread signals for this workspace.", 11))
        return self.card(*body)

    def _automation_lane(
        self,
        *,
        icon,
        title: str,
        status: str,
        detail: str,
        tone: str,
    ) -> ft.Control:
        return ft.Container(
            ft.Row([
                ft.Container(
                    ft.Icon(icon, size=17, color=tone),
                    width=34,
                    height=34,
                    border_radius=11,
                    bgcolor=ft.Colors.with_opacity(0.11, tone),
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    ft.Row([
                        ft.Text(
                            title,
                            size=12,
                            color=TEXT,
                            weight=ft.FontWeight.W_700,
                        ),
                        self.pill(status, tone),
                    ], wrap=True, spacing=7),
                    ft.Text(
                        detail,
                        size=10,
                        color=MUTED,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ], spacing=3, tight=True, expand=True),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
            padding=ft.padding.symmetric(10, 11),
            border_radius=12,
            bgcolor=SURFACE_ALT,
        )

    def _autonomous_desk_card(self) -> ft.Control:
        niche_entry = self._ensure(
            "niche_signals",
            lambda: self._load_niche_signals(limit=12),
            "Loading autonomous work…",
        )
        edge_entry = self._ensure(
            "edge_sync",
            lambda: self.client.call("edge_sync_status"),
            "Checking discovery lanes…",
        )
        lanes: list[ft.Control] = []
        if edge_entry is None:
            lanes.append(self._automation_lane(
                icon=ft.Icons.CLOUD_SYNC,
                title="Discovery & organizer",
                status="Checking",
                detail="Reading the durable Oracle delivery and organization lanes.",
                tone=ACCENT2,
            ))
        elif "error" in edge_entry:
            lanes.append(self._automation_lane(
                icon=ft.Icons.ERROR_OUTLINE,
                title="Discovery & organizer",
                status="Unavailable",
                detail=str(edge_entry["error"]),
                tone=WARN,
            ))
        else:
            edge = edge_entry.get("data") or {}
            health = edge.get("health")
            health = health if isinstance(health, dict) else {}
            remote = edge.get("remote")
            remote = remote if isinstance(remote, dict) else {}
            organizer = edge.get("organizer")
            organizer = organizer if isinstance(organizer, dict) else {}
            last_sync = edge.get("last_sync")
            last_sync = last_sync if isinstance(last_sync, dict) else {}
            healthy = bool(health.get("ok"))
            queued = int(organizer.get("queued_jobs") or 0)
            synced = self._ago(last_sync.get("synced_at")) or "not yet"
            lanes.append(self._automation_lane(
                icon=ft.Icons.CLOUD_SYNC,
                title="Discovery & organizer",
                status="Delivering" if healthy else "Needs attention",
                detail=(
                    f"Oracle {remote.get('state') or 'unknown'} · organizer "
                    f"{organizer.get('state') or 'unknown'} · {queued:,} queued · "
                    f"last import {synced}. {health.get('summary') or ''}"
                ).strip(),
                tone=ACCENT if healthy else WARN,
            ))

        if niche_entry is None:
            lanes.append(self._automation_lane(
                icon=ft.Icons.WORKSPACES_OUTLINE,
                title="Niche upkeep",
                status="Checking",
                detail="Reading workspace sizes, automatic-upkeep state, and alerts.",
                tone=ACCENT2,
            ))
            lanes.append(self._automation_lane(
                icon=ft.Icons.SMART_TOY_OUTLINED,
                title="Hermes Niche Desk",
                status="Checking",
                detail="Looking for the latest bounded multi-agent brief.",
                tone=ACCENT2,
            ))
        elif "error" in niche_entry:
            detail = str(niche_entry["error"])
            lanes.append(self._automation_lane(
                icon=ft.Icons.ERROR_OUTLINE,
                title="Niche upkeep",
                status="Unavailable",
                detail=detail,
                tone=WARN,
            ))
            lanes.append(self._automation_lane(
                icon=ft.Icons.ERROR_OUTLINE,
                title="Hermes Niche Desk",
                status="Unavailable",
                detail=detail,
                tone=WARN,
            ))
        else:
            state = _niche_watch_state(niche_entry.get("data"))
            active = sum(
                1 for workspace in state["workspaces"]
                if bool(workspace.get("autonomous", 1))
            )
            paused = len(state["workspaces"]) - active
            lanes.append(self._automation_lane(
                icon=ft.Icons.WORKSPACES_OUTLINE,
                title="Niche upkeep",
                status=f"{active}/{len(state['workspaces'])} active",
                detail=(
                    f"{state['signal_count']:,} unread signals · {paused:,} paused "
                    "workspaces · snapshots and alerts remain reversible."
                ),
                tone=ACCENT if active else AMBER,
            ))
            hermes_signals = [
                signal for signal in state["signals"]
                if self._niche_signal_source(signal).lower() == "hermes"
                or str(signal.get("entity_type") or "").lower() == "operator"
            ]
            latest = hermes_signals[0] if hermes_signals else None
            if latest:
                hermes_status = (
                    f"Brief {self._ago(latest.get('detected_at'))}"
                    if self._ago(latest.get("detected_at"))
                    else "Brief published"
                )
                hermes_detail = (
                    f"{latest.get('niche_name') or 'Niche workspace'} · "
                    f"{latest.get('title') or 'New operator brief'}. "
                    "Scout, Classifier, and Curator reviewed one bounded packet."
                )
                hermes_tone = ACCENT2
            else:
                hermes_status = "Ready"
                hermes_detail = (
                    "Scout, Classifier, and Curator review one bounded evidence "
                    "packet; only the coordinator can write."
                )
                hermes_tone = MUTED
            lanes.append(self._automation_lane(
                icon=ft.Icons.SMART_TOY_OUTLINED,
                title="Hermes Niche Desk",
                status=hermes_status,
                detail=hermes_detail,
                tone=hermes_tone,
            ))

        ap = self.autopilot
        ap_last = ap.log[0] if ap.log else {}
        ap_detail = (
            f"Last pass {ap_last.get('at')} · "
            f"{len(ap_last.get('tools') or [])} tools recorded."
            if ap_last
            else "No local maintenance passes recorded yet."
        )
        lanes.append(self._automation_lane(
            icon=ft.Icons.AUTO_MODE,
            title="Beacon Autopilot",
            status="On" if ap.enabled else "Off",
            detail=ap_detail,
            tone=ACCENT if ap.enabled else MUTED,
        ))
        return self.card(
            ft.Row([
                ft.Icon(ft.Icons.HUB_OUTLINED, size=18, color=ACCENT2),
                h("Work ledger", 16),
                ft.Container(expand=True),
                self.pill("Reversible controls", ACCENT2, ft.Icons.HISTORY),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            *lanes,
        )

    def _render_overview(self) -> None:
        self._watch_overview()
        self._watch_cloud_sync()
        self.content.controls.append(ft.Row([
            self.pill(
                (f"Auto-refresh {OVERVIEW_REFRESH_SECONDS // 60}m"
                 if OVERVIEW_REFRESH_SECONDS >= 60 else
                 f"Auto-refresh {OVERVIEW_REFRESH_SECONDS}s"),
                ACCENT2,
                ft.Icons.SYNC,
            ),
            muted(self._cache_updated_text("overview"), 11),
        ], spacing=8))

        self.content.controls.extend(self._niche_watch_section())

        research_card = self._research_progress_card()
        research_card.col = {"sm": 12, "md": 12, "lg": 7}

        entry = self._ensure("overview", self._index_stats, "Loading index stats…")
        if entry is None:
            index_card = self.card(
                h("Index snapshot", 16),
                self._loading_note("Loading index stats…"),
            )
        elif "error" in entry:
            index_card = self._error_card(entry["error"])
        else:
            counted = entry["data"].get("_counted_at")
            index_card = self.card(
                ft.Row([h("Index snapshot", 16),
                        muted(index_stats.counted_text({"counted_at": counted}), 11)
                        if counted else ft.Container()],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                self.stat_grid(entry["data"],
                               ["channels", "videos", "niches", "niche_channels",
                                "faceless_verified", "outlier_snapshots", "crawl_runs"],
                               cell_col={"sm": 6, "md": 6, "lg": 6}),
            )
        index_card.col = {"sm": 12, "md": 12, "lg": 5}
        self.content.controls.append(ft.ResponsiveRow(
            [research_card, index_card],
            columns=12,
            spacing=16,
            run_spacing=16,
        ))

        self.content.controls.append(self.section_heading(
            "Discovery operations",
            "What is collecting, organizing, and focusing the index right now.",
            ft.Icons.RADAR,
        ))
        # live background-scraper card (status read fresh each render, not cached)
        self.content.controls.append(self._scraper_card())
        self.content.controls.append(self._scrape_targets_card())

        self.content.controls.append(self.section_heading(
            "Cloud collection",
            "Secure staged discoveries arriving from Oracle.",
            ft.Icons.CLOUD_SYNC,
        ))
        self.content.controls.append(self._cloud_sync_card())

    def _research_progress_card(self) -> ft.Control:
        """Show how much of the expanded scraper-search backlog has been tested."""
        try:
            state = _research_progress_state(sweep_control.watchlist_summary())
        except Exception as ex:  # noqa: BLE001 - keep Overview usable if state is unreadable
            return self.card(
                ft.Row([ft.Icon(ft.Icons.QUERY_STATS, color=ACCENT2, size=18),
                        h("Research progress", 16)], spacing=8),
                muted("Research tracking is temporarily unavailable."),
                muted(str(ex)[:160], 10),
            )

        return self.card(
            ft.Row([
                ft.Icon(ft.Icons.QUERY_STATS, color=ACCENT2, size=18),
                h("Research progress", 16),
                ft.Container(expand=True),
                self.pill(f"{state['coverage_label']}% complete", ACCENT2),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text(
                f"{state['tested']:,} of {state['total']:,} searches tested at least once",
                size=19, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.ProgressBar(
                value=state["ratio"], bar_height=9, color=ACCENT,
                bgcolor=BORDER, border_radius=9,
                semantics_label="Research searches tested",
                semantics_value=state["coverage_pct"]),
            self.stat_grid(
                {
                    "tested": state["tested"],
                    "remaining": state["remaining"],
                    "total_searches": state["total"],
                    "base_ideas": state["base_ideas"],
                },
                ["tested", "remaining", "total_searches", "base_ideas"],
            ),
            muted(
                "One base research idea creates several real search variations. "
                "The scraper tests those variations in the background.",
                11,
            ),
            ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=15, color=ACCENT2),
                    ft.Text(state["status"], size=11, color=MUTED, expand=True),
                ], spacing=8),
                padding=10, border_radius=10, bgcolor=ACCENT2_SOFT,
            ),
        )

    def _index_stats(self) -> dict:
        """The library's counts, instantly from the shared snapshot (index_stats.py).

        Counting every row takes ~6 minutes on the server's disk, so the Overview used to sit
        on "Loading index stats…". Now it shows the last count at once with its age, and an old
        count is refreshed quietly in the background — one count at a time across every open
        screen and Muse's door. Only the very first count ever makes anyone wait.
        """
        snapshot = index_stats.ensure(self.client.db_stats,
                                      on_refreshed=self._index_stats_refreshed)
        return {**snapshot["stats"], "_counted_at": snapshot["counted_at"]}

    def _index_stats_refreshed(self, snapshot: dict) -> None:
        """A background recount finished: swap the new numbers in without a loading flash."""
        self.cache["overview"] = self._cache_entry(
            {"data": {**snapshot["stats"], "_counted_at": snapshot["counted_at"]}})
        if self.section == "Overview":
            try:
                self._render()
            except Exception:  # noqa: BLE001
                pass

    def _overview_tick(self) -> None:
        """Refresh Overview counts silently without blanking the existing stat card."""
        key = "overview"
        if key in self._loading:
            return
        self._loading.add(key)
        gen = self._epoch.get(key, 0)

        def work():
            try:
                result = {"data": self._index_stats()}
            except Exception:  # noqa: BLE001 — keep the last good automatic snapshot
                self._loading.discard(key)
                return
            self._loading.discard(key)
            if self._epoch.get(key, 0) != gen:
                return
            self.cache[key] = self._cache_entry(result)
            if self.section == "Overview":
                try:
                    self._render()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _watch_overview(self) -> None:
        """Start the once-only automatic Overview-count refresh loop."""
        if self._overview_watch_started:
            return
        self._overview_watch_started = True

        stop = self._session_stop

        def worker():
            while not stop.wait(OVERVIEW_REFRESH_SECONDS):
                if self.section == "Overview":
                    self._overview_tick()

        threading.Thread(target=worker, daemon=True).start()

    def _cloud_tick(self) -> None:
        """Refresh cloud progress silently while an import is active."""
        key = "edge_sync"
        if key in self._loading:
            return
        self._loading.add(key)
        gen = self._epoch.get(key, 0)

        def work():
            try:
                result = {
                    "data": self.client.call("edge_sync_status")
                }
            except Exception:  # noqa: BLE001 - keep the last good progress snapshot
                self._loading.discard(key)
                return
            self._loading.discard(key)
            if self._epoch.get(key, 0) != gen:
                return
            self.cache[key] = self._cache_entry(result)
            if self.section == "Overview":
                try:
                    self._render()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _watch_cloud_sync(self) -> None:
        """Poll live import progress without making the user press Refresh."""
        if self._cloud_watch_started:
            return
        self._cloud_watch_started = True

        stop = self._session_stop

        def worker():
            while not stop.wait(CLOUD_PROGRESS_REFRESH_SECONDS):
                if self.section != "Overview":
                    continue
                entry = self.cache.get("edge_sync") or {}
                data = entry.get("data")
                data = data if isinstance(data, dict) else {}
                progress = data.get("progress")
                progress = progress if isinstance(progress, dict) else {}
                if (
                    self._cloud_syncing
                    or str(progress.get("state") or "").lower() == "importing"
                ):
                    self._cloud_tick()

        threading.Thread(target=worker, daemon=True).start()

    def _cloud_progress_age(self):
        """Seconds since the cloud import progress file was last written (or None). A large
        age while the state is still 'importing' means the import is STALLED — hung, usually
        waiting for the local scraper to release the SQLite write lock — rather than actively
        working. Lets the card distinguish 'quiet/stuck' from 'busy'. (Reads the file mtime
        directly, like _oracle_state, so it stays a Beacon-side display concern.)"""
        try:
            path = (sweep_control.SCRIPTER_ROOT / "storage" / "edge_sync"
                    / "sync_progress.json")
            if not path.exists():
                return None
            return max(0.0, time.time() - path.stat().st_mtime)
        except Exception:  # noqa: BLE001
            return None

    def _cloud_sync_card(self) -> ft.Control:
        entry = self._ensure(
            "edge_sync",
            lambda: self.client.call("edge_sync_status"),
            "Checking cloud worker…",
        )
        header = ft.Row(
            [
                ft.Icon(ft.Icons.CLOUD_SYNC, color=ACCENT2, size=18),
                h("Cloud worker", 16),
            ],
            spacing=8,
        )
        if entry is None:
            return self.card(
                header,
                self._loading_note("Checking the secure cloud inbox…"),
            )
        if "error" in entry:
            return self.card(
                header,
                ft.Row(
                    [
                        ft.Icon(ft.Icons.ERROR_OUTLINE, color=WARN, size=16),
                        ft.Text(
                            "Cloud status is temporarily unavailable.",
                            color=WARN,
                            weight=ft.FontWeight.BOLD,
                        ),
                    ],
                    spacing=8,
                ),
                muted(entry["error"]),
                ft.OutlinedButton(
                    "Retry",
                    icon=ft.Icons.REFRESH,
                    on_click=self._refresh_cloud,
                ),
            )

        view = _cloud_sync_view_state(entry.get("data") or {})
        color = {
            "accent": ACCENT,
            "accent2": ACCENT2,
            "warn": WARN,
            "muted": MUTED,
        }[view["tone"]]
        sync_disabled = self._cloud_syncing or not view["sync_enabled"]
        sync_text = (
            "Importing..."
            if view["sync_running"]
            else ("Syncing..." if self._cloud_syncing else "Sync now")
        )
        details: list[ft.Control] = [
            ft.Row(
                [
                    ft.Icon(ft.Icons.CLOUD_SYNC, color=color, size=18),
                    h("Cloud worker", 16),
                    self.pill(view["badge"], color),
                    ft.Container(expand=True),
                    ft.OutlinedButton(
                        "Refresh",
                        icon=ft.Icons.REFRESH,
                        on_click=self._refresh_cloud,
                    ),
                    ft.FilledButton(
                        sync_text,
                        icon=ft.Icons.CLOUD_DOWNLOAD,
                        disabled=sync_disabled,
                        on_click=None if sync_disabled else self._sync_cloud,
                        style=(
                            None
                            if sync_disabled
                            else ft.ButtonStyle(bgcolor=ACCENT, color=NAV_BG)
                        ),
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Text(view["message"], size=12, color=color),
        ]
        if view["cloud_in_use"] and not view["configured"]:
            details.append(muted(
                "Imports run on a schedule; manual sync is unavailable from this screen.",
                10,
            ))
        # A dead pipeline must LOOK dead. Before this, a cloud worker frozen mid-job
        # still rendered as "connected" because it never stopped claiming "running".
        if not view.get("health_ok", True):
            details.append(ft.Container(
                ft.Row(
                    [
                        ft.Icon(ft.Icons.ERROR_OUTLINE, color=WARN, size=18),
                        ft.Column(
                            [
                                ft.Text(
                                    "Cloud discovery is not delivering channels.",
                                    size=12, color=WARN, weight=ft.FontWeight.BOLD),
                            ] + [
                                ft.Text(problem, size=11, color=WARN)
                                for problem in view.get("health_problems") or []
                            ] + [
                                muted(
                                    "The cloud worker restarts itself if it stops "
                                    "making progress. If this stays up, check the "
                                    "Oracle worker.", 10),
                            ],
                            spacing=3, expand=True),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.START),
                padding=10, border_radius=8, bgcolor=BG,
                border=ft.border.all(1, WARN)))
        if view["sync_running"]:
            progress_controls: list[ft.Control] = [
                ft.Row(
                    [
                        muted("Safely committed", 10),
                        ft.Container(expand=True),
                        ft.Text(
                            f"{view['progress_processed']:,}/"
                            f"{view['progress_total']:,}",
                            size=10,
                            color=ACCENT2,
                            weight=ft.FontWeight.BOLD,
                        ),
                    ]
                ),
                ft.ProgressBar(
                    value=view["progress_ratio"],
                    color=ACCENT2,
                    bgcolor=BORDER,
                    height=6,
                ),
            ]
            # STALLED detection: a live "importing" state whose progress file hasn't been
            # written in a while is hung (usually blocked on the DB write lock held by the
            # local scraper), not working. Say so plainly instead of a frozen spinner —
            # otherwise it looks identical to active work (the 30-min "stuck" report).
            stall_age = self._cloud_progress_age()
            if stall_age is not None and stall_age > 90:
                mins = int(stall_age // 60)
                ago = f"{mins}m" if mins else f"{int(stall_age)}s"
                progress_controls.insert(0, ft.Container(
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=AMBER, size=16),
                            ft.Text(
                                f"Import looks stalled — no progress for {ago}. It's most "
                                "likely waiting for the local scraper to finish writing to "
                                "the database; it retries automatically.",
                                size=11, color=AMBER, expand=True),
                        ],
                        spacing=8),
                    padding=10, border_radius=8, bgcolor=BG,
                    border=ft.border.all(1, AMBER)))
            if view["activity_label"]:
                activity_row: list[ft.Control] = [
                    ft.ProgressRing(
                        width=14,
                        height=14,
                        stroke_width=2,
                        color=ACCENT,
                    ),
                    ft.Text(
                        view["activity_label"],
                        size=11,
                        color=TEXT,
                        expand=True,
                    ),
                ]
                progress_controls.append(
                    ft.Container(
                        ft.Column(
                            [
                                ft.Row(activity_row, spacing=8),
                                *(
                                    [
                                        ft.ProgressBar(
                                            value=view["activity_ratio"],
                                            color=ACCENT,
                                            bgcolor=BORDER,
                                            height=4,
                                        )
                                    ]
                                    if view["activity_ratio"] is not None
                                    else []
                                ),
                            ],
                            spacing=6,
                            tight=True,
                        ),
                        padding=10,
                        border_radius=8,
                        bgcolor=BG,
                        border=ft.border.all(1, BORDER),
                    )
                )
            details.append(
                ft.Column(progress_controls, spacing=7, tight=True)
            )
        if view["remote_message"]:
            details.append(muted(f"Oracle: {view['remote_message']}", 11))
        if view["last_line"]:
            details.append(muted(view["last_line"], 10))
        details.append(
            muted(
                "Oracle only collects staged results. It never opens or owns "
                "Beacon's main database.",
                11,
            )
        )
        return self.card(*details)

    def _refresh_cloud(self, _=None) -> None:
        self._invalidate("edge_sync")
        self._render()

    def _sync_cloud(self, _=None) -> None:
        if self._cloud_syncing:
            return
        self._cloud_syncing = True
        self._render()

        def work():
            try:
                return self.client.call("import_edge_inbox", max_bundles=20)
            except Exception as ex:  # noqa: BLE001
                return {"error": str(ex)[:200]}

        def done(result):
            self._cloud_syncing = False
            if isinstance(result, dict) and result.get("error"):
                self.toast(f"Cloud sync: {str(result['error'])[:160]}")
                self._invalidate("edge_sync")
            else:
                imported = int((result or {}).get("imported_bundles") or 0)
                failed = int((result or {}).get("failed_bundles") or 0)
                remaining = int((result or {}).get("remaining_bundles") or 0)
                self.toast(
                    f"Cloud sync finished: {imported:,} imported, "
                    f"{failed:,} rejected, {remaining:,} waiting."
                )
                if imported:
                    self._invalidate_all()
                else:
                    self._invalidate("edge_sync")
            self._render()

        self.run_bg(work, done, "Importing cloud discoveries…")

    def _scraper_card(self) -> ft.Control:
        st = sweep_control.read_status()
        running = sweep_control.is_running()
        pipeline = _pipeline_progress_state(st)
        # reconcile the optimistic flags with reality
        if running:
            self._sweep_starting = False
        else:
            self._sweep_stopping = False
        starting, stopping = self._sweep_starting, self._sweep_stopping
        state = str(st.get("state", "not_started"))

        if starting and not running:
            line, color, badge_txt = ("Starting… booting the scraper (a few seconds)",
                                      ACCENT, "● starting")
        elif stopping and running:
            line, color, badge_txt = ("Stopping… it'll finish the current step and exit.",
                                      WARN, "● stopping")
        elif running and pipeline:
            discovery = pipeline["discovery"]
            organizer = pipeline["organizer"]
            line = (
                f"Fast discovery ({discovery['state']}) — {discovery['message']}  ·  "
                f"Organizer ({organizer['state']}) — {organizer['message']}"
            )
            color, badge_txt = ACCENT, "● fast mode"
        elif running and state in ("running", "sweeping", "cycle_complete"):
            detail = (st.get("current_query") or st.get("message")
                      or "harvesting channels & sweeping niches…")
            line, color, badge_txt = f"Working — {detail}", ACCENT, "● running"
        elif running and state == "sleeping":
            secs = int(st.get("next_scan_seconds") or 0)
            lc = st.get("last_completed") or {}
            line = (f"Resting between cycles — next scan in {secs}s"
                    f"  (last: +{lc.get('new_channels', 0)} channels, "
                    f"{lc.get('discovered', 0)} videos)")
            color, badge_txt = ACCENT2, "● running"
        elif running:
            line, color, badge_txt = "Running…", ACCENT, "● running"
        elif state == "error":
            line, color, badge_txt = f"Error: {str(st.get('error', ''))[:130]}", WARN, "○ stopped"
        else:
            line = ("Off — not scraping. Start it to keep discovering channels & "
                    "videos for your niches in the background.")
            color, badge_txt = MUTED, "○ stopped"

        badge = self.pill(badge_txt, MUTED if badge_txt.startswith("○") else ACCENT)
        # Start is inactive while starting OR running; Stop only while genuinely running
        start_off = running or starting
        stop_off = (not running) or stopping
        # NOTE: only style the button green when it's actually clickable — a forced
        # bgcolor would keep it looking green/active even when disabled.
        start_btn = ft.FilledButton(
            "Start scraping", icon=ft.Icons.PLAY_ARROW, disabled=start_off,
            on_click=None if start_off else self._start_scraper,
            style=None if start_off else ft.ButtonStyle(bgcolor=ACCENT, color=NAV_BG))
        stop_btn = ft.OutlinedButton(
            "Stop", icon=ft.Icons.STOP, disabled=stop_off,
            on_click=None if stop_off else self._stop_scraper)

        niches = sweep_control.get_niches()
        field = ft.TextField(
            value="\n".join(niches), multiline=True, min_lines=4, max_lines=12,
            border_color=BORDER, text_size=12, color=TEXT,
            label="Niches / search queries — one per line")

        def _save(_):
            sweep_control.save_niches((field.value or "").splitlines())
            self.toast("Saved — the scraper picks up the new list on its next cycle.")

        box = ft.Container(
            ft.Column([field,
                       ft.FilledButton("Save list", icon=ft.Icons.SAVE, on_click=_save,
                                       style=ft.ButtonStyle(bgcolor=ACCENT, color=NAV_BG))],
                      spacing=8),
            visible=False)

        def _toggle(e, b=box, n=len(niches)):
            b.visible = not b.visible
            e.control.text = "Hide the list" if b.visible else f"Edit niches / queries ({n})"
            try:
                b.update()
                e.control.update()
            except Exception:  # noqa: BLE001
                pass

        pipeline_note = ft.Container(visible=False)
        if pipeline:
            pipeline_note = ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=16, color=ACCENT2),
                    ft.Text(
                        pipeline["queue_text"],
                        size=11,
                        color=MUTED,
                        expand=True,
                    ),
                ], spacing=8),
                padding=10,
                border_radius=10,
                bgcolor=SURFACE_ALT,
            )

        # auto-start-on-login toggle (status read off-thread + cached so render never blocks)
        auto_on = getattr(self, "_autostart_on", None)
        if auto_on is None and not getattr(self, "_autostart_checking", False):
            self._autostart_checking = True

            def _chk():
                try:
                    self._autostart_on = sweep_control.autostart_installed()
                except Exception:  # noqa: BLE001
                    self._autostart_on = False
                self._autostart_checking = False
                if self.section == "Overview":
                    try:
                        self._render()
                    except Exception:  # noqa: BLE001
                        pass

            threading.Thread(target=_chk, daemon=True).start()
        auto_name = "Starts with the server" if ON_SERVER else "Auto-start on login"
        autostart_label = (f"{auto_name}  ·  checking…" if auto_on is None
                           else (f"{auto_name}  ·  ON" if auto_on
                                 else f"{auto_name}  ·  off"))
        autostart_row = ft.Row([
            ft.Switch(value=bool(auto_on), active_color=ACCENT, scale=0.8,
                      on_change=self._toggle_autostart,
                      # The server's own boot setting; Stop/Start above still work, and a Stop
                      # survives a reboot. Changing boot-start is the machine owner's job.
                      disabled=ON_SERVER,
                      tooltip=("The server starts the scraper at boot and restarts it if it "
                               "crashes — unless you pressed Stop" if ON_SERVER else
                               "Keep the scraper running automatically — starts within ~5 min of "
                               "login and restarts it if it ever dies (no admin needed)")),
            muted(autostart_label, 12),
        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        return self.card(
            ft.Row([h("Background scraper", 16), badge,
                    ft.Container(expand=True), start_btn, stop_btn],
                   spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            muted(line),
            pipeline_note,
            muted("Reuses the engine's radar worker — keeps running even if you "
                  "close Beacon. Your manual Deep sweep still works alongside it.", 11),
            autostart_row,
            ft.TextButton(f"Edit niches / queries ({len(niches)})",
                          icon=ft.Icons.EDIT, on_click=_toggle),
            box)

    def _start_scraper(self, _=None) -> None:
        self._sweep_starting = True          # flip the button state INSTANTLY
        started = sweep_control.start_worker()   # quick, non-blocking (spawns a process)
        self.toast("Background scraper starting — it'll begin a cycle in a few seconds."
                   if started else "Scraper is already running.")
        self._render()
        self._watch_sweep()                  # auto-refresh once it actually boots

    def _stop_scraper(self, _=None) -> None:
        self._sweep_stopping = True
        sweep_control.stop_worker()
        self.toast("Stop requested — it'll finish the current step and stop.")
        self._render()
        self._watch_sweep()                  # auto-refresh once it actually exits

    def _toggle_autostart(self, e) -> None:
        want = bool(e.control.value)
        self._autostart_on = want   # optimistic; confirmed after the task op

        def work():
            sweep_control.set_autostart(want)
            return sweep_control.autostart_installed()

        def done(installed):
            self._autostart_on = bool(installed)
            if want and installed:
                self.toast("Auto-start ON — the scraper keeps itself running (starts ~5 min "
                           "after login, restarts if it dies).")
            elif not want and not installed:
                self.toast("Auto-start off — the scraper won't launch itself anymore.")
            else:
                self.toast("Couldn't change auto-start — run install_radar_autostart.ps1 "
                           "manually.")
            self._render()

        self.run_bg(work, done, "Updating auto-start…")

    def _watch_sweep(self, seconds: int = 25) -> None:
        """Briefly poll the scraper's real state so the card catches up to a
        start/stop without the user hitting Refresh — no busy spinner."""
        def worker():
            prev = None
            for _ in range(seconds):
                cur = sweep_control.is_running()
                if cur != prev:
                    prev = cur
                    if self.section == "Overview":
                        try:
                            self._render()
                        except Exception:  # noqa: BLE001
                            pass
                time.sleep(1)
        threading.Thread(target=worker, daemon=True).start()

    def _ch_niche_id(self):
        n = getattr(self, "ch_niche", None)
        return (n.get("id") or n.get("niche_id")) if n else None

    def _ch_in_niche_view(self) -> bool:
        return bool(self.ch_niche) and self.ch_view == "niche"

    def _ch_query_args(self) -> dict:
        """find_channels args for the current filters (niche injected in niche view)."""
        args = {k: v for k, v in self.ch_filters.items() if not k.startswith("_")}
        # Beacon is an opportunity finder, not a general channel browser. This
        # mandatory shared-engine gate requires faceless, repeatable evidence.
        args["replicable_only"] = True
        args["faceless_only"] = True
        args.setdefault("descending", True)
        floor = self.ch_filters.get("_min_outlier")
        if floor is not None:                            # server-side floor (accurate under any sort)
            args["min_outlier_score"] = floor
        if self._ch_in_niche_view():
            args["niche_id"] = self._ch_niche_id()
        topic = self._topic_or_none(self.ch_topic)
        if topic:                                        # clean topic filter (composes with everything)
            args["topic"] = topic
        args["limit"] = self.ch_shown                    # grows with "Show more" (no fixed cap)
        return args

    def _channels_key(self) -> str:
        args = self._ch_query_args()
        return "channels:" + "|".join(
            f"{k}={args[k]}" for k in sorted(args) if args[k] is not None)

    def _ch_base_key(self) -> str:
        """The query signature WITHOUT the paging limit — changes only when the
        filter/sort/niche changes, so we can reset paging then."""
        args = self._ch_query_args()
        return "|".join(f"{k}={args[k]}" for k in sorted(args)
                        if k != "limit" and args[k] is not None)

    def _ch_show_more(self, _=None) -> None:
        self.ch_shown += CH_PAGE
        self._render()

    def _finder_rows(self, tool: str, args: dict) -> list[dict]:
        """Call a finder tool that returns a channel list, retrying a couple times on an
        EMPTY result. Under heavy scraper writes the in-process finder occasionally
        returns [] for a fraction of a second; retrying inline means a transient empty
        never gets cached + frozen as a misleading "0 channels". A non-list response is a
        hard failure → raise so it shows a retryable error card, not a stuck "0"."""
        res: object = []
        for attempt in range(3):
            res = self.client.call(tool, **args)
            if not isinstance(res, list):
                raise FinderError("finder returned no channel list (busy?) — hit Refresh to retry")
            if res or attempt == 2:      # got rows, or last try (genuinely empty)
                break
            time.sleep(1.2)              # transient empty → quick retry
        return res

    def _load_channels(self) -> list[dict]:
        rows = self._finder_rows("find_channels", self._ch_query_args())
        if any(not r.get("avatar_url") for r in rows):   # find_channels returns it now
            self._attach_avatars(rows)
        return rows

    def _ch_set_niche(self, niche) -> None:
        self.ch_niche = niche
        self.ch_view = "niche" if niche else "index"
        self._render()

    def _ch_set_topic(self, topic) -> None:
        self.ch_topic = self._topic_or_none(topic)
        self._render()

    def _ch_set_view(self, view) -> None:
        self.ch_view = view
        self._render()

    def _ch_set_sort(self, key: str) -> None:
        self.ch_filters["sort"] = key
        self._render()

    def _ch_pick_chip(self, name: str, preset: dict) -> None:
        # Presets TOGGLE: clicking the active preset deselects it (back to no preset — just
        # the default sort, no metric filters), so the user is never forced to have one on.
        # A preset replaces metric/format filters; niche + topic live in separate state and survive.
        if self.ch_chip == name:
            self.ch_filters = {"sort": self.ch_filters.get("sort", "opportunity_score"),
                               "limit": 60}
            self.ch_chip = None
        else:
            self.ch_filters = {**preset, "limit": 60}
            self.ch_filters.setdefault("sort", "opportunity_score")
            self.ch_chip = name
        self._render()

    def _ch_remove_filter(self, key: str) -> None:
        self.ch_filters.pop(key, None)
        self.ch_chip = "Custom"
        self._render()

    def _read_filter_fields(self, fields: dict, filt: dict) -> None:
        """Read the modal's filter fields into `filt` (blank clears a field).
        No re-render — the caller closes the dialog and renders once."""
        for key, ctrl in fields.items():
            raw = (ctrl.value or "").strip()
            if key != "category_contains":
                raw = raw.replace(",", "")
            if not raw:
                filt.pop(key, None)
                continue
            if key == "category_contains":
                filt[key] = raw
                continue
            try:
                filt[key] = (float(raw)
                             if key in ("min_views_per_sub", "_min_outlier")
                             else int(raw))
            except ValueError:
                filt.pop(key, None)

    # -- filters modal (progressive disclosure — clean page, filters on demand) --
    _FILTER_KEYS = ("min_subs", "max_subs", "min_avg_views", "max_avg_views",
                    "max_days_since_start", "min_days_since_start", "min_uploads",
                    "max_uploads", "_min_outlier", "min_views_per_sub",
                    "category_contains", "video_format", "days")

    def _active_filter_count(self, filt: dict | None = None) -> int:
        filt = self.ch_filters if filt is None else filt
        return sum(1 for k in self._FILTER_KEYS
                   if filt.get(k) not in (None, "", False))

    def _close_dialog(self, dlg) -> None:
        """Dismiss a transient UI dialog without changing the user's state."""
        try:
            self.page.close(dlg)
        except Exception:  # noqa: BLE001
            pass

    def _open_filters(self, _=None, target: str = "channels") -> None:
        """Open the Filters modal (shared by Channels + Real Time via `target`)."""
        filt = self.ch_filters if target == "channels" else self.rt_filters

        def numf(key, label, width=150):
            v = filt.get(key)
            return ft.TextField(
                label=label, value=("" if v is None else str(v)), width=width,
                dense=True, border_color=BORDER, text_size=13,
                keyboard_type=(ft.KeyboardType.TEXT if key == "category_contains"
                               else ft.KeyboardType.NUMBER))

        fields = {
            "min_subs": numf("min_subs", "Min subs"),
            "max_subs": numf("max_subs", "Max subs"),
            "min_avg_views": numf("min_avg_views", "Min avg views"),
            "max_avg_views": numf("max_avg_views", "Max avg views"),
            "max_days_since_start": numf("max_days_since_start", "Max age (days)"),
            "_min_outlier": numf("_min_outlier", "Min outlier ×"),
            "category_contains": numf("category_contains", "Category contains", 314),
        }
        # Beacon already enforces faceless + replicable globally; the only type
        # choice left here is long-form versus short-form.
        state = {"format": filt.get("video_format") or "all"}
        fmtseg = ft.SegmentedButton(
            allow_multiple_selection=False, allow_empty_selection=False,
            selected={state["format"]},
            segments=[ft.Segment(value="all", label=ft.Text("All")),
                      ft.Segment(value="long", label=ft.Text("Long")),
                      ft.Segment(value="short", label=ft.Text("Short"))],
            on_change=lambda e: state.update(
                format=next(iter(e.control.selected), "all")))

        def section(title, *ctrls):
            return ft.Column([ft.Text(title, size=12, weight=ft.FontWeight.BOLD,
                                      color=MUTED), *ctrls], spacing=10, tight=True)

        def labelled(label, ctrl):
            return ft.Row([ft.Container(muted(label, 12), width=70), ctrl],
                          spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        content = ft.Container(
            ft.Column([
                section("Video format",
                        labelled("Format", fmtseg)),
                ft.Divider(color=BORDER, height=1),
                section("Audience & reach",
                        ft.Row([fields["min_subs"], fields["max_subs"]], spacing=14),
                        ft.Row([fields["min_avg_views"], fields["max_avg_views"]],
                               spacing=14)),
                ft.Divider(color=BORDER, height=1),
                section("Performance & age",
                        ft.Row([fields["max_days_since_start"], fields["_min_outlier"]],
                               spacing=14),
                        fields["category_contains"]),
            ], spacing=16, tight=True, scroll=ft.ScrollMode.AUTO),
            width=360, padding=ft.padding.only(top=4))

        dlg = ft.AlertDialog(
            modal=False, bgcolor=PANEL,
            title=ft.Row(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.TUNE, color=ACCENT, size=20),
                            ft.Text("Filters", color=TEXT),
                        ],
                        spacing=8,
                    ),
                    ft.IconButton(
                        ft.Icons.CLOSE,
                        icon_color=MUTED,
                        tooltip="Close filters",
                        on_click=lambda e: self._close_dialog(dlg),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            content=content,
            actions=[
                ft.Row(
                    [
                        ft.OutlinedButton(
                            "Reset",
                            icon=ft.Icons.RESTART_ALT,
                            on_click=lambda e: self._reset_filters(dlg, target),
                        ),
                        ft.Container(expand=True),
                        ft.TextButton(
                            "Cancel",
                            icon=ft.Icons.CLOSE,
                            on_click=lambda e: self._close_dialog(dlg),
                        ),
                        ft.FilledButton(
                            "Apply",
                            icon=ft.Icons.CHECK,
                            on_click=lambda e: self._apply_filters(
                                fields, state, dlg, target
                            ),
                            style=ft.ButtonStyle(bgcolor=ACCENT, color=NAV_BG),
                        ),
                    ],
                    spacing=8,
                )
            ])
        self.page.open(dlg)

    def _apply_filters(self, fields, state, dlg, target: str = "channels") -> None:
        filt = self.ch_filters if target == "channels" else self.rt_filters
        self._read_filter_fields(fields, filt)
        filt.pop("faceless_only", None)
        if state["format"] in ("long", "short"):
            filt["video_format"] = state["format"]
        else:
            filt.pop("video_format", None)
        if target == "channels":
            self.ch_chip = "Custom"
        else:
            self.rt_chip = "Custom"          # edited filters no longer match a preset chip
            self.rt_shown = CH_PAGE          # new filter → back to first page
        self._close_dialog(dlg)
        self._render()

    def _reset_filters(self, dlg, target: str = "channels") -> None:
        """Clear every metric/type filter, then reopen fresh."""
        if target == "channels":
            sort = self.ch_filters.get("sort", "opportunity_score")
            self.ch_filters = {"sort": sort, "limit": 60}
            self.ch_chip = "Custom"
        else:
            self.rt_filters = {}
            self.rt_chip = "Custom"          # cleared filters no longer match a preset chip
            self.rt_shown = CH_PAGE
        self._close_dialog(dlg)
        self._render()
        self._open_filters(target=target)

    def _ch_filter_label(self, k, v):
        # NOTE: evaluate lazily (if/elif), NOT one big dict literal — a dict literal would
        # run int(v) for the numeric entries even when k is a STRING filter like
        # category_contains="history", crashing with "invalid literal for int()".
        if k == "faceless_only":
            return "faceless only" if v else None
        if k == "category_contains":
            return f"category ~ {v}"
        if k == "video_format":
            return f"format: {v}"
        if k == "min_views_per_sub":
            return f"views/sub ≥ {v}"
        if k == "_min_outlier":
            return f"outlier ≥ {v}x"
        if k == "min_subs":
            return f"subs ≥ {human_num(v)}"
        if k == "max_subs":
            return f"subs ≤ {human_num(v)}"
        if k == "min_avg_views":
            return f"avg views ≥ {human_num(v)}"
        if k == "max_avg_views":
            return f"avg views ≤ {human_num(v)}"
        try:
            iv = int(v)
        except (ValueError, TypeError):
            return None
        return {
            "max_days_since_start": f"age ≤ {iv}d",
            "min_days_since_start": f"age ≥ {iv}d",
            "min_uploads": f"uploads ≥ {iv}",
            "max_uploads": f"uploads ≤ {iv}",
            "days": f"added ≤ {iv}d",   # discovery-recency window (set by a target click-through)
        }.get(k)

    # -- Scrape Targets (steer the scraper at chosen niches, on demand) --------------
    def _scrape_targets_card(self) -> ft.Control:
        import scrape_targets
        targets = scrape_targets.list_targets()
        # Accurate, ENGINE-AGNOSTIC per-target counts come from the database (both the laptop
        # worker AND Oracle write channels.category + first_seen), computed off-thread + cached
        # so render never blocks. This fixes the old "laptop-only" progress that read 0 while
        # Oracle was actually finding channels.
        self._refresh_target_counts(targets)
        counts = getattr(self, "_target_counts", None) or {}
        ds = self._discovery_status()
        any_running = ds["any"]

        topic_dd = self._topic_dd(None, self._target_pick_topic)
        subj = ft.TextField(
            label="…or type a subject (e.g. deep sea exploration)", width=340,
            border_color=BORDER, dense=True, value=getattr(self, "_target_subject", ""),
            on_change=lambda e: setattr(self, "_target_subject", e.control.value),
            on_submit=self._target_add_typed)
        add_btn = ft.OutlinedButton(
            "Add target", icon=ft.Icons.ADD, on_click=self._target_add_typed,
            style=ft.ButtonStyle(color=ACCENT, side=ft.BorderSide(1, ACCENT)))
        input_row = ft.Row([topic_dd, subj, add_btn], wrap=True, spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER)

        rows = []
        for t in targets:
            on = t.get("enabled", True)
            active = on and any_running       # green only when ACTUALLY being scraped
            c = counts.get(t.get("id"))
            toggle = ft.Switch(
                value=on, active_color=ACCENT, scale=0.8,
                tooltip="Pause / resume scraping this niche",
                on_change=lambda e, tid=t.get("id"): self._target_toggle(tid, e.control.value))
            if active:
                btxt, bfg, bbg, bbd = "focusing", NAV_BG, ACCENT, ACCENT
            elif on:
                btxt, bfg, bbg, bbd = "waiting", AMBER, CARD, AMBER
            else:
                btxt, bfg, bbg, bbd = "paused", MUTED, CARD, BORDER
            badge = ft.Container(
                ft.Text(btxt, size=10, color=bfg),
                bgcolor=bbg, padding=ft.padding.symmetric(1, 8),
                border_radius=20, border=ft.border.all(1, bbd))
            prog = self._target_progress_control(t, c, on, any_running)
            remove = ft.OutlinedButton(
                "Remove", icon=ft.Icons.CLOSE,
                on_click=lambda e, tid=t.get("id"): self._target_remove(tid),
                tooltip="Stop scraping this niche and remove the target",
                style=ft.ButtonStyle(color=WARN, side=ft.BorderSide(1, WARN)))
            # A tidy per-target block: top line = toggle/badge/name/remove; the
            # click-through + progress sits just under it. (No expand inside a wrap Row —
            # that misbehaves in Flet; two stacked rows keep the layout clean.)
            top = ft.Row(
                [toggle, badge,
                 ft.Text(str(t.get("label") or "?"), size=13, weight=ft.FontWeight.BOLD,
                         color=TEXT),
                 ft.Container(expand=True), remove],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            rows.append(ft.Container(
                ft.Column([top, ft.Container(prog, padding=ft.padding.only(left=52))],
                          spacing=4, tight=True),
                padding=ft.padding.symmetric(9, 11),
                border_radius=10,
                bgcolor=SURFACE_ALT))

        body = [
            ft.Row([ft.Icon(ft.Icons.GPS_FIXED, color=ACCENT2, size=18),
                    h("Focus the scraper on a niche", 16)], spacing=8),
            muted("Pick a topic or type a subject — the scraper "
                  + ("(this server + the cloud scout)" if ON_SERVER else "(laptop + Oracle)")
                  + " hunts there, "
                  "and new channels get auto-categorized into the matching Topic/filters. Counts "
                  "below come from your database, so they include BOTH engines. Pause or Remove "
                  "a target anytime.", 11),
            self._who_is_finding(ds),
        ]
        body.append(input_row)
        if rows:
            body.append(muted(f"Active targets ({len(targets)}):", 11))
            body.extend(rows)
        else:
            body.append(muted("No targets yet — add one above to steer discovery.", 11))
        return self.card(*body)

    def _refresh_target_counts(self, targets: list[dict]) -> None:
        """Background: exact DB counts of channels discovered via each target's searches, via the
        finder (count_channels category_contains + recent window). Captures BOTH the laptop worker
        AND Oracle (both write channels.category + first_seen_at). Throttled (30s) + cached so the
        card render never blocks and never loops."""
        import time as _t
        # Locked test-and-set so concurrent re-renders (60s tick, cloud tick, nav) can't launch
        # duplicate count passes; the 30s throttle is measured from when the LAST pass FINISHED
        # (stamped in work()), so a pass slower than 30s can't turn into a back-to-back loop.
        with self._tc_lock:
            if self._target_counts_busy:
                return
            if (self._target_counts is not None
                    and _t.monotonic() - self._target_counts_at < 30):
                return
            self._target_counts_busy = True
        snapshot = list(targets)

        def work():
            import datetime as _dt
            import scrape_targets
            ys = None
            try:
                ys = scrape_targets._yield_state()   # read the yield log ONCE for all targets
            except Exception:  # noqa: BLE001
                ys = {}
            result: dict = {}
            for t in snapshot:
                tid = t.get("id")
                seed = (t.get("queries") or [""])[0]
                if not tid or not seed:
                    continue
                days = 7
                try:
                    added = _dt.datetime.strptime(str(t.get("added_at", "")), "%Y-%m-%d %H:%M")
                    days = max(1, (_dt.datetime.now() - added).days + 1)
                except Exception:  # noqa: BLE001
                    pass
                # laptop-side search progress (how many of the target's fanned-out searches
                # the worker has tested at least once) → a Queued/Finding/Swept state word
                run = searches = 0
                try:
                    st = scrape_targets.target_stats(t, ys)
                    run, searches = int(st.get("run", 0)), int(st.get("searches", 0))
                except Exception:  # noqa: BLE001
                    pass
                def _count(**extra):
                    res = self.client.call("count_channels", category_contains=seed,
                                           days=days, **extra)
                    if isinstance(res, list) and res:
                        return int((res[0] or {}).get("count", 0) or 0)
                    if isinstance(res, dict):
                        return int(res.get("count", 0) or 0)
                    return 0
                try:
                    # `found` = everything discovered here (honest "is my target working?"),
                    # `opps` = the faceless replicable subset the Channels grid actually shows,
                    # so the click-through number matches what the user then sees (no 308→17
                    # surprise). Both are cheap COUNT queries.
                    found = _count(faceless_only=False)
                    opps = _count(faceless_only=True, replicable_only=True)
                    result[tid] = {"found": found, "opps": opps, "days": days,
                                   "run": run, "searches": searches}
                except Exception:  # noqa: BLE001
                    result[tid] = {"error": True}   # distinct from "not computed yet" (None)
            with self._tc_lock:
                self._target_counts = result
                self._target_counts_at = _t.monotonic()   # rest starts when the pass FINISHED
                self._target_counts_busy = False
            if self.section == "Overview":
                try:
                    self._render()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _target_progress_control(self, t: dict, c, on: bool, any_running: bool) -> ft.Control:
        """One target's live status: a click-through to the channels it found + a plain
        progress line (Queued / Finding… / Swept) so the number isn't dead text."""
        if not on:
            return muted("paused — not being scraped", 11)
        if c is None:
            return ft.Row([ft.ProgressRing(width=12, height=12, stroke_width=2, color=MUTED),
                           muted("counting…", 11)], spacing=6)
        if c.get("error"):
            return muted("count unavailable — will retry", 11)
        found = int(c.get("found", 0) or 0)
        opps = int(c.get("opps", 0) or 0)
        days = int(c.get("days", 0) or 0)
        run = int(c.get("run", 0) or 0)
        searches = int(c.get("searches", 0) or 0)
        # the click-through shows the faceless-replicable opportunities (Beacon's grid gate),
        # so the LINK number is `opps` — clicking it shows exactly that many.
        label = f"View {opps:,} opportunities →" if opps else "No opportunities yet"
        link = ft.TextButton(
            label, icon=ft.Icons.OPEN_IN_NEW if opps else None, disabled=not opps,
            on_click=lambda _e, tt=t: self._view_target_channels(tt),
            tooltip=("Open the Channels tab filtered to exactly these channels"
                     if opps else None),
            style=ft.ButtonStyle(color=ACCENT if opps else MUTED,
                                 padding=ft.padding.symmetric(2, 8)))
        if searches and run >= searches:
            state = "Swept"
        elif run > 0:
            state = "Finding…"
        else:
            state = "Queued"
        bits = [f"{found:,} discovered here" + (f" in last {days}d" if days else "")]
        if searches:
            bits.append(f"{state} · {run}/{searches} searches tested")
        bits.append("being scraped now" if any_running else "scraper off — Start above")
        return ft.Column([link, muted(" · ".join(bits), 10)], spacing=0, tight=True)

    def _view_target_channels(self, target: dict) -> None:
        """Jump to the Channels tab filtered to exactly the channels this target found —
        the SAME query the count uses (category_contains=seed within the target's window),
        so the grid matches the number the user clicked."""
        seed = ""
        try:
            seed = str((target.get("queries") or [""])[0] or "").strip()
        except Exception:  # noqa: BLE001
            pass
        c = (getattr(self, "_target_counts", None) or {}).get(target.get("id")) or {}
        days = int(c.get("days", 0) or 0)
        filt: dict = {"sort": "opportunity_score", "limit": 60}
        if seed:
            filt["category_contains"] = seed
        if days:
            filt["days"] = days      # discovery-recency window (find_channels now honors it)
        self.ch_filters = filt
        self.ch_chip = "Custom"
        self.ch_topic = None
        self.ch_niche = None
        self.ch_view = "index"
        self.ch_shown = CH_PAGE
        self.section = "Channels"
        self._render()
        self.toast(f"Showing the channels found for “{target.get('label')}”.")

    def _oracle_state(self):
        """(is_running, human_text) for the off-laptop Oracle edge helper. ALWAYS returns a
        text so the user can tell 'working' from 'quiet' from 'not set up' — never hidden."""
        try:
            # Honour the same sync-root override the engine uses, so this line can be
            # exercised against a test root instead of the live operational files.
            root = os.getenv("SIGNAL_EDGE_SYNC_ROOT")
            base = (
                Path(root)
                if root
                else sweep_control.SCRIPTER_ROOT / "storage" / "edge_sync"
            )
            path = base / "remote_status.json"
            if not path.exists():
                return False, "Oracle helper: not set up yet"
            remote = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(remote, dict) or not remote:
                return False, "Oracle helper: not reporting yet"
            state = str(remote.get("state") or "unknown").lower()
            est = 0.0
            reps = remote.get("reports") or []
            if reps and isinstance(reps[0], dict):
                est = float(reps[0].get("estimated_channels_per_hour") or 0)
            claims_running = state in ("running", "collecting", "working", "active", "busy")
            # Freshness from the file mtime (when we last synced this status). A "running"
            # status that hasn't updated in a while means the node likely crashed/went offline,
            # so DON'T report it as running (else a dead Oracle shows a green dot forever).
            # Prefer the worker's OWN progress clock (updated_at) over the file mtime.
            # The sync task rewrites this file every couple of minutes even when the
            # content is byte-identical, so mtime stays fresh while the worker is
            # frozen — that is precisely how a 17-day outage kept showing a green dot.
            secs = None
            try:
                from datetime import datetime, timezone
                stamp = str(remote.get("updated_at") or "").strip()
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                secs = int(max(0, (
                    datetime.now(timezone.utc) - parsed
                ).total_seconds()))
            except Exception:  # noqa: BLE001
                secs = None
            if secs is None:
                try:
                    secs = int(max(0, time.time() - path.stat().st_mtime))
                except Exception:  # noqa: BLE001
                    pass
            STALE_SECS = 1800  # 30 min
            stale = secs is not None and secs >= STALE_SECS
            running = claims_running and not stale

            def _rel(s: int) -> str:
                if s < 60:
                    return "just now"
                if s < 3600:
                    return f"{s // 60}m ago"
                if s < 86400:
                    return f"{s // 3600}h ago"
                return f"{s // 86400}d ago"

            heard = _rel(secs) if secs is not None else ""
            bits = [f"Oracle helper: {state}" + (" (stale)" if stale else "")]
            if est and not stale:
                bits.append(f"~{int(est)}/hr")
            if heard:
                bits.append(f"last heard {heard}")
            return running, " · ".join(bits)
        except Exception:  # noqa: BLE001
            return False, "Oracle helper: status unavailable"

    def _discovery_status(self) -> dict:
        """Who is actually finding channels right now — the laptop worker + Oracle helper."""
        try:
            local = bool(sweep_control.is_running())
        except Exception:  # noqa: BLE001
            local = False
        smsg = ""
        try:
            smsg = str((sweep_control.read_status() or {}).get("message") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        orunning, otext = self._oracle_state()
        return {"local": local, "local_msg": smsg, "oracle_running": orunning,
                "oracle_text": otext, "any": local or orunning}

    def _who_is_finding(self, ds: dict) -> ft.Control:
        """A single honest 'who's discovering right now' summary: two engine chips + a
        headline that turns amber when NOTHING is running (so 'off' can't masquerade as 'on')."""
        def engine(label, running, detail):
            col = ACCENT if running else MUTED
            return ft.Container(
                ft.Column([
                    ft.Row([ft.Text("●" if running else "○", size=13, color=col),
                            ft.Text(label, size=12, weight=ft.FontWeight.BOLD, color=TEXT)],
                           spacing=6),
                    ft.Text(detail, size=10, color=MUTED, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=3, tight=True),
                bgcolor=PANEL, border_radius=8, padding=10, expand=True,
                border=ft.border.all(1, col if running else BORDER))
        laptop = (ds["local_msg"] or "running") if ds["local"] else "off — Start scraping above"
        chips = ft.Row([engine("This server" if ON_SERVER else "This laptop", ds["local"], laptop),
                        engine("Oracle helper", ds["oracle_running"], ds["oracle_text"])],
                       spacing=10)
        if ds["any"]:
            head = ft.Row([ft.Icon(ft.Icons.SENSORS, color=ACCENT, size=15),
                           muted("Finding channels now — enabled targets below are being "
                                 "scraped.", 11)], spacing=6, wrap=True)
        else:
            head = ft.Row([ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=AMBER, size=15),
                           ft.Text("Nothing is discovering right now — Start the scraper above "
                                   "(or turn on auto-start).", size=11, color=AMBER, expand=True)],
                          spacing=6)
        return ft.Column([muted("Who's finding channels right now", 11), head, chips],
                         spacing=8, tight=True)

    def _target_pick_topic(self, e) -> None:
        topic = self._topic_or_none(e.control.value)
        if topic:
            self._target_add(topic, kind="topic")

    def _target_add_typed(self, _=None) -> None:
        subj = (getattr(self, "_target_subject", "") or "").strip()
        if not subj:
            self.toast("Type a subject first (e.g. “deep sea exploration”).")
            return
        self._target_subject = ""
        self._target_add(subj, kind="subject")

    def _target_add(self, subject: str, *, kind: str) -> None:
        import scrape_targets

        def work():
            return scrape_targets.add_target(subject, kind=kind)

        def done(target):
            if not target:
                self.toast("Couldn't add that target.")
            else:
                tail = "" if sweep_control.is_running() else " — click Start scraping to begin"
                self.toast(f"🎯 Targeting “{target.get('label')}” — the scraper will focus "
                           f"here{tail}.")
            self._render()

        self.run_bg(work, done, f"Adding target “{subject}”…")

    def _target_toggle(self, tid, on) -> None:
        import scrape_targets

        def work():
            scrape_targets.set_enabled(tid, bool(on))
            return True

        self.run_bg(work, lambda _r: self._render(), "Updating target…")

    def _target_remove(self, tid) -> None:
        import scrape_targets

        def work():
            scrape_targets.remove_target(tid)
            return True

        def done(_r):
            self.toast("Target removed.")
            self._render()

        self.run_bg(work, done, "Removing target…")

    def _ch_scrape_more(self, _=None) -> None:
        subject = self._topic_or_none(self.ch_topic)
        kind = "topic"
        if not subject and self.ch_niche:
            subject = self.ch_niche.get("name")
            kind = "niche"
        if not subject:
            self.toast("Pick a Topic (or a niche) first, then “Scrape more of this”.")
            return
        # show a persistent confirmation banner (survives the re-render) so the click
        # visibly does something and the user knows WHERE the channels will show up.
        self._scrape_banner = {"label": subject, "kind": kind}
        self._target_add(subject, kind=kind)

    def _scrape_banner_card(self) -> ft.Control | None:
        """Persistent 'now scraping X' confirmation on Channels, with links to the results."""
        b = getattr(self, "_scrape_banner", None)
        if not b:
            return None
        label = str(b.get("label") or "this niche")
        is_topic = b.get("kind") == "topic"

        def watch(_):
            self._scrape_banner = None
            if is_topic:
                self.rt_topic = label
            self.rt_window = 30
            self.rt_shown = CH_PAGE
            self.section = "Real Time"
            self._render()

        def see(_):
            self._scrape_banner = None
            self.section = "Overview"
            self._render()

        dismiss = ft.IconButton(
            ft.Icons.CLOSE, icon_size=16, icon_color=MUTED, tooltip="Dismiss",
            on_click=lambda _e: (setattr(self, "_scrape_banner", None), self._render()))
        where = (f"under Topic = “{label}”" if is_topic else "under the matching Topic")
        return ft.Container(
            ft.Column([
                ft.Row([ft.Icon(ft.Icons.GPS_FIXED, color=ACCENT, size=18),
                        ft.Text(f"Now scraping “{label}”.", size=13,
                                weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Container(expand=True), dismiss],
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                muted(f"New channels will appear on the Real Time feed and {where} — they "
                      "take a cycle or two to be categorized first.", 11),
                ft.Row([
                    ft.OutlinedButton("Watch on Real Time", icon=ft.Icons.BOLT, on_click=watch,
                                      style=ft.ButtonStyle(color=ACCENT,
                                                           side=ft.BorderSide(1, ACCENT))),
                    ft.TextButton("See it on Overview", icon=ft.Icons.GPS_FIXED, on_click=see),
                ], spacing=10, wrap=True),
            ], spacing=8, tight=True),
            padding=14, border_radius=12, bgcolor=CARD, border=ft.border.all(1, ACCENT))

    def _ch_clear_all(self, _=None) -> None:
        # wipe every Channels narrowing back to the default browse (NO preset selected).
        self.ch_filters = {"sort": "opportunity_score", "limit": 60}
        self.ch_chip = None
        self.ch_topic = None
        self.ch_niche = None
        self.ch_view = "index"
        self.ch_shown = CH_PAGE
        self._render()

    def _render_channels(self) -> None:
        # reset paging back to the first page whenever the filter/sort/niche changed
        # (so a new filter starts small & snappy; "Show more" then pages deeper).
        base = self._ch_base_key()
        if base != self._ch_last_base:
            self._ch_last_base = base
            self.ch_shown = CH_PAGE
            self._scrape_banner = None      # a filter change means we've moved on

        nid = self._ch_niche_id()

        # niche selector + sort + the Filters button (the form lives in a modal now)
        nres = self._ensure("niches", lambda: self.client.call("list_niches"), "Loading niches…")
        niches = (nres.get("data") if nres and "data" in nres else []) or []

        def _nof(key):
            return next((n for n in niches
                         if str(n.get("id") or n.get("niche_id")) == key), None)

        niche_dd = ft.Dropdown(
            label="Niche", width=260, border_color=BORDER,
            value=str(nid) if nid else "",
            options=[ft.dropdown.Option(key="", text="All niches (whole index)")] + [
                ft.dropdown.Option(key=str(n.get("id") or n.get("niche_id")),
                                   text=str(n.get("name") or n.get("id")))
                for n in niches],
            on_change=lambda e: self._ch_set_niche(_nof(e.control.value)))
        topic_dd = self._topic_dd(
            self.ch_topic, lambda e: self._ch_set_topic(e.control.value))
        cur_sort = self.ch_filters.get("sort", "opportunity_score")
        sort_dd = ft.Dropdown(
            label="Sort by", width=230, border_color=BORDER, value=cur_sort,
            options=[ft.dropdown.Option(key=k, text=lbl) for lbl, k in CH_SORTS],
            on_change=lambda e: self._ch_set_sort(e.control.value))
        nfilt = self._active_filter_count()
        filters_btn = ft.OutlinedButton(
            f"Filters ({nfilt})" if nfilt else "Filters",
            icon=ft.Icons.TUNE, on_click=self._open_filters,
            style=ft.ButtonStyle(color=TEXT if nfilt else MUTED,
                                 side=ft.BorderSide(1, ACCENT if nfilt else BORDER)))
        # "Scrape more of this" only makes sense when there's a "this" — a Topic or a niche
        # selected. Otherwise it's DISABLED (greyed) so it visibly can't be used.
        can_target = bool(self._topic_or_none(self.ch_topic)) or bool(self.ch_niche)
        scrape_more = ft.OutlinedButton(
            "Scrape more of this", icon=ft.Icons.TRAVEL_EXPLORE,
            on_click=self._ch_scrape_more, disabled=not can_target,
            tooltip=None if can_target else "Pick a Topic or a niche first to scrape more of it",
            style=ft.ButtonStyle(color=ACCENT if can_target else MUTED,
                                 side=ft.BorderSide(1, ACCENT if can_target else BORDER)))

        toolbar = [
            ft.Row([
                ft.Icon(ft.Icons.TUNE, size=18, color=ACCENT2),
                h("Research controls", 16),
                ft.Container(expand=True),
                self.pill(
                    "Faceless + replicable only",
                    ACCENT,
                    ft.Icons.VERIFIED_OUTLINED,
                ),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([niche_dd, topic_dd, sort_dd, filters_btn, scrape_more], wrap=True,
                   spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        if self.ch_niche:
            nname = self.ch_niche.get("name") or "this niche"
            view_btns = []
            for label, view in (("This niche's channels", "niche"), ("Whole index", "index")):
                on = self.ch_view == view
                view_btns.append(ft.OutlinedButton(
                    label, on_click=lambda _, v=view: self._ch_set_view(v),
                    style=ft.ButtonStyle(bgcolor=CARD if on else None,
                                         side=ft.BorderSide(1, ACCENT if on else BORDER))))
            toolbar.append(ft.Row(view_btns, wrap=True, spacing=8))
            toolbar.append(muted(f"Showing “{nname}”. Presets & filters still apply on top.", 11))
        else:
            toolbar.append(muted("Pick a niche to narrow to its channels, or browse the whole index.", 11))

        # presets (a preset fills the filters; then tweak in the modal)
        chips = []
        for name, preset in CH_CHIPS:
            active = self.ch_chip == name
            chips.append(ft.OutlinedButton(
                name, on_click=lambda _, n=name, p=preset: self._ch_pick_chip(n, p),
                style=ft.ButtonStyle(
                    bgcolor=ACCENT_SOFT if active else None,
                    color=ACCENT_DARK if active else MUTED,
                    side=ft.BorderSide(1, ACCENT if active else BORDER),
                    shape=ft.RoundedRectangleBorder(radius=18),
                )))
        toolbar.append(ft.Row(chips, wrap=True, spacing=8))

        # active-filter chips (click a chip to remove that one filter)
        extras = []
        for k, v in list(self.ch_filters.items()):
            if k in ("sort", "limit"):
                continue
            lbl = self._ch_filter_label(k, v)
            if not lbl:
                continue
            extras.append(ft.Container(
                ft.Row([ft.Text(lbl, size=11, color=ACCENT2),
                        ft.Icon(ft.Icons.CLOSE, size=13, color=ACCENT2)], spacing=4, tight=True),
                padding=ft.padding.symmetric(3, 9), border_radius=20,
                bgcolor=ACCENT2_SOFT, ink=True,
                on_click=lambda _, kk=k: self._ch_remove_filter(kk)))
        if extras:
            toolbar.append(ft.Row([muted("Active:", 11)] + extras, wrap=True, spacing=6,
                                  vertical_alignment=ft.CrossAxisAlignment.CENTER))

        self.content.controls.append(self.card(*toolbar))

        banner = self._scrape_banner_card()
        if banner is not None:
            self.content.controls.append(banner)

        if nid is not None:
            self.content.controls.append(self._niche_brief_card(nid))

        # load + results
        entry = self._ensure(self._channels_key(), self._load_channels, "Loading channels…")
        if entry is None:
            self.content.controls.append(self._loading_note("Loading channels…"))
            return
        if "error" in entry:
            self.content.controls.append(self._error_card(entry["error"]))
            return
        rows = entry["data"] or []
        more = len(rows) >= self.ch_shown   # a full page came back → more likely exist

        scope = (f"in “{self.ch_niche.get('name')}”" if self._ch_in_niche_view()
                 else "across the whole index")
        count = ft.Text(f"{len(rows):,}{'+' if more else ''} channels", size=17,
                        weight=ft.FontWeight.W_700, color=TEXT)
        tail = "showing the top — Show more below" if more else "all matches loaded"
        sub = muted(f"{scope} · sorted by {cur_sort.replace('_', ' ')} · {tail}", 11)
        head = ft.Row([count, sub], wrap=True, spacing=8,
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)
        if not rows:
            # Smart empty-state (same idea as Real Time): filters COMBINE, so name what's
            # active + offer one-click recovery, instead of a bare "Nothing here yet."
            nfilt = self._active_filter_count()
            narrowed = nfilt > 0 or bool(self.ch_topic) or bool(self.ch_niche)
            if narrowed:
                bits = []
                if self.ch_niche:
                    bits.append(f"niche “{self.ch_niche.get('name')}”")
                if self.ch_topic:
                    bits.append(f"topic “{self.ch_topic}”")
                if nfilt:
                    bits.append(f"{nfilt} metric filter{'s' if nfilt != 1 else ''}")
                why = " + ".join(bits)
                note = muted(
                    f"No channels match your current filters ({why}). These filters COMBINE "
                    "— stacking them makes a very narrow slice that can genuinely be empty. "
                    "Remove a filter above, or clear them all.")
                clear = ft.OutlinedButton(
                    "Clear all filters", icon=ft.Icons.FILTER_ALT_OFF,
                    on_click=self._ch_clear_all,
                    style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))
                self.content.controls.append(self.card(head, note, clear))
            else:
                self.content.controls.append(self.card(
                    head, muted("Nothing here yet. The scraper adds channels continuously — "
                                "check back in a bit, or widen your search.")))
            return
        body = [
            ft.Container(head, padding=ft.padding.only(left=4, right=4)),
            self.card_grid(rows, "channel", limit=len(rows)),
        ]
        if more:
            body.append(ft.Row([ft.OutlinedButton(
                f"Show more (+{CH_PAGE})", icon=ft.Icons.EXPAND_MORE,
                on_click=self._ch_show_more,
                style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))],
                alignment=ft.MainAxisAlignment.CENTER))
        self.content.controls.append(ft.Column(body, spacing=16, tight=True))

    # -- Real Time (live feed of newly-discovered channels) -----------------
    def _rt_query_args(self) -> dict:
        """recent_channels args: the window + the Real Time metric/topic filters."""
        args = {k: v for k, v in self.rt_filters.items() if not k.startswith("_")}
        args["replicable_only"] = True
        args["faceless_only"] = True
        floor = self.rt_filters.get("_min_outlier")
        if floor is not None:
            args["min_outlier_score"] = floor
        topic = self._topic_or_none(self.rt_topic)
        if topic:
            args["topic"] = topic
        if self.rt_window is not None:
            args["days"] = self.rt_window
        args["limit"] = self.rt_shown
        return args

    def _rt_key(self) -> str:
        args = self._rt_query_args()
        return "realtime:" + "|".join(f"{k}={args[k]}" for k in sorted(args)
                                      if args[k] is not None)

    def _rt_set_window(self, val) -> None:
        self.rt_window = None if val == "all" else int(val)
        self.rt_shown = CH_PAGE                    # new window starts at the first page
        self._render()

    def _rt_show_more(self, _=None) -> None:
        self.rt_shown += CH_PAGE
        self._render()

    def _load_realtime(self) -> list[dict]:
        rows = self._finder_rows("recent_channels", self._rt_query_args())
        if any(not r.get("avatar_url") for r in rows):
            self._attach_avatars(rows)
        return rows

    def _rt_set_topic(self, topic) -> None:
        self.rt_topic = self._topic_or_none(topic)
        self.rt_shown = CH_PAGE
        self._render()

    def _rt_toggle_faceless(self) -> None:
        if self.rt_filters.get("faceless_only"):
            self.rt_filters.pop("faceless_only", None)
        else:
            self.rt_filters["faceless_only"] = True
        self.rt_shown = CH_PAGE
        self._render()

    def _rt_remove_filter(self, key: str) -> None:
        self.rt_filters.pop(key, None)
        self.rt_chip = "Custom"
        self.rt_shown = CH_PAGE
        self._render()

    def _rt_clear_all(self, _=None) -> None:
        # wipe every Real-Time narrowing: metric filters, topic, back to a wide window.
        self.rt_filters = {}
        self.rt_topic = None
        self.rt_chip = None
        self.rt_window = 30
        self.rt_shown = CH_PAGE
        self._render()

    def _rt_pick_chip(self, name: str, preset: dict) -> None:
        # Presets TOGGLE (same as Channels): clicking the active one deselects it.
        # A preset REPLACES the metric filters; RT always sorts by discovery time, so
        # drop the preset's 'sort' key (it doesn't apply to the newest-first feed).
        if self.rt_chip == name:
            self.rt_filters = {}
            self.rt_chip = None
        else:
            self.rt_filters = {k: v for k, v in preset.items() if k != "sort"}
            self.rt_chip = name
        self.rt_shown = CH_PAGE
        self._render()

    @staticmethod
    def _topic_or_none(topic) -> str | None:
        """Map every UI/legacy representation of "all topics" to no filter."""
        if topic is None:
            return None
        value = str(topic).strip()
        if not value or value == ALL_TOPICS or value.casefold() == "all topics":
            return None
        return value

    def _topic_dd(self, current, on_change):
        """Shared Topic dropdown (Channels + Real Time), populated from list_topics."""
        tres = self._ensure("topics", lambda: self.client.call("list_topics"), "Loading topics…")
        topics = (tres.get("data") if tres and "data" in tres else []) or []
        return ft.Dropdown(
            label="Topic", width=240, border_color=BORDER,
            value=self._topic_or_none(current) or ALL_TOPICS,
            options=[ft.dropdown.Option(key=ALL_TOPICS, text="All topics")] + [
                ft.dropdown.Option(key=str(t.get("topic")),
                                   text=f"{t.get('topic')} ({t.get('count'):,})")
                for t in topics],
            on_change=on_change)

    def _rt_tick(self) -> None:
        """One silent refresh: reload newest channels, and only disturb the view
        (re-render) when the top channel actually changed — so scroll isn't reset
        every minute unless there's genuinely something new."""
        key = self._rt_key()
        gen = self._epoch.get(key, 0)      # honor the same invalidation contract as _ensure

        def work():
            try:
                data = self._load_realtime()
            except Exception:  # noqa: BLE001
                return
            # drop a stale tick if the window switched OR the key was invalidated
            # (e.g. a Refresh) while this load was in flight — don't clobber fresh data.
            if self._rt_key() != key or self._epoch.get(key, 0) != gen:
                return
            prev = (self.cache.get(key) or {}).get("data") or []
            self.cache[key] = self._cache_entry({"data": data})
            top_new = data[0].get("channel_id") if data else None
            top_old = prev[0].get("channel_id") if prev else None
            if self.section == "Real Time" and top_new != top_old:
                try:
                    self._render()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=work, daemon=True).start()

    def _watch_realtime(self) -> None:
        """Start the once-only 60s auto-refresh loop (quiet — no busy spinner)."""
        if self._rt_watch_started:
            return
        self._rt_watch_started = True

        stop = self._session_stop

        def worker():
            while not stop.wait(60):
                if self.section == "Real Time":
                    self._rt_tick()
        threading.Thread(target=worker, daemon=True).start()

    def _render_realtime(self) -> None:
        self._watch_realtime()      # kick off the 60s auto-refresh once

        opts = [("1", "Last 24h"), ("7", "Last 7 days"),
                ("30", "Last 30 days"), ("all", "Newest")]
        sel = "all" if self.rt_window is None else str(self.rt_window)
        win_seg = ft.SegmentedButton(
            allow_multiple_selection=False, allow_empty_selection=False,
            selected={sel},
            segments=[ft.Segment(value=v, label=ft.Text(lbl)) for v, lbl in opts],
            on_change=lambda e: self._rt_set_window(next(iter(e.control.selected), "1")))
        topic_dd = self._topic_dd(
            self.rt_topic, lambda e: self._rt_set_topic(e.control.value))
        nfilt = self._active_filter_count(self.rt_filters)
        filters_btn = ft.OutlinedButton(
            f"Filters ({nfilt})" if nfilt else "Filters", icon=ft.Icons.TUNE,
            on_click=lambda e: self._open_filters(target="realtime"),
            style=ft.ButtonStyle(color=TEXT if nfilt else MUTED,
                                 side=ft.BorderSide(1, ACCENT if nfilt else BORDER)))
        header = [
            ft.Row([
                ft.Icon(ft.Icons.BOLT, size=18, color=ACCENT),
                h("Discovery window", 16),
                self.pill("live", ACCENT, ft.Icons.RADIO_BUTTON_CHECKED),
                ft.Container(expand=True),
                self.pill(
                    "Faceless + replicable only",
                    ACCENT,
                    ft.Icons.VERIFIED_OUTLINED,
                ),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([muted("Window:", 12), win_seg], spacing=10, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([topic_dd, filters_btn], wrap=True, spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=15, color=ACCENT2),
                    ft.Text(
                        "Discovery time is when Beacon found a channel. "
                        "Channel age is a separate metric.",
                        size=11,
                        color=MUTED,
                    ),
                ], spacing=8),
                padding=ft.padding.symmetric(9, 11),
                border_radius=11,
                bgcolor=ACCENT2_SOFT,
            ),
        ]
        # preset chips (same set as Channels; a preset replaces the metric filters)
        chips = []
        for name, preset in CH_CHIPS:
            active = self.rt_chip == name
            chips.append(ft.OutlinedButton(
                name, on_click=lambda _, n=name, p=preset: self._rt_pick_chip(n, p),
                style=ft.ButtonStyle(
                    bgcolor=ACCENT_SOFT if active else None,
                    color=ACCENT_DARK if active else MUTED,
                    side=ft.BorderSide(1, ACCENT if active else BORDER),
                    shape=ft.RoundedRectangleBorder(radius=18),
                )))
        header.append(ft.Row(chips, wrap=True, spacing=8))
        # active-filter chips (click a chip to remove that filter) — same combining as Channels
        extras = []
        for k, v in list(self.rt_filters.items()):
            lbl = self._ch_filter_label(k, v)
            if not lbl:
                continue
            extras.append(ft.Container(
                ft.Row([ft.Text(lbl, size=11, color=ACCENT2),
                        ft.Icon(ft.Icons.CLOSE, size=13, color=ACCENT2)], spacing=4, tight=True),
                padding=ft.padding.symmetric(3, 9), border_radius=20,
                bgcolor=ACCENT2_SOFT, ink=True,
                on_click=lambda _, kk=k: self._rt_remove_filter(kk)))
        if extras:
            header.append(ft.Row([muted("Active:", 11)] + extras, wrap=True, spacing=6,
                                  vertical_alignment=ft.CrossAxisAlignment.CENTER))
        try:
            if not sweep_control.is_running():
                header.append(muted("The background scraper is off — start it on Overview "
                                    "to keep new channels flowing in here.", 11))
        except Exception:  # noqa: BLE001
            pass
        self.content.controls.append(self.card(*header))

        entry = self._ensure(self._rt_key(), self._load_realtime, "Loading newest channels…")
        if entry is None:
            self.content.controls.append(self._loading_note("Loading newest channels…"))
            return
        if "error" in entry:
            self.content.controls.append(self._error_card(entry["error"]))
            return
        rows = entry["data"] or []

        more = len(rows) >= self.rt_shown
        window_lbl = ("added in the last 24h" if self.rt_window == 1 else
                      f"added in the last {self.rt_window} days" if self.rt_window
                      else "most recently added")
        count = ft.Text(f"Newest {len(rows):,}{'+' if more else ''} channels", size=17,
                        weight=ft.FontWeight.W_700, color=TEXT)
        sub = muted(f"{window_lbl} · refreshes every 60s", 11)
        head = ft.Row([count, sub], wrap=True, spacing=8,
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)
        if not rows:
            nfilt = self._active_filter_count(self.rt_filters)
            narrowed = nfilt > 0 or self.rt_topic
            if narrowed:
                bits = []
                if self.rt_topic:
                    bits.append(f"topic “{self.rt_topic}”")
                if nfilt:
                    bits.append(f"{nfilt} metric filter{'s' if nfilt != 1 else ''}")
                why = " + ".join(bits)
                note = muted(
                    f"No channels match your current filters ({why}) in this window. "
                    "These filters COMBINE — stacking them makes a very narrow slice "
                    "that can genuinely be empty. Widen the window, or clear the filters.")
                clear = ft.OutlinedButton(
                    "Clear all filters", icon=ft.Icons.FILTER_ALT_OFF,
                    on_click=self._rt_clear_all,
                    style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))
                self.content.controls.append(self.card(head, note, clear))
            else:
                self.content.controls.append(self.card(
                    head, muted("Nothing added in this window yet. Try a wider window, or "
                                "start the background scraper on Overview.")))
            return
        body = [
            ft.Container(head, padding=ft.padding.only(left=4, right=4)),
            self.card_grid(rows, "channel", limit=len(rows)),
        ]
        if more:
            body.append(ft.Row([ft.OutlinedButton(
                f"Show more (+{CH_PAGE})", icon=ft.Icons.EXPAND_MORE,
                on_click=self._rt_show_more,
                style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))],
                alignment=ft.MainAxisAlignment.CENTER))
        self.content.controls.append(ft.Column(body, spacing=16, tight=True))

    # -- Saved: channels you found yourself on YouTube --------------------------------
    def _growth_pill(self, row: dict) -> ft.Control | None:
        """'+5.1K subs since saved · 2.3K/day' when a channel has been measured more than once.

        Nothing is shown from a single reading: one snapshot is a number, not a trend, and
        a fabricated '+0' would read as "flat" when the truth is "not measured yet".
        The growth arrives on the row itself (beacon_save.saved_channels), so a failed
        refresh can no longer wipe it and opening the tab shows it straight away.
        """
        g = beacon_save.growth_summary(row)
        if not g:
            return None
        gained, per_day = g["gained"], g["per_day"]
        rate = f" · {human_num(per_day)}/day" if per_day >= 1 else ""
        up = gained > 0
        return self.pill(
            f"{'+' if up else ''}{human_num(gained)} subs {g['since']}{rate}",
            ACCENT if up else MUTED,
            ft.Icons.TRENDING_UP if up else ft.Icons.TRENDING_DOWN,
        )

    def _saved_actions(self, row: dict) -> list:
        """Per-card controls on the saved shelf: read the evidence, or drop the channel."""
        cid = str(row.get("channel_id") or "")
        name = str(row.get("title") or cid)
        growth = self._growth_pill(row)
        return ([growth] if growth else []) + [
            ft.OutlinedButton(
                "Analysis",
                icon=ft.Icons.INSIGHTS,
                on_click=lambda _, c=cid, n=name, a=row.get("avatar_url"):
                    self._open_channel_analysis(c, n, a),
                style=ft.ButtonStyle(color=ACCENT2, side=ft.BorderSide(1, BORDER),
                                     shape=ft.RoundedRectangleBorder(radius=10)),
            ),
            ft.OutlinedButton(
                "Remove",
                icon=ft.Icons.BOOKMARK_REMOVE,
                on_click=lambda _, c=cid, n=name: self._remove_saved(c, n),
                style=ft.ButtonStyle(color=WARN, side=ft.BorderSide(1, BORDER),
                                     shape=ft.RoundedRectangleBorder(radius=10)),
            ),
        ]

    def _remove_saved(self, channel_id: str, name: str) -> None:
        """Take a channel off the shelf. Nothing is deleted — see beacon_save.remove_saved."""
        nid = self._saved_niche_id
        if not nid or not channel_id:
            return

        def work():
            return beacon_save.remove_saved(self.client, nid, channel_id)

        def done(_res):
            self._saved_result = None          # the old "Saved X" banner is now misleading
            self._invalidate(self._saved_key())
            self.toast(f"Removed {name} — the channel itself is still in your index.")
            if self.section == "Saved":
                self._render()

        self.run_bg(work, done, f"Removing {name}…")

    def _video_row(self, v: dict) -> ft.Control:
        """One upload in the analysis list: what it was called and how it did."""
        vid = str(v.get("video_id") or "")
        views = human_num(v.get("views"))
        secs = v.get("duration_seconds")
        length = fmt_duration(secs) if secs else ""
        published = str(v.get("published_at_normalized") or "")[:10]
        meta = " · ".join(x for x in (f"{views} views" if views else "", length, published) if x)
        return ft.Container(
            ft.Row([
                ft.Column([
                    ft.Text(str(v.get("title") or "—"), size=12, color=TEXT,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(meta, size=10, color=MUTED),
                ], spacing=2, tight=True, expand=True),
                ft.Icon(ft.Icons.OPEN_IN_NEW, size=13, color=MUTED),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(8, 10),
            border_radius=9,
            bgcolor=SURFACE_ALT,
            ink=True,
            on_click=self._open_url(f"https://www.youtube.com/watch?v={vid}" if vid else ""),
        )

    def _open_channel_analysis(self, channel_id: str, name: str,
                               avatar_url: str | None = None) -> None:
        """Open the deep read on one channel.

        The card answers "is this worth my time?". This answers "what is actually working
        here?" — the best-performing uploads in order, which is how a format is learned.
        Loaded on demand: pulling a video list for every card would make the grid crawl.
        """
        def work():
            return beacon_save.channel_analysis(self.client, channel_id)

        def done(details):
            videos = (details or {}).get("top_videos") or []
            subs = human_num(details.get("subscribers"))
            uploads = human_num(details.get("video_count"))
            topic = details.get("topic") or "—"
            verdict = str(details.get("face_verdict") or "unchecked").replace("_", "-")
            desc = (details.get("description") or "").strip()

            head = ft.Row([
                # get_channel_details drops raw_json, which is where the avatar lives,
                # so it never carries one. The card already has it.
                self._avatar(name, 44, avatar_url or details.get("avatar_url")),
                ft.Column([
                    ft.Text(name, size=16, weight=ft.FontWeight.W_700, color=TEXT,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(f"{topic} · {subs} subscribers · {uploads} uploads · {verdict}",
                            size=11, color=MUTED, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=2, tight=True, expand=True),
            ], spacing=10)

            body: list[ft.Control] = [head, ft.Divider(color=BORDER, height=1)]
            if desc:
                body.append(ft.Container(
                    ft.Text(desc[:400] + ("…" if len(desc) > 400 else ""),
                            size=11, color=MUTED, selectable=True),
                    padding=10, border_radius=9, bgcolor=SURFACE_ALT))
            if videos:
                body.append(ft.Text(
                    f"Best-performing uploads ({len(videos)})", size=12,
                    weight=ft.FontWeight.W_700, color=TEXT))
                body.append(muted(
                    "Sorted by views. This is the format doing the work — the titles and "
                    "lengths that earned this channel its numbers.", 11))
                body.extend(self._video_row(v) for v in videos)
            else:
                body.append(muted(
                    "No uploads catalogued yet. The organizer pulls a channel's videos on "
                    "its next cycle — reopen this in a few minutes.", 11))

            dlg = ft.AlertDialog(
                title=ft.Text("Channel analysis", size=15, weight=ft.FontWeight.W_700,
                              color=TEXT),
                content=ft.Container(
                    ft.Column(body, spacing=10, tight=True, scroll=ft.ScrollMode.AUTO),
                    width=560, height=520),
                actions=[
                    ft.TextButton("Open on YouTube", on_click=self._open_url(
                        details.get("url")
                        or f"https://www.youtube.com/channel/{channel_id}")),
                    ft.FilledButton("Close", on_click=lambda _: self._close_dialog(dlg),
                                    style=ft.ButtonStyle(bgcolor=ACCENT, color=NAV_BG)),
                ],
                bgcolor=PANEL,
            )
            self.page.open(dlg)

        self.run_bg(work, done, f"Reading {name}…")

    def _saved_key(self) -> str:
        return f"saved:{self.saved_shown}"

    # How old saved numbers may get before opening the tab quietly refreshes them. Saved
    # channels are the fast movers by definition, so a stale count is actively misleading;
    # 30 min keeps it honest without spending API quota every time the tab is opened.
    SAVED_STALE_SECONDS = 30 * 60
    # After a FAILED check, how long before opening/re-rendering the tab tries again on its
    # own. The automatic check runs on every render of this tab, so without a pause a busy
    # database would be retried in a loop for as long as the tab stayed open. The Refresh
    # live button ignores this and always tries.
    SAVED_RETRY_SECONDS = 2 * 60

    def _saved_refresh(self, _=None, *, silent: bool = False) -> None:
        """Pull live numbers for every saved channel and record a growth point."""
        nid = self._saved_niche_id
        if not nid or getattr(self, "_saved_refreshing", False):
            return
        self._saved_refreshing = True

        def work():
            try:
                return beacon_save.refresh_saved(self.client, nid)
            except Exception as exc:  # noqa: BLE001 — surfaced in the card, not swallowed
                return {"error": str(exc)}

        def done(res):
            # Released here rather than in work(): run_bg calls this right after work on the
            # same thread, and clearing it earlier left a gap in which a background re-render
            # could start a second refresh before this one had been recorded.
            self._saved_refreshing = False
            if not isinstance(res, dict) or res.get("error"):
                # A failed check must not claim the numbers were checked. Stamping
                # _saved_refreshed_at here used to print "checked just now" over stale
                # numbers AND switch off the automatic retry for 30 minutes.
                self._saved_refresh_failed_at = time.time()
                reason = res.get("error") if isinstance(res, dict) else res
                self._saved_result = {"refresh_error": str(reason)}
            elif silent:
                self._saved_refreshed_at = time.time()
                if (self._saved_result or {}).get("refresh_error"):
                    self._saved_result = None      # the old failure no longer applies
            else:
                self._saved_refreshed_at = time.time()
                moved, gained = res.get("changed") or 0, res.get("subs_gained") or 0
                if moved:
                    sign = "+" if gained >= 0 else ""
                    self._saved_result = {"refreshed": True, "text":
                                          f"Updated {moved} channel"
                                          f"{'s' if moved != 1 else ''} · "
                                          f"{sign}{human_num(gained)} subscribers"}
                else:
                    self._saved_result = {"refreshed": True,
                                          "text": "Already up to date."}
            self._invalidate(self._saved_key())
            self.toast("")
            if self.section == "Saved":
                self._render()

        # The automatic check is quiet; its own button still reads "Checking…".
        self.run_bg(work, done, "Checking live numbers…", quiet=silent)
        if not silent:
            self._render()

    def _saved_autorefresh(self) -> None:
        """Refresh on opening the tab when the numbers have gone stale.

        Silent: the operator asked to look at their shelf, not to run a job. If nothing has
        moved they should see nothing happen.
        """
        now = time.time()
        if now - getattr(self, "_saved_refreshed_at", 0.0) < self.SAVED_STALE_SECONDS:
            return
        if now - getattr(self, "_saved_refresh_failed_at", 0.0) < self.SAVED_RETRY_SECONDS:
            return
        if getattr(self, "_saved_refreshing", False) or not self._saved_niche_id:
            return
        self._saved_refresh(silent=True)

    def _saved_show_more(self, _=None) -> None:
        self.saved_shown += CH_PAGE
        self._render()

    def _load_saved(self) -> list[dict]:
        """The saved shelf. Resolving the workspace is part of the load so the very
        first visit (before anything is saved) doesn't create a workspace as a side
        effect of merely looking at the page."""
        nid = self._saved_niche_id
        if nid is None:
            for row in (self.client.call("list_niches") or []):
                if str(row.get("name", "")).strip().lower() == \
                        beacon_save.SAVED_NICHE_NAME.lower():
                    nid = int(row.get("id") or row.get("niche_id"))
                    break
        if nid is None:
            return []
        self._saved_niche_id = nid
        return beacon_save.saved_channels(self.client, nid, limit=self.saved_shown)

    def _save_link(self, _=None) -> None:
        link = (getattr(self, "_saved_draft", "") or "").strip()
        if not link:
            self.toast("Paste a YouTube channel or video link first.")
            return
        if getattr(self, "_saving_link", False):
            return                      # one save at a time; each costs an API lookup
        self._saving_link = True
        self._saved_result = None

        def work():
            try:
                return beacon_save.save_channel(self.client, link,
                                                niche_id=self._saved_niche_id)
            except Exception as exc:  # noqa: BLE001
                # Returned, not raised. run_bg reports a failure only through the status
                # bar at the very bottom of the window — which the taskbar covers — so a
                # dead link would look like nothing happened at all. Carry the reason
                # back and show it in the card, where the operator is already looking.
                return {"error": str(exc), "link": link}
            finally:
                # run_bg skips on_done when the work raises, so the button has to be
                # released here or one bad link disables saving until restart.
                self._saving_link = False

        def done(res):
            self._saved_result = res
            failed = isinstance(res, dict) and res.get("error")
            if not failed:
                # Keep the link in the box when it FAILED, so a fixable typo can be
                # corrected instead of retyped from scratch.
                self._saved_draft = ""
                self._saved_niche_id = res.get("niche_id") or self._saved_niche_id
                self._invalidate(self._saved_key())
            self.toast("")
            if self.section == "Saved":
                self._render()

        self.run_bg(work, done, "Looking up the channel and pulling its numbers…")
        self._render()                  # show the disabled/working button immediately

    def _saved_result_card(self) -> ft.Control | None:
        """What the last save produced — the answer to 'is this find any good?'."""
        res = getattr(self, "_saved_result", None)
        if not res:
            return None
        if res.get("refreshed"):
            return ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.SYNC, size=17, color=ACCENT_DARK),
                    ft.Text(str(res.get("text") or "Updated."), size=12, color=TEXT,
                            expand=True),
                ], spacing=9),
                padding=11, border_radius=11, bgcolor=ACCENT_SOFT,
            )
        if res.get("refresh_error"):
            # Its own banner. This used to fall through to "Could not save that link",
            # which blamed a save nobody had attempted.
            reason = str(res["refresh_error"])
            why = ("The index was busy the whole time Beacon waited — the background "
                   "scraper was writing to it. " if beacon_save.is_busy(reason)
                   else f"{reason[:180]} ")
            return ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.SYNC_PROBLEM, size=17, color=WARN),
                    ft.Column([
                        ft.Text("Couldn't refresh live numbers", size=13,
                                weight=ft.FontWeight.W_700, color=WARN),
                        ft.Text(why + "The cards show the last numbers Beacon had. "
                                "Press Refresh live to try again.",
                                size=11, color=MUTED, selectable=True),
                    ], spacing=1, tight=True, expand=True),
                ], spacing=9),
                padding=11, border_radius=11, bgcolor=WARN_SOFT,
            )
        if res.get("error"):
            return ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.ERROR_OUTLINE, size=17, color=WARN),
                    ft.Column([
                        ft.Text("Could not save that link", size=13,
                                weight=ft.FontWeight.W_700, color=WARN),
                        ft.Text(str(res["error"])[:220], size=11, color=MUTED,
                                selectable=True),
                    ], spacing=1, tight=True, expand=True),
                ], spacing=9),
                padding=11, border_radius=11, bgcolor=WARN_SOFT,
            )
        ch = res.get("channel") or {}
        title = ch.get("title") or res.get("channel_id", "")
        new = res.get("was_new")
        return ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.CHECK_CIRCLE, size=17, color=ACCENT_DARK),
                ft.Column([
                    ft.Text(f"Saved {title}", size=13, weight=ft.FontWeight.W_700,
                            color=TEXT),
                    muted(
                        "New to your index — the crawler had never seen it."
                        if new else
                        "Already in your index; refreshed its numbers and filed it here.",
                        11),
                ], spacing=1, tight=True, expand=True),
            ], spacing=9),
            padding=11, border_radius=11, bgcolor=ACCENT_SOFT,
        )

    def _render_saved(self) -> None:
        field = ft.TextField(
            hint_text="Paste a YouTube link — the channel page or any of its videos",
            value=getattr(self, "_saved_draft", ""),
            expand=True,
            border_color=BORDER,
            bgcolor=SURFACE_ALT,
            on_change=lambda e: setattr(self, "_saved_draft", e.control.value),
            on_submit=lambda _: self._save_link(),
        )
        saving = getattr(self, "_saving_link", False)

        capture = self.card(
            ft.Row([
                ft.Container(
                    ft.Icon(ft.Icons.BOOKMARK_ADD, size=19, color=ACCENT_DARK),
                    width=38, height=38, border_radius=12, bgcolor=ACCENT_SOFT,
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    h("Save a channel you found", 17),
                    muted("Paste the link. Beacon adds it to your index and scores it "
                          "on the same evidence as everything else.", 11),
                ], spacing=1, tight=True, expand=True),
            ], spacing=10),
            ft.Row([
                field,
                ft.FilledButton(
                    "Saving…" if saving else "Save & analyse",
                    icon=ft.Icons.BOOKMARK_ADD,
                    on_click=self._save_link,
                    disabled=saving,
                    style=ft.ButtonStyle(
                        bgcolor=ACCENT, color=NAV_BG,
                        shape=ft.RoundedRectangleBorder(radius=11),
                        padding=ft.padding.symmetric(12, 18),
                    ),
                ),
            ], spacing=10),
            self._saved_result_card() or ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=15, color=ACCENT2),
                    ft.Text("A video link works too — Beacon looks up which channel it "
                            "belongs to. Saving the same channel twice just refreshes it.",
                            size=11, color=MUTED, expand=True),
                ], spacing=8),
                padding=ft.padding.symmetric(9, 11), border_radius=10,
                bgcolor=ACCENT2_SOFT,
            ),
        )
        self.content.controls.append(capture)

        entry = self._ensure(self._saved_key(), self._load_saved,
                             "Loading your saved channels…")
        if entry is None:
            self.content.controls.append(self._loading_note("Loading your saved channels…"))
            return
        if "error" in entry:
            self.content.controls.append(self._error_card(entry["error"]))
            return
        rows = entry.get("data") or []
        if rows:
            self._saved_autorefresh()
        if not rows:
            self.content.controls.append(self.card(
                h("Nothing saved yet", 16),
                muted("When a channel catches your eye on YouTube, paste its link above. "
                      "It joins your index, gets a topic, and is scored like every other "
                      "channel — so you can tell a real opportunity from a big name.", 11),
            ))
            return

        last = getattr(self, "_saved_refreshed_at", 0.0)
        failed_at = getattr(self, "_saved_refresh_failed_at", 0.0)
        now = time.time()
        if failed_at > last:
            fresh = f"live check failed {self._ago_seconds(now - failed_at)}"
            fresh += (f" · last good check {self._ago_seconds(now - last)}" if last
                      else " · showing stored numbers")
        elif last:
            fresh = f"live numbers checked {self._ago_seconds(now - last)}"
        else:
            fresh = "live numbers not checked yet this session"
        refreshing = getattr(self, "_saved_refreshing", False)
        self.content.controls.append(ft.Row([
            ft.Container(
                self.section_heading(
                    f"{len(rows)} saved channel{'s' if len(rows) != 1 else ''}",
                    "Your own finds, scored on the same evidence as the crawler's. "
                    "The opportunity gate is NOT applied here — you chose these, so they "
                    "all show.",
                    ft.Icons.BOOKMARK,
                ),
                expand=True,
            ),
            ft.Column([
                ft.OutlinedButton(
                    "Checking…" if refreshing else "Refresh live",
                    icon=ft.Icons.SYNC,
                    on_click=self._saved_refresh,
                    disabled=refreshing,
                    style=ft.ButtonStyle(color=ACCENT2, side=ft.BorderSide(1, BORDER),
                                         shape=ft.RoundedRectangleBorder(radius=11)),
                ),
                muted(fresh, 10),
            ], spacing=3, tight=True,
               horizontal_alignment=ft.CrossAxisAlignment.END),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12))
        self.content.controls.append(
            self.card_grid(rows, kind="channel", limit=self.saved_shown,
                           niche_id=self._saved_niche_id,
                           actions_for=self._saved_actions, saved=True))
        if len(rows) >= self.saved_shown:
            self.content.controls.append(ft.OutlinedButton(
                f"Show more (+{CH_PAGE})",
                icon=ft.Icons.EXPAND_MORE,
                on_click=self._saved_show_more,
                style=ft.ButtonStyle(color=MUTED, side=ft.BorderSide(1, BORDER),
                                     shape=ft.RoundedRectangleBorder(radius=11)),
            ))

    def _find_key(self) -> str:
        return f"find_channels:{self.find_shown}"   # limit in the key → Show more re-loads

    def _find_show_more(self, _=None) -> None:
        self.find_shown += CH_PAGE
        self._render()

    def _find_sweep_summary(self) -> ft.Control | None:
        result = getattr(self, "_find_sweep_result", None)
        error = getattr(self, "_find_sweep_error", None)
        if not result and not error:
            return None
        subject = (result or {}).get("subject") or getattr(self, "_find_draft", "")
        if error:
            return self.card(
                h(f"Sweep for {subject} could not finish", 16),
                muted(error, 11),
                muted("Your earlier discoveries remain available. Try the sweep again.", 11),
            )
        queries = int(result.get("queries_run") or 0)
        failures = int(result.get("errors") or 0)
        leads = list(result.get("top_channels") or [])[:8]
        body: list[ft.Control] = [
            h(f"Sweep results for {subject}", 17),
            muted(f"{queries} search angles checked · {result.get('videos_found', 0)} videos found "
                  f"· {result.get('channels_discovered', 0)} channels seen", 11),
        ]
        if failures:
            body.append(muted(f"{failures} search angles failed; you can retry this subject.", 11))
        if leads:
            body.append(muted(
                "Research leads seen in several search angles. These have not yet passed "
                "Beacon's faceless and repeatable opportunity checks.", 11))
            for lead in leads:
                channel_id = str(lead.get("channel_id") or "")
                if not channel_id:
                    continue
                body.append(ft.TextButton(
                    f"{lead.get('title') or channel_id} · {lead.get('niche_hits', 0)} matches",
                    icon=ft.Icons.OPEN_IN_NEW,
                    on_click=self._open_url(f"https://www.youtube.com/channel/{channel_id}"),
                ))
        else:
            body.append(muted("No channel leads came back from this sweep. Try a clearer subject.", 11))
        return self.card(*body)

    def _render_find(self) -> None:
        niche_field = ft.TextField(
            label="Niche / topic",
            value=getattr(self, "_find_draft", ""),
            border_color=BORDER,
            dense=True,
            expand=True,
            hint_text="e.g. amish frugal living",
            on_change=lambda e: setattr(self, "_find_draft", e.control.value or ""),
        )
        sweeping = getattr(self, "_deep_sweeping", False)

        def deep_sweep(_):
            q = (niche_field.value or "").strip()
            if not q:
                self.toast("Type a niche to sweep first.")
                return
            if getattr(self, "_deep_sweeping", False):
                return                       # already running — don't queue a 2nd ~100-query sweep
            self._deep_sweeping = True
            self._find_draft = q
            self._find_sweep_result = None
            self._find_sweep_error = None

            def work():
                try:
                    return self.client.call("deep_sweep", niche=q, queries=100)
                except Exception as ex:  # noqa: BLE001
                    self._find_sweep_error = str(ex)[:240]
                    raise
                finally:
                    # reset + re-render on BOTH success and failure so the button never
                    # stays stuck disabled (run_bg's error path doesn't call on_done).
                    self._deep_sweeping = False
                    if self.section == "Find":
                        try:
                            self._render()
                        except Exception:  # noqa: BLE001
                            pass

            def done(r):
                self._find_sweep_result = {**r, "subject": q}
                self._invalidate_all()
                self._render()

            self.run_bg(work, done,
                        f"Deep-sweeping '{q}' (~100 angles)… this runs the scraper, give it a bit.")
            self._render()                   # reflect the disabled state immediately

        sweep_card = self.card(
            ft.Row([
                ft.Container(
                    ft.Icon(ft.Icons.TRAVEL_EXPLORE, size=19, color=ACCENT_DARK),
                    width=38,
                    height=38,
                    border_radius=12,
                    bgcolor=ACCENT_SOFT,
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    h("Launch a deep sweep", 17),
                    muted("Turn one clear subject into a broad discovery run.", 11),
                ], spacing=1, tight=True),
            ], spacing=10),
            ft.Row([
                niche_field,
                ft.FilledButton(
                    "Deep-sweeping…" if sweeping else "Start sweep",
                    icon=ft.Icons.TRAVEL_EXPLORE,
                    on_click=deep_sweep,
                    disabled=sweeping,
                    style=ft.ButtonStyle(
                        bgcolor=ACCENT,
                        color=NAV_BG,
                        shape=ft.RoundedRectangleBorder(radius=11),
                        padding=ft.padding.symmetric(12, 18),
                    ),
                ),
            ], spacing=10),
            ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.STORAGE_OUTLINED, size=15, color=ACCENT2),
                    ft.Text(
                        "New discoveries go into Beacon's one live index; "
                        "no second database is created.",
                        size=11,
                        color=MUTED,
                    ),
                ], spacing=8),
                padding=ft.padding.symmetric(9, 11),
                border_radius=10,
                bgcolor=ACCENT2_SOFT,
            ),
        )
        sweep_card.col = {"sm": 12, "md": 12, "lg": 7}

        def guide_line(icon, title, text):
            return ft.Row([
                ft.Container(
                    ft.Icon(icon, size=15, color=ACCENT2),
                    width=30,
                    height=30,
                    border_radius=10,
                    bgcolor=ACCENT2_SOFT,
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    ft.Text(
                        title,
                        size=12,
                        color=TEXT,
                        weight=ft.FontWeight.W_700,
                    ),
                    ft.Text(text, size=10, color=MUTED),
                ], spacing=1, tight=True, expand=True),
            ], spacing=9, vertical_alignment=ft.CrossAxisAlignment.START)

        guide_card = self.card(
            h("What this run does", 16),
            guide_line(
                ft.Icons.ACCOUNT_TREE_OUTLINED,
                "Expands one subject",
                "Beacon tests about 100 related search angles.",
            ),
            guide_line(
                ft.Icons.FILTER_ALT_OUTLINED,
                "Keeps the opportunity gate",
                "Faceless and repeatable evidence still decide what surfaces.",
            ),
            guide_line(
                ft.Icons.SYNC,
                "Joins the live workflow",
                "Results move through the same categorization and review path.",
            ),
        )
        guide_card.col = {"sm": 12, "md": 12, "lg": 5}
        self.content.controls.append(ft.ResponsiveRow(
            [sweep_card, guide_card],
            columns=12,
            spacing=16,
            run_spacing=16,
        ))
        summary = self._find_sweep_summary()
        if summary is not None:
            self.content.controls.append(summary)

        def load_channels():
            rows = self.client.call(
                "find_channels", replicable_only=True, faceless_only=True,
                sort="opportunity_score", limit=self.find_shown)
            self._attach_avatars(rows)
            return rows

        def start_load(_):
            self._find_requested = True
            self.find_shown = CH_PAGE               # (re)load from the first page
            self._invalidate(self._find_key())      # force a fresh pull
            self._render()                          # _ensure (below) then loads it

        # Loading faceless channels is opt-in (a button), but once requested we go
        # through _ensure so it's cached across tab switches AND a Refresh/Retry
        # (which invalidates the key) actually re-pulls instead of just resetting.
        entry = (self._ensure(self._find_key(), load_channels, "Loading channels…")
                 if self._find_requested else None)
        loading = self._find_key() in self._loading
        have_rows = bool(entry and "data" in entry)
        review_button = ft.OutlinedButton(
            ("Refresh results" if self._find_requested
             else "Browse all opportunities"),
            icon=ft.Icons.LIST,
            on_click=start_load,
            style=ft.ButtonStyle(
                color=ACCENT_DARK,
                side=ft.BorderSide(1, ACCENT),
                shape=ft.RoundedRectangleBorder(radius=11),
                padding=ft.padding.symmetric(11, 16),
            ),
        )
        self.content.controls.append(self.card(
            ft.Row([
                ft.Container(
                    ft.Icon(ft.Icons.FACT_CHECK_OUTLINED, size=18, color=ACCENT2),
                    width=36,
                    height=36,
                    border_radius=11,
                    bgcolor=ACCENT2_SOFT,
                    alignment=ft.alignment.center,
                ),
                ft.Column([
                    h("Browse opportunities across all subjects", 16),
                    muted(
                        "This is Beacon's full faceless, repeatable shortlist. "
                        "It is not limited to the sweep above.",
                        11,
                    ),
                ], spacing=1, tight=True, expand=True),
                review_button,
            ], spacing=11, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ))

        if entry is None and loading:
            self.content.controls.append(self._loading_note("Loading channels…"))
        elif entry and "error" in entry:
            self.content.controls.append(self._error_card(entry["error"]))
        elif have_rows:
            rows = entry["data"] or []
            more = len(rows) >= self.find_shown
            self.content.controls.append(self.section_heading(
                "Replicable opportunities",
                "Formats with enough evidence to investigate further.",
                ft.Icons.VERIFIED_OUTLINED,
            ))
            body = [self.card_grid(rows, "channel", limit=len(rows))]
            if more:
                body.append(ft.Row([ft.OutlinedButton(
                    f"Show more (+{CH_PAGE})", icon=ft.Icons.EXPAND_MORE,
                    on_click=self._find_show_more,
                    style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))],
                    alignment=ft.MainAxisAlignment.CENTER))
            self.content.controls.append(ft.Column(body, spacing=16, tight=True))


def main(page: ft.Page) -> None:
    BeaconApp(page)


if __name__ == "__main__":
    ft.app(target=main)
