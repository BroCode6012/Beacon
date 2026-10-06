from __future__ import annotations

import json
import threading

from googleapiclient.errors import HttpError

from beacon_engine.research.youtube_pool import YouTubePool


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = ""


class _ConcurrencyTracker:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.release = threading.Event()

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= self.expected:
                self.release.set()

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class _Request:
    def __init__(
        self,
        *,
        key: str,
        ids: list[str],
        tracker: _ConcurrencyTracker | None = None,
        exhausted: set[str] | None = None,
    ) -> None:
        self.key = key
        self.ids = ids
        self.tracker = tracker
        self.exhausted = exhausted or set()

    def execute(self):
        if self.key in self.exhausted:
            raise HttpError(
                _Response(403),
                json.dumps(
                    {
                        "error": {
                            "errors": [{"reason": "quotaExceeded"}]
                        }
                    }
                ).encode("utf-8"),
            )
        if self.tracker is not None:
            self.tracker.enter()
            self.tracker.release.wait(timeout=1.0)
        try:
            return {"items": [{"id": value, "key": self.key} for value in self.ids]}
        finally:
            if self.tracker is not None:
                self.tracker.leave()


class _Resource:
    def __init__(
        self,
        *,
        key: str,
        tracker: _ConcurrencyTracker | None = None,
        exhausted: set[str] | None = None,
    ) -> None:
        self.key = key
        self.tracker = tracker
        self.exhausted = exhausted

    def list(self, *, id: str, **_kwargs):
        return _Request(
            key=self.key,
            ids=id.split(","),
            tracker=self.tracker,
            exhausted=self.exhausted,
        )


class _Client:
    def __init__(
        self,
        *,
        key: str,
        tracker: _ConcurrencyTracker | None = None,
        exhausted: set[str] | None = None,
    ) -> None:
        self.key = key
        self.tracker = tracker
        self.exhausted = exhausted

    def videos(self):
        return _Resource(
            key=self.key,
            tracker=self.tracker,
            exhausted=self.exhausted,
        )

    def channels(self):
        return _Resource(
            key=self.key,
            tracker=self.tracker,
            exhausted=self.exhausted,
        )


def test_batched_requests_use_all_keys_concurrently_and_preserve_order(monkeypatch):
    tracker = _ConcurrencyTracker(expected=3)
    monkeypatch.setattr(
        "beacon_engine.research.youtube_pool.build",
        lambda _service, _version, *, developerKey, cache_discovery: _Client(
            key=developerKey,
            tracker=tracker,
        ),
    )
    pool = YouTubePool(["key-1", "key-2", "key-3"])
    ids = [f"video-{index:03d}" for index in range(150)]

    rows = pool.videos(ids)

    assert [row["id"] for row in rows] == ids
    assert tracker.max_active == 3
    assert pool.units == 3
    assert pool.status() == {
        "configured_keys": 3,
        "alive_keys": 3,
        "parallel_workers": 3,
        "quota_units": 3,
        "quota_units_by_key": [1, 1, 1],
    }


def test_exhausted_key_is_removed_and_pending_work_moves_to_healthy_key(monkeypatch):
    monkeypatch.setattr(
        "beacon_engine.research.youtube_pool.build",
        lambda _service, _version, *, developerKey, cache_discovery: _Client(
            key=developerKey,
            exhausted={"bad-key"},
        ),
    )
    pool = YouTubePool(["bad-key", "good-key"])
    ids = [f"channel-{index:03d}" for index in range(100)]

    rows = pool.channels(ids)

    assert [row["id"] for row in rows] == ids
    assert {row["key"] for row in rows} == {"good-key"}
    assert pool.alive == 1
    assert pool.units == 2
    assert pool.status()["parallel_workers"] == 1


def test_worker_cap_limits_parallelism_without_dropping_keys(monkeypatch):
    tracker = _ConcurrencyTracker(expected=2)
    monkeypatch.setattr(
        "beacon_engine.research.youtube_pool.build",
        lambda _service, _version, *, developerKey, cache_discovery: _Client(
            key=developerKey,
            tracker=tracker,
        ),
    )
    pool = YouTubePool(
        ["key-1", "key-2", "key-3", "key-4"],
        max_workers=2,
    )

    rows = pool.videos([f"video-{index:03d}" for index in range(200)])

    assert len(rows) == 200
    assert tracker.max_active == 2
    assert pool.status()["configured_keys"] == 4
    assert pool.status()["parallel_workers"] == 2
