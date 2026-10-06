from __future__ import annotations

import json

from beacon_engine.edge.collector import CollectionBatch, QueryOutcome
from beacon_engine.edge.plan import (
    EdgeQuery,
    build_discovery_plan,
    load_discovery_plan,
)
from beacon_engine.edge.organization import load_organization_job
from beacon_engine.edge.organization import OrganizationState
from beacon_engine.edge.protocol import load_bundle, write_bundle
from beacon_engine.edge.worker import (
    EdgeWorker,
    EdgeWorkerPaths,
    select_scheduled_queries,
)
from beacon_engine.research.providers import ProviderVideo


SIGNING_KEY = "test-only-edge-signing-key"


class FakeCollector:
    def __init__(self, *, fail_once=(), empty=()):
        self.calls = []
        self.fail_once = set(fail_once)
        self.failed = set()
        self.empty = set(empty)

    def __call__(
        self,
        plan,
        scheduled,
        *,
        node_id,
        run_id,
        captured_at,
    ):
        scheduled = list(scheduled)
        self.calls.append(
            [
                (item.spec.query, item.sort, item.previous_video_ids)
                for item in scheduled
            ]
        )
        outcomes = []
        query_runs = []
        for item in scheduled:
            should_fail = (
                item.spec.query in self.fail_once
                and item.spec.query not in self.failed
            )
            if should_fail:
                self.failed.add(item.spec.query)
                status = "error"
                videos = ()
                error = "temporary provider failure"
            elif item.spec.query in self.empty:
                status = "ok"
                videos = ()
                error = ""
            else:
                status = "ok"
                video_id = "vid-" + item.spec.query.replace(" ", "-")
                videos = (
                    ProviderVideo(
                        video_id=video_id,
                        title=item.spec.query,
                        source="fake-edge",
                    ),
                )
                error = ""
            outcome = QueryOutcome(
                query=item.spec.query,
                topic=item.spec.topic,
                sort=item.sort,
                provider="fake-edge",
                status=status,
                videos=videos,
                previous_video_ids=item.previous_video_ids,
                started_at=captured_at,
                finished_at=captured_at,
                error=error,
            )
            outcomes.append(outcome)
            query_runs.append(
                {
                    "node_id": node_id,
                    "job_id": plan.job_id,
                    "run_id": run_id,
                    "query": item.spec.query,
                    "status": status,
                    "result_count": len(videos),
                    "new_result_count": outcome.new_result_count,
                    "finished_at": captured_at,
                }
            )
        return CollectionBatch(
            node_id=node_id,
            job_id=plan.job_id,
            run_id=run_id,
            captured_at=captured_at,
            outcomes=tuple(outcomes),
            records={
                "channels": [],
                "videos": [],
                "search_results": [],
                "video_discoveries": [],
                "video_snapshots": [],
                "query_runs": query_runs,
            },
            quota_units=0,
        )


class OrganizingCollector:
    def __call__(
        self,
        plan,
        scheduled,
        *,
        node_id,
        run_id,
        captured_at,
    ):
        query = list(scheduled)[0].spec.query
        video_id = "organized-video"
        outcome = QueryOutcome(
            query=query,
            topic="history",
            sort="",
            provider="fake-edge",
            status="ok",
            videos=(
                ProviderVideo(
                    video_id=video_id,
                    title="The Forgotten Empire Explained",
                    channel="History Vault",
                    channel_id="UCorganizationworker0001",
                    views=5000,
                    thumbnail_url="https://example.invalid/thumb.jpg",
                    source="fake-edge",
                ),
            ),
            previous_video_ids=(),
            started_at=captured_at,
            finished_at=captured_at,
        )
        return CollectionBatch(
            node_id=node_id,
            job_id=plan.job_id,
            run_id=run_id,
            captured_at=captured_at,
            outcomes=(outcome,),
            records={
                "channels": [{
                    "channel_id": "UCorganizationworker0001",
                    "title": "History Vault",
                    "subscribers": 5000,
                    "source": "fake-edge",
                    "first_seen_at": captured_at,
                    "last_seen_at": captured_at,
                }],
                "videos": [{
                    "video_id": video_id,
                    "title": "The Forgotten Empire Explained",
                    "channel": "History Vault",
                    "channel_id": "UCorganizationworker0001",
                    "views": 5000,
                    "thumbnail_url": "https://example.invalid/thumb.jpg",
                    "source": "fake-edge",
                    "first_seen_at": captured_at,
                    "last_seen_at": captured_at,
                }],
                "search_results": [],
                "video_discoveries": [],
                "video_snapshots": [],
                "query_runs": [{
                    "node_id": node_id,
                    "job_id": plan.job_id,
                    "run_id": run_id,
                    "query": query,
                    "status": "ok",
                    "result_count": 1,
                    "new_result_count": 1,
                    "finished_at": captured_at,
                }],
            },
            quota_units=0,
        )


