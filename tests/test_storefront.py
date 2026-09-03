"""Public pages: JSON-LD escaping, sitemap/robots, reorder links, lifecycle idempotency."""
from __future__ import annotations

from datetime import timedelta

from conftest import create_order, extra_variant, unique_email
from app.security import iso, new_token, utcnow
from app.services import lifecycle

XSS_BODY = "</script><script>alert(1)</script>"


# ------------------------------------------------------------- json-ld
def test_review_body_cannot_close_the_jsonld_script(client, conn):
    pid = conn.execute("SELECT id FROM products WHERE slug = 'drain-shot'").fetchone()["id"]
    conn.execute(
        "INSERT INTO reviews(product_id, author_name, rating, title, body, status, is_verified) VALUES (?, 'Mallory', 5, 'nice', ?, 'approved', 1)",
        (pid, XSS_BODY),
    )
    resp = client.get("/products/drain-shot")
    assert resp.status_code == 200
    html = resp.text
    assert "</script><script>" not in html
    assert "alert(1)" in html  # the review is on the page...
    # ...inside the ld+json as unicode escapes:
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)" in html
    # ...and in the visible review as HTML entities:
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The raw tag never appears in any form.
    assert "<script>alert(1)</script>" not in html


def test_home_page_jsonld_is_escaped_too(client, conn):
    pid = conn.execute("SELECT id FROM products WHERE slug = 'drain-shot'").fetchone()["id"]
    conn.execute("INSERT INTO reviews(product_id, author_name, rating, body, status) VALUES (?, '<b>Bob</b>', 4, ?, 'approved')", (pid, XSS_BODY))
    html = client.get("/").text
    assert "</script><script>" not in html
    assert "<b>Bob</b>" not in html


# ------------------------------------------------------------- seo files
def test_sitemap_lists_product(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "/products/drain-shot</loc>" in resp.text
    assert "<loc>http://testserver/</loc>" in resp.text


def test_robots_disallows_admin(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    assert "Disallow: /admin" in lines
    assert "Disallow: /account" in lines
    assert "Sitemap: http://testserver/sitemap.xml" in lines


# --------------------------------------------------------------- reorder
def test_reorder_link_with_valid_token_fills_cart(client, conn):
    order = create_order(conn, customer_id=None, email=unique_email("reorder"), variant_sku="DS-12", qty=2)
    token = lifecycle._reorder_token(order)
    assert lifecycle.reorder_token_valid(order, token)

    resp = client.get(f"/reorder/{order['order_number']}/{token}")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cart"
    assert client.get("/cart/drawer").json()["count"] == 2
    # The link is case-insensitive on the order number.
    assert client.get(f"/reorder/{order['order_number'].lower()}/{token}").status_code == 303
    assert client.get("/cart/drawer").json()["count"] == 4


def test_reorder_link_with_wrong_token_is_404(client, conn):
    order = create_order(conn, customer_id=None, email=unique_email("reorder"))
    assert client.get(f"/reorder/{order['order_number']}/{new_token(16)}").status_code == 404
    assert client.get(f"/reorder/{order['order_number']}/").status_code == 404
    assert client.get("/cart/drawer").json()["count"] == 0


def test_reorder_token_is_bound_to_the_order(conn):
    a = create_order(conn, customer_id=None, email=unique_email("ra"))
    b = create_order(conn, customer_id=None, email=unique_email("rb"))
    assert not lifecycle.reorder_token_valid(b, lifecycle._reorder_token(a))
    assert not lifecycle.reorder_token_valid(a, "")


# ------------------------------------------------------------- lifecycle
def email_log_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM email_log").fetchone()["n"])


def test_lifecycle_run_all_sends_nothing_the_second_time(conn):
    # One trigger per lifecycle step.
    delivered = create_order(conn, customer_id=None, email=unique_email("lc-review"), status="delivered")
    cart_email = unique_email("lc-cart")
    cart_token = new_token(24)
    conn.execute(
        "INSERT INTO carts(token, email, checkout_started_at) VALUES (?, ?, ?)",
        (cart_token, cart_email, iso(utcnow() - timedelta(hours=10))),
    )
    cart_id = conn.execute("SELECT id FROM carts WHERE token = ?", (cart_token,)).fetchone()["id"]
    conn.execute("INSERT INTO cart_items(cart_id, variant_id, qty) VALUES (?, ?, 1)", (cart_id, extra_variant(conn)))
    conn.execute("INSERT INTO stock_notifications(email, variant_id) VALUES (?, ?)", (unique_email("lc-stock"), extra_variant(conn, "TST-6", "6-pack (test)", 6, 7800, 40)))

    before = email_log_count(conn)
    first = lifecycle.run_all(conn)
    assert -1 not in first.values(), first
    assert first["reviews"] >= 1 and first["abandoned"] >= 1 and first["restock"] >= 1
    after_first = email_log_count(conn)
    assert after_first - before >= 3

    # Markers were written...
    assert conn.execute("SELECT review_invite_sent_at FROM orders WHERE id = ?", (delivered["id"],)).fetchone()["review_invite_sent_at"]
    assert conn.execute("SELECT abandoned_email_sent_at FROM carts WHERE id = ?", (cart_id,)).fetchone()["abandoned_email_sent_at"]
    assert conn.execute("SELECT COUNT(*) AS n FROM stock_notifications WHERE notified_at IS NULL").fetchone()["n"] == 0

    # ...so a second pass is a no-op.
    second = lifecycle.run_all(conn)
    assert second == {"abandoned": 0, "reorder": 0, "reviews": 0, "restock": 0}
    assert email_log_count(conn) == after_first


def test_reorder_reminder_waits_for_the_dose_interval(conn):
    """A 30-day bottle bought today is not due; one bought 26 days ago is."""
    extra_variant(conn, "TST-1", "single bottle (test)", 1, 1000, 100)
    fresh = create_order(conn, customer_id=None, email=unique_email("lc-fresh"), variant_sku="TST-1")
    old_email = unique_email("lc-old")
    old = create_order(conn, customer_id=None, email=old_email, variant_sku="TST-1", created_at=iso(utcnow() - timedelta(days=26)))
    lifecycle.reorder_reminders(conn)
    assert conn.execute("SELECT reorder_reminder_sent_at FROM orders WHERE id = ?", (fresh["id"],)).fetchone()["reorder_reminder_sent_at"] is None
    assert conn.execute("SELECT reorder_reminder_sent_at FROM orders WHERE id = ?", (old["id"],)).fetchone()["reorder_reminder_sent_at"] is not None
    sent = conn.execute("SELECT COUNT(*) AS n FROM email_log WHERE template = 'reorder_reminder' AND to_email = ?", (old_email,)).fetchone()["n"]
    assert sent == 1
