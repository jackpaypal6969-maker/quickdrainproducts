"""Admin: every route in every sub-router depends on require_admin except the
login/2FA handshake in auth.py, which depends on nothing and sets the session."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...deps import csrf_protect, require_admin
from . import auth, catalog, content, customers, logs, marketing, orders, reports, reviews, support

router = APIRouter(prefix="/admin")
router.include_router(auth.router)

protected = APIRouter(dependencies=[Depends(csrf_protect), Depends(require_admin)])
for sub in (catalog, orders, customers, marketing, content, reviews, support, reports, logs):
    protected.include_router(sub.router)
router.include_router(protected)
