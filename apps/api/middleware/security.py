"""Security headers on every API response (complements :class:`RequestIdMiddleware`)."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        rid = getattr(request.state, "request_id", None)
        if rid:
            response.headers.setdefault("X-Request-ID", rid)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin")
        return response
