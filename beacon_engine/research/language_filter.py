"""Detect a channel's content language by SCRIPT and flag off-target ones.

The user targets English/French/Spanish/German — all Latin-script. The junk that
slips in (Bangla, Hindi, Arabic, Chinese, etc.) is non-Latin, which is decidable
from Unicode ranges alone — no ML, no dependencies, near-perfect on the scripts
that matter. A channel's language is judged from its VIDEO TITLES (the real
content language), not its name: many faceless channels have English names but
publish in another language (e.g. "Horror Glimpse" → Bangla content).

Latin-vs-Latin (English vs Portuguese vs Turkish) is NOT decided here — that
needs a language model and is unreliable on short titles. Script filtering
removes the overwhelming majority of off-target channels; the rest is a later,
opt-in refinement.
"""

from __future__ import annotations

# Unicode blocks for the scripts we see. Latin = allowed (covers en/fr/es/de).
_SCRIPTS = [
    ("latin", 0x0041, 0x024F),      # basic + extended Latin (incl. accents)
    ("latin", 0x1E00, 0x1EFF),      # Latin extended additional (Vietnamese etc.)
    ("greek", 0x0370, 0x03FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("hebrew", 0x0590, 0x05FF),
    ("arabic", 0x0600, 0x06FF),
    ("arabic", 0x0750, 0x077F),
    ("devanagari", 0x0900, 0x097F),  # Hindi/Marathi/Nepali
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("thai", 0x0E00, 0x0E7F),
    ("cjk", 0x3040, 0x30FF),          # Japanese kana
    ("cjk", 0x3400, 0x4DBF),          # CJK ext A
    ("cjk", 0x4E00, 0x9FFF),          # CJK unified (Chinese/Japanese)
    ("cjk", 0xAC00, 0xD7AF),          # Hangul (Korean)
]

# Which detected scripts are kept. Everything else is off-target.
ALLOWED_SCRIPTS = {"latin"}
# A channel is off-target when this share of its alphabetic characters is a
# non-allowed script (allows the odd foreign word in an English title).
NON_ALLOWED_THRESHOLD = 0.35


def _script_of(ch: str) -> str | None:
    o = ord(ch)
    for name, lo, hi in _SCRIPTS:
        if lo <= o <= hi:
            return name
    return None  # digits, punctuation, emoji, spaces -> ignored


def script_profile(text: str) -> dict[str, int]:
    """Count alphabetic characters per script (ignores digits/punct/emoji)."""
    counts: dict[str, int] = {}
    for ch in text or "":
        s = _script_of(ch)
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


def classify_language(text: str) -> tuple[bool, str, float]:
    """Return (allowed, dominant_non_allowed_script, non_allowed_fraction).

    allowed=True when the text is empty/short-non-alpha or predominantly Latin.
    """
    counts = script_profile(text)
    total = sum(counts.values())
    if total < 8:
        return True, "", 0.0   # too little signal — don't reject
    non_allowed = {k: v for k, v in counts.items() if k not in ALLOWED_SCRIPTS}
    non_allowed_total = sum(non_allowed.values())
    frac = non_allowed_total / total
    if frac >= NON_ALLOWED_THRESHOLD:
        top = max(non_allowed, key=non_allowed.get)
        return False, top, round(frac, 2)
    return True, "", round(frac, 2)


# --- Latin-language detection (English vs French vs Spanish vs German) --------
#
# The script filter above only decides Latin-vs-non-Latin. To let the user
# ISOLATE one Latin language (English by default) we need to tell those four
# apart. On short titles a full language model is overkill and flaky, so we
# score distinctive high-frequency function words ("the" → English, "le/les" →
# French, "el/los" → Spanish, "der/das" → German) plus language-only accents.
# Judged over a channel's whole title history it is reliable enough to isolate.

_LANG_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "the", "and", "of", "to", "in", "is", "for", "you", "your", "with",
        "this", "that", "was", "were", "are", "how", "why", "what", "when",
        "who", "best", "top", "new", "world", "history", "explained", "vs",
        "a", "an", "on", "it", "his", "her", "their", "did", "do", "we",
    }),
    "fr": frozenset({
        "le", "les", "la", "un", "une", "des", "du", "et", "est", "pour",
        "dans", "qui", "que", "avec", "sur", "pas", "plus", "comment",
        "pourquoi", "ce", "cette", "ses", "mais", "ou", "où", "être", "fait",
        "tout", "sont", "aux", "leur", "nous", "vous", "cet", "français",
    }),
    "es": frozenset({
        "el", "los", "las", "una", "que", "por", "con", "para", "como",
        "más", "qué", "cómo", "se", "su", "sus", "pero", "este", "esta",
        "esto", "año", "día", "hay", "muy", "español", "porque", "cuando",
        "quien", "todo", "nos", "les", "está", "son",
    }),
    "de": frozenset({
        "der", "die", "das", "und", "ist", "ein", "eine", "den", "dem",
        "des", "nicht", "mit", "für", "auf", "wie", "warum", "sich", "auch",
        "aber", "oder", "über", "deutsch", "ich", "war", "sind", "haben",
        "wird", "durch", "einen", "einem", "sein", "nach", "bei", "wenn",
    }),
}
# Accents that occur in only one of the four target languages -> strong signal.
_LANG_ACCENTS: dict[str, frozenset[str]] = {
    "es": frozenset("ñ¿¡"),
    "de": frozenset("äöüß"),
    "fr": frozenset("çàèùâêîôûë"),
}
_WORD_RE = None  # lazily compiled to keep import cheap


