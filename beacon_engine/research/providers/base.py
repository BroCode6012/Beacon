"""Common provider data shapes and parsing helpers."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

from beacon_engine.research.models import VideoStat

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
_CHANNEL_ID_RE = re.compile(r"(UC[A-Za-z0-9_-]{20,})")
_INT_RE = re.compile(r"[\d,.]+")
_RELATIVE_DATE_RE = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
    re.I,
)
_COMPACT_RELATIVE_DATE_RE = re.compile(
    r"\b(\d+)\s*(sec|min|hr|d|w|wk|mo|mth|yr)s?\s+ago\b",
    re.I,
)


class ProviderError(RuntimeError):
    """A provider was reachable but could not satisfy this request."""


class ProviderUnavailable(ProviderError):
    """A provider dependency or public instance is not available."""


@dataclass(slots=True)
class ProviderVideo:
    video_id: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    views: int = 0
    published_at: str = ""
    url: str = ""
    duration_seconds: int = 0
    description: str = ""
    thumbnail_url: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.video_id = normalize_video_id(self.video_id) or self.video_id
        if not self.url and self.video_id:
            self.url = f"https://www.youtube.com/watch?v={self.video_id}"

    def to_stat(self) -> VideoStat:
        return VideoStat(
            video_id=self.video_id,
            title=self.title,
            channel=self.channel,
            channel_id=self.channel_id,
            views=max(int(self.views or 0), 0),
            published_at=self.published_at,
            url=self.url or f"https://www.youtube.com/watch?v={self.video_id}",
        )


@dataclass(slots=True)
class ProviderChannel:
    channel_id: str
    title: str = ""
    handle: str = ""
    subscribers: int = 0
    video_count: int = 0
    view_count: int = 0
    description: str = ""
    url: str = ""
    published_at: str = ""  # channel creation date (for "days since start")
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderComment:
    video_id: str
    text: str
    author: str = ""
    like_count: int = 0
    comment_id: str = ""
    published_at: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderTranscript:
    video_id: str
    text: str
    language: str = ""
    generated: bool | None = None
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_video_id(url_or_id: str | None) -> str:
    s = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    if m := _VIDEO_ID_RE.search(s):
        return m.group(1)
    return ""


def normalize_channel_id(url_or_id: str | None) -> str:
    s = (url_or_id or "").strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", s):
        return s
    if m := _CHANNEL_ID_RE.search(s):
        return m.group(1)
    return ""


def text_value(obj: Any) -> str:
    """Extract display text from common YouTube renderer shapes."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, list):
        return " ".join(text_value(x) for x in obj if text_value(x)).strip()
    if isinstance(obj, dict):
        if "simpleText" in obj:
            return text_value(obj.get("simpleText"))
        if "text" in obj and isinstance(obj.get("text"), str):
            return text_value(obj.get("text"))
        if "runs" in obj:
            return "".join(text_value(run.get("text")) for run in obj.get("runs", [])).strip()
        if "content" in obj:
            return text_value(obj.get("content"))
    return ""


def int_value(value: Any) -> int:
    """Parse view/subscriber strings like '1.2M views' or '4,200'."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = text_value(value).lower().replace(",", "").strip()
    if not text:
        return 0
    mult = 1
    if "b" in text:
        mult = 1_000_000_000
    elif "m" in text:
        mult = 1_000_000
    elif "k" in text:
        mult = 1_000
    m = _INT_RE.search(text)
    if not m:
        return 0
    try:
        return int(float(m.group(0)) * mult)
    except ValueError:
        return 0


def duration_seconds(value: Any) -> int:
    text = text_value(value)
    if not text:
        return 0
    parts = [p for p in text.split(":") if p.strip().isdigit()]
    if not parts:
        return 0
    nums = [int(p) for p in parts]
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def yyyymmdd_to_iso(value: str | None) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        try:
            return _dt.datetime.strptime(s, "%Y%m%d").replace(
                tzinfo=_dt.timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except ValueError:
            return ""
    return s


def normalize_published_at(
    value: str | None,
    *,
    reference: str | _dt.datetime | None = None,
) -> str:
    """Convert provider publication labels into a sortable UTC timestamp."""
    text = (value or "").strip()
    if not text:
        return ""

    anchor = _coerce_datetime(reference) or _dt.datetime.now(_dt.timezone.utc)
    match = _RELATIVE_DATE_RE.search(text)
    if not match:
        match = _COMPACT_RELATIVE_DATE_RE.search(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("second") or unit == "sec":
            delta = _dt.timedelta(seconds=amount)
        elif unit.startswith("minute") or unit == "min":
            delta = _dt.timedelta(minutes=amount)
        elif unit.startswith("hour") or unit == "hr":
            delta = _dt.timedelta(hours=amount)
        elif unit.startswith("day") or unit == "d":
            delta = _dt.timedelta(days=amount)
        elif unit.startswith("week") or unit in {"w", "wk"}:
            delta = _dt.timedelta(weeks=amount)
        elif unit.startswith("month") or unit in {"mo", "mth"}:
            delta = _dt.timedelta(days=amount * 30)
        else:
            delta = _dt.timedelta(days=amount * 365)
        return _iso_utc(anchor - delta)

    if re.fullmatch(r"\d{8}", text):
        try:
            return _iso_utc(_dt.datetime.strptime(text, "%Y%m%d"))
        except ValueError:
            return ""

    parsed = _coerce_datetime(text)
    return _iso_utc(parsed) if parsed else ""


def _coerce_datetime(value: str | _dt.datetime | None) -> _dt.datetime | None:
    if isinstance(value, _dt.datetime):
        parsed = value
    else:
        text = (value or "").strip()
        if not text:
            return None
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = _dt.datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _iso_utc(value: _dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
