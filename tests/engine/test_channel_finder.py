from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from beacon_engine.intelligence.analyst_tools import IntelligenceAnalystTools
from beacon_engine.research.providers import ProviderChannel
from beacon_engine.research.store import YouTubeStore


class ChannelFinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = YouTubeStore(Path(self.tmp.name) / "index.sqlite3")
        # small low-sub high-view channel, a big channel, and a dead one
        self.store.upsert_channel(ProviderChannel(
            channel_id="UC_small", title="Small Outlier", subscribers=8000,
            view_count=2_000_000, video_count=10, published_at="2026-03-01T00:00:00Z",
        ))
        self.store.upsert_channel(ProviderChannel(
            channel_id="UC_big", title="Big Slow", subscribers=5_000_000,
            view_count=10_000_000, video_count=1000, published_at="2012-01-01T00:00:00Z",
        ))
        self.store.upsert_channel(ProviderChannel(
            channel_id="UC_nodata", title="No Uploads", subscribers=1000,
            view_count=0, video_count=0,
        ))
        self.store.set_channel_categories({"UC_small": "scary stories", "UC_big": "history"})
        with self.store.connect() as conn:
            conn.executemany(
                """
                INSERT INTO channel_text_model
                    (channel_id, faceless, proba, model_version, scored_at)
                VALUES (?, ?, ?, 'test', ?)
                """,
                [
                    ("UC_small", 1, 0.9, "2026-07-10T00:00:00Z"),
                    ("UC_big", 1, 0.9, "2026-07-10T00:00:00Z"),
                ],
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_avg_views_per_video_computed(self) -> None:
        rows = {r["channel_id"]: r for r in self.store.channel_metrics(sort="avg_views")}
        self.assertEqual(int(rows["UC_small"]["avg_views"]), 200_000)  # 2M / 10
        self.assertEqual(int(rows["UC_big"]["avg_views"]), 10_000)     # 10M / 1000
        self.assertNotIn("UC_nodata", rows)  # zero uploads excluded

    def test_low_subs_high_views_filter(self) -> None:
        rows = self.store.channel_metrics(max_subs=50_000, min_avg_views=50_000)
        ids = [r["channel_id"] for r in rows]
        self.assertEqual(ids, ["UC_small"])  # only the small outlier qualifies

    def test_days_since_start_from_creation_date(self) -> None:
        rows = {r["channel_id"]: r for r in self.store.channel_metrics()}
        self.assertGreater(rows["UC_small"]["days_since_start"], 0)
        self.assertGreater(rows["UC_big"]["days_since_start"], rows["UC_small"]["days_since_start"])

    def test_new_channel_filter_uses_days(self) -> None:
        rows = self.store.channel_metrics(max_days_since_start=400)
        ids = [r["channel_id"] for r in rows]
        self.assertIn("UC_small", ids)
        self.assertNotIn("UC_big", ids)  # 2012 channel is far too old

    def test_outlier_score_rewards_recent_low_subs_high_views(self) -> None:
        rows = {r["channel_id"]: r for r in self.store.channel_metrics(sort="outlier_score")}
        # small: recent + few subs + high avg views -> strong outlier
        # big: old + many subs + low avg views -> weak
        self.assertGreater(rows["UC_small"]["outlier_score"], rows["UC_big"]["outlier_score"])
        self.assertEqual(rows[next(iter(rows))]["channel_id"], "UC_small")  # ranks first

    def test_outliers_preset_excludes_big_old_channels(self) -> None:
        rows = self.store.channel_metrics(
            max_subs=100_000, min_avg_views=20_000,
            max_days_since_start=365, min_uploads=3, sort="outlier_score",
        )
        ids = [r["channel_id"] for r in rows]
        self.assertIn("UC_small", ids)
        self.assertNotIn("UC_big", ids)

    def test_category_group_taxonomy(self) -> None:
        from beacon_engine.research.taxonomy import category_group
        self.assertEqual(category_group("scary stories explained"), "Horror & Mystery")
        self.assertEqual(category_group("finance scams explained"), "Money & Finance")
        self.assertEqual(category_group("ancient rome documentary"), "History & War")
        self.assertEqual(category_group(""), "Other")

    def test_categories_in_filter(self) -> None:
        ids = [r["channel_id"] for r in self.store.channel_metrics(categories_in=["scary stories"])]
        self.assertIn("UC_small", ids)
        self.assertNotIn("UC_big", ids)  # UC_big's category is "history"

    def test_format_filter_long_vs_short(self) -> None:
        from beacon_engine.research.providers import ProviderVideo
        for i in range(3):
            self.store.upsert_video(ProviderVideo(
                video_id=f"long{i:08d}", channel_id="UC_small", title="t",
                views=1000, duration_seconds=600))
            self.store.upsert_video(ProviderVideo(
                video_id=f"shrt{i:08d}", channel_id="UC_big", title="t",
                views=1000, duration_seconds=30))
        long_ids = [r["channel_id"] for r in self.store.channel_metrics(video_format="long")]
        short_ids = [r["channel_id"] for r in self.store.channel_metrics(video_format="short")]
        self.assertIn("UC_small", long_ids)
        self.assertNotIn("UC_small", short_ids)
        self.assertIn("UC_big", short_ids)
        self.assertNotIn("UC_big", long_ids)

    def test_category_group_breakdown(self) -> None:
        groups = {g["group"]: g for g in self.store.category_group_breakdown()}
        self.assertIn("Horror & Mystery", groups)   # scary stories -> UC_small
        self.assertIn("History & War", groups)       # history -> UC_big

    def test_revenue_estimate_scales_with_views(self) -> None:
        rows = {r["channel_id"]: r for r in self.store.channel_metrics(rpm=4.0)}
        # est_revenue = avg_views * rpm / 1000
        self.assertAlmostEqual(rows["UC_small"]["est_revenue"], 200_000 * 4.0 / 1000, places=2)

    def test_replicable_filter_is_opt_in_and_keeps_default_behavior(self) -> None:
        default_ids = {r["channel_id"] for r in self.store.channel_metrics()}
        self.assertIn("UC_small", default_ids)
        self.assertIn("UC_big", default_ids)

        replicable_ids = {
            r["channel_id"]
            for r in self.store.channel_metrics(
                replicable_only=True, sort="opportunity_score")
        }
        self.assertIn("UC_small", replicable_ids)
        self.assertNotIn("UC_big", replicable_ids)  # old + 1,000 uploads

    def test_labeled_opportunity_names_suppress_obvious_junk_only(self) -> None:
        # Small reviewed benchmark from the live shortlist. The positive cases
        # protect documentary/format names that merely mention sensitive words.
        labels = {
            "MayurMusical": False,
            "CBBC Epic Facts": False,
            "WIRED Investigations": False,
            "Nibble app: all-around knowledge": False,
            "RumiNormalLife | K-POP Demon Hunter": False,
            "Kids Learning Channel": False,
            "TaylorSwiftVEVO": False,
            "Wild Secrets - English": True,
            "Next Wonder Places": True,
            "Faint Signals": True,
            "History of Music Explained": True,
            "Wired for History": True,
        }
        for i, (title, _expected) in enumerate(labels.items()):
            cid = f"UC_label_{i}"
            self.store.upsert_channel(ProviderChannel(
                channel_id=cid, title=title, subscribers=2000,
                view_count=2_000_000, video_count=20,
                published_at="2026-03-01T00:00:00Z"))
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO channel_text_model "
                    "(channel_id, faceless, proba, model_version, scored_at) "
                    "VALUES (?, 1, 0.9, 'test', '2026-07-10T00:00:00Z')",
                    (cid,))
        raw = {r["title"] for r in self.store.channel_metrics(limit=100)}
        ranked = {r["title"] for r in self.store.channel_metrics(
            replicable_only=True, sort="opportunity_score", limit=100)}
        self.assertTrue(set(labels).issubset(raw))
        for title, should_appear in labels.items():
            with self.subTest(title=title):
                self.assertEqual(title in ranked, should_appear)
        count = self.store.channel_metrics(replicable_only=True, count_only=True)[0]["count"]
        self.assertEqual(count, len(ranked))

    def test_opportunity_score_stabilizes_tiny_subscriber_denominators(self) -> None:
        published = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).isoformat().replace("+00:00", "Z")
        self.store.upsert_channel(ProviderChannel(
            channel_id="UC_tiny", title="Tiny Denominator", subscribers=1,
            view_count=300_000, video_count=10, published_at=published,
        ))
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_text_model
                    (channel_id, faceless, proba, model_version, scored_at)
                VALUES ('UC_tiny', 1, 0.9, 'test', ?)
                """,
                ("2026-07-10T00:00:00Z",),
            )
        row = next(r for r in self.store.channel_metrics(
            replicable_only=True, sort="opportunity_score")
                   if r["channel_id"] == "UC_tiny")
        self.assertGreater(row["outlier_score"], 10_000)
        self.assertLess(row["opportunity_score"], 100)

    def test_replicable_filter_flows_through_finder_and_exact_count(self) -> None:
        tools = IntelligenceAnalystTools(self.store)
        rows = tools.find_channels(
            replicable_only=True, sort="opportunity_score", limit=20)
        self.assertEqual([r["channel_id"] for r in rows], ["UC_small"])
        self.assertIn("opportunity_score", rows[0])
        self.assertEqual(
            tools.count_channels(replicable_only=True),
            {"count": 1},
        )


if __name__ == "__main__":
    unittest.main()
