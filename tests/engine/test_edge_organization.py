from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass

import pytest

from beacon_engine.edge.organization import (
    ORGANIZATION_JOB_KIND,
    ORGANIZATION_RESULT_KIND,
    OrganizationBatch,
    OrganizationSettings,
    OrganizationWorker,
    OrganizationWorkerPaths,
    build_organization_job,
    collect_organization_job,
    load_organization_job,
    write_discovery_organization_jobs,
)
from beacon_engine.edge.protocol import EdgeProtocolError, load_bundle, write_bundle
from beacon_engine.research.providers import ProviderVideo


SIGNING_KEY = "test-organization-signing-key"
CHANNEL_ID = "UCorganization000000001"


def _channel():
    return {
        "channel_id": CHANNEL_ID,
        "title": "History Vault",
        "subscribers": 12_000,
        "category": "history documentary",
        "source": "edge-test",
    }


def _video(video_id="seedVideo01", *, category_id=""):
    return {
        "video_id": video_id,
        "title": "The Forgotten Empire Explained",
        "channel": "History Vault",
        "channel_id": CHANNEL_ID,
        "views": 10_000,
        "thumbnail_url": f"https://example.invalid/{video_id}.jpg",
        "source": "edge-test",
        "category_id": category_id,
    }


def test_signed_organization_job_round_trip_and_limits(tmp_path):
    bundle = build_organization_job(
        controller_id="signal-laptop",
        job_id="organization-pilot",
        channels=[_channel()],
        videos=[_video()],
        settings=OrganizationSettings(catalog_workers=2),
        signing_key=SIGNING_KEY,
    )
    path = write_bundle(bundle, tmp_path / "jobs")

    loaded = load_organization_job(path, signing_key=SIGNING_KEY)

    assert loaded.bundle.kind == ORGANIZATION_JOB_KIND
    assert loaded.job_id == "organization-pilot"
    assert loaded.channels[0]["channel_id"] == CHANNEL_ID
    assert loaded.settings.catalog_workers == 2

    with pytest.raises(EdgeProtocolError, match="at least one channel or video"):
        build_organization_job(
            controller_id="signal-laptop",
            job_id="empty",
            signing_key=SIGNING_KEY,
        )


class FakeCatalog:
    name = "fake-catalog"

    def channel_videos(self, channel, *, limit=200):
        return [
            ProviderVideo(
                video_id=f"catalog{index:02d}",
                title=title,
                channel="History Vault",
                channel_id=channel,
                views=20_000 + index,
                thumbnail_url=f"https://example.invalid/{index}.jpg",
                source="fake-catalog",
            )
            for index, title in enumerate(
                (
                    "The Forgotten Empire Explained",
                    "The Rise and Fall of a Lost Dynasty",
                    "The Hidden History Nobody Tells",
                )
            )
        ][:limit]


class FakePool:
    def __init__(self):
        self.units = 0
        self.keys = ["fake"]
        self.alive = 1

    def channels(self, ids, *, part="snippet,statistics"):
        self.units += (len(ids) + 49) // 50
        return [
            {
                "id": channel_id,
                "snippet": {
                    "title": "History Vault",
                    "description": "Documentary history channel",
                    "publishedAt": "2025-01-01T00:00:00Z",
                },
                "statistics": {
                    "subscriberCount": "12000",
                    "videoCount": "30",
                    "viewCount": "400000",
                },
                "contentDetails": {
                    "relatedPlaylists": {
                        "uploads": f"UU{channel_id[2:]}",
                    }
                },
            }
            for channel_id in ids
        ]

    def playlist_video_ids(self, playlist_id, *, limit=50):
        self.units += 1
        return [
            f"officialCatalog{index:02d}"
            for index in range(min(limit, 3))
        ]

    def videos(self, ids, *, part="snippet,statistics,contentDetails"):
        self.units += (len(ids) + 49) // 50
        return [
            {
                "id": video_id,
                "snippet": {
                    "title": f"Official documentary explained {video_id}",
                    "channelTitle": "History Vault",
                    "channelId": CHANNEL_ID,
                    "publishedAt": "2026-07-01T00:00:00Z",
                    "categoryId": "27",
                    "thumbnails": {
                        "high": {
                            "url": f"https://example.invalid/{video_id}.jpg"
                        }
                    },
                },
                "statistics": {"viewCount": "25000"},
                "contentDetails": {"duration": "PT12M"},
            }
            for video_id in ids
        ]


@dataclass
class FakeFaceResult:
    channel_id: str
    thumbnails_checked: int = 3
    faces_found: int = 0
    recurring_count: int = 0
    verdict: str = "faceless"
    confidence: float = 0.75


