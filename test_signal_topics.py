from __future__ import annotations

import unittest

from signal_app import ALL_TOPICS, SignalApp


class SignalTopicSentinelTests(unittest.TestCase):
    def test_dropdown_uses_nonempty_all_topics_sentinel(self) -> None:
        app = SignalApp.__new__(SignalApp)
        app._ensure = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "data": [{"topic": "History", "count": 12}]
        }

        dropdown = app._topic_dd(None, lambda event: None)

        self.assertEqual(dropdown.value, ALL_TOPICS)
        self.assertEqual(dropdown.options[0].key, ALL_TOPICS)
        self.assertEqual(dropdown.options[0].text, "All topics")
        self.assertNotEqual(dropdown.options[0].key, "")

    def test_channels_query_never_sends_all_topics_as_a_filter(self) -> None:
        for stored_value in (ALL_TOPICS, "All topics", "", None):
            with self.subTest(stored_value=stored_value):
                app = SignalApp.__new__(SignalApp)
                app.ch_filters = {"sort": "opportunity_score"}
                app.ch_niche = None
                app.ch_view = "index"
                app.ch_topic = stored_value
                app.ch_shown = 120

                self.assertNotIn("topic", app._ch_query_args())

    def test_realtime_query_never_sends_all_topics_as_a_filter(self) -> None:
        for stored_value in (ALL_TOPICS, "All topics", "", None):
            with self.subTest(stored_value=stored_value):
                app = SignalApp.__new__(SignalApp)
                app.rt_filters = {}
                app.rt_topic = stored_value
                app.rt_window = 1
                app.rt_shown = 120

                self.assertNotIn("topic", app._rt_query_args())

    def test_topic_setters_normalize_sentinel_and_legacy_label(self) -> None:
        app = SignalApp.__new__(SignalApp)
        app._render = lambda: None  # type: ignore[method-assign]
        app.rt_shown = 999

        app._ch_set_topic(ALL_TOPICS)
        app._rt_set_topic("All topics")

        self.assertIsNone(app.ch_topic)
        self.assertIsNone(app.rt_topic)
        self.assertEqual(app.rt_shown, 120)


if __name__ == "__main__":
    unittest.main()
