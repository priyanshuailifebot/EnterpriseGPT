"""Redis-backed sliding-window rate limiter.

Identity precedence (highest to lowest):

    1. API key  → ``api:{key_prefix}`` ............... 1000 req / minute
    2. User JWT → ``user:{user_id}``  ................   60 req / minute
    3. Anonymous IP → ``ip:{client_ip}``  ............   60 req / minute

Implementation uses one Redis sorted set per identity holding a Unix
timestamp per request. On each request we trim entries older than the
window, count what's left, push the new entry, and return 429 with a
``Retry-After`` header if the count exceeds the limit.

Skipped paths: ``/health``, ``/ready``, ``/docs``, ``/redoc``,
``/openapi.json``, and the OpenAPI schema. They must remain available
to load balancers and to humans hitting the docs while debugging.
"""

from __future__ import annotations

import time
from typing import Final

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from core.config import get_settings
from core.redis import get_redis
from core.security import ALGORITHM


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

DEFAULT_USER_LIMIT: Final = 60
DEFAULT_IP_LIMIT: Final = 60
DEFAULT_API_KEY_LIMIT: Final = 1000
WINDOW_SECONDS: Final = 60
KEY_PREFIX: Final = "egpt:rl:"

SKIPPED_PATHS: Final = frozenset(
    {"/health", "/ready", "/", "/docs", "/redoc", "/openapi.json"}
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        user_limit: int = DEFAULT_USER_LIMIT,
        ip_limit: int = DEFAULT_IP_LIMIT,
        api_key_limit: int = DEFAULT_API_KEY_LIMIT,
        window_seconds: int = WINDOW_SECONDS,
    ) -> None:
        super().__init__(app)
        self.user_limit = user_limit
        self.ip_limit = ip_limit
        self.api_key_limit = api_key_limit
        self.window_seconds = window_seconds

    # ---- identity resolution ----

    def _identity(self, request: Request) -> tuple[str, int]:
        """Return the rate-limit (key, limit) tuple for this request."""
        api_key = request.headers.get("X-API-Key")
        if api_key:
            prefix = api_key[:8]
            return f"api:{prefix}", self.api_key_limit

        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            try:
                payload = jwt.decode(
                    token,
                    get_settings().SECRET_KEY,
                    algorithms=[ALGORITHM],
                    options={"verify_exp": False},  # rate-limit even if expired
                )
                sub = payload.get("sub")
                if sub:
                    return f"user:{sub}", self.user_limit
            except JWTError:
                pass

        client_host = request.client.host if request.client else "unknown"
        return f"ip:{client_host}", self.ip_limit

    # ---- main dispatch ----

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in SKIPPED_PATHS:
            return await call_next(request)

        identity_key, limit = self._identity(request)
        redis_key = f"{KEY_PREFIX}{identity_key}"
        now = time.time()
        cutoff = now - self.window_seconds

        redis = get_redis()
        # Trim expired entries and count what remains — do NOT record this
        # request yet so that rejected (429) requests don't consume a slot.
        pipe = redis.pipeline()
        pipe.zremrangebyscore(redis_key, 0, cutoff)
        pipe.zcard(redis_key)
        _, current_count = await pipe.execute()

        if current_count >= limit:
            # Determine when the oldest entry will roll out of the window.
            oldest = await redis.zrange(redis_key, 0, 0, withscores=True)
            if oldest:
                oldest_ts = float(oldest[0][1])
                retry_after = max(1, int(self.window_seconds - (now - oldest_ts)))
            else:
                retry_after = 1
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "limit": limit,
                    "window_seconds": self.window_seconds,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # Request is being served — record it now.
        pipe2 = redis.pipeline()
        pipe2.zadd(redis_key, {f"{now}:{identity_key}": now})
        pipe2.expire(redis_key, self.window_seconds + 5)
        await pipe2.execute()

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(
            max(0, limit - current_count - 1)
        )
        return response
