"""Beacon on a server: the scraper is a system service, and the cloud collector's finds are
imported on the same machine (deploy/server/). Checks the control logic and the service files."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sweep_control as sc

HERE = Path(__file__).resolve().parent
SERVER = HERE / "deploy" / "server"
UNITS = SERVER / "systemd"


class ServerModeControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beacon-server-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in ("STOP_PATH", "PAUSE_PATH", "PID_PATH"):
            patcher = mock.patch.object(sc, name, self.tmp / getattr(sc, name).name)
            patcher.start()
            self.addCleanup(patcher.stop)
        unit = mock.patch.object(sc, "RADAR_UNIT", "beacon-radar.service")
        unit.start()
        self.addCleanup(unit.stop)
        self.calls = []

        def fake_run(cmd, **_kw):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        run = mock.patch.object(sc.subprocess, "run", side_effect=fake_run)
        run.start()
        self.addCleanup(run.stop)
        popen = mock.patch.object(sc.subprocess, "Popen")
        self.popen = popen.start()
        self.addCleanup(popen.stop)

    def test_start_asks_the_service_and_clears_the_pause(self):
        sc.PAUSE_PATH.write_text("paused by user")
        sc.STOP_PATH.write_text("stop")
        self.assertTrue(sc.start_worker())
        self.assertEqual(self.calls, [["systemctl", "start", "--no-block",
                                       "beacon-radar.service"]])
        self.assertFalse(sc.PAUSE_PATH.exists())
        self.assertFalse(sc.STOP_PATH.exists())
        self.popen.assert_not_called()                  # never a second, unmanaged worker

    def test_keep_alive_respects_a_deliberate_stop(self):
        sc.PAUSE_PATH.write_text("paused by user")
        self.assertFalse(sc.start_worker(clear_stop=False))
        self.assertEqual(self.calls, [])

    def test_already_running_is_left_alone(self):
        with mock.patch.object(sc, "is_running", return_value=True):
            self.assertFalse(sc.start_worker())
        self.assertEqual(self.calls, [])

    def test_stop_is_the_same_polite_request_as_on_the_laptop(self):
        sc.stop_worker()
        self.assertTrue(sc.STOP_PATH.exists())
        self.assertTrue(sc.PAUSE_PATH.exists())
        self.assertEqual(self.calls, [])

    def test_autostart_reads_the_service_not_windows(self):
        self.assertTrue(sc.autostart_installed())
        self.assertEqual(self.calls, [["systemctl", "is-enabled", "--quiet",
                                       "beacon-radar.service"]])
        self.assertFalse(sc.set_autostart(False))       # boot-start is the machine owner's call
        self.assertEqual(len(self.calls), 1)            # and no PowerShell was attempted

    def test_laptop_mode_is_unchanged(self):
        with mock.patch.object(sc, "RADAR_UNIT", ""):
            self.assertTrue(sc.start_worker())
        self.popen.assert_called_once()
        self.assertFalse(any(c[0] == "systemctl" for c in self.calls))


class ServiceFileTests(unittest.TestCase):
    def test_every_placeholder_is_one_the_installer_fills(self):
        install = (SERVER / "install.sh").read_text(encoding="utf-8")
        filled = set(re.findall(r"s#(@[A-Z]+@)#", install))
        files = list(UNITS.glob("*.service")) + list(UNITS.glob("*.timer"))
        files.append(SERVER / "polkit" / "50-beacon-radar.rules")
        for path in files:
            for placeholder in set(re.findall(r"@[A-Z]+@", path.read_text(encoding="utf-8"))):
                self.assertIn(placeholder, filled, f"{path.name}: {placeholder}")

    def test_paused_scraper_stays_stopped_after_a_reboot(self):
        unit = (UNITS / "beacon-radar.service").read_text(encoding="utf-8")
        self.assertIn("ExecCondition=", unit)
        self.assertIn(sc.PAUSE_PATH.name, unit)
        self.assertIn("Restart=on-failure", unit)       # a clean Stop is not restarted

    def test_every_beacon_service_gets_youtube_keys_and_none_the_signing_key(self):
        # The data service, the screens and Muse's door call YouTube too (live refreshes,
        # saving a channel, resolving a video link) — before 2026-09-29 only the scraper had keys.
        for name in ("beacon-finder", "beacon-web", "beacon-gateway", "beacon-radar"):
            unit = (UNITS / f"{name}.service").read_text(encoding="utf-8")
            self.assertIn(f"deploy/server/youtube-keys @KEYS@ /run/{name}/youtube.env", unit)
            self.assertIn(f"EnvironmentFile=-/run/{name}/youtube.env", unit)
            self.assertNotIn("EnvironmentFile=@KEYS@", unit, name)

    def test_scraper_gets_youtube_keys_but_not_the_signing_key(self):
        unit = (UNITS / "beacon-radar.service").read_text(encoding="utf-8")
        self.assertIn("deploy/server/youtube-keys @KEYS@ /run/beacon-radar/youtube.env", unit)
        self.assertNotIn("EnvironmentFile=@KEYS@", unit)

    def test_cloud_import_runs_the_engine_through_the_local_stand_in(self):
        unit = (UNITS / "beacon-edge-sync.service").read_text(encoding="utf-8")
        self.assertIn("SIGNAL_EDGE_SSH_BIN=@HOME@/current/Beacon/deploy/server/local-ssh", unit)
        self.assertIn("SIGNAL_EDGE_SSH_HOST=127.0.0.1", unit)
        self.assertIn("-m beacon_engine.edge sync", unit)
        self.assertIn("NoNewPrivileges=yes", unit)      # why local-ssh replaces real sudo
        timer = (UNITS / "beacon-edge-sync.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitInactiveSec=", timer)      # never two imports at once

    def test_installer_never_starts_the_switch_over_services(self):
        install = (SERVER / "install.sh").read_text(encoding="utf-8")
        for line in install.splitlines():
            if re.search(r"systemctl (enable|start)\b", line):
                for unit in ("beacon-edge-sync", "beacon-radar", "beacon-gateway",
                             "beacon-tunnel"):
                    self.assertNotIn(unit, line)
        self.assertIn("for s in beacon-finder beacon-web; do", install)

    def test_muse_door_stays_local_and_the_road_points_at_it(self):
        door = (UNITS / "beacon-gateway.service").read_text(encoding="utf-8")
        self.assertIn("beacon_gateway.py", door)
        road = (UNITS / "beacon-tunnel.service").read_text(encoding="utf-8")
        self.assertIn("--url http://127.0.0.1:${BEACON_GATEWAY_PORT}", road)
        self.assertIn("--no-autoupdate", road)
        example = (HERE / "beacon_host.example.env").read_text(encoding="utf-8")
        self.assertIn("BEACON_GATEWAY_PORT=", example)

    def test_upgrades_never_delete_the_release_a_running_service_uses(self):
        install = (SERVER / "install.sh").read_text(encoding="utf-8")
        self.assertIn('readlink -f "/proc/$pid/cwd"', install)
        self.assertIn("a running service still uses it", install)
        self.assertNotIn("tail -n +4 | xargs -r rm -rf", install)

    def test_polkit_rule_allows_only_the_scraper_unit(self):
        rule = (SERVER / "polkit" / "50-beacon-radar.rules").read_text(encoding="utf-8")
        self.assertIn('action.lookup("unit") == "beacon-radar.service"', rule)
        self.assertIn('subject.user == "@USER@"', rule)

    def test_front_door_template_is_fully_filled_and_keeps_beacon_private(self):
        caddy = SERVER / "caddy"
        template = (caddy / "Caddyfile.template").read_text(encoding="utf-8")
        render = (caddy / "render_caddyfile.py").read_text(encoding="utf-8")
        for placeholder in set(re.findall(r"@[A-Z_]+@", template)):
            self.assertIn(f'"{placeholder}"', render, placeholder)
        self.assertIn("default_sni @ADDRESS@", template)      # bare-IP visits send no SNI
        self.assertIn("basic_auth", template)
        self.assertIn("reverse_proxy 127.0.0.1:@WEB_PORT@", template)
        self.assertIn('"runuser", "-u", "caddy"', render)      # validate as caddy, not root

    def test_linux_files_keep_unix_line_endings(self):
        for path in [SERVER / "local-ssh", SERVER / "youtube-keys", SERVER / "install.sh",
                     SERVER / "caddy" / "Caddyfile.template", HERE / "beacon_host.example.env",
                     *UNITS.iterdir(), *(SERVER / "polkit").iterdir()]:
            self.assertNotIn(b"\r\n", path.read_bytes(), path.name)


class WebIconTests(unittest.TestCase):
    """In the browser, Flet shows its own logo unless Beacon supplies every web icon (2026-09-29:
    the operator saw Flet's low-quality default). They must exist, be the right size, and ship:
    *.png is git-ignored for screenshots, and the server only receives what git would."""

    WANT = {"favicon.png": 32, "icons/icon-192.png": 192, "icons/icon-512.png": 512,
            "icons/icon-maskable-192.png": 192, "icons/icon-maskable-512.png": 512,
            "icons/apple-touch-icon-192.png": 192, "icons/loading-animation.png": 512}

    def test_every_web_icon_exists_at_its_size(self):
        from PIL import Image
        for name, size in self.WANT.items():
            with Image.open(HERE / "assets" / name) as im:
                self.assertEqual(im.size, (size, size), name)

    def test_the_icons_ship_with_the_code(self):
        names = [f"assets/{n}" for n in self.WANT] + ["assets/manifest.json"]
        out = subprocess.run(["git", "check-ignore", *names], cwd=HERE,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "", "git-ignored: " + out.stdout)

    def test_the_start_page_points_at_names_the_old_offline_copy_never_saw(self):
        # Flet's service worker answers favicon.png / manifest.json / icons/* from its own old
        # copies, so a browser that saw Flet's logo kept it. The page is fetched fresh and must
        # point at Beacon-only names, and it must match the installed Flet's page otherwise.
        import tools_make_icons as t
        page = (HERE / "assets" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(page, t.beacon_index_html(t.flet_index_html()),
                         "run: python tools_make_icons.py (Flet's page changed?)")
        for new in t.WEB_NAMES.values():
            self.assertIn(f'"{new}"', page)
            self.assertTrue((HERE / "assets" / new).is_file(), new)
        import json
        for icon in json.loads((HERE / "assets" / "beacon-manifest.json").read_text(
                encoding="utf-8"))["icons"]:
            self.assertTrue((HERE / "assets" / icon["src"]).is_file(), icon["src"])

    def test_the_web_app_is_named_beacon(self):
        import json
        manifest = json.loads((HERE / "assets" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["short_name"], "Beacon")


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class YoutubeKeysTests(unittest.TestCase):
    """youtube-keys: only the YouTube lines reach the scraper, plus a single key when missing."""

    def run_script(self, keys_text):
        tmp = Path(tempfile.mkdtemp(prefix="beacon-keys-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "edge.env").write_bytes(keys_text.encode())
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        out = subprocess.run(["sh", str(SERVER / "youtube-keys"), str(tmp / "edge.env"),
                              str(tmp / "youtube.env"), user], capture_output=True, timeout=30)
        return out, (tmp / "youtube.env").read_text() if (tmp / "youtube.env").exists() else ""

    def test_pool_only_file_gains_a_single_key_and_loses_the_signing_key(self):
        out, env = self.run_script("SIGNAL_EDGE_NODE_ID=node\nSIGNAL_EDGE_SIGNING_KEY=secret\n"
                                   "YOUTUBE_API_KEYS=fake-one,fake-two,fake-three\n")
        self.assertEqual(out.stdout, b"")                       # nothing printed, ever
        self.assertNotIn("SIGNING", env)
        self.assertIn("YOUTUBE_API_KEYS=fake-one,fake-two,fake-three", env)
        self.assertIn("YOUTUBE_API_KEY=fake-one\n", env)

    def test_an_existing_single_key_is_kept_not_replaced(self):
        _, env = self.run_script("YOUTUBE_API_KEYS=fake-one,fake-two\nYOUTUBE_API_KEY=fake-own\n")
        self.assertEqual(env.count("YOUTUBE_API_KEY="), 1)
        self.assertIn("YOUTUBE_API_KEY=fake-own", env)

    def test_quoted_pool(self):
        _, env = self.run_script('YOUTUBE_API_KEYS="fake-one,fake-two"\n')
        self.assertIn("YOUTUBE_API_KEY=fake-one\n", env)

    def test_no_keys_at_all_is_not_an_error(self):
        out, env = self.run_script("SIGNAL_EDGE_SIGNING_KEY=secret\n")
        self.assertEqual(env, "")


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class LocalSshTests(unittest.TestCase):
    """The stand-in for ssh: runs the transport's command here, turning `sudo -n X` into X."""

    def run_shim(self, *args, stdin=b""):
        return subprocess.run(["sh", str(SERVER / "local-ssh"), *args], input=stdin,
                              capture_output=True, timeout=30)

    def test_runs_the_command_locally_with_sudo_dropped(self):
        out = self.run_shim("-i", "key", "-p", "22", "-o", "BatchMode=yes",
                            "ubuntu@127.0.0.1", "sudo -n echo one && sudo -n echo two")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [b"one", b"two"])

    def test_passes_input_through(self):
        out = self.run_shim("ubuntu@localhost", "sudo -n cat", stdin=b"signed ack")
        self.assertEqual(out.stdout, b"signed ack")

    def test_failure_exit_code_reaches_the_transport(self):
        out = self.run_shim("ubuntu@127.0.0.1", "sudo -n false && echo never")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, b"")

    def test_refuses_any_other_machine(self):
        out = self.run_shim("-i", "key", "ubuntu@51.170.132.206", "echo should-not-run")
        self.assertEqual(out.returncode, 255)
        self.assertNotIn(b"should-not-run", out.stdout)


if __name__ == "__main__":
    unittest.main()
