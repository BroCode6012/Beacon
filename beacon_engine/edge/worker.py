"""Durable, resumable scheduler for an always-on Signal edge collector."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import re

from beacon_engine.edge.collector import (
    CollectionBatch,
    ScheduledQuery,
    _safe_error,
    collect_discovery_batch,
)
from beacon_engine.edge.importer import DISCOVERY_BUNDLE_KIND
from beacon_engine.edge.discovery_feedback import allocate_queries, consume_feedback, remember_bundle
from beacon_engine.edge.plan import DiscoveryPlan, load_discovery_plan
from beacon_engine.edge.protocol import build_bundle, utc_now, write_bundle

STATE_VERSION = 1
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# A hung process produces no exit code, so systemd's Restart=on-failure is blind
# to it. On 2026-07-20 the collector deadlocked on a timeout-less socket read and
# systemd kept reporting "active (running)" for 17 days while nothing was
# collected. The watchdog below turns "no progress for N seconds" into a real
# non-zero exit, which is the only thing systemd can actually act on.
EDGE_STALL_EXIT_CODE = 75
DEFAULT_STALL_SECONDS = 1800


def _configured_stall_seconds() -> int:
    """Seconds without a status write before the worker declares itself stalled."""
    raw = os.getenv("SIGNAL_EDGE_STALL_SECONDS", "")
    try:
        value = int(float(raw)) if raw.strip() else DEFAULT_STALL_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_STALL_SECONDS
    if value <= 0:
        return 0  # explicitly disabled
    return max(120, value)


def _epoch_iso(timestamp: float) -> str:
    import datetime as dt

    return (
        dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def default_node_id() -> str:
    raw = os.getenv("SIGNAL_EDGE_NODE_ID") or socket.gethostname() or "oracle-edge"
    clean = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in raw
    ).strip("-")
    return (clean or "oracle-edge")[:128]


@dataclass(frozen=True)
class EdgeWorkerPaths:
    root: Path
    jobs: Path
    outbox: Path
    state: Path
    status: Path
    lock: Path
    stop: Path

    @classmethod
    def under(cls, root: str | os.PathLike) -> "EdgeWorkerPaths":
        base = Path(root)
        return cls(
            root=base,
            jobs=base / "jobs",
            outbox=base / "outbox",
            state=base / "worker_state.json",
            status=base / "worker_status.json",
            lock=base / "worker.lock",
            stop=base / "stop.flag",
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class WorkerRunReport:
    job_id: str
    status: str
    run_id: str = ""
    bundle_path: str = ""
    queries: int = 0
    succeeded: int = 0
    failed: int = 0
    organization_jobs: int = 0
    remaining_untested: int = 0
    next_run_at: str = ""
    message: str = ""
    effective_batch_size: int = 0
    estimated_channels_per_hour: float = 0.0
    failure_percent: float = 0.0
    turbo_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "run_id": self.run_id,
            "bundle_path": self.bundle_path,
            "queries": self.queries,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "organization_jobs": self.organization_jobs,
            "remaining_untested": self.remaining_untested,
            "next_run_at": self.next_run_at,
            "message": self.message,
            "effective_batch_size": self.effective_batch_size,
            "estimated_channels_per_hour": (
                self.estimated_channels_per_hour
            ),
            "failure_percent": self.failure_percent,
            "turbo_action": self.turbo_action,
        }


class EdgeWorkerLock:
    """One-process lock that can recover a stale PID file after a reboot."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    pid = 0
                if self._pid_alive(pid):
                    raise RuntimeError(
                        f"another edge worker is already running (pid {pid})"
                    )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            self.acquired = True
            return
        raise RuntimeError("could not acquire the edge worker lock")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False

    def __enter__(self) -> "EdgeWorkerLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def _empty_state(node_id: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "node_id": node_id,
        "updated_at": "",
        "jobs": {},
    }


