from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beacon_engine.research.enrichment import EnrichmentEngine
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.store import YouTubeStore


class FakeOfficialProvider:
    """Stand-in for the official API: serves batched stats from a fixture map."""

    name = "fake-official"

    def __init__(self, videos: dict[str, int], channels: dict[str, int]) -> None:
        self._videos = videos
        self._channels = channels
        self.video_batches: list[list[str]] = []
        self.channel_batches: list[list[str]] = []

    def videos(self, ids: list[str]) -> list[ProviderVideo]:
        self.video_batches.append(list(ids))
        return [
            ProviderVideo(video_id=v, title=f"t-{v}", channel_id="UC_x", views=self._videos[v])
            for v in ids
            if v in self._videos
        ]

    def channels(self, ids: list[str]) -> list[ProviderChannel]:
        self.channel_batches.append(list(ids))
        return [
            ProviderChannel(channel_id=c, title=f"c-{c}", subscribers=self._channels[c])
            for c in ids
            if c in self._channels
        ]


class EnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = YouTubeStore(Path(self.tmp.name) / "index.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        for i in range(120):
            self.store.upsert_video(ProviderVideo(video_id=f"vid{i:08d}", channel_id="UC_x", views=0))
        for i in range(70):
            self.store.upsert_channel(ProviderChannel(channel_id=f"UC{i:022d}", title=f"c{i}"))

    def test_video_backfill_fills_views_in_batches(self) -> None:
        self._seed()
        ids = self.store.ids_needing_video_stats()
        provider = FakeOfficialProvider({v: 1000 + n for n, v in enumerate(ids)}, {})
        engine = EnrichmentEngine(self.store, provider=provider)

        report = engine.backfill_video_stats()

        self.assertEqual(report.updated, 120)
        self.assertEqual(report.quota_units, 3)  # 120 / 50 -> 3 batches
        self.assertEqual(len(self.store.ids_needing_video_stats()), 0)

    def test_channel_backfill_fills_subscribers(self) -> None:
        self._seed()
        ids = self.store.ids_needing_channel_stats()
        provider = FakeOfficialProvider({}, {c: 5000 for c in ids})
        engine = EnrichmentEngine(self.store, provider=provider)

        report = engine.backfill_channel_stats()

        self.assertEqual(report.updated, 70)
        self.assertEqual(report.quota_units, 2)  # 70 / 50 -> 2 batches
        self.assertEqual(len(self.store.ids_needing_channel_stats()), 0)

    def test_budget_caps_quota_spend(self) -> None:
        self._seed()
        ids = self.store.ids_needing_video_stats()
        provider = FakeOfficialProvider({v: 42 for v in ids}, {})
        engine = EnrichmentEngine(self.store, provider=provider)

        report = engine.backfill_video_stats(max_units=1)

        self.assertEqual(report.quota_units, 1)
        self.assertEqual(report.updated, 50)
        self.assertEqual(len(self.store.ids_needing_video_stats()), 70)  # 120 - 50 left

    def test_dead_ids_are_not_retried_forever(self) -> None:
        # A video the API never returns (deleted/private) stays views=0 but must
        # drop out of the queue after max_attempts so the backfill keeps moving.
        self.store.upsert_video(ProviderVideo(video_id="deadvideo01", channel_id="UC_x", views=0))
        provider = FakeOfficialProvider({}, {})  # returns nothing
        engine = EnrichmentEngine(self.store, provider=provider)

        for _ in range(3):
            engine.backfill_video_stats(max_attempts=3)

        self.assertEqual(self.store.ids_needing_video_stats(max_attempts=3), [])


if __name__ == "__main__":
    unittest.main()
