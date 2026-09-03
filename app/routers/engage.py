"""Newsletter and back-in-stock endpoints — rate limited and domain filtered
from day one."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import JSONResponse

from ..db import get_setting, one, transaction
from ..deps import csrf_protect, flash, get_db, ip, redirect
from ..security import check_rate_limit, validate_email
from ..services import analytics, discounts, emails

router = APIRouter(dependencies=[Depends(csrf_protect)])


def _extra_blocklist(conn: sqlite3.Connection) -> set[str]:
    raw = get_setting(conn, "email_blocklist_extra", "")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def _respond(request: Request, ok: bool, message: str, back: str = "/"):
    if request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 400)
    flash(request, message, "ok" if ok else "error")
    return redirect(back)


@router.post("/newsletter")
def newsletter(request: Request, email: str = Form(""), source: str = Form("modal"), website: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    if website:
        return _respond(request, True, "Check your inbox for your code.")
    if not check_rate_limit(conn, "newsletter-ip", ip(request), limit=5, window_seconds=3600) or not check_rate_limit(conn, "newsletter-global", "all", limit=300, window_seconds=3600):
        return _respond(request, False, "Too many sign-ups right now. Try again later.")
    norm = validate_email(email, _extra_blocklist(conn))
    if not norm:
        return _respond(request, False, "Enter a real, non-disposable email address.")
    if get_setting(conn, "newsletter_enabled", "1") != "1":
        return _respond(request, False, "The newsletter is paused right now.")
    percent = int(get_setting(conn, "newsletter_discount_percent", "10") or 10)
    existing = one(conn, "SELECT id, welcome_code_id, unsubscribed_at FROM newsletter_subscribers WHERE email = ?", (norm,))
    with transaction(conn):
        code = discounts.issue_locked_code(conn, norm, "newsletter", percent=percent, days=30, prefix="WELCOME")
        if existing:
            conn.execute("UPDATE newsletter_subscribers SET unsubscribed_at = NULL, welcome_code_id = COALESCE(welcome_code_id, ?) WHERE id = ?", (code["id"], existing["id"]))
            conn.execute("DELETE FROM email_suppressions WHERE email = ? AND reason = 'unsubscribe'", (norm,))
        else:
            conn.execute("INSERT INTO newsletter_subscribers(email, source, welcome_code_id, ip) VALUES (?, ?, ?, ?)", (norm, source[:30], code["id"], ip(request)))
    if not existing or not existing["welcome_code_id"]:
        emails.send(conn, norm, "newsletter_welcome", f"Your {percent}% welcome code", {"code": code, "percent": percent}, category="marketing", related_type="newsletter", related_id=code["id"])
    analytics.capture(conn, "newsletter_signup", norm, {"source": source[:30], "code": code["code"]})
    return _respond(request, True, "Thanks — your welcome code is on its way." if not existing else "You are already on the list. Your code was re-sent if it was unused.")


@router.post("/notify-me")
def notify_me(request: Request, email: str = Form(""), variant_id: int = Form(0), website: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    if website:
        return _respond(request, True, "We will email you when it is back.")
    if not check_rate_limit(conn, "notify-ip", ip(request), limit=5, window_seconds=3600):
        return _respond(request, False, "Too many requests right now. Try again later.")
    norm = validate_email(email, _extra_blocklist(conn))
    if not norm:
        return _respond(request, False, "Enter a real, non-disposable email address.")
    variant = one(conn, "SELECT v.id, p.slug FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ? AND v.is_active = 1", (variant_id,))
    if not variant:
        return _respond(request, False, "That option was not found.")
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO stock_notifications(email, variant_id, ip) VALUES (?, ?, ?)", (norm, variant_id, ip(request)))
    analytics.capture(conn, "notify_me", norm, {"variant_id": variant_id})
    return _respond(request, True, "We will email you the moment it is back in stock.", back=f"/products/{variant['slug']}")
