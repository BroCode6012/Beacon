"""Contracts for recovering a scraper pause that a dead sync left behind.

The distinction under test is the whole point: a pause THIS script created and failed to
undo must be resumed automatically, while a pause the OPERATOR created must never be
touched. Getting that backwards either strands local scraping for a day (what happened on
2026-09-13) or overrides a deliberate Stop every two minutes.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import engine_link  # noqa: F401 — puts the shared engine on the path
import edge_sync_coordinated as esc


class _Sweep:
    """Stands in for sweep_control: records whether the scraper was restarted."""

    def __init__(self, *, alive_pids=()) -> None:
        self.started = 0
        self._alive = set(alive_pids)

    def start_worker(self, clear_stop: bool = True) -> bool:
        self.started += 1
        return True

    def _pid_alive(self, pid: int) -> bool:
        return pid in self._alive


class PauseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.owner = Path(self._tmp.name) / "edge_sync_pause_owner.json"
        self._patch = patch.object(esc, "OWNER_PATH", self.owner)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # Redirect the log too. Without this the suite writes "resuming the scraper — left
        # paused by a sync that died" into the REAL operational log, inventing production
        # incidents that never happened. The whole point of that log is to be trustworthy.
        self._log_patch = patch.object(
            esc, "LOG_FILE", str(Path(self._tmp.name) / "test.log"))
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write_owner(self, pid: int) -> None:
        self.owner.write_text(json.dumps({"pid": pid, "at": "2026-09-13T21:31:02"}),
                              encoding="utf-8")

    def test_a_pause_with_no_owner_is_left_alone(self) -> None:
        # No ownership file = the operator pressed Stop. Resuming would override them every
        # two minutes and Stop could never stick.
        sweep = _Sweep()
        with patch.object(esc, "sweep_control", sweep):
            self.assertFalse(esc._recover_orphaned_pause())
        self.assertEqual(sweep.started, 0)

    def test_our_pause_from_a_dead_process_is_resumed(self) -> None:
        # The 2026-09-13 case: we paused, the laptop slept, the resume never ran.
        self._write_owner(pid=999999)                 # not alive
        sweep = _Sweep(alive_pids=())
        with patch.object(esc, "sweep_control", sweep):
            self.assertTrue(esc._recover_orphaned_pause())
        self.assertEqual(sweep.started, 1)
        self.assertFalse(self.owner.exists(), "the claim must be cleared once resumed")

    def test_a_pause_held_by_a_LIVE_sibling_is_not_disturbed(self) -> None:
        # Another sync is mid-import right now; resuming would hand the scraper back while
        # that import still needs the database to itself.
        self._write_owner(pid=4321)
        sweep = _Sweep(alive_pids=(4321,))
        with patch.object(esc, "sweep_control", sweep):
            self.assertFalse(esc._recover_orphaned_pause())
        self.assertEqual(sweep.started, 0)
        self.assertTrue(self.owner.exists(), "a live owner's claim must survive")

    def test_a_corrupt_owner_file_still_recovers(self) -> None:
        # A half-written claim (killed mid-write) must not permanently strand the scraper.
        self.owner.write_text("{not json", encoding="utf-8")
        sweep = _Sweep()
        with patch.object(esc, "sweep_control", sweep):
            self.assertTrue(esc._recover_orphaned_pause())
        self.assertEqual(sweep.started, 1)

    def test_claim_then_release_leaves_no_trace(self) -> None:
        esc._claim_pause()
        self.assertTrue(self.owner.exists())
        saved = json.loads(self.owner.read_text(encoding="utf-8"))
        self.assertEqual(saved["pid"], os.getpid())
        esc._release_pause()
        self.assertFalse(self.owner.exists())

    def test_releasing_twice_is_harmless(self) -> None:
        esc._release_pause()
        esc._release_pause()
        self.assertFalse(self.owner.exists())



class KeepAwakeTests(unittest.TestCase):
    """Holding off idle sleep is the other half of the 2026-09-13 failure: pause recovery
    heals an interrupted sync, this prevents most of the interruptions."""

    def test_the_flag_is_set_inside_and_released_after(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-only power API")
        import ctypes
        set_state = ctypes.windll.kernel32.SetThreadExecutionState
        set_state.argtypes = [ctypes.c_uint]
        set_state.restype = ctypes.c_uint

        def system_required_bit() -> bool:
            # Re-assert the same state; the return value is what was in effect before.
            prev = set_state(esc._ES_CONTINUOUS | esc._ES_SYSTEM_REQUIRED)
            set_state(prev or esc._ES_CONTINUOUS)          # restore what we found
            return bool(prev & esc._ES_SYSTEM_REQUIRED)

        self.assertFalse(system_required_bit(), "must start clean")
        with esc._keep_awake() as held:
            self.assertTrue(held)
            self.assertTrue(system_required_bit(), "sleep must be held off during the import")
        self.assertFalse(system_required_bit(), "the machine must be free to sleep again")

    def test_it_releases_even_when_the_import_blows_up(self) -> None:
        # A crashing sync must not leave the laptop permanently unable to sleep.
        with self.assertRaises(RuntimeError):
            with esc._keep_awake():
                raise RuntimeError("import exploded")
        if os.name == "nt":
            import ctypes
            set_state = ctypes.windll.kernel32.SetThreadExecutionState
            set_state.argtypes = [ctypes.c_uint]
            set_state.restype = ctypes.c_uint
            prev = set_state(esc._ES_CONTINUOUS)
            self.assertFalse(prev & esc._ES_SYSTEM_REQUIRED)

    def test_a_missing_power_api_never_breaks_the_sync(self) -> None:
        # Non-Windows, or a locked-down host: the sync must still run, just unprotected.
        with patch.object(esc.sys, "platform", "linux"):
            with esc._keep_awake() as held:
                self.assertFalse(held)


if __name__ == "__main__":
    unittest.main()
