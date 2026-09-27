from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine_link  # noqa: F401
import sweep_control
from beacon_agent import BeaconAgent


class BeaconQueryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.watchlist = root / "scraper_watchlist.json"
        self.yields = root / "harvest_yield.json"
        self.watch_patch = patch.object(sweep_control, "WATCHLIST_PATH", self.watchlist)
        self.yield_patch = patch.object(sweep_control, "HARVEST_YIELD_PATH", self.yields)
        self.watch_patch.start()
        self.yield_patch.start()

    def tearDown(self) -> None:
        self.yield_patch.stop()
        self.watch_patch.stop()
        self.tmp.cleanup()

    def _write(self, queries, **extra) -> None:
        data = {
            "watchlist": list(queries),
            "harvest_expand": False,
            "harvest_per_cycle": 30,
            "harvest_retire_after": 3,
            **extra,
        }
        self.watchlist.write_text(json.dumps(data), encoding="utf-8")
        self.yields.write_text("{}", encoding="utf-8")

    def test_autonomous_addition_is_blocked_while_existing_backlog_is_large(self) -> None:
        self._write([f"topic {i}" for i in range(100)])

        result = sweep_control.add_watchlist_queries(
            ["fresh useful topic"], autonomous=True, now=1_000_000)

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "existing_query_backlog")
        self.assertFalse(result["policy"]["can_add_autonomously"])

    def test_family_and_near_duplicate_variants_are_rejected(self) -> None:
        self._write([
            "science discoveries explained faceless 2026",
            "wildlife documentary full episode faceless",
        ])

        result = sweep_control.add_watchlist_queries([
            "science discoveries documentary 2025",
            "wildlife nature documentary full episode",
        ], now=1_000_000)

        self.assertEqual(result["added"], 0)
        self.assertEqual(
            [item["reason"] for item in result["rejected"]],
            ["family_budget_reached", "near_duplicate"],
        )

    def test_autonomous_call_adds_at_most_two_and_persists_cooldown(self) -> None:
        self._write(["existing useful topic"])

        first = sweep_control.add_watchlist_queries([
            "ancient engineering methods",
            "ocean survival systems",
            "forgotten trade routes",
        ], autonomous=True, now=1_000_000)
        second = sweep_control.add_watchlist_queries(
            ["remote village economies"], autonomous=True, now=1_000_000 + 60)

        self.assertEqual(first["accepted"], [
            "ancient engineering methods", "ocean survival systems"])
        self.assertEqual(first["rejected"][0]["reason"], "per_call_limit")
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["rejected"][0]["reason"], "cooldown_active")
        saved = json.loads(self.watchlist.read_text(encoding="utf-8"))
        policy = saved[sweep_control.QUERY_POLICY_KEY]
        self.assertEqual(policy["last_source"], "autopilot")
        self.assertEqual(len(policy["recent_additions"]), 2)

    def test_capacity_is_a_hard_limit(self) -> None:
        self._write([f"topic {i}" for i in range(sweep_control.QUERY_MAX_TOTAL)])

        result = sweep_control.add_watchlist_queries(
            ["one more useful topic"], now=1_000_000)

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "watchlist_at_capacity")

    def test_summary_uses_full_watchlist_and_measured_yield(self) -> None:
        self._write(["alpha useful topic", "beta useful topic"])
        self.yields.write_text(json.dumps({
            "alpha useful topic": {"searches": 2, "total_new": 6, "zero_streak": 0},
            "beta useful topic": {"searches": 1, "total_new": 0, "zero_streak": 1},
        }), encoding="utf-8")

        summary = sweep_control.watchlist_summary(now=1_000_000)

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["searched_queries"], 2)
        self.assertEqual(summary["coverage_pct"], 100.0)
        self.assertEqual(summary["top_yield_families"][0]["family"], "alpha useful topic")

    def test_summary_counts_successful_empty_attempt_as_tested(self) -> None:
        self._write(["alpha useful topic"])
        self.yields.write_text(json.dumps({
            "alpha useful topic": {
                "attempts": 1,
                "searches": 0,
                "total_new": 0,
                "zero_streak": 0,
            },
        }), encoding="utf-8")

        summary = sweep_control.watchlist_summary(now=1_000_000)

        self.assertEqual(summary["searched_queries"], 1)
        self.assertEqual(summary["unsearched_queries"], 0)


class _Sweep:
    def __init__(self) -> None:
        self.calls = []

    def watchlist_summary(self):
        return {"count": 10, "can_add_autonomously": False}

    def add_watchlist_queries(self, queries, **kwargs):
        self.calls.append((queries, kwargs))
        return {"accepted": [], "rejected": [], "added": 0, "total": 10}


class BeaconAgentQueryPolicyTests(unittest.TestCase):
    def test_autonomous_agent_passes_policy_mode_to_sweep_control(self) -> None:
        sweep = _Sweep()
        agent = BeaconAgent(
            finder=object(), sweep=sweep, allowed_tools={"add_watchlist_queries"},
            autonomous=True)

        agent._dispatch("add_watchlist_queries", {"queries": ["fresh useful topic"]})

        self.assertTrue(sweep.calls[0][1]["autonomous"])
        self.assertEqual(sweep.calls[0][1]["source"], "autopilot")


if __name__ == "__main__":
    unittest.main()
