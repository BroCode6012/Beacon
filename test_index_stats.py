"""The shared index count (index_stats): instant when saved, one count at a time, age shown."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import index_stats as ix


class IndexStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="index-stats-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.snap = self.tmp / "index_stats.json"
        self.lock = self.tmp / "index_stats.lock"
        self.counts = 0

    def count(self, value=100, delay=0.0):
        def run():
            self.counts += 1
            time.sleep(delay)
            return {"channels": value, "videos": value * 20}
        return run

    def test_first_time_counts_then_answers_instantly(self):
        first = ix.ensure(self.count(), self.snap, self.lock)
        self.assertEqual(first["stats"]["channels"], 100)
        second = ix.ensure(self.count(999), self.snap, self.lock)
        self.assertEqual(second["stats"]["channels"], 100)     # the saved count, no wait
        self.assertEqual(self.counts, 1)

    def test_an_old_count_is_shown_at_once_and_refreshed_quietly(self):
        ix.write({"channels": 1}, 1.0, self.snap)
        old = time.time() - ix.REFRESH_SECONDS - 60
        os.utime(self.snap)                                     # file time is not what counts
        data = ix.read(self.snap)
        data["counted_at"] = old
        self.snap.write_text(__import__("json").dumps(data), encoding="utf-8")
        done = threading.Event()
        shown = ix.ensure(self.count(5), self.snap, self.lock, on_refreshed=lambda _s: done.set())
        self.assertEqual(shown["stats"]["channels"], 1)         # instant, even though old
        self.assertTrue(done.wait(5))
        self.assertEqual(ix.read(self.snap)["stats"]["channels"], 5)

    def test_only_one_count_runs_at_a_time(self):
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(0.5)
            self.counts += 1
            return {"channels": 1}

        worker = threading.Thread(target=ix.count_now, args=(slow, self.snap, self.lock))
        worker.start()
        started.wait(2)
        self.assertIsNone(ix.count_now(self.count(), self.snap, self.lock))   # refused, not doubled
        worker.join()
        self.assertEqual(self.counts, 1)
        self.assertFalse(self.lock.exists())                    # released afterwards

    def test_a_lock_left_by_a_killed_process_expires(self):
        self.lock.write_text("12345")
        stale = time.time() - ix.LOCK_STALE_SECONDS - 5
        os.utime(self.lock, (stale, stale))
        self.assertIsNotNone(ix.count_now(self.count(), self.snap, self.lock))

    def test_a_failed_count_leaves_the_old_one_and_the_lock_free(self):
        ix.write({"channels": 42}, 1.0, self.snap)

        def broken():
            raise RuntimeError("database is locked")

        with self.assertRaises(RuntimeError):
            ix.count_now(broken, self.snap, self.lock)
        self.assertEqual(ix.read(self.snap)["stats"]["channels"], 42)
        self.assertFalse(self.lock.exists())

    def test_age_is_said_plainly(self):
        now = time.time()
        snap = {"counted_at": now - 30}
        self.assertEqual(ix.counted_text(snap, now), "counted just now")
        snap["counted_at"] = now - 17 * 60
        self.assertEqual(ix.counted_text(snap, now), "counted 17 min ago")
        snap["counted_at"] = now - 5 * 3600
        self.assertEqual(ix.counted_text(snap, now), "counted 5 h ago")

    def test_a_damaged_file_is_treated_as_no_count(self):
        self.snap.write_text("{not json", encoding="utf-8")
        self.assertIsNone(ix.read(self.snap))


if __name__ == "__main__":
    unittest.main()
