"""Tracked competitors: growth reader, auto-track, auto-drop, dedup, agent tools."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.workspaces import NicheWorkspaceService
from beacon_engine.research.competitor_tracker import sync_competitors, track_bundle_competitors
from beacon_engine.research.models import CompetitorChannel, ResearchBundle, ChannelProfile
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.radar import _workspace_alerts_due, load_config
from beacon_engine.research.store import YouTubeStore


def _iso(days_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days_ago)).isoformat()


class _StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = YouTubeStore(Path(self.tmp.name) / "index.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _index_channel(self, cid: str, title: str, subs: int = 1000,
                       *, last_upload_days: float = 1.0) -> None:
        self.store.upsert_channel(ProviderChannel(
            channel_id=cid, title=title, subscribers=subs, view_count=subs * 100,
            video_count=10, url=f"https://youtube.com/channel/{cid}"))
        self.store.upsert_video(ProviderVideo(
            video_id=f"{cid}_v", title=f"{title} video", channel=title, channel_id=cid,
            views=50_000, published_at=_iso(last_upload_days),
            url=f"https://youtu.be/{cid}_v", source="test"))


class GrowthReaderTests(_StoreTest):
    def test_growth_deltas_over_days(self) -> None:
        cid = "UC" + "a" * 22
        self._index_channel(cid, "Rival A", subs=1000)
        with self.store.connect() as conn:
            for days_ago, subs, views in [(10, 1000, 100_000), (0, 1500, 190_000)]:
                conn.execute(
                    "INSERT INTO channel_snapshots (channel_id, subscribers, "
                    "total_views, video_count, average_views, median_views, "
                    "upload_cadence_days, captured_at, source) "
                    "VALUES (?,?,?,?,0,0,0,?, 'test')",
                    (cid, subs, views, 10, _iso(days_ago)))
        g = ChannelMetricsService(self.store).channel_growth(cid)
        self.assertEqual(g["subs_gained"], 500)
        self.assertEqual(g["views_gained"], 90_000)
        self.assertAlmostEqual(g["days_tracked"], 10, delta=0.2)
        self.assertGreater(g["subs_per_day"], 40)     # ~50/day

    def test_single_snapshot_has_no_growth_yet(self) -> None:
        cid = "UC" + "b" * 22
        self._index_channel(cid, "Rival B")
        ChannelMetricsService(self.store).capture_channel(cid)
        g = ChannelMetricsService(self.store).channel_growth(cid)
        self.assertEqual(g["snapshots"], 1)
        self.assertEqual(g["subs_gained"], 0)


class AutoTrackTests(_StoreTest):
    def test_bundle_competitors_are_tracked_with_baseline(self) -> None:
        comp = "UC" + "c" * 22
        self._index_channel(comp, "Competitor One")
        profile = ChannelProfile(channel_id="UCown0000000000000000000", title="Mine")
        self.store.upsert_channel(ProviderChannel(channel_id=profile.channel_id, title="Mine"))
        bundle = ResearchBundle(niche="history",
                                competitors=[CompetitorChannel(channel_id=comp, title="Competitor One")])
        result = track_bundle_competitors(self.store, "history", profile, bundle)
        niche_id = result["niche_id"]
        # 1 from the bundle + the scraped-from channel itself.
        self.assertEqual(result["competitors"], 2)
        self.assertEqual(result["seed_channel"], 1)
        rows = NicheWorkspaceService(self.store).workspace_channels(niche_id)
        roles = {r["channel_id"]: r["role"] for r in rows}
        self.assertEqual(roles.get(comp), "competitor")
        # baseline snapshot recorded
        self.assertGreaterEqual(len(ChannelMetricsService(self.store).channel_history(comp)), 1)

    def test_scrape_never_marks_a_channel_as_owned(self) -> None:
        """A scrape studies rivals; it must never claim one is the user's channel.

        Signal reads the same niche_channels table, so an auto-set "owned" role
        makes it think the user already runs a channel in that niche."""
        comp = "UC" + "j" * 22
        self._index_channel(comp, "Competitor Two")
        seed = "UCseed000000000000000000"
        self.store.upsert_channel(ProviderChannel(channel_id=seed, title="Studied Rival"))
        profile = ChannelProfile(channel_id=seed, title="Studied Rival")
        bundle = ResearchBundle(
            niche="history",
            competitors=[CompetitorChannel(channel_id=comp, title="Competitor Two")])
        result = track_bundle_competitors(self.store, "history", profile, bundle)

        self.assertNotIn("owned", result)
        rows = NicheWorkspaceService(self.store).workspace_channels(result["niche_id"])
        self.assertEqual([r["role"] for r in rows if r["role"] == "owned"], [])
        roles = {r["channel_id"]: r["role"] for r in rows}
        self.assertEqual(roles.get(seed), "competitor")   # tracked, but as a rival

    def test_scrape_does_not_downgrade_a_hand_set_owned_channel(self) -> None:
        """Manual "owned" is legitimate; a later scrape must leave it alone."""
        mine = "UCmine000000000000000000"
        self.store.upsert_channel(ProviderChannel(channel_id=mine, title="My Channel"))
        svc = NicheWorkspaceService(self.store)
        niche_id = svc.get_or_create_workspace("history")
        svc.add_channel(niche_id, mine, role="owned", source="manual")

        profile = ChannelProfile(channel_id=mine, title="My Channel")
        result = track_bundle_competitors(
            self.store, "history", profile, ResearchBundle(niche="history"))

        self.assertEqual(result["niche_id"], niche_id)
        self.assertEqual(result["seed_channel"], 0)      # skipped, not rewritten
        roles = {r["channel_id"]: r["role"]
                 for r in svc.workspace_channels(niche_id)}
        self.assertEqual(roles.get(mine), "owned")


class AutoDropTests(_StoreTest):
    def test_dead_competitor_dropped_but_active_kept(self) -> None:
        dead = "UC" + "d" * 22
        alive = "UC" + "e" * 22
        self._index_channel(dead, "Dead Channel", last_upload_days=400)
        self._index_channel(alive, "Alive Channel", last_upload_days=3)
        svc = NicheWorkspaceService(self.store)
        nid = svc.create_workspace("drop-test")
        svc.add_channel(nid, dead, role="competitor")
        svc.add_channel(nid, alive, role="competitor")
        summary = sync_competitors(self.store, nid, refresh=False, snapshot=False,
                                   drop_inactive_days=120)
        self.assertIn("Dead Channel", summary["dropped"])
        remaining = {r["channel_id"] for r in svc.workspace_channels(nid)
                     if r["role"] == "competitor"}
        self.assertNotIn(dead, remaining)
        self.assertIn(alive, remaining)


class DedupTests(_StoreTest):
    def test_min_interval_skips_immediate_second_snapshot(self) -> None:
        cid = "UC" + "f" * 22
        self._index_channel(cid, "Dedup Channel")
        metrics = ChannelMetricsService(self.store)
        self.assertTrue(metrics.capture_channel(cid))                       # writes
        self.assertEqual(metrics.capture_channel(cid, min_interval_hours=20), 0)  # skipped
        self.assertEqual(len(metrics.channel_history(cid)), 1)


class WorkspaceSnapshotBatchTests(_StoreTest):
    def _workspace(self) -> tuple[int, list[str]]:
        channel_ids = ["UC" + "h" * 22, "UC" + "i" * 22]
        svc = NicheWorkspaceService(self.store)
        niche_id = svc.create_workspace("bulk-snapshots")
        for index, channel_id in enumerate(channel_ids, 1):
            self._index_channel(channel_id, f"Bulk Channel {index}")
            svc.add_channel(niche_id, channel_id, role="competitor")
        return niche_id, channel_ids

    def test_workspace_capture_uses_bulk_rows_not_per_channel_recapture(self) -> None:
        niche_id, channel_ids = self._workspace()
        metrics = ChannelMetricsService(self.store)

        with patch.object(
            metrics,
            "capture_channel",
            side_effect=AssertionError("workspace capture must stay batched"),
        ):
            captured = metrics.capture_workspace(niche_id)

        self.assertEqual(captured, 2)
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT channel_id, source FROM channel_snapshots ORDER BY channel_id"
            ).fetchall()
        self.assertEqual([row["channel_id"] for row in rows], sorted(channel_ids))
        self.assertTrue(all(row["source"] == "local-index" for row in rows))

    def test_workspace_interval_checks_all_members_in_one_pass(self) -> None:
        niche_id, channel_ids = self._workspace()
        metrics = ChannelMetricsService(self.store)
        self.assertEqual(metrics.capture_workspace(niche_id), 2)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE channel_snapshots SET captured_at=? WHERE channel_id=?",
                (_iso(2), channel_ids[0]),
            )

        self.assertEqual(
            metrics.capture_workspace(niche_id, min_interval_hours=20),
            1,
        )
        with self.store.connect() as conn:
            counts = {
                row["channel_id"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT channel_id, COUNT(*) AS count
                    FROM channel_snapshots
                    GROUP BY channel_id
                    """
                )
            }
        self.assertEqual(counts[channel_ids[0]], 2)
        self.assertEqual(counts[channel_ids[1]], 1)


