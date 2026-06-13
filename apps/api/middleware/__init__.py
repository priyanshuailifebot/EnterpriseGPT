"""Custom Starlette / FastAPI middleware stack.

Middleware order matters: in FastAPI the **last** added middleware is
the **outermost** layer. ``register_middleware`` adds them in the order
expected by the request flow:

    request_id → security → CORS → rate_limit → audit → app
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import Settings
from middleware.audit import AuditLogMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIdMiddleware
from middleware.security import SecurityHeadersMiddleware


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Attach every Phase 1 middleware to ``app`` in the correct order."""
    # Innermost first — the LAST added is OUTERMOST in Starlette.
    app.add_middleware(AuditLogMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)


__all__ = [
    "AuditLogMiddleware",
    "RateLimitMiddleware",
    "RequestIdMiddleware",
    "SecurityHeadersMiddleware",
    "register_middleware",
]
