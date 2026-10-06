"""Give the chat agent EYES: look at a channel's real thumbnails on demand.

The pipelines already use vision (niche report, competitor format-sorting), but
the conversational agent's tools were text-only — it could quote a channel's
stats yet had never SEEN it. This tool downloads the channel's top thumbnails
and runs one vision call, returning a compact visual read the agent can reason
with: faceless or on-camera, thumbnail style, format, quality, beatability.
"""

from __future__ import annotations

from beacon_engine.config import Tier
from beacon_engine.research.models import VideoStat
from beacon_engine.research.thumbnails import download_thumbnails

_SYSTEM = """You are a faceless-YouTube operator inspecting ONE channel by its real
thumbnails (each labeled with title + views). Report, tightly:
1) Production format: UNKNOWN unless actual video evidence is supplied. A recurring face
   can establish a thumbnail face-brand, but cannot prove on-camera footage or AI narration.
   Note whether the SAME creator/host recurs across the
   thumbnails as the channel's face-brand. A stock photo, a PUBLIC FIGURE (e.g. a politician),
   or a shock/reaction face used for urgency does NOT make a channel on-camera — faceless
   channels use faces in thumbnails all the time. Only call it on-camera if a recurring HOST
   is clearly the brand.
2) Thumbnail style: composition, text use, colors, polish (pro or beginner?).
3) Format guess from titles+images (listicle/exposé/documentary/ranking...).
4) What wins for them (tie to view counts) and what to steal.
5) Beatability for a new faceless channel: giant to ignore / beatable rival /
   weak player. Plain text, under 200 words."""


_REVIEW_SYSTEM = """You are a YouTube thumbnail director reviewing a FACELESS channel's
freshly GENERATED thumbnail (IMAGE 1) against proven winning references (the other images —
real competitor thumbnails that already win in this niche). Actually LOOK at image 1 and judge
it honestly, both on its own and versus the references.

Judge mobile readability, topic accuracy, originality and distracting visual defects.
Reference views do not prove CTR or that a particular design caused success. Do not default
to approval. Copying a reference creator's face/branding, a false price or result, or a
misleading absolute text claim is a material problem even when the image is punchy.

Report tightly (under 160 words, plain text):
1) VERDICT: SHIP IT  /  one real fix  /  redo — one line why. Default to SHIP IT whenever it
   reads small, matches the actual video promise, and uses an original subject.
2) WHY it works (2-4 specifics).
3) REAL FIXES — ONLY genuine problems that would lower the click. 0-3 items max, each a concrete
   prompt tweak. If it's ship-ready, write exactly "None — ship it." NEVER pad this list with
   nitpicks or "nice-to-haves".
NOTE: "faceless" is about the VIDEO, not the thumbnail — faces (stock, public figures, reaction)
are a normal urgency device; never flag a face for removal if the winners use faces.
Judge only what you can see; never invent details."""


def review_generated_thumbnail(gen, refs, *, title: str = "",
                               ref_label: str = "winning references", llm=None) -> str:
    """Give the agent EYES on its OWN output. gen = (bytes, mime) of the just-generated
    thumbnail; refs = list[(bytes, mime)] of reference thumbnails to compare against.
    Returns a plain-text critique with concrete regenerate-with fixes."""
    if not gen:
        return "No generated image to look at."
    if llm is None:
        from beacon_engine.llm import LLM
        llm = LLM()
    images = [gen] + list(refs or [])
    manifest = [f'IMAGE 1: the thumbnail I just GENERATED for "{title}".']
    for i in range(len(refs or [])):
        manifest.append(f"IMAGE {i + 2}: {ref_label} #{i + 1} (a proven winner to compare against).")
    return llm.complete(
        task="vision",
        tier=Tier.SONNET, instructions=_REVIEW_SYSTEM,
        prompt="\n".join(manifest) + "\n\nReview IMAGE 1 now.",
        images=images, thinking=False, effort="medium", max_tokens=1100).text.strip()


