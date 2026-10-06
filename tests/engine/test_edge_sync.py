from __future__ import annotations
from contextlib import closing
import gzip
import json
import sqlite3
import threading
from pathlib import Path
import pytest
from beacon_engine.edge import EdgeProtocolError, build_bundle, import_bundle, load_bundle, write_bundle
from beacon_engine.edge.importer import BundleImportSession, DISCOVERY_BUNDLE_KIND, ORGANIZATION_BUNDLE_KIND, _checkpoint_and_trim_wal
from beacon_engine.research.store import YouTubeStore
SIGNING_KEY = 'test-edge-secret'

def _records() -> dict:
    return {'channels': [{'channel_id': 'UCedge000000000000000001', 'title': 'Edge History', 'subscribers': 1200, 'video_count': 12, 'view_count': 44000, 'source': 'scrapetube', 'first_seen_at': '2026-07-12T02:00:00Z', 'last_seen_at': '2026-07-12T02:05:00Z'}], 'videos': [{'video_id': 'edgeVideo01', 'title': 'A forgotten empire', 'channel': 'Edge History', 'channel_id': 'UCedge000000000000000001', 'views': 9000, 'published_at': '2 days ago', 'published_at_normalized': '2026-07-10T02:00:00Z', 'thumbnail_url': 'https://example.invalid/thumb.jpg', 'source': 'scrapetube', 'first_seen_at': '2026-07-12T02:00:00Z', 'last_seen_at': '2026-07-12T02:05:00Z'}], 'channel_classifications': [{'channel_id': 'UCedge000000000000000001', 'likely_faceless': 1, 'confidence': 0.82, 'score': 3.4, 'sample_count': 4, 'positive_title_count': 3, 'negative_title_count': 0, 'positives': ['documentary', 'history'], 'negatives': [], 'classifier': 'text-v3', 'classified_at': '2026-07-12T02:05:00Z'}], 'search_results': [{'query': 'forgotten empires documentary', 'provider': 'scrapetube', 'video_id': 'edgeVideo01', 'position': 1, 'captured_at': '2026-07-12T02:00:00Z'}], 'video_discoveries': [{'video_id': 'edgeVideo01', 'query': 'forgotten empires documentary', 'provider': 'scrapetube', 'discovery_kind': 'search', 'position': 1, 'discovered_at': '2026-07-12T02:00:00Z'}], 'video_snapshots': [{'video_id': 'edgeVideo01', 'views': 9000, 'captured_at': '2026-07-12T02:00:00Z', 'source': 'scrapetube'}]}

def _bundle_path(tmp_path):
    bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id='discovery-cycle-1', records=_records(), metadata={'config_revision': 7}, created_at='2026-07-12T02:06:00Z', signing_key=SIGNING_KEY)
    return (bundle, write_bundle(bundle, tmp_path / 'outbox'))

def test_signed_bundle_round_trip_is_deterministic(tmp_path):
    bundle, path = _bundle_path(tmp_path)
    loaded = load_bundle(path, signing_key=SIGNING_KEY)
    assert loaded.bundle_id == bundle.bundle_id
    assert loaded.node_id == 'oracle-casablanca-1'
    assert loaded.kind == DISCOVERY_BUNDLE_KIND
    assert loaded.manifest['counts']['videos'] == 1
    assert path.name.endswith('.signal-edge.json.gz')

def test_bundle_tampering_is_rejected(tmp_path):
    _bundle, path = _bundle_path(tmp_path)
    envelope = json.loads(gzip.decompress(path.read_bytes()).decode('utf-8'))
    envelope['payload']['records']['videos'][0]['views'] = 999999
    path.write_bytes(gzip.compress(json.dumps(envelope).encode('utf-8')))
    with pytest.raises(EdgeProtocolError, match='checksum'):
        load_bundle(path, signing_key=SIGNING_KEY)

def test_wrong_or_missing_signature_is_rejected(tmp_path):
    _bundle, path = _bundle_path(tmp_path)
    with pytest.raises(EdgeProtocolError, match='signature'):
        load_bundle(path, signing_key='wrong-secret')
    with pytest.raises(EdgeProtocolError, match='signing key'):
        load_bundle(path, signing_key=None)

