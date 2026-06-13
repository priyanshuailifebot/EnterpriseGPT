"""Tests for ``services.pii_service.PIIService``."""

from __future__ import annotations

import re

import pytest

from services.pii_service import (
    DEFAULT_TTL_SECONDS,
    PIIService,
    REDIS_KEY_PREFIX,
)

TOKEN_RE = re.compile(r"<<PII_(?P<type>\w+)_(?P<hex>[0-9a-f]{6})>>")


# ---------------------------------------------------------------------------
# redact / restore
# ---------------------------------------------------------------------------


def test_redact_phone_round_trip() -> None:
    text = "Call me at 555-123-4567 anytime."
    svc = PIIService()
    redacted, token_map = svc.redact(text)
    assert "555-123-4567" not in redacted
    assert TOKEN_RE.search(redacted)
    restored = svc.restore(redacted, token_map)
    assert restored == text


def test_redact_email() -> None:
    redacted, token_map = PIIService().redact("Email: john@example.com — thanks")
    assert "john@example.com" not in redacted
    assert any(t.pii_type == "EMAIL" for t in token_map.values())


def test_redact_ssn_credit_card_ip() -> None:
    text = "SSN 123-45-6789, card 4111-1111-1111-1111, ip 192.168.1.42"
    redacted, token_map = PIIService().redact(text)

    types = {t.pii_type for t in token_map.values()}
    assert "SSN" in types
    assert "CREDIT_CARD" in types
    assert "IP_ADDRESS" in types
    assert "123-45-6789" not in redacted
    assert "4111-1111-1111-1111" not in redacted
    assert "192.168.1.42" not in redacted


def test_redact_multiple_pii_simultaneously() -> None:
    text = (
        "Hi, my email is jane.doe+test@acme.io and phone is +1 (555) 234-5678. "
        "Card on file 4242 4242 4242 4242. IP 10.0.0.1."
    )
    redacted, token_map = PIIService().redact(text)

    types = sorted({t.pii_type for t in token_map.values()})
    assert types == ["CREDIT_CARD", "EMAIL", "IP_ADDRESS", "PHONE"]
    # restore is exact
    assert PIIService().restore(redacted, token_map) == text


def test_redact_text_with_no_pii_returns_unchanged() -> None:
    text = "Just some boring prose."
    redacted, token_map = PIIService().redact(text)
    assert redacted == text
    assert token_map == {}


def test_redact_does_not_double_match_existing_tokens() -> None:
    """Two calls must not regress: tokens from the first round must not be
    matched as PII by the second round."""
    redacted_1, _ = PIIService().redact("a@b.co and c@d.io")
    redacted_2, token_map_2 = PIIService().redact(redacted_1)
    assert redacted_2 == redacted_1
    assert token_map_2 == {}


# ---------------------------------------------------------------------------
# Redis persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_token_map() -> None:
    svc = PIIService()
    text = "Call me at 555-123-4567 about jane@acme.io"
    _, token_map = svc.redact(text)

    session_id = "test-session-1"
    await svc.save_token_map(session_id, token_map)
    loaded = await svc.load_token_map(session_id)

    assert loaded == token_map


@pytest.mark.asyncio
async def test_save_token_map_sets_ttl() -> None:
    from core.redis import get_redis

    svc = PIIService()
    _, token_map = svc.redact("ping ops@example.com")
    session_id = "ttl-session"
    await svc.save_token_map(session_id, token_map, ttl_seconds=DEFAULT_TTL_SECONDS)

    redis = get_redis()
    ttl = await redis.ttl(f"{REDIS_KEY_PREFIX}{session_id}")
    assert 3000 < ttl <= DEFAULT_TTL_SECONDS


@pytest.mark.asyncio
async def test_load_missing_session_returns_empty() -> None:
    svc = PIIService()
    loaded = await svc.load_token_map("nope")
    assert loaded == {}


@pytest.mark.asyncio
async def test_delete_token_map() -> None:
    svc = PIIService()
    _, token_map = svc.redact("hello@x.io")
    sid = "to-delete"
    await svc.save_token_map(sid, token_map)
    await svc.delete_token_map(sid)
    assert await svc.load_token_map(sid) == {}
