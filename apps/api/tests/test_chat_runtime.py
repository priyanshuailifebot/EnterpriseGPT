"""ChatRuntime — full agent loop with a stubbed LLM client.

These tests drive the runtime end-to-end without touching a real LLM: we
inject an ``LLMClient`` stub via the constructor that replays a scripted
list of responses. Tool handlers are wired through the real
``ToolResolver``; with no DB session the data_store handlers fall back to
echo mode so we still exercise the tool-call path.

Three scenarios cover the contract:

* No tools — the LLM returns text on the first call, runtime persists it.
* Tool loop — the LLM calls a data_store tool, gets a result, then
  produces a final answer.
* Output parser — final text fails validation once, runtime re-prompts,
  passes on the retry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from agents.chat_runtime import ChatRuntime, LLMClient, LLMResponse
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
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubLLM(LLMClient):
    """Replays a pre-scripted list of LLMResponse objects."""

    script: list[LLMResponse] = field(default_factory=list)
    calls_seen: list[dict[str, Any]] = field(default_factory=list)

    def __init__(self, script: list[LLMResponse]) -> None:
        # Skip parent ``__init__`` — we don't need settings.
        self.script = list(script)
        self.calls_seen = []

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls_seen.append(
            {"messages": list(messages), "tools": tools, "response_format": response_format}
        )
        if not self.script:
            raise AssertionError("ran out of scripted LLM responses")
        return self.script.pop(0)


async def _register_workspace(client: AsyncClient) -> tuple[str, UUID]:
    body = {
        "email": "chat-runtime@test.io",
        "password": "supersecret123",
        "full_name": "Chat Runtime",
        "role": UserRole.BUILDER.value,
    }
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["access_token"], UUID(data["user"]["workspaces"][0]["workspace_id"])


def _composite_definition() -> WorkflowDefinition:
    """Reusable Tools-Agent composite: chat trigger + agent + 1 data tool + parser."""
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
                id="lookup_customer",
                name="Look up customer",
                parent_agent_id="agent",
                op="read",
                table="customers",
                key="",
                tool_description="Read a customer row by email.",
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


async def _make_workflow_with_definition(
    client: AsyncClient, token: str, workspace_id: UUID, definition: WorkflowDefinition
) -> UUID:
    """Create a workflow row + version that the chat runtime can load."""
    hdr = {"Authorization": f"Bearer {token}"}
    body = {
        "workspace_id": str(workspace_id),
        "definition": json.loads(definition.model_dump_json()),
    }
    resp = await client.post("/api/v1/workflows/", json=body, headers=hdr)
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_no_tool_call_path(
    client: AsyncClient, db_session, settings,
) -> None:
    token, ws = await _register_workspace(client)
    wd = _composite_definition()
    wf_id = await _make_workflow_with_definition(client, token, ws, wd)
    sess = ChatSession(
        workspace_id=ws,
        workflow_id=wf_id,
        trigger_slug="cs",
        agent_node_id="agent",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)

    llm = _StubLLM([LLMResponse(content='{"reply": "Hello there."}')])
    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=wd,
        workspace_connections=[],
        llm=llm,
    )
    result = await runtime.handle_user_message("Hi")
    assert result.assistant_text == '{"reply": "Hello there."}'
    assert result.structured == {"reply": "Hello there."}
    assert result.parser is not None and result.parser.ok
    assert result.tool_call_count == 0
    # System prompt is included in the first LLM call.
    assert llm.calls_seen[0]["messages"][0]["role"] == "system"
    assert llm.calls_seen[0]["messages"][-1] == {"role": "user", "content": "Hi"}


@pytest.mark.asyncio
async def test_runtime_tool_loop(client: AsyncClient, db_session) -> None:
    token, ws = await _register_workspace(client)
    wd = _composite_definition()
    wf_id = await _make_workflow_with_definition(client, token, ws, wd)
    sess = ChatSession(
        workspace_id=ws,
        workflow_id=wf_id,
        trigger_slug="cs",
        agent_node_id="agent",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)

    # First LLM call → tool call. Second LLM call → final assistant text.
    tool_call_response = LLMResponse(
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "lookup_customer",
                    "arguments": json.dumps({"key": "alice@example.com"}),
                },
            }
        ],
    )
    final_response = LLMResponse(content='{"reply": "Found you, Alice."}')
    llm = _StubLLM([tool_call_response, final_response])
    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=wd,
        workspace_connections=[],
        llm=llm,
    )
    result = await runtime.handle_user_message("look me up please")
    assert result.tool_call_count == 1
    assert result.parser is not None and result.parser.ok
    assert result.structured == {"reply": "Found you, Alice."}
    # The second LLM call saw a `tool` message in its history.
    second_call_messages = llm.calls_seen[1]["messages"]
    assert any(m.get("role") == "tool" for m in second_call_messages)


@pytest.mark.asyncio
async def test_runtime_output_parser_retry(client: AsyncClient, db_session) -> None:
    token, ws = await _register_workspace(client)
    wd = _composite_definition()
    wf_id = await _make_workflow_with_definition(client, token, ws, wd)
    sess = ChatSession(
        workspace_id=ws,
        workflow_id=wf_id,
        trigger_slug="cs",
        agent_node_id="agent",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)

    # First final response is invalid JSON. Retry produces valid JSON.
    invalid = LLMResponse(content="not json at all")
    valid_retry = LLMResponse(content='{"reply": "Corrected."}')
    llm = _StubLLM([invalid, valid_retry])
    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=wd,
        workspace_connections=[],
        llm=llm,
    )
    result = await runtime.handle_user_message("hello")
    assert result.parser is not None
    assert result.parser.ok
    assert result.structured == {"reply": "Corrected."}
    # The retry call included a corrective user turn.
    retry_messages = llm.calls_seen[1]["messages"]
    last_user = next(
        (m for m in reversed(retry_messages) if m.get("role") == "user"), None
    )
    assert last_user and "schema validation" in last_user["content"]
