"""Order creation from the Stripe webhook: idempotent on event id, every line
inserted (even at $0), inventory decremented atomically, discount counted once."""
from __future__ import annotations

import json
import uuid

import pytest

from conftest import unique_email, variant_id
from app.services import orders
from app.services.orders import AlreadyProcessed


def fake_session(lines: list[dict], *, email: str | None = None, session_id: str | None = None, metadata: dict | None = None) -> dict:
    subtotal = sum(int(ln.get("p") or 0) * int(ln.get("q") or 1) for ln in lines)
    return {
        "id": session_id or f"cs_test_{uuid.uuid4().hex}",
        "object": "checkout.session",
        "payment_status": "paid",
        "currency": "usd",
        "customer": "cus_test_1",
        "payment_intent": f"pi_{uuid.uuid4().hex[:12]}",
        "customer_details": {
            "email": email or unique_email("buyer"),
            "name": "Buy Er",
            "phone": "+16315551212",
            "address": {"line1": "1 Main St", "line2": "", "city": "Islip", "state": "NY", "postal_code": "11751", "country": "US"},
        },
        "amount_subtotal": subtotal,
        "amount_total": subtotal + 695,
        "total_details": {"amount_discount": 0, "amount_shipping": 695, "amount_tax": 0},
        "metadata": {"lines": json.dumps(lines, separators=(",", ":")), **(metadata or {})},
    }


def stock_of(conn, vid: int) -> int:
    return int(conn.execute("SELECT stock FROM variants WHERE id = ?", (vid,)).fetchone()["stock"])


def make_discount(conn, *, max_uses: int | None, **extra) -> dict:
    code = f"T{uuid.uuid4().hex[:8].upper()}"
    cols = {"code": code, "kind": "percent", "value": 10, "max_uses": max_uses, **extra}
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO discount_codes ({names}) VALUES ({marks})", tuple(cols.values()))
    return dict(conn.execute("SELECT * FROM discount_codes WHERE code = ?", (code,)).fetchone())


def new_variant(conn, stock: int) -> int:
    pid = conn.execute("SELECT id FROM products WHERE slug = 'quick-shot'").fetchone()["id"]
    sku = f"QS-T-{uuid.uuid4().hex[:6].upper()}"
    cur = conn.execute("INSERT INTO variants(product_id, sku, name, units_per_pack, price_cents, stock, sort) VALUES (?, ?, 'test pack', 1, 1600, ?, 99)", (pid, sku, stock))
    return int(cur.lastrowid)


# ------------------------------------------------------------ idempotency
def test_same_event_twice_creates_one_order_and_decrements_stock_once(conn):
    vid = variant_id(conn, "QS-1")
    before = stock_of(conn, vid)
    session = fake_session([{"v": vid, "q": 2, "p": 1600}])
    event_id = f"evt_{uuid.uuid4().hex}"

    order = orders.create_from_checkout_session(conn, session, event_id, "checkout.session.completed")
    assert order["status"] == "paid"
    assert order["order_number"].startswith("QD-")

    with pytest.raises(AlreadyProcessed):
        orders.create_from_checkout_session(conn, session, event_id, "checkout.session.completed")

    assert conn.execute("SELECT COUNT(*) AS n FROM orders WHERE stripe_checkout_session_id = ?", (session["id"],)).fetchone()["n"] == 1
    assert stock_of(conn, vid) == before - 2
    moves = conn.execute("SELECT delta FROM inventory_movements WHERE order_id = ? AND variant_id = ?", (order["id"], vid)).fetchall()
    assert [m["delta"] for m in moves] == [-2]


