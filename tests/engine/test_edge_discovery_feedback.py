from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from beacon_engine.edge.discovery_feedback import (
    MAX_PENDING, MAX_SAMPLES, allocate_queries, consume_feedback,
    context_key, measured_yield, remember_bundle,
)
from beacon_engine.edge.discovery_yield import attribution_counts
from beacon_engine.edge.plan import EdgeQuery, build_discovery_plan, plan_from_bundle
from beacon_engine.edge.protocol import build_bundle, write_ack
from beacon_engine.edge.ssh_transport import verified_ack_payload, EdgeSSHTransportError
from beacon_engine.edge.worker import _ensure_job_state, select_scheduled_queries

KEY = "test-only-feedback-key"
NODE = "feedback-test"


def bundle_for(run=1, *, query="history", sort="newest", topic="History", status="ok", node=NODE):
    cid = "UC" + str(run).zfill(22)
    records = {"channels": [{"channel_id": cid}], "query_runs": [{
        "node_id": node, "job_id": "feedback-job", "run_id": str(run),
        "query": query, "topic": topic, "provider": "free-test", "sort": sort,
        "status": status, "channel_ids": [cid] if status == "ok" else [],
    }]}
    bundle = build_bundle(node_id=node, kind="youtube.discovery.v1", job_id="feedback-job",
                          records=records, metadata={"run": run}, signing_key=KEY)
    return bundle, cid


def job_for():
    return {"revision": "revision-one", "queries": {"history": {}}}


def ack_for(path, bundle, cid, *, new=True, counts=None, key=KEY, status="imported"):
    if counts is None:
        new_ids = {cid} if new else set()
        counts = {"channels_new": len(new_ids), **attribution_counts(bundle.records, new_ids)}
    return write_ack(bundle, path, counts=counts, signing_key=key, status=status)


def consume(job, path):
    return consume_feedback(job, ack_dir=Path(path), node_id=NODE, signing_key=KEY)


def test_no_reward_before_post_commit_ack_and_no_reward_twice(tmp_path):
    job = job_for(); bundle, cid = bundle_for()
    remember_bundle(job, bundle, run_number=1)
    assert consume(job, tmp_path)["waiting"] == 1
    assert "channel_yield" not in job["queries"]["history"]
    ack_for(tmp_path, bundle, cid)
    assert consume(job, tmp_path)["applied"] == 1
    assert consume(job, tmp_path)["applied"] == 0
    assert measured_yield(job["queries"]["history"], topic="History", sort="newest") == (1, 1)


@pytest.mark.parametrize("kind", ["forged", "wrong-node", "failed-status", "malformed", "wrong-counts", "wrong-query", "oversized"])
def test_bad_feedback_cannot_reward_or_stop_collection(tmp_path, kind):
    job = job_for()
    bundle, cid = bundle_for(node="other-node" if kind == "wrong-node" else NODE)
    remember_bundle(job, bundle, run_number=1)
    counts = {"channels_new": 1, **attribution_counts(bundle.records, {cid})}
    if kind == "wrong-counts":
        counts["channels_new_attributed"] = 2
    if kind == "wrong-query":
        counts = {k.replace('"history"', '"wrong-query"'): v for k, v in counts.items()}
    path = ack_for(tmp_path, bundle, cid, counts=counts,
                   key="wrong-key" if kind == "forged" else KEY,
                   status="failed" if kind == "failed-status" else "imported")
    if kind == "malformed": path.write_text("not JSON")
    if kind == "oversized": path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    assert consume(job, tmp_path)["rejected"] == 1
    assert "channel_yield" not in job["queries"]["history"]
    assert bundle.bundle_id in job["pending_channel_feedback"]
    # Correcting the acknowledgement is retryable.
    if kind != "wrong-node":
        ack_for(tmp_path, bundle, cid)
        assert consume(job, tmp_path)["applied"] == 1


@pytest.mark.parametrize("counts", [{"channels_new": 2}, {"query_yield_version": 1, "query_yield_complete": 0}])
def test_old_or_partial_ack_is_unknown_not_zero(tmp_path, counts):
    job = job_for(); bundle, cid = bundle_for()
    remember_bundle(job, bundle, run_number=1)
    ack_for(tmp_path, bundle, cid, counts=counts)
    assert consume(job, tmp_path)["unmeasured"] == 1
    assert "channel_yield" not in job["queries"]["history"]


