"""Administrator migration from a sealed, ordered SQLite record archive.

Run the native import on the database host. Moving a checked compressed package
does not leave an hours-long COPY transaction dependent on the operator's network.
The original recovery SQLite file and all stored cells remain preserved.
"""
import argparse
from hashlib import sha256
import gzip
import json
import os
from pathlib import Path
import struct

from beacon_engine.research.postgres import PostgresDatabase
from beacon_engine.research.postgres_copy import bounded_copy_writer
from beacon_engine.research.postgres_migration import (
    MigrationError, _validated_snapshot, _write_report, file_hash, _metadata,
    _primary_key, _ordered_indexes, _cell, fingerprint_postgres, normalized_migration_row,
)
from beacon_engine.research.sqlite_snapshot_stream import export_snapshot, _read_exact, MAX_FRAME


def build_archive(snapshot, destination, *, progress=None):
    snapshot = _validated_snapshot(snapshot)
    source = json.loads(Path(str(snapshot) + '.json').read_text(encoding='utf-8'))
    destination = Path(destination).resolve()
    partial = Path(str(destination) + '.partial')
    if any(path.exists() for path in (destination, partial, Path(str(destination) + '.json'))):
        raise MigrationError('Archive destination must be a new file.')
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        exported = export_snapshot(snapshot, output, progress=progress, ordered=True)
        output.flush(); os.fsync(output.fileno())
    if file_hash(snapshot) != source['sha256'] or (
            source.get('tables') is not None and exported['tables'] != source['tables']):
        raise MigrationError('The recovery snapshot changed while building its archive.')
    partial.rename(destination)
    report = {'kind':'beacon-sqlite-ordered-archive', 'source_sha256':source['sha256'],
              'sha256':file_hash(destination), 'bytes':destination.stat().st_size,
              'tables':exported['tables']}
    _write_report(str(destination) + '.json', report)
    return report


