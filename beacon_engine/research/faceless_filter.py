"""Cheap faceless-channel signals for crawler prefiltering.

This is intentionally conservative. Text alone cannot prove a channel is faceless,
but it can avoid wasting channel crawls on obvious wrong formats before the vision
classifier makes the real call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from beacon_engine.research.providers import ProviderVideo

CLASSIFIER_VERSION = "text-v3"


@dataclass(frozen=True)
class FacelessTextScore:
    score: float
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    sample_count: int = 0
    positive_title_count: int = 0
    negative_title_count: int = 0
    confidence: float = 0.0

    @property
    def likely_faceless(self) -> bool:
        return self.matches("balanced")

    def matches(self, mode: str = "strict") -> bool:
        mode = (mode or "strict").strip().lower()
        hard_negative = any(term in _HARD_NEGATIVE for term in self.negatives)
        if hard_negative:
            return False
        # Evidence floor: a channel cannot be called faceless from one or two
        # stray titles a broad search happened to surface. Demand a real sample
        # AND more than one on-format title before trusting the label. This is
        # what stops mega-brands (WWE, TEDx, news) sneaking in on a single hit.
        if self.sample_count < 3 or self.positive_title_count < 2:
            return False
        if mode == "loose":
            return self.score >= 0.5 and self.confidence >= 0.5
        if mode == "balanced":
            return self.score >= 1.0 and self.confidence >= 0.58
        return (
            self.sample_count >= 3
            and self.score >= 2.5
            and self.confidence >= 0.68
            and self.negative_title_count == 0
        )


_POSITIVE = {
    "explained": 1.2,
    "documentary": 1.5,
    "case study": 1.4,
    "history": 0.9,
    "mystery": 0.8,
    "facts": 0.8,
    "animated": 1.2,
    "story": 0.6,
    "stories": 0.6,
    "timeline": 0.9,
    "breakdown": 0.9,
    "analysis": 0.7,
    "truth about": 1.4,
    "dark side": 1.2,
    "rise and fall": 1.5,
    "what happened": 1.1,
    "why": 0.4,
    "how": 0.4,
    "top ": 0.5,
    "exposed": 1.2,
    "investigation": 1.2,
    "investigates": 1.0,
    "revealed": 1.0,
    "unsolved": 1.0,
    "forbidden": 0.8,
    "hidden": 0.7,
    "scam": 0.7,
    "brands": 0.4,
    "industry": 0.4,
    "rise of": 0.8,
    "fall of": 0.8,
}

_NEGATIVE = {
    "123 go": 5.0,
    "5-minute crafts": 5.0,
    "crafty panda": 5.0,
    "sara beauty": 5.0,
    "whatsupmoms": 5.0,
    "frontline pbs": 5.0,
    "business insider": 4.0,
    "netflix": 4.0,
    "history channel": 4.0,
    "dw documentary": 4.0,
    "wall street journal": 4.0,
    "free documentary": 4.0,
    "mrballen": 4.0,
    "wttw": 4.0,
    "diy": 2.2,
    "craft": 2.2,
    "crafts": 2.2,
    "life hack": 2.2,
    "life hacks": 2.2,
    "hack": 1.8,
    "hacks": 1.8,
    "makeup": 2.6,
    "beauty": 2.6,
    "nail art": 2.6,
    "mom hacks": 3.0,
    "mom": 2.2,
    "school hacks": 3.0,
    "school supplies": 3.0,
    "funny tricks": 2.2,
    "comedy": 2.0,
    "prank": 2.2,
    "challenge": 2.0,
    "official": 1.5,
    "pbs": 2.4,
    "frontline": 2.4,
    "news": 1.8,
    "podcast": 3.0,
    "interview": 2.5,
    "live": 2.2,
    "stream": 2.2,
    "vlog": 2.5,
    "reaction": 2.2,
    "reacts": 2.2,
    "talk show": 2.4,
    "press conference": 2.8,
    "lecture": 2.0,
    "classroom": 1.8,
    "sermon": 2.4,
    "trailer": 2.0,
    "official music": 2.8,
    "music video": 2.8,
    "behind the scenes": 1.8,
    "day in my life": 2.8,
    "q&a": 2.0,
}

_HARD_NEGATIVE = {
    "history",
    "123 go",
    "5-minute crafts",
    "crafty panda",
    "sara beauty",
    "whatsupmoms",
    "frontline pbs",
    "business insider",
    "netflix",
    "history channel",
    "dw documentary",
    "wall street journal",
    "free documentary",
    "mrballen",
    "wttw",
    "diy",
    "craft",
    "crafts",
    "makeup",
    "beauty",
    "nail art",
    "mom hacks",
    "school hacks",
    "school supplies",
    "podcast",
    "interview",
    "vlog",
    "reaction",
    "music video",
    # Brand/network/format markers in a channel title -> not a faceless channel.
    "vevo",
    "- topic",
    "tedx",
    "ted-ed",
    "ted talks",
    "wwe",
    "espn",
    "wrestling",
    "official artist",
}

_CHANNEL_BLOCKLIST = {
    "netflix",
    "history channel",
    "dw documentary",
    "the wall street journal",
    "wall street journal",
    "frontline pbs",
    "frontline",
    "business insider",
    "free documentary",
    "wttw",
    "mrballen",
    "crafty panda",
    "123 go",
    "5-minute crafts",
    "sara beauty",
    "whatsupmoms",
    # Mega-brands / networks / auto-generated channels that broad searches drag in.
    "vevo",
    "- topic",
    "tedx",
    "ted-ed",
    "ted talks",
    "wwe",
    "espn",
    "wrestling",
    "official artist",
}

_EXACT_CHANNEL_BLOCKLIST = {
    "history",
}


def score_channel_text(
    videos: Iterable[ProviderVideo],
    *,
    channel_title: str = "",
) -> FacelessTextScore:
    items = list(videos)[:20]
    text = " ".join(
        [channel_title]
        + [v.channel for v in items[:8]]
        + [v.title for v in items]
    ).lower()
    channel_text = (channel_title or "").lower()
    positives = [term for term in _POSITIVE if term in text]
    negatives = [term for term in _NEGATIVE if term in text]
    channel_blocks = [term for term in _CHANNEL_BLOCKLIST if term in channel_text]
    if channel_text.strip() in _EXACT_CHANNEL_BLOCKLIST:
        channel_blocks.append(channel_text.strip())
    negatives.extend(channel_blocks)
    positive_title_count = 0
    negative_title_count = 0
    for video in items:
        title = (video.title or "").lower()
        positive_weight = sum(weight for term, weight in _POSITIVE.items() if term in title)
        negative_weight = sum(weight for term, weight in _NEGATIVE.items() if term in title)
        if positive_weight - negative_weight >= 0.6:
            positive_title_count += 1
        if negative_weight - positive_weight >= 1.5:
            negative_title_count += 1
    sample_count = len([item for item in items if (item.title or "").strip()])
    score = sum(_POSITIVE[t] for t in positives) - sum(
        _NEGATIVE.get(t, 0.0) for t in negatives
    )
    score += positive_title_count * 0.75
    score -= negative_title_count * 1.25
    score -= 5.0 * len(channel_blocks)
    denominator = max(sample_count, 1)
    evidence_ratio = (positive_title_count - negative_title_count) / denominator
    confidence = max(0.0, min(1.0, 0.5 + score / 12.0 + evidence_ratio * 0.3))
    return FacelessTextScore(
        score=round(score, 3),
        positives=sorted(set(positives)),
        negatives=sorted(set(negatives)),
        sample_count=sample_count,
        positive_title_count=positive_title_count,
        negative_title_count=negative_title_count,
        confidence=round(confidence, 3),
    )


def looks_faceless_by_text(
    videos: Iterable[ProviderVideo],
    *,
    channel_title: str = "",
    mode: str = "balanced",
) -> bool:
    signal = score_channel_text(videos, channel_title=channel_title)
    return signal.matches(mode)


def load_store_classifications(store) -> dict[str, FacelessTextScore]:
    """Read already-persisted per-channel text scores (a cheap indexed read of
    ~30K rows) instead of re-scoring 400K video titles on every page load. The
    radar keeps these fresh each cycle; the UI only needs to READ them."""
    import json as _json
    out: dict[str, FacelessTextScore] = {}
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT channel_id, score, confidence, sample_count,
                   positive_title_count, negative_title_count,
                   positives_json, negatives_json
            FROM channel_classifications
            """
        ).fetchall()
    for r in rows:
        cid = r["channel_id"]
        if not cid:
            continue
        try:
            positives = _json.loads(r["positives_json"] or "[]")
            negatives = _json.loads(r["negatives_json"] or "[]")
        except Exception:  # noqa: BLE001
            positives, negatives = [], []
        out[cid] = FacelessTextScore(
            score=float(r["score"] or 0),
            positives=positives, negatives=negatives,
            sample_count=int(r["sample_count"] or 0),
            positive_title_count=int(r["positive_title_count"] or 0),
            negative_title_count=int(r["negative_title_count"] or 0),
            confidence=float(r["confidence"] or 0),
        )
    return out


