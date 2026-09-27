"""Beacon's OWN control agent — a self-contained assistant that drives the finder +
background scraper to search, organize, and control the index.

DELIBERATELY SEPARATE from the Production Lab: this imports NOTHING from
scripter.chat / scripter.agent_tools / scripter.llm. Its only engine links are the
exact bridges the rest of Beacon already uses — the FinderClient (read+write finder)
and sweep_control (start/stop the shared scraper).

WHICH MODEL ANSWERS IS A SETTING, NOT CODE
Every model goes through beacon_ai: Azure/OpenAI-compatible services, Claude by API key,
AWS Bedrock, or Claude Code on a subscription. The Agent tab's "AI provider" picker
chooses; ai_providers.json lists what exists; keys live in Beacon/.env. See beacon_ai.py.

This file owns what does NOT change between providers: the system prompt, the tool
definitions (TOOLS, one list for every provider), the dispatcher, and the loop that
alternates model turns with tool calls — written once, so a rule like "roll back a turn
that cannot finish" holds for every provider at the same time.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

import beacon_ai

# Beacon's own .env (keys + model ids). Loaded here so the agent is self-contained.
load_dotenv(Path(__file__).with_name(".env"))

MAX_TOOL_ROUNDS = 18          # safety cap on tool-use loops per user turn
Event = Callable[[dict], None]


SYSTEM_PROMPT = """You are Beacon's control agent — the assistant inside "Beacon", a \
YouTube niche-finder desktop app. You help the user SEARCH, ORGANIZE, and CONTROL a \
shared index of YouTube channels and videos. Beacon surfaces FACELESS, REPLICABLE \
content opportunities — not general celebrity, music, brand, or one-off viral channels.

You work AUTONOMOUSLY: to accomplish the user's goal, take as many tool steps as you \
need (search → decide → act → verify) WITHOUT asking for permission mid-task. You are \
capable and decisive, like a senior operator who owns this index.

You have full WRITE power to edit + clean + organize the index and guide the scraper:
- Eliminate channels: exclude_channels (by id), exclude_by_filter (bulk by criteria), \
run_junk_filter (auto music/meme/brand). Bring back with unexclude_channels.
- Categorize: reclassify_topics, set_channel_topic (fix a channel's topic).
- Organize: create_niche, add_channel_to_niche.
- The SAVED SHELF (channels the user found on YouTube themselves): save_channel takes a \
channel link, an @handle, a bare UC… id, or a link to any of that channel's VIDEOS, adds it \
to the index if it is new, and scores it. list_saved_channels reads the shelf; \
remove_saved_channel takes one off it (the channel stays in the index — only the shelf entry \
goes, so it can be saved again). When the user pastes a YouTube link, save_channel is almost \
always what they want. Note the shelf deliberately does NOT apply the faceless/replicable \
gate: the user chose these, so report a weak one honestly rather than hiding it.
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
Beacon automatically applies replicable_only=true + faceless_only=true to channel searches. \
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
    # -- control the scraper (write / Beacon-local) --
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
    # -- control the APP's views (drive the Beacon UI so the user SEES it) --
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
                    "enum": ["Overview", "Channels", "Real Time", "Find", "Saved",
                             "Agent"]}}, ["view"]),
    _spec("clear_channel_filters", "Reset the Channels view (show the whole index)."),
    # -- the Saved shelf: channels the USER found on YouTube themselves --
    _spec("save_channel",
          "Save a YouTube channel the user found themselves onto the Saved shelf, and "
          "analyse it. Accepts a channel link, an @handle, a bare UC… id, OR a link to any "
          "of its VIDEOS (the channel is looked up from it). Adds it to the index if the "
          "crawler has never seen it. Returns the analysed channel and whether it was new.",
          {"link": _STR}, ["link"]),
    _spec("list_saved_channels",
          "The user's own saved finds, newest first, with their metrics. Beacon's "
          "faceless/replicable gate is deliberately NOT applied here.",
          {"limit": _INT}),
    _spec("remove_saved_channel",
          "Take a channel off the Saved shelf. Removes the shelf entry only — the channel, "
          "its videos and its history stay in the index, so it can be saved again later.",
          {"channel_id": _STR}, ["channel_id"]),
]

# tools handled locally via sweep_control (everything else forwards to the finder)
_LOCAL = {"scraper_status", "start_scraper", "stop_scraper", "get_watchlist",
          "add_watchlist_queries", "remove_watchlist_queries", "set_scraper_config"}
# Saved-shelf tools: orchestrated by beacon_save (resolve link → seed → file → read back),
# which is several finder calls in a required order, not one passthrough.
_SAVED = {"save_channel", "list_saved_channels", "remove_saved_channel"}
# tools that drive the Beacon app UI (need a `ui` reference = the app)
_UI = {"show_channels", "open_view", "clear_channel_filters"}
_SCRAPER_CFG_KEYS = {"min_subs_to_process", "score_outliers_every_n_cycles",
                     "categorize_per_cycle", "harvest_per_cycle"}
