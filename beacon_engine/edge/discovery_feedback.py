"""Bounded scheduler evidence from signed acknowledgements of committed imports.

The collector never opens the canonical index. Only acknowledgements for bundles
it recorded can teach it which searches added globally new channels. Old bundles,
failed searches and missing acknowledgements are not zero-yield observations.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from beacon_engine.edge.discovery_yield import PREFIXES, query_identity
from beacon_engine.edge.ssh_transport import verified_ack_payload

MAX_PENDING = 256
MAX_SAMPLES = 12
MIN_SAMPLES = 3
_BUNDLE_ID = re.compile(r"^[0-9a-f]{64}$")


def context_key(topic: str, sort: str) -> str:
    return json.dumps([topic, sort], ensure_ascii=True, separators=(",", ":"))


def remember_bundle(job: dict, bundle, *, run_number: int) -> None:
    """Record expected identities before import; cap work even during an outage."""
    rows = bundle.records.get("query_runs", [])
    if not rows or any("channel_ids" not in row for row in rows):
        return
    expected = {}
    for row in rows:
        identity = query_identity(row)
        entry = expected.setdefault(identity, {"ok": 0, "error": 0})
        entry["ok" if row.get("status") == "ok" else "error"] += 1
    pending = job.setdefault("pending_channel_feedback", {})
    pending[bundle.bundle_id] = {"revision": job["revision"],
                               "run": run_number, "expected": expected}
    while len(pending) > MAX_PENDING:
        oldest = min(pending, key=lambda bundle_id: pending[bundle_id]["run"])
        del pending[oldest]
        job["feedback_pending_dropped"] = int(job.get("feedback_pending_dropped", 0)) + 1


def _measured_samples(counts: dict, expected: dict) -> list[tuple[str, str, int, int]]:
    if any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError("invalid feedback counts")
    parsed: dict[str, dict[str, int]] = {}
    for name, value in counts.items():
        for field, prefix in PREFIXES.items():
            if name.startswith(prefix):
                identity = name[len(prefix):]
                if identity not in expected:
                    raise ValueError("unexpected feedback query")
                parsed.setdefault(identity, {})[field] = value
    samples = []
    attributed = 0
    for identity, anticipated in expected.items():
        labels = json.loads(identity)
        if not isinstance(labels, list) or len(labels) != 4 or any(not isinstance(v, str) for v in labels):
            raise ValueError("invalid feedback identity")
        item = parsed.get(identity, {})
        ok = item.get("successful_searches", 0)
        if (ok != anticipated["ok"]
                or item.get("failed_searches", 0) != anticipated["error"]):
            raise ValueError("feedback search totals do not match bundle")
        new = item.get("new_channels", 0)
        if (new + item.get("existing_channel_matches", 0) > ok * 200
                or (ok and ("new_channels" not in item
                                      or "existing_channel_matches" not in item))):
            raise ValueError("invalid feedback channel totals")
        attributed += new
        if ok:
            samples.append((labels[0], context_key(labels[1], labels[3]), ok, new))
    if (attributed != counts.get("channels_new_attributed")
            or attributed + counts.get("channels_new_unattributed", 0) != counts.get("channels_new")):
        raise ValueError("feedback attribution totals do not match")
    return samples


def consume_feedback(job: dict, *, ack_dir: Path, node_id: str,
                     signing_key: str | bytes) -> dict[str, int]:
    """Apply known ACKs once, with ordered windows despite late delivery/replays."""
    result = {"applied": 0, "unmeasured": 0, "rejected": 0, "waiting": 0}
    pending = job.setdefault("pending_channel_feedback", {})
    for bundle_id, entry in list(pending.items())[:MAX_PENDING]:
        if not _BUNDLE_ID.fullmatch(bundle_id) or entry.get("revision") != job.get("revision"):
            del pending[bundle_id]
            continue
        path = ack_dir / (bundle_id + ".ack.json")
        if not path.is_file():
            result["waiting"] += 1
            continue
        try:
            payload = verified_ack_payload(path, signing_key=signing_key)
            if payload.get("node_id") != node_id or payload.get("status") not in {"imported", "duplicate"}:
                raise ValueError("feedback ownership or status mismatch")
            counts = payload.get("counts")
            if not isinstance(counts, dict):
                raise ValueError("missing feedback counts")
            if counts.get("query_yield_version") != 1 or counts.get("query_yield_complete") != 1:
                result["unmeasured"] += 1
                del pending[bundle_id]
                continue
            samples = _measured_samples(counts, entry["expected"])
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            result["rejected"] += 1
            continue  # A corrected ACK can be retried; bad evidence cannot stop collection.
        for query, context, ok, new in samples:
            query_state = job.get("queries", {}).get(query)
            if query_state is None:
                continue
            history = query_state.setdefault("channel_yield", {}).setdefault(context, [])
            history.append({"run": entry["run"], "ok": ok, "new": new})
            history.sort(key=lambda sample: sample["run"])
            del history[:-MAX_SAMPLES]
        del pending[bundle_id]
        result["applied"] += 1
    job["channel_feedback"] = result
    return result


def measured_yield(state: dict, *, topic: str, sort: str) -> tuple[int, int]:
    history = (state.get("channel_yield") or {}).get(context_key(topic, sort), [])
    return sum(s["ok"] for s in history), sum(s["new"] for s in history)


def allocate_queries(indexed: list, states: dict, *, batch_limit: int,
                     tick: int) -> tuple[list, dict[str, Any]] | None:
    """60% proven discovery / 20% exploration / 20% oldest-query refresh.

    Small batches rotate the same five-slot allocation across ticks. Discovery
    uses recent measured additions and a three-success minimum. Reserve slots
    remain age-ordered, so low-yield searches can recover and views keep updating.
    """
    ordered = sorted(indexed, key=lambda item: (
        float(states[item[1].query].get("last_completed_ts") or 0), item[0]))
    evidence = {}
    for index, query in ordered:
        sort = query.sort or "newest"
        evidence[index] = measured_yield(states[query.query], topic=query.topic, sort=sort)
    if not any(ok >= MIN_SAMPLES for ok, _ in evidence.values()):
        return None  # Historical/no-ACK states retain the existing scheduler.
    productive = [item for item in ordered
                  if evidence[item[0]][0] >= MIN_SAMPLES and evidence[item[0]][1] > 0]
    productive.sort(key=lambda item: (
        -evidence[item[0]][1] / evidence[item[0]][0],
        float(states[item[1].query].get("last_completed_ts") or 0), item[0]))
    productive_ids = {item[0] for item in productive}
    exploring = [item for item in ordered if item[0] not in productive_ids]
    pools = {"productive": productive, "exploration": exploring, "refresh": ordered}
    # Reserve refresh/exploration first, then fill productive slots; fallback work
    # is explicitly counted separately rather than claiming it was productive.
    pattern = ("productive", "productive", "productive", "exploration", "refresh")
    slots = [pattern[((tick - 1) * batch_limit + n) % 5] for n in range(batch_limit)]
    selected, used = [], set()
    counts = dict.fromkeys((*pools, "fallback"), 0)
    for role in sorted(slots, key=lambda role: {"refresh": 0, "exploration": 1, "productive": 2}[role]):
        item = next((item for item in pools[role] if item[0] not in used), None)
        actual = role
        if item is None:
            item = next((item for item in ordered if item[0] not in used), None)
            actual = "fallback"
        if item is None:
            break
        selected.append(item)
        used.add(item[0])
        counts[actual] += 1
    return selected, {"policy": "committed-channel-yield-v1", **counts,
                      "minimum_successes": MIN_SAMPLES, "sample_window": MAX_SAMPLES}
