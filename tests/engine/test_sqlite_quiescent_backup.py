"""Quiescent recovery must retain exact bytes and refuse incomplete proofs."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from beacon_engine.research.postgres_migration import MigrationError, _validated_snapshot, file_hash
from beacon_engine.research.sqlite_quiescent_backup import compress_quiescent_sqlite, recover_quiescent_sqlite

class QuiescentBackupContracts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.sqlite3'
        self.archive = Path(self.tmp.name) / 'recovery.gz'
        self.recovered = Path(self.tmp.name) / 'recovered.sqlite3'
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('CREATE TABLE parents(id INTEGER PRIMARY KEY, text TEXT)')
            conn.execute('CREATE TABLE children(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parents(id))')
            conn.execute('INSERT INTO parents VALUES(1,?)', ('café 漢字\n\\u0000 literal',))
            conn.execute('INSERT INTO children VALUES(1,1)')

    def compress(self, **kwargs):
        return compress_quiescent_sqlite(self.source, self.archive, writers_paused=True, guard=lambda: True, **kwargs)

    def test_exact_bytes_restored_with_manifest_and_source_unchanged(self):
        before = self.source.read_bytes()
        report = self.compress()
        restored = recover_quiescent_sqlite(self.archive, self.recovered)
        self.assertEqual(report['source_sha256'], restored['sha256'])
        self.assertEqual(self.recovered.read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(_validated_snapshot(self.recovered), self.recovered)
        with self.assertRaises(MigrationError):
            self.compress()
        with self.assertRaises(MigrationError):
            recover_quiescent_sqlite(self.archive, self.recovered)

    def test_explicit_pause_and_live_guard_are_mandatory(self):
        for kwargs in ({}, {'writers_paused': True}, {'writers_paused': True, 'guard': lambda: False}):
            with self.assertRaises(MigrationError):
                compress_quiescent_sqlite(self.source, self.archive, **kwargs)
        self.assertFalse(self.archive.exists())

    def test_pending_wal_refuses_raw_copy(self):
        with closing(sqlite3.connect(self.source)) as writer, writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE parents SET text='pending in WAL'")
            writer.commit()
            with self.assertRaisesRegex(MigrationError, 'pending writes'):
                self.compress()
        self.assertFalse(Path(str(self.archive) + '.json').exists())

    def test_broken_relationships_cannot_publish_success(self):
        with closing(sqlite3.connect(self.source)) as writer, writer:
            writer.execute('INSERT INTO children VALUES(2,999)')
        with self.assertRaisesRegex(MigrationError, 'relationships'):
            self.compress()
        self.assertFalse(Path(str(self.archive) + '.json').exists())

    def test_source_change_during_compression_has_no_success_manifest(self):

        def mutate(item):
            if item['step'] == 'compress_quiescent_source':
                with closing(sqlite3.connect(self.source)) as writer, writer:
                    writer.execute("UPDATE parents SET text='changed while copying'")
        with self.assertRaisesRegex(MigrationError, 'source changed'):
            self.compress(progress=mutate)
        self.assertFalse(Path(str(self.archive) + '.json').exists())

    def test_corrupt_compressed_or_original_hash_cannot_publish_restored_success(self):
        self.compress()
        manifest = Path(str(self.archive) + '.json')
        report = json.loads(manifest.read_text())
        report['source_sha256'] = '0' * 64
        manifest.write_text(json.dumps(report))
        with self.assertRaisesRegex(MigrationError, 'original bytes'):
            recover_quiescent_sqlite(self.archive, self.recovered)
        self.assertFalse(Path(str(self.recovered) + '.json').exists())
        self.assertEqual(file_hash(self.archive), report['sha256'])

    def test_interrupted_guard_does_not_seal_an_archive(self):
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            return calls < 3
        with self.assertRaisesRegex(MigrationError, 'pause was lost'):
            compress_quiescent_sqlite(self.source, self.archive, writers_paused=True, guard=guard)
        self.assertFalse(Path(str(self.archive) + '.json').exists())

@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class QuiescentMigrationContracts(unittest.TestCase):
    from tests.engine.test_postgres_migration import MigrationContracts as _fixtures
    setUp = _fixtures.setUp
    _drop_schema = _fixtures._drop_schema
    _seed = _fixtures._seed
    _seed_at_fixed_time = _fixtures._seed_at_fixed_time
    legacy_tables = _fixtures.legacy_tables
    snapshot = _fixtures.snapshot

    def test_recovered_bytes_support_full_migration_and_final_catch_up(self):
        from beacon_engine.research.postgres_migration import snapshot_sqlite, migrate_snapshot
        from beacon_engine.research.postgres_reconcile import reconcile_snapshot
        source = self.snapshot()
        root = Path(self.tmp.name)
        for attempt in range(2):
            if attempt:
                with self.sqlite.connect() as conn:
                    conn.execute('UPDATE channels SET title=?', ('Updated café 漢字',))
                source = root / 'final.sqlite3'
                snapshot_sqlite(self.sqlite.path, source)
            archive, recovered = (root / ('copy' + str(attempt) + '.gz'), root / ('copy' + str(attempt) + '.sqlite3'))
            compress_quiescent_sqlite(source, archive, writers_paused=True, guard=lambda: True)
            recover_quiescent_sqlite(archive, recovered)
            report = migrate_snapshot(recovered, self.pg.database) if not attempt else reconcile_snapshot(recovered, self.pg.database, writers_paused=True)
            self.assertTrue(report['verified'])
            self.assertEqual(len(report['tables']), 30)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT title FROM channels').fetchone()[0], 'Updated café 漢字')
