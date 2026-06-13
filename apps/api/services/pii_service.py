"""PII Shield — split-lane redact/restore.

Detects common PII patterns in any string of text, replaces each match
with a deterministic-looking opaque token, and lets callers later restore
the original values from a token map. Token maps are persisted in Redis
keyed by ``session_id`` with a 1-hour TTL so the redact / restore lanes
can run on different requests (the LLM never sees raw PII; the response
post-processor restores it before reaching the user).

Patterns covered: EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from typing import Final

from redis.asyncio import Redis

from core.redis import get_redis as _get_redis_global

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REDIS_KEY_PREFIX: Final = "egpt:pii:"
DEFAULT_TTL_SECONDS: Final = 3600  # 1 hour

# Patterns are evaluated in this order so longer/more specific matches win
# over shorter ones (e.g. credit card before phone).
PII_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "EMAIL": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
    "CREDIT_CARD": re.compile(
        r"\b(?:\d{4}[- ]?){3}\d{4}\b"
    ),
    "SSN": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b"
    ),
    "PHONE": re.compile(
        r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
    ),
    "IP_ADDRESS": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
    ),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PIIToken:
    """Single redaction record."""

    token: str        # e.g. "<<PII_EMAIL_a3f9b1>>"
    original: str     # original substring removed from the input
    pii_type: str     # one of PII_PATTERNS keys


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PIIService:
    """Stateless redact/restore service. Redis is only used by save/load."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis  # lazy — see _redis_client()

    # ---- core ops ----

    def redact(self, text: str) -> tuple[str, dict[str, PIIToken]]:
        """Replace every PII match with an opaque token.

        Returns the redacted text plus a token map suitable for ``restore``.
        Order is stable: same input → same set of tokens (the random hex
        differs per call so the same value can repeat across redactions
        without leaking equality).
        """
        token_map: dict[str, PIIToken] = {}
        if not text:
            return text, token_map

        for pii_type, pattern in PII_PATTERNS.items():
            # iterate over matches; replace one at a time to make sure we
            # never re-match a token we just produced.
            while True:
                match = pattern.search(text)
                if match is None:
                    break
                original = match.group(0)
                short_id = secrets.token_hex(3)  # 6 hex chars
                token = f"<<PII_{pii_type}_{short_id}>>"
                token_map[token] = PIIToken(
                    token=token, original=original, pii_type=pii_type
                )
                text = text[: match.start()] + token + text[match.end():]
        return text, token_map

    def restore(self, text: str, token_map: dict[str, PIIToken]) -> str:
        """Replace each token in ``text`` with its original value."""
        if not text or not token_map:
            return text
        for token, pii_token in token_map.items():
            text = text.replace(token, pii_token.original)
        return text

    # ---- persistence (Redis) ----

    def _redis_client(self) -> Redis:
        return self._redis if self._redis is not None else _get_redis_global()

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}{session_id}"

    async def save_token_map(
        self,
        session_id: str,
        token_map: dict[str, PIIToken],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Persist a token map for cross-request restore."""
        if not token_map:
            return
        payload = {token: asdict(t) for token, t in token_map.items()}
        await self._redis_client().set(
            self._key(session_id), json.dumps(payload), ex=ttl_seconds
        )

    async def load_token_map(self, session_id: str) -> dict[str, PIIToken]:
        raw = await self._redis_client().get(self._key(session_id))
        if not raw:
            return {}
        decoded = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        return {
            token: PIIToken(
                token=t["token"], original=t["original"], pii_type=t["pii_type"]
            )
            for token, t in decoded.items()
        }

    async def delete_token_map(self, session_id: str) -> None:
        await self._redis_client().delete(self._key(session_id))


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "PII_PATTERNS",
    "PIIService",
    "PIIToken",
    "REDIS_KEY_PREFIX",
]
