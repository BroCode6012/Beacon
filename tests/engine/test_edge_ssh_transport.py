from __future__ import annotations
from contextlib import closing
import json
import io
import shutil
import sqlite3
import tarfile
import datetime as dt
from pathlib import Path
import pytest
from beacon_engine.edge.importer import import_bundle
from beacon_engine.edge.organization import OrganizationSettings, build_organization_job
from beacon_engine.edge.plan import DiscoveryPlanSettings, EdgeQuery, build_discovery_plan, load_discovery_plan
from beacon_engine.edge.protocol import build_bundle, write_bundle
from beacon_engine.edge.ssh_transport import EdgeSSHConfig, EdgeSSHTransport, EdgeSSHTransportError, verified_ack_bundle_id
from beacon_engine.edge.sync import EdgeSyncPaths, _edge_channels_per_1000_queries, _edge_plan_settings, pull_edge_remote, sync_edge_discovery_plan
from beacon_engine.research.store import YouTubeStore
SIGNING_KEY = 'test-only-ssh-transport-key'

def _records():
    captured = '2026-07-12T20:00:00Z'
    return {'channels': [{'channel_id': 'UCedgeTransport000000001', 'title': 'Transport Channel', 'source': 'edge-test', 'first_seen_at': captured, 'last_seen_at': captured}], 'videos': [{'video_id': 'edgeTransport01', 'title': 'Transport result', 'channel': 'Transport Channel', 'channel_id': 'UCedgeTransport000000001', 'views': 42, 'source': 'edge-test', 'first_seen_at': captured, 'last_seen_at': captured}], 'search_results': [], 'video_discoveries': [], 'video_snapshots': [], 'query_runs': []}

class FakeTransport:

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.acknowledged: list[str] = []
        self.download_batches: list[list[str]] = []
        self.ack_batches: list[list[str]] = []
        self.published_plans: list[tuple[str, str]] = []

    def read_worker_status(self):
        return {'state': 'running', 'message': 'collecting', 'node_id': 'oracle-test'}

    def read_organizer_status(self):
        return {'state': 'running', 'message': 'organizing', 'node_id': 'oracle-test', 'queued_jobs': 3}

    def list_bundle_names(self):
        return sorted(self.files)

    def download_bundle(self, filename):
        return self.files[filename]

    def download_bundles(self, filenames):
        names = list(filenames)
        self.download_batches.append(names)
        return {name: self.files[name] for name in names}

    def publish_discovery_plan(self, plan_path, *, signing_key):
        plan = load_discovery_plan(plan_path, signing_key=signing_key)
        self.published_plans.append((Path(plan_path).name, plan.revision))
        return plan.revision

    def acknowledge(self, *, bundle_filename, ack_path, signing_key):
        bundle_id = verified_ack_bundle_id(ack_path, signing_key=signing_key)
        self.acknowledged.append(bundle_id)
        self.files.pop(bundle_filename)
        return bundle_id

    def acknowledge_many(self, items, *, signing_key):
        pairs = list(items)
        self.ack_batches.append([name for name, _path in pairs])
        return [self.acknowledge(bundle_filename=name, ack_path=ack_path, signing_key=signing_key) for name, ack_path in pairs]

def _remote_bundle(tmp_path):
    bundle = build_bundle(node_id='oracle-test', kind='youtube.discovery.v1', job_id='radar-discovery', records=_records(), signing_key=SIGNING_KEY)
    path = write_bundle(bundle, tmp_path / 'remote')
    return (bundle, path)

def test_ssh_config_is_optional_but_complete_when_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv('SIGNAL_EDGE_SSH_HOST', raising=False)
    assert EdgeSSHConfig.from_env() is None
    key = tmp_path / 'edge-key'
    key.write_text('test', encoding='ascii')
    monkeypatch.setenv('SIGNAL_EDGE_SSH_HOST', '203.0.113.10')
    monkeypatch.setenv('SIGNAL_EDGE_SSH_KEY', str(key))
    config = EdgeSSHConfig.from_env()
    assert config is not None
    assert config.user == 'ubuntu'
    assert config.port == 22
    assert config.remote_root == '/var/lib/signal-edge'

