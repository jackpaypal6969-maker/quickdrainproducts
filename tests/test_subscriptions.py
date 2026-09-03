"""Subscribe-and-save: interval-aware cart, subscription registration at
checkout, renewal orders from invoice.paid, lifecycle sync, account controls."""
from __future__ import annotations

import json

from app.db import one, transaction
from app.services import cart as cart_service
from app.services import catalog, orders, subscriptions
from tests.conftest import add_to_cart, create_customer, get_csrf, login, new_client, variant_id  # noqa: F401


def _fake_checkout(session_id: str, lines: list[dict], email: str, interval: int, shipping: int = 695, sub_id: str = "sub_test_1"):
    meta = {"cart_id": "", "email": email, "lines": json.dumps(lines), "discount_code_id": "", "discount_code": "", "discount_cents": "0", "interval_months": str(interval), "shipping_cents": str(shipping)}
    subtotal = sum(l["p"] * l["q"] for l in lines)
    return {
        "id": session_id, "mode": "subscription", "payment_status": "paid", "amount_subtotal": subtotal, "amount_total": subtotal + shipping, "currency": "usd",
        "customer": "cus_test_1", "subscription": sub_id, "payment_intent": None,
        "customer_details": {"email": email, "name": "Sub Buyer", "phone": "", "address": {"line1": "9 Elm St", "city": "Islip", "state": "NY", "postal_code": "11751", "country": "US"}},
        "shipping_details": {"name": "Sub Buyer", "address": {"line1": "9 Elm St", "city": "Islip", "state": "NY", "postal_code": "11751", "country": "US"}},
        "total_details": {"amount_discount": 0, "amount_shipping": shipping, "amount_tax": 0}, "metadata": meta,
    }


def test_subscription_pricing_uses_admin_percent(conn):
    with transaction(conn):
        conn.execute("UPDATE settings SET value = '15' WHERE key = 'subscription_discount_percent'")
        conn.execute("UPDATE settings SET value = '1,2,3' WHERE key = 'subscription_intervals'")
    p = catalog.get_product(conn, "quick-shot")
    v = p["variants"][0]
    assert v["sub_percent"] == 15
    assert v["sub_price_cents"] == round(v["price_cents"] * 0.85)
    assert p["subscriptions"]["intervals"] == [1, 2, 3]
    three = next(x for x in p["variants"] if x["units_per_pack"] == 3)
    assert three["sub_recommended_months"] == 3
    with transaction(conn):
        conn.execute("UPDATE variants SET subscription_discount_percent = 25 WHERE id = ?", (v["id"],))
    p = catalog.get_product(conn, "quick-shot")
    assert p["variants"][0]["sub_percent"] == 25
    with transaction(conn):
        conn.execute("UPDATE variants SET subscription_discount_percent = NULL WHERE id = ?", (v["id"],))
        conn.execute("UPDATE settings SET value = '10' WHERE key = 'subscription_discount_percent'")


def test_cart_keeps_one_interval_and_discounts_lines(conn):
    session = {}
    cart = cart_service.get_cart(conn, session, create=True)
    vid = variant_id(conn, "QS-1")
    ok, msg = cart_service.add_item(conn, cart, vid, 1, 3)
    assert ok and "every 3 months" in msg
    ok, msg = cart_service.add_item(conn, cart, variant_id(conn, "QS-3"), 1, 2)
    assert not ok and "already has a subscription every 3 months" in msg
    ok, _ = cart_service.add_item(conn, cart, vid, 1, 7)
    assert not ok
    tot = cart_service.totals(conn, cart)
    line = tot["items"][0]
    assert line["interval_months"] == 3 and line["sub_percent"] == 10
    assert line["price_cents"] == round(line["base_price_cents"] * 0.9)
    assert tot["has_subscription"] and tot["subscription_interval"] == 3
    assert tot["subscription_savings_cents"] == line["base_price_cents"] - line["price_cents"]


