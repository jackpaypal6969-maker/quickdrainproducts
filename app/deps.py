"""Request-scoped helpers: db connection, session, current customer/admin,
CSRF enforcement, template rendering."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, HTTPException, Request
from starlette.responses import HTMLResponse, RedirectResponse

from .config import settings
from .db import all_settings, connect, one
from .jinja_env import env
from .security import client_ip, ensure_csrf, parse_iso, utcnow, verify_csrf

ADMIN_SESSION_SECONDS = 12 * 3600
from .services import cart as cart_service


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_session(request: Request) -> dict:
    return request.state.session


def current_customer(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict | None:
    uid = request.state.session.get("uid")
    if not uid:
        return None
    row = one(conn, "SELECT * FROM customers WHERE id = ? AND is_active = 1 AND deleted_at IS NULL AND password_hash != ''", (uid,))
    if not row:
        request.state.session.pop("uid", None)
        return None
    return dict(row)


def require_customer(request: Request, customer: dict | None = Depends(current_customer)) -> dict:
    if not customer:
        request.state.session["next"] = str(request.url.path)
        raise HTTPException(status_code=302, headers={"Location": "/account/login"})
    return customer


def current_admin(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict | None:
    session = request.state.session
    aid = session.get("aid")
    if not aid or not session.get("a2"):
        return None
    started = parse_iso(session.get("a_at"))
    if not started or (utcnow() - started).total_seconds() > ADMIN_SESSION_SECONDS:
        for k in ("aid", "a_totp", "a2", "a_at"):
            session.pop(k, None)
        return None
    row = one(conn, "SELECT * FROM admin_users WHERE id = ? AND is_active = 1", (aid,))
    return dict(row) if row else None


def require_admin(request: Request, admin: dict | None = Depends(current_admin)) -> dict:
    if not admin:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    return admin


async def csrf_protect(request: Request) -> None:
    """Every state-changing request carries the session's CSRF token, either as
    the `csrf_token` form field or the X-CSRF-Token header (fetch)."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    token = request.headers.get("x-csrf-token")
    if not token:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data"):
            form = await request.form()
            token = form.get("csrf_token")
    if not verify_csrf(request.state.session, token):
        raise HTTPException(status_code=403, detail="Your session expired. Reload the page and try again.")


def render(request: Request, template: str, context: dict[str, Any] | None = None, status_code: int = 200, conn: sqlite3.Connection | None = None) -> HTMLResponse:
    session = request.state.session
    csrf_token = ensure_csrf(session)
    ctx: dict[str, Any] = {
        "request": request,
        "path": request.url.path,
        "csrf_token": csrf_token,
        "canonical": f"{settings.base_url}{request.url.path}",
        "cart_count": 0,
        "customer": None,
        "site": {},
        "promo_banner": "",
        "flash": session.pop("flash", None),
    }
    if conn is not None:
        ctx["cart_count"] = cart_service.count(conn, session)
        uid = session.get("uid")
        if uid:
            row = one(conn, "SELECT id, email, first_name, last_name FROM customers WHERE id = ? AND is_active = 1 AND password_hash != ''", (uid,))
            ctx["customer"] = dict(row) if row else None
        ctx["site"] = all_settings(conn)
        ctx["promo_banner"] = ctx["site"].get("promo_banner", "") if ctx["site"].get("promo_banner_enabled", "0") == "1" else ""
    ctx.update(context or {})
    html = env.get_template(template).render(**ctx)
    return HTMLResponse(html, status_code=status_code)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.state.session["flash"] = {"message": message, "kind": kind}


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)


def ip(request: Request) -> str:
    return client_ip(request)
