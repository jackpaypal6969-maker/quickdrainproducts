"""Catalog queries. Products carry variants, images, specs, FAQs and a rating
aggregate computed from approved reviews only."""
from __future__ import annotations

import json
import sqlite3

from ..config import settings
from ..db import all_rows, get_setting, one

IMAGE_WIDTHS = (480, 768, 1200, 1600)


def image_sources(image: dict) -> dict:
    """Return srcset strings for <picture>. Files are produced by
    scripts/build_images.py as <base>-<w>.{avif,webp,jpg}. Missing renditions
    fall back to whatever exists so a half-built pipeline never breaks layout."""
    base = image["base"]
    if image.get("source") == "upload":
        root = settings.media_dir / "uploads"
        url_root = "/media/uploads"
    else:
        root = settings.static_dir / "img" / "products"
        url_root = "/static/img/products"

    def variants(ext: str) -> str:
        parts = []
        for w in IMAGE_WIDTHS:
            if (root / f"{base}-{w}.{ext}").exists():
                parts.append(f"{url_root}/{base}-{w}.{ext} {w}w")
        return ", ".join(parts)

    fallback = ""
    for w in (1200, 768, 480, 1600):
        for ext in ("jpg", "png", "webp"):
            if (root / f"{base}-{w}.{ext}").exists():
                fallback = f"{url_root}/{base}-{w}.{ext}"
                break
        if fallback:
            break
    if not fallback:
        for ext in ("jpg", "png", "webp"):
            if (root / f"{base}.{ext}").exists():
                fallback = f"{url_root}/{base}.{ext}"
                break
    return {
        "avif": variants("avif"),
        "webp": variants("webp"),
        "jpg": variants("jpg"),
        "src": fallback,
        "alt": image.get("alt", ""),
        "width": image.get("width", 1200),
        "height": image.get("height", 1500),
        "missing": not fallback,
    }


