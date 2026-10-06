"""Deterministic pattern mining and evidence-backed idea generation."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from beacon_engine.config import Tier
from beacon_engine.json_utils import as_list, extract_json
from beacon_engine.research.providers.base import utc_now_iso
from beacon_engine.research.providers.comments_provider import CommentDownloaderProvider
from beacon_engine.research.providers.transcript_provider import TranscriptProvider
from beacon_engine.research.models import VideoStat
from beacon_engine.research.store import YouTubeStore
from beacon_engine.research.thumbnails import analyze_thumbnails, download_thumbnails
from beacon_engine.intelligence.workspaces import NicheWorkspaceService

_STOPWORDS = {
    "about", "after", "again", "against", "because", "before", "being",
    "could", "does", "from", "have", "into", "just", "more", "most",
    "only", "over", "that", "their", "them", "then", "there", "these",
    "they", "this", "those", "through", "under", "very", "what", "when",
    "where", "which", "while", "with", "would", "your", "the", "and",
    "for", "you", "why", "how",
}

_FRAMEWORKS = [
    ("numbered_list", re.compile(r"^\s*\d+\b")),
    ("why_question", re.compile(r"^\s*why\b", re.I)),
    ("how_explainer", re.compile(r"^\s*how\b", re.I)),
    ("truth_reveal", re.compile(r"\btruth\b|\bexposed\b|\brevealed\b", re.I)),
    ("rise_and_fall", re.compile(r"\brise and fall\b|\bfall of\b", re.I)),
    ("what_happened", re.compile(r"\bwhat happened\b", re.I)),
    ("warning", re.compile(r"\bstop\b|\bnever\b|\bavoid\b|\bdanger", re.I)),
    ("mystery", re.compile(r"\bmystery\b|\bunsolved\b|\bforbidden\b", re.I)),
    ("comparison", re.compile(r"\bvs\.?\b|\bversus\b|\bbest\b|\bworst\b", re.I)),
]


class PatternIntelligenceService:
    def __init__(self, store: YouTubeStore) -> None:
        self.store = store
        self.workspaces = NicheWorkspaceService(store)

    def extract(
        self,
        niche_id: int,
        days: int | None = 365,
        roles: set[str] | tuple[str, ...] | list[str] | None = ("competitor",),
    ) -> dict:
        feed = self.workspaces.feed(
            niche_id,
            days=days,
            sort="views",
            limit=None,
            roles=roles,
        )
        frameworks: dict[str, list[dict]] = defaultdict(list)
        words: Counter[str] = Counter()
        for video in feed:
            title = video.get("title") or ""
            for name, pattern in _FRAMEWORKS:
                if pattern.search(title):
                    frameworks[name].append(video)
            words.update(
                token
                for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", title.lower())
                if token not in _STOPWORDS
            )
        title_patterns = [
            {
                "name": name,
                "count": len(items),
                "average_views": int(
                    sum(int(item.get("views") or 0) for item in items) / len(items)
                ),
                "examples": [
                    {
                        "video_id": item["video_id"],
                        "title": item.get("title", ""),
                        "views": int(item.get("views") or 0),
                        "url": item.get("url", ""),
                    }
                    for item in sorted(
                        items,
                        key=lambda row: int(row.get("views") or 0),
                        reverse=True,
                    )[:5]
                ],
            }
            for name, items in frameworks.items()
        ]
        title_patterns.sort(
            key=lambda item: (item["average_views"], item["count"]),
            reverse=True,
        )
        topics = [
            {"topic": word, "mentions": count}
            for word, count in words.most_common(25)
        ]
        return {
            "niche_id": int(niche_id),
            "video_count": len(feed),
            "title_patterns": title_patterns,
            "topics": topics,
            "generated_at": utc_now_iso(),
        }

    def save_findings(self, niche_id: int, patterns: dict | None = None) -> int:
        patterns = patterns or self.extract(niche_id)
        count = 0
        with self.store.connect() as conn:
            for item in patterns.get("title_patterns", [])[:12]:
                conn.execute(
                    """
                    INSERT INTO research_findings (
                        niche_id, finding_type, title, explanation,
                        evidence_json, confidence, created_at
                    ) VALUES (?, 'title_pattern', ?, ?, ?, ?, ?)
                    """,
                    (
                        int(niche_id),
                        item["name"].replace("_", " ").title(),
                        (
                            f"Appears in {item['count']} tracked videos with "
                            f"{item['average_views']:,} average views."
                        ),
                        json.dumps(item.get("examples", []), ensure_ascii=True),
                        min(1.0, 0.45 + item["count"] * 0.06),
                        utc_now_iso(),
                    ),
                )
                count += 1
        return count

    def list_findings(self, niche_id: int, limit: int = 100) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM research_findings
                WHERE niche_id=?
                ORDER BY created_at DESC, confidence DESC
                LIMIT ?
                """,
                (int(niche_id), max(int(limit), 0)),
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["evidence"] = _json_load(row.pop("evidence_json"), [])
            out.append(row)
        return out

    def enrichment_candidates(self, niche_id: int, limit: int = 20) -> list[dict]:
        feed = self.workspaces.feed(
            niche_id,
            days=365,
            sort="outlier",
            limit=None,
            roles={"competitor"},
        )
        with self.store.connect() as conn:
            transcript_ids = {
                row["video_id"]
                for row in conn.execute("SELECT DISTINCT video_id FROM transcripts")
            }
            comment_ids = {
                row["video_id"]
                for row in conn.execute("SELECT DISTINCT video_id FROM comments")
            }
        out = []
        for video in feed:
            video_id = video["video_id"]
            item = dict(video)
            item["needs_transcript"] = video_id not in transcript_ids
            item["needs_comments"] = video_id not in comment_ids
            if item["needs_transcript"] or item["needs_comments"]:
                out.append(item)
        return out[: max(int(limit), 0)]

    def enrich_selected(
        self,
        niche_id: int,
        *,
        video_ids: list[str] | None = None,
        limit: int = 5,
        comments_per_video: int = 25,
        transcript_provider=None,
        comment_provider=None,
    ) -> dict:
        candidates = self.enrichment_candidates(
            niche_id,
            limit=max(int(limit), 0) if not video_ids else 500,
        )
        selected = (
            [item for item in candidates if item["video_id"] in set(video_ids)]
            if video_ids
            else candidates[: max(int(limit), 0)]
        )
        transcripts = 0
        comments = 0
        errors: list[str] = []
        transcript_provider = transcript_provider or TranscriptProvider()
        comment_provider = comment_provider or CommentDownloaderProvider()
        for item in selected:
            video_id = item["video_id"]
            if item.get("needs_transcript"):
                try:
                    transcript = transcript_provider.transcript(video_id)
                    if self.store.save_transcript(transcript):
                        transcripts += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{video_id} transcript: {exc}")
            if item.get("needs_comments"):
                try:
                    found = comment_provider.comments(
                        video_id,
                        limit=max(int(comments_per_video), 0),
                    )
                    comments += self.store.save_comments(found)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{video_id} comments: {exc}")
        return {
            "videos": len(selected),
            "transcripts": transcripts,
            "comments": comments,
            "errors": errors,
        }

    def enrichment_summary(self, niche_id: int) -> dict:
        feed = self.workspaces.feed(
            niche_id,
            days=None,
            sort="views",
            limit=None,
            roles={"competitor"},
        )
        video_ids = [item["video_id"] for item in feed if item.get("video_id")]
        if not video_ids:
            return {
                "videos": 0,
                "videos_with_transcripts": 0,
                "transcripts": 0,
                "videos_with_comments": 0,
                "comments": 0,
            }
        placeholders = ", ".join("?" for _ in video_ids)
        with self.store.connect() as conn:
            transcript = conn.execute(
                f"""
                SELECT COUNT(*) AS rows, COUNT(DISTINCT video_id) AS videos
                FROM transcripts
                WHERE video_id IN ({placeholders})
                """,
                video_ids,
            ).fetchone()
            comments = conn.execute(
                f"""
                SELECT COUNT(*) AS rows, COUNT(DISTINCT video_id) AS videos
                FROM comments
                WHERE video_id IN ({placeholders})
                """,
                video_ids,
            ).fetchone()
        return {
            "videos": len(video_ids),
            "videos_with_transcripts": int(transcript["videos"] or 0),
            "transcripts": int(transcript["rows"] or 0),
            "videos_with_comments": int(comments["videos"] or 0),
            "comments": int(comments["rows"] or 0),
        }

    def enrichment_evidence(
        self,
        niche_id: int,
        *,
        video_limit: int = 8,
        transcript_chars: int = 1800,
        comments_per_video: int = 8,
    ) -> list[dict]:
        feed = self.workspaces.feed(
            niche_id,
            days=365,
            sort="outlier",
            limit=max(int(video_limit) * 3, int(video_limit)),
            roles={"competitor"},
        )
        evidence = []
        with self.store.connect() as conn:
            for video in feed:
                transcript = conn.execute(
                    """
                    SELECT text, language
                    FROM transcripts
                    WHERE video_id=?
                    ORDER BY last_seen_at DESC
                    LIMIT 1
                    """,
                    (video["video_id"],),
                ).fetchone()
                comments = conn.execute(
                    """
                    SELECT text, like_count
                    FROM comments
                    WHERE video_id=?
                    ORDER BY like_count DESC, last_seen_at DESC
                    LIMIT ?
                    """,
                    (video["video_id"], max(int(comments_per_video), 0)),
                ).fetchall()
                if not transcript and not comments:
                    continue
                evidence.append({
                    "video_id": video["video_id"],
                    "title": video.get("title", ""),
                    "channel": video.get("channel", ""),
                    "url": video.get("url", ""),
                    "transcript_excerpt": (
                        str(transcript["text"] or "")[: max(int(transcript_chars), 0)]
                        if transcript else ""
                    ),
                    "comments": [
                        {
                            "text": str(item["text"] or ""),
                            "likes": int(item["like_count"] or 0),
                        }
                        for item in comments
                    ],
                })
                if len(evidence) >= max(int(video_limit), 0):
                    break
        return evidence

    def analyze_thumbnail_patterns(
        self,
        niche_id: int,
        *,
        llm,
        limit: int = 12,
    ) -> dict:
        feed = self.workspaces.feed(
            niche_id,
            days=365,
            sort="outlier",
            limit=max(int(limit) * 3, int(limit)),
            roles={"competitor"},
        )
        if not feed:
            raise ValueError("No enabled competitor videos are available.")
        videos = [
            VideoStat(
                video_id=item["video_id"],
                title=item.get("title", ""),
                channel=item.get("channel", ""),
                channel_id=item.get("channel_id", ""),
                views=int(item.get("views") or 0),
                published_at=(
                    item.get("published_at_normalized")
                    or item.get("published_at")
                    or ""
                ),
                url=item.get("url", ""),
            )
            for item in feed
        ]
        downloaded = download_thumbnails(videos, limit=max(int(limit), 1))
        if not downloaded:
            raise ValueError("No thumbnails could be downloaded for this niche.")
        analysis = analyze_thumbnails(llm, downloaded)
        evidence = [
            {
                "video_id": video.video_id,
                "title": video.title,
                "channel": video.channel,
                "views": video.views,
                "url": video.url,
            }
            for video, _data, _media in downloaded
        ]
        with self.store.connect() as conn:
            cur = self.store.insert_id(
                conn,
                """
                INSERT INTO research_findings (
                    niche_id, finding_type, title, explanation,
                    evidence_json, confidence, created_at
                ) VALUES (?, 'thumbnail_pattern', ?, ?, ?, ?, ?)
                """,
                (
                    int(niche_id),
                    "Winning thumbnail patterns",
                    analysis,
                    json.dumps(evidence, ensure_ascii=True),
                    min(1.0, 0.55 + len(evidence) * 0.03),
                    utc_now_iso(),
                ),
            )
            finding_id = cur
        return {
            "id": finding_id,
            "analysis": analysis,
            "evidence": evidence,
            "thumbnail_count": len(evidence),
        }

    def latest_thumbnail_analysis(self, niche_id: int) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM research_findings
                WHERE niche_id=? AND finding_type='thumbnail_pattern'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (int(niche_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["evidence"] = _json_load(item.pop("evidence_json"), [])
        return item

    def generate_ideas(
        self,
        niche_id: int,
        *,
        mode: str = "balanced",
        count: int = 10,
        llm=None,
    ) -> list[dict]:
        clean_mode = mode.strip().lower().replace(" ", "_")
        if clean_mode not in {"balanced", "new_angles", "title_tweaks"}:
            raise ValueError(f"Unsupported idea mode: {mode}")
        workspace = self.workspaces.get_workspace(niche_id)
        if not workspace:
            raise KeyError(f"Unknown niche workspace: {niche_id}")
        patterns = self.extract(niche_id, roles={"competitor"})
        feed = self.workspaces.feed(
            niche_id,
            days=365,
            sort="outlier",
            limit=30,
            roles={"competitor"},
        )
        audience_evidence = self.enrichment_evidence(niche_id, video_limit=6)
        thumbnail_patterns = self.latest_thumbnail_analysis(niche_id)
        evidence = [
            {
                "video_id": item["video_id"],
                "title": item.get("title", ""),
                "channel": item.get("channel", ""),
                "views": int(item.get("views") or 0),
                "outlier_score": float(item.get("outlier_score") or 0),
                "url": item.get("url", ""),
            }
            for item in feed[:20]
        ]
        ideas = (
            self._llm_ideas(
                workspace["name"],
                clean_mode,
                count,
                patterns,
                evidence,
                audience_evidence,
                thumbnail_patterns,
                llm,
            )
            if llm is not None
            else self._fallback_ideas(
                workspace["name"],
                clean_mode,
                count,
                patterns,
                evidence,
            )
        )
        existing = [
            row.get("title", "")
            for row in self.workspaces.feed(
                niche_id,
                days=None,
                limit=None,
                roles={"competitor"},
            )
        ]
        ideas = _dedupe_ideas(ideas, existing)[: max(int(count), 0)]
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO idea_runs (
                    niche_id, mode, input_json, ideas_json, model, cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(niche_id),
                    clean_mode,
                    json.dumps(
                        {
                            "patterns": patterns,
                            "evidence": evidence,
                            "audience_evidence": audience_evidence,
                            "thumbnail_patterns": thumbnail_patterns,
                        },
                        ensure_ascii=True,
                    ),
                    json.dumps(ideas, ensure_ascii=True),
                    getattr(llm, "model_id", "") if llm is not None else "deterministic",
                    0.0,
                    utc_now_iso(),
                ),
            )
        return ideas

    def list_idea_runs(self, niche_id: int, limit: int = 20) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM idea_runs
                WHERE niche_id=?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(niche_id), max(int(limit), 0)),
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["input"] = _json_load(row.pop("input_json"), {})
            row["ideas"] = _json_load(row.pop("ideas_json"), [])
            out.append(row)
        return out

    def _llm_ideas(
        self,
        niche: str,
        mode: str,
        count: int,
        patterns: dict,
        evidence: list[dict],
        audience_evidence: list[dict],
        thumbnail_patterns: dict | None,
        llm,
    ) -> list[dict]:
        instructions = """You are a YouTube intelligence analyst for a faceless studio.
Generate evidence-backed ideas. Return only a JSON array. Every item must contain:
title, angle, hook, why_it_works, evidence_video_ids (array). Never invent evidence."""
        prompt = (
            f"NICHE: {niche}\nMODE: {mode}\nCOUNT: {count}\n"
            f"PATTERNS: {json.dumps(patterns, ensure_ascii=True)[:6000]}\n"
            f"VIDEO EVIDENCE: {json.dumps(evidence, ensure_ascii=True)[:8000]}\n"
            f"AUDIENCE/TRANSCRIPT EVIDENCE: "
            f"{json.dumps(audience_evidence, ensure_ascii=True)[:8000]}\n"
            f"THUMBNAIL PATTERNS: "
            f"{json.dumps(thumbnail_patterns or {}, ensure_ascii=True)[:6000]}"
        )
        try:
            data = extract_json(llm.complete(
                task="analysis",
                tier=Tier.SONNET,
                instructions=instructions,
                prompt=prompt,
                thinking=True,
                effort="medium",
                max_tokens=2500,
            ).text)
        except Exception:
            return self._fallback_ideas(niche, mode, count, patterns, evidence)
        valid_ids = {item["video_id"] for item in evidence}
        out = []
        for item in as_list(data):
            if not isinstance(item, dict) or not item.get("title"):
                continue
            ids = [
                video_id
                for video_id in item.get("evidence_video_ids", [])
                if video_id in valid_ids
            ]
            if not ids and evidence:
                ids = [evidence[0]["video_id"]]
            out.append({
                "title": str(item.get("title", "")).strip(),
                "angle": str(item.get("angle", "")).strip(),
                "hook": str(item.get("hook", "")).strip(),
                "why_it_works": str(item.get("why_it_works", "")).strip(),
                "evidence_video_ids": ids,
            })
        return out

    def _fallback_ideas(
        self,
        niche: str,
        mode: str,
        count: int,
        patterns: dict,
        evidence: list[dict],
    ) -> list[dict]:
        topics = [item["topic"].title() for item in patterns.get("topics", [])[:8]]
        topics = topics or [niche]
        frameworks = [
            item["name"].replace("_", " ")
            for item in patterns.get("title_patterns", [])[:5]
        ] or ["truth reveal", "why question", "warning"]
        templates = {
            "balanced": [
                "The Hidden Truth About {topic}",
                "Why {topic} Is Changing Faster Than Anyone Expected",
                "What Really Happened to {topic}",
            ],
            "new_angles": [
                "The {topic} Detail Everyone Keeps Missing",
                "The Unexpected Industry Built Around {topic}",
                "What {topic} Reveals About the Next Decade",
            ],
            "title_tweaks": [
                "The Truth About {topic} Nobody Explains",
                "Why You Should Look Again at {topic}",
                "{topic}: The Part They Left Out",
            ],
        }
        out = []
        for index in range(max(int(count), 0)):
            topic = topics[index % len(topics)]
            title = templates[mode][index % len(templates[mode])].format(topic=topic)
            source = evidence[index % len(evidence)] if evidence else {}
            framework = frameworks[index % len(frameworks)]
            out.append({
                "title": title,
                "angle": f"Use the proven {framework} structure on {topic}.",
                "hook": f"Within five seconds, reveal the contradiction behind {topic}.",
                "why_it_works": (
                    f"Grounded in {source.get('title', 'the strongest tracked pattern')} "
                    f"and adapted as a {mode.replace('_', ' ')} idea."
                ),
                "evidence_video_ids": (
                    [source["video_id"]] if source.get("video_id") else []
                ),
            })
        return out


def _dedupe_ideas(ideas: list[dict], existing_titles: list[str]) -> list[dict]:
    accepted: list[dict] = []
    comparison = [_normalized(title) for title in existing_titles if title]
    for idea in ideas:
        title = str(idea.get("title") or "").strip()
        normalized = _normalized(title)
        if not normalized:
            continue
        if any(
            SequenceMatcher(None, normalized, other).ratio() >= 0.86
            for other in comparison
        ):
            continue
        comparison.append(normalized)
        accepted.append(idea)
    return accepted


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _json_load(value: str, default):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
