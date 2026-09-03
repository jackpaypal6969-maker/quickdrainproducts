"""Build your box: the page renders with its config block, and the cart line it
posts (12-pack × drains, yearly subscription) prices as drains × sub price."""
from __future__ import annotations

import json
import re

from conftest import get_csrf, variant_id
from app.services import cart as cart_service
from app.services import catalog

CONFIG_RE = re.compile(r'<script type="application/json" id="box-config">(.*?)</script>', re.S)


def _config(html: str) -> dict:
    m = CONFIG_RE.search(html)
    assert m, "no #box-config block"
    return json.loads(m.group(1))


def test_page_renders_with_config(client, conn):
    resp = client.get("/build-your-box")
    assert resp.status_code == 200
    html = resp.text
    vid = variant_id(conn, "DS-12")
    v = conn.execute("SELECT price_cents FROM variants WHERE id = ?", (vid,)).fetchone()
    cfg = _config(html)
    assert cfg["variantId"] == vid and cfg["sku"] == "DS-12"
    assert cfg["priceCents"] == v["price_cents"]
    assert cfg["perBottleCents"] == round(v["price_cents"] / 12)
    assert cfg["subPercent"] == 10 and cfg["subPriceCents"] == round(v["price_cents"] * 0.9)
    assert cfg["subscriptionsEnabled"] is True and 12 in cfg["intervals"]
    # the form app.js posts, and the page's own script
    assert 'data-add-form' in html and f'name="variant_id" value="{vid}"' in html
    assert 'data-box-subscribe' in html and 'data-box-delivery' in html
    assert '/static/js/box.js' in html
    # six fixture rows, each with a 44px stepper pair
    assert html.count('data-fixture="') == 6
    assert html.count('data-step="1"') == 6 and html.count('data-step="-1"') == 6
    assert 'data-preset=' in html and "3-bath home" in html


def test_page_hides_subscribe_choice_when_subscriptions_are_off(client, monkeypatch):
    real = catalog.subscription_config
    monkeypatch.setattr(catalog, "subscription_config", lambda conn: real(conn) | {"enabled": False})
    html = client.get("/build-your-box").text
    assert _config(html)["subscriptionsEnabled"] is False
    assert "data-box-delivery" not in html
    assert 'name="subscribe" value="0"' in html


def test_sitemap_lists_the_builder(client):
    assert "<loc>http://testserver/build-your-box</loc>" in client.get("/sitemap.xml").text


def test_seven_drain_box_posts_as_seven_yearly_twelve_packs(client, conn):
    token = get_csrf(client)
    vid = variant_id(conn, "DS-12")
    resp = client.post(
        "/cart/add",
        data={"variant_id": vid, "qty": 7, "subscribe": 12, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["ok"] is True and body["count"] == 7
    assert "every 12 months" in body["message"]

    v = conn.execute("SELECT price_cents FROM variants WHERE id = ?", (vid,)).fetchone()
    sub_price = round(v["price_cents"] * 0.9)
    item = conn.execute("SELECT cart_id FROM cart_items WHERE variant_id = ? AND qty = 7 AND subscribe = 12 ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
    assert item
    cart = dict(conn.execute("SELECT * FROM carts WHERE id = ?", (item["cart_id"],)).fetchone())
    tot = cart_service.totals(conn, cart)
    assert tot["count"] == 7 and tot["has_subscription"] and tot["subscription_interval"] == 12
    assert tot["subtotal_cents"] == 7 * sub_price
    assert tot["subscription_savings_cents"] == 7 * (v["price_cents"] - sub_price)
    line = tot["items"][0]
    assert line["qty"] == 7 and line["price_cents"] == sub_price and line["line_total_cents"] == 7 * sub_price

    # the cart page shows the same figure
    page = client.get("/cart").text
    assert f"${7 * sub_price / 100:,.2f}" in page
