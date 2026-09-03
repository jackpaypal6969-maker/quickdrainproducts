"""Cart, discount apply, checkout start, success page, resume and reorder links."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse

from ..config import settings
from ..db import all_rows, one, transaction
from ..deps import csrf_protect, current_customer, flash, get_db, ip, redirect, render
from ..jinja_env import env
from ..security import check_rate_limit, iso, normalize_email, validate_email
from ..services import analytics, discounts, orders
from ..services import cart as cart_service
from ..services import lifecycle, stripe_service

router = APIRouter(dependencies=[Depends(csrf_protect)])


def _wants_json(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", "")


async def _payload(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {k: form.get(k) for k in form.keys()}


def _drawer_html(request: Request, conn: sqlite3.Connection, cart: dict | None) -> str:
    tot = cart_service.totals(conn, cart) if cart else {"items": [], "subtotal_cents": 0, "discount_cents": 0, "shipping_cents": 0, "total_estimate_cents": 0, "count": 0, "free_shipping_gap_cents": settings.free_shipping_threshold_cents, "discount": None, "discount_error": ""}
    return env.get_template("components/cart_drawer_body.html").render(totals=tot, csrf_token=request.state.session.get("csrf", ""), settings=settings)


@router.get("/cart")
def cart_page(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    cart = cart_service.get_cart(conn, request.state.session)
    email = (customer or {}).get("email") or (cart or {}).get("email") or ""
    tot = cart_service.totals(conn, cart, email) if cart else None
    return render(request, "pages/cart.html", {
        "totals": tot,
        "checkout_email": email,
        "stripe_ready": stripe_service.configured(),
        "meta_title": f"Your cart | {settings.app_name}",
    }, conn=conn)


@router.get("/cart/drawer")
def cart_drawer(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    cart = cart_service.get_cart(conn, request.state.session)
    return JSONResponse({"ok": True, "count": cart_service.count(conn, request.state.session), "html": _drawer_html(request, conn, cart)})


@router.post("/cart/add")
async def cart_add(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    data = await _payload(request)
    try:
        variant_id = int(data.get("variant_id") or 0)
        qty = int(data.get("qty") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose an option first.")
    raw_sub = str(data.get("subscribe") or "0").strip().lower()
    try:
        interval = int(raw_sub) if raw_sub not in {"true", "on"} else int(data.get("interval") or 1)
    except ValueError:
        interval = 0
    cart = cart_service.get_cart(conn, request.state.session, create=True)
    ok, message = cart_service.add_item(conn, cart, variant_id, qty, interval)
    if ok:
        variant = one(conn, "SELECT v.sku, v.price_cents, p.slug FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?", (variant_id,))
        analytics.capture(conn, "add_to_cart", request.state.session.get("cart", ""), {"sku": variant["sku"] if variant else "", "qty": qty, "value_cents": (variant["price_cents"] * qty) if variant else 0})
    if _wants_json(request):
        status = 200 if ok else 409
        return JSONResponse({"ok": ok, "message": message, "count": cart_service.count(conn, request.state.session), "html": _drawer_html(request, conn, cart)}, status_code=status)
    flash(request, message, "ok" if ok else "error")
    return redirect("/cart")


@router.post("/cart/update")
async def cart_update(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    data = await _payload(request)
    cart = cart_service.get_cart(conn, request.state.session)
    if cart:
        try:
            cart_service.set_qty(conn, cart, int(data.get("item_id") or 0), int(data.get("qty") or 0))
        except (TypeError, ValueError):
            pass
    if _wants_json(request):
        return JSONResponse({"ok": True, "count": cart_service.count(conn, request.state.session), "html": _drawer_html(request, conn, cart)})
    return redirect("/cart")


@router.post("/cart/remove")
async def cart_remove(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    data = await _payload(request)
    cart = cart_service.get_cart(conn, request.state.session)
    if cart:
        try:
            cart_service.remove_item(conn, cart, int(data.get("item_id") or 0))
        except (TypeError, ValueError):
            pass
    if _wants_json(request):
        return JSONResponse({"ok": True, "count": cart_service.count(conn, request.state.session), "html": _drawer_html(request, conn, cart)})
    return redirect("/cart")


@router.post("/cart/discount")
async def cart_discount(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    data = await _payload(request)
    if not check_rate_limit(conn, "discount", ip(request), limit=12, window_seconds=600):
        flash(request, "Too many attempts. Try again in a few minutes.", "error")
        return redirect("/cart")
    cart = cart_service.get_cart(conn, request.state.session, create=True)
    code = discounts.find(conn, str(data.get("code") or ""))
    email = (customer or {}).get("email") or str(data.get("email") or cart.get("email") or "")
    if not code:
        flash(request, "That code was not found.", "error")
        return redirect("/cart")
    tot = cart_service.totals(conn, cart, email)
    ok, reason = discounts.validate(code, email=email, subtotal_cents=tot["subtotal_cents"])
    if not ok:
        flash(request, reason, "error")
        return redirect("/cart")
    with transaction(conn):
        conn.execute("UPDATE carts SET discount_code_id = ?, email = CASE WHEN ? != '' THEN ? ELSE email END, updated_at = ? WHERE id = ?", (code["id"], normalize_email(email), normalize_email(email), iso(), cart["id"]))
    flash(request, f"Code {code['code']} applied.")
    return redirect("/cart")


@router.post("/cart/discount/remove")
async def cart_discount_remove(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    cart = cart_service.get_cart(conn, request.state.session)
    if cart:
        with transaction(conn):
            conn.execute("UPDATE carts SET discount_code_id = NULL, updated_at = ? WHERE id = ?", (iso(), cart["id"]))
    return redirect("/cart")


@router.get("/cart/resume/{token}")
def cart_resume(token: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT * FROM carts WHERE token = ? AND converted_order_id IS NULL", (token,))
    if row:
        request.state.session["cart"] = row["token"]
        flash(request, "Your cart is right where you left it.")
    return redirect("/cart")


@router.post("/checkout")
async def checkout_start(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    data = await _payload(request)
    if not check_rate_limit(conn, "checkout", ip(request), limit=15, window_seconds=3600):
        flash(request, "Too many checkout attempts. Try again shortly.", "error")
        return redirect("/cart")
    cart = cart_service.get_cart(conn, request.state.session)
    if not cart:
        return redirect("/cart")
    email = (customer or {}).get("email") or validate_email(str(data.get("email") or ""))
    if not email:
        flash(request, "Enter a real email address for your receipt.", "error")
        return redirect("/cart")
    tot = cart_service.totals(conn, cart, email)
    if not tot["items"]:
        return redirect("/cart")
    if not stripe_service.configured():
        flash(request, "Checkout is not configured yet (Stripe keys missing).", "error")
        return redirect("/cart")
    if tot["discount_error"]:
        with transaction(conn):
            conn.execute("UPDATE carts SET discount_code_id = NULL WHERE id = ?", (cart["id"],))
        flash(request, f"Discount removed: {tot['discount_error']}", "error")
        return redirect("/cart")
    try:
        session = stripe_service.create_checkout_session(conn, cart, tot, email=email, customer=customer, ip=ip(request))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/cart")
    except Exception as exc:  # noqa: BLE001 - stripe errors
        flash(request, "Stripe could not start checkout. Nothing was charged. Try again in a moment.", "error")
        import logging
        logging.getLogger("qd.checkout").exception("checkout start failed: %s", exc)
        return redirect("/cart")
    return redirect(session.url, status_code=303)


@router.get("/checkout/success")
def checkout_success(request: Request, session_id: str = "", conn: sqlite3.Connection = Depends(get_db)):
    """Reads the order the webhook created. Never creates one."""
    order = orders.get_by_session(conn, session_id) if session_id else None
    if order:
        if request.state.session.get("cart"):
            cart = one(conn, "SELECT id, converted_order_id FROM carts WHERE token = ?", (request.state.session["cart"],))
            if cart and cart["converted_order_id"] == order["id"]:
                request.state.session.pop("cart", None)
        items = orders.items(conn, order["id"])
        return render(request, "pages/order_confirmation.html", {"order": order, "items": items, "meta_title": f"Order {order['order_number']} confirmed"}, conn=conn)
    attempts = int(request.query_params.get("n", "0") or 0)
    return render(request, "pages/checkout_pending.html", {"session_id": session_id, "attempts": attempts, "retry": attempts < 10, "meta_title": "Confirming your payment"}, conn=conn)


@router.get("/reorder/{number}/{token}")
def reorder_link(number: str, token: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    order = one(conn, "SELECT * FROM orders WHERE order_number = ?", (number.upper(),))
    if not order or not lifecycle.reorder_token_valid(dict(order), token):
        raise HTTPException(404)
    cart = cart_service.get_cart(conn, request.state.session, create=True)
    added = 0
    for it in all_rows(conn, "SELECT variant_id, qty FROM order_items WHERE order_id = ? AND variant_id IS NOT NULL", (order["id"],)):
        ok, _ = cart_service.add_item(conn, cart, int(it["variant_id"]), int(it["qty"]), 0)
        added += int(ok)
    flash(request, "Your last order is in the cart." if added else "Those items are not available right now.", "ok" if added else "error")
    return redirect("/cart")
