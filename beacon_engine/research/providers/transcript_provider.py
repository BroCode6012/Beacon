"""Transcript provider around the existing youtube-transcript-api integration."""

from __future__ import annotations

from .base import ProviderTranscript, normalize_video_id
from beacon_engine.research.transcripts import fetch_transcript


class TranscriptProvider:
    name = "youtube-transcript-api"

    def transcript(
        self, url_or_id: str, *, languages: tuple[str, ...] = ("en",)
    ) -> ProviderTranscript | None:
        vid = normalize_video_id(url_or_id)
        if not vid:
            return None
        item = fetch_transcript(vid, languages=languages)
        if not item:
            return None
        return ProviderTranscript(
            video_id=vid,
            text=item.text,
            language=languages[0] if languages else "",
            source=self.name,
        )
