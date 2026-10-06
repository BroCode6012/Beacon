"""The laptop's one-click door to the live Beacon on the server (open_beacon_cloud.py)."""

from __future__ import annotations

import io
import unittest
from unittest import mock

import open_beacon_cloud as obc


class FakeSsh:
    def __init__(self, exit_code=None, stderr=b""):
        self.returncode = exit_code
        self.stderr = io.BytesIO(stderr)

    def poll(self):
        return self.returncode


class ConnectTests(unittest.TestCase):
    def test_reuses_a_tunnel_that_already_works(self):
        with mock.patch.object(obc, "screens_answer", return_value=True), \
                mock.patch.object(obc, "open_tunnel") as tunnel:
            self.assertIsNone(obc.connect())
        tunnel.assert_not_called()                   # never a second ssh for the same port

    def test_opens_a_tunnel_and_waits_for_the_screens(self):
        answers = iter([False, False, True])
        with mock.patch.object(obc, "screens_answer", side_effect=lambda *a: next(answers)), \
                mock.patch.object(obc, "port_taken", return_value=False), \
                mock.patch.object(obc, "open_tunnel", return_value=FakeSsh()), \
                mock.patch.object(obc.time, "sleep"):
            self.assertIsNone(obc.connect())

    def test_reports_why_ssh_gave_up(self):
        with mock.patch.object(obc, "screens_answer", return_value=False), \
                mock.patch.object(obc, "port_taken", return_value=False), \
                mock.patch.object(obc, "open_tunnel",
                                  return_value=FakeSsh(255, b"Permission denied (publickey).")):
            self.assertIn("Permission denied", obc.connect())

    def test_a_foreign_program_on_the_port_is_named_not_hijacked(self):
        with mock.patch.object(obc, "screens_answer", return_value=False), \
                mock.patch.object(obc, "port_taken", return_value=True), \
                mock.patch.object(obc, "open_tunnel") as tunnel:
            self.assertIn("already uses port", obc.connect())
        tunnel.assert_not_called()

    def test_refreshes_this_laptops_address_then_retries_once(self):
        results = iter(["ssh: connect to host x port 22: Connection timed out", None])
        with mock.patch.object(obc, "connect", side_effect=lambda: next(results)), \
                mock.patch.object(obc, "refresh_access") as refresh, \
                mock.patch.object(obc, "open_window") as window, \
                mock.patch.object(obc.Path, "is_file", return_value=True):
            self.assertEqual(obc.main(), 0)
        refresh.assert_called_once()
        window.assert_called_once()

    def test_failure_is_shown_not_swallowed(self):
        with mock.patch.object(obc, "connect", return_value="the server did not answer"), \
                mock.patch.object(obc, "refresh_access"), \
                mock.patch.object(obc, "tell") as tell, \
                mock.patch.object(obc, "open_window") as window, \
                mock.patch.object(obc.Path, "is_file", return_value=True):
            self.assertEqual(obc.main(), 1)
        self.assertIn("did not answer", tell.call_args[0][0])
        window.assert_not_called()

    def test_tunnel_forwards_only_the_screens_port_with_no_remote_command(self):
        with mock.patch.object(obc.subprocess, "Popen") as popen:
            obc.open_tunnel()
        argv = popen.call_args[0][0]
        self.assertIn("-N", argv)
        self.assertIn(f"{obc.LOCAL_PORT}:127.0.0.1:{obc.REMOTE_PORT}", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-1], obc.TARGET)


if __name__ == "__main__":
    unittest.main()
