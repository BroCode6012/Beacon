from __future__ import annotations

import unittest

from signal_agent import SignalAgent
from signal_app import SignalApp


class SignalOpportunityQueryTests(unittest.TestCase):
    def test_channels_query_always_enforces_replicable_faceless_gate(self) -> None:
        app = SignalApp.__new__(SignalApp)
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
        app = SignalApp.__new__(SignalApp)
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


class SignalAgentOpportunityTests(unittest.TestCase):
    def test_agent_channel_reads_are_always_replicable_and_faceless(self) -> None:
        finder = _Finder()
        agent = SignalAgent(finder, sweep=object())

        agent._dispatch("find_channels", {"faceless_only": False, "limit": 5})

        name, args = finder.calls[-1]
        self.assertEqual(name, "find_channels")
        self.assertTrue(args["replicable_only"])
        self.assertTrue(args["faceless_only"])
        self.assertEqual(args["sort"], "opportunity_score")


if __name__ == "__main__":
    unittest.main()
