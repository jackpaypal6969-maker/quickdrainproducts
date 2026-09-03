"""Customers: search, detail, soft-deactivate. Never hard-delete a customer with orders."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ...db import all_rows, one, transaction
from ...deps import flash, get_db, ip, redirect, require_admin
from ...security import iso
from ...services import audit, emails
from .common import arender

router = APIRouter()


@router.get("/customers")
def customer_list(request: Request, q: str = "", page: int = 1, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    per = 40
    page = max(page, 1)
    like = f"%{q.strip()}%"
    where = "WHERE (email LIKE ? OR first_name LIKE ? OR last_name LIKE ?)" if q.strip() else ""
    params: tuple = (like, like, like) if q.strip() else ()
    rows = [dict(r) for r in all_rows(conn, f"""SELECT c.id, c.email, c.first_name, c.last_name, c.phone, c.marketing_opt_in, c.is_active, c.created_at, c.last_login_at, c.deleted_at,
              (c.password_hash != '') AS registered, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count,
              (SELECT COALESCE(SUM(total_cents - refunded_cents),0) FROM orders o WHERE o.customer_id = c.id) AS lifetime_cents
              FROM customers c {where} ORDER BY c.created_at DESC LIMIT ? OFFSET ?""", (*params, per, (page - 1) * per))]
    total = one(conn, f"SELECT COUNT(*) AS n FROM customers {where}", params)["n"]
    return arender(request, "admin/customers.html", {"customers": rows, "q": q, "page": page, "pages": max((total + per - 1) // per, 1), "total": total, "meta_title": "Customers"}, conn, admin)


@router.get("/customers/{customer_id}")
def customer_detail(customer_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM customers WHERE id = ?", (customer_id,))
    if not row:
        raise HTTPException(404)
    customer = dict(row)
    orders_ = [dict(r) for r in all_rows(conn, "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,))]
    addresses = [dict(r) for r in all_rows(conn, "SELECT * FROM addresses WHERE customer_id = ? ORDER BY is_default DESC", (customer_id,))]
    suppression = one(conn, "SELECT reason, created_at FROM email_suppressions WHERE email = ?", (customer["email_norm"],))
    mails = [dict(r) for r in all_rows(conn, "SELECT * FROM email_log WHERE to_email = ? ORDER BY id DESC LIMIT 30", (customer["email_norm"],))]
    subs = [dict(r) for r in all_rows(conn, "SELECT * FROM subscriptions WHERE customer_id = ?", (customer_id,))]
    return arender(request, "admin/customer_detail.html", {"c": customer, "orders": orders_, "addresses": addresses, "suppression": dict(suppression) if suppression else None, "mails": mails, "subscriptions": subs, "meta_title": customer["email"]}, conn, admin)


@router.post("/customers/{customer_id}/deactivate")
def customer_deactivate(customer_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM customers WHERE id = ?", (customer_id,))
    if not row:
        raise HTTPException(404)
    with transaction(conn):
        has_orders = one(conn, "SELECT 1 FROM orders WHERE customer_id = ? LIMIT 1", (customer_id,))
        if has_orders:
            # Soft delete only: order history must keep its customer link.
            conn.execute("UPDATE customers SET is_active = 0, deleted_at = ?, marketing_opt_in = 0 WHERE id = ?", (iso(), customer_id))
            emails.suppress(conn, row["email_norm"], "manual", "deactivated by admin")
            action = "customer.deactivate"
        else:
            conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
            action = "customer.delete"
        audit.log(conn, action, actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="customer", target_id=customer_id, before={"email": row["email"], "has_orders": bool(has_orders)}, ip=ip(request))
    flash(request, "Customer deactivated (orders kept)." if has_orders else "Customer deleted (no orders).")
    return redirect("/admin/customers")


@router.post("/customers/{customer_id}/reactivate")
def customer_reactivate(customer_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    with transaction(conn):
        conn.execute("UPDATE customers SET is_active = 1, deleted_at = NULL WHERE id = ?", (customer_id,))
        audit.log(conn, "customer.reactivate", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="customer", target_id=customer_id, ip=ip(request))
    return redirect(f"/admin/customers/{customer_id}")


@router.post("/customers/{customer_id}/suppression")
def customer_suppression(customer_id: int, request: Request, action: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT email_norm FROM customers WHERE id = ?", (customer_id,))
    if not row:
        raise HTTPException(404)
    with transaction(conn):
        if action == "clear":
            conn.execute("DELETE FROM email_suppressions WHERE email = ?", (row["email_norm"],))
        else:
            emails.suppress(conn, row["email_norm"], "manual", "admin")
        audit.log(conn, "customer.suppression", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="customer", target_id=customer_id, after={"action": action}, ip=ip(request))
    return redirect(f"/admin/customers/{customer_id}")
