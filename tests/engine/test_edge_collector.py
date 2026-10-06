from __future__ import annotations
from contextlib import closing
import json
import sqlite3
from beacon_engine.edge.collector import ScheduledQuery, collect_discovery_batch
from beacon_engine.edge.importer import import_bundle
from beacon_engine.edge.plan import EdgeQuery, build_discovery_plan, plan_from_bundle
from beacon_engine.edge.protocol import build_bundle, write_bundle
from beacon_engine.research.providers import ProviderVideo
from beacon_engine.research.store import YouTubeStore
SIGNING_KEY = 'test-only-edge-signing-key'

class FakeSearch:
    name = 'fake-free'

    def search(self, query, *, limit, sort=None):
        if query == 'provider failure':
            raise RuntimeError('temporary failure key=do-not-leak')
        if query == 'empty query':
            return []
        suffix = 'N' if sort == 'newest' else 'R'
        return [ProviderVideo(video_id=f'edgeVid{suffix}{index:03d}', title=f'{query} result {index}', channel=f'Channel {index}', channel_id=f'UCedgeCollector{index:010d}', views=1000 + index, published_at='2 days ago', source='fake-free') for index in range(min(limit, 2))]

class FakePool:

    def __init__(self):
        self.units = 0

    def search_ids(self, query, *, limit=50, order='viewCount'):
        self.units += 100
        return ['official01']

    def videos(self, ids, *, part='snippet,statistics,contentDetails'):
        if not ids:
            return []
        self.units += 1
        return [{'id': video_id, 'snippet': {'title': f'Official {video_id}', 'channelTitle': f'Official Channel {index}', 'channelId': f'UCofficialCollector{index:08d}', 'publishedAt': '2026-07-10T00:00:00Z', 'description': 'Faceless documentary', 'categoryId': '27', 'thumbnails': {'high': {'url': 'https://img.example/video.jpg'}}}, 'statistics': {'viewCount': str(5000 + index)}, 'contentDetails': {'duration': 'PT8M1S'}} for index, video_id in enumerate(ids)]

    def channels(self, ids, *, part='snippet,statistics'):
        if not ids:
            return []
        self.units += 1
        return [{'id': channel_id, 'snippet': {'title': f'Official Channel {index}', 'customUrl': f'@official{index}', 'description': 'Documentary channel', 'publishedAt': '2025-01-01T00:00:00Z', 'thumbnails': {'medium': {'url': 'https://img.example/channel.jpg'}}}, 'statistics': {'subscriberCount': str(1200 + index), 'videoCount': '30', 'viewCount': '90000'}} for index, channel_id in enumerate(ids)]

def _plan(*, mode='hybrid'):
    return plan_from_bundle(build_discovery_plan(controller_id='signal-laptop', job_id='radar-discovery', queries=['history documentary'], settings={'mode': mode, 'workers': 3, 'batch_size': 3, 'max_quota_units_per_run': 10}, signing_key=SIGNING_KEY))

def test_hybrid_collector_enriches_and_tracks_success_empty_error():
    plan = _plan()
    scheduled = [ScheduledQuery(spec=plan.queries[0], sort='newest', previous_video_ids=('edgeVidN000',), previous_video_views=(('edgeVidN000', 1000),)), ScheduledQuery(spec=EdgeQuery(query='empty query', topic='empty', limit=2)), ScheduledQuery(spec=EdgeQuery(query='provider failure', topic='failure', limit=2))]
    batch = collect_discovery_batch(plan, scheduled, node_id='oracle-casa-1', run_id='run-0001', captured_at='2026-07-12T16:00:00Z', search_factory=FakeSearch, pool_factory=FakePool)
    assert batch.succeeded == 2
    assert batch.failed == 1
    assert batch.quota_units == 2
    assert len(batch.records['videos']) == 2
    assert len(batch.records['channels']) == 2
    assert len(batch.records['channel_classifications']) == 2
    assert len(batch.records['search_results']) == 1
    assert len(batch.records['video_discoveries']) == 1
    assert len(batch.records['query_runs']) == 3
    assert batch.records['query_runs'][0]['channel_ids'] == sorted((row['channel_id'] for row in batch.records['channels']))
    assert {row['topic'] for row in batch.records['channels']} == {'History'}
    assert {row['classifier'] for row in batch.records['channel_classifications']} == {'text-v3'}
    assert {row['sample_count'] for row in batch.records['channel_classifications']} == {1}
    assert {json.dumps(row['raw'], sort_keys=True) for row in batch.records['videos']} == {'{"snippet": {"categoryId": "27"}}'}
    assert {json.dumps(row['raw'], sort_keys=True) for row in batch.records['channels']} == {'{"snippet": {"thumbnails": {"medium": {"url": "https://img.example/channel.jpg"}}}}'}
    assert batch.metadata()['organization'] == {'topics': 2, 'text_classifications': 2, 'topic_classifier': 'topic-rules-v1', 'faceless_classifier': 'text-v3', 'faceless_mode': 'balanced'}
    assert batch.metadata()['delta'] == {'result_records': 2, 'new_results': 1, 'changed_results': 1, 'emitted_videos': 2, 'suppressed_repeat_results': 0}
    first = batch.records['query_runs'][0]
    assert first['sort'] == 'newest'
    assert first['result_count'] == 2
    assert first['new_result_count'] == 1
    empty = batch.records['query_runs'][1]
    assert empty['status'] == 'ok'
    assert empty['result_count'] == 0
    failed = batch.records['query_runs'][2]
    assert failed['status'] == 'error'
    assert 'do-not-leak' not in failed['error']
    assert batch.metadata()['free_search_retries'] == 0

