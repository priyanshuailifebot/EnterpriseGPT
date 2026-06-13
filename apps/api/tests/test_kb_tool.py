"""Knowledge-base (RAG) tool: only fires for agents that declare it, and
grounds the agent's answer via a visible knowledge_base lookup."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.kb_tool import agent_uses_kb, kb_search
from schemas.workflow import AgentNode, TriggerNode, WorkflowDefinition
from services import demo_executor
from services.demo_executor import run_demo


def test_agent_uses_kb_detection() -> None:
    assert agent_uses_kb(["knowledge_base"]) is True
    assert agent_uses_kb(["KB"]) is True
    assert agent_uses_kb(["rag"]) is True
    assert agent_uses_kb(["gmail", "slack"]) is False
    assert agent_uses_kb([]) is False
    assert agent_uses_kb(None) is False


@pytest.mark.asyncio
async def test_kb_search_graceful_when_unavailable() -> None:
    # No workspace / empty query → found False, never raises.
    r = await kb_search("", None, SimpleNamespace(), top_k=3)
    assert r["found"] is False


def _kb_defn(with_kb: bool) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="KB",
        nodes=[
            TriggerNode(id="t", name="In", trigger_type="manual"),
            AgentNode(
                id="resolve", name="Resolve", depends_on=["t"],
                role="Support resolver", instructions="Answer the customer.",
                tools=["knowledge_base"] if with_kb else [],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_kb_agent_emits_lookup_and_grounds(monkeypatch) -> None:
    # Stub RAGService so no real Qdrant/docs are needed.
    async def fake_query(self, question, workspace_id, top_k=8):  # noqa: ANN001
        chunk = SimpleNamespace(
            document_title="Refund Policy", page_number=3, chunk_index=1,
            text="Refunds are issued within 7 business days of approval.",
            score=0.91, document_id=uuid4(),
        )
        return SimpleNamespace(chunks=[chunk], query_embedding_time_ms=1.0)

    import rag.retrieval_service as rs
    monkeypatch.setattr(rs.RAGService, "query", fake_query)

    types = []
    kb_tool_events = []
    async for ev in run_demo(
        definition=_kb_defn(with_kb=True),
        input_data={"input": "When will my refund arrive?"},
        step_delay_ms=0,
        workspace_id=uuid4(),
    ):
        types.append(ev.get("type"))
        if ev.get("tool_name") == "knowledge_base":
            kb_tool_events.append(ev)

    # The KB lookup shows as a tool_call → tool_result pair.
    kinds = [e["type"] for e in kb_tool_events]
    assert "tool_call" in kinds and "tool_result" in kinds
    result = next(e for e in kb_tool_events if e["type"] == "tool_result")
    assert result["data"]["result"]["found"] is True
    assert result["data"]["result"]["sources"][0]["title"] == "Refund Policy"


@pytest.mark.asyncio
async def test_no_kb_lookup_when_not_declared(monkeypatch) -> None:
    called = {"n": 0}

    async def fake_query(self, *a, **k):  # noqa: ANN001
        called["n"] += 1
        return SimpleNamespace(chunks=[], query_embedding_time_ms=0.0)

    import rag.retrieval_service as rs
    monkeypatch.setattr(rs.RAGService, "query", fake_query)

    async for _ in run_demo(
        definition=_kb_defn(with_kb=False),
        input_data={"input": "hi"},
        step_delay_ms=0,
        workspace_id=uuid4(),
    ):
        pass
    # An agent without the knowledge_base tool must never hit the KB.
    assert called["n"] == 0
