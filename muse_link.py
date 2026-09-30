"""Keep the Muse link alive — safe to run every couple of minutes, fixes only what is down.

The link has three pieces, and a laptop that sleeps, reboots or crashes can knock out any of
them:

    Beacon's finder (the data) -> beacon_gateway (the read-only door) -> cloudflared (the
    public road Muse dials)

The BeaconMuseLink scheduled task (install_muse_link.ps1) runs this every 2 minutes, and
Windows runs a missed run as soon as the laptop wakes. Each run checks the three pieces,
restarts ONLY what is down, and exits within seconds. Nothing is ever started twice: the
finder steps aside for a running copy, and the road is replaced only when it is truly gone.

The road's ADDRESS is the one thing that can change, and on a laptop it changes OFTEN. A
quick tunnel lives on Cloudflare's side only while its computer stays connected: after an
11-minute sleep (2026-09-28) Cloudflare had deleted it, and the still-running cloudflared
retried forever with "Unauthorized: Tunnel not found". So a road is replaced at once when its
log says that, after a reboot, or after it fails several checks in a row. When the address
changes you get a Windows notification, and storage/muse_link.json holds a ready message for
Muse (the address only — Muse will ask for the same key again on its secure page). Only an
always-on host (the Oracle step) gives Muse an address that survives the laptop sleeping.

    pythonw muse_link.py            one quiet run (what the scheduled task does)
    python  muse_link.py --status   what is the address right now, and is it working?
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORAGE = HERE / "storage"
STATE_FILE = STORAGE / "muse_link.json"
LOG_FILE = STORAGE / "muse_link.log"
TUNNEL_LOG = STORAGE / "muse_tunnel.log"
GATEWAY_KEY_FILE = STORAGE / "muse_gateway.token.json"
FINDER_TOKENS = Path(os.getenv("FINDER_TOKEN_FILE") or (STORAGE / "finder.tokens.json"))
CLOUDFLARED = Path(os.getenv("BEACON_CLOUDFLARED") or (HERE / "tools" / "cloudflared.exe"))
GATEWAY_PORT = int(os.getenv("BEACON_GATEWAY_PORT", "8770"))
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"

# A laptop that has just woken needs a moment to get back online, and cloudflared reconnects
# on its own. Replacing the road on the first failed check would change its address for
# nothing, so it is only replaced after this many failed checks in a row (~6 min at 2-min runs).
REPLACE_AFTER_FAILURES = 3

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
VERBOSE = False


def _pythonw() -> str:
    # sys.prefix is the venv even when the venv launcher re-executed the base interpreter,
    # and only the venv's interpreter has the engine's packages (same rule as beacon_app).
    candidate = Path(sys.prefix) / "Scripts" / "pythonw.exe"
    return str(candidate if candidate.exists() else sys.executable)


def log(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        STORAGE.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass
    if VERBOSE:
        print(f"[{stamp}] {line}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STORAGE.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# -- health checks ---------------------------------------------------------------------------
# The door writes down every visit; this name lets it leave out the caretaker's own
# 2-minute checks, so its log shows what Muse actually did (two lines every run buried it).
USER_AGENT = "beacon-muse-link"


def _answers(url: str, token: str, timeout: float) -> bool:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:                                           # noqa: BLE001 — down is down
        return False


def gateway_key() -> str:
    try:
        return json.loads(GATEWAY_KEY_FILE.read_text(encoding="utf-8"))["token"]
    except (OSError, ValueError, KeyError):
        return ""


def finder_up() -> bool:
    try:
        data = json.loads(FINDER_TOKENS.read_text(encoding="utf-8"))
        return _answers(data["url"].rstrip("/") + "/health", data["read"], 5)
    except (OSError, ValueError, KeyError):
        return False


def gateway_up() -> bool:
    key = gateway_key()
    return bool(key) and _answers(f"{GATEWAY_URL}/health", key, 5)


def public_up(url: str) -> bool:
    """The real test: does the PUBLIC address reach Beacon, the way Muse reaches it?"""
    key = gateway_key()
    return bool(url and key) and _answers(f"{url}/health", key, 20)


def wait_until(check, seconds: float, every: float = 2.0) -> bool:
    deadline = time.time() + seconds
    while True:
        if check():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(every)


# -- processes -------------------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    """Win32 liveness probe (os.kill(pid, 0) is CTRL_C_EVENT on Windows — not a probe)."""
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, int(pid))       # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        k32.CloseHandle(handle)


def _spawn(args: list[str], stderr=subprocess.DEVNULL) -> int:
    """Start a background process that outlives this run and never shows a window."""
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    proc = subprocess.Popen(args, cwd=str(HERE), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=stderr,
                            creationflags=flags, close_fds=True)
    return proc.pid


def start_finder() -> None:
    # finder_service checks /health first and steps aside for a running copy.
    _spawn([_pythonw(), str(HERE / "finder_service.py")])


def start_gateway() -> None:
    _spawn([_pythonw(), str(HERE / "beacon_gateway.py")])


def other_tunnels(keep_pid: int = 0) -> list[int]:
    """cloudflared processes pointed at OUR door that we did not start (orphans, old copies)."""
    if os.name != "nt":
        return []
    query = ("Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
             f"Where-Object {{ $_.CommandLine -match '127.0.0.1:{GATEWAY_PORT}' }} | "
             "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:                                           # noqa: BLE001
        return []
    return [int(p) for p in out.split() if p.isdigit() and int(p) != keep_pid]


def kill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def tunnel_gone() -> bool:
    """Has Cloudflare deleted our road? Then waiting for it to reconnect is pointless.

    Seen live after an 11-minute sleep: cloudflared, still running, logged
    'Unauthorized: Tunnel not found' every ~30s and would have done so forever.
    Gone = that error is newer than the last successful registration.
    """
    try:
        text = TUNNEL_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return text.rfind("Tunnel not found") > text.rfind("Registered tunnel connection")


def tunnel_url_from_log() -> str:
    try:
        found = URL_RE.findall(TUNNEL_LOG.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return found[-1] if found else ""


def start_tunnel() -> tuple[int, str]:
    """A fresh road: clear away any old one pointed at our door, then open a new one."""
    for pid in other_tunnels():
        log(f"closing an old road (pid {pid})")
        kill(pid)
    if not CLOUDFLARED.exists():
        log(f"cloudflared is missing at {CLOUDFLARED} — cannot open the road")
        return 0, ""
    STORAGE.mkdir(parents=True, exist_ok=True)
    handle = open(TUNNEL_LOG, "w", encoding="utf-8")     # fresh log: its only URL is this one
    pid = _spawn([str(CLOUDFLARED), "tunnel", "--url", GATEWAY_URL, "--no-autoupdate"],
                 stderr=handle)
    handle.close()                                       # the child keeps its own copy
    url = ""
    wait_until(lambda: bool(tunnel_url_from_log()), 45, every=1.5)
    url = tunnel_url_from_log()
    return pid, url


# -- telling the operator --------------------------------------------------------------------
_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [System.Security.SecurityElement]::Escape($env:BEACON_TOAST_TITLE)
$b = [System.Security.SecurityElement]::Escape($env:BEACON_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show((New-Object Windows.UI.Notifications.ToastNotification $xml))
"""


