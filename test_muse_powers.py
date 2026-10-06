"""Muse as Beacon's agent (muse_powers): the agent's own tools, and a trail that can be undone.

The agent is a stand-in that edits a scratch library the way the finder would, so what is under
test is the paper trail: what gets recorded, and whether undo_change really reverses it.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import muse_powers as mp


class FakeAgent:
    """Does to a scratch library what the finder's tools do to the real one."""

    def __init__(self, db: Path):
        self.db = db
        self.calls: list[tuple[str, dict]] = []
        self.config = {"harvest_per_cycle": 50, "min_subs_to_process": 700}
        self.watchlist = ["old query"]

    def _con(self):
        return sqlite3.connect(self.db)

    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        with self._con() as con:
            if name == "exclude_channels":
                con.executemany("INSERT OR REPLACE INTO channel_exclusions VALUES (?, ?)",
                                [(c, args.get("reason", "")) for c in args["channel_ids"]])
                return {"excluded": len(args["channel_ids"])}, False
            if name == "unexclude_channels":
                con.executemany("DELETE FROM channel_exclusions WHERE channel_id = ?",
                                [(c,) for c in args["channel_ids"]])
                return {"unexcluded": len(args["channel_ids"])}, False
            if name == "exclude_by_filter":
                rows = con.execute("SELECT channel_id FROM channels WHERE topic = ?",
                                   (args.get("topic"),)).fetchall()
                con.executemany("INSERT OR REPLACE INTO channel_exclusions VALUES (?, ?)",
                                [(r[0], args["reason"]) for r in rows])
                return {"matched": len(rows), "excluded": len(rows)}, False
            if name == "run_junk_filter":
                con.execute("INSERT OR REPLACE INTO channel_exclusions VALUES ('UCjunk', 'music')")
                return {"excluded": 1}, False
            if name == "set_channel_topic":
                con.executemany("UPDATE channels SET topic = ? WHERE channel_id = ?",
                                [(args["topic"], c) for c in args["channel_ids"]])
                return {"updated": len(args["channel_ids"])}, False
        if name == "set_scraper_config":
            self.config.update(args)
            return {"applied": dict(args)}, False
        if name == "add_watchlist_queries":
            accepted = [q for q in args["queries"] if q not in self.watchlist]
            self.watchlist += accepted
            return {"accepted": accepted, "rejected": []}, False
        if name == "remove_watchlist_queries":
            self.watchlist = [q for q in self.watchlist if q not in args["queries"]]
            return {"removed": 1}, False
        if name == "create_niche":
            return {"niche_id": 12, "name": args["name"]}, False
        if name == "crawl_niche":
            return {"error": "the crawler is busy"}, True
        return {"rows": [], "tool": name}, False


class PowersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="muse-powers-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "library.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE channel_exclusions (channel_id TEXT PRIMARY KEY, reason TEXT)")
            con.execute("CREATE TABLE channels (channel_id TEXT PRIMARY KEY, topic TEXT)")
            con.executemany("INSERT INTO channels VALUES (?, ?)", [
                ("UCa", "History"), ("UCb", "History"), ("UCc", "Space & Physics"),
                ("UCd", "Tech & AI")])
            con.execute("INSERT INTO channel_exclusions VALUES ('UCd', 'hidden long ago')")
        self.agent = FakeAgent(self.db)
        self.powers = mp.MusePowers.for_beacon(
            self.agent, db_path=self.db, changes_path=self.tmp / "changes.jsonl",
            read_config=lambda: dict(self.agent.config))

    def excluded(self):
        with sqlite3.connect(self.db) as con:
            return {r[0] for r in con.execute("SELECT channel_id FROM channel_exclusions")}

    # -- what Muse is offered ------------------------------------------------------------
    def test_offers_the_agents_tools_but_not_the_ones_that_drive_a_window(self):
        for tool in ("exclude_by_filter", "start_scraper", "add_watchlist_queries",
                     "save_channel", "find_channels", "recent_changes", "undo_change"):
            self.assertIn(tool, self.powers.names)
        for tool in ("show_channels", "open_view", "clear_channel_filters"):
            self.assertNotIn(tool, self.powers.names)
        ok, out = self.powers.run("open_view", {"view": "Channels"})
        self.assertFalse(ok)
        self.assertEqual(self.agent.calls, [])

    def test_the_guide_carries_the_agents_rules(self):
        for rule in ("count_channels", "What I did", "undo_change", "Never exclude with no filter"):
            self.assertIn(rule, mp.GUIDE)

    # -- recording -----------------------------------------------------------------------
    def test_reading_leaves_no_trail(self):
        ok, _ = self.powers.run("find_channels", {"topic": "History"})
        self.assertTrue(ok)
        self.assertEqual(self.powers.recent()["changes"], [])

    def test_a_change_is_recorded_and_tells_muse_its_id(self):
        ok, out = self.powers.run("exclude_channels", {"channel_ids": ["UCa"]})
        self.assertTrue(ok)
        self.assertTrue(out["can_undo"])
        change = self.powers.recent()["changes"][0]
        self.assertEqual((change["change_id"], change["tool"]), (out["change_id"],
                                                                 "exclude_channels"))

    def test_intent_is_durable_before_the_agent_writes(self):
        original = self.agent.execute

        def execute(name, args):
            self.assertEqual(self.powers.log.all()[0]["pending"], True)
            return original(name, args)

        self.agent.execute = execute
        with patch.object(mp.os, "fsync", wraps=mp.os.fsync) as fsync:
            ok, out = self.powers.run("exclude_channels", {"channel_ids": ["UCa"]})
        self.assertTrue(ok)
        self.assertEqual(fsync.call_count, 2)
        self.assertEqual(len(self.powers.log.path.read_text().splitlines()), 2)
        self.assertEqual(len(self.powers.log.all()), 1)
        self.assertEqual(self.powers.log.get(out["change_id"])["phase"], "finished")

    def test_no_write_when_audit_cannot_start(self):
        with patch.object(self.powers.log, "append", side_effect=OSError("disk full")):
            ok, out = self.powers.run("exclude_channels", {"channel_ids": ["UCa"]})
        self.assertFalse(ok)
        self.assertIn("no change was made", out["error"])
        self.assertEqual(self.agent.calls, [])
        self.assertNotIn("UCa", self.excluded())

    def test_missing_result_stays_visible_and_cannot_be_undone_blindly(self):
        original = self.powers.log.append
        calls = 0

        def append(record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original(record)

        with patch.object(self.powers.log, "append", side_effect=append):
            ok, out = self.powers.run("exclude_channels", {"channel_ids": ["UCa"]})
        self.assertFalse(ok)
        self.assertIn("may have happened", out["error"])
        self.assertIn("UCa", self.excluded())
        pending = self.powers.recent()["changes"][0]
        self.assertTrue(pending["pending"])
        self.assertFalse(pending["can_undo"])
        self.assertFalse(self.powers.undo(pending["change_id"])[0])

    def test_a_failed_change_is_recorded_as_failed_with_nothing_to_undo(self):
        ok, out = self.powers.run("crawl_niche", {"query": "lost cities"})
        self.assertFalse(ok)
        change = self.powers.recent()["changes"][0]
        self.assertFalse(change["ok"])
        self.assertFalse(change["can_undo"])

    # -- undo, per kind of change -----------------------------------------------------------
    def test_undo_brings_back_only_what_this_change_hid(self):
        _, out = self.powers.run("exclude_channels", {"channel_ids": ["UCa", "UCd"]})
        ok, _ = self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertTrue(ok)
        self.assertEqual(self.excluded(), {"UCd"})       # hidden long ago: stays hidden

    def test_a_bulk_hide_is_tagged_and_undone_channel_by_channel(self):
        _, out = self.powers.run("exclude_by_filter", {"topic": "History", "reason": "junk"})
        self.assertEqual(self.excluded(), {"UCa", "UCb", "UCd"})
        reason = self.agent.calls[-1][1]["reason"]
        self.assertTrue(reason.startswith(f"muse:{out['change_id']}"))
        ok, _ = self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertTrue(ok)
        self.assertEqual(self.excluded(), {"UCd"})

    def test_the_junk_filters_new_exclusions_can_be_undone(self):
        _, out = self.powers.run("run_junk_filter", {})
        self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertEqual(self.excluded(), {"UCd"})

    def test_topics_go_back_to_what_they_were(self):
        _, out = self.powers.run("set_channel_topic",
                                 {"channel_ids": ["UCa", "UCc"], "topic": "Mysteries"})
        self.powers.run("undo_change", {"change_id": out["change_id"]})
        with sqlite3.connect(self.db) as con:
            topics = dict(con.execute("SELECT channel_id, topic FROM channels"))
        self.assertEqual((topics["UCa"], topics["UCc"]), ("History", "Space & Physics"))

    def test_scraper_settings_go_back_to_their_old_values(self):
        _, out = self.powers.run("set_scraper_config", {"harvest_per_cycle": 200})
        self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertEqual(self.agent.config["harvest_per_cycle"], 50)

    def test_only_the_searches_that_were_accepted_are_removed_again(self):
        _, out = self.powers.run("add_watchlist_queries", {"queries": ["old query", "new one"]})
        self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertEqual(self.agent.watchlist, ["old query"])

    def test_undo_is_itself_recorded_and_cannot_run_twice(self):
        _, out = self.powers.run("exclude_channels", {"channel_ids": ["UCb"]})
        self.powers.run("undo_change", {"change_id": out["change_id"]})
        latest = self.powers.recent()["changes"][0]
        self.assertEqual(latest["undo_of"], out["change_id"])
        ok, again = self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertFalse(ok)
        self.assertIn("already undone", again["error"])

    def test_what_cannot_be_undone_says_why(self):
        _, out = self.powers.run("create_niche", {"name": "Lost Cities"})
        self.assertFalse(out["can_undo"])
        ok, reply = self.powers.run("undo_change", {"change_id": out["change_id"]})
        self.assertFalse(ok)
        self.assertIn("workspace", reply["why"])

    def test_an_unknown_change_id(self):
        ok, reply = self.powers.run("undo_change", {"change_id": "nope"})
        self.assertFalse(ok)
        self.assertIn("recent_changes", reply["error"])


class DescribeTests(unittest.TestCase):
    """What the Agent tab shows for each recorded change — plain words, real counts."""

    def test_plain_sentences(self):
        d = mp.describe
        self.assertEqual(d({"tool": "exclude_channels", "ok": True,
                            "args": {"channel_ids": ["a", "b", "c"]}}), "Hid 3 channels")
        self.assertEqual(d({"tool": "exclude_by_filter", "ok": True, "args": {},
                            "result": {"excluded": 1}}), "Hid 1 channel matching a filter")
        self.assertEqual(d({"tool": "add_watchlist_queries", "ok": True,
                            "args": {"queries": ["a", "b"]}, "result": {"accepted": ["a"]}}),
                         "Added 1 of 2 searches")
        self.assertEqual(d({"tool": "stop_scraper", "ok": True}), "Stopped the scraper")

    def test_refusals_and_undos_say_so(self):
        d = mp.describe
        self.assertEqual(d({"tool": "exclude_by_filter", "ok": False, "args": {}}),
                         "Tried to hide channels matching a filter — refused, nothing changed")
        self.assertEqual(d({"tool": "unexclude_channels", "ok": True, "undo_of": "x",
                            "args": {"channel_ids": ["a"]}}), "Undo: brought back 1 channel")


class LastVisitTests(unittest.TestCase):
    def test_only_real_visits_count(self):
        tmp = Path(tempfile.mkdtemp(prefix="muse-log-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log = tmp / "muse_gateway.log"
        log.write_text("\n".join([
            "[2026-09-29 18:58:57] GET /guide -> 200",
            "[2026-09-29 19:00:09] recent_changes {} -> ok in 0.0s (agent access)",
            "[2026-09-29 19:05:00] REFUSED GET /health — no valid key",
            "[2026-09-29 19:06:00] db_stats counted in the background in 364s",
            "[2026-09-29 19:07:00] gateway up on http://127.0.0.1:8770 — 48 tools",
        ]) + "\n", encoding="utf-8")
        visit = mp.last_outside_visit(log)
        self.assertEqual(visit.strftime("%H:%M:%S"), "19:00:09")

    def test_no_log_means_no_visit(self):
        self.assertIsNone(mp.last_outside_visit(Path("does-not-exist.log")))


if __name__ == "__main__":
    unittest.main()