def detect_latin_language(text: str) -> str:
    """Return 'en'|'fr'|'es'|'de' for Latin text, or '' when unsure.

    Scores distinctive function words (and language-only accents), then picks the
    clear winner. Returns '' on a tie or when there is no signal, so callers can
    treat unknown text as "keep under the default (English) view"."""
    global _WORD_RE
    if _WORD_RE is None:
        import re
        _WORD_RE = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)
    lowered = (text or "").lower()
    if not lowered:
        return ""
    scores = {lang: 0 for lang in _LANG_WORDS}
    for token in _WORD_RE.findall(lowered):
        for lang, words in _LANG_WORDS.items():
            if token in words:
                scores[lang] += 1
    for lang, accents in _LANG_ACCENTS.items():
        if any(ch in accents for ch in lowered):
            scores[lang] += 2   # an accent unique to one language is worth ~2 words
    best = max(scores, key=scores.get)
    top = scores[best]
    if top == 0:
        return ""                                   # no evidence
    if sum(1 for v in scores.values() if v == top) > 1:
        return ""                                   # tie -> unsure
    return best


def classify_channel_languages(rows) -> dict[str, str]:
    """Map channel_id -> detected language from each channel's aggregated titles.

    `rows` is any iterable of dict-like videos with 'channel_id' and 'title'.
    Aggregating a channel's titles gives far more signal than a single title,
    so the per-channel verdict is stable even when individual titles are terse."""
    blobs: dict[str, list[str]] = {}
    for row in rows:
        cid = (row.get("channel_id") if isinstance(row, dict)
               else getattr(row, "channel_id", "")) or ""
        title = (row.get("title") if isinstance(row, dict)
                 else getattr(row, "title", "")) or ""
        if not cid or not title:
            continue
        bucket = blobs.setdefault(cid, [])
        if len(bucket) < 40:            # cap: 40 titles is plenty of signal
            bucket.append(title)
    return {cid: detect_latin_language(" ".join(titles))
            for cid, titles in blobs.items()}


def find_wrong_language_channels(store, *, sample_titles: int = 25) -> list[dict]:
    """Channels whose aggregated video titles are dominantly non-Latin.

    Returns [{channel_id, title, script, fraction, titled_videos}] for review /
    exclusion. Only judges channels that have enough titled videos to be sure."""
    hits: list[dict] = []
    with store.connect() as conn:
        channels = conn.execute(
            "SELECT channel_id, title FROM channels").fetchall()
        for cid, ctitle in channels:
            rows = conn.execute(
                "SELECT title FROM videos WHERE channel_id=? "
                "AND COALESCE(title,'')!='' LIMIT ?", (cid, sample_titles)).fetchall()
            if len(rows) < 3:
                continue   # not enough content to judge language
            blob = " ".join(r[0] for r in rows)
            allowed, script, frac = classify_language(blob)
            if not allowed:
                hits.append({"channel_id": cid, "title": ctitle or "",
                             "script": script, "fraction": frac,
                             "titled_videos": len(rows)})
    hits.sort(key=lambda h: h["fraction"], reverse=True)
    return hits
