"""Prove a hung edge worker now exits non-zero instead of hanging forever.

Simulates the 2026-07-20 failure: run_once() blocks on a socket read that never
returns. Before the fix the process stayed alive indefinitely and systemd's
Restart=on-failure never fired. It must now exit EDGE_STALL_EXIT_CODE.
"""
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

CHILD = r'''
import sys, threading, time
sys.path.insert(0, r"{scripter}")
from scripter.edge.worker import EdgeWorker, EdgeWorkerPaths, EDGE_STALL_EXIT_CODE

paths = EdgeWorkerPaths.under(r"{root}")
w = EdgeWorker(paths=paths, signing_key="test-key", node_id="stall-test")

def hang(*a, **k):
    # exactly the 20 July shape: a thread parked forever with nothing raised
    threading.Event().wait()

w.run_once = hang
w.run_forever(poll_seconds=1, stall_seconds=120)   # clamped to a 120s floor
'''


def main() -> int:
    scripter = r"D:\Youtube\Quill"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "edge"
        root.mkdir(parents=True, exist_ok=True)
        code = CHILD.format(scripter=scripter, root=str(root).replace("\\", "\\\\"))
        script = Path(tmp) / "child.py"
        script.write_text(code, encoding="utf-8")

        print("starting a worker whose run_once() hangs forever ...")
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            out, _ = proc.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("FAIL: still hanging after 240s - watchdog did not fire")
            return 1

        print(f"exit code   : {proc.returncode}")
        print(f"stderr says : {(out or '').strip()[:200]}")

        status = root / "worker_status.json"
        if status.exists():
            payload = json.loads(status.read_text(encoding="utf-8"))
            print(f"status state: {payload.get('state')}")
            print(f"status msg  : {payload.get('message')}")

        if proc.returncode == 75:
            print("\nPASS: hung worker exited 75 -> systemd Restart=on-failure fires")
            return 0
        print("\nFAIL: expected exit code 75")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
