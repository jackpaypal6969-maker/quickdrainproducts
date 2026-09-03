"""Settings. Everything host-specific comes from .env; nothing here is absolute.

The only path computed in code is the package's own parent directory, used as the
fallback for APP_DIR when .env does not set it (local development). Production
always sets APP_DIR, DB_PATH, MEDIA_DIR and BACKUP_DIR explicitly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_APP_DIR = _PACKAGE_DIR.parent

load_dotenv(_DEFAULT_APP_DIR / ".env", override=False)


def _flag(name: str, default: str = "off") -> bool:
    # A present-but-empty key (`KEY=`) means "use the default", not "empty".
    return (os.environ.get(name) or default).strip().lower() in {"1", "on", "true", "yes"}


def _int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    app_name: str = field(default_factory=lambda: _str("APP_NAME", "Quick Drain Products"))
    env: str = field(default_factory=lambda: _str("ENV", "development"))
    app_dir: Path = field(default_factory=lambda: Path(_str("APP_DIR") or _DEFAULT_APP_DIR))
    db_path: Path = field(default_factory=lambda: Path(_str("DB_PATH") or (Path(_str("APP_DIR") or _DEFAULT_APP_DIR) / "data" / "quick-drain.db")))
    media_dir: Path = field(default_factory=lambda: Path(_str("MEDIA_DIR") or (Path(_str("APP_DIR") or _DEFAULT_APP_DIR) / "media")))
    backup_dir: Path = field(default_factory=lambda: Path(_str("BACKUP_DIR") or (Path(_str("APP_DIR") or _DEFAULT_APP_DIR) / "backups")))
    base_url: str = field(default_factory=lambda: _str("BASE_URL", "http://127.0.0.1:8006").rstrip("/"))
    server_name: str = field(default_factory=lambda: _str("SERVER_NAME", "localhost"))
    port: int = field(default_factory=lambda: _int("PORT", 8006))
    public_port: int = field(default_factory=lambda: _int("PUBLIC_PORT", 8086))
    workers: int = field(default_factory=lambda: _int("WORKERS", 2))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "info"))
    tz: str = field(default_factory=lambda: _str("TZ", "America/New_York"))

    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY"))
    cookie_secure: bool = field(default_factory=lambda: _flag("COOKIE_SECURE", "off"))
    session_days: int = field(default_factory=lambda: _int("SESSION_DAYS", 30))

    admin_basic_auth_user: str = field(default_factory=lambda: _str("ADMIN_BASIC_AUTH_USER"))
    admin_basic_auth_password: str = field(default_factory=lambda: _str("ADMIN_BASIC_AUTH_PASSWORD"))
    admin_allow_ips: tuple[str, ...] = field(default_factory=lambda: tuple(p.strip() for p in _str("ADMIN_ALLOW_IPS").split(",") if p.strip()))
    admin_2fa: str = field(default_factory=lambda: (_str("ADMIN_2FA") or ("full" if _flag("ADMIN_2FA_REQUIRED", "on") else "authenticator")).lower())

    @property
    def admin_2fa_required(self) -> bool:
        """Email code required after the authenticator (the 'full' chain)."""
        return self.admin_2fa == "full"

    @property
    def admin_totp_required(self) -> bool:
        return self.admin_2fa in ("full", "authenticator")

    stripe_publishable_key: str = field(default_factory=lambda: _str("STRIPE_PUBLISHABLE_KEY"))
    stripe_secret_key: str = field(default_factory=lambda: _str("STRIPE_SECRET_KEY"))
    stripe_webhook_secret: str = field(default_factory=lambda: _str("STRIPE_WEBHOOK_SECRET"))
    stripe_tax_enabled: bool = field(default_factory=lambda: _flag("STRIPE_TAX_ENABLED", "on"))
    subscriptions_enabled: bool = field(default_factory=lambda: _flag("SUBSCRIPTIONS_ENABLED", "off"))

    flat_shipping_cents: int = field(default_factory=lambda: _int("FLAT_SHIPPING_CENTS", 695))
    free_shipping_threshold_cents: int = field(default_factory=lambda: _int("FREE_SHIPPING_THRESHOLD_CENTS", 4900))
    ship_to_countries: tuple[str, ...] = field(default_factory=lambda: tuple(c.strip().upper() for c in _str("SHIP_TO_COUNTRIES", "US").split(",") if c.strip()))

    resend_api_key: str = field(default_factory=lambda: _str("RESEND_API_KEY"))
    resend_webhook_secret: str = field(default_factory=lambda: _str("RESEND_WEBHOOK_SECRET"))
    email_from: str = field(default_factory=lambda: _str("EMAIL_FROM", "Quick Drain Products <orders@example.invalid>"))
    email_reply_to: str = field(default_factory=lambda: _str("EMAIL_REPLY_TO"))
    contact_inbox: str = field(default_factory=lambda: _str("CONTACT_INBOX"))
    email_dry_run: bool = field(default_factory=lambda: _flag("EMAIL_DRY_RUN", "off"))

    parent_site_url: str = field(default_factory=lambda: _str("PARENT_SITE_URL", "https://www.quickdrainny.com").rstrip("/"))
    booking_url: str = field(default_factory=lambda: _str("BOOKING_URL", "https://www.quickdrainny.com"))
    phone_display: str = field(default_factory=lambda: _str("PHONE_DISPLAY", "(631) 888-6200"))
    phone_tel: str = field(default_factory=lambda: _str("PHONE_TEL", "+16318886200"))

    posthog_key: str = field(default_factory=lambda: _str("POSTHOG_KEY"))
    posthog_host: str = field(default_factory=lambda: _str("POSTHOG_HOST", "https://us.i.posthog.com"))
    google_site_verification: str = field(default_factory=lambda: _str("GOOGLE_SITE_VERIFICATION"))

    abandoned_cart_hours: int = field(default_factory=lambda: _int("ABANDONED_CART_HOURS", 4))
    reorder_reminder_lead_days: int = field(default_factory=lambda: _int("REORDER_REMINDER_LEAD_DAYS", 5))

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def stripe_is_live(self) -> bool:
        return self.stripe_secret_key.startswith("sk_live_")

    @property
    def templates_dir(self) -> Path:
        return _DEFAULT_APP_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return _DEFAULT_APP_DIR / "static"

    def validate(self) -> list[str]:
        """Return a list of fatal misconfigurations. Empty means bootable."""
        problems: list[str] = []
        if not self.secret_key or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set and at least 32 characters (openssl rand -hex 32)")
        if self.is_production and not self.cookie_secure and self.base_url.startswith("https://"):
            problems.append("BASE_URL is https but COOKIE_SECURE is off")
        if self.stripe_is_live and not _flag("STRIPE_LIVE_OK", "off"):
            # Deliberate: the brief keeps Stripe in test mode until told otherwise, in every environment.
            problems.append("STRIPE_SECRET_KEY is a live key; live mode is not enabled for this build (set STRIPE_LIVE_OK=on to override)")
        if self.is_production and self.stripe_secret_key and not self.stripe_webhook_secret:
            problems.append("STRIPE_WEBHOOK_SECRET is empty: Stripe would take payments that never become orders")
        if self.is_production and not self.email_dry_run and not self.resend_api_key:
            problems.append("RESEND_API_KEY is empty and EMAIL_DRY_RUN is off: no email could be sent")
        if self.is_production and "@" not in self.email_from:
            problems.append("EMAIL_FROM must be a sender address like 'Quick Drain Products <orders@your-domain>'")
        if self.admin_2fa not in ("full", "authenticator", "off"):
            problems.append("ADMIN_2FA must be full, authenticator or off")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(self.tz)
        except Exception:  # noqa: BLE001
            problems.append(f"TZ '{self.tz}' is not a valid IANA zone (use America/New_York)")
        return problems


settings = Settings()