class RepeatedResultCollector(OrganizingCollector):
    def __call__(
        self,
        plan,
        scheduled,
        *,
        node_id,
        run_id,
        captured_at,
    ):
        batch = super().__call__(
            plan,
            scheduled,
            node_id=node_id,
            run_id=run_id,
            captured_at=captured_at,
        )
        repeated = tuple(
            QueryOutcome(
                query=outcome.query,
                topic=outcome.topic,
                sort=outcome.sort,
                provider=outcome.provider,
                status=outcome.status,
                videos=outcome.videos,
                previous_video_ids=outcome.video_ids,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                error=outcome.error,
                attempts=outcome.attempts,
            )
            for outcome in batch.outcomes
        )
        return CollectionBatch(
            node_id=batch.node_id,
            job_id=batch.job_id,
            run_id=batch.run_id,
            captured_at=batch.captured_at,
            outcomes=repeated,
            records=batch.records,
            quota_units=batch.quota_units,
            warnings=batch.warnings,
        )


def _write_plan(
    paths,
    queries,
    *,
    created_at,
    job_id="radar-discovery",
    batch_size=2,
    interval_seconds=60,
    catchup_delay_seconds=1,
    repeat=True,
    retire_after=3,
    reprobe_every=20,
    turbo_enabled=False,
    target_channels_per_hour=0,
    expected_channels_per_1000_queries=0,
    min_batch_size=1,
    failure_backoff_percent=5,
    batch_recovery_step=1,
    healthy_runs_to_recover=2,
):
    bundle = build_discovery_plan(
        controller_id="signal-laptop",
        job_id=job_id,
        queries=[EdgeQuery(query=query, limit=1) for query in queries],
        settings={
            "mode": "free",
            "batch_size": batch_size,
            "workers": 1,
            "interval_seconds": interval_seconds,
            "catchup_delay_seconds": catchup_delay_seconds,
            "max_quota_units_per_run": 0,
            "repeat": repeat,
            "retire_after": retire_after,
            "reprobe_every": reprobe_every,
            "turbo_enabled": turbo_enabled,
            "target_channels_per_hour": target_channels_per_hour,
            "expected_channels_per_1000_queries": (
                expected_channels_per_1000_queries
            ),
            "min_batch_size": min_batch_size,
            "failure_backoff_percent": failure_backoff_percent,
            "batch_recovery_step": batch_recovery_step,
            "healthy_runs_to_recover": healthy_runs_to_recover,
        },
        created_at=created_at,
        signing_key=SIGNING_KEY,
    )
    return write_bundle(bundle, paths.jobs)


def test_worker_fast_catchup_then_steady_newest_rotation(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["query one", "query two", "query three"],
        created_at="2026-07-12T16:00:00Z",
    )
    collector = FakeCollector()
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=collector,
    )

    first = worker.run_once(now_ts=1000)[0]
    second = worker.run_once(now_ts=1001)[0]
    waiting = worker.run_once(now_ts=1060)[0]
    third = worker.run_once(now_ts=1061)[0]

    assert first.remaining_untested == 1
    assert second.remaining_untested == 0
    assert waiting.status == "waiting"
    assert third.status == "completed"
    assert collector.calls[0] == [
        ("query one", "", ()),
        ("query two", "", ()),
    ]
    assert collector.calls[1] == [("query three", "", ())]
    assert collector.calls[2] == [
        ("query one", "newest", ("vid-query-one",)),
        ("query two", "newest", ("vid-query-two",)),
    ]
    bundles = sorted(paths.outbox.glob("*.signal-edge.json.gz"))
    assert len(bundles) == 3
    assert load_bundle(
        bundles[-1],
        signing_key=SIGNING_KEY,
    ).kind == "youtube.discovery.v1"
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    assert state["jobs"]["radar-discovery"]["queries"]["query one"]["attempts"] == 2
    assert state["jobs"]["radar-discovery"]["queries"]["query one"]["zero_streak"] == 1
    assert "last_video_views" in state["jobs"]["radar-discovery"]["queries"]["query one"]


