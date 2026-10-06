"""Laptop-side inbox status and safe import for remote edge bundles."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beacon_engine.edge.importer import (
    DEFAULT_IMPORT_TRANSACTION_BUNDLES,
    DISCOVERY_BUNDLE_KIND,
    ORGANIZATION_BUNDLE_KIND,
    BundleImportSession,
)
from beacon_engine.edge.plan import (
    DiscoveryPlanSettings,
    build_discovery_plan,
    queries_from_radar_config,
)
from beacon_engine.edge.protocol import load_bundle, utc_now, write_bundle
from beacon_engine.edge.ssh_transport import (
    SSH_HOST_ENV,
    SSH_BATCH_SIZE,
    EdgeSSHTransport,
    bundle_id_prefix,
    verified_ack_bundle_id,
)

SIGNING_KEY_ENV = "SIGNAL_EDGE_SIGNING_KEY"
SYNC_ROOT_ENV = "SIGNAL_EDGE_SYNC_ROOT"
RADAR_CONFIG_ENV = "SIGNAL_EDGE_RADAR_CONFIG"
PLAN_CONTROLLER_ENV = "SIGNAL_EDGE_PLAN_CONTROLLER_ID"
PLAN_JOB_ENV = "SIGNAL_EDGE_PLAN_JOB_ID"
AUTO_PLAN_SYNC_ENV = "SIGNAL_EDGE_AUTO_PLAN_SYNC"
_SYNC_LOCK = threading.Lock()
_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]+\b")
_KEY_PARAM_RE = re.compile(r"(?i)(\bkey=)[^&\s]+")
DEFAULT_CHANNELS_PER_1000_QUERIES = 340
MIN_CHANNELS_PER_1000_QUERIES = 150
MAX_CHANNELS_PER_1000_QUERIES = 350
MIN_YIELD_SAMPLE_QUERIES = 100
YIELD_LOOKBACK_HOURS = 6

# --- cloud discovery liveness -------------------------------------------------
# On 2026-07-20 the remote collector deadlocked on a socket read with no timeout.
# It stayed "running", bundles stopped arriving, and every laptop-side cycle still
# reported success for 17 days. Silence must now read as an error, not as health.
MAX_REMOTE_AGE_HOURS = float(os.getenv("SIGNAL_EDGE_MAX_REMOTE_AGE_HOURS", "3") or 3)
MAX_DRY_HOURS = float(os.getenv("SIGNAL_EDGE_MAX_DRY_HOURS", "6") or 6)
MAX_ZERO_CHANNEL_SYNCS = int(os.getenv("SIGNAL_EDGE_MAX_ZERO_CHANNEL_SYNCS", "5") or 5)


@dataclass(frozen=True)
class EdgeSyncPaths:
    root: Path
    inbox: Path
    acknowledgements: Path
    archive: Path
    status: Path
    progress: Path
    remote_status: Path
    remote_organizer_status: Path
    transport_status: Path
    allowlist_status: Path
    plan_status: Path
    plans: Path

    @classmethod
    def resolve(cls) -> "EdgeSyncPaths":
        repo_root = Path(__file__).resolve().parents[2]
        configured = os.getenv(SYNC_ROOT_ENV)
        state_root = Path(os.getenv("BEACON_ENGINE_STATE_DIR") or repo_root / "storage")
        root = Path(configured) if configured else state_root / "edge_sync"
        if not root.is_absolute():
            root = repo_root / root
        return cls(
            root=root,
            inbox=root / "inbox",
            acknowledgements=root / "acks",
            archive=root / "archive",
            status=root / "sync_status.json",
            progress=root / "sync_progress.json",
            remote_status=root / "remote_status.json",
            remote_organizer_status=root / "remote_organizer_status.json",
            transport_status=root / "transport_status.json",
            allowlist_status=root / "oci_allowlist_status.json",
            plan_status=root / "plan_sync_status.json",
            plans=root / "plans",
        )

    @property
    def quarantine(self) -> Path:
        """Bundles that fail import on QUARANTINE_AFTER_FAILURES consecutive syncs are moved
        here (with an .error.txt sidecar) so a deterministically-bad but signed bundle stops
        jamming the oldest-first inbox slice and blocking newer valid discoveries. Derived (not
        an __init__ field) so existing EdgeSyncPaths construction sites keep working."""
        return self.root / "quarantine"

    @property
    def health(self) -> Path:
        """Tiny durable ledger of when cloud discovery last actually delivered channels.
        sync_status.json is overwritten every sync, so on its own it cannot tell a live
        pipeline from a dead one. Derived (not an __init__ field) for the same reason as
        quarantine: existing construction sites keep working."""
        return self.root / "edge_health.json"

    def ensure(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.acknowledgements.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.plans.mkdir(parents=True, exist_ok=True)
        self.quarantine.mkdir(parents=True, exist_ok=True)


# A signed bundle that fails import on this many CONSECUTIVE syncs is quarantined so it stops
# jamming the bounded, oldest-first inbox slice (see import_edge_inbox).
QUARANTINE_AFTER_FAILURES = 3


def _safe_error(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = _API_KEY_RE.sub("[redacted-key]", message)
    message = _KEY_PARAM_RE.sub(r"\1[redacted]", message)
    message = re.sub(r"\s+", " ", message).strip()
    return f"{type(exc).__name__}: {message}"[:400]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _pending_files(paths: EdgeSyncPaths) -> list[Path]:
    if not paths.inbox.exists():
        return []
    found: list[tuple[float, str, Path]] = []
    for path in paths.inbox.glob("*.signal-edge.json.gz"):
        try:
            if path.is_file():
                found.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    return [item[2] for item in sorted(found)]


def _archive_bundle(paths: EdgeSyncPaths, source: Path) -> Path:
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    directory = paths.archive / month
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if target.exists():
        target = directory / (
            source.name + "." + dt.datetime.now(dt.timezone.utc).strftime("%H%M%S%f")
        )
    os.replace(source, target)
    return target


def _known_bundle_names(paths: EdgeSyncPaths) -> set[str]:
    known = {
        path.name
        for path in paths.inbox.glob("*.signal-edge.json.gz")
        if path.is_file()
    }
    if paths.archive.exists():
        known.update(
            path.name
            for path in paths.archive.rglob("*.signal-edge.json.gz")
            if path.is_file()
        )
    return known


def _acknowledgements_by_prefix(
    paths: EdgeSyncPaths,
    signing_key: str,
) -> tuple[dict[str, tuple[str, Path]], list[dict[str, str]]]:
    found: dict[str, tuple[str, Path]] = {}
    errors: list[dict[str, str]] = []
    if not paths.acknowledgements.exists():
        return found, errors
    for path in paths.acknowledgements.glob("*.ack.json"):
        try:
            bundle_id = verified_ack_bundle_id(
                path,
                signing_key=signing_key,
            )
            found[bundle_id[:16]] = (bundle_id, path)
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": path.name, "error": _safe_error(exc)})
    return found, errors


def _chunks(items: list[Any], size: int = SSH_BATCH_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [items[index:index + step] for index in range(0, len(items), step)]


def _default_radar_config() -> Path:
    return Path(os.getenv("BEACON_ENGINE_STATE_DIR") or Path(__file__).resolve().parents[2] / "storage") / "scraper_watchlist.json"


def _automatic_plan_sync_enabled() -> bool:
    """Keep test/custom sync roots from ever publishing to the live edge node."""
    configured = os.getenv(AUTO_PLAN_SYNC_ENV)
    if configured is not None and configured.strip():
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return not bool(str(os.getenv(SYNC_ROOT_ENV) or "").strip())


def _edge_channels_per_1000_queries(
    db_path: str | os.PathLike | None,
    *,
    lookback_hours: int = YIELD_LOOKBACK_HOURS,
) -> int:
    """Measure recent canonical yield without exposing or moving the index."""
    if not db_path:
        return DEFAULT_CHANNELS_PER_1000_QUERIES
    path = Path(db_path)
    is_postgres = str(db_path) == "postgresql:beacon-index"
    if not is_postgres and not path.is_file():
        return DEFAULT_CHANNELS_PER_1000_QUERIES
    cutoff = (
        dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(hours=max(1, int(lookback_hours)))
    ).isoformat().replace("+00:00", "Z")
    connection: sqlite3.Connection | None = None
    try:
        if is_postgres:
            from beacon_engine.research.store import YouTubeStore
            connection = YouTubeStore(db_path, read_only=True).connect()
        else:
            connection = sqlite3.connect(path, timeout=5)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA query_only=ON")
        successful_queries = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM edge_query_runs
                WHERE status='ok' AND finished_at >= ?
                """,
                (cutoff,),
            ).fetchone()[0]
        )
        if successful_queries < MIN_YIELD_SAMPLE_QUERIES:
            return DEFAULT_CHANNELS_PER_1000_QUERIES
        new_channels = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM channels
                WHERE first_seen_at >= ?
                """,
                (cutoff,),
            ).fetchone()[0]
        )
    except Exception:  # Yield telemetry is best effort on both index backends.
        return DEFAULT_CHANNELS_PER_1000_QUERIES
    finally:
        if connection is not None:
            connection.close()
    measured = round(new_channels * 1_000 / successful_queries)
    bounded = max(
        MIN_CHANNELS_PER_1000_QUERIES,
        min(MAX_CHANNELS_PER_1000_QUERIES, measured),
    )
    return int(round(bounded / 10.0) * 10)


def _edge_plan_settings(
    db_path: str | os.PathLike | None = None,
    *,
    previous_expected_channels_per_1000: int | None = None,
) -> DiscoveryPlanSettings:
    expected = _edge_channels_per_1000_queries(db_path)
    previous_expected = int(
        previous_expected_channels_per_1000 or 0
    )
    if (
        previous_expected > 0
        and abs(expected - previous_expected) < 30
    ):
        expected = previous_expected
    return DiscoveryPlanSettings(
        batch_size=125,
        workers=24,
        interval_seconds=300,
        catchup_delay_seconds=5,
        mode="hybrid",
        max_quota_units_per_run=100,
        adaptive=True,
        retire_after=3,
        reprobe_every=20,
        repeat=True,
        turbo_enabled=True,
        target_channels_per_hour=450,
        expected_channels_per_1000_queries=expected,
        min_batch_size=50,
        failure_backoff_percent=5,
        batch_recovery_step=25,
        healthy_runs_to_recover=2,
    )


def _plan_spec_sha256(
    *,
    controller_id: str,
    job_id: str,
    queries: tuple[Any, ...],
    settings: DiscoveryPlanSettings,
) -> str:
    payload = {
        "controller_id": controller_id,
        "job_id": job_id,
        "queries": [query.as_record() for query in queries],
        "settings": settings.as_dict(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sync_edge_discovery_plan(
    *,
    paths: EdgeSyncPaths | None = None,
    signing_key: str | None = None,
    transport: EdgeSSHTransport | None = None,
    radar_config: str | os.PathLike | None = None,
    db_path: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Upload a new signed plan only when the local watchlist/settings changed."""
    resolved = paths or EdgeSyncPaths.resolve()
    secret = signing_key or os.getenv(SIGNING_KEY_ENV) or ""
    client = transport or EdgeSSHTransport.from_env()
    if client is None:
        return {
            "configured": False,
            "state": "not_configured",
            "uploaded": False,
            "errors": [],
        }
    if not secret:
        raise RuntimeError(f"{SIGNING_KEY_ENV} is required for plan sync")
    configured_path = (
        Path(radar_config)
        if radar_config is not None
        else Path(os.getenv(RADAR_CONFIG_ENV) or _default_radar_config())
    )
    if not configured_path.is_absolute():
        configured_path = Path(__file__).resolve().parents[2] / configured_path
    if not configured_path.is_file():
        raise RuntimeError(
            f"edge radar configuration is missing: {configured_path}"
        )
    resolved.ensure()
    controller_id = str(
        os.getenv(PLAN_CONTROLLER_ENV) or "signal-laptop"
    ).strip()
    job_id = str(os.getenv(PLAN_JOB_ENV) or "radar-discovery").strip()
    queries = queries_from_radar_config(configured_path)
    previous = _read_json(resolved.plan_status)
    previous_settings = previous.get("settings")
    previous_expected = (
        int(
            previous_settings.get(
                "expected_channels_per_1000_queries",
                0,
            )
            or 0
        )
        if isinstance(previous_settings, dict)
        else 0
    )
    settings = _edge_plan_settings(
        db_path,
        previous_expected_channels_per_1000=previous_expected,
    )
    spec_sha256 = _plan_spec_sha256(
        controller_id=controller_id,
        job_id=job_id,
        queries=queries,
        settings=settings,
    )
    if (
        previous.get("spec_sha256") == spec_sha256
        and previous.get("revision")
        and previous.get("state") in {"uploaded", "unchanged"}
    ):
        result = {
            **previous,
            "configured": True,
            "state": "unchanged",
            "uploaded": False,
            "checked_at": utc_now(),
            "errors": [],
        }
        _write_json(resolved.plan_status, result)
        return result
    bundle = build_discovery_plan(
        controller_id=controller_id,
        job_id=job_id,
        queries=queries,
        settings=settings,
        signing_key=secret,
    )
    plan_path = write_bundle(bundle, resolved.plans)
    revision = client.publish_discovery_plan(
        plan_path,
        signing_key=secret,
    )
    result = {
        "configured": True,
        "state": "uploaded",
        "uploaded": True,
        "checked_at": utc_now(),
        "uploaded_at": utc_now(),
        "query_count": len(queries),
        "revision": revision,
        "spec_sha256": spec_sha256,
        "settings": settings.as_dict(),
        "errors": [],
    }
    _write_json(resolved.plan_status, result)
    return result


