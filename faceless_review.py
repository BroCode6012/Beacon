"""An AI reviewer for "is this channel really faceless?" — Muse, or any AI the operator picks.

Why (operator, 2026-09-29): the Overview's "faceless verified" (21,422) only counts channels the
old paid AI vision step confirmed before it was switched off in July. Every visible channel does
carry a faceless label, but for ~57,000 channels with 700+ subscribers that label is shaky: the
free thumbnail face check was UNSURE, or only the text judge decided.

The engine already ranks an AI vision verdict above every automatic check
(store.channel_metrics: vlm > face check > text model > keywords) and stores it with
`store.save_vlm_verdicts`. So a reviewer needs just two things, both here:

  * review_queue()  — the channels Beacon currently SHOWS YOU as faceless on shaky evidence,
    best opportunities first, each with its link, top video titles and thumbnail images;
  * save_verdicts() — the reviewer's decision (faceless / on_camera / unclear), written through
    the engine's own method under the model name "review:<who>", so everyone can see which
    decisions came from which reviewer.

Both are offered as agent tools (beacon_agent.py): Muse gets them through its door with the
change recorded and undoable (muse_powers.py); any AI chosen in the Agent tab can use them too.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

REVIEW_PREFIX = "review:"
DECIDED = {"faceless", "on_camera"}
SCAN_SIZES = (300, 1000, 3000, 5000)    # how deep into the ranked list (the finder caps lists at 5,000)
RANK_CACHE_SECONDS = 600                # the ranking is the slow part; reuse it for 10 minutes
MEGA_SUBSCRIBERS = 2_000_000            # above this, a channel is a star/brand, not a format to copy
HOW_TO_DECIDE = (
    "Look at the thumbnails (open the channel link if unsure). 'faceless' = no recurring human "
    "host on camera: narration over footage, animation, stock or AI visuals, screen recordings, "
    "text/slideshows, hands-only. 'on_camera' = the same person presents on camera in most "
    "videos (face visible as host, vlogger, reactor, podcaster). A face that only appears as a "
    "historical photo, a movie still or a thumbnail illustration does NOT make a channel "
    "on_camera. Use 'unclear' only when you genuinely cannot tell.")

_rank_cache: dict[str, tuple[float, list[dict]]] = {}
_rank_lock = threading.Lock()


def _ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def labels(db_path: str, channel_ids: list[str]) -> dict[str, dict]:
    """What currently decides each channel's faceless label."""
    out: dict[str, dict] = {}
    con = _ro(db_path)
    try:
        for i in range(0, len(channel_ids), 500):
            chunk = channel_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"""SELECT c.channel_id, v.verdict AS ai, v.model AS ai_model,
                               f.verdict AS face, t.faceless AS text_faceless
                        FROM channels c
                        LEFT JOIN channel_vlm_check v ON v.channel_id = c.channel_id
                        LEFT JOIN channel_face_check f ON f.channel_id = c.channel_id
                        LEFT JOIN channel_text_model t ON t.channel_id = c.channel_id
                        WHERE c.channel_id IN ({marks})""", tuple(chunk)):
                out[r["channel_id"]] = dict(r)
    finally:
        con.close()
    return out


def needs_review(label: dict | None) -> str | None:
    """Why a label is shaky (plain words), or None when it is already well founded."""
    label = label or {}
    ai, model = label.get("ai"), str(label.get("ai_model") or "")
    if ai in DECIDED:
        return None                                   # an AI already decided
    if ai == "unclear" and model.startswith(REVIEW_PREFIX) and model != REVIEW_PREFIX + "undo":
        return None                                   # a reviewer already looked and couldn't tell
    if label.get("face") in DECIDED:
        return None                                   # the thumbnail face check was sure
    if label.get("face") == "uncertain":
        return "the thumbnail face check was unsure"
    return "only the text judge decided (no thumbnail check)"


def _ranked(finder, scan: int) -> list[dict]:
    """Beacon's own opportunity ranking of what it shows as faceless (cached briefly)."""
    key = str(scan)
    with _rank_lock:
        hit = _rank_cache.get(key)
        if hit and time.time() - hit[0] < RANK_CACHE_SECONDS:
            return hit[1]
    rows = finder.call("find_channels", faceless_only=True, replicable_only=True,
                       sort="opportunity_score", descending=True, limit=scan)
    rows = rows.get("result", rows) if isinstance(rows, dict) else rows
    rows = list(rows or [])
    with _rank_lock:
        _rank_cache[key] = (time.time(), rows)
    return rows


