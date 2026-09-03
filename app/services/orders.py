"""Orders are created by the Stripe webhook, never by the browser redirect.
Everything that must be consistent (event idempotency, order + lines, inventory,
discount count, cart conversion) happens in one BEGIN IMMEDIATE transaction."""
from __future__ import annotations

import json
import logging
import sqlite3

from ..db import all_rows, one, transaction
from ..security import iso, normalize_email, order_number
from . import audit, discounts

log = logging.getLogger("qd.orders")

ORDER_STATUSES = ("paid", "on_hold", "processing", "shipped", "delivered", "partially_refunded", "refunded", "canceled")


class AlreadyProcessed(Exception):
    pass


def _addr(details: dict | None) -> dict:
    details = details or {}
    a = details.get("address") or {}
    return {
        "shipping_name": details.get("name") or "",
        "shipping_line1": a.get("line1") or "",
        "shipping_line2": a.get("line2") or "",
        "shipping_city": a.get("city") or "",
        "shipping_state": a.get("state") or "",
        "shipping_postal_code": a.get("postal_code") or "",
        "shipping_country": a.get("country") or "US",
        "shipping_phone": details.get("phone") or "",
    }


def _ensure_customer(conn: sqlite3.Connection, email: str, name: str, phone: str, stripe_customer_id: str) -> int:
    """Link to the existing account if one exists — and never touch its password
    or profile. Otherwise create a guest row (empty password_hash) that can only
    be claimed through password reset."""
    norm = normalize_email(email)
    row = one(conn, "SELECT id, stripe_customer_id FROM customers WHERE email_norm = ?", (norm,))
    if row:
        if stripe_customer_id and not row["stripe_customer_id"]:
            conn.execute("UPDATE customers SET stripe_customer_id = ? WHERE id = ?", (stripe_customer_id, row["id"]))
        return int(row["id"])
    first, _, last = (name or "").partition(" ")
    cur = conn.execute(
        "INSERT INTO customers(email, email_norm, password_hash, first_name, last_name, phone, stripe_customer_id) VALUES (?, ?, '', ?, ?, ?, ?)",
        (email, norm, first, last, phone or "", stripe_customer_id or ""),
    )
    return int(cur.lastrowid)


