"""Save a channel YOU found while scrolling YouTube, and analyse it on the spot.

The rest of Beacon surfaces channels the crawler found. This is the other direction:
you spot a good format yourself, paste the link, and it joins the index under the same
rules as everything else — same faceless/replicable evidence, same topic, same
workspace machinery. A find you noticed by eye should not be worth less than one a
query stumbled onto.

Everything here goes through the FINDER, never straight to SQLite. That is not a
formality: the radar worker and the cloud importer are already writing to that database,
and a third writer opening it from the UI process is how this project previously ended
up with lock stalls. The finder is the one sanctioned writer.

Two YouTube link shapes matter and they resolve differently:
  * a CHANNEL link (/channel/UC…, @handle, /c/…, /user/…) names the channel directly;
  * a VIDEO link (watch?v=…, youtu.be/…, /shorts/…) does not — it has to be looked up,
    and it is the shape you actually have when something good shows up in your feed.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

# Beacon's shelf for hand-picked finds. Created on first save; it is an ordinary niche
# workspace, so saved channels inherit alerts, snapshots and upkeep for free.
SAVED_NICHE_NAME = "Saved from YouTube"

_CHANNEL_ID = re.compile(r"(?:youtube\.com/channel/)?(UC[\w-]{22})", re.I)
_HANDLE = re.compile(r"youtube\.com/@([\w.\-]+)", re.I)
_BARE_HANDLE = re.compile(r"^@([\w.\-]+)$")
_CUSTOM = re.compile(r"youtube\.com/c/([\w.\-]+)", re.I)
_USER = re.compile(r"youtube\.com/user/([\w.\-]+)", re.I)
_VIDEO = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([\w-]{11})", re.I)


class SaveError(RuntimeError):
    """A link we could not turn into a channel, with a message fit for the UI."""


def queue_channel(finder, link: str) -> dict:
    """Record a durable request; queueing is not a claim that analysis finished."""
    try:
        result = finder.call("enqueue_channel_save", link=link)
    except Exception as exc:
        # Finder transport diagnostics are useful in logs, not in a beginner's form.
        known = (
            "Use a YouTube channel or video link.",
            "Paste a YouTube channel or video link.",
            "That YouTube link does not point to a channel or video.",
        )
        message = next((text for text in known if text in str(exc)),
                       "Could not record your request. Please try again.")
        raise SaveError(message) from None
    if not isinstance(result, dict) or result.get("error") or not result.get("id"):
        reason = result.get("error") if isinstance(result, dict) else None
        raise SaveError(str(reason or "Could not record your request. Please try again."))
    return {"queued": True, "request_id": result["id"], "niche_id": result.get("niche_id")}


def request_status(request: dict) -> tuple[str, str]:
    """Plain language distinguishes a kept request from a saved channel."""
    status, saved = request.get("status"), bool(request.get("saved_at"))
    if status == "ready":
        return "Ready", "Saved on your shelf. Its latest available numbers and uploads were checked."
    if status == "failed":
        return ("Saved — analysis needs a retry" if saved else "Save needs attention",
                str(request.get("error") or "This step failed. Press Retry to try again."))
    if saved:
        return "Saved — analysis pending", "The channel is on your shelf. Its numbers and uploads are being checked."
    return ("Finding the channel…" if status == "working" else "Queued",
            "Your request is recorded. You can leave this page; processing continues in the background.")


# The radar worker and the cloud importer write this database constantly, and SQLite lets
# only one writer in at a time. Saving already waits its turn (seed_channels retries
# 4 x 2.5s inside the engine); the shelf's other writes get the same patience here, on
# Beacon's side, so the shared engine does not have to change.
BUSY_TRIES = 4
BUSY_WAIT_SECONDS = 2.5
_BUSY_MARKERS = ("database is locked", "database is busy", "index busy")
_sleep = time.sleep          # swapped out by tests so they do not really wait


def is_busy(problem) -> bool:
    """True when a failure only means another writer had the database at that moment."""
    text = str(problem or "").lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def _call_when_free(finder, tool: str, **args):
    """finder.call that waits out a busy database instead of failing on the first lock.

    Only a lock is retried. Any other failure (bad arguments, a quota error) returns or
    raises at once, because asking again would just repeat the same answer more slowly.
    Every tool sent through here is safe to repeat: refreshing stats, filing a channel
    and removing one all end in the same state however many times they run.
    """
    for attempt in range(1, BUSY_TRIES + 1):
        last = attempt == BUSY_TRIES
        try:
            res = finder.call(tool, **args)
        except Exception as exc:  # noqa: BLE001 — FinderError carries the server's reason
            if last or not is_busy(exc):
                raise
        else:
            if last or not (isinstance(res, dict) and is_busy(res.get("error"))):
                return res
        _sleep(BUSY_WAIT_SECONDS)
    raise AssertionError("unreachable")  # the last attempt always returns or raises


def _when(stamp) -> float:
    """ISO-8601 stamp -> epoch seconds; 0.0 when missing or unreadable."""
    if not stamp:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_link(text: str) -> tuple[str, str]:
    """Classify a pasted link. Returns (kind, value) where kind is one of
    'channel_id' | 'video_id' | 'handle' | 'name'.

    Ordered most-certain first. A channel id is unambiguous, a video id needs one API
    lookup, a handle needs a different lookup, and anything else is treated as a name to
    search for — which is a guess, so callers should show the operator what it matched.
    """
    s = (text or "").strip()
    if not s:
        raise SaveError("Paste a YouTube channel or video link first.")
    # Video BEFORE channel-id: a watch URL can carry a UC-looking string in other params,
    # and the video id is the reliable signal in that shape.
    if m := _VIDEO.search(s):
        return "video_id", m.group(1)
    if m := _CHANNEL_ID.search(s):
        return "channel_id", m.group(1)
    if m := (_HANDLE.search(s) or _BARE_HANDLE.match(s)):
        return "handle", m.group(1)
    if m := (_CUSTOM.search(s) or _USER.search(s)):
        return "name", m.group(1)
    if "youtube.com" in s.lower() or "youtu.be" in s.lower():
        raise SaveError("That looks like a YouTube link, but not one that points at a "
                        "channel or a video.")
    return "name", s


def resolve_channel_id(text: str) -> str:
    """Turn a pasted link into a channel id, calling YouTube only when needed.

    A channel link costs nothing. A video link costs one cheap lookup. Handles and
    names cost a lookup too, and the name path is a search — least reliable, so it is
    the last resort rather than the default.
    """
    kind, value = parse_link(text)
    if kind == "channel_id":
        return value

    # Imported lazily: parsing a link must stay usable (and testable) without the
    # engine, a network, or an API key present.
    from beacon_engine.research.youtube_pool import YouTubePool

    if kind == "video_id":
        items = YouTubePool().videos([value], part="snippet")
        if not items:
            raise SaveError("That video no longer exists, or is private.")
        cid = (items[0].get("snippet") or {}).get("channelId") or ""
        if not cid:
            raise SaveError("Could not read which channel that video belongs to.")
        return cid

    # Handles and custom names: the pool has no forHandle call, and the analyzer already
    # implements every one of YouTube's legacy URL forms plus a search fallback.
    from beacon_engine.research.channel import ChannelAnalyzer

    try:
        return ChannelAnalyzer()._resolve(f"@{value}" if kind == "handle" else value)
    except Exception as exc:  # noqa: BLE001 — surfaced as a readable UI message
        raise SaveError(f"Could not find that channel: {str(exc)[:160]}") from exc


def ensure_saved_niche(finder, name: str = SAVED_NICHE_NAME) -> int:
    """The workspace id for hand-picked finds, creating it the first time."""
    for row in (finder.call("list_niches") or []):
        if str(row.get("name", "")).strip().lower() == name.lower():
            return int(row.get("id") or row.get("niche_id"))
    made = finder.call(
        "create_niche", name=name,
        description="Channels you found yourself on YouTube and saved into Beacon.")
    nid = made.get("niche_id") if isinstance(made, dict) else None
    if not nid:
        raise SaveError(f"Could not create the '{name}' workspace: {made}")
    return int(nid)


def shelf_members(finder, niche_id: int) -> dict[str, dict]:
    """channel_id -> {saved_at, growth} for everything on the shelf, in ONE finder call.

    `list_competitors` returns each workspace membership row, whose `added_at` is the
    moment you saved the channel, together with its measured growth from one bulk query.
    Growth used to cost one finder call per card.
    """
    rows = finder.call("list_competitors", niche_id=int(niche_id))
    if not isinstance(rows, list):
        # Raised, never read as "nothing saved": an empty shelf is a real answer, and a
        # hiccup must not be cached as one.
        raise SaveError(f"Could not read your saved shelf: {rows}")
    try:
        tracking_result = finder.call("saved_channel_tracking", niche_id=int(niche_id))
        tracking = tracking_result.get("channels", {}) if isinstance(tracking_result, dict) else {}
    except Exception:
        # Shelf membership remains usable while the read-only measurement is unavailable.
        tracking = {}
    return {
        r["channel_id"]: {"saved_at": r.get("added_at") or "",
                          "growth": r.get("growth") or {},
                          "tracking": tracking.get(r["channel_id"], {"status": "unavailable"})}
        for r in rows if r.get("channel_id")
    }


def saved_channels(finder, niche_id: int, limit: int = 120) -> list[dict]:
    """Everything on the saved shelf, most recently SAVED first.

    Deliberately WITHOUT Beacon's faceless + replicable gate. Every other view applies
    it, because those channels were proposed by a crawler and the gate is what makes the
    list worth reading. These were chosen by a person: hiding one because it failed an
    automatic test would be overruling the operator about their own find. The card says
    when something falls short instead.

    The order comes from the shelf, not the index. find_channels can only sort by its
    `added_at`, which is when the CRAWLER first indexed a channel, so an old channel you
    save today would land at the bottom. Every row also carries `saved_at` and `growth`,
    so a card never needs a lookup of its own.
    """
    members = shelf_members(finder, niche_id)
    if not members:
        return []
    rows = finder.call("find_channels", niche_id=int(niche_id),
                       faceless_only=False, replicable_only=False,
                       sort="added_at", descending=True,
                       # The whole shelf, so the re-sort below cannot drop a recent save
                       # that the crawler happened to discover long ago.
                       limit=max(int(limit), len(members)))
    if not isinstance(rows, list):
        raise SaveError(f"Could not read your saved channels: {rows}")
    for row in rows:
        entry = members.get(row.get("channel_id")) or {}
        row["saved_at"] = entry.get("saved_at", "")
        row["growth"] = entry.get("growth", {})
        row["tracking"] = entry.get("tracking", {})
    rows.sort(key=lambda r: _when(r.get("saved_at")), reverse=True)
    return rows[:int(limit)]


def growth_summary(row: dict) -> dict | None:
    """What a card may honestly say about growth, or None when there is no trend yet.

    Growth runs from a channel's FIRST snapshot, which is not always the day you saved
    it: a channel already tracked in another workspace was being measured before. Only
    when the first measurement came after the save is it truly "since saved".
    """
    g = row.get("growth") or {}
    gained = int(g.get("subs_gained") or 0)
    if int(g.get("snapshots") or 0) < 2 or not gained:
        return None
    saved = _when(row.get("saved_at"))
    first = _when(g.get("first_captured_at"))
    since = "since saved" if saved and first >= saved else "since first measured"
    return {"gained": gained, "per_day": float(g.get("subs_per_day") or 0.0),
            "since": since}


def refresh_saved(finder, niche_id: int) -> dict:
    """Pull live subscriber/view counts for every saved channel and record a growth point.

    Saved channels go stale fast — exactly the ones worth watching move most. A channel
    saved yesterday at 40,700 subscribers was really on 45,800 the next evening: a 12.5%
    move, invisible on the card. Refreshing also writes a `channel_snapshots` row, which is
    what later turns into "gained N subscribers since you saved it" — without repeated
    snapshots there is no growth to show, only a number.

    Verified by reading the rows BACK rather than trusting the tool's own count:
    `sync_competitors` swallows a transient API failure and still reports `refreshed: 0`,
    which is indistinguishable from "everything was already current".
    """
    before = {r.get("channel_id"): (r.get("subscribers") or 0)
              for r in saved_channels(finder, niche_id, limit=400)}
    res = _call_when_free(finder, "sync_competitors", niche_id=int(niche_id))
    if isinstance(res, dict) and res.get("error"):
        raise SaveError(str(res["error"]))
    after_rows = saved_channels(finder, niche_id, limit=400)
    changed = 0
    gained = 0
    for row in after_rows:
        cid = row.get("channel_id")
        was, now = before.get(cid), (row.get("subscribers") or 0)
        if was is None:
            continue
        if now != was:
            changed += 1
            gained += (now - was)
    return {"channels": len(after_rows), "changed": changed, "subs_gained": gained,
            "rows": after_rows}


def remove_saved(finder, niche_id: int, channel_id: str) -> dict:
    """Take a channel off the saved shelf.

    Removes the workspace membership only. The channel, its videos and its history stay in
    the index — nothing is deleted — so re-saving the same link later costs one lookup and
    brings back everything already known about it. "Remove" here means "I am no longer
    interested", not "erase this".
    """
    cid = (channel_id or "").strip()
    if not cid:
        raise SaveError("No channel to remove.")
    res = _call_when_free(finder, "untrack_competitor", niche_id=int(niche_id), channel=cid)
    if isinstance(res, dict) and res.get("error"):
        raise SaveError(str(res["error"]))
    return {"removed": cid, "niche_id": int(niche_id)}


def channel_analysis(finder, channel_id: str) -> dict:
    """Everything worth reading about one saved channel.

    `get_channel_details` carries the raw row plus its best-performing uploads, which is the
    part that actually teaches a format — the metrics say a channel is working, the video
    list says WHAT is working. Pairs with the computed row already on the card.
    """
    cid = (channel_id or "").strip()
    if not cid:
        raise SaveError("No channel to analyse.")
    details = finder.call("get_channel_details", channel_id=cid, video_limit=12)
    if not isinstance(details, dict) or details.get("error"):
        raise SaveError(str((details or {}).get("error") or "Could not read that channel."))
    return details


def save_channel(finder, link: str, *, niche_id: int | None = None) -> dict:
    """Save one pasted link and return the analysed channel.

    Order matters: seed before filing it, because the workspace only accepts channels
    the index already knows about. Returns the channel row plus `was_new` so the UI can
    say whether this was a genuine discovery or something the crawler already had.
    """
    channel_id = resolve_channel_id(link)
    nid = int(niche_id) if niche_id else ensure_saved_niche(finder)

    seeded = finder.call("seed_channels", channel_ids=[channel_id], source="manual")
    if isinstance(seeded, dict) and seeded.get("error"):
        raise SaveError(str(seeded["error"]))
    was_new = bool(isinstance(seeded, dict) and seeded.get("added"))

    filed = _call_when_free(finder, "add_channel_to_niche", niche_id=nid,
                            channel_id=channel_id)
    if isinstance(filed, dict) and filed.get("error"):
        # The channel IS in the index by now, so say exactly which step failed.
        raise SaveError(f"Added the channel to your index, but could not put it on the "
                        f"shelf: {filed['error']}")
    # Only a brand-new channel can be missing a topic, and only_missing keeps this to
    # the rows that actually need it rather than relabelling the whole index.
    if was_new:
        try:
            finder.call("reclassify_topics", only_missing=True)
        except Exception:  # noqa: BLE001 — a missing label must not fail the save
            pass

    # Read the card row back through find_channels, the SAME call the Channels grid uses.
    # compare_channels looks like the obvious choice but returns growth figures without
    # avg_views / outlier_score / opportunity_score — the three numbers that decide
    # whether a find is worth anything. Using one source also means a saved card and a
    # browsed card can never disagree about the same channel.
    row = next((r for r in saved_channels(finder, nid, limit=400)
                if r.get("channel_id") == channel_id), {})
    row = dict(row)
    row.setdefault("channel_id", channel_id)
    return {
        "channel_id": channel_id,
        "was_new": was_new,
        "niche_id": nid,
        "seeded": seeded if isinstance(seeded, dict) else {},
        "channel": row,
    }