def _product_bundle(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    p = dict(row)
    p["variants"] = [dict(v) for v in all_rows(conn, "SELECT * FROM variants WHERE product_id = ? AND is_active = 1 ORDER BY sort, id", (p["id"],))]
    p["images"] = [image_sources(dict(i)) | {"id": i["id"], "kind": i["kind"], "base": i["base"]} for i in all_rows(conn, "SELECT * FROM product_images WHERE product_id = ? ORDER BY sort, id", (p["id"],))]
    p["specs"] = [dict(s) for s in all_rows(conn, "SELECT label, value FROM product_specs WHERE product_id = ? ORDER BY sort, id", (p["id"],))]
    p["faqs"] = [dict(f) for f in all_rows(conn, "SELECT question, answer FROM product_faqs WHERE product_id = ? ORDER BY sort, id", (p["id"],))]
    try:
        p["claims"] = json.loads(p.get("label_claims") or "[]")
    except json.JSONDecodeError:
        p["claims"] = []
    agg = one(conn, "SELECT COUNT(*) AS n, AVG(rating) AS avg FROM reviews WHERE product_id = ? AND status = 'approved'", (p["id"],))
    p["review_count"] = int(agg["n"] or 0)
    p["rating_avg"] = round(float(agg["avg"]), 1) if agg["avg"] else None
    p["hero_image"] = next((i for i in p["images"] if i["kind"] == "hero"), p["images"][0] if p["images"] else None)
    p["from_price_cents"] = min((v["price_cents"] for v in p["variants"]), default=0)
    p["in_stock"] = any(v["stock"] > 0 for v in p["variants"])
    p["safe_for_list"] = [s.strip() for s in (p.get("safe_for") or "").split(",") if s.strip()]
    p["not_safe_for_list"] = [s.strip() for s in (p.get("not_safe_for") or "").split(",") if s.strip()]
    p["sds_available"] = bool(p.get("sds_path")) and (settings.media_dir / p["sds_path"]).exists()
    p["label_available"] = bool(p.get("label_path")) and (settings.media_dir / p["label_path"]).exists()
    p["dose_rows"] = dose_rows(p)
    attach_subscription_pricing(conn, p)
    return p


def subscription_config(conn: sqlite3.Connection) -> dict:
    """Admin-controlled subscribe-and-save settings (site-wide)."""
    try:
        percent = max(0, min(int(get_setting(conn, "subscription_discount_percent", "10") or 10), 90))
    except ValueError:
        percent = 10
    intervals = []
    for part in (get_setting(conn, "subscription_intervals", "1,2,3") or "1").split(","):
        try:
            m = int(part.strip())
        except ValueError:
            continue
        if 1 <= m <= 12 and m not in intervals:
            intervals.append(m)
    return {"enabled": settings.subscriptions_enabled, "percent": percent, "intervals": sorted(intervals) or [1]}


def subscription_price(price_cents: int, percent: int) -> int:
    return max(0, round(price_cents * (100 - max(0, min(percent, 100))) / 100))


def attach_subscription_pricing(conn: sqlite3.Connection, p: dict) -> None:
    cfg = subscription_config(conn)
    p["subscriptions"] = cfg
    interval_days = int(p.get("dose_interval_days") or 30)
    per_unit = max(int(p.get("drains_per_unit") or 1), 1)
    for v in p.get("variants", []):
        pct = v.get("subscription_discount_percent")
        pct = cfg["percent"] if pct is None else int(pct)
        v["sub_percent"] = pct
        v["sub_price_cents"] = subscription_price(int(v["price_cents"]), pct)
        # A 3-pack every 3 months keeps one drain dosed continuously.
        ideal = max(1, round(int(v.get("units_per_pack") or 1) * per_unit * interval_days / 30))
        v["sub_recommended_months"] = min(cfg["intervals"], key=lambda m: (abs(m - ideal), m))


def dose_rows(p: dict) -> list[dict]:
    """The plain coverage table: one unit treats `drains_per_unit` drains for one
    `dose_interval_days` interval. Everything else is arithmetic on the label."""
    rows = []
    interval = int(p.get("dose_interval_days") or 30)
    per_unit = int(p.get("drains_per_unit") or 1)
    unit_word = "month" if interval == 30 else f"{interval}-day period"
    for v in p.get("variants", []):
        units = int(v.get("units_per_pack") or 1)
        drain_intervals = units * per_unit
        rows.append({
            "variant": v,
            "units": units,
            "one_drain_months": drain_intervals,
            "drains_one_month": drain_intervals,
            "interval_word": unit_word,
            "per_unit_cents": round(v["price_cents"] / units),
        })
    return rows


def get_product(conn: sqlite3.Connection, slug: str, include_inactive: bool = False) -> dict | None:
    sql = "SELECT * FROM products WHERE slug = ?" + ("" if include_inactive else " AND is_active = 1")
    row = one(conn, sql, (slug,))
    return _product_bundle(conn, row) if row else None


def get_product_by_id(conn: sqlite3.Connection, product_id: int) -> dict | None:
    row = one(conn, "SELECT * FROM products WHERE id = ?", (product_id,))
    return _product_bundle(conn, row) if row else None


def list_products(conn: sqlite3.Connection, collection_slug: str | None = None) -> list[dict]:
    if collection_slug:
        rows = all_rows(conn, "SELECT p.* FROM products p JOIN collections c ON c.id = p.collection_id WHERE p.is_active = 1 AND c.slug = ? ORDER BY p.sort, p.id", (collection_slug,))
    else:
        rows = all_rows(conn, "SELECT * FROM products WHERE is_active = 1 ORDER BY sort, id")
    return [_product_bundle(conn, r) for r in rows]


def featured_product(conn: sqlite3.Connection) -> dict | None:
    row = one(conn, "SELECT * FROM products WHERE is_active = 1 ORDER BY is_featured DESC, sort, id LIMIT 1")
    return _product_bundle(conn, row) if row else None


def get_variant(conn: sqlite3.Connection, variant_id: int) -> dict | None:
    row = one(conn, "SELECT v.*, p.name AS product_name, p.slug AS product_slug, p.dose_interval_days, p.drains_per_unit, p.hazmat, p.weight_oz AS product_weight_oz FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?", (variant_id,))
    return dict(row) if row else None


def approved_reviews(conn: sqlite3.Connection, product_id: int, limit: int = 20) -> list[dict]:
    return [dict(r) for r in all_rows(conn, "SELECT id, author_name, rating, title, body, is_verified, created_at FROM reviews WHERE product_id = ? AND status = 'approved' ORDER BY created_at DESC LIMIT ?", (product_id, limit))]


def rating_breakdown(conn: sqlite3.Connection, product_id: int) -> dict[int, int]:
    rows = all_rows(conn, "SELECT rating, COUNT(*) AS n FROM reviews WHERE product_id = ? AND status = 'approved' GROUP BY rating", (product_id,))
    out = {i: 0 for i in range(1, 6)}
    for r in rows:
        out[int(r["rating"])] = int(r["n"])
    return out