def test_import_is_atomic_idempotent_and_preserves_cloud_discovery_time(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    YouTubeStore(db_path)
    bundle, path = _bundle_path(tmp_path)
    ack_dir = tmp_path / 'acks'
    first = import_bundle(db_path, path, signing_key=SIGNING_KEY, ack_dir=ack_dir)
    second = import_bundle(db_path, path, signing_key=SIGNING_KEY, ack_dir=ack_dir)
    assert first.status == 'imported'
    assert second.status == 'duplicate'
    assert first.bundle_id == bundle.bundle_id
    assert first.counts == {'channels': 1, 'channels_new': 1, 'channels_existing': 0, 'videos': 1, 'channel_classifications': 1, 'search_results': 1, 'video_discoveries': 1, 'video_snapshots': 1, 'query_runs': 0}
    assert first.ack_path
    with closing(sqlite3.connect(db_path)) as conn, conn:
        channel = conn.execute("\n            SELECT title, subscribers, first_seen_at, last_seen_at\n            FROM channels\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()
        video = conn.execute("\n            SELECT title, views, first_seen_at, last_seen_at\n            FROM videos\n            WHERE video_id='edgeVideo01'\n            ").fetchone()
        assert channel == ('Edge History', 1200, '2026-07-12T02:00:00Z', '2026-07-12T02:05:00Z')
        assert video == ('A forgotten empire', 9000, '2026-07-12T02:00:00Z', '2026-07-12T02:05:00Z')
        assert conn.execute('SELECT COUNT(*) FROM edge_import_batches').fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM video_snapshots WHERE video_id='edgeVideo01'").fetchone()[0] == 1
        assert conn.execute("\n            SELECT likely_faceless, confidence, sample_count, classifier\n            FROM channel_classifications\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone() == (1, 0.82, 4, 'text-v3')

def test_bulk_import_preserves_row_order_and_snapshot_deduplication(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    YouTubeStore(db_path)
    records = _records()
    records['channels'].append({**records['channels'][0], 'title': 'Edge History Updated', 'subscribers': 2400, 'first_seen_at': '2026-07-11T23:00:00Z', 'last_seen_at': '2026-07-12T03:00:00Z'})
    records['videos'].append({**records['videos'][0], 'title': 'A forgotten empire updated', 'views': 12000, 'first_seen_at': '2026-07-11T23:00:00Z', 'last_seen_at': '2026-07-12T03:00:00Z'})
    records['video_snapshots'].extend([dict(records['video_snapshots'][0]), {**records['video_snapshots'][0], 'views': 12000, 'captured_at': '2026-07-12T03:00:00Z'}])
    bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id='bulk-order-cycle', records=records, signing_key=SIGNING_KEY)
    path = write_bundle(bundle, tmp_path / 'outbox')
    report = import_bundle(db_path, path, signing_key=SIGNING_KEY)
    assert report.counts['channels'] == 2
    assert report.counts['videos'] == 2
    assert report.counts['video_snapshots'] == 2
    with closing(sqlite3.connect(db_path)) as conn, conn:
        assert conn.execute("\n            SELECT title, subscribers, first_seen_at, last_seen_at\n            FROM channels\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone() == ('Edge History Updated', 2400, '2026-07-11T23:00:00Z', '2026-07-12T03:00:00Z')
        assert conn.execute("\n            SELECT title, views, first_seen_at, last_seen_at\n            FROM videos\n            WHERE video_id='edgeVideo01'\n            ").fetchone() == ('A forgotten empire updated', 12000, '2026-07-11T23:00:00Z', '2026-07-12T03:00:00Z')
        assert conn.execute("\n            SELECT COUNT(*)\n            FROM video_snapshots\n            WHERE video_id='edgeVideo01'\n            ").fetchone()[0] == 2

def test_import_stores_only_raw_fields_used_by_the_canonical_index(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    YouTubeStore(db_path)
    records = _records()
    records['channels'][0]['raw'] = {'etag': 'discard-channel-etag', 'snippet': {'title': 'already parsed', 'thumbnails': {'medium': {'url': 'https://img.example/channel.jpg'}}}, 'statistics': {'subscriberCount': '1200'}}
    records['videos'][0]['category_id'] = '27'
    records['videos'][0]['raw'] = {'etag': 'discard-video-etag', 'snippet': {'title': 'already parsed', 'categoryId': '27', 'thumbnails': {'high': {'url': 'https://img.example/video.jpg'}}}, 'statistics': {'viewCount': '9000'}, 'contentDetails': {'duration': 'PT8M'}}
    bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id='slim-raw-cycle', records=records, signing_key=SIGNING_KEY)
    path = write_bundle(bundle, tmp_path / 'outbox')
    import_bundle(db_path, path, signing_key=SIGNING_KEY)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        channel_raw = json.loads(conn.execute("\n            SELECT raw_json\n            FROM channels\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()[0])
        video_raw = json.loads(conn.execute("\n            SELECT raw_json\n            FROM videos\n            WHERE video_id='edgeVideo01'\n            ").fetchone()[0])
        assert channel_raw == {'snippet': {'thumbnails': {'medium': {'url': 'https://img.example/channel.jpg'}}}}
        assert video_raw == {'snippet': {'categoryId': '27'}}
        assert conn.execute("\n            SELECT title, views, category_id\n            FROM videos\n            WHERE video_id='edgeVideo01'\n            ").fetchone() == ('A forgotten empire', 9000, '27')

def test_import_session_reuses_one_initialized_store_for_multiple_bundles(tmp_path, monkeypatch):
    from beacon_engine.edge import importer as importer_module
    real_store = importer_module.YouTubeStore
    created = []

    class CountingStore(real_store):

        def __init__(self, *args, **kwargs):
            created.append(1)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(importer_module, 'YouTubeStore', CountingStore)
    db_path = tmp_path / 'canonical.sqlite3'
    first_bundle, first_path = _bundle_path(tmp_path / 'first')
    second = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id='discovery-cycle-2', records=_records(), metadata={'config_revision': 8}, created_at='2026-07-12T02:07:00Z', signing_key=SIGNING_KEY)
    second_path = write_bundle(second, tmp_path / 'second')
    with BundleImportSession(db_path) as session:
        first_report = session.import_path(first_path, signing_key=SIGNING_KEY)
        second_report = session.import_path(second_path, signing_key=SIGNING_KEY)
    assert created == [1]
    assert first_report.bundle_id == first_bundle.bundle_id
    assert second_report.bundle_id == second.bundle_id

def test_import_session_batches_commits_and_writes_acks_after_commit(tmp_path, monkeypatch):
    from beacon_engine.edge import importer as importer_module
    paths = []
    for index in range(3):
        bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id=f'discovery-cycle-{index}', records=_records(), metadata={'batch_test': index}, signing_key=SIGNING_KEY)
        paths.append(write_bundle(bundle, tmp_path / f'bundle-{index}'))
    ack_states = []
    real_write_ack = importer_module.write_ack
    db_path = tmp_path / 'canonical.sqlite3'
    with BundleImportSession(db_path) as session:
        statements = []
        progress_events = []
        activity_events = []
        session.conn.set_trace_callback(statements.append)

        def tracked_write_ack(*args, **kwargs):
            ack_states.append(session.conn.in_transaction)
            return real_write_ack(*args, **kwargs)
        monkeypatch.setattr(importer_module, 'write_ack', tracked_write_ack)
        attempts = session.import_paths(paths, signing_key=SIGNING_KEY, ack_dir=tmp_path / 'acks', transaction_bundles=20, on_progress=lambda chunk, processed, total: progress_events.append((len(chunk), processed, total)), on_activity=activity_events.append)
    assert all((attempt.error is None for attempt in attempts))
    assert all((attempt.report is not None for attempt in attempts))
    assert sum((statement.strip().upper() == 'BEGIN IMMEDIATE' for statement in statements)) == 1
    assert sum((statement.strip().upper() == 'COMMIT' for statement in statements)) == 1
    assert ack_states == [False, False, False]
    assert progress_events == [(3, 3, 3)]
    phases = [event['phase'] for event in activity_events]
    assert 'verifying' in phases
    assert 'waiting_for_database' in phases
    assert 'staging' in phases
    assert 'committing' in phases
    assert 'acknowledging' in phases
    assert any((event['phase'] == 'staging' and event['transaction_processed'] == 3 and (event['transaction_size'] == 3) and (event['active_bundle'] == 3) for event in activity_events))
    assert len(list((tmp_path / 'acks').glob('*.ack.json'))) == 3
    with closing(sqlite3.connect(db_path)) as conn, conn:
        assert conn.execute('SELECT COUNT(*) FROM edge_import_batches').fetchone()[0] == 3

def test_wal_maintenance_runs_off_the_import_thread(tmp_path, monkeypatch):
    from beacon_engine.edge import importer as importer_module
    bundle, bundle_path = _bundle_path(tmp_path / 'bundle')
    db_path = tmp_path / 'canonical.sqlite3'
    main_thread = threading.get_ident()
    maintenance_threads = []

    def tracked_maintenance(*args, **kwargs):
        maintenance_threads.append(threading.get_ident())
        return {'wal_bytes_before': 0, 'wal_bytes_after': 0, 'passive_busy': False, 'passive_log_frames': 0, 'passive_checkpointed_frames': 0, 'truncate_attempted': False, 'truncate_busy': False, 'truncated': False, 'error': ''}
    monkeypatch.setattr(importer_module, '_checkpoint_and_trim_wal', tracked_maintenance)
    with BundleImportSession(db_path) as session:
        report = session.import_path(bundle_path, signing_key=SIGNING_KEY)
    assert report.bundle_id == bundle.bundle_id
    assert maintenance_threads
    assert all((thread_id != main_thread for thread_id in maintenance_threads))
    assert session.wal_maintenance_status['attempts'] >= 1

def test_wal_maintenance_truncates_an_oversized_wal(tmp_path):
    db_path = tmp_path / 'checkpoint.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('CREATE TABLE payloads (value BLOB)')
    connection.executemany('INSERT INTO payloads(value) VALUES (?)', [(b'x' * 8192,) for _index in range(128)])
    connection.commit()
    wal_path = Path(f'{db_path}-wal')
    before = wal_path.stat().st_size
    result = _checkpoint_and_trim_wal(db_path, truncate_bytes=1, busy_timeout_ms=1000)
    connection.close()
    assert before > 0
    assert result['truncate_attempted'] is True
    assert result['truncate_busy'] is False
    assert result['truncated'] is True
    assert result['wal_bytes_after'] < before

def test_import_session_savepoint_keeps_valid_bundles_when_one_fails(tmp_path):
    valid_paths = []
    for index in range(2):
        bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id=f'valid-cycle-{index}', records=_records(), metadata={'savepoint_test': index}, signing_key=SIGNING_KEY)
        valid_paths.append(write_bundle(bundle, tmp_path / f'valid-{index}'))
    invalid_records = _records()
    invalid_records['future_unknown_records'] = [{'id': 'x'}]
    invalid_bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, job_id='invalid-cycle', records=invalid_records, signing_key=SIGNING_KEY)
    invalid_path = write_bundle(invalid_bundle, tmp_path / 'invalid')
    db_path = tmp_path / 'canonical.sqlite3'
    with BundleImportSession(db_path) as session:
        attempts = session.import_paths([valid_paths[0], invalid_path, valid_paths[1]], signing_key=SIGNING_KEY, ack_dir=tmp_path / 'acks', transaction_bundles=20)
    assert [attempt.error is None for attempt in attempts] == [True, False, True]
    assert isinstance(attempts[1].error, EdgeProtocolError)
    assert len(list((tmp_path / 'acks').glob('*.ack.json'))) == 2
    with closing(sqlite3.connect(db_path)) as conn, conn:
        assert conn.execute('SELECT COUNT(*) FROM edge_import_batches').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0] == 1

def test_import_keeps_local_topic_and_earliest_seen_time(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    store = YouTubeStore(db_path)
    with store.connect() as conn:
        conn.execute("\n            INSERT INTO channels (\n                channel_id, title, subscribers, source, raw_json,\n                first_seen_at, last_seen_at, topic\n            ) VALUES (?, ?, ?, ?, '{}', ?, ?, ?)\n            ", ('UCedge000000000000000001', 'Local title', 100, 'local', '2026-07-11T00:00:00Z', '2026-07-11T01:00:00Z', 'History'))
    records = _records()
    records['channels'][0]['topic'] = 'Other'
    bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, records=records, signing_key=SIGNING_KEY)
    path = write_bundle(bundle, tmp_path / 'outbox')
    import_bundle(db_path, path, signing_key=SIGNING_KEY)
    with store.connect() as conn:
        row = conn.execute("\n            SELECT title, subscribers, first_seen_at, last_seen_at, topic\n            FROM channels\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()
    assert tuple(row) == ('Edge History', 1200, '2026-07-11T00:00:00Z', '2026-07-12T02:05:00Z', 'History')

def test_import_preserves_stronger_local_text_classification(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    store = YouTubeStore(db_path)
    with store.connect() as conn:
        conn.execute('\n            INSERT INTO channel_classifications (\n                channel_id, likely_faceless, confidence, score,\n                sample_count, positive_title_count, negative_title_count,\n                positives_json, negatives_json, classifier, classified_at\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ', ('UCedge000000000000000001', 1, 0.94, 8.5, 12, 10, 0, '["documentary"]', '[]', 'text-v3', '2026-07-12T01:00:00Z'))
    _bundle, path = _bundle_path(tmp_path)
    import_bundle(db_path, path, signing_key=SIGNING_KEY)
    with store.connect() as conn:
        row = conn.execute("\n            SELECT likely_faceless, confidence, score, sample_count,\n                   classified_at\n            FROM channel_classifications\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()
    assert tuple(row) == (1, 0.94, 8.5, 12, '2026-07-12T01:00:00Z')

def test_unknown_dataset_rolls_back_everything(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    YouTubeStore(db_path)
    records = _records()
    records['future_unknown_records'] = [{'id': 'x'}]
    bundle = build_bundle(node_id='oracle-casablanca-1', kind=DISCOVERY_BUNDLE_KIND, records=records, signing_key=SIGNING_KEY)
    path = write_bundle(bundle, tmp_path / 'outbox')
    with pytest.raises(EdgeProtocolError, match='unsupported discovery datasets'):
        import_bundle(db_path, path, signing_key=SIGNING_KEY)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        assert conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0] == 0
        ledger_exists = conn.execute("\n            SELECT COUNT(*)\n            FROM sqlite_master\n            WHERE type='table' AND name='edge_import_batches'\n            ").fetchone()[0]
        assert ledger_exists == 0

def _organization_records() -> dict:
    return {'channels': [{'channel_id': 'UCedge000000000000000001', 'title': 'Edge History Organized', 'subscribers': 1400, 'video_count': 20, 'view_count': 88000, 'source': 'official-youtube', 'first_seen_at': '2026-07-12T02:00:00Z', 'last_seen_at': '2026-07-13T18:00:00Z'}], 'videos': [{'video_id': 'organized01', 'title': 'A deeper forgotten empire catalog', 'channel': 'Edge History Organized', 'channel_id': 'UCedge000000000000000001', 'views': 17000, 'published_at': '2026-07-01T00:00:00Z', 'thumbnail_url': 'https://example.invalid/organized.jpg', 'source': 'official-youtube', 'category_id': '27', 'first_seen_at': '2026-07-13T18:00:00Z', 'last_seen_at': '2026-07-13T18:00:00Z'}], 'channel_classifications': [{'channel_id': 'UCedge000000000000000001', 'likely_faceless': 1, 'confidence': 0.96, 'score': 9.0, 'sample_count': 20, 'positive_title_count': 18, 'negative_title_count': 0, 'positives': ['documentary'], 'negatives': [], 'classifier': 'text-v3', 'classified_at': '2026-07-13T18:00:00Z'}], 'channel_face_checks': [{'channel_id': 'UCedge000000000000000001', 'thumbnails_checked': 10, 'faces_found': 0, 'recurring_count': 0, 'verdict': 'faceless', 'confidence': 1.0, 'checked_at': '2026-07-13T18:00:00Z'}], 'enrichment_attempts': [{'entity_type': 'catalog', 'entity_id': 'UCedge000000000000000001', 'attempts': 2, 'status': 'ok', 'attempted_at': '2026-07-13T18:00:00Z'}], 'organization_runs': [{'channel_id': 'UCedge000000000000000001', 'status': 'ok', 'catalog_attempted': 1, 'catalog_status': 'ok', 'catalog_attempts': 2, 'videos_collected': 20, 'categories_filled': 20, 'face_status': 'faceless', 'error': '', 'started_at': '2026-07-13T17:59:00Z', 'finished_at': '2026-07-13T18:00:00Z'}]}

def _organization_bundle(tmp_path, *, source_job_revision='organization-job-revision-1', records=None):
    bundle = build_bundle(node_id='oracle-casablanca-1', kind=ORGANIZATION_BUNDLE_KIND, job_id='organization-cycle-1', records=records or _organization_records(), metadata={'source_job_revision': source_job_revision, 'run_id': 'organization-run-1'}, created_at='2026-07-13T18:00:00Z', signing_key=SIGNING_KEY)
    return (bundle, write_bundle(bundle, tmp_path / 'organization-outbox'))

def test_organization_import_is_safe_idempotent_and_tracks_the_run(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    store = YouTubeStore(db_path)
    bundle, path = _organization_bundle(tmp_path)
    first = import_bundle(db_path, path, signing_key=SIGNING_KEY)
    second = import_bundle(db_path, path, signing_key=SIGNING_KEY)
    assert first.status == 'imported'
    assert second.status == 'duplicate'
    assert first.bundle_id == bundle.bundle_id
    assert first.counts == {'channels': 1, 'channels_new': 1, 'channels_existing': 0, 'videos': 1, 'channel_classifications': 1, 'channel_face_checks': 1, 'enrichment_attempts': 1, 'organization_runs': 1}
    with store.connect() as conn:
        assert tuple(conn.execute("\n            SELECT title, subscribers\n            FROM channels\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()) == ('Edge History Organized', 1400)
        assert conn.execute("\n            SELECT category_id\n            FROM videos\n            WHERE video_id='organized01'\n            ").fetchone()[0] == '27'
        assert tuple(conn.execute("\n            SELECT thumbnails_checked, verdict, confidence\n            FROM channel_face_check\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()) == (10, 'faceless', 1.0)
        assert tuple(conn.execute("\n            SELECT attempts, last_status\n            FROM enrichment_log\n            WHERE entity_type='catalog'\n              AND entity_id='UCedge000000000000000001'\n            ").fetchone()) == (2, 'ok')
        assert tuple(conn.execute('\n            SELECT node_id, job_id, run_id, source_job_revision,\n                   videos_collected, categories_filled, face_status\n            FROM edge_organization_runs\n            ').fetchone()) == ('oracle-casablanca-1', 'organization-cycle-1', 'organization-run-1', 'organization-job-revision-1', 20, 20, 'faceless')
        assert conn.execute('SELECT COUNT(*) FROM edge_organization_import_jobs').fetchone()[0] == 1

def test_organization_import_preserves_stronger_local_face_check(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    store = YouTubeStore(db_path)
    with store.connect() as conn:
        conn.execute('\n            INSERT INTO channel_face_check (\n                channel_id, thumbnails_checked, faces_found, recurring_count,\n                verdict, confidence, checked_at\n            ) VALUES (?, ?, ?, ?, ?, ?, ?)\n            ', ('UCedge000000000000000001', 12, 10, 9, 'on_camera', 1.0, '2026-07-13T17:00:00Z'))
    _bundle, path = _organization_bundle(tmp_path)
    import_bundle(db_path, path, signing_key=SIGNING_KEY)
    with store.connect() as conn:
        assert tuple(conn.execute("\n            SELECT thumbnails_checked, verdict, confidence, checked_at\n            FROM channel_face_check\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()) == (12, 'on_camera', 1.0, '2026-07-13T17:00:00Z')

def test_organization_source_job_revision_can_only_be_imported_once(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    store = YouTubeStore(db_path)
    _first_bundle, first_path = _organization_bundle(tmp_path / 'first')
    changed = _organization_records()
    changed['channels'][0]['title'] = 'Should not replace first result'
    _second_bundle, second_path = _organization_bundle(tmp_path / 'second', records=changed)
    first = import_bundle(db_path, first_path, signing_key=SIGNING_KEY)
    second = import_bundle(db_path, second_path, signing_key=SIGNING_KEY)
    assert first.status == 'imported'
    assert second.status == 'duplicate'
    with store.connect() as conn:
        assert conn.execute("\n            SELECT title\n            FROM channels\n            WHERE channel_id='UCedge000000000000000001'\n            ").fetchone()[0] == 'Edge History Organized'
        assert conn.execute('SELECT COUNT(*) FROM edge_import_batches').fetchone()[0] == 1

def test_unknown_organization_dataset_rolls_back_everything(tmp_path):
    db_path = tmp_path / 'canonical.sqlite3'
    YouTubeStore(db_path)
    records = _organization_records()
    records['future_unknown_records'] = [{'id': 'x'}]
    _bundle, path = _organization_bundle(tmp_path, records=records)
    with pytest.raises(EdgeProtocolError, match='unsupported organization datasets'):
        import_bundle(db_path, path, signing_key=SIGNING_KEY)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        assert conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0] == 0
        assert conn.execute("\n            SELECT COUNT(*)\n            FROM sqlite_master\n            WHERE type='table' AND name='edge_organization_import_jobs'\n            ").fetchone()[0] == 0
