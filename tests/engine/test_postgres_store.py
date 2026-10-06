"""Ported store contracts checked on real PostgreSQL and a synthetic SQLite index."""

import os
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import patch

from beacon_engine.research.postgres import PostgresDatabase
from beacon_engine.research.postgres_adapter import bind_sql, IndexRow
from beacon_engine.research.postgres_store import PostgresYouTubeStore, POSTGRES_INDEX_TARGET
from beacon_engine.research.store import YouTubeStore
from beacon_engine.research.providers.base import ProviderChannel, ProviderVideo, utc_now_iso
from beacon_engine.intelligence.workspaces import NicheWorkspaceService
from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.recommendations import IntelligenceRecommendationService
from beacon_engine.research.junk_filter import opportunity_name_rejection


class ParameterAndRowTests(unittest.TestCase):
    def test_bind_markers_do_not_rewrite_quoted_or_commented_questions(self):
        sql = "SELECT ?, '?', 'it''s ?', \"?\", $$?$$, $body$?$body$ -- ?\n/* ? */ WHERE value LIKE '%?%' AND id=?"
        expected = "SELECT %s, '?', 'it''s ?', \"?\", $$?$$, $body$?$body$ -- ?\n/* ? */ WHERE value LIKE '%%?%%' AND id=%s"
        self.assertEqual(bind_sql(sql), expected)

    def test_positional_and_named_access_preserve_duplicate_names(self):
        row = IndexRow([('value', 1), ('value', 2), ('other', 3)])
        self.assertEqual(row['value'], 1)
        self.assertEqual(row[1], 2)
        self.assertEqual(tuple(row), (1, 2, 3))
        self.assertEqual(dict(row), {'value': 1, 'other': 3})

    def test_schema_settings_cannot_inject_connection_options(self):
        for schema in ('public -c statement_timeout=0', 'a,b', 'a; DROP TABLE channels', ''):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                PostgresDatabase('dbname=test', schema=schema)


@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'),
                     'requires an explicit disposable PostgreSQL test database')
