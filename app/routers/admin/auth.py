"""Admin login: password → authenticator code (or backup code) → emailed code.
Lockout after repeated failures, shared rate limit per IP, every attempt logged."""
from __future__ import annotations

import base64
import io
import sqlite3

import qrcode
from fastapi import APIRouter, Depends, Form, Request

from ...config import settings
from ...db import one, transaction
from ...deps import csrf_protect, current_admin, flash, get_db, ip, redirect, render
from .catalog import dashboard
from ...security import (check_rate_limit, consume_backup_code, iso, lockout_remaining, new_backup_codes, new_email_otp,
                         new_totp_secret, record_admin_failure, record_admin_success, totp_uri, verify_email_otp, verify_password,
                         verify_totp)
from ...services import audit, emails

router = APIRouter(dependencies=[Depends(csrf_protect)])


def _attempt(conn: sqlite3.Connection, request: Request, username: str, success: bool, stage: str) -> None:
    conn.execute("INSERT INTO admin_login_attempts(ip, username, success, stage) VALUES (?, ?, ?, ?)", (ip(request), username[:60], int(success), stage))


def _pending_admin(request: Request, conn: sqlite3.Connection) -> dict | None:
    aid = request.state.session.get("aid")
    if not aid:
        return None
    row = one(conn, "SELECT * FROM admin_users WHERE id = ? AND is_active = 1", (aid,))
    return dict(row) if row else None


def _reset_admin_session(request: Request) -> None:
    for k in ("aid", "a_totp", "a2", "a_at"):
        request.state.session.pop(k, None)


def _send_email_code(conn: sqlite3.Connection, admin: dict) -> None:
    code, code_hash, expires = new_email_otp()
    conn.execute("UPDATE admin_users SET email_otp_hash = ?, email_otp_expires_at = ? WHERE id = ?", (code_hash, expires, admin["id"]))
    emails.send(conn, admin["email"], "admin_code", f"{settings.app_name} admin sign-in code", {"code": code, "username": admin["username"]}, related_type="admin", related_id=admin["id"])


@router.get("/login")
def login_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    _reset_admin_session(request)
    return render(request, "admin/login.html", {"meta_title": "Admin sign in", "no_index": True}, conn=conn)


