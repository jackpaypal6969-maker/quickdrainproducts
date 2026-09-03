"""Security primitives: password hashing, signed sessions, CSRF, a shared
SQLite-backed rate limiter, email validation, TOTP/backup/email second factors,
and admin lockout. Every rule here traces to a live bug on a previous store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from .config import settings
from .db import transaction

SESSION_COOKIE = "qd_session"
_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash or not password:
        return False
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, ValueError):
        return False


def password_policy_error(password: str) -> str | None:
    if len(password) < 10:
        return "Use at least 10 characters."
    if len(password) > 200:
        return "That password is too long."
    if password.lower() in {"password12", "quickdrain1", "1234567890"}:
        return "Choose a less common password."
    return None


# ----------------------------------------------------------------------- session
def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def load_session(request: Request) -> dict:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return {}
    try:
        data = _serializer("session").loads(raw, max_age=settings.session_days * 86400)
    except BadSignature:
        return {}
    return data if isinstance(data, dict) else {}


def save_session(response: Response, data: dict) -> None:
    value = _serializer("session").dumps(data)
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# -------------------------------------------------------------------------- csrf
def ensure_csrf(session: dict) -> str:
    token = session.get("csrf")
    if not token or not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def verify_csrf(session: dict, submitted: str | None) -> bool:
    expected = session.get("csrf")
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))


# ------------------------------------------------------------------- rate limit
def check_rate_limit(conn: sqlite3.Connection, scope: str, subject: str, limit: int, window_seconds: int) -> bool:
    """Shared fixed-window limiter. True = allowed. Backed by SQLite so two
    uvicorn workers share one counter and a published limit is the real limit."""
    now = int(time.time())
    window_start = now - (now % window_seconds)
    key = f"{scope}:{subject}"[:255]
    with transaction(conn):
        conn.execute(
            "INSERT INTO rate_limits(key, window_start, count) VALUES (?, ?, 1)"
            " ON CONFLICT(key, window_start) DO UPDATE SET count = count + 1",
            (key, window_start),
        )
        count = conn.execute(
            "SELECT count FROM rate_limits WHERE key = ? AND window_start = ?", (key, window_start)
        ).fetchone()[0]
        if now % 50 == 0:  # opportunistic prune, cheap
            conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (now - 86400,))
    return count <= limit


def client_ip(request: Request) -> str:
    """uvicorn is started with --proxy-headers --forwarded-allow-ips=127.0.0.1,
    so request.client.host is already the real client when behind nginx."""
    host = request.client.host if request.client else ""
    if host in {"127.0.0.1", "::1", ""}:
        forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "")
        if forwarded:
            host = forwarded.split(",")[0].strip()
    return host or "unknown"


# ------------------------------------------------------------------ email rules
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

RESERVED_TLDS = {"test", "invalid", "example", "localhost", "local", "internal", "onion", "arpa", "corp", "home", "lan"}
RESERVED_DOMAINS = {"example.com", "example.net", "example.org", "test.com", "localhost.com"}

# Disposable / throwaway providers. Not exhaustive; extend via settings key
# `email_blocklist_extra` (comma separated) in the admin without a deploy.
DISPOSABLE_DOMAINS = {
    "10minutemail.com", "10minutemail.net", "20minutemail.com", "33mail.com", "anonbox.net", "anonymbox.com",
    "binkmail.com", "bobmail.info", "burnermail.io", "byom.de", "chammy.info", "cool.fr.nf", "courriel.fr.nf",
    "crazymailing.com", "cuvox.de", "dayrep.com", "deadaddress.com", "despam.it", "discard.email", "discardmail.com",
    "dispostable.com", "disposable.com", "dodgeit.com", "dontreg.com", "drdrb.net", "dropmail.me", "dump-email.info",
    "e4ward.com", "easytrashmail.com", "einrot.com", "emailondeck.com", "emailtemporario.com.br", "emltmp.com",
    "ephemail.net", "fakeinbox.com", "fakemail.fr", "fakemailgenerator.com", "fansworldwide.de", "filzmail.com",
    "fleckens.hu", "getairmail.com", "getnada.com", "gishpuppy.com", "grr.la", "guerrillamail.biz", "guerrillamail.com",
    "guerrillamail.de", "guerrillamail.info", "guerrillamail.net", "guerrillamail.org", "guerrillamailblock.com",
    "gustr.com", "harakirimail.com", "hmamail.com", "hulapla.de", "imgof.com", "inboxalias.com", "inboxbear.com",
    "incognitomail.com", "incognitomail.org", "jetable.com", "jetable.fr.nf", "jetable.net", "jetable.org", "jnxjn.com",
    "jourrapide.com", "kasmail.com", "keemail.me", "koszmail.pl", "kurzepost.de", "lifebyfood.com", "lroid.com",
    "mail-temporaire.fr", "mail.by", "mail4trash.com", "mailcatch.com", "maildrop.cc", "maileater.com", "mailexpire.com",
    "mailforspam.com", "mailin8r.com", "mailinator.com", "mailinator.net", "mailinator2.com", "mailme.lv", "mailmoat.com",
    "mailnesia.com", "mailnull.com", "mailsac.com", "mailslite.com", "mailtemp.info", "mailzilla.com", "meltmail.com",
    "mintemail.com", "moakt.com", "mohmal.com", "mt2015.com", "mytemp.email", "mytrashmail.com", "nomail.xl.cx",
    "nospam.ze.tc", "nowmymail.com", "objectmail.com", "obobbo.com", "oneoffemail.com", "onewaymail.com", "owlpic.com",
    "pookmail.com", "proxymail.eu", "quickinbox.com", "rcpt.at", "rhyta.com", "rmqkr.net", "rtrtr.com", "safetymail.info",
    "sharklasers.com", "shieldemail.com", "shitmail.me", "sneakemail.com", "sogetthis.com", "spam4.me", "spamavert.com",
    "spambob.com", "spambog.com", "spambox.us", "spamex.com", "spamfree24.org", "spamgourmet.com", "spamhole.com",
    "spaml.com", "spamspot.com", "superrito.com", "tafmail.com", "teleworm.us", "temp-mail.org", "temp-mail.ru",
    "tempail.com", "tempe-mail.com", "tempemail.com", "tempemail.net", "tempinbox.com", "tempmail.com", "tempmail.de",
    "tempmail.net", "tempmailer.com", "tempomail.fr", "temporaryemail.net", "temporaryinbox.com", "tempr.email",
    "thankyou2010.com", "throwam.com", "throwawayemailaddress.com", "throwawaymail.com", "tmail.ws", "tmailinator.com",
    "trash-mail.com", "trash-mail.de", "trash2009.com", "trashemail.de", "trashmail.at", "trashmail.com", "trashmail.io",
    "trashmail.me", "trashmail.net", "trashymail.com", "trbvm.com", "tyldd.com", "uggsrock.com", "wegwerfmail.de",
    "wegwerfmail.net", "wegwerfmail.org", "wh4f.org", "yopmail.com", "yopmail.fr", "yopmail.net", "zoemail.org",
    "yomail.info", "mailtemp.net", "tempmailo.com", "emailfake.com", "fakemail.net", "cuvox.de", "armyspy.com",
    "guerrillamail.top", "inboxkitten.com", "luxusmail.org", "mailpoof.com", "tmpmail.org", "tmpmail.net",
}


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def validate_email(raw: str, extra_blocklist: set[str] | None = None) -> str | None:
    """Return the normalized address, or None when it must be refused."""
    email = normalize_email(raw)
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        return None
    domain = email.rsplit("@", 1)[1]
    tld = domain.rsplit(".", 1)[-1]
    if tld in RESERVED_TLDS or domain in RESERVED_DOMAINS:
        return None
    if domain in DISPOSABLE_DOMAINS or (extra_blocklist and domain in extra_blocklist):
        return None
    # Sub-domains of disposable providers (e.g. abc.mailinator.com).
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in DISPOSABLE_DOMAINS:
            return None
    return email


# -------------------------------------------------------------------- tokens
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256((settings.secret_key + ":" + token).encode()).hexdigest()


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


# ---------------------------------------------------------------- second factor
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=settings.app_name)


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    code = re.sub(r"\D", "", code)
    if len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def new_backup_codes(n: int = 10) -> tuple[list[str], str]:
    """Return (plaintext codes to show once, JSON of hashes to store)."""
    codes = ["-".join(secrets.token_hex(2) for _ in range(3)) for _ in range(n)]
    hashes = [hash_token(c.replace("-", "").lower()) for c in codes]
    return codes, json.dumps(hashes)


def consume_backup_code(stored_json: str, code: str) -> str | None:
    """Return the updated JSON with the code removed, or None if it did not match."""
    try:
        hashes: list[str] = json.loads(stored_json or "[]")
    except json.JSONDecodeError:
        return None
    target = hash_token(re.sub(r"[^0-9a-f]", "", (code or "").lower()))
    for h in hashes:
        if hmac.compare_digest(h, target):
            hashes.remove(h)
            return json.dumps(hashes)
    return None


def new_email_otp() -> tuple[str, str, str]:
    """Return (code, hash, expires_at ISO). Six digits, ten minutes."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    return code, hash_token(code), iso(utcnow() + timedelta(minutes=10))


