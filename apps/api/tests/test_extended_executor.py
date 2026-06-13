"""Behaviour tests for the v2 unified-graph executor.

Strategy: stub ``DynamiqService`` so each "agent run" is deterministic and
fast. Condition routing is fed via the ``condition_eval`` constructor hook
so we don't need a live LLM. Redis is the real test instance (autouse
fixture flushes it).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from agents.extended_executor import ExtendedWorkflowExecutor, _resume_payload_key
from core.redis import get_redis
from schemas.workflow import (
    AgentNode,
    ConditionNode,
    ForEachNode,
    MergeNode,
    WaitForWebhookNode,
    WorkflowDefinition,
)


# ---------------------------------------------------------------------------
# Fake DynamiqService — every agent emits a synthetic ``agent_complete``
# event with ``content`` derived from its id and the input it was given.
# ---------------------------------------------------------------------------


class _StubDynamiq:
    def __init__(self, *, outputs_by_id: dict[str, str] | None = None) -> None:
        # Per-agent canned text. Falls back to ``input:<id>``.
        self._outputs = outputs_by_id or {}

    def hydrate_agent_stage(
        self,
        definition: WorkflowDefinition,
        *,
        focus_id: str,
        prior_outputs: dict[str, str],
        agent_tools_by_id: dict[str, list[Any]] | None = None,
    ) -> dict[str, Any]:
        # The real method returns a Dynamiq Workflow; here we return a tag
        # the fake ``run_workflow_stream`` reads back.
        return {"_focus": focus_id}

    async def run_workflow_stream(
        self,
        workflow: dict[str, Any],
        *,
        input_data: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        focus = workflow["_focus"]
        # Emit a thinking event then a workflow_complete with the canonical
        # nested shape the executor expects.
        yield {
            "type": "agent_thinking",
            "agent_id": focus,
            "agent_name": focus,
            "content": "thinking",
        }
        text = self._outputs.get(focus, f"out:{focus}")
        yield {
            "type": "workflow_complete",
            "success": True,
            "result": {focus: {"output": {"content": text}}},
        }


@pytest.fixture
def stub_settings():  # type: ignore[no-untyped-def]
    from core.config import get_settings

    return get_settings()


# ---------------------------------------------------------------------------
# Condition routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condition_routes_to_matched_branch(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="cs",
        nodes=[
            AgentNode(id="lookup", name="Lookup"),
            ConditionNode(
                id="route",
                name="?",
                depends_on=["lookup"],
                expression="existing or new?",
                branches=["existing", "new"],
            ),
            AgentNode(
                id="existing_path",
                name="E",
                depends_on=["route"],
                activate_on={"route": "existing"},
            ),
            AgentNode(
                id="new_path",
                name="N",
                depends_on=["route"],
                activate_on={"route": "new"},
            ),
        ],
    )

    async def force_existing(_expr: str, _up: dict[str, str]) -> str:
        return "existing"

    exec_ = ExtendedWorkflowExecutor(
        stub_settings,
        dynamiq=_StubDynamiq(),
        condition_eval=force_existing,
    )

    events: list[dict[str, Any]] = []
    async for ev in exec_.stream(
        definition=wd,
        execution_id=uuid4(),
        input_data={"input": "hello"},
    ):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "condition_decided" in kinds
    decided = next(e for e in events if e["type"] == "condition_decided")
    assert decided["branch"] == "existing"

    skipped_ids = {e["node_id"] for e in events if e["type"] == "node_skipped"}
    completed_ids = {
        e.get("agent_id")
        for e in events
        if e["type"] == "agent_complete"
    }
    assert "existing_path" in completed_ids
    assert "new_path" in skipped_ids


# ---------------------------------------------------------------------------
# for_each fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_for_each_runs_body_once_per_item(stub_settings: Any) -> None:
    items = [{"name": "Alice"}, {"name": "Bob"}, {"name": "Carol"}]
    wd = WorkflowDefinition(
        name="hr",
        nodes=[
            AgentNode(id="fetch", name="Fetch"),
            ForEachNode(
                id="loop",
                name="loop",
                depends_on=["fetch"],
                items_from="fetch",
                body=["per_item"],
                max_concurrency=3,
            ),
            AgentNode(id="per_item", name="P", depends_on=["loop"]),
        ],
    )

    stub = _StubDynamiq(outputs_by_id={"fetch": json.dumps(items)})

    exec_ = ExtendedWorkflowExecutor(
        stub_settings,
        dynamiq=stub,
        condition_eval=lambda _e, _u: asyncio.sleep(0, result="unused"),  # type: ignore[arg-type]
    )

    events: list[dict[str, Any]] = []
    async for ev in exec_.stream(
        definition=wd,
        execution_id=uuid4(),
        input_data={"input": "{}"},
    ):
        events.append(ev)

    completes = [e for e in events if e["type"] == "for_each_complete"]
    assert len(completes) == 1
    assert completes[0]["count"] == 3

    items_events = [e for e in events if e["type"] == "for_each_item"]
    assert len(items_events) == 3
    # Indices are 0,1,2 — not necessarily in order due to gather, but each
    # must be present exactly once.
    assert sorted(e["for_each_index"] for e in items_events) == [0, 1, 2]


@pytest.mark.asyncio
async def test_for_each_empty_list_produces_empty_results(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="hr-empty",
        nodes=[
            AgentNode(id="fetch", name="Fetch"),
            ForEachNode(
                id="loop",
                name="loop",
                depends_on=["fetch"],
                items_from="fetch",
                body=["per_item"],
            ),
            AgentNode(id="per_item", name="P", depends_on=["loop"]),
        ],
    )
    stub = _StubDynamiq(outputs_by_id={"fetch": "not a list"})
    exec_ = ExtendedWorkflowExecutor(stub_settings, dynamiq=stub)

    events: list[dict[str, Any]] = []
    async for ev in exec_.stream(
        definition=wd,
        execution_id=uuid4(),
        input_data={"input": "{}"},
    ):
        events.append(ev)

    completes = [e for e in events if e["type"] == "for_each_complete"]
    assert len(completes) == 1
    assert completes[0].get("results") == []


# ---------------------------------------------------------------------------
# wait_for_webhook park + resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_webhook_parks_until_payload_arrives(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="hr",
        nodes=[
            AgentNode(id="invite", name="Invite"),
            WaitForWebhookNode(
                id="wait_slot",
                name="Wait",
                depends_on=["invite"],
                timeout_seconds=30,
            ),
            AgentNode(id="next", name="Next", depends_on=["wait_slot"]),
        ],
    )
    exec_ = ExtendedWorkflowExecutor(stub_settings, dynamiq=_StubDynamiq())
    exec_id = uuid4()

    async def driver() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for ev in exec_.stream(
            definition=wd,
            execution_id=exec_id,
            input_data={"input": "x"},
        ):
            out.append(ev)
        return out

    async def resume_after_park() -> None:
        # Spin until the wait_for_webhook event fires (the executor parks
        # immediately after; we just need the Redis key in place).
        deadline = asyncio.get_running_loop().time() + 5.0
        redis = get_redis()
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
            await redis.set(
                _resume_payload_key(exec_id, "wait_slot"),
                json.dumps({"slot_iso": "2030-01-01T10:00:00Z", "language": "en"}),
                ex=120,
            )
            break

    drive_task = asyncio.create_task(driver())
    await resume_after_park()
    events = await asyncio.wait_for(drive_task, timeout=10.0)

    park = [e for e in events if e["type"] == "wait_for_webhook"]
    resumed = [e for e in events if e["type"] == "webhook_resumed"]
    assert len(park) == 1
    assert len(resumed) == 1
    assert resumed[0]["payload"]["language"] == "en"
    # And the downstream agent ran.
    completed_ids = {
        e.get("agent_id")
        for e in events
        if e["type"] == "agent_complete"
    }
    assert "next" in completed_ids


# ---------------------------------------------------------------------------
# Merge: outputs from upstream are joined under the merge node id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_collects_upstream_outputs(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="cs",
        nodes=[
            AgentNode(id="a", name="A"),
            AgentNode(id="b", name="B"),
            MergeNode(id="m", name="M", depends_on=["a", "b"]),
        ],
    )
    exec_ = ExtendedWorkflowExecutor(
        stub_settings,
        dynamiq=_StubDynamiq(outputs_by_id={"a": "alpha", "b": "beta"}),
    )

    final: dict[str, Any] | None = None
    async for ev in exec_.stream(
        definition=wd,
        execution_id=uuid4(),
        input_data={"input": "{}"},
    ):
        if ev["type"] == "workflow_complete":
            final = ev["result"]
    assert final is not None
    outs = final["agent_outputs"]
    assert outs["m"] == {"a": "alpha", "b": "beta"}
