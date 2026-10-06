"""Private recurring recovery archives; never selects a database for the app."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from uuid import uuid4

NAME = re.compile(r'beacon-\d{8}T\d{6}Z-[0-9a-f]{12}\.dump\Z')
GIB = 1024**3


class BackupError(RuntimeError):
    pass


def private_directory(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise BackupError('Recovery storage must be a real private directory.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(directory, 0o700)
    return directory.resolve(strict=True)


@contextmanager
def recovery_lock(directory):
    path = Path(directory) / '.schedule.lock'
    if path.is_symlink():
        raise BackupError('Unexpected recovery lock path.')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                if path.stat().st_size == 0:
                    lock.write(b'0'); lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackupError('Another recovery operation is running.') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def write_private(path, value, *, replace=False):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.partial') if replace else path
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(value, output, sort_keys=True)
        output.flush(); os.fsync(output.fileno())
    if replace:
        os.replace(temporary, path)


def digest(path):
    result = sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024**2), b''):
            result.update(chunk)
    return result.hexdigest()


def completed(directory):
    result = []
    for archive in sorted(Path(directory).glob('beacon-*.dump')):
        if not NAME.fullmatch(archive.name) or archive.is_symlink():
            raise BackupError('Unexpected recovery archive path.')
        manifest = Path(str(archive) + '.json')
        if not manifest.is_file():
            continue  # An interrupted operation cannot approve an archive.
        if manifest.is_symlink():
            raise BackupError('Unexpected recovery manifest path.')
        report = json.loads(manifest.read_text(encoding='utf-8'))
        if (report.get('kind') != 'beacon-postgres-backup'
                or report.get('schema') != 'beacon_index'
                or type(report.get('bytes')) is not int or report['bytes'] <= 0
                or report['bytes'] != archive.stat().st_size
                or not re.fullmatch(r'[0-9a-f]{64}', str(report.get('sha256', '')))
                or len(report.get('tables', {})) != 32):
            raise BackupError('A recovery manifest needs review.')
        ack = Path(str(archive) + '.offhost.json')
        verified = False
        if ack.exists():
            if ack.is_symlink():
                raise BackupError('Unexpected off-host proof path.')
            proof = json.loads(ack.read_text(encoding='utf-8'))
            verified = proof == {'kind': 'beacon-offhost-postgres-backup',
                                'name': archive.name, 'bytes': report['bytes'],
                                'sha256': report['sha256'], 'verified': True}
            if not verified:
                raise BackupError('An off-host verification proof needs review.')
        result.append({'name': archive.name, 'bytes': report['bytes'],
                       'sha256': report['sha256'], 'offhost_verified': verified})
    return result


def acknowledge(directory, proof):
    directory = private_directory(directory)
    with recovery_lock(directory):
        records = {item['name']: item for item in completed(directory)}
        name = proof.get('name')
        if not isinstance(name, str) or name not in records:
            raise BackupError('The off-host proof must name a completed recovery archive.')
        record = records[name]
        expected = {'kind': 'beacon-offhost-postgres-backup', 'name': name,
                    'sha256': record['sha256'], 'bytes': record['bytes'], 'verified': True}
        if proof != expected or digest(directory / name) != record['sha256']:
            raise BackupError('The transferred archive does not match its sealed manifest.')
        path = directory / (name + '.offhost.json')
        if not path.exists():
            write_private(path, proof)
        return expected


def prune_verified(directory, keep):
    """Caller holds the recovery lock; never deletes an unmirrored archive."""
    if type(keep) is not int or keep < 2:
        raise BackupError('Keep at least two off-host verified recovery sets.')
    verified = [item for item in completed(directory) if item['offhost_verified']]
    removed = []
    for item in verified[:-keep]:
        archive = Path(directory) / item['name']
        if digest(archive) != item['sha256']:
            raise BackupError('An older archive changed; preserve it for review.')
        archive.unlink()
        Path(str(archive) + '.json').unlink()
        Path(str(archive) + '.offhost.json').unlink()
        removed.append(item['name'])
    return removed


def create_archive(directory, backup, *, expected_bytes, reserve_bytes=8*GIB,
                   keep=2, free_bytes=None):
    """backup(path, reserve_bytes) must produce the verified native manifest."""
    directory = private_directory(directory)
    if type(expected_bytes) is not int or expected_bytes <= 0 or reserve_bytes < 8*GIB:
        raise BackupError('Measured archive size and at least 8 GiB reserve are required.')
    free_bytes = free_bytes or (lambda: shutil.disk_usage(directory).free)
    with recovery_lock(directory):
        records = completed(directory)
        if len([item for item in records if not item['offhost_verified']]) >= 2:
            raise BackupError('Two recovery sets await off-host verification; transfer them first.')
        prune_verified(directory, keep)
        estimate = max([expected_bytes] + [item['bytes'] for item in records])
        if free_bytes() < reserve_bytes + (estimate * 5 + 3)//4:
            raise BackupError('Insufficient room for a new archive and protected disk reserve.')
        name = 'beacon-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:12] + '.dump'
        path = directory / name
        report = backup(path, reserve_bytes)
        sealed = {item['name']: item for item in completed(directory)}.get(name)
        if sealed is None or sealed['sha256'] != report.get('sha256') or digest(path) != sealed['sha256']:
            raise BackupError('The new archive did not pass its completed-manifest checks.')
        if free_bytes() < reserve_bytes:
            raise BackupError('The protected disk reserve was reached; inspect recovery storage.')
        return {'state': 'awaiting_offhost_verification', **sealed}


def native_backup(path, reserve_bytes):
    import engine_link  # noqa: F401 -- before any shared-engine imports
    from psycopg.conninfo import make_conninfo
    from scripter.research.postgres import PostgresDatabase
    from scripter.research.postgres_migration import backup_postgres
    operation = 'beacon-recurring-backup-' + uuid4().hex[:12]
    dsn = make_conninfo(os.environ['BEACON_POSTGRES_ADMIN_DSN'], application_name=operation)

    class NamedDatabase(PostgresDatabase):
        def open(self):
            conn = super().open()
            conn.execute("SELECT set_config('application_name',%s,false)", (operation,))
            conn.commit()  # Leave snapshot selection to the backup transaction.
            return conn

    database = NamedDatabase(dsn, schema='beacon_index')
    stop = threading.Event(); failed = []; started = time.monotonic()

    def guard():
        while not stop.wait(5):
            if shutil.disk_usage(path.parent).free < reserve_bytes or time.monotonic()-started > 21600:
                failed.append(True)
                with PostgresDatabase(dsn).connect() as control:
                    control.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                                    'WHERE application_name=%s AND usename=current_user '
                                    'AND datname=current_database()', (operation,))
                return

    watcher = threading.Thread(target=guard, daemon=True); watcher.start()
    try:
        report = backup_postgres(database, path, bin_directory=os.getenv('BEACON_PG_BIN', '/usr/lib/postgresql/16/bin'))
        if failed:
            raise BackupError('The recovery disk or time guard stopped this archive.')
        return report
    finally:
        stop.set(); watcher.join(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'list', 'ack'))
    parser.add_argument('--directory', required=True)
    parser.add_argument('--expected-bytes', type=int)
    parser.add_argument('--keep', type=int, default=2)
    args = parser.parse_args()
    try:
        directory = private_directory(args.directory)
        if args.action == 'list':
            result = {'archives': completed(directory)}
        elif args.action == 'ack':
            result = acknowledge(directory, json.load(sys.stdin))
        else:
            if any(os.getenv(key) != 'postgresql:beacon-index' for key in
                   ('BEACON_INDEX_DB', 'SIGNAL_INDEX_DB', 'YOUTUBE_INDEX_DB')):
                raise BackupError('Recurring backups require the coordinated PostgreSQL activation.')
            expected = args.expected_bytes or int(os.environ.get('BEACON_BACKUP_EXPECTED_BYTES', '0'))
            result = create_archive(directory, native_backup, expected_bytes=expected, keep=args.keep)
            write_private(directory / 'schedule-status.json', result, replace=True)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception as error:
        result = {'state': 'failed', 'error_type': type(error).__name__,
                  'reason': str(error) if isinstance(error, BackupError) else 'Recovery operation failed; inspect private service diagnostics.'}
        if args.action == 'create':
            try:
                write_private(Path(args.directory) / 'schedule-status.json', result, replace=True)
            except Exception:
                pass
        print(json.dumps(result)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
