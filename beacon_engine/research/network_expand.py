"""Channel-network expansion via description cross-promotion links.

Keyword search runs out of new channels once a niche is mined. But faceless
channels constantly cross-promote each other -- partner channels, "my other
channel", shout-outs -- as ``youtube.com/channel/UC...`` links in their video
and channel descriptions. Walking those links reaches channels no search query
surfaces, which is how the index keeps growing past the keyword ceiling.

We only read descriptions already stored locally (no new network calls to *find*
candidates), then seed the new channel ids so the normal enrich → catalog →
face-check pipeline processes them.
"""

from __future__ import annotations

import re

# Direct channel-id links only (UC + 22 chars). @handles need a resolve step and
# are skipped here for reliability.
CHANNEL_LINK = re.compile(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{22})", re.I)


def harvest_channel_links(store, *, scan_limit: int = 20000) -> tuple[int, int, int]:
    """Scan stored descriptions for cross-promoted channel ids and seed new ones.

    Returns (links_seen, unique_channels, newly_seeded).
    """
    found: set[str] = set()
    with store.connect() as conn:
        known = {r[0] for r in conn.execute("SELECT channel_id FROM channels")}
        for table in ("videos", "channels"):
            rows = conn.execute(
                f"SELECT description FROM {table} "
                f"WHERE COALESCE(description,'') != '' LIMIT ?",
                (scan_limit,),
            ).fetchall()
            for (desc,) in rows:
                for cid in CHANNEL_LINK.findall(desc or ""):
                    found.add(cid)
    links_seen = len(found)
    new = found - known
    seeded = store.add_channel_seeds(new, source="network")
    return links_seen, len(new), seeded