def notify(title: str, body: str) -> None:
    """A Windows notification — best effort: the log and the state file are the record."""
    if os.name != "nt":
        return
    env = dict(os.environ, BEACON_TOAST_TITLE=title, BEACON_TOAST_BODY=body)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", _TOAST], env=env,
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                           # noqa: BLE001
        pass


def message_for_muse(url: str) -> str:
    """What to tell Muse when the address changes. The address only — never the key.

    Muse ties a saved key to the exact address it was saved for (verified 2026-09-28), so it
    asks for the same key again on its secure page; the operator pastes it there.
    """
    return (f"Beacon's address changed. The new base URL is {url} and the key is the same. "
            "Please update the Beacon connector to use this address from now on; ask me for "
            "the key on your secure page if you need it again, then call GET /health to "
            "confirm it works.")


# -- one run ---------------------------------------------------------------------------------
def run_once() -> dict:
    state = load_state()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["checked_at"] = now

    # 1. the data. The door cannot start without it, so a slow finder ends this run.
    if not finder_up():
        log("Beacon's data service was down — starting it")
        start_finder()
        if not wait_until(finder_up, 90):
            log("the data service is still starting — the next run carries on")
            state["status"] = "starting Beacon's data service"
            save_state(state)
            return state

    # 2. the door.
    if not gateway_up():
        log("the door was closed — opening it")
        start_gateway()
        if not wait_until(gateway_up, 30):
            log("the door did not open — the next run tries again")
            state["status"] = "door not answering"
            save_state(state)
            return state

    # 3. the road — judged from the outside, the way Muse sees it.
    pid, url = int(state.get("tunnel_pid") or 0), str(state.get("url") or "")
    running = pid_alive(pid) and bool(url)
    if running and public_up(url):
        state["failures"] = 0
        state["last_ok"] = now
    else:
        gone = running and tunnel_gone()
        failures = (int(state.get("failures") or 0) + 1 if running and not gone
                    else REPLACE_AFTER_FAILURES)
        state["failures"] = failures
        if gone:
            log("Cloudflare deleted the road while the laptop was offline — replacing it now")
        if failures < REPLACE_AFTER_FAILURES:
            log(f"the road did not answer ({failures}/{REPLACE_AFTER_FAILURES}) — waiting, it "
                "usually reconnects by itself after sleep")
        else:
            why = ("Cloudflare no longer knows it" if gone
                   else "the road failed several checks in a row" if running
                   else "no road was running")
            log(f"opening a new road: {why}")
            if running:
                kill(pid)
            new_pid, new_url = start_tunnel()
            state.update(tunnel_pid=new_pid, url=new_url or url, failures=0)
            if new_url and wait_until(lambda: public_up(new_url), 60, every=4):
                state["last_ok"] = now
            else:
                log("the new road is not answering yet — the next run checks it")

    # 4. a working address Muse has not been told about yet.
    url = str(state.get("url") or "")
    if url and state.get("last_ok") == now and state.get("announced_url") != url:
        state["previous_url"] = state.get("announced_url", "")
        state["announced_url"] = url
        state["changed_at"] = now
        state["message_for_muse"] = message_for_muse(url)
        log(f"new address: {url}")
        notify("Beacon: Muse needs the new address",
               f"{url}  -  tell Muse, then paste the same key when it asks")

    state["status"] = "online" if state.get("last_ok") == now else "checking"
    save_state(state)
    return state


def main() -> int:
    global VERBOSE
    if "--status" in sys.argv:
        state = load_state()
        url = state.get("url", "")
        working = bool(url) and public_up(url)
        print(f"Muse link: {'WORKING' if working else 'NOT answering right now'}")
        print(f"Address:   {url or '(none yet)'}")
        print(f"Last OK:   {state.get('last_ok', 'never')}")
        if state.get("message_for_muse"):
            print(f"\nIf Muse still has the old address, send it this:\n\n"
                  f"{state['message_for_muse']}")
        return 0 if working else 1
    VERBOSE = "-v" in sys.argv
    try:
        run_once()
    except Exception as exc:                                    # noqa: BLE001
        log(f"run failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
