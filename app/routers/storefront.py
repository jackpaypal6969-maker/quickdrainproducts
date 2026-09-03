"""Home, catalog, product detail, downloads, media."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import FileResponse

from ..config import settings
from ..db import all_rows, one
from ..deps import get_db, render
from ..services import catalog
from ..services.seo import breadcrumb_ld, faq_ld, organization_ld, product_ld

router = APIRouter()


def _posts(conn: sqlite3.Connection, limit: int = 3) -> list[dict]:
    return [dict(r) for r in all_rows(conn, "SELECT slug, title, excerpt, cover_base, published_at FROM posts WHERE status = 'published' ORDER BY published_at DESC LIMIT ?", (limit,))]


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    product = catalog.featured_product(conn)
    reviews = catalog.approved_reviews(conn, product["id"], limit=6) if product else []
    jsonld = [organization_ld()]
    if product:
        jsonld.append(product_ld(product, reviews))
        if product["faqs"]:
            jsonld.append(faq_ld(product["faqs"]))
    return render(request, "pages/home.html", {
        "product": product,
        "reviews": reviews,
        "posts": _posts(conn),
        "jsonld": jsonld,
        "meta_title": f"Quick Shot — monthly enzyme drain maintenance | {settings.app_name}",
        "meta_description": "Quick Shot is a natural drain enzyme dosed for monthly use on any drain. 4 fl oz per bottle. From Quick Drain, Long Island's diagnostics-first sewer and drain company.",
        "og_image": product["hero_image"]["src"] if product and product.get("hero_image") and product["hero_image"]["src"] else "",
    }, conn=conn)


@router.get("/products")
def product_list(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    products = catalog.list_products(conn)
    return render(request, "pages/products.html", {
        "products": products,
        "jsonld": [organization_ld(), breadcrumb_ld([("Home", "/"), ("Products", "/products")])],
        "meta_title": f"Drain maintenance products | {settings.app_name}",
        "meta_description": "Maintenance products from Quick Drain. Every claim on this site comes from the product label or safety data sheet.",
    }, conn=conn)


@router.get("/products/{slug}")
def product_detail(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    product = catalog.get_product(conn, slug)
    if not product:
        raise HTTPException(404)
    reviews = catalog.approved_reviews(conn, product["id"])
    breakdown = catalog.rating_breakdown(conn, product["id"])
    jsonld = [organization_ld(), product_ld(product, reviews), breadcrumb_ld([("Home", "/"), ("Products", "/products"), (product["name"], f"/products/{slug}")])]
    if product["faqs"]:
        jsonld.append(faq_ld(product["faqs"]))
    return render(request, "pages/product.html", {
        "product": product,
        "reviews": reviews,
        "breakdown": breakdown,
        "jsonld": jsonld,
        "meta_title": product["seo_title"] or f"{product['name']} — {product['tagline']} | {settings.app_name}",
        "meta_description": product["seo_description"] or product["tagline"],
        "og_image": product["hero_image"]["src"] if product.get("hero_image") and product["hero_image"]["src"] else "",
    }, conn=conn)


def _safe_media(rel: str) -> Path:
    root = settings.media_dir.resolve()
    try:
        target = (root / rel).resolve()
    except (ValueError, OSError):
        raise HTTPException(404)
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return target


@router.get("/products/{slug}/sds")
def product_sds(slug: str, conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT sds_path, name FROM products WHERE slug = ? AND is_active = 1", (slug,))
    if not row or not row["sds_path"]:
        raise HTTPException(404, "The safety data sheet has not been published here yet. Contact us and we will send it as soon as it is available.")
    path = _safe_media(row["sds_path"])
    return FileResponse(path, media_type="application/pdf", filename=f"{slug}-sds.pdf")


@router.get("/products/{slug}/label")
def product_label(slug: str, conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT label_path FROM products WHERE slug = ? AND is_active = 1", (slug,))
    if not row or not row["label_path"]:
        raise HTTPException(404)
    path = _safe_media(row["label_path"])
    return FileResponse(path, filename=f"{slug}-label{path.suffix}")


@router.get("/media/uploads/{rel:path}")
def media_upload(rel: str):
    if ".." in rel or rel.startswith("/"):
        raise HTTPException(404)
    path = _safe_media(f"uploads/{rel}")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


@router.get("/shipping-and-safety")
def shipping_and_safety(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    products = catalog.list_products(conn)
    return render(request, "pages/shipping_safety.html", {
        "products": products,
        "any_hazmat": any(p["hazmat"] for p in products),
        "meta_title": f"Shipping & safety | {settings.app_name}",
        "meta_description": "How Quick Shot ships, why it ships as an ordinary parcel, and where the safety data sheet lives.",
    }, conn=conn)