def pull_edge_remote(
    *,
    paths: EdgeSyncPaths | None = None,
    signing_key: str | None = None,
    max_bundles: int = 20,
    transport: EdgeSSHTransport | None = None,
) -> dict[str, Any]:
    """Pull signed bundles, publish remote status, and flush signed acks."""
    resolved = paths or EdgeSyncPaths.resolve()
    secret = signing_key or os.getenv(SIGNING_KEY_ENV) or ""
    client = transport or EdgeSSHTransport.from_env()
    if client is None:
        return {
            "configured": False,
            "pulled": 0,
            "acknowledged": 0,
            "remote_pending": 0,
            "errors": [],
        }
    if not secret:
        raise RuntimeError(f"{SIGNING_KEY_ENV} is required for SSH transport")
    resolved.ensure()
    errors: list[dict[str, str]] = []
    acknowledged = 0
    pulled = 0

    try:
        remote_status = client.read_worker_status()
        _write_json(resolved.remote_status, remote_status)
    except Exception as exc:  # noqa: BLE001
        errors.append({"file": "worker_status.json", "error": _safe_error(exc)})
    read_organizer_status = getattr(client, "read_organizer_status", None)
    if callable(read_organizer_status):
        try:
            organizer_status = read_organizer_status()
            _write_json(
                resolved.remote_organizer_status,
                organizer_status,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "file": "organizer/organizer_status.json",
                    "error": _safe_error(exc),
                }
            )

    remote_names = client.list_bundle_names()
    acknowledgements, ack_errors = _acknowledgements_by_prefix(
        resolved,
        secret,
    )
    errors.extend(ack_errors)
    pending_acks: list[tuple[str, Path]] = []
    for name in list(remote_names):
        prefix = bundle_id_prefix(name)
        acknowledged_item = acknowledgements.get(prefix)
        if acknowledged_item is None:
            continue
        _bundle_id, ack_path = acknowledged_item
        pending_acks.append((name, ack_path))
    for batch in _chunks(pending_acks):
        batch_ack = getattr(client, "acknowledge_many", None)
        if callable(batch_ack) and len(batch) > 1:
            try:
                acknowledged_ids = batch_ack(
                    batch,
                    signing_key=secret,
                )
                if len(acknowledged_ids) != len(batch):
                    raise RuntimeError(
                        "SSH acknowledgement batch returned an incomplete result"
                    )
                acknowledged += len(batch)
                for name, _ack_path in batch:
                    if name in remote_names:
                        remote_names.remove(name)
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "file": "remote-acknowledgement-batch",
                        "error": _safe_error(exc),
                    }
                )
        for name, ack_path in batch:
            try:
                client.acknowledge(
                    bundle_filename=name,
                    ack_path=ack_path,
                    signing_key=secret,
                )
                acknowledged += 1
                if name in remote_names:
                    remote_names.remove(name)
            except Exception as exc:  # noqa: BLE001
                errors.append({"file": name, "error": _safe_error(exc)})

    limit = max(0, min(int(max_bundles or 0), 100))
    known = _known_bundle_names(resolved)
    candidates = [
        name for name in remote_names
        if name not in known
    ][:limit]
    for batch in _chunks(candidates):
        payloads: dict[str, bytes] = {}
        batch_download = getattr(client, "download_bundles", None)
        if callable(batch_download) and len(batch) > 1:
            try:
                payloads = batch_download(batch)
                if set(payloads) != set(batch):
                    raise RuntimeError(
                        "SSH download batch returned unexpected files"
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "file": "remote-download-batch",
                        "error": _safe_error(exc),
                    }
                )
                payloads = {}
        for name in batch:
            partial = resolved.inbox / f"{name}.partial"
            final = resolved.inbox / name
            try:
                partial.unlink(missing_ok=True)
                payload = payloads.get(name)
                if payload is None:
                    payload = client.download_bundle(name)
                partial.write_bytes(payload)
                bundle = load_bundle(partial, signing_key=secret)
                if bundle.kind not in {
                    DISCOVERY_BUNDLE_KIND,
                    ORGANIZATION_BUNDLE_KIND,
                }:
                    raise ValueError(
                        f"unsupported remote bundle kind '{bundle.kind}'"
                    )
                if not bundle.bundle_id.startswith(bundle_id_prefix(name)):
                    raise ValueError("remote filename does not match bundle identity")
                os.replace(partial, final)
                known.add(name)
                pulled += 1
            except Exception as exc:  # noqa: BLE001
                partial.unlink(missing_ok=True)
                errors.append({"file": name, "error": _safe_error(exc)})

    try:
        remote_pending = len(client.list_bundle_names())
    except Exception as exc:  # noqa: BLE001
        remote_pending = len(remote_names)
        errors.append({"file": "remote-outbox", "error": _safe_error(exc)})
    result = {
        "configured": True,
        "pulled": pulled,
        "acknowledged": acknowledged,
        "remote_pending": remote_pending,
        "errors": errors[:10],
        "updated_at": utc_now(),
    }
    _write_json(resolved.transport_status, result)
    return result


