"""Server-side cart keyed to a token in the signed session cookie."""
from __future__ import annotations

import sqlite3

from ..config import settings
from ..db import all_rows, one, transaction
from ..security import iso, new_token
from . import discounts
from .catalog import every, subscription_config, subscription_price


def get_cart(conn: sqlite3.Connection, session: dict, create: bool = False) -> dict | None:
    token = session.get("cart")
    row = one(conn, "SELECT * FROM carts WHERE token = ?", (token,)) if token else None
    if row:
        return dict(row)
    if not create:
        return None
    token = new_token(24)
    conn.execute("INSERT INTO carts(token, customer_id) VALUES (?, ?)", (token, session.get("uid")))
    session["cart"] = token
    return dict(one(conn, "SELECT * FROM carts WHERE token = ?", (token,)))


def touch(conn: sqlite3.Connection, cart_id: int) -> None:
    conn.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (iso(), cart_id))


def add_item(conn: sqlite3.Connection, cart: dict, variant_id: int, qty: int, subscribe: int = 0) -> tuple[bool, str]:
    """`subscribe` is the delivery interval in months (0 = one-time purchase).
    A cart carries at most one subscription interval because one Stripe
    subscription bills every item on the same schedule."""
    qty = max(1, min(int(qty), 50))
    variant = one(conn, "SELECT v.*, p.is_active AS product_active FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ? AND v.is_active = 1", (variant_id,))
    if not variant or not variant["product_active"]:
        return False, "That option is not available."
    subscribe = int(subscribe or 0)
    if subscribe:
        cfg = subscription_config(conn)
        if not cfg["enabled"]:
            subscribe = 0
        elif subscribe not in cfg["allowed"]:
            return False, "That delivery interval is not offered."
        else:
            other = one(conn, "SELECT subscribe FROM cart_items WHERE cart_id = ? AND subscribe > 0 AND subscribe != ? LIMIT 1", (cart["id"], subscribe))
            if other:
                return False, f"Your cart already has a subscription every {every(other['subscribe'])}. Use the same interval, or check out that subscription first."
    existing = one(conn, "SELECT id, qty FROM cart_items WHERE cart_id = ? AND variant_id = ? AND subscribe = ?", (cart["id"], variant_id, subscribe))
    new_qty = min(qty + (existing["qty"] if existing else 0), 50)
    message = "Added to cart."
    if variant["stock"] < new_qty:
        if variant["stock"] <= 0:
            return False, "Sold out right now. Use notify me and we will email you when it is back."
        if existing and existing["qty"] >= variant["stock"]:
            return False, f"Only {variant['stock']} available and they are already in your cart."
        new_qty = variant["stock"]
        message = f"Only {variant['stock']} available — your cart now holds all of them."
    with transaction(conn):
        if existing:
            conn.execute("UPDATE cart_items SET qty = ? WHERE id = ?", (new_qty, existing["id"]))
        else:
            conn.execute("INSERT INTO cart_items(cart_id, variant_id, qty, subscribe) VALUES (?, ?, ?, ?)", (cart["id"], variant_id, new_qty, subscribe))
        touch(conn, cart["id"])
    if subscribe:
        message = f"Added — delivered every {every(subscribe)}, cancel any time."
    return True, message


def set_qty(conn: sqlite3.Connection, cart: dict, item_id: int, qty: int) -> None:
    item = one(conn, "SELECT ci.id, v.stock FROM cart_items ci JOIN variants v ON v.id = ci.variant_id WHERE ci.id = ? AND ci.cart_id = ?", (item_id, cart["id"]))
    if not item:
        return
    qty = int(qty)
    with transaction(conn):
        if qty <= 0 or item["stock"] <= 0:
            conn.execute("DELETE FROM cart_items WHERE id = ?", (item_id,))
        else:
            conn.execute("UPDATE cart_items SET qty = ? WHERE id = ?", (min(qty, item["stock"], 50), item_id))
        touch(conn, cart["id"])


def remove_item(conn: sqlite3.Connection, cart: dict, item_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM cart_items WHERE id = ? AND cart_id = ?", (item_id, cart["id"]))
        touch(conn, cart["id"])


def clear(conn: sqlite3.Connection, cart_id: int) -> None:
    conn.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart_id,))


