"""Detect kids / cartoon channels that are technically 'faceless' but off-niche.

Children's animation and edu-for-kids channels (Peppa Pig recaps, nursery rhymes,
Wolfoo, ABCmouse ...) have no on-camera host, so the face-check flags them
faceless -- but they are junk for a business/documentary faceless strategy.

Detection is deliberately conservative to avoid nuking real channels:
* a channel whose NAME clearly says kids/cartoon, OR
* a channel where kids-keyword videos are a real share of its catalog
  (>= MIN_KIDS_VIDEOS and >= KIDS_FRACTION of its titled videos).
"""

from __future__ import annotations

from dataclasses import dataclass

# Unambiguous brands/terms -> safe to flag on channel NAME alone.
STRONG_NAME_KEYWORDS = [
    "peppa", "bluey", "ben & holly", "ben and holly", "paw patrol", "cocomelon",
    "baby shark", "pj masks", "wolfoo", "blippi", "little baby bum", "chuchu",
    "morphle", "masha and the bear", "kids diana", "vlad and niki", "ryan's world",
    "spidey and his amazing", "gecko's garage", "nursery rhyme", "kids tv",
    "kids academy", "kids learning", "kids cartoon", "kids songs", "kids channel",
    "kidz", "cartoons for kids", "cartoon for kids",
]

# Generic terms that ALSO appear in real docs ("Preschool to Prison Documentary",
# "...for Children's Health") -> only flag if the channel's VIDEOS confirm kids.
WEAK_NAME_KEYWORDS = [
    "for kids", "for children", "toddler", "preschool", "kindergarten", "kiddo",
    "read aloud", "learn bright", "super simple", "kids hut",
]

# Terms that, appearing across many of a channel's video titles, mark it kids.
VIDEO_KEYWORDS = [
    "peppa", "bluey", "ben & holly", "ben and holly", "paw patrol", "cocomelon",
    "baby shark", "pj masks", "wolfoo", "nursery rhyme", "nursery rhymes",
    "for kids", "for children", "kids song", "kids songs", "toddler", "preschool",
    "kindergarten", "lullaby", "abc song", "sing along", "baby songs",
    "kids cartoon", "cartoon for kids", "learning for kids", "kids learning",
    "educational for kids", "read aloud", "story for kids", "sing-along",
]

MIN_KIDS_VIDEOS = 3       # need at least this many kids-keyword videos
KIDS_FRACTION = 0.35      # ...and this share of the channel's titled videos

# Target-niche signal words. If a channel name has one of these, a weak/generic
# kids term is NOT enough to hide it -- protects e.g. "Preschool to Prison
# Documentary". Only a strong brand or a real video-majority can flag it then.
NICHE_GUARD = [
    "documentary", "explained", "exposed", "case study", "mystery",
    "true crime", "history", "investigat",
]


@dataclass
class KidsHit:
    channel_id: str
    title: str
    reason: str
    kids_videos: int
    total_videos: int


def _name_match(title: str, keywords: list[str]) -> str | None:
    low = (title or "").lower()
    for kw in keywords:
        if kw in low:
            return kw
    return None


def find_kids_channels(store) -> list[KidsHit]:
    like_vid = " OR ".join(["LOWER(v.title) LIKE '%'||?||'%'" for _ in VIDEO_KEYWORDS])
    hits: dict[str, KidsHit] = {}
    with store.connect() as conn:
        # Per-channel kids-keyword video counts (for all channels with videos).
        vid_counts: dict[str, tuple[int, int]] = {}
        for cid, kids_n, total_n in conn.execute(
            f"""
            SELECT v.channel_id,
                   SUM(CASE WHEN ({like_vid}) THEN 1 ELSE 0 END) AS kids_n,
                   COUNT(*) AS total_n
            FROM videos v
            WHERE v.channel_id != '' AND COALESCE(v.title,'') != ''
            GROUP BY v.channel_id
            """,
            tuple(VIDEO_KEYWORDS),
        ).fetchall():
            vid_counts[cid] = (int(kids_n), int(total_n))

        for cid, title in conn.execute(
                "SELECT channel_id, title FROM channels").fetchall():
            kids_n, total_n = vid_counts.get(cid, (0, 0))
            video_confirmed = kids_n >= MIN_KIDS_VIDEOS and total_n and \
                (kids_n / total_n) >= KIDS_FRACTION
            strong = _name_match(title, STRONG_NAME_KEYWORDS)
            weak = _name_match(title, WEAK_NAME_KEYWORDS)

            guarded = _name_match(title, NICHE_GUARD) is not None
            if strong:
                reason = f"name~'{strong}'"
            elif video_confirmed:
                reason = f"{kids_n}/{total_n} kids videos"
            elif weak and kids_n >= 2 and not guarded:
                # Generic name term, backed by real kids videos, name not a doc/niche.
                reason = f"name~'{weak}' + {kids_n} kids videos"
            else:
                continue
            hits[cid] = KidsHit(cid, title or "", reason, kids_n, total_n)
    return sorted(hits.values(), key=lambda h: h.kids_videos, reverse=True)
