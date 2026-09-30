"""Contracts for the Muse link caretaker (muse_link).

Every run must fix exactly what is down and nothing else: a woken laptop must not lose its
address to an impatient restart, a new address must be announced once (and without the key),
and the door must not be opened before the data it serves.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import muse_link as ml


class _World:
    """What the caretaker sees: which pieces are up, and what it was asked to start."""

    def __init__(self, finder=True, gateway=True, tunnel_pid=0, public=True):
        self.finder, self.gateway, self.public = finder, gateway, public
        self.tunnel_pid = tunnel_pid
        self.started: list[str] = []
        self.killed: list[int] = []
        self.notes: list[tuple[str, str]] = []
        self.next_url = "https://brand-new-road.trycloudflare.com"

    def patches(self):
        return {
            "finder_up": lambda: self.finder,
            "gateway_up": lambda: self.gateway,
            "public_up": lambda url: self.public,
            "pid_alive": lambda pid: bool(pid) and pid == self.tunnel_pid,
            "start_finder": lambda: self.started.append("finder"),
            "start_gateway": lambda: self.started.append("gateway"),
            "start_tunnel": self._start_tunnel,
            "kill": self.killed.append,
            "notify": lambda title, body: self.notes.append((title, body)),
            "wait_until": lambda check, seconds, every=2.0: check(),
        }

    def _start_tunnel(self):
        self.started.append("tunnel")
        self.tunnel_pid = 4242
        return 4242, self.next_url


class CaretakerTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="muse-link-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, value in (("STORAGE", tmp), ("STATE_FILE", tmp / "muse_link.json"),
                            ("LOG_FILE", tmp / "muse_link.log"),
                            ("TUNNEL_LOG", tmp / "muse_tunnel.log")):
            p = mock.patch.object(ml, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, world: _World) -> dict:
        with mock.patch.multiple(ml, **world.patches()):
            return ml.run_once()

    # -- the order of things -------------------------------------------------------------
    def test_the_door_is_not_opened_before_the_data_it_serves(self):
        world = _World(finder=False, gateway=False)
        state = self.run_with(world)
        self.assertEqual(world.started, ["finder"])
        self.assertIn("starting", state["status"])

    def test_a_closed_door_is_reopened(self):
        world = _World(gateway=False)
        comes_up_once_started = {"gateway_up": lambda: "gateway" in world.started}
        with mock.patch.multiple(ml, **dict(world.patches(), **comes_up_once_started)):
            ml.run_once()
        self.assertIn("gateway", world.started)

    # -- the road ------------------------------------------------------------------------
    def test_first_run_opens_a_road_and_announces_its_address_once(self):
        world = _World()
        state = self.run_with(world)
        self.assertEqual(world.started, ["tunnel"])
        self.assertEqual(state["url"], world.next_url)
        self.assertEqual(state["status"], "online")
        self.assertEqual(len(world.notes), 1)
        self.assertIn(world.next_url, world.notes[0][1])
        self.run_with(world)                              # the next run: nothing new to say
        self.assertEqual(len(world.notes), 1)
        self.assertEqual(world.started, ["tunnel"])       # and nothing restarted

    def test_a_road_that_just_woke_up_gets_time_before_being_replaced(self):
        world = _World()
        self.run_with(world)                              # road open, address announced
        world.public = False                              # laptop just woke, still offline
        for attempt in (1, 2):
            state = self.run_with(world)
            self.assertEqual(state["failures"], attempt)
            self.assertEqual(world.started, ["tunnel"])   # not replaced yet
        world.public = True                               # it reconnected by itself
        state = self.run_with(world)
        self.assertEqual(state["failures"], 0)
        self.assertEqual(world.started, ["tunnel"])       # same road, same address
        self.assertEqual(len(world.notes), 1)             # nobody told to change anything

    def test_a_road_that_stays_dead_is_replaced_and_the_new_address_announced(self):
        world = _World()
        self.run_with(world)
        first_url = world.next_url
        world.public = False
        self.run_with(world)
        self.run_with(world)
        world.next_url = "https://second-road.trycloudflare.com"
        world.public = True                               # the NEW road answers
        # third failure is judged before the new road exists, so fail the old one once more
        with mock.patch.multiple(ml, **dict(world.patches(),
                                            public_up=lambda url: url != first_url)):
            state = ml.run_once()
        self.assertEqual(world.started, ["tunnel", "tunnel"])
        self.assertEqual(state["url"], "https://second-road.trycloudflare.com")
        self.assertEqual(state["previous_url"], first_url)
        self.assertEqual(len(world.notes), 2)

    def test_after_a_reboot_the_dead_road_is_replaced_at_once(self):
        world = _World()
        self.run_with(world)
        world.tunnel_pid = 0                              # reboot: the process is gone
        world.next_url = "https://after-reboot.trycloudflare.com"
        state = self.run_with(world)
        self.assertEqual(world.started, ["tunnel", "tunnel"])
        self.assertEqual(state["url"], "https://after-reboot.trycloudflare.com")

    # -- what reaches Muse ---------------------------------------------------------------
    def test_the_message_for_muse_carries_the_address_but_never_the_key(self):
        with mock.patch.object(ml, "gateway_key", lambda: "SECRET-KEY-123"):
            text = ml.message_for_muse("https://x.trycloudflare.com")
        self.assertIn("https://x.trycloudflare.com", text)
        self.assertNotIn("SECRET-KEY-123", text)
        self.assertIn("same", text)                       # Muse keeps the key it has

    def test_the_address_is_read_from_the_newest_line_of_the_road_log(self):
        ml.TUNNEL_LOG.write_text(
            "INF https://old-one.trycloudflare.com\nINF Registered tunnel connection\n"
            "INF |  https://new-one.trycloudflare.com  |\n", encoding="utf-8")
        self.assertEqual(ml.tunnel_url_from_log(), "https://new-one.trycloudflare.com")


if __name__ == "__main__":
    unittest.main()


class IdentityTests(unittest.TestCase):
    def test_every_check_names_the_caretaker(self):
        seen = {}

        class _Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout):
            seen.update(request.headers)
            return _Response()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(ml._answers("http://127.0.0.1:8770/health", "k", 5))
        self.assertEqual(seen.get("User-agent"), ml.USER_AGENT)

    def test_the_new_address_message_warns_the_key_is_asked_again(self):
        text = ml.message_for_muse("https://x.trycloudflare.com")
        self.assertIn("key", text)
        self.assertIn("secure page", text)


class DeletedRoadTests(CaretakerTests):
    """2026-09-28: after an 11-minute sleep Cloudflare had deleted the quick tunnel; the
    still-running cloudflared logged 'Unauthorized: Tunnel not found' every ~30s, forever."""

    def test_a_deleted_road_is_recognised_from_its_log(self):
        ml.TUNNEL_LOG.write_text(
            "INF Registered tunnel connection connIndex=0\n"
            "ERR Register tunnel error from server side error=\"Unauthorized: Tunnel not found\"\n",
            encoding="utf-8")
        self.assertTrue(ml.tunnel_gone())
        ml.TUNNEL_LOG.write_text(
            "ERR Register tunnel error from server side error=\"Unauthorized: Tunnel not found\"\n"
            "INF Registered tunnel connection connIndex=0\n", encoding="utf-8")
        self.assertFalse(ml.tunnel_gone())                  # it recovered after that
        ml.TUNNEL_LOG.write_text("INF Registered tunnel connection\n", encoding="utf-8")
        self.assertFalse(ml.tunnel_gone())

    def test_a_deleted_road_is_replaced_at_once_not_after_three_rounds(self):
        world = _World()
        self.run_with(world)                                # road open, address announced
        world.public = False
        world.next_url = "https://after-sleep.trycloudflare.com"
        with mock.patch.multiple(ml, **dict(world.patches(), tunnel_gone=lambda: True,
                                            public_up=lambda url: "after-sleep" in url)):
            state = ml.run_once()
        self.assertEqual(world.started, ["tunnel", "tunnel"])   # replaced on the FIRST check
        self.assertEqual(state["url"], "https://after-sleep.trycloudflare.com")
        self.assertEqual(state["status"], "online")