class WorkspaceAlertCadenceTests(_StoreTest):
    def test_workspace_alerts_run_only_when_interval_is_due(self) -> None:
        self.assertTrue(_workspace_alerts_due(self.store, 60))
        self.store.record_enrichment_attempt(
            "workspace_alerts", ["all"], status="ok")
        self.assertFalse(_workspace_alerts_due(self.store, 60))
        self.assertTrue(_workspace_alerts_due(self.store, 0))

        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE enrichment_log
                SET last_attempt_at=?
                WHERE entity_type='workspace_alerts' AND entity_id='all'
                """,
                (_iso(1),),
            )
        self.assertTrue(_workspace_alerts_due(self.store, 60))

    def test_workspace_alert_interval_loads_from_config(self) -> None:
        config_path = Path(self.tmp.name) / "radar.json"
        config_path.write_text(
            json.dumps({"workspace_alert_interval_minutes": 60}),
            encoding="utf-8",
        )
        self.assertEqual(
            load_config(config_path).workspace_alert_interval_minutes,
            60,
        )


class AutonomousToggleTests(_StoreTest):
    def test_toggle_off_excludes_niche_from_radar_upkeep(self) -> None:
        svc = NicheWorkspaceService(self.store)
        on_id = svc.create_workspace("kept-fresh")
        off_id = svc.create_workspace("paused")
        svc.set_autonomous(off_id, False)
        listed = {w["id"]: int(w.get("autonomous", 1) or 0)
                  for w in svc.list_workspaces()}
        self.assertEqual(listed[on_id], 1)      # default: agent works on it
        self.assertEqual(listed[off_id], 0)     # opted out
        # The radar's selection rule: only autonomous niches get upkeep.
        upkeep = [nid for nid, auto in listed.items() if auto]
        self.assertIn(on_id, upkeep)
        self.assertNotIn(off_id, upkeep)
        # get_workspace carries the flag too (drives the UI switch).
        self.assertEqual(int(svc.get_workspace(off_id)["autonomous"]), 0)
        svc.set_autonomous(off_id, True)        # reversible
        self.assertEqual(int(svc.get_workspace(off_id)["autonomous"]), 1)


class AgentToolTests(_StoreTest):
    def test_list_and_untrack_via_analyst_tools(self) -> None:
        from beacon_engine.intelligence.analyst_tools import IntelligenceAnalystTools
        comp = "UC" + "g" * 22
        self._index_channel(comp, "Tool Competitor")
        svc = NicheWorkspaceService(self.store)
        nid = svc.create_workspace("tool-test")
        svc.add_channel(nid, comp, role="competitor")
        tools = IntelligenceAnalystTools(self.store)
        listed = tools.list_competitors(nid)
        self.assertEqual(len(listed), 1)
        self.assertIn("growth", listed[0])
        tools.untrack_competitor(nid, comp)
        self.assertEqual(len(tools.list_competitors(nid)), 0)


if __name__ == "__main__":
    unittest.main()