def create_from_checkout_session(conn: sqlite3.Connection, session: dict, event_id: str, event_type: str) -> dict:
    """Idempotent on event_id and on the Stripe session id. Returns the order."""
    meta = session.get("metadata") or {}
    session_id = session.get("id", "")
    email = (session.get("customer_details") or {}).get("email") or session.get("customer_email") or meta.get("email") or ""
    if not email:
        raise ValueError("checkout session has no email")

    with transaction(conn):
        try:
            conn.execute("INSERT INTO processed_events(event_id, source, type) VALUES (?, 'stripe', ?)", (event_id, event_type))
        except sqlite3.IntegrityError as exc:
            raise AlreadyProcessed(event_id) from exc

        existing = one(conn, "SELECT * FROM orders WHERE stripe_checkout_session_id = ?", (session_id,))
        if existing:
            return dict(existing)

        try:
            lines = json.loads(meta.get("lines") or "[]")
        except json.JSONDecodeError:
            lines = []
        if not lines:
            lines = _lines_from_stripe(session)

        total_details = session.get("total_details") or {}
        interval_months = int(meta.get("interval_months") or 0)
        shipping_cents = int(total_details.get("amount_shipping") or 0)
        subtotal_cents = int(session.get("amount_subtotal") or 0)
        if interval_months and not shipping_cents:
            # Subscription checkouts bill shipping as a recurring line, so Stripe
            # folds it into amount_subtotal; pull it back out using our own figure.
            shipping_cents = int(meta.get("shipping_cents") or 0)
            subtotal_cents = max(subtotal_cents - shipping_cents, 0)
        amounts = {
            "subtotal_cents": subtotal_cents,
            "discount_cents": int(total_details.get("amount_discount") or 0),
            "shipping_cents": shipping_cents,
            "tax_cents": int(total_details.get("amount_tax") or 0),
            "total_cents": int(session.get("amount_total") or 0),
            "credit_cents": int(meta.get("credit_cents") or 0),
        }
        customer_details = session.get("customer_details") or {}
        collected = session.get("collected_information") if isinstance(session.get("collected_information"), dict) else {}
        shipping = _addr(session.get("shipping_details") or collected.get("shipping_details"))
        if not shipping["shipping_line1"]:
            shipping = _addr(customer_details)
        stripe_customer = session.get("customer") if isinstance(session.get("customer"), str) else (session.get("customer") or {}).get("id", "")
        customer_id = _ensure_customer(conn, email, customer_details.get("name") or shipping["shipping_name"], customer_details.get("phone") or "", stripe_customer or "")

        pi = session.get("payment_intent")
        pi_id = pi if isinstance(pi, str) else (pi or {}).get("id", "")
        sub = session.get("subscription")
        sub_id = sub if isinstance(sub, str) else (sub or {}).get("id", "")
        discount_code_id = int(meta["discount_code_id"]) if meta.get("discount_code_id") else None
        discount_code = meta.get("discount_code") or ""

        number = order_number()
        while one(conn, "SELECT 1 FROM orders WHERE order_number = ?", (number,)):
            number = order_number()

        oversold: list[str] = []
        order_data = {
            "order_number": number,
            "customer_id": customer_id,
            "email": normalize_email(email),
            "status": "paid",
            "stripe_checkout_session_id": session_id,
            "stripe_payment_intent_id": pi_id or "",
            "stripe_customer_id": stripe_customer or "",
            "stripe_subscription_id": sub_id or "",
            "currency": session.get("currency") or "usd",
            **amounts,
            "discount_code_id": discount_code_id,
            "discount_code": discount_code,
            **shipping,
            "utm_source": meta.get("utm_source") or "",
            "utm_medium": meta.get("utm_medium") or "",
            "utm_campaign": meta.get("utm_campaign") or "",
            "referral_code": meta.get("referral_code") or None,
            "cart_id": int(meta["cart_id"]) if meta.get("cart_id") else None,
            "ip": meta.get("ip") or "",
        }
        cols = ", ".join(order_data)
        marks = ", ".join("?" for _ in order_data)
        cur = conn.execute(f"INSERT INTO orders ({cols}) VALUES ({marks})", tuple(order_data.values()))
        order_id = int(cur.lastrowid)

        for ln in lines:
            variant_id = int(ln.get("v") or 0) or None
            qty = max(int(ln.get("q") or 1), 1)
            unit = int(ln.get("p") or 0)
            variant = one(conn, "SELECT v.*, p.name AS product_name, p.dose_interval_days, p.drains_per_unit FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?", (variant_id,)) if variant_id else None
            # Every line is inserted, including zero-cost ones. Credits get their own column.
            conn.execute(
                "INSERT INTO order_items(order_id, variant_id, product_id, sku, product_name, variant_name, qty, unit_price_cents, line_total_cents, discount_cents, credit_cents, units_per_pack, dose_interval_days, drains_per_unit, is_subscription)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id, variant_id, variant["product_id"] if variant else None,
                    variant["sku"] if variant else ln.get("sku", ""),
                    variant["product_name"] if variant else ln.get("name", "Item"),
                    variant["name"] if variant else ln.get("variant", ""),
                    qty, unit, unit * qty, int(ln.get("d") or 0), int(ln.get("c") or 0),
                    variant["units_per_pack"] if variant else 1,
                    variant["dose_interval_days"] if variant else 30,
                    variant["drains_per_unit"] if variant else 1,
                    int(ln.get("s") or 0),
                ),
            )
            if variant:
                dec = conn.execute("UPDATE variants SET stock = stock - ? WHERE id = ? AND stock >= ?", (qty, variant_id, qty))
                if dec.rowcount == 1:
                    conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, order_id) VALUES (?, ?, 'order', ?)", (variant_id, -qty, order_id))
                else:
                    # The line failed the atomic check: money is already taken, so the
                    # order is held for a human instead of silently overselling.
                    oversold.append(f"{variant['sku']} x{qty} (stock {variant['stock']})")

        if not lines:
            # Money was taken but nothing resolved to a line: hold it for a human, never a silent empty 'paid' order.
            conn.execute("UPDATE orders SET status = 'on_hold', admin_note = ? WHERE id = ?", ("LINES MISSING: Stripe session had no resolvable items — check the Stripe dashboard and add lines by hand", order_id))
            audit.log(conn, "order.lines_missing", actor_type="webhook", target_type="order", target_id=order_id, after={"session": session_id})
        if oversold:
            conn.execute("UPDATE orders SET status = 'on_hold', admin_note = ? WHERE id = ?", ("STOCK SHORT AT PAYMENT: " + "; ".join(oversold), order_id))
            audit.log(conn, "order.stock_short", actor_type="webhook", target_type="order", target_id=order_id, after={"oversold": oversold})

        if discount_code_id:
            counted = discounts.redeem(conn, discount_code_id, order_id, email)
            if not counted:
                audit.log(conn, "discount.over_redeemed", actor_type="webhook", target_type="discount_code", target_id=discount_code_id, after={"order_id": order_id})

        if order_data["cart_id"]:
            conn.execute("UPDATE carts SET converted_order_id = ?, updated_at = ? WHERE id = ?", (order_id, iso(), order_data["cart_id"]))
            conn.execute("DELETE FROM cart_items WHERE cart_id = ?", (order_data["cart_id"],))

        if sub_id:
            from . import subscriptions  # local import: subscriptions imports orders
            subscriptions.register_from_checkout(
                conn, stripe_subscription_id=sub_id, stripe_customer_id=stripe_customer or "", customer_id=customer_id,
                email=email, lines=lines, interval_months=int(meta.get("interval_months") or 1),
                shipping_cents=int(meta.get("shipping_cents") or 0), order_id=order_id,
            )

        audit.log(conn, "order.created", actor_type="webhook", target_type="order", target_id=order_id, after={"number": number, "total_cents": amounts["total_cents"], "event": event_id})
        return dict(one(conn, "SELECT * FROM orders WHERE id = ?", (order_id,)))


