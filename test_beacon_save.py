"""Contracts for saving a channel you found yourself on YouTube.

Link parsing is pure, so it is tested directly. The save flow is tested against a fake
finder: what matters is the ORDER and ARGUMENTS of the finder calls (seed before filing,
never a direct database write), not that YouTube answered.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import engine_link  # noqa: F401 — puts the shared engine on the path
import flet as ft
import beacon_save as bs
from beacon_app import BeaconApp


class LinkParsingTests(unittest.TestCase):
    def test_every_youtube_link_shape_resolves_to_the_right_kind(self) -> None:
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": ("video_id", "dQw4w9WgXcQ"),
            "https://youtu.be/eKWHMzHhmg4": ("video_id", "eKWHMzHhmg4"),
            "https://www.youtube.com/shorts/abc12345678": ("video_id", "abc12345678"),
            "https://www.youtube.com/live/abc12345678": ("video_id", "abc12345678"),
            "https://www.youtube.com/channel/UCLmU6mzQAUkQFmSWukexcTg":
                ("channel_id", "UCLmU6mzQAUkQFmSWukexcTg"),
            "UCLmU6mzQAUkQFmSWukexcTg": ("channel_id", "UCLmU6mzQAUkQFmSWukexcTg"),
            "https://www.youtube.com/@veritasium": ("handle", "veritasium"),
            "@mrballen": ("handle", "mrballen"),
            "https://www.youtube.com/c/SomeName": ("name", "SomeName"),
            "https://www.youtube.com/user/SomeUser": ("name", "SomeUser"),
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(bs.parse_link(link), expected)

    def test_a_watch_url_with_extra_params_still_finds_the_video(self) -> None:
        # Real links carry playlist/timestamp params, and a watch URL can contain other
        # UC-looking strings — the video id must still win.
        self.assertEqual(
            bs.parse_link("https://www.youtube.com/watch?list=PLabc&v=eKWHMzHhmg4&t=42"),
            ("video_id", "eKWHMzHhmg4"))

    def test_empty_and_unusable_links_explain_themselves(self) -> None:
        with self.assertRaises(bs.SaveError):
            bs.parse_link("   ")
        with self.assertRaises(bs.SaveError):
            bs.parse_link("https://www.youtube.com/feed/subscriptions")

    def test_a_channel_link_needs_no_network(self) -> None:
        # No API key, no pool, no request: a plain channel link must resolve offline.
        self.assertEqual(
            bs.resolve_channel_id("https://www.youtube.com/channel/UCLmU6mzQAUkQFmSWukexcTg"),
            "UCLmU6mzQAUkQFmSWukexcTg")


class _FakeFinder:
    """Records calls and returns canned results, in the real client's shape.

    `responses` scripts one tool's answers in order; an Exception in the script is raised,
    the way FinderClient raises when the service answers with an error status.
    """

    def __init__(self, *, niches=None, seed=None, rows=None, members=None,
                 responses=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._niches = niches if niches is not None else []
        self._seed = seed if seed is not None else {"requested": 1, "added": 1}
        self._rows = rows if rows is not None else []
        self._members = members
        self._responses = {k: list(v) for k, v in (responses or {}).items()}

    def call(self, tool, **args):
        self.calls.append((tool, args))
        script = self._responses.get(tool)
        if script:
            answer = script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if tool == "list_competitors":
            if self._members is not None:
                return self._members
            return [{"channel_id": r["channel_id"], "added_at": "", "growth": {}}
                    for r in self._rows if r.get("channel_id")]
        if tool == "list_niches":
            return self._niches
        if tool == "create_niche":
            self._niches = [{"id": 7, "name": args.get("name")}]
            return {"niche_id": 7, "name": args.get("name")}
        if tool == "seed_channels":
            return self._seed
        if tool == "find_channels":
            return [dict(r) for r in self._rows]
        return {"ok": True}

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


_CID = "UCLmU6mzQAUkQFmSWukexcTg"
_LINK = f"https://www.youtube.com/channel/{_CID}"


class SaveFlowTests(unittest.TestCase):
    def test_the_shelf_workspace_is_created_once_then_reused(self) -> None:
        f = _FakeFinder()
        self.assertEqual(bs.ensure_saved_niche(f), 7)
        self.assertIn("create_niche", f.names())

        f2 = _FakeFinder(niches=[{"id": 3, "name": bs.SAVED_NICHE_NAME}])
        self.assertEqual(bs.ensure_saved_niche(f2), 3)
        self.assertNotIn("create_niche", f2.names())

    def test_seeding_happens_before_filing_into_the_workspace(self) -> None:
        # The workspace only accepts channels the index already knows, so this order is
        # the whole reason a brand-new channel can be saved at all.
        f = _FakeFinder(rows=[{"channel_id": _CID, "title": "X"}])
        bs.save_channel(f, _LINK, niche_id=3)
        names = f.names()
        self.assertLess(names.index("seed_channels"), names.index("add_channel_to_niche"))

    def test_a_new_channel_is_reported_as_new_and_gets_a_topic(self) -> None:
        f = _FakeFinder(seed={"requested": 1, "added": 1},
                        rows=[{"channel_id": _CID, "title": "X"}])
        res = bs.save_channel(f, _LINK, niche_id=3)
        self.assertTrue(res["was_new"])
        self.assertIn("reclassify_topics", f.names())

    def test_an_already_indexed_channel_skips_the_relabel(self) -> None:
        # added=0 means the crawler already had it, so its topic is already set —
        # relabelling the index again would be pure cost.
        f = _FakeFinder(seed={"requested": 1, "added": 0},
                        rows=[{"channel_id": _CID, "title": "X"}])
        res = bs.save_channel(f, _LINK, niche_id=3)
        self.assertFalse(res["was_new"])
        self.assertNotIn("reclassify_topics", f.names())

    def test_a_busy_index_surfaces_a_readable_error(self) -> None:
        f = _FakeFinder(seed={"error": "index busy, could not save right now"})
        with self.assertRaises(bs.SaveError):
            bs.save_channel(f, _LINK, niche_id=3)

    def test_the_saved_shelf_does_not_apply_the_opportunity_gate(self) -> None:
        # These were chosen by a person. Hiding one for failing an automatic test would
        # overrule the operator about their own find.
        f = _FakeFinder(rows=[{"channel_id": _CID}])
        bs.saved_channels(f, 3)
        _, args = next(c for c in f.calls if c[0] == "find_channels")
        self.assertFalse(args["faceless_only"])
        self.assertFalse(args["replicable_only"])
        self.assertEqual(args["niche_id"], 3)

    def test_save_never_touches_the_database_directly(self) -> None:
        # Every write must go through the finder — the radar and the cloud importer are
        # already writing, and a third writer in the UI process caused lock stalls before.
        f = _FakeFinder(rows=[{"channel_id": _CID}])
        bs.save_channel(f, _LINK, niche_id=3)
        self.assertTrue(f.calls, "the save must go through the finder")
        for tool, _ in f.calls:
            self.assertIsInstance(tool, str)


class _NoWaiting(unittest.TestCase):
    """Busy-database retries sleep between tries; tests count the sleeps instead."""

    def setUp(self) -> None:
        self.sleeps: list[float] = []
        patcher = mock.patch.object(bs, "_sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)


_LOCKED = RuntimeError("Finder call 'sync_competitors' failed (400): database is locked")


class ShelfOrderAndGrowthTests(_NoWaiting):
    def test_the_shelf_is_ordered_by_when_you_saved_not_when_the_crawler_found_it(self) -> None:
        # find_channels sorts by discovery: B was discovered recently, A years ago. But A
        # was SAVED last, so A belongs on top of the shelf.
        f = _FakeFinder(
            rows=[{"channel_id": "UCb"}, {"channel_id": "UCa"}],
            members=[
                {"channel_id": "UCa", "added_at": "2026-09-15T20:00:00Z"},
                {"channel_id": "UCb", "added_at": "2026-09-01T08:00:00Z"},
            ])
        rows = bs.saved_channels(f, 18)
        self.assertEqual([r["channel_id"] for r in rows], ["UCa", "UCb"])
        self.assertEqual(rows[0]["saved_at"], "2026-09-15T20:00:00Z")

    def test_growth_arrives_with_the_shelf_in_one_call(self) -> None:
        growth = {"snapshots": 6, "subs_gained": 8500, "subs_per_day": 5546.1}
        f = _FakeFinder(rows=[{"channel_id": _CID}],
                        members=[{"channel_id": _CID, "added_at": "", "growth": growth}])
        rows = bs.saved_channels(f, 18)
        self.assertEqual(rows[0]["growth"], growth)
        self.assertEqual(f.names().count("list_competitors"), 1)
        self.assertNotIn("get_competitor_growth", f.names())

    def test_the_whole_shelf_is_read_so_paging_cannot_drop_a_recent_save(self) -> None:
        members = [{"channel_id": f"UC{i}", "added_at": f"2026-09-0{i}T00:00:00Z"}
                   for i in range(1, 6)]
        f = _FakeFinder(rows=[{"channel_id": m["channel_id"]} for m in members],
                        members=members)
        rows = bs.saved_channels(f, 18, limit=2)
        _, args = next(c for c in f.calls if c[0] == "find_channels")
        self.assertEqual(args["limit"], 5)
        self.assertEqual([r["channel_id"] for r in rows], ["UC5", "UC4"])

    def test_a_broken_shelf_read_raises_instead_of_looking_empty(self) -> None:
        # "Nothing saved yet" is a real answer; a hiccup must never be shown as one.
        f = _FakeFinder(responses={"list_competitors": [{"error": "boom"}]})
        with self.assertRaises(bs.SaveError):
            bs.saved_channels(f, 18)

    def test_an_empty_shelf_skips_the_channel_query(self) -> None:
        f = _FakeFinder(members=[])
        self.assertEqual(bs.saved_channels(f, 18), [])
        self.assertNotIn("find_channels", f.names())


class GrowthLabelTests(unittest.TestCase):
    _SAVED = "2026-09-14T21:34:26Z"

    def _row(self, **growth) -> dict:
        base = {"snapshots": 5, "subs_gained": 5800, "subs_per_day": 6400.0,
                "first_captured_at": "2026-09-14T22:17:14Z"}
        base.update(growth)
        return {"saved_at": self._SAVED, "growth": base}

    def test_measured_after_saving_is_since_saved(self) -> None:
        self.assertEqual(bs.growth_summary(self._row())["since"], "since saved")

    def test_measured_before_saving_is_not_claimed_as_since_saved(self) -> None:
        # Already tracked in another workspace: the gain started before you saved it.
        row = self._row(first_captured_at="2026-08-01T00:00:00Z")
        self.assertEqual(bs.growth_summary(row)["since"], "since first measured")

    def test_one_reading_is_not_a_trend(self) -> None:
        self.assertIsNone(bs.growth_summary(self._row(snapshots=1)))
        self.assertIsNone(bs.growth_summary({"saved_at": self._SAVED}))

    def test_no_movement_shows_nothing(self) -> None:
        self.assertIsNone(bs.growth_summary(self._row(subs_gained=0)))


class BusyDatabaseTests(_NoWaiting):
    def test_a_refresh_waits_out_a_busy_database(self) -> None:
        f = _FakeFinder(rows=[{"channel_id": _CID}],
                        responses={"sync_competitors": [_LOCKED, _LOCKED, {"refreshed": 1}]})
        res = bs.refresh_saved(f, 18)
        self.assertEqual(res["channels"], 1)
        self.assertEqual(f.names().count("sync_competitors"), 3)
        self.assertEqual(self.sleeps, [bs.BUSY_WAIT_SECONDS] * 2)

    def test_a_busy_answer_is_retried_even_when_it_is_returned_not_raised(self) -> None:
        f = _FakeFinder(rows=[{"channel_id": _CID}],
                        responses={"sync_competitors": [{"error": "database is locked"},
                                                        {"refreshed": 1}]})
        bs.refresh_saved(f, 18)
        self.assertEqual(f.names().count("sync_competitors"), 2)

    def test_a_real_error_is_not_retried(self) -> None:
        f = _FakeFinder(rows=[{"channel_id": _CID}],
                        responses={"sync_competitors": [RuntimeError("quotaExceeded")]})
        with self.assertRaises(RuntimeError):
            bs.refresh_saved(f, 18)
        self.assertEqual(f.names().count("sync_competitors"), 1)
        self.assertEqual(self.sleeps, [])

    def test_it_gives_up_after_the_last_try(self) -> None:
        f = _FakeFinder(rows=[{"channel_id": _CID}],
                        responses={"sync_competitors": [_LOCKED] * 10})
        with self.assertRaises(RuntimeError):
            bs.refresh_saved(f, 18)
        self.assertEqual(f.names().count("sync_competitors"), bs.BUSY_TRIES)

    def test_remove_waits_out_a_busy_database_too(self) -> None:
        f = _FakeFinder(responses={"untrack_competitor": [_LOCKED, {"ok": True}]})
        self.assertEqual(bs.remove_saved(f, 18, _CID)["removed"], _CID)
        self.assertEqual(f.names().count("untrack_competitor"), 2)

    def test_a_channel_that_could_not_be_filed_is_not_reported_as_saved(self) -> None:
        f = _FakeFinder(rows=[{"channel_id": _CID}],
                        responses={"add_channel_to_niche": [{"error": "no such niche"}]})
        with self.assertRaises(bs.SaveError) as ctx:
            bs.save_channel(f, _LINK, niche_id=3)
        self.assertIn("shelf", str(ctx.exception))


def _texts(control) -> list[str]:
    """Every Text value inside a control tree."""
    out: list[str] = []
    if isinstance(control, ft.Text) and control.value:
        out.append(str(control.value))
    for child in (getattr(control, "controls", None) or []):
        out += _texts(child)
    content = getattr(control, "content", None)
    if content is not None:
        out += _texts(content)
    return out


class SavedTabRefreshStateTests(unittest.TestCase):
    def _app(self) -> BeaconApp:
        app = BeaconApp.__new__(BeaconApp)
        app.client = object()
        app.section = "Channels"          # not on Saved, so nothing tries to render
        app.saved_shown = 120
        app._saved_niche_id = 18
        app._saved_refreshing = False
        app._saved_refreshed_at = 0.0
        app._saved_refresh_failed_at = 0.0
        app._saved_result = None
        app.run_bg = lambda work, done, _msg, **_kw: done(work())
        app._invalidate = lambda _key: None
        app.toast = lambda _msg: None
        app._render = lambda: None
        return app

    def test_a_failed_refresh_is_not_reported_as_checked(self) -> None:
        app = self._app()
        with mock.patch.object(bs, "refresh_saved", side_effect=_LOCKED):
            app._saved_refresh(silent=True)
        self.assertEqual(app._saved_refreshed_at, 0.0)
        self.assertGreater(app._saved_refresh_failed_at, 0.0)
        self.assertFalse(app._saved_refreshing)

    def test_a_refresh_failure_is_not_shown_as_a_save_failure(self) -> None:
        app = self._app()
        with mock.patch.object(bs, "refresh_saved", side_effect=_LOCKED):
            app._saved_refresh(silent=True)
        words = " ".join(_texts(app._saved_result_card()))
        self.assertIn("Couldn't refresh live numbers", words)
        self.assertNotIn("Could not save", words)

    def test_after_a_failure_the_tab_does_not_retry_in_a_loop(self) -> None:
        app = self._app()
        with mock.patch.object(bs, "refresh_saved", side_effect=_LOCKED) as refresh:
            app._saved_refresh(silent=True)
            app._saved_autorefresh()           # what the next render would do
            app._saved_autorefresh()
        self.assertEqual(refresh.call_count, 1)

    def test_the_button_still_retries_right_away(self) -> None:
        app = self._app()
        with mock.patch.object(bs, "refresh_saved", side_effect=_LOCKED) as refresh:
            app._saved_refresh(silent=True)
            app._saved_refresh()               # Refresh live
        self.assertEqual(refresh.call_count, 2)

    def test_a_later_success_clears_the_failure(self) -> None:
        app = self._app()
        with mock.patch.object(bs, "refresh_saved", side_effect=_LOCKED):
            app._saved_refresh(silent=True)
        ok = {"channels": 1, "changed": 0, "subs_gained": 0, "rows": []}
        with mock.patch.object(bs, "refresh_saved", return_value=ok):
            app._saved_refresh_failed_at = 0.0  # as if the pause had passed
            app._saved_refresh(silent=True)
        self.assertIsNone(app._saved_result)
        self.assertGreater(app._saved_refreshed_at, 0.0)

    def test_growth_pill_is_read_from_the_row(self) -> None:
        app = self._app()
        app.pill = lambda text, *_a, **_k: SimpleNamespace(text=text)
        row = {"saved_at": "2026-09-14T21:34:26Z",
               "growth": {"snapshots": 6, "subs_gained": 8500, "subs_per_day": 5546.1,
                          "first_captured_at": "2026-09-14T22:17:14Z"}}
        self.assertEqual(app._growth_pill(row).text,
                         "+8.5K subs since saved · 5.5K/day")


if __name__ == "__main__":
    unittest.main()
