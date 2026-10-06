"""Assign every channel ONE clean TOPIC niche (History, Space, True Crime, ...).

The scraper stores the discovering search query in ``channels.category`` (100%
coverage but 43k+ messy variants like 'affiliate marketing exposed mystery'), plus
each channel's title + description. This maps that text into ~20 human-readable
topics via ordered keyword rules — FIRST match wins, so more specific topics are
listed first. Generic harvest-suffix words ('explained' / 'mystery' / 'documentary'
/ 'facts' / 'exposed' / 'case study' / 'rise and fall') are deliberately NOT signals.
Channels that match nothing land in 'Other'.

Backfilled into the ``channels.topic`` column (see store schema); new channels are
re-classified each radar cycle. ~14% land in 'Other' (genuinely generic/mixed).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from beacon_engine.research.store import YouTubeStore

Progress = Callable[[str], None]
CLASSIFIER_VERSION = "topic-rules-v1"

# Ordered, specific-first. (topic, [keywords]) — a keyword matches as a substring of
# the space-normalized "category title description" text.
TOPIC_RULES: list[tuple[str, list[str]]] = [
 ("Space & Physics", ["space","nasa","astronom","cosmic","cosmos","galaxy","galaxies","universe","interstellar","mars ","moon landing","black hole","exoplanet","rocket","spacex","james webb","fermi","extraterrestrial","quantum","relativity","dark matter","dark energy","string theory","particle physics","big bang","multiverse","astrophysic","physics","telescope","supernova","solar system"]),
 ("Home, DIY & Self-Sufficiency", ["homestead","self suffici","self-suffici","frugal","off grid","off-grid","foraging","forage","gardening","garden ","raised bed","no dig","straw bale","milking","goat","chicken coop","butcher","canning","preserv","homemade","home made","home remed","woodworking","hand tools","hand tool","diy","do it yourself","log cabin","backyard","compost","beekeep","ferment","sourdough","amish","prepper","bushcraft","rainwater","solar panel","household hack","harvest","livestock","frugal living","budget meal","growing food","grow your own","survival","primitive tech"]),
 ("Nature & Wildlife", ["wildlife","animal","species","conservation","ecosystem","rewilding","rewild","apex predator","predator","ocean","marine","deep sea","shark","insect","parasite","venomous","extinct","dinosaur","prehistoric","creature","beaver","wolves","wolf ","migration","rainforest","coral","whale","reptile","invasive","habitat","biodiversity","nature reclaims","reclaim","national park","volcano","earthquake","natural disaster","weather","hurricane","tornado"]),
 ("Tech & AI", ["artificial intelligence"," ai ","machine learning","chatgpt","cyber","hacking","hacker","data breach","data privacy","silicon valley","quantum computing","semiconductor","chip war","software","programming","coding","robotic","surveillance","dark web","cryptograph","tech compan","tech billionaire","tech layoff","big tech","internet history","internet mysteries","internet culture","social media","future technology","future tech","technology","gadget","drone","3d print","virtual reality","metaverse","algorithm","faceless channel","youtube automation","youtube channel","content creator"]),
 ("Money & Finance", ["finance","financial","stock market","stock ","invest","crypto","bitcoin","ethereum","defi","nft","economy","economic","recession","inflation","hedge fund","wall street","tax haven","index fund","real estate","mortgage","dividend","venture capital","central bank","money laundering","ponzi","get rich","passive income","currency","gold rush","casino","lottery","insurance","billionaire","millionaire","net worth","wealth","banking","debt"]),
 ("Business & Companies", ["business","compan","startup","corporate","corporation","entrepreneur","monopol","antitrust","bankruptc","dropship","ecommerce","e commerce","amazon fba","affiliate marketing","side hustle","print on demand","mlm","brand ","branding","advertising","marketing","pricing","supply chain","fast fashion","planned obsolescence","franchise","retail","tycoon","mogul","online business","make money online"]),
 ("Products & Reviews", ["mattress","review","best ","buying guide","top 10 best","unboxing","vacuum","appliance","product test","which to buy","worth it","comparison","buyers guide"]),
 ("True Crime & Scams", ["true crime","serial killer","murder","homicide","heist","fraud","scam","cult ","cults","prison","forensic","cold case","criminal","counterfeit","insider trading","espionage","missing people","assassination","kidnap","robbery","mafia","cartel","gang ","detective"]),
 ("Mysteries & Unexplained", ["paranormal","unsolved","conspiracy","ufo","uap ","cryptid","haunted","ghost","occult","bermuda","area 51","secret societ","cursed","forbidden history","declassified","government cover","cover up","disturbing","creepy","scary","horror","urban legend","reddit stories","simulation theory","vanished","enigma","supernatural"]),
 ("Psychology & Mind", ["psycholog","brain","mindset","behavior","manipulat","narciss","cognitive","dopamine","habits","discipline","productivity","persuasion","negotiation","overthinking","attachment theory","body language","meditation","consciousness","self improvement","motivation","mind ","the mind"]),
 ("Health & Body", ["health","nutrition","medical","medicine","disease","pandemic","diet ","fitness","muscle","fat loss","weight loss","longevity","pharma","addiction","gut health","fasting","supplement","sugar industry","surgery","virus","mental health","sleep","calories","workout","hormone"]),
 ("History", ["ancient","medieval","roman empire","rome ","egypt","viking","world war","ww1","ww2","cold war","vietnam war","napoleon","genghis","ottoman","british empire","dynasty","civilization","colonial","prohibition","great depression","mythology","pharaoh","samurai","crusade","empire","historical","weird history","history mysteries","royal family","black death","spanish flu","shipwreck","industrial revolution","french revolution","history"]),
 ("Geopolitics & Military", ["geopolitic","politic","government","election","military","nato","north korea","china economy","russia","middle east","sanctions","nuclear weapon","proxy war","fighter jet","aircraft carrier","submarine","special forces","world economy","failed state","border conflict","why countries","why nations","population collapse","india","africa","development explained","refugee","immigration","war mistakes","world news","news","current events","geopolitical"]),
 ("Engineering & Vehicles", ["engineering","megaproject","megastructure","construction","bridge","dam ","dam failure","dam river","aviation","plane crash","airline","aircraft","disaster","infrastructure","nuclear power","power grid","mining","railway","shipping industry","how things are made","how food is made","architecture","supercar","automotive","electric car","self driving","formula 1","car compan","engine ","tunnel","skyscraper","canal"]),
 ("Food & Consumer", ["food","restaurant","fast food","coffee","chocolate","spice trade","cooking","cuisine","beauty product","cosmetic","fashion brand","product recall","dangerous product","additive","food science","food brand","snack"]),
 ("Celebrity & Entertainment", ["celebrit","hollywood","movie","film ","box office","music industry","one hit wonder","musician","fame","tv show","netflix","streaming","pop culture","meme","youtube history","actor","singer","rapper","band ","music","iqiyi","drama","entertainment"]),
 ("Sports", ["sport","athlete","mma","ufc","boxing","football","soccer","olympic","bodybuilding","doping","chess","basketball","nba","extreme sports","betting","tennis","cycling"]),
 ("Gaming & Anime", ["gaming","video game","game development","esports","anime","manga","gamer","speedrun"]),
 ("Philosophy", ["philosoph","stoic","existential","nihilism","eastern philosophy"]),
 ("Geography & Places", ["geography","travel","tourist","tourism","abandoned","islands","maps history","country collapse","why cities","planned cities","lost city","lost cities","expedition","mountaineering","urban explor"]),
 ("Science & Discovery", ["science","biology","chemistry","genetics","evolution","human body","experiment","mathematic","optical illusion","strange phenomena","discoveries","microscop"]),
]

# Canonical ordered topic list (+ Other), for dropdowns / validation.
TOPICS: list[str] = [name for name, _ in TOPIC_RULES] + ["Other"]


def _norm(t: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip() + " "


def classify_topic(category: str = "", title: str = "", description: str = "") -> str:
    """Return the single best topic for a channel from its text signals."""
    n = _norm(f"{category} {title} {description[:300]}")
    for topic, kws in TOPIC_RULES:
        for kw in kws:
            if kw in n:
                return topic
    return "Other"


def backfill(store: YouTubeStore | None = None, *, only_missing: bool = True,
             on_step: Progress = lambda _m: None) -> dict:
    """Classify channels into topics and write channels.topic. only_missing=True
    only touches channels without a topic yet (cheap to run each radar cycle)."""
    if store is None:
        from beacon_engine.research.store import YouTubeStore

        store = YouTubeStore()
    with store.connect() as conn:
        where = "WHERE COALESCE(topic,'') = ''" if only_missing else ""
        rows = conn.execute(
            f"SELECT channel_id, COALESCE(category,''), COALESCE(title,''), "
            f"COALESCE(description,'') FROM channels {where}").fetchall()
        updates = [(classify_topic(cat, title, desc), cid)
                   for cid, cat, title, desc in rows]
        conn.executemany(
            "UPDATE channels SET topic = ? WHERE channel_id = ?", updates)
    dist = Counter(t for t, _ in updates)
    if updates:
        on_step(f"Classified {len(updates):,} channels into topics")
    return {"classified": len(updates), "distribution": dict(dist)}