def _archive_manifest(path):
    path = Path(path).resolve(strict=True)
    try:
        report = json.loads(Path(str(path) + '.json').read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise MigrationError('Migration requires a sealed ordered archive manifest.') from None
    if (report.get('kind') != 'beacon-sqlite-ordered-archive'
            or report.get('bytes') != path.stat().st_size
            or report.get('sha256') != file_hash(path)
            or not isinstance(report.get('tables'), dict)
            or not isinstance(report.get('source_sha256'), str)
            or len(report['source_sha256']) != 64):
        raise MigrationError('Archive does not match its verified manifest.')
    return path, report


def _payload(stream, digest):
    header = _read_exact(stream, 4)
    size = struct.unpack('>I', header)[0]
    if not 1 <= size <= MAX_FRAME:
        raise MigrationError('Snapshot frame length is invalid.')
    body = _read_exact(stream, size)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise MigrationError('Snapshot frame is not valid JSON.') from None
    if not isinstance(value,dict):
        raise MigrationError('Snapshot frame must be an object.')
    if not value.get('complete'):
        digest.update(header + body)
    return value


def migrate_archive(path, database, *, progress=None, normalize_provider_descriptions=False):
    """Whole-copy rollback and exact typed hashes, using no full SQLite host copy."""
    from psycopg import sql
    if type(normalize_provider_descriptions) is not bool:
        raise MigrationError('Description normalization requires an explicit boolean setting.')
    path, manifest = _archive_manifest(path)
    report, audits = {}, []
    with path.open('rb') as source, gzip.GzipFile(fileobj=source, mode='rb') as stream, database.connect() as conn:
        conn.execute('SET LOCAL statement_timeout=0')
        conn.execute('SET LOCAL idle_in_transaction_session_timeout=0')
        conn.execute('SELECT pg_advisory_xact_lock(726493183)')
        meta = _metadata(conn, database.schema)
        expected = set(meta) - {'beacon_schema_meta','channel_save_requests'}
        if len(expected) != 30 or expected != set(manifest['tables']):
            raise MigrationError('Source and target table inventories differ.')
        for table in expected | {'channel_save_requests'}:
            if conn.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM {}.{}) AS populated').format(
                    sql.Identifier(database.schema),sql.Identifier(table))).fetchone()['populated']:
                raise MigrationError('Migration refuses a populated target table.')
        _ordered_indexes(conn,database.schema,meta)
        conn.execute('SET CONSTRAINTS ALL DEFERRED')
        digest = sha256()
        header = _payload(stream,digest)
        if header.get('kind') != 'beacon-snapshot-stream-v1':
            raise MigrationError('Unsupported snapshot archive.')
        keys = header.get('primary_keys',{})
        if set(keys) != expected:
            raise MigrationError('Archive must declare ordered primary keys for every table.')
        objects = header.get('objects',[])
        tables = [item[1] for item in objects if item[0] == 'table']
        if len(tables) != len(expected) or set(tables) != expected or any(
                item[0] not in ('table','index') for item in objects):
            raise MigrationError('Archive schema inventory differs.')
        while True:
            begin = _payload(stream,digest)
            if begin.get('complete'):
                footer = begin
                break
            table, names = begin.get('table'), begin.get('columns')
            if table not in expected or table in report or not isinstance(names,list):
                raise MigrationError('Archive table is duplicated or unsupported.')
            target_names = [column['column_name'] for column in meta[table]]
            pk = _primary_key(conn,database.schema,table)
            if len(names) != len(target_names) or set(names) != set(target_names) or keys[table] != pk:
                raise MigrationError('Archive columns or primary key differ from the target.')
            positions = [names.index(name) for name in target_names]
            key_positions = [target_names.index(name) for name in pk]
            key_types = [meta[table][index]['data_type'] for index in key_positions]
            query = sql.SQL('COPY {}.{} ({}) FROM STDIN').format(sql.Identifier(database.schema),
                sql.Identifier(table),sql.SQL(',').join(sql.Identifier(name) for name in target_names))
            typed, count, previous = sha256(), 0, None
            with conn.cursor() as cursor, cursor.copy(query, writer=bounded_copy_writer(cursor)) as copy:
                while True:
                    part = _payload(stream,digest)
                    if 'end_table' in part:
                        if part['end_table'] != table or part.get('count') != count or manifest['tables'][table] != count:
                            raise MigrationError('Archive table failed its row-count check.')
                        break
                    rows = part.get('rows')
                    if not isinstance(rows,list):
                        raise MigrationError('Archive table has an invalid record frame.')
                    for record in rows:
                        if not isinstance(record,list) or len(record) != len(names):
                            raise MigrationError('Archive record has an invalid column count.')
                        row = tuple(record[index] for index in positions)
                        if normalize_provider_descriptions:
                            row = normalized_migration_row(table,target_names,pk,row,audits)
                        cells = [_cell(value,column['data_type']) for value,column in zip(row,meta[table])]
                        if any(row[index] is None for index in key_positions):
                            raise MigrationError('Archive primary key contains an unsupported null value.')
                        order = tuple(row[index].encode('utf-8') if kind == 'text' else row[index]
                                      for index,kind in zip(key_positions,key_types))
                        if previous is not None and order <= previous:
                            raise MigrationError('Archive records are not in strict primary-key order.')
                        previous = order
                        for cell in cells:
                            typed.update(struct.pack('>Q',len(cell))); typed.update(cell)
                        typed.update(b'R'); copy.write_row(row); count += 1
                        if progress and count % 100000 == 0:
                            progress({'step':'copy','table':table,'rows':count})
            report[table] = {'rows':count,'sha256':typed.hexdigest()}
            if progress:progress({'step':'copied','table':table,'rows':count})
        if (set(report) != expected or footer.get('sha256') != digest.hexdigest()
                or footer.get('tables') != manifest['tables'] or stream.read(1)):
            raise MigrationError('Archive failed its complete content check.')
        sequences = footer.get('sequences',{})
        for table in expected:
            for column in meta[table]:
                if column['is_identity'] == 'YES':
                    name = column['column_name']
                    maximum = conn.execute(sql.SQL('SELECT COALESCE(MAX({}),0) AS n FROM {}.{}').format(
                        sql.Identifier(name),sql.Identifier(database.schema),sql.Identifier(table))).fetchone()['n']
                    high = max(maximum,int(sequences.get(table,0)))
                    conn.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)',
                        (database.schema+'.'+table,name,max(high,1),high>0))
        conn.execute('SET CONSTRAINTS ALL IMMEDIATE')
        if progress:progress({'step':'verify','table':'all','rows':sum(item['rows'] for item in report.values())})
        verified = fingerprint_postgres(conn,database.schema)
        if any(verified[table] != result for table,result in report.items()):
            raise MigrationError('Copied records differ from the source snapshot.')
        for table in expected:
            conn.execute(sql.SQL('ANALYZE {}.{}').format(sql.Identifier(database.schema),sql.Identifier(table)))
    return {'kind':'beacon-postgres-migration','schema':database.schema,'verified':True,'tables':report,
            'source_sha256':manifest['source_sha256'],'source_archive_sha256':manifest['sha256'],
            'description_normalizations':audits}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','migrate'))
    parser.add_argument('source');parser.add_argument('destination')
    parser.add_argument('--normalize-provider-descriptions',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='build':
            if args.normalize_provider_descriptions:
                raise MigrationError('The archive must preserve all original descriptions.')
            result=build_archive(args.source,args.destination)
        else:
            if Path(args.destination).exists():raise MigrationError('Migration report destination must be a new file.')
            database=PostgresDatabase(os.getenv('BEACON_POSTGRES_ADMIN_DSN',''),
                schema=os.getenv('BEACON_POSTGRES_SCHEMA','beacon_index'))
            result=migrate_archive(args.source,database,normalize_provider_descriptions=args.normalize_provider_descriptions,
                progress=lambda item:print(json.dumps(item),flush=True))
            _write_report(args.destination,result)
            result={'verified':True,'tables':len(result['tables']),
                    'description_normalizations':len(result['description_normalizations'])}
        print(json.dumps(result,sort_keys=True));return 0
    except Exception as error:
        print(json.dumps({'error':str(error) if isinstance(error,MigrationError) else
                         'Administrator operation failed: '+type(error).__name__}));return 2


if __name__=='__main__':raise SystemExit(main())
