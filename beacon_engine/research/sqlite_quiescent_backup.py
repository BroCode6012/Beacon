"""Explicit compressed recovery copy of a drained, checkpointed SQLite index.

Never call this for an online database. The operator must continuously prove
writers are paused; no database settings or source bytes are changed here.
"""
import gzip
from hashlib import sha256
import os
from pathlib import Path
import time

from beacon_engine.research.postgres_migration import (
    MigrationError, _sqlite_reader, _write_report, file_hash,
)


def _signature(path):
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _pending(path):
    return any(Path(str(path) + suffix).exists() and
               Path(str(path) + suffix).stat().st_size
               for suffix in ('-wal', '-journal'))


def _new_destination(path, source):
    path = Path(path).resolve()
    if path == source or path.exists() or Path(str(path) + '.json').exists():
        raise MigrationError('Recovery destination must be a new file.')
    return path


def _verify(path, check):
    with _sqlite_reader(path, immutable=True) as connection:
        connection.set_progress_handler(lambda: (check(), 0)[1], 100000)
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise MigrationError('Quiescent recovery failed integrity verification.')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationError('Quiescent recovery has broken relationships.')
    check(force=True)


def compress_quiescent_sqlite(source, destination, *, writers_paused=False,
                              guard=None, progress=None, deadline_seconds=7200):
    """Guard must raise or return False whenever the coordinated pause is lost."""
    if writers_paused is not True or not callable(guard):
        raise MigrationError('A coordinated writer pause and continuous guard are required.')
    source = Path(source).resolve(strict=True)
    destination = _new_destination(destination, source)
    signature = _signature(source)
    started, last = time.monotonic(), 0

    def check(force=False):
        nonlocal last
        now = time.monotonic()
        if now - started > deadline_seconds:
            raise MigrationError('Quiescent recovery exceeded its time budget.')
        if force or now - last >= 2:
            if guard() is False:
                raise MigrationError('The coordinated writer pause was lost.')
            if _pending(source) or _signature(source) != signature:
                raise MigrationError('Quiescent source changed or has pending writes.')
            last = now

    check(force=True)
    if progress: progress({'step': 'verify_quiescent_source'})
    _verify(source, check)
    digest, count = sha256(), 0
    with source.open('rb') as original, destination.open('xb') as output:
        if os.name != 'nt': os.chmod(destination, 0o600)
        with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=1, mtime=0, filename='') as archive:
            for chunk in iter(lambda: original.read(4 * 1024 * 1024), b''):
                check(); digest.update(chunk); archive.write(chunk); count += len(chunk)
                if progress: progress({'step': 'compress_quiescent_source', 'bytes': count})
        output.flush(); os.fsync(output.fileno())
    check(force=True)
    if count != signature[2]:
        raise MigrationError('Quiescent source length changed during recovery.')
    report = {'kind': 'beacon-quiescent-sqlite-backup', 'integrity': 'ok',
              'relationships_checked': True, 'source_bytes': count,
              'source_sha256': digest.hexdigest(), 'bytes': destination.stat().st_size,
              'sha256': file_hash(destination)}
    check(force=True)
    _write_report(str(destination) + '.json', report)
    return report


def recover_quiescent_sqlite(archive, destination, *, progress=None):
    """Recover only a new off-host file, validating exact bytes and relationships."""
    import json
    archive = Path(archive).resolve(strict=True)
    destination = _new_destination(destination, archive)
    try:
        report = json.loads(Path(str(archive) + '.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise MigrationError('Quiescent recovery requires a sealed archive manifest.') from None
    if (report.get('kind') != 'beacon-quiescent-sqlite-backup' or
            report.get('integrity') != 'ok' or report.get('relationships_checked') is not True or
            report.get('bytes') != archive.stat().st_size or report.get('sha256') != file_hash(archive) or
            type(report.get('source_bytes')) is not int or report['source_bytes'] <= 0):
        raise MigrationError('Quiescent archive does not match its verified manifest.')
    digest, count = sha256(), 0
    with gzip.open(archive, 'rb') as original, destination.open('xb') as output:
        if os.name != 'nt': os.chmod(destination, 0o600)
        for chunk in iter(lambda: original.read(4 * 1024 * 1024), b''):
            count += len(chunk)
            if count > report['source_bytes']:
                raise MigrationError('Recovered source exceeds its sealed size.')
            digest.update(chunk); output.write(chunk)
            if progress: progress({'step': 'recover_quiescent_source', 'bytes': count})
        output.flush(); os.fsync(output.fileno())
    if count != report['source_bytes'] or digest.hexdigest() != report['source_sha256']:
        raise MigrationError('Recovered source differs from the sealed original bytes.')
    _verify(destination, lambda force=False: None)
    snapshot = {'kind': 'beacon-sqlite-snapshot', 'bytes': count,
                'sha256': digest.hexdigest(), 'integrity': 'ok', 'relationships_checked': True}
    _write_report(str(destination) + '.json', snapshot)
    return snapshot