def test_checkout_registers_subscription_and_renewal_creates_order(conn):
    vid = variant_id(conn, "QS-3")
    stock0 = one(conn, "SELECT stock FROM variants WHERE id = ?", (vid,))["stock"]
    price = one(conn, "SELECT price_cents FROM variants WHERE id = ?", (vid,))["price_cents"]
    sub_price = round(price * 0.9)
    order = orders.create_from_checkout_session(conn, _fake_checkout("cs_sub_1", [{"v": vid, "q": 1, "p": sub_price, "s": 3}], "subscriber@gmail.com", 3), "evt_sub_1", "checkout.session.completed")
    assert order["stripe_subscription_id"] == "sub_test_1"
    sub = one(conn, "SELECT * FROM subscriptions WHERE stripe_subscription_id = 'sub_test_1'")
    assert sub and sub["interval_months"] == 3 and sub["shipping_cents"] == 695 and sub["status"] == "active"
    assert json.loads(sub["lines"])[0]["v"] == vid
    assert one(conn, "SELECT stock FROM variants WHERE id = ?", (vid,))["stock"] == stock0 - 1
    # first invoice: no second order
    inv_first = {"id": "in_first", "subscription": "sub_test_1", "billing_reason": "subscription_create", "amount_paid": sub_price + 695, "tax": 0, "customer_email": "subscriber@gmail.com", "lines": {"data": [{"period": {"end": 1893456000}}]}}
    assert subscriptions.handle_invoice_paid(conn, inv_first, "evt_inv_first").startswith("invoice: first cycle")
    assert one(conn, "SELECT COUNT(*) AS n FROM orders WHERE stripe_subscription_id = 'sub_test_1'")["n"] == 1
    assert one(conn, "SELECT next_renewal_at FROM subscriptions WHERE stripe_subscription_id = 'sub_test_1'")["next_renewal_at"].startswith("2030-01-01")
    # renewal invoice → fulfilment order, stock decremented, idempotent on event id
    inv = {"id": "in_cycle_1", "subscription": "sub_test_1", "billing_reason": "subscription_cycle", "amount_paid": sub_price + 695 + 300, "tax": 300, "payment_intent": "pi_cycle_1", "customer_email": "subscriber@gmail.com", "customer_shipping": {"name": "Sub Buyer", "address": {"line1": "9 Elm St", "city": "Islip", "state": "NY", "postal_code": "11751", "country": "US"}}, "lines": {"data": [{"period": {"end": 1901232000}}]}}
    out = subscriptions.handle_invoice_paid(conn, inv, "evt_inv_1")
    assert out.startswith("renewal order QD-")
    assert subscriptions.handle_invoice_paid(conn, inv, "evt_inv_1") == "invoice: duplicate"
    renewals = [dict(r) for r in conn.execute("SELECT * FROM orders WHERE stripe_checkout_session_id = 'inv_in_cycle_1'")]
    assert len(renewals) == 1
    r = renewals[0]
    assert r["total_cents"] == sub_price + 695 + 300 and r["tax_cents"] == 300 and r["shipping_cents"] == 695 and r["subtotal_cents"] == sub_price
    assert r["shipping_line1"] == "9 Elm St" and r["stripe_subscription_id"] == "sub_test_1"
    items = orders.items(conn, r["id"])
    assert len(items) == 1 and items[0]["is_subscription"] == 1 and items[0]["variant_id"] == vid
    assert one(conn, "SELECT stock FROM variants WHERE id = ?", (vid,))["stock"] == stock0 - 2
    assert one(conn, "SELECT last_order_id FROM subscriptions WHERE stripe_subscription_id = 'sub_test_1'")["last_order_id"] == r["id"]
    assert one(conn, "SELECT COUNT(*) AS n FROM email_log WHERE related_id = ? AND template = 'order_confirmation'", (r["id"],))["n"] == 1
    # a failed payment marks past_due; the next paid cycle restores active
    assert subscriptions.handle_invoice_failed(conn, {"id": "in_fail", "subscription": "sub_test_1"}, "evt_fail_1") == "subscription past_due"
    assert one(conn, "SELECT status FROM subscriptions WHERE stripe_subscription_id = 'sub_test_1'")["status"] == "past_due"
    subscriptions.handle_invoice_paid(conn, dict(inv, id="in_cycle_2"), "evt_inv_2")
    assert one(conn, "SELECT status FROM subscriptions WHERE stripe_subscription_id = 'sub_test_1'")["status"] == "active"
    # Stripe says cancel at period end → canceling; deleted → canceled
    assert subscriptions.sync_from_stripe(conn, {"id": "sub_test_1", "status": "active", "cancel_at_period_end": True, "current_period_end": 1901232000, "customer": "cus_test_1"}) == "subscription canceling"
    assert subscriptions.sync_from_stripe(conn, {"id": "sub_test_1", "status": "canceled"}, deleted=True) == "subscription canceled"
    assert one(conn, "SELECT canceled_at FROM subscriptions WHERE stripe_subscription_id = 'sub_test_1'")["canceled_at"]


def test_account_shows_subscription_and_cancel_is_owner_only(conn):
    owner = create_customer(conn, "subowner@gmail.com", "correct-horse-1")
    other = create_customer(conn, "subother@gmail.com", "correct-horse-1")
    owner = owner["id"] if isinstance(owner, dict) else owner
    other = other["id"] if isinstance(other, dict) else other
    with transaction(conn):
        conn.execute("INSERT INTO subscriptions(customer_id, email, stripe_subscription_id, stripe_customer_id, status, interval_months, lines) VALUES (?, 'subowner@gmail.com', 'sub_acct_1', 'cus_x', 'active', 2, ?)", (owner, json.dumps([{"v": 1, "q": 1, "p": 1440, "name": "Quick Shot", "variant": "Single bottle"}])))
    sid = one(conn, "SELECT id FROM subscriptions WHERE stripe_subscription_id = 'sub_acct_1'")["id"]
    c = new_client()
    login(c, "subowner@gmail.com", "correct-horse-1")
    r = c.get("/account")
    assert r.status_code == 200 and "Every 2 months" in r.text and f"/account/subscriptions/{sid}/cancel" in r.text
    c2 = new_client()
    login(c2, "subother@gmail.com", "correct-horse-1")
    r = c2.post(f"/account/subscriptions/{sid}/cancel", data={"csrf_token": get_csrf(c2)})
    assert r.status_code == 404
    # owner's cancel with no Stripe keys fails safe: status unchanged, error flashed
    r = c.post(f"/account/subscriptions/{sid}/cancel", data={"csrf_token": get_csrf(c)})
    assert r.status_code in (302, 303)
    assert one(conn, "SELECT status FROM subscriptions WHERE id = ?", (sid,))["status"] == "active"
    assert one(conn, "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'subscription.cancel_failed'")["n"] == 1


def test_product_page_offers_subscription(client):
    r = client.get("/products/quick-shot")
    assert r.status_code == 200
    assert 'name="delivery"' in r.text and "data-subscribe-input" in r.text and 'data-sub-price' in r.text
    assert "Subscribe &amp; save" in r.text or "Subscribe & save" in r.text