def verify_email_otp(stored_hash: str, expires_at: str | None, code: str) -> bool:
    if not stored_hash or not expires_at or not code:
        return False
    exp = parse_iso(expires_at)
    if not exp or exp < utcnow():
        return False
    return hmac.compare_digest(stored_hash, hash_token(re.sub(r"\D", "", code)))


# ------------------------------------------------------------------ admin lockout
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15


def lockout_remaining(locked_until: str | None) -> int:
    until = parse_iso(locked_until)
    if not until:
        return 0
    remaining = (until - utcnow()).total_seconds()
    return int(remaining) if remaining > 0 else 0


def record_admin_failure(conn: sqlite3.Connection, admin_id: int) -> None:
    row = conn.execute("SELECT failed_attempts FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
    attempts = (row["failed_attempts"] if row else 0) + 1
    locked_until = None
    if attempts >= LOCKOUT_THRESHOLD:
        # escalate: 15 min, then 30, 60 ... capped at 24h
        factor = min(2 ** (attempts - LOCKOUT_THRESHOLD), 96)
        locked_until = iso(utcnow() + timedelta(minutes=LOCKOUT_MINUTES * factor))
    conn.execute("UPDATE admin_users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (attempts, locked_until, admin_id))


def record_admin_success(conn: sqlite3.Connection, admin_id: int) -> None:
    conn.execute("UPDATE admin_users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?", (iso(), admin_id))


def order_number() -> str:
    """QD-YYMMDD-XXXX; the random suffix keeps order counts private."""
    return f"QD-{utcnow():%y%m%d}-{secrets.token_hex(2).upper()}"
