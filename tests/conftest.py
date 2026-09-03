"""Test bootstrap. Environment is pinned BEFORE anything under app/ is imported:
app.config reads os.environ at import time (load_dotenv uses override=False, so
these values beat .env). The database is a throw-away SQLite file per pytest
process, seeded once with scripts/seed.py.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRATCH = Path("/tmp/claude-0/-home-user-jack/0e032b0c-f8ad-5259-95e6-43c9467b037d/scratchpad")
SCRATCH.mkdir(parents=True, exist_ok=True)
DB_PATH = SCRATCH / f"pytest-{os.getpid()}.db"
MEDIA_DIR = SCRATCH / f"pytest-{os.getpid()}-media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

TEST_ENV = {
    "SECRET_KEY": "0123456789abcdef" * 4,  # 64 hex chars
    "ENV": "development",
    "DB_PATH": str(DB_PATH),
    "MEDIA_DIR": str(MEDIA_DIR),
    "EMAIL_DRY_RUN": "on",
    "SUBSCRIPTIONS_ENABLED": "on",
    "ADMIN_2FA": "full",
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_PUBLISHABLE_KEY": "pk_test_dummy",
    "STRIPE_WEBHOOK_SECRET": "whsec_test",
    "ADMIN_2FA_REQUIRED": "on",
    "POSTHOG_KEY": "",
    "ADMIN_ALLOW_IPS": "",
    "ADMIN_BASIC_AUTH_USER": "",
    "ADMIN_BASIC_AUTH_PASSWORD": "",
    "COOKIE_SECURE": "off",
    "BASE_URL": "http://testserver",
}
os.environ.update(TEST_ENV)

# Only now is it safe to touch the app package.
from scripts.seed import main as seed_main  # noqa: E402

seed_main()

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password  # noqa: E402


def _cleanup() -> None:
    for p in SCRATCH.glob(f"pytest-{os.getpid()}*"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass


atexit.register(_cleanup)


# ------------------------------------------------------------------ helpers
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def get_csrf(client: TestClient) -> str:
    """GET / and pull the csrf_token hidden input. The token lives in the
    signed session cookie the client now holds, so it stays valid for POSTs."""
    resp = client.get("/")
    assert resp.status_code == 200, resp.text[:300]
    m = _CSRF_RE.search(resp.text)
    assert m, "no csrf_token hidden input on /"
    return m.group(1)


def new_client() -> TestClient:
    """A fresh browser: its own cookie jar, redirects NOT followed so tests can
    assert on Location, server exceptions surfaced as 500 responses."""
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def unique_email(prefix: str = "u") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@gmail.com"


def create_customer(conn, email: str | None = None, password: str | None = "correct horse battery", first_name: str = "Test") -> dict:
    """Insert a customer directly. password=None makes a guest row (empty hash)."""
    email = email or unique_email("cust")
    norm = email.strip().lower()
    cur = conn.execute(
        "INSERT INTO customers(email, email_norm, password_hash, first_name) VALUES (?, ?, ?, ?)",
        (norm, norm, hash_password(password) if password else "", first_name),
    )
    return {"id": int(cur.lastrowid), "email": norm, "password": password}


def create_order(conn, *, customer_id: int | None, email: str, variant_sku: str | None = "QS-1", qty: int = 1, status: str = "paid", **extra) -> dict:
    """Insert an order (and optionally one line) without going through Stripe."""
    number = f"QD-TEST-{uuid.uuid4().hex[:6].upper()}"
    session_id = f"cs_test_{uuid.uuid4().hex}"
    cols = {
        "order_number": number, "customer_id": customer_id, "email": email.lower(), "status": status,
        "stripe_checkout_session_id": session_id, "subtotal_cents": 1600, "total_cents": 2295, "shipping_cents": 695,
        **extra,
    }
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO orders ({names}) VALUES ({marks})", tuple(cols.values()))
    order_id = int(cur.lastrowid)
    if variant_sku:
        v = conn.execute("SELECT v.*, p.name AS product_name, p.dose_interval_days, p.drains_per_unit FROM variants v JOIN products p ON p.id = v.product_id WHERE v.sku = ?", (variant_sku,)).fetchone()
        conn.execute(
            "INSERT INTO order_items(order_id, variant_id, product_id, sku, product_name, variant_name, qty, unit_price_cents, line_total_cents, units_per_pack, dose_interval_days, drains_per_unit)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, v["id"], v["product_id"], v["sku"], v["product_name"], v["name"], qty, v["price_cents"], v["price_cents"] * qty, v["units_per_pack"], v["dose_interval_days"], v["drains_per_unit"]),
        )
    return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def create_admin(conn, *, password: str = "admin-pass-word-1", totp_enabled: bool = False, totp_secret: str = "", backup_codes_json: str = "[]") -> dict:
    username = f"admin_{uuid.uuid4().hex[:8]}"
    email = f"{username}@gmail.com"
    cur = conn.execute(
        "INSERT INTO admin_users(username, email, password_hash, totp_secret, totp_enabled, backup_codes) VALUES (?, ?, ?, ?, ?, ?)",
        (username, email, hash_password(password), totp_secret, int(totp_enabled), backup_codes_json),
    )
    return {"id": int(cur.lastrowid), "username": username, "email": email, "password": password, "totp_secret": totp_secret}


def variant_id(conn, sku: str = "QS-1") -> int:
    return int(conn.execute("SELECT id FROM variants WHERE sku = ?", (sku,)).fetchone()["id"])


def add_to_cart(client: TestClient, token: str, vid: int, qty: int = 1):
    """The fetch() path the storefront JS uses: JSON body + X-CSRF-Token header."""
    return client.post("/cart/add", json={"variant_id": vid, "qty": qty}, headers={"X-CSRF-Token": token, "X-Requested-With": "fetch", "Accept": "application/json"})


def login(client: TestClient, email: str, password: str):
    token = get_csrf(client)
    return client.post("/account/login", data={"email": email, "password": password, "csrf_token": token})


def register(client: TestClient, email: str, password: str, **fields):
    token = get_csrf(client)
    return client.post("/account/register", data={"email": email, "password": password, "csrf_token": token, **fields})


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def client():
    with new_client() as c:
        yield c


@pytest.fixture
def conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def csrf():
    """Usage: token = csrf(client)."""
    return get_csrf


def reset_rate_limits() -> None:
    """Every TestClient shares one client IP, so a limit hit in one test would
    leak into the next. Wipe the shared counter table."""
    c = connect()
    try:
        c.execute("DELETE FROM rate_limits")
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    reset_rate_limits()
    yield


@pytest.fixture
def rate_limit_reset():
    """Explicit handle for tests that need to clear the counters mid-test."""
    return reset_rate_limits
