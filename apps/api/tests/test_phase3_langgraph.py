"""Phase 3 — LangGraph HITL primitives, checkpoints, dialog escalations."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from agents.langgraph.dialog_graph import route_by_confidence
from models.help_request import HelpRequest
from models.user import UserRole


def test_route_by_confidence_buckets() -> None:
    assert route_by_confidence(0.9) == "execute"
    assert route_by_confidence(0.65) == "clarify"
    assert route_by_confidence(0.2) == "escalate"


async def _register_builder(client: AsyncClient) -> tuple[dict[str, Any], str]:
    body = {
        "email": f"phase3-{uuid.uuid4().hex[:8]}@test.io",
        "password": "supersecret123",
        "full_name": "Phase3",
        "role": UserRole.BUILDER.value,
    }
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    ws_id = data["user"]["workspaces"][0]["workspace_id"]
    return data, ws_id


@pytest.mark.asyncio
async def test_checkpoint_unknown_thread_returns_404(client: AsyncClient) -> None:
    reg, _ws = await _register_builder(client)
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}
    r = await client.get(
        f"/api/v1/workflows/checkpoint-state/{uuid.uuid4()}",
        headers=hdr,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dialog_escalation_creates_help_request(client: AsyncClient, db_session) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    sid = uuid.uuid4().hex
    low_msgs = ["vague stuff", "still unclear hmm", "nope still vague", "last vague"]
    for msg in low_msgs:
        resp = await client.post(
            f"/api/v1/dialog/sessions/{sid}/turn",
            headers=hdr,
            json={"message": msg, "workspace_id": ws_id},
        )
        assert resp.status_code == 200, resp.text

    hrs = (
        (
            await db_session.execute(select(HelpRequest).where(HelpRequest.session_id == sid))
        )
        .scalars()
        .all()
    )
    await db_session.commit()
    assert len(hrs) >= 1


@pytest.mark.asyncio
async def test_pending_hitl_list_empty_ok(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}
    r = await client.get(
        f"/api/v1/workflows/pending-hitl?workspace_id={ws_id}",
        headers=hdr,
    )
    assert r.status_code == 200
    assert r.json().get("items") == []
