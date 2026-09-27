from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

import beacon_ai
import beacon_autopilot
from beacon_autopilot import Autopilot


class _App:
    section = "Overview"


class AutopilotEnvironmentGuardTests(unittest.TestCase):
    def test_environment_guard_suppresses_saved_autopilot_without_rewriting_it(self) -> None:
        with patch.dict(os.environ, {"SIGNAL_DISABLE_AUTOPILOT": "1"}):
            autopilot = Autopilot(_App())
            self.assertTrue(autopilot.disabled_by_env)
            self.assertFalse(autopilot.enabled)

            autopilot.set_enabled(True, interval=5)
            self.assertFalse(autopilot.enabled)
            self.assertIsNone(autopilot.next_run_ts)


class AutopilotProviderPolicyTests(unittest.TestCase):
    """The operator's rule: "when Azure is the provider, always turn it on" — because
    Azure credits are prepaid, while a Claude subscription has usage limits."""

    def _autopilot(self, tmp) -> Autopilot:
        with patch.object(beacon_autopilot, "STATE_FILE", tmp / "_autopilot.json"):
            ap = Autopilot(_App())
            ap._save = lambda: None          # never touch the real state file
            ap.start = lambda: None          # no background thread in tests
            return ap

    def _provider(self, name, title, policy):
        return beacon_ai.Provider(name=name, kind="openai", label=title, autopilot=policy)

    def test_an_azure_provider_turns_it_on_and_says_why(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ap = self._autopilot(tmp)
        self.assertFalse(ap.enabled)
        with patch.object(beacon_ai, "autopilot_provider",
                          return_value=self._provider("foundry", "Foundry", "auto")):
            reason = ap.apply_provider_policy()
        self.assertTrue(ap.enabled)
        self.assertIn("Foundry", reason)
        self.assertIn("Turned on automatically", ap.log[0]["summary"])

    def test_a_subscription_provider_leaves_the_switch_alone(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ap = self._autopilot(tmp)
        with patch.object(beacon_ai, "autopilot_provider",
                          return_value=self._provider("claude-code", "Claude Code", "manual")):
            self.assertEqual(ap.apply_provider_policy(), "")
        self.assertFalse(ap.enabled)          # not switched on for the operator
        self.assertEqual(ap.log, [])

        ap.enabled = True                      # and an operator's ON is not switched off
        with patch.object(beacon_ai, "autopilot_provider",
                          return_value=self._provider("claude-code", "Claude Code", "manual")):
            ap.apply_provider_policy()
        self.assertTrue(ap.enabled)

    def test_the_test_instance_guard_still_wins(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with patch.dict(os.environ, {"SIGNAL_DISABLE_AUTOPILOT": "1"}):
            ap = self._autopilot(tmp)
            with patch.object(beacon_ai, "autopilot_provider",
                              return_value=self._provider("foundry", "Foundry", "auto")):
                self.assertEqual(ap.apply_provider_policy(), "")
            self.assertFalse(ap.enabled)

    def test_the_shipped_providers_carry_the_intended_policy(self) -> None:
        providers, _ = beacon_ai.load()
        self.assertEqual(providers["foundry"].autopilot, "auto")
        self.assertEqual(providers["foundry-thinking"].autopilot, "auto")
        self.assertEqual(providers["azure-openai"].autopilot, "auto")
        self.assertEqual(providers["claude-code"].autopilot, "manual")
        self.assertEqual(providers["bedrock"].autopilot, "manual")

    def test_an_operator_override_stops_the_automatic_switch_on(self) -> None:
        with patch.dict(os.environ, {"BEACON_FOUNDRY_AUTOPILOT": "manual"}):
            providers, _ = beacon_ai.load()
        self.assertEqual(providers["foundry"].autopilot, "manual")


class PassDurationTests(unittest.TestCase):
    def test_a_finished_pass_shows_how_long_it_took(self) -> None:
        # The stamp is the START time; a 12-minute pass looked like one that just began.
        from beacon_app import BeaconApp
        self.assertEqual(BeaconApp._pass_took({"took_secs": 735}), " · took 12m")
        self.assertEqual(BeaconApp._pass_took({"took_secs": 42}), " · took 42s")
        self.assertEqual(BeaconApp._pass_took({}), "")          # older entries stay clean

    def test_a_running_pass_says_how_long_it_has_been_going(self) -> None:
        import time
        from beacon_app import BeaconApp
        self.assertEqual(BeaconApp._pass_running_text(None), "running a maintenance pass…")
        self.assertIn("started just now", BeaconApp._pass_running_text(time.time() - 5))
        self.assertIn("8m so far", BeaconApp._pass_running_text(time.time() - 8 * 60))


class PassPlanTests(unittest.TestCase):
    """Measured 2026-09-17 on the live index: the whole-index sweep costs ~11 minutes and
    excluded 0 / classified 0, because the scraper runs the same upkeep every cycle."""

    def _autopilot(self, log=None, last_deep=None):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "_autopilot.json"
        if log is not None or last_deep is not None:
            import json
            state.write_text(json.dumps({"enabled": True, "interval_minutes": 30,
                                         "last_deep_at": last_deep or 0.0,
                                         "log": log or []}), encoding="utf-8")
        with patch.object(beacon_autopilot, "STATE_FILE", state):
            ap = Autopilot(_App())
        ap._save = lambda: None
        ap._cloud_import_active = lambda: False      # never read the live sync file
        ap._scraper_does_upkeep = lambda: True       # ...or the live scraper config
        return ap

    def test_a_light_pass_cannot_even_ask_for_the_whole_index_tools(self) -> None:
        import time
        ap = self._autopilot(last_deep=time.time() - 3600)      # swept an hour ago
        with patch.object(ap, "_scraper_running", return_value=True):
            goal, tools, kind, why = ap.plan()
        self.assertEqual(kind, "light")
        self.assertNotIn("run_junk_filter", tools)
        self.assertNotIn("reclassify_topics", tools)
        self.assertNotIn("db_stats", tools)
        self.assertIn("recent_channels", tools)
        self.assertIn("every cycle", why)
        self.assertIn("do NOT redo", goal)

    def test_a_stopped_scraper_brings_the_full_sweep_back(self) -> None:
        import time
        ap = self._autopilot(last_deep=time.time() - 60)         # swept a minute ago
        with patch.object(ap, "_scraper_running", return_value=False):
            _goal, tools, kind, why = ap.plan()
        self.assertEqual(kind, "deep")                           # nobody else is doing it
        self.assertIn("run_junk_filter", tools)
        self.assertIn("scraper is off", why)

    def test_the_full_sweep_still_happens_on_its_own_schedule(self) -> None:
        import time
        stale = time.time() - (beacon_autopilot.DEEP_EVERY_HOURS + 1) * 3600
        ap = self._autopilot(last_deep=stale)
        with patch.object(ap, "_scraper_running", return_value=True):
            _goal, tools, kind, why = ap.plan()
        self.assertEqual(kind, "deep")
        self.assertIn("has not run", why)
        self.assertIn("run_junk_filter", tools)

    def test_an_upgrade_does_not_repeat_the_sweep_that_just_ran(self) -> None:
        import datetime as dt
        an_hour_ago = (dt.datetime.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        ap = self._autopilot(log=[{"at": an_hour_ago, "tools": ["run_junk_filter"]}])
        self.assertGreater(ap.last_deep_at, 0)                   # read from the old log
        with patch.object(ap, "_scraper_running", return_value=True):
            self.assertEqual(ap.plan()[2], "light")

    def test_the_card_names_each_kind_of_pass(self) -> None:
        from beacon_app import BeaconApp
        self.assertEqual(BeaconApp._pass_kind({"kind": "light"}), "Light check")
        self.assertEqual(BeaconApp._pass_kind({"kind": "deep"}), "Full sweep")
        self.assertEqual(BeaconApp._pass_kind({}), "Maintenance pass")   # older entries


if __name__ == "__main__":
    unittest.main()


class FailedPassTests(unittest.TestCase):
    """2026-09-19 11:44: Azure answered 401 after 0 seconds, yet the pass was recorded as the
    full sweep — postponing the real one by 12 hours — and the logged reason read "ready"."""

    def _autopilot(self, log=None, last_deep=0.0):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "_autopilot.json"
        import json
        state.write_text(json.dumps({"enabled": True, "interval_minutes": 30,
                                     "last_deep_at": last_deep, "log": log or []}),
                         encoding="utf-8")
        with patch.object(beacon_autopilot, "STATE_FILE", state):
            ap = Autopilot(_App())
        ap._save = lambda: None
        ap._cloud_import_active = lambda: False
        ap._scraper_does_upkeep = lambda: True
        ap.app.client = None
        return ap

    def _run(self, ap, reply):
        class _Agent:
            def __init__(self, *a, **k):
                pass

            def ready(self):
                return True, "ready"

            def run(self, goal, on_event=None):
                return reply

        import beacon_agent
        with patch.object(beacon_agent, "BeaconAgent", _Agent), \
                patch.object(ap, "_scraper_running", return_value=True):
            ap.run_once()
        return ap.log[0]

    def test_a_failed_full_sweep_is_retried_not_postponed(self) -> None:
        import time
        stale = time.time() - (beacon_autopilot.DEEP_EVERY_HOURS + 1) * 3600
        ap = self._autopilot(last_deep=stale)
        entry = self._run(ap, "⚠️ Foundry error: ProviderError: HTTP 401 from example")
        self.assertEqual(entry["kind"], "deep")
        self.assertFalse(entry["ok"])
        self.assertEqual(ap.last_deep_at, stale)                 # nothing was swept
        with patch.object(ap, "_scraper_running", return_value=True):
            self.assertEqual(ap.plan()[2], "deep")               # so the next wake tries again

    def test_a_finished_full_sweep_is_counted(self) -> None:
        ap = self._autopilot(last_deep=1.0)
        entry = self._run(ap, "✓ Swept: classified 3, excluded 0.")
        self.assertTrue(entry["ok"])
        self.assertGreater(ap.last_deep_at, 1.0)

    def test_the_logged_reason_is_the_plans_reason(self) -> None:
        import time
        ap = self._autopilot(last_deep=time.time() - 60)
        entry = self._run(ap, "✓ Nothing new.")
        self.assertEqual(entry["kind"], "light")
        self.assertIn("every cycle", entry["why"])               # was overwritten with "ready"

    def test_the_log_fallback_skips_failures_and_notes(self) -> None:
        import datetime as dt
        ap = self._autopilot(log=[
            {"at": "2026-09-19 12:00", "tools": [], "summary": "Turned on automatically."},
            {"at": "2026-09-19 11:44", "kind": "deep", "tools": [],
             "summary": "⚠️ Foundry error: HTTP 401"},
            {"at": "2026-09-19 11:00", "kind": "light", "tools": ["recent_channels"],
             "summary": "✓ Nothing new."},
            {"at": "2026-09-17 20:43", "tools": ["run_junk_filter", "db_stats"],
             "summary": "I have all the data I need."},
        ])
        self.assertEqual(ap.last_deep_at,
                         dt.datetime(2026, 9, 17, 20, 43).timestamp())

    def test_the_card_never_calls_a_failed_pass_a_sweep(self) -> None:
        from beacon_app import BeaconApp
        self.assertEqual(BeaconApp._pass_kind({"kind": "deep", "ok": False}),
                         "Full sweep (failed)")
        self.assertEqual(BeaconApp._pass_kind({"kind": "deep", "ok": True}), "Full sweep")


class CardHonestyTests(unittest.TestCase):
    """The 11:44 failed pass showed a green tick and "Maintenance completed"."""

    def test_a_failed_pass_says_it_did_not_run(self) -> None:
        from beacon_app import BeaconApp
        failed, problem = BeaconApp._pass_outcome({
            "kind": "deep", "ok": False, "tools": [],
            "summary": "⚠️ Foundry error: ProviderError: HTTP 401 from example"})
        self.assertTrue(failed)
        self.assertTrue(problem.startswith("Foundry error: ProviderError: HTTP 401"))
        # an older entry, from before `ok` was recorded, is caught by its summary
        self.assertTrue(BeaconApp._pass_outcome({"tools": [], "summary": "⚠️ Bedrock error: x"})[0])
        self.assertEqual(BeaconApp._pass_kind({"summary": "⚠️ Bedrock error: x"}),
                         "Maintenance pass (failed)")

    def test_a_finished_pass_and_an_old_note_keep_their_tick(self) -> None:
        from beacon_app import BeaconApp
        self.assertEqual(BeaconApp._pass_outcome(
            {"kind": "light", "ok": True, "summary": "✓ Nothing new."}), (False, ""))
        self.assertEqual(BeaconApp._pass_outcome(
            {"tools": [], "summary": "Turned on automatically."}), (False, ""))

    def test_the_running_line_names_the_kind_of_pass(self) -> None:
        import time
        from beacon_app import BeaconApp
        self.assertIn("full sweep", BeaconApp._pass_running_text(time.time() - 5, "deep"))
        self.assertIn("light check", BeaconApp._pass_running_text(time.time() - 5, "light"))
        self.assertIn("maintenance pass", BeaconApp._pass_running_text(None))


class RunningLineTickerTests(unittest.TestCase):
    """A 10-minute sweep said "started just now" the whole time: nothing redrew the card."""

    def test_the_running_line_keeps_moving_and_the_ticker_stops_after(self) -> None:
        import time
        from types import SimpleNamespace
        from beacon_app import BeaconApp

        class _Line:
            value, updates = "running a full sweep of the index… started just now", 0

            def update(self):
                self.updates += 1

        app = BeaconApp.__new__(BeaconApp)
        app.section = "Agent"
        app._pass_line = _Line()
        app.autopilot = SimpleNamespace(running_now=True, running_kind="deep",
                                        running_since=time.time() - 8 * 60)
        ticks = []

        def fake_sleep(_s):
            ticks.append(1)
            if len(ticks) >= 2:
                app.autopilot.running_now = False     # the pass finishes

        with patch("beacon_app.time.sleep", fake_sleep):
            app._tick_pass_line()                      # returns once the pass is over
        self.assertEqual(app._pass_line.value, "running a full sweep of the index… 8m so far")
        self.assertEqual(app._pass_line.updates, 1)


class CloudImportGuardTests(PassPlanTests):
    """2026-09-19: a full sweep's whole-index reads starved the cloud import — 0 of 4 bundles
    for 11 minutes, all four done ~50s after the scans stopped."""

    def test_a_due_full_sweep_waits_while_a_cloud_import_is_saving(self) -> None:
        import time
        stale = time.time() - (beacon_autopilot.DEEP_EVERY_HOURS + 1) * 3600
        ap = self._autopilot(last_deep=stale)
        ap._cloud_import_active = lambda: True
        with patch.object(ap, "_scraper_running", return_value=True):
            _goal, tools, kind, why = ap.plan()
        self.assertEqual(kind, "light")
        self.assertNotIn("run_junk_filter", tools)
        self.assertIn("cloud import", why)
        ap._cloud_import_active = lambda: False                  # import done: sweep goes
        with patch.object(ap, "_scraper_running", return_value=True):
            self.assertEqual(ap.plan()[2], "deep")

    def test_only_a_fresh_importing_state_counts(self) -> None:
        import json
        import os
        import time
        import sweep_control
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        folder = root / "storage" / "edge_sync"
        folder.mkdir(parents=True)
        progress = folder / "sync_progress.json"
        ap = Autopilot.__new__(Autopilot)
        with patch.object(sweep_control, "SCRIPTER_ROOT", root):
            self.assertFalse(ap._cloud_import_active())          # no file yet
            progress.write_text(json.dumps({"state": "importing"}), encoding="utf-8")
            self.assertTrue(ap._cloud_import_active())
            old = time.time() - Autopilot.IMPORT_FRESH_SECONDS - 60
            os.utime(progress, (old, old))
            self.assertFalse(ap._cloud_import_active())          # a dead run's leftover
            progress.write_text(json.dumps({"state": "complete"}), encoding="utf-8")
            self.assertFalse(ap._cloud_import_active())


class ScraperUpkeepTests(PassPlanTests):
    """A full sweep repeats the scraper's own per-cycle upkeep, so it is only needed when
    nobody else is doing it."""

    def test_switching_off_the_scrapers_upkeep_brings_the_sweep_back(self) -> None:
        import time
        ap = self._autopilot(last_deep=time.time() - 60)         # swept a minute ago
        ap._scraper_does_upkeep = lambda: False
        with patch.object(ap, "_scraper_running", return_value=True):
            _goal, tools, kind, why = ap.plan()
        self.assertEqual(kind, "deep")
        self.assertIn("switched off", why)
        self.assertIn("run_junk_filter", tools)

    def test_the_safety_net_is_weekly_by_default(self) -> None:
        self.assertEqual(beacon_autopilot.DEEP_EVERY_HOURS, 168)

    def test_it_reads_the_scraper_config_the_way_the_radar_does(self) -> None:
        import json
        import sweep_control
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = tmp / "scraper_watchlist.json"
        ap = Autopilot.__new__(Autopilot)
        cases = [({}, True),                                     # radar default: on
                 ({"junk_filter_enabled": True, "topic_classify_enabled": True}, True),
                 ({"junk_filter_enabled": False}, False),
                 ({"topic_classify_enabled": 0}, False)]
        with patch.object(sweep_control, "WATCHLIST_PATH", cfg):
            self.assertFalse(ap._scraper_does_upkeep())          # unreadable: do the upkeep
            for data, expected in cases:
                cfg.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(ap._scraper_does_upkeep(), expected, data)
