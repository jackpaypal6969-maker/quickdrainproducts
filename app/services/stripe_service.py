"""Stripe Checkout (hosted) and the webhook dispatcher.

The browser only ever starts a Checkout Session. Money, order rows, inventory
and discount counts are all settled by the webhook in orders.py."""
from __future__ import annotations

import json
import logging
import sqlite3

import stripe

from ..config import settings
from ..db import one, transaction
from ..security import iso
from . import analytics, emails, orders, subscriptions

log = logging.getLogger("qd.stripe")


def _client() -> None:
    stripe.api_key = settings.stripe_secret_key
    stripe.max_network_retries = 2


def configured() -> bool:
    """Checkout is offered only when the webhook that turns payments into orders
    can be verified. Keys without a webhook secret would charge cards for nothing."""
    return bool(settings.stripe_secret_key and settings.stripe_publishable_key and settings.stripe_webhook_secret)


def _image_url(base: str | None, source: str | None = "static") -> list[str]:
    if not base or not settings.base_url.startswith("https://"):
        return []  # Stripe needs a public https URL; skip on the raw port
    folder = "/media/uploads" if source == "upload" else "/static/img/products"
    return [f"{settings.base_url}{folder}/{base}-768.jpg"]


def create_checkout_session(conn: sqlite3.Connection, cart: dict, tot: dict, *, email: str, customer: dict | None, ip: str) -> stripe.checkout.Session:
    _client()
    lines: list[dict] = []
    snapshot: list[dict] = []
    interval = int(tot.get("subscription_interval") or 0)
    has_sub = bool(interval) and settings.subscriptions_enabled
    for it in tot["items"]:
        if it["qty"] > it["stock"]:
            raise ValueError(f"Only {it['stock']} of {it['product_name']} {it['variant_name']} left.")
        months = int(it.get("interval_months") or 0) if has_sub else 0
        price_data = {
            "currency": "usd",
            "unit_amount": it["price_cents"],
            "tax_behavior": "exclusive",
            "product_data": {
                "name": f"{it['product_name']} — {it['variant_name']}" + (f" (every {months} {'month' if months == 1 else 'months'})" if months else ""),
                "description": (f"{it['units_per_pack']} × 4 fl oz bottle" if it["units_per_pack"] > 1 else "4 fl oz bottle") + (f", {it['sub_percent']}% subscriber price, cancel any time" if months else ""),
                "metadata": {"variant_id": str(it["variant_id"]), "sku": it["sku"], "kind": "product"},
                "images": _image_url(it.get("image_base"), it.get("image_source")),
            },
        }
        if months:
            price_data["recurring"] = {"interval": "month", "interval_count": months}
        lines.append({"price_data": price_data, "quantity": it["qty"]})
        snapshot.append({"v": it["variant_id"], "q": it["qty"], "p": it["price_cents"], "s": months})

    ship = tot["shipping_cents"]
    if has_sub and ship > 0:
        # Subscriptions bill shipping every cycle as a recurring line; Checkout's
        # shipping_options only cover the first invoice.
        lines.append({
            "price_data": {"currency": "usd", "unit_amount": ship, "tax_behavior": "exclusive", "recurring": {"interval": "month", "interval_count": interval},
                            "product_data": {"name": "Shipping", "tax_code": "txcd_92010001", "metadata": {"kind": "shipping"}}},
            "quantity": 1,
        })
    shipping_options = [{
        "shipping_rate_data": {
            "type": "fixed_amount",
            "fixed_amount": {"amount": ship, "currency": "usd"},
            "display_name": "Free shipping" if ship == 0 else "Flat-rate shipping",
            "delivery_estimate": {"minimum": {"unit": "business_day", "value": 2}, "maximum": {"unit": "business_day", "value": 6}},
            "tax_behavior": "exclusive",
            "tax_code": "txcd_92010001",
        }
    }]

    snapshot_json = json.dumps(snapshot, separators=(",", ":"))
    metadata = {
        "cart_id": str(cart["id"]),
        "email": email,
        "customer_id": str(customer["id"]) if customer else "",
        # Stripe caps a metadata value at 500 chars; the cart row keeps the full copy.
        "lines": snapshot_json if len(snapshot_json) <= 500 else "",
        "discount_code_id": str(tot["discount"]["id"]) if tot.get("discount") and tot["discount_cents"] >= 0 and not tot.get("discount_error") else "",
        "discount_code": tot["discount"]["code"] if tot.get("discount") and not tot.get("discount_error") else "",
        "discount_cents": str(tot["discount_cents"]),
        "utm_source": cart.get("utm_source") or "",
        "utm_medium": cart.get("utm_medium") or "",
        "utm_campaign": cart.get("utm_campaign") or "",
        "referral_code": cart.get("referral_code") or "",
        "ip": ip,
        "interval_months": str(interval),
        "shipping_cents": str(ship),
    }

    params: dict = {
        "mode": "subscription" if has_sub else "payment",
        "line_items": lines,
        "success_url": f"{settings.base_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.base_url}/cart",
        "client_reference_id": str(cart["id"]),
        "metadata": metadata,
        "shipping_address_collection": {"allowed_countries": list(settings.ship_to_countries)},
        "phone_number_collection": {"enabled": True},
        "billing_address_collection": "auto",
        "automatic_tax": {"enabled": settings.stripe_tax_enabled},
        "shipping_options": None if has_sub else shipping_options,
        "customer_email": email if not (customer and customer.get("stripe_customer_id")) else None,
    }
    if customer and customer.get("stripe_customer_id"):
        params["customer"] = customer["stripe_customer_id"]
        params["customer_update"] = {"shipping": "auto", "address": "auto"}
    if has_sub:
        params["subscription_data"] = {"metadata": metadata, "description": f"{settings.app_name} — every {interval} {'month' if interval == 1 else 'months'}"}
        params["customer_creation"] = None  # subscription mode always creates a customer
    else:
        params["payment_intent_data"] = {"metadata": metadata, "description": f"{settings.app_name} order"}

    if tot["discount_cents"] > 0 and tot.get("discount") and not tot.get("discount_error"):
        coupon = stripe.Coupon.create(
            amount_off=tot["discount_cents"], currency="usd", duration="once",
            name=tot["discount"]["code"][:40], max_redemptions=1,
            metadata={"discount_code_id": str(tot["discount"]["id"]), "cart_id": str(cart["id"])},
        )
        params["discounts"] = [{"coupon": coupon.id}]

    params = {k: v for k, v in params.items() if v is not None}
    session = stripe.checkout.Session.create(**params)
    with transaction(conn):
        conn.execute(
            "UPDATE carts SET stripe_checkout_session_id = ?, checkout_lines = ?, checkout_started_at = ?, email = ?, customer_id = COALESCE(customer_id, ?), updated_at = ? WHERE id = ?",
            (session.id, snapshot_json, iso(), email, customer["id"] if customer else None, iso(), cart["id"]),
        )
    analytics.capture(conn, "checkout_started", email, {"cart_id": cart["id"], "value_cents": tot["total_estimate_cents"], "items": tot["count"], "discount_code": metadata["discount_code"], "utm_source": metadata["utm_source"], "utm_campaign": metadata["utm_campaign"]})
    return session


