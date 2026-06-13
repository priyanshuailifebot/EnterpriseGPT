"""Publish-gate: a draft never performs real side effects; publishing gates on
a passing test run; editing reverts to draft.

The action-layer enforcement (``invoke_action(live=False)`` previews
side-effecting actions) is unit-tested directly; the lifecycle (publish
requires a completed run, edit reverts) is tested through the API.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from agents.action_runner import _is_side_effecting, invoke_action
from models.user import UserRole
from schemas.workflow import (
    ActionNode,
    AgentNode,
    TriggerNode,
    WorkflowDefinition,
)


# ---------------------------------------------------------------------------
# Action-layer enforcement (the guarantee)
# ---------------------------------------------------------------------------


def test_side_effect_classification() -> None:
    assert _is_side_effecting("gmail", "send_email") is True
    assert _is_side_effecting("slack", "slack_send_message") is True
    assert _is_side_effecting("googlesheets", "write_range") is True
    assert _is_side_effecting("googlesheets", "read_range") is False
    assert _is_side_effecting("gmail", "fetch_emails") is False
    assert _is_side_effecting("pdf_generator", "create_pdf") is False


@pytest.mark.asyncio
async def test_invoke_action_previews_side_effect_when_not_live() -> None:
    result = await invoke_action(
        provider_id="gmail",
        action_slug="send_email",
        params={"to": "x@y.com", "subject": "Hi", "body": "Hello"},
        workspace_connections=[],
        live=False,
    )
    assert result["__dry_run__"] is True
    assert result["__preview__"] is True
    assert result["__blocked_reason__"] == "workflow_not_published"
    # The preview echoes the composed email so the UI can show it.
    assert result["data"]["to"] == "x@y.com"
    assert result["data"]["subject"] == "Hi"


@pytest.mark.asyncio
async def test_invoke_action_allows_readonly_when_not_live() -> None:
    # A read action is not gated even on a draft (so previews use real reads
    # where a connection exists). With no connection it dry-runs anyway, but it
    # is NOT blocked by the publish gate.
    result = await invoke_action(
        provider_id="googlesheets",
        action_slug="read_range",
        params={"range": "A1:B2"},
        workspace_connections=[],
        live=False,
    )
    assert result.get("__blocked_reason__") != "workflow_not_published"


# ---------------------------------------------------------------------------
# Lifecycle through the API
# ---------------------------------------------------------------------------


async def _register(client: AsyncClient, suffix: str) -> str:
    body = {
        "email": f"pub-{suffix}@test.io",
        "password": "supersecret123",
        "full_name": "Pub",
        "role": UserRole.BUILDER.value,
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    return d["access_token"], UUID(d["user"]["workspaces"][0]["workspace_id"])


def _defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Pub WF",
        nodes=[
            TriggerNode(id="t", name="Trigger", trigger_type="manual"),
            AgentNode(id="a", name="Agent", depends_on=["t"], role="x"),
            ActionNode(
                id="send", name="Send", depends_on=["a"],
                provider="gmail", action_slug="send_email",
                params={"to": "x@y.com", "subject": "S", "body": "B"},
            ),
        ],
    )


async def _create(client, token, ws) -> str:
    r = await client.post(
        "/api/v1/workflows/",
        json={"workspace_id": str(ws), "definition": json.loads(_defn().model_dump_json())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _run_demo(client, token, wf_id) -> None:
    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/execute",
        json={"input_data": {}, "demo": True},
        headers={"Authorization": f"Bearer {token}"}, timeout=60.0,
    ) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass


@pytest.mark.asyncio
async def test_new_workflow_starts_as_draft(client: AsyncClient) -> None:
    token, ws = await _register(client, "draft")
    wf_id = await _create(client, token, ws)
    r = await client.get(
        f"/api/v1/workflows/{wf_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.json()["workflow"]["status"] == "draft"


@pytest.mark.asyncio
async def test_publish_requires_a_completed_run(client: AsyncClient) -> None:
    token, ws = await _register(client, "gate")
    wf_id = await _create(client, token, ws)
    hdr = {"Authorization": f"Bearer {token}"}

    # No run yet → publish blocked.
    r = await client.post(f"/api/v1/workflows/{wf_id}/publish", headers=hdr)
    assert r.status_code == 409, r.text

    # A demo run on the current version validates it.
    await _run_demo(client, token, wf_id)
    r = await client.post(f"/api/v1/workflows/{wf_id}/publish", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert r.json()["published_at"] is not None


@pytest.mark.asyncio
async def test_editing_reverts_published_to_draft(client: AsyncClient) -> None:
    token, ws = await _register(client, "revert")
    wf_id = await _create(client, token, ws)
    hdr = {"Authorization": f"Bearer {token}"}
    await _run_demo(client, token, wf_id)
    r = await client.post(f"/api/v1/workflows/{wf_id}/publish", headers=hdr)
    assert r.json()["status"] == "published"

    # Edit (new version) → back to draft.
    upd = await client.put(
        f"/api/v1/workflows/{wf_id}",
        json={"definition": json.loads(_defn().model_dump_json()), "change_note": "tweak"},
        headers=hdr,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["status"] == "draft"
    assert upd.json()["published_at"] is None


@pytest.mark.asyncio
async def test_unpublish_returns_to_draft(client: AsyncClient) -> None:
    token, ws = await _register(client, "unpub")
    wf_id = await _create(client, token, ws)
    hdr = {"Authorization": f"Bearer {token}"}
    await _run_demo(client, token, wf_id)
    await client.post(f"/api/v1/workflows/{wf_id}/publish", headers=hdr)
    r = await client.post(f"/api/v1/workflows/{wf_id}/unpublish", headers=hdr)
    assert r.status_code == 200
    assert r.json()["status"] == "draft"
