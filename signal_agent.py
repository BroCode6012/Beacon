"""Signal's OWN control agent — a self-contained AWS Bedrock (Claude) assistant that
drives the finder + background scraper to search, organize, and control the index.

DELIBERATELY SEPARATE from the Production Lab: this imports NOTHING from
scripter.chat / scripter.agent_tools / scripter.llm. Its only engine links are the
exact bridges the rest of Signal already uses — the FinderClient (read+write finder)
and sweep_control (start/stop the shared scraper). The LLM is a fresh boto3 Bedrock
client; AWS creds + the model id live in Signal's OWN .env (never in code).

Config (Signal/.env, all NEVER in code):
    AWS_ACCESS_KEY_ID=...
    AWS_SECRET_ACCESS_KEY=...
    AWS_REGION=us-east-1              # a region where your Bedrock model is enabled
    SIGNAL_BEDROCK_MODEL=...          # the exact Bedrock model id (Claude Opus)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

# Signal's own .env (AWS creds + model id). Loaded here so the agent is self-contained.
load_dotenv(Path(__file__).with_name(".env"))

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
MAX_TOOL_ROUNDS = 18          # safety cap on tool-use loops per user turn
Event = Callable[[dict], None]

SYSTEM_PROMPT = """You are Signal's control agent — the assistant inside "Signal", a \
YouTube niche-finder desktop app. You help the user SEARCH, ORGANIZE, and CONTROL a \
shared index of YouTube channels and videos. Signal surfaces FACELESS, REPLICABLE \
content opportunities — not general celebrity, music, brand, or one-off viral channels.

You work AUTONOMOUSLY: to accomplish the user's goal, take as many tool steps as you \
need (search → decide → act → verify) WITHOUT asking for permission mid-task. You are \
capable and decisive, like a senior operator who owns this index.