def _query_template() -> dict[str, Any]:
    return {
        "attempts": 0,
        "failures": 0,
        "zero_streak": 0,
        "last_video_ids": [],
        "last_video_views": {},
        "last_result_count": 0,
        "last_new_result_count": 0,
        "last_completed_ts": 0.0,
        "last_error": "",
        "retry_after_ts": 0.0,
    }


def _recent_organization_channels(
    state_path: Path,
    channel_ids: set[str],
    *,
    cutoff_ts: float,
) -> set[str]:
    if not channel_ids or not state_path.is_file():
        return set()
    found: set[str] = set()
    with sqlite3.connect(state_path, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA query_only=ON")
        ids = sorted(channel_ids)
        for index in range(0, len(ids), 400):
            chunk = ids[index:index + 400]
            placeholders = ",".join("?" for _ in chunk)
            found.update(
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT channel_id
                    FROM organization_channels
                    WHERE processed_ts >= ?
                      AND channel_id IN ({placeholders})
                    """,
                    [cutoff_ts, *chunk],
                )
            )
    return found


def _estimated_channels_per_hour(
    plan: DiscoveryPlan,
    batch_size: int,
) -> float:
    expected = int(plan.settings.expected_channels_per_1000_queries or 0)
    if expected <= 0:
        return 0.0
    cycles_per_hour = 3_600 / max(1, plan.settings.interval_seconds)
    return round(batch_size * cycles_per_hour * expected / 1_000, 1)


def _ensure_turbo_controller(
    job: dict[str, Any],
    plan: DiscoveryPlan,
) -> dict[str, Any]:
    settings = plan.settings
    controller = job.setdefault("turbo_controller", {})
    previously_enabled = bool(controller.get("enabled"))
    if not settings.turbo_enabled:
        controller.update(
            {
                "enabled": False,
                "effective_batch_size": settings.batch_size,
                "healthy_runs": 0,
                "last_failure_percent": 0.0,
                "estimated_channels_per_hour": 0.0,
                "target_channels_per_hour": 0,
                "target_met": False,
                "backing_off": False,
                "last_action": "disabled",
            }
        )
        return controller

    minimum = min(settings.min_batch_size, settings.batch_size)
    if previously_enabled:
        current = int(
            controller.get("effective_batch_size")
            or settings.batch_size
        )
    else:
        current = settings.batch_size
    current = max(minimum, min(settings.batch_size, current))
    estimate = _estimated_channels_per_hour(plan, current)
    controller.update(
        {
            "enabled": True,
            "effective_batch_size": current,
            "healthy_runs": max(
                0,
                int(controller.get("healthy_runs") or 0),
            ),
            "last_failure_percent": float(
                controller.get("last_failure_percent") or 0.0
            ),
            "estimated_channels_per_hour": estimate,
            "target_channels_per_hour": settings.target_channels_per_hour,
            "target_met": estimate >= settings.target_channels_per_hour,
            "backing_off": bool(controller.get("backing_off")),
            "last_action": str(
                controller.get("last_action") or "turbo-start"
            ),
        }
    )
    return controller


def _update_turbo_controller(
    job: dict[str, Any],
    plan: DiscoveryPlan,
    *,
    attempted: int,
    failed: int,
) -> dict[str, Any]:
    controller = _ensure_turbo_controller(job, plan)
    if not plan.settings.turbo_enabled:
        return controller

    attempted_count = max(0, int(attempted))
    failed_count = max(0, min(attempted_count, int(failed)))
    failure_percent = (
        round((failed_count * 100.0) / attempted_count, 1)
        if attempted_count
        else 0.0
    )
    settings = plan.settings
    minimum = min(settings.min_batch_size, settings.batch_size)
    current = int(controller["effective_batch_size"])
    healthy_runs = int(controller.get("healthy_runs") or 0)
    backing_off = (
        attempted_count > 0
        and failure_percent > settings.failure_backoff_percent
    )
    if backing_off:
        current = max(
            minimum,
            current - settings.batch_recovery_step,
        )
        healthy_runs = 0
        action = "backoff"
    elif attempted_count > 0:
        if current >= settings.batch_size:
            healthy_runs = 0
            action = "hold"
        else:
            healthy_runs += 1
            if healthy_runs >= settings.healthy_runs_to_recover:
                current = min(
                    settings.batch_size,
                    current + settings.batch_recovery_step,
                )
                healthy_runs = 0
                action = "recover"
            else:
                action = "hold"
    else:
        action = "hold"

    estimate = _estimated_channels_per_hour(plan, current)
    controller.update(
        {
            "effective_batch_size": current,
            "healthy_runs": healthy_runs,
            "last_failure_percent": failure_percent,
            "estimated_channels_per_hour": estimate,
            "target_met": estimate >= settings.target_channels_per_hour,
            "backing_off": backing_off,
            "last_action": action,
        }
    )
    return controller


def _turbo_report_fields(
    job: dict[str, Any],
    plan: DiscoveryPlan,
) -> dict[str, Any]:
    controller = _ensure_turbo_controller(job, plan)
    return {
        "effective_batch_size": int(
            controller.get("effective_batch_size") or 0
        ),
        "estimated_channels_per_hour": float(
            controller.get("estimated_channels_per_hour") or 0.0
        ),
        "failure_percent": float(
            controller.get("last_failure_percent") or 0.0
        ),
        "turbo_action": str(controller.get("last_action") or ""),
    }


def _steady_delay_seconds(
    plan: DiscoveryPlan,
    controller: dict[str, Any],
) -> int:
    if (
        plan.settings.turbo_enabled
        and bool(controller.get("backing_off"))
    ):
        return min(3_600, plan.settings.interval_seconds * 2)
    return plan.settings.interval_seconds


def _ensure_job_state(
    state: dict[str, Any],
    plan: DiscoveryPlan,
    *,
    now_ts: float,
) -> dict[str, Any]:
    jobs = state.setdefault("jobs", {})
    job = jobs.setdefault(
        plan.job_id,
        {
            "revision": "",
            "controller_id": plan.controller_id,
            "plan_created_at": plan.created_at,
            "runs": 0,
            "ticks": 0,
            "complete": False,
            "next_run_ts": 0.0,
            "last_run_at": "",
            "last_bundle_id": "",
            "last_bundle_path": "",
            "last_error": "",
            "queries": {},
        },
    )
    query_state = job.setdefault("queries", {})
    for query in plan.queries:
        existing = query_state.get(query.query)
        spec = [query.topic, query.sort, query.limit]
        if existing is not None:
            if existing.get("feedback_spec") != spec:
                existing.pop("channel_yield", None)
            existing["feedback_spec"] = spec
    if job.get("revision") != plan.revision:
        query_state = {
            query.query: dict(query_state.get(query.query) or _query_template())
            for query in plan.queries
        }
        job.update(
            {
                "revision": plan.revision,
                "controller_id": plan.controller_id,
                "plan_created_at": plan.created_at,
                "complete": False,
                "next_run_ts": min(
                    float(job.get("next_run_ts") or 0.0),
                    now_ts,
                ),
                "last_error": "",
                "queries": query_state,
            }
        )
    else:
        for query in plan.queries:
            query_state.setdefault(query.query, _query_template())
        allowed = {query.query for query in plan.queries}
        job["queries"] = {
            query: value
            for query, value in query_state.items()
            if query in allowed
        }
    _ensure_turbo_controller(job, plan)
    return job


def _query_ready(query_state: dict[str, Any], now_ts: float) -> bool:
    return float(query_state.get("retry_after_ts") or 0.0) <= now_ts


def select_scheduled_queries(
    plan: DiscoveryPlan,
    job_state: dict[str, Any],
    *,
    now_ts: float,
    tick: int,
    limit: int | None = None,
) -> list[ScheduledQuery]:
    """Pick unseen first, then productive old searches, then periodic reprobes."""
    states = job_state.setdefault("queries", {})
    indexed = list(enumerate(plan.queries))
    unseen: list[tuple[int, Any]] = []
    active: list[tuple[int, Any]] = []
    retired: list[tuple[int, Any]] = []
    for index, query in indexed:
        query_state = states.setdefault(query.query, _query_template())
        if not _query_ready(query_state, now_ts):
            continue
        attempts = int(query_state.get("attempts") or 0)
        if attempts <= 0:
            unseen.append((index, query))
            continue
        if (
            plan.settings.adaptive
            and int(query_state.get("zero_streak") or 0)
            >= plan.settings.retire_after
        ):
            retired.append((index, query))
        else:
            active.append((index, query))

    active.sort(
        key=lambda item: (
            float(states[item[1].query].get("last_completed_ts") or 0.0),
            item[0],
        )
    )
    retired.sort(
        key=lambda item: (
            float(states[item[1].query].get("last_completed_ts") or 0.0),
            item[0],
        )
    )
    if unseen:
        candidates = unseen
    else:
        if retired and tick % plan.settings.reprobe_every == 0:
            candidates = retired + active
        else:
            candidates = active

    batch_limit = (
        plan.settings.batch_size
        if limit is None
        else max(1, min(plan.settings.batch_size, int(limit)))
    )
    job_state["search_allocation"] = {"policy": "legacy-video-yield"}
    if plan.settings.adaptive and not unseen:
        allocation = allocate_queries(active + retired, states,
                                      batch_limit=batch_limit, tick=tick)
        if allocation is not None:
            candidates, job_state["search_allocation"] = allocation
    selected: list[ScheduledQuery] = []
    for _index, query in candidates[:batch_limit]:
        query_state = states[query.query]
        attempts = int(query_state.get("attempts") or 0)
        if query.sort:
            effective_sort = query.sort
        elif plan.settings.adaptive and attempts > 0:
            effective_sort = "newest"
        else:
            effective_sort = ""
        selected.append(
            ScheduledQuery(
                spec=query,
                sort=effective_sort,
                previous_video_ids=tuple(
                    str(value)
                    for value in query_state.get("last_video_ids", [])
                    if value
                ),
                previous_video_views=tuple(
                    (
                        str(video_id),
                        max(0, int(views or 0)),
                    )
                    for video_id, views in (
                        query_state.get("last_video_views") or {}
                    ).items()
                    if video_id
                ),
            )
        )
    return selected


def _remaining_untested(plan: DiscoveryPlan, job: dict[str, Any]) -> int:
    states = job.get("queries", {})
    return sum(
        1
        for query in plan.queries
        if int((states.get(query.query) or {}).get("attempts") or 0) <= 0
    )


def _earliest_retry(plan: DiscoveryPlan, job: dict[str, Any]) -> float:
    states = job.get("queries", {})
    values = [
        float((states.get(query.query) or {}).get("retry_after_ts") or 0.0)
        for query in plan.queries
        if int((states.get(query.query) or {}).get("attempts") or 0) <= 0
        and float((states.get(query.query) or {}).get("retry_after_ts") or 0.0) > 0
    ]
    return min(values) if values else 0.0


def _apply_outcomes(
    job: dict[str, Any],
    selected: list[ScheduledQuery],
    batch: CollectionBatch,
    *,
    now_ts: float,
) -> None:
    states = job.setdefault("queries", {})
    by_query = {outcome.query: outcome for outcome in batch.outcomes}
    for scheduled in selected:
        state = states.setdefault(scheduled.spec.query, _query_template())
        outcome = by_query.get(scheduled.spec.query)
        if outcome is None or outcome.status != "ok":
            failures = int(state.get("failures") or 0) + 1
            state["failures"] = failures
            state["last_error"] = (
                outcome.error if outcome is not None else "collector returned no outcome"
            )
            retry_delay = min(3_600, 60 * (2 ** min(failures - 1, 6)))
            state["retry_after_ts"] = now_ts + retry_delay
            continue

        video_ids = list(outcome.video_ids)
        previous_views = {
            str(video_id): max(0, int(views or 0))
            for video_id, views in scheduled.previous_video_views
            if video_id
        }
        current_views: dict[str, int] = {}
        for video in outcome.videos:
            if not video.video_id:
                continue
            views = max(0, int(video.views or 0))
            if views > 0:
                current_views[video.video_id] = views
            elif video.video_id in previous_views:
                current_views[video.video_id] = previous_views[video.video_id]
        state["attempts"] = int(state.get("attempts") or 0) + 1
        state["failures"] = 0
        # Retire on NO-NEW-RESULTS regardless of whether the response carried videos: a query
        # that keeps returning an empty set (or only stale, already-seen videos) is dead and
        # must accrue a streak so select_scheduled_queries can retire it — otherwise the most
        # useless searches hog a batch slot forever.
        if outcome.new_result_count > 0:
            state["zero_streak"] = 0
        else:
            state["zero_streak"] = int(state.get("zero_streak") or 0) + 1
        state["last_video_ids"] = video_ids
        state["last_video_views"] = current_views
        state["last_result_count"] = len(video_ids)
        state["last_new_result_count"] = outcome.new_result_count
        state["last_completed_ts"] = now_ts
        state["last_error"] = ""
        state["retry_after_ts"] = 0.0


class EdgeWorker:
    def __init__(
        self,
        *,
        paths: EdgeWorkerPaths,
        signing_key: str | bytes,
        node_id: str | None = None,
        collect_fn: Callable[..., CollectionBatch] = collect_discovery_batch,
        organization_jobs_dir: str | os.PathLike | None = None,
        organization_state_path: str | os.PathLike | None = None,
        organization_job_writer: Callable[..., list[Path]] | None = None,
    ) -> None:
        if not signing_key:
            raise ValueError("the edge worker requires a signing key")
        self.paths = paths
        self.signing_key = signing_key
        self.node_id = node_id or default_node_id()
        if not _NODE_ID_RE.fullmatch(self.node_id):
            raise ValueError(
                "node_id must start with a letter/number and use only "
                "letters, numbers, dot, underscore, or hyphen"
            )
        self.collect_fn = collect_fn
        self.organization_jobs_dir = (
            Path(organization_jobs_dir)
            if organization_jobs_dir is not None
            else None
        )
        self.organization_state_path = (
            Path(organization_state_path)
            if organization_state_path is not None
            else None
        )
        self.organization_job_writer = organization_job_writer
        # Progress heartbeat, read by the stall watchdog in run_forever.
        self._heartbeat = time.monotonic()
        self.paths.ensure()
        if self.organization_jobs_dir is not None:
            self.organization_jobs_dir.mkdir(parents=True, exist_ok=True)

    def _enqueue_organization_jobs(
        self,
        *,
        plan: DiscoveryPlan,
        batch: CollectionBatch,
        run_id: str,
        now_ts: float,
    ) -> tuple[int, str]:
        if self.organization_jobs_dir is None:
            return 0, ""
        new_video_ids: set[str] = set()
        new_channel_ids: set[str] = set()
        for outcome in batch.outcomes:
            if outcome.status != "ok":
                continue
            previous = set(outcome.previous_video_ids)
            for video in outcome.videos:
                if not video.video_id or video.video_id in previous:
                    continue
                new_video_ids.add(video.video_id)
                if video.channel_id:
                    new_channel_ids.add(video.channel_id)
        if not new_video_ids:
            return 0, ""
        refresh_after_days = 30
        if self.organization_state_path is not None:
            try:
                recent = _recent_organization_channels(
                    self.organization_state_path,
                    new_channel_ids,
                    cutoff_ts=(
                        now_ts - (refresh_after_days * 86_400)
                    ),
                )
                new_channel_ids.difference_update(recent)
            except (OSError, sqlite3.Error):
                pass
        if not new_channel_ids:
            return 0, ""
        videos = [
            row
            for row in batch.records.get("videos", [])
            if (
                str(row.get("video_id") or "") in new_video_ids
                and str(row.get("channel_id") or "") in new_channel_ids
            )
        ]
        new_channel_ids.update(
            str(row.get("channel_id") or "")
            for row in videos
            if row.get("channel_id")
        )
        channels = [
            row
            for row in batch.records.get("channels", [])
            if str(row.get("channel_id") or "") in new_channel_ids
        ]
        if not channels and not videos:
            return 0, ""
        writer = self.organization_job_writer
        if writer is None:
            from beacon_engine.edge.organization import (
                write_discovery_organization_jobs,
            )

            writer = write_discovery_organization_jobs
        try:
            paths = writer(
                channels=channels,
                videos=videos,
                jobs_dir=self.organization_jobs_dir,
                controller_id=self.node_id,
                source_job_id=plan.job_id,
                source_run_id=run_id,
                signing_key=self.signing_key,
                created_at=batch.captured_at,
                settings={
                    "catalog_workers": 8,
                    "face_workers": 2,
                    "refresh_after_days": refresh_after_days,
                },
                channels_per_job=100,
            )
            return len(paths), ""
        except Exception as exc:  # noqa: BLE001
            return 0, _safe_error(exc)

    def load_state(self) -> dict[str, Any]:
        state = _load_json(self.paths.state, _empty_state(self.node_id))
        if int(state.get("version") or 0) != STATE_VERSION:
            state = _empty_state(self.node_id)
        state["node_id"] = self.node_id
        state.setdefault("jobs", {})
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        state["node_id"] = self.node_id
        state["updated_at"] = utc_now()
        _atomic_json(self.paths.state, state)

    def write_status(self, state: str, message: str = "", **extra: Any) -> None:
        # Every status write is a proof of progress for the stall watchdog.
        self._heartbeat = time.monotonic()
        payload = {
            "version": STATE_VERSION,
            "node_id": self.node_id,
            "pid": os.getpid(),
            "state": state,
            "message": message,
            "updated_at": utc_now(),
        }
        payload.update(extra)
        _atomic_json(self.paths.status, payload)

    def load_active_plans(self) -> tuple[list[DiscoveryPlan], list[str]]:
        latest: dict[str, DiscoveryPlan] = {}
        errors: list[str] = []
        for path in sorted(self.paths.jobs.glob("*.signal-edge.json.gz")):
            try:
                plan = load_discovery_plan(
                    path,
                    signing_key=self.signing_key,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            existing = latest.get(plan.job_id)
            if existing is None or (
                plan.created_at,
                plan.revision,
            ) > (
                existing.created_at,
                existing.revision,
            ):
                latest[plan.job_id] = plan
        return [latest[key] for key in sorted(latest)], errors

    def _run_plan(
        self,
        plan: DiscoveryPlan,
        state: dict[str, Any],
        *,
        now_ts: float,
    ) -> WorkerRunReport:
        job = _ensure_job_state(state, plan, now_ts=now_ts)
        consume_feedback(job, ack_dir=self.paths.root / "acks",
                         node_id=self.node_id, signing_key=self.signing_key)
        if bool(job.get("complete")):
            return WorkerRunReport(
                job_id=plan.job_id,
                status="complete",
                **_turbo_report_fields(job, plan),
            )
        next_run_ts = float(job.get("next_run_ts") or 0.0)
        if next_run_ts > now_ts:
            return WorkerRunReport(
                job_id=plan.job_id,
                status="waiting",
                remaining_untested=_remaining_untested(plan, job),
                next_run_at=_epoch_iso(next_run_ts),
                **_turbo_report_fields(job, plan),
            )

        tick = int(job.get("ticks") or 0) + 1
        controller = _ensure_turbo_controller(job, plan)
        selected = select_scheduled_queries(
            plan,
            job,
            now_ts=now_ts,
            tick=tick,
            limit=int(controller["effective_batch_size"]),
        )
        job["ticks"] = tick
        if not selected:
            retry_at = _earliest_retry(plan, job)
            next_run_ts = (
                retry_at
                if retry_at > now_ts
                else now_ts + _steady_delay_seconds(plan, controller)
            )
            job["next_run_ts"] = next_run_ts
            return WorkerRunReport(
                job_id=plan.job_id,
                status="waiting",
                remaining_untested=_remaining_untested(plan, job),
                next_run_at=_epoch_iso(next_run_ts),
                message="no queries are due in this scheduler tick",
                **_turbo_report_fields(job, plan),
            )

        run_number = int(job.get("runs") or 0) + 1
        run_id = f"run-{run_number:08d}"
        captured_at = _epoch_iso(now_ts)
        self.write_status(
            "running",
            f"collecting {len(selected)} queries for {plan.job_id}",
            job_id=plan.job_id,
            run_id=run_id,
            queries=[item.spec.query for item in selected[:10]],
            query_count=len(selected),
            turbo_controller=dict(controller),
        )
        try:
            batch = self.collect_fn(
                plan,
                selected,
                node_id=self.node_id,
                run_id=run_id,
                captured_at=captured_at,
            )
            result = build_bundle(
                node_id=self.node_id,
                kind=DISCOVERY_BUNDLE_KIND,
                job_id=plan.job_id,
                records=batch.records,
                metadata={
                    **batch.metadata(),
                    "plan_revision": plan.revision,
                    "controller_id": plan.controller_id,
                    "scheduler_tick": tick,
                },
                created_at=batch.captured_at,
                signing_key=self.signing_key,
            )
            bundle_path = write_bundle(result, self.paths.outbox)
        except Exception as exc:  # noqa: BLE001
            job["last_error"] = _safe_error(exc)
            controller = _update_turbo_controller(
                job,
                plan,
                attempted=len(selected),
                failed=len(selected),
            )
            delay = (
                _steady_delay_seconds(plan, controller)
                if plan.settings.turbo_enabled
                else 60
            )
            job["next_run_ts"] = now_ts + delay
            return WorkerRunReport(
                job_id=plan.job_id,
                status="error",
                run_id=run_id,
                remaining_untested=_remaining_untested(plan, job),
                next_run_at=_epoch_iso(job["next_run_ts"]),
                message=job["last_error"],
                **_turbo_report_fields(job, plan),
            )

        organization_jobs, organization_error = (
            self._enqueue_organization_jobs(
                plan=plan,
                batch=batch,
                run_id=run_id,
                now_ts=now_ts,
            )
        )
        _apply_outcomes(job, selected, batch, now_ts=now_ts)
        controller = _update_turbo_controller(
            job,
            plan,
            attempted=len(selected),
            failed=batch.failed,
        )
        job["runs"] = run_number
        job["last_run_at"] = captured_at
        job["last_bundle_id"] = result.bundle_id
        job["last_bundle_path"] = str(bundle_path)
        job["last_error"] = ""
        job["last_organization_jobs"] = organization_jobs
        job["last_organization_error"] = organization_error
        remember_bundle(job, result, run_number=run_number)
        remaining = _remaining_untested(plan, job)
        if remaining > 0:
            retry_at = _earliest_retry(plan, job)
            catchup_delay = (
                _steady_delay_seconds(plan, controller)
                if bool(controller.get("backing_off"))
                else plan.settings.catchup_delay_seconds
            )
            catchup_at = now_ts + catchup_delay
            job["next_run_ts"] = (
                retry_at
                if retry_at > catchup_at
                and not any(
                    int((job["queries"].get(query.query) or {}).get("attempts") or 0)
                    <= 0
                    and _query_ready(job["queries"][query.query], catchup_at)
                    for query in plan.queries
                )
                else catchup_at
            )
        elif plan.settings.repeat:
            job["next_run_ts"] = (
                now_ts + _steady_delay_seconds(plan, controller)
            )
        else:
            job["complete"] = True
            job["next_run_ts"] = 0.0

        return WorkerRunReport(
            job_id=plan.job_id,
            status="completed",
            run_id=run_id,
            bundle_path=str(bundle_path),
            queries=len(selected),
            succeeded=batch.succeeded,
            failed=batch.failed,
            organization_jobs=organization_jobs,
            remaining_untested=remaining,
            next_run_at=(
                _epoch_iso(float(job["next_run_ts"]))
                if float(job.get("next_run_ts") or 0.0) > 0
                else ""
            ),
            message=(
                f"organization enqueue warning: {organization_error}"
                if organization_error
                else ""
            ),
            **_turbo_report_fields(job, plan),
        )

    def run_once(self, *, now_ts: float | None = None) -> list[WorkerRunReport]:
        now = float(time.time() if now_ts is None else now_ts)
        state = self.load_state()
        plans, invalid = self.load_active_plans()
        if not plans:
            self.save_state(state)
            self.write_status(
                "idle",
                "no valid discovery plans",
                invalid_plans=invalid,
            )
            return []

        reports: list[WorkerRunReport] = []
        for plan in plans:
            reports.append(self._run_plan(plan, state, now_ts=now))
            self.save_state(state)
        self.write_status(
            "idle",
            "scheduler pass complete",
            invalid_plans=invalid,
            reports=[report.as_dict() for report in reports],
            discovery_selection={plan.job_id: {
                "allocation": state["jobs"][plan.job_id].get("search_allocation", {}),
                "feedback": state["jobs"][plan.job_id].get("channel_feedback", {}),
            } for plan in plans},
        )
        return reports

    def _start_stall_watchdog(
        self,
        stop_event: threading.Event,
        stall_seconds: int,
    ) -> None:
        """Exit non-zero if no status write happens for `stall_seconds`.

        A deadlocked thread pool cannot be unwound from inside the process, so
        this deliberately uses os._exit: a clean shutdown would itself block on
        the hung threads. Exiting non-zero hands recovery to systemd.
        """
        if stall_seconds <= 0:
            return
        interval = max(15.0, min(60.0, stall_seconds / 10.0))

        def watch() -> None:
            while not stop_event.wait(interval):
                idle = time.monotonic() - self._heartbeat
                if idle < stall_seconds:
                    continue
                detail = (
                    f"edge worker stalled: no progress for {int(idle)}s "
                    f"(limit {stall_seconds}s); exiting "
                    f"{EDGE_STALL_EXIT_CODE} so systemd restarts it"
                )
                try:
                    self.write_status("stalled", detail)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    sys.stderr.write(detail + "\n")
                    sys.stderr.flush()
                except Exception:  # noqa: BLE001
                    pass
                os._exit(EDGE_STALL_EXIT_CODE)

        threading.Thread(
            target=watch,
            name="edge-stall-watchdog",
            daemon=True,
        ).start()

    def run_forever(
        self,
        *,
        poll_seconds: int = 15,
        stall_seconds: int | None = None,
    ) -> None:
        delay = max(1, min(int(poll_seconds), 300))
        stop_event = threading.Event()

        def request_stop(_signum=None, _frame=None) -> None:
            stop_event.set()

        previous_handlers: dict[int, Any] = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
            except (ValueError, OSError):
                pass
        try:
            with EdgeWorkerLock(self.paths.lock):
                self.write_status("starting", "edge worker starting")
                self._start_stall_watchdog(
                    stop_event,
                    _configured_stall_seconds()
                    if stall_seconds is None
                    else int(stall_seconds),
                )
                while not stop_event.is_set():
                    if self.paths.stop.exists():
                        try:
                            self.paths.stop.unlink()
                        except OSError:
                            pass
                        break
                    try:
                        self.run_once()
                    except Exception as exc:  # noqa: BLE001
                        self.write_status(
                            "error",
                            _safe_error(exc),
                        )
                    stop_event.wait(delay)
                self.write_status("stopped", "edge worker stopped cooperatively")
        finally:
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass


def read_worker_status(path: str | os.PathLike) -> dict[str, Any]:
    return _load_json(Path(path), {})
