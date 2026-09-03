"""Admin sign-in: password -> authenticator (or backup code) -> emailed code.
Nothing under /admin/ opens until all three have passed."""
from __future__ import annotations

import json
import re
from datetime import timedelta

import pyotp

from conftest import create_admin, get_csrf, new_client
from app.security import hash_token, iso, new_backup_codes, new_totp_secret, utcnow

BACKUP_CODE_RE = re.compile(r"\b[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}\b")


def admin_login(client, admin: dict, password: str | None = None):
    token = get_csrf(client)
    return client.post("/admin/login", data={"username": admin["username"], "password": password or admin["password"], "csrf_token": token})


def admin_row(conn, admin_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM admin_users WHERE id = ?", (admin_id,)).fetchone())


def assert_redirect(resp, location: str):
    assert resp.status_code in (302, 303), (resp.status_code, resp.text[:200])
    assert resp.headers["location"] == location


# ---------------------------------------------------------------- gate
def test_anonymous_admin_redirects_to_login(client):
    resp = client.get("/admin/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


def test_anonymous_admin_subpages_redirect_to_login(client):
    for path in ("/admin/orders", "/admin/products", "/admin/settings"):
        resp = client.get(path)
        assert resp.status_code == 302 and resp.headers["location"] == "/admin/login", path


# ------------------------------------------------------------ password
def test_five_wrong_passwords_lock_the_account(client, conn):
    admin = create_admin(conn)
    for _ in range(5):
        assert_redirect(admin_login(client, admin, "wrong-password-xx"), "/admin/login")
    row = admin_row(conn, admin["id"])
    assert row["failed_attempts"] == 5
    assert row["locked_until"] is not None
    # The right password is refused while locked, and the attempt is logged as such.
    assert_redirect(admin_login(client, admin), "/admin/login")
    assert client.get("/admin/").status_code == 302
    stages = [r["stage"] for r in conn.execute("SELECT stage FROM admin_login_attempts WHERE username = ? ORDER BY id", (admin["username"],))]
    assert stages == ["password"] * 5 + ["locked"]


def test_correct_password_sends_new_admin_to_totp_setup(client, conn):
    admin = create_admin(conn)
    assert_redirect(admin_login(client, admin), "/admin/2fa/setup")
    # Password alone opens nothing.
    assert client.get("/admin/").status_code == 302
    ok = conn.execute("SELECT success FROM admin_login_attempts WHERE username = ? ORDER BY id DESC LIMIT 1", (admin["username"],)).fetchone()
    assert ok["success"] == 1


# ------------------------------------------------------------- totp
def test_totp_enrolment_shows_backup_codes_and_enables_totp(client, conn):
    admin = create_admin(conn)
    assert_redirect(admin_login(client, admin), "/admin/2fa/setup")

    setup = client.get("/admin/2fa/setup")
    secret = admin_row(conn, admin["id"])["totp_secret"]
    assert secret, "GET /admin/2fa/setup must persist a secret"
    assert setup.status_code == 200
    assert secret in setup.text

    token = get_csrf(client)
    resp = client.post("/admin/2fa/setup", data={"code": pyotp.TOTP(secret).now(), "csrf_token": token})
    row = admin_row(conn, admin["id"])
    assert row["totp_enabled"] == 1
    assert len(json.loads(row["backup_codes"])) == 10
    assert resp.status_code == 200
    codes = set(BACKUP_CODE_RE.findall(resp.text))
    assert len(codes) == 10
    # Enrolment is not a login: the email step is still pending.
    assert client.get("/admin/").status_code == 302


def test_totp_setup_with_wrong_code_does_not_enable(client, conn):
    admin = create_admin(conn)
    assert_redirect(admin_login(client, admin), "/admin/2fa/setup")
    client.get("/admin/2fa/setup")  # persists the secret (page render itself is covered elsewhere)
    token = get_csrf(client)
    assert_redirect(client.post("/admin/2fa/setup", data={"code": "000000", "csrf_token": token}), "/admin/2fa/setup")
    assert admin_row(conn, admin["id"])["totp_enabled"] == 0


def test_totp_code_then_wrong_email_code_keeps_admin_closed(client, conn):
    secret = new_totp_secret()
    admin = create_admin(conn, totp_enabled=True, totp_secret=secret)
    assert_redirect(admin_login(client, admin), "/admin/2fa")
    token = get_csrf(client)

    assert_redirect(client.post("/admin/2fa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": token}), "/admin/2fa/email")
    # An emailed code was issued (dry run) and stored hashed.
    row = admin_row(conn, admin["id"])
    assert row["email_otp_hash"] and row["email_otp_expires_at"]
    assert client.get("/admin/").status_code == 302

    assert_redirect(client.post("/admin/2fa/email", data={"code": "000000", "csrf_token": token}), "/admin/2fa/email")
    assert client.get("/admin/").status_code == 302
    row = admin_row(conn, admin["id"])
    assert row["failed_attempts"] == 1
    last = conn.execute("SELECT stage, success FROM admin_login_attempts WHERE username = ? ORDER BY id DESC LIMIT 1", (admin["username"],)).fetchone()
    assert (last["stage"], last["success"]) == ("email_otp", 0)


def test_correct_email_code_completes_login(client, conn):
    secret = new_totp_secret()
    admin = create_admin(conn, totp_enabled=True, totp_secret=secret)
    assert_redirect(admin_login(client, admin), "/admin/2fa")
    token = get_csrf(client)
    assert_redirect(client.post("/admin/2fa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": token}), "/admin/2fa/email")

    # The real code went out by (dry-run) email; plant a known one the same way the route stores it.
    conn.execute("UPDATE admin_users SET email_otp_hash = ?, email_otp_expires_at = ? WHERE id = ?", (hash_token("246810"), iso(utcnow() + timedelta(minutes=5)), admin["id"]))
    assert_redirect(client.post("/admin/2fa/email", data={"code": "246810", "csrf_token": token}), "/admin/")

    row = admin_row(conn, admin["id"])
    assert row["email_otp_hash"] == "" and row["failed_attempts"] == 0 and row["last_login_at"]
    audit = conn.execute("SELECT after_json FROM audit_log WHERE action = 'admin.login' AND actor_id = ? ORDER BY id DESC LIMIT 1", (admin["id"],)).fetchone()
    assert json.loads(audit["after_json"])["factors"] == "password+totp+email"
    # The gate is open: /admin/ no longer bounces to the login page.
    resp = client.get("/admin/")
    assert not (resp.status_code in (302, 303) and resp.headers.get("location") == "/admin/login")