_DATA_MUTATING_TOOLS = {
    "add_channel_to_niche", "create_niche", "exclude_channels",
    "unexclude_channels", "exclude_by_filter", "run_junk_filter",
    "set_channel_topic", "reclassify_topics", "crawl_niche",
    "save_channel", "remove_saved_channel", "seed_channels",
}


class BeaconAgent:
    def __init__(self, finder, sweep, *, ui=None, model: str | None = None,
                 region: str | None = None, allowed_tools: set | None = None,
                 autonomous: bool = False, provider=None, role: str = "agent"):
        self.finder = finder            # Beacon's FinderClient (full read+write key)
        self.sweep = sweep              # the sweep_control module
        self.ui = ui                    # the BeaconApp (drives views); None if headless
        # Which settings slot picks the model: "agent" (the chat) or "autopilot".
        self.role = role
        # A provider name or a beacon_ai.Provider pins this agent to it; None follows the
        # operator's choice in the Agent tab, re-read on every turn.
        self._provider_choice = provider
        self._model_override = model
        self._region_override = region
        # Restricted mode: if set, ONLY these tool names are exposed + callable (used by
        # Autopilot to keep unattended runs to safe/reversible maintenance tools).
        self.allowed_tools = allowed_tools
        self.autonomous = bool(autonomous)
        # Neutral history (see beacon_ai): any provider can continue it, so switching the
        # model mid-conversation keeps the thread instead of wiping it.
        self.messages: list[dict] = []
        self._client = None             # an injected Bedrock client (tests)
        self._chat = None
        self._chat_fingerprint = ""
        self._claude_code: dict[str, beacon_ai.ClaudeCodeRunner] = {}

    # -- which provider -----------------------------------------------------------------
    def provider_config(self) -> beacon_ai.Provider:
        choice = self._provider_choice
        if isinstance(choice, beacon_ai.Provider):
            p = choice
        else:
            providers, use = beacon_ai.load()
            name = choice or beacon_ai.choose(self.role, providers=providers, use=use)
            p = beacon_ai.get_provider(str(name), providers)
        if self._model_override or self._region_override:
            p = beacon_ai.dataclasses.replace(
                p, model=self._model_override or p.model,
                region=self._region_override or p.region)
        return p

    @property
    def provider(self) -> str:
        return self.provider_config().name

    @provider.setter
    def provider(self, value) -> None:
        self._provider_choice = value

    @property
    def model(self) -> str:
        return self.provider_config().model

    @model.setter
    def model(self, value: str) -> None:
        self._model_override = value

    def ready(self) -> tuple[bool, str]:
        """(ok, message) — is the agent configured to run?"""
        return beacon_ai.check(self.provider_config(),
                               injected_client=self._client is not None)

    def describe(self) -> str:
        """Short human label for the active model — shown in the UI so the operator
        always knows which model answered."""
        p = self.provider_config()
        return f"{p.title} · {p.model}" if p.model else p.title

    # -- tool execution --------------------------------------------------------------
    def execute(self, name: str, args: dict) -> tuple[object, bool]:
        """Run one tool the way every provider does: (output, is_error), never raising."""
        try:
            out = self._dispatch(name, args or {})
        except Exception as exc:  # noqa: BLE001 — reported to the model as a tool result
            return {"error": f"{type(exc).__name__}: {exc}"}, True
        return out, bool(isinstance(out, dict) and out.get("error"))

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
        if name in _SAVED:
            import beacon_save
            try:
                nid = beacon_save.ensure_saved_niche(self.finder)
                if name == "save_channel":
                    out = beacon_save.save_channel(
                        self.finder, str(args.get("link") or ""), niche_id=nid)
                    if self.ui is not None:
                        # The shelf on screen is now stale; the operator should see what the
                        # agent just added without hunting for a Refresh button.
                        try:
                            self.ui.agent_data_changed(name)
                        except Exception:  # noqa: BLE001
                            pass
                    return out
                if name == "list_saved_channels":
                    return beacon_save.saved_channels(
                        self.finder, nid, limit=int(args.get("limit") or 60))
                return beacon_save.remove_saved(
                    self.finder, nid, str(args.get("channel_id") or ""))
            except beacon_save.SaveError as exc:
                return {"error": str(exc)}
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
        # Beacon channel searches are always constrained to faceless, repeatable
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
        """Answer one user turn, taking as many tool rounds as the task needs."""
        p = self.provider_config()
        ok, why = beacon_ai.check(p, injected_client=self._client is not None)
        if not ok:
            return f"⚠️ Agent not configured: {why}."
        if p.kind == "claude_code":
            return self._run_claude_code(p, user_text, on_event)
        chat = self._chat_for(p)
        tools = self._neutral_tools()
        # Roll-back point: a turn that cannot finish cleanly (provider error, or rounds
        # exhausted mid tool-loop) is dropped WHOLE. Otherwise the history would end on a
        # user or tool-result message, the next turn would add another user message, and
        # APIs that demand alternating turns would reject every later turn until Clear.
        base = len(self.messages)
        self.messages.append({"role": "user", "text": user_text})
        final = ""
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                reply = chat.chat(SYSTEM_PROMPT, self.messages, tools)
            except Exception as exc:  # noqa: BLE001 — surface provider errors to the UI
                del self.messages[base:]
                return f"⚠️ {p.title} error: {type(exc).__name__}: {exc}"
            turn = {"role": "assistant", "text": reply.get("text") or "",
                    "tool_calls": reply.get("tool_calls") or []}
            if reply.get("wire"):
                turn["wire"] = reply["wire"]
            self.messages.append(turn)
            if turn["text"]:
                final = turn["text"]
                on_event({"type": "text", "text": final})
            if not turn["tool_calls"]:
                return final
            results = []
            for call in turn["tool_calls"]:
                name, args = call["name"], call.get("input") or {}
                on_event({"type": "tool", "name": name, "input": args})
                out, is_error = self.execute(name, args)
                on_event({"type": "tool_result", "name": name,
                          "status": "error" if is_error else "success", "output": out})
                results.append({"id": call["id"], "name": name, "is_error": is_error,
                                "content": json.dumps(out, default=str)[:6000]})
            self.messages.append({"role": "tool", "results": results})
        del self.messages[base:]
        detail = f" Last partial response: {final}" if final else ""
        return (
            f"⚠️ Stopped after {MAX_TOOL_ROUNDS} tool rounds before the task finished."
            f"{detail}"
        )

    def _active_tools(self) -> list[dict]:
        """The tools this agent may use — all of them, or only the restricted set."""
        return (TOOLS if self.allowed_tools is None
                else [t for t in TOOLS if t["toolSpec"]["name"] in self.allowed_tools])

    def _neutral_tools(self) -> list[dict]:
        return [{"name": t["toolSpec"]["name"], "description": t["toolSpec"]["description"],
                 "schema": t["toolSpec"]["inputSchema"]["json"]}
                for t in self._active_tools()]

    def _http_post(self, body: dict, *, urls: list[str], headers: dict, timeout: int) -> dict:
        """The single network seam for HTTP providers (tests replace it)."""
        return beacon_ai.post_json(body, urls=urls, headers=headers, timeout=timeout)

    def _chat_for(self, p: beacon_ai.Provider):
        """The adapter for this provider, rebuilt only when its settings change."""
        fp = beacon_ai.fingerprint(p)
        if self._chat is None or fp != self._chat_fingerprint:
            self._chat = beacon_ai.make_chat(
                p, post=lambda body, **kw: self._http_post(body, **kw),
                bedrock_client=lambda: self._client)
            self._chat_fingerprint = fp
        return self._chat

    # -- Claude Code: the CLI runs its own loop; tools come back through the bridge ------
    def _run_claude_code(self, p: beacon_ai.Provider, user_text: str,
                         on_event: Event) -> str:
        runner = self._claude_code.setdefault(p.name, beacon_ai.ClaudeCodeRunner(p))
        runner.p = p

        def list_tools() -> list[dict]:
            return [{"name": t["name"], "description": t["description"],
                     "inputSchema": t["schema"]}
                    for t in self._neutral_tools()
                    if self.ui is not None or t["name"] not in _UI]

        def call_tool(name: str, args: dict) -> tuple[object, bool]:
            on_event({"type": "tool", "name": name, "input": args})
            out, is_error = self.execute(name, args)
            on_event({"type": "tool_result", "name": name,
                      "status": "error" if is_error else "success", "output": out})
            return out, is_error

        try:
            text = runner.run_turn(system=SYSTEM_PROMPT, user_text=user_text,
                                   history=self.messages, list_tools=list_tools,
                                   call_tool=call_tool)
        except Exception as exc:  # noqa: BLE001 — surface provider errors to the UI
            return f"⚠️ {p.title} error: {type(exc).__name__}: {exc}"
        self.messages += [{"role": "user", "text": user_text},
                          {"role": "assistant", "text": text, "tool_calls": []}]
        if text:
            on_event({"type": "text", "text": text})
        return text

    def describe_vision(self, prompt: str, image_bytes: bytes,
                        *, media_type: str = "image/jpeg") -> str:
        """Ask the VISION provider about one image.

        Separate from the agent loop on purpose: the strongest writer is not always able
        to read images, so images go to their own provider (the "vision" slot). Beacon's
        tools are all data tools today, so nothing calls this yet — it exists so
        thumbnail work has a ready, correct path.
        """
        providers, use = beacon_ai.load()
        p = beacon_ai.get_provider(beacon_ai.choose("vision", providers=providers, use=use),
                                   providers)
        ok, why = beacon_ai.check(p)
        if not ok:
            return f"⚠️ No vision model configured: {why}."
        if p.kind == "claude_code":
            return "⚠️ The vision slot needs an API provider, not Claude Code."
        try:
            return beacon_ai.make_chat(p).describe_image(prompt, image_bytes, media_type)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Vision error: {type(exc).__name__}: {exc}"

    def reset(self) -> None:
        self.messages = []
        for runner in self._claude_code.values():
            runner.session_id = None