@router.get("/")
def admin_home(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """/admin is the one address to remember: the dashboard when signed in,
    the sign-in form (not a redirect) when not."""
    admin = current_admin(request, conn)
    if not admin:
        return render(request, "admin/login.html", {"meta_title": "Admin sign in", "no_index": True}, conn=conn)
    return dashboard(request, conn, admin)


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    _reset_admin_session(request)
    if not check_rate_limit(conn, "admin-login", ip(request), limit=10, window_seconds=900):
        flash(request, "Too many attempts from this address. Wait 15 minutes.", "error")
        return redirect("/admin/login")
    row = one(conn, "SELECT * FROM admin_users WHERE username = ? AND is_active = 1", (username.strip(),))
    with transaction(conn):
        if row and lockout_remaining(row["locked_until"]):
            _attempt(conn, request, username, False, "locked")
            flash(request, f"This account is locked for {lockout_remaining(row['locked_until']) // 60 + 1} more minutes.", "error")
            return redirect("/admin/login")
        if not row or not verify_password(row["password_hash"], password):
            _attempt(conn, request, username, False, "password")
            if row:
                record_admin_failure(conn, row["id"])
            flash(request, "Those details do not match.", "error")
            return redirect("/admin/login")
        _attempt(conn, request, username, True, "password")
    request.state.session["aid"] = row["id"]
    request.state.session["a_at"] = iso()
    if not settings.admin_totp_required:
        # ADMIN_2FA=off: password only. Loud in the admin and in final_check; not for real traffic.
        with transaction(conn):
            record_admin_success(conn, row["id"])
            audit.log(conn, "admin.login", actor_type="admin", actor_id=row["id"], actor_name=row["username"], ip=ip(request), after={"factors": "password"})
        request.state.session["a_totp"] = True
        request.state.session["a2"] = True
        return redirect("/admin/")
    if not row["totp_enabled"]:
        return redirect("/admin/2fa/setup")
    return redirect("/admin/2fa")


@router.get("/2fa/setup")
def totp_setup(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin:
        return redirect("/admin/login")
    if admin["totp_enabled"]:
        return redirect("/admin/2fa")
    secret = admin["totp_secret"] or new_totp_secret()
    if not admin["totp_secret"]:
        with transaction(conn):
            conn.execute("UPDATE admin_users SET totp_secret = ? WHERE id = ?", (secret, admin["id"]))
    uri = totp_uri(secret, admin["username"])
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return render(request, "admin/totp_setup.html", {"qr": qr_data, "secret": secret, "meta_title": "Set up authenticator", "no_index": True}, conn=conn)


@router.post("/2fa/setup")
def totp_setup_confirm(request: Request, code: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin:
        return redirect("/admin/login")
    if not verify_totp(admin["totp_secret"], code):
        flash(request, "That code did not match. Scan the QR again and enter the current 6 digits.", "error")
        return redirect("/admin/2fa/setup")
    codes, hashes_json = new_backup_codes()
    with transaction(conn):
        conn.execute("UPDATE admin_users SET totp_enabled = 1, backup_codes = ? WHERE id = ?", (hashes_json, admin["id"]))
        audit.log(conn, "admin.totp_enrolled", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="admin", target_id=admin["id"], ip=ip(request))
        request.state.session["a_totp"] = True
        if not settings.admin_2fa_required:
            request.state.session["a2"] = True
            record_admin_success(conn, admin["id"])
            audit.log(conn, "admin.login", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], ip=ip(request), after={"factors": "password+totp"})
    if settings.admin_2fa_required:
        _send_email_code(conn, admin)
    return render(request, "admin/backup_codes.html", {"codes": codes, "meta_title": "Backup codes", "no_index": True}, conn=conn)


@router.get("/2fa")
def totp_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin:
        return redirect("/admin/login")
    if not admin["totp_enabled"]:
        return redirect("/admin/2fa/setup")
    return render(request, "admin/totp.html", {"meta_title": "Authenticator code", "no_index": True}, conn=conn)


@router.post("/2fa")
def totp_verify(request: Request, code: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin:
        return redirect("/admin/login")
    if not check_rate_limit(conn, "admin-2fa", ip(request), limit=10, window_seconds=900):
        _reset_admin_session(request)
        flash(request, "Too many code attempts. Sign in again in 15 minutes.", "error")
        return redirect("/admin/login")
    ok = verify_totp(admin["totp_secret"], code)
    stage = "totp"
    with transaction(conn):
        if not ok:
            updated = consume_backup_code(admin["backup_codes"], code)
            if updated is not None:
                ok = True
                stage = "backup"
                conn.execute("UPDATE admin_users SET backup_codes = ? WHERE id = ?", (updated, admin["id"]))
                audit.log(conn, "admin.backup_code_used", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="admin", target_id=admin["id"], ip=ip(request))
        _attempt(conn, request, admin["username"], ok, stage)
        if not ok:
            record_admin_failure(conn, admin["id"])
            flash(request, "That code did not match.", "error")
            return redirect("/admin/2fa")
        request.state.session["a_totp"] = True
        if settings.admin_2fa_required:
            pass  # emailed below, after the transaction commits
        else:
            request.state.session["a2"] = True
            record_admin_success(conn, admin["id"])
            audit.log(conn, "admin.login", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], ip=ip(request), after={"factors": "password+totp"})
            return redirect("/admin/")
    _send_email_code(conn, admin)
    return redirect("/admin/2fa/email")


@router.get("/2fa/email")
def email_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin or not request.state.session.get("a_totp"):
        return redirect("/admin/login")
    return render(request, "admin/email_code.html", {"email_hint": _mask(admin["email"]), "meta_title": "Email code", "no_index": True}, conn=conn)


@router.post("/2fa/email/resend")
def email_resend(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin or not request.state.session.get("a_totp"):
        return redirect("/admin/login")
    if not check_rate_limit(conn, "admin-email-resend", str(admin["id"]), limit=3, window_seconds=600):
        flash(request, "Wait a few minutes before requesting another code.", "error")
        return redirect("/admin/2fa/email")
    _send_email_code(conn, admin)
    flash(request, "A new code was sent.")
    return redirect("/admin/2fa/email")


@router.post("/2fa/email")
def email_verify(request: Request, code: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    admin = _pending_admin(request, conn)
    if not admin or not request.state.session.get("a_totp"):
        return redirect("/admin/login")
    if not check_rate_limit(conn, "admin-email-code", ip(request), limit=8, window_seconds=900):
        _reset_admin_session(request)
        flash(request, "Too many code attempts. Sign in again in 15 minutes.", "error")
        return redirect("/admin/login")
    ok = verify_email_otp(admin["email_otp_hash"], admin["email_otp_expires_at"], code)
    with transaction(conn):
        _attempt(conn, request, admin["username"], ok, "email_otp")
        if not ok:
            record_admin_failure(conn, admin["id"])
            flash(request, "That code did not match or has expired.", "error")
            return redirect("/admin/2fa/email")
        conn.execute("UPDATE admin_users SET email_otp_hash = '', email_otp_expires_at = NULL WHERE id = ?", (admin["id"],))
        record_admin_success(conn, admin["id"])
        audit.log(conn, "admin.login", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], ip=ip(request), after={"factors": "password+totp+email"})
    request.state.session["a2"] = True
    return redirect("/admin/")


@router.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    _reset_admin_session(request)
    return redirect("/admin/login")


def _mask(email: str) -> str:
    if "@" not in email:
        return "your email"
    user, domain = email.split("@", 1)
    return f"{user[:2]}…@{domain}"
