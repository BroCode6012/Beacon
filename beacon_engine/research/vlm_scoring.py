"""Faceless scoring of 'uncertain' channels using the app's own vision model.

The CPU face-check leaves some channels undecided; this looks at their thumbnails and
calls it. It used to mean renting a Lightning AI GPU, uploading a queue, running
Qwen2.5-VL on it and shutting the machine down. The Foundry deployment reads images
itself, so the whole remote-GPU dance is gone: the model Beacon already talks to for
everything else answers this too.

What that changes, practically:
  * No GPU to start, so there is no minimum batch to justify one. `min_batch` survives
    only so callers keep working; it defaults to 1.
  * Nothing can be left billing after a crash — there is no machine to leak.
  * It is sequential, one channel per call, so it is slower per channel than a batched
    GPU pass. `max_minutes` is a real wall-clock budget: when it runs out the run stops
    and returns what it scored, and the rest stay queued for next time. Verdicts are
    saved as they are produced, so an interrupted run keeps its work.

Whatever vision deployment is configured answers — this is deliberately not tied to one
model. It has run on Ornith 1.0 (~13s per channel, since deleted) and on
Qwen3-VL-30B-A3B-Instruct (~1-3s per channel).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import requests

from beacon_engine.config import MODELS, Tier
from beacon_engine.llm import LLM
from beacon_engine.logging_util import get_logger

log = get_logger()
Progress = Callable[[str], None]

# Enough frames to judge a channel's format without paying for a whole grid.
THUMBNAILS_PER_CHANNEL = 4
_VERDICTS = {"faceless", "on_camera", "unclear"}

_INSTRUCTIONS = (
    "You classify YouTube channels by production format from their thumbnails.\n"
    "Answer with EXACTLY ONE of these words and nothing else:\n"
    "  faceless   — the videos have no on-camera human host\n"
    "  on_camera  — a recurring human host presents to camera\n"
    "  unclear    — the thumbnails do not settle it\n"
    "A face in a thumbnail does NOT by itself mean on_camera: stock faces, reaction "
    "crops, celebrities and news subjects are common on faceless channels. Judge "
    "whether the CHANNEL appears to be presented by a person on camera."
)


@dataclass
class VLMResult:
    eligible: int = 0
    started_gpu: bool = False      # always False; kept so callers stay unchanged
    scored: int = 0
    imported: int = 0
    skipped_reason: str = ""


def _fetch(url: str, timeout: int = 20) -> tuple[bytes, str] | None:
    """Download one thumbnail as (bytes, media_type), or None if it will not load."""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception:  # noqa: BLE001 — a dead thumbnail is not a failed run
        return None
    media = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    if not media.startswith("image/"):
        media = "image/jpeg"
    return r.content, media


def _parse(text: str) -> str:
    """The model's word, or 'unclear' when it said something else."""
    word = (text or "").strip().strip(".").lower().split()
    for token in word:
        cleaned = token.strip("*`'\"").replace("-", "_")
        if cleaned in _VERDICTS:
            return cleaned
    return "unclear"


@dataclass
class VLMRunner:
    """Scores uncertain channels with the selected/default vision-capable engine."""

    llm: LLM | None = None
    tier: Tier = Tier.HAIKU        # a classification, not a judgment call
    thumbnails: int = THUMBNAILS_PER_CHANNEL

    def _client(self) -> LLM:
        if self.llm is None:
            self.llm = LLM()
        return self.llm

    def score_channel(self, row: dict) -> dict | None:
        """One channel -> a verdict dict for store.save_vlm_verdicts, or None."""
        urls = (row.get("thumbnails") or [])[: self.thumbnails]
        images = [img for img in (_fetch(u) for u in urls) if img]
        if not images:
            return None
        res = self._client().complete(
            task="vision",
            tier=self.tier,
            instructions=_INSTRUCTIONS,
            prompt=(f"Channel: {row.get('title') or '(untitled)'}\n"
                    f"{len(images)} thumbnails from this channel are attached. "
                    "Reply with one word."),
            images=images,
            max_tokens=MODELS[self.tier].min_output_tokens,
        )
        return {
            "channel_id": row.get("channel_id"),
            "verdict": _parse(res.text),
            "model": getattr(res, "model", "") or MODELS[self.tier].model_id,
            "raw": (res.text or "")[:500],
        }

    def score_uncertain(
        self,
        store,
        *,
        min_batch: int = 1,
        limit: int | None = None,
        dry_run: bool = False,
        max_minutes: int = 45,
        on_step: Progress = lambda _m: None,
    ) -> VLMResult:
        rows = store.export_uncertain_for_vlm(limit=limit,
                                              max_thumbnails=self.thumbnails)
        result = VLMResult(eligible=len(rows))
        if len(rows) < min_batch:
            result.skipped_reason = (
                f"only {len(rows)} uncertain channels (< min_batch {min_batch})")
            on_step(result.skipped_reason)
            return result
        if dry_run:
            result.skipped_reason = "dry-run: no calls made"
            on_step(f"{result.skipped_reason} (would score {len(rows):,} channels)")
            return result

        deadline = time.time() + max_minutes * 60
        pending: list[dict] = []
        for i, row in enumerate(rows, 1):
            if time.time() >= deadline:
                result.skipped_reason = (
                    f"stopped at the {max_minutes}-minute budget after {i - 1:,} of "
                    f"{len(rows):,}; the rest stay queued")
                on_step(result.skipped_reason)
                break
            try:
                verdict = self.score_channel(row)
            except Exception as exc:  # noqa: BLE001 — one bad channel is not a run
                log.warning("vlm: %s failed: %s", row.get("channel_id"), str(exc)[:120])
                continue
            if not verdict:
                continue
            pending.append(verdict)
            # Flush periodically so an interrupted run keeps the work it paid for.
            if len(pending) >= 25:
                result.imported += store.save_vlm_verdicts(pending)
                pending = []
            if i % 25 == 0:
                on_step(f"VLM scored {i:,}/{len(rows):,}")
        if pending:
            result.imported += store.save_vlm_verdicts(pending)
        result.scored = result.imported
        return result
