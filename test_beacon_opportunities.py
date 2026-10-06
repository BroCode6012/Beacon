from __future__ import annotations

import unittest

import flet as ft

from beacon_agent import BeaconAgent
from beacon_app import BeaconApp


class BeaconOpportunityQueryTests(unittest.TestCase):
    def test_find_sweep_summary_separates_research_leads_from_opportunities(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app._find_sweep_result = {
            "subject": "paper crafts", "queries_run": 100, "videos_found": 24,
            "channels_discovered": 3, "errors": 2,
            "top_channels": [{"channel_id": "UCexample", "title": "Paper channel",
                              "niche_hits": 4}],
        }
        app._find_sweep_error = None
        app._open_url = lambda _url: lambda _event: None

        def labels(control) -> list[str]:
            found = [control.value] if isinstance(control, ft.Text) else []
            if isinstance(control, ft.TextButton):
                found.append(control.text or "")
            for child in getattr(control, "controls", ()) or ():
                found.extend(labels(child))
            child = getattr(control, "content", None)
            if child is not None:
                found.extend(labels(child))
            return found

        summary = " ".join(labels(app._find_sweep_summary()))
        self.assertIn("Sweep results for paper crafts", summary)
        self.assertIn("2 search angles failed", summary)
        self.assertIn("have not yet passed", summary)
        self.assertIn("Paper channel · 4 matches", summary)

    def test_saved_card_does_not_claim_an_on_camera_channel_qualifies(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app._avatar = lambda *_args: ft.Container()
        app._open_url = lambda _url: lambda _event: None
        app._shell = lambda body, **_kwargs: body
        app.pill = lambda label, *_args: ft.Text(label)
        row = {
            "title": "On-camera example", "channel_id": "UCexample",
            "faceless": False, "opportunity_score": 4.6,
            "video_count": 20, "avg_views": 10000,
            "subscribers": 2000, "days_since_start": 100,
        }

        def labels(control) -> list[str]:
            found = [control.value] if isinstance(control, ft.Text) else []
            for child in getattr(control, "controls", ()) or ():
                found.extend(labels(child))
            child = getattr(control, "content", None)
            if child is not None:
                found.extend(labels(child))
            return found

        saved_text = " ".join(labels(app.card_grid([row], "channel", saved=True)))
        ranked_text = " ".join(labels(app.card_grid([row], "channel")))
        self.assertIn("Saved · review fit", saved_text)
        self.assertIn("Performance signals", saved_text)
        self.assertIn("has not passed", saved_text)
        self.assertNotIn("Why it qualifies", saved_text)
        self.assertIn("Why it qualifies", ranked_text)

    def test_channels_query_always_enforces_replicable_faceless_gate(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app.ch_filters = {"sort": "opportunity_score"}
        app.ch_niche = None
        app.ch_view = "index"
        app.ch_topic = None
        app.ch_shown = 120

        args = app._ch_query_args()

        self.assertTrue(args["replicable_only"])
        self.assertTrue(args["faceless_only"])
        self.assertEqual(args["sort"], "opportunity_score")

    def test_realtime_query_always_enforces_replicable_faceless_gate(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app.rt_filters = {}
        app.rt_topic = None
        app.rt_window = 1
        app.rt_shown = 120

        args = app._rt_query_args()

        self.assertTrue(args["replicable_only"])
        self.assertTrue(args["faceless_only"])
        self.assertEqual(args["days"], 1)


class _Finder:
    def __init__(self) -> None:
        self.calls = []

    def call(self, name, **args):
        self.calls.append((name, args))
        return []


class BeaconAgentOpportunityTests(unittest.TestCase):
    def test_agent_channel_reads_are_always_replicable_and_faceless(self) -> None:
        finder = _Finder()
        agent = BeaconAgent(finder, sweep=object())

        agent._dispatch("find_channels", {"faceless_only": False, "limit": 5})

        name, args = finder.calls[-1]
        self.assertEqual(name, "find_channels")
        self.assertTrue(args["replicable_only"])
        self.assertTrue(args["faceless_only"])
        self.assertEqual(args["sort"], "opportunity_score")


if __name__ == "__main__":
    unittest.main()
