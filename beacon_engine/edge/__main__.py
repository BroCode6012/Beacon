"""Command line entry point for Signal's transport-agnostic edge worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(os.getenv("BEACON_ENV_FILE") or Path(__file__).resolve().parents[2] / ".env"), override=False)

from beacon_engine.edge.plan import (
    DiscoveryPlanSettings,
    EdgeQuery,
    build_discovery_plan,
    load_discovery_plan,
    queries_from_radar_config,
)
from beacon_engine.edge.backfill import build_organization_backfill_jobs
from beacon_engine.edge.organization import (
    OrganizationWorker,
    OrganizationWorkerPaths,
    read_organization_status,
)
from beacon_engine.edge.protocol import write_bundle
from beacon_engine.edge.ssh_transport import EdgeSSHTransport
from beacon_engine.edge.sync import import_edge_inbox
from beacon_engine.edge.worker import (
    EdgeWorker,
    EdgeWorkerLock,
    EdgeWorkerPaths,
    default_node_id,
    read_worker_status,
)

SIGNING_KEY_ENV = "SIGNAL_EDGE_SIGNING_KEY"


def _signing_key() -> str:
    key = os.getenv(SIGNING_KEY_ENV) or ""
    if not key:
        raise RuntimeError(
            f"{SIGNING_KEY_ENV} is required; keep it in the node environment, "
            "not in command-line arguments"
        )
    return key


def _query_file(path: str | os.PathLike) -> tuple[EdgeQuery, ...]:
    records: list[EdgeQuery] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "\t" in text:
            topic, query = (part.strip() for part in text.split("\t", 1))
            records.append(EdgeQuery(query=query, topic=topic))
        else:
            records.append(EdgeQuery(query=text))
    return tuple(records)


def _plan_settings(args: argparse.Namespace) -> DiscoveryPlanSettings:
    return DiscoveryPlanSettings(
        batch_size=args.batch_size,
        workers=args.workers,
        interval_seconds=args.interval_seconds,
        catchup_delay_seconds=args.catchup_delay_seconds,
        mode=args.mode,
        max_quota_units_per_run=args.max_quota_units,
        adaptive=not args.no_adaptive,
        retire_after=args.retire_after,
        reprobe_every=args.reprobe_every,
        repeat=not args.one_shot,
    )


def _cmd_create_plan(args: argparse.Namespace) -> int:
    if args.radar_config:
        queries = queries_from_radar_config(args.radar_config)
    else:
        queries = _query_file(args.queries_file)
    bundle = build_discovery_plan(
        controller_id=args.controller_id,
        job_id=args.job_id,
        queries=queries,
        settings=_plan_settings(args),
        signing_key=_signing_key(),
    )
    path = write_bundle(bundle, args.jobs_dir)
    print(
        json.dumps(
            {
                "status": "created",
                "path": str(path),
                "job_id": args.job_id,
                "queries": len(queries),
                "revision": bundle.bundle_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_validate_plan(args: argparse.Namespace) -> int:
    plan = load_discovery_plan(args.path, signing_key=_signing_key())
    print(
        json.dumps(
            {
                "status": "valid",
                "job_id": plan.job_id,
                "controller_id": plan.controller_id,
                "queries": len(plan.queries),
                "revision": plan.revision,
                "settings": plan.settings.as_dict(),
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    paths = EdgeWorkerPaths.under(args.root)
    organization_jobs_dir = (
        Path(args.organization_jobs_dir)
        if args.organization_jobs_dir
        else paths.root / "organizer" / "jobs"
    )
    worker = EdgeWorker(
        paths=paths,
        signing_key=_signing_key(),
        node_id=args.node_id,
        organization_jobs_dir=organization_jobs_dir,
        organization_state_path=(
            paths.root / "organizer" / "organization_state.sqlite3"
        ),
    )
    if args.once:
        with EdgeWorkerLock(paths.lock):
            reports = worker.run_once()
        print(json.dumps([report.as_dict() for report in reports], sort_keys=True))
        return 0
    worker.run_forever(poll_seconds=args.poll_seconds)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    paths = EdgeWorkerPaths.under(args.root)
    payload = read_worker_status(paths.status)
    if not payload:
        payload = {"state": "unknown", "message": "no worker status exists yet"}
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


def _cmd_organizer_worker(args: argparse.Namespace) -> int:
    paths = OrganizationWorkerPaths.under(
        args.root,
        outbox=args.outbox,
    )
    worker = OrganizationWorker(
        paths=paths,
        signing_key=_signing_key(),
        node_id=args.node_id,
    )
    if args.once:
        with EdgeWorkerLock(paths.lock):
            reports = worker.run_once(max_jobs=args.max_jobs)
        print(
            json.dumps(
                [report.as_dict() for report in reports],
                sort_keys=True,
            )
        )
        return 0
    worker.run_forever(poll_seconds=args.poll_seconds)
    return 0


def _cmd_organizer_status(args: argparse.Namespace) -> int:
    paths = OrganizationWorkerPaths.under(
        args.root,
        outbox=args.outbox,
    )
    payload = read_organization_status(paths.status)
    if not payload:
        payload = {
            "state": "unknown",
            "message": "no organizer status exists yet",
        }
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


def _cmd_organizer_stop(args: argparse.Namespace) -> int:
    paths = OrganizationWorkerPaths.under(
        args.root,
        outbox=args.outbox,
    )
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.stop.write_text("stop requested\n", encoding="ascii")
    print(json.dumps({"status": "requested", "path": str(paths.stop)}))
    return 0


def _cmd_publish_organization_backfill(args: argparse.Namespace) -> int:
    db_path = (
        args.db
        or os.getenv("YOUTUBE_INDEX_DB")
        or os.getenv("BEACON_INDEX_DB")
        or os.getenv("SIGNAL_INDEX_DB")
        or ""
    )
    if not db_path:
        raise RuntimeError(
            "--db or YOUTUBE_INDEX_DB is required for organization backfill"
        )
    report = build_organization_backfill_jobs(
        db_path,
        jobs_dir=args.jobs_dir,
        signing_key=_signing_key(),
        controller_id=args.controller_id,
        batch_id=args.batch_id,
        limit=args.limit,
        channels_per_job=args.channels_per_job,
        seed_videos_per_channel=args.seed_videos,
        min_subscribers=args.min_subscribers,
        settings={
            "catalog_videos": args.catalog_videos,
            "catalog_workers": args.catalog_workers,
            "face_workers": args.face_workers,
            "max_quota_units": args.max_quota_units,
            "min_subscribers": args.min_subscribers,
        },
    )
    published: list[str] = []
    if not args.local_only and report.jobs:
        transport = EdgeSSHTransport.from_env()
        if transport is None:
            raise RuntimeError(
                "SIGNAL_EDGE_SSH_HOST is required unless --local-only is used"
            )
        for path in report.jobs:
            published.append(
                transport.publish_organization_job(
                    path,
                    signing_key=_signing_key(),
                )
            )
    print(
        json.dumps(
            {
                **report.as_dict(),
                "published": len(published),
                "revisions": published,
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    paths = EdgeWorkerPaths.under(args.root)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.stop.write_text("stop requested\n", encoding="ascii")
    print(json.dumps({"status": "requested", "path": str(paths.stop)}))
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    db_path = (
        args.db
        or os.getenv("YOUTUBE_INDEX_DB")
        or os.getenv("BEACON_INDEX_DB")
        or os.getenv("SIGNAL_INDEX_DB")
        or ""
    )
    if not db_path:
        raise RuntimeError(
            "--db or YOUTUBE_INDEX_DB is required for edge synchronization"
        )
    result = import_edge_inbox(
        db_path,
        max_bundles=int(args.max_bundles or 20),
    )
    print(json.dumps(result, sort_keys=True))
    transport_errors = (
        (result.get("transport") or {}).get("errors") or []
        if isinstance(result.get("transport"), dict)
        else []
    )
    plan_errors = (
        (result.get("plan_sync") or {}).get("errors") or []
        if isinstance(result.get("plan_sync"), dict)
        else []
    )
    return 1 if (
        result.get("error")
        or int(result.get("failed_bundles") or 0) > 0
        or transport_errors
        or plan_errors
    ) else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m beacon_engine.edge",
        description="Signed Oracle/remote discovery worker for Signal.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create-plan",
        help="Create a signed recurring discovery plan.",
    )
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--radar-config",
        help="Snapshot a shared scraper_watchlist.json file.",
    )
    source.add_argument(
        "--queries-file",
        help="One query per line, or topic<TAB>query.",
    )
    create.add_argument("--jobs-dir", required=True)
    create.add_argument("--controller-id", default="signal-laptop")
    create.add_argument("--job-id", default="radar-discovery")
    create.add_argument("--batch-size", type=int, default=30)
    create.add_argument("--workers", type=int, default=6)
    create.add_argument("--interval-seconds", type=int, default=600)
    create.add_argument("--catchup-delay-seconds", type=int, default=5)
    create.add_argument(
        "--mode",
        choices=("free", "hybrid", "official"),
        default="hybrid",
    )
    create.add_argument("--max-quota-units", type=int, default=100)
    create.add_argument("--retire-after", type=int, default=3)
    create.add_argument("--reprobe-every", type=int, default=20)
    create.add_argument("--no-adaptive", action="store_true")
    create.add_argument("--one-shot", action="store_true")
    create.set_defaults(func=_cmd_create_plan)

    validate = subparsers.add_parser(
        "validate-plan",
        help="Verify a plan signature and print its non-secret summary.",
    )
    validate.add_argument("path")
    validate.set_defaults(func=_cmd_validate_plan)

    worker = subparsers.add_parser(
        "worker",
        help="Run the durable edge scheduler.",
    )
    worker.add_argument("--root", default="storage/edge")
    worker.add_argument("--node-id", default=default_node_id())
    worker.add_argument("--poll-seconds", type=int, default=15)
    worker.add_argument(
        "--organization-jobs-dir",
        help=(
            "Directory for signed deep-organization jobs "
            "(default: <root>/organizer/jobs)."
        ),
    )
    worker.add_argument("--once", action="store_true")
    worker.set_defaults(func=_cmd_worker)

    status = subparsers.add_parser("status", help="Print the latest worker status.")
    status.add_argument("--root", default="storage/edge")
    status.set_defaults(func=_cmd_status)

    organizer_worker = subparsers.add_parser(
        "organizer-worker",
        help="Run the separate durable deep-organization scheduler.",
    )
    organizer_worker.add_argument(
        "--root",
        default="storage/edge/organizer",
    )
    organizer_worker.add_argument(
        "--outbox",
        default="storage/edge/outbox",
    )
    organizer_worker.add_argument("--node-id", default=default_node_id())
    organizer_worker.add_argument("--poll-seconds", type=int, default=15)
    organizer_worker.add_argument("--max-jobs", type=int, default=1)
    organizer_worker.add_argument("--once", action="store_true")
    organizer_worker.set_defaults(func=_cmd_organizer_worker)

    organizer_status = subparsers.add_parser(
        "organizer-status",
        help="Print the latest deep-organizer status.",
    )
    organizer_status.add_argument(
        "--root",
        default="storage/edge/organizer",
    )
    organizer_status.add_argument(
        "--outbox",
        default="storage/edge/outbox",
    )
    organizer_status.set_defaults(func=_cmd_organizer_status)

    organizer_stop = subparsers.add_parser(
        "organizer-stop",
        help="Request a cooperative deep-organizer stop.",
    )
    organizer_stop.add_argument(
        "--root",
        default="storage/edge/organizer",
    )
    organizer_stop.add_argument(
        "--outbox",
        default="storage/edge/outbox",
    )
    organizer_stop.set_defaults(func=_cmd_organizer_stop)

    backfill = subparsers.add_parser(
        "publish-organization-backfill",
        help=(
            "Read a bounded backlog slice, build signed jobs, and publish "
            "them to the remote organizer."
        ),
    )
    backfill.add_argument("--db", help="Canonical Signal SQLite index path.")
    backfill.add_argument(
        "--jobs-dir",
        default="storage/edge_sync/organization_jobs",
    )
    backfill.add_argument("--controller-id", default="signal-laptop")
    backfill.add_argument("--batch-id")
    backfill.add_argument("--limit", type=int, default=100)
    backfill.add_argument("--channels-per-job", type=int, default=100)
    backfill.add_argument("--seed-videos", type=int, default=12)
    backfill.add_argument("--min-subscribers", type=int, default=700)
    backfill.add_argument("--catalog-videos", type=int, default=30)
    backfill.add_argument("--catalog-workers", type=int, default=4)
    backfill.add_argument("--face-workers", type=int, default=2)
    backfill.add_argument("--max-quota-units", type=int, default=500)
    backfill.add_argument("--local-only", action="store_true")
    backfill.set_defaults(func=_cmd_publish_organization_backfill)

    stop = subparsers.add_parser("stop", help="Request a cooperative worker stop.")
    stop.add_argument("--root", default="storage/edge")
    stop.set_defaults(func=_cmd_stop)

    sync = subparsers.add_parser(
        "sync",
        help="Pull, verify, import, acknowledge, and clean remote edge bundles.",
    )
    sync.add_argument("--db", help="Canonical Signal SQLite index path.")
    sync.add_argument("--max-bundles", type=int, default=100)
    sync.set_defaults(func=_cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001
        print(f"edge command failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
