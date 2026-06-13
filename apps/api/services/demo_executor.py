"""Mocked walk-the-graph executor used by demo/test runs.

This is the backend of the "Test workflow" button on the visual canvas
(n8n parity). It walks the workflow definition in topological order
and emits the same ``ExecutionEvent`` shape that the real Dynamiq
executor produces, so the frontend's SSE consumer (StepTimeline +
canvas status overlay) doesn't have to special-case demo runs.

What we mock:
  * LLM calls — every ``agent`` node emits a deterministic message
    derived from its role + instructions; tool calls are simulated by
    invoking the agent's satellites once each with mock arguments.
  * Action / DataStore nodes — return ``__dry_run__: true`` stubs the
    same way the real ``action_runner`` does when no connection exists.
  * Condition / If nodes — pick the FIRST branch label (deterministic
    so the user sees the same graph traversal every time).
  * ForEach — fan out over a synthetic 2-item array so the user sees
    the loop body fire twice.
  * Wait for webhook — emits the wait event and immediately resumes
    with a stub payload (vs. blocking like the real executor).

This module itself performs no persistence — it only yields events. The
service layer (``WorkflowService.execute_workflow``) persists a lightweight
``demo=True`` ``WorkflowExecution`` + per-node step rows from the emitted
``node_complete`` events so the test-run inspector can reopen a demo run.
Those rows are excluded from the default executions listing. When called
directly (e.g. unit tests) with no ``execution_id``, a synthetic UUID is
minted per invocation and nothing is persisted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from agents.kb_tool import agent_uses_kb, kb_search
from core.snapshot import snapshot
from services.mock_responses import mock_for_action, mock_for_data_store
from schemas.workflow import (
    ActionNode,
    AgentNode,
    ConditionNode,
    DataStoreNode,
    ForEachNode,
    IfNode,
    MemoryNode,
    MergeNode,
    OutputParserNode,
    TriggerNode,
    WaitForWebhookNode,
    WorkflowDefinition,
    workflow_execution_order,
)

log = logging.getLogger(__name__)

# Cap how long any single demo agent call can take. Demo runs should
# feel snappy even when the LLM hiccups, so we bound each agent's wall
# clock and fall back to the synthesized stub on timeout.
_DEMO_LLM_TIMEOUT_S = 20.0
# Token budget per demo agent call. Demo runs are previews — users care
# about shape, not exhaustive answers. Keeping this small also keeps cost
# bounded if a user mashes the Test button.
_DEMO_LLM_MAX_TOKENS = 400

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_demo(
    *,
    definition: WorkflowDefinition,
    input_data: dict[str, Any] | None = None,
    step_delay_ms: int = 250,
    settings: Any | None = None,
    execution_id: UUID | None = None,
    branch_overrides: dict[str, str] | None = None,
    workspace_id: UUID | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Walk ``definition`` and yield SSE-shaped event dicts.

    ``step_delay_ms`` introduces a small artificial pause between
    agent_start → agent_thinking → agent_complete so the user sees the
    canvas status overlay transition through running → done. Set to 0
    in unit tests to keep them fast.

    ``settings`` enables the optional real-LLM path. When provided AND
    Azure OpenAI credentials are configured, each agent node calls the
    real LLM (Azure deployment from ``AZURE_OPENAI_DEPLOYMENT``) and the
    real text appears in the ``agent_complete`` event. External
    integrations remain dry-run regardless. If the call fails for any
    reason (timeout, auth, network) we fall back to the synthetic stub
    so the demo run still completes end-to-end.

    ``execution_id`` lets the caller (the service layer) thread the
    persisted ``WorkflowExecution.id`` through so the emitted events and
    step rows share one id. Defaults to a fresh UUID for direct callers
    (e.g. unit tests) that don't persist anything.
    """
    execution_id = execution_id or uuid4()
    yield _event(
        "workflow_start",
        execution_id=execution_id,
        workflow_id=None,
        data={"definition_name": definition.name, "demo": True},
    )

    # Surface the trigger before we walk executable nodes — it doesn't
    # have a depends_on cycle entry but the canvas overlay wants to
    # mark it as "done" first.
    trigger_node = _first_trigger(definition)
    if trigger_node is not None:
        yield _event(
            "trigger_fired",
            agent_id=trigger_node.id,
            agent_name=trigger_node.name,
            execution_id=execution_id,
            data={"trigger_type": trigger_node.trigger_type, "input": input_data or {}},
        )

    order = workflow_execution_order(definition)
    node_by_id = {n.id: n for n in definition.iter_nodes()}
    outputs: dict[str, Any] = {}
    decisions: dict[str, str] = {}
    skipped: set[str] = set()

    for node_id in order:
        node = node_by_id.get(node_id)
        if node is None:
            continue

        kind = getattr(node, "kind", None) or type(node).__name__
        node_started = time.time()
        node_input = _demo_input(node, outputs, input_data)

        # Trigger already emitted above.
        if isinstance(node, TriggerNode):
            outputs[node.id] = {"trigger_type": node.trigger_type, **(input_data or {})}
            yield _demo_node_complete(
                node, kind, node_input, outputs[node.id], node_started, execution_id
            )
            continue

        # Branch pruning: skip nodes whose activating decision wasn't taken, or
        # whose upstream was itself skipped — so the demo follows ONE path.
        # A merge is an OR-join: it converges mutually-exclusive branches, so it
        # is pruned only when ALL of its inputs were skipped, not just one.
        deps = getattr(node, "depends_on", []) or []
        if isinstance(node, MergeNode):
            upstream_pruned = bool(deps) and all(d in skipped for d in deps)
        else:
            upstream_pruned = any(d in skipped for d in deps)
        if not _activated(node, decisions, skipped) or upstream_pruned:
            skipped.add(node.id)
            yield _event(
                "node_skipped",
                agent_id=node.id,
                agent_name=node.name,
                execution_id=execution_id,
                data={"reason": "branch_not_taken"},
            )
            continue

        if isinstance(node, AgentNode):
            async for ev in _run_agent(
                node,
                definition,
                execution_id,
                outputs,
                step_delay_ms,
                input_data=input_data,
                settings=settings,
                workspace_id=workspace_id,
            ):
                yield ev
        elif isinstance(node, ActionNode):
            async for ev in _run_action(node, execution_id, outputs, step_delay_ms):
                yield ev
        elif isinstance(node, DataStoreNode):
            async for ev in _run_data_store(node, execution_id, outputs, step_delay_ms):
                yield ev
        elif isinstance(node, ConditionNode):
            async for ev in _run_condition(
                node, execution_id, outputs,
                branch_overrides=branch_overrides, settings=settings, input_data=input_data,
            ):
                yield ev
        elif isinstance(node, IfNode):
            async for ev in _run_if(
                node, execution_id, outputs,
                branch_overrides=branch_overrides, settings=settings, input_data=input_data,
            ):
                yield ev
        elif isinstance(node, ForEachNode):
            async for ev in _run_for_each(
                node, definition, execution_id, outputs, step_delay_ms
            ):
                yield ev
        elif isinstance(node, MergeNode):
            outputs[node.id] = {dep: outputs.get(dep) for dep in node.depends_on}
            yield _event(
                "agent_complete",
                agent_id=node.id,
                agent_name=node.name,
                execution_id=execution_id,
                data={"result": outputs[node.id]},
            )
        elif isinstance(node, WaitForWebhookNode):
            yield _event(
                "wait_for_webhook",
                agent_id=node.id,
                agent_name=node.name,
                execution_id=execution_id,
                data={"description": node.description},
            )
            # Simulate the resume immediately so the demo run doesn't block.
            outputs[node.id] = {"__demo_resumed__": True, "payload": {"ok": True}}
            yield _event(
                "webhook_resumed",
                agent_id=node.id,
                agent_name=node.name,
                execution_id=execution_id,
                data={"payload": outputs[node.id]["payload"]},
            )
        else:
            # MemoryNode / OutputParserNode / HumanHandoffNode are not in the
            # top-level execution order — guard anyway.
            continue

        # Record the branch a condition/if picked so downstream activate_on
        # gates can prune the paths that weren't taken.
        if isinstance(node, (ConditionNode, IfNode)):
            decided = outputs.get(node.id)
            if isinstance(decided, str):
                decisions[node.id] = decided

        # Per-node inspection event — emitted for every executed node kind so
        # the test-run drawer and persisted step rows match a real run.
        out = outputs.get(node.id)
        dry = isinstance(out, dict) and bool(out.get("__dry_run__"))
        yield _demo_node_complete(
            node, kind, node_input, out, node_started, execution_id, dry_run=dry
        )

    yield _event(
        "workflow_complete",
        execution_id=execution_id,
        success=True,
        data={"outputs": outputs, "demo": True},
    )


