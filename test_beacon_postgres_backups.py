import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import postgres_backup_schedule as schedule


class RecoveryScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def seal(self, name, *, acknowledged=False):
        path = self.directory / name
        path.write_bytes(b'a real archive fixture')
        report = {'kind': 'beacon-postgres-backup', 'schema': 'beacon_index',
                  'bytes': path.stat().st_size, 'sha256': schedule.digest(path),
                  'tables': {str(i): {'rows': i, 'sha256': 'a'*64} for i in range(32)}}
        schedule.write_private(str(path) + '.json', report)
        if acknowledged:
            schedule.acknowledge(self.directory, self.proof(name, report))
        return report

    @staticmethod
    def proof(name, report):
        return {'kind': 'beacon-offhost-postgres-backup', 'verified': True,
                'name': name, 'bytes': report['bytes'], 'sha256': report['sha256']}

    @staticmethod
    def name(day):
        return f'beacon-202610{day:02d}T020000Z-0123456789ab.dump'

    def backup(self, path, reserve):
        self.assertEqual(reserve, 8*schedule.GIB)
        return self.seal(path.name)

    def test_completed_native_archive_waits_for_exact_offhost_proof(self):
        result = schedule.create_archive(self.directory, self.backup, expected_bytes=100,
                                         free_bytes=lambda: 10*schedule.GIB)
        self.assertEqual(result['state'], 'awaiting_offhost_verification')
        self.assertFalse(result['offhost_verified'])
        report = json.loads((self.directory / (result['name'] + '.json')).read_text())
        proof = self.proof(result['name'], report)
        self.assertEqual(schedule.acknowledge(self.directory, proof), proof)
        self.assertEqual(schedule.acknowledge(self.directory, proof), proof)
        self.assertTrue(schedule.completed(self.directory)[0]['offhost_verified'])

    def test_tampered_archive_and_forged_or_traversal_proofs_are_refused(self):
        name = self.name(1); report = self.seal(name)
        for proof in (dict(self.proof(name, report), sha256='b'*64),
                      dict(self.proof(name, report), name='../outside.dump')):
            with self.assertRaises(schedule.BackupError):
                schedule.acknowledge(self.directory, proof)
        path = self.directory / name
        path.write_bytes(b'x' * report['bytes'])
        with self.assertRaisesRegex(schedule.BackupError, 'sealed manifest'):
            schedule.acknowledge(self.directory, self.proof(name, report))
        self.assertFalse(Path(str(path) + '.offhost.json').exists())

    def test_retention_preserves_two_verified_sets_and_every_unmirrored_set(self):
        for day in range(1, 5):
            self.seal(self.name(day), acknowledged=day < 4)
        with schedule.recovery_lock(self.directory):
            removed = schedule.prune_verified(self.directory, 2)
        self.assertEqual(removed, [self.name(1)])
        self.assertEqual([x['name'] for x in schedule.completed(self.directory)],
                         [self.name(2), self.name(3), self.name(4)])
        with self.assertRaises(schedule.BackupError):
            schedule.prune_verified(self.directory, 1)

    def test_two_unmirrored_sets_block_new_archives_without_pruning_verified_sets(self):
        for day in range(1, 6):
            self.seal(self.name(day), acknowledged=day <= 3)
        with self.assertRaisesRegex(schedule.BackupError, 'transfer them first'):
            schedule.create_archive(self.directory, self.fail, expected_bytes=100,
                                     free_bytes=lambda: 100*schedule.GIB)
        self.assertEqual(len(schedule.completed(self.directory)), 5)

    def test_crashed_backup_never_approves_partial_or_manifestless_files(self):
        def crash(path, _):
            Path(str(path) + '.partial').write_bytes(b'partial')
            raise RuntimeError('simulated interrupted dump')
        with self.assertRaises(RuntimeError):
            schedule.create_archive(self.directory, crash, expected_bytes=100,
                                     free_bytes=lambda: 10*schedule.GIB)
        (self.directory / self.name(1)).write_bytes(b'unsealed')
        self.assertEqual(schedule.completed(self.directory), [])

    def test_measured_space_floor_and_concurrent_operation_prevent_creation(self):
        for expected, reserve, free in ((0, 8, 10), (100, 7, 10), (schedule.GIB, 8, 9)):
            with self.subTest(expected=expected, reserve=reserve), self.assertRaises(schedule.BackupError):
                schedule.create_archive(self.directory, self.fail, expected_bytes=expected,
                                         reserve_bytes=reserve*schedule.GIB,
                                         free_bytes=lambda: free*schedule.GIB)
        with schedule.recovery_lock(self.directory), self.assertRaisesRegex(schedule.BackupError, 'running'):
            with schedule.recovery_lock(self.directory):
                self.fail('A second operation must not acquire the lock')

    def test_changed_old_archive_stops_retention_and_preserves_recovery_files(self):
        for day in range(1, 4):
            self.seal(self.name(day), acknowledged=True)
        path = self.directory / self.name(1); path.write_bytes(b'x'*path.stat().st_size)
        with self.assertRaisesRegex(schedule.BackupError, 'changed'):
            schedule.prune_verified(self.directory, 2)
        self.assertEqual(len(schedule.completed(self.directory)), 3)

    def test_service_stays_unenabled_and_failures_do_not_reveal_credentials(self):
        here = Path(__file__).parent
        service = (here / 'deploy/server/systemd/beacon-postgres-backup.service').read_text()
        self.assertIn('ConditionPathExists=@DATA@/postgres.env', service)
        self.assertIn('EnvironmentFile=@DATA@/postgres-admin.env', service)
        self.assertNotIn('beacon-postgres-backup.timer', (here / 'deploy/server/install.sh').read_text())
        with patch.dict(os.environ, {'BEACON_INDEX_DB': 'sqlite', 'BEACON_POSTGRES_ADMIN_DSN': 'secret-test-value'}), \
                patch('sys.argv', ['schedule', 'create', '--directory', str(self.directory)]), \
                patch('builtins.print') as printed:
            self.assertEqual(schedule.main(), 1)
        status = json.loads((self.directory / 'schedule-status.json').read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertNotIn('secret-test-value', json.dumps(status))
        self.assertNotIn('secret-test-value', str(printed.call_args))


if __name__ == '__main__':
    unittest.main()
