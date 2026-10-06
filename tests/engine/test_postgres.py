"""Connection foundation checks; real-server tests require an explicit test DSN.

BEACON_TEST_POSTGRES_DSN must target a disposable database, never the live index.
The integration fixture creates and removes only its own randomly named table.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import monotonic
from uuid import uuid4

from beacon_engine.research.postgres import PostgresDatabase, PostgresUnavailable


class PostgresConfigurationTests(unittest.TestCase):
    def test_connection_string_is_hidden(self):
        db = PostgresDatabase("postgresql://user:private-password@host/database")
        self.assertNotIn("private-password", repr(db))
        self.assertNotIn("postgresql://", repr(db))

    def test_missing_connection_string_is_rejected(self):
        for value in (None, "", " "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PostgresDatabase(value)

    def test_disabled_or_unbounded_timeouts_are_rejected(self):
        for name, bad_values in (
            ("connect_timeout_seconds", [0, -1, 31, True, "5"]),
            ("statement_timeout_ms", [0, -1, 120_001, True, "30000"]),
            ("lock_timeout_ms", [0, -1, 120_001, True, "2000"]),
            ("idle_transaction_timeout_ms", [0, -1, 120_001, True, "30000"]),
        ):
            for value in bad_values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    PostgresDatabase("dbname=test", **{name: value})

    def test_lock_wait_expires_before_statement_deadline(self):
        with self.assertRaises(ValueError):
            PostgresDatabase("dbname=test", lock_timeout_ms=30_000)

    def test_read_only_is_not_a_truthy_string(self):
        with self.assertRaises(ValueError):
            PostgresDatabase("dbname=test", read_only="false")


@unittest.skipUnless(os.getenv("BEACON_TEST_POSTGRES_DSN"),
                     "requires an explicit disposable PostgreSQL test database")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.driver = psycopg
        cls.db = PostgresDatabase(os.environ["BEACON_TEST_POSTGRES_DSN"])
        cls.table = "beacon_connection_test_" + uuid4().hex
        # The generated identifier contains only our fixed prefix and hex digits.
        with cls.db.connect() as conn:
            conn.execute(f"CREATE TABLE {cls.table} (id INTEGER PRIMARY KEY, value TEXT)")

    @classmethod
    def tearDownClass(cls):
        with cls.db.connect() as conn:
            conn.execute(f"DROP TABLE {cls.table}")

    def test_success_commits_and_closes(self):
        with self.db.connect() as conn:
            conn.execute(f"INSERT INTO {self.table} VALUES (%s, %s)", (1, "saved"))
        self.assertTrue(conn.closed)
        with self.db.connect() as reader:
            self.assertEqual(reader.execute(
                f"SELECT value FROM {self.table} WHERE id = %s", (1,)
            ).fetchone(), {"value": "saved"})

    def test_failure_rolls_back_and_closes(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.db.connect() as conn:
                conn.execute(f"INSERT INTO {self.table} VALUES (%s, %s)", (2, "pending"))
                raise RuntimeError("interrupted")
        self.assertTrue(conn.closed)
        with self.db.connect() as reader:
            self.assertIsNone(reader.execute(
                f"SELECT id FROM {self.table} WHERE id = %s", (2,)
            ).fetchone())

    def test_read_only_connection_blocks_writes(self):
        db = replace(self.db, read_only=True)
        with db.connect() as reader:
            self.assertEqual(reader.execute("SHOW transaction_read_only").fetchone(),
                             {"transaction_read_only": "on"})
        with self.assertRaises(self.driver.errors.ReadOnlySqlTransaction):
            with db.connect() as conn:
                conn.execute(f"INSERT INTO {self.table} VALUES (3, 'forbidden')")
        self.assertTrue(conn.closed)

    def test_independent_writes_proceed_while_another_transaction_is_open(self):
        with self.db.connect() as holding:
            holding.execute(f"INSERT INTO {self.table} VALUES (4, 'held')")
            with ThreadPoolExecutor(max_workers=1) as worker:
                def save():
                    with self.db.connect() as writer:
                        writer.execute(f"INSERT INTO {self.table} VALUES (5, 'separate')")
                worker.submit(save).result(timeout=5)
            with self.db.connect() as reader:
                rows = reader.execute(
                    f"SELECT id FROM {self.table} WHERE id IN (4, 5) ORDER BY id"
                ).fetchall()
                self.assertEqual(rows, [{"id": 5}])

    def test_conflicting_write_has_a_bounded_lock_wait(self):
        short_wait = replace(self.db, lock_timeout_ms=150)
        with self.db.connect() as holding:
            holding.execute(f"INSERT INTO {self.table} VALUES (6, 'held')")
            started = monotonic()
            with self.assertRaises(self.driver.errors.LockNotAvailable):
                with short_wait.connect() as writer:
                    writer.execute(f"INSERT INTO {self.table} VALUES (6, 'conflict')")
            self.assertLess(monotonic() - started, 3)
        self.assertTrue(writer.closed)

    def test_slow_statement_is_cancelled(self):
        short_deadline = replace(self.db, statement_timeout_ms=200, lock_timeout_ms=100)
        started = monotonic()
        with self.assertRaises(self.driver.errors.QueryCanceled):
            with short_deadline.connect() as conn:
                conn.execute("SELECT pg_sleep(3)")
        self.assertLess(monotonic() - started, 2)
        self.assertTrue(conn.closed)

    def test_server_settings_apply_to_every_connection(self):
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SHOW statement_timeout").fetchone(),
                             {"statement_timeout": "30s"})
            self.assertEqual(conn.execute("SHOW lock_timeout").fetchone(),
                             {"lock_timeout": "2s"})
            self.assertEqual(conn.execute("SHOW idle_in_transaction_session_timeout").fetchone(),
                             {"idle_in_transaction_session_timeout": "30s"})

    def test_bad_connection_diagnostics_do_not_expose_conninfo(self):
        db = PostgresDatabase("password=private-password invalid_option=bad")
        with self.assertRaises(PostgresUnavailable) as caught:
            with db.connect():
                self.fail("invalid connection unexpectedly succeeded")
        self.assertNotIn("private-password", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
