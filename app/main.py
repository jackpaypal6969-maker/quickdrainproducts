"""Application factory. Docs are off in production, sessions ride a signed
cookie, every response gets security headers, and /admin sits behind an
optional IP allow-list or HTTP basic auth for the raw-port phase."""
from __future__ import annotations

import base64
import hmac
import json
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .config import settings
from .db import connect, migrate, one
from .deps import render
from .security import clear_session, client_ip, load_session, save_session

log = logging.getLogger("qd")
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        session = load_session(request)
        before = json.dumps(session, sort_keys=True, default=str)
        request.state.session = session
        request.state.clear_session = False
        response = await call_next(request)
        if request.state.clear_session:
            clear_session(response)
        elif json.dumps(session, sort_keys=True, default=str) != before:
            save_session(response, session)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/admin"):
            gate = _admin_gate(request)
            if gate is not None:
                return gate
        response = await call_next(request)
        posthog = f"{settings.posthog_host} https://*.posthog.com" if settings.posthog_key else ""
        csp = (
            "default-src 'self'; "
            f"script-src 'self' {posthog}; ".replace("  ", " ")
            + "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self'; "
            f"connect-src 'self' {posthog}; ".replace("  ", " ")
            + "frame-src https://checkout.stripe.com; "
            "form-action 'self' https://checkout.stripe.com; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(self \"https://checkout.stripe.com\")")
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith(("/admin", "/account", "/cart", "/checkout")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _admin_gate(request: Request) -> Response | None:
    ip = client_ip(request)
    if settings.admin_allow_ips and ip not in settings.admin_allow_ips:
        return PlainTextResponse("Not available from this address.", status_code=403)
    if settings.admin_basic_auth_user and settings.admin_basic_auth_password:
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(user, settings.admin_basic_auth_user) and hmac.compare_digest(password, settings.admin_basic_auth_password)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return PlainTextResponse("Authentication required.", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Quick Drain admin"'})
    return None


def _seed_defaults() -> None:
    conn = connect()
    try:
        defaults = {
            "promo_banner": "",
            "promo_banner_enabled": "0",
            "newsletter_discount_percent": "10",
            "newsletter_enabled": "1",
            "reviews_enabled": "1",
            "store_notice": "",
            "email_blocklist_extra": "",
            "subscription_discount_percent": "10",
            "subscription_intervals": "1,2,3",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    finally:
        conn.close()


def create_app() -> FastAPI:
    problems = settings.validate()
    for p in problems:
        log.error("CONFIG: %s", p)
    if problems:
        # Every validate() entry is fatal: unsigned sessions, a live key without the
        # explicit override, or an https BASE_URL with a non-Secure cookie.
        raise RuntimeError("Refusing to start: " + "; ".join(problems))

    prod = settings.is_production
    app = FastAPI(
        title=settings.app_name,
        docs_url=None if prod else "/docs",
        redoc_url=None if prod else "/redoc",
        openapi_url=None if prod else "/openapi.json",
    )
    migrate()
    _seed_defaults()

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SessionMiddleware)

    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    from .routers import account, cart, content, engage, seo, storefront, webhooks
    from .routers.admin import router as admin_router

    app.include_router(webhooks.router)
    app.include_router(seo.router)
    app.include_router(storefront.router)
    app.include_router(cart.router)
    app.include_router(account.router)
    app.include_router(content.router)
    app.include_router(engage.router)
    app.include_router(admin_router)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        conn = connect()
        try:
            products = one(conn, "SELECT COUNT(*) AS n FROM products")["n"]
        finally:
            conn.close()
        return JSONResponse({"ok": True, "env": settings.env, "products": products, "stripe": bool(settings.stripe_secret_key), "stripe_mode": "live" if settings.stripe_is_live else "test"})

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        location = (exc.headers or {}).get("Location") if exc.headers else None
        if exc.status_code in {302, 303, 307} and location:
            return RedirectResponse(location, status_code=exc.status_code)
        wants_json = "application/json" in request.headers.get("accept", "") or request.headers.get("x-requested-with") == "fetch"
        if wants_json:
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        if not hasattr(request.state, "session"):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)
        if exc.status_code == 404:
            return render(request, "pages/404.html", status_code=404)
        return render(request, "pages/error.html", {"status_code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": False, "error": "Invalid input."}, status_code=422)
        return render(request, "pages/error.html", {"status_code": 422, "detail": "That form was missing something. Go back and try again."}, status_code=422)

    @app.exception_handler(Exception)
    async def server_error_handler(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        if not hasattr(request.state, "session"):
            return PlainTextResponse("Something went wrong.", status_code=500)
        try:
            return render(request, "pages/500.html", status_code=500)
        except Exception:  # noqa: BLE001
            return PlainTextResponse("Something went wrong.", status_code=500)

    return app


app = create_app()
