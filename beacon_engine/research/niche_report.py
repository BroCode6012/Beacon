"""Vision-driven niche report: how tough is the competition, and what can I make?

Smart Scrape already crawls a channel and its niche competitors live and scores
the outlier videos. This module takes that raw material one step further: it
downloads the REAL thumbnails of the strongest videos (analyzed channel +
competitors), pairs each image with its title / views / channel, and sends the
whole board to Claude vision. The model SEES the thumbnails the way a viewer
would, reads the titles next to them, and returns a structured read on the niche:
competition level, the formats and thumbnail patterns that win, the openings a
new faceless channel could take, and a plain go/caution verdict.

The heavy lifting (live scraping, outlier scoring) is done upstream; here we only
turn that into a picture the eye can judge, then a report the user can act on.
"""

from __future__ import annotations

from typing import Callable

from beacon_engine.config import Tier
from beacon_engine.json_utils import as_list, extract_json
from beacon_engine.llm import LLM
from beacon_engine.research.models import (
    ChannelInsight,
    ChannelProfile,
    NicheReport,
    TitlePattern,
    VideoStat,
)
from beacon_engine.research.outliers import IndexedOutlier
from beacon_engine.research.thumbnails import download_thumbnails

Progress = Callable[[str], None]

# Cap: 16 thumbnails is a rich board for one vision call and still cheap
# (~230 tokens each). More channels than videos-per-channel keeps it diverse.
MAX_THUMBNAILS = 16
MAX_PER_CHANNEL = 3


def _outlier_to_stat(o: IndexedOutlier) -> VideoStat:
    return VideoStat(
        video_id=o.video_id, title=o.title, channel=o.channel,
        channel_id=o.channel_id, views=o.views, published_at=o.published_at,
        url=o.url or f"https://www.youtube.com/watch?v={o.video_id}",
    )


def select_report_videos(
    profile: ChannelProfile,
    outliers: list[IndexedOutlier],
    *,
    limit: int = MAX_THUMBNAILS,
    per_channel: int = MAX_PER_CHANNEL,
) -> list[VideoStat]:
    """Pick a diverse, high-signal board of videos for the vision pass.

    Blends the analyzed channel's own top videos with the niche's best outliers
    (its competitors), takes the strongest few PER channel so one giant channel
    can't fill the board, and orders by views."""
    candidates: list[VideoStat] = []
    candidates.extend(v for v in (profile.top_videos or []) if v.video_id)
    candidates.extend(_outlier_to_stat(o) for o in outliers if o.video_id)

    # strongest first, then round-robin a cap per channel for diversity
    candidates.sort(key=lambda v: int(v.views or 0), reverse=True)
    seen_video: set[str] = set()
    per_channel_count: dict[str, int] = {}
    picked: list[VideoStat] = []
    for v in candidates:
        if v.video_id in seen_video:
            continue
        key = v.channel_id or v.channel or v.video_id
        if per_channel_count.get(key, 0) >= per_channel:
            continue
        seen_video.add(v.video_id)
        per_channel_count[key] = per_channel_count.get(key, 0) + 1
        picked.append(v)
        if len(picked) >= limit:
            break
    return picked


_SYSTEM = """You are a YouTube niche strategist advising a FACELESS channel. You are
given the REAL thumbnails of the top videos in one niche — the channel the user
wants to beat plus its live-scraped competitors — and each image is labeled with
its title, view count, and channel. STUDY THE IMAGES THEMSELVES (composition,
color, text, subject, emotion) together with the titles and the view numbers, then
judge the niche as a business opportunity for a new faceless creator.

Tie claims to evidence you can actually see ("the 3 biggest all use X"). NOTE:
"faceless" is about the VIDEO (no on-camera host), NOT the thumbnail — faces in
thumbnails (stock, public figures, shock/reaction) are a normal urgency device that
faceless channels use constantly. If winners use faces on thumbnails, report it as a
pattern to COPY, not something to translate away.

CRITICAL — the faceless business model: big on-camera/authority channels in the board
are NOT competitors, only psychology inspiration (label them that way). Judge
competition_level and opportunity_score against BEATABLE FACELESS channels only
(~1k-500k subs). A niche dominated by on-camera giants but thin on faceless players
= LOW real competition and a HIGH opportunity for a new faceless channel.

Return ONLY JSON in exactly this shape:
{
  "competition_level": "High | Medium | Low",
  "opportunity_score": 0-100,          // how good an opening this niche is for a NEW faceless channel
  "market_snapshot": "2-3 sentences: how big and how saturated this niche looks",
  "competition_summary": "who dominates and WHY they win (format, cadence, packaging)",
  "dominant_formats": ["the video formats that clearly win here, each concrete"],
  "thumbnail_playbook": "a tight rule sheet for faceless thumbnails that win HERE, grounded in the images, plus what to AVOID",
  "title_patterns": ["repeatable title shapes the winners use"],
  "format_opportunities": ["specific formats a NEW faceless channel could make and compete"],
  "content_gaps": ["under-served angles/topics the incumbents are missing"],
  "recommended_angle": "the single best way in, in one sentence",
  "verdict": "one-line go / caution call with the key reason"
}

Be concrete and specific to THIS niche. No preamble, JSON only."""