class FakeFaceChecker:
    def check_channel(self, channel_id, urls, *, max_thumbnails=12):
        return FakeFaceResult(
            channel_id=channel_id,
            thumbnails_checked=min(len(urls), max_thumbnails),
        )


def test_collector_catalogs_categories_classifies_and_face_checks(tmp_path):
    bundle = build_organization_job(
        controller_id="signal-laptop",
        job_id="organization-pilot",
        channels=[_channel()],
        videos=[_video()],
        settings={
            "catalog_videos": 3,
            "catalog_workers": 1,
            "face_workers": 1,
            "max_quota_units": 10,
        },
        signing_key=SIGNING_KEY,
    )
    job_path = write_bundle(bundle, tmp_path / "jobs")
    job = load_organization_job(job_path, signing_key=SIGNING_KEY)

    batch = collect_organization_job(
        job,
        node_id="oracle-test",
        run_id="org-run-1",
        captured_at="2026-07-13T18:00:00Z",
        catalog_factory=FakeCatalog,
        pool_factory=FakePool,
        face_checker_factory=FakeFaceChecker,
        models_dir=tmp_path / "models",
    )

    assert len(batch.records["channels"]) == 1
    assert len(batch.records["videos"]) == 4
    assert {
        row["category_id"] for row in batch.records["videos"]
    } == {"27"}
    classification = batch.records["channel_classifications"][0]
    assert classification["sample_count"] == 4
    assert classification["likely_faceless"] == 1
    assert batch.records["channel_face_checks"][0]["verdict"] == "faceless"
    assert batch.records["enrichment_attempts"][0]["status"] == "ok"
    assert batch.records["organization_runs"][0]["catalog_status"] == "ok"
    assert batch.quota_units == 3


class ParallelFakePool(FakePool):
    def __init__(self):
        super().__init__()
        self.keys = ["fake-1", "fake-2", "fake-3"]
        self.alive = 3
        self.parallel_workers = 3
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.release = threading.Event()

    def channels(self, ids, *, part="snippet,statistics"):
        self.units += (len(ids) + 49) // 50
        return [
            {
                "id": channel_id,
                "snippet": {
                    "title": f"History Vault {index}",
                    "description": "Documentary history channel",
                    "publishedAt": "2025-01-01T00:00:00Z",
                },
                "statistics": {
                    "subscriberCount": "12000",
                    "videoCount": "30",
                    "viewCount": "400000",
                },
                "contentDetails": {
                    "relatedPlaylists": {
                        "uploads": f"UU{channel_id[2:]}",
                    }
                },
            }
            for index, channel_id in enumerate(ids)
        ]

    def playlist_video_ids(self, playlist_id, *, limit=50):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 3:
                self.release.set()
        self.release.wait(timeout=1.0)
        try:
            self.units += 1
            return [f"official-{playlist_id[-6:]}"]
        finally:
            with self.lock:
                self.active -= 1


def test_official_catalog_uses_healthy_keys_concurrently(tmp_path):
    channels = [
        {
            **_channel(),
            "channel_id": f"UCorganization{index:012d}",
            "title": f"History Vault {index}",
        }
        for index in range(3)
    ]
    videos = [
        {
            **_video(f"seedVideo{index:02d}"),
            "channel_id": channels[index]["channel_id"],
            "channel": channels[index]["title"],
        }
        for index in range(3)
    ]
    bundle = build_organization_job(
        controller_id="signal-laptop",
        job_id="organization-parallel-keys",
        channels=channels,
        videos=videos,
        settings={
            "catalog_videos": 1,
            "face_check_enabled": False,
            "max_quota_units": 20,
        },
        signing_key=SIGNING_KEY,
    )
    job_path = write_bundle(bundle, tmp_path / "jobs")
    job = load_organization_job(job_path, signing_key=SIGNING_KEY)
    pool = ParallelFakePool()

    batch = collect_organization_job(
        job,
        node_id="oracle-test",
        run_id="org-parallel",
        captured_at="2026-07-14T16:30:00Z",
        catalog_factory=FakeCatalog,
        pool_factory=lambda: pool,
        models_dir=tmp_path / "models",
    )

    assert pool.max_active == 3
    assert all(
        row["catalog_status"] == "ok"
        for row in batch.records["organization_runs"]
    )
    assert len(batch.records["videos"]) == 6
    assert batch.quota_units == 5