_REFBOARD_SYSTEM = """You are shown a niche's REAL winning YouTube thumbnails — the reference
board a FACELESS creator adapts new thumbnails from. Look at each image.

For EACH image, one tight line: its subject/focal point, whether it uses a FACE (faces are a
normal URGENCY device here — just note it, never judge it as "not faceless"), the on-image text
hook, the palette/contrast, and the emotional trigger (shock/scale/danger/betrayal/curiosity).

Then finish with ONE clear recommendation: which image is the STRONGEST base to ADAPT for a new
video in this niche, and why. Number your notes to match the image order. Plain text, tight."""


_STYLE_BRIEF_SYSTEM = """You are shown a niche's winning YouTube thumbnails. In UNDER 60 words,
extract the shared VISUAL FORMULA to COPY, as a tight directive brief fed straight into an image
generator to match the look:
- PALETTE: name the exact dominant colors.
- TEXT: weight, case, color, and where the banner/badge sits.
- LAYOUT: the composition (split, hero object, before/after...).
- FACES: whether winners use a face for urgency (a face is a normal urgency device here).
No preamble, no headings — just the brief."""


def reference_style_brief(images, *, llm=None) -> str:
    """A tight copy-this palette/style brief extracted from the reference thumbnails, so
    'make a thumbnail' automatically matches the niche's winning colors & look."""
    if not images:
        return ""
    if llm is None:
        from beacon_engine.llm import LLM
        llm = LLM()
    return llm.complete(
        task="vision",
        tier=Tier.SONNET, instructions=_STYLE_BRIEF_SYSTEM,
        prompt="Extract the copy-this visual formula from these winning thumbnails.",
        images=list(images), thinking=False, effort="low", max_tokens=220).text.strip()


def describe_reference_thumbnails(images, *, llm=None) -> str:
    """Vision-describe the WHOLE reference board so the agent can SEE its options and pick the
    strongest base to adapt. images = list[(bytes, mime)]."""
    if not images:
        return "No reference thumbnails to look at."
    if llm is None:
        from beacon_engine.llm import LLM
        llm = LLM()
    manifest = "\n".join(f"Image {i + 1}" for i in range(len(images)))
    return llm.complete(
        task="vision",
        tier=Tier.SONNET, instructions=_REFBOARD_SYSTEM,
        prompt=manifest + "\n\nDescribe each thumbnail, then recommend the best base to adapt.",
        images=list(images), thinking=False, effort="medium", max_tokens=1300).text.strip()


def inspect_channel_visuals(store, channel: str, *, llm=None, limit: int = 12) -> dict:
    """Vision-read one channel's indexed top videos. Returns the analysis text."""
    with store.connect() as conn:
        ch = conn.execute(
            "SELECT channel_id, title FROM channels WHERE channel_id=? "
            "OR lower(handle)=lower(?) OR lower(title)=lower(?) LIMIT 1",
            (channel, channel.lstrip("@"), channel)).fetchone()
        cid = ch["channel_id"] if ch else channel
        rows = conn.execute(
            "SELECT video_id, title, views, channel FROM videos "
            "WHERE channel_id=? ORDER BY views DESC LIMIT ?", (cid, int(limit))).fetchall()
    if not rows:
        return {"error": f"No indexed videos for '{channel}'. Crawl it first "
                         "(crawl_niche or deep_sweep), then inspect again."}
    stats = [VideoStat(video_id=r["video_id"], title=r["title"] or "",
                       channel=r["channel"] or "", views=int(r["views"] or 0),
                       published_at="", url="") for r in rows]
    items = download_thumbnails(stats, limit=limit)
    if not items:
        return {"error": "Thumbnails could not be downloaded (network)."}
    if llm is None:
        from beacon_engine.llm import LLM
        llm = LLM()
    manifest = "\n".join(f"Image {i+1}: \"{v.title}\" — {v.views:,} views"
                         for i, (v, _, _) in enumerate(items))
    text = llm.complete(
        task="vision",
        tier=Tier.SONNET, instructions=_SYSTEM,
        prompt=f"CHANNEL: {ch['title'] if ch else channel}\n\n{manifest}\n\nInspect.",
        images=[(d, m) for _, d, m in items], thinking=False, effort="medium",
        max_tokens=1200).text.strip()
    return {"channel_id": cid, "thumbnails_seen": len(items), "visual_analysis": text}