def lines(conn: sqlite3.Connection, cart_id: int) -> list[dict]:
    rows = all_rows(
        conn,
        """SELECT ci.id, ci.qty, ci.subscribe, v.id AS variant_id, v.sku, v.name AS variant_name, v.price_cents,
                  v.compare_at_cents, v.stock, v.units_per_pack, v.stripe_price_id, v.stripe_subscription_price_id,
                  v.subscription_discount_percent,
                  p.id AS product_id, p.name AS product_name, p.slug AS product_slug, p.dose_interval_days,
                  p.drains_per_unit, p.hazmat, COALESCE(v.weight_oz, p.weight_oz) AS weight_oz,
                  (SELECT base FROM product_images WHERE product_id = p.id ORDER BY CASE kind WHEN 'hero' THEN 0 ELSE 1 END, sort LIMIT 1) AS image_base,
                  (SELECT source FROM product_images WHERE product_id = p.id ORDER BY CASE kind WHEN 'hero' THEN 0 ELSE 1 END, sort LIMIT 1) AS image_source
           FROM cart_items ci
           JOIN variants v ON v.id = ci.variant_id
           JOIN products p ON p.id = v.product_id
           WHERE ci.cart_id = ?
           ORDER BY ci.id""",
        (cart_id,),
    )
    cfg = subscription_config(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["base_price_cents"] = d["price_cents"]
        d["interval_months"] = int(d["subscribe"] or 0)
        if d["interval_months"] and cfg["enabled"]:
            pct = d["subscription_discount_percent"]
            pct = cfg["percent"] if pct is None else int(pct)
            d["sub_percent"] = pct
            d["price_cents"] = subscription_price(d["base_price_cents"], pct)
        else:
            d["sub_percent"] = 0
            d["interval_months"] = 0
        d["line_total_cents"] = d["price_cents"] * d["qty"]
        d["short_stock"] = d["qty"] > d["stock"]
        d["thumb_url"] = (f"/media/uploads/{d['image_base']}-480.jpg" if d.get("image_source") == "upload" else f"/static/img/products/{d['image_base']}-480.jpg") if d.get("image_base") else ""
        out.append(d)
    return out


def count(conn: sqlite3.Connection, session: dict) -> int:
    token = session.get("cart")
    if not token:
        return 0
    row = one(conn, "SELECT COALESCE(SUM(ci.qty), 0) AS n FROM cart_items ci JOIN carts c ON c.id = ci.cart_id WHERE c.token = ?", (token,))
    return int(row["n"]) if row else 0


def shipping_cents(subtotal_after_discount: int, free_shipping: bool = False) -> int:
    if subtotal_after_discount <= 0:
        return 0
    if free_shipping or subtotal_after_discount >= settings.free_shipping_threshold_cents:
        return 0
    return settings.flat_shipping_cents


def totals(conn: sqlite3.Connection, cart: dict, email: str = "") -> dict:
    items = lines(conn, cart["id"])
    subtotal = sum(i["line_total_cents"] for i in items)
    discount_row = None
    discount_cents = 0
    discount_error = ""
    free_ship = False
    if cart.get("discount_code_id"):
        discount_row = one(conn, "SELECT * FROM discount_codes WHERE id = ?", (cart["discount_code_id"],))
        if discount_row:
            ok, reason = discounts.validate(dict(discount_row), email=email or cart.get("email", ""), subtotal_cents=subtotal)
            if ok:
                discount_cents, free_ship = discounts.amount(dict(discount_row), subtotal)
            else:
                discount_error = reason
    after = max(subtotal - discount_cents, 0)
    ship = shipping_cents(after, free_ship)
    return {
        "items": items,
        "subtotal_cents": subtotal,
        "discount_cents": discount_cents,
        "discount": dict(discount_row) if discount_row else None,
        "discount_error": discount_error,
        "shipping_cents": ship,
        "free_shipping_gap_cents": max(settings.free_shipping_threshold_cents - after, 0) if ship else 0,
        "total_estimate_cents": after + ship,
        "count": sum(i["qty"] for i in items),
        "has_subscription": any(i["interval_months"] for i in items),
        "subscription_interval": next((i["interval_months"] for i in items if i["interval_months"]), 0),
        "subscription_savings_cents": sum((i["base_price_cents"] - i["price_cents"]) * i["qty"] for i in items if i["interval_months"]),
    }


def merge_into_customer(conn: sqlite3.Connection, session: dict, customer_id: int) -> None:
    """On login: move guest lines into the customer's most recent open cart (or
    adopt the guest cart). Quantities add, capped by stock."""
    guest = get_cart(conn, session)
    owned = one(conn, "SELECT * FROM carts WHERE customer_id = ? AND converted_order_id IS NULL ORDER BY updated_at DESC LIMIT 1", (customer_id,))
    with transaction(conn):
        if guest and not guest.get("customer_id"):
            if owned and owned["id"] != guest["id"]:
                for item in all_rows(conn, "SELECT * FROM cart_items WHERE cart_id = ?", (guest["id"],)):
                    stock = one(conn, "SELECT stock FROM variants WHERE id = ?", (item["variant_id"],))
                    cap = int(stock["stock"]) if stock else 0
                    if cap <= 0:
                        continue  # sold out since it was added: drop the line rather than carry a phantom unit
                    existing = one(conn, "SELECT id, qty FROM cart_items WHERE cart_id = ? AND variant_id = ? AND subscribe = ?", (owned["id"], item["variant_id"], item["subscribe"]))
                    if existing:
                        conn.execute("UPDATE cart_items SET qty = ? WHERE id = ?", (min(existing["qty"] + item["qty"], cap, 50), existing["id"]))
                    else:
                        conn.execute("INSERT INTO cart_items(cart_id, variant_id, qty, subscribe) VALUES (?, ?, ?, ?)", (owned["id"], item["variant_id"], min(item["qty"], cap, 50), item["subscribe"]))
                conn.execute("DELETE FROM carts WHERE id = ?", (guest["id"],))
                session["cart"] = owned["token"]
            else:
                conn.execute("UPDATE carts SET customer_id = ?, updated_at = ? WHERE id = ?", (customer_id, iso(), guest["id"]))
        elif owned:
            session["cart"] = owned["token"]
