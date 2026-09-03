"""Discount codes, newsletter list, site settings, promo banner."""
from __future__ import annotations

import csv
import io
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import StreamingResponse

from ...db import all_rows, all_settings, one, set_setting, transaction
from ...deps import flash, get_db, ip, redirect, require_admin
from ...security import hash_password, normalize_email, parse_iso, password_policy_error, verify_password
from ...services import audit, discounts
from .common import arender, as_int, dollars_to_cents

router = APIRouter()

SETTING_KEYS = ("promo_banner", "promo_banner_enabled", "newsletter_discount_percent", "newsletter_enabled", "reviews_enabled", "store_notice", "email_blocklist_extra", "subscription_discount_percent", "subscription_intervals")


@router.get("/discounts")
def discount_list(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, """SELECT d.*, (SELECT COUNT(*) FROM discount_redemptions r WHERE r.discount_code_id = d.id) AS redemptions,
              (SELECT COALESCE(SUM(o.total_cents),0) FROM orders o WHERE o.discount_code_id = d.id) AS revenue_cents
              FROM discount_codes d ORDER BY d.created_at DESC LIMIT 500""")]
    return arender(request, "admin/discounts.html", {"discounts": rows, "meta_title": "Discount codes"}, conn, admin)


@router.post("/discounts/save")
def discount_save(request: Request, id: str = Form("0"), code: str = Form(""), kind: str = Form("percent"), value: str = Form("0"), min_subtotal: str = Form(""), max_uses: str = Form(""), restricted_to_email: str = Form(""), starts_at: str = Form(""), expires_at: str = Form(""), channel: str = Form(""), note: str = Form(""), is_active: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    did = as_int(id)
    norm_code = discounts.normalize_code(code)
    if not norm_code or kind not in ("percent", "fixed", "free_shipping"):
        flash(request, "Code and kind are required.", "error")
        return redirect("/admin/discounts")
    if kind == "percent":
        val = max(0, min(as_int(value), 100))
    elif kind == "fixed":
        val = dollars_to_cents(value) or 0
    else:
        val = 0
    data = {
        "code": norm_code,
        "kind": kind,
        "value": val,
        "min_subtotal_cents": dollars_to_cents(min_subtotal) or 0,
        "max_uses": as_int(max_uses) or None,
        "restricted_to_email": normalize_email(restricted_to_email) or None,
        "starts_at": _date(starts_at),
        "expires_at": _date(expires_at, end_of_day=True),
        "channel": channel.strip()[:40],
        "note": note.strip()[:300],
        "is_active": 1 if is_active else 0,
    }
    with transaction(conn):
        before = one(conn, "SELECT * FROM discount_codes WHERE id = ?", (did,)) if did else None
        if did and before:
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE discount_codes SET {sets} WHERE id = ?", (*data.values(), did))
        else:
            if one(conn, "SELECT 1 FROM discount_codes WHERE code = ?", (norm_code,)):
                flash(request, "That code already exists.", "error")
                return redirect("/admin/discounts")
            cur = conn.execute(f"INSERT INTO discount_codes ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})", tuple(data.values()))
            did = int(cur.lastrowid)
        audit.log(conn, "discount.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="discount_code", target_id=did, before=dict(before) if before else None, after=data, ip=ip(request))
    flash(request, f"Code {norm_code} saved.")
    return redirect("/admin/discounts")


def _date(value: str, end_of_day: bool = False) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if len(v) == 10:
        v = v + ("T23:59:59Z" if end_of_day else "T00:00:00Z")
    dt = parse_iso(v)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@router.post("/discounts/{discount_id}/toggle")
def discount_toggle(discount_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT is_active FROM discount_codes WHERE id = ?", (discount_id,))
    if not row:
        raise HTTPException(404)
    with transaction(conn):
        conn.execute("UPDATE discount_codes SET is_active = ? WHERE id = ?", (0 if row["is_active"] else 1, discount_id))
        audit.log(conn, "discount.toggle", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="discount_code", target_id=discount_id, before={"is_active": row["is_active"]}, after={"is_active": 0 if row["is_active"] else 1}, ip=ip(request))
    return redirect("/admin/discounts")


@router.get("/newsletter")
def newsletter(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, "SELECT n.*, d.code, d.usage_count FROM newsletter_subscribers n LEFT JOIN discount_codes d ON d.id = n.welcome_code_id ORDER BY n.created_at DESC LIMIT 500")]
    notify = [dict(r) for r in all_rows(conn, "SELECT s.*, v.sku FROM stock_notifications s JOIN variants v ON v.id = s.variant_id ORDER BY s.created_at DESC LIMIT 200")]
    suppressed = [dict(r) for r in all_rows(conn, "SELECT * FROM email_suppressions ORDER BY created_at DESC LIMIT 200")]
    return arender(request, "admin/newsletter.html", {"subscribers": rows, "notify": notify, "suppressed": suppressed, "meta_title": "Newsletter"}, conn, admin)


@router.get("/newsletter/export.csv")
def newsletter_export(conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "source", "created_at", "unsubscribed_at"])
    for r in all_rows(conn, "SELECT email, source, created_at, unsubscribed_at FROM newsletter_subscribers WHERE unsubscribed_at IS NULL AND email NOT IN (SELECT email FROM email_suppressions) ORDER BY created_at"):
        w.writerow([r["email"], r["source"], r["created_at"], r["unsubscribed_at"] or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=newsletter.csv"})


@router.get("/settings")
def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    admins = [dict(r) for r in all_rows(conn, "SELECT id, username, email, totp_enabled, is_active, last_login_at, failed_attempts, locked_until FROM admin_users ORDER BY id")]
    return arender(request, "admin/settings.html", {"values": all_settings(conn), "admins": admins, "meta_title": "Settings"}, conn, admin)


@router.post("/settings")
async def settings_save(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    form = await request.form()
    before = all_settings(conn)
    with transaction(conn):
        for key in SETTING_KEYS:
            if key in ("promo_banner_enabled", "newsletter_enabled", "reviews_enabled"):
                value = "1" if form.get(key) else "0"
            elif key in ("newsletter_discount_percent", "subscription_discount_percent"):
                value = str(max(0, min(as_int(form.get(key), 10), 90)))
            elif key == "subscription_intervals":
                months = sorted({m for m in (as_int(x) for x in str(form.get(key) or "").split(",")) if 1 <= m <= 12})
                value = ",".join(str(m) for m in months) or "1"
            else:
                value = str(form.get(key) or "").strip()[:2000]
            set_setting(conn, key, value)
        audit.log(conn, "settings.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="settings", before={k: before.get(k) for k in SETTING_KEYS}, after={k: form.get(k) for k in SETTING_KEYS}, ip=ip(request))
    flash(request, "Settings saved.")
    return redirect("/admin/settings")


@router.post("/settings/password")
def admin_password(request: Request, current_password: str = Form(""), password: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    if not verify_password(admin["password_hash"], current_password):
        flash(request, "Your current password was not right.", "error")
        return redirect("/admin/settings")
    err = password_policy_error(password)
    if err:
        flash(request, err, "error")
        return redirect("/admin/settings")
    with transaction(conn):
        conn.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?", (hash_password(password), admin["id"]))
        audit.log(conn, "admin.password_change", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="admin", target_id=admin["id"], ip=ip(request))
    flash(request, "Admin password changed.")
    return redirect("/admin/settings")


@router.post("/settings/admins/{admin_id}/unlock")
def admin_unlock(admin_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    with transaction(conn):
        conn.execute("UPDATE admin_users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (admin_id,))
        audit.log(conn, "admin.unlock", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="admin", target_id=admin_id, ip=ip(request))
    return redirect("/admin/settings")


@router.post("/settings/admins/{admin_id}/reset-2fa")
def admin_reset_2fa(admin_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    with transaction(conn):
        conn.execute("UPDATE admin_users SET totp_secret = '', totp_enabled = 0, backup_codes = '[]' WHERE id = ?", (admin_id,))
        audit.log(conn, "admin.reset_2fa", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="admin", target_id=admin_id, ip=ip(request))
    flash(request, "Authenticator reset; it re-enrolls on next sign-in.")
    return redirect("/admin/settings")
