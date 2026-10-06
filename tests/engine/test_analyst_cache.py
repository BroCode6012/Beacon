from __future__ import annotations

import unittest

from beacon_engine.intelligence.analyst_tools import (
    IntelligenceAnalystTools,
    _CACHE,
)


class AnalystWriteCacheInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        _CACHE.clear()

    def tearDown(self) -> None:
        _CACHE.clear()

    @staticmethod
    def _tools():
        return IntelligenceAnalystTools.__new__(IntelligenceAnalystTools)

    def test_successful_write_clears_cached_reads(self) -> None:
        tools = self._tools()
        source = [{"id": 1, "name": "before"}]
        tools.list_niches = lambda **_: list(source)
        tools.create_niche = lambda **_: {"niche_id": 2}

        self.assertEqual(tools.dispatch("list_niches")[0]["name"], "before")
        source[0] = {"id": 1, "name": "after"}
        self.assertEqual(tools.dispatch("list_niches")[0]["name"], "before")

        tools.dispatch("create_niche", {"name": "new"})

        self.assertEqual(tools.dispatch("list_niches")[0]["name"], "after")

    def test_failed_write_keeps_last_good_cached_read(self) -> None:
        tools = self._tools()
        source = [{"id": 1, "name": "before"}]
        tools.list_niches = lambda **_: list(source)
        tools.create_niche = lambda **_: {"error": "not created"}

        self.assertEqual(tools.dispatch("list_niches")[0]["name"], "before")
        source[0] = {"id": 1, "name": "after"}

        tools.dispatch("create_niche", {"name": "new"})

        self.assertEqual(tools.dispatch("list_niches")[0]["name"], "before")


if __name__ == "__main__":
    unittest.main()
