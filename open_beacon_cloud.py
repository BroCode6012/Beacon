"""Open the LIVE Beacon — the one running on the server — in an app window on this laptop.

After the switch-over the library lives on the server, and the laptop's own copy stops growing.
Beacon's screens on the server listen only on the server itself (127.0.0.1), never on the
internet, so this opens a private SSH tunnel (the same key the cloud sync always used) and then
shows those screens in a browser app window:

    laptop 127.0.0.1:8571  ── ssh ──>  server 127.0.0.1:8570 (Beacon's web screens)

Nothing is exposed publicly; without the key nobody else can reach the screens. Run with
pythonw (no console window). A problem is shown in a message box, never swallowed.

Settings (environment or Beacon's .env-free defaults): BEACON_CLOUD_SSH (user@host),
BEACON_CLOUD_KEY (private key file), BEACON_CLOUD_LOCAL_PORT, BEACON_CLOUD_REMOTE_PORT.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = os.getenv("BEACON_CLOUD_SSH", "ubuntu@51.170.132.206")
KEY = os.getenv("BEACON_CLOUD_KEY", str(Path.home() / ".ssh" / "signal_edge_oracle_ed25519"))
LOCAL_PORT = int(os.getenv("BEACON_CLOUD_LOCAL_PORT", "8571"))
REMOTE_PORT = int(os.getenv("BEACON_CLOUD_REMOTE_PORT", "8570"))
URL = f"http://127.0.0.1:{LOCAL_PORT}/"
ENGINE = Path(os.getenv("FACELESS_ENGINE_DIR") or (HERE.parent / "Quill"))
HIDDEN = 0x08000000 if os.name == "nt" else 0                   # CREATE_NO_WINDOW
DETACHED = (0x00000008 | HIDDEN) if os.name == "nt" else 0       # + DETACHED_PROCESS: outlives us


def ssh_binary() -> str:
    windows = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
    return str(windows) if windows.exists() else (shutil.which("ssh") or "ssh")


def screens_answer(timeout: float = 3.0) -> bool:
    """True when Beacon's web screens answer through the tunnel (not merely an open port)."""
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def port_taken() -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", LOCAL_PORT)) == 0


def open_tunnel() -> subprocess.Popen:
    """A background ssh that only forwards the one port — no shell, no remote command."""
    return subprocess.Popen(
        [ssh_binary(), "-i", KEY, "-N", "-L", f"{LOCAL_PORT}:127.0.0.1:{REMOTE_PORT}",
         "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
         "-o", "ServerAliveCountMax=4", "-o", "ConnectTimeout=15", TARGET],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=DETACHED, close_fds=True)


def refresh_access() -> None:
    """The server's firewall admits SSH only from this laptop's current internet address.
    BeaconOracleAccess refreshes that every 5 min; do it now too in case the address just
    changed (the updater is the engine's standalone script, its settings in the engine .env)."""
    updater = ENGINE / "scripter" / "edge" / "oci_allowlist.py"
    python = ENGINE / "storage" / "oci_updater_venv" / "Scripts" / "python.exe"
    if updater.exists() and python.exists():
        subprocess.run([str(python), str(updater), "--status",
                        str(ENGINE / "storage" / "edge_sync" / "oci_allowlist_status.json")],
                       capture_output=True, timeout=120, creationflags=HIDDEN)


def connect(wait_seconds: int = 30) -> str | None:
    """Tunnel up and the screens answering → None; otherwise a plain-words reason."""
    if screens_answer():
        return None
    if port_taken():
        return (f"Something on this laptop already uses port {LOCAL_PORT} but it is not "
                "Beacon. Close it (or set BEACON_CLOUD_LOCAL_PORT) and try again.")
    proc = open_tunnel()
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if screens_answer():
            return None
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            return err or f"ssh stopped with code {proc.returncode}"
        time.sleep(1)
    return "The server did not answer in time — it may be restarting; try again in a minute."


def open_window() -> None:
    """An app-style window (no tabs/address bar) when Chrome or Edge is installed."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for browser in candidates:
        if browser.is_file():
            subprocess.Popen([str(browser), f"--app={URL}", "--window-size=1280,860"],
                             creationflags=DETACHED, close_fds=True)
            return
    webbrowser.open(URL)


def tell(message: str) -> None:
    if os.name == "nt" and "pythonw" in Path(sys.executable).name.lower():
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Beacon (cloud)", 0x10)
    else:
        print(message, file=sys.stderr)


def main() -> int:
    if not Path(KEY).is_file():
        tell(f"The server key is missing: {KEY}")
        return 2
    problem = connect()
    if problem and ("Permission denied" in problem or "timed out" in problem.lower()
                    or "Connection refused" in problem or "Connection reset" in problem):
        refresh_access()                       # this laptop's address may just have changed
        problem = connect()
    if problem:
        tell("Could not open the live Beacon on the server.\n\n" + problem)
        return 1
    open_window()
    return 0


if __name__ == "__main__":
    sys.exit(main())
