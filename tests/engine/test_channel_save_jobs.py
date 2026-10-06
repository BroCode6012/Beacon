"""Durable saves, recovery and lease fencing on a real PostgreSQL schema."""

import os
import datetime as dt
import threading
import unittest
from unittest.mock import patch

from tests.engine import test_postgres_store as fixtures
from beacon_engine.research.channel_save_jobs import (
    ChannelSaveQueue, ChannelSaveWorker, InvalidSaveLink, LostSaveLease,
    parse_save_link, YouTubeSaveProvider, LEASE_SECONDS, MAX_ATTEMPTS,
)
from beacon_engine.intelligence.analyst_tools import IntelligenceAnalystTools


class SaveLinkTests(unittest.TestCase):
    def test_supported_links_have_stable_shapes(self):
        cid = 'UC' + '1' * 22
        cases = {
            cid: ('channel_id', cid),
            'https://www.youtube.com/channel/' + cid: ('channel_id', cid),
            'youtube.com/@DisasterBreakdown/videos': ('handle', 'DisasterBreakdown'),
            '@DisasterBreakdown': ('handle', 'DisasterBreakdown'),
            'https://youtu.be/abcdefghijk?t=12': ('video_id', 'abcdefghijk'),
            'https://youtube.com/watch?list=x&v=abcdefghijk': ('video_id', 'abcdefghijk'),
            'https://youtube.com/shorts/abcdefghijk': ('video_id', 'abcdefghijk'),
            'https://youtube.com/user/Example': ('username', 'Example'),
            'https://youtube.com/c/Example': ('name', 'Example'),
            'Paper   History': ('name', 'Paper History'),
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(parse_save_link(link), expected)

    def test_invalid_and_foreign_urls_fail_without_network_or_queue_writes(self):
        for link in ('', 'x' * 1025, 'https://youtube.com/', 'https://evil.test/@Example',
                     'https://youtube.com.evil.test/@Example', 'https://user:password@youtube.com/@Example',
                     'file:///private/file', 'https://youtube.com/watch?v=bad'):
            with self.subTest(link=link), self.assertRaises(InvalidSaveLink):
                parse_save_link(link)

    def test_analysis_maps_api_data_without_scanning_unrelated_channels(self):
        provider = YouTubeSaveProvider.__new__(YouTubeSaveProvider)
        from unittest.mock import Mock
        provider.pool = Mock()
        cid = 'UC' + '1' * 22
        provider.pool.channels.return_value = [{
            'id': cid, 'snippet': {'title': 'Paper History', 'publishedAt': '2026-07-01T00:00:00Z'},
            'statistics': {'subscriberCount': '2000', 'viewCount': '2400000', 'videoCount': '24'},
            'contentDetails': {'relatedPlaylists': {'uploads': 'test-playlist'}},
        }]
        provider.pool.playlist_video_ids.return_value = ['abcdefghijk']
        provider.pool.videos.return_value = [{
            'id': 'abcdefghijk', 'snippet': {'title': 'History documentary', 'channelId': cid,
                'publishedAt': '2026-09-01T00:00:00Z', 'categoryId': '27'},
            'statistics': {'viewCount': '100000'}, 'contentDetails': {'duration': 'PT10M'},
        }]
        packet = provider.analyse(cid)
        self.assertEqual(packet['channel']['subscribers'], 2000)
        self.assertEqual(packet['videos'][0]['views'], 100000)
        self.assertEqual(packet['videos'][0]['channel_id'], cid)
        self.assertEqual(packet['average_views'], 100000)
        self.assertEqual(packet['classifications'][0]['channel_id'], cid)
        provider.pool.playlist_video_ids.assert_called_once_with('test-playlist', limit=30)


@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'),
                     'requires an explicit disposable PostgreSQL test database')
