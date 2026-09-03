"""Customer accounts. Registration never writes to an existing row; guests
claim their account through password reset. Every /account route resolves the
current customer and every {id} route checks ownership."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ..config import settings
from ..db import all_rows, one, transaction
from ..deps import csrf_protect, current_customer, flash, get_db, ip, redirect, render, require_customer
from ..security import (check_rate_limit, hash_password, hash_token, iso, new_token, normalize_email, password_policy_error,
                        utcnow, validate_email, verify_password)
from ..services import audit, emails, orders, stripe_service, subscriptions
from ..services import cart as cart_service

router = APIRouter(prefix="/account", dependencies=[Depends(csrf_protect)])

US_STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()


def _login(request: Request, conn: sqlite3.Connection, customer_id: int) -> None:
    request.state.session["uid"] = customer_id
    cart_service.merge_into_customer(conn, request.state.session, customer_id)
    conn.execute("UPDATE customers SET last_login_at = ? WHERE id = ?", (iso(), customer_id))


# ------------------------------------------------------------------ register
@router.get("/register")
def register_form(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    if customer:
        return redirect("/account")
    return render(request, "account/register.html", {"meta_title": "Create an account"}, conn=conn)


@router.post("/register")
def register(request: Request, email: str = Form(""), password: str = Form(""), first_name: str = Form(""), last_name: str = Form(""), marketing: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    if not check_rate_limit(conn, "register", ip(request), limit=5, window_seconds=3600):
        flash(request, "Too many sign-ups from this connection. Try again later.", "error")
        return redirect("/account/register")
    norm = validate_email(email)
    if not norm:
        flash(request, "Enter a real, non-disposable email address.", "error")
        return redirect("/account/register")
    err = password_policy_error(password)
    if err:
        flash(request, err, "error")
        return redirect("/account/register")
    existing = one(conn, "SELECT id, password_hash FROM customers WHERE email_norm = ?", (norm,))
    if existing:
        # Registration must never write to an existing account (live ATO bug on PPS).
        # One message for both registered and guest rows, so the form does not say
        # which kind of history an address has.
        flash(request, "That email is already with us. Sign in, or use 'Reset password' to set a password — it works for past guest orders too.", "error")
        return redirect("/account/login")
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO customers(email, email_norm, password_hash, first_name, last_name, marketing_opt_in) VALUES (?, ?, ?, ?, ?, ?)",
            (norm, norm, hash_password(password), first_name.strip()[:80], last_name.strip()[:80], 1 if marketing else 0),
        )
        cid = int(cur.lastrowid)
        audit.log(conn, "customer.register", actor_type="customer", actor_id=cid, target_type="customer", target_id=cid, ip=ip(request))
        _login(request, conn, cid)
    flash(request, "Welcome. Your account is ready.")
    return redirect(request.state.session.pop("next", None) or "/account")


# --------------------------------------------------------------------- login
@router.get("/login")
def login_form(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict | None = Depends(current_customer)):
    if customer:
        return redirect("/account")
    return render(request, "account/login.html", {"meta_title": "Sign in"}, conn=conn)


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    norm = normalize_email(email)
    if not check_rate_limit(conn, "login-ip", ip(request), limit=10, window_seconds=900) or not check_rate_limit(conn, "login-email", norm, limit=10, window_seconds=3600):
        flash(request, "Too many sign-in attempts. Wait a few minutes and try again.", "error")
        return redirect("/account/login")
    row = one(conn, "SELECT * FROM customers WHERE email_norm = ? AND is_active = 1 AND deleted_at IS NULL", (norm,))
    if not row or not verify_password(row["password_hash"], password):
        flash(request, "That email and password do not match.", "error")
        return redirect("/account/login")
    with transaction(conn):
        _login(request, conn, int(row["id"]))
    return redirect(request.state.session.pop("next", None) or "/account")


@router.post("/logout")
def logout(request: Request):
    request.state.session.pop("uid", None)
    request.state.session.pop("cart", None)
    return redirect("/")


# ------------------------------------------------------------- password reset
@router.get("/reset")
def reset_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return render(request, "account/reset_request.html", {"meta_title": "Reset password"}, conn=conn)


@router.post("/reset")
def reset_request(request: Request, email: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    norm = normalize_email(email)
    if not check_rate_limit(conn, "reset-ip", ip(request), limit=5, window_seconds=3600) or not check_rate_limit(conn, "reset-email", norm, limit=3, window_seconds=3600):
        flash(request, "Too many reset requests. Try again later.", "error")
        return redirect("/account/reset")
    row = one(conn, "SELECT id, email FROM customers WHERE email_norm = ? AND is_active = 1 AND deleted_at IS NULL", (norm,))
    if row:
        token = new_token(32)
        with transaction(conn):
            conn.execute("INSERT INTO password_resets(customer_id, token_hash, expires_at) VALUES (?, ?, ?)", (row["id"], hash_token(token), iso(utcnow() + timedelta(hours=2))))
        emails.send(conn, row["email"], "password_reset", "Reset your Quick Drain Products password", {"reset_url": f"{settings.base_url}/account/reset/{token}"}, related_type="customer", related_id=row["id"])
    flash(request, "If that email has an account or past orders, a reset link is on its way. It expires in two hours.")
    return redirect("/account/login")


@router.get("/reset/{token}")
def reset_with_token_form(token: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT id FROM password_resets WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?", (hash_token(token), iso()))
    if not row:
        flash(request, "That reset link is invalid or expired. Request a new one.", "error")
        return redirect("/account/reset")
    return render(request, "account/reset_form.html", {"token": token, "meta_title": "Choose a new password"}, conn=conn)


@router.post("/reset/{token}")
def reset_with_token(token: str, request: Request, password: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT pr.id, pr.customer_id FROM password_resets pr WHERE pr.token_hash = ? AND pr.used_at IS NULL AND pr.expires_at > ?", (hash_token(token), iso()))
    if not row:
        flash(request, "That reset link is invalid or expired. Request a new one.", "error")
        return redirect("/account/reset")
    err = password_policy_error(password)
    if err:
        flash(request, err, "error")
        return redirect(f"/account/reset/{token}")
    with transaction(conn):
        conn.execute("UPDATE customers SET password_hash = ? WHERE id = ?", (hash_password(password), row["customer_id"]))
        conn.execute("UPDATE password_resets SET used_at = ? WHERE id = ?", (iso(), row["id"]))
        conn.execute("UPDATE password_resets SET used_at = COALESCE(used_at, ?) WHERE customer_id = ?", (iso(), row["customer_id"]))
        audit.log(conn, "customer.password_reset", actor_type="customer", actor_id=row["customer_id"], target_type="customer", target_id=row["customer_id"], ip=ip(request))
        _login(request, conn, int(row["customer_id"]))
    flash(request, "Password updated. You are signed in.")
    return redirect("/account")


# ----------------------------------------------------------------- dashboard
@router.get("")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    order_rows = orders.list_for_customer(conn, customer["id"])
    addresses = [dict(a) for a in all_rows(conn, "SELECT * FROM addresses WHERE customer_id = ? ORDER BY is_default DESC, id", (customer["id"],))]
    subs = subscriptions.for_customer(conn, customer["id"])
    return render(request, "account/dashboard.html", {"orders": order_rows, "addresses": addresses, "subscriptions": subs, "states": US_STATES, "profile": customer, "stripe_ready": stripe_service.configured(), "meta_title": "Your account"}, conn=conn)


# ------------------------------------------------------------ subscriptions
def _owned_subscription(conn: sqlite3.Connection, sub_id: int, customer: dict) -> dict:
    row = one(conn, "SELECT * FROM subscriptions WHERE id = ? AND customer_id = ?", (sub_id, customer["id"]))
    if not row:
        raise HTTPException(404)
    return dict(row)


@router.post("/subscriptions/{sub_id}/cancel")
def subscription_cancel(sub_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    sub = _owned_subscription(conn, sub_id, customer)
    if sub["status"] in ("canceled", "incomplete_expired"):
        flash(request, "That subscription is already canceled.")
        return redirect("/account")
    try:
        stripe_service.cancel_subscription(sub["stripe_subscription_id"], at_period_end=True)
    except Exception as exc:  # noqa: BLE001
        flash(request, "Stripe could not update the subscription right now. Nothing changed; try again in a minute or contact us.", "error")
        audit.log(conn, "subscription.cancel_failed", actor_type="customer", actor_id=customer["id"], target_type="subscription", target_id=sub_id, after={"error": str(exc)[:200]}, ip=ip(request))
        return redirect("/account")
    with transaction(conn):
        conn.execute("UPDATE subscriptions SET status = 'canceling', cancel_at_period_end = 1, updated_at = ? WHERE id = ?", (iso(), sub_id))
        audit.log(conn, "subscription.cancel", actor_type="customer", actor_id=customer["id"], target_type="subscription", target_id=sub_id, before={"status": sub["status"]}, after={"status": "canceling"}, ip=ip(request))
    flash(request, "Canceled. You keep what you already paid for; nothing renews after this period.")
    return redirect("/account")


@router.post("/subscriptions/{sub_id}/resume")
def subscription_resume(sub_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    sub = _owned_subscription(conn, sub_id, customer)
    if sub["status"] != "canceling":
        return redirect("/account")
    try:
        stripe_service.resume_subscription(sub["stripe_subscription_id"])
    except Exception as exc:  # noqa: BLE001
        flash(request, "Stripe could not update the subscription right now. Try again in a minute.", "error")
        audit.log(conn, "subscription.resume_failed", actor_type="customer", actor_id=customer["id"], target_type="subscription", target_id=sub_id, after={"error": str(exc)[:200]}, ip=ip(request))
        return redirect("/account")
    with transaction(conn):
        conn.execute("UPDATE subscriptions SET status = 'active', cancel_at_period_end = 0, updated_at = ? WHERE id = ?", (iso(), sub_id))
        audit.log(conn, "subscription.resume", actor_type="customer", actor_id=customer["id"], target_type="subscription", target_id=sub_id, ip=ip(request))
    flash(request, "Subscription resumed.")
    return redirect("/account")


@router.post("/subscriptions/portal")
def subscription_portal(request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    """Stripe's hosted billing portal: update the card, change address, see invoices."""
    stripe_customer_id = customer.get("stripe_customer_id") or (one(conn, "SELECT stripe_customer_id FROM subscriptions WHERE customer_id = ? AND stripe_customer_id != '' ORDER BY created_at DESC LIMIT 1", (customer["id"],)) or {"stripe_customer_id": ""})["stripe_customer_id"]
    if not stripe_customer_id or not stripe_service.configured():
        flash(request, "No billing profile found for this account yet.", "error")
        return redirect("/account")
    try:
        url = stripe_service.billing_portal_url(stripe_customer_id, f"{settings.base_url}/account")
    except Exception as exc:  # noqa: BLE001
        flash(request, "Stripe's billing page is not available right now. Contact us to update payment details.", "error")
        audit.log(conn, "subscription.portal_failed", actor_type="customer", actor_id=customer["id"], after={"error": str(exc)[:200]}, ip=ip(request))
        return redirect("/account")
    return redirect(url, status_code=303)