def _videos(db_path: str, channel_id: str, n: int = 6) -> list[dict]:
    con = _ro(db_path)
    try:
        rows = con.execute(
            """SELECT video_id, title, thumbnail_url FROM videos
               WHERE channel_id = ? ORDER BY COALESCE(views, 0) DESC LIMIT ?""",
            (channel_id, n)).fetchall()
    finally:
        con.close()
    return [{"title": r["title"] or "",
             "thumbnail": r["thumbnail_url"] or f"https://i.ytimg.com/vi/{r['video_id']}/hqdefault.jpg",
             "url": f"https://www.youtube.com/watch?v={r['video_id']}"} for r in rows]


def _deep(db_path: str, limit: int, skip: set[str]) -> list[tuple[dict, str]]:
    """Past Beacon's ranked opportunity list (the finder returns at most 5,000 rows): every
    other channel shown as faceless on shaky evidence, 700+ subscribers — so an always-on
    reviewer never runs out of work. Ordered the way Beacon spots opportunities, by views per
    subscriber; mega channels (over MEGA_SUBSCRIBERS) are skipped: ranked by raw views the
    first picks were Rihanna and Kanye West, which are never a replicable faceless format."""
    con = _ro(db_path)
    try:
        rows = con.execute(
            """SELECT c.channel_id, c.title, c.topic, c.subscribers,
                      CAST(COALESCE(c.view_count, 0) AS REAL) / MAX(COALESCE(c.video_count, 0), 1)
                          AS avg_views,
                      f.verdict AS face
               FROM channels c
               JOIN channel_text_model t ON t.channel_id = c.channel_id AND t.faceless = 1
               LEFT JOIN channel_face_check f ON f.channel_id = c.channel_id
               LEFT JOIN channel_vlm_check v ON v.channel_id = c.channel_id
               WHERE COALESCE(c.subscribers, 0) BETWEEN 700 AND ?
                 AND c.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)
                 AND (f.verdict IS NULL OR f.verdict NOT IN ('faceless', 'on_camera'))
                 AND (v.verdict IS NULL OR v.verdict NOT IN ('faceless', 'on_camera'))
                 -- COALESCE: with no AI row at all, a bare comparison is NULL, and
                 -- NOT (NULL ...) would silently drop exactly the channels never reviewed
                 AND NOT (COALESCE(v.verdict, '') = 'unclear'
                          AND COALESCE(v.model, '') LIKE 'review:%'
                          AND COALESCE(v.model, '') != 'review:undo')
               ORDER BY avg_views / MAX(COALESCE(c.subscribers, 0), 1) DESC
               LIMIT ?""", (MEGA_SUBSCRIBERS, limit + len(skip))).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        if r["channel_id"] in skip:
            continue
        why = ("the thumbnail face check was unsure" if r["face"] == "uncertain"
               else "only the text judge decided (no thumbnail check)")
        out.append(({"channel_id": r["channel_id"], "title": r["title"], "topic": r["topic"],
                     "subscribers": r["subscribers"], "avg_views": int(r["avg_views"] or 0)}, why))
        if len(out) >= limit:
            break
    return out


def review_queue(finder, db_path: str, limit: int = 10) -> dict:
    """The next channels worth a reviewer's eyes, most valuable first."""
    limit = max(1, min(int(limit or 10), 25))
    picked: list[tuple[dict, str]] = []
    scanned = 0
    for scan in SCAN_SIZES:
        rows = _ranked(finder, scan)
        scanned = len(rows)
        found = labels(db_path, [r["channel_id"] for r in rows])
        picked = []
        for r in rows:
            why = needs_review(found.get(r["channel_id"]))
            if why:
                picked.append((r, why))
                if len(picked) >= limit:
                    break
        if len(picked) >= limit or len(rows) < scan:
            break
    source = f"Beacon's top {scanned} opportunities"
    if len(picked) < limit:                              # the ranked list is done: go deeper
        picked += _deep(db_path, limit - len(picked), {r["channel_id"] for r, _ in picked})
        source += " — then the rest, by views per subscriber"
    channels = []
    for r, why in picked:
        channels.append({
            "channel_id": r["channel_id"], "title": r.get("title"),
            "url": f"https://www.youtube.com/channel/{r['channel_id']}",
            "topic": r.get("topic"), "subscribers": r.get("subscribers"),
            "avg_views": r.get("avg_views"), "opportunity_score": r.get("opportunity_score"),
            "shown_as": "faceless", "why_review": why,
            "videos": _videos(db_path, r["channel_id"]),
        })
    return {"channels": channels, "how_to_decide": HOW_TO_DECIDE,
            "then": "call set_faceless_verdict with your decisions (batches are fine)",
            "looked_through_top": scanned, "source": source,
            "left_to_review_estimate": remaining(db_path)}


