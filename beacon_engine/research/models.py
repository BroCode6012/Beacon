"""Typed containers for everything the research phase produces.

This is the "ammunition" the writing agents consume later. Keep these serializable
(pydantic) so a full research bundle can be saved to disk and reused across runs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VideoStat(BaseModel):
    video_id: str
    title: str
    channel: str
    channel_id: str = ""
    views: int
    published_at: str
    url: str


class TitlePattern(BaseModel):
    """A title worth modeling, drawn from the ceiling of a keyword search."""

    title: str
    views: int
    channel: str
    url: str


class CeilingFloor(BaseModel):
    """Result of ceiling/floor mining for a single keyword + time window.

    ceiling = the top performer (the bar to clear). floor = where views fall off a
    cliff; titles between ceiling and floor are the proven patterns worth stealing.
    """

    keyword: str
    time_window: str  # "all_time" | "12_months" | "30_days"
    ceiling: VideoStat | None = None
    floor_views: int = 0
    patterns: list[TitlePattern] = Field(default_factory=list)


class RedditPain(BaseModel):
    """A real audience pain point / emotional angle mined from Reddit."""

    subreddit: str
    title: str
    score: int
    num_comments: int
    upvote_ratio: float  # low ratio + high comments = controversy ("content with nerves")
    url: str
    quote: str = ""  # an exact quote (post body or hot comment)


class Transcript(BaseModel):
    """A competitor video transcript used as a writing training example."""

    video_id: str
    title: str
    url: str
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class CommentPain(BaseModel):
    """A cluster of audience pain/desire mined from competitor video comments."""

    theme: str                        # the pain/desire in one line
    signal: str = ""                  # confusion|objection|request|missing|emotion|pain
    frequency: str = ""               # rare | common | very common
    quotes: list[str] = Field(default_factory=list)   # representative real quotes
    hook_fuel: str = ""               # a punchy spoken line that opens a video on this
    angle: str = ""                   # a video angle this comment cluster demands
    evidence_note: str = "Selected audience reactions; factual claims and proposed hooks/angles need independent checking."


class ChannelProfile(BaseModel):
    """What we pull straight from YouTube about a channel."""

    channel_id: str
    title: str
    handle: str = ""
    description: str = ""
    subscribers: int = 0
    video_count: int = 0
    view_count: int = 0
    top_videos: list[VideoStat] = Field(default_factory=list)   # by views
    recent_titles: list[str] = Field(default_factory=list)
    catalog_videos: list[VideoStat] = Field(default_factory=list)  # bounded upload catalog, not just winners

    def existing_titles(self) -> list[str]:
        """All titles the channel has used (top + recent), de-duped — so new ideas
        can be checked against what's already been done."""
        seen, out = set(), []
        for t in [v.title for v in self.catalog_videos + self.top_videos] + self.recent_titles:
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(t)
        return out


class BreakoutVideo(BaseModel):
    """A video that punched far above its channel's size — an opening signal.

    The killer signal: small channel + recent upload + high views-per-subscriber means
    real demand the big channels haven't saturated yet.
    """

    title: str
    channel: str
    subscribers: int = 0
    views: int = 0
    age_days: int = 0
    views_per_day: float = 0.0
    views_per_sub: float = 0.0
    url: str = ""


class ChannelInsight(BaseModel):
    """The LLM's read on a channel: its niche, voice, and what it already does."""

    niche: str
    sub_niche: str = ""
    format: str = ""
    audience: str = ""
    tone: str = ""
    themes: list[str] = Field(default_factory=list)   # search-friendly content themes
    positioning: str = ""                              # what makes this channel distinct


class CompetitorChannel(BaseModel):
    """A channel that covers the same topics — discovered by topic overlap."""

    channel_id: str
    title: str
    subscribers: int = 0
    videos_seen: int = 0          # how many of the niche's topic searches it showed up in
    total_views: int = 0          # summed views of those appearances (scale signal)
    ceiling_views: int = 0        # their best video we saw (the bar)
    median_views: int = 0         # their typical performance
    floor_views: int = 0          # their weakest video we saw
    sample_titles: list[str] = Field(default_factory=list)
    url: str = ""
    style_match: float = 0.0      # 0-10 vision score: same FORMAT as the pasted channel?
    faceless: bool = True         # vision read: is this channel faceless like ours?


class ResearchBundle(BaseModel):
    """Everything the research phase gathered for one niche/topic."""

    niche: str
    keywords: list[str] = Field(default_factory=list)
    ceiling_floors: list[CeilingFloor] = Field(default_factory=list)
    reddit_pains: list[RedditPain] = Field(default_factory=list)
    comment_pains: list[CommentPain] = Field(default_factory=list)   # from YouTube comments
    transcripts: list[Transcript] = Field(default_factory=list)
    competitors: list[CompetitorChannel] = Field(default_factory=list)   # same-format rivals
    inspiration_channels: list[CompetitorChannel] = Field(default_factory=list)  # same topic, diff format
    format_twins: list[CompetitorChannel] = Field(default_factory=list)  # same FORMAT, different topic
    trending: list[TitlePattern] = Field(default_factory=list)   # recent niche breakouts
    breakouts: list[BreakoutVideo] = Field(default_factory=list)  # small-channel openings
    voice_profile: str = ""      # the channel's own narration voice (rule sheet)
    learnings: str = ""          # YOUR channel's audience-pattern rule sheet (analytics)
    research_notes: str = ""     # source coverage, limitations and research context; not own-channel analytics
    thumbnail_intel: str = ""    # winning-thumbnail rule sheet (vision over real niche thumbs)

    def top_patterns(self, limit: int = 30) -> list[TitlePattern]:
        """Highest-view title patterns across all keyword/window sweeps.

        A strong video is often the ceiling for several keywords, so de-dupe by
        URL (keeping the highest-view occurrence) before ranking.
        """
        best: dict[str, TitlePattern] = {}
        for cf in self.ceiling_floors:
            for p in cf.patterns:
                cur = best.get(p.url)
                if cur is None or p.views > cur.views:
                    best[p.url] = p
        return sorted(best.values(), key=lambda p: p.views, reverse=True)[:limit]


class NicheReport(BaseModel):
    """A vision-driven read on a whole niche: how tough the competition is, what
    formats and thumbnails win, and what a new channel could actually make.

    Produced by looking at the REAL thumbnails (paired with their titles + views)
    of the analyzed channel and its live-scraped competitors — so the verdict is
    grounded in what the eye sees, not just text stats."""

    niche: str = ""
    competition_level: str = ""          # "High" | "Medium" | "Low"
    opportunity_score: int = 0           # 0-100: how good an opening this niche is
    market_snapshot: str = ""            # scale + saturation in a couple of sentences
    competition_summary: str = ""        # who dominates and why they win
    dominant_formats: list[str] = Field(default_factory=list)   # formats that win here
    thumbnail_playbook: str = ""         # vision-derived winning-thumbnail rules
    title_patterns: list[str] = Field(default_factory=list)     # repeatable title shapes
    format_opportunities: list[str] = Field(default_factory=list)  # formats YOU could make
    content_gaps: list[str] = Field(default_factory=list)       # under-served angles
    recommended_angle: str = ""          # the single best way in
    verdict: str = ""                    # one-line go / caution call
    channels_analyzed: int = 0
    thumbnails_analyzed: int = 0
    sample_videos: list[TitlePattern] = Field(default_factory=list)  # what was looked at
