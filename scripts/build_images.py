#!/usr/bin/env python3
"""Build every product image the storefront serves.

    cd /path/to/quick-drain-products && .venv/bin/python scripts/build_images.py

Outputs (all under static/img/):
  products/quick-shot-bottle.svg          deterministic vector bottle, transparent, 1200x1500
  src/quick-shot-{hero,label,counter}.png 1600x2000 Chromium renders (rendition sources)
  products/quick-shot-<name>-<w>.{avif,webp,jpg}  via app.services.images.make_renditions
  og-default.jpg                          1200x630 Open Graph card
  favicon.svg, apple-touch-icon.png       cyan droplet-in-circle mark on indigo

The photo of the real bottle is not available as a file, so the bottle is drawn
as SVG reproducing the label copy exactly. Compositions are rendered headless
with Chromium through the project's node_modules/playwright (python-playwright
is not installed). Re-running overwrites everything; nothing else is touched.
If node, playwright or Chromium are missing the script prints a WARN and exits 0.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.images import make_renditions  # noqa: E402

IMG = ROOT / "static" / "img"
PRODUCTS = IMG / "products"
SRC = IMG / "src"
FONTS = ROOT / "static" / "fonts"
NODE_PLAYWRIGHT = ROOT / "node_modules" / "playwright"
CHROMIUM = Path(os.environ.get("QD_CHROMIUM", "/opt/pw-browsers/chromium"))

INDIGO_900 = "#140B33"
INDIGO_950 = "#0D0724"
CYAN = "#31F3E6"
CYAN_TINT = "#9EFFF8"


# --------------------------------------------------------------------------- #
# 1. The bottle                                                               #
# --------------------------------------------------------------------------- #

def font_face_css() -> str:
    geist = base64.b64encode((FONTS / "geist-variable.woff2").read_bytes()).decode()
    return (
        "@font-face{font-family:'Geist';font-style:normal;font-weight:100 900;"
        f"src:url(data:font/woff2;base64,{geist}) format('woff2');}}"
    )


def bottle_svg() -> str:
    """White 4 fl oz HDPE bottle, white ribbed cap, royal-blue wrap label.
    viewBox 1200x1500, transparent background, no drop shadow."""
    body = (
        "M 475 300 C 430 312, 340 370, 340 480 L 340 1330 "
        "Q 340 1392 402 1392 L 798 1392 Q 860 1392 860 1330 L 860 480 "
        "C 860 370, 770 312, 725 300 Z"
    )
    fam = "Geist, Inter, system-ui, sans-serif"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 1500" width="1200" height="1500" role="img" aria-labelledby="qs-title">
<title id="qs-title">Quick Shot 4 fl oz bottle — natural drain enzyme, dosed for monthly use on any drain</title>
<defs>
<style>{font_face_css()}
.t{{font-family:{fam};fill:#FFFFFF;text-anchor:middle;}}</style>
<!-- white plastic: soft vertical gradient -->
<linearGradient id="plastic" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#FFFFFF"/><stop offset="0.55" stop-color="#F4F6FA"/><stop offset="1" stop-color="#DFE3EA"/>
</linearGradient>
<!-- cylinder shading: darkens both edges, gives the label its curvature -->
<linearGradient id="curve" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="#000" stop-opacity="0.30"/><stop offset="0.14" stop-color="#000" stop-opacity="0.06"/>
<stop offset="0.42" stop-color="#000" stop-opacity="0"/><stop offset="0.74" stop-color="#000" stop-opacity="0.02"/>
<stop offset="0.9" stop-color="#000" stop-opacity="0.12"/><stop offset="1" stop-color="#000" stop-opacity="0.34"/>
</linearGradient>
<!-- single specular strip on the left third -->
<linearGradient id="spec" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="#FFF" stop-opacity="0"/><stop offset="0.45" stop-color="#FFF" stop-opacity="0.55"/>
<stop offset="0.7" stop-color="#FFF" stop-opacity="0.35"/><stop offset="1" stop-color="#FFF" stop-opacity="0"/>
</linearGradient>
<!-- deep royal blue label -->
<linearGradient id="label" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#1B40C4"/><stop offset="0.5" stop-color="#1638B4"/><stop offset="1" stop-color="#11309E"/>
</linearGradient>
<linearGradient id="capgrad" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#E4E8EF"/>
</linearGradient>
<!-- cap ribs -->
<pattern id="ribs" patternUnits="userSpaceOnUse" x="455" y="0" width="20" height="10">
<rect x="0" y="0" width="6" height="10" fill="#000" fill-opacity="0.10"/>
<rect x="6" y="0" width="3" height="10" fill="#FFF" fill-opacity="0.55"/>
</pattern>
<clipPath id="bodyclip"><path d="{body}"/></clipPath>
<clipPath id="capclip"><rect x="455" y="112" width="290" height="152" rx="18"/></clipPath>
</defs>

<!-- cap -->
<g clip-path="url(#capclip)">
<rect x="455" y="112" width="290" height="152" fill="url(#capgrad)"/>
<rect x="455" y="112" width="290" height="152" fill="url(#ribs)"/>
<rect x="455" y="112" width="290" height="152" fill="url(#curve)"/>
<rect x="490" y="112" width="40" height="152" fill="url(#spec)" opacity="0.8"/>
<rect x="455" y="112" width="290" height="14" fill="#FFF" fill-opacity="0.6"/>
</g>
<!-- cap skirt / neck collar -->
<rect x="470" y="262" width="260" height="14" fill="#CBD1DA"/>
<rect x="478" y="276" width="244" height="26" fill="#EEF1F5"/>
<rect x="478" y="276" width="244" height="26" fill="url(#curve)"/>

<!-- body -->
<path d="{body}" fill="url(#plastic)"/>
<g clip-path="url(#bodyclip)">
<rect x="320" y="560" width="560" height="730" fill="url(#label)"/>
<rect x="320" y="560" width="560" height="3" fill="#FFF" fill-opacity="0.12"/>
<rect x="320" y="1287" width="560" height="3" fill="#000" fill-opacity="0.18"/>

<!-- label copy -->
<text class="t" x="600" y="626" font-size="28" font-weight="600" letter-spacing="9">DRAIN MAINTAINER</text>
<rect x="382" y="662" width="436" height="262" rx="131" fill="none" stroke="#FFFFFF" stroke-width="9"/>
<text class="t" font-size="118" font-weight="800" letter-spacing="-2" transform="translate(600 775) skewX(-12) scale(0.84 1)">QUICK</text>
<text class="t" font-size="118" font-weight="800" letter-spacing="-2" transform="translate(600 885) skewX(-12) scale(0.84 1)">SHOT</text>
<text class="t" x="600" y="1005" font-size="37" font-weight="700" letter-spacing="1.5" fill="#BFF3FF">NATURAL DRAIN ENZYME</text>
<text class="t" x="600" y="1076" font-size="27" font-weight="600" letter-spacing="1">DOSED FOR MONTHLY USE</text>
<text class="t" x="600" y="1114" font-size="27" font-weight="600" letter-spacing="1">ON ANY DRAIN</text>
<text class="t" x="600" y="1252" font-size="19" font-weight="500" letter-spacing="2">NET CONTENTS 4 FL OZ (118mL)</text>

<!-- shading over plastic and label alike -->
<rect x="320" y="290" width="560" height="1120" fill="url(#curve)"/>
<rect x="392" y="290" width="56" height="1120" fill="url(#spec)"/>
<!-- base edge -->
<rect x="320" y="1372" width="560" height="30" fill="#000" fill-opacity="0.08"/>
</g>
</svg>
"""


