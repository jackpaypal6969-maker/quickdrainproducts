"""CSRF, the shared rate limiter, email validation, markdown-lite, webhook
signature check, security headers, and docs-off-in-production."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, SCRATCH, TEST_ENV, add_to_cart, get_csrf, unique_email, variant_id
from app.main import app
from app.security import check_rate_limit, validate_email
from app.services import markdown_lite


# ------------------------------------------------------------------- csrf
def test_post_without_csrf_token_is_forbidden(client, conn):
    client.get("/")  # establish a session so the failure is the token, not a missing session
    resp = client.post("/cart/add", data={"variant_id": variant_id(conn), "qty": 1})
    assert resp.status_code == 403


def test_post_with_wrong_csrf_token_is_forbidden(client, conn):
    get_csrf(client)
    resp = client.post("/cart/add", data={"variant_id": variant_id(conn), "qty": 1, "csrf_token": "x" * 43})
    assert resp.status_code == 403


def test_post_with_csrf_header_returns_json(client, conn):
    token = get_csrf(client)
    resp = add_to_cart(client, token, variant_id(conn), 1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["count"] == 1 and "html" in body


# ------------------------------------------------------------- rate limit
def test_sixth_newsletter_signup_within_the_hour_is_refused(client):
    token = get_csrf(client)
    headers = {"Accept": "application/json"}
    for i in range(5):
        resp = client.post("/newsletter", data={"email": unique_email(f"nl{i}"), "csrf_token": token, "website": ""}, headers=headers)
        assert resp.status_code == 200, (i, resp.text)
        assert resp.json()["ok"] is True
    resp = client.post("/newsletter", data={"email": unique_email("nl6"), "csrf_token": token, "website": ""}, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert "Too many" in resp.json()["message"]


def test_check_rate_limit_returns_false_on_limit_plus_one(conn):
    for _ in range(3):
        assert check_rate_limit(conn, "unit-test", "subject", limit=3, window_seconds=3600) is True
    assert check_rate_limit(conn, "unit-test", "subject", limit=3, window_seconds=3600) is False
    # A different subject in the same scope has its own counter.
    assert check_rate_limit(conn, "unit-test", "other", limit=3, window_seconds=3600) is True


# ------------------------------------------------------- email validation
@pytest.mark.parametrize("address", [
    "someone@test.com",
    "someone@example.com",
    "foo@bar.test",
    "someone@mailinator.com",
    "someone@sub.mailinator.com",
    "not-an-email",
    "",
])
def test_reserved_and_disposable_emails_are_rejected(address):
    assert validate_email(address) is None


def test_gmail_address_is_accepted_and_normalized():
    assert validate_email("  Someone@Gmail.com ") == "someone@gmail.com"


def test_extra_blocklist_is_honoured():
    assert validate_email("a@blocked.example.io", {"blocked.example.io"}) is None
    assert validate_email("a@fine.example.io", {"blocked.example.io"}) == "a@fine.example.io"


# ------------------------------------------------------------- markdown
def test_markdown_lite_emits_no_raw_html():
    out = str(markdown_lite.render('<b>bold</b> <img src=x onerror="alert(1)"> **strong**'))
    assert "<b>" not in out and "<img" not in out
    assert "&lt;b&gt;bold&lt;/b&gt;" in out
    assert "<strong>strong</strong>" in out


def test_markdown_lite_drops_javascript_links():
    out = str(markdown_lite.render("[click](javascript:alert(1)) and [ok](https://example.com/x)"))
    assert 'href="javascript:' not in out
    assert "<a" not in out.split(" and ")[0]  # the javascript: link is left as plain text
    assert 'href="https://example.com/x" rel="noopener"' in out
    # Relative and mailto links remain usable.
    assert 'href="/products"' in str(markdown_lite.render("[p](/products)"))




# ----------------------------------------------------------- webhook auth
def test_stripe_webhook_with_garbage_signature_is_rejected(client):
    assert os.environ["STRIPE_WEBHOOK_SECRET"] == "whsec_test"
    resp = client.post("/webhooks/stripe", content=b'{"id":"evt_x","type":"checkout.session.completed"}', headers={"Stripe-Signature": "t=1,v1=garbage", "Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.text == "invalid signature"


def test_stripe_webhook_without_signature_is_rejected(client):
    resp = client.post("/webhooks/stripe", content=b"{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


# --------------------------------------------------------------- headers
def test_security_headers_present_on_home(client):
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_account_pages_are_not_cached(client):
    resp = client.get("/account/login")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ docs
def test_docs_enabled_in_development(client):
    assert app.openapi_url == "/openapi.json"
    assert client.get("/docs").status_code == 200


def test_docs_disabled_in_production():
    env = {**os.environ, **TEST_ENV, "ENV": "production", "DB_PATH": str(SCRATCH / f"pytest-{os.getpid()}-prod.db")}
    code = (
        "import app.main as m; "
        "assert m.app.openapi_url is None, m.app.openapi_url; "
        "assert m.app.docs_url is None; assert m.app.redoc_url is None; "
        "print('docs-off')"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "docs-off" in proc.stdout
