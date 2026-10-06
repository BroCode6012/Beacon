"""Explicit final catch-up from a verified snapshot into an inactive PG target.

Compare every row, apply only differences, remove obsolete rows child-first,
then verify every table before commit. Never reconcile an active save queue.
"""
from hashlib import sha256
import json
from pathlib import Path
import re
import struct

from beacon_engine.research.postgres_migration import (
    MigrationError, _validated_snapshot, _sqlite_reader, _metadata, _primary_key, _cell,
    normalized_migration_row,
)


def row_hash(row, columns):
    digest = sha256()
    for value, column in zip(row, columns):
        cell = _cell(value, column['data_type'])
        digest.update(struct.pack('>Q', len(cell)))
        digest.update(cell)
    digest.update(b'R')
    return digest.digest()


def postgres_row_hash(columns):
    """Native SHA uses exactly the existing migration's typed cell framing."""
    from psycopg import sql
    framed = []
    for column in columns:
        name, kind = sql.Identifier(column['column_name']), column['data_type']
        if kind == 'text':
            value = sql.SQL('{} || convert_to({},\'UTF8\')').format(sql.Literal(b'T'), name)
        elif kind in ('integer', 'bigint', 'smallint'):
            value = sql.SQL('{} || convert_to({}::text,\'UTF8\')').format(sql.Literal(b'I'), name)
        elif kind in ('double precision', 'real'):
            value = sql.SQL('{} || float8send({}::double precision)').format(sql.Literal(b'F'), name)
        else:
            raise MigrationError('A reconciliation column type needs review.')
        cell = sql.SQL('CASE WHEN {} IS NULL THEN {} ELSE {} END').format(name, sql.Literal(b'N'), value)
        framed.append(sql.SQL('int8send(octet_length({})::bigint) || ({})').format(cell, cell))
    return sql.SQL('sha256({} || {})').format(sql.SQL(' || ').join(framed), sql.Literal(b'R'))


def _hash_query(schema, table, columns, pk):
    from psycopg import sql
    types = {c['column_name']: c['data_type'] for c in columns}
    order = [sql.SQL('{} COLLATE "C"').format(sql.Identifier(c)) if types[c] == 'text'
             else sql.Identifier(c) for c in pk]
    return sql.SQL('SELECT {}, {} AS _row_hash FROM {}.{} ORDER BY {}').format(
        sql.SQL(',').join(sql.Identifier(c) for c in pk), postgres_row_hash(columns),
        sql.Identifier(schema), sql.Identifier(table), sql.SQL(',').join(order))


def _key(row, names, pk):
    result = tuple(row[names.index(c)] for c in pk)
    if any(value is None for value in result):
        raise MigrationError('A snapshot primary key contains an unsupported null value.')
    return result


