"""Cron-driven lifecycle email: abandoned cart nudge, reorder reminders on the
dose interval, review invitations, back-in-stock notices. Each function is
idempotent — it marks what it sent so a second run within the hour sends nothing."""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from ..config import settings
from ..db import all_rows, transaction
from ..security import iso, parse_iso, utcnow
from . import cart as cart_service
from . import discounts, emails

log = logging.getLogger("qd.lifecycle")


def abandoned_carts(conn: sqlite3.Connection, limit: int = 100) -> int:
    cutoff = iso(utcnow() - timedelta(hours=settings.abandoned_cart_hours))
    rows = all_rows(
        conn,
        """SELECT c.* FROM carts c
           WHERE c.email != '' AND c.checkout_started_at IS NOT NULL AND c.checkout_started_at < ?
             AND c.converted_order_id IS NULL AND c.abandoned_email_sent_at IS NULL
             AND EXISTS (SELECT 1 FROM cart_items ci WHERE ci.cart_id = c.id)
             AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.email = c.email AND o.created_at > c.checkout_started_at)
           ORDER BY c.checkout_started_at LIMIT ?""",
        (cutoff, limit),
    )
    sent = 0
    for row in rows:
        cart = dict(row)
        with transaction(conn):
            code = discounts.issue_locked_code(conn, cart["email"], "abandoned", percent=10, days=7, prefix="BACK")
            tot = cart_service.totals(conn, cart, cart["email"])
            conn.execute("UPDATE carts SET abandoned_email_sent_at = ? WHERE id = ?", (iso(), cart["id"]))
        emails.send(
            conn, cart["email"], "abandoned_cart", "You left Drain Shot in your cart",
            {"cart": cart, "totals": tot, "code": code, "resume_url": f"{settings.base_url}/cart/resume/{cart['token']}"},
            category="marketing", related_type="cart", related_id=cart["id"],
        )
        sent += 1
    return sent


def coverage_days(items: list[dict]) -> int:
    """How long the order covers one drain: units × interval, per the label.
    Different products are dosed concurrently, so the reminder follows the one
    that runs out first."""
    per_product: dict = {}
    for it in items:
        key = it.get("product_id") or it.get("sku") or id(it)
        per_product[key] = per_product.get(key, 0) + int(it["qty"]) * int(it["units_per_pack"]) * int(it["dose_interval_days"]) * max(int(it["drains_per_unit"]), 1)
    return min(per_product.values()) if per_product else 0


def reorder_reminders(conn: sqlite3.Connection, limit: int = 200) -> int:
    rows = all_rows(
        conn,
        """SELECT o.* FROM orders o
           WHERE o.status IN ('paid','processing','shipped','delivered') AND o.reorder_reminder_sent_at IS NULL
             AND o.stripe_subscription_id = ''
             AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.email = o.email AND o2.created_at > o.created_at)
           ORDER BY o.created_at LIMIT ?""",
        (limit,),
    )
    sent = 0
    now = utcnow()
    for row in rows:
        order = dict(row)
        items = [dict(i) for i in all_rows(conn, "SELECT * FROM order_items WHERE order_id = ?", (order["id"],))]
        days = coverage_days(items)
        if days <= 0:
            continue
        created = parse_iso(order["created_at"])
        due = created + timedelta(days=days - settings.reorder_reminder_lead_days)
        if now < due:
            continue
        with transaction(conn):
            conn.execute("UPDATE orders SET reorder_reminder_sent_at = ? WHERE id = ?", (iso(), order["id"]))
        emails.send(
            conn, order["email"], "reorder_reminder", "Time for the next Drain Shot dose",
            {"order": order, "items": items, "coverage_days": days, "reorder_url": f"{settings.base_url}/reorder/{order['order_number']}/{_reorder_token(order)}"},
            category="marketing", related_type="order", related_id=order["id"],
        )
        sent += 1
    return sent


def _reorder_token(order: dict) -> str:
    from ..security import hash_token
    return hash_token(f"reorder:{order['id']}:{order['email']}")[:32]


def reorder_token_valid(order: dict, token: str) -> bool:
    from ..security import constant_eq
    return constant_eq(_reorder_token(order), token or "")


def review_invites(conn: sqlite3.Connection, limit: int = 200) -> int:
    cutoff_shipped = iso(utcnow() - timedelta(days=10))
    rows = all_rows(
        conn,
        """SELECT * FROM orders WHERE review_invite_sent_at IS NULL
           AND (status = 'delivered' OR (status = 'shipped' AND shipped_at < ?))
           ORDER BY created_at LIMIT ?""",
        (cutoff_shipped, limit),
    )
    sent = 0
    for row in rows:
        order = dict(row)
        items = [dict(i) for i in all_rows(conn, "SELECT * FROM order_items WHERE order_id = ?", (order["id"],))]
        with transaction(conn):
            conn.execute("UPDATE orders SET review_invite_sent_at = ? WHERE id = ?", (iso(), order["id"]))
        emails.send(
            conn, order["email"], "review_invite", "How is Drain Shot working for you?",
            {"order": order, "items": items, "review_url": f"{settings.base_url}/reviews/new/{order['order_number']}/{_reorder_token(order)}"},
            category="marketing", related_type="order", related_id=order["id"],
        )
        sent += 1
    return sent


def back_in_stock(conn: sqlite3.Connection, limit: int = 300) -> int:
    rows = all_rows(
        conn,
        """SELECT n.*, v.name AS variant_name, v.stock, p.name AS product_name, p.slug AS product_slug
           FROM stock_notifications n JOIN variants v ON v.id = n.variant_id JOIN products p ON p.id = v.product_id
           WHERE n.notified_at IS NULL AND v.stock > 0 AND v.is_active = 1 AND p.is_active = 1
           ORDER BY n.created_at LIMIT ?""",
        (limit,),
    )
    sent = 0
    for row in rows:
        n = dict(row)
        with transaction(conn):
            conn.execute("UPDATE stock_notifications SET notified_at = ? WHERE id = ?", (iso(), n["id"]))
        emails.send(
            conn, n["email"], "back_in_stock", f"{n['product_name']} {n['variant_name']} is back in stock",
            {"notice": n, "product_url": f"{settings.base_url}/products/{n['product_slug']}"},
            category="marketing", related_type="stock_notification", related_id=n["id"],
        )
        sent += 1
    return sent


def housekeeping(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM rate_limits WHERE window_start < strftime('%s','now') - 86400")
        conn.execute("DELETE FROM password_resets WHERE expires_at < ?", (iso(utcnow() - timedelta(days=2)),))
        conn.execute("DELETE FROM admin_login_attempts WHERE created_at < ?", (iso(utcnow() - timedelta(days=30)),))
        conn.execute("DELETE FROM carts WHERE customer_id IS NULL AND converted_order_id IS NULL AND email = '' AND updated_at < ?", (iso(utcnow() - timedelta(days=45)),))


def run_all(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for name, fn in (("abandoned", abandoned_carts), ("reorder", reorder_reminders), ("reviews", review_invites), ("restock", back_in_stock)):
        try:
            out[name] = fn(conn)
        except Exception:  # noqa: BLE001
            log.exception("lifecycle step %s failed", name)
            out[name] = -1
    housekeeping(conn)
    return out
