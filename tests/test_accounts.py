"""Customer accounts: registration never touches an existing row, guests claim
through password reset, ownership on /account/orders, cart merge on login."""
from __future__ import annotations

from datetime import timedelta

from conftest import add_to_cart, create_customer, create_order, get_csrf, login, new_client, register, unique_email, variant_id
from app.security import hash_token, iso, utcnow

PASSWORD = "correct horse battery"


# ------------------------------------------------------------- registration
def test_register_creates_account_and_signs_in(client, conn):
    email = unique_email("reg")
    resp = register(client, email, PASSWORD, first_name="Ann")
    assert resp.status_code == 303 and resp.headers["location"] == "/account"
    row = conn.execute("SELECT password_hash, first_name FROM customers WHERE email_norm = ?", (email,)).fetchone()
    assert row and row["password_hash"].startswith("$argon2")
    assert client.get("/account").status_code == 200


def test_reregister_same_email_different_case_leaves_hash_unchanged(client, conn):
    email = unique_email("case")
    assert register(client, email, PASSWORD).status_code == 303
    before = conn.execute("SELECT password_hash FROM customers WHERE email_norm = ?", (email,)).fetchone()["password_hash"]

    with new_client() as attacker:
        resp = register(attacker, email.upper(), "a totally different pw", first_name="Mallory")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account/login"

    after = conn.execute("SELECT password_hash, first_name FROM customers WHERE email_norm = ?", (email,)).fetchone()
    assert after["password_hash"] == before
    assert after["first_name"] != "Mallory"
    assert conn.execute("SELECT COUNT(*) AS n FROM customers WHERE email_norm = ?", (email,)).fetchone()["n"] == 1


def test_register_refuses_guest_row_and_reset_flow_claims_it(client, conn):
    guest = create_customer(conn, password=None)  # empty password_hash = created by an order

    resp = register(client, guest["email"], PASSWORD)
    assert resp.status_code == 303 and resp.headers["location"] == "/account/login"
    assert conn.execute("SELECT password_hash FROM customers WHERE id = ?", (guest["id"],)).fetchone()["password_hash"] == ""
    # Registration did not sign the caller in as the guest.
    assert client.get("/account").status_code == 302

    # The real reset request writes a password_resets row (the token itself goes out by email).
    token = get_csrf(client)
    resp = client.post("/account/reset", data={"email": guest["email"], "csrf_token": token})
    assert resp.status_code == 303 and resp.headers["location"] == "/account/login"
    pr = conn.execute("SELECT token_hash, expires_at, used_at FROM password_resets WHERE customer_id = ?", (guest["id"],)).fetchone()
    assert pr and len(pr["token_hash"]) == 64 and pr["used_at"] is None

    # We cannot invert the hash, so plant a known token the same way the route would.
    known = "known-reset-token-" + guest["email"].split("@")[0]
    conn.execute("INSERT INTO password_resets(customer_id, token_hash, expires_at) VALUES (?, ?, ?)", (guest["id"], hash_token(known), iso(utcnow() + timedelta(hours=1))))

    assert client.get(f"/account/reset/{known}").status_code == 200
    resp = client.post(f"/account/reset/{known}", data={"password": PASSWORD, "csrf_token": token})
    assert resp.status_code == 303 and resp.headers["location"] == "/account"

    row = conn.execute("SELECT password_hash FROM customers WHERE id = ?", (guest["id"],)).fetchone()
    assert row["password_hash"].startswith("$argon2")
    assert client.get("/account").status_code == 200
    # Every outstanding reset for that customer is now burned.
    assert conn.execute("SELECT COUNT(*) AS n FROM password_resets WHERE customer_id = ? AND used_at IS NULL", (guest["id"],)).fetchone()["n"] == 0
    # And the token cannot be replayed.
    assert client.get(f"/account/reset/{known}").status_code == 303


def test_reset_with_unknown_token_redirects(client):
    resp = client.get("/account/reset/not-a-real-token")
    assert resp.status_code == 303 and resp.headers["location"] == "/account/reset"


# --------------------------------------------------------------- ownership
def test_anonymous_account_redirects_to_login(client):
    resp = client.get("/account")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/account/login"


def test_customer_cannot_read_another_customers_order(client, conn):
    a = create_customer(conn)
    b = create_customer(conn)
    b_order = create_order(conn, customer_id=b["id"], email=b["email"])

    assert login(client, a["email"], a["password"]).status_code == 303
    assert client.get("/account").status_code == 200
    assert client.get(f"/account/orders/{b_order['id']}").status_code == 404

    # The owner still sees it.
    with new_client() as owner:
        assert login(owner, b["email"], b["password"]).status_code == 303
        assert owner.get(f"/account/orders/{b_order['id']}").status_code == 200


def test_customer_cannot_post_on_another_customers_order(client, conn):
    a = create_customer(conn)
    b = create_customer(conn)
    b_order = create_order(conn, customer_id=b["id"], email=b["email"])
    assert login(client, a["email"], a["password"]).status_code == 303
    token = get_csrf(client)

    assert client.post(f"/account/orders/{b_order['id']}/reorder", data={"csrf_token": token}).status_code == 404
    assert client.post(f"/account/orders/{b_order['id']}/rma", data={"csrf_token": token, "reason": "leaked", "details": "x"}).status_code == 404
    assert conn.execute("SELECT COUNT(*) AS n FROM rma_requests WHERE order_id = ?", (b_order["id"],)).fetchone()["n"] == 0


# ------------------------------------------------------------- cart merge
def test_cart_merges_guest_lines_into_owned_cart_on_login(client, conn):
    vid = variant_id(conn, "QS-1")
    email = unique_email("merge")
    assert register(client, email, PASSWORD).status_code == 303
    token = get_csrf(client)

    # Signed in: 1 in the owned cart.
    assert add_to_cart(client, token, vid, 1).status_code == 200
    assert client.get("/cart/drawer").json()["count"] == 1

    # Sign out; the session forgets the cart token.
    assert client.post("/account/logout", data={"csrf_token": token}).status_code == 303
    assert client.get("/cart/drawer").json()["count"] == 0

    # Guest: 2 in a brand new cart.
    token = get_csrf(client)
    assert add_to_cart(client, token, vid, 2).status_code == 200
    assert client.get("/cart/drawer").json()["count"] == 2

    # Sign back in: 2 + 1 = 3, and the guest cart row is gone.
    assert login(client, email, PASSWORD).status_code == 303
    assert client.get("/cart/drawer").json()["count"] == 3
    cid = conn.execute("SELECT id FROM customers WHERE email_norm = ?", (email,)).fetchone()["id"]
    carts = conn.execute("SELECT id FROM carts WHERE customer_id = ? AND converted_order_id IS NULL", (cid,)).fetchall()
    assert len(carts) == 1
    qty = conn.execute("SELECT SUM(qty) AS q FROM cart_items WHERE cart_id = ?", (carts[0]["id"],)).fetchone()["q"]
    assert qty == 3
