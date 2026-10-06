"""Analyze a YouTube channel from a URL/@handle: who they are and what works for them.

This is the new entry point — instead of typing a niche, the user pastes their channel
link. We resolve it to a channel ID (handling the several URL forms YouTube uses),
pull the channel's top-performing videos and recent titles, and hand that to the LLM to
infer the niche. Downstream, research + ideation build on that.
"""

from __future__ import annotations

import re

from googleapiclient.discovery import build

from beacon_engine.config import settings
from beacon_engine.research.models import ChannelProfile, VideoStat

_CHANNEL_ID = re.compile(r"youtube\.com/channel/(UC[\w-]{20,})", re.I)
_HANDLE = re.compile(r"(?:youtube\.com/)?@([\w.\-]+)", re.I)
_USER = re.compile(r"youtube\.com/user/([\w.\-]+)", re.I)
_CUSTOM = re.compile(r"youtube\.com/c/([\w.\-]+)", re.I)


class ChannelAnalyzer:
    def __init__(self, api_key: str | None = None):
        key = api_key or settings.youtube_api_key
        if not key:
            raise RuntimeError("YOUTUBE_API_KEY is not set (needed to read the channel).")
        self._yt = build("youtube", "v3", developerKey=key, cache_discovery=False)

    def analyze(self, url_or_handle: str, *, top_n: int = 25, recent_n: int = 25) -> ChannelProfile:
        channel_id = self._resolve(url_or_handle)
        resp = self._yt.channels().list(
            part="snippet,statistics,contentDetails", id=channel_id
        ).execute()
        items = resp.get("items", [])
        if not items:
            raise RuntimeError(f"Channel not found for: {url_or_handle}")
        c = items[0]
        snip, stats = c.get("snippet", {}), c.get("statistics", {})
        uploads = c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        catalog = self._catalog_videos(uploads)

        return ChannelProfile(
            channel_id=channel_id,
            title=snip.get("title", ""),
            handle=snip.get("customUrl", ""),
            description=(snip.get("description", "") or "")[:2000],
            subscribers=int(stats.get("subscriberCount", 0) or 0),
            video_count=int(stats.get("videoCount", 0) or 0),
            view_count=int(stats.get("viewCount", 0) or 0),
            top_videos=(sorted(catalog, key=lambda v: v.views, reverse=True)[:top_n]
                        if catalog else self._top_videos(channel_id, top_n)),
            recent_titles=([v.title for v in catalog[:recent_n]]
                           if catalog else self._recent_titles(uploads, recent_n)),
            catalog_videos=catalog,
        )

    # -- channel resolution -------------------------------------------------

    def _resolve(self, s: str) -> str:
        s = s.strip()
        if m := _CHANNEL_ID.search(s):
            return m.group(1)
        if re.fullmatch(r"UC[\w-]{20,}", s):  # bare channel id
            return s
        if m := _HANDLE.search(s):
            return self._by_handle("@" + m.group(1))
        if m := _USER.search(s):
            r = self._yt.channels().list(part="id", forUsername=m.group(1)).execute()
            if r.get("items"):
                return r["items"][0]["id"]
        name = m.group(1) if (m := _CUSTOM.search(s)) else s
        return self._by_search(name)

    def _by_handle(self, handle: str) -> str:
        r = self._yt.channels().list(part="id", forHandle=handle).execute()
        if r.get("items"):
            return r["items"][0]["id"]
        return self._by_search(handle.lstrip("@"))

    def _by_search(self, q: str) -> str:
        r = self._yt.search().list(part="snippet", q=q, type="channel", maxResults=1).execute()
        items = r.get("items", [])
        if not items:
            raise RuntimeError(f"Could not resolve a channel from: {q}")
        return items[0]["snippet"]["channelId"]

    # -- video pulls --------------------------------------------------------

    def _catalog_videos(self, uploads: str, *, limit: int = 200) -> list[VideoStat]:
        """Read uploads cheaply, including ordinary/weak videos needed to judge winners.

        Older large channels are explicitly sampled; never imply this is their full history.
        Uploads + batched details cost far less API quota than view-sorted search.list.
        """
        if not uploads:
            return []
        try:
            ids: list[str] = []
            token = None
            seen_tokens: set[str] = set()
            while len(ids) < limit:
                params = dict(part="contentDetails", playlistId=uploads,
                              maxResults=min(50, limit - len(ids)))
                if token:
                    params["pageToken"] = token
                page = self._yt.playlistItems().list(**params).execute()
                if not page.get("items"):
                    break
                ids.extend(it["contentDetails"]["videoId"] for it in page.get("items", [])
                           if it.get("contentDetails", {}).get("videoId"))
                token = page.get("nextPageToken")
                if not token or token in seen_tokens:
                    break
                seen_tokens.add(token)
            out = []
            for start in range(0, len(ids), 50):
                page = self._yt.videos().list(part="snippet,statistics",
                                             id=",".join(ids[start:start + 50])).execute()
                for it in page.get("items", []):
                    sn = it.get("snippet", {})
                    out.append(VideoStat(
                        video_id=it["id"], title=sn.get("title", ""),
                        channel=sn.get("channelTitle", ""), channel_id=sn.get("channelId", ""),
                        views=int(it.get("statistics", {}).get("viewCount", 0) or 0),
                        published_at=sn.get("publishedAt", ""),
                        url=f"https://www.youtube.com/watch?v={it['id']}"))
            return sorted(out, key=lambda v: v.published_at, reverse=True)
        except Exception:
            return []

    def _top_videos(self, channel_id: str, n: int) -> list[VideoStat]:
        r = self._yt.search().list(
            part="id", channelId=channel_id, type="video",
            order="viewCount", maxResults=min(n, 50),
        ).execute()
        ids = [it["id"]["videoId"] for it in r.get("items", []) if it["id"].get("videoId")]
        if not ids:
            return []
        d = self._yt.videos().list(part="snippet,statistics", id=",".join(ids)).execute()
        out = []
        for it in d.get("items", []):
            stt, sn = it.get("statistics", {}), it.get("snippet", {})
            out.append(VideoStat(
                video_id=it["id"], title=sn.get("title", ""),
                channel=sn.get("channelTitle", ""),
                channel_id=sn.get("channelId", ""),
                views=int(stt.get("viewCount", 0) or 0),
                published_at=sn.get("publishedAt", ""),
                url=f"https://www.youtube.com/watch?v={it['id']}",
            ))
        out.sort(key=lambda v: v.views, reverse=True)
        return out

    def _recent_titles(self, uploads_playlist: str, n: int) -> list[str]:
        if not uploads_playlist:
            return []
        try:
            r = self._yt.playlistItems().list(
                part="snippet", playlistId=uploads_playlist, maxResults=min(n, 50)
            ).execute()
            return [it["snippet"]["title"] for it in r.get("items", [])]
        except Exception:  # noqa: BLE001
            return []
