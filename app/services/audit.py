"""Who changed what and when."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def _dump(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)[:20000]
    except (TypeError, ValueError):
        return str(value)[:20000]


def log(
    conn: sqlite3.Connection,
    action: str,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
    actor_name: str = "",
    target_type: str = "",
    target_id: int | None = None,
    before: Any = None,
    after: Any = None,
    ip: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor_type, actor_id, actor_name, action, target_type, target_id, before_json, after_json, ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (actor_type, actor_id, actor_name, action, target_type, target_id, _dump(before), _dump(after), ip),
    )