class PostgresStoreContracts(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ['BEACON_TEST_POSTGRES_DSN']
        self.admin = PostgresDatabase(self.dsn)
        self.schema = 'beacon_store_test_' + uuid4().hex
        with self.admin.connect() as conn:
            conn.execute('CREATE SCHEMA ' + self.schema)
        self.addCleanup(self._drop_schema)
        self.pg = PostgresYouTubeStore(dsn=self.dsn, schema=self.schema, initialize=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sqlite = YouTubeStore(Path(self.tmp.name) / 'index.sqlite3')
        self.cid = 'UC' + '1' * 22
        self.fixture_time = utc_now_iso()

    def _drop_schema(self):
        with self.admin.connect() as conn:
            conn.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    def _seed(self, store):
        with patch('beacon_engine.research.store.utc_now_iso', return_value=self.fixture_time):
            self._seed_at_fixed_time(store)

    def _seed_at_fixed_time(self, store):
        store.upsert_channel(ProviderChannel(
            channel_id=self.cid, title='Paper History', subscribers=10_000,
            video_count=24, view_count=2_000_000, published_at='2026-07-01T00:00:00Z',
            raw={'snippet': {'thumbnails': {'medium': {'url': 'https://example.test/avatar'}}}},
        ))
        for index in range(4):
            store.upsert_video(ProviderVideo(
                video_id=f'video{index:06d}', channel_id=self.cid,
                channel='Paper History', title=f'History {index}', views=40_000 + index,
                published_at='2026-09-01T00:00:00Z', duration_seconds=600,
            ))
        store.save_channel_classifications({self.cid: SimpleNamespace(
            likely_faceless=True, confidence=.95, score=2,
            sample_count=4, positive_title_count=4,
        )})

    def test_schema_columns_match_the_original_research_store(self):
        with self.sqlite.connect() as sqlite_conn, self.pg.connect() as pg_conn:
            tables = [r[0] for r in sqlite_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            for table in tables:
                old = [r['name'] for r in sqlite_conn.execute(f'PRAGMA table_info({table})')]
                new = [r[0] for r in pg_conn.execute(
                    'SELECT column_name FROM information_schema.columns '
                    'WHERE table_schema=? AND table_name=? ORDER BY ordinal_position',
                    (self.schema, table),
                )]
                with self.subTest(table=table):
                    self.assertEqual(old, new)

    def test_owner_upgrade_prevents_parallel_subtransactions_in_read_helpers(self):
        import psycopg
        helpers=('json_valid(text)','json_extract(text,text)','beacon_timestamp(text)',
                 'julianday(text)','julianday(text,text)','datetime(text)','datetime(text,text)')
        with self.pg.database.connect() as conn:
            conn.execute('CREATE TABLE parallel_read_probe AS '
                'SELECT CASE WHEN i %% 3=0 THEN %s ELSE %s END AS document, '
                'CASE WHEN i %% 4=0 THEN %s ELSE %s END AS published '
                'FROM generate_series(1,1000) i',
                ('invalid json',json.dumps({'avatar':'fixture','description':'before\0after'}),
                 'not a date','2026-07-01T00:00:00Z'))
            conn.execute('ANALYZE parallel_read_probe')
            # Reproduce the old version-4 labels even after new schema creation.
            for helper in helpers:conn.execute('ALTER FUNCTION '+helper+' PARALLEL SAFE')
            conn.execute('UPDATE beacon_schema_meta SET version=4')
        query="""SELECT SUM(CASE WHEN json_valid(document)
                    AND json_extract(document,'$.avatar')='fixture' THEN 1 ELSE 0 END) AS avatars,
                SUM(CASE WHEN julianday(published) IS NOT NULL THEN 1 ELSE 0 END) AS dated,
                MIN(datetime(published,'+1 day')) AS next_day,
                MAX(julianday(published,'-1 day')) AS previous_day,
                MIN(datetime(published)) AS original_day
                FROM parallel_read_probe"""
        def force_parallel(conn, *, debug=True):
            for setting in ('max_parallel_workers_per_gather=2','min_parallel_table_scan_size=0',
                            'parallel_setup_cost=0','parallel_tuple_cost=0',
                            'jit=off'):
                conn.execute('SET LOCAL '+setting)
            # Debug mode also launches workers for trivial statements *inside*
            # PL/pgSQL. Use it only to force the old failing outer plan; after
            # upgrade the normal planner still has zero parallel cost thresholds.
            conn.execute('SET LOCAL debug_parallel_query='+('on' if debug else 'off'))
        with self.assertRaises(psycopg.errors.InvalidTransactionState):
            with self.pg.database.connect() as conn:
                force_parallel(conn);conn.execute(query).fetchone()
        self.pg.initialize_schema()
        with self.pg.database.connect() as conn:
            force_parallel(conn,debug=False)
            result=conn.execute(query).fetchone()
            self.assertEqual(result['avatars'],667);self.assertEqual(result['dated'],750)
            self.assertEqual(result['next_day'],'2026-07-02 00:00:00')
            self.assertEqual(result['original_day'],'2026-07-01 00:00:00')
            self.assertEqual(result['previous_day'],2461221.5)
            plan=conn.execute('EXPLAIN (FORMAT JSON) '+query).fetchone()['QUERY PLAN']
            self.assertNotIn('Gather',json.dumps(plan))
            for helper in helpers:
                self.assertEqual(conn.execute('SELECT proparallel FROM pg_proc WHERE oid=%s::regprocedure',
                                              (self.schema+'.'+helper,)).fetchone()['proparallel'],'u')

    def test_factory_keeps_sqlite_sessions_and_public_identifier_separate(self):
        with patch.dict(os.environ, {'BEACON_POSTGRES_DSN': self.dsn,
                                     'BEACON_POSTGRES_SCHEMA': self.schema}):
            pg = YouTubeStore(POSTGRES_INDEX_TARGET)
            sqlite_store = YouTubeStore(Path(self.tmp.name) / 'lab-session.sqlite3')
            self.assertIsInstance(pg, PostgresYouTubeStore)
            self.assertEqual(sqlite_store.backend, 'sqlite')
            self.assertNotIn(self.dsn, str(pg.path))

    def test_big_counters_and_idempotent_seeds(self):
        for store in (self.sqlite, self.pg):
            with self.subTest(backend=store.backend):
                self.assertEqual(store.add_channel_seeds([self.cid]), 1)
                self.assertEqual(store.add_channel_seeds([self.cid]), 0)
                store.upsert_channel(ProviderChannel(channel_id=self.cid, view_count=9_000_000_000))
                with store.connect() as conn:
                    self.assertEqual(conn.execute('SELECT view_count FROM channels').fetchone()[0],
                                     9_000_000_000)

    def test_new_provider_descriptions_keep_originals_and_do_not_mutate_input(self):
        original = 'provider café\0漢字'
        channel = ProviderChannel(channel_id=self.cid, description=original, raw={'kept': 'channel'})
        video = ProviderVideo(video_id='nul-video', channel_id=self.cid, description=original, raw={'kept': 'video'})
        self.pg.upsert_channel(channel); self.pg.upsert_video(video)
        with self.pg.connect() as conn:
            for table in ('channels', 'videos'):
                row = conn.execute('SELECT description,raw_json FROM '+table).fetchone()
                self.assertEqual(row[0], 'provider café漢字')
                raw = json.loads(row[1])
                self.assertEqual(base64.b64decode(raw['_beacon_description_nul_v1']['original_utf8_base64']).decode(), original)
        self.assertEqual(channel.description, original); self.assertEqual(video.description, original)
        self.assertNotIn('_beacon_description_nul_v1', channel.raw)

    def test_query_channels_filters_rankings_and_avatars_match(self):
        for store in (self.sqlite, self.pg):
            self._seed(store)
        for kwargs in ({}, {'faceless_only': True}, {'replicable_only': True},
                       {'video_format': 'long'}, {'count_only': True},
                       {'added_within_days': 1}, {'max_subs': 500},
                       {'sort': 'opportunity_score'}):
            with self.subTest(filters=kwargs):
                old = self.sqlite.channel_metrics(**kwargs)
                new = self.pg.channel_metrics(**kwargs)
                self.assertEqual(len(old), len(new))
                for a, b in zip(old, new):
                    self.assertEqual(set(a), set(b))
                    for key, value in a.items():
                        if isinstance(value, float):
                            self.assertAlmostEqual(value, b[key], places=5)
                        else:
                            self.assertEqual(value, b[key], key)
        self.assertEqual(self.sqlite.channel_avatars([self.cid]), self.pg.channel_avatars([self.cid]))

    def test_workspaces_views_snapshots_and_exclusions(self):
        outputs = []
        for store in (self.sqlite, self.pg):
            self._seed(store)
            workspaces = NicheWorkspaceService(store)
            nid = workspaces.create_workspace('My History')
            self.assertEqual(workspaces.find_workspace_by_name('my HISTORY'), nid)
            workspaces.add_channel(nid, self.cid)
            views = IntelligenceRecommendationService(store)
            view = views.save_view(nid, 'Evidence', filters={'min_views': 1000})
            self.assertEqual(views.save_view(nid, 'eVIDENCE', filters={'min_views': 2000}), view)
            self.assertEqual(views.list_saved_views(nid)[0]['filters'], {'min_views': 2000})
            metrics = ChannelMetricsService(store)
            self.assertGreater(metrics.capture_channel(self.cid), 0)
            rows = workspaces.workspace_channels(nid)
            self.assertEqual(rows[0]['channel_id'], self.cid)
            outputs.append(rows[0]['average_views'])
            store.exclude_channels([self.cid], reason='manual')
            self.assertEqual(store.channel_metrics(), [])
            store.unexclude_channels([self.cid])
            self.assertEqual(len(store.channel_metrics()), 1)
            self.assertEqual(workspaces.list_workspaces()[0]['channel_count'], 1)
        self.assertEqual(outputs[0], outputs[1])

    def test_ranked_metrics_retain_boundaries_and_avoid_unselected_json(self):
        self._seed(self.sqlite); self._seed(self.pg)
        for store in (self.sqlite, self.pg):
            for index, uploads, views, published in (
                (2, 10, 100_000, '2026-07-01T00:00:00Z'),
                (3, 0, 100_000, 'not a date'),
                (4, 24, 0, ''),
                (5, 10, 100_000, 'not a date'),
            ):
                with patch('beacon_engine.research.store.utc_now_iso',return_value=self.fixture_time):
                    store.upsert_channel(ProviderChannel(
                        channel_id='UC'+str(index)*22, title='Paper History '+str(index),
                        subscribers=10_000, video_count=uploads, view_count=views,
                        published_at=published, raw={'deferred_avatar': index},
                    ))
        for filters in ({'limit': 1}, {'sort': 'days_since_start', 'descending': False},
                        {'count_only': True}, {'replicable_only': True},
                        {'min_avg_views': 10_000, 'max_avg_views': 10_000},
                        {'min_uploads': 10, 'max_uploads': 10},
                        {'min_days_since_start': 1, 'max_days_since_start': 1095}):
            with self.subTest(filters=filters):
                old=self.sqlite.channel_metrics(**filters)
                new=self.pg.channel_metrics(**filters)
                # SQL does not define tie ordering. Compare the complete records.
                self.assertEqual(len(old),len(new))
                self.assertEqual([row.get('channel_id') for row in old],
                                 [row.get('channel_id') for row in new])
                for left,right in zip(sorted(old,key=lambda row:row.get('channel_id','')),
                                      sorted(new,key=lambda row:row.get('channel_id',''))):
                    self.assertEqual(set(left),set(right))
                    for key,value in left.items():
                        if isinstance(value,float):self.assertAlmostEqual(value,right[key],places=7)
                        else:self.assertEqual(value,right[key],key)
        import psycopg
        with self.pg.database.connect() as conn:
            conn.execute("CREATE OR REPLACE FUNCTION json_valid(value TEXT) RETURNS BOOLEAN "
                "LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL UNSAFE AS $$ BEGIN "
                "IF strpos(value, 'deferred_avatar')>0 THEN "
                "RAISE EXCEPTION 'Unselected provider payload was parsed'; END IF; "
                "RETURN true; END $$")
        # A deliberate payload failure outside the page must not affect ranking
        # or counts; selecting that payload must still expose the real failure.
        self.assertEqual(self.pg.channel_metrics(limit=1)[0]['channel_id'],self.cid)
        self.assertEqual(self.pg.channel_metrics(count_only=True),[{'count':3}])
        with self.assertRaises(psycopg.errors.RaiseException):
            self.pg.channel_metrics(limit=10)

    def test_format_filters_preserve_eligibility_counts_and_rank_before_limit(self):
        for store in (self.sqlite, self.pg):
            for index, durations, views in (
                (1, (30, 60, 0), 9_000_000),
                (2, (90, 90, 0), 6_000_000),
                (3, (120, 600, -1), 3_000_000),
                (4, (0, -1), 2_000_000),
            ):
                cid = 'UC' + str(index) * 22
                with patch('beacon_engine.research.store.utc_now_iso', return_value=self.fixture_time):
                    store.upsert_channel(ProviderChannel(
                        channel_id=cid, title='Paper History '+str(index),
                        subscribers=10_000, video_count=24, view_count=views,
                        published_at='2026-07-01T00:00:00Z',
                    ))
                store.set_channel_topic([cid], 'History')
                store.save_channel_classifications({cid: SimpleNamespace(
                    likely_faceless=True, confidence=.95, score=2,
                    sample_count=3, positive_title_count=3,
                )})
                for position, duration in enumerate(durations):
                    store.upsert_video(ProviderVideo(
                        video_id=f'format-{index}-{position}', channel_id=cid,
                        duration_seconds=duration, title='History', views=1000,
                    ))
        for fmt in ('long', 'short'):
            for extra in ({}, {'limit': 1}, {'count_only': True},
                          {'min_subs': 9999, 'max_subs': 10001, 'topic': 'History',
                           'replicable_only': True, 'sort': 'opportunity_score'},
                          {'min_subs': 20000, 'count_only': True},
                          {'sort': 'subscribers', 'descending': False},
                          {'sort': 'revenue', 'rpm': -4.0, 'limit': 1},
                          {'sort': 'revenue', 'rpm': 0.0, 'limit': 2},
                          {'sort': 'opportunity_score', 'descending': False, 'limit': 1}):
                filters = dict(video_format=fmt, **extra)
                with self.subTest(filters=filters):
                    old = self.sqlite.channel_metrics(**filters)
                    new = self.pg.channel_metrics(**filters)
                    self.assertEqual(len(old), len(new))
                    for left, right in zip(old, new):
                        self.assertEqual(set(left), set(right))
                        for key, value in left.items():
                            if isinstance(value, float):
                                self.assertAlmostEqual(value, right[key], places=7)
                            else:
                                self.assertEqual(value, right[key], key)
        self.assertEqual(self.pg.channel_metrics(video_format='long', limit=1)[0]['channel_id'],
                         'UC' + '2' * 22)
        self.assertEqual(self.pg.channel_metrics(video_format='long', count_only=True),
                         [{'count': 2}])
        self.assertEqual(self.pg.channel_metrics(video_format='short', count_only=True),
                         [{'count': 1}])

    def test_format_page_stops_duration_reads_after_enough_ranked_matches(self):
        for index in range(40):
            cid = 'UC' + str(index).zfill(22)
            self.pg.upsert_channel(ProviderChannel(
                channel_id=cid, title='Ranked history', subscribers=1000,
                video_count=20, view_count=4_000_000-index*1000,
                published_at='2026-07-01T00:00:00Z',
            ))
            self.pg.upsert_video(ProviderVideo(
                video_id=f'ranked-{index}', channel_id=cid,
                duration_seconds=30 if index < 2 else 600,
            ))
        original_connect = self.pg.connect
        captured = []

        def inspected_connect():
            connection = original_connect()
            original_execute = connection.execute

            def execute(statement, parameters=None):
                captured.append(original_execute(
                    'EXPLAIN (ANALYZE, FORMAT JSON) '+statement,
                    parameters).fetchone()[0])
                return original_execute(statement, parameters)

            connection.execute = execute
            return connection

        with patch.object(self.pg, 'connect', side_effect=inspected_connect):
            page = self.pg.channel_metrics(video_format='long', limit=1)
        self.assertEqual([r['channel_id'] for r in page], ['UC'+str(2).zfill(22)])

        def nodes(node):
            yield node
            for child in node.get('Plans', []):
                yield from nodes(child)

        duration_reads = [n for n in nodes(captured[0][0]['Plan'])
                          if n.get('Relation Name') == 'videos']
        self.assertTrue(duration_reads)
        self.assertTrue(all(n['Actual Loops'] == 3 for n in duration_reads), duration_reads)
        # Counts must still inspect the complete set, including later matches.
        self.assertEqual(self.pg.channel_metrics(video_format='long', count_only=True),
                         [{'count': 38}])

    def test_equal_discovery_times_and_scores_have_stable_page_boundaries(self):
        for store in (self.sqlite,self.pg):
            for index in (3,1,2):
                with patch('beacon_engine.research.store.utc_now_iso',return_value=self.fixture_time):
                    store.upsert_channel(ProviderChannel(
                        channel_id='UC'+str(index)*22,title='Stable history',
                        subscribers=10_000,video_count=24,view_count=2_000_000,
                        published_at='2026-07-01T00:00:00Z'))
        for sort in ('avg_views','opportunity_score','added_at'):
            for descending in (True,False):
                with self.subTest(sort=sort,descending=descending):
                    for store in (self.sqlite,self.pg):
                        self.assertEqual([row['channel_id'] for row in
                            store.channel_metrics(sort=sort,descending=descending,limit=2)],
                            ['UC'+'1'*22,'UC'+'2'*22])

    def test_full_index_metric_predicates_do_not_drive_classification_probes(self):
        self._seed(self.pg)
        original_connect=self.pg.connect
        captured=[]
        def inspected_connect():
            connection=original_connect(); original_execute=connection.execute
            def execute(statement,parameters=None):
                captured.append(original_execute('EXPLAIN (FORMAT JSON) '+statement,
                                                 parameters).fetchone()[0])
                return original_execute(statement,parameters)
            connection.execute=execute
            return connection
        with patch.object(self.pg,'connect',side_effect=inspected_connect):
            self.pg.channel_metrics(replicable_only=True,sort='opportunity_score',limit=1)
        def nodes(node):
            yield node
            for child in node.get('Plans',[]):yield from nodes(child)
        all_nodes=list(nodes(captured[0][0]['Plan']))
        metric_scan=next(node for node in all_nodes
                         if node.get('Node Type')=='CTE Scan' and node.get('CTE Name')=='m')
        self.assertIn('opportunity_score',metric_scan['Filter'])
        self.assertFalse(any('opportunity_score' in node.get('Filter','')
                             for node in all_nodes if node.get('Relation Name')=='channels'))

    def test_discovery_events_and_run_identities(self):
        self._seed(self.pg)
        run = self.pg.start_run(kind='test', query='history')
        video = ProviderVideo(video_id='video000000', channel_id=self.cid)
        self.pg.save_search_results('history', 'test', [video], run_id=run)
        self.pg.save_search_results('history', 'test', [video], run_id=run)
        self.pg.finish_run(run, status='ok', counts={'videos': 1})
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM search_results').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM crawl_runs WHERE id=?', (run,)).fetchone()[0], 'ok')

    def test_name_gate_matches_python_including_word_boundaries(self):
        titles = ('Paper History', 'Netflix', 'National Geographic', 'Geography Now',
                  'Drake - Topic', 'TaylorSwiftVEVO', 'K-Pop Hits', 'Cocomelon',
                  'BBC Earth', 'Memes', 'αbbcα', '  NBC NEWS\t', '', None)
        with self.pg.connect() as conn:
            for title in titles:
                with self.subTest(title=title):
                    result = conn.execute('SELECT opportunity_name_rejection(?)', (title,)).fetchone()[0]
                    self.assertEqual(result, int(bool(opportunity_name_rejection(title))))

    def test_malformed_dates_and_json_do_not_break_queries(self):
        with self.pg.connect() as conn:
            row = conn.execute("SELECT julianday(?), json_valid(?), json_extract(?, '$.snippet.categoryId')",
                               ('not a date', '{broken', '{broken')).fetchone()
            self.assertEqual(tuple(row), (None, False, None))

    def test_json_nul_does_not_break_listing_or_change_stored_metadata(self):
        raw = {'snippet': {'description': 'before\0\0after café漢字',
                           'thumbnails': {'medium': {'url': 'https://example.test/avatar'}}}}
        encoded = json.dumps(raw, ensure_ascii=False)
        for store in (self.sqlite, self.pg):
            self._seed(store)
            with store.connect() as conn:
                conn.execute('UPDATE channels SET raw_json=? WHERE channel_id=?', (encoded, self.cid))
            self.assertEqual(store.channel_metrics()[0]['avatar_url'], 'https://example.test/avatar')
            self.assertEqual(store.channel_avatars([self.cid]), {self.cid: 'https://example.test/avatar'})
            with store.connect() as conn:
                self.assertEqual(conn.execute('SELECT raw_json FROM channels').fetchone()[0], encoded)
        with self.pg.connect() as conn:
            self.assertTrue(conn.execute('SELECT json_valid(?)', (encoded,)).fetchone()[0])
            self.assertEqual(conn.execute("SELECT json_extract(?, '$.snippet.description')", (encoded,)).fetchone()[0],
                             'beforeafter café漢字')
            for value in ('literal\\u0000', 'slash\\\0end', '\\'*2+'\0end', 'a\0\0b'):
                with self.subTest(value=value):
                    payload = json.dumps({'value': value})
                    self.assertEqual(conn.execute("SELECT json_extract(?, '$.value')", (payload,)).fetchone()[0],
                                     value.replace('\0', ''))
            malformed = r'[1\u0000]'
            self.assertEqual(tuple(conn.execute("SELECT json_valid(?), json_extract(?, '$.value')",
                                               (malformed, malformed)).fetchone()), (False, None))

    def test_schema_upgrade_preserves_records_and_replaces_json_helpers(self):
        self._seed(self.pg)
        with self.pg.connect() as conn:
            original = tuple(conn.execute('SELECT * FROM channels').fetchone())
            conn.execute('UPDATE beacon_schema_meta SET version=3')
            conn.execute("CREATE OR REPLACE FUNCTION json_valid(value TEXT) RETURNS BOOLEAN "
                         "LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$ BEGIN "
                         "PERFORM value::jsonb; RETURN true; "
                         "EXCEPTION WHEN invalid_text_representation THEN RETURN false; END $$")
        self.pg.initialize_schema()
        self.pg.initialize_schema()
        with self.pg.connect() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM channels').fetchone()), original)
            self.assertEqual(conn.execute('SELECT version FROM beacon_schema_meta').fetchone()[0], 6)
            self.assertTrue(conn.execute('SELECT json_valid(?)', (json.dumps({'description':'a\0b'}),)).fetchone()[0])

    def test_read_only_store_blocks_writes(self):
        import psycopg
        read = PostgresYouTubeStore(dsn=self.dsn, schema=self.schema, read_only=True)
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            read.add_channel_seeds([self.cid])

    def test_statistics_index_upgrade_preserves_records_and_covers_video_reads(self):
        self._seed(self.pg)
        names=('idx_videos_channel_metrics','idx_video_snapshots_count',
               'idx_video_discoveries_count')
        workspaces=NicheWorkspaceService(self.pg)
        niche=workspaces.create_workspace('Statistics index probe')
        workspaces.add_channel(niche,self.cid)
        with self.pg.database.connect() as conn:
            for name in names:conn.execute('DROP INDEX '+name)
            conn.execute('UPDATE beacon_schema_meta SET version=5')
            # Mix channels across heap pages, as in the full imported catalog.
            conn.execute("INSERT INTO videos(video_id,channel_id,duration_seconds,first_seen_at,last_seen_at) "
                         "SELECT 'index-probe-'||i,CASE WHEN i %% 50=0 THEN %s "
                         "ELSE 'index-neighbor-'||(i %% 250) END,"
                         "CASE WHEN i %% 4=0 THEN 0 ELSE 120 END,%s,%s "
                         "FROM generate_series(1,1000) i",(self.cid,self.fixture_time,self.fixture_time))
        before_counts=self.pg.summary()
        before_workspaces=workspaces.list_workspaces()
        self.assertEqual(before_workspaces[0]['video_count'],24)
        before_formats=self.pg.channel_metrics(video_format='long')
        self.pg.initialize_schema();self.pg.initialize_schema()
        self.assertEqual(self.pg.summary(),before_counts)
        self.assertEqual(NicheWorkspaceService(self.pg).list_workspaces(),before_workspaces)
        self.assertEqual(self.pg.channel_metrics(video_format='long'),before_formats)
        # The full import prepares visibility maps before reader benchmarking.
        # Reproduce that prerequisite rather than forcing an impossible
        # heap-free scan immediately after the fixture's inserts.
        with self.pg.database.open() as conn:
            conn.autocommit=True
            conn.execute('VACUUM ANALYZE videos')
        with self.pg.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT version FROM beacon_schema_meta').fetchone()['version'],6)
            for name in names:
                metadata=conn.execute('SELECT indisvalid,indisready FROM pg_index '
                                      'WHERE indexrelid=%s::regclass',(self.schema+'.'+name,)).fetchone()
                self.assertTrue(metadata['indisvalid']);self.assertTrue(metadata['indisready'])
            conn.execute('SET LOCAL enable_seqscan=off')
            plan=conn.execute('EXPLAIN (FORMAT JSON) SELECT last_seen_at,duration_seconds '
                              'FROM videos WHERE channel_id=%s',(self.cid,)).fetchone()['QUERY PLAN'][0]['Plan']
            self.assertEqual(plan['Node Type'],'Index Only Scan')
            self.assertEqual(plan['Index Name'],'idx_videos_channel_metrics')
        # Counts follow real writes and rollback, rather than cached estimates.
        import psycopg
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.pg.database.connect() as conn:
                conn.execute("INSERT INTO video_snapshots(video_id,views,captured_at) "
                             "VALUES ('video000000',1,%s)",(self.fixture_time,))
                self.assertEqual(conn.execute('SELECT count(*) AS n FROM video_snapshots').fetchone()['n'],
                                 before_counts['video_snapshots']+1)
                conn.execute("DO $$ BEGIN RAISE EXCEPTION 'Roll back count probe'; END $$")
        self.assertEqual(self.pg.summary(),before_counts)


if __name__ == '__main__':
    unittest.main()