def test_ssh_config_rejects_unsafe_remote_root(tmp_path, monkeypatch):
    key = tmp_path / 'edge-key'
    key.write_text('test', encoding='ascii')
    monkeypatch.setenv('SIGNAL_EDGE_SSH_HOST', '203.0.113.10')
    monkeypatch.setenv('SIGNAL_EDGE_SSH_KEY', str(key))
    monkeypatch.setenv('SIGNAL_EDGE_REMOTE_ROOT', '/var/lib/../etc')
    with pytest.raises(EdgeSSHTransportError, match='remote Signal edge root'):
        EdgeSSHConfig.from_env()

def test_pull_verifies_imports_acknowledges_and_cleans_remote(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(tmp_path / 'sync'))
    paths = EdgeSyncPaths.resolve()
    bundle, remote_path = _remote_bundle(tmp_path)
    transport = FakeTransport({remote_path.name: remote_path.read_bytes()})
    pulled = pull_edge_remote(paths=paths, signing_key=SIGNING_KEY, max_bundles=5, transport=transport)
    assert pulled['pulled'] == 1
    local = paths.inbox / remote_path.name
    assert local.exists()
    assert json.loads(paths.remote_status.read_text())['state'] == 'running'
    assert json.loads(paths.remote_organizer_status.read_text())['queued_jobs'] == 3
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    report = import_bundle(db_path, local, signing_key=SIGNING_KEY, ack_dir=paths.acknowledgements)
    archived = paths.archive / '2026-07' / local.name
    archived.parent.mkdir(parents=True)
    shutil.move(local, archived)
    flushed = pull_edge_remote(paths=paths, signing_key=SIGNING_KEY, max_bundles=0, transport=transport)
    assert report.bundle_id == bundle.bundle_id
    assert flushed['acknowledged'] == 1
    assert flushed['remote_pending'] == 0
    assert transport.acknowledged == [bundle.bundle_id]

def test_pull_batches_multiple_downloads_and_acknowledgements(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(tmp_path / 'sync'))
    paths = EdgeSyncPaths.resolve()
    remote: dict[str, bytes] = {}
    bundles = []
    for index in range(3):
        bundle = build_bundle(node_id='oracle-test', kind='youtube.discovery.v1', job_id='radar-discovery', records=_records(), metadata={'index': index}, signing_key=SIGNING_KEY)
        path = write_bundle(bundle, tmp_path / f'remote-{index}')
        remote[path.name] = path.read_bytes()
        bundles.append((bundle, path))
    transport = FakeTransport(remote)
    pulled = pull_edge_remote(paths=paths, signing_key=SIGNING_KEY, max_bundles=5, transport=transport)
    assert pulled['pulled'] == 3
    expected_names = sorted((path.name for _bundle, path in bundles))
    assert transport.download_batches == [expected_names]
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    for _bundle, path in bundles:
        import_bundle(db_path, paths.inbox / path.name, signing_key=SIGNING_KEY, ack_dir=paths.acknowledgements)
    flushed = pull_edge_remote(paths=paths, signing_key=SIGNING_KEY, max_bundles=0, transport=transport)
    assert flushed['acknowledged'] == 3
    assert transport.ack_batches == [expected_names]
    assert transport.files == {}

class RecordingSSHTransport(EdgeSSHTransport):

    def __init__(self, config, output=b''):
        super().__init__(config)
        self.output = output
        self.commands: list[tuple[str, bytes | None, int]] = []

    def _run(self, remote_command, *, input_bytes=None, timeout=180):
        self.commands.append((remote_command, input_bytes, timeout))
        return self.output

def test_real_ssh_transport_downloads_tar_batch(tmp_path):
    names = ['20260713000000-1111111111111111.signal-edge.json.gz', '20260713000001-2222222222222222.signal-edge.json.gz']
    payloads = {name: f'payload-{index}'.encode() for index, name in enumerate(names)}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, payload in payloads.items():
            member = tarfile.TarInfo(name=name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = RecordingSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key), stream.getvalue())
    assert transport.download_bundles(names) == payloads
    assert len(transport.commands) == 1
    assert 'tar -C /var/lib/signal-edge/outbox -cf -' in transport.commands[0][0]

