"""Async Redis connection pool.

Initialized at app startup. Routes/services should call ``get_redis()`` to
obtain a client; the underlying connection pool is reused across requests.
"""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from core.config import get_settings

_pool: ConnectionPool | None = None
_client: Redis | None = None


def init_redis() -> Redis:
    """Create the Redis connection pool + client. Idempotent."""
    global _pool, _client

    if _client is not None:
        return _client

    settings = get_settings()
    _pool = ConnectionPool.from_url(
        settings.REDIS_URL,
        max_connections=50,
        decode_responses=True,
        encoding="utf-8",
    )
    _client = Redis(connection_pool=_pool)
    return _client


async def dispose_redis() -> None:
    """Close the Redis client and underlying connection pool."""
    global _pool, _client

    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect(inuse_connections=True)
        _pool = None


def get_redis() -> Redis:
    """Return the active Redis client, initializing it on first call."""
    if _client is None:
        return init_redis()
    return _client


async def ping_redis() -> bool:
    """Return True if Redis is reachable."""
    try:
        client = get_redis()
        return bool(await client.ping())
    except Exception:  # noqa: BLE001
        return False