def test_admin_dashboard_renders_after_full_login(client, conn):
    secret = new_totp_secret()
    admin = create_admin(conn, totp_enabled=True, totp_secret=secret)
    assert_redirect(admin_login(client, admin), "/admin/2fa")
    token = get_csrf(client)
    assert_redirect(client.post("/admin/2fa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": token}), "/admin/2fa/email")
    conn.execute("UPDATE admin_users SET email_otp_hash = ?, email_otp_expires_at = ? WHERE id = ?", (hash_token("135791"), iso(utcnow() + timedelta(minutes=5)), admin["id"]))
    assert_redirect(client.post("/admin/2fa/email", data={"code": "135791", "csrf_token": token}), "/admin/")
    assert client.get("/admin/").status_code == 200


def test_expired_email_code_is_refused(client, conn):
    secret = new_totp_secret()
    admin = create_admin(conn, totp_enabled=True, totp_secret=secret)
    assert_redirect(admin_login(client, admin), "/admin/2fa")
    token = get_csrf(client)
    assert_redirect(client.post("/admin/2fa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": token}), "/admin/2fa/email")
    conn.execute("UPDATE admin_users SET email_otp_hash = ?, email_otp_expires_at = ? WHERE id = ?", (hash_token("999999"), iso(utcnow() - timedelta(minutes=1)), admin["id"]))
    assert_redirect(client.post("/admin/2fa/email", data={"code": "999999", "csrf_token": token}), "/admin/2fa/email")
    assert client.get("/admin/").status_code == 302


# ------------------------------------------------------------ backup code
def test_backup_code_is_accepted_in_place_of_totp_on_fresh_login(conn):
    codes, hashes_json = new_backup_codes()
    admin = create_admin(conn, totp_enabled=True, totp_secret=new_totp_secret(), backup_codes_json=hashes_json)

    with new_client() as fresh:
        assert_redirect(admin_login(fresh, admin), "/admin/2fa")
        token = get_csrf(fresh)
        assert_redirect(fresh.post("/admin/2fa", data={"code": codes[0], "csrf_token": token}), "/admin/2fa/email")
        # Email step still pending, so the admin stays closed.
        assert fresh.get("/admin/").status_code == 302

    row = admin_row(conn, admin["id"])
    remaining = json.loads(row["backup_codes"])
    assert len(remaining) == 9
    last = conn.execute("SELECT stage, success FROM admin_login_attempts WHERE username = ? ORDER BY id DESC LIMIT 1", (admin["username"],)).fetchone()
    assert (last["stage"], last["success"]) == ("backup", 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'admin.backup_code_used' AND actor_id = ?", (admin["id"],)).fetchone()["n"] == 1

    # The same code cannot be used twice.
    with new_client() as again:
        assert_redirect(admin_login(again, admin), "/admin/2fa")
        token = get_csrf(again)
        assert_redirect(again.post("/admin/2fa", data={"code": codes[0], "csrf_token": token}), "/admin/2fa")
    assert len(json.loads(admin_row(conn, admin["id"])["backup_codes"])) == 9


def test_wrong_totp_code_is_refused_and_counted(client, conn):
    admin = create_admin(conn, totp_enabled=True, totp_secret=new_totp_secret())
    assert_redirect(admin_login(client, admin), "/admin/2fa")
    token = get_csrf(client)
    assert_redirect(client.post("/admin/2fa", data={"code": "123456", "csrf_token": token}), "/admin/2fa")
    assert admin_row(conn, admin["id"])["failed_attempts"] == 1
    assert client.get("/admin/2fa/email").status_code in (302, 303)  # a_totp never set
