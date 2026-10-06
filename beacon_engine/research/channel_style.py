"""Split topic-matched channels into DIRECT competitors vs INSPIRATION, by FORMAT.

Topic overlap alone is not enough: searching "Jewish DNA" surfaces a faceless AI-documentary
channel (a real competitor) AND a real on-camera professor and a big media org (same topic,
totally different format). This uses Claude vision to LOOK at the pasted channel's thumbnails
and each candidate's, judging how closely the FORMAT/STYLE matches — so we can spy on true
look-alikes and keep the rest as angle/inspiration fuel.
"""

from __future__ import annotations

from beacon_engine.config import Tier
from beacon_engine.json_utils import extract_json
from beacon_engine.llm import LLM
from beacon_engine.research.models import ChannelProfile, CompetitorChannel, VideoStat
from beacon_engine.research.thumbnails import download_thumbnails

_SYSTEM = """You compare YouTube channels by FORMAT and STYLE — NOT by topic.

You are shown a REFERENCE channel's thumbnails first, then ONE thumbnail from each CANDIDATE
channel. Judge how closely each candidate's FORMAT matches the reference:
- Faceless vs a real on-camera presenter / talking head
- AI-generated / illustrated / stock-composited imagery vs real footage or a filmed person
- Thumbnail aesthetic: layout, text-banner style, colour treatment, overall vibe
- Production type, polish, and title style

Score each candidate 0-10, where 10 = essentially the SAME KIND of channel as the reference
(a direct competitor you'd study and beat) and 0 = a totally different format (e.g. an
academic lecturer filmed on camera, a big professional media org, a real-footage documentary)
EVEN IF the topic overlaps. Also flag whether the candidate is faceless.

Return ONLY JSON mapping candidate number to {"score": <0-10>, "faceless": <true|false>}.
Example: {"1": {"score": 9, "faceless": true}, "2": {"score": 2, "faceless": false}}"""


def split_by_format(
    llm: LLM, profile: ChannelProfile, competitors: list[CompetitorChannel],
    videos_by_channel: dict[str, list[VideoStat]], *, threshold: float = 6.0,
    require_faceless: bool = True,
    tier: Tier = Tier.SONNET,
) -> tuple[list[CompetitorChannel], list[CompetitorChannel]]:
    """Return (direct_competitors, inspiration_channels). Degrades gracefully: if vision or
    thumbnails are unavailable, everything stays a direct competitor."""
    if not competitors or not profile.top_videos:
        return competitors, []

    ref = download_thumbnails(profile.top_videos[:3], limit=3)
    if not ref:
        return competitors, []
    ref_imgs = [(b, m) for _, b, m in ref]

    cand_imgs: list[tuple[bytes, str]] = []
    classified: list[CompetitorChannel] = []
    for c in competitors:
        vids = videos_by_channel.get(c.channel_id, [])
        if not vids:
            continue
        got = download_thumbnails(vids[:1], limit=1)
        if got:
            cand_imgs.append((got[0][1], got[0][2]))
            classified.append(c)
    if not cand_imgs:
        return competitors, []

    n_ref = len(ref_imgs)
    lines = [f"Candidate {i + 1} (image #{n_ref + i + 1}): \"{c.title}\" — "
             f"e.g. {(c.sample_titles[0] if c.sample_titles else '')}"
             for i, c in enumerate(classified)]
    prompt = (
        f"The first {n_ref} images are the REFERENCE channel to match. The remaining images "
        f"are candidate channels, one thumbnail each:\n" + "\n".join(lines) +
        "\n\nScore each candidate's FORMAT/STYLE match to the reference and return the JSON.")

    try:
        data = extract_json(llm.complete(
            task="analysis",
            tier=tier, instructions=_SYSTEM, prompt=prompt,
            images=ref_imgs + cand_imgs, thinking=True, effort="medium",
            max_tokens=1500).text)
    except Exception:  # noqa: BLE001 — vision unavailable -> keep all as competitors
        return competitors, []
    if not isinstance(data, dict):
        return competitors, []

    direct, inspiration = [], []
    for i, c in enumerate(classified):
        rec = data.get(str(i + 1)) or {}
        c.style_match = float(rec.get("score", 10) or 0)
        c.faceless = bool(rec.get("faceless", True))
        is_direct = c.style_match >= threshold
        if require_faceless:
            is_direct = is_direct and c.faceless
        (direct if is_direct else inspiration).append(c)

    # When faceless is required, unknown/no-thumbnail channels stay out of direct.
    skipped = [c for c in competitors if c not in classified]
    if require_faceless:
        inspiration.extend(skipped)
    else:
        direct.extend(skipped)
    direct = sorted(direct, key=lambda c: c.style_match, reverse=True)
    inspiration = sorted(inspiration, key=lambda c: c.total_views, reverse=True)
    return direct, inspiration