# ---------------------------------------------------------------------------
# Per-kind handlers
# ---------------------------------------------------------------------------


async def _run_agent(
    node: AgentNode,
    defn: WorkflowDefinition,
    execution_id: UUID,
    outputs: dict[str, Any],
    step_delay_ms: int,
    *,
    input_data: dict[str, Any] | None = None,
    settings: Any | None = None,
    workspace_id: UUID | None = None,
) -> AsyncIterator[dict[str, Any]]:
    yield _event(
        "agent_start",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={
            "role": node.role,
            "instructions_preview": node.instructions[:120],
            "real_llm": bool(settings) and _has_azure_creds(settings),
        },
    )
    await _sleep(step_delay_ms)

    yield _event(
        "agent_thinking",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        content=_synth_agent_reasoning(node),
    )
    await _sleep(step_delay_ms)

    # Invoke each satellite tool once with mock args so the UI shows the
    # tool_call → tool_result pair like a real run.
    sat_outputs: list[dict[str, Any]] = []
    for sat in _satellites_of(defn, node.id):
        tool_name = getattr(sat, "name", sat.id)
        if isinstance(sat, ActionNode):
            args = {"_demo": True}
            result = _mock_action_result(sat)
        elif isinstance(sat, DataStoreNode):
            args = {"key": "demo-key"}
            result = _mock_data_store_result(sat)
        else:
            continue
        yield _event(
            "tool_call",
            agent_id=node.id,
            agent_name=node.name,
            tool_name=tool_name,
            execution_id=execution_id,
            data={"args": args, "node_id": sat.id},
        )
        await _sleep(max(step_delay_ms // 2, 50))
        yield _event(
            "tool_result",
            agent_id=node.id,
            agent_name=node.name,
            tool_name=tool_name,
            execution_id=execution_id,
            data={"result": result, "node_id": sat.id},
        )
        sat_outputs.append({"tool": tool_name, "result": result})

    # Knowledge base (RAG): only for agents that declare the tool. Retrieve
    # grounding passages from the workspace's documents and show the lookup as
    # a tool_call → tool_result pair, then feed the context to the LLM so the
    # answer is grounded + cited.
    kb_context = ""
    if workspace_id is not None and agent_uses_kb(node.tools):
        question = _kb_question(node, input_data, outputs)
        yield _event(
            "tool_call",
            agent_id=node.id,
            agent_name=node.name,
            tool_name="knowledge_base",
            execution_id=execution_id,
            data={"args": {"query": question[:200]}, "node_id": f"{node.id}__kb"},
        )
        kb = await kb_search(question, workspace_id, settings, top_k=5)
        await _sleep(max(step_delay_ms // 2, 50))
        yield _event(
            "tool_result",
            agent_id=node.id,
            agent_name=node.name,
            tool_name="knowledge_base",
            execution_id=execution_id,
            data={
                "result": {
                    "found": kb.get("found"),
                    "count": kb.get("count", 0),
                    "sources": kb.get("sources", []),
                },
                "node_id": f"{node.id}__kb",
            },
        )
        if kb.get("found"):
            kb_context = kb["context"]
        sat_outputs.append(
            {"tool": "knowledge_base", "result": {"found": kb.get("found"), "sources": kb.get("sources", [])}}
        )

    # Choose between real LLM call (when caller opted-in AND creds exist)
    # and the deterministic stub. The real path is wrapped in a timeout
    # + broad except so a misconfigured deployment can't hang the demo.
    final: str
    used_real_llm = False
    if settings is not None and _has_azure_creds(settings):
        real_text = await _call_real_azure_for_agent(
            node, settings=settings, input_data=input_data, prior_outputs=outputs,
            kb_context=kb_context,
        )
        if real_text:
            final = real_text
            used_real_llm = True
        else:
            final = _synth_agent_final_output(node)
    else:
        final = _synth_agent_final_output(node)

    outputs[node.id] = {
        "content": final,
        "tool_calls": sat_outputs,
        "__real_llm__": used_real_llm,
    }
    yield _event(
        "agent_complete",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        content=final,
        data={"result": outputs[node.id], "real_llm": used_real_llm},
    )


async def _run_action(
    node: ActionNode,
    execution_id: UUID,
    outputs: dict[str, Any],
    step_delay_ms: int,
) -> AsyncIterator[dict[str, Any]]:
    # Top-level (non-satellite) action — emit a dry-run invocation event.
    yield _event(
        "action_invoked",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"provider": node.provider, "action_slug": node.action_slug},
    )
    await _sleep(step_delay_ms // 2)
    result = _mock_action_result(node)
    outputs[node.id] = result
    yield _event(
        "action_dry_run" if result.get("__dry_run__") else "action_result",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"result": result},
    )


async def _run_data_store(
    node: DataStoreNode,
    execution_id: UUID,
    outputs: dict[str, Any],
    step_delay_ms: int,
) -> AsyncIterator[dict[str, Any]]:
    result = _mock_data_store_result(node)
    outputs[node.id] = result
    yield _event(
        "data_store_op",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"op": node.op, "table": node.table, "result": result},
    )
    await _sleep(step_delay_ms // 4)


async def _run_condition(
    node: ConditionNode,
    execution_id: UUID,
    outputs: dict[str, Any],
    *,
    branch_overrides: dict[str, str] | None = None,
    settings: Any | None = None,
    input_data: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    branches = list(node.branches) or ["default"]
    branch, decided_by = _decide_branch(
        node.id, branches, node.expression,
        branch_overrides=branch_overrides, settings=settings,
        input_data=input_data, prior_outputs=outputs,
    )
    if settings is not None and _has_azure_creds(settings) and decided_by == "llm":
        llm = await _call_real_azure_for_branch(
            node.expression, branches, settings=settings,
            input_data=input_data, prior_outputs=outputs,
        )
        if llm in branches:
            branch = llm
    outputs[node.id] = branch
    yield _event(
        "condition_decided",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"branch": branch, "branches_available": branches, "decided_by": decided_by},
    )


async def _run_if(
    node: IfNode,
    execution_id: UUID,
    outputs: dict[str, Any],
    *,
    branch_overrides: dict[str, str] | None = None,
    settings: Any | None = None,
    input_data: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    branches = ["true", "false"]
    branch, decided_by = _decide_branch(
        node.id, branches, node.expression,
        branch_overrides=branch_overrides, settings=settings,
        input_data=input_data, prior_outputs=outputs, default="true",
    )
    if settings is not None and _has_azure_creds(settings) and decided_by == "llm":
        llm = await _call_real_azure_for_branch(
            node.expression, branches, settings=settings,
            input_data=input_data, prior_outputs=outputs,
        )
        if llm in branches:
            branch = llm
    outputs[node.id] = branch
    yield _event(
        "if_decided",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"branch": branch, "expression": node.expression, "decided_by": decided_by},
    )


def _decide_branch(
    node_id: str,
    branches: list[str],
    expression: str | None,
    *,
    branch_overrides: dict[str, str] | None,
    settings: Any | None,
    input_data: dict[str, Any] | None,
    prior_outputs: dict[str, Any],
    default: str | None = None,
) -> tuple[str, str]:
    """Pick a branch label and report how it was decided.

    Priority: explicit override → (LLM, signalled to caller) → first branch.
    Returns ``(branch, decided_by)`` where decided_by ∈ {override, llm, default}.
    """
    ov = (branch_overrides or {}).get(node_id)
    if ov and ov in branches:
        return ov, "override"
    if settings is not None and _has_azure_creds(settings):
        # Caller performs the (async) LLM call; signal intent + give a safe
        # provisional value in case the call fails.
        return (default or branches[0]), "llm"
    return (default or branches[0]), "default"


async def _run_for_each(
    node: ForEachNode,
    defn: WorkflowDefinition,
    execution_id: UUID,
    outputs: dict[str, Any],
    step_delay_ms: int,
) -> AsyncIterator[dict[str, Any]]:
    items = [{"index": 0, "demo": True}, {"index": 1, "demo": True}]
    yield _event(
        "for_each_started",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"item_count": len(items), "items_from": node.items_from},
    )
    item_outputs: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        yield _event(
            "for_each_item",
            agent_id=node.id,
            agent_name=node.name,
            execution_id=execution_id,
            data={"index": idx, "item": item},
        )
        # Walk body nodes once per item. We don't actually re-emit per-
        # node events here — the body nodes appear later in the top-level
        # execution order and run normally. The for_each_item event is
        # what the canvas overlay needs to show the loop iterations.
        item_outputs.append(item)
        await _sleep(max(step_delay_ms // 4, 25))
    outputs[node.id] = item_outputs
    yield _event(
        "for_each_complete",
        agent_id=node.id,
        agent_name=node.name,
        execution_id=execution_id,
        data={"item_count": len(items)},
    )


# ---------------------------------------------------------------------------
# Mock output generators
# ---------------------------------------------------------------------------


def _synth_agent_reasoning(node: AgentNode) -> str:
    """Synthesize a short "thinking" line that reflects the agent's role."""
    role = (node.role or "agent").strip().split(".")[0]
    return (
        f"[demo] Reasoning as: {role}. Considering inputs and applicable tools, "
        f"selecting the best next step…"
    )


def _synth_agent_final_output(node: AgentNode) -> str:
    role = (node.role or "agent").strip().split(".")[0]
    return (
        f"[demo] Mock response from {node.name} ({role}). "
        f"This output appears because the workflow is running in demo mode — "
        f"replace this by configuring an LLM and running the real executor."
    )


def _mock_action_result(node: ActionNode) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "__dry_run__": True,
        "__demo__": True,
        "provider": node.provider,
        "action_slug": node.action_slug,
        "echo_params": node.params or {},
        "message": (
            f"[demo] {node.provider}.{node.action_slug} would fire here. "
            f"Connect a real {node.provider} integration to perform the call."
        ),
    }
    realistic = mock_for_action(node.provider, node.action_slug, node.params or {})
    if realistic:
        merged: dict[str, Any] = {}
        merged.update(realistic)
        merged.update(envelope)
        return merged
    return envelope


def _mock_data_store_result(node: DataStoreNode) -> dict[str, Any]:
    params: dict[str, Any] = {
        "table": node.table,
        "key": node.key,
        "filter": node.filter or {},
        "payload": node.payload or {},
    }
    payload = mock_for_data_store(node.op, params)
    return {"__demo__": True, **payload}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_trigger(defn: WorkflowDefinition) -> TriggerNode | None:
    for n in defn.iter_nodes():
        if isinstance(n, TriggerNode):
            return n
    return None


def _satellites_of(defn: WorkflowDefinition, agent_id: str) -> list[Any]:
    out = []
    for n in defn.iter_nodes():
        if isinstance(n, (ActionNode, DataStoreNode, MemoryNode, OutputParserNode)):
            if getattr(n, "parent_agent_id", None) == agent_id:
                out.append(n)
    return out


def _activated(node: Any, decisions: dict[str, str], skipped: set[str]) -> bool:
    """Whether ``node`` should run given the branch decisions made so far.

    Mirrors ``ExtendedWorkflowExecutor._activated`` so the demo run follows the
    same single path a real run would, instead of executing every branch.
    """
    activate_on = getattr(node, "activate_on", None)
    if not activate_on:
        return True
    for ref, required in activate_on.items():
        if ref in skipped:
            return False
        if ref in decisions and required != "*" and decisions[ref] != required:
            return False
    return True


def _kb_question(
    node: Any, input_data: dict[str, Any] | None, outputs: dict[str, Any]
) -> str:
    """Build the retrieval query for a KB-grounded agent: the triggering input
    plus the relevant upstream outputs (the customer's problem, in practice)."""
    parts: list[str] = []
    if input_data:
        parts.append(json.dumps(input_data, default=str))
    for dep in getattr(node, "depends_on", []) or []:
        v = outputs.get(dep)
        if v:
            parts.append(str(v)[:500])
    q = " ".join(parts).strip()
    if not q:
        q = (getattr(node, "instructions", "") or getattr(node, "role", "") or "").strip()
    return q[:1000]


def _demo_input(
    node: Any, outputs: dict[str, Any], input_data: dict[str, Any] | None
) -> dict[str, Any]:
    """Best-effort generic input view for a demo node's node_complete event.

    Mirrors the shape the extended executor's ``_build_input`` produces so the
    test-run drawer renders identically for demo and real runs.
    """
    if isinstance(node, TriggerNode):
        return {"input": input_data or {}}
    deps = getattr(node, "depends_on", []) or []
    return {
        "upstream": {dep: outputs.get(dep) for dep in deps},
        "trigger_input": input_data or {},
    }


def _demo_node_complete(
    node: Any,
    kind: str,
    node_input: Any,
    output: Any,
    started_at: float,
    execution_id: UUID,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build a ``node_complete`` event matching the extended executor's shape.

    ``snapshot()`` always returns a dict, so the snapshot keys survive
    ``_event``'s None-dropping and demo rows match real rows exactly.
    """
    return _event(
        "node_complete",
        node_id=node.id,
        agent_id=node.id,
        node_name=node.name,
        node_kind=kind,
        input_snapshot=snapshot(node_input),
        output_snapshot=snapshot(output),
        status="completed",
        duration_ms=int((time.time() - started_at) * 1000),
        dry_run=dry_run,
        execution_id=execution_id,
    )


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build an ExecutionEvent-shaped dict.

    Drops ``None`` values so the SSE payload stays compact.
    """
    payload: dict[str, Any] = {"type": event_type}
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, UUID):
            payload[k] = str(v)
        else:
            payload[k] = v
    payload.setdefault("ts", time.time())
    return payload


async def _sleep(ms: int) -> None:
    if ms <= 0:
        return
    await asyncio.sleep(ms / 1000.0)


# ---------------------------------------------------------------------------
# Optional real-LLM path
# ---------------------------------------------------------------------------


def _has_azure_creds(settings: Any) -> bool:
    """Cheap pre-flight check before we attempt an Azure call.

    Treats blank strings as "missing". Mirrors the same check the real
    ``DynamiqService._resolve_llm`` performs so demo mode and production
    mode agree on what "creds present" means.
    """
    ep = (getattr(settings, "AZURE_OPENAI_ENDPOINT", "") or "").strip()
    key = (getattr(settings, "AZURE_OPENAI_API_KEY", "") or "").strip()
    return bool(ep) and bool(key)


async def _call_real_azure_for_branch(
    expression: str | None,
    branches: list[str],
    *,
    settings: Any,
    input_data: dict[str, Any] | None,
    prior_outputs: dict[str, Any],
) -> str | None:
    """Pick one branch label for a condition/if by asking the LLM.

    A single, low-token completion forced to answer with exactly one of the
    declared branch labels. Lets a demo run follow the path the *input*
    actually implies (e.g. "I'm a new customer" → the ``new`` branch) instead
    of always taking the first branch. Returns ``None`` on any failure.
    """
    try:
        from openai import AsyncAzureOpenAI
    except ImportError:
        return None

    ep = (settings.AZURE_OPENAI_ENDPOINT or "").strip().rstrip("/")
    key = (settings.AZURE_OPENAI_API_KEY or "").strip()
    deployment = (
        getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "")
        or getattr(settings, "AZURE_OPENAI_DEFAULT_MODEL", "")
        or ""
    ).strip()
    api_version = getattr(settings, "AZURE_OPENAI_API_VERSION", "")
    if not (ep and key and deployment):
        return None

    labels = ", ".join(branches)
    system_text = (
        "You are a routing classifier inside a workflow. Decide which branch "
        f"applies and respond with EXACTLY one of these labels, nothing else: {labels}."
    )
    bits: list[str] = []
    if expression:
        bits.append(f"## Decision\n{expression}")
    if input_data:
        bits.append("## Triggering input\n" + json.dumps(input_data, default=str)[:1500])
    if prior_outputs:
        bits.append("## Prior outputs\n" + json.dumps(prior_outputs, default=str)[:1500])
    bits.append(f"Respond with one of: {labels}")
    user_text = "\n\n".join(bits)

    try:
        client = AsyncAzureOpenAI(azure_endpoint=ep, api_key=key, api_version=api_version)
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                model=deployment,
                temperature=0.0,
                max_tokens=16,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_text},
                ],
            ),
            timeout=_DEMO_LLM_TIMEOUT_S,
        )
        raw = (completion.choices[0].message.content or "").strip().lower()
        # Exact match first, then substring (LLM may add punctuation).
        for b in branches:
            if raw == b.lower():
                return b
        for b in branches:
            if b.lower() in raw:
                return b
        return None
    except Exception:  # noqa: BLE001 — demo must never break on LLM errors
        log.warning("demo_executor.condition_llm_failed", exc_info=True)
        return None


