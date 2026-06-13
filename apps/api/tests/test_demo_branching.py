"""Demo executor branch fidelity (#3 pruning + #4 overrides).

A test run must follow ONE path — the branches not taken are pruned — and the
tester can force a specific path with ``branch_overrides`` so each route can be
demonstrated deterministically.
"""

from __future__ import annotations

import pytest

from schemas.workflow import (
    ActionNode,
    ConditionNode,
    MergeNode,
    TriggerNode,
    WorkflowDefinition,
)
from services.demo_executor import run_demo


def _cs_defn() -> WorkflowDefinition:
    """Customer-service shape with proper activate_on gating."""
    return WorkflowDefinition(
        name="CS",
        nodes=[
            TriggerNode(id="trig", name="Inbound", trigger_type="manual"),
            ConditionNode(
                id="validate", name="Validate customer",
                depends_on=["trig"], expression="existing or new?",
                branches=["existing", "new"],
            ),
            ConditionNode(
                id="check_complaint", name="Complaint exists?",
                depends_on=["validate"], expression="open complaint?",
                branches=["has_complaint", "no_complaint"],
                activate_on={"validate": "existing"},
            ),
            ActionNode(
                id="escalate", name="Escalate", depends_on=["check_complaint"],
                provider="http_bearer", action_slug="escalate_complaint",
                activate_on={"check_complaint": "has_complaint"},
            ),
            ActionNode(
                id="ticket_existing", name="Create ticket (existing)",
                depends_on=["check_complaint"],
                provider="http_bearer", action_slug="create_ticket",
                activate_on={"check_complaint": "no_complaint"},
            ),
            ActionNode(
                id="register", name="Register", depends_on=["validate"],
                provider="http_bearer", action_slug="register_customer",
                activate_on={"validate": "new"},
            ),
            ActionNode(
                id="ticket_new", name="Create ticket (new)",
                depends_on=["register"],
                provider="http_bearer", action_slug="create_ticket",
            ),
        ],
    )


async def _run(overrides=None) -> tuple[set[str], set[str]]:
    completed: set[str] = set()
    skipped: set[str] = set()
    async for ev in run_demo(
        definition=_cs_defn(), input_data={"input": "hi"},
        step_delay_ms=0, branch_overrides=overrides,
    ):
        if ev.get("type") == "node_complete":
            completed.add(ev["node_id"])
        elif ev.get("type") == "node_skipped":
            skipped.add(ev["agent_id"])
    return completed, skipped


@pytest.mark.asyncio
async def test_default_path_prunes_other_branches() -> None:
    # Defaults: validate→existing, check_complaint→has_complaint → escalate.
    completed, skipped = await _run()
    assert completed == {"trig", "validate", "check_complaint", "escalate"}
    assert skipped == {"ticket_existing", "register", "ticket_new"}


@pytest.mark.asyncio
async def test_override_new_customer_path() -> None:
    completed, skipped = await _run({"validate": "new"})
    # New path: validate→new, register→create ticket. Existing-side pruned.
    assert completed == {"trig", "validate", "register", "ticket_new"}
    assert "escalate" in skipped
    assert "check_complaint" in skipped
    assert "ticket_existing" in skipped


@pytest.mark.asyncio
async def test_override_existing_no_complaint_path() -> None:
    completed, skipped = await _run(
        {"validate": "existing", "check_complaint": "no_complaint"}
    )
    assert completed == {"trig", "validate", "check_complaint", "ticket_existing"}
    assert "escalate" in skipped
    assert "register" in skipped


def _merge_defn() -> WorkflowDefinition:
    """Two mutually-exclusive branches converging on a merge → reply tail."""
    return WorkflowDefinition(
        name="Merge",
        nodes=[
            TriggerNode(id="trig", name="In", trigger_type="manual"),
            ConditionNode(
                id="route", name="Route", depends_on=["trig"],
                expression="a or b?", branches=["a", "b"],
            ),
            ActionNode(
                id="prep_a", name="Prep A", depends_on=["route"],
                provider="http_bearer", action_slug="do_a",
                activate_on={"route": "a"},
            ),
            ActionNode(
                id="prep_b", name="Prep B", depends_on=["route"],
                provider="http_bearer", action_slug="do_b",
                activate_on={"route": "b"},
            ),
            MergeNode(id="join", name="Join", depends_on=["prep_a", "prep_b"]),
            ActionNode(
                id="reply", name="Reply", depends_on=["join"],
                provider="http_bearer", action_slug="send_response",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_merge_is_or_join_runs_when_one_branch_taken() -> None:
    # Force branch "a": prep_b is pruned, but the merge + reply must STILL run
    # (a merge converges mutually-exclusive branches — OR-join, not AND).
    completed: set[str] = set()
    skipped: set[str] = set()
    async for ev in run_demo(
        definition=_merge_defn(), input_data={"input": "x"},
        step_delay_ms=0, branch_overrides={"route": "a"},
    ):
        if ev.get("type") == "node_complete":
            completed.add(ev["node_id"])
        elif ev.get("type") == "node_skipped":
            skipped.add(ev["agent_id"])
    assert "join" in completed, "merge must run when at least one branch ran"
    assert "reply" in completed, "shared tail after merge must run"
    assert "prep_a" in completed
    assert skipped == {"prep_b"}


@pytest.mark.asyncio
async def test_invalid_override_falls_back_to_first_branch() -> None:
    # An override that isn't a real branch label is ignored → default path.
    completed, _ = await _run({"validate": "bogus"})
    assert "escalate" in completed  # existing→has_complaint default path
