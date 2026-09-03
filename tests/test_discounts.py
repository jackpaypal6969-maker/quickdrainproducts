"""Discount validation rules and the amount arithmetic."""
from __future__ import annotations

from datetime import timedelta

from app.security import iso, utcnow
from app.services import discounts


def code(**over) -> dict:
    base = {
        "id": 1, "code": "TEST10", "kind": "percent", "value": 10, "min_subtotal_cents": 0, "max_uses": None,
        "usage_count": 0, "restricted_to_email": None, "starts_at": None, "expires_at": None, "is_active": 1,
    }
    base.update(over)
    return base


def test_valid_code_passes():
    ok, reason = discounts.validate(code(), email="a@gmail.com", subtotal_cents=1600)
    assert ok is True and reason == ""


def test_expired_code_is_refused():
    ok, reason = discounts.validate(code(expires_at=iso(utcnow() - timedelta(minutes=1))), email="a@gmail.com", subtotal_cents=1600)
    assert ok is False and "expired" in reason


def test_not_yet_started_code_is_refused():
    ok, reason = discounts.validate(code(starts_at=iso(utcnow() + timedelta(days=1))), email="a@gmail.com", subtotal_cents=1600)
    assert ok is False and "not active yet" in reason


def test_inactive_code_is_refused():
    ok, _ = discounts.validate(code(is_active=0), email="a@gmail.com", subtotal_cents=1600)
    assert ok is False


def test_email_locked_code_refuses_another_email():
    locked = code(restricted_to_email="owner@gmail.com")
    ok, reason = discounts.validate(locked, email="someone.else@gmail.com", subtotal_cents=1600)
    assert ok is False and "different email" in reason
    ok, _ = discounts.validate(locked, email="", subtotal_cents=1600)
    assert ok is False
    ok, _ = discounts.validate(locked, email="  Owner@Gmail.com ", subtotal_cents=1600)
    assert ok is True


def test_minimum_subtotal_is_enforced():
    ok, reason = discounts.validate(code(min_subtotal_cents=4200), email="a@gmail.com", subtotal_cents=1600)
    assert ok is False and "$42.00" in reason
    ok, _ = discounts.validate(code(min_subtotal_cents=4200), email="a@gmail.com", subtotal_cents=4200)
    assert ok is True


def test_max_uses_reached_is_refused():
    ok, reason = discounts.validate(code(max_uses=1, usage_count=1), email="a@gmail.com", subtotal_cents=1600)
    assert ok is False and "already been used" in reason
    ok, _ = discounts.validate(code(max_uses=2, usage_count=1), email="a@gmail.com", subtotal_cents=1600)
    assert ok is True


def test_amounts_cap_at_subtotal():
    assert discounts.amount(code(kind="percent", value=10), 1600) == (160, False)
    assert discounts.amount(code(kind="percent", value=150), 1600) == (1600, False)
    assert discounts.amount(code(kind="fixed", value=5000), 1600) == (1600, False)
    assert discounts.amount(code(kind="free_shipping", value=0), 1600) == (0, True)


def test_normalize_code_uppercases_and_strips_junk():
    assert discounts.normalize_code(" welcome-abc123 ") == "WELCOME-ABC123"
    assert discounts.normalize_code("a b'c;--") == "ABC--"


def test_issue_locked_code_is_idempotent_per_email_and_channel(conn):
    email = "locked-code-test@gmail.com"
    first = discounts.issue_locked_code(conn, email, "unit-test", percent=10, days=7, prefix="UT")
    # still valid → the same row comes back, untouched
    again = discounts.issue_locked_code(conn, email, "unit-test", percent=10, days=7, prefix="UT")
    assert again["id"] == first["id"] and again["usage_count"] == 0 and again["is_active"] == 1
    # used up → a fresh code is issued and the old row is never reset
    conn.execute("UPDATE discount_codes SET usage_count = 1 WHERE id = ?", (first["id"],))
    second = discounts.issue_locked_code(conn, email, "unit-test", percent=10, days=7, prefix="UT")
    assert second["id"] != first["id"]
    old = conn.execute("SELECT usage_count, is_active, max_uses FROM discount_codes WHERE id = ?", (first["id"],)).fetchone()
    assert old["usage_count"] == 1 and old["is_active"] == 1 and old["max_uses"] == 1
    assert second["usage_count"] == 0 and second["is_active"] == 1 and second["max_uses"] == 1
    assert second["restricted_to_email"] == email
