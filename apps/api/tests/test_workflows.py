"""Phase 2 — workflow persistence, NL interpret (mocked), SSE, HITL."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.user import UserRole
from models.workflow_execution import WorkflowExecution
from models.workflow_version import WorkflowVersion
from schemas.workflow import AgentDefinition, WorkflowDefinition
from services.workflow_service import WorkflowService


async def _register_builder(client: AsyncClient) -> tuple[dict[str, Any], UUID]:
    body = {
        "email": "wf-builder@test.io",
        "password": "supersecret123",
        "full_name": "WF Builder",
        "role": UserRole.BUILDER.value,
    }
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    ws_id = UUID(data["user"]["workspaces"][0]["workspace_id"])
    return data, ws_id


def _sample_definition(
    *,
    with_hitl: bool = False,
) -> WorkflowDefinition:
    agents = [
        AgentDefinition(
            id="collector",
            name="Collector Agent",
            role="Gather requirements",
            instructions="Summarize the objective.",
            tools=[],
            depends_on=[],
        ),
        AgentDefinition(
            id="executor",
            name="Executor Agent",
            role="Execute work",
            instructions="Produce the deliverable.",
            tools=[],
            depends_on=["collector"],
        ),
    ]
    return WorkflowDefinition(
        name="Test Workflow",
        description="Synthetic definition for tests",
        trigger="manual",
        agents=agents,
        human_checkpoints=(["collector"] if with_hitl else []),
        output_format="text",
    )


@pytest.mark.asyncio
async def test_interpret_returns_workflow_definition(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    fake_def = _sample_definition()
    fake_def = fake_def.model_copy(update={"name": "LLM-derived"})

    async def mock_interpret(
        *_args: Any, user_input: str, **_kwargs: Any
    ) -> WorkflowDefinition:
        assert "user-input" in user_input
        return fake_def

    with patch(
        "services.workflow_interpreter.WorkflowInterpreter.interpret",
        new=mock_interpret,
    ):
        resp = await client.post(
            "/api/v1/workflows/interpret",
            headers=hdr,
            json={
                "text": "user-input-plan something",
                "workspace_id": str(ws_id),
                "skip_clarification": True,
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ready"
    assert data["definition"]["name"] == "LLM-derived"
    assert len(data["definition"]["agents"]) == 2


@pytest.mark.asyncio
async def test_create_then_list_workflows(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    definition = _sample_definition()
    hdr = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/v1/workflows/",
        headers=hdr,
        json={
            "workspace_id": str(ws_id),
            "definition": definition.model_dump(),
        },
    )
    assert resp.status_code == 201, resp.text
    wf = resp.json()
    assert wf["current_version"] == 1

    lst = await client.get("/api/v1/workflows/", headers=hdr)
    assert lst.status_code == 200
    body = lst.json()
    assert body["total"] >= 1


@pytest.mark.asyncio
async def test_update_workflow_increments_version(client: AsyncClient, db_session) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}

    definition = _sample_definition()
    created = await client.post(
        "/api/v1/workflows/",
        headers=hdr,
        json={"workspace_id": str(ws_id), "definition": definition.model_dump()},
    )
    wf_id = created.json()["id"]

    modified = definition.model_copy(
        update={
            "name": "Renamed WF",
            "agents": [
                definition.agents[0].model_copy(),
                AgentDefinition(
                    id="executor",
                    name="Exec",
                    role="Exec",
                    instructions="Go",
                    tools=[],
                    depends_on=["collector"],
                ),
            ],
        }
    )
    resp = await client.put(
        f"/api/v1/workflows/{wf_id}",
        headers=hdr,
        json={"definition": modified.model_dump()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_version"] == 2

    vers = (
        await db_session.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == UUID(wf_id))
        )
    ).scalars().all()
    await db_session.commit()
    assert sorted(v.version for v in vers) == [1, 2]


@pytest.mark.asyncio
async def test_execute_sse_minimal_events_mock_stream(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}

    definition = _sample_definition()
    created = await client.post(
        "/api/v1/workflows/",
        headers=hdr,
        json={"workspace_id": str(ws_id), "definition": definition.model_dump()},
    )
    wf_id = created.json()["id"]

    async def fake_stream(*args: Any, **kwargs: Any):
        yield {"type": "workflow_start", "workflow_name": "x"}
        yield {"type": "agent_start", "agent_id": "a1", "agent_name": "A"}
        yield {
            "type": "workflow_complete",
            "success": True,
            "result": {"a1": {"output": {"content": "done"}}},
        }

    with patch(
        "agents.dynamiq_service.DynamiqService.run_workflow_stream",
        new=fake_stream,
    ):
        async with client.stream(
            "POST",
            f"/api/v1/workflows/{wf_id}/execute",
            headers=hdr,
            json={"input_data": {"input": "run it"}},
            timeout=30.0,
        ) as resp:
            assert resp.status_code == 200, resp.text
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "workflow_complete" in buf:
                    break
            types: list[str] = []
            for line in buf.split("\n"):
                if line.startswith("data:"):
                    with suppress(json.JSONDecodeError):
                        types.append(json.loads(line[5:].strip()).get("type"))
            assert "workflow_start" in types
            assert "agent_start" in types
            assert "workflow_complete" in types


@pytest.mark.asyncio
async def test_hitl_stream_emits_checkpoint_then_completes(
    client: AsyncClient,
    db_session,
):
    """HITL SSE path: checkpoint event, unblock poll, Dynamiq mocked.

    We mock ``WorkflowService._poll_approval`` instead of POSTing ``/approve`` while
    the execute stream stays open — httpx + ASGITransport can deadlock otherwise.
    Direct Redis exercised by polling implementation in production and can be covered
    in a subprocess integration test."""

    async def collector_stream(*args: Any, **kwargs: Any):
        yield {"type": "workflow_start", "workflow_name": "collector"}
        yield {
            "type": "workflow_complete",
            "success": True,
            "result": {"collector": {"output": {"content": "gathered"}}},
        }

    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}

    definition = _sample_definition(with_hitl=True)
    wf_id = (
        await client.post(
            "/api/v1/workflows/",
            headers=hdr,
            json={"workspace_id": str(ws_id), "definition": definition.model_dump()},
        )
    ).json()["id"]

    mock_wf = MagicMock(name="mock_workflow")

    async def stream_runner(*args: Any, **kwargs: Any):
        async for evt in collector_stream():
            yield evt

    with (
        patch(
            "agents.dynamiq_service.DynamiqService.hydrate_agent_stage",
            return_value=mock_wf,
        ),
        patch(
            "agents.dynamiq_service.DynamiqService.run_workflow_stream",
            new=stream_runner,
        ),
        patch.object(
            WorkflowService,
            "_poll_approval",
            new=AsyncMock(return_value={"approved": True, "feedback": None}),
        ),
    ):
        async with client.stream(
            "POST",
            f"/api/v1/workflows/{wf_id}/execute",
            headers=hdr,
            json={"input_data": {"input": "go"}},
            timeout=60.0,
        ) as resp:
            assert resp.status_code == 200, resp.text
            saw_checkpoint = False
            saw_terminal = False
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                ptype = payload.get("type")
                if ptype == "hitl_required":
                    saw_checkpoint = True
                if ptype == "workflow_complete" and payload.get("success"):
                    saw_terminal = True
                    break
            assert saw_checkpoint is True
            assert saw_terminal is True

    ex_rows = (
        (
            await db_session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_id == UUID(wf_id))
            )
        )
        .scalars()
        .all()
    )
    assert ex_rows[-1].status.value == "completed"