def _combine_transport(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    if not before.get("configured") and not after.get("configured"):
        return before or after
    return {
        "configured": bool(
            before.get("configured") or after.get("configured")
        ),
        "pulled": int(before.get("pulled") or 0)
        + int(after.get("pulled") or 0),
        "acknowledged": int(before.get("acknowledged") or 0)
        + int(after.get("acknowledged") or 0),
        "remote_pending": int(
            after.get("remote_pending")
            if after.get("configured")
            else before.get("remote_pending")
            or 0
        ),
        "errors": [
            *(before.get("errors") or []),
            *(after.get("errors") or []),
        ][:10],
        "updated_at": str(
            after.get("updated_at")
            or before.get("updated_at")
            or utc_now()
        ),
    }


class _BackgroundAcknowledgementFlusher:
    """Coalesce post-commit acknowledgement uploads onto one SSH worker."""

    def __init__(
        self,
        *,
        paths: EdgeSyncPaths,
        signing_key: str,
        transport: EdgeSSHTransport | None,
        remote_pending: int,
    ) -> None:
        self.paths = paths
        self.signing_key = signing_key
        self.transport = transport
        self.remote_pending = max(0, int(remote_pending or 0))
        self._condition = threading.Condition()
        self._pending = False
        self._stopping = False
        self._result: dict[str, Any] = {
            "configured": False,
            "pulled": 0,
            "acknowledged": 0,
            "remote_pending": self.remote_pending,
            "errors": [],
        }
        self._thread: threading.Thread | None = None
        if transport is not None:
            self._thread = threading.Thread(
                target=self._run,
                name="signal-edge-acknowledgements",
                daemon=True,
            )
            self._thread.start()

    def request(self) -> None:
        """Request one flush after a transaction has created durable acks."""
        if self._thread is None:
            return
        with self._condition:
            if self._stopping:
                return
            self._pending = True
            self._condition.notify()

    def finish(self) -> dict[str, Any]:
        """Wait for any requested flush while preserving retryable local acks."""
        thread = self._thread
        if thread is None:
            return dict(self._result)
        with self._condition:
            self._stopping = True
            self._condition.notify()
        thread.join()
        self._thread = None
        return dict(self._result)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if not self._pending and self._stopping:
                    return
                self._pending = False
            try:
                flushed = pull_edge_remote(
                    paths=self.paths,
                    signing_key=self.signing_key,
                    max_bundles=0,
                    transport=self.transport,
                )
            except Exception as exc:  # noqa: BLE001
                flushed = {
                    "configured": self.transport is not None,
                    "pulled": 0,
                    "acknowledged": 0,
                    "remote_pending": self.remote_pending,
                    "errors": [
                        {
                            "file": "remote-acknowledgement",
                            "error": _safe_error(exc),
                        }
                    ],
                    "updated_at": utc_now(),
                }
            self._result = _combine_transport(self._result, flushed)
            self.remote_pending = int(
                self._result.get("remote_pending") or self.remote_pending
            )
            with self._condition:
                if self._stopping and not self._pending:
                    return


def _age_hours(stamp: str) -> float | None:
    """Hours since an ISO-8601 stamp, or None when it is missing/unparseable."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - parsed
    return max(0.0, delta.total_seconds() / 3600.0)


def _record_sync_health(paths: EdgeSyncPaths, result: dict[str, Any]) -> None:
    """Remember when cloud discovery last actually delivered channels.

    Best-effort only: a failure here must never break an import that has already
    committed."""
    try:
        ledger = _read_json(paths.health)
        channels = int((result.get("counts") or {}).get("channels") or 0)
        processed = int(result.get("processed") or 0)
        if channels > 0:
            ledger["last_channels_at"] = str(result.get("synced_at") or utc_now())
            ledger["last_channels_count"] = channels
            ledger["zero_channel_syncs"] = 0
        elif processed > 0:
            # Bundles arrived but carried no channels — the "0 channels several
            # times in a row" shape. An idle sync with nothing pending is not a
            # dry cycle and must not inflate the streak.
            ledger["zero_channel_syncs"] = int(ledger.get("zero_channel_syncs") or 0) + 1
        ledger["updated_at"] = utc_now()
        if not ledger.get("first_seen_at"):
            ledger["first_seen_at"] = ledger["updated_at"]
        _write_json(paths.health, ledger)
    except Exception:
        return


def _edge_health(
    *,
    configured: bool,
    remote: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Judge cloud discovery by PROGRESS, not by the remote merely claiming 'running'.

    Deliberately does NOT require this process to hold the signing key: Signal's UI
    only imports the key in the scheduled sync process, so keying the check on that
    would make it permanently silent in the one place a person actually looks."""
    in_use = configured or bool(remote.get("updated_at")) or bool(ledger)
    if not in_use:
        return {
            "state": "off",
            "ok": True,
            "problems": [],
            "summary": "Cloud discovery is not configured on this laptop.",
        }

    problems: list[str] = []
    remote_age = _age_hours(str(remote.get("updated_at") or ""))
    if remote_age is None:
        problems.append("The cloud worker has never reported a status to this laptop.")
    elif remote_age > MAX_REMOTE_AGE_HOURS:
        problems.append(
            f"The cloud worker last reported {_describe_age(remote_age)} ago "
            f"(expected within {MAX_REMOTE_AGE_HOURS:g}h) - it looks stuck, not busy."
        )

    dry_hours = _age_hours(str(ledger.get("last_channels_at") or ""))
    # Before the first delivery there is nothing to measure against; fall back to
    # how long this ledger has existed so a never-delivering pipeline still trips.
    if dry_hours is None:
        dry_hours = _age_hours(str(ledger.get("first_seen_at") or ""))
        label = "No channels have arrived since monitoring started"
    else:
        label = "No new channels have arrived"
    if dry_hours is not None and dry_hours > MAX_DRY_HOURS:
        problems.append(f"{label} for {_describe_age(dry_hours)}.")

    zero_streak = int(ledger.get("zero_channel_syncs") or 0)
    if zero_streak >= MAX_ZERO_CHANNEL_SYNCS:
        problems.append(
            f"The last {zero_streak} cloud imports in a row delivered 0 channels."
        )

    if problems:
        return {
            "state": "error",
            "ok": False,
            "problems": problems,
            "summary": problems[0],
            "remote_age_hours": remote_age,
            "hours_since_channels": dry_hours,
            "zero_channel_syncs": zero_streak,
        }
    return {
        "state": "ok",
        "ok": True,
        "problems": [],
        "summary": "Cloud discovery is delivering channels.",
        "remote_age_hours": remote_age,
        "hours_since_channels": dry_hours,
        "zero_channel_syncs": zero_streak,
    }


def _describe_age(hours: float) -> str:
    """Plain-English age for a non-technical reader."""
    if hours < 1:
        return f"{int(round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{int(hours // 24)} days"


def edge_sync_status() -> dict[str, Any]:
    """Non-secret cloud inbox state for Signal and read-only Finder clients."""
    paths = EdgeSyncPaths.resolve()
    pending = _pending_files(paths)
    last_sync = _read_json(paths.status)
    remote = _read_json(paths.remote_status)
    organizer = _read_json(paths.remote_organizer_status)
    transport = _read_json(paths.transport_status)
    allowlist = _read_json(paths.allowlist_status)
    plan_sync = _read_json(paths.plan_status)
    progress = _read_json(paths.progress)
    if not remote:
        remote = _read_json(paths.root / "worker_status.json")
    configured = bool(os.getenv(SIGNING_KEY_ENV))
    pending_bytes = 0
    newest_at = ""
    for path in pending:
        try:
            stat = path.stat()
            pending_bytes += int(stat.st_size)
            newest_at = max(
                newest_at,
                dt.datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=dt.timezone.utc,
                ).isoformat().replace("+00:00", "Z"),
            )
        except OSError:
            continue
    return {
        "configured": configured,
        "transport_configured": bool(os.getenv(SSH_HOST_ENV)),
        "health": _edge_health(
            configured=configured,
            remote=remote,
            ledger=_read_json(paths.health),
        ),
        "pending_bundles": len(pending),
        "pending_bytes": pending_bytes,
        "newest_bundle_at": newest_at,
        "acknowledgements": (
            len(list(paths.acknowledgements.glob("*.ack.json")))
            if paths.acknowledgements.exists()
            else 0
        ),
        "last_sync": last_sync,
        "progress": progress,
        "transport": transport,
        "allowlist": allowlist,
        "plan_sync": plan_sync,
        "remote": {
            "state": str(remote.get("state") or "unknown"),
            "message": str(remote.get("message") or "")[:300],
            "updated_at": str(remote.get("updated_at") or ""),
            "node_id": str(remote.get("node_id") or "")[:128],
        },
        "organizer": {
            "state": str(organizer.get("state") or "unknown"),
            "message": str(organizer.get("message") or "")[:300],
            "updated_at": str(organizer.get("updated_at") or ""),
            "node_id": str(organizer.get("node_id") or "")[:128],
            "queued_jobs": int(organizer.get("queued_jobs") or 0),
        },
    }