async def _call_real_azure_for_agent(
    node: AgentNode,
    *,
    settings: Any,
    input_data: dict[str, Any] | None,
    prior_outputs: dict[str, Any],
    kb_context: str = "",
) -> str | None:
    """Run a single LLM completion against Azure for this agent.

    Returns the assistant's text, or ``None`` on any failure (logged but
    not raised — the caller falls back to the synthetic stub so the demo
    run still completes).

    No tool calling, no memory: this is the *demo* path, so the agent
    sees a focused prompt of role + instructions + upstream context +
    triggering input. The real production executor handles the full
    tool-loop + memory + retries.
    """
    try:
        # Local import keeps ``demo_executor`` cheap to import (most
        # demo paths never need the OpenAI SDK).
        from openai import AsyncAzureOpenAI
    except ImportError:
        log.warning("demo_executor.openai_sdk_missing")
        return None

    ep = (settings.AZURE_OPENAI_ENDPOINT or "").strip().rstrip("/")
    key = (settings.AZURE_OPENAI_API_KEY or "").strip()
    deployment = (
        getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "")
        or getattr(settings, "AZURE_OPENAI_DEFAULT_MODEL", "")
        or ""
    ).strip()
    api_version = getattr(settings, "AZURE_OPENAI_API_VERSION", "")

    if not (ep and key and deployment):
        return None

    system_text = _agent_system_prompt(node)
    if kb_context:
        system_text += (
            "\n\n# Knowledge base\n"
            "Ground your answer in the numbered sources below and cite them "
            "inline as [1], [2]. If the sources don't cover the question, say so."
        )
    user_text = _agent_user_prompt(node, input_data=input_data, prior_outputs=prior_outputs)
    if kb_context:
        user_text += "\n\nSources:\n" + kb_context

    try:
        client = AsyncAzureOpenAI(
            azure_endpoint=ep, api_key=key, api_version=api_version
        )
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                model=deployment,
                temperature=0.0,
                max_tokens=_DEMO_LLM_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_text},
                ],
            ),
            timeout=_DEMO_LLM_TIMEOUT_S,
        )
        choice = completion.choices[0]
        text = (choice.message.content or "").strip()
        return text or None
    except asyncio.TimeoutError:
        log.warning(
            "demo_executor.llm_timeout",
            extra={"agent_id": node.id, "timeout_s": _DEMO_LLM_TIMEOUT_S},
        )
        return None
    except Exception:  # noqa: BLE001 — demo must never break on LLM errors
        log.warning("demo_executor.llm_failed", extra={"agent_id": node.id}, exc_info=True)
        return None


