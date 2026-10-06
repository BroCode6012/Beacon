"""Draw Beacon's lighthouse icon crisply at any size, and write every icon file Beacon uses.

The original assets/beacon_icon.ico tops out at 256 px, so phone/tablet icons (512 px) made
from it would be blurry, and in the web version Flet showed its own default logo because
Beacon had no web icons at all (operator, 2026-09-29: "the default icon is a low quality one").

This redraws the same design — navy tile, two warm light beams, a glowing light with three
rings, a white tower with two red stripes on a grey base — from measurements of the original
(256-unit coordinates), at 8x supersampling, then scales down. Two small cleanups over the
original: the beams stay inside the tile (they spilled over its top edge), and the rings are
solid (they were see-through holes, which looked dark on a dark taskbar).

    python tools_make_icons.py        # writes into assets/
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

NAVY = (13, 22, 40, 255)
BEAM = (255, 195, 65, 80)            # warm yellow, see-through
GLOW = (255, 212, 100, 255)
CORE = (255, 251, 230, 255)
RING = (255, 236, 176, 255)
TOWER = (236, 240, 247, 255)
STRIPE = (213, 50, 49, 255)
BASE = (188, 197, 211, 255)
GAP = (5, 12, 28, 255)                # the thin dark line between the light and the tower


def draw(size: int, *, full_bleed: bool = False, content_scale: float = 1.0) -> Image.Image:
    """The icon at `size` px. full_bleed: square navy background to the edges (for phone
    icons the system crops itself); content_scale shrinks the drawing toward the centre (the
    'maskable' safe zone is the middle 80%)."""
    S = 2048                                   # supersampled canvas
    k = S / 256.0

    def p(x, y):                               # 256-unit point -> canvas, scaled about centre
        return ((128 + (x - 128) * content_scale) * k, (128 + (y - 128) * content_scale) * k)

    def r(v):
        return v * content_scale * k

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile = Image.new("L", (S, S), 0)
    td = ImageDraw.Draw(tile)
    if full_bleed:
        td.rectangle([0, 0, S, S], fill=255)
    else:
        td.rounded_rectangle([9 * k, 9 * k, 247 * k, 247 * k], radius=40 * k, fill=255)
    img.paste(NAVY, (0, 0), tile)

    # Two beams from the light, clipped to the tile.
    beams = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beams)
    apex = p(128, 101)
    for outer, inner in (((21.7, -10), (76.5, -10)), ((234.3, -10), (179.5, -10))):
        # extend the edges past the top so clipping, not the triangle, makes the edge
        bd.polygon([apex, p(*_along(apex_units=(128, 101), to=outer)),
                    p(*_along(apex_units=(128, 101), to=inner))], fill=BEAM)
    beam_alpha = ImageChops.multiply(beams.getchannel("A"), tile)
    beams.putalpha(beam_alpha)
    img = Image.alpha_composite(img, beams)

    d = ImageDraw.Draw(img)
    cx, cy = 128, 101.5
    for radius, width in ((45.5, 4.0), (33.0, 3.6), (23.0, 3.2)):
        box = [*p(cx - radius, cy - radius), *p(cx + radius, cy + radius)]
        d.ellipse(box, outline=RING, width=max(1, round(r(width))))
    d.ellipse([*p(cx - 16.5, cy - 16.5), *p(cx + 16.5, cy + 16.5)], fill=GLOW)
    d.ellipse([*p(cx - 9.5, cy - 9.5), *p(cx + 9.5, cy + 9.5)], fill=CORE)

    def left(y):                               # the tower's left edge (mirrored for the right)
        return 112.6 - (y - 122) * 14 / 75

    d.rectangle([*p(left(119) - 2, 118.5), *p(256 - left(119) + 2, 122)], fill=GAP)
    d.polygon([p(left(122), 122), p(256 - left(122), 122),
               p(256 - left(204.5), 204.5), p(left(204.5), 204.5)], fill=TOWER)
    for top, bottom in ((136.5, 152.5), (169.0, 184.5)):
        d.polygon([p(left(top), top), p(256 - left(top), top),
                   p(256 - left(bottom), bottom), p(left(bottom), bottom)], fill=STRIPE)
    d.rounded_rectangle([*p(85, 205), *p(171, 220)], radius=r(6), fill=BASE)

    return img.resize((size, size), Image.LANCZOS)


def _along(apex_units, to):
    """A point well beyond `to` on the ray from the apex — so beams run off the tile."""
    ax, ay = apex_units
    tx, ty = to
    return ax + (tx - ax) * 1.6, ay + (ty - ay) * 1.6


# Why the web page gets Beacon-only file names too: Flet's web client installs a service worker
# that keeps its OWN copies of favicon.png, manifest.json and icons/*.png and always answers
# from them (flutter_service_worker.js, cache-first for every file in its RESOURCES list). A
# browser that opened Beacon before these icons existed kept showing Flet's logo even though the
# server sent Beacon's (operator, 2026-09-29). Files it does not list go to the server, and the
# page itself is fetched online-first — so the page below points at names it has never cached.
WEB_NAMES = {"favicon.png": "beacon-favicon.png",
             "icons/apple-touch-icon-192.png": "icons/beacon-apple-touch-192.png",
             "icons/loading-animation.png": "icons/beacon-loading.png",
             "manifest.json": "beacon-manifest.json"}


def beacon_index_html(flet_index: str) -> str:
    """Flet's own start page with Beacon's names and icons (Flet still patches it at serve
    time: base href, websocket path, renderer — exactly as it does its own)."""
    page = flet_index
    for old, new in WEB_NAMES.items():
        assert f'"{old}"' in page, f"Flet's index.html no longer references {old}"
        page = page.replace(f'"{old}"', f'"{new}"')
    replacements = {
        "<title>Flet</title>": "<title>Beacon — Niche Finder</title>",
        '<meta name="description" content="Flet application.">':
            '<meta name="description" content="Beacon — find faceless, replicable YouTube niches">',
        '<meta name="apple-mobile-web-app-title" content="Flet">':
            '<meta name="apple-mobile-web-app-title" content="Beacon">',
    }
    for old, new in replacements.items():
        assert old in page, f"Flet's index.html changed: {old!r} not found"
        page = page.replace(old, new)
    return page


def flet_index_html() -> str:
    import flet_web
    return (Path(flet_web.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")


MANIFEST = {
    "name": "Beacon — Niche Finder", "short_name": "Beacon", "start_url": ".",
    "display": "standalone", "background_color": "#0D1628", "theme_color": "#0D1628",
    "description": "Beacon — find faceless, replicable YouTube niches",
    "orientation": "natural", "prefer_related_applications": False,
}


def manifest(prefix: str) -> str:
    import json
    data = dict(MANIFEST, icons=[
        {"src": f"icons/{prefix}192.png", "sizes": "192x192", "type": "image/png"},
        {"src": f"icons/{prefix}512.png", "sizes": "512x512", "type": "image/png"},
        {"src": f"icons/{prefix}maskable-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "maskable"},
        {"src": f"icons/{prefix}maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"}])
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    icons = ASSETS / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    favicon = draw(64).resize((32, 32), Image.LANCZOS)
    for name in ("favicon.png", "beacon-favicon.png"):
        favicon.save(ASSETS / name)
    for n in (192, 512):
        for prefix in ("icon-", "beacon-"):
            draw(n).save(icons / f"{prefix}{n}.png")
            draw(n, full_bleed=True, content_scale=0.78).save(icons / f"{prefix}maskable-{n}.png")
    for name in ("apple-touch-icon-192.png", "beacon-apple-touch-192.png"):
        draw(192, full_bleed=True).save(icons / name)
    for name in ("loading-animation.png", "beacon-loading.png"):
        draw(512).save(icons / name)
    (ASSETS / "manifest.json").write_bytes(manifest("icon-").encode("utf-8"))
    (ASSETS / "beacon-manifest.json").write_bytes(manifest("beacon-").encode("utf-8"))
    (ASSETS / "index.html").write_bytes(beacon_index_html(flet_index_html()).encode("utf-8"))
    big = draw(256)
    big.save(ASSETS / "beacon_icon.ico",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw(512).save(ASSETS / "beacon_icon_512.png")
    print("icons written to", ASSETS)


if __name__ == "__main__":
    main()