def test_worker_enqueues_signed_organization_jobs_for_new_discoveries(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["history documentary"],
        created_at="2026-07-13T18:00:00Z",
        batch_size=1,
        repeat=False,
    )
    organization_jobs = paths.root / "organizer" / "jobs"
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=OrganizingCollector(),
        organization_jobs_dir=organization_jobs,
    )

    report = worker.run_once(now_ts=7000)[0]

    assert report.status == "completed"
    assert report.organization_jobs == 1
    job_path = next(organization_jobs.glob("*.signal-edge.json.gz"))
    job = load_organization_job(job_path, signing_key=SIGNING_KEY)
    assert job.channels[0]["channel_id"] == "UCorganizationworker0001"
    assert job.videos[0]["video_id"] == "organized-video"
    assert job.bundle.payload["metadata"]["source"] == {
        "kind": "discovery",
        "job_id": "radar-discovery",
        "run_id": "run-00000001",
    }
    assert job.settings.catalog_workers == 8


def test_worker_does_not_enqueue_repeated_discovery_results(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["history documentary"],
        created_at="2026-07-13T18:00:30Z",
        batch_size=1,
        repeat=False,
    )
    organization_jobs = paths.root / "organizer" / "jobs"
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=RepeatedResultCollector(),
        organization_jobs_dir=organization_jobs,
    )

    report = worker.run_once(now_ts=7050)[0]

    assert report.status == "completed"
    assert report.organization_jobs == 0
    assert list(organization_jobs.glob("*.signal-edge.json.gz")) == []


def test_worker_does_not_enqueue_recently_organized_channels(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["history documentary"],
        created_at="2026-07-13T18:00:45Z",
        batch_size=1,
        repeat=False,
    )
    organization_jobs = paths.root / "organizer" / "jobs"
    organization_state_path = (
        paths.root / "organizer" / "organization_state.sqlite3"
    )
    state = OrganizationState(organization_state_path)
    with state.connect() as connection:
        connection.execute(
            """
            INSERT INTO organization_channels (
                channel_id, processed_ts, source_job_revision, status
            ) VALUES (?, ?, ?, 'ok')
            """,
            (
                "UCorganizationworker0001",
                7_000,
                "previous-job",
            ),
        )
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=OrganizingCollector(),
        organization_jobs_dir=organization_jobs,
        organization_state_path=organization_state_path,
    )

    report = worker.run_once(now_ts=7_100)[0]

    assert report.status == "completed"
    assert report.organization_jobs == 0
    assert list(organization_jobs.glob("*.signal-edge.json.gz")) == []


def test_organization_enqueue_failure_does_not_fail_discovery(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["history documentary"],
        created_at="2026-07-13T18:01:00Z",
        batch_size=1,
        repeat=False,
    )

    def fail_writer(**_kwargs):
        raise RuntimeError("temporary organizer queue problem")

    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=OrganizingCollector(),
        organization_jobs_dir=paths.root / "organizer" / "jobs",
        organization_job_writer=fail_writer,
    )

    report = worker.run_once(now_ts=7100)[0]

    assert report.status == "completed"
    assert report.organization_jobs == 0
    assert "temporary organizer queue problem" in report.message
    assert len(list(paths.outbox.glob("*.signal-edge.json.gz"))) == 1


def test_failed_query_stays_untested_until_retry_succeeds(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["retry me"],
        created_at="2026-07-12T16:01:00Z",
        batch_size=1,
        repeat=False,
    )
    collector = FakeCollector(fail_once={"retry me"})
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=collector,
    )

    failed = worker.run_once(now_ts=2000)[0]
    waiting = worker.run_once(now_ts=2001)[0]
    recovered = worker.run_once(now_ts=2060)[0]

    assert failed.failed == 1
    assert failed.remaining_untested == 1
    assert waiting.status == "waiting"
    assert recovered.succeeded == 1
    assert recovered.remaining_untested == 0
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    job = state["jobs"]["radar-discovery"]
    assert job["complete"] is True
    assert job["queries"]["retry me"]["attempts"] == 1