class DurableSaveContracts(unittest.TestCase):
    setUp = fixtures.PostgresStoreContracts.setUp
    _drop_schema = fixtures.PostgresStoreContracts._drop_schema

    def queue(self):
        self.clock = [1_000_000.0]
        return ChannelSaveQueue(self.pg, clock=lambda: self.clock[0])

    def packet(self):
        return {'channel': {'channel_id': self.cid, 'title': 'Saved History',
                             'subscribers': 2000, 'video_count': 24, 'view_count': 2400000},
                'videos': [], 'classifications': [], 'snapshots': [], 'average_views': 100000}

    def test_automatic_checks_recover_legacy_shelf_without_browser_and_respect_cooldowns(self):
        now = dt.datetime.now(dt.timezone.utc)
        queue = ChannelSaveQueue(self.pg, clock=lambda: now.timestamp())
        queue.enqueue(self.cid)
        worker = ChannelSaveWorker(queue, provider_factory=lambda: type('Provider', (), {
            'analyse': lambda _, cid: self.packet(),
        })())
        worker.run_once()
        self.assertEqual(queue.enqueue_due_saved(), 0)  # Active request is not duplicated.
        worker.run_once()
        with self.pg.connect() as c:
            old = (now - dt.timedelta(hours=7)).isoformat().replace('+00:00', 'Z')
            c.execute('UPDATE channel_save_requests SET updated_at=?', (old,))
            c.execute("UPDATE channel_snapshots SET captured_at=?,source='local-index'", (old,))
        self.assertEqual(queue.enqueue_due_saved(), 1)
        self.assertEqual(queue.enqueue_due_saved(), 0)
        self.assertTrue(queue.list()[0]['result']['automatic_refresh'])
        worker.run_once()
        self.assertEqual(queue.enqueue_due_saved(), 0)
        with self.pg.connect() as c:
            c.execute('DELETE FROM channel_snapshots')
            c.execute("UPDATE channel_save_requests SET status='failed',updated_at=?", (old,))
            c.execute('UPDATE niche_channels SET enabled=0')
        self.assertEqual(queue.enqueue_due_saved(), 0)  # Removed membership stays removed.
        with self.pg.connect() as c:c.execute('UPDATE niche_channels SET enabled=1')
        self.assertEqual(queue.enqueue_due_saved(), 1)
        # A manual click elevates a pending automatic request instead of duplicating it.
        job = queue.claim()
        self.assertEqual(job['phase'], 'analyse')
        queue.fail(job, 'Fixture failure', permanent=True)
        self.assertEqual(queue.enqueue_due_saved(), 0)
        self.assertEqual(queue.enqueue(self.cid)['phase'], 'analyse')
        self.assertFalse(queue.list()[0]['result'].get('automatic_refresh', False))

    def test_enqueue_returns_a_committed_request_and_duplicates_reuse_it(self):
        queue = self.queue()
        one = queue.enqueue('https://youtube.com/channel/' + self.cid)
        two = queue.enqueue(self.cid)
        self.assertEqual(one['id'], two['id'])
        self.assertEqual(one['status'], 'queued')
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM channel_save_requests').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM niche_channels').fetchone()[0], 0)

    def test_shelf_refresh_queues_analysis_without_duplicates_or_early_ready(self):
        queue = self.queue()
        queue.enqueue(self.cid)
        ChannelSaveWorker(queue).run_once()
        queued = queue.list()[0]
        self.assertEqual(queue.refresh_saved(queued['niche_id']), {'queued_refresh': 1})
        self.assertEqual(len(queue.list()), 1)
        worker = ChannelSaveWorker(queue, provider_factory=lambda: type('Provider', (), {
            'analyse': lambda _, cid: self.packet(),
        })())
        worker.run_once()
        self.assertEqual(queue.list()[0]['status'], 'ready')
        queue.refresh_saved(queued['niche_id'])
        refreshed = queue.list()[0]
        self.assertEqual(refreshed['status'], 'queued')
        self.assertEqual(refreshed['phase'], 'analyse')
        self.assertTrue(refreshed['saved_at'])
        self.assertEqual(refreshed['attempts'], 0)
        self.assertEqual(queue.refresh_saved(987654), {'queued_refresh': 0})

    def test_save_commits_before_analysis_and_browser_is_not_a_worker(self):
        queue = self.queue()
        queue.enqueue(self.cid)
        packet = self.packet()
        owner = self

        class Provider:
            def analyse(self, cid):
                # A separate reader sees Saved before any slow provider work.
                with owner.pg.connect() as conn:
                    owner.assertEqual(conn.execute('SELECT COUNT(*) FROM niche_channels').fetchone()[0], 1)
                    owner.assertEqual(conn.execute('SELECT saved_at FROM channel_save_requests').fetchone()[0] != '', True)
                return packet

        worker = ChannelSaveWorker(queue, provider_factory=Provider)
        self.assertTrue(worker.run_once())
        saved = queue.list()[0]
        self.assertTrue(saved['saved_at'])
        self.assertEqual((saved['status'], saved['phase']), ('queued', 'analyse'))
        # Reconstructing the queue models a newly connected browser/process.
        restored = ChannelSaveQueue(self.pg, clock=lambda: self.clock[0])
        self.assertEqual(restored.list()[0]['id'], saved['id'])
        self.assertTrue(ChannelSaveWorker(restored, provider_factory=Provider).run_once())
        self.assertEqual(restored.list()[0]['status'], 'ready')
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT title FROM channels').fetchone()[0], 'Saved History')
            self.assertEqual(conn.execute('SELECT autonomous FROM niches').fetchone()[0], 1)

    def test_crashed_worker_is_reclaimed_and_stale_worker_cannot_write(self):
        queue = self.queue()
        queue.enqueue(self.cid)
        abandoned = queue.claim()
        self.assertIsNone(queue.claim())
        self.clock[0] += LEASE_SECONDS + 1
        resumed = queue.claim()
        self.assertNotEqual(abandoned['lease_owner'], resumed['lease_owner'])
        with self.assertRaises(LostSaveLease):
            queue.file_channel(abandoned, self.cid)
        queue.file_channel(resumed, self.cid)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM niche_channels').fetchone()[0], 1)

    def test_analysis_failures_preserve_membership_bound_retries_and_hide_secrets(self):
        queue = self.queue()
        queue.enqueue(self.cid)

        def unavailable():
            raise RuntimeError('https://provider.test?key=synthetic-secret password=synthetic-secret')

        worker = ChannelSaveWorker(queue, provider_factory=unavailable)
        worker.run_once()  # Direct channel links can be saved without API credentials.
        for _ in range(MAX_ATTEMPTS):
            worker.run_once()
            self.clock[0] += 100
        request = queue.list()[0]
        self.assertEqual(request['status'], 'failed')
        self.assertEqual(request['attempts'], MAX_ATTEMPTS)
        self.assertTrue(request['saved_at'])
        self.assertNotIn('synthetic-secret', str(request))
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM niche_channels').fetchone()[0], 1)
        retry = queue.retry(request['id'])
        self.assertEqual((retry['status'], retry['attempts']), ('queued', 0))

    def test_simultaneous_requests_for_one_link_are_idempotent(self):
        queue = self.queue()
        ids, errors = [], []

        def submit():
            try:
                ids.append(queue.enqueue(self.cid)['id'])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 1)

    def test_wrong_analysis_channel_cannot_overwrite_saved_request(self):
        queue = self.queue()
        queue.enqueue(self.cid)
        job = queue.claim()
        queue.file_channel(job, self.cid)
        job['channel_id'] = self.cid
        packet = self.packet()
        packet['channel']['channel_id'] = 'UC' + '9' * 22
        with self.assertRaises(ValueError):
            queue.apply_analysis(job, packet)
        self.assertTrue(queue.list()[0]['saved_at'])

    def test_worker_commits_invalidate_finder_workspace_cache(self):
        queue = self.queue()
        tools = IntelligenceAnalystTools(self.pg)
        self.assertEqual(tools.dispatch('list_niches'), [])
        queue.enqueue(self.cid)
        ChannelSaveWorker(queue).run_once()  # No provider needed for direct-id filing.
        tools.dispatch('list_channel_save_requests')
        self.assertEqual(len(tools.dispatch('list_niches')), 1)
