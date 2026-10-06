"""Measured, deterministic base-query top-ups for the always-on radar.

The radar never invents arbitrary text. It selects from a reviewed catalog of
faceless/replicable subjects, requires evidence across several already-indexed
faceless channels, applies measured harvest-yield support, and then passes the
same capacity/cooldown/duplicate rules used by Signal Autopilot.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from beacon_engine.research.channel_discovery import expand_queries
from beacon_engine.research.query_policy import (
    QUERY_MAX_TOTAL,
    QUERY_NEAR_DUPLICATE_JACCARD,
    QUERY_POLICY_KEY,
    normalize_query,
    query_attempts,
    query_family,
    query_tokens,
    token_jaccard,
    watchlist_config_lock,
)
from beacon_engine.research.store import YouTubeStore

_WRITE_LOCK = threading.Lock()

# Reviewed subject gaps only: no celebrities, music, kids, brands/networks, or
# year/faceless/modifier filler. Evidence decides which of these is useful now.
QUERY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "History": (
        "civil war strategy",
        "lost civilizations archaeology",
        "ancient engineering techniques",
        "archaeological discoveries",
        "historical unsolved mysteries",
        "ancient mythology origins",
        "forgotten wars explained",
        "empire collapse explained",
        "ancient trade routes",
        "lost expeditions history",
        "polar exploration disasters",
        "ancient medicine practices",
        "historical hoaxes exposed",
        "shipwreck archaeology",
    ),
    "Nature & Wildlife": (
        "animal survival strategies",
        "predator behavior documentary",
        "deep ocean discoveries",
        "ocean ecosystem mysteries",
        "human evolution discoveries",
        "evolutionary mysteries explained",
        "dinosaur discoveries",
        "paleontology discoveries",
        "wildlife conservation failures",
        "invasive species explained",
        "extinct animals explained",
        "cave ecosystem discoveries",
    ),
    "Science & Discovery": (
        "genetic discoveries explained",
        "scientific fraud cases",
        "psychology experiments explained",
        "medical breakthroughs history",
        "disease outbreak history",
        "chemistry discoveries explained",
        "mathematics mysteries explained",
        "scientific accidents explained",
        "laboratory disasters explained",
        "evolution experiments explained",
    ),
    "Space & Physics": (
        "quantum computing breakthroughs",
        "space mission failures",
        "rocket failures explained",
        "satellite technology explained",
        "space industry economics",
        "physics experiments explained",
        "astronomy discoveries explained",
        "space engineering failures",
    ),
    "Tech & AI": (
        "internet mysteries investigations",
        "cybercrime investigations",
        "technology company failures",
        "artificial intelligence failures",
        "internet infrastructure explained",
        "semiconductor industry explained",
        "software disasters explained",
        "cybersecurity breaches explained",
        "robotics breakthroughs",
        "computer industry history",
        "algorithm failures explained",
        "data privacy investigations",
    ),
    "Money & Finance": (
        "money laundering investigations",
        "market crashes explained",
        "banking scandals explained",
        "insurance fraud cases",
        "economic bubbles explained",
        "currency crises explained",
        "stock market manipulation",
        "ponzi scheme investigations",
        "black market economies",
        "financial fraud investigations",
        "debt crisis explained",
        "tax haven investigations",
    ),
    "Business & Companies": (
        "supply chain failures",
        "corporate fraud investigations",
        "consumer product failures",
        "business monopoly history",
        "luxury industry investigations",
        "counterfeit industry explained",
        "hidden industries explained",
        "corporate espionage operations",
        "manufacturing processes explained",
        "logistics systems explained",
        "retail industry failures",
        "startup collapse investigations",
    ),
    "True Crime & Scams": (
        "cold case investigations",
        "cult investigations",
        "prison escapes explained",
        "organized crime investigations",
        "art heist investigations",
        "smuggling networks explained",
        "forensic science cases",
        "criminal psychology cases",
        "counterfeit crime investigations",
        "espionage operations explained",
    ),
    "Engineering & Vehicles": (
        "maritime disaster investigations",
        "aviation accident investigations",
        "industrial accidents explained",
        "infrastructure failures explained",
        "megaproject failures",
        "engineering failure investigations",
        "nuclear disasters explained",
        "earthquake engineering",
        "mining disasters explained",
        "railway disasters explained",
        "airport operations explained",
        "energy grid failures",
        "water infrastructure explained",
        "urban planning failures",
        "architecture disasters",
        "rare earth mining explained",
        "construction failures explained",
        "tunnel disasters explained",
    ),
    "Health & Body": (
        "medical mysteries cases",
        "food industry investigations",
        "medical fraud investigations",
        "pharmaceutical scandals explained",
        "nutrition industry investigations",
        "public health failures",
        "surgery breakthroughs history",
        "addiction science explained",
    ),
    "Psychology & Mind": (
        "philosophy thought experiments",
        "behavioral science experiments",
        "mass psychology explained",
        "decision making failures",
        "social psychology experiments",
        "cognitive bias investigations",
    ),
    "Geography & Places": (
        "abandoned places history",
        "mountaineering disasters",
        "cave exploration disasters",
        "deep sea disasters",
        "lost city archaeology",
        "island survival history",
    ),
}

_FORMAT_WORDS = {
    "case",
    "cases",
    "discoveries",
    "discovery",
    "documentary",
    "explained",
    "exposed",
    "failure",
    "failures",
    "history",
    "investigation",
    "investigations",
    "operations",
    "origins",
    "practices",
    "strategies",
    "techniques",
}
_MATCH_ALIASES = {
    "archaeological": "archae",
    "archaeology": "archae",
    "companies": "compan",
    "company": "compan",
    "computer": "comput",
    "computers": "comput",
    "computing": "comput",
    "economic": "econom",
    "economics": "econom",
    "economies": "econom",
    "industries": "indust",
    "industry": "indust",
    "mysteries": "myster",
    "mystery": "myster",
}


def _match_key(token: str) -> str:
    token = str(token or "").casefold()
    if token in _MATCH_ALIASES:
        return _MATCH_ALIASES[token]
    if len(token) >= 7:
        return token[:6]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _core_keys(query: str) -> set[str]:
    family = query_family(query)
    return {
        _match_key(token)
        for token in query_tokens(family)
        if token not in _FORMAT_WORDS
    }


def _covered_by_watchlist(query: str, watchlist: list[str]) -> bool:
    normalized = normalize_query(query)
    family = query_family(query)
    candidate_keys = _core_keys(query)
    for existing in watchlist:
        if normalize_query(existing) == normalized:
            return True
        existing_family = query_family(existing)
        if family and (
            family == existing_family
            or token_jaccard(family, existing_family)
            >= QUERY_NEAR_DUPLICATE_JACCARD
        ):
            return True
        existing_keys = _core_keys(existing)
        if (
            len(candidate_keys) >= 2
            and existing_keys
            and (
                candidate_keys <= existing_keys
                or len(candidate_keys & existing_keys)
                / len(candidate_keys | existing_keys)
                >= 0.62
            )
        ):
            return True
    return False


def query_coverage(watchlist: list[str], yield_state: dict, *, expand: bool) -> dict:
    expanded = expand_queries(watchlist) if expand else list(dict.fromkeys(watchlist))
    attempted = sum(
        query_attempts(yield_state.get(query)) > 0
        for query in expanded
    )
    return {
        "expanded": len(expanded),
        "attempted": attempted,
        "remaining": max(0, len(expanded) - attempted),
        "coverage": attempted / len(expanded) if expanded else 1.0,
    }


def _harvest_support(query: str, yield_state: dict) -> dict:
    candidate = _core_keys(query)
    best_average = 0.0
    related_new = 0
    related_searches = 0
    for existing, raw in yield_state.items():
        if not isinstance(raw, dict):
            continue
        existing_keys = _core_keys(existing)
        if not candidate or not existing_keys:
            continue
        overlap = len(candidate & existing_keys) / len(candidate)
        if overlap < 0.5:
            continue
        searches = int(raw.get("searches", 0) or 0)
        new_channels = int(raw.get("total_new", 0) or 0)
        if searches <= 0:
            continue
        related_new += new_channels
        related_searches += searches
        best_average = max(best_average, new_channels / searches)
    return {
        "related_new_channels": related_new,
        "related_searches": related_searches,
        "best_average_yield": round(best_average, 3),
    }


def rank_candidates(
    evidence_rows: list[tuple[str, str, int, str]],
    watchlist: list[str],
    yield_state: dict,
    policy: dict,
    *,
    min_channels: int,
    min_videos: int,
    max_per_topic: int,
) -> list[dict]:
    """Rank reviewed candidates by cross-channel evidence and harvest support."""
    topic_counts = Counter({
        str(topic): int(count or 0)
        for topic, count in (policy.get("radar_topic_counts") or {}).items()
    })
    specs = []
    by_key: dict[str, list[int]] = defaultdict(list)
    for topic, queries in QUERY_CANDIDATES.items():
        for query in queries:
            if _covered_by_watchlist(query, watchlist):
                continue
            keys = _core_keys(query)
            if len(keys) < 2 or topic_counts[topic] >= max_per_topic:
                continue
            index = len(specs)
            primary = sorted(keys, key=lambda value: (-len(value), value))[0]
            specs.append({
                "query": query,
                "topic": topic,
                "keys": keys,
                "channels": set(),
                "videos": 0,
                "views": 0,
            })
            by_key[primary].append(index)

    for title, channel_id, views, _stored_topic in evidence_rows:
        title_keys = {_match_key(token) for token in query_tokens(title)}
        candidate_indexes: set[int] = set()
        for key in title_keys:
            candidate_indexes.update(by_key.get(key, ()))
        for index in candidate_indexes:
            spec = specs[index]
            if spec["keys"] <= title_keys:
                spec["channels"].add(channel_id)
                spec["videos"] += 1
                spec["views"] += max(0, int(views or 0))

    ranked = []
    for spec in specs:
        channel_count = len(spec["channels"])
        video_count = int(spec["videos"])
        if channel_count < min_channels or video_count < min_videos:
            continue
        support = _harvest_support(spec["query"], yield_state)
        if support["related_new_channels"] <= 0 and channel_count < min_channels + 2:
            continue
        score = (
            channel_count * 20
            + min(video_count, 40)
            + math.log10(max(int(spec["views"]), 1)) * 3
            + min(float(support["best_average_yield"]) * 8, 40)
            + min(int(support["related_new_channels"]) * 0.1, 30)
        )
        ranked.append({
            "query": spec["query"],
            "topic": spec["topic"],
            "score": round(score, 3),
            "evidence_channels": channel_count,
            "evidence_videos": video_count,
            "evidence_views": int(spec["views"]),
            **support,
        })
    ranked.sort(
        key=lambda item: (
            item["score"],
            item["evidence_channels"],
            item["evidence_views"],
            item["query"],
        ),
        reverse=True,
    )
    return ranked


def _evidence_rows(store: YouTubeStore, limit: int) -> list[tuple[str, str, int, str]]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                v.title,
                v.channel_id,
                v.views,
                COALESCE(c.topic, '')
            FROM videos v
            JOIN channels c ON c.channel_id=v.channel_id
            JOIN channel_classifications cc ON cc.channel_id=c.channel_id
            LEFT JOIN channel_exclusions ce ON ce.channel_id=c.channel_id
            WHERE ce.channel_id IS NULL
              AND cc.likely_faceless=1
              AND cc.confidence>=0.55
              AND c.subscribers BETWEEN 1000 AND 500000
              AND c.video_count BETWEEN 8 AND 500
              AND v.views>=25000
              AND v.title!=''
              AND (v.duration_seconds=0 OR v.duration_seconds>180)
              AND COALESCE(c.topic, '') NOT IN (
                  'Celebrity & Entertainment', 'Sports', 'Gaming & Anime'
              )
            ORDER BY v.views DESC
            LIMIT ?
            """,
            (max(1000, int(limit)),),
        ).fetchall()
    return [
        (str(row[0]), str(row[1]), int(row[2] or 0), str(row[3] or ""))
        for row in rows
    ]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".radar-topup.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def maybe_top_up_queries(
    config,
    store: YouTubeStore,
    *,
    config_path: str | os.PathLike,
    yield_path: str | os.PathLike,
    now: float | None = None,
    evidence_rows: list[tuple[str, str, int, str]] | None = None,
) -> dict:
    """Add at most two evidence-backed base queries after full first-pass coverage."""
    now_ts = time.time() if now is None else float(now)
    path = Path(config_path)
    yield_state = _read_json(Path(yield_path))
    raw = _read_json(path)
    watchlist = [
        str(query).strip()
        for query in raw.get("watchlist", config.watchlist)
        if str(query).strip()
    ]
    coverage = query_coverage(
        watchlist,
        yield_state,
        expand=bool(config.harvest_expand),
    )
    base = {
        "added": [],
        "candidates": [],
        "coverage": round(coverage["coverage"], 6),
        "remaining": coverage["remaining"],
    }
    if not bool(getattr(config, "query_topup_enabled", False)):
        return {**base, "reason": "disabled"}
    if coverage["coverage"] < float(config.query_topup_min_coverage):
        return {**base, "reason": "coverage_incomplete"}
    if len(watchlist) >= int(config.query_topup_max_total):
        return {**base, "reason": "watchlist_at_capacity"}

    policy = raw.get(QUERY_POLICY_KEY)
    policy = dict(policy) if isinstance(policy, dict) else {}
    last_added = float(policy.get("last_added_ts", 0) or 0)
    if now_ts - last_added < float(config.query_topup_cooldown_hours) * 3600:
        return {**base, "reason": "cooldown_active"}
    last_check = float(policy.get("last_radar_check_ts", 0) or 0)
    if now_ts - last_check < float(config.query_topup_check_interval_minutes) * 60:
        return {**base, "reason": "check_interval_active"}

    rows = evidence_rows
    if rows is None:
        rows = _evidence_rows(store, int(config.query_topup_evidence_limit))
    ranked = rank_candidates(
        list(rows),
        watchlist,
        yield_state,
        policy,
        min_channels=int(config.query_topup_min_channels),
        min_videos=int(config.query_topup_min_videos),
        max_per_topic=int(config.query_topup_max_per_topic),
    )
    selected = []
    used_topics = set()
    for candidate in ranked:
        if candidate["topic"] in used_topics:
            continue
        selected.append(candidate)
        used_topics.add(candidate["topic"])
        if len(selected) >= int(config.query_topup_max_per_run):
            break

    with _WRITE_LOCK, watchlist_config_lock(path):
        current = _read_json(path)
        current_watchlist = [
            str(query).strip()
            for query in current.get("watchlist", watchlist)
            if str(query).strip()
        ]
        current_policy = current.get(QUERY_POLICY_KEY)
        current_policy = (
            dict(current_policy) if isinstance(current_policy, dict) else {}
        )
        current_coverage = query_coverage(
            current_watchlist,
            yield_state,
            expand=bool(config.harvest_expand),
        )
        current_base = {
            **base,
            "coverage": round(current_coverage["coverage"], 6),
            "remaining": current_coverage["remaining"],
        }
        if (
            current_coverage["coverage"]
            < float(config.query_topup_min_coverage)
        ):
            return {**current_base, "reason": "coverage_incomplete"}
        if len(current_watchlist) >= min(
            int(config.query_topup_max_total),
            QUERY_MAX_TOTAL,
        ):
            return {**current_base, "reason": "watchlist_at_capacity"}
        current_last_added = float(current_policy.get("last_added_ts", 0) or 0)
        if now_ts - current_last_added < float(config.query_topup_cooldown_hours) * 3600:
            return {**current_base, "reason": "cooldown_active"}
        current_last_check = float(
            current_policy.get("last_radar_check_ts", 0) or 0
        )
        if (
            now_ts - current_last_check
            < float(config.query_topup_check_interval_minutes) * 60
        ):
            return {**current_base, "reason": "check_interval_active"}

        accepted = []
        for candidate in selected:
            if len(current_watchlist) + len(accepted) >= min(
                int(config.query_topup_max_total),
                QUERY_MAX_TOTAL,
            ):
                break
            if _covered_by_watchlist(candidate["query"], current_watchlist + accepted):
                continue
            accepted.append(candidate["query"])

        stamp = dt.datetime.fromtimestamp(
            now_ts,
            tz=dt.timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        post_watchlist = current_watchlist + accepted
        post_coverage = query_coverage(
            post_watchlist,
            yield_state,
            expand=bool(config.harvest_expand),
        )
        current_policy.update({
            "last_radar_check_ts": now_ts,
            "last_radar_check_at": stamp,
            "last_radar_result": {
                "added": list(accepted),
                "eligible_candidates": len(ranked),
                "coverage": round(current_coverage["coverage"], 6),
                "searches_remaining_after": post_coverage["remaining"],
            },
        })
        if accepted:
            evidence_by_query = {
                candidate["query"]: candidate for candidate in selected
            }
            recent = current_policy.get("recent_additions")
            recent = list(recent) if isinstance(recent, list) else []
            topic_counts = Counter({
                str(topic): int(count or 0)
                for topic, count in (
                    current_policy.get("radar_topic_counts") or {}
                ).items()
            })
            additions = []
            for query in accepted:
                evidence = evidence_by_query[query]
                topic_counts[evidence["topic"]] += 1
                additions.append({
                    "query": query,
                    "family": query_family(query),
                    "topic": evidence["topic"],
                    "at": stamp,
                    "source": "radar",
                    "evidence_channels": evidence["evidence_channels"],
                    "evidence_videos": evidence["evidence_videos"],
                    "evidence_views": evidence["evidence_views"],
                    "related_new_channels": evidence["related_new_channels"],
                })
            current_policy.update({
                "last_added_ts": now_ts,
                "last_added_at": stamp,
                "last_source": "radar",
                "radar_topic_counts": dict(topic_counts),
                "recent_additions": additions + recent[:48],
            })
            current["watchlist"] = post_watchlist
        current[QUERY_POLICY_KEY] = current_policy
        _atomic_write_json(path, current)

    return {
        **current_base,
        "reason": "added" if accepted else "no_evidence_backed_candidates",
        "added": accepted,
        "candidates": ranked[:10],
        "coverage": round(post_coverage["coverage"], 6),
        "remaining": post_coverage["remaining"],
        "total": len(post_watchlist),
    }
