"""Signed discovery plans consumed by an always-on Signal edge worker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from beacon_engine.edge.protocol import (
    EdgeBundle,
    EdgeProtocolError,
    build_bundle,
    load_bundle,
)

DISCOVERY_PLAN_KIND = "youtube.discovery.plan.v1"
MAX_PLAN_QUERIES = 10_000
MAX_QUERY_LENGTH = 200
MAX_RESULTS_PER_BATCH = 5_000
_SPACE_RE = re.compile(r"\s+")
_SORTS = {"", "relevance", "newest"}
_MODES = {"free", "hybrid", "official"}


@dataclass(frozen=True)
class EdgeQuery:
    query: str
    topic: str = ""
    limit: int = 40
    sort: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "topic": self.topic,
            "limit": self.limit,
            "sort": self.sort,
        }


@dataclass(frozen=True)
class DiscoveryPlanSettings:
    batch_size: int = 30
    workers: int = 6
    interval_seconds: int = 600
    catchup_delay_seconds: int = 5
    mode: str = "hybrid"
    max_quota_units_per_run: int = 100
    adaptive: bool = True
    retire_after: int = 3
    reprobe_every: int = 20
    repeat: bool = True
    turbo_enabled: bool = False
    target_channels_per_hour: int = 0
    expected_channels_per_1000_queries: int = 0
    min_batch_size: int = 1
    failure_backoff_percent: int = 5
    batch_recovery_step: int = 10
    healthy_runs_to_recover: int = 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "workers": self.workers,
            "interval_seconds": self.interval_seconds,
            "catchup_delay_seconds": self.catchup_delay_seconds,
            "mode": self.mode,
            "max_quota_units_per_run": self.max_quota_units_per_run,
            "adaptive": self.adaptive,
            "retire_after": self.retire_after,
            "reprobe_every": self.reprobe_every,
            "repeat": self.repeat,
            "turbo_enabled": self.turbo_enabled,
            "target_channels_per_hour": self.target_channels_per_hour,
            "expected_channels_per_1000_queries": (
                self.expected_channels_per_1000_queries
            ),
            "min_batch_size": self.min_batch_size,
            "failure_backoff_percent": self.failure_backoff_percent,
            "batch_recovery_step": self.batch_recovery_step,
            "healthy_runs_to_recover": self.healthy_runs_to_recover,
        }


@dataclass(frozen=True)
class DiscoveryPlan:
    bundle: EdgeBundle
    job_id: str
    controller_id: str
    queries: tuple[EdgeQuery, ...]
    settings: DiscoveryPlanSettings

    @property
    def revision(self) -> str:
        return self.bundle.bundle_id

    @property
    def created_at(self) -> str:
        return str(self.bundle.payload.get("created_at") or "")


def _clean_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    if not text and not allow_empty:
        raise EdgeProtocolError(f"{field} is required")
    if len(text) > MAX_QUERY_LENGTH:
        raise EdgeProtocolError(
            f"{field} exceeds the {MAX_QUERY_LENGTH}-character limit"
        )
    return text


def _integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EdgeProtocolError(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise EdgeProtocolError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return number


def _boolean(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise EdgeProtocolError(f"{field} must be true or false")


def _normalize_query(value: str | Mapping[str, Any]) -> EdgeQuery:
    if isinstance(value, str):
        data: Mapping[str, Any] = {"query": value}
    elif isinstance(value, Mapping):
        data = value
    else:
        raise EdgeProtocolError("each discovery query must be text or an object")
    query = _clean_text(data.get("query"), "query")
    topic = _clean_text(data.get("topic"), "topic", allow_empty=True)
    limit = _integer(
        data.get("limit", 40),
        "query limit",
        minimum=1,
        maximum=200,
    )
    sort = str(data.get("sort") or "").strip().lower()
    if sort not in _SORTS:
        raise EdgeProtocolError(
            "query sort must be empty, 'relevance', or 'newest'"
        )
    return EdgeQuery(query=query, topic=topic, limit=limit, sort=sort)


def normalize_queries(
    queries: Iterable[str | Mapping[str, Any] | EdgeQuery],
) -> tuple[EdgeQuery, ...]:
    normalized: list[EdgeQuery] = []
    seen: set[str] = set()
    for value in queries:
        query = value if isinstance(value, EdgeQuery) else _normalize_query(value)
        key = query.query.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(query)
        if len(normalized) > MAX_PLAN_QUERIES:
            raise EdgeProtocolError(
                f"a discovery plan may contain at most {MAX_PLAN_QUERIES:,} queries"
            )
    if not normalized:
        raise EdgeProtocolError("a discovery plan requires at least one query")
    return tuple(normalized)


def _normalize_settings(
    value: DiscoveryPlanSettings | Mapping[str, Any] | None,
) -> DiscoveryPlanSettings:
    if isinstance(value, DiscoveryPlanSettings):
        data: Mapping[str, Any] = value.as_dict()
    else:
        data = value or {}
    mode = str(data.get("mode", "hybrid") or "").strip().lower()
    if mode not in _MODES:
        raise EdgeProtocolError(
            "plan mode must be 'free', 'hybrid', or 'official'"
        )
    # In OFFICIAL mode every search costs 100 quota units, so a per-run budget below 100 could
    # never run a single search — the worker would retry every query forever making zero
    # progress. (Hybrid/free searches are quota-free — only enrichment uses quota — so only
    # official needs this floor.)
    if mode == "official" and _integer(
        data.get("max_quota_units_per_run", 100),
        "max_quota_units_per_run",
        minimum=0,
        maximum=5_000,
    ) < 100:
        raise EdgeProtocolError(
            "max_quota_units_per_run must be at least 100 for 'official' mode "
            "(one official search costs 100 units)"
        )
    settings = DiscoveryPlanSettings(
        batch_size=_integer(
            data.get("batch_size", 30),
            "batch_size",
            minimum=1,
            maximum=200,
        ),
        workers=_integer(
            data.get("workers", 6),
            "workers",
            minimum=1,
            maximum=32,
        ),
        interval_seconds=_integer(
            data.get("interval_seconds", 600),
            "interval_seconds",
            minimum=60,
            maximum=86_400,
        ),
        catchup_delay_seconds=_integer(
            data.get("catchup_delay_seconds", 5),
            "catchup_delay_seconds",
            minimum=1,
            maximum=3_600,
        ),
        mode=mode,
        max_quota_units_per_run=_integer(
            data.get("max_quota_units_per_run", 100),
            "max_quota_units_per_run",
            minimum=0,
            maximum=5_000,
        ),
        adaptive=_boolean(data.get("adaptive", True), "adaptive"),
        retire_after=_integer(
            data.get("retire_after", 3),
            "retire_after",
            minimum=1,
            maximum=20,
        ),
        reprobe_every=_integer(
            data.get("reprobe_every", 20),
            "reprobe_every",
            minimum=2,
            maximum=1_000,
        ),
        repeat=_boolean(data.get("repeat", True), "repeat"),
        turbo_enabled=_boolean(
            data.get("turbo_enabled", False),
            "turbo_enabled",
        ),
        target_channels_per_hour=_integer(
            data.get("target_channels_per_hour", 0),
            "target_channels_per_hour",
            minimum=0,
            maximum=100_000,
        ),
        expected_channels_per_1000_queries=_integer(
            data.get("expected_channels_per_1000_queries", 0),
            "expected_channels_per_1000_queries",
            minimum=0,
            maximum=10_000,
        ),
        min_batch_size=_integer(
            data.get("min_batch_size", 1),
            "min_batch_size",
            minimum=1,
            maximum=200,
        ),
        failure_backoff_percent=_integer(
            data.get("failure_backoff_percent", 5),
            "failure_backoff_percent",
            minimum=1,
            maximum=100,
        ),
        batch_recovery_step=_integer(
            data.get("batch_recovery_step", 10),
            "batch_recovery_step",
            minimum=1,
            maximum=200,
        ),
        healthy_runs_to_recover=_integer(
            data.get("healthy_runs_to_recover", 2),
            "healthy_runs_to_recover",
            minimum=1,
            maximum=20,
        ),
    )
    if settings.min_batch_size > settings.batch_size:
        raise EdgeProtocolError("min_batch_size may not exceed batch_size")
    if settings.turbo_enabled:
        if settings.target_channels_per_hour <= 0:
            raise EdgeProtocolError(
                "turbo plans require target_channels_per_hour"
            )
        if settings.expected_channels_per_1000_queries <= 0:
            raise EdgeProtocolError(
                "turbo plans require expected_channels_per_1000_queries"
            )
    return settings


def _validate_batch_capacity(
    queries: tuple[EdgeQuery, ...],
    settings: DiscoveryPlanSettings,
) -> None:
    largest_query = max(query.limit for query in queries)
    projected = settings.batch_size * largest_query
    if projected > MAX_RESULTS_PER_BATCH:
        raise EdgeProtocolError(
            "batch_size multiplied by the largest query limit may not exceed "
            f"{MAX_RESULTS_PER_BATCH:,} staged results"
        )


def build_discovery_plan(
    *,
    controller_id: str,
    job_id: str,
    queries: Iterable[str | Mapping[str, Any] | EdgeQuery],
    settings: DiscoveryPlanSettings | Mapping[str, Any] | None = None,
    created_at: str | None = None,
    signing_key: str | bytes | None,
) -> EdgeBundle:
    """Create one immutable plan revision without embedding credentials."""
    clean_queries = normalize_queries(queries)
    clean_settings = _normalize_settings(settings)
    _validate_batch_capacity(clean_queries, clean_settings)
    return build_bundle(
        node_id=controller_id,
        kind=DISCOVERY_PLAN_KIND,
        job_id=job_id,
        records={"queries": [query.as_record() for query in clean_queries]},
        metadata={
            "settings": clean_settings.as_dict(),
            "query_count": len(clean_queries),
        },
        created_at=created_at,
        signing_key=signing_key,
    )


def plan_from_bundle(bundle: EdgeBundle) -> DiscoveryPlan:
    if bundle.kind != DISCOVERY_PLAN_KIND:
        raise EdgeProtocolError(
            f"expected plan kind '{DISCOVERY_PLAN_KIND}', got '{bundle.kind}'"
        )
    job_id = str(bundle.payload.get("job_id") or "").strip()
    if not job_id:
        raise EdgeProtocolError("a discovery plan requires a job_id")
    unknown = set(bundle.records) - {"queries"}
    if unknown:
        raise EdgeProtocolError(
            "unsupported discovery plan datasets: " + ", ".join(sorted(unknown))
        )
    queries = normalize_queries(bundle.records.get("queries", []))
    metadata = bundle.payload.get("metadata")
    if not isinstance(metadata, dict):
        raise EdgeProtocolError("plan metadata must be an object")
    settings = _normalize_settings(metadata.get("settings"))
    _validate_batch_capacity(queries, settings)
    expected_count = metadata.get("query_count")
    if expected_count is not None and int(expected_count) != len(queries):
        raise EdgeProtocolError("plan query_count does not match its query records")
    return DiscoveryPlan(
        bundle=bundle,
        job_id=job_id,
        controller_id=bundle.node_id,
        queries=queries,
        settings=settings,
    )


def load_discovery_plan(
    path: str | Path,
    *,
    signing_key: str | bytes | None,
    require_signature: bool = True,
) -> DiscoveryPlan:
    return plan_from_bundle(
        load_bundle(
            path,
            signing_key=signing_key,
            require_signature=require_signature,
        )
    )


def queries_from_radar_config(path: str | Path) -> tuple[EdgeQuery, ...]:
    """Snapshot the shared radar watchlist into portable edge query records."""
    from beacon_engine.research.channel_discovery import expand_queries
    from beacon_engine.research.radar import load_config

    config = load_config(path)
    records: list[EdgeQuery] = []
    seen: set[str] = set()
    for base in config.watchlist:
        topic = _clean_text(base, "watchlist topic")
        expanded = expand_queries([topic]) if config.harvest_expand else [topic]
        for query_text in expanded:
            key = query_text.casefold()
            if key in seen:
                continue
            seen.add(key)
            records.append(
                EdgeQuery(
                    query=query_text,
                    topic=topic,
                    limit=max(1, min(200, int(config.harvest_per_query))),
                )
            )
    return normalize_queries(records)
