"""Real COPY, exact-content comparison, refusal gates and native recovery."""
from contextlib import closing
import json
import base64
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch
from tests.engine import test_postgres_store as fixtures
from beacon_engine.research.postgres import PostgresDatabase
from beacon_engine.research.postgres_migration import MigrationError, snapshot_sqlite, migrate_snapshot, fingerprint_postgres, backup_postgres, restore_postgres, _native_environment, _sqlite_reader, ADMIN_SQLITE_CACHE_KIB

class SnapshotSafety(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.sqlite3'
        self.destination = Path(self.tmp.name) / 'snapshot.sqlite3'
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT)')
            conn.execute('INSERT INTO sample VALUES(1,?)', ('newlines\nUnicode: café',))

    def test_admin_scan_cache_is_bounded_and_cannot_modify_or_persist_source_settings(self):
        with closing(sqlite3.connect(self.source)) as source, source:
            original_cache = source.execute('PRAGMA cache_size').fetchone()[0]
        with _sqlite_reader(self.source) as reader:
            self.assertEqual(reader.execute('PRAGMA cache_size').fetchone()[0], -ADMIN_SQLITE_CACHE_KIB)
            self.assertLessEqual(ADMIN_SQLITE_CACHE_KIB, 512 * 1024)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("UPDATE sample SET value='forbidden'")
        with closing(sqlite3.connect(self.source)) as source, source:
            self.assertEqual(source.execute('PRAGMA cache_size').fetchone()[0], original_cache)
            self.assertEqual(source.execute('SELECT value FROM sample').fetchone()[0], 'newlines\nUnicode: café')

    def test_snapshot_is_integrity_checked_and_existing_files_are_never_overwritten(self):
        report = snapshot_sqlite(self.source, self.destination)
        self.assertEqual(report['integrity'], 'ok')
        with closing(sqlite3.connect(self.destination)) as conn, conn:
            self.assertEqual(conn.execute('SELECT value FROM sample').fetchone()[0], 'newlines\nUnicode: café')
        with self.assertRaises(MigrationError):
            snapshot_sqlite(self.source, self.destination)
        with self.assertRaises(MigrationError):
            snapshot_sqlite(self.source, self.source)

    def test_native_credentials_stay_in_environment_not_public_identifier(self):
        db = PostgresDatabase('dbname=test host=localhost user=owner password=synthetic-private')
        env, name = _native_environment(db)
        self.assertEqual(name, 'test')
        self.assertEqual(env['PGPASSWORD'], 'synthetic-private')
        self.assertEqual(env['PGDATABASE'], 'test')
        self.assertNotIn('synthetic-private', repr(db))

    def test_interrupted_snapshot_has_no_success_manifest_and_source_stays_intact(self):

        def fail(_):
            raise RuntimeError('synthetic interruption')
        with self.assertRaises(RuntimeError):
            snapshot_sqlite(self.source, self.destination, progress=fail)
        self.assertFalse(Path(str(self.destination) + '.json').exists())
        with closing(sqlite3.connect(self.source)) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sample').fetchone()[0], 1)

    def test_wal_source_produces_standalone_snapshot_and_later_writes_do_not_change_it(self):
        writer = sqlite3.connect(self.source)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE sample SET value='committed in WAL'")
        writer.commit()
        snapshot_sqlite(self.source, self.destination)
        writer.execute("UPDATE sample SET value='later live write'")
        writer.commit()
        with closing(sqlite3.connect(self.destination)) as restored, restored:
            self.assertEqual(restored.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(restored.execute('SELECT value FROM sample').fetchone()[0], 'committed in WAL')
        self.assertFalse(Path(str(self.destination) + '-wal').exists())

@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'), 'requires a disposable PostgreSQL test database')
class MigrationContracts(unittest.TestCase):
    setUp = fixtures.PostgresStoreContracts.setUp
    _drop_schema = fixtures.PostgresStoreContracts._drop_schema
    _seed = fixtures.PostgresStoreContracts._seed
    _seed_at_fixed_time = fixtures.PostgresStoreContracts._seed_at_fixed_time

    def legacy_tables(self):
        from beacon_engine.edge import importer
        with self.sqlite.connect() as conn:
            conn.executescript(LEGACY_LAB_DDL)
        with self.sqlite.connect() as conn:
            for ddl in (importer._IMPORT_LEDGER_SQL, importer._ORGANIZATION_IMPORT_JOBS_SQL, importer._ORGANIZATION_RUNS_SQL, importer._QUERY_RUNS_SQL):
                conn.execute(ddl)

    def snapshot(self):
        self.legacy_tables()
        self._seed(self.sqlite)
        with self.sqlite.connect() as conn:
            conn.execute('UPDATE channels SET title=?,raw_json=?', ('History\nCafé 漢字', '{"quote":"\\"","lines":"a\\nb"}'))
            conn.execute("INSERT INTO niches(id,name,created_at,updated_at) VALUES(47,'Saved from YouTube',?,?)", (self.fixture_time, self.fixture_time))
            conn.execute("INSERT INTO niche_channels(niche_id,channel_id,role,enabled,source,added_at) VALUES(47,?,'competitor',1,'manual',?)", (self.cid, self.fixture_time))
            conn.execute("UPDATE sqlite_sequence SET seq=81 WHERE name='niches'")
        path = Path(self.tmp.name) / 'snapshot.sqlite3'
        snapshot_sqlite(self.sqlite.path, path)
        return path

    def test_complete_migration_preserves_records_relationships_and_sequence_highwater(self):
        path = self.snapshot()
        result = migrate_snapshot(path, self.pg.database)
        self.assertTrue(result['verified'])
        self.assertEqual(len(result['tables']), 30)
        with self.pg.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT title FROM channels').fetchone()['title'], 'History\nCafé 漢字')
            self.assertEqual(conn.execute('SELECT niche_id FROM niche_channels').fetchone()['niche_id'], 47)
            added = conn.execute("INSERT INTO niches(name,created_at,updated_at) VALUES('Later',%s,%s) RETURNING id", (self.fixture_time, self.fixture_time)).fetchone()['id']
            self.assertEqual(added, 82)
        with self.assertRaisesRegex(MigrationError, 'populated'):
            migrate_snapshot(path, self.pg.database)

    def test_streamed_off_host_snapshot_migrates_with_exact_content_checks(self):
        import io
        from beacon_engine.research.sqlite_snapshot_stream import export_snapshot, receive_snapshot
        self.legacy_tables()
        self._seed(self.sqlite)
        stream = io.BytesIO()
        export_snapshot(self.sqlite.path, stream)
        stream.seek(0)
        path = Path(self.tmp.name) / 'streamed.sqlite3'
        receive_snapshot(stream, path)
        result = migrate_snapshot(path, self.pg.database)
        self.assertEqual(result['tables']['channels']['rows'], 1)
        self.assertEqual(result['tables']['videos']['rows'], 4)
        self.assertTrue(result['verified'])

    def test_migration_provisions_ordered_reads_and_fingerprint_restores_planner_setting(self):
        migrate_snapshot(self.snapshot(), self.pg.database)
        with self.pg.database.connect() as conn:
            conn.execute('SET LOCAL enable_sort=off')
            conn.execute('SET LOCAL enable_bitmapscan=off')
            plan = conn.execute('EXPLAIN (FORMAT JSON) SELECT video_id FROM videos ORDER BY video_id COLLATE "C"').fetchone()['QUERY PLAN'][0]['Plan']

            def nodes(node):
                return [node] + [part for child in node.get('Plans', []) for part in nodes(child)]
            selected = nodes(plan)
            self.assertFalse(any((n['Node Type'] == 'Sort' for n in selected)))
            self.assertTrue(any((n.get('Index Name') == 'beacon_migration_videos_pk_c' for n in selected)))
            for setting in ('on', 'off'):
                conn.execute('SET LOCAL enable_sort=' + setting)
                result = fingerprint_postgres(conn, self.schema)
                self.assertEqual(result['videos']['rows'], 4)
                self.assertEqual(conn.execute('SHOW enable_sort').fetchone()['enable_sort'], setting)

    def test_tampered_snapshot_or_missing_manifest_is_refused(self):
        path = self.snapshot()
        Path(str(path) + '.json').unlink()
        with self.assertRaisesRegex(MigrationError, 'manifest'):
            migrate_snapshot(path, self.pg.database)
        snapshot_sqlite(path, Path(self.tmp.name) / 'tampered.sqlite3')
        changed = Path(self.tmp.name) / 'tampered.sqlite3'
        with closing(sqlite3.connect(changed)) as conn, conn:
            conn.execute("UPDATE channels SET title='altered'")
        with self.assertRaisesRegex(MigrationError, 'manifest'):
            migrate_snapshot(changed, self.pg.database)

    def test_legacy_column_order_is_copied_by_name(self):
        self.legacy_tables()
        self._seed(self.sqlite)
        with closing(sqlite3.connect(self.sqlite.path)) as conn, conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='videos'").fetchone()[0]
            definitions = ddl[ddl.index('(') + 1:ddl.rindex(')')].split(',')
            normalized = next((d for d in definitions if d.strip().startswith('published_at_normalized ')))
            definitions.remove(normalized)
            definitions.append(normalized)
            conn.execute('CREATE TABLE videos_reordered (' + ','.join(definitions) + ')')
            names = [r[1] for r in conn.execute('PRAGMA table_info(videos)')]
            select = ','.join(('"' + c + '"' for c in names))
            conn.execute('INSERT INTO videos_reordered (' + select + ') SELECT ' + select + ' FROM videos')
            conn.execute('DROP TABLE videos')
            conn.execute('ALTER TABLE videos_reordered RENAME TO videos')
        path = Path(self.tmp.name) / 'reordered.sqlite3'
        snapshot_sqlite(self.sqlite.path, path)
        result = migrate_snapshot(path, self.pg.database)
        self.assertEqual(result['tables']['videos']['rows'], 4)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT views FROM videos ORDER BY video_id').fetchone()[0], 40000)

    def test_unrepresentable_text_rolls_back_all_copied_records(self):
        self.legacy_tables()
        self._seed(self.sqlite)
        with self.sqlite.connect() as conn:
            conn.execute('UPDATE videos SET title=?', ('embedded\x00null',))
        path = Path(self.tmp.name) / 'invalid.sqlite3'
        snapshot_sqlite(self.sqlite.path, path)
        for enabled in (False, True):
            with self.subTest(description_policy=enabled), self.assertRaisesRegex(MigrationError, 'cannot be preserved'):
                migrate_snapshot(path, self.pg.database, normalize_provider_descriptions=enabled)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 0)

    def test_description_nul_policy_is_explicit_audited_and_preserves_the_source(self):
        self.snapshot()
        original = 'café\x00漢字'
        with self.sqlite.connect() as conn:
            conn.execute('UPDATE channels SET description=?', (original,))
            conn.execute('UPDATE videos SET description=? WHERE video_id=(SELECT min(video_id) FROM videos)', (original,))
        path = Path(self.tmp.name) / 'descriptions.sqlite3'
        snapshot_sqlite(self.sqlite.path, path)
        with self.assertRaises(MigrationError):
            migrate_snapshot(path, self.pg.database)
        result = migrate_snapshot(path, self.pg.database, normalize_provider_descriptions=True)
        self.assertTrue(result['verified'])
        audit = result['description_normalizations']
        self.assertEqual({x['table'] for x in audit}, {'channels', 'videos'})
        self.assertEqual(len(audit), 2)
        self.assertTrue(all((base64.b64decode(x['original_utf8_base64']).decode() == original for x in audit)))
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT description FROM channels').fetchone()[0], 'café漢字')
        with self.sqlite.connect() as conn:
            self.assertEqual(conn.execute('SELECT description FROM channels').fetchone()[0], original)

    def test_schema_drift_is_refused_without_partial_copy(self):
        path = self.snapshot()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE undocumented(id INTEGER PRIMARY KEY)')
        verified = Path(self.tmp.name) / 'drift.sqlite3'
        snapshot_sqlite(path, verified)
        with self.assertRaisesRegex(MigrationError, 'inventories differ'):
            migrate_snapshot(verified, self.pg.database)

    @unittest.skipUnless(os.getenv('BEACON_TEST_PG_BIN'), 'requires native pg_dump/pg_restore on test host')
    def test_native_backup_restores_exact_records_into_empty_database_only(self):
        import psycopg
        from psycopg import sql
        self._seed(self.pg)
        archive = Path(self.tmp.name) / 'index.dump'
        binaries = os.environ['BEACON_TEST_PG_BIN']
        backup = backup_postgres(self.pg.database, archive, bin_directory=binaries)
        recovery_name = 'beacon_recovery_test_' + uuid4().hex
        admin = self.admin.open()
        admin.autocommit = True
        try:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(recovery_name)))
            recovery = PostgresDatabase(psycopg.conninfo.make_conninfo(self.dsn, dbname=recovery_name), schema=self.schema)
            result = restore_postgres(recovery, archive, bin_directory=binaries)
            self.assertTrue(result['verified'])
            self.assertEqual(result['tables'], backup['tables'])
            with self.assertRaisesRegex(MigrationError, 'populated'):
                restore_postgres(recovery, archive, bin_directory=binaries)
            with archive.open('ab') as file:
                file.write(b'corrupt')
            with self.assertRaisesRegex(MigrationError, 'manifest'):
                restore_postgres(recovery, archive, bin_directory=binaries)
        finally:
            admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(recovery_name)))
            admin.close()
