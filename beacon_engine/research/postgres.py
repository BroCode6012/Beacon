"""Explicit PostgreSQL connections for the isolated migration rehearsal.

Callers must opt in explicitly. SQLite session databases keep their existing
behavior. Database credentials are separate from the public index identifier.
SQL here uses native Psycopg parameters (%s); no SQLite SQL is rewritten.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import re
from typing import Iterator, Any


class PostgresUnavailable(RuntimeError):
    """A connection failure whose public message contains no credentials."""


@dataclass(frozen=True)
class PostgresDatabase:
    # DSNs may contain passwords. Keep them out of object reprs and diagnostics.
    dsn: str = field(repr=False)
    read_only: bool = False
    schema: str = "public"
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 2_000
    idle_transaction_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not isinstance(self.dsn, str) or not self.dsn.strip():
            raise ValueError("A PostgreSQL connection string is required")
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if not isinstance(self.schema, str) or not re.fullmatch(r"[a-z_][a-z_0-9]{0,62}", self.schema):
            raise ValueError("schema must be a simple lowercase PostgreSQL identifier")
        for name, maximum in (
            ("connect_timeout_seconds", 30),
            ("statement_timeout_ms", 120_000),
            ("lock_timeout_ms", 120_000),
            ("idle_transaction_timeout_ms", 120_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer from 1 to {maximum}")
        if self.lock_timeout_ms >= self.statement_timeout_ms:
            raise ValueError("lock_timeout_ms must be less than statement_timeout_ms")

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """One bounded transaction, committed on success, rolled back on failure.

        Connections always close on exit. Read-only transactions are enforced by
        PostgreSQL; production deployment must ALSO use a restricted database
        role and retain Finder token authorization. This is not a role grant.
        """
        with self.open() as conn:
            yield conn

    def open(self) -> Any:
        """Open a connection for a caller that explicitly owns its lifetime."""
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise PostgresUnavailable(
                "PostgreSQL support is not installed; use requirements-postgres.txt"
            ) from None

        # These settings apply only to our connections, not to other applications
        # on the server. Validated integers cannot inject startup options.
        options = (
            f"-c statement_timeout={self.statement_timeout_ms} "
            f"-c lock_timeout={self.lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={self.idle_transaction_timeout_ms} "
            f"-c TimeZone=UTC -c search_path={self.schema},pg_catalog"
        )
        try:
            conn = psycopg.connect(
                self.dsn, connect_timeout=self.connect_timeout_seconds,
                application_name="beacon-postgres-rehearsal", options=options,
                row_factory=dict_row, autocommit=False,
            )
        except (psycopg.OperationalError, psycopg.ProgrammingError):
            # Driver diagnostics can include hostnames, usernames or passwords
            # from malformed conninfo. Do not propagate them to the UI or logs.
            raise PostgresUnavailable(
                "Could not connect to PostgreSQL; check its configuration and availability"
            ) from None
        try:
            conn.read_only = self.read_only
        except BaseException:
            conn.close()
            raise
        return conn
