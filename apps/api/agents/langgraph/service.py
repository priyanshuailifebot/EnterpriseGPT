"""LangGraph orchestration — HITL + Dynamiq streams, dialog, checkpoint reads."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast
from uuid import UUID

import httpx
from langchain_core.messages import HumanMessage, message_to_dict
from langgraph.types import Command

from agents.langgraph.clarification_graph import (
    build_clarification_graph,
    initial_workflow_scoping_payload,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.dynamiq_service import DynamiqService
from agents.langgraph.checkpointer import get_checkpointer
from agents.langgraph.dialog_graph import build_dialog_graph
from agents.langgraph.hitl_graph import build_hitl_graph
from agents.langgraph.state import DialogState, WorkflowState
from core.config import Settings
from models.help_request import HelpRequest
from models.workflow import Workflow as WFRow
from models.workflow_execution import WorkflowExecution, WorkflowExecutionStatus
from schemas.workflow import AgentDefinition, WorkflowDefinition
from services.clarification_exceptions import ClarificationSessionNotFoundError
from services.workflow_interpreter import WorkflowInterpreter

log = logging.getLogger(__name__)


def _interrupt_payload(chunk: dict[str, Any]) -> dict[str, Any] | None:
    raw = chunk.get("__interrupt__")
    if not raw:
        return None
    try:
        intr = raw[0]
        val = getattr(intr, "value", None)
    except (IndexError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _serialize_lc_messages(msgs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in msgs or []:
        try:
            out.append(message_to_dict(m))
        except (TypeError, ValueError):  # pragma: no cover
            out.append({"type": "unknown", "content": str(m)})
    return out


def _probe_definition() -> WorkflowDefinition:
    """Structure matches all HITL builds so ``aget_state`` can load checkpoints."""
    return WorkflowDefinition(
        name="_checkpoint_probe_",
        agents=[
            AgentDefinition(
                id="probe",
                name="Probe",
                role="",
                instructions="",
                tools=[],
                depends_on=[],
            )
        ],
        human_checkpoints=[],
    )


class LangGraphService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream_hitl_with_dynamiq(
        self,
        *,
        definition: WorkflowDefinition,
        dynamiq: DynamiqService,
        dynamiq_input: dict[str, Any],
        workflow_id: UUID,
        execution_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        max_iterations: int = 10,
        poll_approval: Callable[[], Awaitable[dict[str, Any]]],
        agent_tools_by_id: dict[str, list[Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        checkpointer = await get_checkpointer(self.settings)
        tid = str(execution_id)
        side_channel: deque[dict[str, Any]] = deque()

        async def push_event(ev: dict[str, Any]) -> None:
            side_channel.append(ev)

        graph = build_hitl_graph(
            definition,
            dynamiq,
            dynamiq_input=dynamiq_input,
            push_event=push_event,
            agent_tools_by_id=agent_tools_by_id,
        ).compile(checkpointer=checkpointer)

        init: WorkflowState = {
            "messages": [],
            "current_agent": "",
            "agent_outputs": {},
            "tool_calls": [],
            "human_feedback": None,
            "hitl_checkpoint_ref": None,
            "error": None,
            "iteration_count": 0,
            "max_iterations": max_iterations,
            "execution_id": tid,
            "user_id": str(user_id),
            "workspace_id": str(workspace_id),
            "confidence": 1.0,
        }

        cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}
        cmd_in: Any = init

        while True:
            saw_interrupt = False
            async for chunk in graph.astream(cmd_in, cfg):
                chunk_d = cast(dict[str, Any], chunk)
                while side_channel:
                    yield side_channel.popleft()

                ip = _interrupt_payload(chunk_d)
                if ip is not None:
                    yield {
                        "type": "hitl_required",
                        "agent_id": ip.get("agent_id"),
                        "checkpoint_id": ip.get("checkpoint_id"),
                        "execution_id": tid,
                        "workflow_id": str(workflow_id),
                        "thread_id": tid,
                        "message": "Human approval required before continuing.",
                    }
                    verdict_raw = await poll_approval()
                    cmd_in = Command(
                        resume={
                            "approved": bool(verdict_raw.get("approved")),
                            "feedback": verdict_raw.get("feedback"),
                        }
                    )
                    saw_interrupt = True
                    break

            while side_channel:
                yield side_channel.popleft()

            if saw_interrupt:
                continue

            snap = await graph.aget_state(cfg)
            vals_raw = getattr(snap, "values", None)
            vals = vals_raw if isinstance(vals_raw, dict) else {}

            err = vals.get("error") if vals else None
            outs_raw = vals.get("agent_outputs") if vals else None
            agent_out: dict[str, Any] = {}
            if isinstance(outs_raw, dict):
                agent_out = {k: v for k, v in outs_raw.items() if not str(k).startswith("__")}

            if err:
                yield {"type": "error", "message": str(err)}
            else:
                yield {
                    "type": "workflow_complete",
                    "success": True,
                    "execution_id": tid,
                    "result": {"agent_outputs": agent_out},
                }
            return

    async def get_checkpoint_state(self, thread_id: str) -> dict[str, Any] | None:
        cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        checkpointer = await get_checkpointer(self.settings)
        dynamiq = DynamiqService(self.settings)

        probe = build_hitl_graph(
            _probe_definition(),
            dynamiq,
            dynamiq_input={"input": "{}"},
            push_event=None,
        ).compile(checkpointer=checkpointer)
        snap = await probe.aget_state(cfg)
        vals = getattr(snap, "values", None)
        if not vals:
            return None
        out = dict(vals)
        if "messages" in out:
            out["messages"] = _serialize_lc_messages(out["messages"])
        return out

    async def list_pending_hitl(self, db: AsyncSession, *, workspace_id: UUID) -> list[dict[str, Any]]:
        stmt = (
            select(WorkflowExecution)
            .join(WFRow, WorkflowExecution.workflow_id == WFRow.id)
            .where(
                WFRow.workspace_id == workspace_id,
                WorkflowExecution.status == WorkflowExecutionStatus.HITL_WAITING,
            )
        )
        rows = list((await db.execute(stmt)).scalars().all())
        return [
            {
                "execution_id": str(r.id),
                "workflow_id": str(r.workflow_id),
                "status": r.status.value,
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
            for r in rows
        ]

    @staticmethod
    def workflow_scoping_thread_id(session_id: str) -> str:
        """Isolate clarification checkpoints from other dialog graphs sharing Redis."""
        return f"ws_scope:{session_id}"

    async def _compiled_workflow_clarification_graph(self) -> Any:
        checkpointer = await get_checkpointer(self.settings)
        interpreter = WorkflowInterpreter(self.settings)
        return build_clarification_graph(self.settings, interpreter).compile(
            checkpointer=checkpointer
        )

    async def workflow_scoping_get_meta(self, session_id: str) -> tuple[str, str] | None:
        graph = await self._compiled_workflow_clarification_graph()
        cfg: dict[str, Any] = {"configurable": {"thread_id": self.workflow_scoping_thread_id(session_id)}}
        snap = await graph.aget_state(cfg)
        vals = getattr(snap, "values", None)
        if not isinstance(vals, dict) or str(vals.get("session_id", "")) != session_id:
            return None
        ws = vals.get("workspace_id")
        uid = vals.get("user_id")
        if ws is None or uid is None:
            return None
        return str(ws), str(uid)

    async def invoke_workflow_clarification(
        self,
        human_message: HumanMessage,
        *,
        session_id: str,
        workspace_id: UUID | None,
        user_id: UUID | None,
        available_tools: list[str],
        is_new_analysis: bool,
        original_prompt: str | None,
    ) -> dict[str, Any]:
        graph = await self._compiled_workflow_clarification_graph()
        cfg: dict[str, Any] = {"configurable": {"thread_id": self.workflow_scoping_thread_id(session_id)}}
        snap = await graph.aget_state(cfg)
        prev = getattr(snap, "values", None)

        if is_new_analysis:
            if workspace_id is None or user_id is None or not (original_prompt or "").strip():
                raise ValueError(
                    "workspace_id, user_id, and original_prompt are required for new clarification"
                )
            base: dict[str, Any] = initial_workflow_scoping_payload(
                session_id=session_id,
                workspace_id=str(workspace_id),
                user_id=str(user_id),
                original_prompt=(original_prompt or "").strip(),
                available_tools=list(available_tools),
                settings=self.settings,
            )
        else:
            if not isinstance(prev, dict) or str(prev.get("session_id", "")) != session_id:
                raise ClarificationSessionNotFoundError(session_id)
            base = dict(prev)

        incoming: dict[str, Any] = dict(base)
        incoming["available_tools"] = list(available_tools)
        incoming["max_rounds"] = self.settings.CLARIFICATION_MAX_ROUNDS
        incoming["confidence_threshold"] = self.settings.CLARIFICATION_CONFIDENCE_THRESHOLD
        incoming["preview_before_ready"] = self.settings.CLARIFICATION_PREVIEW_BEFORE_READY
        incoming["messages"] = [human_message]

        await graph.ainvoke(incoming, cfg)
        snap2 = await graph.aget_state(cfg)
        vals_raw = getattr(snap2, "values", None)
        api = vals_raw.get("clarification_api") if isinstance(vals_raw, dict) else None
        if not isinstance(api, dict) or not api.get("kind"):
            raise RuntimeError("missing clarification_api after workflow scoping invoke")

        out: dict[str, Any] = {"kind": api.get("kind")}
        for key in (
            "session_id",
            "questions",
            "round_number",
            "original_prompt",
            "augmented_prompt",
            "rounds_used",
            "message",
        ):
            if key in api:
                out[key] = api[key]
        return out

    async def run_dialog_turn(
        self,
        *,
        db: AsyncSession,
        session_id: str,
        user_message: str,
        workspace_id: UUID | None,
    ) -> AsyncIterator[dict[str, Any]]:
        checkpointer = await get_checkpointer(self.settings)

        async def on_escalate(payload: dict[str, Any]) -> None:
            row = HelpRequest(
                workspace_id=workspace_id,
                session_id=str(payload.get("session_id") or session_id),
                reason=str(payload.get("reason") or "escalation"),
                payload_json=dict(payload),
            )
            db.add(row)
            await db.commit()

            url = (self.settings.HELP_ESCALATION_WEBHOOK_URL or "").strip()
            if url:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        await client.post(url, json=payload)
                except Exception as exc:  # noqa: BLE001
                    log.warning("help_escalation.webhook_failed", error=str(exc))

        graph = build_dialog_graph(self.settings, on_escalate=on_escalate).compile(
            checkpointer=checkpointer
        )
        cfg = {"configurable": {"thread_id": session_id}}
        snap = await graph.aget_state(cfg)
        seed = getattr(snap, "values", None) if snap else None
        merged: dict[str, Any] = dict(seed) if isinstance(seed, dict) else {}

        payload: DialogState = {
            "messages": [HumanMessage(content=user_message)],
            "detected_intent": merged.get("detected_intent"),
            "required_slots": list(merged.get("required_slots") or []),
            "filled_slots": dict(merged.get("filled_slots") or {}),
            "confirmation_pending": bool(merged.get("confirmation_pending", False)),
            "session_id": session_id,
            "escalation_count": int(merged.get("escalation_count") or 0),
            "last_activity": merged.get("last_activity"),
            "dialogue_phase": merged.get("dialogue_phase") or "greeting",
        }
        if workspace_id:
            payload["workspace_id"] = str(workspace_id)

        update_raw = await graph.ainvoke(payload, cfg)
        update = update_raw if isinstance(update_raw, dict) else {}

        yield {
            "type": "dialog_turn",
            "session_id": session_id,
            "phase": update.get("dialogue_phase"),
            "confirmation_pending": bool(update.get("confirmation_pending", False)),
            "messages_tail": _serialize_lc_messages(update.get("messages"))[-12:],
        }


__all__ = ["LangGraphService"]