def test_unchanged_repeat_results_only_emit_query_tracking():
    plan = _plan(mode='free')
    scheduled = [ScheduledQuery(spec=plan.queries[0], sort='newest', previous_video_ids=('edgeVidN000', 'edgeVidN001'), previous_video_views=(('edgeVidN000', 1000), ('edgeVidN001', 1001)))]
    batch = collect_discovery_batch(plan, scheduled, node_id='oracle-casa-1', run_id='run-repeat', captured_at='2026-07-12T16:01:00Z', search_factory=FakeSearch, pool_factory=FakePool)
    assert batch.records['channels'] == []
    assert batch.records['videos'] == []
    assert batch.records['channel_classifications'] == []
    assert batch.records['search_results'] == []
    assert batch.records['video_discoveries'] == []
    assert batch.records['video_snapshots'] == []
    assert len(batch.records['query_runs']) == 1
    assert batch.metadata()['delta'] == {'result_records': 2, 'new_results': 0, 'changed_results': 0, 'emitted_videos': 0, 'suppressed_repeat_results': 2}

def test_transient_empty_response_is_retried_with_bounded_backoff(monkeypatch):
    calls = {'count': 0}

    class TransientSearch:
        name = 'fake-free'

        def search(self, query, *, limit, sort=None):
            calls['count'] += 1
            if calls['count'] < 3:
                raise json.JSONDecodeError('temporary empty response', '', 0)
            return [ProviderVideo(video_id='retryRecovered01', title='Recovered documentary result', channel='Recovered Channel', channel_id='UCretryRecovered000001', views=5000, source='fake-free')]
    sleeps = []
    monkeypatch.setattr('beacon_engine.edge.collector.time.sleep', lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr('beacon_engine.edge.collector.random.uniform', lambda _start, _end: 0.0)
    plan = _plan(mode='free')
    batch = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0])], node_id='oracle-casa-1', run_id='run-retry', captured_at='2026-07-12T16:02:00Z', search_factory=TransientSearch, pool_factory=FakePool)
    assert batch.succeeded == 1
    assert calls['count'] == 3
    assert sleeps == [0.75, 1.5]
    assert batch.outcomes[0].attempts == 3
    assert batch.metadata()['free_search_retries'] == 2

def test_collected_bundle_imports_query_tracking_atomically(tmp_path):
    plan = _plan(mode='free')
    batch = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0])], node_id='oracle-casa-1', run_id='run-0002', captured_at='2026-07-12T16:05:00Z', search_factory=FakeSearch, pool_factory=FakePool)
    result_bundle = build_bundle(node_id=batch.node_id, kind='youtube.discovery.v1', job_id=batch.job_id, records=batch.records, metadata=batch.metadata(), created_at=batch.captured_at, signing_key=SIGNING_KEY)
    bundle_path = write_bundle(result_bundle, tmp_path / 'outbox')
    db_path = tmp_path / 'youtube.sqlite3'
    YouTubeStore(db_path)
    report = import_bundle(db_path, bundle_path, signing_key=SIGNING_KEY)
    assert report.counts['query_runs'] == 1
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute('\n            SELECT node_id, job_id, run_id, query, status, result_count\n            FROM edge_query_runs\n            ').fetchone()
    assert row == ('oracle-casa-1', 'radar-discovery', 'run-0002', 'history documentary', 'ok', 2)

