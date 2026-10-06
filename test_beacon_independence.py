"""A Beacon checkout must work without loading another app or its credentials."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent


class IndependentEngineTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows windowless launcher')
    def test_windowless_launcher_preserves_worker_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder)/'worker.py'
            marker = Path(folder)/'ran.txt'
            script.write_text('import pathlib, sys\npathlib.Path(sys.argv[1]).write_text("ran")\nsys.exit(7)\n')
            result = subprocess.run(['wscript.exe', '//B', '//Nologo',
                str(ROOT/'run_python_hidden.vbs'), sys.executable, str(script), str(marker)], timeout=20)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(marker.read_text(), 'ran')

    def test_demo_store_transaction_releases_connection_after_exit(self):
        import sqlite3
        import tempfile
        from beacon_engine.research.store import YouTubeStore
        with tempfile.TemporaryDirectory() as folder:
            store = YouTubeStore(Path(folder)/'demo.sqlite3')
            with store.connect() as conn:
                self.assertEqual(conn.execute('SELECT 1').fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_actual_import_graph_contains_no_other_app(self):
        probe = r'''
import importlib.abc, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
class RejectOtherApp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'scripter' or fullname.startswith('scripter.'):
            raise AssertionError('attempted to import the other app')
sys.meta_path.insert(0, RejectOtherApp())
import engine_link
from beacon_engine.intelligence.analyst_tools import TOOL_SCHEMAS
from beacon_engine.intelligence.local_api import create_server
from beacon_engine.research.radar import RadarConfig
from beacon_engine.edge.worker import EdgeWorker
from beacon_engine.research.channel_save_jobs import ChannelSaveQueue
import beacon_app, beacon_gateway, beacon_save
assert not {'list_labs', 'log_video', 'sync_lab_performance'} & TOOL_SCHEMAS.keys()
assert not any('Quill' in str(p) for p in sys.path)
for name, module in list(sys.modules.items()):
    if name.startswith('beacon_engine') and getattr(module, '__file__', None):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
print('standalone import graph passed')
'''
        env = dict(os.environ)
        env.update(BEACON_ENV_FILE=str(ROOT/'missing-test.env'),
                   BEACON_HOST_FILE=str(ROOT/'missing-test.env'),
                   FACELESS_ENGINE_DIR=str(ROOT.parent/'Quill'),
                   SIGNAL_DISABLE_AUTOPILOT='1')
        result = subprocess.run([sys.executable, '-I', '-c', probe, str(ROOT)],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('standalone import graph passed', result.stdout)

    def test_engine_has_no_writing_app_modules_or_imports(self):
        engine = ROOT/'beacon_engine'
        self.assertFalse((engine/'writing').exists())
        self.assertFalse((engine/'chat.py').exists())
        self.assertFalse((engine/'intelligence/production_lab.py').exists())
        for path in engine.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name.split('.')[0]=='scripter' for a in node.names), path)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual((node.module or '').split('.')[0], 'scripter', path)

    def test_host_ignores_legacy_external_engine_setting(self):
        import beacon_host
        from unittest.mock import patch
        with patch.dict(os.environ, {'FACELESS_ENGINE_DIR':str(ROOT.parent/'Quill')}):
            self.assertEqual(beacon_host.default_engine_dir(ROOT), ROOT)

    def test_server_workers_run_the_beacon_owned_namespace(self):
        for name in ('beacon-radar.service','beacon-save-worker.service','beacon-edge-sync.service'):
            content = (ROOT/'deploy/server/systemd'/name).read_text(encoding='utf-8')
            self.assertIn('WorkingDirectory=@HOME@/current/Beacon', content)
            self.assertIn('-m beacon_engine.', content)
            self.assertNotIn('/Quill', content)
