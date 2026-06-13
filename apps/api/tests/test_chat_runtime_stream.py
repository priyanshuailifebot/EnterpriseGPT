"""ChatRuntime — streaming variant with mocked LLM stream chunks.

Verifies that ``handle_user_message_stream`` emits the SSE events the
frontend depends on, accumulates partial deltas correctly, assembles
tool calls from chunked argument JSON, and re-prompts the LLM when the
output parser rejects a draft.

We stub the ``LLMClient`` so each round of the tool loop replays a
pre-scripted list of ``LLMStreamChunk`` objects.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from agents.chat_runtime import (
    ChatRuntime,
    LLMClient,
    LLMStreamChunk,
)
from core.config import get_settings
from models.chat_session import ChatSession
from models.user import UserRole
from schemas.workflow import (
    AgentNode,
    DataStoreNode,
    MemoryNode,
    OutputParserNode,
    TriggerNode,
    WorkflowDefinition,
)


# ---------------------------------------------------------------------------
# Stub LLM that replays scripted streams.
# ---------------------------------------------------------------------------


@dataclass
class _StreamStubLLM(LLMClient):
    """Each call to ``complete_stream`` pops the next scripted list of chunks."""

    rounds: list[list[LLMStreamChunk]] = field(default_factory=list)
    seen_rounds: list[dict[str, Any]] = field(default_factory=list)

    def __init__(self, rounds: list[list[LLMStreamChunk]]) -> None:
        self.rounds = [list(r) for r in rounds]
        self.seen_rounds = []

    async def complete(self, **_kw: Any):
        raise AssertionError("non-streaming path should not be exercised here")

    async def complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        self.seen_rounds.append(
            {"messages": list(messages), "tools": tools,
             "response_format": response_format}
        )
        if not self.rounds:
            raise AssertionError("ran out of scripted LLM streaming rounds")
        chunks = self.rounds.pop(0)
        for c in chunks:
            yield c


def _composite_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="cs",
        nodes=[
            MemoryNode(id="mem", name="mem", scope="session", max_turns=12),
            TriggerNode(
                id="chat",
                name="Chat",
                trigger_type="chat",
                slug="cs",
                chat_memory_ref="mem",
            ),
            AgentNode(
                id="agent",
                name="Agent",
                depends_on=["chat"],
                role="CS",
                instructions="Help the user.",
                memory_ref="mem",
                output_parser_ref="parser",
                chat_model={"provider": "azure", "model": "gpt-4o-mini"},
            ),
            DataStoreNode(
                id="lookup",
                name="Look up",
                parent_agent_id="agent",
                op="read",
                table="customers",
                key="",
                tool_description="Read a row by key.",
            ),
            OutputParserNode(
                id="parser",
                name="parser",
                parent_agent_id="agent",
                json_schema={
                    "type": "object",
                    "required": ["reply"],
                    "properties": {"reply": {"type": "string"}},
                },
                max_retries=2,
            ),
        ],
    )


async def _register_workspace(client: AsyncClient) -> tuple[str, UUID]:
    body = {
        "email": "chat-stream@test.io",
        "password": "supersecret123",
        "full_name": "Chat Stream",
        "role": UserRole.BUILDER.value,
    }
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["access_token"], UUID(data["user"]["workspaces"][0]["workspace_id"])


async def _make_workflow(
    client: AsyncClient, token: str, ws: UUID, wd: WorkflowDefinition,
) -> UUID:
    hdr = {"Authorization": f"Bearer {token}"}
    body = {
        "workspace_id": str(ws),
        "definition": json.loads(wd.model_dump_json()),
    }
    resp = await client.post("/api/v1/workflows/", json=body, headers=hdr)
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _setup(client: AsyncClient, db_session) -> tuple[ChatSession, WorkflowDefinition]:
    token, ws = await _register_workspace(client)
    wd = _composite_definition()
    wf_id = await _make_workflow(client, token, ws, wd)
    sess = ChatSession(
        workspace_id=ws,
        workflow_id=wf_id,
        trigger_slug="cs",
        agent_node_id="agent",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)
    return sess, wd


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_emits_deltas_and_turn_complete(
    client: AsyncClient, db_session
) -> None:
    sess, wd = await _setup(client, db_session)

    # One round: assistant streams "He" "llo " "wo" "rld" then finishes.
    round1 = [
        LLMStreamChunk(content_delta="He"),
        LLMStreamChunk(content_delta="llo "),
        LLMStreamChunk(content_delta='{"reply": "Hi"}'),
        LLMStreamChunk(finish="stop", prompt_tokens=10, completion_tokens=5),
    ]
    llm = _StreamStubLLM([round1])

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=wd,
        workspace_connections=[],
        llm=llm,
    )

    events: list[dict[str, Any]] = []
    async for ev in runtime.handle_user_message_stream("hi"):
        events.append(ev)

    types = [e["type"] for e in events]
    assert types[0] == "ready"
    assert "assistant_delta" in types
    assert types[-1] == "turn_complete"

    # Concatenated deltas form the full assistant response.
    deltas = [e["delta"] for e in events if e["type"] == "assistant_delta"]
    assert "".join(deltas) == 'Hello {"reply": "Hi"}'

    # Parser validated the final JSON portion.
    final = events[-1]
    assert final["parser_status"] == "ok"
    assert final["structured"] == {"reply": "Hi"}


@pytest.mark.asyncio
async def test_stream_assembles_tool_call_across_chunks(
    client: AsyncClient, db_session
) -> None:
    sess, wd = await _setup(client, db_session)

    # Round 1 — assistant emits a tool_call streamed across multiple
    # chunks (name first, then argument JSON split into pieces). The
    # runtime must reassemble these into a single call to ``lookup``.
    round1 = [
        LLMStreamChunk(
            tool_call_index=0,
            tool_call_delta={
                "id": "call_1",
                "function": {"name": "lookup"},
            },
        ),
        LLMStreamChunk(
            tool_call_index=0,
            tool_call_delta={
                "function": {"arguments": '{"k'},
            },
        ),
        LLMStreamChunk(
            tool_call_index=0,
            tool_call_delta={
                "function": {"arguments": 'ey":'},
            },
        ),
        LLMStreamChunk(
            tool_call_index=0,
            tool_call_delta={
                "function": {"arguments": ' "alice"}'},
            },
        ),
        LLMStreamChunk(finish="tool_calls"),
    ]
    # Round 2 — assistant finalises after the tool result.
    round2 = [
        LLMStreamChunk(content_delta='{"reply": "found"}'),
        LLMStreamChunk(finish="stop"),
    ]
    llm = _StreamStubLLM([round1, round2])

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=wd,
        workspace_connections=[],
        llm=llm,
    )
    events: list[dict[str, Any]] = []
    async for ev in runtime.handle_user_message_stream("look me up"):
        events.append(ev)

    tool_call_events = [e for e in events if e["type"] == "tool_call"]
    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_call_events) == 1
    assert tool_call_events[0]["name"] == "lookup"
    assert tool_call_events[0]["args"] == {"key": "alice"}
    assert len(tool_result_events) == 1
    assert tool_result_events[0]["name"] == "lookup"

    # Second LLM round saw a `tool` message in its history.
    second = llm.seen_rounds[1]
    assert any(m.get("role") == "tool" for m in second["messages"])


@pytest.mark.asyncio
async def test_stream_parser_retry_then_success(
    client: AsyncClient, db_session
) -> None:
    sess, wd = await _setup(client, db_session)

    # Round 1 — invalid JSON. Parser will re-prompt.
    invalid_round = [
        LLMStreamChunk(content_delta="not valid"),
        LLMStreamChunk(finish="stop"),
    ]
    # The parser's reprompt itself triggers ANOTHER streaming round that
    # returns valid JSON.
    valid_round = [
        LLMStreamChunk(content_delta='{"reply": "corrected"}'),
        LLMStreamChunk(finish="stop"),
    ]
    llm = _StreamStubLLM([invalid_round, valid_round])

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=wd,
        workspace_connections=[],
        llm=llm,
    )
    events: list[dict[str, Any]] = []
    async for ev in runtime.handle_user_message_stream("hi"):
        events.append(ev)

    parser_retries = [e for e in events if e["type"] == "parser_retry"]
    assert len(parser_retries) >= 1
    final = events[-1]
    assert final["type"] == "turn_complete"
    assert final["parser_status"] == "ok"
    assert final["structured"] == {"reply": "corrected"}
