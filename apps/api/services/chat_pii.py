"""Per-session PII redaction helper for the chat runtime.

Design choice: redact at the **persistence boundary**. The LLM and tools
operate on raw text (so tool dispatch like ``check_customer_exists("alice@…")``
keeps working without surgery). What we redact is what hits durable
storage — the ``chat_messages`` rows and the Redis ``MemoryStore`` lists.

A single ``ChatPIIRedactor`` is instantiated per inbound request. It:

1. Loads the existing per-session token map from Redis (if any).
2. On every ``redact_for_persistence(text)`` call, extracts new tokens and
   accumulates them into the in-memory map (de-duped against existing).
3. On every ``restore_for_display(text)`` / ``restore_for_llm(text)`` call,
   substitutes tokens back to their original values.
4. ``flush()`` writes the accumulated map back to Redis under the session
   key with a sliding TTL.

This keeps the durable layer (Postgres + Redis memory list) free of PII
at rest while the in-process layer (LLM messages, tool args) sees the
real content. Compliance gets the "no PII at rest" property; functional
correctness is preserved.
"""

from __future__ import annotations

import logging
from uuid import UUID

from services.pii_service import PIIService, PIIToken

log = logging.getLogger(__name__)


class ChatPIIRedactor:
    """Owns one session's token map for the duration of a request."""

    def __init__(self, pii: PIIService, *, session_id: UUID) -> None:
        self._pii = pii
        self._session_id = str(session_id)
        self._map: dict[str, PIIToken] = {}
        self._loaded = False

    async def load(self) -> None:
        """Load the existing token map from Redis. Idempotent."""
        if self._loaded:
            return
        try:
            self._map = await self._pii.load_token_map(self._session_id)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("chat_pii.load_failed", session=self._session_id, error=str(exc))
            self._map = {}
        self._loaded = True

    def redact_for_persistence(self, text: str | None) -> str | None:
        """Replace any new PII with tokens and merge into the running map.

        ``None`` and empty strings pass through unchanged. Strings that
        contain no PII matches return verbatim.
        """
        if not text:
            return text
        redacted, new_tokens = self._pii.redact(text)
        if new_tokens:
            self._map.update(new_tokens)
        return redacted

    def restore_for_display(self, text: str | None) -> str | None:
        """Substitute every known token back to its original value."""
        if not text or not self._map:
            return text
        return self._pii.restore(text, self._map)

    # Display vs LLM restore have identical semantics today but diverging
    # later as we add per-policy variants (e.g. mask all but last 4 of a
    # card number). Keeping two named methods makes those policy hooks
    # explicit at call sites.
    restore_for_llm = restore_for_display

    async def flush(self, *, ttl_seconds: int | None = None) -> None:
        if not self._map:
            return
        try:
            kwargs: dict[str, int] = {}
            if ttl_seconds is not None:
                kwargs["ttl_seconds"] = ttl_seconds
            await self._pii.save_token_map(self._session_id, self._map, **kwargs)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("chat_pii.flush_failed", session=self._session_id, error=str(exc))

    @property
    def token_count(self) -> int:
        return len(self._map)


__all__ = ["ChatPIIRedactor"]
