"""Discount codes. Single-use is a real column (max_uses) counted locally in
usage_count. Nothing here ever flips is_active on redemption, and re-issuing a
locked code returns the existing row untouched."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import timedelta

from ..db import one
from ..security import iso, normalize_email, parse_iso, utcnow


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum() or ch in "-_")[:40]


def validate(row: dict, email: str, subtotal_cents: int) -> tuple[bool, str]:
    if not row or not row.get("is_active"):
        return False, "That code is not active."
    now = utcnow()
    starts = parse_iso(row.get("starts_at"))
    expires = parse_iso(row.get("expires_at"))
    if starts and now < starts:
        return False, "That code is not active yet."
    if expires and now > expires:
        return False, "That code has expired."
    if row.get("max_uses") is not None and int(row.get("usage_count") or 0) >= int(row["max_uses"]):
        return False, "That code has already been used."
    locked = row.get("restricted_to_email")
    if locked:
        if not email or normalize_email(email) != normalize_email(locked):
            return False, "That code is tied to a different email address."
    if subtotal_cents < int(row.get("min_subtotal_cents") or 0):
        return False, f"That code needs a subtotal of at least ${int(row['min_subtotal_cents']) / 100:.2f}."
    return True, ""


def amount(row: dict, subtotal_cents: int) -> tuple[int, bool]:
    """Return (discount_cents, free_shipping)."""
    kind = row.get("kind")
    value = int(row.get("value") or 0)
    if kind == "percent":
        return min(subtotal_cents, round(subtotal_cents * max(0, min(value, 100)) / 100)), False
    if kind == "fixed":
        return min(subtotal_cents, max(0, value)), False
    if kind == "free_shipping":
        return 0, True
    return 0, False


def find(conn: sqlite3.Connection, code: str) -> dict | None:
    row = one(conn, "SELECT * FROM discount_codes WHERE code = ?", (normalize_code(code),))
    return dict(row) if row else None


def redeem(conn: sqlite3.Connection, discount_code_id: int, order_id: int, email: str) -> bool:
    """Count one redemption inside the caller's transaction. Returns False when
    the code was already at max_uses (the order still stands; payment is done)."""
    cur = conn.execute(
        "UPDATE discount_codes SET usage_count = usage_count + 1 WHERE id = ? AND (max_uses IS NULL OR usage_count < max_uses)",
        (discount_code_id,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO discount_redemptions(discount_code_id, order_id, email) VALUES (?, ?, ?)",
        (discount_code_id, order_id, normalize_email(email)),
    )
    return cur.rowcount == 1


def issue_locked_code(conn: sqlite3.Connection, email: str, channel: str, percent: int, days: int, prefix: str) -> dict:
    """Email-locked, single-use, expiring code. Idempotent per (email, channel):
    a second call returns the same row without resetting usage_count or is_active."""
    email = normalize_email(email)
    existing = one(conn, "SELECT * FROM discount_codes WHERE restricted_to_email = ? AND channel = ? ORDER BY id DESC LIMIT 1", (email, channel))
    if existing:
        return dict(existing)
    for _ in range(10):
        code = f"{prefix}-{secrets.token_hex(3).upper()}"
        if not one(conn, "SELECT 1 FROM discount_codes WHERE code = ?", (code,)):
            break
    conn.execute(
        "INSERT INTO discount_codes(code, kind, value, max_uses, restricted_to_email, expires_at, channel, note)"
        " VALUES (?, 'percent', ?, 1, ?, ?, ?, ?)",
        (code, percent, email, iso(utcnow() + timedelta(days=days)), channel, f"auto-issued via {channel}"),
    )
    return dict(one(conn, "SELECT * FROM discount_codes WHERE code = ?", (code,)))
