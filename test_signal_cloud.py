from signal_app import CACHE_TTL_SECONDS, _cloud_sync_view_state


def test_cloud_setup_pending_is_plain_and_disabled():
    state = _cloud_sync_view_state(
        {
            "configured": False,
            "pending_bundles": 4,
        }
    )

    assert state["badge"] == "setup pending"
    assert state["tone"] == "muted"
    assert state["sync_enabled"] is False
    assert "not connected yet" in state["message"]


def test_cloud_waiting_state_enables_sync():
    state = _cloud_sync_view_state(
        {
            "configured": True,
            "pending_bundles": 3,
            "remote": {"state": "running", "message": "collecting"},
        }
    )

    assert state["badge"] == "3 waiting"
    assert state["tone"] == "accent2"
    assert state["sync_enabled"] is True
    assert "waiting safely" in state["message"]
    assert state["remote_message"] == "collecting"


def test_cloud_failure_is_quarantined_and_visible():
    state = _cloud_sync_view_state(
        {
            "configured": True,
            "pending_bundles": 1,
            "last_sync": {
                "synced_at": "2026-07-12T17:05:00Z",
                "imported_bundles": 2,
                "duplicate_bundles": 1,
                "failed_bundles": 1,
            },
        }
    )

    assert state["badge"] == "attention"
    assert state["tone"] == "warn"
    assert state["sync_enabled"] is True
    assert "stayed quarantined" in state["message"]
    assert "imported 2" in state["last_line"]
    assert "rejected 1" in state["last_line"]


def test_cloud_empty_connected_state_and_cache_ttl():
    state = _cloud_sync_view_state(
        {
            "configured": True,
            "pending_bundles": 0,
            "remote": {"state": "idle"},
        }
    )

    assert state["badge"] == "connected"
    assert state["tone"] == "accent"
    assert state["sync_enabled"] is False
    assert CACHE_TTL_SECONDS["edge_sync"] == 10
