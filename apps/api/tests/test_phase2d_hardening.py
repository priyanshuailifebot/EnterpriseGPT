"""Phase 2d — schema validation for the new node-kinds + ChatPIIRedactor unit tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from schemas.workflow import (
    AgentNode,
    HumanHandoffNode,
    MemoryNode,
    OutputParserNode,
    TriggerNode,
    WorkflowDefinition,
    satellites_by_agent,
    workflow_execution_order,
)


# ---------------------------------------------------------------------------
# HumanHandoffNode — schema validation
# ---------------------------------------------------------------------------


def test_human_handoff_node_is_satellite_only() -> None:
    wd = WorkflowDefinition(
        name="cs",
        nodes=[
            TriggerNode(id="t", name="t", trigger_type="chat", slug="cs"),
            AgentNode(id="agent", name="A", depends_on=["t"]),
            HumanHandoffNode(id="handoff", name="Escalate", parent_agent_id="agent"),
        ],
    )
    # Excluded from top-level order (non-executable + satellite).
    order = workflow_execution_order(wd)
    assert order == ["t", "agent"]
    sats = satellites_by_agent(wd)["agent"]
    assert any(s.id == "handoff" for s in sats)


def test_human_handoff_requires_parent_to_be_agent() -> None:
    with pytest.raises(ValueError, match="must be an AgentNode"):
        WorkflowDefinition(
            name="x",
            nodes=[
                MemoryNode(id="mem", name="m"),
                HumanHandoffNode(
                    id="h", name="h", parent_agent_id="mem",
                ),
            ],
        )


def test_human_handoff_has_default_tool_description() -> None:
    h = HumanHandoffNode(id="h", name="Escalate to human")
    assert "Escalate" in h.tool_description
    assert h.priority_default == "normal"


# ---------------------------------------------------------------------------
# ChatPIIRedactor — local PII tests with a real PIIService (no Redis writes)
# ---------------------------------------------------------------------------


from services.chat_pii import ChatPIIRedactor
from services.pii_service import PIIService


class _StubPII(PIIService):
    """Subclass that skips Redis I/O; used only for unit tests."""

    def __init__(self) -> None:
        super().__init__(redis=None)
        self.saved: dict[str, Any] | None = None

    async def load_token_map(self, _session_id: str):
        return self.saved or {}

    async def save_token_map(self, _session_id: str, token_map, **_kw) -> None:
        self.saved = dict(token_map)


@pytest.mark.asyncio
async def test_redactor_redacts_then_restores() -> None:
    from uuid import uuid4

    pii = _StubPII()
    r = ChatPIIRedactor(pii, session_id=uuid4())
    await r.load()
    redacted = r.redact_for_persistence(
        "Hi, my email is alice@example.com and ph 555-123-4567."
    ) or ""
    assert "alice@example.com" not in redacted
    assert "555-123-4567" not in redacted
    assert "<<PII_EMAIL_" in redacted
    # Restoration roundtrips back to the originals.
    restored = r.restore_for_display(redacted) or ""
    assert "alice@example.com" in restored
    assert "555-123-4567" in restored


@pytest.mark.asyncio
async def test_redactor_empty_input_passthrough() -> None:
    from uuid import uuid4

    pii = _StubPII()
    r = ChatPIIRedactor(pii, session_id=uuid4())
    await r.load()
    assert r.redact_for_persistence(None) is None
    assert r.redact_for_persistence("") == ""
    assert r.restore_for_display("") == ""


@pytest.mark.asyncio
async def test_redactor_flush_writes_token_map() -> None:
    from uuid import uuid4

    pii = _StubPII()
    r = ChatPIIRedactor(pii, session_id=uuid4())
    await r.load()
    r.redact_for_persistence("alice@example.com")
    await r.flush()
    assert pii.saved is not None
    assert len(pii.saved) >= 1


@pytest.mark.asyncio
async def test_redactor_load_is_idempotent() -> None:
    from uuid import uuid4

    pii = _StubPII()
    r = ChatPIIRedactor(pii, session_id=uuid4())
    await r.load()
    await r.load()  # second call must be a no-op
    assert r.token_count == 0