def classify_store_channels(
    store,
    *,
    mode: str = "strict",
    persist: bool = True,
    only_missing: bool = False,
) -> dict[str, FacelessTextScore]:
    """Classify complete indexed channels from multiple stored video titles."""
    missing_join = ""
    missing_where = ""
    if only_missing:
        missing_join = (
            "LEFT JOIN channel_classifications cc "
            "ON cc.channel_id = v.channel_id"
        )
        missing_where = "AND cc.channel_id IS NULL"
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                v.video_id, v.title, v.channel, v.channel_id, v.views,
                v.published_at, v.url
            FROM videos v
            {missing_join}
            WHERE v.channel_id != ''
              {missing_where}
            ORDER BY v.channel_id, v.views DESC, v.last_seen_at DESC
            """
        ).fetchall()
    grouped: dict[str, list[ProviderVideo]] = {}
    titles: dict[str, str] = {}
    for row in rows:
        channel_id = row["channel_id"] or ""
        if not channel_id:
            continue
        titles.setdefault(channel_id, row["channel"] or channel_id)
        grouped.setdefault(channel_id, []).append(ProviderVideo(
            video_id=row["video_id"] or "faceless000",
            title=row["title"] or "",
            channel=row["channel"] or "",
            channel_id=channel_id,
            views=int(row["views"] or 0),
            published_at=row["published_at"] or "",
            url=row["url"] or "",
        ))
    signals = {
        channel_id: score_channel_text(videos, channel_title=titles[channel_id])
        for channel_id, videos in grouped.items()
    }
    if persist:
        store.save_channel_classifications(signals, mode=mode)
    return signals
