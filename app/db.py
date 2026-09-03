"""SQLite access. One connection per request, WAL mode, foreign keys on.

`migrate()` applies schema.sql (all IF NOT EXISTS) and then any numbered
migrations in MIGRATIONS that have not been recorded in schema_migrations.
"""
from __future__ import annotations

import contextlib
import itertools
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Append-only. Each entry: (version, [sql, ...]). Never edit a shipped version.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, []),  # initial schema is schema.sql itself
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
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, statements in MIGRATIONS:
            if version in applied:
                continue
            with transaction(conn):
                for sql in statements:
                    conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
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
