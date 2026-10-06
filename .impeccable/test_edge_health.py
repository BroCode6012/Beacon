"""Prove the laptop now calls a silent cloud pipeline an ERROR, not a success.

Replays the real 2026-07-20 outage shape against an isolated sync root, so the
15 GB canonical index and the live edge_sync folder are never touched.
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\Youtube\Beacon")


def ago(hours: float) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return stamp.isoformat().replace("+00:00", "Z")


def scenario(root: Path, name: str, remote: dict, ledger: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    (root / "remote_status.json").write_text(json.dumps(remote), encoding="utf-8")
    (root / "edge_health.json").write_text(json.dumps(ledger), encoding="utf-8")
    os.environ["SIGNAL_EDGE_SYNC_ROOT"] = str(root)
    os.environ["SIGNAL_EDGE_SIGNING_KEY"] = "test-key-not-real"
    import importlib
    from beacon_engine.edge import sync as sync_mod
    importlib.reload(sync_mod)
    health = sync_mod.edge_sync_status()["health"]
    print(f"\n--- {name} ---")
    print(f"  state   : {health['state']}")
    print(f"  summary : {health['summary']}")
    for extra in health["problems"][1:]:
        print(f"            {extra}")
    return health


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # 1. What the laptop saw for 17 days: worker claims "running", frozen stamp.
        h = scenario(
            base / "outage",
            "the real 2026-07-20 outage (frozen 'running' status)",
            {
                "state": "running",
                "message": "collecting 125 queries for radar-discovery",
                "updated_at": "2026-07-20T16:58:46Z",
                "node_id": "oracle-casablanca-1",
            },
            {"last_channels_at": "2026-07-20T17:00:00Z", "zero_channel_syncs": 0},
        )
        if h["state"] != "error":
            failures.append("outage was not reported as an error")

        # 2. Bundles arriving but empty - the "0 channels several times" shape.
        h = scenario(
            base / "dry",
            "bundles keep arriving with 0 channels",
            {"state": "running", "message": "scheduler pass complete", "updated_at": ago(0.1)},
            {"last_channels_at": ago(1), "zero_channel_syncs": 6},
        )
        if h["state"] != "error":
            failures.append("zero-channel streak was not reported as an error")

        # 3. Healthy pipeline must stay quiet - no false alarms.
        h = scenario(
            base / "healthy",
            "healthy pipeline (must NOT alarm)",
            {"state": "running", "message": "scheduler pass complete", "updated_at": ago(0.05)},
            {"last_channels_at": ago(0.2), "zero_channel_syncs": 0},
        )
        if h["state"] != "ok":
            failures.append("healthy pipeline raised a false alarm")

        # 4. Not configured - must be silent, not a scary error. Called directly:
        # the real .env repopulates the signing key whenever the module reloads.
        from beacon_engine.edge import sync as sync_mod
        h = sync_mod._edge_health(configured=False, remote={}, ledger={})
        print("\n--- cloud not configured (must stay silent) ---")
        print(f"  state   : {h['state']}")
        print(f"  summary : {h['summary']}")
        if h["state"] != "off":
            failures.append("unconfigured cloud did not report 'off'")

        # 5. A never-delivering pipeline must trip even with no delivery on record.
        h = scenario(
            base / "never",
            "monitoring started but nothing ever delivered",
            {"state": "running", "message": "collecting", "updated_at": ago(0.05)},
            {"first_seen_at": ago(30)},
        )
        if h["state"] != "error":
            failures.append("never-delivering pipeline was not reported as an error")

    print()
    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1
    print("PASS: silence now reads as an error; healthy stays quiet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
