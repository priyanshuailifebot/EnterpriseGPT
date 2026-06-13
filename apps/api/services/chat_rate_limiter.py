"""Per-session rate limits + budgets for the chat runtime.

Three ceilings, each independently optional, configured on the
``TriggerNode.rate_limits`` field:

* ``messages_per_minute`` — sliding-window message count. Protects against
  a runaway client looping on the message endpoint.
* ``max_total_tokens``    — cumulative prompt + completion tokens for the
  session. Hard cap on a single conversation's LLM budget.
* ``max_total_cost_cents``— cumulative USD-cent ceiling derived from the
  pricing table. Same protection as ``max_total_tokens`` but expressed in
  the unit ops actually monitor.

Design choices:

* **Pre-flight rate check** runs BEFORE the LLM call so we never burn
  tokens on a request we're going to reject. Token + cost ceilings can
  only be checked post-hoc because we don't know the response size yet,
  so they're enforced against the *cumulative* totals stored on the
  ChatSession — the next turn is rejected once the prior ones pushed us
  past the budget.
* **Fail open on Redis errors.** Hard-failing every chat message because
  Redis flapped would be worse than briefly losing rate-limit precision.
  The Redis client raises on connection issues; we log + allow.
* **Sliding window via sorted set** — ``ZADD`` the message timestamp and
  ``ZRANGEBYSCORE`` the trailing 60 s window. Faster + more accurate
  than the fixed-bucket counter pattern; the cost is one extra
  millisecond per request which is well within budget.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from models.chat_session import ChatSession
from services.llm_pricing import microcents_to_cents

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str | None = None
    retry_after_seconds: int | None = None
    # When ``allowed=False`` due to a budget cap we surface the current
    # totals so the UI can render an informative message.
    snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class RateLimitConfig:
    """Parsed shape of ``TriggerNode.rate_limits``."""

    messages_per_minute: int | None = None
    max_total_tokens: int | None = None
    max_total_cost_cents: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> RateLimitConfig | None:
        if not raw:
            return None
        mpm = raw.get("messages_per_minute")
        mtt = raw.get("max_total_tokens")
        mtc = raw.get("max_total_cost_cents")
        if mpm is None and mtt is None and mtc is None:
            return None
        return cls(
            messages_per_minute=(int(mpm) if mpm is not None else None),
            max_total_tokens=(int(mtt) if mtt is not None else None),
            max_total_cost_cents=(int(mtc) if mtc is not None else None),
        )


def _msg_window_key(session_id: UUID) -> str:
    return f"egpt:chat:msg_window:{session_id}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class RateLimiter:
    """Stateless wrapper around the Redis ops. Safe to instantiate per request."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # Pre-flight: called before the LLM call. ``session`` MUST be the
    # latest row from the DB (with cumulative totals).
    # ------------------------------------------------------------------

    async def check(
        self,
        *,
        config: RateLimitConfig | None,
        session: ChatSession,
    ) -> RateLimitDecision:
        if config is None:
            return RateLimitDecision(allowed=True)

        # 1) Budget caps — cheap. Cumulative totals already on the row.
        if config.max_total_tokens is not None:
            used = session.total_prompt_tokens + session.total_completion_tokens
            if used >= config.max_total_tokens:
                return RateLimitDecision(
                    allowed=False,
                    reason="max_total_tokens_exceeded",
                    snapshot={
                        "used_tokens": used,
                        "max_tokens": config.max_total_tokens,
                    },
                )
        if config.max_total_cost_cents is not None:
            used_cents = microcents_to_cents(session.total_cost_microcents)
            if used_cents >= config.max_total_cost_cents:
                return RateLimitDecision(
                    allowed=False,
                    reason="max_total_cost_exceeded",
                    snapshot={
                        "used_cents": used_cents,
                        "max_cents": config.max_total_cost_cents,
                    },
                )

        # 2) Sliding window message-rate check.
        if config.messages_per_minute is not None:
            now = time.time()
            window_start = now - 60.0
            key = _msg_window_key(session.id)
            try:
                # Drop entries older than the window, count remaining.
                pipe = self._redis.pipeline()
                pipe.zremrangebyscore(key, 0, window_start)
                pipe.zcard(key)
                _, count = await pipe.execute()
            except Exception as exc:  # noqa: BLE001
                # Fail-open on Redis errors. Log so ops notices.
                log.warning("rate_limiter.redis_error_fail_open", error=str(exc))
                return RateLimitDecision(allowed=True)

            if int(count or 0) >= config.messages_per_minute:
                # Compute Retry-After from the oldest entry in the window.
                try:
                    oldest = await self._redis.zrange(key, 0, 0, withscores=True)
                    retry_after = (
                        max(1, int(60 - (now - float(oldest[0][1]))))
                        if oldest else 30
                    )
                except Exception:  # noqa: BLE001
                    retry_after = 30
                return RateLimitDecision(
                    allowed=False,
                    reason="messages_per_minute_exceeded",
                    retry_after_seconds=retry_after,
                    snapshot={
                        "messages_in_window": int(count or 0),
                        "max_messages": config.messages_per_minute,
                    },
                )

        return RateLimitDecision(allowed=True)

    # ------------------------------------------------------------------
    # Record a successful message attempt in the sliding window. Called
    # AFTER ``check`` returns ``allowed=True`` and BEFORE the runtime
    # actually drives the LLM (so even if the LLM blows up, the slot was
    # consumed — otherwise a failing model lets a client retry forever).
    # ------------------------------------------------------------------

    async def record(
        self, *, config: RateLimitConfig | None, session_id: UUID
    ) -> None:
        if config is None or config.messages_per_minute is None:
            return
        now = time.time()
        key = _msg_window_key(session_id)
        try:
            pipe = self._redis.pipeline()
            pipe.zadd(key, {f"{now}:{session_id}:{int(now*1000)}": now})
            pipe.expire(key, 120)  # keep the key warm a touch longer than the window
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("rate_limiter.record_failed", error=str(exc))


__all__ = ["RateLimitConfig", "RateLimitDecision", "RateLimiter"]
