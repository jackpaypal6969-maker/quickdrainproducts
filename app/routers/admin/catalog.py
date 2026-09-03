"""Products, variants, inventory, images, specs, FAQs."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ...config import settings
from ...db import all_rows, one, transaction
from ...deps import flash, get_db, ip, redirect, require_admin
from ...services import audit, catalog, images
from .common import arender, as_int, dollars_to_cents

router = APIRouter()

FORMULATIONS = ("enzymatic", "bacterial", "caustic", "acid", "surfactant")


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:80] or "product"


@router.get("/")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    stats = {
        "revenue_30d": one(conn, "SELECT COALESCE(SUM(total_cents - refunded_cents),0) AS c FROM orders WHERE created_at >= datetime('now','-30 days') AND status != 'canceled'")["c"],
        "orders_30d": one(conn, "SELECT COUNT(*) AS n FROM orders WHERE created_at >= datetime('now','-30 days')")["n"],
        "customers": one(conn, "SELECT COUNT(*) AS n FROM customers WHERE deleted_at IS NULL")["n"],
        "subscribers": one(conn, "SELECT COUNT(*) AS n FROM newsletter_subscribers WHERE unsubscribed_at IS NULL")["n"],
        "units_30d": one(conn, "SELECT COALESCE(SUM(oi.qty * oi.units_per_pack),0) AS n FROM order_items oi JOIN orders o ON o.id = oi.order_id WHERE o.created_at >= datetime('now','-30 days')")["n"],
    }
    recent = [dict(r) for r in all_rows(conn, "SELECT id, order_number, email, status, total_cents, created_at FROM orders ORDER BY created_at DESC LIMIT 8")]
    low = [dict(r) for r in all_rows(conn, "SELECT v.*, p.name AS product_name FROM variants v JOIN products p ON p.id = v.product_id WHERE v.is_active = 1 AND v.stock <= v.low_stock_threshold ORDER BY v.stock")]
    placeholders = one(conn, "SELECT value FROM settings WHERE key = 'prices_are_placeholders'")
    return arender(request, "admin/dashboard.html", {"stats": stats, "recent": recent, "low": low, "prices_placeholder": bool(placeholders and placeholders["value"] == "1"), "meta_title": "Admin"}, conn, admin)


@router.get("/products")
def products(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, "SELECT p.*, (SELECT COUNT(*) FROM variants v WHERE v.product_id = p.id) AS variant_count, (SELECT COALESCE(SUM(stock),0) FROM variants v WHERE v.product_id = p.id) AS stock FROM products p ORDER BY sort, id")]
    return arender(request, "admin/products.html", {"products": rows, "meta_title": "Products"}, conn, admin)


@router.get("/products/new")
def product_new(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    collections = [dict(r) for r in all_rows(conn, "SELECT id, name FROM collections ORDER BY sort")]
    return arender(request, "admin/product_form.html", {"product": None, "movements": [], "collections": collections, "formulations": FORMULATIONS, "meta_title": "New product"}, conn, admin)


@router.get("/products/{product_id}")
def product_edit(product_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    product = catalog.get_product_by_id(conn, product_id)
    if not product:
        raise HTTPException(404)
    product["all_variants"] = [dict(v) for v in all_rows(conn, "SELECT * FROM variants WHERE product_id = ? ORDER BY sort, id", (product_id,))]
    product["image_rows"] = [dict(i) for i in all_rows(conn, "SELECT * FROM product_images WHERE product_id = ? ORDER BY sort, id", (product_id,))]
    movements = [dict(m) for m in all_rows(conn, "SELECT m.*, v.sku FROM inventory_movements m JOIN variants v ON v.id = m.variant_id WHERE v.product_id = ? ORDER BY m.id DESC LIMIT 30", (product_id,))]
    collections = [dict(r) for r in all_rows(conn, "SELECT id, name FROM collections ORDER BY sort")]
    return arender(request, "admin/product_form.html", {"product": product, "movements": movements, "collections": collections, "formulations": FORMULATIONS, "meta_title": f"Edit {product['name']}"}, conn, admin)


@router.post("/products/save")
async def product_save(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    form = await request.form()
    pid = as_int(form.get("id"))
    formulation = str(form.get("formulation_type") or "enzymatic")
    if formulation not in FORMULATIONS:
        formulation = "enzymatic"
    claims = [c.strip() for c in str(form.get("label_claims") or "").splitlines() if c.strip()]
    data = {
        "name": str(form.get("name") or "").strip()[:120],
        "slug": _slugify(str(form.get("slug") or form.get("name") or "")),
        "tagline": str(form.get("tagline") or "").strip()[:200],
        "description": str(form.get("description") or "").strip()[:20000],
        "formulation_type": formulation,
        "hazmat": 1 if formulation in ("caustic", "acid") or form.get("hazmat") else 0,
        "active_ingredients": str(form.get("active_ingredients") or "").strip()[:2000],
        "net_volume_oz": float(form.get("net_volume_oz") or 0) or None,
        "net_volume_ml": float(form.get("net_volume_ml") or 0) or None,
        "dose_text": str(form.get("dose_text") or "").strip()[:300],
        "dose_interval_days": max(as_int(form.get("dose_interval_days"), 30), 1),
        "drains_per_unit": max(as_int(form.get("drains_per_unit"), 1), 1),
        "directions": str(form.get("directions") or "").strip()[:5000],
        "safe_for": str(form.get("safe_for") or "").strip()[:500],
        "not_safe_for": str(form.get("not_safe_for") or "").strip()[:500],
        "label_claims": json.dumps(claims),
        "prop65_warning": str(form.get("prop65_warning") or "").strip()[:1000],
        "weight_oz": float(form.get("weight_oz") or 5) or 5,
        "collection_id": as_int(form.get("collection_id")) or None,
        "is_active": 1 if form.get("is_active") else 0,
        "is_featured": 1 if form.get("is_featured") else 0,
        "sort": as_int(form.get("sort")),
        "seo_title": str(form.get("seo_title") or "").strip()[:150],
        "seo_description": str(form.get("seo_description") or "").strip()[:300],
    }
    if not data["name"]:
        flash(request, "A product needs a name.", "error")
        return redirect("/admin/products")
    with transaction(conn):
        before = one(conn, "SELECT * FROM products WHERE id = ?", (pid,)) if pid else None
        if pid and before:
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE products SET {sets}, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (*data.values(), pid))
        else:
            if one(conn, "SELECT 1 FROM products WHERE slug = ?", (data["slug"],)):
                data["slug"] = f"{data['slug']}-{uuid.uuid4().hex[:4]}"
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            cur = conn.execute(f"INSERT INTO products ({cols}) VALUES ({marks})", tuple(data.values()))
            pid = int(cur.lastrowid)
        # specs + faqs come as parallel lists
        conn.execute("DELETE FROM product_specs WHERE product_id = ?", (pid,))
        for i, (label, value) in enumerate(zip(form.getlist("spec_label"), form.getlist("spec_value"))):
            if str(label).strip() and str(value).strip():
                conn.execute("INSERT INTO product_specs(product_id, label, value, sort) VALUES (?, ?, ?, ?)", (pid, str(label).strip()[:80], str(value).strip()[:500], i))
        conn.execute("DELETE FROM product_faqs WHERE product_id = ?", (pid,))
        for i, (q, a) in enumerate(zip(form.getlist("faq_q"), form.getlist("faq_a"))):
            if str(q).strip() and str(a).strip():
                conn.execute("INSERT INTO product_faqs(product_id, question, answer, sort) VALUES (?, ?, ?, ?)", (pid, str(q).strip()[:200], str(a).strip()[:2000], i))
        audit.log(conn, "product.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="product", target_id=pid, before=dict(before) if before else None, after=data, ip=ip(request))
    flash(request, "Product saved.")
    return redirect(f"/admin/products/{pid}")


@router.post("/products/{product_id}/variants/save")
async def variant_save(product_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    form = await request.form()
    vid = as_int(form.get("variant_id"))
    price = dollars_to_cents(form.get("price"))
    compare = dollars_to_cents(form.get("compare_at"))
    if price is None or price < 0:
        flash(request, "Price must be a dollar amount.", "error")
        return redirect(f"/admin/products/{product_id}")
    data = {
        "sku": str(form.get("sku") or "").strip().upper()[:40],
        "name": str(form.get("name") or "").strip()[:80],
        "units_per_pack": max(as_int(form.get("units_per_pack"), 1), 1),
        "price_cents": price,
        "compare_at_cents": compare if compare and compare > price else None,
        "stripe_price_id": str(form.get("stripe_price_id") or "").strip()[:80],
        "stripe_subscription_price_id": str(form.get("stripe_subscription_price_id") or "").strip()[:80],
        "subscription_discount_percent": (max(0, min(as_int(form.get("subscription_discount_percent")), 90)) if str(form.get("subscription_discount_percent") or "").strip() != "" else None),
        "low_stock_threshold": max(as_int(form.get("low_stock_threshold"), 5), 0),
        "weight_oz": float(form.get("weight_oz") or 0) or None,
        "is_active": 1 if form.get("is_active") else 0,
        "sort": as_int(form.get("sort")),
    }
    if not data["sku"] or not data["name"]:
        flash(request, "SKU and name are required.", "error")
        return redirect(f"/admin/products/{product_id}")
    with transaction(conn):
        before = one(conn, "SELECT * FROM variants WHERE id = ? AND product_id = ?", (vid, product_id)) if vid else None
        if vid and before:
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE variants SET {sets} WHERE id = ?", (*data.values(), vid))
        else:
            if one(conn, "SELECT 1 FROM variants WHERE sku = ?", (data["sku"],)):
                flash(request, "That SKU already exists.", "error")
                return redirect(f"/admin/products/{product_id}")
            stock = max(as_int(form.get("stock")), 0)
            cur = conn.execute(f"INSERT INTO variants (product_id, stock, {', '.join(data)}) VALUES (?, ?, {', '.join('?' for _ in data)})", (product_id, stock, *data.values()))
            vid = int(cur.lastrowid)
            if stock:
                conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, admin_id, note) VALUES (?, ?, 'restock', ?, 'initial')", (vid, stock, admin["id"]))
        if before and one(conn, "SELECT value FROM settings WHERE key = 'prices_are_placeholders'") and before["price_cents"] != price:
            conn.execute("UPDATE settings SET value = '0' WHERE key = 'prices_are_placeholders'")
        audit.log(conn, "variant.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="variant", target_id=vid, before=dict(before) if before else None, after=data, ip=ip(request))
    flash(request, "Variant saved.")
    return redirect(f"/admin/products/{product_id}")


@router.post("/products/{product_id}/variants/{variant_id}/stock")
def variant_stock(product_id: int, variant_id: int, request: Request, delta: str = Form("0"), note: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    d = as_int(delta)
    variant = one(conn, "SELECT * FROM variants WHERE id = ? AND product_id = ?", (variant_id, product_id))
    if not variant or d == 0:
        return redirect(f"/admin/products/{product_id}")
    with transaction(conn):
        cur = conn.execute("UPDATE variants SET stock = stock + ? WHERE id = ? AND stock + ? >= 0", (d, variant_id, d))
        if cur.rowcount == 0:
            flash(request, "That adjustment would take stock below zero.", "error")
            return redirect(f"/admin/products/{product_id}")
        conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, admin_id, note) VALUES (?, ?, ?, ?, ?)", (variant_id, d, "restock" if d > 0 else "adjust", admin["id"], note.strip()[:200]))
        audit.log(conn, "inventory.adjust", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="variant", target_id=variant_id, before={"stock": variant["stock"]}, after={"stock": variant["stock"] + d, "note": note.strip()[:200]}, ip=ip(request))
    flash(request, f"Stock adjusted by {d:+d}.")
    return redirect(f"/admin/products/{product_id}")


ALLOWED_IMAGE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@router.post("/products/{product_id}/images")
async def image_upload(product_id: int, request: Request, image: UploadFile = File(...), alt: str = Form(""), kind: str = Form("gallery"), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    product = one(conn, "SELECT id, slug FROM products WHERE id = ?", (product_id,))
    if not product:
        raise HTTPException(404)
    ext = ALLOWED_IMAGE.get(image.content_type or "")
    if not ext:
        flash(request, "Upload a JPEG, PNG or WebP.", "error")
        return redirect(f"/admin/products/{product_id}")
    raw = await image.read()
    if len(raw) > 12 * 1024 * 1024:
        flash(request, "Image is larger than 12 MB.", "error")
        return redirect(f"/admin/products/{product_id}")
    base = f"{product['slug']}-{uuid.uuid4().hex[:8]}"
    upload_dir = settings.media_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    src = upload_dir / f"{base}-source{ext}"
    src.write_bytes(raw)
    try:
        w, h = images.make_renditions(src, upload_dir, base)
    except Exception as exc:  # noqa: BLE001
        src.unlink(missing_ok=True)
        flash(request, f"That file could not be processed as an image ({exc}).", "error")
        return redirect(f"/admin/products/{product_id}")
    kind = kind if kind in ("hero", "gallery", "label") else "gallery"
    with transaction(conn):
        if kind == "hero":
            conn.execute("UPDATE product_images SET kind = 'gallery' WHERE product_id = ? AND kind = 'hero'", (product_id,))
        conn.execute("INSERT INTO product_images(product_id, base, source, alt, width, height, kind, sort) VALUES (?, ?, 'upload', ?, ?, ?, ?, (SELECT COALESCE(MAX(sort),0)+1 FROM product_images WHERE product_id = ?))", (product_id, base, alt.strip()[:200], w, h, kind, product_id))
        audit.log(conn, "product.image_upload", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="product", target_id=product_id, after={"base": base, "kind": kind}, ip=ip(request))
    flash(request, "Image uploaded and renditions built.")
    return redirect(f"/admin/products/{product_id}")


@router.post("/products/{product_id}/images/{image_id}/delete")
def image_delete(product_id: int, image_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM product_images WHERE id = ? AND product_id = ?", (image_id, product_id))
    if row:
        with transaction(conn):
            conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
            audit.log(conn, "product.image_delete", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="product", target_id=product_id, before=dict(row), ip=ip(request))
        if row["source"] == "upload":
            images.remove_renditions(settings.media_dir / "uploads", row["base"])
            for p in (settings.media_dir / "uploads").glob(f"{row['base']}-source.*"):
                p.unlink(missing_ok=True)
    return redirect(f"/admin/products/{product_id}")


@router.post("/products/{product_id}/images/{image_id}/hero")
def image_hero(product_id: int, image_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    with transaction(conn):
        conn.execute("UPDATE product_images SET kind = 'gallery' WHERE product_id = ? AND kind = 'hero'", (product_id,))
        conn.execute("UPDATE product_images SET kind = 'hero' WHERE id = ? AND product_id = ?", (image_id, product_id))
    return redirect(f"/admin/products/{product_id}")


ALLOWED_DOC = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}


@router.post("/products/{product_id}/documents")
async def document_upload(product_id: int, request: Request, doc: UploadFile = File(...), doc_kind: str = Form("sds"), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    product = one(conn, "SELECT id, slug FROM products WHERE id = ?", (product_id,))
    if not product:
        raise HTTPException(404)
    ext = ALLOWED_DOC.get(doc.content_type or "")
    if not ext or (doc_kind == "sds" and ext != ".pdf"):
        flash(request, "The SDS must be a PDF; the label can be a PDF, JPEG or PNG.", "error")
        return redirect(f"/admin/products/{product_id}")
    raw = await doc.read()
    if len(raw) > 20 * 1024 * 1024:
        flash(request, "File is larger than 20 MB.", "error")
        return redirect(f"/admin/products/{product_id}")
    folder = settings.media_dir / "sds"
    folder.mkdir(parents=True, exist_ok=True)
    rel = f"sds/{product['slug']}-{doc_kind}{ext}"
    (settings.media_dir / rel).write_bytes(raw)
    col = "sds_path" if doc_kind == "sds" else "label_path"
    with transaction(conn):
        conn.execute(f"UPDATE products SET {col} = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (rel, product_id))
        audit.log(conn, f"product.{doc_kind}_upload", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="product", target_id=product_id, after={"path": rel}, ip=ip(request))
    flash(request, f"{'SDS' if doc_kind == 'sds' else 'Label'} uploaded.")
    return redirect(f"/admin/products/{product_id}")


@router.get("/collections")
def collections(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, "SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.collection_id = c.id) AS product_count FROM collections c ORDER BY sort, id")]
    return arender(request, "admin/collections.html", {"collections": rows, "meta_title": "Collections"}, conn, admin)


@router.post("/collections/save")
def collection_save(request: Request, id: str = Form("0"), name: str = Form(""), description: str = Form(""), sort: str = Form("0"), is_active: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    cid = as_int(id)
    name = name.strip()[:80]
    if not name:
        return redirect("/admin/collections")
    with transaction(conn):
        if cid:
            conn.execute("UPDATE collections SET name = ?, description = ?, sort = ?, is_active = ? WHERE id = ?", (name, description.strip()[:500], as_int(sort), 1 if is_active else 0, cid))
        else:
            slug = _slugify(name)
            if one(conn, "SELECT 1 FROM collections WHERE slug = ?", (slug,)):
                slug = f"{slug}-{uuid.uuid4().hex[:4]}"
            conn.execute("INSERT INTO collections(slug, name, description, sort, is_active) VALUES (?, ?, ?, ?, ?)", (slug, name, description.strip()[:500], as_int(sort), 1 if is_active else 0))
        audit.log(conn, "collection.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="collection", target_id=cid or None, after={"name": name}, ip=ip(request))
    return redirect("/admin/collections")
