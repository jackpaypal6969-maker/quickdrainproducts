from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from starlette.responses import PlainTextResponse, Response

from ..config import settings
from ..db import all_rows
from ..deps import get_db

router = APIRouter()

STATIC_PATHS = ["/", "/products", "/blog", "/shipping-and-safety", "/contact", "/legal/terms", "/legal/privacy", "/legal/refunds", "/legal/shipping", "/legal/accessibility"]


@router.get("/sitemap.xml")
def sitemap(conn: sqlite3.Connection = Depends(get_db)):
    urls = [(p, "weekly", "0.8" if p == "/" else "0.5") for p in STATIC_PATHS]
    for r in all_rows(conn, "SELECT slug, updated_at FROM products WHERE is_active = 1"):
        urls.append((f"/products/{r['slug']}", "weekly", "1.0"))
    for r in all_rows(conn, "SELECT slug FROM posts WHERE status = 'published'"):
        urls.append((f"/blog/{r['slug']}", "monthly", "0.6"))
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, freq, prio in urls:
        loc = (settings.base_url + path).replace("&", "&amp;")
        body.append(f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq><priority>{prio}</priority></url>")
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml")


@router.get("/robots.txt")
def robots():
    lines = [
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /account",
        "Disallow: /cart",
        "Disallow: /checkout",
        "Disallow: /webhooks",
        "Disallow: /email/",
        "Allow: /",
        f"Sitemap: {settings.base_url}/sitemap.xml",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")