@router.get("/orders/{order_id}")
def order_detail(order_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    order = orders.get_for_customer(conn, order_id, customer["id"])
    if not order:
        raise HTTPException(404)
    items = orders.items(conn, order["id"])
    rmas = [dict(r) for r in all_rows(conn, "SELECT * FROM rma_requests WHERE order_id = ? ORDER BY id DESC", (order["id"],))]
    return render(request, "account/order.html", {"order": order, "items": items, "rmas": rmas, "meta_title": f"Order {order['order_number']}"}, conn=conn)


@router.post("/orders/{order_id}/reorder")
def reorder(order_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    order = orders.get_for_customer(conn, order_id, customer["id"])
    if not order:
        raise HTTPException(404)
    cart = cart_service.get_cart(conn, request.state.session, create=True)
    added = 0
    for it in orders.items(conn, order["id"]):
        if it["variant_id"]:
            ok, _ = cart_service.add_item(conn, cart, int(it["variant_id"]), int(it["qty"]))
            added += int(ok)
    flash(request, "Added to your cart." if added else "Those items are not available right now.", "ok" if added else "error")
    return redirect("/cart")


@router.post("/orders/{order_id}/rma")
def rma_request(order_id: int, request: Request, reason: str = Form(""), details: str = Form(""), conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    order = orders.get_for_customer(conn, order_id, customer["id"])
    if not order:
        raise HTTPException(404)
    if not check_rate_limit(conn, "rma", str(customer["id"]), limit=5, window_seconds=86400):
        flash(request, "You already have return requests open. We will reply by email.", "error")
        return redirect(f"/account/orders/{order_id}")
    reason = reason.strip()[:120] or "Return requested"
    with transaction(conn):
        cur = conn.execute("INSERT INTO rma_requests(order_id, email, reason, details) VALUES (?, ?, ?, ?)", (order["id"], order["email"], reason, details.strip()[:4000]))
        audit.log(conn, "rma.requested", actor_type="customer", actor_id=customer["id"], target_type="rma", target_id=int(cur.lastrowid), after={"order": order["order_number"], "reason": reason}, ip=ip(request))
    if settings.contact_inbox:
        emails.send(conn, settings.contact_inbox, "rma_notification", f"Return request on {order['order_number']}", {"order": order, "reason": reason, "details": details.strip()[:4000]}, related_type="order", related_id=order["id"])
    flash(request, "Return request received. We reply within one business day.")
    return redirect(f"/account/orders/{order_id}")


# ------------------------------------------------------------------ profile
@router.post("/profile")
def update_profile(request: Request, first_name: str = Form(""), last_name: str = Form(""), phone: str = Form(""), conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    with transaction(conn):
        conn.execute("UPDATE customers SET first_name = ?, last_name = ?, phone = ? WHERE id = ?", (first_name.strip()[:80], last_name.strip()[:80], phone.strip()[:30], customer["id"]))
    flash(request, "Profile saved.")
    return redirect("/account")


@router.post("/password")
def change_password(request: Request, current_password: str = Form(""), password: str = Form(""), conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    if not verify_password(customer["password_hash"], current_password):
        flash(request, "Your current password was not right.", "error")
        return redirect("/account")
    err = password_policy_error(password)
    if err:
        flash(request, err, "error")
        return redirect("/account")
    with transaction(conn):
        conn.execute("UPDATE customers SET password_hash = ? WHERE id = ?", (hash_password(password), customer["id"]))
        audit.log(conn, "customer.password_change", actor_type="customer", actor_id=customer["id"], target_type="customer", target_id=customer["id"], ip=ip(request))
    flash(request, "Password changed.")
    return redirect("/account")


@router.post("/marketing")
def marketing_pref(request: Request, opt_in: str = Form(""), conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    with transaction(conn):
        conn.execute("UPDATE customers SET marketing_opt_in = ? WHERE id = ?", (1 if opt_in else 0, customer["id"]))
        if opt_in:
            conn.execute("DELETE FROM email_suppressions WHERE email = ? AND reason = 'unsubscribe'", (customer["email_norm"],))
        else:
            emails.suppress(conn, customer["email_norm"], "unsubscribe", "account preference")
    flash(request, "Email preference saved.")
    return redirect("/account")


# ---------------------------------------------------------------- addresses
@router.post("/addresses")
def add_address(request: Request, name: str = Form(""), line1: str = Form(""), line2: str = Form(""), city: str = Form(""), state: str = Form(""), postal_code: str = Form(""), phone: str = Form(""), label: str = Form("Home"), conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    if not (line1.strip() and city.strip() and state.strip().upper() in US_STATES and postal_code.strip()):
        flash(request, "Street, city, state and ZIP are required.", "error")
        return redirect("/account")
    count = one(conn, "SELECT COUNT(*) AS n FROM addresses WHERE customer_id = ?", (customer["id"],))["n"]
    if count >= 10:
        flash(request, "You can save up to 10 addresses.", "error")
        return redirect("/account")
    with transaction(conn):
        conn.execute(
            "INSERT INTO addresses(customer_id, label, name, line1, line2, city, state, postal_code, country, phone, is_default) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?)",
            (customer["id"], label.strip()[:30] or "Home", name.strip()[:100], line1.strip()[:120], line2.strip()[:120], city.strip()[:80], state.strip().upper(), postal_code.strip()[:12], phone.strip()[:30], 1 if count == 0 else 0),
        )
    flash(request, "Address saved.")
    return redirect("/account")


@router.post("/addresses/{address_id}/delete")
def delete_address(address_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    with transaction(conn):
        conn.execute("DELETE FROM addresses WHERE id = ? AND customer_id = ?", (address_id, customer["id"]))
    return redirect("/account")


@router.post("/addresses/{address_id}/default")
def default_address(address_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), customer: dict = Depends(require_customer)):
    owned = one(conn, "SELECT id FROM addresses WHERE id = ? AND customer_id = ?", (address_id, customer["id"]))
    if not owned:
        raise HTTPException(404)
    with transaction(conn):
        conn.execute("UPDATE addresses SET is_default = 0 WHERE customer_id = ?", (customer["id"],))
        conn.execute("UPDATE addresses SET is_default = 1 WHERE id = ?", (address_id,))
    return redirect("/account")
