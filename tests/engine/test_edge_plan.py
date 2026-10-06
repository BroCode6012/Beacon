from __future__ import annotations

import json

import pytest

from beacon_engine.edge.plan import (
    DISCOVERY_PLAN_KIND,
    DiscoveryPlanSettings,
    build_discovery_plan,
    load_discovery_plan,
    queries_from_radar_config,
)
from beacon_engine.edge.protocol import EdgeProtocolError, write_bundle


SIGNING_KEY = "test-only-edge-signing-key"


def test_signed_discovery_plan_round_trip_and_deduplicates(tmp_path):
    bundle = build_discovery_plan(
        controller_id="signal-laptop",
        job_id="radar-discovery",
        queries=[
            "Forgotten empires documentary",
            {"query": "  forgotten   empires documentary  ", "limit": 80},
            {
                "query": "business failures explained",
                "topic": "business documentaries",
                "limit": 50,
                "sort": "newest",
            },
        ],
        settings=DiscoveryPlanSettings(
            batch_size=24,
            workers=8,
            interval_seconds=900,
            catchup_delay_seconds=3,
            max_quota_units_per_run=75,
            turbo_enabled=True,
            target_channels_per_hour=450,
            expected_channels_per_1000_queries=340,
            min_batch_size=12,
            failure_backoff_percent=5,
            batch_recovery_step=4,
            healthy_runs_to_recover=3,
        ),
        created_at="2026-07-12T15:00:00Z",
        signing_key=SIGNING_KEY,
    )
    path = write_bundle(bundle, tmp_path)

    plan = load_discovery_plan(path, signing_key=SIGNING_KEY)

    assert plan.bundle.kind == DISCOVERY_PLAN_KIND
    assert plan.job_id == "radar-discovery"
    assert plan.controller_id == "signal-laptop"
    assert plan.revision == bundle.bundle_id
    assert [item.query for item in plan.queries] == [
        "Forgotten empires documentary",
        "business failures explained",
    ]
    assert plan.queries[1].topic == "business documentaries"
    assert plan.queries[1].sort == "newest"
    assert plan.settings.batch_size == 24
    assert plan.settings.workers == 8
    assert plan.settings.mode == "hybrid"
    assert plan.settings.turbo_enabled is True
    assert plan.settings.target_channels_per_hour == 450
    assert plan.settings.expected_channels_per_1000_queries == 340
    assert plan.settings.min_batch_size == 12
    assert plan.settings.batch_recovery_step == 4
    assert plan.settings.healthy_runs_to_recover == 3


def test_plan_rejects_invalid_or_unsafe_limits():
    with pytest.raises(EdgeProtocolError, match="at least one query"):
        build_discovery_plan(
            controller_id="signal-laptop",
            job_id="empty",
            queries=[],
            signing_key=SIGNING_KEY,
        )
    with pytest.raises(EdgeProtocolError, match="batch_size"):
        build_discovery_plan(
            controller_id="signal-laptop",
            job_id="oversized",
            queries=["history documentary"],
            settings={"batch_size": 500},
            signing_key=SIGNING_KEY,
        )
    with pytest.raises(EdgeProtocolError, match="plan mode"):
        build_discovery_plan(
            controller_id="signal-laptop",
            job_id="bad-mode",
            queries=["history documentary"],
            settings={"mode": "unbounded"},
            signing_key=SIGNING_KEY,
        )
    with pytest.raises(EdgeProtocolError, match="staged results"):
        build_discovery_plan(
            controller_id="signal-laptop",
            job_id="too-large-batch",
            queries=[{"query": "history documentary", "limit": 200}],
            settings={"batch_size": 50},
            signing_key=SIGNING_KEY,
        )
    with pytest.raises(EdgeProtocolError, match="min_batch_size"):
        build_discovery_plan(
            controller_id="signal-laptop",
            job_id="bad-turbo-floor",
            queries=["history documentary"],
            settings={
                "batch_size": 20,
                "min_batch_size": 21,
            },
            signing_key=SIGNING_KEY,
        )


def test_plan_signature_and_kind_are_enforced(tmp_path):
    bundle = build_discovery_plan(
        controller_id="signal-laptop",
        job_id="signed-plan",
        queries=["history documentary"],
        signing_key=SIGNING_KEY,
    )
    path = write_bundle(bundle, tmp_path)

    with pytest.raises(EdgeProtocolError, match="signature"):
        load_discovery_plan(path, signing_key="wrong-key")


def test_radar_config_snapshot_preserves_base_topic_and_expands(tmp_path):
    config_path = tmp_path / "scraper_watchlist.json"
    config_path.write_text(
        json.dumps(
            {
                "watchlist": [
                    "ancient engineering",
                    "business failures explained",
                ],
                "harvest_expand": True,
                "harvest_per_query": 55,
            }
        ),
        encoding="utf-8",
    )

    queries = queries_from_radar_config(config_path)
    by_text = {item.query: item for item in queries}

    assert "ancient engineering" in by_text
    assert "ancient engineering documentary" in by_text
    assert by_text["ancient engineering documentary"].topic == "ancient engineering"
    assert by_text["ancient engineering documentary"].limit == 55
    assert "business failures explained" in by_text
    assert "business failures explained explained" not in by_text