def _lines_from_stripe(session: dict) -> list[dict]:
    items = ((session.get("line_items") or {}).get("data")) or []
    out = []
    for it in items:
        price = it.get("price") or {}
        product = price.get("product")
        meta = (product.get("metadata") or {}) if isinstance(product, dict) else {}
        if meta.get("kind") == "shipping":
            continue
        recurring = price.get("recurring") or {}
        months = int(recurring.get("interval_count") or 0) if recurring.get("interval") == "month" else 0
        out.append({"v": int(meta.get("variant_id") or 0), "q": int(it.get("quantity") or 1), "p": int(price.get("unit_amount") or 0), "s": months, "name": it.get("description", "Item")})
    return out


def items(conn: sqlite3.Connection, order_id: int) -> list[dict]:
    return [dict(r) for r in all_rows(conn, "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))]


def get(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = one(conn, "SELECT * FROM orders WHERE id = ?", (order_id,))
    return dict(row) if row else None


def get_for_customer(conn: sqlite3.Connection, order_id: int, customer_id: int) -> dict | None:
    """Ownership, not existence."""
    row = one(conn, "SELECT * FROM orders WHERE id = ? AND customer_id = ?", (order_id, customer_id))
    return dict(row) if row else None


def get_by_number_and_email(conn: sqlite3.Connection, number: str, email: str) -> dict | None:
    row = one(conn, "SELECT * FROM orders WHERE order_number = ? AND email = ?", (number.strip().upper(), normalize_email(email)))
    return dict(row) if row else None


def get_by_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = one(conn, "SELECT * FROM orders WHERE stripe_checkout_session_id = ?", (session_id,))
    return dict(row) if row else None


def list_for_customer(conn: sqlite3.Connection, customer_id: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in all_rows(conn, "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?", (customer_id, limit))]


def set_status(conn: sqlite3.Connection, order: dict, status: str, *, tracking: str | None = None, carrier: str | None = None, admin_id: int | None = None, admin_name: str = "", ip: str = "") -> None:
    if status not in ORDER_STATUSES:
        raise ValueError(status)
    before = {"status": order["status"], "tracking_number": order["tracking_number"], "carrier": order["carrier"]}
    data = {"status": status, "updated_at": iso()}
    if tracking is not None:
        data["tracking_number"] = tracking.strip()
    if carrier is not None:
        data["carrier"] = carrier.strip()
    if status == "shipped" and not order.get("shipped_at"):
        data["shipped_at"] = iso()
    if status == "delivered" and not order.get("delivered_at"):
        data["delivered_at"] = iso()
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE orders SET {sets} WHERE id = ?", (*data.values(), order["id"]))
    audit.log(conn, "order.status_change", actor_type="admin" if admin_id else "system", actor_id=admin_id, actor_name=admin_name, target_type="order", target_id=order["id"], before=before, after={k: v for k, v in data.items() if k != "updated_at"}, ip=ip)


def record_refund(conn: sqlite3.Connection, payment_intent_id: str, refunded_cents: int, event_id: str) -> dict | None:
    if not payment_intent_id:
        return None
    with transaction(conn):
        try:
            conn.execute("INSERT INTO processed_events(event_id, source, type) VALUES (?, 'stripe', 'charge.refunded')", (event_id,))
        except sqlite3.IntegrityError:
            return None
        row = one(conn, "SELECT * FROM orders WHERE stripe_payment_intent_id = ?", (payment_intent_id,))
        if not row:
            return None
        # A partial refund keeps the fulfilment state (on_hold/shipped/...) so queues and reminders still see it.
        status = "refunded" if refunded_cents >= row["total_cents"] else ("partially_refunded" if row["status"] in ("paid", "processing", "partially_refunded") else row["status"])
        conn.execute("UPDATE orders SET refunded_cents = ?, status = ?, updated_at = ? WHERE id = ?", (refunded_cents, status, iso(), row["id"]))
        audit.log(conn, "order.refund_recorded", actor_type="webhook", target_type="order", target_id=row["id"], after={"refunded_cents": refunded_cents, "status": status})
        return dict(one(conn, "SELECT * FROM orders WHERE id = ?", (row["id"],)))
