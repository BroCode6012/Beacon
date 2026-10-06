"""Narrow, lossless audit of provider descriptions PostgreSQL cannot store.

No identifier, title, user note or other text is silently changed. Original
description bytes are retained in provider metadata or the private migration
report. SQLite and the sealed recovery snapshot remain untouched.
"""
import base64
from hashlib import sha256
import json


def normalize_description(description, raw_json):
    if not isinstance(description, str) or '\0' not in description:
        return description, raw_json
    try:
        raw = json.loads(raw_json)
    except (ValueError, TypeError):
        raw = None
    if not isinstance(raw, dict):
        raw = {'_beacon_original_raw_json': raw_json}
    else:
        raw = dict(raw)
    audit = {'original_utf8_base64': base64.b64encode(description.encode('utf-8')).decode('ascii'),
             'nul_characters': description.count('\0')}
    if '_beacon_description_nul_v1' in raw:
        audit['previous'] = raw['_beacon_description_nul_v1']
    raw['_beacon_description_nul_v1'] = audit
    return description.replace('\0', ''), json.dumps(raw, ensure_ascii=False)


def normalize_snapshot_row(table, names, pk, row, audit):
    if table not in ('channels', 'videos'):
        return row
    index = names.index('description')
    description = row[index]
    if not isinstance(description, str) or '\0' not in description:
        return row
    encoded = description.encode('utf-8')
    if len(audit) >= 1000 or sum(item['original_bytes'] for item in audit) + len(encoded) > 16 * 1024**2:
        raise ValueError('Description normalization exceeds the bounded review limit.')
    cleaned = description.replace('\0', '')
    audit.append({'table': table, 'primary_key': {c: row[names.index(c)] for c in pk},
                  'column': 'description', 'policy': 'remove-nul-v1',
                  'original_utf8_base64': base64.b64encode(encoded).decode('ascii'),
                  'original_bytes': len(encoded), 'nul_characters': description.count('\0'),
                  'replacement_sha256': sha256(cleaned.encode('utf-8')).hexdigest()})
    values = list(row)
    values[index] = cleaned
    return tuple(values)
