"""Build your box: a month's supply. The page renders with its config block, the
builder-only single-bottle SKU never shows on the product page, and the cart
line it posts (one bottle per drain, monthly subscription) prices per bottle."""
from __future__ import annotations

import json
import re

from conftest import get_csrf, variant_id
from app.db import _v5_monthly_box, transaction
from app.services import cart as cart_service
from app.services import catalog

CONFIG_RE = re.compile(r'<script type="application/json" id="box-config">(.*?)</script>', re.S)


def _config(html: str) -> dict:
    m = CONFIG_RE.search(html)
    assert m, "no #box-config block"
    return json.loads(m.group(1))


def test_page_renders_with_monthly_config(client, conn):
    resp = client.get("/build-your-box")
    assert resp.status_code == 200
    html = resp.text
    vid = variant_id(conn, "DS-1")
    v = conn.execute("SELECT price_cents, units_per_pack, builder_only FROM variants WHERE id = ?", (vid,)).fetchone()
    assert v["units_per_pack"] == 1 and v["builder_only"] == 1
    cfg = _config(html)
    assert cfg["variantId"] == vid and cfg["sku"] == "DS-1" and cfg["unitsPerPack"] == 1
    assert cfg["priceCents"] == v["price_cents"] == cfg["perBottleCents"] == 1000
    assert cfg["subPercent"] == 10 and cfg["subPriceCents"] == 900
    assert cfg["subscriptionsEnabled"] is True and cfg["interval"] == 1
    # the form app.js posts, and the page's own script
    assert 'data-add-form' in html and f'name="variant_id" value="{vid}"' in html
    assert 'data-box-subscribe' in html and 'data-box-delivery' in html
    assert '/static/js/box.js' in html
    # copy is monthly, not yearly
    assert "We size the month." in html and "every month" in html
    assert "size the year" not in html and "Renews every 12 months" not in html
    # six fixture rows, each with a 44px stepper pair, and a 20-bottle rack per row
    assert html.count('data-fixture="') == 6
    assert html.count('data-step="1"') == 6 and html.count('data-step="-1"') == 6
    assert html.count('class="box-cell"') == 6 * 20
    assert 'data-preset=' in html and "3-bath home" in html


def test_builder_sku_is_hidden_from_the_product_page(client, conn):
    p = catalog.get_product(conn, "drain-shot")
    assert [v["sku"] for v in p["variants"]] == ["DS-12"]
    html = client.get("/products/drain-shot").text
    assert "Bottle · one drain, one month" not in html and 'name="variant_id" value="%d"' % variant_id(conn, "DS-1") not in html
    # ...but the builder variant carries subscribe-and-save pricing
    b = catalog.builder_variant(conn, p)
    assert b["sku"] == "DS-1" and b["sub_price_cents"] == 900 and b["sub_percent"] == 10
    # and the product page still offers only the yearly interval
    assert p["subscriptions"]["intervals"] == [12]
    assert p["subscriptions"]["builder_interval"] == 1 and p["subscriptions"]["allowed"] == [1, 12]


def test_page_hides_subscribe_choice_when_subscriptions_are_off(client, monkeypatch):
    real = catalog.subscription_config
    monkeypatch.setattr(catalog, "subscription_config", lambda conn: real(conn) | {"enabled": False})
    html = client.get("/build-your-box").text
    assert _config(html)["subscriptionsEnabled"] is False
    assert "data-box-delivery" not in html
    assert 'name="subscribe" value="0"' in html


def test_sitemap_lists_the_builder(client):
    assert "<loc>http://testserver/build-your-box</loc>" in client.get("/sitemap.xml").text


def test_seven_drain_box_posts_as_seven_bottles_monthly(client, conn):
    token = get_csrf(client)
    vid = variant_id(conn, "DS-1")
    resp = client.post(
        "/cart/add",
        data={"variant_id": vid, "qty": 7, "subscribe": 1, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["ok"] is True and body["count"] == 7
    assert "every month" in body["message"] and "every 1 month" not in body["message"]

    item = conn.execute("SELECT cart_id FROM cart_items WHERE variant_id = ? AND qty = 7 AND subscribe = 1 ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
    assert item
    cart = dict(conn.execute("SELECT * FROM carts WHERE id = ?", (item["cart_id"],)).fetchone())
    tot = cart_service.totals(conn, cart)
    assert tot["count"] == 7 and tot["has_subscription"] and tot["subscription_interval"] == 1
    assert tot["subtotal_cents"] == 7 * 900
    assert tot["subscription_savings_cents"] == 7 * 100
    line = tot["items"][0]
    assert line["qty"] == 7 and line["price_cents"] == 900 and line["line_total_cents"] == 6300

    # the cart page shows the same figure
    page = client.get("/cart").text
    assert "$63.00" in page and "every month" in page


def test_one_time_box_prices_per_bottle(conn):
    cart = cart_service.get_cart(conn, {}, create=True)
    ok, _ = cart_service.add_item(conn, cart, variant_id(conn, "DS-1"), 8, 0)
    assert ok
    tot = cart_service.totals(conn, cart)
    assert tot["subtotal_cents"] == 8000 and not tot["has_subscription"]
    # the product-page intervals still gate everything else
    ok, msg = cart_service.add_item(conn, cart, variant_id(conn, "DS-1"), 1, 2)
    assert not ok and "not offered" in msg


def test_v5_migration_is_idempotent_and_reinstates_the_builder_sku(conn):
    with transaction(conn):
        conn.execute("DELETE FROM inventory_movements WHERE variant_id = (SELECT id FROM variants WHERE sku = 'DS-1')")
        conn.execute("DELETE FROM cart_items WHERE variant_id = (SELECT id FROM variants WHERE sku = 'DS-1')")
        conn.execute("DELETE FROM variants WHERE sku = 'DS-1'")
        conn.execute("DELETE FROM settings WHERE key = 'builder_subscription_interval'")
    with transaction(conn):
        _v5_monthly_box(conn)
        _v5_monthly_box(conn)  # second run: no duplicate SKU, no error
    row = conn.execute("SELECT units_per_pack, price_cents, builder_only, is_active FROM variants WHERE sku = 'DS-1'").fetchone()
    assert tuple(row) == (1, 1000, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM variants WHERE sku = 'DS-1'").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM settings WHERE key = 'builder_subscription_interval'").fetchone()[0] == "1"
