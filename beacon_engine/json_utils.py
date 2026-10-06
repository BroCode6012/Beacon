"""Robust JSON extraction from model output (shared by ideation + writing).

Models sometimes wrap JSON in prose or code fences even when told not to; adaptive
thinking also emits separate thinking blocks (we only ever parse the text blocks).
These helpers tolerate that and recover the intended JSON value.
"""

from __future__ import annotations

import json
import re


def extract_json(text: str):
    """Parse a JSON value from model output, tolerating prose/code fences."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced [...] or {...} span.
    for open_c, close_c in (("[", "]"), ("{", "}")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Could not parse JSON from model output:\n{text[:500]}")


def as_list(data) -> list:
    """Coerce a JSON value into a list (tolerating {'items': [...]} wrappers)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    return []
