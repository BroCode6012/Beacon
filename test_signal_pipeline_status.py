from __future__ import annotations

import unittest

from signal_app import _pipeline_progress_state


class SignalPipelineStatusTests(unittest.TestCase):
    def test_split_pipeline_reports_both_lanes_and_durable_backlog(self) -> None:
        state = _pipeline_progress_state({
            "pipeline_mode": "split",
            "discovery": {
                "state": "sleeping",
                "message": "next search in 60s",
            },
            "organizer": {
                "state": "running",
                "message": "organizing queued channels",
            },
            "queue": {
                "pending_batches": 2,
                "pending_channels": 45,
            },
        })

        self.assertEqual(state["discovery"]["state"], "sleeping")
        self.assertEqual(state["organizer"]["state"], "running")
        self.assertEqual(state["pending_channels"], 45)
        self.assertIn("45 newly found channels", state["queue_text"])
        self.assertIn("reopening resumes them", state["queue_text"])

    def test_empty_split_queue_has_plain_language_state(self) -> None:
        state = _pipeline_progress_state({"pipeline_mode": "split"})

        self.assertEqual(state["pending_channels"], 0)
        self.assertEqual(state["discovery"]["state"], "starting")
        self.assertIn("queue is clear", state["queue_text"])

    def test_legacy_worker_does_not_show_split_tracker(self) -> None:
        self.assertIsNone(_pipeline_progress_state({"state": "running"}))


if __name__ == "__main__":
    unittest.main()