You have full WRITE power to edit + clean + organize the index and guide the scraper:
- Eliminate channels: exclude_channels (by id), exclude_by_filter (bulk by criteria), \
run_junk_filter (auto music/meme/brand). Bring back with unexclude_channels.
- Categorize: reclassify_topics, set_channel_topic (fix a channel's topic).
- Organize: create_niche, add_channel_to_niche.
- Guide the scraper: crawl_niche, start/stop_scraper, add/remove_watchlist_queries, \
set_scraper_config, scraper_status.
- Control the APP'S VIEWS: show_channels applies filters to the Channels tab AND \
switches to it so the USER SEES the results — use it whenever the user wants to SEE / \
filter / browse channels in the app ("show me ...", "filter the channels view", "edit \
the view"). open_view switches tabs; clear_channel_filters resets. (find_channels / \
count_channels only return data to YOU; show_channels changes what the user sees.)
EVERYTHING you exclude is REVERSIBLE (nothing is deleted; unexclude_channels undoes it).

Read/answer tools: find_channels, count_channels, list_topics, list_niches, db_stats, \
recent_channels, get_channel_details, radar_status, find_rising_videos.

RULES:
- For "how many ..." ALWAYS use count_channels (one exact number) — never list + tally \
(find_channels is capped by its limit).
- Before a BULK destructive action (exclude_by_filter / run_junk_filter / \
reclassify_topics only_missing=false), FIRST count_channels to know the scope, then act. \
Never exclude with no filter.
- You are under the user's control via TRANSPARENCY: at the END of any turn where you \
CHANGED anything, finish with a clear report titled "✓ What I did" — a short bullet list \
of each change (action, how many affected, and how to undo). If you only read, just \
answer the question. Always report real numbers and names.
- Every channel has a clean TOPIC (History, Space & Physics, True Crime & Scams, Home \
DIY & Self-Sufficiency, Tech & AI, Money & Finance, ... call list_topics to see them) \
and there are curated NICHE workspaces (call list_niches). Topics ≠ niches.
- find_channels filters COMBINE in one query (min_subs, max_subs, \
min_avg_views, max_avg_views, min/max age, min/max uploads, min_views_per_sub, \
min_outlier_score, category, topic, niche_id, video_format long|short, sort, limit). \
Signal automatically applies replicable_only=true + faceless_only=true to channel searches. \
opportunity_score is the preferred sort because it stabilizes tiny subscriber ratios and \
requires repeatable evidence.
- WRITE / CONTROL actions (start_scraper, stop_scraper, add_watchlist_queries, \
crawl_niche, add_channel_to_niche) change real state. Do them when the user clearly \
asks; afterwards state plainly what you did.
- Query additions are policy-controlled: the tool enforces capacity, family/near-duplicate \
checks, measured harvest backlog, and an autonomous cooldown. Report accepted and rejected \
queries exactly from the tool result.
- Be concise and concrete. Report counts and real channel names. Use numbers. When you \
finish, give a short plain-language summary of what you found or did."""


# --- tool specs (Bedrock `converse` toolConfig format) -----------------------------
def _spec(name, desc, props=None, required=None):
    return {"toolSpec": {"name": name, "description": desc, "inputSchema": {"json": {
        "type": "object",
        "properties": props or {},
        "required": required or [],
    }}}}

_INT = {"type": "integer"}
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_NUM = {"type": "number"}

TOOLS = [
    _spec("db_stats", "Whole-index counts (channels, videos, niches, faceless, etc.)."),
    _spec("list_topics", "The clean topic niches (History/Space/...) with a channel count each."),
    _spec("list_niches", "The curated niche workspaces (id, name) and their sizes."),
    _spec("find_channels",
          "Search the channel index. All filters optional and combine in one query.",
          {"faceless_only": _BOOL, "min_subs": _INT, "max_subs": _INT,
           "min_avg_views": _INT, "max_avg_views": _INT, "min_outlier_score": _NUM,
           "min_days_since_start": _INT, "max_days_since_start": _INT,
           "min_uploads": _INT, "max_uploads": _INT, "min_views_per_sub": _NUM,
           "category_contains": _STR, "category": _STR,
           "topic": _STR, "niche_id": _INT,
           "video_format": {"type": "string", "enum": ["long", "short"]},
           "sort": {"type": "string",
                    "enum": ["opportunity_score", "avg_views", "subscribers",
                             "views_per_sub", "days_since_start", "uploads", "revenue"]},
           "descending": _BOOL, "limit": _INT}),
    _spec("count_channels",
          "Exact COUNT of channels matching filters (same filters as find_channels). "
          "Use this for 'how many ...' — never list + tally.",
          {"faceless_only": _BOOL, "min_subs": _INT, "max_subs": _INT,
           "min_avg_views": _INT, "max_avg_views": _INT,
           "min_outlier_score": _NUM, "max_days_since_start": _INT,
           "category": _STR, "topic": _STR, "niche_id": _INT,
           "video_format": {"type": "string", "enum": ["long", "short"]}}),
    _spec("recent_channels",
          "Newest channels ADDED to the index (discovery time), newest first.",
          {"days": _INT, "faceless_only": _BOOL,
           "min_subs": _INT, "max_subs": _INT,
           "min_avg_views": _INT, "max_avg_views": _INT,
           "min_outlier_score": _NUM, "max_days_since_start": _INT,
           "min_views_per_sub": _NUM, "topic": _STR,
           "video_format": {"type": "string", "enum": ["long", "short"]},
           "category_contains": _STR, "niche_id": _INT, "limit": _INT}),
    _spec("find_rising_videos",
          "Videos gaining views fastest right now (live trend).",
          {"days": _INT, "limit": _INT, "faceless_only": _BOOL}),
    _spec("get_channel_details", "Full profile of one channel by id.",
          {"channel_id": _STR}, ["channel_id"]),
    _spec("get_niche_channels", "Channels tracked in one niche workspace.",
          {"niche_id": _INT}, ["niche_id"]),
    _spec("discover_channels", "Find untracked channels related to a niche workspace.",
          {"niche_id": _INT, "limit": _INT}, ["niche_id"]),
    _spec("radar_status", "Live background-scraper / pipeline state."),
    # -- organize / edit / clean the index (write; ALL reversible) --
    _spec("add_channel_to_niche", "Add an indexed channel to a niche workspace.",
          {"niche_id": _INT, "channel_id": _STR,
           "role": {"type": "string",
                    "enum": ["competitor", "inspiration", "owned"]}},
          ["niche_id", "channel_id"]),
    _spec("create_niche", "Create a new niche workspace (returns niche_id to fill).",
          {"name": _STR, "description": _STR}, ["name"]),
    _spec("exclude_channels", "Hide specific channels from ALL views + scraping (reversible).",
          {"channel_ids": {"type": "array", "items": _STR}, "reason": _STR}, ["channel_ids"]),
    _spec("unexclude_channels", "Bring back previously-excluded channels.",
          {"channel_ids": {"type": "array", "items": _STR}}, ["channel_ids"]),
    _spec("exclude_by_filter",
          "Bulk-exclude ALL channels matching a filter (reversible; needs >=1 filter). "
          "First COUNT with count_channels so you can report how many.",
          {"reason": _STR, "faceless_only": _BOOL, "min_subs": _INT, "max_subs": _INT,
           "min_avg_views": _INT, "max_avg_views": _INT, "topic": _STR,
           "video_format": {"type": "string", "enum": ["long", "short"]}}),
    _spec("run_junk_filter", "Auto-exclude music / meme / big-brand channels (reversible)."),
    _spec("set_channel_topic", "Override the clean topic niche for channels.",
          {"channel_ids": {"type": "array", "items": _STR}, "topic": _STR},
          ["channel_ids", "topic"]),
    _spec("reclassify_topics", "Re-run the topic classifier (only_missing=false re-labels ALL).",
          {"only_missing": _BOOL}),
    # -- control the scraper (write / Signal-local) --
    _spec("crawl_niche", "Scrape a search query NOW: find seeds, crawl top channels, store.",
          {"query": _STR}, ["query"]),
    _spec("scraper_status", "Is the background scraper running, and its latest state."),
    _spec("start_scraper", "Start the background scraper (continuous)."),
    _spec("stop_scraper", "Ask the background scraper to stop after its current step."),
    _spec("get_watchlist",
          "Measured full-watchlist summary: capacity, query coverage/yield, cooldown, "
          "and whether autonomous additions are currently allowed."),
    _spec("add_watchlist_queries",
          "Policy-controlled query addition. Returns exact accepted/rejected queries; "
          "autonomous calls may be declined for backlog, cooldown, duplicates, or capacity.",
          {"queries": {"type": "array", "items": _STR}}, ["queries"]),
    _spec("remove_watchlist_queries", "Remove search queries from the scraper's watchlist.",
          {"queries": {"type": "array", "items": _STR}}, ["queries"]),
    _spec("set_scraper_config", "Tune scraper settings (take effect next cycle).",
          {"min_subs_to_process": _INT, "score_outliers_every_n_cycles": _INT,
           "categorize_per_cycle": _INT, "harvest_per_cycle": _INT}),
    # -- control the APP's views (drive the Signal UI so the user SEES it) --
    _spec("show_channels",
          "Apply filters to the Channels tab AND switch to it so the USER SEES the "
          "results. Use for 'show me ...' / 'filter the channels' / 'edit the view'. "
          "All filters optional and combine.",
          {"topic": _STR, "niche_name": _STR, "faceless_only": _BOOL, "min_subs": _INT,
           "max_subs": _INT, "min_avg_views": _INT, "max_avg_views": _INT,
           "min_outlier_score": _NUM, "max_days_since_start": _INT,
           "video_format": {"type": "string", "enum": ["long", "short"]},
           "sort": {"type": "string",
                    "enum": ["opportunity_score", "avg_views", "subscribers",
                             "views_per_sub", "days_since_start", "uploads", "revenue"]}}),
    _spec("open_view", "Switch the app to a view.",
          {"view": {"type": "string",
                    "enum": ["Overview", "Channels", "Real Time", "Find", "Agent"]}}, ["view"]),
    _spec("clear_channel_filters", "Reset the Channels view (show the whole index)."),
]

# tools handled locally via sweep_control (everything else forwards to the finder)
_LOCAL = {"scraper_status", "start_scraper", "stop_scraper", "get_watchlist",
          "add_watchlist_queries", "remove_watchlist_queries", "set_scraper_config"}
# tools that drive the Signal app UI (need a `ui` reference = the app)
_UI = {"show_channels", "open_view", "clear_channel_filters"}
_SCRAPER_CFG_KEYS = {"min_subs_to_process", "score_outliers_every_n_cycles",
                     "categorize_per_cycle", "harvest_per_cycle"}
_DATA_MUTATING_TOOLS = {
    "add_channel_to_niche", "create_niche", "exclude_channels",
    "unexclude_channels", "exclude_by_filter", "run_junk_filter",
    "set_channel_topic", "reclassify_topics", "crawl_niche",
}


class SignalAgent:
    def __init__(self, finder, sweep, *, ui=None, model: str | None = None,
                 region: str | None = None, allowed_tools: set | None = None,
                 autonomous: bool = False):
        self.finder = finder            # Signal's FinderClient (full read+write key)
        self.sweep = sweep              # the sweep_control module
        self.ui = ui                    # the SignalApp (drives views); None if headless
        self.model = model or os.getenv("SIGNAL_BEDROCK_MODEL") or ""
        self.region = region or REGION
        # Restricted mode: if set, ONLY these tool names are exposed + callable (used by
        # Autopilot to keep unattended runs to safe/reversible maintenance tools).
        self.allowed_tools = allowed_tools
        self.autonomous = bool(autonomous)
        self.messages: list[dict] = []  # conversation history (Bedrock converse format)
        self._client = None

    # -- Bedrock client (lazy so import never fails without creds) -------------------
    def _bedrock(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def ready(self) -> tuple[bool, str]:
        """(ok, message) — is the agent configured to run?"""
        if not self.model:
            return False, "SIGNAL_BEDROCK_MODEL is not set in Signal/.env"
        if not (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")):
            return False, "AWS credentials are not set in Signal/.env"
        return True, "ready"

    # -- tool execution --------------------------------------------------------------
    def _dispatch(self, name: str, args: dict) -> object:
        result = self._dispatch_raw(name, args)
        if (name in _DATA_MUTATING_TOOLS and self.ui is not None
                and not (isinstance(result, dict) and result.get("error"))):
            try:
                self.ui.agent_data_changed(name)
            except Exception:  # noqa: BLE001 — cache invalidation must not break the tool
                pass
        return result

    def _dispatch_raw(self, name: str, args: dict) -> object:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return {"error": f"'{name}' is disabled in this (restricted) mode"}
        # Irreversible-write safety: a RESTRICTED/autonomous pass (e.g. Autopilot) must never
        # re-label the WHOLE index — a full reclassify overwrites manual topic corrections and
        # there is no undo — so force only_missing. A full interactive agent (allowed_tools is
        # None) can still do an explicit whole-index reclassify when the user asks for it.
        if name == "reclassify_topics" and (self.allowed_tools is not None or self.autonomous):
            args["only_missing"] = True
        if name in _UI:
            if self.ui is None:
                return {"error": "no app UI attached (running headless)"}
            if name == "open_view":
                return self.ui.agent_open_view(args.get("view"))
            if name == "clear_channel_filters":
                return self.ui.agent_clear_channels()
            return self.ui.agent_show_channels(**args)      # show_channels
        if name == "scraper_status":
            st = self.sweep.read_status() or {}
            st.pop("watchlist", None)          # too big for the model
            return {"running": self.sweep.is_running(), "state": st.get("state"),
                    "message": st.get("message"), "cycle": st.get("cycle")}
        if name == "start_scraper":
            launched = self.sweep.start_worker()
            return {"launched": bool(launched),
                    "running": self.sweep.is_running(),
                    "note": "already running" if not launched else "starting"}
        if name == "stop_scraper":
            self.sweep.stop_worker()
            return {"ok": True, "note": "stop requested — exits after the current step"}
        if name == "get_watchlist":
            return self.sweep.watchlist_summary()
        if name == "add_watchlist_queries":
            return self.sweep.add_watchlist_queries(
                args.get("queries") or [],
                autonomous=self.autonomous,
                source="autopilot" if self.autonomous else "agent",
            )
        if name == "remove_watchlist_queries":
            drop = {str(q).strip().lower() for q in (args.get("queries") or [])}
            # atomic read-modify-write under the cross-process lock, else a concurrent
            # watchlist writer (radar top-up / Autopilot) can be clobbered by this remove.
            counts = {"before": 0, "after": 0}

            def _rm(cur):
                counts["before"] = len(cur)
                kept = [q for q in cur if str(q).strip().lower() not in drop]
                counts["after"] = len(kept)
                return kept

            self.sweep.update_niches(_rm)
            return {"removed": counts["before"] - counts["after"], "total": counts["after"]}
        if name == "set_scraper_config":
            applied = {}

            def _apply_cfg(data):
                for k in _SCRAPER_CFG_KEYS:
                    if k in args and args[k] is not None:
                        data[k] = int(args[k])
                        applied[k] = int(args[k])
                return data

            # locked read-modify-write of the shared config file (preserves the watchlist +
            # any concurrent edits, instead of overwriting the whole file from a stale read).
            self.sweep.update_config(_apply_cfg)
            return {"applied": applied, "note": "takes effect on the next scraper cycle"}
        # Signal channel searches are always constrained to faceless, repeatable
        # opportunities. The engine flag is optional for Production Lab callers,
        # but mandatory inside this product.
        if name in {"find_channels", "count_channels", "recent_channels"}:
            args["replicable_only"] = True
            args["faceless_only"] = True
        if name == "find_channels" and args.get("sort") in (None, "outlier_score"):
            args["sort"] = "opportunity_score"
        # everything else -> the finder (read + write tools), args forwarded as-is
        return self.finder.call(name, **args)

    # -- the agent loop --------------------------------------------------------------
    def run(self, user_text: str, on_event: Event = lambda _e: None) -> str:
        ok, why = self.ready()
        if not ok:
            return f"⚠️ Agent not configured: {why}."
        client = self._bedrock()
        tools = (TOOLS if self.allowed_tools is None
                 else [t for t in TOOLS if t["toolSpec"]["name"] in self.allowed_tools])
        # Roll-back point: if this turn can't complete cleanly (Bedrock error, or rounds
        # exhausted mid tool-loop), we DROP everything appended this turn. Otherwise the
        # history would end on a user/tool-result message; the next turn appends another
        # user message → two consecutive user turns, which the Converse API rejects,
        # permanently bricking every later turn until Clear.
        base = len(self.messages)
        self.messages.append({"role": "user", "content": [{"text": user_text}]})
        final = ""
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                resp = client.converse(
                    modelId=self.model,
                    system=[{"text": SYSTEM_PROMPT}],
                    messages=self.messages,
                    toolConfig={"tools": tools},
                    inferenceConfig={"maxTokens": 2000, "temperature": 0.0},
                )
            except Exception as exc:  # noqa: BLE001 — surface Bedrock errors to the UI
                del self.messages[base:]
                return f"⚠️ Bedrock error: {type(exc).__name__}: {exc}"
            msg = resp.get("output", {}).get("message", {})
            self.messages.append(msg)
            blocks = msg.get("content", []) or []
            for b in blocks:
                if b.get("text", "").strip():
                    final = b["text"].strip()
                    on_event({"type": "text", "text": final})
            tool_uses = [b["toolUse"] for b in blocks if "toolUse" in b]
            if resp.get("stopReason") != "tool_use" or not tool_uses:
                return final
            results = []
            for tu in tool_uses:
                on_event({"type": "tool", "name": tu["name"], "input": tu.get("input") or {}})
                try:
                    out = self._dispatch(tu["name"], tu.get("input") or {})
                    status = ("error" if isinstance(out, dict) and out.get("error")
                              else "success")
                except Exception as exc:  # noqa: BLE001
                    out = {"error": f"{type(exc).__name__}: {exc}"}
                    status = "error"
                on_event({
                    "type": "tool_result", "name": tu["name"],
                    "status": status, "output": out,
                })
                results.append({"toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": json.dumps(out, default=str)[:6000]}],
                    "status": status,
                }})
            self.messages.append({"role": "user", "content": results})
        # Rounds exhausted without a final assistant text → history ends on tool-results
        # (user role). Roll the whole turn back so the next message isn't rejected.
        del self.messages[base:]
        detail = f" Last partial response: {final}" if final else ""
        return (
            f"⚠️ Stopped after {MAX_TOOL_ROUNDS} tool rounds before the task finished."
            f"{detail}"
        )

    def reset(self) -> None:
        self.messages = []
