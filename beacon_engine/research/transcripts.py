"""Fetch competitor video transcripts to use as writing training examples.

The reference method pulls competitor transcripts (their de-facto scripts) and feeds
them to the model as few-shot examples so generated scripts match a proven voice and
structure. We use `youtube-transcript-api` (no key required) rather than scraping.

The library's surface changed between 0.6.x (classmethod `get_transcript`) and 1.x
(instance `.fetch`), so we support both and treat any failure as "unavailable".
"""

from __future__ import annotations

import re

from youtube_transcript_api import YouTubeTranscriptApi

from beacon_engine.research.models import Transcript, VideoStat

_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def parse_video_id(url_or_id: str) -> str | None:
    """Accept a full URL or a bare 11-char video id."""
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    m = _ID_RE.search(url_or_id)
    return m.group(1) if m else None


def _raw_segments(vid: str, languages: list[str]) -> list[str]:
    """Return segment texts, handling both 0.6.x and 1.x library APIs."""
    # 1.x instance API
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(vid, languages=languages)
        return [getattr(seg, "text", "") for seg in fetched]
    except (AttributeError, TypeError):
        pass
    # 0.6.x classmethod API
    segments = YouTubeTranscriptApi.get_transcript(vid, languages=languages)
    return [seg.get("text", "") for seg in segments]


def fetch_transcript(
    url_or_id: str, *, title: str = "", languages: tuple[str, ...] = ("en",)
) -> Transcript | None:
    """Fetch one transcript; returns None if unavailable (disabled/none/private)."""
    vid = parse_video_id(url_or_id)
    if not vid:
        return None
    try:
        texts = _raw_segments(vid, list(languages))
    except Exception:
        # Disabled, no transcript, private/removed, network — all non-fatal.
        return None

    text = _clean(" ".join(texts))
    if not text:
        return None
    return Transcript(
        video_id=vid,
        title=title,
        url=f"https://www.youtube.com/watch?v={vid}",
        text=text,
    )


def fetch_for_videos(
    videos: list[VideoStat], *, limit: int = 4, max_scan: int = 25
) -> list[Transcript]:
    """Collect up to `limit` usable transcripts, scanning down the ranked list.

    Many top videos are Shorts or have captions disabled, so we keep scanning
    (up to `max_scan` candidates) until we've gathered `limit` real transcripts
    rather than giving up after the first few.
    """
    out: list[Transcript] = []
    if limit <= 0:
        return out
    for v in videos[:max_scan]:
        if len(out) >= limit:
            break
        t = fetch_transcript(v.video_id, title=v.title)
        if t and t.word_count > 100:
            out.append(t)
    return out


def _clean(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", text)  # drop [Music], [Applause], etc.
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def style_excerpt(text: str, *, max_words: int = 700) -> str:
    """Bounded opening/middle/ending samples, so a prefix cannot masquerade as a script.

    Gaps are labeled: these excerpts preserve the close and midpoint without stitching
    unrelated sentences into a false continuous passage.
    """
    words = (text or "").split()
    if max_words <= 0:
        return ""
    if len(words) <= max_words:
        return " ".join(words)
    opening = max(1, max_words * 2 // 5)
    middle = max_words // 3
    closing = max_words - opening - middle
    start = max(opening, (len(words) - middle) // 2)
    return ("[OPENING SAMPLE]\n" + " ".join(words[:opening])
            + "\n[... OMITTED ...]\n[MIDDLE SAMPLE]\n"
            + " ".join(words[start:start + middle])
            + "\n[... OMITTED ...]\n[ENDING SAMPLE]\n"
            + (" ".join(words[-closing:]) if closing else ""))
