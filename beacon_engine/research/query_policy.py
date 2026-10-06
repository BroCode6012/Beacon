"""Shared deterministic policy helpers for radar/watchlist query additions."""

from __future__ import annotations

import os
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path

QUERY_MAX_TOTAL = 600
QUERY_AUTONOMOUS_MAX_PER_CALL = 2
QUERY_AGENT_MAX_PER_CALL = 10
QUERY_COOLDOWN_SECONDS = 12 * 60 * 60
QUERY_MIN_COVERAGE = 0.70
QUERY_NEAR_DUPLICATE_JACCARD = 0.80
QUERY_POLICY_KEY = "signal_query_policy"

EDGE_MODIFIERS = [
    ("the", "truth", "about"),
    ("rise", "and", "fall"),
    ("case", "study"),
    ("documentary",),
    ("documentaries",),
    ("explained",),
    ("mystery",),
    ("mysteries",),
    ("facts",),
    ("story",),
    ("stories",),
    ("history",),
    ("exposed",),
    ("faceless",),
    ("youtube",),
    ("channel",),
    ("channels",),
]


def query_tokens(query: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(query or "")).casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return [token for token in text.split() if token]


def normalize_query(query: str) -> str:
    return " ".join(query_tokens(query))


def query_family(query: str) -> str:
    """Canonical family, ignoring expansion modifiers and year suffixes."""
    tokens = [
        token
        for token in query_tokens(query)
        if not re.fullmatch(r"20\d{2}", token)
    ]
    changed = True
    while changed:
        changed = False
        for phrase in EDGE_MODIFIERS:
            size = len(phrase)
            if len(tokens) - size >= 2 and tuple(tokens[:size]) == phrase:
                tokens = tokens[size:]
                changed = True
                break
            if len(tokens) - size >= 2 and tuple(tokens[-size:]) == phrase:
                tokens = tokens[:-size]
                changed = True
                break
    return " ".join(tokens)


def token_jaccard(left: str, right: str) -> float:
    a, b = set(left.split()), set(right.split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def query_attempts(raw: dict | None) -> int:
    """First-pass attempts, compatible with yield files created before this field."""
    state = raw if isinstance(raw, dict) else {}
    return max(
        int(state.get("attempts", 0) or 0),
        int(state.get("searches", 0) or 0),
    )


@contextmanager
def watchlist_config_lock(path: str | os.PathLike):
    """Cross-process lock for watchlist read-modify-write operations."""
    config_path = Path(path)
    lock_path = config_path.with_name(config_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
