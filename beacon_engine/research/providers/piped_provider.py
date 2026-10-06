"""Piped public API adapter.

Public Piped instances are useful fallbacks, not a guaranteed backbone. This
adapter keeps timeouts short and returns empty results on missing instance data.
"""

from __future__ import annotations

import requests

from .base import (
    ProviderComment,
    ProviderUnavailable,
    ProviderVideo,
    int_value,
    normalize_channel_id,
    normalize_video_id,
)


class PipedProvider:
    name = "piped"

    def __init__(self, base_url: str = "https://pipedapi.kavin.rocks") -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "faceless-beacon_engine/0.1"})

    def video(self, url_or_id: str) -> ProviderVideo | None:
        vid = normalize_video_id(url_or_id)
        if not vid:
            return None
        data = self._get(f"/streams/{vid}")
        return _video_from_streams(vid, data) if isinstance(data, dict) else None

    def trending(self, *, region: str = "US", limit: int = 50) -> list[ProviderVideo]:
        data = self._get("/trending", params={"region": region})
        if not isinstance(data, list):
            return []
        return [v for v in (_video_from_related(x) for x in data[:limit]) if v]

    def channel_videos(self, channel: str, *, limit: int = 200) -> list[ProviderVideo]:
        channel_id = normalize_channel_id(channel) or channel.strip()
        data = self._get(f"/channel/{channel_id}")
        return self._paged_streams(channel_id, data, limit)

    def comments(self, url_or_id: str, *, limit: int = 100) -> list[ProviderComment]:
        vid = normalize_video_id(url_or_id)
        if not vid:
            return []
        data = self._get(f"/comments/{vid}")
        out = _comments_from_page(vid, data)
        nextpage = data.get("nextpage") if isinstance(data, dict) else ""
        while nextpage and len(out) < limit:
            data = self._get(f"/nextpage/comments/{vid}", params={"nextpage": nextpage})
            out.extend(_comments_from_page(vid, data))
            nextpage = data.get("nextpage") if isinstance(data, dict) else ""
        return out[:limit]

    def _paged_streams(
        self, channel_id: str, data: object, limit: int
    ) -> list[ProviderVideo]:
        out = _streams_from_page(data)
        nextpage = data.get("nextpage") if isinstance(data, dict) else ""
        while nextpage and len(out) < limit:
            data = self._get(f"/nextpage/channel/{channel_id}", params={"nextpage": nextpage})
            out.extend(_streams_from_page(data))
            nextpage = data.get("nextpage") if isinstance(data, dict) else ""
        return out[:limit]

    def _get(self, path: str, params: dict | None = None) -> object:
        try:
            resp = self.session.get(self.base_url + path, params=params, timeout=20)
            if resp.status_code >= 500:
                raise ProviderUnavailable(f"Piped instance returned {resp.status_code}")
            if resp.status_code >= 400:
                return {}
            return resp.json()
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"Piped instance unavailable: {exc}") from exc


def _video_from_streams(video_id: str, data: dict) -> ProviderVideo:
    return ProviderVideo(
        video_id=video_id,
        title=str(data.get("title") or ""),
        channel=str(data.get("uploader") or ""),
        channel_id=normalize_channel_id(str(data.get("uploaderUrl") or "")),
        views=int_value(data.get("views")),
        published_at=str(data.get("uploadDate") or ""),
        duration_seconds=int_value(data.get("duration")),
        description=str(data.get("description") or ""),
        thumbnail_url=str(data.get("thumbnailUrl") or ""),
        source="piped",
        raw=data,
    )


def _video_from_related(item: dict) -> ProviderVideo | None:
    url = str(item.get("url") or "")
    vid = normalize_video_id(url)
    if not vid:
        return None
    return ProviderVideo(
        video_id=vid,
        title=str(item.get("title") or ""),
        channel=str(item.get("uploaderName") or item.get("uploader") or ""),
        channel_id=normalize_channel_id(str(item.get("uploaderUrl") or "")),
        views=int_value(item.get("views")),
        published_at=str(item.get("uploadedDate") or ""),
        duration_seconds=int_value(item.get("duration")),
        thumbnail_url=str(item.get("thumbnail") or ""),
        source="piped",
        raw=item,
    )


def _streams_from_page(data: object) -> list[ProviderVideo]:
    if not isinstance(data, dict):
        return []
    streams = data.get("relatedStreams") or []
    return [v for v in (_video_from_related(x) for x in streams if isinstance(x, dict)) if v]


def _comments_from_page(video_id: str, data: object) -> list[ProviderComment]:
    if not isinstance(data, dict):
        return []
    out: list[ProviderComment] = []
    for item in data.get("comments") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("commentText") or "").strip()
        if not text:
            continue
        out.append(ProviderComment(
            video_id=video_id,
            text=text,
            author=str(item.get("author") or ""),
            like_count=int_value(item.get("likeCount")),
            comment_id=str(item.get("commentId") or ""),
            published_at=str(item.get("commentedTime") or ""),
            source="piped",
            raw=item,
        ))
    return out