def _agent_system_prompt(node: AgentNode) -> str:
    """Compose the system prompt for a demo agent call.

    Mirrors the shape of ``ChatRuntime._build_message_array`` so users
    see the same agent persona in demo runs as in production. We append
    a small reminder that external tool calls are not invoked.
    """
    parts: list[str] = []
    if (node.role or "").strip():
        parts.append(f"# Role\n{node.role.strip()}")
    if (node.instructions or "").strip():
        parts.append(f"# Instructions\n{node.instructions.strip()}")
    # Benign steering only. Earlier wording ("this is a test run, tools are
    # mocked, do not actually call any…") tripped Azure OpenAI's jailbreak
    # content filter (400 content_filter), forcing a fallback to the stub.
    parts.append(
        "# Response\n"
        "Reply directly with the answer you would produce for this step, "
        "based only on the information provided below."
    )
    return "\n\n".join(parts) if parts else "You are a helpful assistant."


def _agent_user_prompt(
    node: AgentNode,
    *,
    input_data: dict[str, Any] | None,
    prior_outputs: dict[str, Any],
) -> str:
    """Compose the user-side message: trigger input + upstream node outputs.

    Upstream context is filtered to nodes this agent declared as
    ``depends_on`` so the prompt doesn't bloat with unrelated outputs.
    """
    bits: list[str] = []
    if input_data:
        bits.append("Input:\n" + json.dumps(input_data, default=str, indent=2))
    relevant_upstream: dict[str, Any] = {}
    for dep in node.depends_on:
        if dep in prior_outputs:
            relevant_upstream[dep] = prior_outputs[dep]
    if relevant_upstream:
        bits.append(
            "Context from previous steps:\n"
            + json.dumps(relevant_upstream, default=str, indent=2)
        )
    # Plain task framing — avoid meta "produce the message you'd give for this
    # run" phrasing, which Azure's Prompt Shields can flag as a jailbreak.
    bits.append("Write your response.")
    return "\n\n".join(bits)


