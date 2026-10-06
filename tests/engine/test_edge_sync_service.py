from __future__ import annotations
from contextlib import closing
import json
import shutil
import sqlite3
import threading
import pytest
from beacon_engine.edge.protocol import build_bundle, write_bundle
from beacon_engine.edge.sync import EdgeSyncPaths, _automatic_plan_sync_enabled, edge_sync_status, import_edge_inbox
from beacon_engine.intelligence.analyst_tools import TOOL_SCHEMAS, IntelligenceAnalystTools
from beacon_engine.research.store import YouTubeStore
SIGNING_KEY = 'test-only-edge-signing-key'

@pytest.fixture(autouse=True)
def _disable_live_oracle_transport(monkeypatch):
    monkeypatch.delenv('SIGNAL_EDGE_SSH_HOST', raising=False)

def _records():
    captured = '2026-07-12T17:00:00Z'
    return {'channels': [{'channel_id': 'UCedgeSync000000000000001', 'title': 'Edge Sync Channel', 'source': 'edge-test', 'first_seen_at': captured, 'last_seen_at': captured}], 'videos': [{'video_id': 'edgeSync001', 'title': 'Edge sync result', 'channel': 'Edge Sync Channel', 'channel_id': 'UCedgeSync000000000000001', 'views': 1234, 'source': 'edge-test', 'first_seen_at': captured, 'last_seen_at': captured}], 'search_results': [{'query': 'edge sync documentary', 'provider': 'edge-test', 'video_id': 'edgeSync001', 'position': 1, 'captured_at': captured}], 'video_discoveries': [{'video_id': 'edgeSync001', 'query': 'edge sync documentary', 'provider': 'edge-test', 'discovery_kind': 'search', 'position': 1, 'discovered_at': captured}], 'video_snapshots': [{'video_id': 'edgeSync001', 'views': 1234, 'captured_at': captured, 'source': 'edge:test'}], 'query_runs': [{'node_id': 'oracle-casa-1', 'job_id': 'radar-discovery', 'run_id': 'run-00000001', 'query': 'edge sync documentary', 'status': 'ok', 'result_count': 1, 'new_result_count': 1, 'finished_at': captured}]}

def _bundle(paths: EdgeSyncPaths):
    bundle = build_bundle(node_id='oracle-casa-1', kind='youtube.discovery.v1', job_id='radar-discovery', records=_records(), created_at='2026-07-12T17:00:00Z', signing_key=SIGNING_KEY)
    return write_bundle(bundle, paths.inbox)

def test_automatic_plan_sync_is_disabled_for_custom_test_roots(monkeypatch):
    monkeypatch.delenv('SIGNAL_EDGE_AUTO_PLAN_SYNC', raising=False)
    monkeypatch.delenv('SIGNAL_EDGE_SYNC_ROOT', raising=False)
    assert _automatic_plan_sync_enabled() is True
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', 'custom-test-root')
    assert _automatic_plan_sync_enabled() is False
    monkeypatch.setenv('SIGNAL_EDGE_AUTO_PLAN_SYNC', '1')
    assert _automatic_plan_sync_enabled() is True
    monkeypatch.setenv('SIGNAL_EDGE_AUTO_PLAN_SYNC', '0')
    assert _automatic_plan_sync_enabled() is False

def test_status_requires_key_but_does_not_create_or_write(tmp_path, monkeypatch):
    root = tmp_path / 'sync'
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(root))
    monkeypatch.delenv('SIGNAL_EDGE_SIGNING_KEY', raising=False)
    status = edge_sync_status()
    assert status['configured'] is False
    assert status['pending_bundles'] == 0
    assert not root.exists()

def test_import_archives_acknowledges_and_deduplicates(tmp_path, monkeypatch):
    root = tmp_path / 'sync'
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(root))
    monkeypatch.setenv('SIGNAL_EDGE_SIGNING_KEY', SIGNING_KEY)
    paths = EdgeSyncPaths.resolve()
    bundle_path = _bundle(paths)
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    before = edge_sync_status()
    first = import_edge_inbox(db_path, max_bundles=5)
    after = edge_sync_status()
    assert before['configured'] is True
    assert before['pending_bundles'] == 1
    assert first['imported_bundles'] == 1
    assert first['failed_bundles'] == 0
    assert first['counts']['channels'] == 1
    assert first['wal_maintenance']['attempts'] >= 1
    assert first['wal_maintenance']['error'] == ''
    assert after['pending_bundles'] == 0
    assert after['acknowledgements'] == 1
    assert after['progress']['state'] == 'complete'
    assert after['progress']['processed_bundles'] == 1
    assert after['progress']['total_bundles'] == 1
    assert after['progress']['backlog_remaining'] == 0
    archived = next(paths.archive.rglob('*.signal-edge.json.gz'))
    assert not bundle_path.exists()
    shutil.copy2(archived, paths.inbox / archived.name)
    duplicate = import_edge_inbox(db_path)
    assert duplicate['imported_bundles'] == 0
    assert duplicate['duplicate_bundles'] == 1
    assert duplicate['remaining_bundles'] == 0
    assert duplicate['counts'] == {}