def retrieve_session(session_id: str) -> dict:
    _client()
    return stripe.checkout.Session.retrieve(session_id, expand=["line_items", "line_items.data.price.product"]).to_dict_recursive()


def construct_event(payload: bytes, signature: str) -> stripe.Event:
    _client()
    return stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)


def handle_event(conn: sqlite3.Connection, event: stripe.Event) -> str:
    """Returns a short outcome string for logging. Raises to make Stripe retry."""
    kind = event["type"]
    obj = event["data"]["object"]
    data = obj.to_dict_recursive() if hasattr(obj, "to_dict_recursive") else dict(obj)

    if kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if data.get("payment_status") not in {"paid", "no_payment_required"}:
            return "ignored: unpaid session"
        if not _is_our_session(conn, data):
            return "ignored: session not created by this store"
        if not (data.get("metadata") or {}).get("lines"):
            # Metadata capped or absent: the cart row holds the full snapshot.
            cart_row = one(conn, "SELECT checkout_lines FROM carts WHERE id = ? AND stripe_checkout_session_id = ?", (int((data.get("metadata") or {}).get("cart_id") or 0), data.get("id", "")))
            if cart_row and cart_row["checkout_lines"]:
                data = dict(data)
                data["metadata"] = dict(data.get("metadata") or {}, lines=cart_row["checkout_lines"])
            else:
                try:
                    data = retrieve_session(data["id"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not expand session %s: %s", data.get("id"), exc)
        try:
            order = orders.create_from_checkout_session(conn, data, event["id"], kind)
        except orders.AlreadyProcessed:
            return "duplicate"
        _after_order(conn, order)
        return f"order {order['order_number']}"

    if kind == "checkout.session.async_payment_failed":
        with transaction(conn):
            conn.execute("UPDATE carts SET stripe_checkout_session_id = '', updated_at = ? WHERE stripe_checkout_session_id = ?", (iso(), data.get("id", "")))
        return "async payment failed"

    if kind == "checkout.session.expired":
        with transaction(conn):
            conn.execute("UPDATE carts SET stripe_checkout_session_id = '', updated_at = ? WHERE stripe_checkout_session_id = ?", (iso(), data.get("id", "")))
        return "session expired"

    if kind == "charge.refunded":
        pi = data.get("payment_intent")
        pi_id = pi if isinstance(pi, str) else (pi or {}).get("id", "")
        if not pi_id:
            return "refund: charge has no payment intent (ignored)"
        order = orders.record_refund(conn, pi_id, int(data.get("amount_refunded") or 0), event["id"])
        return f"refund recorded on {order['order_number']}" if order else "refund: no matching order"

    if kind in {"customer.subscription.deleted", "customer.subscription.updated", "customer.subscription.created"}:
        return subscriptions.sync_from_stripe(conn, data, deleted=kind.endswith("deleted"))

    if kind == "invoice.paid":
        outcome = subscriptions.handle_invoice_paid(conn, data, event["id"])
        if outcome == "invoice: subscription unknown locally":
            # 500 → Stripe retries with backoff; by then checkout.session.completed has usually landed.
            raise RuntimeError(outcome)
        return outcome

    if kind == "invoice.payment_failed":
        return subscriptions.handle_invoice_failed(conn, data, event["id"])

    return "ignored"


def cancel_subscription(stripe_subscription_id: str, at_period_end: bool = True) -> dict:
    _client()
    if at_period_end:
        sub = stripe.Subscription.modify(stripe_subscription_id, cancel_at_period_end=True)
    else:
        sub = stripe.Subscription.cancel(stripe_subscription_id)
    return sub.to_dict_recursive()


def resume_subscription(stripe_subscription_id: str) -> dict:
    _client()
    return stripe.Subscription.modify(stripe_subscription_id, cancel_at_period_end=False).to_dict_recursive()


def billing_portal_url(stripe_customer_id: str, return_url: str) -> str:
    _client()
    session = stripe.billing_portal.Session.create(customer=stripe_customer_id, return_url=return_url)
    return session.url


def _is_our_session(conn: sqlite3.Connection, data: dict) -> bool:
    """Only sessions this store created (cart id in metadata, session id on that
    cart) become orders. Payment Links or other apps on the same Stripe account
    never do."""
    meta = data.get("metadata") or {}
    try:
        cart_id = int(meta.get("cart_id") or data.get("client_reference_id") or 0)
    except (TypeError, ValueError):
        cart_id = 0
    if not cart_id:
        return False
    row = one(conn, "SELECT stripe_checkout_session_id FROM carts WHERE id = ?", (cart_id,))
    return bool(row) and row["stripe_checkout_session_id"] == data.get("id", "")


def _after_order(conn: sqlite3.Connection, order: dict) -> None:
    """Side effects that must not roll back the order: email + analytics.
    Runs after the order transaction committed, never inside a write lock."""
    try:
        lines = orders.items(conn, order["id"])
        emails.send(
            conn, order["email"], "order_confirmation",
            f"Order {order['order_number']} confirmed — {settings.app_name}",
            {"order": order, "items": lines},
            related_type="order", related_id=order["id"],
        )
    except Exception:  # noqa: BLE001
        log.exception("confirmation email failed for order %s", order["id"])
    analytics.capture(conn, "checkout_completed", order["email"], {
        "order_number": order["order_number"], "value_cents": order["total_cents"], "discount_code": order["discount_code"],
        "utm_source": order["utm_source"], "utm_medium": order["utm_medium"], "utm_campaign": order["utm_campaign"],
    })
