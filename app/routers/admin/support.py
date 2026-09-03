"""Contact inbox."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ...db import all_rows, one, transaction
from ...deps import get_db, ip, redirect, require_admin
from ...services import audit
from .common import arender

router = APIRouter()


@router.get("/contact")
def inbox(request: Request, status: str = "new", conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    status = status if status in ("new", "replied", "closed") else "new"
    rows = [dict(r) for r in all_rows(conn, "SELECT * FROM contact_messages WHERE status = ? ORDER BY created_at DESC LIMIT 300", (status,))]
    return arender(request, "admin/contact.html", {"messages": rows, "status": status, "meta_title": "Contact inbox"}, conn, admin)


@router.post("/contact/{message_id}")
def inbox_update(message_id: int, request: Request, status: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT status FROM contact_messages WHERE id = ?", (message_id,))
    if not row or status not in ("new", "replied", "closed"):
        raise HTTPException(404)
    with transaction(conn):
        conn.execute("UPDATE contact_messages SET status = ? WHERE id = ?", (status, message_id))
        audit.log(conn, "contact.status", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="contact", target_id=message_id, before={"status": row["status"]}, after={"status": status}, ip=ip(request))
    return redirect(f"/admin/contact?status={row['status']}")
