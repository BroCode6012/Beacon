"""Scrape Targets — point the background scraper at niches you choose, on demand.

A *target* is one or more base search ideas you add to the scraper's watchlist to focus
discovery on a niche — a clean Topic (e.g. "Space & Physics") or any typed subject
(e.g. "deep sea exploration"). The shared engine fans each base idea into ~10 real searches
and gives never-tested ones priority, so BOTH the laptop worker and the Oracle edge start
finding channels there within a cycle or two. Newly-discovered channels flow through the
normal enrich → categorize → topic-classify → organize pipeline, so they land in the matching
Topic/filters automatically.

You can run several targets at once and toggle any of them off (pause) or remove them — full
control. State lives in a Signal-side JSON (`_targets.json`); the actual scraping is driven
purely by adding/removing the target's base ideas in the shared watchlist via `sweep_control`,
which also flows to the Oracle plan on the next sync. This module is Signal-only and imports
nothing from the shared engine except through `sweep_control`.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from pathlib import Path

import sweep_control

STATE_FILE = Path(__file__).with_name("_targets.json")
_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _seed_for(subject: str) -> str:
    """Turn a user subject / topic label into ONE clean base idea (the engine then fans it into
    ~10 real searches). '&' and stray punctuation make poor search seeds, so normalize them."""
    cleaned = str(subject or "").replace("&", " ").replace(",", " ").lower()
    return " ".join(cleaned.split()).strip()


def _load() -> list[dict]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        items = data.get("targets") if isinstance(data, dict) else data
        return [t for t in (items or []) if isinstance(t, dict)]
    except Exception:  # noqa: BLE001
        return []


def _save(targets: list[dict]) -> None:
    try:
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"targets": targets}, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)  # atomic
    except Exception:  # noqa: BLE001
        pass


def _sync_watchlist(old_targets: list[dict], new_targets: list[dict]) -> None:
    """Reconcile the shared watchlist with the target list: strip every base idea that was
    target-owned before this change, then add back the ones owned by CURRENTLY-ENABLED targets.
    This cleanly handles add / pause / resume / remove while leaving base/manual queries alone
    (they were never target-owned, so they are never stripped)."""
    old_owned = {
        str(q).strip()
        for t in old_targets for q in (t.get("queries") or []) if str(q).strip()
    }
    new_enabled = {
        str(q).strip()
        for t in new_targets if t.get("enabled", True)
        for q in (t.get("queries") or []) if str(q).strip()
    }
    watchlist = [q for q in sweep_control.get_niches() if str(q).strip() not in old_owned]
    have = {str(q).strip() for q in watchlist}
    for query in sorted(new_enabled):
        if query not in have:
            watchlist.append(query)
            have.add(query)
    sweep_control.save_niches(watchlist)


def list_targets() -> list[dict]:
    return _load()


def _yield_state() -> dict:
    """The scraper's per-search yield log (attempts + new-channel counts). File read, no DB."""
    try:
        return sweep_control._read_yield_state() or {}
    except Exception:  # noqa: BLE001
        return {}


def _expanded(queries: list[str]) -> list[str]:
    """The real searches a target's base ideas fan out into (same set the scraper runs)."""
    seeds = [str(q) for q in (queries or []) if str(q).strip()]
    try:
        from scripter.research.channel_discovery import expand_queries
        return expand_queries(seeds)
    except Exception:  # noqa: BLE001
        return seeds


def target_stats(target: dict, yield_state: dict | None = None) -> dict:
    """Live progress for one target from the yield log: how many of its searches the laptop
    scraper has run, and how many NEW channels those searches have surfaced. (The Oracle edge
    also runs them, so real totals can be higher — this is the laptop-side, no-DB view.)"""
    ys = yield_state if yield_state is not None else _yield_state()
    expanded = _expanded(target.get("queries"))
    run = 0
    found = 0
    for query in expanded:
        raw = ys.get(query) or {}
        attempts = max(int(raw.get("attempts", 0) or 0), int(raw.get("searches", 0) or 0))
        if attempts > 0:
            run += 1
        found += int(raw.get("total_new", 0) or 0)
    return {"searches": len(expanded), "run": run, "found": found}


def list_targets_with_stats() -> list[dict]:
    ys = _yield_state()
    return [{**t, "stats": target_stats(t, ys)} for t in _load()]


def add_target(subject: str, *, label: str | None = None, kind: str = "subject") -> dict | None:
    """Add (or re-enable) a target for `subject`. Returns the target dict, or None if empty."""
    seed = _seed_for(subject)
    if not seed:
        return None
    with _LOCK:
        old = _load()
        # If a target already owns this exact seed, just make sure it is enabled.
        for existing in old:
            if seed in [str(q).strip() for q in (existing.get("queries") or [])]:
                new = [{**t, "enabled": True} if t is existing else t for t in old]
                _sync_watchlist(old, new)
                _save(new)
                return next(t for t in new if t.get("id") == existing.get("id"))
        target = {
            "id": uuid.uuid4().hex[:8],
            "label": (label or subject).strip() or seed,
            "kind": kind,
            "queries": [seed],
            "enabled": True,
            "added_at": _now(),
        }
        new = old + [target]
        _sync_watchlist(old, new)
        _save(new)
        return target


def set_enabled(target_id: str, on: bool) -> None:
    with _LOCK:
        old = _load()
        new = [{**t, "enabled": bool(on)} if t.get("id") == target_id else t for t in old]
        _sync_watchlist(old, new)
        _save(new)


def remove_target(target_id: str) -> None:
    with _LOCK:
        old = _load()
        new = [t for t in old if t.get("id") != target_id]
        _sync_watchlist(old, new)
        _save(new)
