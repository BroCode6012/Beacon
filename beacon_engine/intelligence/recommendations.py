"""Discovery candidates, saved views, alerts, jobs, and morning briefs."""

from __future__ import annotations

import datetime as dt
import json
import re
from hashlib import sha1

from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.workspaces import NicheWorkspaceService
from beacon_engine.research.providers.base import normalize_published_at, utc_now_iso
from beacon_engine.research.store import YouTubeStore

_SIGNAL_SEVERITIES = {"info", "medium", "high"}
_OPERATOR_SIGNAL_TYPES = {
    "hermes_brief",
    "emerging_niche",
    "classification_review",
    "curation_review",
    "pipeline_attention",
}


class IntelligenceRecommendationService:
    def __init__(self, store: YouTubeStore) -> None:
        self.store = store
        self.workspaces = NicheWorkspaceService(store)
        self.metrics = ChannelMetricsService(store)

    def discovery_candidates(self, niche_id: int, limit: int = 30) -> list[dict]:
        tracked = {
            item["channel_id"]
            for item in self.workspaces.workspace_channels(niche_id)
        }
        with self.store.connect() as conn:
            shared = conn.execute(
                """
                WITH tracked_queries AS (
                    SELECT DISTINCT vd.query
                    FROM niche_channels nc
                    JOIN videos v ON v.channel_id = nc.channel_id
                    JOIN video_discoveries vd ON vd.video_id = v.video_id
                    WHERE nc.niche_id=? AND nc.enabled=1
                      AND nc.role='competitor' AND vd.query != ''
                )
                SELECT
                    v.channel_id,
                    COALESCE(NULLIF(MAX(c.title), ''), MAX(v.channel), v.channel_id)
                        AS title,
                    COUNT(DISTINCT vd.query) AS shared_queries,
                    COUNT(DISTINCT v.video_id) AS matched_videos,
                    MAX(v.views) AS best_views,
                    SUM(v.views) AS matched_views,
                    COALESCE(MAX(c.subscribers), 0) AS subscribers,
                    COALESCE(MAX(cc.confidence), 0) AS faceless_confidence,
                    COALESCE(MAX(cc.likely_faceless), 0) AS likely_faceless
                FROM video_discoveries vd
                JOIN tracked_queries tq ON tq.query = vd.query
                JOIN videos v ON v.video_id = vd.video_id
                LEFT JOIN channels c ON c.channel_id = v.channel_id
                LEFT JOIN channel_classifications cc
                    ON cc.channel_id = v.channel_id
                WHERE v.channel_id != ''
                GROUP BY v.channel_id
                ORDER BY
                    likely_faceless DESC,
                    shared_queries DESC,
                    matched_videos DESC,
                    matched_views DESC
                LIMIT ?
                """,
                (int(niche_id), max(int(limit) * 3, int(limit))),
            ).fetchall()
            if not shared:
                shared = conn.execute(
                    """
                    SELECT
                        v.channel_id,
                        COALESCE(NULLIF(MAX(c.title), ''), MAX(v.channel), v.channel_id)
                            AS title,
                        0 AS shared_queries,
                        COUNT(DISTINCT v.video_id) AS matched_videos,
                        MAX(v.views) AS best_views,
                        SUM(v.views) AS matched_views,
                        COALESCE(MAX(c.subscribers), 0) AS subscribers,
                        COALESCE(MAX(cc.confidence), 0) AS faceless_confidence,
                        COALESCE(MAX(cc.likely_faceless), 0) AS likely_faceless
                    FROM videos v
                    LEFT JOIN channels c ON c.channel_id = v.channel_id
                    LEFT JOIN channel_classifications cc
                        ON cc.channel_id = v.channel_id
                    WHERE v.channel_id != ''
                    GROUP BY v.channel_id
                    ORDER BY likely_faceless DESC, matched_views DESC
                    LIMIT ?
                    """,
                    (max(int(limit) * 3, int(limit)),),
                ).fetchall()
        return [
            {
                **dict(row),
                "url": f"https://www.youtube.com/channel/{row['channel_id']}",
            }
            for row in shared
            if row["channel_id"] not in tracked
        ][: max(int(limit), 0)]

    def save_view(
        self,
        niche_id: int,
        name: str,
        *,
        entity_type: str = "feed",
        filters: dict | None = None,
    ) -> int:
        clean = " ".join((name or "").split())
        if not clean:
            raise ValueError("Saved view name is required.")
        now = utc_now_iso()
        with self.store.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO saved_views (
                    niche_id, name, entity_type, filters_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT({self.store.saved_view_conflict()}) DO UPDATE SET
                    entity_type=excluded.entity_type,
                    filters_json=excluded.filters_json,
                    updated_at=excluded.updated_at
                """,
                (
                    int(niche_id),
                    clean,
                    entity_type,
                    json.dumps(filters or {}, ensure_ascii=True),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                f"SELECT id FROM saved_views WHERE niche_id=? AND {self.store.casefold('name')}={self.store.casefold('?')}",
                (int(niche_id), clean),
            ).fetchone()
            return int(row["id"])

    def list_saved_views(self, niche_id: int) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM saved_views
                WHERE niche_id=?
                ORDER BY updated_at DESC, name
                """,
                (int(niche_id),),
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["filters"] = _json_load(row.pop("filters_json"), {})
            out.append(row)
        return out

    def delete_saved_view(self, view_id: int) -> None:
        with self.store.connect() as conn:
            conn.execute("DELETE FROM saved_views WHERE id=?", (int(view_id),))

    def _alert_excluded_channels(self) -> set[str]:
        """Channels whose uploads must never raise an alert.

        `channel_exclusions` is documented as "hidden from every view", but alerts were
        reading the workspace feed directly and never consulted it — so a channel the junk
        filter had already thrown out kept generating notifications forever. That is how a
        26.4M-subscriber broadcaster ended up as 525 unread "outliers" in workspaces about
        indie faceless formats.
        """
        try:
            with self.store.connect() as conn:
                return {r[0] for r in conn.execute(
                    "SELECT channel_id FROM channel_exclusions")}
        except Exception:  # noqa: BLE001 — a read failure must not stop alerting entirely
            return set()

    def _retire_excluded_alerts(self, niche_id: int) -> int:
        """Delete this niche's video alerts whose channel is now excluded.

        Deleting rather than marking read: an excluded channel's alert is not something the
        operator has seen and dismissed, it is something that should never have been raised.
        Leaving it read would still inflate every count. The underlying channel and videos
        are untouched — un-excluding the channel lets its alerts be raised again.
        """
        try:
            with self.store.connect() as conn:
                # EXISTS, not IN: the IN form materialises every video belonging to an
                # excluded channel (millions of rows) before comparing. EXISTS walks the
                # ~30k alerts instead and does one primary-key lookup each, which is the
                # difference between minutes and milliseconds on a 3.8M-video index.
                cur = conn.execute(
                    """
                    DELETE FROM alerts
                     WHERE niche_id = ?
                       AND entity_type = 'video'
                       AND EXISTS (
                           SELECT 1 FROM videos v
                            JOIN channel_exclusions x ON x.channel_id = v.channel_id
                            WHERE v.video_id = alerts.entity_id)
                    """,
                    (int(niche_id),),
                )
                return int(cur.rowcount or 0)
        except Exception:  # noqa: BLE001 — cleanup must never break alert detection
            return 0

    def detect_alerts(self, niche_id: int) -> int:
        self._remove_invalid_new_upload_alerts(niche_id)
        videos = self.workspaces.feed(
            niche_id,
            days=30,
            format_mode="all",
            sort="newest",
            limit=None,
            roles={"competitor"},
        )
        excluded = self._alert_excluded_channels()
        if excluded:
            videos = [v for v in videos if v.get("channel_id") not in excluded]
            # Retire alerts already raised for a channel that has since been excluded.
            # Filtering new ones is not enough on its own: the ones raised before the
            # exclusion sit unread forever, and an operator judges the feed by what is IN
            # it. Excluding a channel has to clean up after itself.
            self._retire_excluded_alerts(niche_id)
        now = dt.datetime.now(dt.timezone.utc)
        inserted = 0
        for video in videos:
            detected: list[tuple[str, str, str, str, dict]] = []
            first_seen = _parse_date(video.get("first_seen_at") or "")
            published = _published_date(video)
            if (
                first_seen
                and published
                and -6 * 3600 <= (now - published).total_seconds() <= 48 * 3600
                and 0 <= (now - first_seen).total_seconds() <= 48 * 3600
            ):
                detected.append((
                    "new_upload",
                    "info",
                    f"New YouTube upload: {video.get('title') or video['video_id']}",
                    (
                        f"{video.get('channel') or 'Channel'} published this on "
                        f"{_display_date(published)}; first indexed on "
                        f"{_display_date(first_seen)}."
                    ),
                    {
                        "published_at": _iso_utc(published),
                        "published_label": video.get("published_at", ""),
                        "first_seen_at": video.get("first_seen_at", ""),
                    },
                ))
            score = float(video.get("outlier_score") or 0)
            if score >= 2.0:
                detected.append((
                    "outlier",
                    "high" if score >= 5 else "medium",
                    f"{score:.1f}x outlier: {video.get('title') or video['video_id']}",
                    f"{int(video.get('views') or 0):,} views versus "
                    f"{int(video.get('baseline_views') or 0):,} baseline.",
                    {
                        "outlier_score": score,
                        "views": int(video.get("views") or 0),
                        "baseline_views": int(video.get("baseline_views") or 0),
                    },
                ))
            velocity = float(video.get("observed_views_per_hour") or 0)
            if bool(video.get("velocity_reliable")) and velocity >= 100:
                detected.append((
                    "acceleration",
                    "high" if velocity >= 1_000 else "medium",
                    f"Accelerating: {video.get('title') or video['video_id']}",
                    f"Observed growth is {velocity:,.0f} views/hour.",
                    {
                        "observed_views_per_hour": velocity,
                        "observed_views_gained": int(
                            video.get("observed_views_gained") or 0
                        ),
                        "snapshot_count": int(video.get("snapshot_count") or 0),
                        "observed_hours": float(video.get("observed_hours") or 0),
                    },
                ))
            for alert_type, severity, title, message, evidence in detected:
                full_evidence = {
                    **evidence,
                    "url": video.get("url", ""),
                    "channel": video.get("channel", ""),
                }
                if alert_type == "new_upload":
                    inserted += self._upsert_new_upload_alert(
                        niche_id=niche_id,
                        video_id=video["video_id"],
                        severity=severity,
                        title=title,
                        message=message,
                        evidence=full_evidence,
                    )
                else:
                    inserted += self._insert_alert(
                        niche_id=niche_id,
                        entity_type="video",
                        entity_id=video["video_id"],
                        alert_type=alert_type,
                        severity=severity,
                        title=title,
                        message=message,
                        evidence=full_evidence,
                    )
        return inserted

    def _remove_invalid_new_upload_alerts(self, niche_id: int) -> int:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.id, a.detected_at,
                    v.published_at, v.published_at_normalized, v.first_seen_at
                FROM alerts a
                LEFT JOIN videos v ON v.video_id = a.entity_id
                WHERE a.niche_id=? AND a.alert_type='new_upload'
                """,
                (int(niche_id),),
            ).fetchall()
            invalid_ids = []
            for raw in rows:
                row = dict(raw)
                detected = _parse_date(row.get("detected_at") or "")
                first_seen = _parse_date(row.get("first_seen_at") or "")
                published = _published_date(row)
                valid = bool(
                    detected
                    and first_seen
                    and published
                    and -6 * 3600
                    <= (detected - published).total_seconds()
                    <= 48 * 3600
                    and 0
                    <= (detected - first_seen).total_seconds()
                    <= 48 * 3600
                )
                if not valid:
                    invalid_ids.append(int(row["id"]))
            if invalid_ids:
                conn.executemany(
                    "DELETE FROM alerts WHERE id=?",
                    [(alert_id,) for alert_id in invalid_ids],
                )
        return len(invalid_ids)

    def _upsert_new_upload_alert(
        self,
        *,
        niche_id: int,
        video_id: str,
        severity: str,
        title: str,
        message: str,
        evidence: dict,
    ) -> int:
        with self.store.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM alerts
                WHERE niche_id=? AND entity_type='video'
                  AND entity_id=? AND alert_type='new_upload'
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(niche_id), video_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE alerts
                    SET severity=?, title=?, message=?, evidence_json=?
                    WHERE id=?
                    """,
                    (
                        severity,
                        title,
                        message,
                        json.dumps(evidence, ensure_ascii=True),
                        int(existing["id"]),
                    ),
                )
                return 0
        return self._insert_alert(
            niche_id=niche_id,
            entity_type="video",
            entity_id=video_id,
            alert_type="new_upload",
            severity=severity,
            title=title,
            message=message,
            evidence=evidence,
        )

    def list_alerts(
        self,
        niche_id: int,
        *,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        where = "AND read_at IS NULL" if unread_only else ""
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM alerts
                WHERE niche_id=? {where}
                ORDER BY detected_at DESC, id DESC
                LIMIT ?
                """,
                (int(niche_id), max(int(limit), 0)),
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["evidence"] = _json_load(row.pop("evidence_json"), {})
            out.append(row)
        return out

    def mark_alert_read(self, alert_id: int, read: bool = True) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE alerts SET read_at=? WHERE id=?",
                (utc_now_iso() if read else None, int(alert_id)),
            )

    def list_niche_signals(
        self,
        *,
        niche_id: int | None = None,
        unread_only: bool = True,
        days: int | None = 30,
        limit: int = 100,
    ) -> dict:
        """Return a deduplicated notification feed plus workspace summaries.

        Alert detection can observe the same video repeatedly as its evidence
        changes. The durable table keeps that history; the operator feed shows
        only the newest row for each niche/entity/type so one accelerating
        video cannot flood the interface.
        """
        clean_niche_id = int(niche_id) if niche_id is not None else None
        clean_days = (
            max(1, min(int(days), 3650))
            if days is not None
            else None
        )
        clean_limit = max(0, min(int(limit), 500))
        where = ["1=1"]
        params: list[object] = []
        if clean_niche_id is not None:
            where.append("a.niche_id=?")
            params.append(clean_niche_id)
        if clean_days is not None:
            where.append("datetime(a.detected_at) >= datetime('now', ?)")
            params.append(f"-{clean_days} days")
        where_sql = " AND ".join(where)
        unread_sql = " AND read_at IS NULL" if unread_only else ""
        ranked_sql = f"""
            WITH ranked AS (
                SELECT
                    a.*,
                    n.name AS niche_name,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            a.niche_id,
                            a.entity_type,
                            a.entity_id,
                            a.alert_type
                        ORDER BY a.detected_at DESC, a.id DESC
                    ) AS signal_rank
                FROM alerts a
                JOIN niches n ON n.id = a.niche_id
                WHERE {where_sql}
            )
        """
        with self.store.connect() as conn:
            rows = conn.execute(
                ranked_sql
                + """
                SELECT *
                FROM ranked
                WHERE signal_rank=1
                """
                + unread_sql
                + """
                ORDER BY
                    detected_at DESC,
                    CASE severity
                        WHEN 'high' THEN 0
                        WHEN 'medium' THEN 1
                        ELSE 2
                    END,
                    id DESC
                LIMIT ?
                """,
                [*params, clean_limit],
            ).fetchall()
            summaries = conn.execute(
                ranked_sql
                + """
                SELECT
                    niche_id,
                    COUNT(*) AS signal_count,
                    SUM(CASE WHEN severity='high' THEN 1 ELSE 0 END)
                        AS high_signal_count,
                    MAX(detected_at) AS latest_signal_at
                FROM ranked
                WHERE signal_rank=1
                """
                + unread_sql
                + """
                GROUP BY niche_id
                """,
                params,
            ).fetchall()

        signals = []
        for raw in rows:
            row = dict(raw)
            row.pop("signal_rank", None)
            row["evidence"] = _json_load(row.pop("evidence_json"), {})
            signals.append(row)

        if clean_niche_id is None:
            workspaces = self.workspaces.list_workspaces()
        else:
            workspace = self.workspaces.get_workspace(clean_niche_id)
            workspaces = [workspace] if workspace else []
        counts = {int(row["niche_id"]): dict(row) for row in summaries}
        for workspace in workspaces:
            summary = counts.get(int(workspace["id"]), {})
            workspace["signal_count"] = int(summary.get("signal_count") or 0)
            workspace["high_signal_count"] = int(
                summary.get("high_signal_count") or 0
            )
            workspace["latest_signal_at"] = summary.get("latest_signal_at") or ""

        return {
            "signals": signals,
            "workspaces": workspaces,
            "signal_count": sum(
                int(item.get("signal_count") or 0) for item in workspaces
            ),
            "high_signal_count": sum(
                int(item.get("high_signal_count") or 0) for item in workspaces
            ),
            "unread_only": bool(unread_only),
            "days": clean_days,
            "generated_at": utc_now_iso(),
        }

    def mark_niche_alerts(
        self,
        *,
        alert_ids: list[int] | None = None,
        niche_id: int | None = None,
        read: bool = True,
    ) -> int:
        """Acknowledge exact alerts or all alerts in one niche.

        Marking is reversible (`read=False`) and reports rows whose state
        actually changed rather than echoing the requested IDs.
        """
        ids = list(dict.fromkeys(
            int(value) for value in (alert_ids or []) if int(value) > 0
        ))
        if len(ids) > 500:
            raise ValueError("At most 500 alert IDs may be changed at once.")
        clean_niche_id = int(niche_id) if niche_id is not None else None
        if not ids and clean_niche_id is None:
            raise ValueError("Provide alert_ids or niche_id.")

        where = []
        params: list[object] = []
        if ids:
            where.append(f"id IN ({', '.join('?' for _ in ids)})")
            params.extend(ids)
        if clean_niche_id is not None:
            where.append("niche_id=?")
            params.append(clean_niche_id)
        if read:
            where.append("read_at IS NULL")
            value = utc_now_iso()
        else:
            where.append("read_at IS NOT NULL")
            value = None
        with self.store.connect() as conn:
            cur = conn.execute(
                f"UPDATE alerts SET read_at=? WHERE {' AND '.join(where)}",
                [value, *params],
            )
            return int(cur.rowcount)

    def publish_niche_signal(
        self,
        *,
        niche_id: int,
        title: str,
        message: str,
        signal_type: str = "hermes_brief",
        severity: str = "info",
        subject: str = "",
        source: str = "hermes",
        evidence: dict | None = None,
    ) -> dict:
        """Publish one bounded operator notification into the normal alert feed."""
        workspace = self.workspaces.get_workspace(int(niche_id))
        if not workspace:
            raise KeyError(f"Unknown niche workspace: {niche_id}")
        clean_title = " ".join(str(title or "").split())
        clean_message = " ".join(str(message or "").split())
        clean_type = str(signal_type or "").strip().lower()
        clean_severity = str(severity or "").strip().lower()
        clean_source = re.sub(r"[^a-z0-9_-]+", "-", str(source or "hermes").lower())
        clean_subject = re.sub(
            r"[^a-z0-9_-]+",
            "-",
            str(subject or clean_type).strip().lower(),
        ).strip("-")
        if not clean_title or not clean_message:
            raise ValueError("Signal title and message are required.")
        if len(clean_title) > 160 or len(clean_message) > 1000:
            raise ValueError("Signal title/message is too long.")
        if clean_type not in _OPERATOR_SIGNAL_TYPES:
            raise ValueError(f"Unsupported operator signal type: {signal_type}")
        if clean_severity not in _SIGNAL_SEVERITIES:
            raise ValueError(f"Unsupported signal severity: {severity}")
        if not isinstance(evidence, (dict, type(None))):
            raise ValueError("Signal evidence must be an object.")

        payload = {
            **(evidence or {}),
            "source": clean_source or "hermes",
            "operator_title": clean_title,
            "operator_message": clean_message,
        }
        inserted = self._insert_alert(
            niche_id=int(niche_id),
            entity_type="operator",
            entity_id=f"{clean_source or 'hermes'}:{clean_subject or clean_type}"[:180],
            alert_type=clean_type,
            severity=clean_severity,
            title=clean_title,
            message=clean_message,
            evidence=payload,
        )
        return {
            "published": int(inserted),
            "niche_id": int(niche_id),
            "niche_name": workspace["name"],
            "signal_type": clean_type,
            "severity": clean_severity,
        }

    def morning_brief(self, niche_id: int, limit: int = 8, *, detect: bool = True) -> dict:
        # detect_alerts WRITES the alerts table. The session chat agent (read-only on
        # the DB) passes detect=False and reads whatever the radar already computed.
        if detect:
            self.detect_alerts(niche_id)
        workspace = self.workspaces.get_workspace(niche_id)
        alerts = self.list_alerts(niche_id, unread_only=True, limit=limit)
        metrics = self.metrics.workspace_metrics(niche_id, roles={"competitor"})
        leaders = sorted(
            metrics,
            key=lambda item: (
                float(item["channel_outlier_score"] or 0),
                float(item["average_views"] or 0),
            ),
            reverse=True,
        )[:5]
        recent = self.workspaces.feed(
            niche_id,
            days=7,
            sort="newest",
            limit=10,
            roles={"competitor"},
        )
        velocity_rows = self.workspaces.feed(
            niche_id,
            days=30,
            sort="velocity",
            limit=None,
            roles={"competitor"},
        )
        velocity_ready = sum(
            bool(item.get("velocity_reliable"))
            for item in velocity_rows
        )
        return {
            "workspace": workspace,
            "alerts": alerts,
            "channel_leaders": leaders,
            "recent_uploads": recent,
            "velocity_ready_count": velocity_ready,
            "velocity_pending_count": max(len(velocity_rows) - velocity_ready, 0),
            "generated_at": utc_now_iso(),
        }

    def create_job(
        self,
        job_type: str,
        *,
        scope: str = "",
        schedule: str = "",
        status: str = "queued",
    ) -> int:
        now = utc_now_iso()
        with self.store.connect() as conn:
            cur = self.store.insert_id(
                conn,
                """
                INSERT INTO background_jobs (
                    job_type, scope, status, schedule, progress_json,
                    error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', '', ?, ?)
                """,
                (job_type, scope, status, schedule, now, now),
            )
            return cur

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        progress: dict | None = None,
        error: str | None = None,
    ) -> None:
        updates = ["updated_at=?"]
        params: list[object] = [utc_now_iso()]
        if status is not None:
            updates.append("status=?")
            params.append(status)
        if progress is not None:
            updates.append("progress_json=?")
            params.append(json.dumps(progress, ensure_ascii=True))
        if error is not None:
            updates.append("error=?")
            params.append(error)
        params.append(int(job_id))
        with self.store.connect() as conn:
            conn.execute(
                f"UPDATE background_jobs SET {', '.join(updates)} WHERE id=?",
                params,
            )

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM background_jobs
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(int(limit), 0),),
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["progress"] = _json_load(row.pop("progress_json"), {})
            out.append(row)
        return out

    def retry_job(self, job_id: int) -> None:
        self.update_job(
            job_id,
            status="queued",
            progress={},
            error="",
        )

    def _insert_alert(
        self,
        *,
        niche_id: int,
        entity_type: str,
        entity_id: str,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        evidence: dict,
    ) -> int:
        fingerprint = sha1(
            f"{niche_id}:{entity_type}:{entity_id}:{alert_type}:"
            f"{json.dumps(evidence, sort_keys=True)}".encode("utf-8")
        ).hexdigest()
        with self.store.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts (
                    niche_id, entity_type, entity_id, alert_type, severity,
                    title, message, evidence_json, fingerprint, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (
                    int(niche_id),
                    entity_type,
                    entity_id,
                    alert_type,
                    severity,
                    title,
                    message,
                    json.dumps(evidence, ensure_ascii=True),
                    fingerprint,
                    utc_now_iso(),
                ),
            )
            return 1 if cur.rowcount else 0


def _json_load(value: str, default):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _parse_date(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _published_date(video: dict) -> dt.datetime | None:
    normalized = video.get("published_at_normalized") or normalize_published_at(
        video.get("published_at") or "",
        reference=video.get("first_seen_at") or None,
    )
    return _parse_date(normalized)


def _display_date(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
