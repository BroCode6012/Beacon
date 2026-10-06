"""Committed channel additions must not be confused with refreshed records."""
import json

import pytest

from beacon_engine.edge import importer
from beacon_engine.edge.protocol import build_bundle, write_bundle
from beacon_engine.research.store import YouTubeStore


def bundle_path(root, label, rows, *, invalid=False, organization=False):
    records = {"channels": rows}
    if invalid:
        records["videos"] = [{"title": "Missing required video ID"}]
    bundle = build_bundle(
        node_id="yield-test", job_id=label,
        kind=(importer.ORGANIZATION_BUNDLE_KIND if organization
              else importer.DISCOVERY_BUNDLE_KIND),
        metadata={"source_job_revision": label, "run_id": label} if organization else {},
        records=records, signing_key="synthetic-yield-key",
    )
    return write_bundle(bundle, root / label)


@pytest.mark.parametrize("organization", [False, True])
def test_distinct_new_channels_and_existing_updates_survive_duplicate_delivery(tmp_path, organization):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    rows = [{"channel_id": "existing", "title": "Old"}]
    first = bundle_path(tmp_path, "first", rows)
    mixed = bundle_path(tmp_path, "mixed", [
        {"channel_id": "existing", "title": "Updated"},
        {"channel_id": "new", "title": "New"},
        {"channel_id": "new", "title": "Merged", "subscribers": 123},
    ], organization=organization)
    with importer.BundleImportSession(tmp_path / "index.sqlite3") as session:
        session.import_path(first, signing_key="synthetic-yield-key")
        report = session.import_path(mixed, signing_key="synthetic-yield-key", ack_dir=tmp_path / "acks")
        duplicate = session.import_path(mixed, signing_key="synthetic-yield-key")
    assert report.counts["channels"] == 3  # Legacy record count remains compatible.
    assert report.counts["channels_new"] == 1
    assert report.counts["channels_existing"] == 1
    assert duplicate.duplicate
    assert duplicate.counts == report.counts
    ack = json.loads((tmp_path / "acks" / (report.bundle_id + ".ack.json")).read_text())
    assert ack["payload"]["counts"] == report.counts
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 2
        row = conn.execute("SELECT title,subscribers FROM channels WHERE channel_id='new'").fetchone()
        assert tuple(row) == ("Merged", 123)
        assert json.loads(conn.execute("SELECT counts_json FROM edge_import_batches WHERE bundle_id=?", (report.bundle_id,)).fetchone()[0]) == report.counts


def test_failed_bundle_does_not_leave_reserved_ids_or_publish_growth(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    broken = bundle_path(tmp_path, "broken", [{"channel_id": "new"}], invalid=True)
    good = bundle_path(tmp_path, "good", [{"channel_id": "new"}])
    with importer.BundleImportSession(tmp_path / "index.sqlite3") as session:
        results = session.import_paths([broken, good], signing_key="synthetic-yield-key", ack_dir=tmp_path / "acks")
    assert results[0].error
    assert results[1].report.counts["channels_new"] == 1
    assert len(list((tmp_path / "acks").glob("*.json"))) == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1


def test_large_and_empty_bundles_use_bounded_distinct_parent_batches(tmp_path):
    store = YouTubeStore(tmp_path / "index.sqlite3")
    paths = [bundle_path(tmp_path, "large", [{"channel_id": str(i)} for i in range(751)]),
             bundle_path(tmp_path, "empty", [])]
    with importer.BundleImportSession(tmp_path / "index.sqlite3") as session:
        reports = [session.import_path(p, signing_key="synthetic-yield-key") for p in paths]
    assert reports[0].counts["channels_new"] == 751
    assert reports[0].counts["channels_existing"] == 0
    assert reports[1].counts["channels_new"] == reports[1].counts["channels_existing"] == 0
