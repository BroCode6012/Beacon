"""Durable, fenced channel saves; YouTube work stays outside write transactions.

The dedicated worker runs independently of browsers and Finder requests.
PostgreSQL is selected explicitly; legacy SQLite saving remains unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import datetime as dt
import re
import time
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from statistics import median
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from beacon_engine.research.providers.base import utc_now_iso, normalize_published_at

SAVED_NICHE_NAME = 'Saved from YouTube'
MAX_ATTEMPTS = 3
LEASE_SECONDS = 600
MAX_UPLOADS = 30
SAVED_REFRESH_HOURS = 6
_CHANNEL = re.compile(r'UC[\w-]{22}\Z', re.ASCII)
_VIDEO = re.compile(r'[\w-]{11}\Z', re.ASCII)


class InvalidSaveLink(ValueError):
    pass


class LostSaveLease(RuntimeError):
    pass


def parse_save_link(text: str) -> tuple[str, str]:
    value = str(text or '').strip()
    if not value:
        raise InvalidSaveLink('Paste a YouTube channel or video link first.')
    if len(value) > 1024:
        raise InvalidSaveLink('That link is too long. Paste the channel or video link.')
    if _CHANNEL.fullmatch(value):
        return 'channel_id', value
    if value.startswith('@') and re.fullmatch(r'@[\w.-]+', value):
        return 'handle', value[1:]
    if '://' not in value and re.match(r'(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/', value, re.I):
        value = 'https://' + value
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in (
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be',
        ) or parsed.username or parsed.password:
            raise InvalidSaveLink('Use a YouTube channel or video link.')
        parts = [unquote(p) for p in parsed.path.split('/') if p]
        candidate = ''
        if parsed.hostname in ('youtu.be', 'www.youtu.be') and parts:
            candidate = parts[0]
        elif parts and parts[0] == 'watch':
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        elif len(parts) >= 2 and parts[0] in ('shorts', 'live', 'embed'):
            candidate = parts[1]
        if candidate and _VIDEO.fullmatch(candidate):
            return 'video_id', candidate
        if len(parts) >= 2 and parts[0] == 'channel' and _CHANNEL.fullmatch(parts[1]):
            return 'channel_id', parts[1]
        if parts and re.fullmatch(r'@[\w.-]+', parts[0]):
            return 'handle', parts[0][1:]
        if len(parts) >= 2 and parts[0] in ('c', 'user'):
            return ('username' if parts[0] == 'user' else 'name'), parts[1]
        raise InvalidSaveLink('That YouTube link does not point to a channel or video.')
    return 'name', ' '.join(value.split())


def _public(row):
    result = {key: row[key] for key in (
        'id', 'input_link', 'channel_id', 'niche_id', 'status', 'phase',
        'attempts', 'saved_at', 'error', 'created_at', 'updated_at',
    )}
    result['result'] = json.loads(row['result_json'] or '{}')
    return result


class ChannelSaveQueue:
    def __init__(self, store, *, clock=time.time):
        if store.backend != 'postgresql':
            raise ValueError('Durable channel saving requires the PostgreSQL index')
        self.store = store
        self.clock = clock

    def enqueue(self, link: str) -> dict:
        kind, value = parse_save_link(link)
        normalized = value if kind in ('channel_id', 'video_id') else value.casefold()
        key = sha256((kind + ':' + normalized).encode()).hexdigest()
        now = utc_now_iso()
        with self.store.connect() as conn:
            row = conn.execute(
                """INSERT INTO channel_save_requests
                   (request_key,input_link,link_kind,link_value,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET
                   status=CASE WHEN channel_save_requests.status IN ('ready','failed')
                               THEN 'queued' ELSE channel_save_requests.status END,
                   result_json=CASE WHEN channel_save_requests.status<>'working'
                                    THEN '{}' ELSE channel_save_requests.result_json END,
                   attempts=CASE WHEN channel_save_requests.status IN ('ready','failed')
                                 THEN 0 ELSE channel_save_requests.attempts END,
                   available_at=CASE WHEN channel_save_requests.status IN ('ready','failed')
                                     THEN 0 ELSE channel_save_requests.available_at END,
                   error=CASE WHEN channel_save_requests.status IN ('ready','failed')
                              THEN '' ELSE channel_save_requests.error END,
                   updated_at=excluded.updated_at
                   RETURNING *""", (key, str(link).strip(), kind, value, now, now),
            ).fetchone()
        return _public(row)  # Queued is visible only after the transaction commits.

    def list(self, limit=50):
        with self.store.connect() as conn:
            rows = conn.execute(
                'SELECT * FROM channel_save_requests ORDER BY updated_at DESC,id DESC LIMIT ?',
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [_public(row) for row in rows]

    def retry(self, request_id: int):
        with self.store.connect() as conn:
            row = conn.execute(
                "UPDATE channel_save_requests SET status='queued',attempts=0,error='',result_json='{}',"
                "available_at=0,lease_until=0,lease_owner='',updated_at=? "
                "WHERE id=? AND status='failed' RETURNING *",
                (utc_now_iso(), int(request_id)),
            ).fetchone()
        if row is None:
            raise ValueError('Only a failed save request can be retried.')
        return _public(row)

    def refresh_saved(self, niche_id: int):
        """Queue a bounded shelf check without contacting YouTube in the browser."""
        now = utc_now_iso()
        with self.store.connect() as conn:
            rows = conn.execute(
                """INSERT INTO channel_save_requests
                   (request_key,input_link,link_kind,link_value,channel_id,niche_id,
                    phase,saved_at,created_at,updated_at)
                   SELECT encode(sha256(convert_to('channel_id:'||channel_id,'UTF8')),'hex'),
                          'https://youtube.com/channel/'||channel_id,'channel_id',channel_id,
                          channel_id,niche_id,'analyse',added_at,?,?
                   FROM niche_channels WHERE niche_id=? AND enabled=1
                   ORDER BY added_at DESC LIMIT 500
                   ON CONFLICT(request_key) DO UPDATE SET
                   status=CASE WHEN channel_save_requests.status IN ('ready','failed')
                               THEN 'queued' ELSE channel_save_requests.status END,
                   result_json=CASE WHEN channel_save_requests.status<>'working'
                                    THEN '{}' ELSE channel_save_requests.result_json END,
                   attempts=CASE WHEN channel_save_requests.status IN ('ready','failed')
                                 THEN 0 ELSE channel_save_requests.attempts END,
                   available_at=CASE WHEN channel_save_requests.status IN ('ready','failed')
                                     THEN 0 ELSE channel_save_requests.available_at END,
                   error=CASE WHEN channel_save_requests.status IN ('ready','failed')
                              THEN '' ELSE channel_save_requests.error END,
                   updated_at=excluded.updated_at RETURNING id""", (now, now, int(niche_id)),
            ).fetchall()
        return {'queued_refresh': len(rows)}

    def enqueue_due_saved(self, limit=20):
        """One bounded background pass; only actual remote readings prove freshness.

        Existing queued/working saves win. Failed checks cool down for six hours;
        merely queueing a check never makes its numbers fresh.
        """
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        stamp = now.isoformat().replace('+00:00', 'Z')
        cutoff = (now - dt.timedelta(hours=SAVED_REFRESH_HOURS)).isoformat().replace('+00:00', 'Z')
        with self.store.connect() as conn:
            rows = conn.execute(
                """INSERT INTO channel_save_requests
                   (request_key,input_link,link_kind,link_value,channel_id,niche_id,
                    phase,saved_at,created_at,updated_at,result_json)
                   SELECT encode(sha256(convert_to('channel_id:'||nc.channel_id,'UTF8')),'hex'),
                          'https://youtube.com/channel/'||nc.channel_id,'channel_id',nc.channel_id,
                          nc.channel_id,nc.niche_id,'analyse',nc.added_at,?,?,
                          '{"automatic_refresh":true}'
                   FROM niche_channels nc JOIN niches n ON n.id=nc.niche_id
                   WHERE n.name=? AND nc.enabled=1
                     AND NOT EXISTS (SELECT 1 FROM channel_snapshots cs
                         WHERE cs.channel_id=nc.channel_id AND captured_at>?
                           AND source IN ('manual-save','manual-save-hidden-subs'))
                     AND NOT EXISTS (SELECT 1 FROM channel_save_requests r
                         WHERE (r.channel_id=nc.channel_id OR
                                (r.link_kind='channel_id' AND r.link_value=nc.channel_id))
                           AND (r.status IN ('queued','working') OR r.updated_at>?))
                   ORDER BY nc.added_at LIMIT ?
                   ON CONFLICT(request_key) DO UPDATE SET status='queued',phase='analyse',
                       attempts=0,available_at=0,error='',updated_at=excluded.updated_at,
                       result_json=excluded.result_json
                   WHERE channel_save_requests.status IN ('ready','failed')
                     AND channel_save_requests.updated_at<=? RETURNING id""",
                (stamp, stamp, SAVED_NICHE_NAME, cutoff, cutoff,
                 max(1, min(int(limit), 20)), cutoff),
            ).fetchall()
        return len(rows)

    def claim(self):
        now = self.clock()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_save_requests WHERE "
                "(status='queued' AND available_at<=?) OR (status='working' AND lease_until<=?) "
                "ORDER BY CASE WHEN result_json::jsonb->>'automatic_refresh'='true' "
                "THEN 1 ELSE 0 END,id "
                "FOR UPDATE SKIP LOCKED LIMIT 1", (now, now),
            ).fetchone()
            if row is None:
                return None
            if row['attempts'] >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE channel_save_requests SET status='failed',lease_owner='',lease_until=0,"
                    "error='The worker was interrupted repeatedly. Your request is kept; press Retry.',"
                    "updated_at=? WHERE id=?", (utc_now_iso(), row['id']),
                )
                return None
            return dict(conn.execute(
                "UPDATE channel_save_requests SET status='working',attempts=attempts+1,"
                "lease_owner=?,lease_until=?,updated_at=? WHERE id=? RETURNING *",
                (uuid4().hex, now + LEASE_SECONDS, utc_now_iso(), row['id']),
            ).fetchone())

    def _owned(self, conn, job):
        row = conn.execute(
            'SELECT * FROM channel_save_requests WHERE id=? FOR UPDATE', (job['id'],),
        ).fetchone()
        if (not row or row['status'] != 'working' or
                row['lease_owner'] != job['lease_owner'] or row['lease_until'] <= self.clock()):
            raise LostSaveLease('This save request belongs to another worker now.')
        return row

    def file_channel(self, job, channel_id):
        if not _CHANNEL.fullmatch(str(channel_id)):
            raise InvalidSaveLink('YouTube did not return a valid channel for that link.')
        now = utc_now_iso()
        with self.store.connect() as conn:
            self._owned(conn, job)
            conn.execute(
                "INSERT INTO channels(channel_id,title,source,first_seen_at,last_seen_at) "
                "VALUES(?,?,'manual',?,?) ON CONFLICT(channel_id) DO NOTHING",
                (channel_id, channel_id, now, now),
            )
            conn.execute(
                "INSERT INTO niches(name,description,created_at,updated_at,autonomous) "
                "VALUES(?,'Channels you found yourself on YouTube.',?,?,1) "
                "ON CONFLICT (beacon_nocase(name)) DO NOTHING", (SAVED_NICHE_NAME, now, now),
            )
            nid = conn.execute('SELECT id FROM niches WHERE beacon_nocase(name)=beacon_nocase(?)',
                               (SAVED_NICHE_NAME,)).fetchone()[0]
            conn.execute(
                "INSERT INTO niche_channels(niche_id,channel_id,role,enabled,source,added_at) "
                "VALUES(?,?,'competitor',1,'manual',?) ON CONFLICT(niche_id,channel_id) "
                "DO UPDATE SET enabled=1", (nid, channel_id, now),
            )
            conn.execute(
                "UPDATE channel_save_requests SET channel_id=?,niche_id=?,saved_at=?,phase='analyse',"
                "attempts=0,error='',updated_at=? WHERE id=?",
                (channel_id, nid, now, now, job['id']),
            )
        # Membership and Saved state commit together; no network call holds this lock.

    def apply_analysis(self, job, packet):
        from beacon_engine.edge.importer import (
            _import_channels, _import_videos, _import_channel_classifications,
            _import_video_snapshots,
        )
        channel = packet['channel']
        if channel['channel_id'] != job['channel_id']:
            raise ValueError('Analysis returned a different channel')
        now = utc_now_iso()
        with self.store.connect() as conn:
            self._owned(conn, job)
            _import_channels(conn, [channel])
            _import_videos(conn, packet.get('videos', []))
            _import_channel_classifications(conn, packet.get('classifications', []))
            _import_video_snapshots(conn, packet.get('snapshots', []))
            hidden = channel.get('raw', {}).get('statistics', {}).get('hiddenSubscriberCount') is True
            conn.execute(
                "INSERT INTO channel_snapshots(channel_id,subscribers,total_views,video_count,"
                "average_views,median_views,captured_at,source) VALUES(?,?,?,?,?,?,?,?)",
                (job['channel_id'], channel.get('subscribers', 0), channel.get('view_count', 0),
                 channel.get('video_count', 0), packet.get('average_views', 0),
                 packet.get('median_views', 0), now,
                 'manual-save-hidden-subs' if hidden else 'manual-save'),
            )
            conn.execute(
                "UPDATE channel_save_requests SET status='ready',lease_owner='',lease_until=0,error='',"
                "result_json=?,updated_at=? WHERE id=?",
                (json.dumps({'title': channel.get('title', job['channel_id']),
                             'uploads_checked': len(packet.get('videos', [])),
                             'automatic_refresh': json.loads(job['result_json'] or '{}').get('automatic_refresh', False)}), now, job['id']),
            )

    def fail(self, job, message, *, permanent=False):
        with self.store.connect() as conn:
            row = self._owned(conn, job)
            final = permanent or row['attempts'] >= MAX_ATTEMPTS
            conn.execute(
                "UPDATE channel_save_requests SET status=?,error=?,available_at=?,lease_owner='',"
                "lease_until=0,updated_at=? WHERE id=?",
                ('failed' if final else 'queued', message,
                 self.clock() + min(60, 10 * 2 ** row['attempts']), utc_now_iso(), job['id']),
            )


class YouTubeSaveProvider:
    """A bounded pool for one channel, with no whole-index classification scans."""
    def __init__(self):
        import httplib2
        from googleapiclient.discovery import build
        from beacon_engine.research.youtube_pool import YouTubePool

        class BoundedPool(YouTubePool):
            def _client(self, index):
                if index not in self._clients:
                    self._clients[index] = build(
                        'youtube', 'v3', developerKey=self.keys[index],
                        http=httplib2.Http(timeout=20), cache_discovery=False,
                    )
                return self._clients[index]

        self.pool = BoundedPool(max_workers=1)

    def resolve(self, kind, value):
        if kind == 'channel_id':
            return value
        if kind == 'video_id':
            items = self.pool.videos([value], part='snippet')
            if items:
                return items[0].get('snippet', {}).get('channelId', '')
        elif kind in ('handle', 'username'):
            field = 'forHandle' if kind == 'handle' else 'forUsername'
            reply = self.pool._call(lambda yt: yt.channels().list(part='id', **{field: value}))
            if reply.get('items'):
                return reply['items'][0]['id']
        else:
            reply = self.pool._call(lambda yt: yt.search().list(
                part='snippet', q=value, type='channel', maxResults=1), cost=100)
            if reply.get('items'):
                return reply['items'][0]['snippet']['channelId']
        raise InvalidSaveLink('Could not find that channel. Check the link; it may be private or removed.')

    def analyse(self, channel_id):
        from beacon_engine.research.providers.official_youtube_provider import _channel_from_item, _video_from_item
        from beacon_engine.research.faceless_filter import score_channel_text, CLASSIFIER_VERSION
        from beacon_engine.research.topic_classifier import classify_topic
        items = self.pool.channels([channel_id], part='snippet,statistics,contentDetails')
        if not items:
            raise InvalidSaveLink('The channel is saved, but YouTube could not return its numbers.')
        item = items[0]
        playlist = item.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads', '')
        ids = self.pool.playlist_video_ids(playlist, limit=MAX_UPLOADS) if playlist else []
        videos = [asdict(_video_from_item(v)) for v in self.pool.videos(ids)] if ids else []
        videos = [v for v in videos if v['channel_id'] == channel_id]
        for video in videos:
            video['published_at_normalized'] = normalize_published_at(video['published_at'])
            snippet = video.get('raw', {}).get('snippet', {})
            video['category_id'] = str(snippet.get('categoryId') or '')
        channel = asdict(_channel_from_item(item))
        category_counts = Counter(v['category_id'] for v in videos if v['category_id'])
        category = category_counts.most_common(1)[0][0] if category_counts else ''
        channel['topic'] = classify_topic(category=category, title=channel['title'],
                                          description=channel['description'])
        from beacon_engine.research.providers.base import ProviderVideo
        score = score_channel_text(
            [ProviderVideo(**{k: v[k] for k in ProviderVideo.__dataclass_fields__}) for v in videos],
            channel_title=channel['title'],
        )
        classified = asdict(score)
        classified.update(channel_id=channel_id, likely_faceless=int(score.likely_faceless),
                          classifier=CLASSIFIER_VERSION, classified_at=utc_now_iso())
        counts = [v['views'] for v in videos]
        return {
            'channel': channel, 'videos': videos, 'classifications': [classified],
            'snapshots': [{'video_id': v['video_id'], 'views': v['views'],
                           'captured_at': utc_now_iso(), 'source': 'manual-save'} for v in videos],
            'average_views': sum(counts) / len(counts) if counts else 0,
            'median_views': median(counts) if counts else 0,
        }


class ChannelSaveWorker:
    def __init__(self, queue, *, provider_factory=YouTubeSaveProvider):
        self.queue = queue
        self.provider_factory = provider_factory

    def run_once(self):
        job = self.queue.claim()
        if job is None:
            return False
        try:
            provider = None
            if job['phase'] == 'resolve':
                if job['link_kind'] == 'channel_id':
                    channel_id = job['link_value']
                else:
                    provider = self.provider_factory()
                    channel_id = provider.resolve(job['link_kind'], job['link_value'])
                self.queue.file_channel(job, channel_id)
                # Analysis has its own bounded retry budget and starts in a later
                # claim. A crash here cannot undo the committed Saved membership.
                with self.queue.store.connect() as conn:
                    self.queue._owned(conn, job)
                    conn.execute("UPDATE channel_save_requests SET status='queued',lease_owner='',"
                                 "lease_until=0,available_at=0 WHERE id=?", (job['id'],))
            else:
                provider = self.provider_factory()
                self.queue.apply_analysis(job, provider.analyse(job['channel_id']))
        except LostSaveLease:
            pass  # A newer worker owns it; stale results must never be committed.
        except Exception as exc:
            # Provider/driver exceptions can contain API keys or DSNs. Persist a
            # readable category, never the raw exception string.
            message = str(exc) if isinstance(exc, InvalidSaveLink) else (
                'Could not contact YouTube or finish this step. Your request and any saved channel are kept.'
            )
            try:
                self.queue.fail(job, message, permanent=isinstance(exc, InvalidSaveLink))
            except LostSaveLease:
                pass
        return True


def main():
    import signal
    import threading
    from dotenv import load_dotenv
    from beacon_engine.research.store import YouTubeStore, resolve_index_path
    load_dotenv(Path(os.getenv("BEACON_ENV_FILE") or Path(__file__).resolve().parents[2] / ".env"), override=False)
    queue = ChannelSaveQueue(YouTubeStore(resolve_index_path()))
    worker = ChannelSaveWorker(queue)
    stopping = threading.Event()
    next_refresh = 0.0
    for name in ('SIGTERM', 'SIGINT'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            if time.monotonic() >= next_refresh:
                # Five-minute scheduling tick, six-hour per-channel cooldown.
                next_refresh = time.monotonic() + 300
                queue.enqueue_due_saved()
            worked = worker.run_once()
        except Exception:
            # A database outage must not discard jobs or expose connection secrets.
            worked = False
        stopping.wait(.25 if worked else 2)


if __name__ == '__main__':
    main()
