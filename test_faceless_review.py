"""The AI faceless reviewer (faceless_review + its agent tools): what it offers, what it saves,
and that Muse's decisions are recorded and undo exactly. Runs on a scratch library that the
engine's own YouTubeStore creates, so the tables are the real ones."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import engine_link  # noqa: F401 — the engine on sys.path, before beacon_engine imports
from beacon_engine.research.store import YouTubeStore

import faceless_review as fr
import muse_powers as mp

NOW = "2026-09-29T20:00:00Z"


class FakeFinder:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def call(self, tool, **args):
        assert tool == "find_channels" and args["faceless_only"] and args["replicable_only"]
        self.calls += 1
        return self.rows[: args["limit"]]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="faceless-review-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(self.tmp / "library.sqlite3")
        YouTubeStore(self.db)                                   # the engine builds the schema
        fr._stores.clear()
        fr._rank_cache.clear()
        con = sqlite3.connect(self.db)
        ids = ["UCai", "UCsure", "UCunsure", "UCtext", "UCtext2"]
        for i, cid in enumerate(ids):
            con.execute("INSERT INTO channels (channel_id, title, subscribers, first_seen_at, "
                        "last_seen_at) VALUES (?, ?, ?, ?, ?)", (cid, cid, 5000 - i, NOW, NOW))
            for j in range(8):
                con.execute("INSERT INTO videos (video_id, title, channel_id, views, first_seen_at,"
                            " last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (f"{cid}v{j}", f"{cid} video {j}", cid, 1000 * j, NOW, NOW))
        con.execute("INSERT INTO channel_vlm_check VALUES ('UCai', 'faceless', 'Qwen', '', ?)", (NOW,))
        con.execute("INSERT INTO channel_face_check VALUES ('UCsure', 6, 0, 0, 'faceless', .9, ?)", (NOW,))
        con.execute("INSERT INTO channel_face_check VALUES ('UCunsure', 6, 3, 1, 'uncertain', .4, ?)", (NOW,))
        for cid in ("UCtext", "UCtext2"):
            con.execute("INSERT INTO channel_text_model VALUES (?, 1, .8, 'v', ?)", (cid, NOW))
        con.commit()
        con.close()
        self.finder = FakeFinder([{"channel_id": c, "title": c, "opportunity_score": 10 - i}
                                  for i, c in enumerate(ids)])

    def test_only_shaky_labels_are_offered_best_first(self):
        q = fr.review_queue(self.finder, self.db, limit=10)
        self.assertEqual([c["channel_id"] for c in q["channels"]], ["UCunsure", "UCtext", "UCtext2"])
        self.assertIn("unsure", q["channels"][0]["why_review"])
        self.assertIn("text judge", q["channels"][1]["why_review"])
        self.assertIn("faceless", q["how_to_decide"])

    def test_each_offer_carries_what_a_reviewer_needs_to_see(self):
        c = fr.review_queue(self.finder, self.db, limit=1)["channels"][0]
        self.assertTrue(c["url"].endswith("/UCunsure"))
        self.assertEqual(len(c["videos"]), 6)
        self.assertEqual(c["videos"][0]["title"], "UCunsure video 7")        # most viewed first
        self.assertTrue(c["videos"][0]["thumbnail"].startswith("https://i.ytimg.com/vi/"))

    def test_a_verdict_goes_through_the_engine_and_outranks_the_checks(self):
        out = fr.save_verdicts(self.db, [{"channel_id": "UCunsure", "verdict": "on_camera",
                                          "reason": "same host in every thumbnail"}], "muse")
        self.assertEqual((out["saved"], out["on_camera"]), (1, 1))
        label = fr.labels(self.db, ["UCunsure"])["UCunsure"]
        self.assertEqual((label["ai"], label["ai_model"]), ("on_camera", "review:muse"))
        self.assertNotIn("UCunsure", [c["channel_id"] for c in
                                      fr.review_queue(self.finder, self.db)["channels"]])

    def test_after_the_ranked_list_it_keeps_going_by_views_per_subscriber(self):
        con = sqlite3.connect(self.db)
        for cid, views, vids in (("UCunsure", 90_000, 10), ("UCtext", 10_000, 10),
                                 ("UCtext2", 500_000, 10)):
            con.execute("UPDATE channels SET view_count = ?, video_count = ? WHERE channel_id = ?",
                        (views, vids, cid))
        # shown as faceless = the text judge says so while the face check was unsure
        con.execute("INSERT INTO channel_text_model VALUES ('UCunsure', 1, .7, 'v', ?)", (NOW,))
        con.commit()
        con.close()
        only_decided = FakeFinder([{"channel_id": "UCai"}, {"channel_id": "UCsure"}])
        q = fr.review_queue(only_decided, self.db, limit=3)
        self.assertEqual([c["channel_id"] for c in q["channels"]], ["UCtext2", "UCunsure", "UCtext"])
        self.assertIn("views per subscriber", q["source"])
        self.assertEqual(q["left_to_review_estimate"], 3)

    def test_stars_are_not_worth_a_reviewers_time(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE channels SET subscribers = 46400000, view_count = 9e12, video_count = 10 "
                    "WHERE channel_id = 'UCtext2'")
        con.commit()
        con.close()
        deep = [r["channel_id"] for r, _ in fr._deep(self.db, 5, set())]
        self.assertNotIn("UCtext2", deep)

    def test_the_count_left_shrinks_as_decisions_arrive(self):
        self.assertEqual(fr.remaining(self.db), 2)                 # UCtext + UCtext2
        fr.save_verdicts(self.db, [{"channel_id": "UCtext", "verdict": "faceless"}], "muse")
        self.assertEqual(fr.remaining(self.db), 1)
        fr.save_verdicts(self.db, [{"channel_id": "UCtext2", "verdict": "unclear"}], "muse")
        self.assertEqual(fr.remaining(self.db), 0)                 # a reviewer's "unclear" is done

    def test_bad_input_is_refused_not_guessed(self):
        out = fr.save_verdicts(self.db, [{"channel_id": "nope", "verdict": "faceless"},
                                         {"channel_id": "UCtext", "verdict": "maybe"}], "muse")
        self.assertEqual(out["saved"], 0)
        self.assertEqual(len(out["refused"]), 2)

    def test_unclear_from_a_reviewer_is_not_offered_again_but_an_undo_is(self):
        fr.save_verdicts(self.db, [{"channel_id": "UCtext", "verdict": "unclear"}], "muse")
        self.assertIsNone(fr.needs_review(fr.labels(self.db, ["UCtext"])["UCtext"]))
        fr.save_verdicts(self.db, [{"channel_id": "UCtext", "verdict": "unclear"}], "undo")
        self.assertIsNotNone(fr.needs_review(fr.labels(self.db, ["UCtext"])["UCtext"]))


class MuseReviewerTests(ReviewTests):
    """Through muse_powers: the agent's dispatcher, the record, the undo."""

    def setUp(self):
        super().setUp()
        import beacon_agent
        # Point the agent at the scratch library, then put back exactly what was there — a
        # bare pop() would remove the real setting for every test that runs after this one.
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {"YOUTUBE_INDEX_DB": self.db})
        patcher.start()
        self.addCleanup(patcher.stop)

        class Sweep:
            pass

        agent = beacon_agent.BeaconAgent(self.finder, Sweep(), ui=None)
        self.powers = mp.MusePowers.for_beacon(agent, db_path=self.db,
                                               changes_path=self.tmp / "changes.jsonl",
                                               read_config=dict, reviewer="muse")

    def test_muse_decides_it_is_recorded_as_muse_and_undo_restores(self):
        ok, out = self.powers.run("set_faceless_verdict", {"reviewer": "someone-else", "verdicts": [
            {"channel_id": "UCunsure", "verdict": "on_camera"},
            {"channel_id": "UCai", "verdict": "on_camera"}]})
        self.assertTrue(ok)
        self.assertTrue(out["can_undo"])
        self.assertEqual(fr.labels(self.db, ["UCunsure"])["UCunsure"]["ai_model"], "review:muse")
        self.assertEqual(mp.describe(self.powers.log.all()[-1]), "Checked faceless: 2 on camera")
        ok, _ = self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertTrue(ok)
        after = fr.labels(self.db, ["UCunsure", "UCai"])
        self.assertEqual(after["UCai"]["ai"], "faceless")               # its old AI verdict back
        self.assertEqual(after["UCunsure"]["ai"], "unclear")            # none before → neutral
        self.assertIsNotNone(fr.needs_review(after["UCunsure"]))        # and back in the queue

    def test_the_queue_is_offered_to_muse(self):
        ok, out = self.powers.run("faceless_review_queue", {"limit": 2})
        self.assertTrue(ok)
        self.assertEqual(len(out["channels"]), 2)
        self.assertEqual(self.powers.recent()["changes"], [])            # reading is not a change


if __name__ == "__main__":
    unittest.main()
