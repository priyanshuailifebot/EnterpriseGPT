"""Tier 1 test-run inspection: node_complete persistence + /steps + readiness.

Covers the three things the inspect drawer relies on:
  * Demo runs persist a ``demo=True`` execution + step rows, but are excluded
    from the default executions listing.
  * The native (pure-agent) path synthesizes node_complete from agent_complete
    so step rows exist there too.
  * ``GET /executions/{id}/steps`` returns ordered per-node records and is
    gated to workspace members.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.user import UserRole
from models.workflow_execution import WorkflowExecution
from models.workflow_execution_step import WorkflowExecutionStep
from schemas.workflow import (
    ActionNode,
    AgentDefinition,
    AgentNode,
    TriggerNode,
    WorkflowDefinition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register(client: AsyncClient, suffix: str) -> tuple[str, UUID]:
    body = {
        "email": f"steps-{suffix}@test.io",
        "password": "supersecret123",
        "full_name": "Steps",
        "role": UserRole.BUILDER.value,
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    return d["access_token"], UUID(d["user"]["workspaces"][0]["workspace_id"])


async def _create(
    client: AsyncClient, token: str, ws: UUID, defn: WorkflowDefinition
) -> UUID:
    r = await client.post(
        "/api/v1/workflows/",
        json={"workspace_id": str(ws), "definition": json.loads(defn.model_dump_json())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


def _v2_defn() -> WorkflowDefinition:
    """Trigger → agent → unconnected action (so the action dry-runs)."""
    return WorkflowDefinition(
        name="Steps Demo",
        nodes=[
            TriggerNode(id="trig", name="Trigger", trigger_type="manual"),
            AgentNode(id="agent", name="Agent", depends_on=["trig"], role="helper"),
            ActionNode(
                id="notify",
                name="Notify",
                depends_on=["agent"],
                provider="slack",
                action_slug="slack_send_message",
                params={"channel": "#x"},
            ),
        ],
    )


def _agents_defn() -> WorkflowDefinition:
    """Legacy pure-agent definition — exercises the native Dynamiq path."""
    return WorkflowDefinition(
        name="Native",
        trigger="manual",
        agents=[
            AgentDefinition(id="collector", name="Collector", role="gather", tools=[]),
        ],
        output_format="text",
    )


async def _stream_events(
    client: AsyncClient, wf_id: UUID, token: str, body: dict[str, Any]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        f"/api/v1/workflows/{wf_id}/execute",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            with suppress(json.JSONDecodeError):
                events.append(json.loads(line[5:].strip()))
    return events


# ---------------------------------------------------------------------------
# Demo persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_run_persists_steps_excluded_from_executions(
    client: AsyncClient, db_session
) -> None:
    token, ws = await _register(client, "demo")
    wf_id = await _create(client, token, ws, _v2_defn())

    events = await _stream_events(
        client, wf_id, token, {"input_data": {"foo": "bar"}, "demo": True}
    )

    # readiness verdict rides the terminal event and flags the dry-run action.
    complete = next(e for e in events if e["type"] == "workflow_complete")
    assert complete["readiness"]["ready"] is False
    reasons = {i["reason"] for i in complete["readiness"]["issues"]}
    assert "action_not_connected" in reasons

    # A demo=True execution row + step rows were persisted.
    ex_rows = (
        (
            await db_session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.workflow_id == wf_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ex_rows) == 1
    assert ex_rows[0].demo is True
    assert ex_rows[0].status.value == "completed"

    steps = (
        (
            await db_session.execute(
                select(WorkflowExecutionStep).where(
                    WorkflowExecutionStep.execution_id == ex_rows[0].id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {s.node_id for s in steps} == {"trig", "agent", "notify"}
    assert all(s.demo is True for s in steps)

    # Demo rows are excluded from the default executions listing.
    listed = await client.get(
        f"/api/v1/workflows/{wf_id}/executions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 0
    assert listed.json()["items"] == []


@pytest.mark.asyncio
async def test_steps_endpoint_returns_ordered_records(
    client: AsyncClient, db_session
) -> None:
    token, ws = await _register(client, "list")
    wf_id = await _create(client, token, ws, _v2_defn())
    await _stream_events(client, wf_id, token, {"input_data": {}, "demo": True})

    ex_id = (
        (
            await db_session.execute(
                select(WorkflowExecution.id).where(
                    WorkflowExecution.workflow_id == wf_id
                )
            )
        )
        .scalars()
        .one()
    )

    resp = await client.get(
        f"/api/v1/workflows/{wf_id}/executions/{ex_id}/steps",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    # Ordered by step_index, snapshots present, action flagged dry-run.
    assert [s["step_index"] for s in items] == sorted(s["step_index"] for s in items)
    assert all(isinstance(s["input_snapshot"], dict) for s in items)
    assert all(isinstance(s["output_snapshot"], dict) for s in items)
    notify = next(s for s in items if s["node_id"] == "notify")
    assert notify["dry_run"] is True


# ---------------------------------------------------------------------------
# Native path synthesis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_path_synthesizes_and_persists_node_complete(
    client: AsyncClient, db_session
) -> None:
    token, ws = await _register(client, "native")
    wf_id = await _create(client, token, ws, _agents_defn())

    async def fake_stream(*args: Any, **kwargs: Any):
        yield {"type": "workflow_start", "workflow_name": "x"}
        yield {
            "type": "agent_complete",
            "agent_id": "collector",
            "agent_name": "Collector",
            "content": "gathered",
        }
        yield {
            "type": "workflow_complete",
            "success": True,
            "result": {"collector": {"output": {"content": "gathered"}}},
        }

    with patch(
        "agents.dynamiq_service.DynamiqService.run_workflow_stream", new=fake_stream
    ):
        events = await _stream_events(client, wf_id, token, {"input_data": {"input": "go"}})

    types = [e["type"] for e in events]
    # node_complete synthesized from agent_complete on the native path.
    assert "node_complete" in types
    nc = next(e for e in events if e["type"] == "node_complete")
    assert nc["node_id"] == "collector"
    assert nc["node_kind"] == "agent"
    assert isinstance(nc["output_snapshot"], dict)
    # Original agent_complete is preserved (existing FE consumers).
    assert "agent_complete" in types
    # Terminal carries the readiness verdict.
    complete = next(e for e in events if e["type"] == "workflow_complete")
    assert "readiness" in complete

    # Real (non-demo) run shows up in the executions list and has step rows.
    ex_rows = (
        (
            await db_session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.workflow_id == wf_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ex_rows) == 1
    assert ex_rows[0].demo is False

    steps = (
        (
            await db_session.execute(
                select(WorkflowExecutionStep).where(
                    WorkflowExecutionStep.execution_id == ex_rows[0].id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {s.node_id for s in steps} == {"collector"}
    assert all(s.demo is False for s in steps)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steps_endpoint_404_for_non_member(
    client: AsyncClient, db_session
) -> None:
    token_a, ws_a = await _register(client, "owner")
    wf_id = await _create(client, token_a, ws_a, _v2_defn())
    await _stream_events(client, wf_id, token_a, {"input_data": {}, "demo": True})
    ex_id = (
        (
            await db_session.execute(
                select(WorkflowExecution.id).where(
                    WorkflowExecution.workflow_id == wf_id
                )
            )
        )
        .scalars()
        .one()
    )

    token_b, _ = await _register(client, "intruder")
    resp = await client.get(
        f"/api/v1/workflows/{wf_id}/executions/{ex_id}/steps",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404
