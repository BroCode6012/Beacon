"""Smart Scrape competitor/trending selection must be scoped to the NICHE.

Regression: before this, competitors were the globally-biggest channels in the
whole shared index (MrBeast, music, kids) regardless of the analyzed niche.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beacon_engine.research.providers import ProviderVideo
from beacon_engine.research.smart_scraper import (
    SmartScrapePlan,
    _competitors_from_store,
    _niche_channel_hits,
    _niche_scope_ids,
    _niche_top_patterns,
    _top_patterns_from_store,
)
from beacon_engine.research.store import YouTubeStore


class _ScrapeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = YouTubeStore(Path(self.tmp.name) / "index.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _discover(self, query: str, videos: list[ProviderVideo]) -> None:
        self.store.upsert_videos(videos)
        rid = self.store.start_run(kind="search", query=query, provider="test")
        self.store.save_search_results(query, "test", videos, run_id=rid)


class NicheScopingTests(_ScrapeStoreTest):
    def test_competitors_ranked_by_niche_relevance_not_global_views(self) -> None:
        food = "UC" + "f" * 22
        # A real niche channel: many videos that rank for the niche query.
        self._discover("food brands exposed", [
            ProviderVideo(video_id=f"food{i:02d}", title=f"Brand #{i} EXPOSED",
                          channel="Food Brands Exposed", channel_id=food,
                          views=200_000 + i, url=f"https://youtu.be/food{i:02d}", source="t")
            for i in range(8)
        ])
        # A global mega-channel whose ONE viral clip happens to match the query.
        beast = "UC" + "b" * 22
        self._discover("food brands exposed", [
            ProviderVideo(video_id="beast1", title="I ate the world's biggest food",
                          channel="MrBeast", channel_id=beast, views=300_000_000,
                          url="https://youtu.be/beast1", source="t"),
        ])
        # ...plus lots of unrelated MrBeast videos in the index (global noise).
        self.store.upsert_videos([
            ProviderVideo(video_id=f"beastx{i}", title=f"Challenge {i}", channel="MrBeast",
                          channel_id=beast, views=250_000_000, url=f"u{i}", source="t")
            for i in range(20)
        ])

        hits = _niche_channel_hits(self.store, ["food brands exposed"])
        self.assertEqual(hits.get(food), 8)
        self.assertEqual(hits.get(beast), 1)

        comps = _competitors_from_store(self.store, niche_hits=hits)
        titles = [c.title for c in comps]
        self.assertEqual(titles[0], "Food Brands Exposed")   # niche channel ranks first
        # MrBeast (1 tangential hit) must NOT outrank the niche channel despite 300M views
        self.assertLess(titles.index("Food Brands Exposed"),
                        titles.index("MrBeast") if "MrBeast" in titles else 999)

    def test_scope_prefers_multi_hit_channels(self) -> None:
        hits = {"a": 5, "b": 3, "c": 1, "d": 1, "e": 4, "f": 2, "g": 1}
        scope = _niche_scope_ids(hits)          # 4 channels with >=2 hits -> still <5
        self.assertEqual(scope, set(hits))      # relaxes to all when strong set is tiny
        big = {f"c{i}": (2 if i < 6 else 1) for i in range(10)}
        strong = _niche_scope_ids(big)
        self.assertTrue(all(big[c] >= 2 for c in strong))   # keeps only 2+ hit channels
        self.assertEqual(len(strong), 6)

    def test_trending_scoped_to_niche_channels(self) -> None:
        food = "UC" + "f" * 22
        self._discover("food brands exposed", [
            ProviderVideo(video_id="f1", title="Olive oil fraud", channel="Food Exposed",
                          channel_id=food, views=500_000, url="uf1", source="t"),
        ])
        beast = "UC" + "b" * 22
        self.store.upsert_videos([
            ProviderVideo(video_id="b1", title="I gave away $1M", channel="MrBeast",
                          channel_id=beast, views=300_000_000, url="ub1", source="t"),
        ])
        scope = _niche_scope_ids(_niche_channel_hits(self.store, ["food brands exposed"]))
        patterns = _top_patterns_from_store(self.store, limit=10, channel_ids=scope)
        channels = {p.channel for p in patterns}
        self.assertIn("Food Exposed", channels)
        self.assertNotIn("MrBeast", channels)   # global mega-video excluded from trending


class TwoSignalTrendingTests(_ScrapeStoreTest):
    def test_prank_video_excluded_when_channel_not_in_scope(self) -> None:
        food = "UC" + "f" * 22
        self._discover("fast food secrets documentary", [
            ProviderVideo(video_id=f"food{i}", title=f"Fast food secret #{i}",
                          channel="Food Exposed", channel_id=food, views=500_000 + i,
                          url=f"uf{i}", source="t") for i in range(3)
        ])
        prank = "UC" + "p" * 22
        # A viral prank that keyword-matches "fast food" but isn't a food channel.
        self._discover("fast food secrets documentary", [
            ProviderVideo(video_id="prank1", title="I Built a SECRET McDonald's!",
                          channel="Prank Bros", channel_id=prank, views=200_000_000,
                          url="up1", source="t"),
        ])
        # Two-signal: ranked for niche AND from a niche channel -> prank excluded.
        pats = _niche_top_patterns(self.store, ["fast food secrets documentary"],
                                   limit=10, channel_scope={food})
        channels = {p.channel for p in pats}
        self.assertIn("Food Exposed", channels)
        self.assertNotIn("Prank Bros", channels)   # 200M prank kept out of trending


class FacelessPreferenceTests(_ScrapeStoreTest):
    def test_faceless_channels_rank_first_in_trending(self) -> None:
        faceless = "UC" + "f" * 22
        oncam = "UC" + "o" * 22
        self._discover("food brands exposed", [
            ProviderVideo(video_id="on1", title="Doctor reacts to food brands",
                          channel="Dr OnCamera", channel_id=oncam, views=9_000_000,
                          url="u1", source="t"),
            ProviderVideo(video_id="fl1", title="10 Food Brands EXPOSED",
                          channel="Faceless Foods", channel_id=faceless, views=400_000,
                          url="u2", source="t"),
        ])
        pats = _niche_top_patterns(self.store, ["food brands exposed"], limit=5,
                                   prefer_channel_ids={faceless})
        # The faceless creator's system puts the faceless video first despite 22x fewer views.
        self.assertEqual(pats[0].channel, "Faceless Foods")
        self.assertEqual(pats[1].channel, "Dr OnCamera")   # kept, but demoted

    def test_min_hits_one_keeps_single_hit_twins(self) -> None:
        twin = "UC" + "t" * 22
        self._discover("shampoo brands exposed", [
            ProviderVideo(video_id="sh1", title="5 Toxic Shampoos EXPOSED",
                          channel="Toxitalk", channel_id=twin, views=150_000,
                          url="u3", source="t"),
        ])
        hits = _niche_channel_hits(self.store, ["shampoo brands exposed"])
        comps = _competitors_from_store(self.store, niche_hits=hits, min_hits=1)
        self.assertIn("Toxitalk", [c.title for c in comps])   # 1 hit is enough for twins


class ScopeThresholdTests(unittest.TestCase):
    def test_min_hits_cuts_tangential(self) -> None:
        hits = {f"real{i}": 8 for i in range(6)}       # 6 strong niche channels
        hits.update({f"tan{i}": 1 for i in range(4)})  # 4 tangential 1-hit channels
        scope = _niche_scope_ids(hits)                 # min_hits=3 default
        self.assertEqual(len(scope), 6)
        self.assertTrue(all(c.startswith("real") for c in scope))

    def test_relaxes_when_too_few_strong(self) -> None:
        hits = {"a": 2, "b": 2, "c": 2, "d": 2, "e": 2, "f": 1}
        scope = _niche_scope_ids(hits)   # no >=3 -> relax to >=2 (5 channels)
        self.assertEqual(scope, {"a", "b", "c", "d", "e"})


class PlanTransferTests(unittest.TestCase):
    def test_transfer_queries_dedup_and_limit(self) -> None:
        plan = SmartScrapePlan(niche="food", format_transfer_queries=[
            "shampoo brands exposed", "Shampoo Brands Exposed", "car brands to avoid",
            "supplement brands ranked", "makeup exposed", "gadgets exposed",
            "cleaning products toxic", "tech exposed",
        ])
        out = plan.transfer_queries(limit=6)
        self.assertEqual(len(out), 6)                      # capped
        self.assertEqual(len(out), len({q.lower() for q in out}))   # de-duped


class FormatTwinModelTests(unittest.TestCase):
    def test_bundle_round_trips_format_twins(self) -> None:
        from beacon_engine.research.models import CompetitorChannel, ResearchBundle
        b = ResearchBundle(niche="food", format_twins=[
            CompetitorChannel(channel_id="UC1", title="Shampoo Exposed", subscribers=50_000)])
        again = ResearchBundle.model_validate_json(b.model_dump_json())
        self.assertEqual(len(again.format_twins), 1)
        self.assertEqual(again.format_twins[0].title, "Shampoo Exposed")


if __name__ == "__main__":
    unittest.main()
