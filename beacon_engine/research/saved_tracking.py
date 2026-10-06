"""Bounded Saved measurements from verified remote snapshots, never extrapolation."""
from __future__ import annotations

import datetime as dt
from statistics import median

REMOTE_SOURCES = ('manual-save', 'manual-save-hidden-subs')


def _date(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def _stamp(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _baseline(conn, table, id_field, identity, end):
    # Nearest reading at or before the 24-hour target, no more than six hours early.
    # The UI says ~24h and reveals the actual interval; no hourly interpolation.
    sources = "source IN ('manual-save','manual-save-hidden-subs')" if table == 'channel_snapshots' else "source='manual-save'"
    return conn.execute(
        f"SELECT * FROM {table} WHERE {id_field}=? AND captured_at<=? "
        f"AND captured_at>=? AND {sources} ORDER BY captured_at DESC,id DESC LIMIT 1",
        (identity, _stamp(end - dt.timedelta(hours=24)),
         _stamp(end - dt.timedelta(hours=30))),
    ).fetchone()


def saved_channel_tracking(store, niche_id, *, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    output = {}
    with store.connect() as conn:
        ids = [r[0] for r in conn.execute(
            'SELECT channel_id FROM niche_channels WHERE niche_id=? AND enabled=1 '
            'ORDER BY added_at DESC LIMIT 500', (int(niche_id),),
        )]
        for cid in ids:
            latest = conn.execute(
                "SELECT * FROM channel_snapshots WHERE channel_id=? AND "
                "source IN ('manual-save','manual-save-hidden-subs') "
                "ORDER BY captured_at DESC,id DESC LIMIT 1", (cid,),
            ).fetchone()
            end = _date(latest['captured_at']) if latest else None
            item = {'checked_at': latest['captured_at'] if end else '',
                    'status': 'collecting', 'subscribers_delta': None,
                    'views_delta': None, 'uploads_delta': None, 'interval_hours': None,
                    'video_signal': None}
            output[cid] = item
            if end is None:
                item['status'] = 'unmeasured'
                continue
            if (now - end).total_seconds() < -300 or (now - end).total_seconds() > 8 * 3600:
                item['status'] = 'stale'
                continue  # Old windows must not be presented as today's movement.
            baseline = _baseline(conn, 'channel_snapshots', 'channel_id', cid, end)
            start = _date(baseline['captured_at']) if baseline else None
            if start is not None:
                item.update(status='measured', baseline_at=baseline['captured_at'],
                            interval_hours=round((end - start).total_seconds() / 3600, 2),
                            views_delta=latest['total_views'] - baseline['total_views'],
                            uploads_delta=latest['video_count'] - baseline['video_count'])
                if latest['source'] != 'manual-save-hidden-subs' and baseline['source'] != 'manual-save-hidden-subs':
                    item['subscribers_delta'] = latest['subscribers'] - baseline['subscribers']
            # Latest 30 uploads only. Each history lookup uses the existing ID/time index.
            videos = conn.execute(
                "SELECT video_id,title FROM videos WHERE channel_id=? "
                "ORDER BY published_at_normalized DESC,video_id DESC LIMIT 30", (cid,),
            ).fetchall()
            moves = []
            for video in videos:
                recent = conn.execute(
                    "SELECT views,captured_at FROM video_snapshots WHERE video_id=? "
                    "AND source='manual-save' AND captured_at<=? AND captured_at>=? "
                    "ORDER BY captured_at DESC,id DESC LIMIT 1",
                    (video['video_id'], _stamp(end), _stamp(end - dt.timedelta(minutes=10))),
                ).fetchone()
                if not recent:
                    continue
                previous = _baseline(conn, 'video_snapshots', 'video_id', video['video_id'], _date(recent['captured_at']))
                if previous:
                    delta = recent['views'] - previous['views']
                    moves.append({'video_id': video['video_id'], 'title': video['title'],
                                  'views_delta': delta,
                                  'interval_hours': round((_date(recent['captured_at']) - _date(previous['captured_at'])).total_seconds() / 3600, 2)})
            # Quiet comparative signal, not a viral prediction. Five measured peers,
            # 10K absolute gain and three times typical growth prevent tiny-base noise.
            if len(moves) >= 5:
                typical = max(1, median(max(0, v['views_delta']) / v['interval_hours'] for v in moves))
                best = max(moves, key=lambda v: v['views_delta'] / v['interval_hours'])
                if best['views_delta'] >= 10000 and best['views_delta'] / best['interval_hours'] >= typical * 3:
                    item['video_signal'] = {**best, 'compared_uploads': len(moves)}
    return {'channels': output, 'refresh_hours': 6,
            'comparison': 'Measured differences about 24 hours apart, ending at the last successful check. Subscriber counts are rounded by YouTube. Video signals compare at most 30 recent uploads.'}