def test_argv_enforces_fail_closed_host_key_verification(tmp_path):
    """The ONLY code enforcing host-key / MITM protection is _argv; pin its security flags so a
    future refactor can't silently weaken them (every other transport test stubs _run, so _argv
    would otherwise be untested)."""
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = EdgeSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key, port=2222))
    argv = transport._argv('true')

    def opt(name):
        for i, token in enumerate(argv):
            if token == '-o' and i + 1 < len(argv) and argv[i + 1].startswith(name + '='):
                return argv[i + 1]
        return None
    assert opt('StrictHostKeyChecking') == 'StrictHostKeyChecking=yes'
    assert opt('BatchMode') == 'BatchMode=yes'
    assert opt('IdentitiesOnly') == 'IdentitiesOnly=yes'
    assert '-i' in argv and str(key) in argv
    assert '-p' in argv and '2222' in argv
    assert argv[-2] == 'ubuntu@203.0.113.10'
    assert argv[-1] == 'true'

def test_worker_status_read_has_a_short_non_blocking_timeout(tmp_path):
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = RecordingSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key), b'{"state":"running"}')
    assert transport.read_worker_status()['state'] == 'running'
    assert transport.commands == [('sudo -n cat /var/lib/signal-edge/worker_status.json', None, 30)]

def test_key_configuration_uses_stdin_and_returns_counts_only(tmp_path):
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = RecordingSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key), json.dumps({'status': 'configured', 'configured_keys': 6, 'added_keys': 3, 'changed': True, 'restarted': True, 'services_active': True}).encode('utf-8'))
    additions = ['test_key_four_abcdefghijklmnop', 'test_key_five_abcdefghijklmnop', 'test_key_six_abcdefghijklmnopq']
    result = transport.configure_youtube_keys(additions)
    assert result['configured_keys'] == 6
    assert len(transport.commands) == 1
    command, payload, timeout = transport.commands[0]
    assert all((value not in command for value in additions))
    assert json.loads(payload)['keys'] == additions
    assert 'configure-youtube-keys.py' in command
    assert timeout == 180

def test_organizer_status_read_has_a_short_non_blocking_timeout(tmp_path):
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = RecordingSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key), b'{"state":"running","queued_jobs":2}')
    assert transport.read_organizer_status()['queued_jobs'] == 2
    assert transport.commands == [('sudo -n sh -c \'if [ -f /var/lib/signal-edge/organizer/organizer_status.json ]; then cat /var/lib/signal-edge/organizer/organizer_status.json; else printf "{}"; fi\'', None, 30)]

def test_real_ssh_transport_publishes_verified_plan_atomically(tmp_path):
    plan = build_discovery_plan(controller_id='signal-laptop', job_id='radar-discovery', queries=[EdgeQuery('history documentary', topic='history')], settings=DiscoveryPlanSettings(mode='free'), signing_key=SIGNING_KEY)
    path = write_bundle(plan, tmp_path / 'plans')
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = RecordingSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key))
    revision = transport.publish_discovery_plan(path, signing_key=SIGNING_KEY)
    assert revision == plan.bundle_id
    assert len(transport.commands) == 1
    command, payload, timeout = transport.commands[0]
    assert '/var/lib/signal-edge/jobs' in command
    assert '.partial' in command
    assert payload == path.read_bytes()
    assert timeout == 300

def test_real_ssh_transport_publishes_verified_organization_job_atomically(tmp_path):
    job = build_organization_job(controller_id='signal-laptop', job_id='organization-pilot', channels=[{'channel_id': 'UCedgeTransport000000001', 'title': 'Transport Channel', 'subscribers': 1200}], settings=OrganizationSettings(catalog_workers=1, face_workers=1), signing_key=SIGNING_KEY)
    path = write_bundle(job, tmp_path / 'organization-jobs')
    key = tmp_path / 'key'
    key.write_text('test', encoding='ascii')
    transport = RecordingSSHTransport(EdgeSSHConfig(host='203.0.113.10', user='ubuntu', key_path=key))
    revision = transport.publish_organization_job(path, signing_key=SIGNING_KEY)
    assert revision == job.bundle_id
    assert len(transport.commands) == 1
    command, payload, timeout = transport.commands[0]
    assert '/var/lib/signal-edge/organizer/jobs' in command
    assert '.partial' in command
    assert payload == path.read_bytes()
    assert timeout == 300

