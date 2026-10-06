"""Real backpressure prevents a stalled server from queuing a whole table."""
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from beacon_engine.research.postgres import PostgresDatabase
from beacon_engine.research.postgres_copy import bounded_copy_writer


@unittest.skipUnless(os.getenv('BEACON_TEST_POSTGRES_DSN'),
                     'requires an explicit disposable PostgreSQL test database')
class BoundedCopyContracts(unittest.TestCase):
    def setUp(self):
        self.db=PostgresDatabase(os.environ['BEACON_TEST_POSTGRES_DSN'])
        self.name='beacon_copy_pressure_'+uuid4().hex
        self.lock=int(uuid4().hex[:7],16)
        with self.db.connect() as conn:
            conn.execute(f'CREATE TABLE {self.name}(id integer PRIMARY KEY,value text)')
            conn.execute(f'CREATE FUNCTION {self.name}_gate() RETURNS trigger LANGUAGE plpgsql AS '
                         f'$$ BEGIN PERFORM pg_advisory_xact_lock({self.lock}); RETURN NEW; END $$')
            conn.execute(f'CREATE TRIGGER copy_gate BEFORE INSERT ON {self.name} FOR EACH ROW EXECUTE FUNCTION {self.name}_gate()')
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.db.connect() as conn:
            conn.execute(f'DROP TABLE {self.name}')
            conn.execute(f'DROP FUNCTION {self.name}_gate()')

    def test_blocked_backend_applies_backpressure_before_the_table_is_buffered(self):
        payload='café漢字\tnew\nline\\'+('x'*65536)
        produced=[];backend={};started=threading.Event()
        def produce():
            with self.db.connect() as conn:
                backend['pid']=conn.info.backend_pid
                conn.execute('SET LOCAL statement_timeout=0')
                conn.execute('SET LOCAL lock_timeout=0')
                started.set()
                with conn.cursor() as cursor,cursor.copy(f'COPY {self.name} FROM STDIN',writer=bounded_copy_writer(cursor)) as copy:
                    for index in range(1000):
                        copy.write_row((index,payload));produced.append(index)
        with self.db.open() as control,ThreadPoolExecutor(max_workers=1) as pool:
            control.execute('SELECT pg_advisory_lock(%s)',(self.lock,))
            future=pool.submit(produce)
            try:
                self.assertTrue(started.wait(5))
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    row=control.execute('SELECT wait_event FROM pg_stat_activity WHERE pid=%s',(backend['pid'],)).fetchone()
                    if row and row['wait_event']=='advisory':break
                    time.sleep(.01)
                else:self.fail('The controlled backend did not reach its insert gate')
                time.sleep(.3)
                self.assertFalse(future.done())
                self.assertLess(len(produced),128,'The client queued over 8 MiB while the server was blocked')
            finally:control.execute('SELECT pg_advisory_unlock(%s)',(self.lock,))
            future.result(timeout=30)
        self.assertEqual(len(produced),1000)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) AS n FROM {self.name}').fetchone()['n'],1000)
            self.assertEqual(conn.execute(f'SELECT value FROM {self.name} WHERE id=999').fetchone()['value'],payload)

    def test_copy_error_rolls_back_and_connection_remains_reusable(self):
        with self.assertRaisesRegex(RuntimeError,'interrupted'):
            with self.db.connect() as conn:
                with conn.cursor() as cursor,cursor.copy(f'COPY {self.name} FROM STDIN',writer=bounded_copy_writer(cursor)) as copy:
                    copy.write_row((1,'café\t漢字\n\\'))
                    raise RuntimeError('interrupted')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) AS n FROM {self.name}').fetchone()['n'],0)
            with conn.cursor() as cursor,cursor.copy(f'COPY {self.name} FROM STDIN',writer=bounded_copy_writer(cursor)) as copy:
                copy.write_row((2,'recovered'))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute(f'SELECT value FROM {self.name}').fetchone()['value'],'recovered')


if __name__=='__main__':unittest.main()
