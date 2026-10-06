"""A locally staged record archive retains the whole-copy verification contract."""
from contextlib import closing
import base64
import gzip
import io
import json
import os
import sqlite3
from pathlib import Path
import struct
import unittest
from hashlib import sha256
from beacon_engine.research.postgres_archive_migration import build_archive, migrate_archive
from beacon_engine.research.postgres_migration import MigrationError, file_hash, _write_report
from beacon_engine.research.sqlite_snapshot_stream import export_snapshot, _read_exact, _frame
from tests.engine import test_postgres_migration as fixtures

@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'), 'requires an explicit disposable PostgreSQL test database')
class ArchiveMigrationContracts(unittest.TestCase):
    setUp = fixtures.MigrationContracts.setUp
    _drop_schema = fixtures.MigrationContracts._drop_schema
    _seed = fixtures.MigrationContracts._seed
    _seed_at_fixed_time = fixtures.MigrationContracts._seed_at_fixed_time
    legacy_tables = fixtures.MigrationContracts.legacy_tables
    snapshot = fixtures.MigrationContracts.snapshot

    def _archive(self):
        snapshot = self.snapshot()
        destination = Path(self.tmp.name) / 'ordered.records.gz'
        report = build_archive(snapshot, destination)
        return (snapshot, destination, report)

    def test_ordered_archive_migrates_every_cell_and_original_sequence_clock(self):
        snapshot, path, manifest = self._archive()
        result = migrate_archive(path, self.pg.database)
        self.assertTrue(result['verified'])
        self.assertEqual(len(result['tables']), 30)
        self.assertEqual(result['source_sha256'], file_hash(snapshot))
        self.assertEqual(result['source_archive_sha256'], manifest['sha256'])
        self.assertEqual(result['tables']['videos']['rows'], 4)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT title FROM channels').fetchone()[0], 'History\nCafé 漢字')
            row = conn.execute("INSERT INTO niches(name,created_at,updated_at) VALUES('After archive',?,?) RETURNING id", (self.fixture_time, self.fixture_time)).fetchone()
            self.assertEqual(row[0], 82)
        with self.assertRaisesRegex(MigrationError, 'populated'):
            migrate_archive(path, self.pg.database)

    def test_original_descriptions_are_preserved_in_the_archive_and_explicit_audit(self):
        self.snapshot()
        original = 'café\x00漢字'
        with self.sqlite.connect() as conn:
            conn.execute('UPDATE channels SET description=?', (original,))
        from beacon_engine.research.postgres_migration import snapshot_sqlite
        snapshot = Path(self.tmp.name) / 'nul.sqlite3'
        snapshot_sqlite(self.sqlite.path, snapshot)
        archive = Path(self.tmp.name) / 'nul.records.gz'
        build_archive(snapshot, archive)
        with self.assertRaises(MigrationError):
            migrate_archive(archive, self.pg.database)
        result = migrate_archive(archive, self.pg.database, normalize_provider_descriptions=True)
        self.assertEqual(base64.b64decode(result['description_normalizations'][0]['original_utf8_base64']).decode(), original)
        with self.sqlite.connect() as conn:
            self.assertEqual(conn.execute('SELECT description FROM channels').fetchone()[0], original)

    def test_interrupted_copy_rolls_back_all_tables_and_the_sealed_archive_retries(self):
        _, archive, _ = self._archive()

        def interrupt(item):
            if item['step'] == 'copied' and item['table'] == 'channels':
                raise ConnectionError('simulated dropped operator link')
        with self.assertRaises(ConnectionError):
            migrate_archive(archive, self.pg.database, progress=interrupt)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 0)
        self.assertTrue(migrate_archive(archive, self.pg.database)['verified'])

    def test_unordered_or_truncated_archives_do_not_commit_partial_data(self):
        snapshot, ordered, manifest = self._archive()
        raw = io.BytesIO()
        export_snapshot(snapshot, raw)
        unordered = Path(self.tmp.name) / 'unordered.records.gz'
        unordered.write_bytes(raw.getvalue())
        report = dict(manifest, sha256=file_hash(unordered), bytes=unordered.stat().st_size)
        _write_report(str(unordered) + '.json', report)
        with self.assertRaisesRegex(MigrationError, 'ordered primary keys'):
            migrate_archive(unordered, self.pg.database)
        truncated = Path(self.tmp.name) / 'truncated.records.gz'
        truncated.write_bytes(ordered.read_bytes()[:-8])
        report.update(sha256=file_hash(truncated), bytes=truncated.stat().st_size)
        _write_report(str(truncated) + '.json', report)
        with self.assertRaises(Exception):
            migrate_archive(truncated, self.pg.database)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 0)

    def test_resealed_out_of_order_rows_roll_back_every_table(self):
        _, archive, manifest = self._archive()
        output = io.BytesIO()
        digest = sha256()
        active = None
        with gzip.open(archive, 'rb') as source, gzip.GzipFile(fileobj=output, mode='wb') as target:
            while True:
                size = struct.unpack('>I', _read_exact(source, 4))[0]
                part = json.loads(_read_exact(source, size))
                if part.get('complete'):
                    part['sha256'] = digest.hexdigest()
                    _frame(target, part, None)
                    break
                if 'table' in part:
                    active = part['table']
                if active == 'videos' and 'rows' in part:
                    part['rows'].reverse()
                _frame(target, part, digest)
        altered = Path(self.tmp.name) / 'out-of-order.records.gz'
        altered.write_bytes(output.getvalue())
        _write_report(str(altered) + '.json', dict(manifest, sha256=file_hash(altered), bytes=altered.stat().st_size))
        with self.assertRaisesRegex(MigrationError, 'strict primary-key order'):
            migrate_archive(altered, self.pg.database)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 0)

    def test_archive_tampering_and_existing_destinations_are_refused(self):
        snapshot, archive, _ = self._archive()
        with self.assertRaisesRegex(MigrationError, 'new file'):
            build_archive(snapshot, archive)
        archive.write_bytes(archive.read_bytes() + b'tampered')
        with self.assertRaisesRegex(MigrationError, 'verified manifest'):
            migrate_archive(archive, self.pg.database)

    def test_legacy_column_order_is_mapped_by_name(self):
        self.snapshot()
        with closing(sqlite3.connect(self.sqlite.path)) as conn, conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='videos'").fetchone()[0]
            columns = ddl[ddl.index('(') + 1:ddl.rindex(')')].split(',')
            normalized = next((column for column in columns if column.strip().startswith('published_at_normalized ')))
            columns.remove(normalized)
            columns.append(normalized)
            conn.execute('CREATE TABLE reordered_videos (' + ','.join(columns) + ')')
            names = [row[1] for row in conn.execute('PRAGMA table_info(videos)')]
            selected = ','.join(('"' + name + '"' for name in names))
            conn.execute('INSERT INTO reordered_videos (' + selected + ') SELECT ' + selected + ' FROM videos')
            conn.execute('DROP TABLE videos')
            conn.execute('ALTER TABLE reordered_videos RENAME TO videos')
        from beacon_engine.research.postgres_migration import snapshot_sqlite
        snapshot = Path(self.tmp.name) / 'reordered.sqlite3'
        snapshot_sqlite(self.sqlite.path, snapshot)
        archive = Path(self.tmp.name) / 'reordered.records.gz'
        build_archive(snapshot, archive)
        self.assertEqual(migrate_archive(archive, self.pg.database)['tables']['videos']['rows'], 4)
if __name__ == '__main__':
    unittest.main()
