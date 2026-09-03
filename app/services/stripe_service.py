"""Stripe Checkout (hosted) and the webhook dispatcher.

The browser only ever starts a Checkout Session. Money, order rows, inventory
and discount counts are all settled by the webhook in orders.py."""
from __future__ import annotations

import json
import logging
import sqlite3

import stripe

from ..config import settings
from ..db import transaction
from ..security import iso
from . import analytics, emails, orders

log = logging.getLogger("qd.stripe")


def _client() -> None:
    stripe.api_key = settings.stripe_secret_key
    stripe.max_network_retries = 2


def configured() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_publishable_key)


def _image_url(base: str | None) -> list[str]:
    if not base or not settings.base_url.startswith("https://"):
        return []  # Stripe needs a public https URL; skip on the raw port
    return [f"{settings.base_url}/static/img/products/{base}-768.jpg"]


def create_checkout_session(conn: sqlite3.Connection, cart: dict, tot: dict, *, email: str, customer: dict | None, ip: str) -> stripe.checkout.Session:
    _client()
    lines: list[dict] = []
    snapshot: list[dict] = []
    has_sub = False
    for it in tot["items"]:
        if it["qty"] > it["stock"]:
            raise ValueError(f"Only {it['stock']} of {it['product_name']} {it['variant_name']} left.")
        if it["subscribe"] and settings.subscriptions_enabled and it["stripe_subscription_price_id"]:
            has_sub = True
            lines.append({"price": it["stripe_subscription_price_id"], "quantity": it["qty"]})
        else:
            lines.append({
                "price_data": {
                    "currency": "usd",
                    "unit_amount": it["price_cents"],
                    "tax_behavior": "exclusive",
                    "product_data": {
                        "name": f"{it['product_name']} — {it['variant_name']}",
                        "description": f"{it['units_per_pack']} × 4 fl oz bottle" if it["units_per_pack"] > 1 else "4 fl oz bottle",
                        "metadata": {"variant_id": str(it["variant_id"]), "sku": it["sku"]},
                        "images": _image_url(it.get("image_base")),
                    },
                },
                "quantity": it["qty"],
            })
        snapshot.append({"v": it["variant_id"], "q": it["qty"], "p": it["price_cents"], "s": int(bool(it["subscribe"]))})

    ship = tot["shipping_cents"]
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

    metadata = {
        "cart_id": str(cart["id"]),
        "email": email,
        "customer_id": str(customer["id"]) if customer else "",
        "lines": json.dumps(snapshot, separators=(",", ":"))[:500],
        "discount_code_id": str(tot["discount"]["id"]) if tot.get("discount") and tot["discount_cents"] >= 0 and not tot.get("discount_error") else "",
        "discount_code": tot["discount"]["code"] if tot.get("discount") and not tot.get("discount_error") else "",
        "discount_cents": str(tot["discount_cents"]),
        "utm_source": cart.get("utm_source") or "",
        "utm_medium": cart.get("utm_medium") or "",
        "utm_campaign": cart.get("utm_campaign") or "",
        "referral_code": cart.get("referral_code") or "",
        "ip": ip,
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
        "shipping_options": shipping_options,
        "customer_email": email if not (customer and customer.get("stripe_customer_id")) else None,
    }
    if customer and customer.get("stripe_customer_id"):
        params["customer"] = customer["stripe_customer_id"]
        params["customer_update"] = {"shipping": "auto", "address": "auto"}
    if has_sub:
        params["subscription_data"] = {"metadata": metadata}
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
            "UPDATE carts SET stripe_checkout_session_id = ?, checkout_started_at = ?, email = ?, customer_id = COALESCE(customer_id, ?), updated_at = ? WHERE id = ?",
            (session.id, iso(), email, customer["id"] if customer else None, iso(), cart["id"]),
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
        if not (data.get("metadata") or {}).get("lines"):
            # Session created elsewhere (or metadata truncated): pull the expanded line items.
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
        order = orders.record_refund(conn, pi_id, int(data.get("amount_refunded") or 0), event["id"])
        return f"refund recorded on {order['order_number']}" if order else "refund: no matching order"

    if kind in {"customer.subscription.deleted", "customer.subscription.updated"}:
        status = "canceled" if kind.endswith("deleted") else str(data.get("status") or "active")
        with transaction(conn):
            conn.execute("UPDATE subscriptions SET status = ?, canceled_at = CASE WHEN ? = 'canceled' THEN ? ELSE canceled_at END WHERE stripe_subscription_id = ?", (status, status, iso(), data.get("id", "")))
        return f"subscription {status}"

    return "ignored"


def _after_order(conn: sqlite3.Connection, order: dict) -> None:
    """Side effects that must not roll back the order: email + analytics."""
    try:
        lines = orders.items(conn, order["id"])
        with transaction(conn):
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
