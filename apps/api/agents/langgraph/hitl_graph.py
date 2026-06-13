"""HITL LangGraph — plan → (execute | human interrupt)* → finalize."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import interrupt

from agents.dynamiq_service import DynamiqService
from agents.langgraph.state import WorkflowState
from schemas.workflow import WorkflowDefinition
from schemas.workflow import workflow_execution_order as topo_order

log = logging.getLogger(__name__)


def _agent_blob(output: dict[str, Any] | None, agent_id: str) -> str:
    if not isinstance(output, dict):
        return ""
    cell = output.get(agent_id)
    if not isinstance(cell, dict):
        return ""
    inner = cell.get("output")
    if isinstance(inner, dict):
        content = inner.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(inner)[:16000]
    if isinstance(inner, str):
        return inner
    return ""


def build_hitl_graph(
    definition: WorkflowDefinition,
    dynamiq: DynamiqService,
    *,
    dynamiq_input: dict[str, Any],
    push_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    agent_tools_by_id: dict[str, list[Any]] | None = None,
) -> StateGraph:
    """Return an *uncompiled* StateGraph wired for Dynamiq single-agent stages + HITL."""

    async def _emit(ev: dict[str, Any]) -> None:
        if push_event:
            await push_event(ev)

    async def plan_node(state: WorkflowState) -> dict[str, Any]:
        order = topo_order(definition)
        outs = dict(state["agent_outputs"])
        outs["plan"] = " → ".join(order)
        outs["__ordered_ids__"] = order
        outs["__next_execution_index__"] = 0
        outs["__hitl_approved_indices__"] = []
        outs["__human_checkpoint_agent_ids__"] = list(definition.human_checkpoints)
        return {"agent_outputs": outs, "iteration_count": 0, "error": None}

    def route_next(state: WorkflowState) -> str:
        if state.get("error"):
            return "error_finalize"
        outs = state["agent_outputs"]
        ids = outs.get("__ordered_ids__") or []
        k = int(outs.get("__next_execution_index__", 0))
        if k >= len(ids):
            return "finalize"
        hc = set(outs.get("__human_checkpoint_agent_ids__") or [])
        appr = set(outs.get("__hitl_approved_indices__") or [])
        next_aid = ids[k]
        if next_aid in hc and k not in appr:
            return "hitl_prep"
        return "execute_agent"

    async def hitl_prep_node(state: WorkflowState) -> dict[str, Any]:
        outs = state["agent_outputs"]
        ids = outs["__ordered_ids__"]
        k = int(outs.get("__next_execution_index__", 0))
        aid = ids[k]
        cp = f"{aid}:{state['execution_id']}"
        return {"current_agent": aid, "hitl_checkpoint_ref": cp}

    async def human_review_node(state: WorkflowState) -> dict[str, Any]:
        outs0 = dict(state["agent_outputs"])
        ids = outs0["__ordered_ids__"]
        k = int(outs0.get("__next_execution_index__", 0))
        aid = ids[k]
        cp = state.get("hitl_checkpoint_ref") or f"{aid}:{state['execution_id']}"
        payload = {"agent_id": aid, "checkpoint_id": cp, "execution_id": state["execution_id"]}
        verdict = interrupt(payload)
        if not isinstance(verdict, dict):
            verdict = {"approved": False, "feedback": None}
        approved = bool(verdict.get("approved"))
        feedback = verdict.get("feedback")
        outs = dict(outs0)
        if approved:
            appr = list(outs.get("__hitl_approved_indices__") or [])
            if k not in appr:
                appr.append(k)
            outs["__hitl_approved_indices__"] = appr
        fb_str = feedback if isinstance(feedback, str) else None
        return {
            "human_feedback": fb_str or json.dumps(verdict),
            "agent_outputs": outs,
            "hitl_checkpoint_ref": cp if not approved else None,
        }

    def route_post_hitl(state: WorkflowState) -> str:
        outs = state["agent_outputs"]
        k = int(outs.get("__next_execution_index__", 0))
        appr = set(outs.get("__hitl_approved_indices__") or [])
        if k in appr:
            return "execute_agent"
        return "cancel_end"

    async def execute_agent_node(state: WorkflowState) -> dict[str, Any]:
        if state["iteration_count"] >= state["max_iterations"]:
            return {"error": "max_iterations_exceeded"}

        outs0 = dict(state["agent_outputs"])
        ids = outs0["__ordered_ids__"]
        k = int(outs0.get("__next_execution_index__", 0))
        aid = ids[k]

        prior: dict[str, str] = {}
        for ix in range(0, k):
            pid = ids[ix]
            txt = outs0.get(pid)
            if isinstance(txt, str):
                prior[pid] = txt

        wf_stage = dynamiq.hydrate_agent_stage(
            definition,
            focus_id=aid,
            prior_outputs=prior,
            agent_tools_by_id=agent_tools_by_id,
        )

        streamed: Any = None
        err_msg: str | None = None
        try:
            async for ev in dynamiq.run_workflow_stream(wf_stage, input_data=dynamiq_input):
                et = ev.get("type")
                if et == "workflow_start":
                    continue
                if et == "workflow_complete":
                    streamed = ev.get("result")
                    if not ev.get("success", True):
                        err_msg = ev.get("message") or "stage_failure"
                    continue
                await _emit(ev)
                if et == "error":
                    err_msg = str(ev.get("message") or "stage_failure")
        except Exception as exc:  # noqa: BLE001
            log.exception("langgraph.hitl.execute_agent.failed", agent_id=aid)
            err_msg = str(exc)

        if err_msg:
            return {"error": err_msg}

        blob = _agent_blob(streamed if isinstance(streamed, dict) else None, aid)
        outs = dict(outs0)
        outs[aid] = blob
        outs["__next_execution_index__"] = k + 1

        await _emit(
            {
                "type": "agent_complete",
                "agent_id": aid,
                "agent_name": next((a.name for a in definition.agents if a.id == aid), aid),
                "content": blob,
            }
        )
        return {
            "agent_outputs": outs,
            "iteration_count": state["iteration_count"] + 1,
            "current_agent": aid,
            "hitl_checkpoint_ref": None,
            "error": None,
        }

    async def finalize_node(state: WorkflowState) -> dict[str, Any]:
        _ = state
        return {"hitl_checkpoint_ref": None}

    async def cancel_end_node(state: WorkflowState) -> dict[str, Any]:
        return {"error": state.get("error") or "hitl_rejected"}

    async def error_finalize_node(state: WorkflowState) -> dict[str, Any]:
        return {"error": state.get("error") or "fatal"}

    builder = StateGraph(WorkflowState)
    builder.add_node("plan", plan_node)
    builder.add_node("hitl_prep", hitl_prep_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("execute_agent", execute_agent_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("cancel_end", cancel_end_node)
    builder.add_node("error_finalize", error_finalize_node)

    builder.add_edge(START, "plan")
    mapping = {
        "finalize": "finalize",
        "hitl_prep": "hitl_prep",
        "execute_agent": "execute_agent",
        "error_finalize": "error_finalize",
    }
    builder.add_conditional_edges("plan", route_next, mapping)
    builder.add_edge("hitl_prep", "human_review")
    builder.add_conditional_edges(
        "human_review",
        route_post_hitl,
        {"execute_agent": "execute_agent", "cancel_end": "cancel_end"},
    )
    builder.add_conditional_edges("execute_agent", route_next, mapping)

    builder.add_edge("finalize", END)
    builder.add_edge("cancel_end", END)
    builder.add_edge("error_finalize", END)

    return builder


__all__ = ["build_hitl_graph"]
