"""Exclude 'obvious no-need' channels from every view AND from further scraping:
music/label channels (VEVO, YouTube '- Topic' auto-channels), meme/compilation
channels, and big networks/brands. Matches on the channel NAME only (high
precision) and writes to channel_exclusions (reversible via unexclude_channels).

Same pattern as kids_filter / language_filter: find -> exclude the new ones.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable

from beacon_engine.research.store import YouTubeStore

Progress = Callable[[str], None]

# Music / auto-generated artist channels — very high precision.
_MUSIC_SUFFIX = " - topic"          # YouTube auto-topic channels (e.g. "Drake - Topic")
_MUSIC_SUBSTR = ("vevo",)           # "TaylorSwiftVEVO" etc. — 'vevo' never appears in normal names

# Meme / low-value compilation channels (whole-word match).
_MEME_WORDS = (
    "meme", "memes", "dank meme", "dank memes", "funny moments", "funny fails",
    "fails compilation", "tiktok compilation", "best vines", "vine compilation",
    "try not to laugh", "funny compilation", "shorts compilation",
)

# Big networks / brands that aren't indie faceless channels (whole-word match).
_BRAND_WORDS = (
    "netflix", "tedx", "ted-ed", "wwe", "wall street journal", "wall street journal",
    "business insider", "dw documentary", "free documentary", "pbs frontline",
    "frontline pbs", "5-minute crafts", "123 go", "crafty panda", "buzzfeed",
    "history channel", "vogue", "espn",
    # Broadcasters and networks. A documentary house with its own film crews is not a
    # format an indie creator can repeat, so it is noise in an opportunity index however
    # well its uploads perform. National Geographic alone was generating hundreds of
    # "outlier" alerts inside workspaces about indie faceless channels. These match on
    # whole words, so "national geographic" catches its regional arms (UK, Asia, Africa…)
    # without touching an unrelated channel that merely mentions geography.
    "national geographic", "nat geo", "natgeo", "bbc", "bbc earth", "bbc news",
    "discovery channel", "discovery uk", "animal planet", "smithsonian channel",
    "pbs", "pbs nova", "cnbc", "cnn", "abc news", "nbc news", "cbs news", "sky news",
    "al jazeera", "vice news", "vice", "bloomberg", "reuters", "fox news",
    "curiositystream", "nova pbs", "history uk",
    # Broadcaster/editorial brands that appeared as high-ranked "opportunities".
    "cbbc", "wired investigations", "nibble app",
)

_MEME_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in _MEME_WORDS))
_BRAND_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in _BRAND_WORDS))


def classify_junk(title: str) -> str | None:
    """Return 'music' / 'meme' / 'brand' if the channel NAME is obvious junk, else None."""
    t = (title or "").strip().lower()
    if not t:
        return None
    if (t.endswith(_MUSIC_SUFFIX) or t.endswith("musical")
            or any(s in t for s in _MUSIC_SUBSTR)):
        return "music"
    if _BRAND_RE.search(t):
        return "brand"
    if _MEME_RE.search(t):
        return "meme"
    return None


def opportunity_name_rejection(title: str) -> str | None:
    """Conservative, read-only name gate for faceless opportunity queries.

    This does not exclude rows from the index. The regular junk/kids cleaners
    still own reversible, evidence-backed exclusions for all other views.
    """
    reason = classify_junk(title)
    if reason:
        return reason
    from beacon_engine.research.kids_filter import STRONG_NAME_KEYWORDS

    low = (title or "").lower()
    if any(word in low for word in STRONG_NAME_KEYWORDS):
        return "kids"
    if "k-pop" in low or "kpop" in low:
        return "music"
    return None


def find_junk_channels(store: YouTubeStore | None = None) -> list[tuple[str, str]]:
    """(channel_id, reason) for every channel whose name is obvious junk."""
    store = store or YouTubeStore()
    out: list[tuple[str, str]] = []
    with store.connect() as conn:
        for cid, title in conn.execute(
                "SELECT channel_id, title FROM channels WHERE COALESCE(title,'') != ''"):
            reason = classify_junk(title)
            if reason:
                out.append((cid, reason))
    return out


def find_music_channels(store: YouTubeStore | None = None, *,
                        min_videos: int = 3, ratio: float = 0.5) -> list[str]:
    """Channels whose videos are predominantly YouTube category 10 (Music) — the
    reliable, first-party way to catch music channels (e.g. artist channels the
    name filter misses). Reads the categorised videos.category_id column (populated
    for all sources by research/categorize.py), so coverage grows as more videos
    get a category fetched."""
    store = store or YouTubeStore()
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT channel_id
            FROM videos
            WHERE category_id NOT IN ('', 'na') AND category_id IS NOT NULL
            GROUP BY channel_id
            HAVING COUNT(*) >= ?
               AND SUM(CASE WHEN category_id = '10' THEN 1 ELSE 0 END)
                     * 1.0 / COUNT(*) >= ?
            """, (int(min_videos), float(ratio))).fetchall()
    return [r[0] for r in rows]


def exclude_junk(store: YouTubeStore | None = None, *,
                 on_step: Progress = lambda _m: None) -> dict:
    """Exclude newly-found junk channels (music/meme/brand). Reversible.
    Music is caught two ways: by name (VEVO/'- Topic') AND by YouTube category
    (videos predominantly category 10)."""
    store = store or YouTubeStore()
    already = store.excluded_channel_ids()
    by_reason: dict[str, set] = defaultdict(set)
    for cid, reason in find_junk_channels(store):        # name-based
        if cid not in already:
            by_reason[reason].add(cid)
    try:
        for cid in find_music_channels(store):           # category-based music
            if cid not in already:
                by_reason["music"].add(cid)
    except Exception:  # noqa: BLE001 — category signal is best-effort
        pass
    total = 0
    for reason, ids in by_reason.items():
        total += store.exclude_channels(list(ids), reason=reason)
    if total:
        on_step("Excluded junk channels: " +
                ", ".join(f"{len(ids)} {r}" for r, ids in by_reason.items()))
    return {"excluded": total, "by_reason": {r: len(ids) for r, ids in by_reason.items()}}