def test_pull_accepts_signed_organization_results(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(tmp_path / 'sync'))
    paths = EdgeSyncPaths.resolve()
    bundle = build_bundle(node_id='oracle-test', kind='youtube.organization.v1', job_id='organization-pilot', records={'channels': [], 'videos': [], 'channel_classifications': [], 'channel_face_checks': [], 'enrichment_attempts': [], 'organization_runs': []}, metadata={'source_job_revision': 'organization-job-revision', 'run_id': 'organization-run-1'}, signing_key=SIGNING_KEY)
    remote_path = write_bundle(bundle, tmp_path / 'remote-organization')
    transport = FakeTransport({remote_path.name: remote_path.read_bytes()})
    pulled = pull_edge_remote(paths=paths, signing_key=SIGNING_KEY, max_bundles=5, transport=transport)
    assert pulled['pulled'] == 1
    assert (paths.inbox / remote_path.name).exists()

def test_plan_sync_uploads_only_when_watchlist_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_EDGE_SYNC_ROOT', str(tmp_path / 'sync'))
    config = tmp_path / 'scraper_watchlist.json'
    config.write_text(json.dumps({'watchlist': ['history documentary'], 'harvest_expand': False, 'harvest_per_query': 40}), encoding='utf-8')
    paths = EdgeSyncPaths.resolve()
    transport = FakeTransport({})
    first = sync_edge_discovery_plan(paths=paths, signing_key=SIGNING_KEY, transport=transport, radar_config=config)
    first_plan_path = next(paths.plans.glob('*.signal-edge.json.gz'))
    first_plan = load_discovery_plan(first_plan_path, signing_key=SIGNING_KEY)
    second = sync_edge_discovery_plan(paths=paths, signing_key=SIGNING_KEY, transport=transport, radar_config=config)
    config.write_text(json.dumps({'watchlist': ['history documentary', 'space documentary'], 'harvest_expand': False, 'harvest_per_query': 40}), encoding='utf-8')
    third = sync_edge_discovery_plan(paths=paths, signing_key=SIGNING_KEY, transport=transport, radar_config=config)
    assert first['state'] == 'uploaded'
    assert first['query_count'] == 1
    assert first_plan.settings.batch_size == 125
    assert first_plan.settings.workers == 24
    assert first_plan.settings.turbo_enabled is True
    assert first_plan.settings.target_channels_per_hour == 450
    assert first_plan.settings.min_batch_size == 50
    assert second['state'] == 'unchanged'
    assert second['uploaded'] is False
    assert third['state'] == 'uploaded'
    assert third['query_count'] == 2
    assert len(transport.published_plans) == 2

def test_edge_yield_measurement_uses_recent_canonical_channels(tmp_path):
    db_path = tmp_path / 'youtube.sqlite3'
    captured = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript('\n            CREATE TABLE channels (\n                channel_id TEXT PRIMARY KEY,\n                first_seen_at TEXT NOT NULL\n            );\n            CREATE TABLE edge_query_runs (\n                id INTEGER PRIMARY KEY,\n                status TEXT NOT NULL,\n                finished_at TEXT NOT NULL\n            );\n            ')
        connection.executemany('INSERT INTO channels(channel_id, first_seen_at) VALUES (?, ?)', [(f'channel-{index}', captured) for index in range(105)])
        connection.executemany("INSERT INTO edge_query_runs(status, finished_at) VALUES ('ok', ?)", [(captured,) for _index in range(300)])
    assert _edge_channels_per_1000_queries(db_path) == 350
    assert _edge_plan_settings(db_path, previous_expected_channels_per_1000=340).expected_channels_per_1000_queries == 340
    assert _edge_plan_settings(db_path, previous_expected_channels_per_1000=300).expected_channels_per_1000_queries == 350

def test_tampered_ack_is_never_used_for_remote_cleanup(tmp_path):
    bundle, path = _remote_bundle(tmp_path)
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    ack_dir = tmp_path / 'acks'
    import_bundle(db_path, path, signing_key=SIGNING_KEY, ack_dir=ack_dir)
    ack_path = ack_dir / f'{bundle.bundle_id}.ack.json'
    payload = json.loads(ack_path.read_text(encoding='utf-8'))
    payload['payload']['status'] = 'tampered'
    ack_path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(EdgeSSHTransportError, match='checksum'):
        verified_ack_bundle_id(ack_path, signing_key=SIGNING_KEY)
