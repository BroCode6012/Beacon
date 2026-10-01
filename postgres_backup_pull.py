"""Resume private off-host recovery transfers and acknowledge verified bytes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import postgres_backup_schedule as recovery


class Remote:
    def __init__(self, settings):
        self.directory = settings['remote_directory']
        self.python = settings['remote_python']
        self.script = settings['remote_script']
        key = Path(settings['ssh_key']).resolve(strict=True)
        self.ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
                    '-i', str(key), settings['ssh_host']]
        for path in (self.directory, self.python, self.script):
            if not isinstance(path, str) or not path.startswith('/'):
                raise recovery.BackupError('Remote recovery paths must be explicit absolute paths.')
        if not isinstance(settings['ssh_host'], str) or settings['ssh_host'].startswith('-'):
            raise recovery.BackupError('An explicit SSH destination is required.')

    def command(self, argv, *, input=None, timeout=300):
        result = subprocess.run(self.ssh + [shlex.join(['sudo', '-n', *argv])],
                                input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
        if result.returncode:
            raise recovery.BackupError('The private recovery server operation failed.')
        return result.stdout

    def catalog(self):
        value = json.loads(self.command([self.python, self.script, 'list', '--directory', self.directory]))
        return value['archives']

    def manifest(self, name):
        return self.command(['cat', self.directory + '/' + name + '.json'])

    def append(self, name, offset, output, timeout):
        argv = ['sudo', '-n', 'tail', '-c', '+' + str(offset + 1), self.directory + '/' + name]
        try:
            result = subprocess.run(self.ssh + [shlex.join(argv)], stdout=output,
                                    stderr=subprocess.PIPE, timeout=timeout)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def acknowledge(self, proof):
        result = json.loads(self.command([self.python, self.script, 'ack', '--directory', self.directory],
                                        input=json.dumps(proof).encode('utf-8')))
        if result != proof:
            raise recovery.BackupError('The server did not confirm the exact off-host proof.')


def valid_record(item):
    return (isinstance(item, dict) and isinstance(item.get('name'), str)
            and recovery.NAME.fullmatch(item['name'])
            and type(item.get('bytes')) is int and item['bytes'] > 0
            and isinstance(item.get('sha256'), str)
            and len(item['sha256']) == 64 and all(c in '0123456789abcdef' for c in item['sha256']))


def pull(directory, remote, *, keep=7, free_bytes=None, progress=None):
    directory = recovery.private_directory(directory)
    free_bytes = free_bytes or (lambda: shutil.disk_usage(directory).free)
    progress = progress or (lambda _: None)
    started = time.monotonic()
    transferred = []
    with recovery.recovery_lock(directory):
        recovery.prune_verified(directory, keep)
        records = remote.catalog()
        if not isinstance(records, list) or not all(valid_record(item) for item in records):
            raise recovery.BackupError('The remote recovery catalog needs review.')
        if len({item['name'] for item in records}) != len(records):
            raise recovery.BackupError('The recovery catalog contains duplicate archive names.')
        for item in records:
            path = directory / item['name']
            partial = Path(str(path) + '.partial')
            manifest = Path(str(path) + '.json')
            if any(candidate.is_symlink() for candidate in (path, partial, manifest)):
                raise recovery.BackupError('Unexpected local recovery file path.')
            encoded = remote.manifest(item['name']); report = json.loads(encoded)
            if (report.get('kind') != 'beacon-postgres-backup' or report.get('schema') != 'beacon_index'
                    or report.get('sha256') != item['sha256'] or report.get('bytes') != item['bytes']
                    or len(report.get('tables', {})) != 32):
                raise recovery.BackupError('The remote archive and completed manifest differ.')
            if not path.exists():
                if not partial.exists():
                    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
                if partial.stat().st_size > item['bytes']:
                    raise recovery.BackupError('An incomplete file exceeds the sealed archive size.')
                remaining = item['bytes'] - partial.stat().st_size
                if free_bytes() < remaining + 8*recovery.GIB:
                    raise recovery.BackupError('Insufficient off-host room and protected disk reserve.')
                for attempt in range(8):
                    offset = partial.stat().st_size
                    if offset == item['bytes']:
                        break
                    budget = min(7200, 21600 - (time.monotonic() - started))
                    if budget <= 0:
                        raise recovery.BackupError('The off-host transfer time budget expired.')
                    progress({'state': 'transferring', 'name': item['name'],
                              'resume_bytes': offset, 'total_bytes': item['bytes'], 'attempt': attempt+1})
                    with partial.open('ab') as output:
                        successful = remote.append(item['name'], offset, output, budget)
                        output.flush(); os.fsync(output.fileno())
                    if partial.stat().st_size > item['bytes']:
                        raise recovery.BackupError('The transfer exceeds its sealed archive size.')
                    if successful and partial.stat().st_size == item['bytes']:
                        break
                if partial.stat().st_size != item['bytes'] or recovery.digest(partial) != item['sha256']:
                    raise recovery.BackupError('The off-host transfer did not pass its size/hash check.')
                partial.rename(path)
            elif path.stat().st_size != item['bytes'] or recovery.digest(path) != item['sha256']:
                raise recovery.BackupError('An existing off-host archive differs; preserve it for review.')
            if manifest.exists():
                if manifest.read_bytes() != encoded:
                    raise recovery.BackupError('An existing off-host manifest differs; preserve it for review.')
            else:
                fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(encoded); output.flush(); os.fsync(output.fileno())
            proof = {'kind': 'beacon-offhost-postgres-backup', 'verified': True,
                     'name': item['name'], 'bytes': item['bytes'], 'sha256': item['sha256']}
            remote.acknowledge(proof)  # Only after local bytes and manifest are sealed.
            local_proof = Path(str(path) + '.offhost.json')
            if local_proof.is_symlink():
                raise recovery.BackupError('Unexpected local off-host proof path.')
            if local_proof.exists():
                if json.loads(local_proof.read_text(encoding='utf-8')) != proof:
                    raise recovery.BackupError('The existing local verification proof differs.')
            else:
                recovery.write_private(local_proof, proof)
            transferred.append(item['name'])
        recovery.prune_verified(directory, keep)
    return {'state': 'complete', 'verified': True, 'archives': transferred,
            'checked_utc': datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    directory = None
    try:
        settings = json.loads(Path(args.config).read_text(encoding='utf-8'))
        directory = recovery.private_directory(settings['local_directory'])
        result = pull(directory, Remote(settings), keep=settings.get('keep_verified', 7),
                      progress=lambda value: print(json.dumps(value), flush=True))
        recovery.write_private(directory / 'transfer-status.json', result, replace=True)
        print(json.dumps(result)); return 0
    except Exception as error:
        result = {'state': 'failed', 'error_type': type(error).__name__,
                  'reason': str(error) if isinstance(error, recovery.BackupError) else 'Private recovery transfer failed; inspect the scheduled task result.'}
        if directory is not None:
            recovery.write_private(directory / 'transfer-status.json', result, replace=True)
        print(json.dumps(result)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
