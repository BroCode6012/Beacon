"""PostgreSQL implementation of the shared research-index store.

Schema provisioning is explicit. Ordinary service starts cannot perform DDL.
Credentials never appear in the public target, cache identity or file paths.
"""

from __future__ import annotations

import os
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from beacon_engine.research.postgres import PostgresDatabase, PostgresUnavailable
from beacon_engine.research.postgres_adapter import PostgresIndexConnection
from beacon_engine.research.store import YouTubeStore, _json
from beacon_engine.research.postgres_text import normalize_description

POSTGRES_INDEX_TARGET = "postgresql:beacon-index"
POSTGRES_SCHEMA_VERSION = 6


class PostgresYouTubeStore(YouTubeStore):
    backend = "postgresql"

    def __init__(self, path=None, *, read_only=False, dsn=None, initialize=False, schema=None):
        self.path = POSTGRES_INDEX_TARGET
        self.read_only = read_only
        configured = dsn or (
            os.getenv("BEACON_POSTGRES_READ_DSN") if read_only else None
        ) or os.getenv("BEACON_POSTGRES_DSN")
        if not configured:
            raise PostgresUnavailable("PostgreSQL index connection is not configured")
        self.database = PostgresDatabase(
            configured, read_only=read_only,
            schema=schema or os.getenv("BEACON_POSTGRES_SCHEMA") or "public",
        )
        if initialize:
            if read_only:
                raise ValueError("A read-only store cannot initialize the schema")
            self.initialize_schema()

    @property
    def state_directory(self):
        return Path(os.getenv("BEACON_INDEX_STATE_DIR") or "storage")

    @property
    def cache_identity(self):
        identity = self.database.dsn + "\0" + self.database.schema
        return "postgresql:" + sha256(identity.encode("utf-8")).hexdigest()

    def connect(self):
        return PostgresIndexConnection(self.database.open())

    def upsert_channel(self, channel):
        description, raw = normalize_description(channel.description, _json(channel.raw))
        if description != channel.description:
            channel = replace(channel, description=description, raw=json.loads(raw))
        return super().upsert_channel(channel)

    def upsert_video(self, video, *, snapshot=True):
        description, raw = normalize_description(video.description, _json(video.raw))
        if description != video.description:
            video = replace(video, description=description, raw=json.loads(raw))
        return super().upsert_video(video, snapshot=snapshot)

    def initialize_schema(self):
        """Run versioned DDL using an explicit schema-owner connection."""
        schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        with self.database.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(726493182)")
            conn.execute(schema, prepare=False)

    def insert_id(self, conn, statement, parameters):
        return conn.insert_id(statement, parameters)

    def casefold(self, expression):
        return f"beacon_nocase({expression})"

    def saved_view_conflict(self):
        return "niche_id, (beacon_nocase(name))"

    def _init(self):
        # Base SQLite migrations must never run against this backend.
        raise RuntimeError("Use explicit PostgreSQL schema initialization")
