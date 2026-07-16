from __future__ import annotations

import unittest

from signal_app import SignalApp


class FilterLabelTests(unittest.TestCase):
    """Regression: `_ch_filter_label` must not crash on a STRING filter value.

    It used to build one big dict literal, which eagerly ran int(v) for the numeric
    entries even when the key was a string filter like category_contains='history' —
    raising "invalid literal for int() with base 10: 'history'" and blanking the whole
    Channels toolbar. This is exactly the path the target click-through + the
    'Documentary Newcomers' preset (category_contains='documentary') hit.
    """

    def setUp(self) -> None:
        self.app = SignalApp.__new__(SignalApp)

    def test_string_category_value_does_not_crash(self) -> None:
        for value in ("history", "documentary", "deep sea exploration"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.app._ch_filter_label("category_contains", value),
                    f"category ~ {value}",
                )

    def test_numeric_labels_still_render(self) -> None:
        self.assertEqual(self.app._ch_filter_label("max_days_since_start", 180), "age ≤ 180d")
        self.assertEqual(self.app._ch_filter_label("min_subs", 50_000), "subs ≥ 50K")
        self.assertEqual(self.app._ch_filter_label("video_format", "long"), "format: long")
        self.assertEqual(self.app._ch_filter_label("faceless_only", True), "faceless only")

    def test_days_discovery_window_is_a_visible_removable_chip(self) -> None:
        # Regression for the target click-through: the discovery-recency `days` window must
        # render a chip (so it's visible + removable) and count as an active filter.
        self.assertEqual(self.app._ch_filter_label("days", 1), "added ≤ 1d")
        self.assertIn("days", self.app._FILTER_KEYS)
        self.assertEqual(self.app._active_filter_count({"days": 7}), 1)

    def test_non_numeric_value_for_numeric_key_is_skipped_not_crashed(self) -> None:
        # A malformed value for a numeric key returns None (chip hidden) instead of raising.
        self.assertIsNone(self.app._ch_filter_label("max_days_since_start", "oops"))

    def test_unknown_key_returns_none(self) -> None:
        self.assertIsNone(self.app._ch_filter_label("sort", "opportunity_score"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
