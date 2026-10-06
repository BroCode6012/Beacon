"""Signed importer guarantees exercised against a disposable PostgreSQL schema."""

import os
import json
import base64
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from beacon_engine.edge.protocol import build_bundle, write_bundle
from beacon_engine.edge import importer
from beacon_engine.research.postgres import PostgresDatabase
from beacon_engine.research.postgres_store import PostgresYouTubeStore, POSTGRES_INDEX_TARGET
from beacon_engine.research.providers.base import ProviderChannel


@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'),
                     'requires an explicit disposable PostgreSQL test database')
class PostgresImporterContracts(unittest.TestCase):
    def setUp(self):
        self.admin = PostgresDatabase(os.environ['BEACON_TEST_POSTGRES_DSN'])
        self.schema = 'beacon_import_test_' + uuid4().hex
        with self.admin.connect() as conn:
            conn.execute('CREATE SCHEMA ' + self.schema)
        self.addCleanup(self._drop_schema)
        self.store = PostgresYouTubeStore(
            dsn=self.admin.dsn, schema=self.schema, initialize=True,
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = 'synthetic-import-test-signing-key'
        self.environment = patch.dict(os.environ, {
            'BEACON_POSTGRES_DSN': self.admin.dsn,
            'BEACON_POSTGRES_SCHEMA': self.schema,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _drop_schema(self):
        with self.admin.connect() as conn:
            conn.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    def bundle(self, label, *, invalid=False, organization=False):
        cid = 'UC' + label.ljust(22, '0')[:22]
        records = {
            'channels': [{'channel_id': cid, 'title': 'Imported History',
                          'subscribers': 1200, 'video_count': 24,
                          'view_count': 9_000_000_000}],
            'videos': [{'video_id': label, 'channel_id': cid,
                        'title': 'History', 'views': 9_000_000_000}],
        }
        metadata = {}
        if organization:
            metadata = {'source_job_revision': label, 'run_id': label}
            records['channel_face_checks'] = [{
                'channel_id': cid, 'verdict': 'faceless', 'confidence': .95,
            }]
            records['organization_runs'] = [{'channel_id': cid, 'status': 'ok'}]
        else:
            records['search_results'] = [{'video_id': label, 'query': 'history', 'provider': 'test'}]
            records['video_discoveries'] = [{'video_id': label, 'query': 'history'}]
            records['video_snapshots'] = [{'video_id': label, 'views': 9_000_000_000}]
            records['query_runs'] = [{
                'node_id': 'synthetic-node', 'job_id': label, 'run_id': label,
                'query': 'history', 'status': 'ok', 'result_count': 1,
            }]
        if invalid:
            # A genuine database failure after the parent row has been staged.
            records['videos'][0]['views'] = 10 ** 30
        bundle = build_bundle(
            node_id='synthetic-node',
            kind=importer.ORGANIZATION_BUNDLE_KIND if organization else importer.DISCOVERY_BUNDLE_KIND,
            job_id=label, records=records, metadata=metadata, signing_key=self.key,
        )
        return bundle, write_bundle(bundle, self.root / label)

    def test_signed_import_commits_before_ack_and_is_idempotent(self):
        bundle, path = self.bundle('first')
        real_ack = importer.write_ack
        checked = []

        def acknowledge(*args, **kwargs):
            with self.store.connect() as conn:
                checked.append(conn.execute(
                    'SELECT COUNT(*) FROM edge_import_batches WHERE bundle_id=?',
                    (bundle.bundle_id,),
                ).fetchone()[0])
            return real_ack(*args, **kwargs)

        with patch.object(importer, 'write_ack', side_effect=acknowledge):
            with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
                self.assertIsNone(session._wal_maintenance)
                first = session.import_path(path, signing_key=self.key, ack_dir=self.root / 'ack')
                second = session.import_path(path, signing_key=self.key, ack_dir=self.root / 'ack')
        self.assertEqual(checked, [1, 1])
        self.assertEqual(first.status, 'imported')
        self.assertEqual(second.status, 'duplicate')
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM video_discoveries').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT views FROM videos').fetchone()[0], 9_000_000_000)

    def test_database_error_rolls_back_one_bundle_and_keeps_its_neighbors(self):
        paths = [self.bundle(label, invalid=label == 'broken')[1]
                 for label in ('before', 'broken', 'after')]
        with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
            results = session.import_paths(paths, signing_key=self.key,
                                           ack_dir=self.root / 'ack', transaction_bundles=3)
        self.assertEqual([bool(r.error) for r in results], [False, True, False])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edge_import_batches').fetchone()[0], 2)
        self.assertEqual(len(list((self.root / 'ack').glob('*'))), 2)
        self.assertEqual([r.report.counts['channels_new'] for r in results if r.report], [1, 1])

    def test_new_channel_yield_excludes_existing_ids_and_replayed_bundles(self):
        _, path = self.bundle('yield')
        with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
            first = session.import_path(path, signing_key=self.key)
            duplicate = session.import_path(path, signing_key=self.key)
        self.assertEqual(first.counts['channels_new'], 1)
        self.assertEqual(first.counts['channels_existing'], 0)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.counts, first.counts)
        cid = 'UC' + 'yield'.ljust(22, '0')[:22]
        records = {'channels': [{'channel_id': cid, 'title': 'Updated'},
                                {'channel_id': cid, 'subscribers': 1500}]}
        bundle = build_bundle(node_id='synthetic-node', kind=importer.DISCOVERY_BUNDLE_KIND,
                              job_id='refresh', records=records, signing_key=self.key)
        path = write_bundle(bundle, self.root / 'refresh')
        with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
            refreshed = session.import_path(path, signing_key=self.key)
        self.assertEqual(refreshed.counts['channels'], 2)
        self.assertEqual(refreshed.counts['channels_new'], 0)
        self.assertEqual(refreshed.counts['channels_existing'], 1)
        with self.store.connect() as conn:
            row = conn.execute('SELECT title,subscribers FROM channels WHERE channel_id=?', (cid,)).fetchone()
            self.assertEqual(tuple(row), ('Updated', 1500))

    def test_signed_description_normalization_retains_original_bytes_before_ack(self):
        original = 'signed café\0漢字'
        cid = 'UC' + '2' * 22
        records = {'channels': [{'channel_id': cid, 'description': original}],
                   'videos': [{'video_id': 'signed-nul', 'channel_id': cid, 'description': original}]}
        bundle = build_bundle(node_id='synthetic-node', kind=importer.DISCOVERY_BUNDLE_KIND,
                              job_id='nul-description', records=records, signing_key=self.key)
        path = write_bundle(bundle, self.root / 'nul-description')
        real_ack = importer.write_ack
        def acknowledge(*args, **kwargs):
            with self.store.connect() as conn:
                for table in ('channels', 'videos'):
                    row = conn.execute('SELECT description,raw_json FROM '+table).fetchone()
                    self.assertEqual(row[0], 'signed café漢字')
                    raw = json.loads(row[1])
                    self.assertEqual(base64.b64decode(raw['_beacon_description_nul_v1']['original_utf8_base64']).decode(), original)
            return real_ack(*args, **kwargs)
        with patch.object(importer, 'write_ack', side_effect=acknowledge):
            with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
                result = session.import_path(path, signing_key=self.key, ack_dir=self.root / 'ack')
        self.assertEqual(result.status, 'imported')
        self.assertEqual(records['channels'][0]['description'], original)

    def test_query_attribution_is_committed_before_ack_and_reported_read_only(self):
        from beacon_engine.edge.discovery_yield import discovery_yield_report
        cid = 'UC' + '7' * 22
        records = {'channels': [{'channel_id': cid}], 'query_runs': [{
            'node_id': 'synthetic-node', 'job_id': 'attribution', 'run_id': 'one',
            'query': 'history documentary', 'topic': 'History', 'provider': 'test',
            'sort': 'newest', 'status': 'ok', 'channel_ids': [cid],
        }]}
        bundle = build_bundle(node_id='synthetic-node', kind=importer.DISCOVERY_BUNDLE_KIND,
                              job_id='attribution', records=records, signing_key=self.key)
        path = write_bundle(bundle, self.root / 'attribution')
        real_ack = importer.write_ack
        def acknowledge(*args, **kwargs):
            result = discovery_yield_report(self.store, minimum_searches=1)
            self.assertEqual(result['queries'][0]['new_channels'], 1)
            return real_ack(*args, **kwargs)
        with patch.object(importer, 'write_ack', side_effect=acknowledge):
            with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
                report = session.import_path(path, signing_key=self.key, ack_dir=self.root / 'ack')
        self.assertEqual(report.counts['channels_new_attributed'], 1)
        self.assertEqual(report.counts['channels_new_unattributed'], 0)

    def test_unrelated_operator_write_commits_during_open_import_transaction(self):
        _, path = self.bundle('concurrent')
        real_stage = importer._stage_loaded_bundle
        failures = []

        def operator_write():
            try:
                self.store.upsert_channel(ProviderChannel(channel_id='UC' + '9' * 22,
                                                          title='Operator History'))
            except Exception as exc:
                failures.append(exc)

        def stage(*args):
            report = real_stage(*args)
            thread = threading.Thread(target=operator_write)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), 'Operator write blocked behind unrelated importer rows')
            return report

        with patch.object(importer, '_stage_loaded_bundle', side_effect=stage):
            with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
                session.import_path(path, signing_key=self.key)
        self.assertEqual(failures, [])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 2)

    def test_same_channel_writer_can_finish_before_the_next_bundle_is_staged(self):
        first, first_path = self.bundle('contended')
        _, second_path = self.bundle('neighbor')
        cid = first.records['channels'][0]['channel_id']
        self.store.upsert_channel(ProviderChannel(channel_id=cid, title='Before import'))
        real_stage = importer._stage_loaded_bundle
        real_ack = importer.write_ack
        writes = []
        committed_at_ack = []

        def edit_channel():
            try:
                with self.store.connect() as conn:
                    conn.execute("SELECT set_config('lock_timeout', ?, true)", ('100ms',))
                    conn.execute('UPDATE channels SET title=? WHERE channel_id=?',
                                 ('Operator edit', cid))
            except Exception as exc:
                return type(exc).__name__
            return 'committed'

        def stage(conn, bundle, path):
            if bundle.bundle_id != first.bundle_id:
                # A user refreshing this channel must not wait for unrelated
                # later bundles after its own imported data is complete.
                writes.append(edit_channel())
            result = real_stage(conn, bundle, path)
            if bundle.bundle_id == first.bundle_id:
                # PostgreSQL correctly protects an edit still in progress.
                writes.append(edit_channel())
            return result

        def acknowledge(*args, **kwargs):
            with self.store.connect() as conn:
                committed_at_ack.append(conn.execute(
                    'SELECT COUNT(*) FROM edge_import_batches').fetchone()[0])
            return real_ack(*args, **kwargs)

        with patch.object(importer, '_stage_loaded_bundle', side_effect=stage), \
                patch.object(importer, 'write_ack', side_effect=acknowledge):
            with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
                attempts = session.import_paths(
                    [first_path, second_path], signing_key=self.key,
                    ack_dir=self.root / 'ack', transaction_bundles=20,
                )
        self.assertEqual([attempt.error for attempt in attempts], [None, None])
        self.assertEqual(writes, ['LockNotAvailable', 'committed'])
        self.assertEqual(committed_at_ack, [1, 2])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT title FROM channels WHERE channel_id=?',
                                          (cid,)).fetchone()[0], 'Operator edit')

    def test_organization_job_deduplication_and_signature_rejection(self):
        _, path = self.bundle('organization', organization=True)
        with importer.BundleImportSession(POSTGRES_INDEX_TARGET) as session:
            with self.assertRaises(Exception):
                session.import_path(path, signing_key='wrong-test-key')
            first = session.import_path(path, signing_key=self.key)
            second = session.import_path(path, signing_key=self.key)
        self.assertEqual((first.status, second.status), ('imported', 'duplicate'))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edge_organization_runs').fetchone()[0], 1)
