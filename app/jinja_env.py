"""Jinja environment: autoescape always on, custom filters registered here so
the deploy-time parse check (scripts/check_templates.py) can import the same
environment and never false-fail on est_date / fmt_money.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .config import settings
from .security import parse_iso
from .services import markdown_lite
from .services.catalog import every

try:
    _TZ = ZoneInfo(settings.tz)
except Exception:  # noqa: BLE001 - validate() reports it; keep imports alive
    _TZ = ZoneInfo("America/New_York")


def _to_local(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        dt = parse_iso(str(value))
        if dt is None:
            return None
    return dt.astimezone(_TZ)


def est_date(value) -> str:
    dt = _to_local(value)
    return f"{dt:%b} {dt.day}, {dt:%Y}" if dt else ""


def est_datetime(value) -> str:
    dt = _to_local(value)
    if not dt:
        return ""
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt:%b} {dt.day}, {dt:%Y} {hour}:{dt:%M} {dt:%p} ET"


def est_time(value) -> str:
    dt = _to_local(value)
    if not dt:
        return ""
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{dt:%M} {dt:%p}"


def est_short(value) -> str:
    dt = _to_local(value)
    return f"{dt.month}/{dt.day}/{dt:%y}" if dt else ""


def fmt_money(cents, show_zero_cents: bool = True) -> str:
    """Money is integer cents everywhere. Digit strings are accepted; anything
    else is a caller bug and is logged instead of silently rendering $0.00."""
    if cents is None or cents == "":
        cents = 0
    elif isinstance(cents, bool):
        cents = int(cents)
    elif isinstance(cents, str) and cents.strip().lstrip("-").isdigit():
        cents = int(cents.strip())
    elif isinstance(cents, float) and cents.is_integer():
        cents = int(cents)
    elif not isinstance(cents, int):
        logging.getLogger("qd.templates").warning("fmt_money got %r (%s); expected integer cents", cents, type(cents).__name__)
        cents = 0
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    dollars, rem = divmod(cents, 100)
    if not show_zero_cents and rem == 0:
        return f"{sign}${dollars:,}"
    return f"{sign}${dollars:,}.{rem:02d}"


def fmt_money_short(cents) -> str:
    return fmt_money(cents, show_zero_cents=False)


def plural(n, singular: str, plural_form: str | None = None) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n} {word}"


def render_jsonld(data) -> Markup:
    """JSON-LD for a <script> tag. `<`, `>` and `&` become unicode escapes so
    review text can never close the script element (a stored-XSS class of bug)."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    raw = raw.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return Markup(raw)


_asset_cache: dict[str, tuple[float, str]] = {}


def asset(path: str) -> str:
    """Versioned static URL. CSS comes from the build manifest (hashed filename);
    everything else gets a content-hash query string. Both survive nginx's
    30-day /static/ cache because the URL changes when the file does."""
    static_dir = settings.static_dir
    if path == "css/app.css":
        manifest = static_dir / "css" / "manifest.json"
        if manifest.exists():
            try:
                name = json.loads(manifest.read_text())["app.css"]
                return f"/static/css/{name}"
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        return "/static/css/app.css"
    file = static_dir / path
    try:
        mtime = file.stat().st_mtime
    except OSError:
        return f"/static/{path}"
    cached = _asset_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    digest = hashlib.sha1(file.read_bytes()).hexdigest()[:10]
    url = f"/static/{path}?v={digest}"
    _asset_cache[path] = (mtime, url)
    return url


def build_env(templates_dir: Path | None = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir or settings.templates_dir)),
        # HTML always escaped; plain-text email twins (.txt) are not HTML and must not be.
        autoescape=select_autoescape(enabled_extensions=("html", "htm", "xml"), disabled_extensions=("txt",), default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        est_date=est_date,
        est_datetime=est_datetime,
        est_time=est_time,
        est_short=est_short,
        fmt_money=fmt_money,
        fmt_money_short=fmt_money_short,
        plural=plural,
        every=every,
        md=markdown_lite.render,
    )
    env.globals.update(
        settings=settings,
        render_jsonld=render_jsonld,
        asset=asset,
        current_year=lambda: datetime.now(_TZ).year,
    )
    return env


env = build_env()
