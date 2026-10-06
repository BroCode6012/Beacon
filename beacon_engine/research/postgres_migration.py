"""Explicit administrator snapshot, migration and recovery tools.

No service calls these automatically. Credentials come from a private environment;
reports contain counts/hashes only. A live SQLite file is never reset or replaced.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import time

from beacon_engine.research.postgres import PostgresDatabase
from beacon_engine.research.postgres_store import PostgresYouTubeStore
from beacon_engine.research.postgres_copy import bounded_copy_writer


class MigrationError(RuntimeError):
    """Public diagnostics never include a DSN or record contents."""


ADMIN_SQLITE_CACHE_KIB = 512 * 1024


def file_hash(path):
    digest = sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(path, report):
    with Path(path).open('x', encoding='utf-8') as output:
        if os.name != 'nt':
            os.chmod(path, 0o600)
        json.dump(report, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def _sqlite_reader(path, *, immutable=False):
    path = Path(path).resolve(strict=True)
    suffix = '&immutable=1' if immutable else ''
    conn = sqlite3.connect(path.as_uri() + '?mode=ro' + suffix, uri=True, timeout=5)
    try:
        conn.execute('PRAGMA query_only=ON')
        # Connection-local, bounded memory for multi-million-row ordered reads
        # and relationship checks. Never persist a setting in the live database.
        conn.execute('PRAGMA cache_size=-' + str(ADMIN_SQLITE_CACHE_KIB))
        yield conn
    finally:
        conn.close()


def snapshot_sqlite(source, destination, *, progress=None, deadline_seconds=7200):
    """Consistent online backup, never a raw copy of a running WAL database."""
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if source == destination or destination.exists() or Path(str(destination) + '.json').exists():
        raise MigrationError('Snapshot destination must be a new file.')
    if shutil.disk_usage(destination.parent).free < source.stat().st_size + 512 * 1024**2:
        raise MigrationError('There is insufficient disk space for the snapshot.')
    started = time.monotonic()
    def copied(status, remaining, total):
        if time.monotonic() - started > deadline_seconds:
            raise MigrationError('Snapshot exceeded its time budget; the source is unchanged.')
        if progress:
            progress({'step': 'snapshot', 'pages_remaining': remaining, 'pages_total': total})
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with _sqlite_reader(source) as original, closing(sqlite3.connect(destination)) as backup:
        original.execute('BEGIN')
        original.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
        original.backup(backup, pages=8192, progress=copied, sleep=.1)
        backup.execute('PRAGMA journal_mode=DELETE')
        backup.execute('PRAGMA cache_size=-' + str(ADMIN_SQLITE_CACHE_KIB))
        if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise MigrationError('Snapshot failed integrity verification; the source is unchanged.')
    report = {'kind': 'beacon-sqlite-snapshot', 'bytes': destination.stat().st_size,
              'sha256': file_hash(destination), 'integrity': 'ok'}
    _write_report(str(destination) + '.json', report)
    return report


def _validated_snapshot(path):
    path = Path(path).resolve(strict=True)
    if any(Path(str(path) + suffix).exists() and Path(str(path) + suffix).stat().st_size
           for suffix in ('-wal', '-journal')):
        raise MigrationError('Snapshot has pending writes and does not match its manifest.')
    try:
        report = json.loads(Path(str(path) + '.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise MigrationError('Migration requires a verified snapshot manifest.') from None
    if (report.get('kind') != 'beacon-sqlite-snapshot' or report.get('integrity') != 'ok'
            or report.get('bytes') != path.stat().st_size or report.get('sha256') != file_hash(path)):
        raise MigrationError('Snapshot does not match its verified manifest.')
    return path


def _metadata(conn, schema):
    rows = conn.execute(
        "SELECT table_name,column_name,data_type,is_identity FROM information_schema.columns "
        "WHERE table_schema=%s ORDER BY table_name,ordinal_position", (schema,),
    ).fetchall()
    tables = {}
    for row in rows:
        tables.setdefault(row['table_name'], []).append(row)
    return tables


def _primary_key(conn, schema, table):
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,pos) "
        "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum "
        "WHERE i.indisprimary AND n.nspname=%s AND c.relname=%s ORDER BY k.pos", (schema, table),
    ).fetchall()
    if not rows:
        raise MigrationError('Every migrated table must have a deterministic primary key.')
    return [row['attname'] for row in rows]


def _ordered_indexes(conn, schema, columns):
    """Administrator-only support for exact, byte-ordered verification reads.

    The default database collation can differ from explicit COLLATE C even when
    today's identifiers are ASCII. Matching indexes avoid large external sorts.
    Invoke only after the empty-target gate; normal service starts never do DDL.
    """
    from psycopg import sql
    for table, meta in columns.items():
        pk = _primary_key(conn, schema, table)
        types = {c['column_name']: c['data_type'] for c in meta}
        if not any(types[c] == 'text' for c in pk):
            continue
        order = [sql.SQL('{} COLLATE "C"').format(sql.Identifier(c)) if types[c] == 'text'
                 else sql.Identifier(c) for c in pk]
        conn.execute(sql.SQL('CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})').format(
            sql.Identifier('beacon_migration_' + table + '_pk_c'),sql.Identifier(schema),
            sql.Identifier(table),sql.SQL(',').join(order)))


def _cell(value, kind):
    if value is None:
        return b'N'
    if kind in ('integer', 'bigint', 'smallint'):
        if not isinstance(value, int):
            raise MigrationError('An integer column contains a value that needs review.')
        return b'I' + str(value).encode('ascii')
    if kind in ('double precision', 'real'):
        number = float(value)
        if not math.isfinite(number):
            raise MigrationError('A numeric column contains a non-finite value.')
        return b'F' + struct.pack('>d', number)
    if kind == 'text' and isinstance(value, str) and '\0' not in value:
        return b'T' + value.encode('utf-8')
    raise MigrationError('A column contains a value that cannot be preserved in PostgreSQL.')


def _digest(rows, columns):
    digest, count = sha256(), 0
    for row in rows:
        count += 1
        values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        for value, column in zip(values, columns):
            cell = _cell(value, column['data_type'])
            digest.update(struct.pack('>Q', len(cell)))
            digest.update(cell)
        digest.update(b'R')
    return {'rows': count, 'sha256': digest.hexdigest()}


def fingerprint_postgres(conn, schema):
    from psycopg import sql
    previous_sort = conn.execute('SHOW enable_sort').fetchone()['enable_sort']
    conn.execute('SET LOCAL enable_sort=off')
    result = {}
    for table, columns in _metadata(conn, schema).items():
        pk = _primary_key(conn, schema, table)
        types = {c['column_name']: c['data_type'] for c in columns}
        order = [sql.SQL('{} COLLATE "C"').format(sql.Identifier(c))
                 if types[c] == 'text' else sql.Identifier(c) for c in pk]
        query = sql.SQL('SELECT {} FROM {}.{} ORDER BY {}').format(
            sql.SQL(',').join(sql.Identifier(c['column_name']) for c in columns),
            sql.Identifier(schema), sql.Identifier(table), sql.SQL(',').join(order),
        )
        with conn.cursor(name='fingerprint_' + table) as cursor:
            cursor.itersize = 2000
            cursor.execute(query)
            result[table] = _digest(cursor, columns)
    # On an error the enclosing transaction rolls back, restoring the setting.
    # On success leave callers' query-planning preferences exactly as found.
    conn.execute('SET LOCAL enable_sort=' + ('on' if previous_sort == 'on' else 'off'))
    return result


def normalized_migration_row(table, names, pk, row, audit):
    from beacon_engine.research.postgres_text import normalize_snapshot_row
    try:
        return normalize_snapshot_row(table, names, pk, row, audit)
    except ValueError:
        raise MigrationError('Description normalization exceeds the bounded review limit.') from None


def migrate_snapshot(source, database, *, progress=None, normalize_provider_descriptions=False):
    """Copy a checked snapshot into a provisioned, entirely empty target schema."""
    from psycopg import sql
    if type(normalize_provider_descriptions) is not bool:
        raise MigrationError('Description normalization requires an explicit boolean setting.')
    normalizations = []
    source = _validated_snapshot(source)
    with _sqlite_reader(source, immutable=True) as old, database.connect() as new:
        new.execute('SET LOCAL statement_timeout=0')
        new.execute('SET LOCAL idle_in_transaction_session_timeout=0')
        new.execute('SELECT pg_advisory_xact_lock(726493184)')
        columns = _metadata(new, database.schema)
        expected = set(columns) - {'beacon_schema_meta', 'channel_save_requests'}
        actual = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual != expected:
            raise MigrationError('Source and target table inventories differ; review the schema first.')
        for table in columns:
            if table != 'beacon_schema_meta' and new.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM {}.{}) AS populated').format(
                    sql.Identifier(database.schema), sql.Identifier(table))).fetchone()['populated']:
                raise MigrationError('Migration refuses a populated target schema.')
        if old.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationError('Source contains broken relationships; review them before migration.')
        _ordered_indexes(new, database.schema, columns)
        # Parent tables precede their children in the explicit research schema.
        ddl = Path(__file__).with_name('postgres_schema.sql').read_text(encoding='utf-8')
        ordered = re.findall(r'CREATE TABLE IF NOT EXISTS ([a-z_]+)', ddl)
        sequences = dict(old.execute('SELECT name,seq FROM sqlite_sequence'))
        report = {}
        for table in ordered:
            if table not in expected:
                continue
            target_columns = columns[table]
            names = [c['column_name'] for c in target_columns]
            original = [r[1] for r in old.execute('PRAGMA table_info("' + table + '")')]
            if set(original) != set(names):
                raise MigrationError('Source and target column inventories differ.')
            pk = _primary_key(new, database.schema, table)
            select = 'SELECT ' + ','.join('"' + c + '"' for c in names) + ' FROM "' + table + '" ORDER BY ' + ','.join('"' + c + '" COLLATE BINARY' for c in pk)
            query = sql.SQL('COPY {}.{} ({}) FROM STDIN').format(sql.Identifier(database.schema),
                sql.Identifier(table), sql.SQL(',').join(sql.Identifier(c) for c in names))
            source_digest, count = sha256(), 0
            with new.cursor() as cursor, cursor.copy(query, writer=bounded_copy_writer(cursor)) as copy:
                for row in old.execute(select):
                    if normalize_provider_descriptions:
                        row = normalized_migration_row(table, names, pk, row, normalizations)
                    for value, col in zip(row, target_columns):
                        cell = _cell(value, col['data_type'])
                        source_digest.update(struct.pack('>Q', len(cell)))
                        source_digest.update(cell)
                    source_digest.update(b'R')
                    copy.write_row(row)
                    count += 1
                    if progress and count % 100000 == 0:
                        progress({'step': 'copy', 'table': table, 'rows': count})
            report[table] = {'rows': count, 'sha256': source_digest.hexdigest()}
            for col in target_columns:
                if col['is_identity'] == 'YES':
                    maximum = new.execute(sql.SQL('SELECT max({}) AS maximum FROM {}.{}').format(
                        sql.Identifier(col['column_name']), sql.Identifier(database.schema), sql.Identifier(table))).fetchone()['maximum']
                    high = max(maximum or 0, sequences.get(table, 0))
                    new.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)',
                                (database.schema + '.' + table, col['column_name'], max(high, 1), high > 0))
            if progress:
                progress({'step': 'copied', 'table': table, 'rows': count})
        verified = fingerprint_postgres(new, database.schema)
        if any(report[table] != verified[table] for table in expected):
            raise MigrationError('Transferred records differ from the snapshot; migration rolled back.')
        for table in expected:
            new.execute(sql.SQL('ANALYZE {}.{}').format(sql.Identifier(database.schema), sql.Identifier(table)))
    return {'kind': 'beacon-postgres-migration', 'schema': database.schema, 'tables': report,
            'source_sha256': file_hash(source), 'verified': True,
            'description_normalizations': normalizations}


def _native_environment(database):
    from psycopg.conninfo import conninfo_to_dict
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('PG'):
            env.pop(key)
    env['PGCONNECT_TIMEOUT'] = str(database.connect_timeout_seconds)
    env['PGAPPNAME'] = 'beacon-administrator-backup'
    env['PGOPTIONS'] = '-c statement_timeout=0 -c lock_timeout=2000'
    params = conninfo_to_dict(database.dsn)
    mapping = {'host': 'PGHOST', 'hostaddr': 'PGHOSTADDR', 'port': 'PGPORT',
               'dbname': 'PGDATABASE', 'user': 'PGUSER', 'password': 'PGPASSWORD',
               'passfile': 'PGPASSFILE', 'service': 'PGSERVICE', 'sslmode': 'PGSSLMODE',
               'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY', 'sslrootcert': 'PGSSLROOTCERT',
               'sslcrl': 'PGSSLCRL', 'connect_timeout': 'PGCONNECT_TIMEOUT',
               'application_name': 'PGAPPNAME', 'options': 'PGOPTIONS',
               'target_session_attrs': 'PGTARGETSESSIONATTRS', 'channel_binding': 'PGCHANNELBINDING'}
    for key, value in params.items():
        if key not in mapping:
            raise MigrationError('Native backup connection contains an unsupported option.')
        env[mapping[key]] = value
    name = params.get('dbname') or params.get('user') or ''
    if not name:
        raise MigrationError('Native backup requires an explicit database name.')
    env['PGDATABASE'] = name
    return env, name


def backup_postgres(database, destination, *, bin_directory=''):
    destination = Path(destination).resolve()
    partial = Path(str(destination) + '.partial')
    if destination.exists() or partial.exists() or Path(str(destination) + '.json').exists():
        raise MigrationError('Backup destination must be a new file.')
    env, _ = _native_environment(database)
    binary = str(Path(bin_directory) / 'pg_dump') if bin_directory else 'pg_dump'
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output, database.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute('SET LOCAL statement_timeout=0')
        conn.execute('SET LOCAL idle_in_transaction_session_timeout=0')
        snapshot = conn.execute('SELECT pg_export_snapshot() AS snapshot').fetchone()['snapshot']
        tables = fingerprint_postgres(conn, database.schema)
        proc = subprocess.run([binary, '-Fc', '--no-owner', '--no-privileges',
            '--schema=' + database.schema, '--snapshot=' + snapshot], env=env,
            stdout=output, stderr=subprocess.PIPE, timeout=21600)
        if proc.returncode:
            raise MigrationError('Native PostgreSQL backup failed; no backup was marked complete.')
        output.flush()
        os.fsync(output.fileno())
    partial.rename(destination)
    report = {'kind': 'beacon-postgres-backup', 'schema': database.schema,
              'bytes': destination.stat().st_size, 'sha256': file_hash(destination), 'tables': tables}
    _write_report(str(destination) + '.json', report)
    return report


def restore_postgres(database, archive, *, bin_directory=''):
    """Recovery targets must be empty databases, never an in-place overwrite."""
    archive = Path(archive).resolve(strict=True)
    report = json.loads(Path(str(archive) + '.json').read_text(encoding='utf-8'))
    if (report.get('kind') != 'beacon-postgres-backup' or report.get('schema') != database.schema
            or report.get('sha256') != file_hash(archive) or report.get('bytes') != archive.stat().st_size):
        raise MigrationError('Backup does not match its manifest or selected schema.')
    with database.connect() as conn:
        exists = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%%') AS populated").fetchone()['populated']
        if exists:
            raise MigrationError('Recovery refuses a populated target database.')
    env, name = _native_environment(database)
    if not name or name.startswith('postgresql:'):
        raise MigrationError('Recovery requires an explicit target database name.')
    binary = str(Path(bin_directory) / 'pg_restore') if bin_directory else 'pg_restore'
    # -d must be present to execute a restore. The alias is non-secret; full DSN is env-only.
    proc = subprocess.run([binary, '--no-owner', '--no-privileges', '--exit-on-error',
        '--single-transaction', '--dbname=' + name, str(archive)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=21600)
    if proc.returncode:
        raise MigrationError('Native PostgreSQL recovery failed; target is not approved for use.')
    with database.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute('SET LOCAL statement_timeout=0')
        conn.execute('SET LOCAL idle_in_transaction_session_timeout=0')
        if fingerprint_postgres(conn, database.schema) != report['tables']:
            raise MigrationError('Recovered records differ from the backup manifest.')
    return {'kind': 'beacon-postgres-recovery', 'verified': True, 'tables': report['tables']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'initialize', 'migrate', 'backup', 'restore'))
    parser.add_argument('--source')
    parser.add_argument('--destination')
    parser.add_argument('--schema', default='beacon_index')
    parser.add_argument('--bin-directory', default='')
    parser.add_argument('--normalize-provider-descriptions', action='store_true')
    args = parser.parse_args()
    try:
        if args.normalize_provider_descriptions and (args.action != 'migrate' or not args.destination
                or Path(args.destination).exists()):
            raise MigrationError('Description normalization requires a new private migration report destination.')
        if args.action == 'snapshot':
            result = snapshot_sqlite(args.source, args.destination)
        else:
            database = PostgresDatabase(os.getenv('BEACON_POSTGRES_ADMIN_DSN', ''), schema=args.schema)
            if args.action == 'initialize':
                from psycopg import sql
                with database.connect() as conn:
                    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(args.schema)))
                PostgresYouTubeStore(dsn=database.dsn, schema=args.schema, initialize=True)
                result = {'schema': args.schema, 'initialized': True}
            elif args.action == 'migrate':
                result = migrate_snapshot(args.source, database,
                    normalize_provider_descriptions=args.normalize_provider_descriptions)
                if args.normalize_provider_descriptions:
                    _write_report(args.destination, result)
                    result = {'verified': True, 'tables': len(result['tables']),
                              'description_normalizations': len(result['description_normalizations'])}
            elif args.action == 'backup':
                result = backup_postgres(database, args.destination, bin_directory=args.bin_directory)
            else:
                result = restore_postgres(database, args.source, bin_directory=args.bin_directory)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, MigrationError)
                          else 'Administrator operation failed: ' + type(exc).__name__}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
