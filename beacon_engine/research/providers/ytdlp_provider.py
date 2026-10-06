"""yt-dlp adapter for metadata/search/channel extraction without media downloads."""

from __future__ import annotations

from typing import Any

from .base import ProviderUnavailable, ProviderVideo, normalize_video_id, yyyymmdd_to_iso


class YtDlpProvider:
    name = "yt-dlp"

    def __init__(self, *, cookies: str | None = None) -> None:
        try:
            from yt_dlp import YoutubeDL  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable("yt-dlp is not installed. Run: pip install yt-dlp") from exc
        self._YoutubeDL = YoutubeDL
        self._cookies = cookies

    def video(self, url_or_id: str) -> ProviderVideo | None:
        vid = normalize_video_id(url_or_id)
        url = f"https://www.youtube.com/watch?v={vid}" if vid else url_or_id
        with self._YoutubeDL(self._opts(extract_flat=False, noplaylist=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        return _video_from_info(info, source=self.name) if isinstance(info, dict) else None

    def search(self, query: str, *, limit: int = 25) -> list[ProviderVideo]:
        target = f"ytsearch{max(limit, 1)}:{query}"
        with self._YoutubeDL(self._opts(extract_flat=False, noplaylist=True)) as ydl:
            info = ydl.extract_info(target, download=False)
        entries = info.get("entries", []) if isinstance(info, dict) else []
        return [v for v in (_video_from_info(e, source=self.name) for e in entries) if v]

    def channel_videos(self, url_or_channel: str, *, limit: int = 200) -> list[ProviderVideo]:
        url = _channel_url(url_or_channel)
        opts = self._opts(extract_flat=True, noplaylist=False)
        opts["playlistend"] = max(limit, 1)
        with self._YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries", []) if isinstance(info, dict) else []
        out: list[ProviderVideo] = []
        for entry in entries[:limit]:
            video = _video_from_info(entry, source=self.name)
            if video:
                out.append(video)
        return out

    def _opts(self, *, extract_flat: bool | str, noplaylist: bool) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": extract_flat,
            "noplaylist": noplaylist,
            "ignoreerrors": True,
            "socket_timeout": 20,
        }
        if self._cookies:
            opts["cookiefile"] = self._cookies
        return opts


def _channel_url(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}/videos"
    if s.startswith("UC"):
        return f"https://www.youtube.com/channel/{s}/videos"
    return f"https://www.youtube.com/@{s.lstrip('@')}/videos"


def _video_from_info(info: Any, *, source: str) -> ProviderVideo | None:
    if not isinstance(info, dict):
        return None
    video_id = info.get("id") or normalize_video_id(info.get("url") or info.get("webpage_url"))
    if not video_id:
        return None
    title = info.get("title") or ""
    url = info.get("webpage_url") or info.get("original_url") or ""
    if not url or not str(url).startswith("http"):
        url = f"https://www.youtube.com/watch?v={video_id}"
    thumbs = info.get("thumbnails") or []
    thumb = ""
    if thumbs and isinstance(thumbs[-1], dict):
        thumb = thumbs[-1].get("url", "") or ""
    return ProviderVideo(
        video_id=str(video_id),
        title=str(title or ""),
        channel=str(info.get("channel") or info.get("uploader") or ""),
        channel_id=str(info.get("channel_id") or info.get("uploader_id") or ""),
        views=int(info.get("view_count") or 0),
        published_at=yyyymmdd_to_iso(str(info.get("upload_date") or "")),
        url=url,
        duration_seconds=int(info.get("duration") or 0),
        description=str(info.get("description") or ""),
        thumbnail_url=thumb,
        source=source,
        raw=info,
    )
