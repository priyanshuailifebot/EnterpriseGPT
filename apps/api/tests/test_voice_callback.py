"""P9 — voice route registration + Retell callback guards."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agents.action_runner import invoke_action
from core.redis import get_redis
from routers.voice import _route_key, retell_callback


@pytest.mark.asyncio
async def test_register_voice_route_writes_redis() -> None:
    ws = uuid4()
    out = await invoke_action(
        provider_id="internal",
        action_slug="register_voice_route",
        params={
            "call_id": "call_abc",
            "target_slug": "hr-scoring",
            "context": {"candidate_id": "c1"},
        },
        workspace_connections=[],
        workspace_id=ws,
        live=True,
    )
    assert out["data"]["registered"] is True
    raw = await get_redis().get(_route_key("call_abc"))
    rec = json.loads(raw if isinstance(raw, str) else raw.decode())
    assert rec["target_slug"] == "hr-scoring"
    assert rec["ctx"] == {"candidate_id": "c1"}
    assert rec["workspace_id"] == str(ws)
    await get_redis().delete(_route_key("call_abc"))


@pytest.mark.asyncio
async def test_register_voice_route_dry_run_when_not_live() -> None:
    out = await invoke_action(
        provider_id="internal",
        action_slug="register_voice_route",
        params={"call_id": "c", "target_slug": "s"},
        workspace_connections=[],
        workspace_id=uuid4(),
        live=False,
    )
    assert out.get("__dry_run__") is True


@pytest.mark.asyncio
async def test_register_voice_route_requires_fields() -> None:
    out = await invoke_action(
        provider_id="internal",
        action_slug="register_voice_route",
        params={"call_id": "c"},  # missing target_slug
        workspace_connections=[],
        workspace_id=uuid4(),
        live=True,
    )
    assert out.get("ok") is False


@pytest.mark.asyncio
async def test_callback_disabled_without_secret() -> None:
    # RETELL_WEBHOOK_SECRET defaults empty → endpoint disabled (503), no DB touch.
    with pytest.raises(HTTPException) as ei:
        await retell_callback(payload={"call_id": "x"}, x_retell_secret=None, db=None)  # type: ignore[arg-type]
    assert ei.value.status_code == 503
