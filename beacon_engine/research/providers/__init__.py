"""Free/low-quota YouTube data providers used by the crawler.

The existing research pipeline is API-first. These providers are intentionally
small adapters around tools such as yt-dlp, scrapetube, Piped/Invidious, and the
current transcript/comment helpers so the crawler can mix sources without making
the rest of the app care where a record came from.
"""

from .base import (
    ProviderChannel,
    ProviderComment,
    ProviderError,
    ProviderTranscript,
    ProviderUnavailable,
    ProviderVideo,
    normalize_channel_id,
    normalize_video_id,
)

__all__ = [
    "ProviderChannel",
    "ProviderComment",
    "ProviderError",
    "ProviderTranscript",
    "ProviderUnavailable",
    "ProviderVideo",
    "normalize_channel_id",
    "normalize_video_id",
]
