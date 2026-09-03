"""SQLite access. One connection per request, WAL mode, foreign keys on.

`migrate()` applies schema.sql (all IF NOT EXISTS) and then any numbered
migrations in MIGRATIONS that have not been recorded in schema_migrations.
"""
from __future__ import annotations

import contextlib
import itertools
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _v2_subscriptions(conn: sqlite3.Connection) -> None:
    """Subscribe-and-save: interval, discount override, per-cycle lines."""
    _add_column(conn, "variants", "subscription_discount_percent", "INTEGER")
    for col, ddl in (
        ("stripe_customer_id", "TEXT NOT NULL DEFAULT ''"),
        ("interval_months", "INTEGER NOT NULL DEFAULT 1"),
        ("lines", "TEXT NOT NULL DEFAULT '[]'"),
        ("shipping_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("cancel_at_period_end", "INTEGER NOT NULL DEFAULT 0"),
        ("next_renewal_at", "TEXT"),
        ("last_order_id", "INTEGER"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column(conn, "subscriptions", col, ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_customer ON subscriptions(customer_id)")


# Append-only. Each entry: (version, [sql or callable, ...]). Never edit a shipped version.
def _v3_checkout_lines(conn: sqlite3.Connection) -> None:
    _add_column(conn, "carts", "checkout_lines", "TEXT NOT NULL DEFAULT ''")


def _v4_drain_shot(conn: sqlite3.Connection) -> None:
    """Launch catalog change: the product is Drain Shot (as on the label) and is
    sold as a 12-pack only. Old packs are deactivated, never deleted, so any
    order history that references them survives."""
    row = conn.execute("SELECT id FROM products WHERE slug = 'quick-shot'").fetchone()
    if not row:
        return
    pid = row[0]
    conn.execute(
        "UPDATE products SET slug = 'drain-shot', name = 'Drain Shot', tagline = ?, seo_title = ?, seo_description = ?, description = replace(description, 'Quick Shot', 'Drain Shot'), updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (
            "A natural drain enzyme, dosed for monthly use on any drain. Sold as a one-year supply.",
            "Drain Shot — natural drain enzyme, one-year supply | Quick Drain Products",
            "Drain Shot is a natural drain enzyme dosed for monthly use on any drain. Twelve 4 fl oz bottles: a year for one drain. Ships as an ordinary parcel. From Quick Drain, Long Island.",
            pid,
        ),
    )
    conn.execute("UPDATE variants SET is_active = 0 WHERE product_id = ? AND sku IN ('QS-1', 'QS-3', 'QS-6')", (pid,))
    if not conn.execute("SELECT 1 FROM variants WHERE sku = 'DS-12'").fetchone():
        conn.execute("INSERT INTO variants(product_id, sku, name, units_per_pack, price_cents, stock, sort) VALUES (?, 'DS-12', '12-pack · one-year supply', 12, 12000, 50, 0)", (pid,))
        conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, note) VALUES ((SELECT id FROM variants WHERE sku = 'DS-12'), 50, 'restock', 'launch catalog')")
    conn.execute("UPDATE product_images SET base = replace(base, 'quick-shot', 'drain-shot'), alt = replace(alt, 'Quick Shot', 'Drain Shot') WHERE product_id = ?", (pid,))
    conn.execute("UPDATE product_faqs SET question = replace(question, 'Quick Shot', 'Drain Shot'), answer = replace(answer, 'Quick Shot', 'Drain Shot') WHERE product_id = ?", (pid,))
    conn.execute(
        "UPDATE product_faqs SET question = 'Why a 12-pack?', answer = 'Twelve monthly doses is one drain for one year — or twelve drains for one month. The coverage table above does the arithmetic. Subscribers get a fresh box each year at 10% off and can cancel any time.' WHERE product_id = ? AND question = 'How do the packs work?'",
        (pid,),
    )
    conn.execute("UPDATE posts SET title = replace(title, 'Quick Shot', 'Drain Shot'), body = replace(body, 'Quick Shot', 'Drain Shot'), excerpt = replace(excerpt, 'Quick Shot', 'Drain Shot')")
    conn.execute("UPDATE settings SET value = '12' WHERE key = 'subscription_intervals' AND value = '1,2,3'")


def _v5_monthly_box(conn: sqlite3.Connection) -> None:
    """Build your box sells a month, not a year: one bottle per drain, shipped
    once or every month. That needs a per-bottle SKU the product page does not
    list (builder_only) and a monthly interval the builder alone may use."""
    _add_column(conn, "variants", "builder_only", "INTEGER NOT NULL DEFAULT 0")
    row = conn.execute("SELECT id FROM products WHERE slug = 'drain-shot'").fetchone()
    if not row:
        return
    pid = row[0]
    if not conn.execute("SELECT 1 FROM variants WHERE sku = 'DS-1'").fetchone():
        conn.execute("INSERT INTO variants(product_id, sku, name, units_per_pack, price_cents, stock, sort, builder_only) VALUES (?, 'DS-1', 'Bottle · one drain, one month', 1, 1000, 600, 1, 1)", (pid,))
        conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, note) VALUES ((SELECT id FROM variants WHERE sku = 'DS-1'), 600, 'restock', 'build your box launch')")
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('builder_subscription_interval', '1')")


MIGRATIONS: list[tuple[int, list]] = [
    (1, []),  # initial schema is schema.sql itself
    (2, [_v2_subscriptions]),
    (3, [_v3_checkout_lines]),
    (4, [_v4_drain_shot]),
    (5, [_v5_monthly_box]),
]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(db_path: Path | None = None) -> None:
    """Safe to run from several processes at once (two uvicorn workers, the
    cron job): WAL is set with a retry, schema.sql is IF NOT EXISTS throughout,
    and each migration is decided under the write lock."""
    conn = connect(db_path)
    try:
        for attempt in range(20):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError:
                time.sleep(0.05 * (attempt + 1))
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for version, statements in MIGRATIONS:
            with transaction(conn):
                cur = conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,))
                if cur.rowcount == 0:
                    continue  # already applied (possibly by a sibling process a moment ago)
                for step in statements:
                    if callable(step):
                        step(conn)
                    else:
                        conn.execute(step)
    finally:
        conn.close()


_savepoint_seq = itertools.count(1)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE so writers serialize; rolls back on any exception.
    Reentrant: a nested call becomes a SAVEPOINT, so a service that opens its
    own transaction can be called from inside a route's transaction and an
    inner failure only unwinds the inner block."""
    if conn.in_transaction:
        name = f"sp{next(_savepoint_seq)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def one(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple | dict = (), default: Any = None) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    return row[0]


def insert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> int:
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, table: str, row_id: int, data: dict[str, Any]) -> int:
    if not data:
        return 0
    sets = ", ".join(f"{k} = ?" for k in data)
    cur = conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))
    return cur.rowcount


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
