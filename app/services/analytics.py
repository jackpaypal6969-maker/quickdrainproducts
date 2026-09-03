"""Named conversion events. Stored locally always; forwarded to PostHog when a
key is configured. Server-side capture is what makes checkout_completed
attributable even though the browser never sees the webhook."""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("qd.analytics")

EVENTS = {"add_to_cart", "checkout_started", "checkout_completed", "newsletter_signup", "notify_me", "review_submitted"}


def capture(conn: sqlite3.Connection | None, name: str, distinct_id: str, props: dict[str, Any] | None = None) -> None:
    props = dict(props or {})
    if conn is not None:
        try:
            conn.execute("INSERT INTO analytics_events(name, distinct_id, props) VALUES (?, ?, ?)", (name, distinct_id or "", json.dumps(props, default=str)[:4000]))
        except sqlite3.Error as exc:  # never let analytics break a request
            log.warning("analytics insert failed: %s", exc)
    if not settings.posthog_key:
        return
    try:
        httpx.post(
            f"{settings.posthog_host.rstrip('/')}/capture/",
            json={"api_key": settings.posthog_key, "event": name, "distinct_id": distinct_id or "anonymous", "properties": props | {"$lib": "quick-drain-server"}},
            timeout=3.0,
        )
    except httpx.HTTPError as exc:
        log.warning("posthog capture failed: %s", exc)
