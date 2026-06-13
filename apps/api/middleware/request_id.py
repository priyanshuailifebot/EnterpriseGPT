"""Adds an ``X-Request-ID`` header to every request and binds it into
the structlog context so downstream log lines carry it automatically.

Implemented as a pure ASGI middleware (rather than
``BaseHTTPMiddleware``) to avoid the "No response returned." failure
mode that ``BaseHTTPMiddleware`` raises on client disconnects and
cancellations.
"""

from __future__ import annotations

import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send


REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp, *, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        self._header_name_bytes = header_name.encode("latin-1")
        self._header_name_lower = self._header_name_bytes.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming: str | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == self._header_name_lower:
                incoming = value.decode("latin-1")
                break

        request_id = incoming or uuid.uuid4().hex

        # Expose via ``request.state.request_id`` for downstream middleware/handlers.
        scope.setdefault("state", {})["request_id"] = request_id

        # Bind into structlog so every logger.* in the request scope sees it.
        # Each ASGI request runs in its own task, so contextvars are isolated.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        header_value_bytes = request_id.encode("latin-1")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != self._header_name_lower
                ]
                headers.append((self._header_name_bytes, header_value_bytes))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.clear_contextvars()