def test_same_session_under_a_new_event_id_returns_existing_order(conn):
    """Stripe can retry with a fresh event id; the session id is the second guard."""
    vid = variant_id(conn, "QS-1")
    before = stock_of(conn, vid)
    session = fake_session([{"v": vid, "q": 1, "p": 1600}])
    first = orders.create_from_checkout_session(conn, session, f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    second = orders.create_from_checkout_session(conn, session, f"evt_{uuid.uuid4().hex}", "checkout.session.async_payment_succeeded")
    assert first["id"] == second["id"]
    assert stock_of(conn, vid) == before - 1


def test_zero_price_line_is_inserted(conn):
    vid = variant_id(conn, "QS-1")
    vid3 = variant_id(conn, "QS-3")
    session = fake_session([{"v": vid, "q": 1, "p": 1600}, {"v": vid3, "q": 1, "p": 0}])
    order = orders.create_from_checkout_session(conn, session, f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    items = orders.items(conn, order["id"])
    assert len(items) == 2
    free = [i for i in items if i["variant_id"] == vid3]
    assert len(free) == 1
    assert free[0]["unit_price_cents"] == 0 and free[0]["line_total_cents"] == 0 and free[0]["qty"] == 1


def test_guest_customer_row_is_created_with_empty_password(conn):
    email = unique_email("guest")
    session = fake_session([{"v": variant_id(conn), "q": 1, "p": 1600}], email=email)
    order = orders.create_from_checkout_session(conn, session, f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    assert row["email_norm"] == email and row["password_hash"] == "" and row["first_name"] == "Buy"


# --------------------------------------------------------------- discounts
def test_discount_usage_counts_once_and_stays_active(conn):
    code = make_discount(conn, max_uses=5)
    vid = variant_id(conn)
    session = fake_session([{"v": vid, "q": 1, "p": 1600}], metadata={"discount_code_id": str(code["id"]), "discount_code": code["code"]})
    event_id = f"evt_{uuid.uuid4().hex}"

    order = orders.create_from_checkout_session(conn, session, event_id, "checkout.session.completed")
    with pytest.raises(AlreadyProcessed):
        orders.create_from_checkout_session(conn, session, event_id, "checkout.session.completed")

    row = conn.execute("SELECT usage_count, is_active FROM discount_codes WHERE id = ?", (code["id"],)).fetchone()
    assert row["usage_count"] == 1
    assert row["is_active"] == 1
    assert order["discount_code"] == code["code"] and order["discount_code_id"] == code["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM discount_redemptions WHERE discount_code_id = ?", (code["id"],)).fetchone()["n"] == 1


def test_single_use_code_on_two_orders_is_counted_once_and_audited(conn):
    code = make_discount(conn, max_uses=1)
    vid = variant_id(conn)
    meta = {"discount_code_id": str(code["id"]), "discount_code": code["code"]}
    first = orders.create_from_checkout_session(conn, fake_session([{"v": vid, "q": 1, "p": 1600}], metadata=meta), f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    second = orders.create_from_checkout_session(conn, fake_session([{"v": vid, "q": 1, "p": 1600}], metadata=meta), f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    assert first["id"] != second["id"]

    row = conn.execute("SELECT usage_count, is_active FROM discount_codes WHERE id = ?", (code["id"],)).fetchone()
    assert row["usage_count"] == 1
    assert row["is_active"] == 1
    audit = conn.execute("SELECT * FROM audit_log WHERE action = 'discount.over_redeemed' AND target_type = 'discount_code' AND target_id = ?", (code["id"],)).fetchall()
    assert len(audit) == 1
    assert json.loads(audit[0]["after_json"])["order_id"] == second["id"]
    # Both orders still stand — the money was already taken.
    assert second["status"] == "paid"


# --------------------------------------------------------------- inventory
def test_two_sessions_for_last_unit_hold_the_second_and_never_go_negative(conn):
    vid = new_variant(conn, stock=1)
    first = orders.create_from_checkout_session(conn, fake_session([{"v": vid, "q": 1, "p": 1600}]), f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    second = orders.create_from_checkout_session(conn, fake_session([{"v": vid, "q": 1, "p": 1600}]), f"evt_{uuid.uuid4().hex}", "checkout.session.completed")

    assert first["status"] == "paid"
    assert second["status"] == "on_hold"
    assert second["admin_note"].startswith("STOCK SHORT AT PAYMENT")
    assert stock_of(conn, vid) == 0
    # The held order still has its line, so a human can fulfil it once restocked.
    assert len(orders.items(conn, second["id"])) == 1
    # Only the first decrement was recorded.
    assert conn.execute("SELECT COUNT(*) AS n FROM inventory_movements WHERE variant_id = ? AND reason = 'order'", (vid,)).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'order.stock_short' AND target_id = ?", (second["id"],)).fetchone()["n"] == 1


def test_oversized_quantity_never_drives_stock_negative(conn):
    vid = new_variant(conn, stock=2)
    order = orders.create_from_checkout_session(conn, fake_session([{"v": vid, "q": 5, "p": 1600}]), f"evt_{uuid.uuid4().hex}", "checkout.session.completed")
    assert order["status"] == "on_hold"
    assert stock_of(conn, vid) == 2


def test_session_without_email_is_rejected_and_writes_nothing(conn):
    session = fake_session([{"v": variant_id(conn), "q": 1, "p": 1600}])
    session["customer_details"] = {}
    session["metadata"].pop("email", None)
    event_id = f"evt_{uuid.uuid4().hex}"
    with pytest.raises(ValueError):
        orders.create_from_checkout_session(conn, session, event_id, "checkout.session.completed")
    assert conn.execute("SELECT COUNT(*) AS n FROM processed_events WHERE event_id = ?", (event_id,)).fetchone()["n"] == 0
