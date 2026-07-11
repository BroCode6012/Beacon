from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from signal_autopilot import Autopilot


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


if __name__ == "__main__":
    unittest.main()