def reconcile_snapshot(source, database, *, writers_paused=False, progress=None,
                       normalize_provider_descriptions=False):
    """Caller must drain canonical writers and keep them paused through commit."""
    from psycopg import sql
    if writers_paused is not True:
        raise MigrationError('Final reconciliation requires an explicit coordinated writer pause.')
    if type(normalize_provider_descriptions) is not bool:
        raise MigrationError('Description normalization requires an explicit boolean setting.')
    source = _validated_snapshot(source)
    report, changes, normalizations = {}, {}, []
    with _sqlite_reader(source, immutable=True) as old, database.connect() as new:
        new.execute('SET LOCAL statement_timeout=0')
        new.execute('SET LOCAL idle_in_transaction_session_timeout=0')
        new.execute('SELECT pg_advisory_xact_lock(726493184)')
        columns = _metadata(new, database.schema)
        expected = set(columns) - {'beacon_schema_meta', 'channel_save_requests'}
        actual = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual != expected:
            raise MigrationError('Source and target table inventories differ.')
        if new.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM {}.channel_save_requests) AS active').format(
                sql.Identifier(database.schema))).fetchone()['active']:
            raise MigrationError('Reconciliation refuses a target with saved-channel requests.')
        if old.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationError('The final snapshot contains broken relationships.')
        ddl = Path(__file__).with_name('postgres_schema.sql').read_text(encoding='utf-8')
        ordered = [t for t in re.findall(r'CREATE TABLE IF NOT EXISTS ([a-z_]+)', ddl) if t in expected]
        if set(ordered) != expected:
            raise MigrationError('Reconciliation dependency order is incomplete.')
        deletion_tables, deferred_relationships = {}, []
        for table in ordered:
            target_columns = columns[table]
            names = [c['column_name'] for c in target_columns]
            if set(names) != {r[1] for r in old.execute('PRAGMA table_info("' + table + '")')}:
                raise MigrationError('Source and target column inventories differ.')
            pk = _primary_key(new, database.schema, table)
            target = sql.Identifier(database.schema, table)
            keys = sql.SQL(',').join(sql.Identifier(c) for c in pk)
            temporary = '_beacon_deleted_' + table
            deletion_tables[table] = (temporary, pk)
            new.execute(sql.SQL('CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WITH NO DATA').format(
                sql.Identifier(temporary), keys, target))
            new.execute(sql.SQL('ALTER TABLE pg_temp.{} ADD PRIMARY KEY ({})').format(sql.Identifier(temporary), keys))
            delete_key = sql.SQL('INSERT INTO pg_temp.{} ({}) VALUES ({})').format(
                sql.Identifier(temporary), keys, sql.SQL(',').join(sql.Placeholder() for _ in pk))
            assignments = sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c),sql.Identifier(c))
                                          for c in names if c not in pk)
            conflict = sql.SQL('DO UPDATE SET {}').format(assignments) if assignments else sql.SQL('DO NOTHING')
            upsert = sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) {}').format(
                target, sql.SQL(',').join(sql.Identifier(c) for c in names),
                sql.SQL(',').join(sql.Placeholder() for _ in names), keys, conflict)
            # Unique labels can be swapped or reused after a removal. Stage their
            # changed rows first so old labels cannot block a valid final state.
            # Cascaded children are compared later in dependency order and restored
            # from the authoritative snapshot inside this same transaction.
            unique_labels = new.execute('SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid '
                'JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s '
                'AND i.indisunique AND NOT i.indisprimary) AS present',(database.schema,table)).fetchone()['present']
            changed_table = '_beacon_changed_' + table
            if unique_labels:
                relationships = new.execute("SELECT n.nspname AS schema,c.relname AS table,k.conname AS name,"
                    "k.confdeltype,k.condeferrable,k.condeferred FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE k.contype='f' AND k.confrelid=%s::regclass "
                    "AND k.confdeltype<>'c'",(database.schema+'.'+table,)).fetchall()
                for relation in relationships:
                    if relation['schema'] != database.schema or relation['confdeltype'] != 'a':
                        raise MigrationError('A unique-label table has relationships that need review.')
                    # Lab children use NO ACTION rather than CASCADE. Defer their
                    # checks until the final parents/children are in place, then
                    # restore their original timing before committing.
                    new.execute(sql.SQL('ALTER TABLE {}.{} ALTER CONSTRAINT {} DEFERRABLE INITIALLY DEFERRED').format(
                        sql.Identifier(relation['schema']),sql.Identifier(relation['table']),sql.Identifier(relation['name'])))
                    deferred_relationships.append(relation)
                new.execute(sql.SQL('CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT * FROM {} WITH NO DATA').format(
                    sql.Identifier(changed_table),target))
                staged_insert = sql.SQL('INSERT INTO pg_temp.{} ({}) VALUES ({})').format(
                    sql.Identifier(changed_table),sql.SQL(',').join(sql.Identifier(c) for c in names),
                    sql.SQL(',').join(sql.Placeholder() for _ in names))
            select = 'SELECT ' + ','.join('"' + c + '"' for c in names) + ' FROM "' + table + '" ORDER BY ' + ','.join('"' + c + '" COLLATE BINARY' for c in pk)
            count, changed, deleted, digest, pending, obsolete = 0, 0, 0, sha256(), [], []
            def flush():
                if pending:
                    with new.cursor() as writer:
                        writer.executemany(staged_insert if unique_labels else upsert, pending)
                    pending.clear()
                if obsolete:
                    with new.cursor() as writer:
                        writer.executemany(delete_key, obsolete)
                    obsolete.clear()
            with new.cursor(name='reconcile_' + table) as cursor:
                cursor.itersize = 2000
                cursor.execute(_hash_query(database.schema, table, target_columns, pk))
                current = next(cursor, None)
                for row in old.execute(select):
                    if normalize_provider_descriptions:
                        row = normalized_migration_row(table, names, pk, row, normalizations)
                    key, hashed = _key(row, names, pk), row_hash(row, target_columns)
                    digest.update(hashed)
                    count += 1
                    while current is not None and tuple(current[c] for c in pk) < key:
                        obsolete.append(tuple(current[c] for c in pk)); deleted += 1
                        current = next(cursor, None)
                        if len(obsolete) >= 1000: flush()
                    if current is None or tuple(current[c] for c in pk) != key:
                        pending.append(row); changed += 1
                    else:
                        if bytes(current['_row_hash']) != hashed:
                            pending.append(row); changed += 1
                        current = next(cursor, None)
                    if len(pending) >= 1000: flush()
                    if progress and count % 100000 == 0:
                        progress({'step':'compare','table':table,'rows':count,'changed':changed,'deleted':deleted})
                while current is not None:
                    obsolete.append(tuple(current[c] for c in pk)); deleted += 1
                    current = next(cursor, None)
                    if len(obsolete) >= 1000: flush()
                flush()
            if unique_labels:
                condition = sql.SQL(' AND ').join(sql.SQL('target.{}=difference.{}').format(
                    sql.Identifier(c),sql.Identifier(c)) for c in pk)
                for difference in (temporary,changed_table):
                    new.execute(sql.SQL('DELETE FROM {} target USING pg_temp.{} difference WHERE {}').format(
                        target,sql.Identifier(difference),condition))
                selected = sql.SQL(',').join(sql.Identifier(c) for c in names)
                new.execute(sql.SQL('INSERT INTO {} ({}) SELECT {} FROM pg_temp.{}').format(
                    target,selected,selected,sql.Identifier(changed_table)))
            report[table] = {'rows':count,'sha256_of_row_sha256':digest.hexdigest()}
            changes[table] = {'upserted':changed,'deleted':deleted}
            if progress: progress({'step':'compared','table':table,'rows':count,**changes[table]})
        # All final parents now exist and changed children point at final parents.
        for table in reversed(ordered):
            temporary, pk = deletion_tables[table]
            condition = sql.SQL(' AND ').join(sql.SQL('target.{}=obsolete.{}').format(
                sql.Identifier(c),sql.Identifier(c)) for c in pk)
            new.execute(sql.SQL('DELETE FROM {}.{} target USING pg_temp.{} obsolete WHERE {}').format(
                sql.Identifier(database.schema),sql.Identifier(table),sql.Identifier(temporary),condition))
        for table in ordered:
            digest, count = sha256(), 0
            pk = deletion_tables[table][1]
            with new.cursor(name='verify_' + table) as cursor:
                cursor.itersize = 2000
                cursor.execute(_hash_query(database.schema,table,columns[table],pk))
                for row in cursor:
                    digest.update(bytes(row['_row_hash'])); count += 1
            if report[table] != {'rows':count,'sha256_of_row_sha256':digest.hexdigest()}:
                raise MigrationError('Final reconciliation differs from the snapshot; all changes rolled back.')
        new.execute('SET CONSTRAINTS ALL IMMEDIATE')
        for relation in deferred_relationships:
            timing = ('DEFERRABLE INITIALLY DEFERRED' if relation['condeferred'] else 'DEFERRABLE INITIALLY IMMEDIATE') \
                if relation['condeferrable'] else 'NOT DEFERRABLE INITIALLY IMMEDIATE'
            new.execute(sql.SQL('ALTER TABLE {}.{} ALTER CONSTRAINT {} {}').format(
                sql.Identifier(relation['schema']),sql.Identifier(relation['table']),sql.Identifier(relation['name']),sql.SQL(timing)))
        sequences = dict(old.execute('SELECT name,seq FROM sqlite_sequence'))
        for table in ordered:
            for column in columns[table]:
                if column['is_identity'] != 'YES': continue
                name = column['column_name']
                maximum = new.execute(sql.SQL('SELECT max({}) AS high FROM {}.{}').format(
                    sql.Identifier(name),sql.Identifier(database.schema),sql.Identifier(table))).fetchone()['high'] or 0
                existing = new.execute('SELECT last_value AS high FROM pg_sequences WHERE schemaname=%s '
                    'AND sequencename=(SELECT relname FROM pg_class WHERE oid=pg_get_serial_sequence(%s,%s)::regclass)',
                    (database.schema,database.schema+'.'+table,name)).fetchone()['high'] or 0
                high = max(maximum,existing,sequences.get(table,0))
                new.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)',
                    (database.schema+'.'+table,name,max(high,1),high>0))
    source_hash = json.loads(Path(str(source) + '.json').read_text(encoding='utf-8'))['sha256']
    return {'kind':'beacon-postgres-reconciliation','verified':True,'schema':database.schema,
            'source_sha256':source_hash,'tables':report,'changes':changes,
            'description_normalizations':normalizations}


