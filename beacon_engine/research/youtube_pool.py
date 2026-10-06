"""Concurrent pool of YouTube Data API keys.

Each key must belong to a SEPARATE Google Cloud project (quota is per-project,
10k units/day). Batched work is split across healthy keys at the same time.
Keys that hit quota, are invalid, have the API disabled, or are restricted are
marked dead and skipped for the rest of the process session. So N valid keys
across N projects ≈ N × 10k units/day, with bounded concurrent throughput.

Returns raw API `items` dicts (callers read snippet/statistics/etc. directly).
Keys come from YOUTUBE_API_KEYS (comma-separated in .env); config.load_dotenv()
has already populated the environment by the time this imports.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Empty, Queue
from typing import Callable

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from beacon_engine.config import settings  # noqa: F401 — triggers load_dotenv() on import
from beacon_engine.logging_util import get_logger

log = get_logger()

_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                  "userRateLimitExceeded"}
_DEFAULT_MAX_WORKERS = 16
_HARD_MAX_WORKERS = 32
_KEY_WAIT_SECONDS = 0.1


class QuotaExhausted(RuntimeError):
    """Every key in the pool is exhausted or unavailable for now."""


def load_keys() -> list[str]:
    raw = os.getenv("YOUTUBE_API_KEYS") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        single = os.getenv("YOUTUBE_API_KEY")
        if single:
            keys = [single.strip()]
    return list(dict.fromkeys(keys))


def _reason(e: HttpError) -> str:
    try:
        details = getattr(e, "error_details", None)
        if details and isinstance(details, list):
            return str(details[0].get("reason", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        import json
        body = e.content.decode() if isinstance(e.content, bytes) else e.content
        return str(json.loads(body)["error"]["errors"][0].get("reason", ""))
    except Exception:  # noqa: BLE001
        return ""


def _configured_worker_limit(key_count: int) -> int:
    raw = str(os.getenv("YOUTUBE_API_WORKERS") or "").strip()
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            log.warning("ignoring invalid YOUTUBE_API_WORKERS value")
        else:
            if requested > 0:
                return min(key_count, requested, _HARD_MAX_WORKERS)
    return min(key_count, _DEFAULT_MAX_WORKERS)


class YouTubePool:
    def __init__(
        self,
        keys: list[str] | None = None,
        *,
        max_workers: int | None = None,
    ) -> None:
        self.keys = keys or load_keys()
        if not self.keys:
            raise RuntimeError("No YouTube API keys — set YOUTUBE_API_KEYS in .env")
        self._clients: dict[int, object] = {}
        self._state_lock = threading.Lock()
        self._available: Queue[int] = Queue()
        for index in range(len(self.keys)):
            self._available.put(index)
        self._dead: set[int] = set()   # index -> unusable this session (quota/invalid)
        self.units = 0                 # approx quota units spent this session
        self._key_units = [0 for _ in self.keys]
        requested_workers = (
            _configured_worker_limit(len(self.keys))
            if max_workers is None
            else max(1, int(max_workers))
        )
        self._max_workers = min(
            len(self.keys),
            requested_workers,
            _HARD_MAX_WORKERS,
        )

    def _client(self, i: int):
        if i not in self._clients:
            self._clients[i] = build("youtube", "v3", developerKey=self.keys[i],
                                     cache_discovery=False)
        return self._clients[i]

    @property
    def alive(self) -> int:
        with self._state_lock:
            return len(self.keys) - len(self._dead)

    @property
    def parallel_workers(self) -> int:
        return min(self._max_workers, self.alive)

    def status(self) -> dict[str, object]:
        """Return non-secret pool telemetry safe for logs and status screens."""
        with self._state_lock:
            alive = len(self.keys) - len(self._dead)
            return {
                "configured_keys": len(self.keys),
                "alive_keys": alive,
                "parallel_workers": min(self._max_workers, alive),
                "quota_units": int(self.units),
                "quota_units_by_key": list(self._key_units),
            }

    def _acquire_key(self) -> int:
        while True:
            with self._state_lock:
                if len(self._dead) >= len(self.keys):
                    raise QuotaExhausted(
                        f"all {len(self.keys)} YouTube API keys "
                        "exhausted/unavailable this session"
                    )
            try:
                index = self._available.get(timeout=_KEY_WAIT_SECONDS)
            except Empty:
                continue
            with self._state_lock:
                if index in self._dead:
                    continue
            return index

    def _release_key(self, index: int) -> None:
        with self._state_lock:
            reusable = index not in self._dead
        if reusable:
            self._available.put(index)

    def _mark_dead(self, index: int) -> None:
        with self._state_lock:
            self._dead.add(index)

    def _call(self, make_req: Callable, cost: int = 1):
        while True:
            i = self._acquire_key()
            try:
                result = make_req(self._client(i)).execute()
            except HttpError as e:
                reason = _reason(e)
                status = getattr(e.resp, "status", None)
                if status == 403 and reason in _QUOTA_REASONS:
                    log.info("key #%d exhausted (%s) — rotating", i + 1, reason)
                    self._mark_dead(i)
                elif status in (400, 403, 404):
                    log.info("key #%d unusable (%s %s) — skipping", i + 1, status, reason)
                    self._mark_dead(i)
                else:
                    self._release_key(i)
                    raise
                continue
            except Exception:
                self._release_key(i)
                raise
            with self._state_lock:
                self.units += cost
                self._key_units[i] += cost
            self._release_key(i)
            return result

    def _parallel_chunks(
        self,
        chunks: list[list[str]],
        request: Callable[[object, list[str]], object],
    ) -> list[dict]:
        if not chunks:
            return []
        workers = min(len(chunks), max(1, self.parallel_workers))
        if workers == 1:
            responses = [
                self._call(lambda client, chunk=chunk: request(client, chunk))
                for chunk in chunks
            ]
        else:
            responses: list[object | None] = [None] * len(chunks)
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="youtube-api",
            ) as executor:
                futures = {
                    executor.submit(
                        self._call,
                        lambda client, chunk=chunk: request(client, chunk),
                    ): index
                    for index, chunk in enumerate(chunks)
                }
                for future in as_completed(futures):
                    responses[futures[future]] = future.result()
        out: list[dict] = []
        for response in responses:
            if isinstance(response, dict):
                out.extend(response.get("items", []))
        return out

    # -- batched fetchers (1 unit per 50 ids) -------------------------------
    def videos(self, ids: list[str], *, part: str = "snippet,statistics,contentDetails") -> list[dict]:
        clean = [v for v in dict.fromkeys(i for i in ids if i)]
        chunks = [clean[j:j + 50] for j in range(0, len(clean), 50)]
        return self._parallel_chunks(
            chunks,
            lambda yt, chunk: yt.videos().list(
                part=part,
                id=",".join(chunk),
            ),
        )

    def channels(self, ids: list[str], *, part: str = "snippet,statistics") -> list[dict]:
        clean = [c for c in dict.fromkeys(i for i in ids if i)]
        chunks = [clean[j:j + 50] for j in range(0, len(clean), 50)]
        return self._parallel_chunks(
            chunks,
            lambda yt, chunk: yt.channels().list(
                part=part,
                id=",".join(chunk),
            ),
        )

    def playlist_video_ids(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
    ) -> list[str]:
        """Fetch upload IDs at one quota unit per page (up to 50 per page)."""
        clean = str(playlist_id or "").strip()
        remaining = max(0, int(limit or 0))
        if not clean or remaining <= 0:
            return []
        ids: list[str] = []
        page_token = None
        while len(ids) < remaining:
            request = {
                "part": "contentDetails",
                "playlistId": clean,
                "maxResults": min(50, remaining - len(ids)),
            }
            if page_token:
                request["pageToken"] = page_token
            response = self._call(
                lambda yt, r=dict(request): yt.playlistItems().list(**r)
            )
            for item in response.get("items", []):
                video_id = str(
                    (item.get("contentDetails") or {}).get("videoId") or ""
                )
                if video_id and video_id not in ids:
                    ids.append(video_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return ids

    def search_ids(self, query: str, *, limit: int = 50, order: str = "viewCount") -> list[str]:
        """search.list costs 100 units per call — use sparingly."""
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            req = dict(part="id", q=query, type="video", order=order,
                       maxResults=min(50, limit - len(ids)))
            if page_token:
                req["pageToken"] = page_token
            resp = self._call(lambda yt, r=dict(req): yt.search().list(**r), cost=100)
            ids.extend(it["id"]["videoId"] for it in resp.get("items", [])
                       if it.get("id", {}).get("videoId"))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids
