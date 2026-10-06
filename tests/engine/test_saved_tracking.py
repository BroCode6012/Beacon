"""Saved comparisons require actual remote history; missing readings are not zeros."""
import datetime as dt

import pytest

from beacon_engine.research.store import YouTubeStore
from beacon_engine.research.saved_tracking import saved_channel_tracking
from beacon_engine.intelligence.analyst_tools import TOOL_SCHEMAS

NOW = dt.datetime(2026, 10, 3, 16, tzinfo=dt.timezone.utc)


@pytest.fixture
def store(tmp_path):
    result = YouTubeStore(tmp_path / 'index.sqlite3')
    with result.connect() as c:
        c.execute("INSERT INTO channels(channel_id,title,first_seen_at,last_seen_at) VALUES('c','Tracked','','')")
        c.execute("INSERT INTO niches(id,name,created_at,updated_at) VALUES(1,'Saved from YouTube','','')")
        c.execute("INSERT INTO niche_channels(niche_id,channel_id,added_at) VALUES(1,'c','')")
    return result


def snapshot(store, hours=0, subs=1045, views=120000, source='manual-save'):
    stamp = (NOW - dt.timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')
    with store.connect() as c:
        c.execute('INSERT INTO channel_snapshots(channel_id,subscribers,total_views,video_count,captured_at,source) VALUES(?,?,?,?,?,?)',
                  ('c', subs, views, 10, stamp, source))


def report(store):
    return saved_channel_tracking(store, 1, now=NOW)['channels']['c']


def test_real_24h_changes_include_flat_negative_values_and_actual_check(store):
    snapshot(store, 24, subs=1000, views=100000)
    snapshot(store)
    row = report(store)
    assert row['status'] == 'measured'
    assert row['subscribers_delta'] == 45 and row['views_delta'] == 20000
    assert row['interval_hours'] == 24 and row['uploads_delta'] == 0
    snapshot(store, subs=999, views=99000)
    row = report(store)
    assert row['subscribers_delta'] == -1 and row['views_delta'] == -1000
    assert TOOL_SCHEMAS['saved_channel_tracking']['write'] is False


def test_local_snapshots_cannot_claim_a_live_check_and_one_point_is_not_growth(store):
    snapshot(store, source='local-index')
    assert report(store)['status'] == 'unmeasured'
    snapshot(store)
    assert report(store)['status'] == 'collecting'
    assert report(store)['subscribers_delta'] is None


def test_stale_or_distant_windows_are_unavailable_and_hidden_subs_are_not_loss(store):
    snapshot(store, 60)
    snapshot(store, 36)
    assert report(store)['status'] == 'stale'
    snapshot(store)
    assert report(store)['status'] == 'collecting'  # 36h is not a 24h comparison.
    snapshot(store, 24, source='manual-save-hidden-subs', subs=0)
    row = report(store)
    assert row['status'] == 'measured' and row['subscribers_delta'] is None
    assert row['views_delta'] == 0


def test_video_signal_requires_five_peers_absolute_growth_and_fresh_window(store):
    snapshot(store, 24)
    snapshot(store)
    with store.connect() as c:
        for n in range(5):
            vid = 'v' + str(n)
            c.execute('INSERT INTO videos(video_id,channel_id,title,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)',
                      (vid, 'c', 'Measured video ' + str(n), '', ''))
            for hours, views in [(24, 1000), (0, 21000 if n == 0 else 1200)]:
                c.execute('INSERT INTO video_snapshots(video_id,views,captured_at,source) VALUES(?,?,?,?)',
                          (vid, views, (NOW - dt.timedelta(hours=hours)).isoformat().replace('+00:00', 'Z'), 'manual-save'))
    signal = report(store)['video_signal']
    assert signal['video_id'] == 'v0' and signal['views_delta'] == 20000
    assert signal['compared_uploads'] == 5
    with store.connect() as c:
        c.execute("UPDATE video_snapshots SET views=5000 WHERE video_id='v0' AND views=21000")
    assert report(store)['video_signal'] is None
    assert saved_channel_tracking(store, 1234, now=NOW)['channels'] == {}
