"""Comment provider backed by youtube-comment-downloader."""

from __future__ import annotations

from itertools import islice
from typing import Any

from .base import ProviderComment, ProviderUnavailable, int_value, normalize_video_id


class CommentDownloaderProvider:
    name = "youtube-comment-downloader"

    def __init__(self) -> None:
        try:
            from youtube_comment_downloader import (  # type: ignore
                SORT_BY_POPULAR,
                SORT_BY_RECENT,
                YoutubeCommentDownloader,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(
                "youtube-comment-downloader is not installed. "
                "Run: pip install youtube-comment-downloader"
            ) from exc
        self._downloader = YoutubeCommentDownloader()
        self._popular = SORT_BY_POPULAR
        self._recent = SORT_BY_RECENT

    def comments(
        self, url_or_id: str, *, limit: int = 100, sort: str = "popular"
    ) -> list[ProviderComment]:
        vid = normalize_video_id(url_or_id)
        if not vid:
            return []
        url = f"https://www.youtube.com/watch?v={vid}"
        sort_by = self._popular if sort == "popular" else self._recent
        items = self._downloader.get_comments_from_url(url, sort_by=sort_by)
        out: list[ProviderComment] = []
        for item in islice(items, max(limit, 0)):
            comment = _comment_from_item(vid, item)
            if comment:
                out.append(comment)
        return out


def _comment_from_item(video_id: str, item: dict[str, Any]) -> ProviderComment | None:
    text = item.get("text") or item.get("comment") or ""
    if not str(text).strip():
        return None
    return ProviderComment(
        video_id=video_id,
        text=str(text).strip(),
        author=str(item.get("author") or ""),
        like_count=int_value(item.get("votes") or item.get("like_count")),
        comment_id=str(item.get("cid") or item.get("comment_id") or ""),
        published_at=str(item.get("time") or ""),
        source="youtube-comment-downloader",
        raw=item,
    )
