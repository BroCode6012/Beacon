"""Group the many raw discovery-query categories into clean top-level buckets.

Channel categories are stored as the search query that surfaced them (e.g.
"finance scams explained", "ancient rome documentary mystery"). There are
hundreds of those, many near-duplicates. For NexLev-style filtering we collapse
them into a small set of top-level categories the user can actually browse.
"""

from __future__ import annotations

# Order matters: first keyword hit wins, so put more specific buckets earlier.
CATEGORY_GROUPS: list[tuple[str, list[str]]] = [
    ("Money & Finance", [
        "finance", "stock market", "scam", "fraud", "investing", "economy crash",
        "side hustle", "online business", "youtube automation", "faceless channel",
        "real estate bubble", "money",
    ]),
    ("Business & Brands", [
        "business", "startup", "company rise", "company", "billionaire", "brand",
        "consumer product", "restaurant", "luxury", "corporate", "product recall",
        "dangerous product", "fashion brand", "beauty product",
    ]),
    ("True Crime", [
        "true crime", "criminal", "heist", "cult", "prison", "murder",
        "serial killer", "fraud case",
    ]),
    ("Psychology & Mind", [
        "psychology", "human behavior", "philosophy", "stoicism", "mindset",
    ]),
    ("Horror & Mystery", [
        "scary", "paranormal", "unsolved", "creepy", "urban legend",
        "internet mysteries", "horror", "ghost", "reddit stories",
    ]),
    ("History & War", [
        "history", "ancient", "civilization", "war ", "military", "royal",
        "empire", "lost civilization", "historical disaster",
    ]),
    ("Geopolitics", [
        "geopolitics", "country collapse", "border conflict", "world economy",
        "political scandal", "why countries are poor",
    ]),
    ("Science & Space", [
        "science", "physics", "biology", "space", "astronomy", "nasa", "cosmic",
        "exoplanet", "chemistry", "quantum",
    ]),
    ("Engineering & Disasters", [
        "engineering", "construction", "bridge collapse", "dam failure",
        "megaproject",
    ]),
    ("Aviation", ["aviation", "plane crash", "airline", "aircraft"]),
    ("Ocean & Deep Sea", [
        "ocean", "deep sea", "shipwreck", "marine biology", "underwater",
    ]),
    ("Tech & Gaming", [
        "technology company", "cybersecurity", "hacking", "internet history",
        "social media", "ai documentary", "robotics", "video game", "gaming",
    ]),
    ("Entertainment & Sports", [
        "celebrity", "hollywood", "movie industry", "music industry", "athlete",
        "sports", "mma", "bodybuilding", "football", "rise and fall",
    ]),
    ("Health & Food", [
        "health myth", "medical", "ultraprocessed", "food brand", "food",
        "nutrition", "disease",
    ]),
    ("Automotive", ["car compan", "automotive", "supercar"]),
    ("Survival & Nature", [
        "survival", "mountaineering", "expedition", "natural disaster", "volcano",
        "earthquake", "weather disaster", "animal attack", "wildlife",
    ]),
    ("Travel & Places", [
        "travel", "tourist", "abandoned places", "architecture", "real estate",
    ]),
]

_FALLBACK = "Other"


def category_group(raw_category: str | None) -> str:
    """Map a raw discovery-query category to its top-level bucket."""
    text = (raw_category or "").lower()
    if not text.strip():
        return _FALLBACK
    for group, keywords in CATEGORY_GROUPS:
        if any(keyword in text for keyword in keywords):
            return group
    return _FALLBACK


def group_names() -> list[str]:
    """All top-level group names, in display order, plus the fallback."""
    return [name for name, _ in CATEGORY_GROUPS] + [_FALLBACK]


def group_raw_categories(raw_categories: list[str]) -> dict[str, list[str]]:
    """Bucket a list of raw categories by their top-level group."""
    out: dict[str, list[str]] = {}
    for raw in raw_categories:
        out.setdefault(category_group(raw), []).append(raw)
    return out
