from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
from pathlib import Path

from beacon_engine.research.channel_discovery import ChannelDiscoveryEngine, expand_queries
from beacon_engine.research.crawler import CrawlReport
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.radar import load_config
from beacon_engine.research.store import YouTubeStore


class FakeCrawler:
    """Stands in for YouTubeCrawler: each query 'discovers' fixed channels/videos."""

    def __init__(self, store: YouTubeStore, plan: dict[str, list[tuple[str, str]]]) -> None:
        self.store = store
        self.plan = plan  # query -> list of (channel_id, video_id)

    def crawl_search(self, query, *, limit=60, enrich=False, providers=None) -> CrawlReport:
        report = CrawlReport(kind="search", query=query, requested=limit)
        for channel_id, video_id in self.plan.get(query, []):
            self.store.upsert_channel(ProviderChannel(channel_id=channel_id, title=channel_id))
            self.store.upsert_video(ProviderVideo(video_id=video_id, channel_id=channel_id, title=f"t-{video_id}"))
            self.store.save_search_results(query, "fake", [ProviderVideo(video_id=video_id, channel_id=channel_id)])
            report.discovered += 1
        report.providers_used = ["fake"]
        return report


class ParallelFakeCrawler:
    """Network fetches overlap; database writes are recorded by caller order."""

    def __init__(
        self,
        store: YouTubeStore,
        plan: dict[str, list[tuple[str, str]]],
        tracker: dict,
    ) -> None:
        self.store = store
        self.plan = plan
        self.tracker = tracker

    def fetch_search(self, query, *, limit=60, providers=None, sort=None):
        with self.tracker["lock"]:
            self.tracker["active"] += 1
            self.tracker["max_active"] = max(
                self.tracker["max_active"], self.tracker["active"])
        try:
            time.sleep(self.tracker.get("delays", {}).get(query, 0.04))
            if query in self.tracker.get("fail", set()):
                raise RuntimeError("planned fetch failure")
            videos = [
                ProviderVideo(
                    video_id=video_id,
                    channel_id=channel_id,
                    channel=channel_id,
                    title=f"t-{video_id}",
                    source="fake",
                )
                for channel_id, video_id in self.plan.get(query, [])
            ]
            report = CrawlReport(
                kind="search",
                query=query,
                requested=limit,
                discovered=len(videos),
                providers_used=["fake"],
            )
            return videos, report
        finally:
            with self.tracker["lock"]:
                self.tracker["active"] -= 1

    def store_fetched_search(self, query, videos, report) -> None:
        self.tracker["stored_order"].append(query)
        for video in videos:
            self.store.upsert_channel(ProviderChannel(
                channel_id=video.channel_id, title=video.channel_id))
            self.store.upsert_video(video)
        self.store.save_search_results(query, "fake", videos)


class ChannelDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = YouTubeStore(Path(self.tmp.name) / "index.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_expand_queries_fans_out_and_dedupes(self) -> None:
        out = expand_queries(["ancient rome", "ancient rome"], modifiers=["documentary", "facts"])
        self.assertEqual(out, ["ancient rome", "ancient rome documentary", "ancient rome facts"])

    def test_expand_skips_modifier_already_present(self) -> None:
        out = expand_queries(["rome documentary"], modifiers=["documentary", "facts"])
        self.assertEqual(out, ["rome documentary", "rome documentary facts"])

    def test_harvest_worker_config_is_bounded(self) -> None:
        config_path = Path(self.tmp.name) / "radar.json"
        config_path.write_text(
            json.dumps({"harvest_workers": 99}),
            encoding="utf-8",
        )

        self.assertEqual(load_config(config_path).harvest_workers, 8)

    def test_harvest_counts_only_new_channels(self) -> None:
        plan = {
            "rome": [("UC_a", "v1"), ("UC_b", "v2")],
            "egypt": [("UC_b", "v3"), ("UC_c", "v4")],  # UC_b repeats -> not new
        }
        engine = ChannelDiscoveryEngine(self.store, crawler=FakeCrawler(self.store, plan))

        report = engine.harvest(["rome", "egypt"], per_query=10)

        self.assertEqual(report.channels_before, 0)
        self.assertEqual(report.channels_after, 3)  # a, b, c
        self.assertEqual(report.new_channels, 3)
        self.assertEqual(report.videos_seen, 4)

    def test_categorize_labels_channels_by_dominant_query(self) -> None:
        plan = {
            "scary stories": [("UC_x", "vx1"), ("UC_x", "vx2")],
            "finance scams": [("UC_y", "vy1")],
        }
        engine = ChannelDiscoveryEngine(self.store, crawler=FakeCrawler(self.store, plan))
        engine.harvest(["scary stories", "finance scams"], per_query=10)

        report = engine.categorize()

        self.assertEqual(report.categorized, 2)
        cats = dict(self.store.category_breakdown())
        self.assertEqual(cats.get("scary stories"), 1)
        self.assertEqual(cats.get("finance scams"), 1)

    def test_incremental_categorize_only_processes_missing_channels(self) -> None:
        plan = {
            "old query": [("UC_old", "v_old")],
            "new query": [("UC_new", "v_new")],
        }
        engine = ChannelDiscoveryEngine(
            self.store,
            crawler=FakeCrawler(self.store, plan),
        )
        engine.harvest(["old query"], per_query=10)
        engine.categorize()
        self.store.set_channel_categories({"UC_old": "manual category"})

        engine.harvest(["new query"], per_query=10)
        report = engine.categorize(only_missing=True)

        with self.store.connect() as conn:
            categories = {
                row["channel_id"]: row["category"]
                for row in conn.execute(
                    "SELECT channel_id, category FROM channels"
                )
            }
        self.assertEqual(report.categorized, 1)
        self.assertEqual(categories["UC_old"], "manual category")
        self.assertEqual(categories["UC_new"], "new query")
        with self.store.connect() as conn:
            classified = int(conn.execute(
                "SELECT COUNT(*) FROM channel_classifications"
            ).fetchone()[0])
        self.assertEqual(classified, 2)

    def test_parallel_harvest_fetches_concurrently_but_stores_in_query_order(self) -> None:
        plan = {
            "slow": [("UC_a", "v1"), ("UC_b", "v2")],
            "fast": [("UC_b", "v3"), ("UC_c", "v4")],
            "middle": [("UC_d", "v5")],
        }
        tracker = {
            "lock": threading.Lock(),
            "active": 0,
            "max_active": 0,
            "stored_order": [],
            "delays": {"slow": 0.08, "fast": 0.01, "middle": 0.04},
        }
        factory = lambda: ParallelFakeCrawler(self.store, plan, tracker)
        engine = ChannelDiscoveryEngine(
            self.store,
            crawler=factory(),
            crawler_factory=factory,
        )
        yields = []

        report = engine.harvest(
            ["slow", "fast", "middle"],
            per_query=10,
            workers=3,
            on_query_result=lambda query, delta: yields.append((query, delta)),
        )

        self.assertGreaterEqual(tracker["max_active"], 2)
        self.assertEqual(tracker["stored_order"], ["slow", "fast", "middle"])
        self.assertEqual(yields, [("slow", 2), ("fast", 1), ("middle", 1)])
        self.assertEqual(report.new_channels, 4)
        self.assertEqual(report.videos_seen, 5)

    def test_parallel_harvest_isolates_one_failed_query(self) -> None:
        plan = {
            "good one": [("UC_a", "v1")],
            "good two": [("UC_b", "v2")],
        }
        tracker = {
            "lock": threading.Lock(),
            "active": 0,
            "max_active": 0,
            "stored_order": [],
            "fail": {"broken"},
        }
        factory = lambda: ParallelFakeCrawler(self.store, plan, tracker)
        engine = ChannelDiscoveryEngine(
            self.store,
            crawler=factory(),
            crawler_factory=factory,
        )

        report = engine.harvest(
            ["good one", "broken", "good two"],
            per_query=10,
            workers=3,
        )

        self.assertEqual(report.searched, 2)
        self.assertEqual(report.new_channels, 2)
        self.assertEqual(tracker["stored_order"], ["good one", "good two"])
        self.assertTrue(any("broken: RuntimeError" in err for err in report.errors))

    def test_successful_empty_search_records_attempt_but_not_yield(self) -> None:
        engine = ChannelDiscoveryEngine(
            self.store,
            crawler=FakeCrawler(self.store, {"empty topic": []}),
        )
        attempts = []
        yields = []

        report = engine.harvest(
            ["empty topic"],
            per_query=10,
            on_query_attempt=attempts.append,
            on_query_result=lambda query, delta: yields.append((query, delta)),
        )

        self.assertEqual(report.searched, 1)
        self.assertEqual(attempts, ["empty topic"])
        self.assertEqual(yields, [])

    def test_provider_failure_does_not_record_successful_attempt(self) -> None:
        class FailedCrawler:
            def crawl_search(
                self,
                query,
                *,
                limit=60,
                enrich=False,
                providers=None,
            ) -> CrawlReport:
                return CrawlReport(
                    kind="search",
                    query=query,
                    requested=limit,
                    errors=["fake: provider unavailable"],
                )

        attempts = []
        report = ChannelDiscoveryEngine(
            self.store,
            crawler=FailedCrawler(),
        ).harvest(
            ["retry topic"],
            per_query=10,
            on_query_attempt=attempts.append,
        )

        self.assertEqual(report.searched, 1)
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
