#!/usr/bin/env python
"""Hourly cron entry point: abandoned carts, reorder reminders, review invites,
back-in-stock notices, housekeeping. Safe to run any number of times."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect, migrate  # noqa: E402
from app.services import lifecycle  # noqa: E402


def main() -> None:
    migrate()
    conn = connect()
    try:
        result = lifecycle.run_all(conn)
    finally:
        conn.close()
    print("lifecycle:", " ".join(f"{k}={v}" for k, v in result.items()))


if __name__ == "__main__":
    main()
