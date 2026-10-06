"""Invidious public API adapter."""

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


class InvidiousProvider:
    name = "invidious"

    def __init__(self, base_url: str = "https://yewtu.be") -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "faceless-beacon_engine/0.1"})

    def search(self, query: str, *, limit: int = 50) -> list[ProviderVideo]:
        data = self._get("/api/v1/search", params={"q": query, "type": "video"})
        if not isinstance(data, list):
            return []
        out = [_video_from_search(x) for x in data if isinstance(x, dict)]
        return [v for v in out if v][:limit]

    def video(self, url_or_id: str) -> ProviderVideo | None:
        vid = normalize_video_id(url_or_id)
        if not vid:
            return None
        data = self._get(f"/api/v1/videos/{vid}")
        return _video_from_video(vid, data) if isinstance(data, dict) else None

    def channel_videos(self, channel: str, *, limit: int = 200) -> list[ProviderVideo]:
        channel_id = normalize_channel_id(channel) or channel.strip()
        data = self._get(f"/api/v1/channels/{channel_id}/videos")
        videos = data.get("videos") if isinstance(data, dict) else data
        if not isinstance(videos, list):
            return []
        return [v for v in (_video_from_search(x) for x in videos[:limit]) if v]

    def comments(self, url_or_id: str, *, limit: int = 100) -> list[ProviderComment]:
        vid = normalize_video_id(url_or_id)
        if not vid:
            return []
        data = self._get(f"/api/v1/comments/{vid}")
        comments = data.get("comments") if isinstance(data, dict) else []
        out: list[ProviderComment] = []
        for item in comments or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or item.get("contentHtml") or "").strip()
            if not text:
                continue
            out.append(ProviderComment(
                video_id=vid,
                text=text,
                author=str(item.get("author") or ""),
                like_count=int_value(item.get("likeCount")),
                comment_id=str(item.get("commentId") or ""),
                published_at=str(item.get("publishedText") or ""),
                source=self.name,
                raw=item,
            ))
        return out[:limit]

    def _get(self, path: str, params: dict | None = None) -> object:
        try:
            resp = self.session.get(self.base_url + path, params=params, timeout=20)
            if resp.status_code >= 500:
                raise ProviderUnavailable(f"Invidious instance returned {resp.status_code}")
            if resp.status_code >= 400:
                return {}
            return resp.json()
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"Invidious instance unavailable: {exc}") from exc


def _video_from_search(item: dict) -> ProviderVideo | None:
    vid = item.get("videoId") or normalize_video_id(str(item.get("url") or ""))
    if not vid:
        return None
    return ProviderVideo(
        video_id=str(vid),
        title=str(item.get("title") or ""),
        channel=str(item.get("author") or ""),
        channel_id=str(item.get("authorId") or ""),
        views=int_value(item.get("viewCount")),
        published_at=str(item.get("publishedText") or item.get("published") or ""),
        duration_seconds=int_value(item.get("lengthSeconds")),
        thumbnail_url=_thumb(item),
        source="invidious",
        raw=item,
    )


def _video_from_video(video_id: str, item: dict) -> ProviderVideo:
    return ProviderVideo(
        video_id=video_id,
        title=str(item.get("title") or ""),
        channel=str(item.get("author") or ""),
        channel_id=str(item.get("authorId") or ""),
        views=int_value(item.get("viewCount")),
        published_at=str(item.get("publishedText") or item.get("published") or ""),
        duration_seconds=int_value(item.get("lengthSeconds")),
        description=str(item.get("description") or ""),
        thumbnail_url=_thumb(item),
        source="invidious",
        raw=item,
    )


def _thumb(item: dict) -> str:
    thumbs = item.get("videoThumbnails") or []
    if thumbs and isinstance(thumbs[-1], dict):
        return str(thumbs[-1].get("url") or "")
    return ""
