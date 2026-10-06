"""Search attribution comes from committed unique IDs, never changing video pages."""
import json

import pytest

from beacon_engine.edge import importer
from beacon_engine.edge.discovery_yield import attribution_counts, discovery_yield_report, query_identity
from beacon_engine.edge.protocol import EdgeProtocolError, build_bundle, write_bundle
from beacon_engine.intelligence.analyst_tools import IntelligenceAnalystTools, TOOL_SCHEMAS
from beacon_engine.research.store import YouTubeStore


def run(query, ids, *, status="ok", finished="2026-10-03T14:00:00Z"):
    return {"node_id": "yield-node", "job_id": "yield-test", "run_id": query,
            "query": query, "topic": "History", "provider": "test", "sort": "newest",
            "status": status, "channel_ids": ids, "finished_at": finished}


def deliver(root, label, rows, runs, *, invalid=False):
    records = {"channels": rows, "query_runs": runs}
    if invalid:
        records["videos"] = [{"title": "Required ID missing"}]
    bundle = build_bundle(node_id="yield-node", kind=importer.DISCOVERY_BUNDLE_KIND,
                          job_id=label, records=records, signing_key="synthetic-yield-key")
    path = write_bundle(bundle, root / label)
    with importer.BundleImportSession(root / "index.sqlite3") as session:
        report = session.import_path(path, signing_key="synthetic-yield-key", ack_dir=root / "acks")
    return report, path


def test_overlapping_searches_credit_one_new_channel_to_first_completed_search(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    deliver(tmp_path, "seed", [{"channel_id": "known"}], [])
    early = run("earlier history", ["known", "shared"], finished="2026-10-03T14:00:00Z")
    late = run("later history", ["shared", "other"], finished="2026-10-03T14:00:01Z")
    report, path = deliver(tmp_path, "overlap", [{"channel_id": cid} for cid in ["known", "shared", "other"]], [late, early])
    counts = report.counts
    assert counts["channels_new"] == counts["channels_new_attributed"] == 2
    assert counts["channels_new_unattributed"] == 0
    assert counts["yield_new:" + query_identity(early)] == 1
    assert counts["yield_new:" + query_identity(late)] == 1
    assert counts["yield_existing:" + query_identity(early)] == 1
    assert attribution_counts({"channels": [{"channel_id": cid} for cid in ["known", "shared", "other"]],
                               "query_runs": [early, late]}, {"shared", "other"})["yield_new:" + query_identity(early)] == 1
    with importer.BundleImportSession(tmp_path / "index.sqlite3") as session:
        assert session.import_path(path, signing_key="synthetic-yield-key").duplicate
    result = discovery_yield_report(store, minimum_searches=1)
    assert result["successful_searches"] == 2
    assert result["attributed_new_channels"] == 2
    assert result["counted_new_channels"] == 3
    assert result["unattributed_new_channels"] == 1
    assert result["historical_bundles_without_provenance"] == 1
    assert len(result["queries"]) == 2
    ack = json.loads((tmp_path / "acks" / (report.bundle_id + ".ack.json")).read_text())
    assert ack["payload"]["counts"] == counts


def test_empty_and_failed_queries_are_separate_from_unknown_history(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    deliver(tmp_path, "empty", [], [run("empty", []), run("failed", [], status="error")])
    legacy = run("legacy", [])
    legacy.pop("channel_ids")
    deliver(tmp_path, "legacy", [{"channel_id": "old-source"}], [legacy])
    result = discovery_yield_report(store, minimum_searches=1)
    assert result["failed_searches"] == result["successful_searches"] == 1
    assert result["counted_new_channels"] == result["unattributed_new_channels"] == 1
    assert result["historical_bundles_without_provenance"] == 1
    assert [row["query"] for row in result["queries"]] == ["empty"]
    assert result["queries"][0]["new_channels_per_1000_searches"] == 0


@pytest.mark.parametrize("ids,status", [("not-a-list", "ok"), (["phantom"], "ok"), (["new"], "error"), (["new"] * 201, "ok")])
def test_invalid_provenance_rolls_back_channel_reservations_and_has_no_ack(tmp_path, ids, status):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    with pytest.raises(EdgeProtocolError):
        deliver(tmp_path, "invalid", [{"channel_id": "new"}], [run("bad", ids, status=status)])
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0
    assert not list((tmp_path / "acks").glob("*.json"))


def test_changed_video_parent_can_be_attributed_without_a_new_video_discovery(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    report, _ = deliver(tmp_path, "changed-parent", [{"channel_id": "new"}], [run("changed video", ["new"])])
    assert report.counts["video_discoveries"] == 0
    assert report.counts["channels_new_attributed"] == 1
    assert discovery_yield_report(store, minimum_searches=1)["queries"][0]["new_channels"] == 1


def test_tiny_sample_is_not_ranked_ahead_of_repeated_measured_searches(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    for i in range(3):
        deliver(tmp_path, "steady-" + str(i), [{"channel_id": "steady-" + str(i)}], [run("steady", ["steady-" + str(i)])])
    deliver(tmp_path, "one-off", [{"channel_id": "viral-" + str(i)} for i in range(4)], [run("one-off", ["viral-" + str(i) for i in range(4)])])
    result = discovery_yield_report(store)
    assert result["status"] == "measured"
    assert result["queries_below_minimum"] == 1
    assert [row["query"] for row in result["queries"]] == ["steady"]
    assert result["queries"][0]["successful_searches"] == 3


def test_report_is_bounded_and_read_tool_preserves_database_contents(tmp_path, monkeypatch):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    for i in range(3):
        deliver(tmp_path, str(i), [], [run(str(i), [])])
    monkeypatch.setattr("beacon_engine.edge.discovery_yield.MAX_REPORT_BUNDLES", 2)
    readonly = YouTubeStore(tmp_path / "index.sqlite3", read_only=True)
    tools = IntelligenceAnalystTools(store=readonly)
    assert TOOL_SCHEMAS["discovery_yield"]["write"] is False
    with store.connect() as conn:
        before = list(conn.execute("SELECT bundle_id,counts_json FROM edge_import_batches ORDER BY bundle_id"))
    result = tools.dispatch("discovery_yield", {"minimum_searches": 1})
    assert result["truncated"]
    assert result["measured_bundles"] == 2
    with store.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT bundle_id,counts_json FROM edge_import_batches ORDER BY bundle_id")] == [tuple(row) for row in before]


def test_empty_index_returns_collecting_and_rejects_unbounded_arguments(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    assert discovery_yield_report(store)["status"] == "collecting"
    with pytest.raises(ValueError):
        discovery_yield_report(store, hours=100000)
