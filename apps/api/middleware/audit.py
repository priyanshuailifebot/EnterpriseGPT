"""Audit-logging middleware.

For every state-changing HTTP method (``POST``, ``PUT``, ``PATCH``,
``DELETE``) we asynchronously persist a row to the ``audit_logs`` table
with the user id (if a Bearer token is present), method, path, status
code, IP, user agent, and duration. The work runs *after* the response
is sent so it never blocks the client.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Final

from jose import JWTError, jwt
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.config import get_settings
from core.database import get_session_factory
from core.logging import get_logger
from core.security import ALGORITHM
from models.audit_log import AuditLog


_AUDITED_METHODS: Final = {"POST", "PUT", "PATCH", "DELETE"}
_SKIPPED_PATH_PREFIXES: Final = ("/health", "/ready", "/docs", "/redoc", "/openapi")

logger = get_logger("enterprisegpt.middleware.audit")


def _user_id_from_request(request: Request) -> uuid.UUID | None:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(
            token,
            get_settings().SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
    except JWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        return uuid.UUID(sub)
    except ValueError:
        return None


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _resource_from_path(path: str) -> tuple[str | None, str | None]:
    """Best-effort ``(resource_type, resource_id)`` extraction.

    For paths like ``/api/v1/workflows/abc/execute``:

        resource_type = "workflows"
        resource_id   = "abc"
    """
    parts = [p for p in path.strip("/").split("/") if p]
    # Skip leading "api" / "v1" segments.
    while parts and parts[0] in {"api", "v1"}:
        parts.pop(0)
    if not parts:
        return None, None
    resource_type = parts[0]
    resource_id = parts[1] if len(parts) > 1 else None
    return resource_type, resource_id


async def _persist_audit_log(
    *,
    user_id: uuid.UUID | None,
    method: str,
    path: str,
    status_code: int,
    ip_address: str | None,
    user_agent: str | None,
    duration_ms: int,
    request_id: str | None,
) -> None:
    factory = get_session_factory()
    resource_type, resource_id = _resource_from_path(path)
    try:
        async with factory() as session:
            session.add(
                AuditLog(
                    user_id=user_id,
                    action=f"{method} {path}",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    ip_address=ip_address,
                    payload={
                        "status_code": status_code,
                        "user_agent": user_agent,
                        "duration_ms": duration_ms,
                        "request_id": request_id,
                    },
                )
            )
            await session.commit()
    except SQLAlchemyError as exc:
        logger.warning("audit.persist_failed", error=str(exc))


class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        method = request.method.upper()
        if method not in _AUDITED_METHODS or any(
            request.url.path.startswith(p) for p in _SKIPPED_PATH_PREFIXES
        ):
            return await call_next(request)

        start = time.perf_counter()
        user_id = _user_id_from_request(request)
        ip_address = _client_ip(request)
        user_agent = request.headers.get("user-agent")
        request_id = getattr(request.state, "request_id", None)

        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        # Fire-and-forget — never block the response on audit IO.
        asyncio.create_task(
            _persist_audit_log(
                user_id=user_id,
                method=method,
                path=request.url.path,
                status_code=response.status_code,
                ip_address=ip_address,
                user_agent=user_agent,
                duration_ms=duration_ms,
                request_id=request_id,
            )
        )
        return response