# --------------------------------------------------------------------------- #
# 2. Compositions (HTML rendered by Chromium)                                 #
# --------------------------------------------------------------------------- #

def _inline_svg(svg: str) -> str:
    # drop the fixed width/height so CSS sizes it
    return svg.replace('width="1200" height="1500" ', "", 1)


def page(body: str, css: str, w: int, h: int) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;padding:0;width:{w}px;height:{h}px;overflow:hidden;}}"
        "body{position:relative;}"
        ".bottle svg{display:block;width:100%;height:100%;}"
        f"{css}</style></head><body>{body}</body></html>"
    )


def hero_html(svg: str) -> str:
    bottle = _inline_svg(svg)
    css = f"""
body{{background:radial-gradient(70% 55% at 50% 42%, {INDIGO_900} 0%, {INDIGO_950} 100%);}}
.glow{{position:absolute;left:50%;top:1460px;width:1200px;height:260px;transform:translateX(-50%);
  background:radial-gradient(50% 50% at 50% 50%, rgba(49,243,230,0.10) 0%, rgba(49,243,230,0) 70%);}}
.bottle{{position:absolute;left:50%;top:170px;width:1104px;height:1380px;transform:translateX(-50%);}}
.reflect{{position:absolute;left:50%;top:1542px;width:1104px;height:1380px;transform:translateX(-50%) scaleY(-1);
  opacity:0.2;filter:blur(1.5px);
  -webkit-mask-image:linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0) 28%);
  mask-image:linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0) 28%);}}
"""
    body = f"<div class='glow'></div><div class='reflect'>{bottle}</div><div class='bottle'>{bottle}</div>"
    return page(body, css, 1600, 2000)


