"""Signal Autopilot — OPTIONAL background maintenance loop (OFF by default).

When enabled, every N minutes it runs a RESTRICTED agent that does only SAFE,
reversible maintenance — categorize newly-discovered channels, hide new junk/music,
and (sparingly) top up the scraper's queries if it's underfed — then appends a
timestamped entry to an activity log you review in the Agent tab.

It CANNOT do destructive things: the agent is locked to a safe toolset (no bulk
exclude / delete / topic-override / niche-create / scraper start-stop). Runs inside
the Signal app while it's open (the durable background scraper handles discovery on
its own). Separate from the Lab; uses Signal's own SignalAgent (Bedrock).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

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
        # a second real UI cannot start paid Bedrock work or mutate the live
        # watchlist. It does NOT overwrite the user's persisted enabled setting.
        self.disabled_by_env = os.getenv("SIGNAL_DISABLE_AUTOPILOT", "").strip() == "1"
        self.enabled = bool(st.get("enabled", False)) and not self.disabled_by_env
        self.interval = max(5, int(st.get("interval_minutes", DEFAULT_INTERVAL)))
        self.log: list[dict] = list(st.get("log", []))[:50]
        self.running_now = False
        self.next_run_ts: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _save(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(
                {"enabled": self.enabled, "interval_minutes": self.interval,
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

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

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
        while not self._stop.is_set() and self.enabled:
            delay = 20 if first else self.interval * 60   # short delay before first run
            first = False
            self.next_run_ts = time.time() + delay
            waited = 0
            while waited < delay and not self._stop.is_set() and self.enabled:
                time.sleep(5)
                waited += 5
            if self._stop.is_set() or not self.enabled:
                break
            self.run_once()

    def run_once(self) -> None:
        if self.running_now:
            return
        self.running_now = True
        self._render()
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        actions: list[str] = []
        query_changes: list[dict] = []
        try:
            import sweep_control
            from signal_agent import SignalAgent
            agent = SignalAgent(
                self.app.client, sweep_control, ui=self.app, allowed_tools=SAFE_TOOLS,
                autonomous=True)
            ok, why = agent.ready()
            if not ok:
                summary = f"Not configured: {why}"
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
                summary = agent.run(MAINTENANCE_GOAL, on_event=ev)
        except Exception as exc:  # noqa: BLE001
            summary = f"error: {type(exc).__name__}: {exc}"
        self.log.insert(0, {
            "at": stamp,
            "tools": actions,
            "summary": (summary or "")[:1200],
            "query_changes": query_changes,
        })
        self.log = self.log[:50]
        self._save()
        self.running_now = False
        self._render()

    def _render(self) -> None:
        try:
            if getattr(self.app, "section", None) == "Agent":
                self.app._render()
        except Exception:  # noqa: BLE001
            pass
