from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Request

from ...db import one
from ...deps import render


def nav_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "orders_open": one(conn, "SELECT COUNT(*) AS n FROM orders WHERE status IN ('paid','on_hold','processing')")["n"],
        "orders_hold": one(conn, "SELECT COUNT(*) AS n FROM orders WHERE status = 'on_hold'")["n"],
        "reviews_pending": one(conn, "SELECT COUNT(*) AS n FROM reviews WHERE status = 'pending'")["n"],
        "rma_open": one(conn, "SELECT COUNT(*) AS n FROM rma_requests WHERE status IN ('requested','approved','received')")["n"],
        "contact_new": one(conn, "SELECT COUNT(*) AS n FROM contact_messages WHERE status = 'new'")["n"],
        "low_stock": one(conn, "SELECT COUNT(*) AS n FROM variants WHERE is_active = 1 AND stock <= low_stock_threshold")["n"],
    }


def arender(request: Request, template: str, ctx: dict[str, Any], conn: sqlite3.Connection, admin: dict) -> Any:
    data = {"admin": admin, "counts": nav_counts(conn), "section": template.split("/")[1].split(".")[0] if "/" in template else ""}
    data.update(ctx)
    return render(request, template, data, conn=conn)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def dollars_to_cents(value: Any) -> int | None:
    s = str(value or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return round(float(s) * 100)
    except ValueError:
        return None