# ---------------------------------------------------------------------------
# Sample-input scaffolder
# ---------------------------------------------------------------------------


def sample_input_for(definition: WorkflowDefinition) -> dict[str, Any]:
    """Return a trigger-aware stub payload suitable for ``input_data``.

    Used by the "Test workflow" panel so the user doesn't have to craft
    the input JSON by hand. Mirrors n8n's "Generate sample input"
    affordance.
    """
    trig = _first_trigger(definition)
    if trig is None:
        return {}
    if trig.trigger_type == "chat":
        return {"message": "Hello from the demo run."}
    if trig.trigger_type == "webhook":
        return {"event": "demo.webhook", "payload": {"id": "demo-1", "ok": True}}
    if trig.trigger_type == "schedule":
        return {"scheduled_at": "2026-05-18T00:00:00Z"}
    if trig.trigger_type == "form":
        out: dict[str, Any] = {}
        for f in trig.form_fields:
            key = str(f.get("key") or f.get("name") or "field")
            typ = f.get("type") or "text"
            opts = f.get("options") or []
            if typ == "multi_choice" and opts:
                out[key] = list(opts[:1])
            elif typ == "choice" and opts:
                out[key] = opts[0]
            else:
                out[key] = f.get("placeholder") or f"Sample {key}"
        return out
    return {"input": "Sample input for demo run."}


__all__ = ["run_demo", "sample_input_for"]
