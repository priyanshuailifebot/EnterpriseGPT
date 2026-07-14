"""P4 — per-node ``on_error`` failure policy for action nodes.

Monkeypatches ``invoke_action`` so an action with ``action_slug == "failing"``
raises; everything else succeeds. Exercises fail / continue / route.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

import agents.extended_executor as ee
from agents.extended_executor import ExtendedWorkflowExecutor
from schemas.workflow import ActionNode, ForEachNode, WorkflowDefinition


@pytest.fixture
def stub_settings():  # type: ignore[no-untyped-def]
    from core.config import get_settings

    return get_settings()


@pytest.fixture(autouse=True)
def _patch_invoke_action(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(**kwargs: Any) -> dict[str, Any]:
        slug = kwargs.get("action_slug")
        if slug == "failing":
            raise RuntimeError("boom")
        if slug == "list_action":
            return {"__dry_run__": False, "data": [{"x": 1}, {"x": 2}]}
        return {"__dry_run__": False, "data": {"ok": True}}

    monkeypatch.setattr(ee, "invoke_action", _fake)


def _action(node_id: str, slug: str, **kw: Any) -> ActionNode:
    return ActionNode(
        id=node_id,
        name=node_id,
        provider="http_bearer",
        action_slug=slug,
        max_retries=0,  # single attempt — deterministic + fast
        **kw,
    )


async def _run(wd: WorkflowDefinition, stub_settings: Any) -> list[dict[str, Any]]:
    exec_ = ExtendedWorkflowExecutor(stub_settings)
    return [
        ev
        async for ev in exec_.stream(
            definition=wd, execution_id=uuid4(), input_data={"input": "x"}
        )
    ]


@pytest.mark.asyncio
async def test_on_error_fail_emits_fatal_error(stub_settings: Any) -> None:
    wd = WorkflowDefinition(name="fail", nodes=[_action("a", "failing", on_error="fail")])
    events = await _run(wd, stub_settings)
    errors = [e for e in events if e["type"] == "error"]
    assert any(e.get("node_id") == "a" for e in errors)
    assert not any(e["type"] == "node_error" for e in events)


@pytest.mark.asyncio
async def test_on_error_continue_skips_dependents(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="cont",
        nodes=[
            _action("a", "failing", on_error="continue"),
            _action("b", "ok_action", depends_on=["a"]),
        ],
    )
    events = await _run(wd, stub_settings)
    assert not any(e["type"] == "error" for e in events)
    handled = [e for e in events if e["type"] == "node_error"]
    assert handled and handled[0]["handled"] == "continue"
    skipped = {e["node_id"] for e in events if e["type"] == "node_skipped"}
    assert "b" in skipped  # dependent pruned because "a" was skipped
    assert any(e["type"] == "workflow_complete" for e in events)


@pytest.mark.asyncio
async def test_on_error_route_activates_error_branch(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="route",
        nodes=[
            _action("a", "failing", on_error="route"),
            _action("on_fail", "ok_action", depends_on=["a"], activate_on={"a": "failed"}),
            _action("on_ok", "ok_action", depends_on=["a"], activate_on={"a": "ok"}),
        ],
    )
    events = await _run(wd, stub_settings)
    assert not any(e["type"] == "error" for e in events)
    handled = [e for e in events if e["type"] == "node_error"]
    assert handled and handled[0]["handled"] == "route"
    skipped = {e["node_id"] for e in events if e["type"] == "node_skipped"}
    ran = {e.get("node_id") for e in events if e["type"] == "action_result"}
    assert "on_fail" in ran  # error branch activated
    assert "on_ok" in skipped  # success branch pruned


@pytest.mark.asyncio
async def test_for_each_continue_isolates_item_failure(stub_settings: Any) -> None:
    # A per-item body action fails for every item; on_error="continue" must
    # isolate it (no fatal error, loop completes) rather than abort the batch.
    wd = WorkflowDefinition(
        name="loop-fail",
        nodes=[
            _action("src", "list_action"),
            ForEachNode(
                id="loop",
                name="loop",
                depends_on=["src"],
                items_from="src",
                items_path="$.data",
                body=["fail_item"],
            ),
            _action("fail_item", "failing", depends_on=["loop"], on_error="continue"),
        ],
    )
    events = await _run(wd, stub_settings)
    assert not any(e["type"] == "error" for e in events)
    handled = [e for e in events if e["type"] == "node_error" and e.get("handled") == "continue"]
    assert len(handled) == 2  # one per item, isolated
    assert any(e["type"] == "for_each_complete" for e in events)
    assert any(e["type"] == "workflow_complete" for e in events)


@pytest.mark.asyncio
async def test_on_error_route_ok_branch_on_success(stub_settings: Any) -> None:
    wd = WorkflowDefinition(
        name="route-ok",
        nodes=[
            _action("a", "ok_action", on_error="route"),
            _action("on_ok", "ok_action", depends_on=["a"], activate_on={"a": "ok"}),
            _action("on_fail", "ok_action", depends_on=["a"], activate_on={"a": "failed"}),
        ],
    )
    events = await _run(wd, stub_settings)
    ran = {e.get("node_id") for e in events if e["type"] == "action_result"}
    skipped = {e["node_id"] for e in events if e["type"] == "node_skipped"}
    assert "on_ok" in ran  # success branch activated on "ok" decision
    assert "on_fail" in skipped