def test_successful_empty_query_completes_one_shot_plan(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["empty result"],
        created_at="2026-07-12T16:02:00Z",
        batch_size=1,
        repeat=False,
    )
    collector = FakeCollector(empty={"empty result"})
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=collector,
    )

    report = worker.run_once(now_ts=3000)[0]

    assert report.succeeded == 1
    assert report.remaining_untested == 0
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    job = state["jobs"]["radar-discovery"]
    assert job["complete"] is True
    assert job["queries"]["empty result"]["attempts"] == 1
    # A successful-but-EMPTY response yields no new results, so it MUST accrue a zero_streak
    # (like a stale response) — otherwise a permanently-empty (dead) query could never retire
    # and would hog a batch slot forever.
    assert job["queries"]["empty result"]["zero_streak"] == 1


def test_new_plan_revision_preserves_old_progress_and_prioritizes_new_query(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["existing query"],
        created_at="2026-07-12T16:03:00Z",
        batch_size=1,
        repeat=False,
    )
    collector = FakeCollector()
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=collector,
    )
    worker.run_once(now_ts=4000)
    _write_plan(
        paths,
        ["existing query", "new query"],
        created_at="2026-07-12T16:04:00Z",
        batch_size=1,
        repeat=False,
    )

    report = worker.run_once(now_ts=4001)[0]

    assert report.remaining_untested == 0
    assert collector.calls[-1][0][0] == "new query"
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    queries = state["jobs"]["radar-discovery"]["queries"]
    assert queries["existing query"]["attempts"] == 1
    assert queries["new query"]["attempts"] == 1


def test_retired_query_waits_for_reprobe_tick(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["stable query"],
        created_at="2026-07-12T16:05:00Z",
        batch_size=1,
        repeat=True,
        retire_after=1,
        reprobe_every=4,
    )
    collector = FakeCollector()
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=collector,
    )

    worker.run_once(now_ts=5000)
    worker.run_once(now_ts=5060)
    skipped = worker.run_once(now_ts=5120)[0]
    reprobed = worker.run_once(now_ts=5180)[0]

    assert skipped.status == "waiting"
    assert skipped.message == "no queries are due in this scheduler tick"
    assert reprobed.status == "completed"
    assert len(collector.calls) == 3
    assert collector.calls[-1][0][1] == "newest"


def test_reprobe_tick_reserves_capacity_for_retired_query(tmp_path):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    plan_path = _write_plan(
        paths,
        ["active query", "retired query"],
        created_at="2026-07-12T16:06:00Z",
        batch_size=1,
        retire_after=1,
        reprobe_every=2,
    )
    plan = load_discovery_plan(plan_path, signing_key=SIGNING_KEY)
    job = {
        "queries": {
            "active query": {
                "attempts": 2,
                "zero_streak": 0,
                "last_completed_ts": 1.0,
                "last_video_ids": ["active-video"],
            },
            "retired query": {
                "attempts": 2,
                "zero_streak": 1,
                "last_completed_ts": 2.0,
                "last_video_ids": ["retired-video"],
            },
        }
    }

    selected = select_scheduled_queries(
        plan,
        job,
        now_ts=6000,
        tick=2,
    )

    assert [item.spec.query for item in selected] == ["retired query"]


def test_turbo_worker_backs_off_on_errors_and_recovers_after_healthy_runs(
    tmp_path,
):
    paths = EdgeWorkerPaths.under(tmp_path / "edge")
    paths.ensure()
    _write_plan(
        paths,
        ["query one", "query two", "query three", "query four"],
        created_at="2026-07-14T18:00:00Z",
        batch_size=4,
        interval_seconds=60,
        turbo_enabled=True,
        target_channels_per_hour=120,
        expected_channels_per_1000_queries=500,
        min_batch_size=2,
        failure_backoff_percent=5,
        batch_recovery_step=1,
        healthy_runs_to_recover=2,
    )
    collector = FakeCollector(fail_once={"query four"})
    worker = EdgeWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-casa-1",
        collect_fn=collector,
    )

    backed_off = worker.run_once(now_ts=10_000)[0]
    recovered_query = worker.run_once(now_ts=10_120)[0]
    recovered_batch = worker.run_once(now_ts=10_180)[0]

    assert backed_off.failed == 1
    assert backed_off.failure_percent == 25.0
    assert backed_off.effective_batch_size == 3
    assert backed_off.turbo_action == "backoff"
    assert backed_off.next_run_at == "1970-01-01T02:48:40Z"
    assert recovered_query.succeeded == 1
    assert recovered_query.effective_batch_size == 3
    assert recovered_batch.effective_batch_size == 4
    assert recovered_batch.turbo_action == "recover"
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    controller = state["jobs"]["radar-discovery"]["turbo_controller"]
    assert controller["effective_batch_size"] == 4
    assert controller["backing_off"] is False
