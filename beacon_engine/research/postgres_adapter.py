"""DB-API result and parameter contracts for the explicitly ported index SQL.

Only bind markers are converted here. Statements, functions, conflicts, schema
and transaction commands must already be valid PostgreSQL; SQLite-specific SQL
is intentionally not translated by this adapter.
"""

from __future__ import annotations

import re
from decimal import Decimal


def bind_sql(statement: str) -> str:
    """Convert qmark bind markers, preserving literals, identifiers and comments."""
    out = []
    i = 0
    state = ""
    dollar = ""
    while i < len(statement):
        ch = statement[i]
        pair = statement[i:i + 2]
        if state in {"'", '"'}:
            out.append("%%" if ch == "%" else ch)
            if ch == state:
                if i + 1 < len(statement) and statement[i + 1] == state:
                    out.append(state)
                    i += 1
                else:
                    state = ""
        elif state == "--":
            out.append("%%" if ch == "%" else ch)
            if ch == "\n":
                state = ""
        elif state == "/*":
            out.append("%%" if ch == "%" else ch)
            if pair == "*/":
                out.append("/")
                i += 1
                state = ""
        elif state == "$":
            if statement.startswith(dollar, i):
                out.append(dollar)
                i += len(dollar) - 1
                state = ""
            else:
                out.append("%%" if ch == "%" else ch)
        elif pair in {"--", "/*"}:
            state = pair
            out.append(pair)
            i += 1
        elif ch in {"'", '"'}:
            state = ch
            out.append(ch)
        elif ch == "$" and (match := re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", statement[i:])):
            dollar = match.group(0)
            state = "$"
            out.append(dollar)
            i += len(dollar) - 1
        elif ch == "?":
            out.append("%s")
        else:
            out.append("%%" if ch == "%" else ch)
        i += 1
    return "".join(out)


class IndexRow:
    """Named access/dict conversion plus SQLite's positional access/unpacking."""

    def __init__(self, pairs=()):
        pairs = tuple(pairs)
        self._keys = tuple(key for key, _ in pairs)
        self._values = tuple(value for _, value in pairs)
        self._index = {}
        for index, key in enumerate(self._keys):
            self._index.setdefault(key, index)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def keys(self):
        return self._keys

    def values(self):
        return self._values

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return repr(dict(self))


def index_row_factory(cursor):
    if cursor.description is None:
        return lambda values: IndexRow()
    names = [column.name for column in cursor.description]

    def make_row(values):
        # PostgreSQL SUM(bigint) returns Decimal; the index's JSON contract uses
        # integers for whole counters and floats for fractional metrics.
        def normalize(value):
            if isinstance(value, Decimal):
                return int(value) if value == value.to_integral_value() else float(value)
            return value
        return IndexRow(zip(names, map(normalize, values)))
    return make_row


class IndexCursor:
    def __init__(self, cursor, *, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)


class PostgresIndexConnection:
    """Connection owned by one store operation or one durable importer session."""

    dialect = "postgresql"

    def __init__(self, connection):
        self.raw = connection
        self.raw.row_factory = index_row_factory
        self.total_changes = 0

    def execute(self, statement, parameters=None):
        sql = bind_sql(statement) if parameters is not None else statement
        cursor = self.raw.execute(sql, parameters)
        if re.match(r"\s*(INSERT|UPDATE|DELETE)\b", statement, re.I):
            self.total_changes += max(cursor.rowcount, 0)
        return IndexCursor(cursor)

    def executemany(self, statement, parameters):
        cursor = self.raw.cursor()
        cursor.executemany(bind_sql(statement), parameters)
        self.total_changes += max(cursor.rowcount, 0)
        return IndexCursor(cursor)

    def insert_id(self, statement, parameters):
        """Explicit RETURNING for inserts that used SQLite lastrowid."""
        cursor = self.execute(statement.rstrip().rstrip(";") + " RETURNING id", parameters)
        row = cursor.fetchone()
        return int(row[0]) if row else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()

    def __getattr__(self, name):
        return getattr(self.raw, name)
