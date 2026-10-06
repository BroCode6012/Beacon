from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from beacon_engine.research.sqlite_snapshot_stream import export_snapshot, receive_snapshot
from beacon_engine.research.postgres_migration import MigrationError, _validated_snapshot

class StreamedSnapshotTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.sqlite3'
        self.destination = Path(self.tmp.name) / 'recovery.sqlite3'
        self.writer = sqlite3.connect(self.source)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.executescript('CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT,number INTEGER); CREATE INDEX record_number ON records(number); CREATE TABLE child(id INTEGER PRIMARY KEY,record_id INTEGER REFERENCES records(id));')
        self.writer.executemany('INSERT INTO records(value,number) VALUES(?,?)', [('Unicode café 漢字\nquote "', 9000000000 + i) for i in range(10000)])
        self.writer.execute('INSERT INTO child VALUES(1,1)')
        self.writer.execute("UPDATE sqlite_sequence SET seq=12000 WHERE name='records'")
        self.writer.commit()

    def stream(self, progress=None):
        stream = io.BytesIO()
        export_snapshot(self.source, stream, progress=progress)
        stream.seek(0)
        return stream

    def test_stream_preserves_schema_values_relationships_indexes_and_highwater(self):
        report = receive_snapshot(self.stream(), self.destination)
        self.assertEqual(_validated_snapshot(self.destination), self.destination)
        self.assertEqual(report['tables'], {'records': 10000, 'child': 1})
        with closing(sqlite3.connect(self.destination)) as conn, conn:
            self.assertEqual(conn.execute('SELECT value,number FROM records WHERE id=1').fetchone(), ('Unicode café 漢字\nquote "', 9000000000))
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchone()[0], 'record_number')
            self.assertEqual(conn.execute('INSERT INTO records(value) VALUES(?)', ('new',)).lastrowid, 12001)

    def test_live_write_during_export_is_excluded_from_the_pinned_snapshot(self):
        changed = []

        def write(progress):
            if progress['table'] == 'child' and (not changed):
                changed.append(True)
                self.writer.execute("UPDATE records SET value='later live write' WHERE id=1")
                self.writer.commit()
        receive_snapshot(self.stream(progress=write), self.destination)
        self.assertEqual(self.writer.execute('SELECT value FROM records WHERE id=1').fetchone()[0], 'later live write')
        with closing(sqlite3.connect(self.destination)) as conn, conn:
            self.assertEqual(conn.execute('SELECT value FROM records WHERE id=1').fetchone()[0], 'Unicode café 漢字\nquote "')

    def test_incomplete_transfer_has_no_success_manifest_and_never_overwrites(self):
        encoded = self.stream().getvalue()
        with self.assertRaises((MigrationError, EOFError)):
            receive_snapshot(io.BytesIO(encoded[:-50]), self.destination)
        self.assertFalse(Path(str(self.destination) + '.json').exists())
        with self.assertRaises(MigrationError):
            receive_snapshot(io.BytesIO(encoded), self.destination)
        self.assertEqual(self.writer.execute('SELECT COUNT(*) FROM records').fetchone()[0], 10000)

    def test_corrupt_compressed_transfer_is_not_marked_complete(self):
        encoded = bytearray(self.stream().getvalue())
        encoded[-8] ^= 1
        with self.assertRaises((MigrationError, gzip.BadGzipFile)):
            receive_snapshot(io.BytesIO(encoded), self.destination)
        self.assertFalse(Path(str(self.destination) + '.json').exists())

    def test_broken_relationship_is_reported_without_altering_source(self):
        self.writer.execute('UPDATE child SET record_id=999999')
        self.writer.commit()
        with self.assertRaisesRegex(MigrationError, 'broken relationships'):
            receive_snapshot(self.stream(), self.destination)
        self.assertFalse(Path(str(self.destination) + '.json').exists())
import gzip
