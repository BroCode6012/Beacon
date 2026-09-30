from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from beacon_app import BeaconApp


class _Page:
    def __init__(self) -> None:
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


class BeaconBusyStateTests(unittest.TestCase):
    def _app(self) -> BeaconApp:
        app = BeaconApp.__new__(BeaconApp)
        app.page = _Page()
        app.spinner = SimpleNamespace(visible=False)
        app.status = SimpleNamespace(value="")
        app._busy = False
        app._render_lock = threading.RLock()
        app._busy_lock = threading.Lock()
        app._busy_tasks = {}
        app._busy_seq = 0
        return app

    def test_concurrent_operations_keep_spinner_until_all_finish(self) -> None:
        app = self._app()

        first = app._begin_busy("Loading channels…")
        second = app._begin_busy("Loading topics…")
        app._end_busy(first)

        self.assertTrue(app._busy)
        self.assertTrue(app.spinner.visible)
        self.assertEqual(app.status.value, "Loading topics…")

        app._end_busy(second)

        self.assertFalse(app._busy)
        self.assertFalse(app.spinner.visible)
        self.assertEqual(app.status.value, "")


if __name__ == "__main__":
    unittest.main()