def test_failed_search_is_not_zero_success(tmp_path):
    job = job_for(); bundle, cid = bundle_for(status="error")
    remember_bundle(job, bundle, run_number=1)
    ack_for(tmp_path, bundle, cid, new=False)
    assert consume(job, tmp_path)["applied"] == 1
    assert "channel_yield" not in job["queries"]["history"]


def test_import_replay_ack_keeps_original_committed_counts_once(tmp_path):
    job = job_for(); bundle, cid = bundle_for()
    remember_bundle(job, bundle, run_number=1)
    ack_for(tmp_path, bundle, cid, status="duplicate")
    assert consume(job, tmp_path)["applied"] == 1
    assert consume(job, tmp_path)["applied"] == 0
    assert measured_yield(job["queries"]["history"], topic="History", sort="newest") == (1, 1)


def test_worker_persists_pending_and_applies_ack_after_restart(tmp_path):
    from beacon_engine.edge.worker import EdgeWorker, EdgeWorkerPaths
    from beacon_engine.edge.collector import CollectionBatch, QueryOutcome
    from beacon_engine.edge.protocol import write_bundle, load_bundle

    paths = EdgeWorkerPaths.under(tmp_path / "worker")
    paths.ensure()
    plan = build_discovery_plan(controller_id="test", job_id="feedback-job",
                                queries=[EdgeQuery(query="history", topic="History", sort="newest")],
                                settings={"batch_size": 1, "interval_seconds": 60}, signing_key=KEY)
    write_bundle(plan, paths.jobs)
    def collect(plan, selected, *, node_id, run_id, captured_at):
        bundle, _ = bundle_for()
        return CollectionBatch(node_id=node_id, job_id=plan.job_id, run_id=run_id,
                               captured_at=captured_at, records=bundle.records, quota_units=0,
                               outcomes=(QueryOutcome(query="history", topic="History", sort="newest",
                                                      provider="free-test", status="ok", videos=(),
                                                      previous_video_ids=(), started_at=captured_at,
                                                      finished_at=captured_at, error=""),))
    worker = EdgeWorker(paths=paths, signing_key=KEY, node_id=NODE, collect_fn=collect)
    report = worker.run_once(now_ts=100)[0]
    pending = worker.load_state()["jobs"]["feedback-job"]["pending_channel_feedback"]
    bundle = load_bundle(report.bundle_path, signing_key=KEY)
    assert bundle.bundle_id in pending
    ack_for(paths.root / "acks", bundle, bundle.records["channels"][0]["channel_id"])
    restarted = EdgeWorker(paths=paths, signing_key=KEY, node_id=NODE, collect_fn=collect)
    assert restarted.run_once(now_ts=101)[0].status == "waiting"
    state = restarted.load_state()["jobs"]["feedback-job"]
    assert state["channel_feedback"]["applied"] == 1
    assert measured_yield(state["queries"]["history"], topic="History", sort="newest") == (1, 1)


def test_late_ack_cannot_replace_recent_window_and_sort_is_separate(tmp_path):
    job = job_for()
    old, old_cid = bundle_for(1)
    remember_bundle(job, old, run_number=1)
    for n in range(2, MAX_SAMPLES + 3):
        bundle, cid = bundle_for(n)
        remember_bundle(job, bundle, run_number=n)
        ack_for(tmp_path, bundle, cid, new=False)
        consume(job, tmp_path)
    ack_for(tmp_path, old, old_cid)
    consume(job, tmp_path)
    assert measured_yield(job["queries"]["history"], topic="History", sort="newest") == (MAX_SAMPLES, 0)
    relevance, cid = bundle_for(99, sort="relevance")
    remember_bundle(job, relevance, run_number=99)
    ack_for(tmp_path, relevance, cid)
    consume(job, tmp_path)
    assert measured_yield(job["queries"]["history"], topic="History", sort="newest") == (MAX_SAMPLES, 0)
    assert measured_yield(job["queries"]["history"], topic="History", sort="relevance") == (1, 1)


