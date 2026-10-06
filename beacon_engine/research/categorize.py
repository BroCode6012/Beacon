"""Populate videos.category_id (YouTube snippet.categoryId, e.g. '10' = Music).

Two stages, cheapest first:
1. FREE backfill from raw_json — official-API videos already carry categoryId in
   their stored JSON, so we copy it into the column with zero quota.
2. Pool fetch — for the remaining videos (discovered via scrapetube etc., no
   categoryId in JSON) fetch snippet via the rotating API-key pool (1 unit / 50
   videos). Videos the API can't return (deleted/private) are marked 'na' so we
   never re-fetch them.

Category is per-video; `channel_categories()` rolls it up to a dominant category
per channel (the reliable, first-party way to spot music/other channel types).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Callable

from beacon_engine.logging_util import get_logger
from beacon_engine.research.store import YouTubeStore
from beacon_engine.research.youtube_pool import QuotaExhausted, YouTubePool

log = get_logger()
Progress = Callable[[str], None]

# Standard YouTube videoCategory IDs (US) — static, so no quota to look them up.
CATEGORY_NAMES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
    "15": "Pets & Animals", "17": "Sports", "18": "Short Movies",
    "19": "Travel & Events", "20": "Gaming", "21": "Videoblogging",
    "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
    "25": "News & Politics", "26": "Howto & Style", "27": "Education",
    "28": "Science & Technology", "29": "Nonprofits & Activism", "30": "Movies",
    "31": "Anime/Animation", "32": "Action/Adventure", "33": "Classics",
    "34": "Comedy", "35": "Documentary", "36": "Drama", "37": "Family",
    "38": "Foreign", "39": "Horror", "40": "Sci-Fi/Fantasy", "41": "Thriller",
    "42": "Shorts", "43": "Shows", "44": "Trailers", "na": "(unavailable)",
}


def category_name(cid: str | None) -> str:
    return CATEGORY_NAMES.get(str(cid or ""), str(cid or ""))


def backfill_from_raw_json(store: YouTubeStore | None = None,
                           *, on_step: Progress = lambda _m: None) -> int:
    """Copy categoryId out of stored raw_json into the column. Zero quota."""
    store = store or YouTubeStore()
    with store.connect() as conn:
        cur = conn.execute(
            """
            UPDATE videos
            SET category_id = json_extract(raw_json, '$.snippet.categoryId')
            WHERE (category_id IS NULL OR category_id = '')
              AND json_valid(raw_json)
              AND json_extract(raw_json, '$.snippet.categoryId') IS NOT NULL
            """
        )
        n = cur.rowcount
    on_step(f"Backfilled {n:,} categories from stored JSON (free)")
    return n


def fetch_missing(store: YouTubeStore | None = None,
                  pool: YouTubePool | None = None,
                  *, limit: int | None = None, batch: int = 50,
                  on_step: Progress = lambda _m: None) -> dict:
    """Fetch categoryId via the API-key pool for videos still missing it."""
    store = store or YouTubeStore()
    pool = pool or YouTubePool()
    with store.connect() as conn:
        sql = "SELECT video_id FROM videos WHERE category_id IS NULL OR category_id = ''"
        if limit:
            sql += f" LIMIT {int(limit)}"
        todo = [r[0] for r in conn.execute(sql)]
    on_step(f"{len(todo):,} videos need a category fetched via API")
    fetched = marked_na = 0
    for j in range(0, len(todo), batch):
        chunk = todo[j:j + batch]
        try:
            items = pool.videos(chunk, part="snippet")
        except QuotaExhausted:
            on_step(f"All keys exhausted at {j:,}/{len(todo):,} — stopping")
            break
        got = {it["id"]: str(it.get("snippet", {}).get("categoryId", "") or "")
               for it in items}
        rows = []
        for vid in chunk:
            cid = got.get(vid)
            if cid:
                rows.append((cid, vid)); fetched += 1
            else:
                rows.append(("na", vid)); marked_na += 1     # deleted/private
        with store.connect() as conn:
            conn.executemany(
                "UPDATE videos SET category_id = ? WHERE video_id = ?", rows)
        if (j // batch) % 20 == 0:
            on_step(f"{j + len(chunk):,}/{len(todo):,} fetched  "
                    f"(got {fetched:,} / na {marked_na:,} / ~{pool.units:,} units, "
                    f"{pool.alive}/{len(pool.keys)} keys alive)")
    return {"fetched": fetched, "na": marked_na, "units": pool.units,
            "keys_alive": pool.alive}


def category_summary(store: YouTubeStore | None = None) -> list[tuple[str, str, int]]:
    """(category_id, name, video_count) for populated categories, biggest first."""
    store = store or YouTubeStore()
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT category_id, COUNT(*) c
            FROM videos
            WHERE category_id NOT IN ('', 'na') AND category_id IS NOT NULL
            GROUP BY category_id ORDER BY c DESC
            """
        ).fetchall()
    return [(r[0], category_name(r[0]), r[1]) for r in rows]


def channel_categories(store: YouTubeStore | None = None,
                       *, min_videos: int = 3) -> dict[str, tuple[str, float]]:
    """channel_id -> (dominant category_id, share 0..1) over its categorised videos."""
    store = store or YouTubeStore()
    per: dict[str, Counter] = defaultdict(Counter)
    with store.connect() as conn:
        for cid, cat in conn.execute(
                "SELECT channel_id, category_id FROM videos "
                "WHERE category_id NOT IN ('', 'na') AND category_id IS NOT NULL"):
            per[cid][cat] += 1
    out: dict[str, tuple[str, float]] = {}
    for cid, counter in per.items():
        total = sum(counter.values())
        if total < min_videos:
            continue
        top, n = counter.most_common(1)[0]
        out[cid] = (top, n / total)
    return out


def run(store: YouTubeStore | None = None, pool: YouTubePool | None = None,
        *, fetch: bool = True, limit: int | None = None,
        on_step: Progress = lambda _m: None) -> dict:
    """Full categorize pass: free backfill, then (optionally) pool fetch."""
    store = store or YouTubeStore()
    backfilled = backfill_from_raw_json(store, on_step=on_step)
    result = {"backfilled": backfilled}
    if fetch:
        result.update(fetch_missing(store, pool, limit=limit, on_step=on_step))
    return result
