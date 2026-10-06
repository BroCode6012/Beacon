"""Refuse a partial database switch. Private settings never reach diagnostics."""
import json
import os
from pathlib import Path
import subprocess
import sys

INDEX_UNITS = ('beacon-web', 'beacon-finder', 'beacon-gateway', 'beacon-radar',
               'beacon-edge-sync', 'beacon-save-worker')
TARGET = 'postgresql:beacon-index'
ALIASES = ('BEACON_INDEX_DB', 'SIGNAL_INDEX_DB', 'YOUTUBE_INDEX_DB')


class ActivationError(RuntimeError):
    pass


def settings(path):
    result = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator:
            raise ActivationError('PostgreSQL activation settings need review.')
        result[key.strip()] = value.strip().strip('\"\'')
    return result


def selected(values):
    return all(values.get(key) == TARGET for key in ALIASES) and bool(
        values.get('BEACON_POSTGRES_DSN'))


def running_environment(unit):
    result = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', unit],
                            capture_output=True, text=True, check=True)
    pid = int(result.stdout.strip() or 0)
    if not pid:
        return None
    try:
        content = Path(f'/proc/{pid}/environ').read_bytes()
    except FileNotFoundError:
        return None  # The service finished between the two observations.
    return dict(item.decode('utf-8').split('=', 1) for item in content.split(b'\0')
                if b'=' in item)


def check(data, environment=running_environment):
    data = Path(data)
    private = data / 'postgres.env'
    if not private.exists():
        return False  # Installing PostgreSQL support never selects it by itself.
    if not selected(settings(private)):
        raise ActivationError('PostgreSQL activation settings need review.')
    config = json.loads((data / 'engine-storage/scraper_watchlist.json').read_text(encoding='utf-8'))
    if config.get('db_path') != TARGET:
        raise ActivationError('Radar still selects SQLite. Complete the coordinated cutover first.')
    for unit in INDEX_UNITS:
        values = environment(unit)
        if values is not None and not selected(values):
            raise ActivationError(f'{unit} still selects SQLite. Drain and stop index services before cutover.')
    return True


def main():
    try:
        active = check(sys.argv[1])
        print('PostgreSQL activation checks passed.' if active else 'SQLite deployment retained.')
    except ActivationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Database activation could not be verified; deployment stopped.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
