"""Validation tests for the Tools-Agent composite primitives.

Covers Phase 1 schema additions:

* ``chat`` trigger sub-type
* ``MemoryNode`` + ``OutputParserNode`` kinds
* ``parent_agent_id`` rules (which kinds may declare it, parent must be
  an AgentNode, satellites cannot ``depends_on``, nothing depends on a
  satellite, memory_ref / output_parser_ref / chat_memory_ref must point
  at the right kinds, satellites are excluded from the executor's order
  and from the cycle check)
"""

from __future__ import annotations

import pytest

from schemas.workflow import (
    ActionNode,
    AgentNode,
    DataStoreNode,
    MemoryNode,
    OutputParserNode,
    TriggerNode,
    WorkflowDefinition,
    satellites_by_agent,
    workflow_execution_order,
)


# ---------------------------------------------------------------------------
# Happy path — a full Tools-Agent composite.
# ---------------------------------------------------------------------------


def _composite() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="cs",
        nodes=[
            MemoryNode(id="mem", name="Conversation Memory", scope="session"),
            TriggerNode(
                id="chat",
                name="Customer Service Chat",
                trigger_type="chat",
                slug="cs-chat",
                chat_memory_ref="mem",
            ),
            AgentNode(
                id="agent",
                name="Customer Service Agent",
                depends_on=["chat"],
                role="CX agent.",
                tools=["check_customer", "register_customer"],
                memory_ref="mem",
                output_parser_ref="parser",
                chat_model={"provider": "openai", "model": "gpt-4o"},
            ),
            OutputParserNode(
                id="parser",
                name="Structured Output",
                parent_agent_id="agent",
                json_schema={"type": "object"},
            ),
            ActionNode(
                id="tool_check",
                name="Check Customer",
                provider="http_bearer",
                action_slug="check_customer",
                parent_agent_id="agent",
            ),
            DataStoreNode(
                id="tool_register",
                name="Register Customer",
                parent_agent_id="agent",
                op="write",
                table="customers",
                key="email",
            ),
        ],
    )


def test_composite_parses() -> None:
    wd = _composite()
    assert wd.name == "cs"


def test_satellites_excluded_from_execution_order() -> None:
    wd = _composite()
    order = workflow_execution_order(wd)
    # ``mem`` is non-executable; parser + tools are satellites — none appear.
    assert order == ["chat", "agent"]


def test_satellites_grouped_by_parent_agent() -> None:
    wd = _composite()
    by_parent = satellites_by_agent(wd)
    assert set(by_parent.keys()) == {"agent"}
    ids = {n.id for n in by_parent["agent"]}
    assert ids == {"parser", "tool_check", "tool_register"}


def test_chat_trigger_subtype_accepted() -> None:
    wd = WorkflowDefinition(
        name="x",
        nodes=[
            MemoryNode(id="m", name="m", scope="session"),
            TriggerNode(
                id="t",
                name="T",
                trigger_type="chat",
                slug="x",
                chat_memory_ref="m",
            ),
            AgentNode(id="a", name="A", depends_on=["t"]),
        ],
    )
    assert wd.iter_nodes()[1].trigger_type == "chat"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Validation failures.
# ---------------------------------------------------------------------------


def test_satellite_parent_must_exist() -> None:
    with pytest.raises(ValueError, match="unknown parent_agent_id"):
        WorkflowDefinition(
            name="x",
            nodes=[
                AgentNode(id="a", name="A"),
                ActionNode(
                    id="sat",
                    name="Sat",
                    provider="p",
                    action_slug="s",
                    parent_agent_id="ghost",
                ),
            ],
        )


def test_satellite_parent_must_be_agent() -> None:
    with pytest.raises(ValueError, match="must be an AgentNode"):
        WorkflowDefinition(
            name="x",
            nodes=[
                ActionNode(id="other", name="Other", provider="p", action_slug="s"),
                ActionNode(
                    id="sat",
                    name="Sat",
                    provider="p",
                    action_slug="s",
                    parent_agent_id="other",
                ),
            ],
        )


def test_satellite_cannot_depends_on() -> None:
    with pytest.raises(ValueError, match="cannot declare depends_on"):
        WorkflowDefinition(
            name="x",
            nodes=[
                AgentNode(id="a", name="A"),
                AgentNode(id="b", name="B"),
                ActionNode(
                    id="sat",
                    name="Sat",
                    provider="p",
                    action_slug="s",
                    parent_agent_id="a",
                    depends_on=["b"],
                ),
            ],
        )


def test_nothing_depends_on_satellite() -> None:
    with pytest.raises(ValueError, match="cannot depend on satellite"):
        WorkflowDefinition(
            name="x",
            nodes=[
                AgentNode(id="a", name="A"),
                ActionNode(
                    id="sat",
                    name="Sat",
                    provider="p",
                    action_slug="s",
                    parent_agent_id="a",
                ),
                AgentNode(id="next", name="Next", depends_on=["sat"]),
            ],
        )


def test_memory_ref_must_point_at_memory_node() -> None:
    with pytest.raises(ValueError, match="memory_ref"):
        WorkflowDefinition(
            name="x",
            nodes=[
                AgentNode(id="a", name="A", memory_ref="other"),
                AgentNode(id="other", name="Other"),
            ],
        )


def test_output_parser_ref_must_point_at_parser() -> None:
    with pytest.raises(ValueError, match="output_parser_ref"):
        WorkflowDefinition(
            name="x",
            nodes=[
                AgentNode(id="a", name="A", output_parser_ref="other"),
                AgentNode(id="other", name="Other"),
            ],
        )


def test_chat_memory_ref_must_point_at_memory_node() -> None:
    with pytest.raises(ValueError, match="chat_memory_ref"):
        WorkflowDefinition(
            name="x",
            nodes=[
                AgentNode(id="a", name="A"),
                TriggerNode(
                    id="t",
                    name="T",
                    trigger_type="chat",
                    chat_memory_ref="a",
                ),
            ],
        )


def test_only_satellite_kinds_may_set_parent_agent_id() -> None:
    # AgentNode itself cannot be a satellite (it's not in _SATELLITE_KINDS).
    # The pydantic discriminated union doesn't expose ``parent_agent_id``
    # on AgentNode at all, so the only way to violate this rule is via a
    # kind that DOES declare the field but isn't in the allow list. There
    # currently isn't one — every kind in ``_SATELLITE_KINDS`` is allowed.
    # This test guards against future regressions by checking the allow
    # list explicitly.
    from schemas.workflow import _SATELLITE_KINDS

    assert _SATELLITE_KINDS == frozenset(
        {"action", "data_store", "memory", "output_parser", "human_handoff"}
    )


def test_shared_memory_two_agents_allowed() -> None:
    # The same MemoryNode may be referenced by both an agent and a chat
    # trigger (or two agents). This is what makes the "shared Memory"
    # pattern in the n8n screenshot work.
    wd = WorkflowDefinition(
        name="x",
        nodes=[
            MemoryNode(id="mem", name="m", scope="session"),
            TriggerNode(
                id="t", name="T", trigger_type="chat",
                slug="x", chat_memory_ref="mem",
            ),
            AgentNode(id="a", name="A", depends_on=["t"], memory_ref="mem"),
        ],
    )
    assert wd.iter_nodes()[2].memory_ref == "mem"  # type: ignore[union-attr]