LEGACY_LAB_DDL = "\n                CREATE TABLE IF NOT EXISTS labs (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    name TEXT NOT NULL UNIQUE,\n                    channel_id TEXT DEFAULT '',\n                    niche TEXT DEFAULT '',\n                    about TEXT DEFAULT '',\n                    audience TEXT DEFAULT '',\n                    voice TEXT DEFAULT '',\n                    goals TEXT DEFAULT '',\n                    created_at TEXT NOT NULL,\n                    updated_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS lab_videos (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    lab_id INTEGER NOT NULL REFERENCES labs(id),\n                    title TEXT NOT NULL,\n                    idea TEXT DEFAULT '',\n                    status TEXT NOT NULL DEFAULT 'idea',\n                    script_path TEXT DEFAULT '',\n                    video_url TEXT DEFAULT '',\n                    performance TEXT DEFAULT '',\n                    created_at TEXT NOT NULL,\n                    updated_at TEXT NOT NULL\n                );\n                CREATE INDEX IF NOT EXISTS idx_lab_videos_lab\n                    ON lab_videos(lab_id, status);\n                CREATE TABLE IF NOT EXISTS lab_notes (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    lab_id INTEGER NOT NULL REFERENCES labs(id),\n                    kind TEXT NOT NULL DEFAULT 'note',\n                    text TEXT NOT NULL,\n                    created_at TEXT NOT NULL\n                );\n                CREATE INDEX IF NOT EXISTS idx_lab_notes_lab\n                    ON lab_notes(lab_id, kind);\n                "