def _manifest(items: list[tuple[VideoStat, bytes, str]]) -> str:
    return "\n".join(
        f"Image {i+1}: \"{v.title}\" — {v.views:,} views ({v.channel})"
        for i, (v, _, _) in enumerate(items)
    )


def _channels_count(videos: list[VideoStat]) -> int:
    return len({v.channel_id or v.channel for v in videos if (v.channel_id or v.channel)})


def build_niche_report(
    llm: LLM,
    profile: ChannelProfile,
    insight: ChannelInsight,
    outliers: list[IndexedOutlier],
    *,
    niche: str = "",
    tier: Tier = Tier.OPUS,
    limit: int = MAX_THUMBNAILS,
    on_step: Progress = lambda _m: None,
) -> NicheReport:
    """Download the niche's real thumbnails and return a vision-grounded report.

    Best-effort: if thumbnails can't be fetched the model still reasons from titles
    + views; if the model or network fails entirely, a heuristic fallback report is
    returned so the caller always gets something usable."""
    niche = niche or insight.niche or profile.title
    videos = select_report_videos(profile, outliers, limit=limit)
    if not videos:
        return _fallback_report(niche, profile, outliers)

    on_step(f"Downloading {len(videos)} real thumbnails for vision analysis...")
    items = download_thumbnails(videos, limit=limit, on_step=on_step)
    analyzed = [v for v, _, _ in items] or videos
    sample = [TitlePattern(title=v.title, views=v.views, channel=v.channel, url=v.url)
              for v in analyzed]

    context = (
        f"NICHE: {niche}\n"
        f"CHANNEL TO BEAT: {profile.title} "
        f"({profile.subscribers:,} subs)\n"
        f"FORMAT: {insight.format or 'unknown'}\n"
        f"AUDIENCE: {insight.audience or 'unknown'}\n"
        f"Board below: {len(items)} thumbnails across "
        f"{_channels_count(analyzed)} channels (the target + its competitors).\n\n"
        f"{_manifest(items)}\n\n"
        "Analyze the images with their titles and view counts, then return the JSON report."
    )
    images = [(data, media) for _, data, media in items]
    on_step(
        f"Analyzing {len(items)} thumbnails + titles with vision AI..."
        if items else "Analyzing the niche from titles + views..."
    )
    try:
        raw = llm.complete(
            task="analysis",
            tier=tier, instructions=_SYSTEM, prompt=context,
            images=images or None, thinking=True, effort="high", max_tokens=4_000,
        ).text
        data = extract_json(raw)
    except Exception:  # noqa: BLE001 — never let the report break the scrape
        data = None

    if not isinstance(data, dict):
        report = _fallback_report(niche, profile, outliers)
        report.thumbnails_analyzed = len(items)
        report.channels_analyzed = _channels_count(analyzed)
        report.sample_videos = sample[:24]
        return report

    return NicheReport(
        niche=niche,
        competition_level=str(data.get("competition_level", "") or "").strip(),
        opportunity_score=_clamp_score(data.get("opportunity_score")),
        market_snapshot=str(data.get("market_snapshot", "") or "").strip(),
        competition_summary=str(data.get("competition_summary", "") or "").strip(),
        dominant_formats=_str_list(data.get("dominant_formats")),
        thumbnail_playbook=str(data.get("thumbnail_playbook", "") or "").strip(),
        title_patterns=_str_list(data.get("title_patterns")),
        format_opportunities=_str_list(data.get("format_opportunities")),
        content_gaps=_str_list(data.get("content_gaps")),
        recommended_angle=str(data.get("recommended_angle", "") or "").strip(),
        verdict=str(data.get("verdict", "") or "").strip(),
        channels_analyzed=_channels_count(analyzed),
        thumbnails_analyzed=len(items),
        sample_videos=sample[:24],
    )


def _clamp_score(value) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _str_list(value) -> list[str]:
    out: list[str] = []
    for item in as_list(value):
        s = str(item).strip()
        if s:
            out.append(s)
    return out[:12]


def _fallback_report(
    niche: str, profile: ChannelProfile, outliers: list[IndexedOutlier],
) -> NicheReport:
    """Deterministic report when there's no LLM/vision — still grounded in the data."""
    stats = select_report_videos(profile, outliers)
    channels = _channels_count(stats)
    top = sorted(stats, key=lambda v: int(v.views or 0), reverse=True)
    # crude competition read from how many rivals and how high the ceiling is
    ceiling = max((int(v.views or 0) for v in stats), default=0)
    if channels >= 6 and ceiling >= 1_000_000:
        level, score = "High", 35
    elif channels >= 3:
        level, score = "Medium", 55
    else:
        level, score = "Low", 70
    return NicheReport(
        niche=niche,
        competition_level=level,
        opportunity_score=score,
        market_snapshot=(
            f"Saw {len(stats)} strong videos across {channels} channels in "
            f"'{niche}', topping out near {ceiling:,} views."
        ),
        competition_summary="Ranked from the live-scraped outliers (no AI read available).",
        recommended_angle="Re-run with an AI engine connected for the full vision report.",
        verdict=f"{level} competition (heuristic).",
        channels_analyzed=channels,
        sample_videos=[
            TitlePattern(title=v.title, views=v.views, channel=v.channel, url=v.url)
            for v in top[:24]
        ],
    )