def label_html(svg: str) -> str:
    bottle = _inline_svg(svg)
    # scale so the label band (viewBox y 560..1290) fills the frame with a margin
    s = 1500 / 730
    top = 1000 - 925 * s
    css = f"""
body{{background:radial-gradient(70% 55% at 50% 45%, {INDIGO_900} 0%, {INDIGO_950} 100%);}}
.bottle{{position:absolute;left:50%;top:{top:.1f}px;width:{1200*s:.1f}px;height:{1500*s:.1f}px;transform:translateX(-50%);}}
"""
    return page(f"<div class='bottle'>{bottle}</div>", css, 1600, 2000)


def counter_html(svg: str) -> str:
    bottle = _inline_svg(svg)
    css = f"""
body{{background:linear-gradient(to bottom, {INDIGO_950} 0%, {INDIGO_900} 62%, #1B1140 100%);}}
.rim{{position:absolute;left:50%;top:120px;width:1500px;height:1700px;transform:translateX(-50%);
  background:radial-gradient(38% 46% at 63% 44%, rgba(49,243,230,0.22) 0%, rgba(49,243,230,0.06) 45%, rgba(49,243,230,0) 72%);}}
.floor{{position:absolute;left:0;top:1560px;width:1600px;height:440px;
  background:linear-gradient(to bottom, rgba(255,255,255,0.05), rgba(255,255,255,0) 55%);}}
.floorline{{position:absolute;left:0;top:1560px;width:1600px;height:1px;background:rgba(169,232,255,0.16);}}
.shadow{{position:absolute;left:50%;top:1500px;width:900px;height:150px;transform:translateX(-50%);
  background:radial-gradient(50% 50% at 50% 50%, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0) 70%);}}
.bottle{{position:absolute;left:50%;top:190px;width:1104px;height:1380px;transform:translateX(-50%);
  filter:drop-shadow(10px 0 10px rgba(49,243,230,0.55)) drop-shadow(-4px 0 8px rgba(49,243,230,0.16));}}
"""
    body = "<div class='rim'></div><div class='floor'></div><div class='floorline'></div><div class='shadow'></div>" \
           f"<div class='bottle'>{bottle}</div>"
    return page(body, css, 1600, 2000)


def og_html(svg: str) -> str:
    bottle = _inline_svg(svg)
    inter = base64.b64encode((FONTS / "inter-latin-wght-normal.woff2").read_bytes()).decode()
    css = f"""
@font-face{{font-family:'Inter';font-weight:100 900;src:url(data:font/woff2;base64,{inter}) format('woff2');}}
body{{background:radial-gradient(60% 90% at 20% 30%, {INDIGO_900} 0%, {INDIGO_950} 100%);color:#F7FBFF;}}
.hero-glow{{position:absolute;left:0;top:0;width:1200px;height:630px;
  background:radial-gradient(50% 50% at 20% 0%, rgba(49,243,230,0.32) 0%, rgba(49,243,230,0) 32%);}}
.bottle{{position:absolute;left:96px;top:40px;width:440px;height:550px;}}
.copy{{position:absolute;left:540px;top:0;width:600px;height:630px;display:flex;flex-direction:column;justify-content:center;}}
.eyebrow{{font-family:Geist,sans-serif;font-weight:800;font-size:15px;letter-spacing:0.12em;text-transform:uppercase;color:{CYAN};margin:0 0 22px;}}
h1{{font-family:Geist,sans-serif;font-weight:800;font-size:74px;line-height:0.96;letter-spacing:-0.045em;margin:0 0 26px;}}
p{{font-family:Inter,sans-serif;font-weight:500;font-size:30px;line-height:1.3;color:rgba(224,239,255,0.78);margin:0;}}
.rule{{width:64px;height:3px;background:{CYAN};margin:30px 0 0;border-radius:999px;}}
"""
    body = (
        "<div class='hero-glow'></div>"
        f"<div class='bottle'>{bottle}</div>"
        "<div class='copy'><div class='eyebrow'>Quick Shot &middot; drain maintainer</div>"
        "<h1>Quick Drain Products</h1><p>One bottle. One drain. One month.</p><div class='rule'></div></div>"
    )
    return page(body, css, 1200, 630)


# --------------------------------------------------------------------------- #
# 3. Mark: favicon + apple touch icon                                          #
# --------------------------------------------------------------------------- #