def test_discovery_batch_splits_into_bounded_organization_jobs(tmp_path):
    channels = [
        {**_channel(), "channel_id": f"UCorganization{index:012d}"}
        for index in range(3)
    ]
    videos = [
        {
            **_video(f"seedVideo{index:02d}"),
            "channel_id": channels[index]["channel_id"],
        }
        for index in range(3)
    ]

    paths = write_discovery_organization_jobs(
        channels=channels,
        videos=videos,
        jobs_dir=tmp_path / "jobs",
        controller_id="oracle-test",
        source_job_id="radar-discovery",
        source_run_id="run-0001",
        signing_key=SIGNING_KEY,
        created_at="2026-07-13T18:05:00Z",
        channels_per_job=2,
    )

    assert len(paths) == 2
    loaded = [
        load_organization_job(path, signing_key=SIGNING_KEY)
        for path in paths
    ]
    assert [len(job.channels) for job in loaded] == [2, 1]
    assert [len(job.videos) for job in loaded] == [2, 1]


def test_large_discovery_batch_is_normalized_then_split(tmp_path):
    channels = [
        {**_channel(), "channel_id": f"UCorganization{index:012d}"}
        for index in range(501)
    ]

    paths = write_discovery_organization_jobs(
        channels=channels,
        videos=[],
        jobs_dir=tmp_path / "jobs",
        controller_id="oracle-test",
        source_job_id="radar-discovery",
        source_run_id="run-large",
        signing_key=SIGNING_KEY,
        created_at="2026-07-13T20:05:00Z",
        channels_per_job=200,
    )

    assert len(paths) == 3
    loaded = [
        load_organization_job(path, signing_key=SIGNING_KEY)
        for path in paths
    ]
    assert [len(job.channels) for job in loaded] == [200, 200, 101]


class FakeOrganizationCollector:
    def __call__(
        self,
        job,
        *,
        node_id,
        run_id,
        captured_at,
        models_dir,
    ):
        return OrganizationBatch(
            node_id=node_id,
            job_id=job.job_id,
            run_id=run_id,
            captured_at=captured_at,
            records={
                "channels": list(job.channels),
                "videos": list(job.videos),
                "channel_classifications": [],
                "channel_face_checks": [],
                "enrichment_attempts": [],
                "organization_runs": [],
            },
            quota_units=0,
        )


def test_organization_worker_processes_once_and_archives_job(tmp_path):
    paths = OrganizationWorkerPaths.under(
        tmp_path / "organizer",
        outbox=tmp_path / "shared-outbox",
    )
    paths.ensure()
    bundle = build_organization_job(
        controller_id="signal-laptop",
        job_id="organization-worker-test",
        channels=[_channel()],
        videos=[_video()],
        signing_key=SIGNING_KEY,
    )
    job_path = write_bundle(bundle, paths.jobs)
    worker = OrganizationWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-test",
        collect_fn=FakeOrganizationCollector(),
    )

    first = worker.run_once(now_ts=1000)

    assert first[0].status == "completed"
    result_path = next(paths.outbox.glob("*.signal-edge.json.gz"))
    result = load_bundle(result_path, signing_key=SIGNING_KEY)
    assert result.kind == ORGANIZATION_RESULT_KIND
    assert result.payload["metadata"]["source_job_revision"] == bundle.bundle_id
    assert not job_path.exists()
    archived = list(paths.archive.rglob("*.signal-edge.json.gz"))
    assert len(archived) == 1

    replay = paths.jobs / archived[0].name
    shutil.copy2(archived[0], replay)
    second = worker.run_once(now_ts=1001)

    assert second[0].status == "duplicate"
    assert len(list(paths.outbox.glob("*.signal-edge.json.gz"))) == 1
    status = json.loads(paths.status.read_text(encoding="utf-8"))
    assert status["state"] == "idle"


def test_organization_worker_skips_recent_channel_video_only_jobs(tmp_path):
    paths = OrganizationWorkerPaths.under(
        tmp_path / "organizer",
        outbox=tmp_path / "shared-outbox",
    )
    paths.ensure()
    first_bundle = build_organization_job(
        controller_id="signal-laptop",
        job_id="organization-first",
        channels=[_channel()],
        videos=[_video()],
        signing_key=SIGNING_KEY,
    )
    write_bundle(first_bundle, paths.jobs)
    worker = OrganizationWorker(
        paths=paths,
        signing_key=SIGNING_KEY,
        node_id="oracle-test",
        collect_fn=FakeOrganizationCollector(),
    )

    first = worker.run_once(now_ts=1000)
    repeated_bundle = build_organization_job(
        controller_id="signal-laptop",
        job_id="organization-repeat",
        videos=[_video("seedVideo02")],
        signing_key=SIGNING_KEY,
    )
    repeated_path = write_bundle(repeated_bundle, paths.jobs)
    second = worker.run_once(now_ts=1001)

    assert first[0].status == "completed"
    assert second[0].status == "skipped"
    assert not repeated_path.exists()
    assert len(list(paths.outbox.glob("*.signal-edge.json.gz"))) == 1
