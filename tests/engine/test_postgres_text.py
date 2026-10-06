import base64
import json
import unittest
from beacon_engine.research.postgres_text import normalize_description, normalize_snapshot_row


class DescriptionPolicyTests(unittest.TestCase):
    def test_provider_original_and_metadata_are_retained_without_changing_healthy_payloads(self):
        original = 'café\0漢字\nline'
        description, raw = normalize_description(original, '{"snippet":{"other":"kept"}}')
        self.assertEqual(description, 'café漢字\nline')
        payload = json.loads(raw)
        self.assertEqual(payload['snippet']['other'], 'kept')
        self.assertEqual(base64.b64decode(payload['_beacon_description_nul_v1']['original_utf8_base64']).decode(), original)
        self.assertNotIn('\0', raw)
        self.assertEqual(normalize_description('healthy', '{  "x":1 }'), ('healthy', '{  "x":1 }'))

    def test_snapshot_policy_never_changes_identifiers_titles_or_other_tables(self):
        names = ['id', 'title', 'description']; row = ('key\0', 'title\0', 'text\0'); audit = []
        normalized = normalize_snapshot_row('channels', names, ['id'], row, audit)
        self.assertEqual(normalized, ('key\0', 'title\0', 'text'))
        self.assertEqual(row, ('key\0', 'title\0', 'text\0'))
        self.assertEqual(normalize_snapshot_row('lab_notes', names, ['id'], row, []), row)
        self.assertEqual(base64.b64decode(audit[0]['original_utf8_base64']).decode(), 'text\0')

    def test_normalization_refuses_unbounded_audit_memory(self):
        audit = [{'original_bytes': 1}] * 1000
        with self.assertRaises(ValueError):
            normalize_snapshot_row('channels', ['id', 'description'], ['id'], ('key', 'text\0'), audit)
        self.assertEqual(len(audit), 1000)
