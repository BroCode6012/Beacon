from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from beacon_engine.research.radar import (
    RadarCycleResult,
    _select_harvest_queries,
    _write_lane_status,
    _run_split_pipeline,
    load_config,
    read_status,
    run_discovery_cycle,
    run_worker,
)
from beacon_engine.research.radar_queue import RadarWorkQueue


class RadarWorkQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = RadarWorkQueue(self.root / "radar_work_queue.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_queue_coalesces_pending_discovery_batches(self) -> None:
        self.queue.enqueue_organization(12, payload={"cycle": 1})
        self.queue.enqueue_organization(8, payload={"cycle": 2})

        batch = self.queue.claim_organization(
            "worker-a",
            now="2026-07-11T10:00:00Z",
        )

        self.assertIsNotNone(batch)
        self.assertEqual(batch.jobs, 2)
        self.assertEqual(batch.channel_count, 20)
        self.assertEqual(self.queue.stats()["pending_channels"], 20)
        self.assertEqual(self.queue.stats()["queued_channels"], 0)
        self.assertEqual(self.queue.stats()["running_channels"], 20)
        self.assertEqual(self.queue.complete(batch), 2)
        self.assertEqual(self.queue.stats()["pending_batches"], 0)

    def test_expired_lease_is_recovered_after_restart(self) -> None:
        self.queue.enqueue_organization(
            5,
            now="2026-07-11T10:00:00Z",
        )
        first = self.queue.claim_organization(
            "worker-a",
            lease_seconds=60,
            now="2026-07-11T10:00:00Z",
        )
        reopened = RadarWorkQueue(self.queue.path)

        second = reopened.claim_organization(
            "worker-b",
            now="2026-07-11T10:02:00Z",
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(second.channel_count, 5)
        self.assertEqual(second.worker, "worker-b")

    def test_zero_channel_discovery_does_not_create_work(self) -> None:
        self.assertIsNone(self.queue.enqueue_organization(0))
        self.assertEqual(self.queue.stats()["pending_batches"], 0)


class SplitRadarPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "radar.json"
        self.status_path = self.root / "status.json"
        self.stop_path = self.root / "stop.flag"
        self.pid_path = self.root / "radar.pid"
        self.db_path = self.root / "index.sqlite3"
        self.queue_path = self.root / "queue.sqlite3"
        self.config_path.write_text(
            json.dumps({
                "db_path": str(self.db_path),
                "work_queue_path": str(self.queue_path),
                "split_pipeline_enabled": True,
                "discovery_interval_minutes": 2,
                "watchlist": ["history documentaries"],
                "harvest_expand": False,
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_split_config_loads_without_changing_legacy_default(self) -> None:
        loaded = load_config(self.config_path)
        self.assertTrue(loaded.split_pipeline_enabled)
        self.assertEqual(loaded.discovery_interval_minutes, 2)
        self.assertFalse(load_config(self.root / "missing.json").split_pipeline_enabled)

    def test_never_attempted_query_is_pulled_into_the_next_slice(self) -> None:
        queries = [f"query {index}" for index in range(100)]
        state = {
            query: {"attempts": 1, "zero_streak": 0}
            for query in queries
            if query != "query 90"
        }

        selected, next_cursor = _select_harvest_queries(
            queries,
            state,
            cursor=0,
            take=10,
            retire_after=3,
            reprobe=False,
        )

        self.assertIn("query 90", selected)
        self.assertEqual(len(selected), 10)
        self.assertEqual(next_cursor, 10)

    def test_discovery_status_reports_query_topup_result(self) -> None:
        config = load_config(self.config_path)
        config.query_topup_enabled = True
        topup = {
            "added": ["animal survival strategies"],
            "reason": "added",
            "coverage": 1.0,
            "remaining": 0,
        }

        with patch(
            "beacon_engine.research.radar._harvest_slice",
            return_value=0,
        ), patch(
            "beacon_engine.research.query_topup.maybe_top_up_queries",
            return_value=topup,
        ):
            result = run_discovery_cycle(
                config,
                RadarWorkQueue(self.queue_path),
                config_path=self.config_path,
                status_path=self.status_path,
                stop_path=self.stop_path,
                cycle=7,
            )

        status = read_status(self.status_path)
        discovery = status["discovery"]
        self.assertEqual(result, 0)
        self.assertEqual(discovery["queries_added"], 1)
        self.assertEqual(
            discovery["query_additions"],
            ["animal survival strategies"],
        )
        self.assertEqual(discovery["query_topup_reason"], "added")
        self.assertEqual(discovery["query_coverage"], 1.0)
        self.assertEqual(discovery["query_searches_remaining"], 0)

    def test_once_runs_discovery_then_organizer_and_drains_queue(self) -> None:
        def fake_discovery(config, queue, **_kwargs):
            queue.enqueue_organization(7, payload={"test": True})
            return 7

        organizer_calls = []

        def fake_organizer(config, **kwargs):
            organizer_calls.append((config, kwargs))
            return RadarCycleResult()

        with patch(
            "beacon_engine.research.radar.run_discovery_cycle",
            side_effect=fake_discovery,
        ), patch(
            "beacon_engine.research.radar.run_cycle",
            side_effect=fake_organizer,
        ):
            result = run_worker(
                config_path=self.config_path,
                status_path=self.status_path,
                stop_path=self.stop_path,
                pid_path=self.pid_path,
                once=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(organizer_calls), 1)
        self.assertFalse(organizer_calls[0][0].harvest_enabled)
        self.assertTrue(organizer_calls[0][1]["organization_only"])
        self.assertEqual(
            RadarWorkQueue(self.queue_path).stats()["pending_batches"],
            0,
        )
        self.assertFalse(self.pid_path.exists())

    def test_empty_queue_runs_full_maintenance_cycle(self) -> None:
        organizer_calls = []

        def fake_discovery(_config, _queue, **_kwargs):
            return 0

        def fake_organizer(config, **kwargs):
            organizer_calls.append((config, kwargs))
            return RadarCycleResult()

        with patch(
            "beacon_engine.research.radar.run_discovery_cycle",
            side_effect=fake_discovery,
        ), patch(
            "beacon_engine.research.radar.run_cycle",
            side_effect=fake_organizer,
        ):
            result = run_worker(
                config_path=self.config_path,
                status_path=self.status_path,
                stop_path=self.stop_path,
                pid_path=self.pid_path,
                once=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(organizer_calls), 1)
        self.assertFalse(organizer_calls[0][1]["organization_only"])

    def test_disabled_local_harvest_keeps_empty_queue_in_organization_mode(
        self,
    ) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["harvest_enabled"] = False
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        organizer_calls = []

        def fake_discovery(_config, _queue, **_kwargs):
            return 0

        def fake_organizer(config, **kwargs):
            organizer_calls.append((config, kwargs))
            return RadarCycleResult()

        with patch(
            "beacon_engine.research.radar.run_discovery_cycle",
            side_effect=fake_discovery,
        ), patch(
            "beacon_engine.research.radar.run_cycle",
            side_effect=fake_organizer,
        ):
            result = run_worker(
                config_path=self.config_path,
                status_path=self.status_path,
                stop_path=self.stop_path,
                pid_path=self.pid_path,
                once=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(organizer_calls), 1)
        self.assertTrue(organizer_calls[0][1]["organization_only"])

    def test_organizer_exception_stops_discovery_before_joining(self) -> None:
        errors = []

        def fake_discovery(_config, _queue, **_kwargs):
            return 0

        def fake_organizer(_config, **_kwargs):
            return RadarCycleResult()

        def fake_wait(
            _seconds,
            stop_path,
            *,
            tick=None,
            stop_event=None,
        ):
            if threading.current_thread().name == "radar-discovery":
                while (
                    not Path(stop_path).exists()
                    and not (stop_event is not None and stop_event.is_set())
                ):
                    time.sleep(0.01)
                return
            raise RuntimeError("organizer tick failed")

        def run_pipeline():
            try:
                _run_split_pipeline(
                    config_path=self.config_path,
                    status_path=self.status_path,
                    stop_path=self.stop_path,
                    once=False,
                    on_step=lambda _message: None,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with patch(
            "beacon_engine.research.radar.run_discovery_cycle",
            side_effect=fake_discovery,
        ), patch(
            "beacon_engine.research.radar.run_cycle",
            side_effect=fake_organizer,
        ), patch(
            "beacon_engine.research.radar._wait_for_stop",
            side_effect=fake_wait,
        ):
            worker = threading.Thread(target=run_pipeline, daemon=True)
            worker.start()
            worker.join(timeout=2)
            still_alive = worker.is_alive()
            if still_alive:
                self.stop_path.write_text("cleanup", encoding="utf-8")
                worker.join(timeout=2)

        self.assertFalse(still_alive)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_lane_status_preserves_both_workers_and_queue_counts(self) -> None:
        _write_lane_status(
            self.status_path,
            "discovery",
            "sleeping",
            queue_stats={"pending_batches": 2, "pending_channels": 45},
            message="next search in 60s",
        )
        _write_lane_status(
            self.status_path,
            "organizer",
            "running",
            queue_stats={"pending_batches": 2, "pending_channels": 45},
            message="organizing queued channels",
        )

        status = read_status(self.status_path)
        self.assertEqual(status["pipeline_mode"], "split")
        self.assertEqual(status["discovery"]["state"], "sleeping")
        self.assertEqual(status["organizer"]["state"], "running")
        self.assertEqual(status["queue"]["pending_channels"], 45)
        self.assertIn("Discovery:", status["message"])
        self.assertIn("Organizer:", status["message"])

    def test_lane_summary_prefers_fresh_message_over_stale_query(self) -> None:
        _write_lane_status(
            self.status_path,
            "organizer",
            "running",
            current_query="stale niche query",
            message="scanning niche",
        )
        _write_lane_status(
            self.status_path,
            "organizer",
            "running",
            message="organizing channel backlog",
        )

        status = read_status(self.status_path)
        self.assertIn(
            "Organizer: organizing channel backlog",
            status["message"],
        )

    def test_continuous_startup_claims_first_discovery_before_organizer(self) -> None:
        order = []

        def fake_discovery(config, queue, **_kwargs):
            time.sleep(0.05)
            queue.enqueue_organization(9, payload={"test": True})
            order.append("discovery")
            return 9

        def fake_organizer(config, **_kwargs):
            order.append("organizer")
            threading.Timer(
                0.05,
                lambda: self.stop_path.write_text("stop", encoding="utf-8"),
            ).start()
            return RadarCycleResult()

        with patch(
            "beacon_engine.research.radar.run_discovery_cycle",
            side_effect=fake_discovery,
        ), patch(
            "beacon_engine.research.radar.run_cycle",
            side_effect=fake_organizer,
        ):
            cycles = _run_split_pipeline(
                config_path=self.config_path,
                status_path=self.status_path,
                stop_path=self.stop_path,
                once=False,
                on_step=lambda _message: None,
            )

        self.assertEqual(cycles, 1)
        self.assertEqual(order, ["discovery", "organizer"])
        self.assertEqual(
            RadarWorkQueue(self.queue_path).stats()["pending_batches"],
            0,
        )

    def test_stop_during_organizer_returns_claimed_work_to_queue(self) -> None:
        def fake_discovery(config, queue, **_kwargs):
            queue.enqueue_organization(11, payload={"test": True})
            return 11

        def interrupted_organizer(config, **_kwargs):
            self.stop_path.write_text("stop", encoding="utf-8")
            return RadarCycleResult()

        with patch(
            "beacon_engine.research.radar.run_discovery_cycle",
            side_effect=fake_discovery,
        ), patch(
            "beacon_engine.research.radar.run_cycle",
            side_effect=interrupted_organizer,
        ):
            cycles = _run_split_pipeline(
                config_path=self.config_path,
                status_path=self.status_path,
                stop_path=self.stop_path,
                once=True,
                on_step=lambda _message: None,
            )

        stats = RadarWorkQueue(self.queue_path).stats()
        self.assertEqual(cycles, 1)
        self.assertEqual(stats["queued_batches"], 1)
        self.assertEqual(stats["running_batches"], 0)
        self.assertEqual(stats["queued_channels"], 11)


if __name__ == "__main__":
    unittest.main()
