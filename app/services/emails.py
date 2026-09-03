"""Outbound email through Resend. Every message: suppression check, unsubscribe
link, log row. Bounces and complaints arrive on the Resend webhook and land in
email_suppressions so a dead address is never mailed twice."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
import time

import httpx
from itsdangerous import BadSignature, URLSafeSerializer

from ..config import settings
from ..db import one
from ..jinja_env import env
from ..security import iso, normalize_email

log = logging.getLogger("qd.email")
RESEND_URL = "https://api.resend.com/emails"


def unsubscribe_token(email: str) -> str:
    return URLSafeSerializer(settings.secret_key, salt="unsubscribe").dumps(normalize_email(email))


def email_from_unsubscribe_token(token: str) -> str | None:
    try:
        return URLSafeSerializer(settings.secret_key, salt="unsubscribe").loads(token)
    except BadSignature:
        return None


def is_suppressed(conn: sqlite3.Connection, email: str, category: str) -> str | None:
    row = one(conn, "SELECT reason FROM email_suppressions WHERE email = ?", (normalize_email(email),))
    if not row:
        return None
    reason = row["reason"]
    if reason in {"bounce", "complaint", "manual"}:
        return reason
    if reason == "unsubscribe" and category == "marketing":
        return reason
    return None


def suppress(conn: sqlite3.Connection, email: str, reason: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO email_suppressions(email, reason, note) VALUES (?, ?, ?)"
        " ON CONFLICT(email) DO UPDATE SET reason = CASE WHEN excluded.reason IN ('bounce','complaint','manual') THEN excluded.reason ELSE email_suppressions.reason END, note = excluded.note",
        (normalize_email(email), reason, note),
    )
    conn.execute("UPDATE newsletter_subscribers SET unsubscribed_at = COALESCE(unsubscribed_at, ?) WHERE email = ?", (iso(), normalize_email(email)))


def render(template: str, context: dict) -> tuple[str, str]:
    html = env.get_template(f"emails/{template}.html").render(**context)
    try:
        text = env.get_template(f"emails/{template}.txt").render(**context)
    except Exception:  # noqa: BLE001 - text part is optional
        text = ""
    return html, text


def send(
    conn: sqlite3.Connection,
    to_email: str,
    template: str,
    subject: str,
    context: dict | None = None,
    *,
    category: str = "transactional",
    related_type: str = "",
    related_id: int | None = None,
    reply_to: str | None = None,
) -> int:
    """Render + send. Returns the email_log id. Never raises to the caller."""
    to_email = normalize_email(to_email)
    ctx = {
        "settings": settings,
        "base_url": settings.base_url,
        "unsubscribe_url": f"{settings.base_url}/email/unsubscribe/{unsubscribe_token(to_email)}",
        "category": category,
        **(context or {}),
    }
    reason = is_suppressed(conn, to_email, category)
    if reason:
        cur = conn.execute(
            "INSERT INTO email_log(to_email, template, subject, category, status, error, related_type, related_id) VALUES (?, ?, ?, ?, 'suppressed', ?, ?, ?)",
            (to_email, template, subject, category, f"suppressed: {reason}", related_type, related_id),
        )
        return int(cur.lastrowid)
    try:
        html, text = render(template, ctx)
    except Exception as exc:  # noqa: BLE001
        log.exception("email render failed for %s", template)
        cur = conn.execute(
            "INSERT INTO email_log(to_email, template, subject, category, status, error, related_type, related_id) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)",
            (to_email, template, subject, category, f"render: {exc}"[:500], related_type, related_id),
        )
        return int(cur.lastrowid)

    cur = conn.execute(
        "INSERT INTO email_log(to_email, template, subject, category, status, related_type, related_id) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
        (to_email, template, subject, category, related_type, related_id),
    )
    log_id = int(cur.lastrowid)

    if settings.email_dry_run or not settings.resend_api_key:
        conn.execute("UPDATE email_log SET status = 'dry_run', updated_at = ? WHERE id = ?", (iso(), log_id))
        log.info("EMAIL DRY RUN -> %s [%s] %s", to_email, template, subject)
        return log_id

    payload = {
        "from": settings.email_from,
        "to": [to_email],
        "subject": subject,
        "html": html,
        "headers": {"List-Unsubscribe": f"<{ctx['unsubscribe_url']}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        "tags": [{"name": "template", "value": template}, {"name": "category", "value": category}],
    }
    if text:
        payload["text"] = text
    if reply_to or settings.email_reply_to:
        payload["reply_to"] = reply_to or settings.email_reply_to
    try:
        resp = httpx.post(RESEND_URL, json=payload, headers={"Authorization": f"Bearer {settings.resend_api_key}"}, timeout=10.0)
        if resp.status_code < 300:
            provider_id = resp.json().get("id", "")
            conn.execute("UPDATE email_log SET status = 'sent', provider_id = ?, updated_at = ? WHERE id = ?", (provider_id, iso(), log_id))
        else:
            conn.execute("UPDATE email_log SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (f"{resp.status_code}: {resp.text[:400]}", iso(), log_id))
    except httpx.HTTPError as exc:
        conn.execute("UPDATE email_log SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (str(exc)[:400], iso(), log_id))
    return log_id


# ------------------------------------------------------------ Resend webhook
def verify_svix_signature(secret: str, headers: dict, body: bytes) -> bool:
    """Resend signs with Svix: HMAC-SHA256 over "{id}.{timestamp}.{body}"."""
    if not secret:
        return False
    msg_id = headers.get("svix-id", "")
    ts = headers.get("svix-timestamp", "")
    sigs = headers.get("svix-signature", "")
    if not (msg_id and ts and sigs):
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except ValueError:
        return False
    expected = base64.b64encode(hmac.new(key_bytes, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    for part in sigs.split(" "):
        if "," in part:
            _, sig = part.split(",", 1)
            if hmac.compare_digest(sig, expected):
                return True
    return False


def handle_resend_event(conn: sqlite3.Connection, event: dict) -> str:
    kind = event.get("type", "")
    data = event.get("data", {}) or {}
    email_id = data.get("email_id", "")
    to_list = data.get("to") or []
    to_email = normalize_email(to_list[0]) if to_list else ""
    status_map = {
        "email.delivered": "delivered",
        "email.bounced": "bounced",
        "email.complained": "complained",
        "email.delivery_delayed": "delayed",
        "email.sent": "sent",
    }
    status = status_map.get(kind)
    if status and email_id:
        conn.execute("UPDATE email_log SET status = ?, updated_at = ? WHERE provider_id = ?", (status, iso(), email_id))
    if kind == "email.bounced" and to_email:
        bounce = data.get("bounce") or {}
        suppress(conn, to_email, "bounce", json.dumps(bounce)[:400])
    elif kind == "email.complained" and to_email:
        suppress(conn, to_email, "complaint")
    return kind
