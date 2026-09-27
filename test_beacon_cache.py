from __future__ import annotations

import threading
import time
import unittest

from beacon_agent import BeaconAgent
from beacon_app import BeaconApp


class BeaconCacheTests(unittest.TestCase):
    def _app(self) -> BeaconApp:
        app = BeaconApp.__new__(BeaconApp)
        app.cache = {}
        app._loading = set()
        app._epoch = {}
        app._rechecked = set()
        app.section = "Channels"
        app._render = lambda: None  # type: ignore[method-assign]

        def run_now(fn, on_done, _msg, **_kwargs):
            on_done(fn())

        app.run_bg = run_now  # type: ignore[method-assign]
        return app

    def test_fresh_entry_is_reused_without_loading(self) -> None:
        app = self._app()
        app.cache["topics"] = app._cache_entry({"data": ["fresh"]})
        calls = []

        entry = app._ensure("topics", lambda: calls.append(True), "Loading topics")

        self.assertEqual(entry["data"], ["fresh"])
        self.assertEqual(calls, [])

    def test_stale_entry_stays_visible_while_replaced(self) -> None:
        app = self._app()
        stale = app._cache_entry({"data": ["old"]})
        stale["_cached_at"] = time.monotonic() - (app._cache_ttl("topics") + 1)
        app.cache["topics"] = stale

        entry = app._ensure("topics", lambda: ["new"], "Refreshing topics")

        self.assertIs(entry, stale)
        self.assertEqual(app.cache["topics"]["data"], ["new"])
        self.assertGreater(app.cache["topics"]["_cached_at"], stale["_cached_at"])

    def test_channels_and_realtime_refresh_invalidate_topic_counts(self) -> None:
        for section, active_key in (
            ("Channels", "channels:sort=opportunity_score"),
            ("Real Time", "realtime:days=1"),
        ):
            with self.subTest(section=section):
                app = self._app()
                app.section = section
                app.cache = {
                    "topics": app._cache_entry({"data": []}),
                    "niches": app._cache_entry({"data": []}),
                    active_key: app._cache_entry({"data": []}),
                }
                app._channels_key = lambda: active_key  # type: ignore[method-assign]
                app._rt_key = lambda: active_key  # type: ignore[method-assign]

                app._refresh()

                self.assertNotIn("topics", app.cache)
                self.assertNotIn(active_key, app.cache)
                if section == "Channels":
                    self.assertNotIn("niches", app.cache)


class _Finder:
    def __init__(self, result=None) -> None:
        self.result = {"changed": 1} if result is None else result

    def call(self, _name, **_args):
        return self.result


class _Ui:
    def __init__(self) -> None:
        self.changed = []

    def agent_data_changed(self, name) -> None:
        self.changed.append(name)


class BeaconAgentCacheInvalidationTests(unittest.TestCase):
    def test_successful_write_notifies_ui_but_read_does_not(self) -> None:
        ui = _Ui()
        agent = BeaconAgent(_Finder(), sweep=object(), ui=ui)

        agent._dispatch("run_junk_filter", {})
        agent._dispatch("list_topics", {})

        self.assertEqual(ui.changed, ["run_junk_filter"])

    def test_failed_write_does_not_invalidate_good_cached_data(self) -> None:
        ui = _Ui()
        agent = BeaconAgent(_Finder({"error": "no change"}), sweep=object(), ui=ui)

        agent._dispatch("reclassify_topics", {"only_missing": True})

        self.assertEqual(ui.changed, [])


if __name__ == "__main__":
    unittest.main()
