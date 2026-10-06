"""Muse as Beacon's agent — the same tools and rules as the Agent tab, plus a record of every change.

The operator's decision (2026-09-29): "the agent is Muse", with the same powers as Beacon's own
agent. So nothing here re-implements a tool: every call goes through `BeaconAgent.execute`, the
dispatcher the Agent tab uses — the faceless/replicable gate on searches, the query-adding policy,
the locked config edits, the reversible exclusions all apply unchanged. Only the tools that drive
a window on screen (show_channels, open_view, clear_channel_filters) are left out: Muse has no
Beacon window to drive.

What is added is the paper trail, because Muse works from outside and the operator must be able
to see and undo what it did:

  * every call that changes something is written to storage/muse_changes.jsonl with the exact
    arguments, the result, and HOW TO UNDO IT — for a bulk hide that means the actual channel
    ids, read back right after (the tool itself only reports a count);
  * `recent_changes` lists them, `undo_change` reverses one (and is itself recorded).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Tools that change state. Beacon's agent marks the data-changing ones; the scraper controls
# live in sweep_control and change state too.
SCRAPER_CHANGES = {"start_scraper", "stop_scraper", "add_watchlist_queries",
                   "remove_watchlist_queries", "set_scraper_config"}

# Where the door keeps its records — shared with the screens (the Agent tab lists and undoes
# the outside agent's changes from the same file).
STORAGE = Path(__file__).resolve().parent / "storage"
CHANGES_FILE = STORAGE / "muse_changes.jsonl"
GATEWAY_LOG = STORAGE / "muse_gateway.log"


def last_outside_visit(log_path: Path | None = None, tail_bytes: int = 64_000):
    """When the outside agent last used the door successfully (a datetime in the door's local
    time), from the tail of its log. Refusals, the door's own start-up and background-count
    lines are not visits."""
    try:
        with open(log_path or GATEWAY_LOG, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.startswith("[") or "REFUSED" in line or "gateway up" in line \
                or "db_stats" in line or "gateway stopped" in line:
            continue
        try:
            return datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None

GUIDE = """You are Beacon's agent, working through this connection. Beacon finds FACELESS, \
REPLICABLE YouTube channel opportunities in a live index of ~260,000 channels and ~5 million \
videos that grows around the clock on a server.

You may search AND change things: hide junk channels (exclude_channels by id, exclude_by_filter \
in bulk, run_junk_filter), bring them back (unexclude_channels), fix topics (set_channel_topic, \
reclassify_topics), organize niche workspaces (create_niche, add_channel_to_niche), manage the \
user's Saved shelf (save_channel, list_saved_channels, remove_saved_channel), and steer the \
scraper (crawl_niche, start_scraper, stop_scraper, get_watchlist, add_watchlist_queries, \
remove_watchlist_queries, set_scraper_config, scraper_status).

