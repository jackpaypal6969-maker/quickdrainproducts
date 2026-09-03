"""Subscribe-and-save. Stripe bills each cycle; this module turns the paid
invoice into a fulfilment order (idempotent on event.id), keeps the local
subscription row in sync with Stripe, and never touches money itself."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from ..db import all_rows, one, transaction
from ..security import iso, normalize_email, order_number
from . import analytics, audit, emails, orders

log = logging.getLogger("qd.subscriptions")

STATUS_LABELS = {
    "active": "Active",
    "trialing": "Active",
    "past_due": "Payment problem",
    "unpaid": "Payment problem",
    "canceling": "Ends after this period",
    "canceled": "Canceled",
    "paused": "Paused",
    "incomplete": "Not started",
    "incomplete_expired": "Not started",
}


def _ts(value) -> str | None:
    try:
        return iso(datetime.fromtimestamp(int(value), tz=timezone.utc)) if value else None
    except (TypeError, ValueError, OSError):
        return None


def register_from_checkout(conn: sqlite3.Connection, *, stripe_subscription_id: str, stripe_customer_id: str, customer_id: int | None, email: str, lines: list[dict], interval_months: int, shipping_cents: int, order_id: int) -> None:
    """Called inside the order-creation transaction for a subscription checkout."""
    sub_lines = []
    for ln in lines:
        if int(ln.get("s") or 0):
            v = one(conn, "SELECT v.sku, v.name, p.name AS product_name FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?", (int(ln.get("v") or 0),))
            sub_lines.append({"v": int(ln.get("v") or 0), "q": int(ln.get("q") or 1), "p": int(ln.get("p") or 0), "sku": v["sku"] if v else "", "name": v["product_name"] if v else "Item", "variant": v["name"] if v else ""})
    if not sub_lines:
        # Fallback: every product line is recurring in a subscription checkout; flag it for a human.
        sub_lines = [{"v": int(ln.get("v") or 0), "q": int(ln.get("q") or 1), "p": int(ln.get("p") or 0), "sku": ln.get("sku", ""), "name": ln.get("name", "Item"), "variant": ln.get("variant", "")} for ln in lines if int(ln.get("v") or 0)]
        audit.log(conn, "subscription.lines_inferred", actor_type="webhook", target_type="order", target_id=order_id, after={"subscription": stripe_subscription_id, "lines": sub_lines})
    conn.execute(
        """INSERT INTO subscriptions(customer_id, email, variant_id, stripe_subscription_id, stripe_customer_id, status, interval_days, interval_months, lines, shipping_cents, last_order_id, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(stripe_subscription_id) DO UPDATE SET customer_id = COALESCE(excluded.customer_id, subscriptions.customer_id), lines = excluded.lines, shipping_cents = excluded.shipping_cents, last_order_id = excluded.last_order_id, updated_at = excluded.updated_at""",
        (customer_id, normalize_email(email), sub_lines[0]["v"] if sub_lines else None, stripe_subscription_id, stripe_customer_id or "", max(interval_months, 1) * 30, max(interval_months, 1), json.dumps(sub_lines), shipping_cents, order_id, iso()),
    )


def sync_from_stripe(conn: sqlite3.Connection, sub: dict, *, deleted: bool = False) -> str:
    sid = sub.get("id", "")
    if not sid:
        return "subscription: no id"
    status = "canceled" if deleted else str(sub.get("status") or "active")
    cancel_at_end = 1 if sub.get("cancel_at_period_end") else 0
    if status == "active" and cancel_at_end:
        status = "canceling"
    period_end = _ts(sub.get("current_period_end"))
    if period_end is None:
        items = ((sub.get("items") or {}).get("data")) or []
        if items:
            period_end = _ts(items[0].get("current_period_end"))
    customer = sub.get("customer")
    customer_id = customer if isinstance(customer, str) else (customer or {}).get("id", "")
    with transaction(conn):
        row = one(conn, "SELECT id, status FROM subscriptions WHERE stripe_subscription_id = ?", (sid,))
        if not row:
            # Created outside Checkout (e.g. in the dashboard): keep a record so the account page can show it.
            meta = sub.get("metadata") or {}
            email = normalize_email(meta.get("email") or "")
            if not email:
                return "subscription: unknown and no email"
            conn.execute(
                "INSERT INTO subscriptions(customer_id, email, stripe_subscription_id, stripe_customer_id, status, interval_months, cancel_at_period_end, next_renewal_at, updated_at) VALUES ((SELECT id FROM customers WHERE email_norm = ?), ?, ?, ?, ?, ?, ?, ?, ?)",
                (email, email, sid, customer_id or "", status, int(meta.get("interval_months") or 1), cancel_at_end, period_end, iso()),
            )
            return f"subscription recorded ({status})"
        conn.execute(
            "UPDATE subscriptions SET status = ?, cancel_at_period_end = ?, next_renewal_at = COALESCE(?, next_renewal_at), stripe_customer_id = CASE WHEN ? != '' THEN ? ELSE stripe_customer_id END, canceled_at = CASE WHEN ? = 'canceled' THEN COALESCE(canceled_at, ?) ELSE canceled_at END, updated_at = ? WHERE id = ?",
            (status, cancel_at_end, period_end, customer_id or "", customer_id or "", status, iso(), iso(), row["id"]),
        )
        if row["status"] != status:
            audit.log(conn, "subscription.status", actor_type="webhook", target_type="subscription", target_id=row["id"], before={"status": row["status"]}, after={"status": status})
    return f"subscription {status}"


def _invoice_period_end(invoice: dict) -> str | None:
    lines = ((invoice.get("lines") or {}).get("data")) or []
    ends = [((ln.get("period") or {}).get("end")) for ln in lines]
    ends = [e for e in ends if e]
    return _ts(max(ends)) if ends else None


def handle_invoice_paid(conn: sqlite3.Connection, invoice: dict, event_id: str) -> str:
    sub_ref = invoice.get("subscription")
    if isinstance(sub_ref, dict):
        sub_ref = sub_ref.get("id")
    if not sub_ref:
        parent = invoice.get("parent") or {}
        sub_ref = ((parent.get("subscription_details") or {}).get("subscription")) if isinstance(parent, dict) else None
        if isinstance(sub_ref, dict):
            sub_ref = sub_ref.get("id")
    if not sub_ref:
        return "invoice: not a subscription"
    reason = invoice.get("billing_reason") or ""
    period_end = _invoice_period_end(invoice)
    if reason == "subscription_create":
        # First invoice: checkout.session.completed already created the order.
        with transaction(conn):
            conn.execute("UPDATE subscriptions SET next_renewal_at = COALESCE(?, next_renewal_at), updated_at = ? WHERE stripe_subscription_id = ?", (period_end, iso(), sub_ref))
        return "invoice: first cycle (order came from checkout)"
    if reason not in {"subscription_cycle", "subscription_update", "subscription_threshold"}:
        return f"invoice: ignored ({reason or 'manual'})"
    order = create_renewal_order(conn, invoice, sub_ref, event_id, period_end)
    if order is None:
        return "invoice: duplicate"
    if order == "unknown":
        return "invoice: subscription unknown locally"
    return f"renewal order {order['order_number']}"


def handle_invoice_failed(conn: sqlite3.Connection, invoice: dict, event_id: str) -> str:
    sub_ref = invoice.get("subscription")
    if isinstance(sub_ref, dict):
        sub_ref = sub_ref.get("id")
    if not sub_ref:
        return "invoice failed: not a subscription"
    with transaction(conn):
        try:
            conn.execute("INSERT INTO processed_events(event_id, source, type) VALUES (?, 'stripe', 'invoice.payment_failed')", (event_id,))
        except sqlite3.IntegrityError:
            return "duplicate"
        row = one(conn, "SELECT id FROM subscriptions WHERE stripe_subscription_id = ?", (sub_ref,))
        if row:
            conn.execute("UPDATE subscriptions SET status = 'past_due', updated_at = ? WHERE id = ?", (iso(), row["id"]))
            audit.log(conn, "subscription.payment_failed", actor_type="webhook", target_type="subscription", target_id=row["id"], after={"invoice": invoice.get("id")})
    return "subscription past_due"


def create_renewal_order(conn: sqlite3.Connection, invoice: dict, stripe_subscription_id: str, event_id: str, period_end: str | None):
    """One fulfilment order per paid renewal invoice. Idempotent on the event id
    and on the invoice id (stored as the order's checkout session key)."""
    with transaction(conn):
        try:
            conn.execute("INSERT INTO processed_events(event_id, source, type) VALUES (?, 'stripe', 'invoice.paid')", (event_id,))
        except sqlite3.IntegrityError:
            return None
        sub = one(conn, "SELECT * FROM subscriptions WHERE stripe_subscription_id = ?", (stripe_subscription_id,))
        if not sub:
            return "unknown"
        session_key = f"inv_{invoice.get('id', '')}"
        existing = one(conn, "SELECT * FROM orders WHERE stripe_checkout_session_id = ?", (session_key,))
        if existing:
            return dict(existing)
        try:
            lines = json.loads(sub["lines"] or "[]")
        except json.JSONDecodeError:
            lines = []
        last = one(conn, "SELECT * FROM orders WHERE id = ?", (sub["last_order_id"],)) if sub["last_order_id"] else None
        ship_details = invoice.get("customer_shipping") or {}
        addr = (ship_details.get("address") or {}) if isinstance(ship_details, dict) else {}
        shipping = {
            "shipping_name": (ship_details.get("name") if isinstance(ship_details, dict) else "") or (last["shipping_name"] if last else "") or invoice.get("customer_name") or "",
            "shipping_line1": addr.get("line1") or (last["shipping_line1"] if last else ""),
            "shipping_line2": addr.get("line2") or (last["shipping_line2"] if last else ""),
            "shipping_city": addr.get("city") or (last["shipping_city"] if last else ""),
            "shipping_state": addr.get("state") or (last["shipping_state"] if last else ""),
            "shipping_postal_code": addr.get("postal_code") or (last["shipping_postal_code"] if last else ""),
            "shipping_country": addr.get("country") or (last["shipping_country"] if last else "US"),
            "shipping_phone": (ship_details.get("phone") if isinstance(ship_details, dict) else "") or (last["shipping_phone"] if last else ""),
        }
        total = int(invoice.get("amount_paid") or invoice.get("total") or 0)
        tax = int(invoice.get("tax") or 0)
        shipping_cents = int(sub["shipping_cents"] or 0)
        subtotal = sum(int(ln.get("p") or 0) * int(ln.get("q") or 1) for ln in lines)
        discount = max(0, subtotal + shipping_cents + tax - total) if total else 0
        pi = invoice.get("payment_intent")
        pi_id = pi if isinstance(pi, str) else (pi or {}).get("id", "")
        number = order_number()
        while one(conn, "SELECT 1 FROM orders WHERE order_number = ?", (number,)):
            number = order_number()
        email = normalize_email(invoice.get("customer_email") or sub["email"])
        customer_id = sub["customer_id"] or (one(conn, "SELECT id FROM customers WHERE email_norm = ?", (email,)) or {"id": None})["id"]
        data = {
            "order_number": number, "customer_id": customer_id, "email": email, "status": "paid",
            "stripe_checkout_session_id": session_key, "stripe_payment_intent_id": pi_id or "",
            "stripe_customer_id": sub["stripe_customer_id"] or "", "stripe_subscription_id": stripe_subscription_id,
            "currency": invoice.get("currency") or "usd", "subtotal_cents": subtotal, "discount_cents": discount,
            "shipping_cents": shipping_cents, "tax_cents": tax, "total_cents": total, **shipping,
            "utm_source": "subscription", "utm_medium": "renewal", "utm_campaign": f"every-{sub['interval_months']}m",
        }
        cur = conn.execute(f"INSERT INTO orders ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})", tuple(data.values()))
        order_id = int(cur.lastrowid)
        oversold = []
        for ln in lines:
            vid = int(ln.get("v") or 0) or None
            qty = max(int(ln.get("q") or 1), 1)
            variant = one(conn, "SELECT v.*, p.name AS product_name, p.dose_interval_days, p.drains_per_unit FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?", (vid,)) if vid else None
            conn.execute(
                "INSERT INTO order_items(order_id, variant_id, product_id, sku, product_name, variant_name, qty, unit_price_cents, line_total_cents, units_per_pack, dose_interval_days, drains_per_unit, is_subscription) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (order_id, vid, variant["product_id"] if variant else None, variant["sku"] if variant else ln.get("sku", ""), variant["product_name"] if variant else ln.get("name", "Item"), variant["name"] if variant else ln.get("variant", ""), qty, int(ln.get("p") or 0), int(ln.get("p") or 0) * qty, variant["units_per_pack"] if variant else 1, variant["dose_interval_days"] if variant else 30, variant["drains_per_unit"] if variant else 1),
            )
            if variant:
                dec = conn.execute("UPDATE variants SET stock = stock - ? WHERE id = ? AND stock >= ?", (qty, vid, qty))
                if dec.rowcount == 1:
                    conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, order_id) VALUES (?, ?, 'order', ?)", (vid, -qty, order_id))
                else:
                    oversold.append(f"{variant['sku']} x{qty} (stock {variant['stock']})")
        if not lines:
            conn.execute("UPDATE orders SET status = 'on_hold', admin_note = ? WHERE id = ?", ("LINES MISSING: renewal invoice paid but the subscription has no recorded items — check Stripe and add lines by hand", order_id))
            audit.log(conn, "order.lines_missing", actor_type="webhook", target_type="order", target_id=order_id, after={"subscription": stripe_subscription_id})
        if oversold:
            conn.execute("UPDATE orders SET status = 'on_hold', admin_note = ? WHERE id = ?", ("STOCK SHORT AT RENEWAL: " + "; ".join(oversold), order_id))
            audit.log(conn, "order.stock_short", actor_type="webhook", target_type="order", target_id=order_id, after={"oversold": oversold})
        conn.execute("UPDATE subscriptions SET last_order_id = ?, status = CASE WHEN status IN ('past_due','unpaid') THEN 'active' ELSE status END, next_renewal_at = COALESCE(?, next_renewal_at), updated_at = ? WHERE id = ?", (order_id, period_end, iso(), sub["id"]))
        audit.log(conn, "order.created", actor_type="webhook", target_type="order", target_id=order_id, after={"number": number, "total_cents": total, "renewal_of": stripe_subscription_id, "event": event_id})
        order = dict(one(conn, "SELECT * FROM orders WHERE id = ?", (order_id,)))
    try:
        emails.send(conn, order["email"], "order_confirmation", f"Your Quick Shot renewal is on the way — order {order['order_number']}", {"order": order, "items": orders.items(conn, order["id"]), "renewal": True}, related_type="order", related_id=order["id"])
    except Exception:  # noqa: BLE001
        log.exception("renewal confirmation failed for %s", order["id"])
    analytics.capture(conn, "checkout_completed", order["email"], {"order_number": order["order_number"], "value_cents": order["total_cents"], "renewal": True, "utm_source": "subscription"})
    return order


def for_customer(conn: sqlite3.Connection, customer_id: int) -> list[dict]:
    out = []
    for r in all_rows(conn, "SELECT * FROM subscriptions WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,)):
        d = dict(r)
        try:
            d["line_list"] = json.loads(d.get("lines") or "[]")
        except json.JSONDecodeError:
            d["line_list"] = []
        d["label"] = STATUS_LABELS.get(d["status"], d["status"].replace("_", " ").title())
        d["is_live"] = d["status"] in {"active", "trialing", "past_due", "canceling", "unpaid"}
        d["can_cancel"] = d["status"] in {"active", "trialing", "past_due", "unpaid"}
        d["can_resume"] = d["status"] == "canceling"
        out.append(d)
    return out