def test_import_publishes_live_staging_progress_before_commit(tmp_path, monkeypatch):
    from beacon_engine.edge import importer as importer_module
    root = tmp_path / 'sync'
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(root))
    monkeypatch.setenv('SIGNAL_EDGE_SIGNING_KEY', SIGNING_KEY)
    paths = EdgeSyncPaths.resolve()
    _bundle(paths)
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    observed = []
    real_stage = importer_module._stage_loaded_bundle

    def observe_stage(*args, **kwargs):
        observed.append(json.loads(paths.progress.read_text(encoding='utf-8')))
        return real_stage(*args, **kwargs)
    monkeypatch.setattr(importer_module, '_stage_loaded_bundle', observe_stage)
    result = import_edge_inbox(db_path)
    assert result['imported_bundles'] == 1
    assert observed
    assert observed[0]['state'] == 'importing'
    assert observed[0]['processed_bundles'] == 0
    assert observed[0]['phase'] == 'staging'
    assert observed[0]['transaction_number'] == 1
    assert observed[0]['transaction_total'] == 1
    assert observed[0]['transaction_processed'] == 0
    assert observed[0]['transaction_size'] == 1
    assert observed[0]['active_bundle'] == 1
    final = edge_sync_status()['progress']
    assert final['state'] == 'complete'
    assert final['phase'] == 'complete'

def test_import_flushes_committed_ack_batches_while_next_transaction_runs(tmp_path, monkeypatch):
    from beacon_engine.edge import importer as importer_module
    from beacon_engine.edge import sync as sync_module
    root = tmp_path / 'sync'
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(root))
    monkeypatch.setenv('SIGNAL_EDGE_SIGNING_KEY', SIGNING_KEY)
    paths = EdgeSyncPaths.resolve()
    remote_files: dict[str, bytes] = {}
    for index in range(3):
        bundle = build_bundle(node_id='oracle-casa-1', kind='youtube.discovery.v1', job_id=f'radar-discovery-{index}', records=_records(), metadata={'background_ack_test': index}, signing_key=SIGNING_KEY)
        path = write_bundle(bundle, paths.inbox)
        remote_files[path.name] = path.read_bytes()
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)

    class BlockingTransport:

        def __init__(self):
            self.files = dict(remote_files)
            self.lock = threading.Lock()
            self.ack_started = threading.Event()
            self.release_ack = threading.Event()
            self.ack_batches: list[list[str]] = []
            self.committed_before_ack = 0

        def read_worker_status(self):
            return {'state': 'running', 'node_id': 'oracle-test'}

        def list_bundle_names(self):
            with self.lock:
                return sorted(self.files)

        def download_bundle(self, filename):
            raise AssertionError(f'unexpected download: {filename}')

        def acknowledge_many(self, items, *, signing_key):
            pairs = list(items)
            self.ack_batches.append([name for name, _path in pairs])
            with closing(sqlite3.connect(db_path)) as connection, connection:
                self.committed_before_ack = int(connection.execute('SELECT COUNT(*) FROM edge_import_batches').fetchone()[0])
            self.ack_started.set()
            if not self.release_ack.wait(5):
                raise RuntimeError('test acknowledgement release timed out')
            with self.lock:
                for name, _path in pairs:
                    self.files.pop(name, None)
            return [name for name, _path in pairs]

        def acknowledge(self, *, bundle_filename, ack_path, signing_key):
            with self.lock:
                self.files.pop(bundle_filename, None)
            return bundle_filename
    transport = BlockingTransport()
    monkeypatch.setattr(sync_module.EdgeSSHTransport, 'from_env', classmethod(lambda cls: transport))
    monkeypatch.setattr(sync_module, 'DEFAULT_IMPORT_TRANSACTION_BUNDLES', 2)
    real_stage = importer_module._stage_loaded_bundle
    staged = 0
    overlapped = False

    def observe_stage(*args, **kwargs):
        nonlocal staged, overlapped
        staged += 1
        if staged == 3:
            overlapped = transport.ack_started.wait(2)
            transport.release_ack.set()
        return real_stage(*args, **kwargs)
    monkeypatch.setattr(importer_module, '_stage_loaded_bundle', observe_stage)
    result = import_edge_inbox(db_path, max_bundles=3)
    assert result['imported_bundles'] == 3
    assert result['transport']['acknowledged'] == 3
    assert overlapped is True
    assert transport.committed_before_ack == 2
    assert len(transport.ack_batches) == 1
    assert len(transport.ack_batches[0]) == 2
    assert transport.files == {}

def test_invalid_bundle_stays_pending_and_is_reported(tmp_path, monkeypatch):
    root = tmp_path / 'sync'
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(root))
    monkeypatch.setenv('SIGNAL_EDGE_SIGNING_KEY', SIGNING_KEY)
    paths = EdgeSyncPaths.resolve()
    paths.inbox.mkdir(parents=True)
    bad = paths.inbox / 'bad.signal-edge.json.gz'
    bad.write_bytes(b'not a signed gzip bundle')
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    result = import_edge_inbox(db_path)
    assert result['failed_bundles'] == 1
    assert result['remaining_bundles'] == 1
    assert result['errors'][0]['file'] == bad.name
    assert bad.exists()
    assert edge_sync_status()['last_sync']['failed_bundles'] == 1

def test_import_refuses_without_signing_key(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(tmp_path / 'sync'))
    monkeypatch.delenv('SIGNAL_EDGE_SIGNING_KEY', raising=False)
    result = import_edge_inbox(tmp_path / 'youtube.sqlite3')
    assert 'error' in result
    assert 'SIGNAL_EDGE_SIGNING_KEY' in result['error']

def test_finder_tools_expose_read_status_and_full_access_import(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(tmp_path / 'sync'))
    monkeypatch.setenv('SIGNAL_EDGE_SIGNING_KEY', SIGNING_KEY)
    store = YouTubeStore(tmp_path / 'youtube.sqlite3')
    tools = IntelligenceAnalystTools(store)
    assert TOOL_SCHEMAS['edge_sync_status']['write'] is False
    assert TOOL_SCHEMAS['import_edge_inbox']['write'] is True
    assert tools.dispatch('edge_sync_status')['configured'] is True
    result = tools.dispatch('import_edge_inbox', {'max_bundles': 1})
    assert result['processed'] == 0
