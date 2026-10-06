"""Stream a consistent logical SQLite snapshot directly to an off-host backup.

The authenticated transport is supplied by the administrator. Records are framed
JSON, not executable INSERT statements. Reports contain hashes/counts only.
"""
from contextlib import closing
from hashlib import sha256
import gzip
import json
import os
from pathlib import Path
import sqlite3
import struct

from beacon_engine.research.postgres_migration import (
    MigrationError, _sqlite_reader, _write_report, file_hash, ADMIN_SQLITE_CACHE_KIB,
)

MAX_FRAME = 32 * 1024 * 1024


def _frame(output, payload, digest):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_FRAME:
        raise MigrationError('A snapshot record exceeds the transfer limit.')
    packet = struct.pack('>I', len(encoded)) + encoded
    output.write(packet)
    if digest is not None:
        digest.update(packet)


def export_snapshot(source, output, *, progress=None, ordered=False):
    """A pinned WAL read transaction keeps every table at the same point in time."""
    digest, tables = sha256(), {}
    with _sqlite_reader(source) as conn, gzip.GzipFile(fileobj=output, mode='wb', compresslevel=3) as stream:
        conn.execute('BEGIN')
        objects = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type DESC,name").fetchall()
        if any(kind not in ('table', 'index') for kind, *_ in objects):
            raise MigrationError('Snapshot contains unsupported schema objects; review them first.')
        header = {'kind': 'beacon-snapshot-stream-v1', 'objects': objects}
        keys = {}
        if ordered:
            pending = {item[1]: item for item in objects if item[0] == 'table'}
            dependencies = {name: {row[2] for row in conn.execute(
                'PRAGMA foreign_key_list("' + name.replace('"', '""') + '")')}
                for name in pending}
            ordered_objects = []
            completed = set()
            while pending:
                ready = sorted(name for name in pending if dependencies[name] <= completed)
                if not ready:
                    raise MigrationError('Ordered snapshots require resolvable table dependencies.')
                for name in ready:
                    ordered_objects.append(pending.pop(name))
                    completed.add(name)
            objects = ordered_objects + [item for item in objects if item[0] == 'index']
            header['objects'] = objects
            for kind, name, _, _ in objects:
                if kind == 'table':
                    meta = conn.execute('PRAGMA table_info("' + name.replace('"', '""') + '")').fetchall()
                    keys[name] = [row[1] for row in sorted(meta, key=lambda r: r[5]) if row[5]]
                    if not keys[name]:
                        raise MigrationError('Ordered snapshots require primary keys on every table.')
            header['primary_keys'] = keys
        _frame(stream, header, digest)
        sequences = conn.execute("SELECT name FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
        highwater = dict(conn.execute('SELECT name,seq FROM sqlite_sequence')) if sequences else {}
        for kind, name, _, ddl in objects:
            if kind != 'table':
                continue
            columns = [row[1] for row in conn.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')]
            _frame(stream, {'table': name, 'columns': columns}, digest)
            rows, batch, bytes_in_batch = 0, [], 0
            query = 'SELECT * FROM "' + name.replace('"', '""') + '"'
            if ordered:
                query += ' ORDER BY ' + ','.join('"' + key.replace('"', '""') + '" COLLATE BINARY' for key in keys[name])
            for row in conn.execute(query):
                # Blob values need an explicit format, rather than lossy conversion.
                if any(isinstance(value, bytes) for value in row):
                    raise MigrationError('Snapshot contains a binary record that needs review.')
                batch.append(tuple(row))
                rows += 1
                bytes_in_batch += sum(len(value.encode('utf-8')) if isinstance(value, str) else 16 for value in row)
                if len(batch) >= 2000 or bytes_in_batch >= 512 * 1024:
                    _frame(stream, {'rows': batch}, digest)
                    batch, bytes_in_batch = [], 0
                if progress and rows % 100000 == 0:
                    progress({'table': name, 'rows': rows})
            if batch:
                _frame(stream, {'rows': batch}, digest)
            tables[name] = rows
            _frame(stream, {'end_table': name, 'count': rows}, digest)
            if progress:
                progress({'table': name, 'rows': rows, 'complete': True})
        _frame(stream, {'complete': True, 'sha256': digest.hexdigest(),
                        'tables': tables, 'sequences': highwater}, None)
    return {'tables': tables}


def _read_exact(stream, size):
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise MigrationError('Snapshot transfer ended before completion.')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def receive_snapshot(source, destination, *, progress=None):
    destination = Path(destination).resolve()
    if destination.exists() or Path(str(destination) + '.json').exists():
        raise MigrationError('Snapshot destination must be a new file.')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    digest, counts, active, columns, table_rows = sha256(), {}, None, [], 0
    with gzip.GzipFile(fileobj=source, mode='rb') as stream, closing(sqlite3.connect(destination)) as conn:
        # Only this new, incomplete backup is built without journaling. No live file
        # is ever opened for writes. A success manifest appears only after verification.
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA temp_store=FILE')
        objects = None
        while True:
            header = _read_exact(stream, 4)
            size = struct.unpack('>I', header)[0]
            if not 1 <= size <= MAX_FRAME:
                raise MigrationError('Snapshot frame length is invalid.')
            encoded = _read_exact(stream, size)
            payload = json.loads(encoded)
            if payload.get('complete'):
                if active is not None or payload.get('sha256') != digest.hexdigest() or payload.get('tables') != counts:
                    raise MigrationError('Snapshot stream failed its content check.')
                if stream.read(1):
                    raise MigrationError('Snapshot contains unexpected trailing records.')
                break
            digest.update(header + encoded)
            if objects is None:
                if payload.get('kind') != 'beacon-snapshot-stream-v1':
                    raise MigrationError('Unsupported snapshot stream.')
                objects = payload['objects']
                for kind, name, _, ddl in objects:
                    if kind == 'table':
                        # The source DDL is local application schema, received over the
                        # administrator's authenticated transport, never provider input.
                        conn.execute(ddl)
                    elif kind != 'index':
                        raise MigrationError('Unsupported snapshot schema object.')
            elif 'table' in payload:
                if active is not None or payload['table'] in counts:
                    raise MigrationError('Snapshot tables are duplicated or incomplete.')
                active, columns, table_rows = payload['table'], payload['columns'], 0
                actual = [r[1] for r in conn.execute('PRAGMA table_info("' + active.replace('"', '""') + '")')]
                if columns != actual:
                    raise MigrationError('Snapshot column inventory differs.')
            elif 'rows' in payload:
                if active is None:
                    raise MigrationError('Snapshot records have no active table.')
                quoted = '"' + active.replace('"', '""') + '"'
                conn.executemany('INSERT INTO ' + quoted + ' VALUES(' + ','.join('?' for _ in columns) + ')', payload['rows'])
                table_rows += len(payload['rows'])
                conn.commit()
                if progress:
                    progress({'table': active, 'rows': table_rows})
            elif 'end_table' in payload:
                if payload['end_table'] != active or payload['count'] != table_rows:
                    raise MigrationError('Snapshot table failed its row-count check.')
                counts[active], active = table_rows, None
            else:
                raise MigrationError('Snapshot contains an unknown record.')
        if set(counts) != {name for kind, name, _, _ in objects if kind == 'table'}:
            raise MigrationError('Snapshot table inventory is incomplete.')
        for kind, _, _, ddl in objects:
            if kind == 'index':
                conn.execute(ddl)
        if payload['sequences']:
            conn.execute('DELETE FROM sqlite_sequence')
            conn.executemany('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', payload['sequences'].items())
        conn.commit()
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA cache_size=-' + str(ADMIN_SQLITE_CACHE_KIB))
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise MigrationError('Received snapshot failed its integrity check.')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationError('Received snapshot contains broken relationships.')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    # Closing all connections and fsyncing precede the manifest that declares success.
    with destination.open('r+b') as file:
        os.fsync(file.fileno())
    report = {'kind': 'beacon-sqlite-snapshot', 'bytes': destination.stat().st_size,
              'sha256': file_hash(destination), 'integrity': 'ok', 'tables': counts,
              'stream_sha256': payload['sha256']}
    _write_report(str(destination) + '.json', report)
    return report
