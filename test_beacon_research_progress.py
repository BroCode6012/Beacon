from __future__ import annotations

import unittest

from beacon_app import _research_progress_state


class BeaconResearchProgressTests(unittest.TestCase):
    def test_backlog_tracker_shows_exact_tested_and_remaining_counts(self) -> None:
        state = _research_progress_state({
            "count": 422,
            "max_total": 600,
            "expanded_queries": 4_143,
            "searched_queries": 1_896,
            "can_add_autonomously": False,
            "block_reason": "existing_query_backlog",
        })

        self.assertEqual(state["base_ideas"], 422)
        self.assertEqual(state["total"], 4_143)
        self.assertEqual(state["tested"], 1_896)
        self.assertEqual(state["remaining"], 2_247)
        self.assertEqual(state["coverage_pct"], 45.8)
        self.assertAlmostEqual(state["ratio"], 1_896 / 4_143)
        self.assertIn("2,247 remaining searches", state["status"])

    def test_tracker_clamps_inconsistent_counts_to_a_valid_progress_range(self) -> None:
        state = _research_progress_state({
            "expanded_queries": 10,
            "searched_queries": 15,
            "can_add_autonomously": True,
        })

        self.assertEqual(state["total"], 15)
        self.assertEqual(state["tested"], 15)
        self.assertEqual(state["remaining"], 0)
        self.assertEqual(state["ratio"], 1.0)
        self.assertEqual(state["coverage_pct"], 100.0)
        self.assertIn("may add up to 2", state["status"])

    def test_tracker_never_labels_an_incomplete_pass_as_100_percent(self) -> None:
        state = _research_progress_state({
            "expanded_queries": 4_164,
            "searched_queries": 4_162,
        })

        self.assertEqual(state["remaining"], 2)
        self.assertEqual(state["coverage_pct"], 99.95)
        self.assertEqual(state["coverage_label"], "99.95")

    def test_cooldown_status_is_plain_language(self) -> None:
        state = _research_progress_state({
            "expanded_queries": 20,
            "searched_queries": 20,
            "block_reason": "cooldown_active",
            "cooldown_remaining_minutes": 1,
        })

        self.assertIn("1 more minute", state["status"])


if __name__ == "__main__":
    unittest.main()
