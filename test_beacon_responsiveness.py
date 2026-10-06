"""Contracts that keep Beacon from "always loading".

Measured 2026-09-16 before these rules existed: the loading bar was on for 100% of a
2.5-minute session, the Channels page (10,163 elements) was redrawn three times while nobody
touched it, and the Overview counts took 144s because the database service shared the app's
Python process. Each test below pins one of the fixes.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import beacon_app
from beacon_app import BeaconApp


class _Page:
    def __init__(self) -> None:
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


class BrowserSessionTests(unittest.TestCase):
    def test_disconnect_stops_watchers_and_reconnect_starts_fresh_ones(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        old_stop = threading.Event()
        app._session_stop = old_stop
        app._overview_watch_started = True
        app._cloud_watch_started = True
        app._rt_watch_started = True
        app.autopilot = SimpleNamespace(enabled=True, shutdown=mock.Mock(), start=mock.Mock())
        app.content = object()
        app._render = mock.Mock()

        app._session_disconnected()
        self.assertTrue(old_stop.is_set())
        self.assertFalse(app._overview_watch_started)
        self.assertFalse(app._cloud_watch_started)
        self.assertFalse(app._rt_watch_started)
        app.autopilot.shutdown.assert_called_once()

        app._session_connected()
        self.assertIsNot(app._session_stop, old_stop)
        self.assertTrue(old_stop.is_set())  # old workers cannot revive on reconnect
        self.assertFalse(app._session_stop.is_set())
        app.autopilot.start.assert_called_once()
        app._render.assert_called_once()

    def test_closed_session_does_not_publish_background_redraw(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app._render_lock = threading.RLock()
        app._session_stop = threading.Event()
        app._session_stop.set()
        app.page = _Page()
        app.content = SimpleNamespace(controls=["existing"])
        BeaconApp._render(app)
        self.assertEqual(app.content.controls, ["existing"])
        self.assertEqual(app.page.updates, 0)


def _busy_app(section: str = "Channels") -> BeaconApp:
    app = BeaconApp.__new__(BeaconApp)
    app.page = _Page()
    app.section = section
    app.spinner = SimpleNamespace(visible=False)
    app.busy_bar = SimpleNamespace(visible=False)
    app.status = SimpleNamespace(value="", color=None)
    app._busy = False
    app._busy_lock = threading.Lock()
    app._render_lock = threading.RLock()
    app._busy_tasks = {}
    app._busy_seq = 0
    return app


class LoadingSignalTests(unittest.TestCase):
    def test_loading_during_render_does_not_publish_a_half_built_page(self) -> None:
        app = _busy_app("Find")
        app._rendering = True
        token = app._begin_busy("Loading channels…", "Find")
        self.assertEqual(app.page.updates, 0)
        app._end_busy(token)
        self.assertEqual(app.page.updates, 0)

    def test_another_screens_load_does_not_show_here(self) -> None:
        app = _busy_app("Channels")
        app._begin_busy("Loading index stats…", "Overview")
        self.assertFalse(app.busy_bar.visible)
        self.assertEqual(app.status.value, "")
        self.assertTrue(app._busy)                 # the work is still tracked

    def test_it_appears_when_you_go_to_that_screen_and_leaves_with_you(self) -> None:
        app = _busy_app("Channels")
        app._begin_busy("Loading index stats…", "Overview")
        app.section = "Overview"
        app._refresh_busy_state()
        self.assertTrue(app.busy_bar.visible)
        app.section = "Saved"
        app._refresh_busy_state()
        self.assertFalse(app.busy_bar.visible)

    def test_work_you_started_shows_on_every_screen(self) -> None:
        app = _busy_app("Channels")
        token = app._begin_busy("Saving channel…")
        app.section = "Agent"
        app._refresh_busy_state()
        self.assertTrue(app.busy_bar.visible)
        self.assertEqual(app.status.value, "Saving channel…")
        app._end_busy(token)
        self.assertFalse(app.busy_bar.visible)

    def test_a_quiet_job_never_shows_a_loading_signal(self) -> None:
        app = _busy_app()
        finished = threading.Event()
        app.run_bg(lambda: time.sleep(0.05), lambda _r: finished.set(), "Refreshing…",
                   quiet=True)
        self.assertFalse(app.busy_bar.visible)
        self.assertTrue(finished.wait(5))
        self.assertEqual(app._busy_tasks, {})


def _cache_app(section: str = "Channels") -> tuple[BeaconApp, list, list]:
    app = BeaconApp.__new__(BeaconApp)
    app.cache, app._loading, app._epoch, app._rechecked = {}, set(), {}, set()
    app.section = section
    app._render_lock = threading.RLock()
    app._section_keys = {}
    app._render_keys = None
    renders: list = []
    calls: list = []
    app._render = lambda: renders.append(app.section)  # type: ignore[method-assign]

    def run_now(fn, on_done, msg, **kwargs):
        calls.append((msg, kwargs))
        on_done(fn())

    app.run_bg = run_now  # type: ignore[method-assign]
    return app, renders, calls


class RedrawTests(unittest.TestCase):
    def test_first_load_shows_loading_for_this_screen_only(self) -> None:
        app, _renders, calls = _cache_app("Channels")
        app._section_keys["Channels"] = {"topics"}
        app._ensure("topics", lambda: ["History"], "Loading topics…")
        self.assertEqual(calls, [("Loading topics…", {"screen": "Channels"})])

    def test_refreshing_data_already_on_screen_is_quiet(self) -> None:
        app, _renders, calls = _cache_app()
        stale = app._cache_entry({"data": ["old"]})
        stale["_cached_at"] -= app._cache_ttl("topics") + 1
        app.cache["topics"] = stale
        app._ensure("topics", lambda: ["new"], "Loading topics…")
        self.assertEqual(calls[0][1], {"quiet": True})

    def test_an_identical_refresh_does_not_redraw(self) -> None:
        app, renders, _calls = _cache_app()
        app._section_keys["Channels"] = {"topics"}
        stale = app._cache_entry({"data": ["same"]})
        stale["_cached_at"] -= app._cache_ttl("topics") + 1
        app.cache["topics"] = stale
        app._ensure("topics", lambda: ["same"], "x")
        self.assertEqual(renders, [])

    def test_a_changed_refresh_on_screen_redraws(self) -> None:
        app, renders, _calls = _cache_app()
        app._section_keys["Channels"] = {"topics"}
        stale = app._cache_entry({"data": ["old"]})
        stale["_cached_at"] -= app._cache_ttl("topics") + 1
        app.cache["topics"] = stale
        app._ensure("topics", lambda: ["new"], "x")
        self.assertEqual(renders, ["Channels"])

    def test_data_for_another_screen_does_not_redraw_this_one(self) -> None:
        # The Overview counts finishing used to rebuild the whole Channels page.
        app, renders, _calls = _cache_app("Channels")
        app._section_keys = {"Channels": {"channels:x"}, "Overview": {"overview"}}
        app._ensure("overview", lambda: {"channels": 1}, "Loading index stats…")
        self.assertEqual(renders, [])
        self.assertIn("overview", app.cache)       # still cached for an instant visit

    def test_a_render_records_what_its_screen_displays(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app._render_lock = threading.RLock()
        app._section_keys, app._render_keys = {}, None
        app.section = "Saved"
        app.page = _Page()
        app.content = SimpleNamespace(controls=[])
        app._nav_tiles = lambda: []
        app._nav_col = SimpleNamespace(controls=[])
        app._page_header = lambda: None
        app._restore_scroll = lambda: None
        app._render_saved = lambda: app._render_keys.add("saved:40")
        BeaconApp._render(app)
        self.assertEqual(app._section_keys["Saved"], {"saved:40"})
        self.assertIsNone(app._render_keys)

    def test_a_screen_error_keeps_navigation_and_shows_recovery(self) -> None:
        app = BeaconApp.__new__(BeaconApp)
        app._render_lock = threading.RLock()
        app._section_keys, app._render_keys = {}, None
        app.section = "Find"
        app.page = _Page()
        app.content = SimpleNamespace(controls=["old"])
        app._nav_tiles = lambda: ["Find"]
        app._nav_col = SimpleNamespace(controls=["old nav"])
        app._page_header = lambda: "header"
        app._error_card = lambda _message: "recovery"
        app._restore_scroll = lambda: None
        app._render_find = lambda: (_ for _ in ()).throw(RuntimeError("broken card"))
        with self.assertLogs(level="ERROR"):
            BeaconApp._render(app)
        self.assertEqual(app.content.controls, ["header", "recovery"])
        self.assertEqual(app._nav_col.controls, ["Find"])
        self.assertEqual(app.page.updates, 1)
        self.assertFalse(app._rendering)


class _Proc:
    def __init__(self, exits: bool = False) -> None:
        self.pid, self._exits = 4242, exits

    def poll(self):
        return 0 if self._exits else None


class DataServiceProcessTests(unittest.TestCase):
    def _app(self) -> BeaconApp:
        app = BeaconApp.__new__(BeaconApp)
        app._finder_process = None
        return app

    def _patches(self, up_sequence, proc):
        answers = iter(up_sequence)
        client = mock.MagicMock()
        client.is_up.side_effect = lambda: next(answers, up_sequence[-1])
        return [
            mock.patch.object(beacon_app, "FinderClient", return_value=client),
            mock.patch.object(beacon_app.os.path, "exists", return_value=False),
            mock.patch.object(BeaconApp, "_start_finder_process",
                              staticmethod(lambda: proc)),
            mock.patch.object(BeaconApp, "_kill_process_tree", staticmethod(lambda _p: None)),
            mock.patch.object(beacon_app.atexit, "register"),
            mock.patch.object(beacon_app.time, "sleep"),
            mock.patch("beacon_engine.intelligence.local_api.serve_in_thread"),
        ]

    def _run(self, up_sequence, proc):
        app = self._app()
        patches = self._patches(up_sequence, proc)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        app._resolve_service()
        return app, patches

    def test_a_running_service_is_reused_without_starting_another(self) -> None:
        app, _ = self._run([True], _Proc())
        self.assertFalse(app.hosting)
        self.assertIsNone(app._finder_process)

    def test_the_service_runs_in_its_own_process_and_stops_with_beacon(self) -> None:
        proc = _Proc()
        app, _ = self._run([False, False, True], proc)
        self.assertFalse(app.hosting)                  # not inside the app any more
        self.assertIs(app._finder_process, proc)
        beacon_app.atexit.register.assert_called_once_with(app._stop_finder_process)

    def test_it_falls_back_to_in_process_when_the_process_cannot_start(self) -> None:
        app, _ = self._run([False], _Proc(exits=True))
        self.assertTrue(app.hosting)
        self.assertIsNone(app._finder_process)


class BusyDatabaseStartTests(DataServiceProcessTests):
    """2026-09-19 ~15:21: folding a 1.8 GB write-ahead log back into the index locked every
    newcomer out for ~3 minutes. The service died on its first "database is locked", the
    in-process fallback hit the same lock, and the window showed only Flet's bare "There was
    an error while processing your request: database is locked"."""

    def _run_waiting(self, up_sequence, proc, waiting: bool):
        with mock.patch.object(BeaconApp, "FINDER_START_TIMEOUT", 0), \
                mock.patch.object(BeaconApp, "_finder_waiting_for_db", lambda self: waiting):
            return self._run(up_sequence, proc)

    def test_it_keeps_waiting_while_the_service_waits_for_the_database(self) -> None:
        proc = _Proc()
        app, _ = self._run_waiting([False] * 6 + [True], proc, waiting=True)
        self.assertFalse(app.hosting)                  # the service came up; no fallback
        self.assertIs(app._finder_process, proc)

    def test_a_stuck_start_that_is_not_waiting_still_gives_up(self) -> None:
        app, _ = self._run_waiting([False], _Proc(), waiting=False)
        self.assertTrue(app.hosting)                   # fell back, as before

    def test_the_fallback_waits_out_a_busy_database_too(self) -> None:
        import sqlite3
        app = self._app()
        patches = self._patches([False], _Proc(exits=True))
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        import beacon_engine.intelligence.local_api as local_api
        local_api.serve_in_thread.side_effect = [
            sqlite3.OperationalError("database is locked"), None]
        app._resolve_service()
        self.assertTrue(app.hosting)
        self.assertEqual(local_api.serve_in_thread.call_count, 2)

    def test_any_other_start_error_reaches_the_window_instead_of_a_loop(self) -> None:
        app = self._app()
        patches = self._patches([False], _Proc(exits=True))
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        import beacon_engine.intelligence.local_api as local_api
        local_api.serve_in_thread.side_effect = RuntimeError("no such table: channels")
        with self.assertRaises(RuntimeError):
            app._resolve_service()
        self.assertEqual(local_api.serve_in_thread.call_count, 1)

    def test_only_the_latest_start_counts_as_waiting(self) -> None:
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self.addCleanup(os.remove, path)
        app = self._app()
        def write_log(*lines):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

        waiting = "FINDER waiting for the database -- database is locked"
        with mock.patch.object(BeaconApp, "FINDER_LOG", path):
            write_log("--- FINDER start 1 ---", waiting,
                      "--- FINDER start 2 ---", "FINDER service up")
            self.assertFalse(app._finder_waiting_for_db())     # that was an older start
            write_log("--- FINDER start 3 ---", waiting)
            self.assertTrue(app._finder_waiting_for_db())

    def test_a_failed_start_explains_itself_and_offers_a_retry(self) -> None:
        import sqlite3
        app = self._app()

        def texts(control):
            found = []
            for attr in ("value", "text"):
                if isinstance(getattr(control, attr, None), str):
                    found.append(getattr(control, attr))
            for child in (getattr(control, "controls", None) or []):
                found += texts(child)
            content = getattr(control, "content", None)
            if content is not None:
                found += texts(content)
            return found

        shown = " | ".join(texts(app._boot_failure(
            sqlite3.OperationalError("database is locked"))))
        self.assertIn("The database is still busy", shown)
        self.assertIn("Try again", shown)
        self.assertNotIn("There was an error", shown)
        other = " | ".join(texts(app._boot_failure(RuntimeError("boom"))))
        self.assertIn("couldn't start its data service", other)
        self.assertIn("RuntimeError: boom", other)

if __name__ == "__main__":
    unittest.main()
