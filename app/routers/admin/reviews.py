"""Review moderation queue. Approved reviews feed AggregateRating."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ...db import all_rows, one, transaction
from ...deps import flash, get_db, ip, redirect, require_admin
from ...security import iso
from ...services import audit
from .common import arender

router = APIRouter()


@router.get("/reviews")
def review_queue(request: Request, status: str = "pending", conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    status = status if status in ("pending", "approved", "rejected") else "pending"
    rows = [dict(r) for r in all_rows(conn, "SELECT r.*, p.name AS product_name FROM reviews r JOIN products p ON p.id = r.product_id WHERE r.status = ? ORDER BY r.created_at DESC LIMIT 300", (status,))]
    return arender(request, "admin/reviews.html", {"reviews": rows, "status": status, "meta_title": "Reviews"}, conn, admin)


@router.post("/reviews/{review_id}")
def review_moderate(review_id: int, request: Request, action: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM reviews WHERE id = ?", (review_id,))
    if not row:
        raise HTTPException(404)
    if action not in ("approve", "reject", "delete"):
        return redirect("/admin/reviews")
    with transaction(conn):
        if action == "delete":
            conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        else:
            conn.execute("UPDATE reviews SET status = ?, moderated_by = ?, moderated_at = ? WHERE id = ?", ("approved" if action == "approve" else "rejected", admin["id"], iso(), review_id))
        audit.log(conn, f"review.{action}", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="review", target_id=review_id, before={"status": row["status"]}, ip=ip(request))
    flash(request, f"Review {action}d." if action != "delete" else "Review deleted.")
    return redirect(f"/admin/reviews?status={row['status']}")
