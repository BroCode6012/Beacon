"""Each Flet app claims its own taskbar button.

Quill, Beacon and Vault all draw through the one flet.exe in Quill's venv, so Windows
stacked them under a single button (2026-10-01). These tests run a real throwaway child
process with a real window, and check that the helper finds it through the process tree
and that the ID it writes is the one Windows reads back.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import unittest

from beacon_engine import taskbar_identity as ti

_WINDOW_CHILD = textwrap.dedent("""
    import sys, time, win32gui, win32con, win32api
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = sys.argv[1]
    wc.lpfnWndProc = {win32con.WM_DESTROY: lambda *a: 0}
    wc.hInstance = win32api.GetModuleHandle(None)
    win32gui.RegisterClass(wc)
    win32gui.CreateWindow(sys.argv[1], "taskbar-identity-test", win32con.WS_OVERLAPPEDWINDOW,
                          0, 0, 50, 50, 0, 0, wc.hInstance, None)
    print("ready", flush=True)
    time.sleep(30)
""")


@unittest.skipUnless(sys.platform == "win32", "taskbar IDs are a Windows feature")
class TaskbarIdentityTests(unittest.TestCase):
    def setUp(self):
        self.child = subprocess.Popen(
            [sys.executable, "-c", _WINDOW_CHILD, ti.FLET_WINDOW_CLASS],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.child.stdout.readline().strip(), "ready")

    def tearDown(self):
        self.child.kill()
        self.child.wait()

    def _child_windows(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pids = ti._descendant_pids(__import__("os").getpid())
            hits = ti._flet_windows(pids)
            if hits:
                return pids, hits
            time.sleep(0.1)
        self.fail("the child's Flet-class window was never found")

    def test_finds_a_flet_window_in_a_child_process(self):
        pids, hits = self._child_windows()
        self.assertIn(self.child.pid, pids)
        self.assertEqual(len(hits), 1)

    def test_stamped_id_reads_back(self):
        _, (hwnd,) = self._child_windows()
        self.assertIsNone(ti.window_app_id(hwnd))
        ti.stamp_window(hwnd, "BroCode.Test")
        self.assertEqual(ti.window_app_id(hwnd), "BroCode.Test")

    def test_claim_stamps_the_window_in_the_background(self):
        _, (hwnd,) = self._child_windows()
        thread = ti.claim_taskbar_button("BroCode.ClaimTest", timeout=5)
        thread.join(6)
        self.assertEqual(ti.window_app_id(hwnd), "BroCode.ClaimTest")


if __name__ == "__main__":
    unittest.main()