RULES
- "How many ...?" -> count_channels (one exact number). Never list and tally.
- Before a BULK change (exclude_by_filter, run_junk_filter, reclassify_topics with \
only_missing=false) FIRST count_channels to know the scope. Never exclude with no filter. \
Never re-label every topic (only_missing=false) unless the user explicitly asks.
- Searches (find_channels, count_channels, recent_channels) are automatically limited to \
faceless, replicable channels, sorted by opportunity_score unless you choose another sort.
- Adding searches is policy-controlled: report exactly which were accepted and rejected.
- YOUR STANDING JOB — settle shaky FACELESS labels: faceless_review_queue gives channels \
Beacon shows as faceless on weak evidence, best opportunities first, with thumbnails. Look at \
them (open the channel if unsure) and record faceless / on_camera / unclear with \
set_faceless_verdict. A face in a historical photo, movie still or thumbnail illustration is \
NOT on_camera; the same person presenting in most videos IS. Your verdict overrides the \
automatic checks everywhere in Beacon.
- Nothing is ever deleted. Every change you make is recorded with how to undo it: \
recent_changes shows them, undo_change(change_id) reverses one.
- After ANY turn in which you changed something, end with "✓ What I did": each change, how \
many channels it touched, and its change_id (so the user can ask you to undo it).
- Report real numbers and channel names. Be concise."""

_EXTRA_TOOLS = [
    {"name": "recent_changes",
     "description": "Every change made through this connection, newest first: what, when, "
                    "how many, the change_id, and whether it can be undone.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "undo_change",
     "description": "Reverse one earlier change by its change_id (from recent_changes). "
                    "The undo is recorded as a change of its own.",
     "inputSchema": {"type": "object", "properties": {"change_id": {"type": "string"}},
                     "required": ["change_id"]}},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(value: Any, limit: int = 600) -> Any:
    """Keep the log readable: long lists become counts, long text is cut."""
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    if isinstance(value, list) and len(value) > 20:
        return f"[{len(value)} items]"
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


class ChangeLog:
    """Append-only, durable intent and outcome records for each change."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The gateway and Beacon web are separate processes. A tiny lock
            # file prevents their JSON lines from interleaving during writes.
            with open(self.path.with_suffix(self.path.suffix + ".lock"), "a+b") as guard:
                guard.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(guard.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record, default=str) + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                finally:
                    guard.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(guard.fileno(), fcntl.LOCK_UN)

    def all(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out: list[dict] = []
        positions: dict[str, int] = {}
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            change_id = record.get("change_id")
            if record.get("phase") == "started":
                record = {**record, "ok": None, "pending": True}
            if change_id and change_id in positions:
                out[positions[change_id]] = record
            else:
                if change_id:
                    positions[change_id] = len(out)
                out.append(record)
        return out

    def get(self, change_id: str) -> dict | None:
        return next((r for r in self.all() if r.get("change_id") == change_id), None)


class MusePowers:
    def __init__(self, agent, *, db_path: str | Path, changes_path: str | Path,
                 read_config: Callable[[], dict], agent_tools: list[dict],
                 hidden_tools: set[str], change_tools: set[str], reviewer: str = "muse"):
        """`agent` is a BeaconAgent (or anything with `.execute(name, args)`), built with the
        finder's FULL key; `read_config` reads the scraper settings (to undo a settings edit)."""
        self.agent = agent
        self.db_path = str(db_path)
        self.log = ChangeLog(Path(changes_path))
        self.read_config = read_config
        self.change_tools = set(change_tools)
        self.reviewer = reviewer                 # whose faceless decisions these are, on record
        self._tools = [t for t in agent_tools if t["name"] not in hidden_tools] + _EXTRA_TOOLS
        self.names = {t["name"] for t in self._tools}

    @classmethod
    def for_beacon(cls, agent, *, db_path, changes_path, read_config,
                   reviewer: str = "muse") -> "MusePowers":
        import beacon_agent
        tools = [{"name": t["toolSpec"]["name"],
                  "description": t["toolSpec"]["description"],
                  "inputSchema": t["toolSpec"]["inputSchema"]["json"]}
                 for t in beacon_agent.TOOLS]
        return cls(agent, db_path=db_path, changes_path=changes_path, read_config=read_config,
                   agent_tools=tools, hidden_tools=set(beacon_agent._UI),
                   change_tools=set(beacon_agent._DATA_MUTATING_TOOLS) | SCRAPER_CHANGES,
                   reviewer=reviewer)

    def tools(self) -> list[dict]:
        return list(self._tools)

    # -- the library, read-only, for "what exactly changed" ------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=60)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def _excluded(self, reason: str | None = None) -> set[str]:
        if reason is None:
            return {r[0] for r in self._query("SELECT channel_id FROM channel_exclusions")}
        return {r[0] for r in self._query(
            "SELECT channel_id FROM channel_exclusions WHERE reason = ?", (reason,))}

    def _topics(self, ids: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found.update({r[0]: r[1] for r in self._query(
                f"SELECT channel_id, COALESCE(topic, '') FROM channels "
                f"WHERE channel_id IN ({marks})", tuple(chunk))})
        return found

    # -- running a tool ------------------------------------------------------------------
    def run(self, name: str, args: dict) -> tuple[bool, Any]:
        """(ok, result). Never raises: a failure is a result the caller can report."""
        args = dict(args or {})
        if name not in self.names:
            return False, {"error": f"'{name}' is not available on this connection"}
        if name == "recent_changes":
            return True, self.recent(int(args.get("limit") or 20))
        if name == "undo_change":
            return self.undo(str(args.get("change_id") or ""))
        if name not in self.change_tools:
            out, is_error = self.agent.execute(name, args)
            return not is_error, out
        return self._change(name, args)

    def _change(self, name: str, args: dict, undo_of: str | None = None) -> tuple[bool, Any]:
        change_id = uuid.uuid4().hex[:10]
        before: dict[str, Any] = {}
        try:
            if name in {"exclude_channels", "unexclude_channels", "run_junk_filter"}:
                before["excluded"] = self._excluded()
            if name == "exclude_by_filter":
                # Tag the bulk hide so its exact channels can be read back afterwards.
                args["reason"] = f"muse:{change_id}: {args.get('reason') or 'bulk cleanup'}"
            if name == "set_channel_topic":
                before["topics"] = self._topics([str(c) for c in args.get("channel_ids") or []])
            if name == "set_scraper_config":
                before["config"] = self.read_config()
            if name == "set_faceless_verdict":
                # Decisions are on record under WHO made them; an undo is labelled as one.
                args["reviewer"] = "undo" if undo_of else self.reviewer
                import faceless_review
                ids = [str((v or {}).get("channel_id") or "") for v in args.get("verdicts") or []]
                before["verdicts"] = faceless_review.previous_verdicts(self.db_path, ids)
        except Exception as exc:  # noqa: BLE001
            return False, {"error": "Could not check the current state; no change was made.",
                           "detail": f"{type(exc).__name__}: {exc}"}
        try:
            self.log.append({"phase": "started", "change_id": change_id, "at": _now(),
                             "tool": name, "args": args, "undo_of": undo_of})
        except OSError as exc:
            return False, {"error": "Could not record the change; no change was made.",
                           "detail": f"{type(exc).__name__}: {exc}"}
        started = time.time()
        try:
            out, is_error = self.agent.execute(name, args)
        except Exception as exc:  # noqa: BLE001
            out, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
        undo = None if is_error else self._undo_for(name, args, out, before, change_id)
        record = {"phase": "finished", "change_id": change_id, "at": _now(),
                  "tool": name, "args": args,
                  "ok": not is_error, "result": _short(out),
                  "seconds": round(time.time() - started, 1), "undo": undo,
                  "undo_of": undo_of}
        try:
            self.log.append(record)
        except OSError as exc:
            return False, {"error": "The change may have happened, but its result could not be "
                                    "recorded. Check the pending change before retrying.",
                           "change_id": change_id,
                           "detail": f"{type(exc).__name__}: {exc}"}
        if isinstance(out, dict):
            out = {**out, "change_id": change_id,
                   "can_undo": bool(undo and undo.get("steps"))}
        return not is_error, out

    def _undo_for(self, name: str, args: dict, out: Any, before: dict,
                  change_id: str) -> dict | None:
        """How to reverse this exact change — or why it cannot be."""
        steps: list[dict] = []
        note = ""
        was = before.get("excluded")
        try:
            if name == "exclude_channels":
                ids = [c for c in args.get("channel_ids") or [] if was is None or c not in was]
                steps = [{"tool": "unexclude_channels", "args": {"channel_ids": ids}}] if ids else []
            elif name == "exclude_by_filter":
                ids = sorted(self._excluded(args["reason"]))
                steps = [{"tool": "unexclude_channels", "args": {"channel_ids": ids}}] if ids else []
            elif name == "run_junk_filter":
                ids = sorted(self._excluded() - (was or set())) if was is not None else []
                steps = [{"tool": "unexclude_channels", "args": {"channel_ids": ids}}] if ids else []
            elif name == "unexclude_channels":
                ids = [c for c in args.get("channel_ids") or [] if was and c in was]
                steps = ([{"tool": "exclude_channels",
                           "args": {"channel_ids": ids, "reason": f"muse undo {change_id}"}}]
                         if ids else [])
            elif name == "set_channel_topic":
                by_topic: dict[str, list[str]] = {}
                for cid, topic in (before.get("topics") or {}).items():
                    if topic:
                        by_topic.setdefault(topic, []).append(cid)
                steps = [{"tool": "set_channel_topic", "args": {"channel_ids": ids, "topic": t}}
                         for t, ids in by_topic.items()]
            elif name == "set_faceless_verdict":
                restore = [{"channel_id": cid,
                            "verdict": prev.get("ai") if prev.get("ai") in
                            ("faceless", "on_camera", "unclear") else "unclear",
                            "reason": f"undo of {change_id}"}
                           for cid, prev in (before.get("verdicts") or {}).items() if cid]
                steps = [{"tool": "set_faceless_verdict", "args": {"verdicts": restore}}] \
                    if restore else []
                note = "channels without an earlier AI verdict go back to the automatic checks"
            elif name == "set_scraper_config":
                old = before.get("config") or {}
                prev = {k: old[k] for k in args if k in old}
                steps = [{"tool": "set_scraper_config", "args": prev}] if prev else []
            elif name == "add_watchlist_queries":
                accepted = list((out or {}).get("accepted") or []) if isinstance(out, dict) else []
                steps = ([{"tool": "remove_watchlist_queries", "args": {"queries": accepted}}]
                         if accepted else [])
            elif name == "remove_watchlist_queries":
                steps = [{"tool": "add_watchlist_queries",
                          "args": {"queries": list(args.get("queries") or [])}}]
                note = "re-adding goes through the query policy, which may decline some"
            elif name == "start_scraper":
                steps = [{"tool": "stop_scraper", "args": {}}]
            elif name == "stop_scraper":
                steps = [{"tool": "start_scraper", "args": {}}]
            elif name == "save_channel":
                cid = (out or {}).get("channel_id") if isinstance(out, dict) else None
                steps = [{"tool": "remove_saved_channel", "args": {"channel_id": cid}}] if cid else []
            elif name == "remove_saved_channel":
                cid = args.get("channel_id")
                steps = [{"tool": "save_channel", "args": {"link": cid}}] if cid else []
            else:
                note = {"create_niche": "a niche workspace stays; nothing is lost by keeping it",
                        "add_channel_to_niche": "the channel stays filed in that workspace",
                        "crawl_niche": "what a crawl adds is new data; it is kept",
                        "reclassify_topics": "topics are re-derived by rules; run it again "
                                             "rather than undo",
                        "seed_channels": "added channels are kept"}.get(name, "")
        except Exception as exc:  # noqa: BLE001
            note = f"could not record the undo: {type(exc).__name__}: {exc}"
        return {"steps": steps, "note": note}

    # -- reviewing and reversing -----------------------------------------------------------
    def undone_ids(self) -> set[str]:
        return {r.get("undo_of") for r in self.log.all() if r.get("undo_of") and r.get("ok")}

    def recent(self, limit: int = 20) -> dict:
        rows = self.log.all()[-max(1, min(limit, 200)):][::-1]
        return {"changes": [{
            "change_id": r.get("change_id"), "at": r.get("at"), "tool": r.get("tool"),
            "args": r.get("args"), "ok": r.get("ok"), "result": r.get("result"),
            "pending": bool(r.get("pending")),
            "can_undo": bool((r.get("undo") or {}).get("steps")),
            "undo_note": (r.get("undo") or {}).get("note", ""),
            "undo_of": r.get("undo_of")} for r in rows], "total": len(self.log.all())}

    def undo(self, change_id: str) -> tuple[bool, Any]:
        record = self.log.get(change_id)
        if record is None:
            return False, {"error": f"no change with id '{change_id}' (see recent_changes)"}
        if record.get("pending"):
            return False, {"error": "that change has no recorded result yet; check its state first"}
        steps = (record.get("undo") or {}).get("steps") or []
        if not steps:
            return False, {"error": "that change cannot be undone",
                           "why": (record.get("undo") or {}).get("note", "")}
        if any(r.get("undo_of") == change_id and r.get("ok") for r in self.log.all()):
            return False, {"error": f"change {change_id} was already undone"}
        results = []
        ok_all = True
        for step in steps:
            ok, out = self._change(step["tool"], dict(step["args"]), undo_of=change_id)
            ok_all = ok_all and ok
            results.append({"tool": step["tool"], "ok": ok, "result": _short(out)})
        return ok_all, {"undone": change_id, "steps": results}


# -- plain words for people (the Agent tab lists what the outside agent changed) -------------
def _n(value) -> int:
    return len(value) if isinstance(value, (list, tuple)) else int(value or 0)


def describe(record: dict) -> str:
    """One short sentence for a recorded change, e.g. "Hid 3 channels"."""
    tool = record.get("tool", "")
    if record.get("pending"):
        return f"Started {tool.replace('_', ' ')}; result not recorded — check before retrying"
    args = record.get("args") or {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    ids = args.get("channel_ids")
    count = _n(ids) if isinstance(ids, list) else None

    def channels(n):
        return f"{n} channel{'' if n == 1 else 's'}"

    if tool == "exclude_channels":
        text = f"Hid {channels(count or 0)}"
    elif tool == "unexclude_channels":
        text = f"Brought back {channels(count if count is not None else _n(result.get('unexcluded')))}"
    elif tool == "exclude_by_filter":
        text = f"Hid {channels(_n(result.get('excluded')))} matching a filter"
    elif tool == "run_junk_filter":
        text = f"Ran the junk cleaner: hid {channels(_n(result.get('excluded')))}"
    elif tool == "set_channel_topic":
        text = f"Moved {channels(count or 0)} to the topic \"{args.get('topic', '')}\""
    elif tool == "reclassify_topics":
        text = "Re-sorted channels into topics"
    elif tool == "create_niche":
        text = f"Created the niche \"{args.get('name', '')}\""
    elif tool == "add_channel_to_niche":
        text = "Filed a channel into a niche"
    elif tool == "add_watchlist_queries":
        text = f"Added {_n(result.get('accepted'))} of {_n(args.get('queries'))} searches"
    elif tool == "remove_watchlist_queries":
        text = f"Removed {_n(args.get('queries'))} searches"
    elif tool == "set_scraper_config":
        text = "Changed scraper settings: " + ", ".join(f"{k} = {v}" for k, v in args.items())
    elif tool == "set_faceless_verdict":
        parts = [f"{_n(result.get(k))} {label}" for k, label in
                 (("faceless", "faceless"), ("on_camera", "on camera"), ("unclear", "unclear"))
                 if _n(result.get(k))]
        text = "Checked faceless: " + (", ".join(parts) if parts else "no channels")
    elif tool == "start_scraper":
        text = "Started the scraper"
    elif tool == "stop_scraper":
        text = "Stopped the scraper"
    elif tool == "crawl_niche":
        text = f"Searched YouTube for \"{args.get('query', '')}\""
    elif tool == "save_channel":
        text = "Saved a channel to your Saved shelf"
    elif tool == "remove_saved_channel":
        text = "Took a channel off your Saved shelf"
    else:
        text = tool.replace("_", " ").capitalize()
    if not record.get("ok"):
        attempt = {"exclude_channels": "hide channels", "unexclude_channels": "bring channels back",
                   "exclude_by_filter": "hide channels matching a filter",
                   "run_junk_filter": "run the junk cleaner",
                   "set_channel_topic": "change channels' topic",
                   "add_watchlist_queries": "add searches",
                   "remove_watchlist_queries": "remove searches",
                   "set_scraper_config": "change scraper settings",
                   "start_scraper": "start the scraper", "stop_scraper": "stop the scraper",
                   "crawl_niche": "search YouTube", "save_channel": "save a channel",
                   "set_faceless_verdict": "record faceless decisions",
                   "create_niche": "create a niche"}.get(tool, tool.replace("_", " "))
        text = f"Tried to {attempt} — refused, nothing changed"
    if record.get("undo_of"):
        text = f"Undo: {text[0].lower()}{text[1:]}"
    return text