def test_hybrid_collection_keeps_free_results_when_keys_are_unavailable():
    plan = _plan()

    def no_pool():
        raise RuntimeError('No YouTube API keys')
    batch = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0])], node_id='oracle-casa-1', run_id='run-0003', captured_at='2026-07-12T16:10:00Z', search_factory=FakeSearch, pool_factory=no_pool)
    assert batch.succeeded == 1
    assert len(batch.records['videos']) == 2
    assert batch.quota_units == 0
    assert batch.warnings == ('official enrichment unavailable: RuntimeError: No YouTube API keys',)

def test_hybrid_repeat_observes_views_but_skips_discarded_channel_lookups():

    class TrackingPool(FakePool):

        def __init__(self):
            super().__init__()
            self.video_calls = []
            self.channel_calls = []

        def videos(self, ids, **kwargs):
            self.video_calls.append(list(ids))
            return super().videos(ids, **kwargs)

        def channels(self, ids, **kwargs):
            self.channel_calls.append(list(ids))
            return super().channels(ids, **kwargs)
    pool = TrackingPool()
    plan = _plan()
    batch = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0], previous_video_ids=('edgeVidR000', 'edgeVidR001'), previous_video_views=(('edgeVidR000', 5000), ('edgeVidR001', 5001)))], node_id='oracle-casa-1', run_id='unchanged-repeat', search_factory=FakeSearch, pool_factory=lambda: pool)
    assert pool.video_calls == [['edgeVidR000', 'edgeVidR001']]
    assert pool.channel_calls == []
    assert batch.quota_units == 1
    assert len(batch.records['query_runs']) == 1
    assert batch.outcomes[0].video_ids == ('edgeVidR000', 'edgeVidR001')
    assert batch.records['channels'] == batch.records['videos'] == []
    assert batch.records['query_runs'][0]['channel_ids'] == []
    pool = TrackingPool()
    changed = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0], previous_video_ids=('edgeVidR000', 'edgeVidR001'), previous_video_views=(('edgeVidR000', 4999), ('edgeVidR001', 5001)))], node_id='oracle-casa-1', run_id='changed-repeat', search_factory=FakeSearch, pool_factory=lambda: pool)
    assert pool.channel_calls == [['UCofficialCollector00000000']]
    assert changed.quota_units == 2
    assert len(changed.records['channels']) == len(changed.records['videos']) == 1
    assert changed.records['channels'][0]['subscribers'] == 1200
    assert changed.records['video_snapshots'][0]['views'] == 5000
    assert changed.records['video_discoveries'] == []
    assert changed.records['query_runs'][0]['channel_ids'] == ['UCofficialCollector00000000']

def test_cloud_text_organization_marks_repeatable_channel_from_batch():

    class RepeatingSearch:
        name = 'fake-free'

        def search(self, query, *, limit, sort=None):
            return [ProviderVideo(video_id=f'repeatable{index:02d}', title=title, channel='History Vault', channel_id='UCrepeatableHistory0001', views=10000 + index, source='fake-free') for index, title in enumerate(('The Forgotten Empire Explained', 'The Rise and Fall of a Lost Dynasty', 'The Hidden History Nobody Tells'))]
    plan = _plan(mode='free')
    batch = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0])], node_id='oracle-casa-1', run_id='run-organization', captured_at='2026-07-12T16:12:00Z', search_factory=RepeatingSearch, pool_factory=FakePool)
    assert batch.records['channels'][0]['topic'] == 'History'
    classification = batch.records['channel_classifications'][0]
    assert classification['channel_id'] == 'UCrepeatableHistory0001'
    assert classification['sample_count'] == 3
    assert classification['likely_faceless'] == 1
    assert classification['confidence'] >= 0.58

def test_persistent_pool_quota_is_reported_per_batch_not_for_whole_day():
    plan = _plan()
    pool = FakePool()
    pool.units = 40
    batch = collect_discovery_batch(plan, [ScheduledQuery(spec=plan.queries[0])], node_id='oracle-casa-1', run_id='run-0004', captured_at='2026-07-12T16:15:00Z', search_factory=FakeSearch, pool_factory=lambda: pool)
    assert pool.units == 42
    assert batch.quota_units == 2
