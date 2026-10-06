from __future__ import annotations

import json
from unittest.mock import patch

from beacon_engine.research.query_topup import (
    maybe_top_up_queries,
    query_coverage,
    rank_candidates,
)
from beacon_engine.research.radar import RadarConfig, load_config
from beacon_engine.research.store import YouTubeStore


def _rows(title: str, prefix: str, channels: int = 5):
    return [
        (title, f"{prefix}-{index}", 100_000 + index, "")
        for index in range(channels)
    ]


def test_coverage_counts_a_successful_empty_attempt():
    summary = query_coverage(
        ["alpha topic"],
        {"alpha topic": {"attempts": 1, "searches": 0}},
        expand=False,
    )

    assert summary["attempted"] == 1
    assert summary["remaining"] == 0
    assert summary["coverage"] == 1.0


def test_topup_config_is_bounded_and_preserves_policy(tmp_path):
    config_path = tmp_path / "radar.json"
    config_path.write_text(
        json.dumps({
            "query_topup_enabled": True,
            "query_topup_max_total": 999,
            "query_topup_max_per_run": 99,
            "query_topup_min_coverage": 2,
            "query_topup_evidence_limit": 10,
            "signal_query_policy": {
                "last_added_ts": 123.0,
                "recent_additions": [{"query": "safe topic"}],
            },
        }),
        encoding="utf-8",
    )

    loaded = load_config(config_path)

    assert loaded.query_topup_enabled is True
    assert loaded.query_topup_max_total == 600
    assert loaded.query_topup_max_per_run == 2
    assert loaded.query_topup_min_coverage == 1.0
    assert loaded.query_topup_evidence_limit == 1000
    assert loaded.signal_query_policy["last_added_ts"] == 123.0
    assert loaded.signal_query_policy["recent_additions"] == [
        {"query": "safe topic"}
    ]


def test_ranked_candidates_require_cross_channel_evidence():
    rows = (
        _rows("Animal survival strategies in the wild", "nature")
        + _rows("Historical unsolved mysteries finally decoded", "history")
    )

    ranked = rank_candidates(
        rows,
        ["existing useful topic"],
        {},
        {},
        min_channels=3,
        min_videos=5,
        max_per_topic=4,
    )

    assert {item["query"] for item in ranked[:2]} == {
        "animal survival strategies",
        "historical unsolved mysteries",
    }
    assert all(item["evidence_channels"] == 5 for item in ranked[:2])


def test_topup_adds_at_most_two_topics_and_persists_shared_cooldown(tmp_path):
    config_path = tmp_path / "radar.json"
    yield_path = tmp_path / "yield.json"
    db_path = tmp_path / "index.sqlite3"
    config_path.write_text(
        json.dumps({"watchlist": ["existing useful topic"]}),
        encoding="utf-8",
    )
    yield_path.write_text(
        json.dumps({"existing useful topic": {"attempts": 1}}),
        encoding="utf-8",
    )
    config = RadarConfig(
        watchlist=["existing useful topic"],
        db_path=str(db_path),
        harvest_expand=False,
        query_topup_enabled=True,
        query_topup_min_coverage=1.0,
        query_topup_min_channels=3,
        query_topup_min_videos=5,
    )
    rows = (
        _rows("Animal survival strategies in the wild", "nature")
        + _rows("Historical unsolved mysteries finally decoded", "history")
        + _rows("Forgotten wars explained through maps", "war")
    )
    store = YouTubeStore(db_path)

    first = maybe_top_up_queries(
        config,
        store,
        config_path=config_path,
        yield_path=yield_path,
        evidence_rows=rows,
        now=1_000_000,
    )
    second = maybe_top_up_queries(
        config,
        store,
        config_path=config_path,
        yield_path=yield_path,
        evidence_rows=rows,
        now=1_000_060,
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    recent = saved["signal_query_policy"]["recent_additions"]
    assert first["reason"] == "added"
    assert len(first["added"]) == 2
    assert first["remaining"] == 2
    assert first["coverage"] == round(1 / 3, 6)
    assert len(saved["watchlist"]) == 3
    assert len({item["topic"] for item in recent[:2]}) == 2
    assert all(item["source"] == "radar" for item in recent[:2])
    assert second["reason"] == "coverage_incomplete"
    assert second["added"] == []

    yield_path.write_text(json.dumps({
        query: {"attempts": 1}
        for query in saved["watchlist"]
    }), encoding="utf-8")
    third = maybe_top_up_queries(
        config,
        store,
        config_path=config_path,
        yield_path=yield_path,
        evidence_rows=rows,
        now=1_000_120,
    )

    assert third["reason"] == "cooldown_active"
    assert third["added"] == []


def test_topup_waits_for_complete_first_pass(tmp_path):
    config_path = tmp_path / "radar.json"
    yield_path = tmp_path / "yield.json"
    db_path = tmp_path / "index.sqlite3"
    config_path.write_text(
        json.dumps({"watchlist": ["untested useful topic"]}),
        encoding="utf-8",
    )
    yield_path.write_text("{}", encoding="utf-8")
    config = RadarConfig(
        watchlist=["untested useful topic"],
        db_path=str(db_path),
        harvest_expand=False,
        query_topup_enabled=True,
    )

    result = maybe_top_up_queries(
        config,
        YouTubeStore(db_path),
        config_path=config_path,
        yield_path=yield_path,
        evidence_rows=[],
        now=1_000_000,
    )

    assert result["reason"] == "coverage_incomplete"
    assert result["remaining"] == 1
    assert json.loads(config_path.read_text(encoding="utf-8"))["watchlist"] == [
        "untested useful topic"
    ]


def test_topup_rechecks_coverage_after_a_concurrent_watchlist_change(tmp_path):
    config_path = tmp_path / "radar.json"
    yield_path = tmp_path / "yield.json"
    db_path = tmp_path / "index.sqlite3"
    config_path.write_text(
        json.dumps({"watchlist": ["tested useful topic"]}),
        encoding="utf-8",
    )
    yield_path.write_text(
        json.dumps({"tested useful topic": {"attempts": 1}}),
        encoding="utf-8",
    )
    config = RadarConfig(
        watchlist=["tested useful topic"],
        db_path=str(db_path),
        harvest_expand=False,
        query_topup_enabled=True,
    )

    def add_manual_backlog(*_args, **_kwargs):
        current = json.loads(config_path.read_text(encoding="utf-8"))
        current["watchlist"].append("manual untested topic")
        config_path.write_text(json.dumps(current), encoding="utf-8")
        return []

    with patch(
        "beacon_engine.research.query_topup.rank_candidates",
        side_effect=add_manual_backlog,
    ):
        result = maybe_top_up_queries(
            config,
            YouTubeStore(db_path),
            config_path=config_path,
            yield_path=yield_path,
            evidence_rows=[],
            now=1_000_000,
        )

    assert result["reason"] == "coverage_incomplete"
    assert result["remaining"] == 1
    assert json.loads(config_path.read_text(encoding="utf-8"))["watchlist"] == [
        "tested useful topic",
        "manual untested topic",
    ]
