"""Contracts for the move kit (beacon_move).

A move is only allowed to change the target once everything is proven identical: a damaged
or missing part, a count mismatch, or a secret in the code must stop it BEFORE anything is
replaced — and a dropped upload must resume, not restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import beacon_move as bm


def make_library(path: Path, channels: int = 40, videos_per: int = 25, wal: bool = True):
    con = sqlite3.connect(str(path))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE channels (channel_id TEXT PRIMARY KEY, title TEXT, blob BLOB)")
    con.execute("CREATE TABLE videos (video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT)")
    for c in range(channels):
        con.execute("INSERT INTO channels VALUES (?, ?, ?)",
                    (f"UC{c:04d}", f"Channel {c}", os.urandom(40_000)))   # ~1.6 MB, spreads parts
        con.executemany("INSERT INTO videos VALUES (?, ?, ?)",
                        [(f"v{c}-{v}", f"UC{c:04d}", f"Video {v}") for v in range(videos_per)])
    con.execute("PRAGMA user_version=8")
    con.commit()
    con.close()


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beacon-move-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "source.sqlite3"
        make_library(self.src)
        self.out = self.tmp / "package"
        self.target = self.tmp / "target" / "youtube_index.sqlite3"
        quiet = mock.patch.object(bm, "say", lambda _message: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def pack(self):
        return bm.pack(self.src, self.out, part_mb=1)

    def test_a_library_arrives_byte_for_byte_with_the_same_counts(self):
        manifest = self.pack()
        self.assertGreater(len(manifest["parts"]), 1)             # really split
        self.assertEqual(manifest["database"]["counts"], {"channels": 40, "videos": 1000})
        self.assertEqual(manifest["database"]["health_check"], "ok")   # checked at the source
        self.assertFalse((self.out / "snapshot.sqlite3").exists())   # parts ARE the snapshot
        report = bm.receive(self.out, self.target)
        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"], {"channels": 40, "videos": 1000})
        self.assertEqual(bm.sha256_file(self.target), manifest["database"]["raw_sha256"])

    def test_the_snapshot_is_consistent_while_another_program_holds_the_library(self):
        writer = sqlite3.connect(str(self.src))
        writer.execute("INSERT INTO videos VALUES ('uncommitted', 'UC0000', 'not yet')")
        try:
            manifest = self.pack()                               # a write is in flight
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(manifest["database"]["counts"]["videos"], 1000)

    def test_a_damaged_part_stops_the_move_and_the_old_library_is_untouched(self):
        manifest = self.pack()
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"the library that is already there")
        part = self.out / manifest["parts"][1]["name"]
        data = bytearray(part.read_bytes())
        data[100] ^= 0xFF
        part.write_bytes(bytes(data))
        with self.assertRaises(bm.MoveError) as caught:
            bm.receive(self.out, self.target)
        self.assertIn("damaged", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"the library that is already there")

    def test_a_missing_part_stops_the_move(self):
        manifest = self.pack()
        (self.out / manifest["parts"][-1]["name"]).unlink()
        with self.assertRaises(bm.MoveError) as caught:
            bm.receive(self.out, self.target)
        self.assertIn("missing", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_the_old_library_and_its_journal_files_move_aside_together(self):
        self.pack()
        self.target.parent.mkdir(parents=True)
        for suffix, data in (("", b"old db"), ("-wal", b"old wal"), ("-shm", b"old shm")):
            Path(str(self.target) + suffix).write_bytes(data)
        bm.receive(self.out, self.target)
        previous = Path(str(self.target) + ".previous")
        self.assertEqual(previous.read_bytes(), b"old db")
        self.assertEqual(Path(str(previous) + "-wal").read_bytes(), b"old wal")
        # the OLD journal must never sit next to the NEW library — it would be replayed into it
        self.assertFalse(Path(str(self.target) + "-wal").exists())
        self.assertFalse(Path(str(self.target) + "-shm").exists())

    def test_a_resumed_upload_sends_only_missing_or_damaged_parts(self):
        manifest = self.pack()
        names = [p["name"] for p in manifest["parts"]]
        remote = {names[0]: manifest["parts"][0]["sha256"],        # arrived intact
                  names[1]: "0" * 64}                             # arrived damaged
        todo = [p["name"] for p in bm.parts_to_send(manifest, remote)]
        self.assertNotIn(names[0], todo)
        self.assertIn(names[1], todo)
        self.assertEqual(len(todo), len(names) - 1)

    def test_parts_are_plain_gzip(self):
        import gzip
        manifest = self.pack()
        first = self.out / manifest["parts"][0]["name"]
        self.assertEqual(len(gzip.decompress(first.read_bytes())),
                         manifest["parts"][0]["raw_bytes"])


class CodePackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beacon-code-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.beacon, self.engine = self.tmp / "Beacon", self.tmp / "Engine"
        for repo in (self.beacon, self.engine):
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            (repo / ".gitignore").write_text(".env\nstorage/\n", encoding="utf-8")
            (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")       # ignored
            (repo / "storage").mkdir()
            (repo / "storage" / "db.sqlite3").write_bytes(b"data")         # ignored

    def test_a_clean_package_contains_only_beacon_and_leaves_ignored_files_out(self):
        import tarfile
        out = self.tmp / "code.tar.gz"
        report = bm.package_code(out, beacon=self.beacon)
        with tarfile.open(out) as tar:
            names = sorted(tar.getnames())
        self.assertIn("Beacon/app.py", names)
        self.assertFalse(any(n.startswith("Quill/") for n in names))
        self.assertFalse(any(".env" in n or "sqlite3" in n for n in names))
        self.assertEqual(report["files"], len(names))

    def test_a_secret_in_the_code_stops_the_package(self):
        fake_key = "sk-" + "a1B2" * 8                      # built here, never written literally
        (self.beacon / "config.py").write_text(f"KEY = '{fake_key}'\n", encoding="utf-8")
        with self.assertRaises(bm.MoveError) as caught:
            bm.package_code(self.tmp / "code.tar.gz", beacon=self.beacon)
        self.assertIn("Beacon/config.py", str(caught.exception))
        self.assertFalse((self.tmp / "code.tar.gz").exists())


class HealthCheckTests(MoveTests):
    def test_the_target_can_still_re_check_on_request(self):
        self.pack()
        report = bm.receive(self.out, self.target, check="quick")
        self.assertEqual(report["health_check"], "quick")

    def test_a_damaged_source_is_refused_before_anything_is_sent(self):
        from unittest import mock
        with mock.patch.object(bm, "health_check", lambda _db: "*** page 7: corrupt"):
            with self.assertRaises(bm.MoveError) as caught:
                self.pack()
        self.assertIn("health check", str(caught.exception))
        self.assertFalse(list(self.out.glob("part-*.gz")))       # nothing packed
        self.assertFalse((self.out / "snapshot.sqlite3").exists())   # and no copy left behind


class SettingsCarryTests(unittest.TestCase):
    """The scraper's memory travels too — and lands pointing at the TARGET's library."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beacon-settings-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "laptop-engine"
        (self.src / "models").mkdir(parents=True)
        self.db = self.tmp / "laptop-lib" / "youtube_index.sqlite3"
        self.db.parent.mkdir()
        self.queue = self.src / "radar_work_queue.sqlite3"
        self.config = {"db_path": r"D:\Youtube\Beacon\storage\youtube_index.sqlite3",
                       "work_queue_path": str(self.queue), "face_check_workers": 10,
                       "harvest_enabled": True, "watchlist": [{"query": "lost history"}]}
        (self.src / "scraper_watchlist.json").write_text(json.dumps(self.config))
        for name in ("harvest_yield.json", "harvest_cursor.json", "niche_cursor.json"):
            (self.src / name).write_text(json.dumps({"cursor": 3766, "file": name}))
        for name in ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"):
            (self.src / "models" / name).write_bytes(os.urandom(2048))
        (self.db.parent / "faceless_text_model.joblib").write_bytes(os.urandom(4096))
        con = sqlite3.connect(self.queue)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, what TEXT)")
        con.executemany("INSERT INTO jobs (what) VALUES (?)", [("catalog",), ("enrich",)])
        con.commit()                     # left OPEN with a WAL: the copy must still include it
        self.addCleanup(con.close)
        self.package = self.tmp / "out" / "beacon_settings.tar.gz"
        self.target = self.tmp / "server" / "engine-storage"
        self.target_db = self.tmp / "server" / "data" / "youtube_index.sqlite3"

    def carry(self, **kw):
        bm.pack_settings(self.package, self.src, self.db)
        return bm.apply_settings(self.package, self.target, self.target_db, **kw)

    def test_everything_arrives_byte_for_byte(self):
        self.carry()
        for name in bm.ENGINE_FILES:
            if name != bm.CONFIG:
                self.assertEqual((self.target / name).read_bytes(),
                                 (self.src / name).read_bytes(), name)
        self.assertEqual((self.target_db.parent / "faceless_text_model.joblib").read_bytes(),
                         (self.db.parent / "faceless_text_model.joblib").read_bytes())

    def test_config_points_at_the_targets_library(self):
        self.carry()
        cfg = json.loads((self.target / "scraper_watchlist.json").read_text())
        self.assertEqual(cfg["db_path"], str(self.target_db))
        self.assertEqual(cfg["work_queue_path"], "")          # = beside the library
        self.assertEqual(cfg["watchlist"], self.config["watchlist"])   # searches unchanged
        self.assertNotIn("D:", json.dumps(cfg))

    def test_work_queue_copied_consistently_beside_the_library(self):
        self.carry()
        con = sqlite3.connect(self.target_db.parent / "radar_work_queue.sqlite3")
        try:
            self.assertEqual([r[0] for r in con.execute("SELECT what FROM jobs ORDER BY id")],
                             ["catalog", "enrich"])
        finally:
            con.close()

    def test_overrides_size_it_for_a_smaller_machine(self):
        self.carry(overrides=bm.parse_overrides(["face_check_workers=2",
                                                 "harvest_enabled=false"]))
        cfg = json.loads((self.target / "scraper_watchlist.json").read_text())
        self.assertEqual(cfg["face_check_workers"], 2)
        self.assertIs(cfg["harvest_enabled"], False)

    def test_misspelled_override_is_refused_before_anything_changes(self):
        bm.pack_settings(self.package, self.src, self.db)
        with self.assertRaises(bm.MoveError):
            bm.apply_settings(self.package, self.target, self.target_db,
                              overrides={"face_check_workrs": 2})
        self.assertFalse((self.target / "harvest_yield.json").exists())

    def test_bad_override_syntax(self):
        with self.assertRaises(bm.MoveError):
            bm.parse_overrides(["face_check_workers"])

    def test_damaged_package_changes_nothing(self):
        bm.pack_settings(self.package, self.src, self.db)
        import io
        import tarfile
        rebuilt = self.tmp / "tampered.tar.gz"
        with tarfile.open(self.package) as old, tarfile.open(rebuilt, "w:gz") as new:
            for member in old.getmembers():
                data = old.extractfile(member).read()
                if member.name == "engine/harvest_cursor.json":
                    data = b'{"cursor": 0}'
                    member.size = len(data)
                new.addfile(member, io.BytesIO(data))
        with self.assertRaises(bm.MoveError):
            bm.apply_settings(rebuilt, self.target, self.target_db)
        self.assertFalse(self.target.exists())

    def test_previous_files_are_kept(self):
        self.target.mkdir(parents=True)
        (self.target / "harvest_cursor.json").write_text('{"cursor": 1}')
        self.carry()
        self.assertEqual((self.target / "harvest_cursor.json.previous").read_text(),
                         '{"cursor": 1}')

    def test_old_queue_journal_is_moved_away_not_replayed(self):
        self.target_db.parent.mkdir(parents=True)
        stale = self.target_db.parent / "radar_work_queue.sqlite3-wal"
        stale.write_bytes(b"old journal")
        self.carry()
        self.assertFalse(stale.exists())
        self.assertTrue((self.target_db.parent /
                         "radar_work_queue.sqlite3.previous-wal").exists())

    def test_refuses_a_key_hiding_in_the_config(self):
        cfg = dict(self.config, note="AIza" + "x" * 35)
        (self.src / "scraper_watchlist.json").write_text(json.dumps(cfg))
        with self.assertRaises(bm.MoveError):
            bm.pack_settings(self.package, self.src, self.db)
        self.assertFalse(self.package.exists())

    def test_missing_model_is_named(self):
        (self.src / "models" / "face_recognition_sface_2021dec.onnx").unlink()
        with self.assertRaises(bm.MoveError) as ctx:
            bm.pack_settings(self.package, self.src, self.db)
        self.assertIn("sface", str(ctx.exception))

    def test_collector_plan_status_travels_when_present(self):
        (self.src / "edge_sync").mkdir()
        (self.src / "edge_sync" / "plan_sync_status.json").write_text('{"state": "unchanged"}')
        self.carry()
        self.assertEqual((self.target / "edge_sync" / "plan_sync_status.json").read_text(),
                         '{"state": "unchanged"}')
        self.assertFalse((self.target / "edge_sync" / "edge_health.json").exists())

    def test_nothing_else_from_the_folder_travels(self):
        (self.src / "radar.log").write_text("log")
        (self.src / ".env").write_text("YOUTUBE_API_KEYS=nope")
        bm.pack_settings(self.package, self.src, self.db)
        import tarfile
        with tarfile.open(self.package) as tar:
            names = set(tar.getnames())
        self.assertNotIn("engine/radar.log", names)
        self.assertNotIn("engine/.env", names)
        self.assertFalse(any(n.endswith(".tmp") for n in names))
        self.assertFalse(list(self.package.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
