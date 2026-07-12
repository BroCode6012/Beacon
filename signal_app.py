"""SIGNAL — the standalone niche investigator & finder (its own app window).

Signal is a CLIENT of the Finder service (the shared niche-finder tool). It does NOT read the
database directly and does NOT import the discovery engine's guts — it drives the finder through
`FinderClient` with the FULL key, exactly like the Lab will with a read-only key. This keeps
Signal small and comfortable to work on alone.

On launch it connects to a running Finder service; if none is up, it self-hosts one in-process
so Signal always works standalone (Phase 4 will make that a proper always-on separate process).

Run (uses the Lab venv, which has the deps):
    D:\\Youtube\\Scripter\\.venv\\Scripts\\python.exe D:\\Youtube\\Signal\\signal_app.py
"""

from __future__ import annotations

import json
import os
import threading
import time

import engine_link  # noqa: F401 — wires shared engine onto sys.path + sets YOUTUBE_INDEX_DB
import flet as ft

import sweep_control  # drives the shared background scraper (must import AFTER engine_link)
import signal_agent   # Signal's OWN Bedrock control agent (separate from the Lab's chat)
import signal_autopilot  # optional background maintenance loop (off by default)
from scripter.intelligence.finder_client import FinderClient, FinderError

# --- palette (self-contained; Signal owns its own look) ---------------------
BG = "#0b0f14"
PANEL = "#141b23"
CARD = "#1a232d"
BORDER = "#26313d"
TEXT = "#e6edf3"
MUTED = "#8b98a5"
ACCENT = "#86efac"      # Signal green (matches the old Signal nav accent)
ACCENT2 = "#9dd1ff"
WARN = "#fca5a5"

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
    return ft.Text(txt, size=size, weight=ft.FontWeight.BOLD, color=TEXT)


# a small rotating palette so letter-avatars aren't all one colour
AVATAR_COLORS = [ACCENT, ACCENT2, "#f0abfc", "#fca5a5", "#fcd34d", "#93c5fd", "#a7f3d0"]


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
    remote_pending = nonnegative_int(transport.get("remote_pending"))
    remote = payload.get("remote")
    remote = remote if isinstance(remote, dict) else {}
    failed = nonnegative_int(last_sync.get("failed_bundles"))
    imported = nonnegative_int(last_sync.get("imported_bundles"))
    duplicates = nonnegative_int(last_sync.get("duplicate_bundles"))
    synced_at = str(last_sync.get("synced_at") or "")
    remote_state = str(remote.get("state") or "unknown").lower()
    remote_message = str(remote.get("message") or "").strip()

    if not configured:
        badge = "setup pending"
        tone = "muted"
        message = (
            "Oracle is not connected yet. Signal is ready, but the secure "
            "signing key and file transport still need setup."
        )
    elif failed:
        badge = "attention"
        tone = "warn"
        message = (
            f"Last sync rejected {failed:,} bundle"
            f"{'s' if failed != 1 else ''}. "
            "They stayed quarantined; nothing unsafe was imported."
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
        "sync_enabled": configured and (
            pending > 0 or transport_configured
        ),
    }


# Quick filters over Signal's mandatory faceless + replicable opportunity gate.
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
CH_PAGE = 120
ALL_TOPICS = "__all_topics__"
OVERVIEW_REFRESH_SECONDS = max(
    5, int(os.getenv("SIGNAL_OVERVIEW_REFRESH_SECONDS", "60")))
