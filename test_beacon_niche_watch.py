from __future__ import annotations

import unittest
from datetime import datetime, timezone

from beacon_app import CH_PAGE, BeaconApp, _niche_watch_state


class _Finder:
    def __init__(self, result=None) -> None:
        self.result = {"changed": 1} if result is None else result
        self.calls = []

    def call(self, name, **args):
        self.calls.append((name, args))
        return self.result


class BeaconNicheWatchStateTests(unittest.TestCase):
    def test_fresh_workspace_activity_does_not_make_old_alerts_look_current(self) -> None:
        state = _niche_watch_state(
            {
                "signals": [{
                    "detected_at": "2026-08-07T03:19:42Z",
                }],
                "workspaces": [{
                    "latest_beacon_at": "2026-08-07T03:19:42Z",
                    "last_activity_at": "2026-08-17T14:30:00Z",
                }],
                "signal_count": 12,
                "high_signal_count": 3,
            },
            now=datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(state["freshness"], "fresh_evidence_stale_alerts")
        self.assertEqual(state["signal_count"], 12)
        self.assertEqual(state["high_signal_count"], 3)

    def test_beacon_source_and_evidence_url_are_human_readable(self) -> None:
        signal = {
            "entity_type": "operator",
            "entity_id": "hermes:brief",
            "evidence": {
                "source": "hermes",
                "url": "https://www.youtube.com/watch?v=abc",
            },
        }

        self.assertEqual(BeaconApp._niche_signal_source(signal), "Hermes")
        self.assertEqual(
            BeaconApp._niche_signal_url(signal),
            "https://www.youtube.com/watch?v=abc",
        )


class BeaconNicheWatchActionTests(unittest.TestCase):
    def _app(self, result=None) -> BeaconApp:
        app = BeaconApp.__new__(BeaconApp)
        app.client = _Finder(result)
        app.cache = {
            "niche_signals": {"data": {}},
            "niche_signals:4": {"data": {}},
            "niches": {"data": []},
        }
        app._loading = set()
        app._epoch = {}
        app._rechecked = set()
        app._niche_actions = set()
        app._last_read_signal = None
        app.niche_signal_shown = 6
        app.niche_workspace_expanded = False
        app.section = "Overview"
        app.ch_niche = None
        app.ch_view = "index"
        app.ch_shown = 0
        app._render_count = 0
        app._messages = []
        app._render = lambda: setattr(  # type: ignore[method-assign]
            app, "_render_count", app._render_count + 1
        )
        app.toast = app._messages.append  # type: ignore[method-assign]

        def run_now(fn, on_done, _msg):
            on_done(fn())

        app.run_bg = run_now  # type: ignore[method-assign]
        return app

    def test_mark_read_uses_finder_and_invalidates_every_signal_view(self) -> None:
        app = self._app({"changed": 1, "read": True})

        app._mark_niche_signal_read({"id": 19, "niche_id": 4})

        self.assertEqual(
            app.client.calls,
            [("mark_niche_alert_read", {"alert_ids": [19], "read": True})],
        )
        self.assertNotIn("niche_signals", app.cache)
        self.assertNotIn("niche_signals:4", app.cache)
        self.assertEqual(app._messages[-1], "Notification marked read.")
        self.assertEqual(app._last_read_signal["id"], 19)

    def test_undo_last_read_restores_the_notification(self) -> None:
        app = self._app({"changed": 1, "read": False})
        app._last_read_signal = {"id": 19, "niche_id": 4}

        app._undo_last_niche_signal_read()

        self.assertEqual(
            app.client.calls,
            [("mark_niche_alert_read", {"alert_ids": [19], "read": False})],
        )
        self.assertIsNone(app._last_read_signal)
        self.assertEqual(app._messages[-1], "Notification restored to unread.")

    def test_show_more_signals_grows_the_request_and_invalidates_cache(self) -> None:
        app = self._app()

        app._show_more_niche_signals()

        self.assertEqual(app.niche_signal_shown, 12)
        self.assertNotIn("niche_signals", app.cache)

    def test_workspace_runway_can_expand_and_collapse(self) -> None:
        app = self._app()

        app._toggle_niche_workspaces()
        self.assertTrue(app.niche_workspace_expanded)
        app._toggle_niche_workspaces()
        self.assertFalse(app.niche_workspace_expanded)

    def test_pause_workspace_is_reversible_and_uses_full_finder_write(self) -> None:
        app = self._app({"niche_id": 4, "autonomous": False})

        app._set_niche_autonomous({"id": 4, "name": "Science"}, False)

        self.assertEqual(
            app.client.calls,
            [("set_niche_autonomous", {"niche_id": 4, "enabled": False})],
        )
        self.assertIn("resume it anytime", app._messages[-1])
        self.assertNotIn("niches", app.cache)

    def test_open_workspace_switches_to_channels_niche_view(self) -> None:
        app = self._app()

        app._open_niche_workspace({"id": 4, "name": "Science"})

        self.assertEqual(app.section, "Channels")
        self.assertEqual(app.ch_view, "niche")
        self.assertEqual(app.ch_niche["id"], 4)
        self.assertEqual(app.ch_shown, CH_PAGE)      # back to the first page


if __name__ == "__main__":
    unittest.main()
