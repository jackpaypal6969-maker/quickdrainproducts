"""Orders, fulfilment, RMA queue."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ...config import settings
from ...db import all_rows, one, transaction
from ...deps import flash, get_db, ip, redirect, require_admin
from ...services import audit, emails, orders
from .common import arender

router = APIRouter()


@router.get("/orders")
def order_list(request: Request, status: str = "", q: str = "", page: int = 1, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    where, params = [], []
    if status and status in orders.ORDER_STATUSES:
        where.append("status = ?")
        params.append(status)
    if q.strip():
        where.append("(order_number LIKE ? OR email LIKE ? OR shipping_name LIKE ? OR tracking_number LIKE ?)")
        like = f"%{q.strip()}%"
        params += [like, like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    per = 40
    page = max(page, 1)
    rows = [dict(r) for r in all_rows(conn, f"SELECT * FROM orders {sql_where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, per, (page - 1) * per))]
    total = one(conn, f"SELECT COUNT(*) AS n FROM orders {sql_where}", tuple(params))["n"]
    return arender(request, "admin/orders.html", {"orders": rows, "status": status, "q": q, "page": page, "pages": max((total + per - 1) // per, 1), "total": total, "statuses": orders.ORDER_STATUSES, "meta_title": "Orders"}, conn, admin)


@router.get("/orders/{order_id}")
def order_detail(order_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    order = orders.get(conn, order_id)
    if not order:
        raise HTTPException(404)
    items = orders.items(conn, order_id)
    customer = one(conn, "SELECT id, email, first_name, last_name, password_hash != '' AS registered FROM customers WHERE id = ?", (order["customer_id"],)) if order["customer_id"] else None
    rmas = [dict(r) for r in all_rows(conn, "SELECT * FROM rma_requests WHERE order_id = ? ORDER BY id DESC", (order_id,))]
    log = [dict(r) for r in all_rows(conn, "SELECT * FROM audit_log WHERE target_type = 'order' AND target_id = ? ORDER BY id DESC", (order_id,))]
    mails = [dict(r) for r in all_rows(conn, "SELECT * FROM email_log WHERE related_type = 'order' AND related_id = ? ORDER BY id DESC", (order_id,))]
    stripe_prefix = "https://dashboard.stripe.com/" + ("" if settings.stripe_is_live else "test/")
    return arender(request, "admin/order_detail.html", {
        "order": order, "items": items, "customer": dict(customer) if customer else None, "rmas": rmas, "log": log, "mails": mails,
        "stripe_payment_url": f"{stripe_prefix}payments/{order['stripe_payment_intent_id']}" if order["stripe_payment_intent_id"] else "",
        "stripe_session_url": f"{stripe_prefix}checkout/sessions/{order['stripe_checkout_session_id']}" if order["stripe_checkout_session_id"] else "",
        "statuses": orders.ORDER_STATUSES, "meta_title": f"Order {order['order_number']}",
    }, conn, admin)


@router.post("/orders/{order_id}/status")
def order_status(order_id: int, request: Request, status: str = Form(""), tracking_number: str = Form(""), carrier: str = Form(""), notify: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    order = orders.get(conn, order_id)
    if not order:
        raise HTTPException(404)
    if status not in orders.ORDER_STATUSES:
        flash(request, "Unknown status.", "error")
        return redirect(f"/admin/orders/{order_id}")
    with transaction(conn):
        orders.set_status(conn, order, status, tracking=tracking_number, carrier=carrier, admin_id=admin["id"], admin_name=admin["username"], ip=ip(request))
        if status == "shipped" and notify:
            fresh = orders.get(conn, order_id)
            emails.send(conn, order["email"], "shipping_notification", f"Order {order['order_number']} is on its way", {"order": fresh, "items": orders.items(conn, order_id)}, related_type="order", related_id=order_id)
    flash(request, f"Order marked {status.replace('_', ' ')}." + (" Customer emailed." if status == "shipped" and notify else ""))
    return redirect(f"/admin/orders/{order_id}")


@router.post("/orders/{order_id}/note")
def order_note(order_id: int, request: Request, admin_note: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    order = orders.get(conn, order_id)
    if not order:
        raise HTTPException(404)
    with transaction(conn):
        conn.execute("UPDATE orders SET admin_note = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (admin_note.strip()[:4000], order_id))
        audit.log(conn, "order.note", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="order", target_id=order_id, before={"admin_note": order["admin_note"]}, after={"admin_note": admin_note.strip()[:4000]}, ip=ip(request))
    return redirect(f"/admin/orders/{order_id}")


@router.post("/orders/{order_id}/resend-confirmation")
def resend_confirmation(order_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    order = orders.get(conn, order_id)
    if not order:
        raise HTTPException(404)
    with transaction(conn):
        emails.send(conn, order["email"], "order_confirmation", f"Order {order['order_number']} confirmed — {settings.app_name}", {"order": order, "items": orders.items(conn, order_id)}, related_type="order", related_id=order_id)
        audit.log(conn, "order.resend_confirmation", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="order", target_id=order_id, ip=ip(request))
    flash(request, "Confirmation re-sent.")
    return redirect(f"/admin/orders/{order_id}")


# ------------------------------------------------------------------------ RMA
RMA_STATUSES = ("requested", "approved", "received", "refunded", "rejected")


@router.get("/rma")
def rma_queue(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, "SELECT r.*, o.order_number, o.total_cents FROM rma_requests r JOIN orders o ON o.id = r.order_id ORDER BY CASE r.status WHEN 'requested' THEN 0 WHEN 'approved' THEN 1 WHEN 'received' THEN 2 ELSE 3 END, r.created_at DESC")]
    return arender(request, "admin/rma.html", {"rmas": rows, "statuses": RMA_STATUSES, "meta_title": "Returns"}, conn, admin)


@router.post("/rma/{rma_id}")
def rma_update(rma_id: int, request: Request, status: str = Form(""), admin_note: str = Form(""), restock: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM rma_requests WHERE id = ?", (rma_id,))
    if not row or status not in RMA_STATUSES:
        raise HTTPException(404)
    with transaction(conn):
        resolved = "strftime('%Y-%m-%dT%H:%M:%SZ','now')" if status in ("refunded", "rejected") else "resolved_at"
        conn.execute(f"UPDATE rma_requests SET status = ?, admin_note = ?, resolved_at = {resolved} WHERE id = ?", (status, admin_note.strip()[:2000], rma_id))
        if status == "received" and restock and row["status"] != "received":
            for it in all_rows(conn, "SELECT variant_id, qty FROM order_items WHERE order_id = ? AND variant_id IS NOT NULL", (row["order_id"],)):
                conn.execute("UPDATE variants SET stock = stock + ? WHERE id = ?", (it["qty"], it["variant_id"]))
                conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, order_id, admin_id, note) VALUES (?, ?, 'rma', ?, ?, 'return received')", (it["variant_id"], it["qty"], row["order_id"], admin["id"]))
        audit.log(conn, "rma.update", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="rma", target_id=rma_id, before={"status": row["status"]}, after={"status": status, "restock": bool(restock)}, ip=ip(request))
    flash(request, f"Return marked {status}.")
    return redirect("/admin/rma")
