"""Schema-level tests for v2 polymorphic ``WorkflowDefinition`` nodes.

These run without DB / Redis — pure pydantic validation. They lock in the
contract the interpreter and the executor both depend on.
"""

from __future__ import annotations

import pytest

from schemas.workflow import (
    AgentDefinition,
    AgentNode,
    ConditionNode,
    ForEachNode,
    MergeNode,
    WaitForWebhookNode,
    WorkflowDefinition,
    workflow_execution_order,
)


# ---------------------------------------------------------------------------
# Back-compat: legacy agents-only definitions still validate identically.
# ---------------------------------------------------------------------------


def test_legacy_agents_only_definition_still_validates() -> None:
    wd = WorkflowDefinition(
        name="legacy",
        agents=[
            AgentDefinition(id="a", name="A"),
            AgentDefinition(id="b", name="B", depends_on=["a"]),
        ],
    )
    order = workflow_execution_order(wd)
    assert order == ["a", "b"]
    # iter_nodes promotes them to AgentNode.
    promoted = wd.iter_nodes()
    assert all(isinstance(n, AgentNode) for n in promoted)
    assert [n.id for n in promoted] == ["a", "b"]


def test_legacy_human_checkpoints_still_validate() -> None:
    wd = WorkflowDefinition(
        name="legacy-hitl",
        agents=[AgentDefinition(id="a", name="A")],
        human_checkpoints=["a"],
    )
    assert wd.human_checkpoints == ["a"]


# ---------------------------------------------------------------------------
# v2 happy paths.
# ---------------------------------------------------------------------------


def test_v2_condition_routes_downstream_agents() -> None:
    wd = WorkflowDefinition(
        name="cs",
        nodes=[
            AgentNode(id="lookup", name="Look up customer"),
            ConditionNode(
                id="route",
                name="Existing?",
                depends_on=["lookup"],
                expression="Existing customer? existing or new",
                branches=["existing", "new"],
            ),
            AgentNode(
                id="existing_path",
                name="Existing",
                depends_on=["route"],
                activate_on={"route": "existing"},
            ),
            AgentNode(
                id="new_path",
                name="New",
                depends_on=["route"],
                activate_on={"route": "new"},
            ),
        ],
    )
    order = workflow_execution_order(wd)
    assert order.index("lookup") < order.index("route")
    assert order.index("route") < order.index("existing_path")
    assert order.index("route") < order.index("new_path")


def test_v2_for_each_with_body_validates() -> None:
    wd = WorkflowDefinition(
        name="hr",
        nodes=[
            AgentNode(id="fetch", name="Fetch list"),
            ForEachNode(
                id="loop",
                name="For each",
                depends_on=["fetch"],
                items_from="fetch",
                body=["per_item"],
            ),
            AgentNode(id="per_item", name="Process", depends_on=["loop"]),
        ],
    )
    assert workflow_execution_order(wd)[0] == "fetch"


def test_v2_wait_for_webhook_validates() -> None:
    wd = WorkflowDefinition(
        name="hr",
        nodes=[
            AgentNode(id="invite", name="Send invite"),
            WaitForWebhookNode(
                id="wait_slot",
                name="Wait",
                depends_on=["invite"],
                description="candidate picks a slot",
                timeout_seconds=300,
            ),
            AgentNode(id="next", name="Next", depends_on=["wait_slot"]),
        ],
    )
    order = workflow_execution_order(wd)
    assert order == ["invite", "wait_slot", "next"]


def test_v2_merge_validates() -> None:
    wd = WorkflowDefinition(
        name="cs",
        nodes=[
            AgentNode(id="a", name="A"),
            AgentNode(id="b", name="B"),
            MergeNode(id="m", name="Merge", depends_on=["a", "b"]),
        ],
    )
    order = workflow_execution_order(wd)
    assert order.index("m") == 2


# ---------------------------------------------------------------------------
# Validation failures.
# ---------------------------------------------------------------------------


def test_empty_definition_rejected() -> None:
    with pytest.raises(ValueError, match="at least one agent or node"):
        WorkflowDefinition(name="empty")


def test_unknown_depends_on_rejected() -> None:
    with pytest.raises(ValueError, match="depends_on references unknown"):
        WorkflowDefinition(
            name="bad",
            nodes=[AgentNode(id="a", name="A", depends_on=["ghost"])],
        )


def test_cycle_rejected() -> None:
    # ``a -> b -> a`` — purely structural; pydantic accepts each node in
    # isolation but the model_validator catches the cycle.
    with pytest.raises(ValueError, match="cycle"):
        WorkflowDefinition(
            name="cyc",
            nodes=[
                AgentNode(id="a", name="A", depends_on=["b"]),
                AgentNode(id="b", name="B", depends_on=["a"]),
            ],
        )


def test_duplicate_node_ids_rejected() -> None:
    with pytest.raises(ValueError, match="node ids must be unique"):
        WorkflowDefinition(
            name="dup",
            nodes=[
                AgentNode(id="x", name="X1"),
                AgentNode(id="x", name="X2"),
            ],
        )


def test_for_each_unknown_items_from_rejected() -> None:
    with pytest.raises(ValueError, match="items_from refers to unknown"):
        WorkflowDefinition(
            name="bad",
            nodes=[
                AgentNode(id="a", name="A"),
                ForEachNode(
                    id="loop",
                    name="Loop",
                    items_from="ghost",
                    body=["a"],
                ),
            ],
        )


def test_condition_duplicate_branches_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate branches"):
        WorkflowDefinition(
            name="bad",
            nodes=[
                ConditionNode(
                    id="c",
                    name="Cond",
                    expression="?",
                    branches=["yes", "yes"],
                )
            ],
        )


def test_activate_on_unknown_target_rejected() -> None:
    with pytest.raises(ValueError, match="activate_on references unknown"):
        WorkflowDefinition(
            name="bad",
            nodes=[
                AgentNode(
                    id="a",
                    name="A",
                    activate_on={"ghost": "yes"},
                ),
            ],
        )
