"""Audit log, email log, admin login attempts."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from ...db import all_rows, one
from ...deps import get_db, require_admin
from .common import arender

router = APIRouter()


@router.get("/audit")
def audit_log(request: Request, q: str = "", page: int = 1, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    per = 100
    page = max(page, 1)
    where = "WHERE (action LIKE ? OR actor_name LIKE ? OR target_type LIKE ?)" if q.strip() else ""
    like = f"%{q.strip()}%"
    params: tuple = (like, like, like) if q.strip() else ()
    rows = [dict(r) for r in all_rows(conn, f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, per, (page - 1) * per))]
    total = one(conn, f"SELECT COUNT(*) AS n FROM audit_log {where}", params)["n"]
    return arender(request, "admin/audit.html", {"rows": rows, "q": q, "page": page, "pages": max((total + per - 1) // per, 1), "meta_title": "Audit log"}, conn, admin)


@router.get("/emails")
def email_log(request: Request, status: str = "", page: int = 1, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    per = 100
    page = max(page, 1)
    where = "WHERE status = ?" if status else ""
    params: tuple = (status,) if status else ()
    rows = [dict(r) for r in all_rows(conn, f"SELECT * FROM email_log {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, per, (page - 1) * per))]
    counts = {r["status"]: r["n"] for r in all_rows(conn, "SELECT status, COUNT(*) AS n FROM email_log GROUP BY status")}
    total_sent = sum(v for k, v in counts.items() if k in ("sent", "delivered", "bounced", "complained"))
    bounce_rate = round(100 * (counts.get("bounced", 0) + counts.get("complained", 0)) / total_sent, 2) if total_sent else 0
    return arender(request, "admin/emails.html", {"rows": rows, "counts": counts, "bounce_rate": bounce_rate, "status": status, "page": page, "meta_title": "Email log"}, conn, admin)


@router.get("/logins")
def login_attempts(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, "SELECT * FROM admin_login_attempts ORDER BY id DESC LIMIT 300")]
    by_ip = [dict(r) for r in all_rows(conn, "SELECT ip, COUNT(*) AS n, SUM(success) AS ok FROM admin_login_attempts WHERE created_at >= datetime('now','-7 days') GROUP BY ip ORDER BY n DESC LIMIT 30")]
    return arender(request, "admin/logins.html", {"rows": rows, "by_ip": by_ip, "meta_title": "Admin sign-in attempts"}, conn, admin)