CACHE_ERROR_TTL_SECONDS = 15
CACHE_TTL_SECONDS = {
    "overview": OVERVIEW_REFRESH_SECONDS,
    "edge_sync": 10,
    "topics": 300,
    "niches": 300,
    "channels:": 120,
    "realtime:": 60,
    "find_channels:": 300,
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
            raw.get("current_query") or raw.get("message") or state
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
            "Closing the laptop pauses and safely resumes the remaining work."
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
            "Closing the laptop pauses them safely; reopening resumes them."
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


class SignalApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.section = "Overview"
        self.hosting = False
        self.ch_niche: dict | None = None   # niche selected on Channels (filters the grid)
        self.ch_topic: str | None = None    # clean topic niche filter (History/Space/...) — survives presets
        self.ch_view = "index"              # "index" = browse whole index; "niche" = this niche's channels
        self.agent = None                   # Signal's Bedrock agent (lazy); its chat transcript:
        self.agent_log: list[dict] = []
        self._busy = False
        self._busy_lock = threading.Lock()
        self._busy_tasks: dict[int, str] = {}
        self._busy_seq = 0
        # in-memory data cache so switching tabs is instant (load once, reuse).
        # keys: "overview", "niches", f"niche:{id}", "find_channels".
        self.cache: dict[str, dict] = {}
        self._loading: set[str] = set()
        self._epoch: dict[str, int] = {}   # bumped on invalidate → voids in-flight loads
        self._rechecked: set[str] = set()  # browse keys that already got a transient-empty recheck
        self._find_requested = False       # has the user asked to load faceless channels?
        # optimistic scraper state so the Start/Stop buttons flip INSTANTLY on click
        # (the worker takes a few seconds to boot/exit); reconciled to reality each render.
        self._sweep_starting = False
        self._sweep_stopping = False
        self._cloud_syncing = False
        # Real Time feed: discovery-recency window (days) + the once-only auto-refresh guard.
        self.rt_window = 1                 # 1 / 7 / 30 days, or None for "newest all-time"
        self.rt_filters: dict = {}         # metric filters for the Real Time feed (own set)
        self.rt_topic: str | None = None   # topic filter for the Real Time feed
        self.rt_chip: str | None = None    # active preset chip on Real Time
        self._rt_watch_started = False
        self._overview_watch_started = False
        # progressive paging (no hard cap): how many cards are currently loaded per tab.
        # "Show more" grows these; they reset to CH_PAGE when the filter/window changes.
        self.ch_shown = CH_PAGE
        self.rt_shown = CH_PAGE
        self.find_shown = CH_PAGE
        self._ch_last_base = None          # detects a filter/sort/niche change → reset paging
        # NexLev-style Channels browser: the active filter set + which preset chip.
        # (keys with a leading _ are client-side only, stripped before the finder call)
        self.ch_filters: dict = {**CH_CHIPS[0][1], "limit": 60}
        self.ch_chip = CH_CHIPS[0][0]

        page.title = "Signal — Niche Finder"
        page.bgcolor = BG
        page.padding = 0
        page.theme_mode = ft.ThemeMode.DARK
        try:
            page.window.width, page.window.height = 1180, 820
        except Exception:  # noqa: BLE001 — older/newer flet
            pass

        self.client = self._resolve_service()

        # Autopilot: optional background maintenance loop (OFF by default; starts only
        # if the user previously enabled it). Runs safe/reversible tasks + logs them.
        self.autopilot = signal_autopilot.Autopilot(self)
        if self.autopilot.enabled:
            self.autopilot.start()

        # layout: left nav | content | status bar
        self.content = ft.Column([], scroll=ft.ScrollMode.AUTO, spacing=14, expand=True)
        self.status = ft.Text("", size=12, color=MUTED)
        self.spinner = ft.ProgressRing(width=15, height=15, stroke_width=2,
                                       color=ACCENT, visible=False)
        nav = self._nav()
        body = ft.Container(self.content, expand=True, padding=24, bgcolor=BG)
        status_bar = ft.Container(
            ft.Row([self.spinner, self.status], spacing=8),
            padding=ft.padding.symmetric(6, 16), bgcolor=PANEL,
            border=ft.border.only(top=ft.border.BorderSide(1, BORDER)))
        page.add(ft.Column([
            ft.Row([nav, body], expand=True, spacing=0,
                   vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            status_bar,
        ], expand=True, spacing=0))

        self._render()

    # -- service wiring -----------------------------------------------------
    def _resolve_service(self) -> FinderClient:
        """Connect to a running Finder service (from the shared token file, else defaults).
        If nothing is up, self-host one in-process so Signal always works."""
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
        # self-host in-process (fallback)
        from scripter.intelligence.local_api import serve_in_thread
        serve_in_thread(host=HOST, port=PORT, db_path=DB,
                        token=FULL_TOKEN, read_token=READ_TOKEN)
        self.hosting = True
        return FinderClient(f"http://{HOST}:{PORT}", FULL_TOKEN)

    # -- shell helpers ------------------------------------------------------
    def _nav_tiles(self) -> list[ft.Container]:
        """Build the nav tiles for the CURRENT section (rebuilt on every render
        so the green highlight follows the page you're on)."""
        items = [("Overview", ft.Icons.INSIGHTS),
                 ("Channels", ft.Icons.GRID_VIEW), ("Real Time", ft.Icons.BOLT),
                 ("Find", ft.Icons.TRAVEL_EXPLORE), ("Agent", ft.Icons.AUTO_AWESOME)]

        def tile(name, icon):
            active = name == self.section
            return ft.Container(
                ft.Row([ft.Icon(icon, size=18, color=ACCENT if active else MUTED),
                        ft.Text(name, color=TEXT if active else MUTED,
                                weight=ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL)],
                       spacing=10),
                padding=ft.padding.symmetric(11, 16), border_radius=8,
                bgcolor=CARD if active else None, ink=True,
                on_click=lambda _, n=name: self._go(n))

        return [tile(n, i) for n, i in items]

    def _nav(self) -> ft.Container:
        self._nav_col = ft.Column(self._nav_tiles(), spacing=4)
        return ft.Container(
            ft.Column([
                ft.Row([ft.Icon(ft.Icons.SENSORS, color=ACCENT, size=22),
                        h("Signal", 20)], spacing=8),
                muted("Faceless opportunity finder", 11),
                ft.Divider(color=BORDER),
                self._nav_col,
            ], spacing=10),
            width=210, padding=16, bgcolor=PANEL,
            border=ft.border.only(right=ft.border.BorderSide(1, BORDER)))

    def _go(self, name: str) -> None:
        self.section = name
        self._render()

    def toast(self, msg: str) -> None:
        self.status.value = msg
        try:
            self.status.update()
        except Exception:  # noqa: BLE001
            pass

    def _apply_busy_state(self, busy: bool, msg: str) -> None:
        self._busy = busy
        self.spinner.visible = busy
        self.status.value = msg
        try:
            self.page.update()
        except Exception:  # noqa: BLE001
            pass

    def _begin_busy(self, msg: str) -> int:
        """Register one background operation and return its unique token."""
        with self._busy_lock:
            self._busy_seq += 1
            token = self._busy_seq
            self._busy_tasks[token] = msg
            current = self._busy_tasks[max(self._busy_tasks)]
        self._apply_busy_state(True, current)
        return token

    def _end_busy(self, token: int) -> None:
        """Finish one operation without hiding other concurrent work."""
        with self._busy_lock:
            self._busy_tasks.pop(token, None)
            busy = bool(self._busy_tasks)
            current = self._busy_tasks[max(self._busy_tasks)] if busy else ""
        self._apply_busy_state(busy, current)

    def run_bg(self, fn, on_done, start_msg: str) -> None:
        """Run a finder call off the UI thread; re-render on completion."""
        token = self._begin_busy(start_msg)

        def worker():
            try:
                result = fn()
                err = None
            except Exception as ex:  # noqa: BLE001
                result, err = None, ex
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
            self._end_busy(token)
            raise

    # -- agent (Signal's own Bedrock assistant, drives finder + scraper) ----
    def _get_agent(self):
        if self.agent is None:
            self.agent = signal_agent.SignalAgent(self.client, sweep_control, ui=self)
        return self.agent

    # -- app-view control the agent can drive (edit the Channels view, switch tabs) --
    def agent_open_view(self, view=None) -> dict:
        valid = {"Overview", "Channels", "Real Time", "Find", "Agent"}
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
        else:
            return
        if self.section == "Agent":
            try:
                self._render()
            except Exception:  # noqa: BLE001
                pass

    def _agent_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text or getattr(self, "_busy", False):
            return
        self.agent_log.append({"kind": "user", "text": text})
        self._render()
        agent = self._get_agent()
        self.run_bg(
            lambda: agent.run(text, on_event=self._agent_event),
            on_done=lambda _r: (self.toast(""),
                                self._render() if self.section == "Agent" else None),
            start_msg="Agent is thinking…")

    def _agent_bubble(self, m: dict) -> ft.Control:
        kind = m.get("kind")
        if kind == "user":
            return ft.Row([ft.Container(
                ft.Text(m.get("text", ""), color=TEXT, selectable=True),
                bgcolor=CARD, border=ft.border.all(1, ACCENT2),
                padding=ft.padding.symmetric(8, 12), border_radius=12)],
                alignment=ft.MainAxisAlignment.END)
        if kind == "tool":
            inp = m.get("input") or {}
            arg = ", ".join(f"{k}={v}" for k, v in list(inp.items())[:3])
            return ft.Row([ft.Icon(ft.Icons.BUILD, size=13, color=MUTED),
                           muted(f"{m.get('name','')}({arg})", 11)], spacing=6)
        return ft.Row([ft.Container(
            ft.Text(m.get("text", ""), color=TEXT, selectable=True),
            bgcolor=PANEL, border=ft.border.all(1, BORDER),
            padding=ft.padding.symmetric(9, 13), border_radius=12, expand=True)],
            alignment=ft.MainAxisAlignment.START)

    def _render_agent(self) -> None:
        self.content.controls.append(muted(
            "Ask me to search, organize, or control the index — I drive the finder + "
            "scraper for you. I can read stats, find channels, add to niches, and "
            "start/stop the scraper.", 12))
        agent = self._get_agent()
        ok, why = agent.ready()
        if not ok:
            self.content.controls.append(self._error_card(
                f"Agent needs setup: {why}. Add your AWS keys + SIGNAL_BEDROCK_MODEL to "
                "Signal's .env (D:\\Youtube\\Signal\\.env), then reopen Signal."))
        # Autopilot control (background maintenance loop)
        self.content.controls.append(self._autopilot_card())
        # input row FIRST so it's always visible (transcript can grow tall below it)
        self._agent_input = ft.TextField(
            hint_text="Message the agent…", expand=True, border_color=BORDER,
            on_submit=lambda e: self._agent_send(e.control.value))
        send = ft.IconButton(ft.Icons.SEND, icon_color=ACCENT, tooltip="Send",
                             on_click=lambda _: self._agent_send(self._agent_input.value))
        clear = ft.TextButton("Clear", icon=ft.Icons.CLEAR_ALL,
                              on_click=lambda _: (self.agent_log.clear(),
                                                  self._get_agent().reset(), self._render()))
        self.content.controls.append(ft.Row(
            [self._agent_input, send, clear],
            vertical_alignment=ft.CrossAxisAlignment.CENTER))
        bubbles = [self._agent_bubble(m) for m in self.agent_log] or [muted(
            "Try:  “how many faceless History channels under 5k subs?”  ·  "
            "“list the topics”  ·  “what's the scraper doing?”  ·  “start the scraper”.", 12)]
        self.content.controls.append(self.card(
            ft.ListView(bubbles, spacing=10, auto_scroll=True, height=360, padding=6)))

    def _autopilot_card(self) -> ft.Control:
        ap = self.autopilot
        on = ap.enabled
        badge = self.pill("● on" if on else "○ off", ACCENT if on else MUTED)
        seg = ft.SegmentedButton(
            allow_multiple_selection=False, allow_empty_selection=False,
            selected={"on" if on else "off"},
            segments=[ft.Segment(value="off", label=ft.Text("Off")),
                      ft.Segment(value="on", label=ft.Text("On"))],
            on_change=lambda e: self._autopilot_toggle("on" in e.control.selected))
        self._ap_interval = ft.TextField(
            label="Every (min)", value=str(ap.interval), width=110, dense=True,
            border_color=BORDER, text_size=13, keyboard_type=ft.KeyboardType.NUMBER)
        run_btn = ft.OutlinedButton("Run now", icon=ft.Icons.PLAY_ARROW,
                                    disabled=ap.running_now,
                                    on_click=lambda _: self._autopilot_run_now())
        if ap.running_now:
            status = ft.Row([ft.ProgressRing(width=13, height=13, stroke_width=2, color=ACCENT),
                             muted("running a maintenance pass…", 11)], spacing=8)
        elif on:
            secs = ap.seconds_to_next()
            status = muted(f"next run in ~{secs // 60}m {secs % 60}s" if secs is not None
                           else "on", 11)
        else:
            status = muted("off — turn on to run safe maintenance on a timer", 11)
        items = []
        for e in ap.log[:3]:
            tools = ", ".join(t for t in e.get("tools", []) if t)[:80]
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
            items.append(ft.Container(
                ft.Column([
                    ft.Row([ft.Icon(ft.Icons.SCHEDULE, size=12, color=MUTED),
                            muted(e.get("at", ""), 11),
                            muted(("· " + tools) if tools else "", 10)], spacing=6, wrap=True),
                    *[muted(line, 10) for line in query_lines],
                    ft.Text(e.get("summary", ""), size=12, color=TEXT, selectable=True,
                            max_lines=6, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=3, tight=True),
                padding=8, border_radius=8, bgcolor=PANEL, border=ft.border.all(1, BORDER)))
        log_col = (ft.Column(items, spacing=8, tight=True) if items
                   else muted("No runs yet — turn On or hit Run now.", 11))
        return self.card(
            ft.Row([ft.Icon(ft.Icons.AUTO_MODE, color=ACCENT, size=18),
                    h("Autopilot", 15), badge, seg, self._ap_interval, run_btn],
                   spacing=10, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            muted("Runs SAFE maintenance on a timer — categorize new channels, hide junk, "
                  "sparingly top up the scraper — and logs each pass. Never deletes; runs "
                  "while Signal is open.", 11),
            status,
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
    def card(self, *controls) -> ft.Container:
        return ft.Container(
            ft.Column(list(controls), spacing=10, tight=True),
            padding=18, bgcolor=CARD, border_radius=12, border=ft.border.all(1, BORDER))

    def stat_grid(self, stats: dict, keys: list[str]) -> ft.Row:
        cells = []
        for k in keys:
            if k not in stats:
                continue
            v = stats[k]
            v = f"{v:,}" if isinstance(v, int) else str(v)
            cells.append(ft.Column(
                [ft.Text(v, size=20, weight=ft.FontWeight.BOLD, color=ACCENT),
                 muted(k.replace("_", " "), 11)],
                spacing=1, horizontal_alignment=ft.CrossAxisAlignment.CENTER))
        return ft.Row(cells, spacing=26, wrap=True)

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
    CARD_W = 250          # width of one card; the grid wraps to fit the window
    THUMB_H = 141         # 250 * 9/16 -> proper 16:9 video thumbnail

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
            bgcolor=ft.Colors.with_opacity(0.14, color))

    def _shell(self, *children, on_click=None) -> ft.Container:
        """The common card frame: fixed width, rounded, clipped, bordered.
        Pass on_click to make the whole card a clickable link (with a hover ripple)."""
        return ft.Container(
            ft.Column(list(children), spacing=0, tight=True),
            width=self.CARD_W, bgcolor=CARD, border_radius=12,
            border=ft.border.all(1, BORDER),
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
            width=self.CARD_W, height=self.THUMB_H, bgcolor=PANEL,
            alignment=ft.alignment.center)

    def _avatar(self, name: str, size: int = 30, avatar_url=None) -> ft.Container:
        """A circular channel avatar. Uses the real profile image when we have
        one, and falls back to a coloured letter circle (also if the image fails)."""
        letter = (name or "?").strip()[:1].upper() or "?"
        color = AVATAR_COLORS[ord(letter) % len(AVATAR_COLORS)]
        letter_circle = ft.Container(
            ft.Text(letter, size=max(12, int(size * 0.46)),
                    weight=ft.FontWeight.BOLD, color=BG),
            width=size, height=size, border_radius=size / 2, bgcolor=color,
            alignment=ft.alignment.center)
        if avatar_url:
            return ft.Container(
                ft.Image(src=avatar_url, width=size, height=size,
                         fit=ft.ImageFit.COVER, error_content=letter_circle),
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
        """A small KPI tile: muted icon+label on top, the big value below (in ink).
        The value wears the primary text token; only the little icon carries colour."""
        return ft.Container(
            ft.Column([
                ft.Row([ft.Icon(icon, size=13, color=tint), muted(label, 10)],
                       spacing=5, tight=True,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(value or "—", size=16, weight=ft.FontWeight.BOLD, color=TEXT),
            ], spacing=3, tight=True),
            bgcolor=PANEL, border_radius=8,
            padding=ft.padding.symmetric(8, 10), expand=True)

    def _outlier_meter(self, score) -> ft.Column:
        """A segmented bar meter for the opportunity score. The fill length encodes
        magnitude; the fill colour encodes the quality band; the number is exact."""
        color, frac = self._outlier_level(score)
        SEG = 8
        filled = round(frac * SEG)
        bars = [ft.Container(height=7, expand=True, border_radius=2,
                             bgcolor=color if i < filled else BORDER)
                for i in range(SEG)]
        num = ft.Text(f"{float(score):.1f}×" if score else "—",
                      size=13, weight=ft.FontWeight.BOLD, color=TEXT)
        return ft.Column([
            ft.Row([muted("Opportunity score", 10),
                    ft.Container(num, expand=True,
                                 alignment=ft.alignment.center_right)],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row(bars, spacing=2),
        ], spacing=6, tight=True)

    def channel_card(self, r: dict, niche_id=None, tracked=None) -> ft.Container:
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
            ft.Text(name, size=14, weight=ft.FontWeight.BOLD, color=TEXT,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(subtitle, size=11, color=(ACCENT2 if topic else MUTED),
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        ], spacing=2, tight=True, expand=True)
        header_row = [self._avatar(name, 44, r.get("avatar_url")), title_col]
        if faceless:
            header_row.append(self.pill("faceless", "#f0abfc", ft.Icons.MASKS))
        header = ft.Row(header_row, spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.START)

        tiles = ft.Column([
            ft.Row([self._stat_tile("Avg views", avg, ft.Icons.VISIBILITY, ACCENT),
                    self._stat_tile("Subscribers", subs, ft.Icons.PEOPLE, ACCENT2)],
                   spacing=8),
            ft.Row([self._stat_tile("Uploads", vids, ft.Icons.VIDEO_LIBRARY, MUTED),
                    self._stat_tile("Age", self._fmt_age(days),
                                    ft.Icons.CALENDAR_MONTH, MUTED)], spacing=8),
        ], spacing=8, tight=True)

        # freshness badge — only present on Real Time rows (they carry added_at)
        col = []
        ago = self._ago(r.get("added_at"))
        if ago:
            col.append(self.pill(f"added {ago}", ACCENT, ft.Icons.SCHEDULE))
        col += [header, tiles, self._outlier_meter(score)]

        body = ft.Container(ft.Column(col, spacing=12, tight=True), padding=14)
        return self._shell(body, on_click=self._open_url(channel_url))

    def card_grid(self, rows: list[dict], kind: str = "video",
                  *, limit: int = 24, niche_id=None, tracked=None,
                  tracked_ids=None) -> ft.Control:
        if not rows:
            return muted("Nothing here yet.")
        if kind == "video":
            cards = [self.video_card(r) for r in rows[:limit]]
        else:
            def _tracked(r):
                if tracked_ids is not None:      # per-row: is it in the niche?
                    return r.get("channel_id") in tracked_ids
                return tracked
            cards = [self.channel_card(r, niche_id=niche_id, tracked=_tracked(r))
                     for r in rows[:limit]]
        return ft.Row(cards, wrap=True, spacing=14, run_spacing=14)

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
                    self._render()
                    return
                self.cache[key] = self._cache_entry(result)
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

            self.run_bg(safe_load, done, msg)
        return entry

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
            self._invalidate("overview", "edge_sync")
        elif self.section == "Channels":
            self._invalidate("niches", "topics")
            self._invalidate(self._channels_key())
        elif self.section == "Real Time":
            self._invalidate("topics")
            self._invalidate(self._rt_key())
        elif self.section == "Find":
            self._invalidate(self._find_key())
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
    def _render(self) -> None:
        # refresh nav highlight on the MOUNTED nav column (not a throwaway copy)
        try:
            self._nav_col.controls = self._nav_tiles()
        except Exception:  # noqa: BLE001
            pass
        self.content.controls = []
        conn = ("self-hosted (in-process)" if self.hosting
                else f"connected → {self.client.base_url}")
        self.content.controls.append(ft.Row([
            h(self.section, 22),
            ft.Container(muted(f"finder: {conn}", 11), expand=True,
                         alignment=ft.alignment.center_right),
            ft.IconButton(ft.Icons.REFRESH, icon_color=MUTED, icon_size=18,
                          tooltip="Refresh this tab's data", on_click=self._refresh),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER))
        if self.section == "Overview":
            self._render_overview()
        elif self.section == "Channels":
            self._render_channels()
        elif self.section == "Real Time":
            self._render_realtime()
        elif self.section == "Find":
            self._render_find()
        elif self.section == "Agent":
            self._render_agent()
        try:
            self.page.update()
        except Exception:  # noqa: BLE001
            pass

    def _render_overview(self) -> None:
        self._watch_overview()
        self.content.controls.append(muted(
            f"Live counts from the shared index · auto-refreshes every "
            f"{OVERVIEW_REFRESH_SECONDS}s · {self._cache_updated_text('overview')}."))

        self.content.controls.append(self._research_progress_card())

        entry = self._ensure("overview", lambda: self.client.db_stats(),
                             "Loading index stats…")
        if entry is None:
            self.content.controls.append(self._loading_note("Loading index stats…"))
        elif "error" in entry:
            self.content.controls.append(self._error_card(entry["error"]))
        else:
            self.content.controls.append(self.card(
                h("Index", 16),
                self.stat_grid(entry["data"],
                               ["channels", "videos", "niches", "niche_channels",
                                "faceless_verified", "outlier_snapshots", "crawl_runs"]),
            ))
        # live background-scraper card (status read fresh each render, not cached)
        self.content.controls.append(self._scraper_card())
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
                padding=10, border_radius=8, bgcolor=PANEL,
                border=ft.border.all(1, BORDER),
            ),
        )

    def _overview_tick(self) -> None:
        """Refresh Overview counts silently without blanking the existing stat card."""
        key = "overview"
        if key in self._loading:
            return
        self._loading.add(key)
        gen = self._epoch.get(key, 0)

        def work():
            try:
                result = {"data": self.client.db_stats()}
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

        def worker():
            while True:
                time.sleep(OVERVIEW_REFRESH_SECONDS)
                if self.section == "Overview":
                    self._overview_tick()

        threading.Thread(target=worker, daemon=True).start()

    def _cloud_sync_card(self) -> ft.Control:
        entry = self._ensure(
            "edge_sync",
            lambda: self.client.call("edge_sync_status"),
            "Checking cloud workerâ€¦",
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
                self._loading_note("Checking the secure cloud inboxâ€¦"),
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
        sync_text = "Syncingâ€¦" if self._cloud_syncing else "Sync now"
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
                            else ft.ButtonStyle(bgcolor=ACCENT, color=BG)
                        ),
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Text(view["message"], size=12, color=color),
        ]
        if view["remote_message"]:
            details.append(muted(f"Oracle: {view['remote_message']}", 11))
        if view["last_line"]:
            details.append(muted(view["last_line"], 10))
        details.append(
            muted(
                "Oracle only collects staged results. It never opens or owns "
                "Signal's main database.",
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

        self.run_bg(work, done, "Importing cloud discoveriesâ€¦")

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
            style=None if start_off else ft.ButtonStyle(bgcolor=ACCENT, color=BG))
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
                                       style=ft.ButtonStyle(bgcolor=ACCENT, color=BG))],
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
                border_radius=8,
                bgcolor=PANEL,
                border=ft.border.all(1, BORDER),
            )

        return self.card(
            ft.Row([h("Background scraper", 16), badge,
                    ft.Container(expand=True), start_btn, stop_btn],
                   spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            muted(line),
            pipeline_note,
            muted("Reuses the engine's radar worker — keeps running even if you "
                  "close Signal. Your manual Deep sweep still works alongside it.", 11),
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
        n = self.ch_niche
        return (n.get("id") or n.get("niche_id")) if n else None

    def _ch_in_niche_view(self) -> bool:
        return bool(self.ch_niche) and self.ch_view == "niche"

    def _ch_query_args(self) -> dict:
        """find_channels args for the current filters (niche injected in niche view)."""
        args = {k: v for k, v in self.ch_filters.items() if not k.startswith("_")}
        # Signal is an opportunity finder, not a general channel browser. This
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
        # A preset replaces metric/format filters; niche + topic live in separate
        # state and therefore survive.
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
                    "category_contains", "video_format")

    def _active_filter_count(self, filt: dict | None = None) -> int:
        filt = self.ch_filters if filt is None else filt
        return sum(1 for k in self._FILTER_KEYS
                   if filt.get(k) not in (None, "", False))

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
        # Signal already enforces faceless + replicable globally; the only type
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
            modal=True, bgcolor=PANEL,
            title=ft.Row([ft.Icon(ft.Icons.TUNE, color=ACCENT, size=20),
                          ft.Text("Filters", color=TEXT)], spacing=8),
            content=content,
            actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            actions=[
                ft.OutlinedButton("Reset", icon=ft.Icons.RESTART_ALT,
                                  on_click=lambda e: self._reset_filters(dlg, target)),
                ft.FilledButton("Apply", icon=ft.Icons.CHECK,
                                on_click=lambda e: self._apply_filters(fields, state, dlg, target),
                                style=ft.ButtonStyle(bgcolor=ACCENT, color=BG)),
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
            self.rt_shown = CH_PAGE          # new filter → back to first page
        try:
            self.page.close(dlg)
        except Exception:  # noqa: BLE001
            pass
        self._render()

    def _reset_filters(self, dlg, target: str = "channels") -> None:
        """Clear every metric/type filter, then reopen fresh."""
        if target == "channels":
            sort = self.ch_filters.get("sort", "opportunity_score")
            self.ch_filters = {"sort": sort, "limit": 60}
            self.ch_chip = "Custom"
        else:
            self.rt_filters = {}
            self.rt_shown = CH_PAGE
        try:
            self.page.close(dlg)
        except Exception:  # noqa: BLE001
            pass
        self._render()
        self._open_filters(target=target)

    def _ch_filter_label(self, k, v):
        return {
            "faceless_only": "faceless only" if v else None,
            "max_subs": f"subs ≤ {human_num(v)}",
            "min_subs": f"subs ≥ {human_num(v)}",
            "min_avg_views": f"avg views ≥ {human_num(v)}",
            "max_avg_views": f"avg views ≤ {human_num(v)}",
            "max_days_since_start": f"age ≤ {int(v)}d",
            "min_days_since_start": f"age ≥ {int(v)}d",
            "min_uploads": f"uploads ≥ {int(v)}",
            "max_uploads": f"uploads ≤ {int(v)}",
            "min_views_per_sub": f"views/sub ≥ {v}",
            "category_contains": f"category ~ {v}",
            "_min_outlier": f"outlier ≥ {v}x",
            "video_format": f"format: {v}",
        }.get(k)

    def _render_channels(self) -> None:
        # reset paging back to the first page whenever the filter/sort/niche changed
        # (so a new filter starts small & snappy; "Show more" then pages deeper).
        base = self._ch_base_key()
        if base != self._ch_last_base:
            self._ch_last_base = base
            self.ch_shown = CH_PAGE

        self.content.controls.append(muted(
            "Browse faceless formats with repeatable performance evidence. Music, brands, "
            "one-off viral accidents, and unstable tiny-data ratios are filtered out."))

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
            label="Sort by", width=180, border_color=BORDER, value=cur_sort,
            options=[ft.dropdown.Option(key=k, text=lbl) for lbl, k in CH_SORTS],
            on_change=lambda e: self._ch_set_sort(e.control.value))
        nfilt = self._active_filter_count()
        filters_btn = ft.OutlinedButton(
            f"Filters ({nfilt})" if nfilt else "Filters",
            icon=ft.Icons.TUNE, on_click=self._open_filters,
            style=ft.ButtonStyle(color=TEXT if nfilt else MUTED,
                                 side=ft.BorderSide(1, ACCENT if nfilt else BORDER)))

        toolbar = [ft.Row([niche_dd, topic_dd, sort_dd, filters_btn], wrap=True, spacing=10,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER)]
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
                style=ft.ButtonStyle(bgcolor=CARD if active else None,
                                     side=ft.BorderSide(1, ACCENT if active else BORDER))))
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
                ft.Row([ft.Text(lbl, size=11, color=TEXT),
                        ft.Icon(ft.Icons.CLOSE, size=13, color=MUTED)], spacing=4, tight=True),
                padding=ft.padding.symmetric(3, 9), border_radius=20, bgcolor=CARD,
                border=ft.border.all(1, BORDER), ink=True,
                on_click=lambda _, kk=k: self._ch_remove_filter(kk)))
        if extras:
            toolbar.append(ft.Row([muted("Active:", 11)] + extras, wrap=True, spacing=6,
                                  vertical_alignment=ft.CrossAxisAlignment.CENTER))

        self.content.controls.append(self.card(h("Channels", 16), *toolbar))

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
        count = ft.Text(f"{len(rows):,}{'+' if more else ''} channels", size=13,
                        weight=ft.FontWeight.BOLD, color=TEXT)
        tail = "showing the top — Show more below" if more else "all matches loaded"
        sub = muted(f"{scope} · sorted by {cur_sort.replace('_', ' ')} · {tail}", 11)
        head = ft.Row([count, sub], wrap=True, spacing=8,
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)
        body = [head, self.card_grid(rows, "channel", limit=len(rows))]
        if more:
            body.append(ft.Row([ft.OutlinedButton(
                f"Show more (+{CH_PAGE})", icon=ft.Icons.EXPAND_MORE,
                on_click=self._ch_show_more,
                style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))],
                alignment=ft.MainAxisAlignment.CENTER))
        self.content.controls.append(self.card(*body))

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
        self.rt_chip = "All"
        self.rt_window = 30
        self.rt_shown = CH_PAGE
        self._render()

    def _rt_pick_chip(self, name: str, preset: dict) -> None:
        # a preset REPLACES the metric filters; RT always sorts by discovery time, so
        # drop the preset's 'sort' key (it doesn't apply to the newest-first feed).
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

        def worker():
            while True:
                time.sleep(60)
                if self.section == "Real Time":
                    self._rt_tick()
        threading.Thread(target=worker, daemon=True).start()

    def _render_realtime(self) -> None:
        self._watch_realtime()      # kick off the 60s auto-refresh once
        self.content.controls.append(muted(
            "New faceless, replicable opportunities as the scraper discovers them. This "
            "is discovery time (when WE found the channel), not YouTube channel age. "
            "The feed auto-refreshes every 60 seconds."))

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
            ft.Row([h("Real Time", 16), self.pill("● live", ACCENT)], spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([muted("Window:", 12), win_seg], spacing=10, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([topic_dd, filters_btn], wrap=True, spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        # preset chips (same set as Channels; a preset replaces the metric filters)
        chips = []
        for name, preset in CH_CHIPS:
            active = self.rt_chip == name
            chips.append(ft.OutlinedButton(
                name, on_click=lambda _, n=name, p=preset: self._rt_pick_chip(n, p),
                style=ft.ButtonStyle(bgcolor=CARD if active else None,
                                     side=ft.BorderSide(1, ACCENT if active else BORDER))))
        header.append(ft.Row(chips, wrap=True, spacing=8))
        # active-filter chips (click a chip to remove that filter) — same combining as Channels
        extras = []
        for k, v in list(self.rt_filters.items()):
            lbl = self._ch_filter_label(k, v)
            if not lbl:
                continue
            extras.append(ft.Container(
                ft.Row([ft.Text(lbl, size=11, color=TEXT),
                        ft.Icon(ft.Icons.CLOSE, size=13, color=MUTED)], spacing=4, tight=True),
                padding=ft.padding.symmetric(3, 9), border_radius=20, bgcolor=CARD,
                border=ft.border.all(1, BORDER), ink=True,
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
        count = ft.Text(f"Newest {len(rows):,}{'+' if more else ''} channels", size=13,
                        weight=ft.FontWeight.BOLD, color=TEXT)
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
        body = [head, self.card_grid(rows, "channel", limit=len(rows))]
        if more:
            body.append(ft.Row([ft.OutlinedButton(
                f"Show more (+{CH_PAGE})", icon=ft.Icons.EXPAND_MORE,
                on_click=self._rt_show_more,
                style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))],
                alignment=ft.MainAxisAlignment.CENTER))
        self.content.controls.append(self.card(*body))

    def _find_key(self) -> str:
        return f"find_channels:{self.find_shown}"   # limit in the key → Show more re-loads

    def _find_show_more(self, _=None) -> None:
        self.find_shown += CH_PAGE
        self._render()

    def _render_find(self) -> None:
        niche_field = ft.TextField(label="Niche / topic to sweep", border_color=BORDER,
                                   dense=True, expand=True,
                                   hint_text="e.g. amish frugal pest control")

        def deep_sweep(_):
            q = (niche_field.value or "").strip()
            if not q:
                self.toast("Type a niche to sweep first.")
                return
            self.run_bg(lambda: self.client.call("deep_sweep", niche=q, queries=100),
                        lambda r: (self.toast(f"Deep sweep done: {json.dumps(r, default=str)[:180]}"),
                                   self._invalidate_all(), self._render()),
                        f"Deep-sweeping '{q}' (~100 angles)… this runs the scraper, give it a bit.")

        self.content.controls.append(self.card(
            h("Deep sweep a niche", 16),
            muted("Carpet-bomb ~100 search angles and flood faceless finds into the index "
                  "(full-access finder command — writes)."),
            ft.Row([niche_field,
                    ft.FilledButton("Deep sweep", icon=ft.Icons.TRAVEL_EXPLORE,
                                    on_click=deep_sweep,
                                    style=ft.ButtonStyle(bgcolor=ACCENT, color="#0b0f14"))],
                   spacing=10),
        ))

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
        self.content.controls.append(ft.OutlinedButton(
            ("Reload replicable opportunities" if self._find_requested
             else "Load replicable opportunities"),
            icon=ft.Icons.LIST, on_click=start_load))

        if entry is None and loading:
            self.content.controls.append(self._loading_note("Loading channels…"))
        elif entry and "error" in entry:
            self.content.controls.append(self._error_card(entry["error"]))
        elif have_rows:
            rows = entry["data"] or []
            more = len(rows) >= self.find_shown
            body = [h("Replicable faceless opportunities", 16),
                    muted("Formats with repeatable performance evidence. Click a card to open YouTube."),
                    self.card_grid(rows, "channel", limit=len(rows))]
            if more:
                body.append(ft.Row([ft.OutlinedButton(
                    f"Show more (+{CH_PAGE})", icon=ft.Icons.EXPAND_MORE,
                    on_click=self._find_show_more,
                    style=ft.ButtonStyle(side=ft.BorderSide(1, ACCENT), color=TEXT))],
                    alignment=ft.MainAxisAlignment.CENTER))
            self.content.controls.append(self.card(*body))


def main(page: ft.Page) -> None:
    SignalApp(page)


if __name__ == "__main__":
    ft.app(target=main)
