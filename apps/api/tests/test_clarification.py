"""Clarification loop — mocked clarifier LLM, LangGraph checkpoint sessions."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from httpx import AsyncClient

from models.user import UserRole
from schemas.workflow import AgentDefinition, WorkflowDefinition
from services.clarification_service import SESSION_KEY_PREFIX

_CLARIFIER_PATCH = "agents.langgraph.clarification_graph.call_workflow_clarifier_async"

from tests.fixtures.clarifier_responses import (
    SPECIFIC_READY,
    STILL_AMBIGUOUS_ROUND_2,
    VAGUE_ROUND_1,
)


async def _register_builder(client: AsyncClient) -> tuple[dict[str, Any], UUID]:
    suffix = uuid.uuid4().hex[:10]
    body = {
        "email": f"clarify-builder-{suffix}@test.io",
        "password": "supersecret123",
        "full_name": "Clarify Builder",
        "role": UserRole.BUILDER.value,
    }
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    ws_id = UUID(data["user"]["workspaces"][0]["workspace_id"])
    return data, ws_id


def _minimal_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Clarified Workflow",
        description="test",
        trigger="manual",
        agents=[
            AgentDefinition(
                id="a1",
                name="Agent One",
                role="r",
                instructions="i",
                tools=[],
                depends_on=[],
            )
        ],
        human_checkpoints=[],
        output_format="text",
    )


@pytest.mark.asyncio
async def test_vague_prompt_triggers_clarification(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return dict(VAGUE_ROUND_1)

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        resp = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "automate my work", "workspace_id": str(ws_id)},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert data["session_id"]
    assert data["round_number"] == 1
    assert len(data["questions"]) >= 2


@pytest.mark.asyncio
async def test_specific_prompt_skips_clarification(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    fake_def = _minimal_definition()

    async def mock_interpret(
        *_args: Any, user_input: str, **_kwargs: Any
    ) -> WorkflowDefinition:
        return fake_def

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return dict(SPECIFIC_READY)

    with (
        patch(
            _CLARIFIER_PATCH,
            new=AsyncMock(side_effect=_fake_clarifier),
        ),
        patch(
            "services.workflow_interpreter.WorkflowInterpreter.interpret",
            new=mock_interpret,
        ),
    ):
        resp = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "text": "When a Zendesk ticket is created labeled billing, summarize it, "
                "then post to Slack #billing with human approval before send.",
                "workspace_id": str(ws_id),
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ready"
    assert data["rounds_used"] == 0
    assert data["definition"]["name"] == "Clarified Workflow"


@pytest.mark.asyncio
async def test_skip_clarification_flag(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    fake_def = _minimal_definition()
    clarifier_mock = AsyncMock(return_value=VAGUE_ROUND_1)

    async def mock_interpret(
        *_args: Any, **_kwargs: Any
    ) -> WorkflowDefinition:
        return fake_def

    with (
        patch(
            _CLARIFIER_PATCH,
            clarifier_mock,
        ),
        patch(
            "services.workflow_interpreter.WorkflowInterpreter.interpret",
            new=mock_interpret,
        ),
    ):
        resp = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "text": "automate my work",
                "workspace_id": str(ws_id),
                "skip_clarification": True,
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"
    clarifier_mock.assert_not_called()


@pytest.mark.asyncio
async def test_multi_round_clarification(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]

    calls: list[dict[str, Any]] = []

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        calls.append({})
        if len(calls) == 1:
            return dict(VAGUE_ROUND_1)
        return dict(STILL_AMBIGUOUS_ROUND_2)

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r1 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "automate my work", "workspace_id": str(ws_id)},
        )
    assert r1.status_code == 200
    sid = r1.json()["session_id"]
    q_ids = [q["id"] for q in r1.json()["questions"]]

    answers = [
        {"question_id": q_ids[0], "answer": "Manual"},
        {"question_id": q_ids[1], "answer": "PDF report"},
    ]

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r2 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"session_id": sid, "answers": answers},
        )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["status"] == "needs_clarification"
    assert body2["round_number"] == 2


@pytest.mark.asyncio
async def test_force_proceed(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    fake_def = _minimal_definition()

    async def mock_interpret(
        *_args: Any, user_input: str, **_kwargs: Any
    ) -> WorkflowDefinition:
        assert "Clarifications:" in user_input
        return fake_def

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return dict(VAGUE_ROUND_1)

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r1 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "automate my work", "workspace_id": str(ws_id)},
        )
    sid = r1.json()["session_id"]
    q_ids = [q["id"] for q in r1.json()["questions"]]
    answers = [
        {"question_id": q_ids[0], "answer": "Manual"},
        {"question_id": q_ids[1], "answer": "report"},
    ]

    with patch(
        "services.workflow_interpreter.WorkflowInterpreter.interpret",
        new=mock_interpret,
    ):
        r2 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "session_id": sid,
                "answers": answers,
                "force_proceed": True,
            },
        )
    assert r2.status_code == 200
    assert r2.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_max_rounds_enforced(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    fake_def = _minimal_definition()

    async def mock_interpret(
        *_args: Any, **_kwargs: Any
    ) -> WorkflowDefinition:
        return fake_def

    ambig_cycle = [dict(VAGUE_ROUND_1), dict(STILL_AMBIGUOUS_ROUND_2), dict(VAGUE_ROUND_1)]
    idx = {"n": 0}

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        i = idx["n"]
        idx["n"] += 1
        return ambig_cycle[min(i, len(ambig_cycle) - 1)]

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r1 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "do stuff", "workspace_id": str(ws_id)},
        )
    assert r1.status_code == 200
    sid = r1.json()["session_id"]

    def answer_latest(questions: list[dict[str, Any]]) -> list[dict[str, str]]:
        out = []
        for q in questions:
            if q["type"] == "text":
                out.append({"question_id": q["id"], "answer": "x"})
            else:
                opt = (q.get("options") or ["A"])[0]
                out.append({"question_id": q["id"], "answer": opt})
        return out

    body = r1.json()
    clar_patch_path = _CLARIFIER_PATCH
    for _ in range(2):
        ans = answer_latest(body["questions"])
        with patch(
            clar_patch_path,
            new=AsyncMock(side_effect=_fake_clarifier),
        ):
            resp = await client.post(
                "/api/v1/workflows/interpret",
                headers={"Authorization": f"Bearer {token}"},
                json={"session_id": sid, "answers": ans},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "needs_clarification"

    ans3 = answer_latest(body["questions"])
    with patch(
        "services.workflow_interpreter.WorkflowInterpreter.interpret",
        new=mock_interpret,
    ):
        r_final = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"session_id": sid, "answers": ans3},
        )
    assert r_final.status_code == 200
    final = r_final.json()
    assert final["status"] == "ready"
    assert final["rounds_used"] >= 3


@pytest.mark.asyncio
async def test_session_expiry(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    resp = await client.post(
        "/api/v1/workflows/interpret",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "session_id": "deadbeef_deadbeef_deadbeef_deadbeef",
            "answers": [{"question_id": "x", "answer": "y"}],
            "force_proceed": True,
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_augmented_prompt_includes_qa(client: AsyncClient) -> None:
    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]
    fake_def = _minimal_definition()
    captured: dict[str, str] = {}

    async def mock_interpret(
        *_args: Any, user_input: str, **_kwargs: Any
    ) -> WorkflowDefinition:
        captured["prompt"] = user_input
        return fake_def

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return dict(VAGUE_ROUND_1)

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r1 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "original vague ask", "workspace_id": str(ws_id)},
        )
    sid = r1.json()["session_id"]
    q = r1.json()["questions"]

    with patch(
        "services.workflow_interpreter.WorkflowInterpreter.interpret",
        new=mock_interpret,
    ):
        r2 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "session_id": sid,
                "answers": [
                    {"question_id": q[0]["id"], "answer": "Manual"},
                    {"question_id": q[1]["id"], "answer": "Slack summary"},
                ],
                "force_proceed": True,
            },
        )
    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "ready"
    assert "original vague ask" in data["augmented_prompt"]
    assert "Clarifications:" in data["augmented_prompt"]
    assert "Q:" in data["augmented_prompt"]
    assert captured["prompt"] == data["augmented_prompt"]


@pytest.mark.asyncio
async def test_clarification_session_uses_checkpoint_not_legacy_redis_key(
    client: AsyncClient,
) -> None:
    """Clarification state lives in LangGraph checkpoints; legacy Redis clar keys are unused."""

    from core.redis import get_redis

    reg, ws_id = await _register_builder(client)
    token = reg["access_token"]

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return dict(VAGUE_ROUND_1)

    redis = get_redis()
    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r1 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "automate my work", "workspace_id": str(ws_id)},
        )
    assert r1.status_code == 200
    sid = r1.json()["session_id"]
    legacy_key = f"{SESSION_KEY_PREFIX}{sid}"
    assert await redis.get(legacy_key) is None

    q = r1.json()["questions"]
    fake_def = _minimal_definition()
    with (
        patch(
            _CLARIFIER_PATCH,
            new=AsyncMock(side_effect=_fake_clarifier),
        ),
        patch(
            "services.workflow_interpreter.WorkflowInterpreter.interpret",
            new=AsyncMock(return_value=fake_def),
        ),
    ):
        r2 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "session_id": sid,
                "answers": [
                    {"question_id": q[0]["id"], "answer": "Manual"},
                    {"question_id": q[1]["id"], "answer": "report"},
                ],
                "force_proceed": True,
            },
        )
    assert r2.status_code == 200
    assert await redis.get(legacy_key) is None


@pytest.mark.asyncio
async def test_clarification_access_denied_wrong_user(client: AsyncClient) -> None:
    reg_a, ws_a = await _register_builder(client)
    reg_b, _ws_b = await _register_builder(client)
    token_a = reg_a["access_token"]
    token_b = reg_b["access_token"]

    async def _fake_clarifier(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return dict(VAGUE_ROUND_1)

    with patch(
        _CLARIFIER_PATCH,
        new=AsyncMock(side_effect=_fake_clarifier),
    ):
        r1 = await client.post(
            "/api/v1/workflows/interpret",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"text": "automate my work", "workspace_id": str(ws_a)},
        )
    sid = r1.json()["session_id"]
    q = r1.json()["questions"]

    resp = await client.post(
        "/api/v1/workflows/interpret",
        headers={"Authorization": f"Bearer {token_b}"},
        json={
            "session_id": sid,
            "answers": [
                {"question_id": q[0]["id"], "answer": "Manual"},
                {"question_id": q[1]["id"], "answer": "x"},
            ],
        },
    )
    assert resp.status_code == 403
