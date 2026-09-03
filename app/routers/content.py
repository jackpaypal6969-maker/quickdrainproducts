"""Blog, legal pages, contact, guest order lookup + RMA, reviews, unsubscribe."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ..config import settings
from ..db import all_rows, one, transaction
from ..deps import csrf_protect, current_customer, flash, get_db, ip, redirect, render
from ..security import check_rate_limit, normalize_email, validate_email
from ..services import analytics, audit, emails, lifecycle, orders
from ..services.seo import article_ld, breadcrumb_ld

router = APIRouter(dependencies=[Depends(csrf_protect)])

LEGAL_PAGES = {
    "terms": "Terms of sale",
    "privacy": "Privacy policy",
    "refunds": "Refunds & returns",
    "shipping": "Shipping policy",
    "accessibility": "Accessibility statement",
}


# ---------------------------------------------------------------------- blog
@router.get("/blog")
def blog_index(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    posts = [dict(r) for r in all_rows(conn, "SELECT slug, title, excerpt, cover_base, published_at, author FROM posts WHERE status = 'published' ORDER BY published_at DESC")]
    return render(request, "blog/index.html", {"posts": posts, "jsonld": [breadcrumb_ld([("Home", "/"), ("Drain maintenance notes", "/blog")])], "meta_title": f"Drain maintenance notes | {settings.app_name}", "meta_description": "Plain-English notes on drain maintenance from Quick Drain: where a monthly dose fits, what it cannot do, and when to book a camera inspection."}, conn=conn)


@router.get("/blog/{slug}")
def blog_post(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT * FROM posts WHERE slug = ? AND status = 'published'", (slug,))
    if not row:
        raise HTTPException(404)
    post = dict(row)
    more = [dict(r) for r in all_rows(conn, "SELECT slug, title, excerpt FROM posts WHERE status = 'published' AND slug != ? ORDER BY published_at DESC LIMIT 3", (slug,))]
    return render(request, "blog/post.html", {"post": post, "more": more, "jsonld": [article_ld(post), breadcrumb_ld([("Home", "/"), ("Notes", "/blog"), (post["title"], f"/blog/{slug}")])], "meta_title": post["seo_title"] or f"{post['title']} | {settings.app_name}", "meta_description": post["seo_description"] or post["excerpt"]}, conn=conn)


# --------------------------------------------------------------------- legal
@router.get("/legal/{slug}")
def legal_page(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    if slug not in LEGAL_PAGES:
        raise HTTPException(404)
    row = one(conn, "SELECT * FROM pages WHERE slug = ? AND status = 'published'", (slug,))
    page = dict(row) if row else {"slug": slug, "title": LEGAL_PAGES[slug], "body": "", "updated_at": ""}
    return render(request, "legal/page.html", {"page": page, "default_template": f"legal/{slug}.html", "meta_title": f"{page['title']} | {settings.app_name}"}, conn=conn)


# ------------------------------------------------------------------- contact
@router.get("/contact")
def contact_form(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    return render(request, "pages/contact.html", {"prefill": customer or {}, "meta_title": f"Contact | {settings.app_name}"}, conn=conn)


@router.post("/contact")
def contact_submit(request: Request, name: str = Form(""), email: str = Form(""), phone: str = Form(""), order_number: str = Form(""), subject: str = Form(""), body: str = Form(""), website: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    if website:  # honeypot
        return redirect("/contact?sent=1")
    if not check_rate_limit(conn, "contact", ip(request), limit=5, window_seconds=3600):
        flash(request, "Too many messages from this connection. Call us instead: " + settings.phone_display, "error")
        return redirect("/contact")
    norm = validate_email(email)
    if not norm or not name.strip() or not body.strip():
        flash(request, "Name, a real email address and a message are required.", "error")
        return redirect("/contact")
    with transaction(conn):
        cur = conn.execute("INSERT INTO contact_messages(name, email, phone, order_number, subject, body, ip) VALUES (?, ?, ?, ?, ?, ?, ?)", (name.strip()[:100], norm, phone.strip()[:30], order_number.strip().upper()[:20], subject.strip()[:150], body.strip()[:5000], ip(request)))
        mid = int(cur.lastrowid)
    if settings.contact_inbox:
        emails.send(conn, settings.contact_inbox, "contact_notification", f"[Store contact] {subject.strip()[:80] or 'New message'} — {name.strip()[:60]}", {"name": name.strip(), "email": norm, "phone": phone.strip(), "order_number": order_number.strip().upper(), "subject": subject.strip(), "body": body.strip()}, related_type="contact", related_id=mid, reply_to=norm)
    flash(request, "Message received. We reply within one business day.")
    return redirect("/contact?sent=1")


# --------------------------------------------------------------- order lookup
@router.get("/orders/lookup")
def lookup_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return render(request, "pages/order_lookup.html", {"order": None, "meta_title": f"Find your order | {settings.app_name}"}, conn=conn)


@router.post("/orders/lookup")
def lookup(request: Request, order_number: str = Form(""), email: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    if not check_rate_limit(conn, "lookup", ip(request), limit=10, window_seconds=900):
        flash(request, "Too many lookups. Try again in a few minutes.", "error")
        return redirect("/orders/lookup")
    order = orders.get_by_number_and_email(conn, order_number, email)
    if not order:
        flash(request, "No order matched that number and email.", "error")
        return redirect("/orders/lookup")
    items = orders.items(conn, order["id"])
    rmas = [dict(r) for r in all_rows(conn, "SELECT * FROM rma_requests WHERE order_id = ? ORDER BY id DESC", (order["id"],))]
    return render(request, "pages/order_lookup.html", {"order": order, "items": items, "rmas": rmas, "lookup_email": normalize_email(email), "meta_title": f"Order {order['order_number']}"}, conn=conn)


@router.post("/orders/lookup/rma")
def lookup_rma(request: Request, order_number: str = Form(""), email: str = Form(""), reason: str = Form(""), details: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    if not check_rate_limit(conn, "rma-guest", ip(request), limit=5, window_seconds=86400):
        flash(request, "Too many requests. We will reply to your existing request by email.", "error")
        return redirect("/orders/lookup")
    order = orders.get_by_number_and_email(conn, order_number, email)
    if not order:
        flash(request, "No order matched that number and email.", "error")
        return redirect("/orders/lookup")
    reason = reason.strip()[:120] or "Return requested"
    with transaction(conn):
        cur = conn.execute("INSERT INTO rma_requests(order_id, email, reason, details) VALUES (?, ?, ?, ?)", (order["id"], order["email"], reason, details.strip()[:4000]))
        audit.log(conn, "rma.requested", actor_type="customer", target_type="rma", target_id=int(cur.lastrowid), after={"order": order["order_number"]}, ip=ip(request))
    if settings.contact_inbox:
        emails.send(conn, settings.contact_inbox, "rma_notification", f"Return request on {order['order_number']}", {"order": order, "reason": reason, "details": details.strip()[:4000]}, related_type="order", related_id=order["id"])
    flash(request, "Return request received. We reply within one business day.")
    return redirect("/orders/lookup")


# ------------------------------------------------------------------- reviews
@router.get("/reviews/new/{number}/{token}")
def review_form(number: str, token: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT * FROM orders WHERE order_number = ?", (number.upper(),))
    if not row or not lifecycle.reorder_token_valid(dict(row), token):
        raise HTTPException(404)
    order = dict(row)
    items = orders.items(conn, order["id"])
    return render(request, "pages/review_form.html", {"order": order, "items": items, "token": token, "verified": True, "meta_title": "Write a review"}, conn=conn)


@router.post("/reviews")
def review_submit(request: Request, product_id: int = Form(0), rating: int = Form(0), title: str = Form(""), body: str = Form(""), author_name: str = Form(""), email: str = Form(""), order_number: str = Form(""), token: str = Form(""), website: str = Form(""), conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    if website:
        return redirect("/")
    if not check_rate_limit(conn, "review", ip(request), limit=3, window_seconds=86400):
        flash(request, "You have submitted several reviews today. Thank you — they are in the queue.", "error")
        return redirect("/")
    product = one(conn, "SELECT id, slug FROM products WHERE id = ? AND is_active = 1", (product_id,))
    if not product or not (1 <= rating <= 5) or not body.strip() or not author_name.strip():
        flash(request, "Pick a star rating, add your name and a few words.", "error")
        return redirect(f"/products/{product['slug']}#reviews" if product else "/")
    verified = 0
    order_id = None
    if order_number and token:
        row = one(conn, "SELECT * FROM orders WHERE order_number = ?", (order_number.upper(),))
        if row and lifecycle.reorder_token_valid(dict(row), token):
            verified = 1
            order_id = row["id"]
            email = row["email"]
    elif customer:
        prior = one(conn, "SELECT o.id FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.customer_id = ? AND oi.product_id = ? LIMIT 1", (customer["id"], product_id))
        if prior:
            verified = 1
            order_id = prior["id"]
        email = customer["email"]
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO reviews(product_id, customer_id, order_id, author_name, email, rating, title, body, is_verified, ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, customer["id"] if customer else None, order_id, author_name.strip()[:60], normalize_email(email)[:254], rating, title.strip()[:120], body.strip()[:3000], verified, ip(request)),
        )
        audit.log(conn, "review.submitted", actor_type="customer", actor_id=customer["id"] if customer else None, target_type="review", target_id=int(cur.lastrowid), ip=ip(request))
    analytics.capture(conn, "review_submitted", normalize_email(email) or ip(request), {"product_id": product_id, "rating": rating, "verified": verified})
    flash(request, "Thanks. Your review is in the moderation queue and will appear once approved.")
    return redirect(f"/products/{product['slug']}#reviews")


# --------------------------------------------------------------- unsubscribe
@router.get("/email/unsubscribe/{token}")
def unsubscribe_form(token: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    email = emails.email_from_unsubscribe_token(token)
    if not email:
        raise HTTPException(404)
    return render(request, "pages/unsubscribe.html", {"email": email, "token": token, "done": False, "meta_title": "Unsubscribe"}, conn=conn)


@router.post("/email/unsubscribe/{token}")
def unsubscribe(token: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    email = emails.email_from_unsubscribe_token(token)
    if not email:
        raise HTTPException(404)
    with transaction(conn):
        emails.suppress(conn, email, "unsubscribe", "link")
        conn.execute("UPDATE customers SET marketing_opt_in = 0 WHERE email_norm = ?", (email,))
    return render(request, "pages/unsubscribe.html", {"email": email, "token": token, "done": True, "meta_title": "Unsubscribed"}, conn=conn)