def mark_svg(size: int, *, rounded: bool) -> str:
    """Cyan droplet inside a cyan ring on indigo. Drawn in a 64-unit space."""
    bg = f'<rect width="64" height="64" rx="{14 if rounded else 0}" fill="{INDIGO_900}"/>'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{size}" height="{size}" role="img" aria-label="Quick Drain Products">
{bg}
<circle cx="32" cy="32" r="23" fill="none" stroke="{CYAN}" stroke-width="3.5"/>
<path d="M32 17.5 C 27 25.5, 22.5 30.5, 22.5 36.2 A 9.5 9.5 0 0 0 41.5 36.2 C 41.5 30.5, 37 25.5, 32 17.5 Z" fill="{CYAN}"/>
<path d="M27.2 36.6 A 4.8 4.8 0 0 0 30.3 41.0" fill="none" stroke="{INDIGO_900}" stroke-width="2" stroke-linecap="round"/>
</svg>
"""


def touch_icon_html() -> str:
    css = f"body{{background:{INDIGO_900};}} svg{{display:block;width:180px;height:180px;}}"
    return page(mark_svg(180, rounded=False), css, 180, 180)


# --------------------------------------------------------------------------- #
# 4. Chromium via node + playwright                                            #
# --------------------------------------------------------------------------- #

RENDER_JS = r"""
const fs = require('fs');
const { chromium } = require(process.argv[2]);
const jobs = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
(async () => {
  const browser = await chromium.launch({
    executablePath: process.argv[4],
    args: ['--no-sandbox', '--disable-gpu', '--font-render-hinting=none', '--hide-scrollbars'],
  });
  try {
    for (const job of jobs) {
      const page = await browser.newPage({ viewport: { width: job.width, height: job.height }, deviceScaleFactor: 1 });
      await page.goto('file://' + job.html, { waitUntil: 'load' });
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: job.out, type: 'png', clip: { x: 0, y: 0, width: job.width, height: job.height } });
      await page.close();
      console.log('rendered ' + job.out);
    }
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error('render failed: ' + (e.stack || e)); process.exit(1); });
"""


def render(jobs: list[dict]) -> bool:
    node = shutil.which("node")
    if not node:
        print("WARN: node not found on PATH; skipping Chromium renders.")
        return False
    if not (NODE_PLAYWRIGHT / "package.json").exists():
        print(f"WARN: playwright not installed at {NODE_PLAYWRIGHT}; skipping Chromium renders.")
        return False
    if not CHROMIUM.exists():
        print(f"WARN: Chromium not found at {CHROMIUM}; skipping Chromium renders.")
        return False
    with tempfile.TemporaryDirectory(prefix="qd-images-") as tmp:
        tmpdir = Path(tmp)
        spec = []
        for j in jobs:
            html_path = tmpdir / f"{j['name']}.html"
            html_path.write_text(j["html"], encoding="utf-8")
            spec.append({"html": str(html_path), "out": str(j["out"]), "width": j["width"], "height": j["height"]})
        (tmpdir / "render.js").write_text(RENDER_JS, encoding="utf-8")
        (tmpdir / "jobs.json").write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(
            [node, str(tmpdir / "render.js"), str(NODE_PLAYWRIGHT), str(tmpdir / "jobs.json"), str(CHROMIUM)],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"Chromium render failed (exit {proc.returncode})")
    return True


# --------------------------------------------------------------------------- #

def main() -> int:
    PRODUCTS.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)

    svg = bottle_svg()
    (PRODUCTS / "quick-shot-bottle.svg").write_text(svg, encoding="utf-8")
    (IMG / "favicon.svg").write_text(mark_svg(64, rounded=True), encoding="utf-8")
    print("wrote products/quick-shot-bottle.svg, favicon.svg")

    heroes = {
        "quick-shot-hero": hero_html(svg),
        "quick-shot-label": label_html(svg),
        "quick-shot-counter": counter_html(svg),
    }
    jobs = [{"name": n, "html": h, "out": SRC / f"{n}.png", "width": 1600, "height": 2000} for n, h in heroes.items()]
    jobs.append({"name": "og-default", "html": og_html(svg), "out": SRC / "og-default.png", "width": 1200, "height": 630})
    jobs.append({"name": "apple-touch-icon", "html": touch_icon_html(), "out": IMG / "apple-touch-icon.png", "width": 180, "height": 180})

    if not render(jobs):
        print("WARN: renditions not rebuilt (bottle SVG and favicon were written).")
        return 0

    from PIL import Image

    for name in heroes:
        w, h = make_renditions(SRC / f"{name}.png", PRODUCTS, name)
        print(f"renditions {name}: largest {w}x{h}")

    with Image.open(SRC / "og-default.png") as im:
        im.convert("RGB").save(IMG / "og-default.jpg", "JPEG", quality=86, optimize=True, progressive=True)
    (SRC / "og-default.png").unlink()
    print("wrote og-default.jpg, apple-touch-icon.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
