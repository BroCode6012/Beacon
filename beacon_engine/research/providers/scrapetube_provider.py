"""scrapetube adapter: fast ID discovery without YouTube Data API quota."""

from __future__ import annotations

from itertools import islice
from typing import Any, Iterable

import os

from .base import (
    ProviderUnavailable,
    ProviderVideo,
    duration_seconds,
    int_value,
    normalize_channel_id,
    text_value,
)

# scrapetube builds a plain requests.Session and never passes `timeout=`, and
# requests with no timeout blocks on the socket FOREVER. On 2026-07-20 that hung
# the Oracle edge collector: YouTube dropped several connections mid-read, the
# worker threads parked in recv() with nothing raised, and the whole discovery
# pipeline deadlocked silently for 17 days. Give every session scrapetube makes
# a (connect, read) timeout so a stall raises a retryable error instead.
_CONNECT_TIMEOUT = float(os.getenv("SCRAPETUBE_CONNECT_TIMEOUT", "10") or 10)
_READ_TIMEOUT = float(os.getenv("SCRAPETUBE_READ_TIMEOUT", "30") or 30)


def _install_session_timeout(scrapetube_module: Any) -> None:
    """Make scrapetube's sessions time out. Idempotent and dependency-free."""
    inner = getattr(scrapetube_module, "scrapetube", scrapetube_module)
    original = getattr(inner, "get_session", None)
    if original is None or getattr(original, "_signal_timeout_patched", False):
        return

    def get_session(*args: Any, **kwargs: Any):
        session = original(*args, **kwargs)
        request = session.request

        def timed_request(method: Any, url: Any, **kw: Any):
            kw.setdefault("timeout", (_CONNECT_TIMEOUT, _READ_TIMEOUT))
            return request(method, url, **kw)

        # Instance-level override: session.get/post route through self.request,
        # so this covers every call without touching other requests users.
        session.request = timed_request
        return session

    get_session._signal_timeout_patched = True  # type: ignore[attr-defined]
    inner.get_session = get_session


class ScrapetubeProvider:
    name = "scrapetube"

    def __init__(self) -> None:
        try:
            import scrapetube  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(
                "scrapetube is not installed. Run: pip install scrapetube"
            ) from exc
        _install_session_timeout(scrapetube)
        self._scrapetube = scrapetube

    def search(self, query: str, *, limit: int = 50,
               sort: str | None = None) -> list[ProviderVideo]:
        # sort="newest"/"upload_date" asks scrapetube for upload-date order so a
        # re-searched query surfaces channels that posted since the last sweep,
        # instead of re-returning the same all-time top results. Default (None)
        # keeps scrapetube's normal relevance order (unchanged behavior).
        kwargs: dict = {}
        if sort in ("newest", "upload_date", "date"):
            kwargs["sort_by"] = "upload_date"
        return self._collect(self._scrapetube.get_search(query, **kwargs), limit)

    def channel_videos(self, channel: str, *, limit: int = 200) -> list[ProviderVideo]:
        channel_id = normalize_channel_id(channel) or channel.strip()
        return self._collect(self._scrapetube.get_channel(channel_id), limit)

    def playlist_videos(self, playlist_id: str, *, limit: int = 200) -> list[ProviderVideo]:
        return self._collect(self._scrapetube.get_playlist(playlist_id), limit)

    def _collect(self, items: Iterable[dict[str, Any]], limit: int) -> list[ProviderVideo]:
        out: list[ProviderVideo] = []
        for item in islice(items, max(limit, 0)):
            video = _video_from_renderer(item)
            if video:
                out.append(video)
        return out


def _video_from_renderer(item: dict[str, Any]) -> ProviderVideo | None:
    video_id = item.get("videoId") or item.get("id")
    if not video_id:
        return None
    title = text_value(item.get("title"))
    owner = item.get("ownerText") or item.get("shortBylineText") or item.get("longBylineText")
    channel = text_value(owner)
    channel_id = ""
    if isinstance(owner, dict):
        runs = owner.get("runs") or []
        if runs and isinstance(runs[0], dict):
            browse = (
                runs[0]
                .get("navigationEndpoint", {})
                .get("browseEndpoint", {})
            )
            channel_id = browse.get("browseId", "") or ""
    views = int_value(item.get("viewCountText") or item.get("shortViewCountText"))
    thumb = _thumbnail(item.get("thumbnail"))
    return ProviderVideo(
        video_id=str(video_id),
        title=title,
        channel=channel,
        channel_id=channel_id,
        views=views,
        published_at=text_value(item.get("publishedTimeText")),
        duration_seconds=duration_seconds(item.get("lengthText")),
        thumbnail_url=thumb,
        source="scrapetube",
        raw=item,
    )


def _thumbnail(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    thumbs = obj.get("thumbnails") or []
    if not thumbs:
        return ""
    last = thumbs[-1]
    return last.get("url", "") if isinstance(last, dict) else ""