_remaining_cache: dict[str, tuple[float, int]] = {}


def remaining(db_path: str) -> int:
    """Roughly how many shaky faceless labels (700+ subscribers) are still unreviewed — so a
    reviewer, and the operator, can see the job shrink. Counted at most every 10 minutes."""
    hit = _remaining_cache.get(db_path)
    if hit and time.time() - hit[0] < RANK_CACHE_SECONDS:
        return hit[1]
    con = _ro(db_path)
    try:
        n = int(con.execute(
            """SELECT COUNT(*) FROM channels c
               JOIN channel_text_model t ON t.channel_id = c.channel_id AND t.faceless = 1
               LEFT JOIN channel_face_check f ON f.channel_id = c.channel_id
               LEFT JOIN channel_vlm_check v ON v.channel_id = c.channel_id
               WHERE COALESCE(c.subscribers, 0) BETWEEN 700 AND ?
                 AND c.channel_id NOT IN (SELECT channel_id FROM channel_exclusions)
                 AND (f.verdict IS NULL OR f.verdict NOT IN ('faceless', 'on_camera'))
                 AND (v.verdict IS NULL OR v.verdict NOT IN ('faceless', 'on_camera'))
                 -- COALESCE: with no AI row at all, a bare comparison is NULL, and
                 -- NOT (NULL ...) would silently drop exactly the channels never reviewed
                 AND NOT (COALESCE(v.verdict, '') = 'unclear'
                          AND COALESCE(v.model, '') LIKE 'review:%'
                          AND COALESCE(v.model, '') != 'review:undo')""",
            (MEGA_SUBSCRIBERS,)).fetchone()[0])
    finally:
        con.close()
    _remaining_cache[db_path] = (time.time(), n)
    return n


_stores: dict[str, object] = {}


def _store(db_path: str):
    if db_path not in _stores:
        from beacon_engine.research.store import YouTubeStore   # the engine's own write path
        _stores[db_path] = YouTubeStore(db_path)
    return _stores[db_path]


def save_verdicts(db_path: str, verdicts: list[dict], reviewer: str = "agent") -> dict:
    """Store reviewer decisions through the engine's own method. They outrank every automatic
    check everywhere in Beacon (filters, counts, Real Time)."""
    who = "".join(ch for ch in str(reviewer or "agent").lower() if ch.isalnum() or ch in "-_.")
    clean, refused = [], []
    for v in verdicts or []:
        cid = str((v or {}).get("channel_id") or "").strip()
        verdict = str((v or {}).get("verdict") or "").strip().lower().replace("-", "_")
        verdict = {"on camera": "on_camera", "oncamera": "on_camera"}.get(verdict, verdict)
        if not cid.startswith("UC") or verdict not in DECIDED | {"unclear"}:
            refused.append({"channel_id": cid, "verdict": verdict})
            continue
        clean.append({"channel_id": cid, "verdict": verdict,
                      "raw": str((v or {}).get("reason") or "")[:500],
                      "model": REVIEW_PREFIX + (who or "agent")})
    saved = _store(db_path).save_vlm_verdicts(clean) if clean else 0
    counts = {k: sum(1 for c in clean if c["verdict"] == k) for k in ("faceless", "on_camera", "unclear")}
    with _rank_lock:
        _rank_cache.clear()                   # the next queue must not offer these again
        _remaining_cache.clear()
    out = {"saved": saved, **counts, "reviewer": REVIEW_PREFIX + (who or "agent")}
    if refused:
        out["refused"] = refused
        out["refused_why"] = "needs a UC… channel id and verdict faceless / on_camera / unclear"
    return out


def previous_verdicts(db_path: str, channel_ids: list[str]) -> dict[str, dict]:
    """The AI verdicts in place before a change — what an undo must restore."""
    found = labels(db_path, [str(c) for c in channel_ids])
    return {cid: {"ai": (found.get(cid) or {}).get("ai"),
                  "ai_model": (found.get(cid) or {}).get("ai_model")} for cid in channel_ids}
