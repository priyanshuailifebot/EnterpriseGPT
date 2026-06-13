"""LangGraph Redis checkpointer (AsyncRedisSaver) with test-safe memory fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from core.config import Settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_checkpointer_singleton: object | None = None
_init_lock = asyncio.Lock()


def _should_use_memory(settings: Settings) -> bool:
    mode = getattr(settings, "LANGGRAPH_CHECKPOINTER_MODE", "auto").lower()
    if mode == "memory":
        return True
    if mode == "redis":
        return False
    return "enterprisegpt_test" in getattr(settings, "DATABASE_URL", "")


async def get_checkpointer(settings: Settings) -> object:
    """Return a shared LangGraph checkpointer (Redis or in-memory).

    Redis path uses AsyncRedisSaver and requires Redis Stack (JSON + RediSearch)
    modules. Falls back to in-memory saver if setup raises.
    """
    global _checkpointer_singleton
    async with _init_lock:
        if _checkpointer_singleton is not None:
            return _checkpointer_singleton

        if _should_use_memory(settings):
            from langgraph.checkpoint.memory import InMemorySaver

            saver = InMemorySaver()
            _checkpointer_singleton = saver
            logger.info("langgraph.checkpointer.memory")
            return saver

        ttl_minutes = getattr(settings, "LANGGRAPH_CHECKPOINT_DEFAULT_TTL_MINUTES", 1440)

        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver

            saver = AsyncRedisSaver(
                redis_url=settings.REDIS_URL,
                ttl={"default_ttl": float(ttl_minutes)},
            )
            await saver.asetup()
            await saver.aset_client_info()
            _checkpointer_singleton = saver
            logger.info("langgraph.checkpointer.redis")
        except Exception as exc:
            logger.warning(
                "langgraph.checkpointer.redis_failed_fallback_memory",
                error=str(exc),
            )
            from langgraph.checkpoint.memory import InMemorySaver

            _checkpointer_singleton = InMemorySaver()
        return _checkpointer_singleton


__all__ = ["get_checkpointer"]
