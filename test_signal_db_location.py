from __future__ import annotations

import unittest

import engine_link
import sweep_control


class SignalDatabaseLocationTests(unittest.TestCase):
    def test_signal_database_target_is_inside_signal_storage(self) -> None:
        expected = (
            engine_link.SIGNAL_ROOT / "storage" / "youtube_index.sqlite3"
        ).resolve()
        self.assertEqual(engine_link.SIGNAL_DB.resolve(), expected)

    def test_radar_operational_state_stays_in_scripter_storage(self) -> None:
        expected = (engine_link.LAB_REPO / "storage").resolve()
        self.assertEqual(sweep_control.STORAGE.resolve(), expected)
        self.assertEqual(
            sweep_control.SCRIPTER_ROOT,
            engine_link.LAB_REPO.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
