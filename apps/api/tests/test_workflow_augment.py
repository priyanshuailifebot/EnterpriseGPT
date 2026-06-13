"""Augment endpoint + interpreter unit tests.

The interpreter's actual LLM call is mocked at the service-bound
``WorkflowInterpreter`` instance so we exercise the full HTTP path
without burning Azure OpenAI tokens. The diff helper is tested
independently against in-memory definitions.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import AsyncClient

from models.user import UserRole
from schemas.workflow import (
    ActionNode,
    AgentNode,
    MemoryNode,
    TriggerNode,
    WorkflowDefinition,
)
from services.workflow_interpreter import diff_definitions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_builder(client: AsyncClient, suffix: str = "aug") -> tuple[str, UUID]:
    body = {
        "email": f"wf-{suffix}@test.io",
        "password": "supersecret123",
        "full_name": "WF Aug",
        "role": UserRole.BUILDER.value,
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    return d["access_token"], UUID(d["user"]["workspaces"][0]["workspace_id"])


def _starter_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Lead Triage",
        description="Score and route new leads.",
        nodes=[
            TriggerNode(
                id="trigger",
                name="New Lead",
                trigger_type="webhook",
                slug="new-lead",
            ),
            AgentNode(
                id="scorer",
                name="Score Lead",
                depends_on=["trigger"],
                role="Lead scoring agent",
                instructions="Rate the lead from 1-10.",
            ),
        ],
    )


async def _create_workflow(
    client: AsyncClient, token: str, ws: UUID, defn: WorkflowDefinition
) -> UUID:
    r = await client.post(
        "/api/v1/workflows/",
        json={
            "workspace_id": str(ws),
            "definition": json.loads(defn.model_dump_json()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


# ---------------------------------------------------------------------------
# diff_definitions — pure helper
# ---------------------------------------------------------------------------


def test_diff_detects_added_node() -> None:
    before = _starter_definition()
    after = before.model_copy(deep=True)
    after.nodes = list(after.nodes) + [
        ActionNode(
            id="notify_slack",
            name="Notify Slack",
            depends_on=["scorer"],
            provider="slack",
            action_slug="slack_send_message",
            params={"channel": "#sales"},
        )
    ]
    changes = diff_definitions(before=before, after=after)
    assert any("added" in c and "notify_slack" in c for c in changes)
    assert not any("removed" in c for c in changes)


def test_diff_detects_removed_node() -> None:
    before = _starter_definition()
    after = before.model_copy(deep=True)
    after.nodes = [n for n in after.nodes if n.id != "scorer"]
    # Remove the dangling reference too so validation passes for the copy.
    changes = diff_definitions(before=before, after=after)
    assert any("removed" in c and "scorer" in c for c in changes)


def test_diff_detects_modified_node() -> None:
    before = _starter_definition()
    after = before.model_copy(deep=True)
    # Mutate the agent's instructions.
    for n in after.nodes:
        if n.id == "scorer" and isinstance(n, AgentNode):
            n.instructions = "Rate from 1-10 and explain in one sentence."
    changes = diff_definitions(before=before, after=after)
    assert any("modified" in c and "scorer" in c for c in changes)


def test_diff_no_changes_returns_empty() -> None:
    before = _starter_definition()
    after = before.model_copy(deep=True)
    assert diff_definitions(before=before, after=after) == []


# ---------------------------------------------------------------------------
# POST /augment — endpoint round-trip with mocked interpreter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_augment_endpoint_returns_proposed_definition(
    client: AsyncClient,
) -> None:
    token, ws = await _register_builder(client, "endpoint")
    starter = _starter_definition()
    wf_id = await _create_workflow(client, token, ws, starter)

    # The "augmented" definition the LLM (mocked) is supposed to produce:
    # adds a Slack notification step after the scorer.
    augmented = starter.model_copy(deep=True)
    augmented.nodes = list(augmented.nodes) + [
        ActionNode(
            id="notify_slack",
            name="Notify Slack",
            depends_on=["scorer"],
            provider="slack",
            action_slug="slack_send_message",
            params={"channel": "#sales", "text": "New lead scored"},
        )
    ]

    mock_augment = AsyncMock(return_value=augmented)
    # Patch the interpreter on the service singleton actually wired into
    # the FastAPI dependency graph.
    from main import app  # noqa: WPS433

    # The dep returns a fresh service each request; the cleanest hook is to
    # patch the augment method on every WorkflowInterpreter instance.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.workflow_interpreter.WorkflowInterpreter.augment",
            mock_augment,
        )
        r = await client.post(
            f"/api/v1/workflows/{wf_id}/augment",
            json={
                "message": "Add a Slack notification after scoring",
                "current_definition": json.loads(starter.model_dump_json()),
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200, r.text
    payload = r.json()
    assert "proposed_definition" in payload
    assert "changes" in payload

    # Round-trip: the proposed definition validates against the schema.
    proposed = WorkflowDefinition.model_validate(payload["proposed_definition"])
    proposed_ids = {n.id for n in proposed.iter_nodes()}
    assert "notify_slack" in proposed_ids
    assert "scorer" in proposed_ids  # stable id preserved

    # Mock was called with the expected current_definition.
    assert mock_augment.await_count == 1
    call_kwargs = mock_augment.await_args.kwargs
    assert call_kwargs["user_message"] == "Add a Slack notification after scoring"
    assert call_kwargs["current_definition"].name == starter.name

    # Diff surface: at least one "added" entry for the new Slack node.
    assert any("added" in c for c in payload["changes"])


@pytest.mark.asyncio
async def test_augment_endpoint_404_when_workflow_missing(
    client: AsyncClient,
) -> None:
    token, _ws = await _register_builder(client, "missing")
    fake_wf = UUID("00000000-0000-0000-0000-000000000000")
    starter = _starter_definition()
    r = await client.post(
        f"/api/v1/workflows/{fake_wf}/augment",
        json={
            "message": "add a slack step",
            "current_definition": json.loads(starter.model_dump_json()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_augment_endpoint_rejects_invalid_body(client: AsyncClient) -> None:
    token, ws = await _register_builder(client, "invalid")
    starter = _starter_definition()
    wf_id = await _create_workflow(client, token, ws, starter)

    # Missing required ``message`` field.
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/augment",
        json={
            "current_definition": json.loads(starter.model_dump_json()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_augment_endpoint_503_when_interpreter_fails(
    client: AsyncClient,
) -> None:
    """Interpreter raising ``WorkflowInterpretationError`` surfaces as 503."""
    token, ws = await _register_builder(client, "fail")
    starter = _starter_definition()
    wf_id = await _create_workflow(client, token, ws, starter)

    from services.workflow_interpreter import WorkflowInterpretationError

    async def _boom(*_a: Any, **_kw: Any) -> WorkflowDefinition:
        raise WorkflowInterpretationError("LLM unavailable")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.workflow_interpreter.WorkflowInterpreter.augment",
            _boom,
        )
        r = await client.post(
            f"/api/v1/workflows/{wf_id}/augment",
            json={
                "message": "do something",
                "current_definition": json.loads(starter.model_dump_json()),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 503
    assert "LLM unavailable" in r.text


# ---------------------------------------------------------------------------
# Interpreter.augment unit test — preserves stable ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interpreter_augment_preserves_stable_ids(monkeypatch) -> None:
    """``augment`` returns whatever JSON the LLM emits, validated.

    We assert the contract: when the model echoes a graph that keeps the
    original ids, the returned definition contains them.
    """
    from services.workflow_interpreter import WorkflowInterpreter
    from core.config import get_settings

    starter = _starter_definition()
    augmented = starter.model_copy(deep=True)
    augmented.nodes = list(augmented.nodes) + [
        ActionNode(
            id="notify_slack",
            name="Notify Slack",
            depends_on=["scorer"],
            provider="slack",
            action_slug="slack_send_message",
            params={},
        )
    ]
    raw_json = augmented.model_dump_json()

    async def _fake_call_llm(self, *, messages):  # type: ignore[no-untyped-def]
        return raw_json

    monkeypatch.setattr(WorkflowInterpreter, "_call_llm", _fake_call_llm)

    interp = WorkflowInterpreter(get_settings())
    result = await interp.augment(
        current_definition=starter,
        user_message="Add a slack notification after scoring",
        available_tools=["slack_send_message"],
    )
    ids = {n.id for n in result.iter_nodes()}
    assert "trigger" in ids and "scorer" in ids and "notify_slack" in ids
