"""Beacon Autopilot — OPTIONAL background maintenance loop (OFF by default).

When enabled, every N minutes it runs a RESTRICTED agent that does only SAFE,
reversible maintenance — categorize newly-discovered channels, hide new junk/music,
and (sparingly) top up the scraper's queries if it's underfed — then appends a
timestamped entry to an activity log you review in the Agent tab.

It CANNOT do destructive things: the agent is locked to a safe toolset (no bulk
exclude / delete / topic-override / niche-create / scraper start-stop). Runs inside
the Beacon app while it's open (the durable background scraper handles discovery on
its own). Separate from the Lab; uses Beacon's own BeaconAgent on the "autopilot" AI slot,
which follows the Agent tab's provider unless ai_providers.json pins another.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

import beacon_ai

STATE_FILE = Path(__file__).with_name("_autopilot.json")
DEFAULT_INTERVAL = 30

# Only reversible / additive maintenance + read/report tools. NO exclude_channels,
# exclude_by_filter, set_channel_topic, create_niche, start/stop_scraper, etc.
SAFE_TOOLS = {
    "db_stats", "list_topics", "list_niches", "find_channels", "count_channels",
    "recent_channels", "find_rising_videos", "get_channel_details", "radar_status",
    "get_watchlist", "scraper_status",
    "reclassify_topics", "run_junk_filter", "add_watchlist_queries",
}

# Measured on the live 24 GB index (2026-09-17), one pass:
#   run_junk_filter    452s  -> excluded 0
#   db_stats           151s
#   recent_channels     40s
#   find_rising_videos  36s
#   reclassify_topics    1s  -> classified 0
# The two expensive steps found NOTHING because the background scraper runs the same junk
# filter and topic classifier every cycle (junk_filter_enabled / topic_classify_enabled).
# So a LIGHT pass is the default — it reads what is new and reports — and the whole-index
# upkeep only runs when it is actually needed: when the scraper is OFF, or its own junk
# filter / topic classifier is switched off (nobody else is doing it), plus a weekly safety
# net. The safety net was 12h until 2026-09-19, when a live sweep's whole-index reads held
# the cloud import at 0 of 4 bundles for 11 minutes — a real cost for work that changes
# nothing while the scraper runs.
DEEP_EVERY_HOURS = max(1, int(os.getenv("BEACON_AUTOPILOT_DEEP_HOURS", "168")))

# A light pass may not even ASK for the whole-index tools: an instruction is a request, an
# allowlist is a guarantee.
HEAVY_TOOLS = {"run_junk_filter", "reclassify_topics", "db_stats"}
LIGHT_TOOLS = SAFE_TOOLS - HEAVY_TOOLS

LIGHT_GOAL = (
    "Do a LIGHT check of what is new, then give a SHORT report. The background scraper "
    "already labels topics and hides junk on every cycle, so do NOT redo that here.\n"
    "1. scraper_status — note if the background scraper is OFF.\n"
    "2. recent_channels — what has been discovered recently.\n"
    "3. find_rising_videos — anything gaining views fast.\n"
    "4. Call get_watchlist. Only when can_add_autonomously=true AND measured yield shows "
    "a useful gap, add AT MOST 2 focused base queries. Never add year/faceless/modifier "
    "variants; the radar expands each base itself. If additions are blocked, do not retry.\n"
    "End with a one-paragraph brief: what is new and what is worth a look. Be concise; use "
    "real numbers/names."
)

MAINTENANCE_GOAL = (
    "Do a routine maintenance pass on the index, then give a SHORT report. Only these "
    "safe steps:\n"
    "1. reclassify_topics(only_missing=true) — label any newly-discovered channels.\n"
    "2. run_junk_filter — hide any new music/meme/big-brand channels.\n"
    "3. scraper_status — note if the background scraper is OFF.\n"
    "4. Skim db_stats + recent_channels + find_rising_videos — note anything NEW or "
    "interesting (new outliers, a topic growing fast).\n"
    "5. Call get_watchlist. Only when can_add_autonomously=true AND measured yield shows "
    "a useful gap, add AT MOST 2 focused base queries. Never add year/faceless/modifier "
    "variants; the radar expands each base itself. If additions are blocked, do not retry.\n"
    "Do NOT exclude channels by filter, delete, override topics, create niches, or "
    "start/stop the scraper. End with a one-paragraph brief: what you did + what's worth "
    "a look. Be concise; use real numbers/names."
)


def pass_failed(summary: str) -> bool:
    """True when a pass never did its work (provider down, not configured, crashed).

    The agent reports its own failures as text ("⚠️ Foundry error: HTTP 401…") rather than
    raising, so the summary is the only place that says so.
    """
    text = (summary or "").lstrip()
    return text.startswith(("⚠", "error:", "Not configured"))


def swept(entry: dict) -> bool:
    """Did this logged pass actually run the whole-index upkeep?

    Entries from before light passes existed carry no `kind` — every pass was a full sweep
    then — but Autopilot's own notes ("Turned on automatically…") have no tools and are not.
    """
    if entry.get("kind") == "light" or not entry.get("tools"):
        return False
    if entry.get("ok") is False:
        return False
    return not pass_failed(entry.get("summary", ""))


def _load() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


class Autopilot:
    def __init__(self, app) -> None:
        self.app = app
        st = _load()
        # Verification instances use this environment-only kill switch so opening
        # a second real UI cannot start paid model work or mutate the live
        # watchlist. It does NOT overwrite the user's persisted enabled setting.
        self.disabled_by_env = os.getenv("SIGNAL_DISABLE_AUTOPILOT", "").strip() == "1"
        self.enabled = bool(st.get("enabled", False)) and not self.disabled_by_env
        self.interval = max(5, int(st.get("interval_minutes", DEFAULT_INTERVAL)))
        self.log: list[dict] = list(st.get("log", []))[:50]
        self.running_now = False
        self._run_lock = threading.Lock()   # atomic guard for run_once (scheduler vs "Run now")
        self._ctl_lock = threading.Lock()   # serializes start()/loop-exit so a fast Off→On
        #                                     can't leave 'enabled' with no running thread
        self.next_run_ts: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.auto_reason = ""       # set when the provider asks to keep Autopilot on
        self.running_since: float | None = None
        self.running_kind: str | None = None   # "light" / "deep" while a pass runs
        self.last_deep_at = float(st.get("last_deep_at") or 0.0)
        if not self.last_deep_at:
            self.last_deep_at = self.last_sweep_in_log()

    def last_sweep_in_log(self) -> float:
        """When the newest pass that really swept the whole index started (0 if none).

        Before light passes existed every pass was a full sweep, so without this the first
        run after the upgrade would redo an 11-minute sweep that had just run.
        """
        for entry in self.log:
            if swept(entry):
                try:
                    return dt.datetime.strptime(
                        entry.get("at", ""), "%Y-%m-%d %H:%M").timestamp()
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _save(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(
                {"enabled": self.enabled, "interval_minutes": self.interval,
                 "last_deep_at": self.last_deep_at,
                 "log": self.log[:50]}, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # -- control --------------------------------------------------------------------
    def set_enabled(self, on: bool, interval: int | None = None) -> None:
        if self.disabled_by_env:
            self.enabled = False
            self.next_run_ts = None
            return
        self.enabled = bool(on)
        if interval:
            self.interval = max(5, int(interval))
        self._save()
        if self.enabled:
            self.start()
        else:
            self._stop.set()
            self.next_run_ts = None

    def _scraper_running(self) -> bool:
        """Is the background scraper alive? It checks the worker process itself
        (sweep_control.is_running), not radar_status.json, whose message goes stale
        mid-step."""
        try:
            import sweep_control
            return bool(sweep_control.is_running())
        except Exception:  # noqa: BLE001 — unknown state counts as "not running", which
            return False   # only makes Autopilot do MORE upkeep, never less

    def plan(self) -> tuple[str, set, str, str]:
        """(goal, allowed tools, kind, why) for the next pass.

        Deep upkeep costs ~11 minutes of whole-index scanning and finds nothing while the
        scraper is doing the same work every cycle — so it is the exception, not the rule.
        """
        hours = (time.time() - (self.last_deep_at or 0)) / 3600
        if not self._scraper_running():
            deep_why = ("the background scraper is off, so nobody else is labelling topics "
                        "or hiding junk")
        elif not self._scraper_does_upkeep():
            deep_why = ("the scraper's own junk filter or topic labelling is switched off "
                        "in its settings")
        elif hours >= DEEP_EVERY_HOURS:
            deep_why = f"a full sweep has not run for {int(hours)}h"
        else:
            return (LIGHT_GOAL, LIGHT_TOOLS, "light",
                    "the scraper already labels topics and hides junk every cycle")
        if self._cloud_import_active():
            return (LIGHT_GOAL, LIGHT_TOOLS, "light",
                    "a cloud import is saving data right now — the full sweep waits for the "
                    "next run so it does not slow the import down")
        return MAINTENANCE_GOAL, SAFE_TOOLS, "deep", deep_why

    def _scraper_does_upkeep(self) -> bool:
        """Does the scraper's config run the same junk filter + topic labelling each cycle?

        Both default to on in the radar, so a missing key counts as on; an unreadable config
        counts as off, which only makes Autopilot do MORE upkeep, never less.
        """
        try:
            import sweep_control
            cfg = json.loads(sweep_control.WATCHLIST_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        # Same reading as radar.load_config: default True, then bool() of whatever is set.
        return (bool(cfg.get("junk_filter_enabled", True))
                and bool(cfg.get("topic_classify_enabled", True)))

    # A full sweep reads the whole index. Measured 2026-09-19: while one ran, the cloud import
    # (which holds the database's single writer slot while it stages a bundle) read 0.6 MB in
    # 10s against ~900 MB for the scans, sat 11 minutes on 0 of 4 bundles, and finished all
    # four ~50s after the scans stopped. An "importing" state older than this is a dead run.
    IMPORT_FRESH_SECONDS = 60 * 60

    def _cloud_import_active(self) -> bool:
        """Is the cloud import in the middle of saving bundles right now?"""
        try:
            import sweep_control
            path = (sweep_control.SCRIPTER_ROOT / "storage" / "edge_sync"
                    / "sync_progress.json")
            state = str(json.loads(path.read_text(encoding="utf-8")).get("state") or "")
            age = time.time() - path.stat().st_mtime
        except Exception:  # noqa: BLE001 — no file / unreadable: no import to protect
            return False
        return state == "importing" and age < self.IMPORT_FRESH_SECONDS

    def apply_provider_policy(self) -> str:
        """Keep Autopilot on while the selected provider asks for it.

        The operator's rule (2026-09-17): "when Azure is the provider, always turn it on".
        Azure credits are prepaid, so a maintenance pass every N minutes costs nothing
        extra — while a Claude subscription has usage limits, so providers marked "manual"
        leave the Off/On switch entirely to the operator. Returns a one-line reason for
        the card, or "" when this provider leaves the decision alone.
        """
        if self.disabled_by_env:
            return ""
        try:
            provider = beacon_ai.autopilot_provider()
        except Exception:  # noqa: BLE001 — a settings problem must not block the app
            return ""
        if provider.autopilot != "auto":
            self.auto_reason = ""
            return ""
        self.auto_reason = f"kept on automatically because {provider.title} is the provider"
        if not self.enabled:
            self.set_enabled(True)
            self.note(f"Turned on automatically — the provider is {provider.title}.")
        return self.auto_reason

    def note(self, summary: str) -> None:
        """Record something Autopilot did to itself, so the log explains every change."""
        self.log.insert(0, {"at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "tools": [], "summary": summary})
        self.log = self.log[:50]
        self._save()

    def start(self) -> None:
        if self.disabled_by_env:
            return
        # Under _ctl_lock (paired with the loop's exit check) so a fast Off→On is race-free:
        # clearing _stop FIRST means a still-alive thread just keeps running (un-stopped);
        # if the thread is exiting it will have already nulled self._thread under the same
        # lock, so we spawn a fresh one. Either way: enabled ⇒ exactly one runner.
        with self._ctl_lock:
            self._stop.clear()
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop this page's scheduler without changing the saved On setting."""
        with self._ctl_lock:
            self._stop.set()
            self.next_run_ts = None

    def run_now(self) -> None:
        """Trigger an immediate maintenance run (the 'Run now' button)."""
        if self.disabled_by_env:
            return
        threading.Thread(target=self.run_once, daemon=True).start()

    def seconds_to_next(self) -> int | None:
        if not self.enabled or self.next_run_ts is None:
            return None
        return max(0, int(self.next_run_ts - time.time()))

    # -- the loop -------------------------------------------------------------------
    def _loop(self) -> None:
        first = True
        while True:
            # Decide continue-vs-exit under _ctl_lock so a concurrent start() (fast Off→On)
            # can't slip between "I decided to exit" and "I'm gone" and leave no runner.
            with self._ctl_lock:
                if self._stop.is_set() or not self.enabled:
                    self._thread = None   # signal 'no runner' so start() will spawn a fresh one
                    return
            delay = 20 if first else self.interval * 60   # short delay before first run
            first = False
            self.next_run_ts = time.time() + delay
            waited = 0
            while waited < delay and not self._stop.is_set() and self.enabled:
                time.sleep(5)
                waited += 5
            with self._ctl_lock:
                if self._stop.is_set() or not self.enabled:
                    self._thread = None
                    return
            self.run_once()

    def run_once(self) -> None:
        # Atomic test-and-set: a scheduled pass and a manual "Run now" must never both start
        # (double model cost + concurrent writes to the shared index). try/finally then
        # guarantees the flag always clears, so a failed or stalled pass can't wedge every
        # future run.
        with self._run_lock:
            if self.running_now:
                return
            self.running_now = True
        try:
            self._render()
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            # A pass can run for many minutes (the junk filter and rising-video scans read
            # the whole index). The stamp is when it STARTED, so record how long it took —
            # otherwise a finished pass looks like one that only just began.
            started_at = time.time()
            self.running_since = started_at
            actions: list[str] = []
            query_changes: list[dict] = []
            kind, why = "light", ""
            try:
                import sweep_control
                from beacon_agent import BeaconAgent
                goal, tools, kind, why = self.plan()
                self.running_kind = kind
                self._render()
                agent = BeaconAgent(
                    self.app.client, sweep_control, ui=self.app, allowed_tools=tools,
                    autonomous=True, role="autopilot")
                ok, problem = agent.ready()
                if not ok:
                    summary = f"Not configured: {problem}"
                else:
                    def ev(e):
                        if e.get("type") == "tool":
                            actions.append(e.get("name", ""))
                        elif (e.get("type") == "tool_result"
                              and e.get("name") == "add_watchlist_queries"):
                            out = e.get("output")
                            if isinstance(out, dict):
                                query_changes.append({
                                    "accepted": list(out.get("accepted") or []),
                                    "rejected": list(out.get("rejected") or []),
                                    "total": out.get("total"),
                                })
                    summary = agent.run(goal, on_event=ev)
            except Exception as exc:  # noqa: BLE001
                summary = f"error: {type(exc).__name__}: {exc}"
            # A pass that failed (2026-09-19: Azure answered 401 after 0 seconds) did no
            # upkeep. Counting it as the full sweep postponed the real one by 12 hours; now
            # the next wake simply tries the sweep again.
            ok_pass = not pass_failed(summary)
            if kind == "deep" and ok_pass:
                self.last_deep_at = started_at
            self.log.insert(0, {
                "at": stamp,
                "took_secs": int(time.time() - started_at),
                "kind": kind,
                "why": why,
                "ok": ok_pass,
                "tools": actions,
                "summary": (summary or "")[:1200],
                "query_changes": query_changes,
            })
            self.log = self.log[:50]
            self._save()
        finally:
            self.running_now = False
            self.running_kind = None
            self._render()

    def _render(self) -> None:
        try:
            if getattr(self.app, "section", None) == "Agent":
                self.app._render()
        except Exception:  # noqa: BLE001
            pass
