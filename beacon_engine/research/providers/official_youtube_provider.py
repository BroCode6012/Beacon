"""Official YouTube Data API provider used as an optional fallback."""

from __future__ import annotations

from googleapiclient.discovery import build

from beacon_engine.config import settings
from beacon_engine.research.providers.base import (
    ProviderChannel,
    ProviderUnavailable,
    ProviderVideo,
    duration_seconds,
    normalize_channel_id,
    normalize_video_id,
)

_DUR = __import__("re").compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class OfficialYouTubeProvider:
    name = "official-youtube"

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.youtube_api_key
        if not key:
            raise ProviderUnavailable("YOUTUBE_API_KEY is not set")
        self._yt = build("youtube", "v3", developerKey=key, cache_discovery=False)

    def search(self, query: str, *, limit: int = 50) -> list[ProviderVideo]:
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            req = dict(
                part="id",
                q=query,
                type="video",
                order="viewCount",
                maxResults=min(50, limit - len(ids)),
            )
            if page_token:
                req["pageToken"] = page_token
            resp = self._yt.search().list(**req).execute()
            ids.extend(
                item["id"]["videoId"]
                for item in resp.get("items", [])
                if item.get("id", {}).get("videoId")
            )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return self.videos(ids)

    def videos(self, ids: list[str]) -> list[ProviderVideo]:
        clean = list(dict.fromkeys(normalize_video_id(v) or v for v in ids if v))
        out: list[ProviderVideo] = []
        for i in range(0, len(clean), 50):
            chunk = clean[i:i + 50]
            resp = self._yt.videos().list(
                part="snippet,statistics,contentDetails",
                id=",".join(chunk),
            ).execute()
            out.extend(_video_from_item(item) for item in resp.get("items", []))
        return out

    def video(self, url_or_id: str) -> ProviderVideo | None:
        vids = self.videos([url_or_id])
        return vids[0] if vids else None

    def channels(self, ids: list[str]) -> list[ProviderChannel]:
        """Batch channel stats (subscribers, totals). 50 channels = 1 quota unit."""
        clean = list(dict.fromkeys(
            normalize_channel_id(c) or c.strip() for c in ids if c and c.strip()
        ))
        out: list[ProviderChannel] = []
        for i in range(0, len(clean), 50):
            chunk = clean[i:i + 50]
            resp = self._yt.channels().list(
                part="snippet,statistics",
                id=",".join(chunk),
                maxResults=50,
            ).execute()
            out.extend(_channel_from_item(item) for item in resp.get("items", []))
        return out

    def channel_videos(self, channel: str, *, limit: int = 200) -> list[ProviderVideo]:
        channel_id = normalize_channel_id(channel) or channel.strip()
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            req = dict(
                part="id",
                channelId=channel_id,
                type="video",
                order="date",
                maxResults=min(50, limit - len(ids)),
            )
            if page_token:
                req["pageToken"] = page_token
            resp = self._yt.search().list(**req).execute()
            ids.extend(
                item["id"]["videoId"]
                for item in resp.get("items", [])
                if item.get("id", {}).get("videoId")
            )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return self.videos(ids)


def _video_from_item(item: dict) -> ProviderVideo:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    cd = item.get("contentDetails", {})
    thumbs = sn.get("thumbnails", {})
    thumb = ""
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs:
            thumb = thumbs[key].get("url", "")
            break
    return ProviderVideo(
        video_id=item.get("id", ""),
        title=sn.get("title", ""),
        channel=sn.get("channelTitle", ""),
        channel_id=sn.get("channelId", ""),
        views=int(st.get("viewCount", 0) or 0),
        published_at=sn.get("publishedAt", ""),
        duration_seconds=_seconds(cd.get("duration", "")),
        description=sn.get("description", ""),
        thumbnail_url=thumb,
        source="official-youtube",
        raw=item,
    )


def _channel_from_item(item: dict) -> ProviderChannel:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    channel_id = item.get("id", "")
    hidden = bool(st.get("hiddenSubscriberCount"))
    return ProviderChannel(
        channel_id=channel_id,
        title=sn.get("title", ""),
        handle=(sn.get("customUrl", "") or "").lstrip("@"),
        subscribers=0 if hidden else int(st.get("subscriberCount", 0) or 0),
        video_count=int(st.get("videoCount", 0) or 0),
        view_count=int(st.get("viewCount", 0) or 0),
        description=sn.get("description", ""),
        url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
        published_at=sn.get("publishedAt", ""),
        source="official-youtube",
        raw=item,
    )


def _seconds(iso: str) -> int:
    match = _DUR.fullmatch(iso or "")
    if not match:
        return duration_seconds(iso)
    h, m, s = (int(x) if x else 0 for x in match.groups())
    return h * 3600 + m * 60 + s