def main():
    import argparse
    import os
    import sys
    from beacon_engine.research.postgres import PostgresDatabase
    from beacon_engine.research.postgres_migration import _write_report
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot')
    parser.add_argument('report')
    parser.add_argument('--writers-paused',action='store_true')
    parser.add_argument('--normalize-provider-descriptions',action='store_true')
    args = parser.parse_args()
    try:
        if Path(args.report).exists():
            raise MigrationError('Reconciliation report must be a new file.')
        database = PostgresDatabase(os.environ.get('BEACON_POSTGRES_ADMIN_DSN',''),
                                    schema=os.environ.get('BEACON_POSTGRES_SCHEMA','beacon_index'))
        result = reconcile_snapshot(args.snapshot,database,writers_paused=args.writers_paused,
                                    normalize_provider_descriptions=args.normalize_provider_descriptions,
                                    progress=lambda item:print(json.dumps(item),flush=True))
        _write_report(args.report,result)
        print(json.dumps({'verified':True,'tables':len(result['tables'])}))
        return 0
    except MigrationError as error:
        print(str(error),file=sys.stderr)
    except Exception as error:
        print('Reconciliation failed ('+type(error).__name__+'); inspect private administrator diagnostics.',file=sys.stderr)
    return 1


if __name__=='__main__':
    import sys
    sys.exit(main())
