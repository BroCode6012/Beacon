from __future__ import annotations

import unittest
from unittest.mock import patch

import engine_link
import sweep_control


class BeaconDatabaseLocationTests(unittest.TestCase):
    def test_beacon_database_target_is_inside_signal_storage(self) -> None:
        expected = (
            engine_link.SIGNAL_ROOT / "storage" / "youtube_index.sqlite3"
        ).resolve()
        self.assertEqual(engine_link.SIGNAL_DB.resolve(), expected)

    def test_shared_database_never_falls_back_to_beacon_engine_storage(self) -> None:
        with patch.dict(
            engine_link.os.environ,
            {"SIGNAL_INDEX_DB": "", "YOUTUBE_INDEX_DB": ""},
        ):
            self.assertEqual(engine_link.resolve_shared_db(), engine_link.SIGNAL_DB)

    def test_radar_operational_state_stays_in_beacon_engine_storage(self) -> None:
        expected = (engine_link.ENGINE_ROOT / "storage").resolve()
        self.assertEqual(sweep_control.STORAGE.resolve(), expected)
        self.assertEqual(
            sweep_control.ENGINE_ROOT,
            engine_link.ENGINE_ROOT.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