def test_pending_cap_and_revision_change_do_not_apply_old_evidence(tmp_path):
    job = job_for()
    for n in range(MAX_PENDING + 1):
        bundle, cid = bundle_for(n + 1)
        remember_bundle(job, bundle, run_number=n + 1)
    assert len(job["pending_channel_feedback"]) == MAX_PENDING
    assert job["feedback_pending_dropped"] == 1
    ack_for(tmp_path, bundle, cid)
    job["revision"] = "changed"
    assert consume(job, tmp_path)["applied"] == 0
    assert not job["pending_channel_feedback"]


def population(n=20):
    indexed = [(i, EdgeQuery(query=f"q{i}", topic="History")) for i in range(n)]
    states = {q.query: {"attempts": 10, "zero_streak": 0, "last_completed_ts": i,
                      "channel_yield": {context_key(q.topic, "newest"): [
                          {"run": sample, "ok": 1, "new": 0 if i < 10 else i}
                          for sample in range(3)]}}
              for i, q in indexed}
    return indexed, states


def test_budget_rewards_actual_channels_and_reserves_exploration_refresh():
    indexed, states = population()
    # Repeated new videos must not make these globally-zero queries productive.
    states["q0"]["last_new_result_count"] = 100
    selected, counts = allocate_queries(indexed, states, batch_limit=5, tick=1)
    assert [q.query for _, q in selected] == ["q0", "q1", "q19", "q18", "q17"]
    assert (counts["productive"], counts["exploration"], counts["refresh"]) == (3, 1, 1)
    assert len({q.query for _, q in selected}) == 5


def test_one_search_batches_rotate_all_three_budgets():
    indexed, states = population()
    roles = [allocate_queries(indexed, states, batch_limit=1, tick=t)[1] for t in range(1, 6)]
    assert sum(r["productive"] for r in roles) == 3
    assert sum(r["exploration"] for r in roles) == 1
    assert sum(r["refresh"] for r in roles) == 1


def test_small_samples_and_wrong_context_cannot_win_productive_slots():
    indexed, states = population()
    states["q0"]["channel_yield"][context_key("History", "newest")] = [{"run": 1, "ok": 1, "new": 200}]
    selected, counts = allocate_queries(indexed, states, batch_limit=5, tick=1)
    assert "q0" not in [q.query for _, q in selected[2:]]
    for state in states.values(): state["channel_yield"] = {context_key("Wrong topic", "newest"): [{"run": 1, "ok": 3, "new": 200}]}
    assert allocate_queries(indexed, states, batch_limit=5, tick=1) is None


def test_low_yield_queries_are_revisited_and_missing_feedback_is_legacy():
    indexed, states = population()
    seen = set()
    for tick in range(1, 31):
        selected, _ = allocate_queries(indexed, states, batch_limit=5, tick=tick)
        for _, query in selected:
            seen.add(query.query)
            states[query.query]["last_completed_ts"] = 100 + tick
    assert seen == set(states)
    for state in states.values(): state.pop("channel_yield")
    assert allocate_queries(indexed, states, batch_limit=5, tick=31) is None


def test_scheduler_first_pass_retries_adaptive_switch_and_topic_change():
    indexed, states = population()
    plan = plan_from_bundle(build_discovery_plan(
        controller_id="test", job_id="feedback-job", queries=[q for _, q in indexed],
        settings={"batch_size": 5, "adaptive": True}, signing_key=KEY))
    job = {"queries": states}
    states["q8"]["attempts"] = 0
    assert [q.spec.query for q in select_scheduled_queries(plan, job, now_ts=50, tick=1)] == ["q8"]
    states["q8"]["attempts"] = 1
    states["q19"]["retry_after_ts"] = 100
    assert "q19" not in [q.spec.query for q in select_scheduled_queries(plan, job, now_ts=50, tick=2)]
    nonadaptive = plan_from_bundle(build_discovery_plan(
        controller_id="test", job_id="feedback-job", queries=[q for _, q in indexed],
        settings={"batch_size": 5, "adaptive": False}, signing_key=KEY))
    assert [q.spec.query for q in select_scheduled_queries(nonadaptive, job, now_ts=50, tick=3)] == [f"q{i}" for i in range(5)]
    for query in plan.queries: states[query.query]["feedback_spec"] = ["old-topic", "", query.limit]
    root = {"jobs": {plan.job_id: job}}
    _ensure_job_state(root, plan, now_ts=50)
    assert all("channel_yield" not in state for state in job["queries"].values())


