"""Deployment must not split the canonical index between two database engines."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SERVER = HERE / 'deploy/server'
spec = importlib.util.spec_from_file_location('postgres_activation', SERVER / 'check_postgres_activation.py')
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


class PostgreSQLDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        (self.data / 'engine-storage').mkdir()
        self.values = dict.fromkeys(activation.ALIASES, activation.TARGET)
        self.values['BEACON_POSTGRES_DSN'] = 'private-test-placeholder'

    def prepare(self, target=activation.TARGET):
        (self.data / 'postgres.env').write_text('\n'.join(f'{k}={v}' for k,v in self.values.items()),encoding='utf-8')
        (self.data / 'engine-storage/scraper_watchlist.json').write_text(json.dumps({'db_path':target}),encoding='utf-8')

    def test_absent_activation_file_preserves_sqlite(self):
        self.assertFalse(activation.check(self.data, lambda unit: self.fail('No process inspection needed')))

    def test_stopped_services_allow_explicit_cutover_and_pg_upgrade_is_allowed(self):
        self.prepare()
        self.assertTrue(activation.check(self.data, lambda unit: None))
        self.assertTrue(activation.check(self.data, lambda unit: self.values))

    def test_every_old_index_process_blocks_partial_activation(self):
        self.prepare()
        for old_unit in activation.INDEX_UNITS:
            with self.subTest(unit=old_unit), self.assertRaises(activation.ActivationError) as error:
                activation.check(self.data,lambda unit: {} if unit==old_unit else self.values)
            self.assertNotIn(self.values['BEACON_POSTGRES_DSN'],str(error.exception))

    def test_explicit_radar_path_must_be_switched_together(self):
        self.prepare('/legacy/index.sqlite3')
        with self.assertRaisesRegex(activation.ActivationError,'Radar still selects SQLite'):
            activation.check(self.data,lambda unit:None)

    def test_incomplete_activation_settings_are_refused(self):
        del self.values['YOUTUBE_INDEX_DB']
        self.prepare()
        with self.assertRaises(activation.ActivationError):
            activation.check(self.data,lambda unit:None)

    def test_worker_has_private_pg_settings_keys_and_cooperative_stop(self):
        worker=(SERVER/'systemd/beacon-save-worker.service').read_text(encoding='utf-8')
        self.assertIn('ConditionPathExists=@DATA@/postgres.env',worker)
        self.assertIn('EnvironmentFile=@DATA@/postgres.env',worker)
        self.assertIn('-m beacon_engine.research.channel_save_jobs',worker)
        self.assertIn('TimeoutStopSec=180',worker)
        self.assertNotIn('EnvironmentFile=@KEYS@',worker)
        installer=(SERVER/'install.sh').read_text(encoding='utf-8')
        self.assertLess(installer.index('check_postgres_activation.py'),installer.index('mv -Tf'))
        self.assertNotIn('systemctl enable beacon-save-worker',installer)
        self.assertNotIn('systemctl start beacon-save-worker',installer)


if __name__=='__main__':
    unittest.main()
