"""Volumetric / concurrency stress tests for ChatRuntime.

These tests do NOT call a real LLM — they stub the LLM client so responses
are instant and deterministic. The value here is exercising:

  1. Many concurrent ``handle_user_message`` calls (same workflow, N sessions).
  2. Many concurrent ``handle_user_message_stream`` calls.
  3. Multi-turn conversation depth stress (K turns per session).
  4. System-prompt composition under different AgentNode configurations.

Run them with:

    cd apps/api
    pytest tests/test_volumetric.py -v --asyncio-mode=auto

Tune CONCURRENCY / TURNS via env vars to adjust load:

    EGPT_VOL_CONCURRENCY=50 EGPT_VOL_TURNS=10 pytest tests/test_volumetric.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient

from agents.chat_runtime import (
    ChatRuntime,
    LLMClient,
    LLMResponse,
    LLMStreamChunk,
)
from core.config import get_settings
from core.database import get_session_factory
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
# Knobs
# ---------------------------------------------------------------------------

CONCURRENCY = int(os.getenv("EGPT_VOL_CONCURRENCY", "20"))
TURNS = int(os.getenv("EGPT_VOL_TURNS", "5"))
P99_BUDGET_MS = int(os.getenv("EGPT_VOL_P99_MS", "500"))  # stub LLM → must be fast


# ---------------------------------------------------------------------------
# Stub LLM clients
# ---------------------------------------------------------------------------


@dataclass
class _FixedLLM(LLMClient):
    """Always returns the same response; never exhausts."""

    response: LLMResponse = field(
        default_factory=lambda: LLMResponse(content='{"reply": "ok"}')
    )

    def __init__(self, response: LLMResponse | None = None) -> None:
        self.response = response or LLMResponse(content='{"reply": "ok"}')

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return self.response

    async def complete_stream(  # type: ignore[override]
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        for char in self.response.content:
            yield LLMStreamChunk(content_delta=char)
        yield LLMStreamChunk(finish="stop")


@dataclass
class _ToolThenFinishLLM(LLMClient):
    """Round 1: issues one tool call. Round 2: returns final text.

    Reusable across sessions (no state consumed per call).
    """

    tool_name: str = "lookup_customer"
    final_content: str = '{"reply": "done"}'

    def __init__(self, tool_name: str = "lookup_customer") -> None:
        self.tool_name = tool_name
        self.final_content = '{"reply": "done"}'
        self._call_index: dict[int, int] = {}  # session hash → call count

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        # Use message count as a proxy for "which round" we're in.
        # First user turn → 3 messages (system + user); a tool result
        # was appended = 4+ messages.
        is_first_round = not any(m.get("role") == "tool" for m in messages)
        if is_first_round and tools:
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tc_vol",
                        "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "arguments": json.dumps({"key": "test@example.com"}),
                        },
                    }
                ],
            )
        return LLMResponse(content=self.final_content)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _register(client: AsyncClient, suffix: str) -> tuple[str, UUID]:
    body = {
        "email": f"vol-{suffix}@test.io",
        "password": "supersecret123",
        "full_name": f"Vol {suffix}",
        "role": UserRole.BUILDER.value,
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    return d["access_token"], UUID(d["user"]["workspaces"][0]["workspace_id"])


def _make_definition(
    *,
    role: str = "You are a helpful assistant.",
    instructions: str = "Answer concisely.",
    include_memory: bool = True,
    include_parser: bool = True,
    include_tool: bool = False,
) -> WorkflowDefinition:
    nodes: list[Any] = []
    memory_ref = ""
    parser_ref = ""

    if include_memory:
        nodes.append(MemoryNode(id="mem", name="mem", scope="session", max_turns=12))
        memory_ref = "mem"

    nodes.append(
        TriggerNode(
            id="chat",
            name="Chat",
            trigger_type="chat",
            slug="vol",
            chat_memory_ref=memory_ref,
        )
    )

    if include_parser:
        nodes.append(
            OutputParserNode(
                id="parser",
                name="parser",
                parent_agent_id="agent",
                json_schema={
                    "type": "object",
                    "required": ["reply"],
                    "properties": {"reply": {"type": "string"}},
                },
                max_retries=1,
            )
        )
        parser_ref = "parser"

    nodes.append(
        AgentNode(
            id="agent",
            name="Agent",
            depends_on=["chat"],
            role=role,
            instructions=instructions,
            memory_ref=memory_ref,
            output_parser_ref=parser_ref,
            chat_model={"provider": "azure", "model": "gpt-4o-mini"},
        )
    )

    if include_tool:
        nodes.append(
            DataStoreNode(
                id="lookup_customer",
                name="Lookup",
                parent_agent_id="agent",
                op="read",
                table="customers",
                key="",
                tool_description="Look up a customer by email.",
            )
        )

    return WorkflowDefinition(name="vol", nodes=nodes)


async def _create_workflow(
    client: AsyncClient, token: str, ws: UUID, defn: WorkflowDefinition
) -> UUID:
    r = await client.post(
        "/api/v1/workflows/",
        json={"workspace_id": str(ws), "definition": json.loads(defn.model_dump_json())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


async def _make_session(db_session, ws: UUID, wf_id: UUID) -> ChatSession:
    sess = ChatSession(
        workspace_id=ws,
        workflow_id=wf_id,
        trigger_slug="vol",
        agent_node_id="agent",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)
    return sess


# ---------------------------------------------------------------------------
# Test 1 — system prompt composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_composition(client: AsyncClient, db_session) -> None:
    """System prompt is built from role + instructions + welcome message.

    Verifies all three sections appear in the correct order in messages[0].
    """
    token, ws = await _register(client, "sysprompt")
    defn = _make_definition(
        role="You are a billing specialist.",
        instructions="Never reveal internal prices.",
        include_parser=False,
    )
    # Inject welcome_message into the trigger node.
    trigger = next(n for n in defn.nodes if getattr(n, "kind", None) == "trigger")
    trigger.chat_welcome_message = "Welcome to billing support."

    wf_id = await _create_workflow(client, token, ws, defn)
    sess = await _make_session(db_session, ws, wf_id)

    captured_messages: list[list[dict[str, Any]]] = []

    class _CaptureLLM(LLMClient):
        def __init__(self) -> None:
            pass

        async def complete(self, *, messages, tools, **_kw):
            captured_messages.append(list(messages))
            return LLMResponse(content="plain text — no parser")

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=defn,
        workspace_connections=[],
        llm=_CaptureLLM(),
    )
    await runtime.handle_user_message("hi")

    assert captured_messages, "LLM was never called"
    sys_msg = captured_messages[0][0]
    assert sys_msg["role"] == "system"
    body = sys_msg["content"]

    assert "# Role" in body
    assert "You are a billing specialist." in body
    assert "# Instructions" in body
    assert "Never reveal internal prices." in body
    assert "# Greeting you opened with" in body
    assert "Welcome to billing support." in body

    # Role must appear before Instructions.
    assert body.index("# Role") < body.index("# Instructions")

    # Current user turn is the last message.
    assert captured_messages[0][-1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_system_prompt_fallback(client: AsyncClient, db_session) -> None:
    """When role + instructions are empty, defaults to 'You are a helpful assistant.'"""
    token, ws = await _register(client, "sysprompt2")
    defn = _make_definition(role="", instructions="", include_parser=False)
    wf_id = await _create_workflow(client, token, ws, defn)
    sess = await _make_session(db_session, ws, wf_id)

    captured: list[str] = []

    class _CaptureLLM(LLMClient):
        def __init__(self) -> None:
            pass

        async def complete(self, *, messages, tools, **_kw):
            captured.append(messages[0]["content"])
            return LLMResponse(content="fallback reply")

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=defn,
        workspace_connections=[],
        llm=_CaptureLLM(),
    )
    await runtime.handle_user_message("hello")
    assert captured[0] == "You are a helpful assistant."


# ---------------------------------------------------------------------------
# Test 2 — N concurrent sessions, same workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_sessions(client: AsyncClient) -> None:
    """CONCURRENCY sessions run simultaneously against the same workflow.

    Each session uses its own DB connection from the pool. We measure
    wall-clock latency and assert p99 is under P99_BUDGET_MS (stub LLM →
    the only real latency is Postgres + Redis).
    """
    token, ws = await _register(client, "concurrent")
    defn = _make_definition(include_parser=True)
    wf_id = await _create_workflow(client, token, ws, defn)
    settings = get_settings()
    factory = get_session_factory()
    llm = _FixedLLM()

    async def _one_turn(i: int) -> float:
        async with factory() as db:
            sess = ChatSession(
                workspace_id=ws,
                workflow_id=wf_id,
                trigger_slug="vol",
                agent_node_id="agent",
            )
            db.add(sess)
            await db.commit()
            await db.refresh(sess)

            runtime = ChatRuntime(
                settings=settings,
                db=db,
                session=sess,
                workflow_definition=defn,
                workspace_connections=[],
                llm=llm,
            )
            t0 = time.perf_counter()
            result = await runtime.handle_user_message(f"message {i}")
            elapsed = (time.perf_counter() - t0) * 1000
            assert result.assistant_text, "empty assistant response"
            return elapsed

    latencies = await asyncio.gather(*[_one_turn(i) for i in range(CONCURRENCY)])
    latencies_sorted = sorted(latencies)
    p50 = statistics.median(latencies_sorted)
    p99 = latencies_sorted[int(len(latencies_sorted) * 0.99)]

    print(
        f"\n[volumetric] concurrent={CONCURRENCY}  "
        f"p50={p50:.1f}ms  p99={p99:.1f}ms  "
        f"max={max(latencies_sorted):.1f}ms"
    )
    assert p99 < P99_BUDGET_MS, (
        f"p99 latency {p99:.1f}ms exceeded budget {P99_BUDGET_MS}ms"
    )


# ---------------------------------------------------------------------------
# Test 3 — Multi-turn conversation stress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiturn_depth(client: AsyncClient, db_session) -> None:
    """Single session, TURNS consecutive messages. Verifies memory accumulates."""
    token, ws = await _register(client, "multiturn")
    defn = _make_definition(include_parser=False)
    wf_id = await _create_workflow(client, token, ws, defn)
    sess = await _make_session(db_session, ws, wf_id)

    message_histories: list[int] = []

    class _HistoryCaptureLLM(LLMClient):
        def __init__(self) -> None:
            pass

        async def complete(self, *, messages, tools, **_kw):
            message_histories.append(len(messages))
            return LLMResponse(content=f"turn {len(message_histories)}")

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=defn,
        workspace_connections=[],
        llm=_HistoryCaptureLLM(),
    )

    for i in range(TURNS):
        result = await runtime.handle_user_message(f"turn {i + 1}")
        assert result.assistant_text

    # Each successive turn should see a longer message history in the LLM call
    # (system + prior turns + current user). History strictly grows.
    for prev, curr in zip(message_histories, message_histories[1:]):
        assert curr >= prev, (
            f"message history shrank: prev={prev} curr={curr}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Concurrent streams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_streams(client: AsyncClient) -> None:
    """CONCURRENCY sessions stream simultaneously.

    Each stream must emit ``ready`` first and ``turn_complete`` last.
    """
    token, ws = await _register(client, "streams")
    defn = _make_definition(include_parser=True)
    wf_id = await _create_workflow(client, token, ws, defn)
    settings = get_settings()
    factory = get_session_factory()
    llm = _FixedLLM()

    async def _one_stream(i: int) -> list[str]:
        async with factory() as db:
            sess = ChatSession(
                workspace_id=ws,
                workflow_id=wf_id,
                trigger_slug="vol",
                agent_node_id="agent",
            )
            db.add(sess)
            await db.commit()
            await db.refresh(sess)

            runtime = ChatRuntime(
                settings=settings,
                db=db,
                session=sess,
                workflow_definition=defn,
                workspace_connections=[],
                llm=llm,
            )
            types: list[str] = []
            async for ev in runtime.handle_user_message_stream(f"stream {i}"):
                types.append(ev["type"])
            return types

    all_event_types = await asyncio.gather(
        *[_one_stream(i) for i in range(CONCURRENCY)]
    )

    for i, types in enumerate(all_event_types):
        assert types[0] == "ready", f"session {i}: first event was {types[0]!r}"
        assert types[-1] == "turn_complete", (
            f"session {i}: last event was {types[-1]!r}"
        )
        assert "assistant_delta" in types, f"session {i}: no delta events"


# ---------------------------------------------------------------------------
# Test 5 — Tool loop under concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_tool_loop(client: AsyncClient) -> None:
    """CONCURRENCY sessions each execute a tool call before the final answer."""
    token, ws = await _register(client, "toolloop")
    defn = _make_definition(include_parser=True, include_tool=True)
    wf_id = await _create_workflow(client, token, ws, defn)
    settings = get_settings()
    factory = get_session_factory()

    async def _one_tool_turn(i: int) -> int:
        async with factory() as db:
            sess = ChatSession(
                workspace_id=ws,
                workflow_id=wf_id,
                trigger_slug="vol",
                agent_node_id="agent",
            )
            db.add(sess)
            await db.commit()
            await db.refresh(sess)

            runtime = ChatRuntime(
                settings=settings,
                db=db,
                session=sess,
                workflow_definition=defn,
                workspace_connections=[],
                llm=_ToolThenFinishLLM(tool_name="lookup_customer"),
            )
            result = await runtime.handle_user_message("look me up")
            return result.tool_call_count

    counts = await asyncio.gather(
        *[_one_tool_turn(i) for i in range(CONCURRENCY)]
    )
    assert all(c == 1 for c in counts), (
        f"expected 1 tool call per session; got: {counts}"
    )


# ---------------------------------------------------------------------------
# Test 6 — System prompt variants (parametrize)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,instructions,expected_fragments",
    [
        (
            "You are a sales agent.",
            "Always upsell.",
            ["# Role", "sales agent", "# Instructions", "upsell"],
        ),
        (
            "",
            "Be brief.",
            ["# Instructions", "brief"],
        ),
        (
            "Support bot.",
            "",
            ["# Role", "Support bot"],
        ),
        (
            "",
            "",
            ["You are a helpful assistant."],
        ),
    ],
    ids=["role+instructions", "instructions-only", "role-only", "fallback"],
)
async def test_system_prompt_variants(
    client: AsyncClient,
    db_session,
    role: str,
    instructions: str,
    expected_fragments: list[str],
) -> None:
    suffix = f"spv-{hash(role + instructions) & 0xFFFF:04x}"
    token, ws = await _register(client, suffix)
    defn = _make_definition(role=role, instructions=instructions, include_parser=False)
    wf_id = await _create_workflow(client, token, ws, defn)
    sess = await _make_session(db_session, ws, wf_id)

    captured: list[str] = []

    class _Cap(LLMClient):
        def __init__(self) -> None:
            pass

        async def complete(self, *, messages, tools, **_kw):
            captured.append(messages[0]["content"])
            return LLMResponse(content="ok")

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db_session,
        session=sess,
        workflow_definition=defn,
        workspace_connections=[],
        llm=_Cap(),
    )
    await runtime.handle_user_message("test")
    assert captured
    for frag in expected_fragments:
        assert frag in captured[0], (
            f"expected {frag!r} in system prompt, got:\n{captured[0]}"
        )