def test_verification_returns_same_authenticated_payload(tmp_path):
    bundle, cid = bundle_for()
    path = ack_for(tmp_path, bundle, cid)
    assert verified_ack_payload(path, signing_key=KEY)["bundle_id"] == bundle.bundle_id
    envelope = json.loads(path.read_text())
    envelope["payload"]["counts"]["channels_new"] += 1
    path.write_text(json.dumps(envelope))
    with pytest.raises(EdgeSSHTransportError): verified_ack_payload(path, signing_key=KEY)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_real_commit_ack_existing_channel_replay_and_rollback(tmp_path, monkeypatch, backend):
    from uuid import uuid4
    from beacon_engine.edge import importer
    from beacon_engine.edge.protocol import write_bundle, EdgeProtocolError
    from beacon_engine.research.postgres import PostgresDatabase
    from beacon_engine.research.postgres_store import PostgresYouTubeStore, POSTGRES_INDEX_TARGET
    from beacon_engine.research.store import YouTubeStore

    schema = None
    if backend == "postgres":
        dsn = os.getenv("BEACON_TEST_POSTGRES_DSN")
        if not dsn: pytest.skip("requires an explicitly isolated PostgreSQL database")
        schema = "beacon_feedback_test_" + uuid4().hex
        admin = PostgresDatabase(dsn)
        with admin.connect() as c: c.execute("CREATE SCHEMA " + schema)
        monkeypatch.setenv("BEACON_POSTGRES_DSN", dsn)
        monkeypatch.setenv("BEACON_POSTGRES_SCHEMA", schema)
        store = PostgresYouTubeStore(dsn=dsn, schema=schema, initialize=True)
        target = POSTGRES_INDEX_TARGET
    else:
        target = tmp_path / "index.sqlite3"
        store = YouTubeStore(target)
    try:
        job = job_for()
        first, cid = bundle_for()
        remember_bundle(job, first, run_number=1)
        path = write_bundle(first, tmp_path / "first")
        with importer.BundleImportSession(target) as session:
            assert session.import_path(path, signing_key=KEY, ack_dir=tmp_path / "acks").counts["channels_new"] == 1
            assert session.import_path(path, signing_key=KEY, ack_dir=tmp_path / "acks").status == "duplicate"
        assert consume(job, tmp_path / "acks")["applied"] == 1
        second = build_bundle(node_id=NODE, kind=first.kind, job_id="feedback-job",
                              records=first.records, metadata={"repeat": 2}, signing_key=KEY)
        remember_bundle(job, second, run_number=2)
        with importer.BundleImportSession(target) as session:
            report = session.import_path(write_bundle(second, tmp_path / "second"), signing_key=KEY, ack_dir=tmp_path / "acks")
            assert report.counts["channels_new"] == 0
        assert consume(job, tmp_path / "acks")["applied"] == 1
        assert measured_yield(job["queries"]["history"], topic="History", sort="newest") == (2, 1)
        bad, _ = bundle_for(3)
        invalid = build_bundle(node_id=NODE, kind=bad.kind, job_id="feedback-job",
                               records={**bad.records, "videos": [{"title": "Required ID missing"}]}, signing_key=KEY)
        remember_bundle(job, invalid, run_number=3)
        with pytest.raises(EdgeProtocolError), importer.BundleImportSession(target) as session:
            session.import_path(write_bundle(invalid, tmp_path / "bad"), signing_key=KEY, ack_dir=tmp_path / "acks")
        assert consume(job, tmp_path / "acks")["waiting"] == 1
        with store.connect() as c: assert c.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
        assert measured_yield(job["queries"]["history"], topic="History", sort="newest") == (2, 1)
    finally:
        if schema:
            with admin.connect() as c: c.execute("DROP SCHEMA " + schema + " CASCADE")
