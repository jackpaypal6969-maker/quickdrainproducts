"""Sales reports: revenue by day/week/month, units by variant, discount code
performance. Every table has a CSV twin."""
from __future__ import annotations

import csv
import io
import sqlite3

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from ...db import all_rows
from ...deps import get_db, require_admin
from .common import arender

router = APIRouter()

GROUPS = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}


def revenue_rows(conn: sqlite3.Connection, group: str, days: int) -> list[dict]:
    fmt = GROUPS.get(group, GROUPS["day"])
    return [dict(r) for r in all_rows(conn, f"""SELECT strftime('{fmt}', created_at) AS period, COUNT(*) AS orders,
              SUM(subtotal_cents) AS subtotal_cents, SUM(discount_cents) AS discount_cents, SUM(shipping_cents) AS shipping_cents,
              SUM(tax_cents) AS tax_cents, SUM(total_cents) AS total_cents, SUM(refunded_cents) AS refunded_cents,
              SUM(total_cents - refunded_cents) AS net_cents
              FROM orders WHERE created_at >= datetime('now', ?) AND status != 'canceled'
              GROUP BY period ORDER BY period DESC""", (f"-{days} days",))]


def units_rows(conn: sqlite3.Connection, days: int) -> list[dict]:
    return [dict(r) for r in all_rows(conn, """SELECT oi.sku, oi.product_name, oi.variant_name, SUM(oi.qty) AS packs, SUM(oi.qty * oi.units_per_pack) AS bottles,
              SUM(oi.line_total_cents) AS revenue_cents, COUNT(DISTINCT oi.order_id) AS orders
              FROM order_items oi JOIN orders o ON o.id = oi.order_id
              WHERE o.created_at >= datetime('now', ?) AND o.status != 'canceled'
              GROUP BY oi.sku, oi.product_name, oi.variant_name ORDER BY revenue_cents DESC""", (f"-{days} days",))]


def discount_rows(conn: sqlite3.Connection, days: int) -> list[dict]:
    return [dict(r) for r in all_rows(conn, """SELECT d.code, d.channel, d.kind, d.value, d.usage_count, d.max_uses,
              COUNT(o.id) AS orders, COALESCE(SUM(o.total_cents),0) AS revenue_cents, COALESCE(SUM(o.discount_cents),0) AS discount_cents
              FROM discount_codes d LEFT JOIN orders o ON o.discount_code_id = d.id AND o.created_at >= datetime('now', ?)
              GROUP BY d.id ORDER BY orders DESC, d.created_at DESC LIMIT 200""", (f"-{days} days",))]


def channel_rows(conn: sqlite3.Connection, days: int) -> list[dict]:
    return [dict(r) for r in all_rows(conn, """SELECT COALESCE(NULLIF(utm_source,''), '(direct)') AS source, COALESCE(NULLIF(utm_campaign,''), '') AS campaign,
              COUNT(*) AS orders, SUM(total_cents) AS revenue_cents, SUM(CASE WHEN discount_code != '' THEN 1 ELSE 0 END) AS with_code
              FROM orders WHERE created_at >= datetime('now', ?) AND status != 'canceled' GROUP BY source, campaign ORDER BY revenue_cents DESC""", (f"-{days} days",))]


@router.get("/reports")
def reports(request: Request, group: str = "day", days: int = 30, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    group = group if group in GROUPS else "day"
    days = days if days in (7, 30, 90, 365) else 30
    return arender(request, "admin/reports.html", {
        "group": group, "days": days,
        "revenue": revenue_rows(conn, group, days), "units": units_rows(conn, days), "discounts": discount_rows(conn, days), "channels": channel_rows(conn, days),
        "meta_title": "Reports",
    }, conn, admin)


@router.get("/reports/{kind}.csv")
def report_csv(kind: str, group: str = "day", days: int = 30, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    group = group if group in GROUPS else "day"
    days = days if days in (7, 30, 90, 365) else 30
    rows = {"revenue": lambda: revenue_rows(conn, group, days), "units": lambda: units_rows(conn, days), "discounts": lambda: discount_rows(conn, days), "channels": lambda: channel_rows(conn, days)}.get(kind)
    if not rows:
        return StreamingResponse(iter(["unknown report\n"]), media_type="text/plain", status_code=404)
    data = rows()
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
        w.writeheader()
        for r in data:
            w.writerow({k: (f"{v / 100:.2f}" if isinstance(k, str) and k.endswith("_cents") and v is not None else v) for k, v in r.items()})
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={kind}-{group}-{days}d.csv"})
