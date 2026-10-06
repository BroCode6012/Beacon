"""Contracts for "where Beacon lives is a setting" (beacon_host).

Moving Beacon to a better host must mean changing one file, never the code — and a laptop
without that file must behave exactly as it always has.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import beacon_host as bh


class ParseTests(unittest.TestCase):
    def test_lines_comments_quotes_and_exports(self):
        text = """
        # a comment
        FACELESS_ENGINE_DIR=/opt/beacon/current/Quill
        export TZ=Africa/Casablanca
        FINDER_PORT = 8765   # trailing comment
        QUOTED="a value with # inside"
        SINGLE='x'
        not a setting
        """
        self.assertEqual(bh.parse(text), {
            "FACELESS_ENGINE_DIR": "/opt/beacon/current/Quill",
            "TZ": "Africa/Casablanca",
            "FINDER_PORT": "8765",
            "QUOTED": "a value with # inside",
            "SINGLE": "x",
        })


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beacon-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.file = self.tmp / "beacon_host.env"

    def test_the_file_fills_gaps_but_the_environment_wins(self):
        self.file.write_text("BEACON_TEST_A=from-file\nBEACON_TEST_B=from-file\n",
                             encoding="utf-8")
        with mock.patch.dict(os.environ, {"BEACON_TEST_B": "from-env"}, clear=False):
            os.environ.pop("BEACON_TEST_A", None)
            applied = bh.load(self.file)
            self.assertEqual(os.environ["BEACON_TEST_A"], "from-file")
            self.assertEqual(os.environ["BEACON_TEST_B"], "from-env")   # never overridden
            self.assertEqual(applied, {"BEACON_TEST_A": "from-file"})
            os.environ.pop("BEACON_TEST_A", None)

    def test_no_file_changes_nothing(self):
        before = dict(os.environ)
        self.assertEqual(bh.load(self.tmp / "missing.env"), {})
        self.assertEqual(dict(os.environ), before)

    def test_a_time_zone_is_applied_where_the_system_supports_it(self):
        self.file.write_text("TZ=Africa/Casablanca\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(bh.time, "tzset", create=True) as tzset:
            os.environ.pop("TZ", None)
            bh.load(self.file)
            self.assertEqual(os.environ["TZ"], "Africa/Casablanca")
            tzset.assert_called_once()


class EngineLocationTests(unittest.TestCase):
    def test_an_engine_beside_beacon_is_found_on_any_machine(self):
        tmp = Path(tempfile.mkdtemp(prefix="beacon-layout-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "Beacon").mkdir()
        (tmp / "Quill" / "scripter").mkdir(parents=True)
        self.assertEqual(bh.default_engine_dir(tmp / "Beacon"), tmp / "Quill")

    def test_without_one_it_falls_back_to_the_laptop_layout(self):
        tmp = Path(tempfile.mkdtemp(prefix="beacon-layout-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "Beacon").mkdir()
        self.assertEqual(bh.default_engine_dir(tmp / "Beacon"), bh.LEGACY_ENGINE_DIR)

    def test_the_real_host_file_is_never_committed(self):
        ignored = Path(__file__).with_name(".gitignore").read_text(encoding="utf-8")
        self.assertIn("beacon_host.env", ignored.splitlines())


if __name__ == "__main__":
    unittest.main()