def import_edge_inbox(
    db_path: str | os.PathLike,
    *,
    max_bundles: int = 20,
) -> dict[str, Any]:
    """Verify, import, acknowledge, and archive a bounded inbox slice."""
    signing_key = os.getenv(SIGNING_KEY_ENV) or ""
    if not signing_key:
        return {
            "error": (
                f"{SIGNING_KEY_ENV} is not configured on this laptop; "
                "cloud bundles cannot be verified"
            )
        }
    limit = max(1, min(int(max_bundles or 20), 100))
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"error": "a cloud inbox sync is already running"}
    progress_paths: EdgeSyncPaths | None = None
    progress_started = 0.0
    progress_started_at = ""
    try:
        paths = EdgeSyncPaths.resolve()
        progress_paths = paths
        paths.ensure()
        transport_client = EdgeSSHTransport.from_env()
        if _automatic_plan_sync_enabled():
            try:
                plan_sync = sync_edge_discovery_plan(
                    paths=paths,
                    signing_key=signing_key,
                    transport=transport_client,
                    db_path=db_path,
                )
            except Exception as exc:  # noqa: BLE001
                plan_sync = {
                    "configured": transport_client is not None,
                    "state": "error",
                    "uploaded": False,
                    "checked_at": utc_now(),
                    "errors": [
                        {"file": "discovery-plan", "error": _safe_error(exc)}
                    ],
                }
                _write_json(paths.plan_status, plan_sync)
        else:
            plan_sync = {
                "configured": transport_client is not None,
                "state": "disabled",
                "uploaded": False,
                "checked_at": utc_now(),
                "errors": [],
            }
            _write_json(paths.plan_status, plan_sync)
        try:
            transport_before = pull_edge_remote(
                paths=paths,
                signing_key=signing_key,
                max_bundles=limit,
                transport=transport_client,
            )
        except Exception as exc:  # noqa: BLE001
            transport_before = {
                "configured": bool(os.getenv(SSH_HOST_ENV)),
                "pulled": 0,
                "acknowledged": 0,
                "remote_pending": 0,
                "errors": [
                    {"file": "remote-transport", "error": _safe_error(exc)}
                ],
            }
        pending_before = _pending_files(paths)
        files = pending_before[:limit]
        remote_before = int(transport_before.get("remote_pending") or 0)
        progress_started = time.monotonic()
        progress_started_at = utc_now()
        progress_imported = 0
        progress_duplicates = 0
        progress_failed = 0
        progress_processed = 0

        def write_progress(
            *,
            state: str,
            processed: int,
            total: int,
            backlog_remaining: int,
            error: str = "",
            activity: dict[str, Any] | None = None,
        ) -> None:
            activity = activity if isinstance(activity, dict) else {}
            elapsed = max(0.0, time.monotonic() - progress_started)
            rate = processed / elapsed if processed > 0 and elapsed > 0 else 0.0
            eta_seconds = (
                max(0.0, (total - processed) / rate)
                if rate > 0 and processed < total
                else 0.0
            )
            _write_json(
                paths.progress,
                {
                    "state": state,
                    "started_at": progress_started_at,
                    "updated_at": utc_now(),
                    "processed_bundles": processed,
                    "total_bundles": total,
                    "remaining_bundles": max(0, total - processed),
                    "backlog_remaining": max(0, backlog_remaining),
                    "imported_bundles": progress_imported,
                    "duplicate_bundles": progress_duplicates,
                    "failed_bundles": progress_failed,
                    "elapsed_seconds": round(elapsed, 3),
                    "bundles_per_minute": round(rate * 60.0, 2),
                    "eta_seconds": round(eta_seconds, 1),
                    "error": error[:400],
                    "phase": str(activity.get("phase") or ""),
                    "transaction_number": int(
                        activity.get("transaction_number") or 0
                    ),
                    "transaction_total": int(
                        activity.get("transaction_total") or 0
                    ),
                    "transaction_processed": int(
                        activity.get("transaction_processed") or 0
                    ),
                    "transaction_size": int(
                        activity.get("transaction_size") or 0
                    ),
                    "active_bundle": int(
                        activity.get("active_bundle") or 0
                    ),
                },
            )

        write_progress(
            state="importing",
            processed=0,
            total=len(files),
            backlog_remaining=len(pending_before) + remote_before,
            activity={"phase": "preparing"},
        )

        def record_import_activity(activity: dict[str, Any]) -> None:
            write_progress(
                state="importing",
                processed=progress_processed,
                total=len(files),
                backlog_remaining=(
                    max(0, len(pending_before) - progress_processed)
                    + remote_before
                ),
                activity=activity,
            )

        def record_import_progress(
            chunk: list,
            processed: int,
            total: int,
        ) -> None:
            nonlocal progress_imported, progress_duplicates, progress_failed
            nonlocal progress_processed
            for attempt in chunk:
                if attempt.error is not None or attempt.report is None:
                    progress_failed += 1
                elif attempt.report.duplicate:
                    progress_duplicates += 1
                else:
                    progress_imported += 1
            progress_processed = processed
            write_progress(
                state="importing",
                processed=processed,
                total=total,
                backlog_remaining=(
                    max(0, len(pending_before) - processed) + remote_before
                ),
                activity={"phase": "transaction_complete"},
            )
            acknowledgement_flusher.request()

        imported = 0
        duplicates = 0
        failed = 0
        counts: dict[str, int] = {}
        errors: list[dict[str, str]] = []
        bundle_ids: list[str] = []
        failed_paths: list[Path] = []
        failed_errors: dict[str, str] = {}
        wal_maintenance: dict[str, Any] = {}
        acknowledgement_flusher = _BackgroundAcknowledgementFlusher(
            paths=paths,
            signing_key=signing_key,
            transport=transport_client,
            remote_pending=remote_before,
        )
        try:
            with BundleImportSession(db_path) as importer:
                attempts = importer.import_paths(
                    files,
                    signing_key=signing_key,
                    ack_dir=paths.acknowledgements,
                    transaction_bundles=DEFAULT_IMPORT_TRANSACTION_BUNDLES,
                    on_progress=record_import_progress,
                    on_activity=record_import_activity,
                )
                for attempt in attempts:
                    if attempt.error is not None:
                        failed += 1
                        err = _safe_error(attempt.error)
                        errors.append({"file": attempt.path.name, "error": err})
                        failed_paths.append(attempt.path)
                        failed_errors[attempt.path.name] = err
                        continue
                    report = attempt.report
                    if report is None:
                        failed += 1
                        err = "RuntimeError: importer returned no result"
                        errors.append({"file": attempt.path.name, "error": err})
                        failed_paths.append(attempt.path)
                        failed_errors[attempt.path.name] = err
                        continue
                    bundle_ids.append(report.bundle_id)
                    if report.duplicate:
                        duplicates += 1
                    else:
                        imported += 1
                        for name, value in report.counts.items():
                            counts[name] = counts.get(name, 0) + int(value)
                    _archive_bundle(paths, attempt.path)
            wal_maintenance = dict(importer.wal_maintenance_status)
        finally:
            transport_after = acknowledgement_flusher.finish()
        # Quarantine bundles that keep FAILING import. They already passed pull-time signature
        # verification, so a repeated staging/DB failure is likely deterministic; leaving them
        # in the oldest-first inbox makes them re-fail every sync and block newer valid bundles.
        # Transient failures (below the threshold) stay in the inbox and simply retry. This
        # whole block is best-effort and never raises into the sync.
        if failed_paths:
            retry_path = paths.root / "quarantine_retries.json"
            try:
                retries = json.loads(retry_path.read_text(encoding="utf-8"))
                if not isinstance(retries, dict):
                    retries = {}
            except (OSError, ValueError):
                retries = {}
            next_retries: dict[str, int] = {}
            for bad in failed_paths:
                name = bad.name
                streak = int(retries.get(name, 0) or 0) + 1
                if streak >= QUARANTINE_AFTER_FAILURES:
                    try:
                        paths.quarantine.mkdir(parents=True, exist_ok=True)
                        os.replace(bad, paths.quarantine / name)
                        (paths.quarantine / (name + ".error.txt")).write_text(
                            failed_errors.get(name, "import failed"), encoding="utf-8")
                        continue   # moved out of the inbox -> drop its counter
                    except OSError:
                        pass
                next_retries[name] = streak
            try:
                _write_json(retry_path, next_retries)
            except Exception:  # noqa: BLE001
                pass
        remaining = len(_pending_files(paths))
        result = {
            "synced_at": utc_now(),
            "processed": len(files),
            "imported_bundles": imported,
            "duplicate_bundles": duplicates,
            "failed_bundles": failed,
            "remaining_bundles": remaining,
            "counts": counts,
            "bundle_ids": bundle_ids,
            "errors": errors[:10],
            "plan_sync": plan_sync,
            "wal_maintenance": wal_maintenance,
            "transport": _combine_transport(
                transport_before,
                transport_after,
            ),
        }
        _write_json(paths.status, result)
        _record_sync_health(paths, result)
        write_progress(
            state="complete",
            processed=len(files),
            total=len(files),
            backlog_remaining=(
                remaining + int(result["transport"].get("remote_pending") or 0)
            ),
            activity={"phase": "complete"},
        )
        return result
    except Exception as exc:
        if progress_paths is not None and progress_started_at:
            try:
                _write_json(
                    progress_paths.progress,
                    {
                        "state": "error",
                        "started_at": progress_started_at,
                        "updated_at": utc_now(),
                        "processed_bundles": 0,
                        "total_bundles": 0,
                        "remaining_bundles": 0,
                        "backlog_remaining": 0,
                        "imported_bundles": 0,
                        "duplicate_bundles": 0,
                        "failed_bundles": 0,
                        "elapsed_seconds": round(
                            max(0.0, time.monotonic() - progress_started),
                            3,
                        ),
                        "bundles_per_minute": 0.0,
                        "eta_seconds": 0.0,
                        "error": _safe_error(exc),
                        "phase": "error",
                        "transaction_number": 0,
                        "transaction_total": 0,
                        "transaction_processed": 0,
                        "transaction_size": 0,
                        "active_bundle": 0,
                    },
                )
            except Exception:
                pass
        raise
    finally:
        _SYNC_LOCK.release()
