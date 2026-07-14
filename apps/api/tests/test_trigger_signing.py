"""P7′ — signed trigger context + internal.sign_link action."""

from __future__ import annotations

import pytest

from agents.action_runner import invoke_action
from core.security import sign_trigger_context, verify_trigger_context


def test_sign_verify_round_trip() -> None:
    ctx = {"candidate_id": "c-123", "purpose": "slot"}
    token = sign_trigger_context(ctx)
    assert verify_trigger_context(token) == ctx


def test_verify_rejects_garbage() -> None:
    assert verify_trigger_context("not-a-token") is None


def test_verify_rejects_tamper() -> None:
    token = sign_trigger_context({"candidate_id": "c-1"})
    # Flip a character in the signature segment.
    tampered = token[:-3] + ("aaa" if token[-3:] != "aaa" else "bbb")
    assert verify_trigger_context(tampered) is None


def test_verify_rejects_expired() -> None:
    # Mint an already-expired token directly (sign_trigger_context clamps TTL to
    # >=1s, so we bypass it to exercise verify's expiry handling).
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    from core.config import get_settings
    from core.security import ALGORITHM

    token = jwt.encode(
        {
            "typ": "trigger_ctx",
            "ctx": {"candidate_id": "c-1"},
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        get_settings().SECRET_KEY,
        algorithm=ALGORITHM,
    )
    assert verify_trigger_context(token) is None


@pytest.mark.asyncio
async def test_internal_sign_link_action() -> None:
    result = await invoke_action(
        provider_id="internal",
        action_slug="sign_link",
        params={
            "context": {"candidate_id": "c-9", "purpose": "approve"},
            "path": "/api/v1/workflows/W/link/hr-approve",
        },
        workspace_connections=[],
    )
    url = result["data"]["url"]
    token = result["data"]["token"]
    assert "ctx=" in url
    assert token in url
    assert verify_trigger_context(token) == {"candidate_id": "c-9", "purpose": "approve"}
