"""Inbound webhooks. No CSRF here (no session); signatures are the auth."""
from __future__ import annotations

import json
import logging
import sqlite3

import stripe
from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, PlainTextResponse

from ..config import settings
from ..deps import get_db
from ..services import emails, stripe_service

log = logging.getLogger("qd.webhooks")
router = APIRouter()


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    if not settings.stripe_webhook_secret:
        return PlainTextResponse("webhook secret not configured", status_code=503)
    try:
        event = stripe_service.construct_event(payload, signature)
    except (stripe.SignatureVerificationError, ValueError) as exc:
        log.warning("stripe webhook rejected: %s", exc)
        return PlainTextResponse("invalid signature", status_code=400)
    try:
        outcome = stripe_service.handle_event(conn, event)
    except Exception:  # noqa: BLE001 - a 500 makes Stripe retry, which is what we want
        log.exception("stripe webhook failed for %s %s", event["type"], event["id"])
        return PlainTextResponse("error", status_code=500)
    log.info("stripe %s %s -> %s", event["type"], event["id"], outcome)
    return JSONResponse({"received": True, "outcome": outcome})


@router.post("/webhooks/resend")
async def resend_webhook(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    payload = await request.body()
    if not emails.verify_svix_signature(settings.resend_webhook_secret, {k.lower(): v for k, v in request.headers.items()}, payload):
        return PlainTextResponse("invalid signature", status_code=400)
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return PlainTextResponse("bad json", status_code=400)
    from ..db import transaction
    with transaction(conn):
        kind = emails.handle_resend_event(conn, event)
    return JSONResponse({"received": True, "type": kind})
