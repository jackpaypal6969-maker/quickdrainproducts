"""Rendition pipeline: one source image in, AVIF + WebP + JPEG at four widths
out, all with the same aspect ratio so width/height attributes stay honest."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

WIDTHS = (480, 768, 1200, 1600)


def make_renditions(src: Path, out_dir: Path, base: str, *, background: tuple[int, int, int] = (20, 11, 51)) -> tuple[int, int]:
    """Write <base>-<w>.{avif,webp,jpg} into out_dir. Returns (width, height) of
    the largest rendition. Transparent sources are flattened onto the brand
    background so JPEG never gets a black box."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, background)
            flat.paste(rgba, mask=rgba.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        src_w, src_h = im.size
        largest = (0, 0)
        for w in WIDTHS:
            if w > src_w and largest != (0, 0):
                continue  # never upscale past the source, but always emit at least one
            target_w = min(w, src_w)
            target_h = round(src_h * target_w / src_w)
            rend = im.resize((target_w, target_h), Image.LANCZOS)
            rend.save(out_dir / f"{base}-{w}.jpg", "JPEG", quality=84, optimize=True, progressive=True)
            rend.save(out_dir / f"{base}-{w}.webp", "WEBP", quality=82, method=6)
            try:
                rend.save(out_dir / f"{base}-{w}.avif", "AVIF", quality=60, speed=6)
            except (OSError, ValueError):
                pass  # AVIF encoder missing on this box: <picture> falls through to WebP/JPEG
            largest = (target_w, target_h)
    return largest


def remove_renditions(out_dir: Path, base: str) -> None:
    for w in WIDTHS:
        for ext in ("jpg", "webp", "avif"):
            p = out_dir / f"{base}-{w}.{ext}"
            if p.exists():
                p.unlink()
