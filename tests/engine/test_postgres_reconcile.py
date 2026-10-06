"""Catch-up must preserve inserts, edits, removals, relationships and ID clocks."""
import os
import base64
from pathlib import Path
import unittest

from tests.engine import test_postgres_migration as fixtures
from beacon_engine.research.postgres_migration import MigrationError,snapshot_sqlite,migrate_snapshot,fingerprint_postgres
from beacon_engine.research.postgres_reconcile import reconcile_snapshot,row_hash,postgres_row_hash


@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'),'requires disposable PostgreSQL')
class ReconciliationContracts(unittest.TestCase):
    setUp=fixtures.MigrationContracts.setUp
    _drop_schema=fixtures.MigrationContracts._drop_schema
    _seed=fixtures.MigrationContracts._seed
    _seed_at_fixed_time=fixtures.MigrationContracts._seed_at_fixed_time
    legacy_tables=fixtures.MigrationContracts.legacy_tables
    snapshot=fixtures.MigrationContracts.snapshot

    def initial(self):
        migrate_snapshot(self.snapshot(),self.pg.database)

    def test_description_policy_matches_migration_and_idempotent_catch_up(self):
        self.initial()
        original = 'updated café\0漢字'
        with self.sqlite.connect() as conn:
            conn.execute('UPDATE channels SET description=?', (original,))
            conn.execute('UPDATE videos SET description=? WHERE video_id=(SELECT min(video_id) FROM videos)', (original,))
        path = self.final()
        for attempt in range(2):
            result = reconcile_snapshot(path, self.pg.database, writers_paused=True,
                                        normalize_provider_descriptions=True)
            self.assertEqual(len(result['description_normalizations']), 2)
            self.assertTrue(all(base64.b64decode(x['original_utf8_base64']).decode() == original
                                for x in result['description_normalizations']))
            self.assertEqual(result['changes']['channels']['upserted'], 1 if attempt == 0 else 0)
        with self.pg.connect() as conn:
            self.assertEqual(conn.execute('SELECT description FROM channels').fetchone()[0], 'updated café漢字')

    def final(self):
        path=Path(self.tmp.name)/'final.sqlite3'
        snapshot_sqlite(self.sqlite.path,path)
        return path

    def test_sql_hash_matches_typed_python_hash_for_unicode_nulls_float_and_big_integer(self):
        from psycopg import sql
        columns=[{'column_name':n,'data_type':t} for n,t in
                 [('word','text'),('absent','text'),('amount','bigint'),('ratio','double precision')]]
        values=('café\n漢字\"%',None,9000000000,-0.25)
        with self.pg.database.connect() as conn:
            hashed=conn.execute(sql.SQL('SELECT {} AS hash FROM (SELECT %s::text word,%s::text absent,%s::bigint amount,%s::double precision ratio) value').format(postgres_row_hash(columns)),values).fetchone()['hash']
        self.assertEqual(bytes(hashed),row_hash(values,columns))

    def test_changes_new_parent_child_and_deletions_match_all_thirty_tables(self):
        self.initial()
        with self.sqlite.connect() as conn:
            conn.execute('UPDATE channels SET title=?,subscribers=?',('Updated café\n漢字',9000000000))
            conn.execute("INSERT INTO channels(channel_id,title,first_seen_at,last_seen_at) VALUES('UC-new','New',?,?)",(self.fixture_time,self.fixture_time))
            conn.execute("INSERT INTO niches(id,name,created_at,updated_at) VALUES(90,'New workspace',?,?)",(self.fixture_time,self.fixture_time))
            conn.execute("INSERT INTO niche_channels(niche_id,channel_id,role,added_at) VALUES(90,'UC-new','competitor',?)",(self.fixture_time,))
            conn.execute('DELETE FROM niche_channels WHERE niche_id=47')
            conn.execute('DELETE FROM niches WHERE id=47')
            conn.execute('DELETE FROM videos WHERE video_id=(SELECT min(video_id) FROM videos)')
            conn.execute("UPDATE sqlite_sequence SET seq=150 WHERE name='niches'")
        result=reconcile_snapshot(self.final(),self.pg.database,writers_paused=True)
        self.assertTrue(result['verified']);self.assertEqual(len(result['tables']),30)
        self.assertEqual(result['changes']['channels']['upserted'],2)
        self.assertEqual(result['changes']['videos']['deleted'],1)
        self.assertEqual(result['changes']['niches']['deleted'],1)
        with self.pg.database.connect() as conn:
            added=conn.execute("INSERT INTO niches(name,created_at,updated_at) VALUES('Later',%s,%s) RETURNING id",(self.fixture_time,self.fixture_time)).fetchone()['id']
            self.assertEqual(added,151)

    def test_no_change_is_idempotent_and_does_not_rewrite_records(self):
        self.initial()
        source=self.final()
        for _ in range(2):
            result=reconcile_snapshot(source,self.pg.database,writers_paused=True)
            self.assertTrue(all(v=={'upserted':0,'deleted':0} for v in result['changes'].values()))

    def test_batched_mutations_do_not_change_the_named_cursor_snapshot(self):
        self.legacy_tables()
        with self.sqlite.connect() as conn:
            conn.executemany('INSERT INTO channels(channel_id,title,first_seen_at,last_seen_at) VALUES(?,?,?,?)',
                [(f'bulk-{i:05}',f'Original {i}',self.fixture_time,self.fixture_time) for i in range(5000)])
        initial=Path(self.tmp.name)/'initial.sqlite3'
        snapshot_sqlite(self.sqlite.path,initial);migrate_snapshot(initial,self.pg.database)
        with self.sqlite.connect() as conn:
            conn.execute("UPDATE channels SET title='Changed'")
            conn.execute("DELETE FROM channels WHERE channel_id<'bulk-02000'")
            conn.executemany('INSERT INTO channels(channel_id,title,first_seen_at,last_seen_at) VALUES(?,?,?,?)',
                [(f'bulk-{i:05}',f'New {i}',self.fixture_time,self.fixture_time) for i in range(5000,7500)])
        result=reconcile_snapshot(self.final(),self.pg.database,writers_paused=True)
        self.assertEqual(result['changes']['channels'],{'upserted':5500,'deleted':2000})
        self.assertEqual(result['tables']['channels']['rows'],5500)

    def test_swapped_unique_workspace_names_preserve_memberships(self):
        initial=self.snapshot()
        with self.sqlite.connect() as conn:
            conn.execute("INSERT INTO niches(id,name,created_at,updated_at) VALUES(82,'Second',?,?)",(self.fixture_time,self.fixture_time))
            for ident,name in ((200,'Lab Alpha'),(201,'Lab Beta')):
                conn.execute('INSERT INTO labs(id,name,created_at,updated_at) VALUES(?,?,?,?)',(ident,name,self.fixture_time,self.fixture_time))
            conn.execute("INSERT INTO lab_notes(lab_id,text,created_at) VALUES(200,'Keep this note',?)",(self.fixture_time,))
            conn.execute("INSERT INTO lab_videos(lab_id,title,created_at,updated_at) VALUES(200,'Keep this idea',?,?)",(self.fixture_time,self.fixture_time))
        initial=Path(self.tmp.name)/'two-workspaces.sqlite3';snapshot_sqlite(self.sqlite.path,initial)
        migrate_snapshot(initial,self.pg.database)
        with self.sqlite.connect() as conn:
            conn.execute("UPDATE niches SET name='temporary' WHERE id=47")
            conn.execute("UPDATE niches SET name='Saved from YouTube' WHERE id=82")
            conn.execute("UPDATE niches SET name='Second' WHERE id=47")
            conn.execute("UPDATE labs SET name='temporary lab' WHERE id=200")
            conn.execute("UPDATE labs SET name='Lab Alpha' WHERE id=201")
            conn.execute("UPDATE labs SET name='Lab Beta' WHERE id=200")
        result=reconcile_snapshot(self.final(),self.pg.database,writers_paused=True)
        self.assertTrue(result['verified'])
        with self.pg.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT niche_id FROM niche_channels').fetchone()['niche_id'],47)
            self.assertEqual(conn.execute('SELECT name FROM niches WHERE id=47').fetchone()['name'],'Second')
            self.assertEqual(conn.execute('SELECT lab_id FROM lab_notes').fetchone()['lab_id'],200)
            self.assertEqual(conn.execute('SELECT lab_id FROM lab_videos').fetchone()['lab_id'],200)
            timing=conn.execute("SELECT condeferrable,condeferred FROM pg_constraint WHERE contype='f' AND confrelid=%s::regclass",(self.schema+'.labs',)).fetchall()
            self.assertTrue(timing)
            self.assertTrue(all(not row['condeferrable'] and not row['condeferred'] for row in timing))

    def test_active_queue_or_missing_pause_refuses_before_any_changes(self):
        self.initial();source=self.final()
        with self.assertRaisesRegex(MigrationError,'writer pause'):
            reconcile_snapshot(source,self.pg.database)
        from beacon_engine.research.channel_save_jobs import ChannelSaveQueue
        ChannelSaveQueue(self.pg).enqueue('https://www.youtube.com/@sample')
        with self.assertRaisesRegex(MigrationError,'saved-channel requests'):
            reconcile_snapshot(source,self.pg.database,writers_paused=True)

    def test_late_unrepresentable_value_rolls_back_earlier_changes(self):
        self.initial()
        with self.pg.database.connect() as conn:before=fingerprint_postgres(conn,self.schema)
        with self.sqlite.connect() as conn:
            conn.execute("UPDATE channels SET title='valid early edit'")
            conn.execute('UPDATE videos SET title=?',('invalid\0text',))
        with self.assertRaises(MigrationError):
            reconcile_snapshot(self.final(),self.pg.database,writers_paused=True)
        with self.pg.database.connect() as conn:self.assertEqual(fingerprint_postgres(conn,self.schema),before)


if __name__=='__main__':unittest.main()
