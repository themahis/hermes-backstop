"""Regenerates the Backstop logo and catalog banner (dev-only; needs Pillow and Windows' Segoe UI/Bahnschrift).

    python assets/make_assets.py

The mark: a failure (the red ball) dropping into a net — a backstop that catches what gets past.
logo.svg is the vector source; the PNGs are drawn from the same geometry.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
INK, WHITE, LIME, CORAL = (11, 15, 46), (255, 255, 255), (217, 255, 63), (255, 92, 92)
S = 4  # supersampling

# geometry on a 512 grid (kept in sync with logo.svg)
BASKET = "M 118 236 L 118 344 Q 118 400 174 400 L 338 400 Q 394 400 394 344 L 394 236"
NET_V = [((206, 252), (206, 386)), ((306, 252), (306, 386))]
NET_H = ((136, 318), (376, 318))
BALL = (256, 166, 38)
TRAIL = [((228, 64), (228, 102)), ((284, 52), (284, 98))]

SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <title>Backstop</title>
  <rect width="512" height="512" rx="112" fill="#0B0F2E"/>
  <g stroke="#D9FF3F" stroke-linecap="round" fill="none">
    <path d="{BASKET}" stroke-width="32" stroke-linejoin="round"/>
    <g stroke-width="14" stroke-opacity="0.5">
      <line x1="206" y1="252" x2="206" y2="386"/><line x1="306" y1="252" x2="306" y2="386"/>
      <line x1="136" y1="318" x2="376" y2="318"/>
    </g>
  </g>
  <g stroke="#FFFFFF" stroke-width="14" stroke-linecap="round" stroke-opacity="0.55">
    <line x1="228" y1="64" x2="228" y2="102"/><line x1="284" y1="52" x2="284" y2="98"/>
  </g>
  <circle cx="256" cy="166" r="38" fill="#FF5C5C"/>
</svg>
"""


def stroke(d, pts, width, colour, k):
    pts = [(x * k, y * k) for x, y in pts]
    d.line(pts, fill=colour, width=int(width * k), joint="curve")
    r = width * k / 2
    for x, y in pts:  # round caps and joins
        d.ellipse((x - r, y - r, x + r, y + r), fill=colour)


def quad(p0, p1, p2, n=24):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / n for i in range(n + 1))]


def draw_mark(img, ox, oy, size):
    k = size / 512
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((ox, oy, ox + size, oy + size), radius=112 * k, fill=INK)
    o = lambda pts: [(x + ox / k, y + oy / k) for x, y in pts]
    faint = Image.new("RGBA", img.size, (0, 0, 0, 0)); df = ImageDraw.Draw(faint)
    for a, b in NET_V + [NET_H]:
        stroke(df, o([a, b]), 14, LIME + (128,), k)
    for a, b in TRAIL:
        stroke(df, o([a, b]), 14, WHITE + (140,), k)
    img.alpha_composite(faint)
    sub = Image.new("RGBA", img.size, (0, 0, 0, 0)); ds = ImageDraw.Draw(sub)
    basket = [(118, 236), (118, 344)] + quad((118, 344), (118, 400), (174, 400))[1:] + [(338, 400)]         + quad((338, 400), (394, 400), (394, 344))[1:] + [(394, 236)]
    stroke(ds, o(basket), 32, LIME, k)
    cx, cy, r = BALL
    cx, cy = (cx + ox / k) * k, (cy + oy / k) * k
    ds.ellipse((cx - r * k, cy - r * k, cx + r * k, cy + r * k), fill=CORAL)
    img.alpha_composite(sub)


def logo_png(size):
    big = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    draw_mark(big, 0, 0, size * S)
    return big.resize((size, size), Image.LANCZOS)


def font(path, size, variation=None):
    f = ImageFont.truetype(path, size)
    if variation: f.set_variation_by_name(variation)
    return f


def banner():
    W, H = 1200 * S, 600 * S
    img = Image.new("RGBA", (W, H), INK + (255,))
    d = ImageDraw.Draw(img)
    # faint grid, like a terminal backdrop
    for x in range(0, W, 48 * S):
        d.line([(x, 0), (x, H)], fill=(255, 255, 255, 10), width=S)
    for y in range(0, H, 48 * S):
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 10), width=S)
    draw_mark(img, 96 * S, 150 * S, 300 * S)
    cond = r"C:\Windows\Fonts\bahnschrift.ttf"
    ui = r"C:\Windows\Fonts\segoeui.ttf"
    uib = r"C:\Windows\Fonts\segoeuib.ttf"
    mono = r"C:\Windows\Fonts\consola.ttf"
    d.text((460 * S, 150 * S), "BACKSTOP", font=font(cond, 128 * S, "Bold Condensed"), fill=WHITE)
    d.text((464 * S, 296 * S), "A failed build in your terminal,", font=font(ui, 38 * S), fill=(200, 205, 230))
    d.text((464 * S, 344 * S), "diagnosed read-only in Hermes.", font=font(uib, 38 * S), fill=LIME)
    d.text((464 * S, 430 * S), "PowerShell · bash · zsh   →   Hermes Desktop", font=font(mono, 24 * S), fill=(150, 156, 190))
    return img.resize((1200, 600), Image.LANCZOS).convert("RGB")


if __name__ == "__main__":
    (HERE / "logo.svg").write_text(SVG, encoding="utf-8")
    logo_png(512).save(HERE / "logo.png")
    logo_png(128).save(HERE / "logo-128.png")
    banner().save(HERE / "banner.png", optimize=True)
    print("wrote logo.svg, logo.png, logo-128.png, banner.png")
