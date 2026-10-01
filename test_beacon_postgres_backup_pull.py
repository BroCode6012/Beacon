import json
from pathlib import Path
import tempfile
import unittest

import postgres_backup_pull as transfer
import postgres_backup_schedule as recovery


class TransferSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.payload = b'Unicode archive fixture caf\xc3\xa9 with exact bytes'
        from hashlib import sha256
        self.name = 'beacon-20261001T020000Z-0123456789ab.dump'
        self.record = {'name': self.name, 'bytes': len(self.payload), 'sha256': sha256(self.payload).hexdigest()}
        self.report = {'kind': 'beacon-postgres-backup', 'schema': 'beacon_index',
                       'bytes': len(self.payload), 'sha256': self.record['sha256'],
                       'tables': {str(i): {} for i in range(32)}}
        case = self

        class FakeRemote:
            offsets = []; proofs = []; interrupt = False; corrupt = False; reject_ack = False
            def catalog(self): return [case.record]
            def manifest(self, _): return json.dumps(case.report).encode()
            def append(self, name, offset, output, timeout):
                self.offsets.append(offset)
                content = case.payload[offset:]
                if self.corrupt: content = b'x' * len(content)
                if self.interrupt and offset == 0:
                    output.write(content[:10]); return False
                output.write(content); return True
            def acknowledge(self, proof):
                if self.reject_ack: raise recovery.BackupError('Simulated acknowledgement interruption')
                case.assertEqual(recovery.digest(case.directory / case.name), proof['sha256'])
                self.proofs.append(proof)
        self.remote = FakeRemote()

    def run_pull(self):
        return transfer.pull(self.directory, self.remote, free_bytes=lambda: 10*recovery.GIB)

    def test_interrupted_transfer_resumes_then_acknowledges_exact_bytes(self):
        self.remote.interrupt = True
        self.assertTrue(self.run_pull()['verified'])
        self.assertEqual(self.remote.offsets, [0, 10])
        self.assertEqual((self.directory / self.name).read_bytes(), self.payload)
        self.assertTrue(recovery.completed(self.directory)[0]['offhost_verified'])
        self.run_pull()
        self.assertEqual(self.remote.offsets, [0, 10])

    def test_corruption_is_retained_unapproved_and_never_acknowledged(self):
        self.remote.corrupt = True
        with self.assertRaisesRegex(recovery.BackupError, 'size/hash'):
            self.run_pull()
        self.assertTrue((self.directory / (self.name+'.partial')).exists())
        self.assertFalse((self.directory / self.name).exists())
        self.assertEqual(self.remote.proofs, [])

    def test_lost_ack_reuses_verified_local_archive_without_retransmitting(self):
        self.remote.reject_ack = True
        with self.assertRaises(recovery.BackupError): self.run_pull()
        self.assertFalse(recovery.completed(self.directory)[0]['offhost_verified'])
        self.remote.reject_ack = False
        self.assertTrue(self.run_pull()['verified'])
        self.assertEqual(self.remote.offsets, [0])

    def test_malformed_catalog_or_manifest_blocks_file_creation(self):
        original = self.record.copy()
        for invalid in ('../outside.dump', '-unsafe.dump'):
            self.record['name'] = invalid
            with self.assertRaises(recovery.BackupError): self.run_pull()
        self.record = original
        self.report['sha256'] = 'b'*64
        with self.assertRaises(recovery.BackupError): self.run_pull()
        self.assertEqual(self.remote.offsets, [])

    def test_existing_file_is_never_overwritten_and_space_floor_blocks_transfer(self):
        path = self.directory / self.name; path.write_bytes(b'original user data')
        with self.assertRaises(recovery.BackupError): self.run_pull()
        self.assertEqual(path.read_bytes(), b'original user data')
        path.unlink()  # This test fixture alone; no application recovery file.
        with self.assertRaisesRegex(recovery.BackupError, 'room'):
            transfer.pull(self.directory, self.remote, free_bytes=lambda: 8*recovery.GIB)
        self.assertEqual(self.remote.proofs, [])


if __name__ == '__main__':
    unittest.main()
